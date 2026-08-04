// レストランリスト検索 Web アプリのサーバ。
// 静的ファイルの配信と API を 1 サービスで賄う。
// 静的ファイルは go:embed でバイナリに埋め込むので、成果物はバイナリ 1 個だけ。
package main

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"cloud.google.com/go/firestore"
	recaptcha "cloud.google.com/go/recaptchaenterprise/v2/apiv1"
	recaptchapb "cloud.google.com/go/recaptchaenterprise/v2/apiv1/recaptchaenterprisepb"
	"github.com/joho/godotenv"
	"google.golang.org/api/iterator"
)

//go:embed all:web
var webFS embed.FS

// 全国地方公共団体コードと「都道府県 + 市区町村」のフルパスを、コード順にタブ区切りで並べたもの。
// 出典: 総務省「都道府県コード及び市区町村コード」 (https://www.soumu.go.jp/denshijiti/code.html) の Excel。
// 都道府県名もツリーの並び順もここだけから作るので、静的な地名データの持ち場はこのファイル 1 つ。
// 市町村合併があったら Excel から作り直すこと。
// 54KB あるが、ブラウザに送るのはリストが実在するパスの分だけ (cachedLists の order)。
//
//go:embed muni-order.txt
var muniOrderData string

const (
	listsTTL     = 5 * time.Minute // lists のプロセス内キャッシュの寿命
	reportWindow = time.Hour       // レート制限の集計窓
	reportLimit  = 5               // 1 IP あたり窓内で受け付ける報告数
	maxBodyBytes = 8 << 10
)

// 報告フォームの各項目の最大文字数 (rune 数)。
var maxLen = map[string]int{
	"area": 50, "pref": 10, "shareUrl": 300, "comment": 1000, "contact": 200,
}

// 47 都道府県。コード順。報告フォームの選択肢と検証に使う。
var prefectures []string

// ツリーのノードのフルパス -> 全国地方公共団体コード。
// 比べるのは兄弟どうしだけなので、コードをそのまま並び順に使える。
var muniRank = map[string]int{}

func init() {
	for line := range strings.SplitSeq(strings.TrimSpace(muniOrderData), "\n") {
		code, path, _ := strings.Cut(line, "\t")
		n, err := strconv.Atoi(code)
		if err != nil {
			panic("muni-order.txt の団体コードが数値でない: " + line)
		}
		// 都道府県は市区町村コード (5 桁のうち下 3 桁) が 000 の行。
		if code[2:5] == "000" {
			prefectures = append(prefectures, path)
		}
		// 北海道泊村のように同名の自治体が同じ都道府県に 2 つあるとパスが衝突する。
		// どちらもフルパスでは見分けられないので、コードの小さい方を採る。
		if _, dup := muniRank[path]; !dup {
			muniRank[path] = n
		}
	}
	if len(prefectures) != 47 {
		panic(fmt.Sprintf("muni-order.txt から取れた都道府県が %d 件", len(prefectures)))
	}
}

type server struct {
	fs        *firestore.Client
	recaptcha *recaptcha.Client
	project   string
	siteKey   string
	mapsKey   string
	mapID     string
	minScore  float64

	lists     listsCache
	rateMu    sync.Mutex
	rateSeen  map[string][]time.Time
	rateSweep time.Time
}

type listsCache struct {
	mu   sync.Mutex
	body []byte
	etag string
	at   time.Time
}

func main() {
	// .env があれば読み込む。ローカル開発で毎回 export しなくて済むようにするためのもの。
	// godotenv は既存の環境変数を上書きしないので、Cloud Run の --set-env-vars が常に勝つ。
	// ファイルが無いのが通常の状態 (本番) なのでエラーは無視する。
	_ = godotenv.Load()

	ctx := context.Background()
	project := os.Getenv("GOOGLE_CLOUD_PROJECT")
	if project == "" {
		log.Fatal("GOOGLE_CLOUD_PROJECT が未設定")
	}
	// 別用途のデータベースを同じプロジェクトに足せるよう、名前付きデータベースに置いている。
	// 未設定なら (default) を指すが、このプロジェクトの (default) は移行時に削除済み。
	database := os.Getenv("FIRESTORE_DATABASE")
	if database == "" {
		database = "(default)"
	}
	fsClient, err := firestore.NewClientWithDatabase(ctx, project, database)
	if err != nil {
		log.Fatalf("Firestore クライアントの生成に失敗: %v", err)
	}
	defer fsClient.Close()
	log.Printf("Firestore: project=%s database=%s", project, database)

	s := &server{
		fs:       fsClient,
		project:  project,
		siteKey:  os.Getenv("RECAPTCHA_SITE_KEY"),
		mapsKey:  os.Getenv("MAPS_API_KEY"),
		mapID:    os.Getenv("MAPS_MAP_ID"),
		minScore: 0.5,
		rateSeen: map[string][]time.Time{},
	}
	if s.mapsKey == "" {
		log.Print("警告: MAPS_API_KEY が未設定のため地図を表示できない")
	}
	if s.mapID == "" {
		// AdvancedMarkerElement には Map ID が要る。
		// DEMO_MAP_ID は開発用で、本番はコンソールで作った ID を設定すること。
		s.mapID = "DEMO_MAP_ID"
	}
	if s.siteKey == "" {
		// ローカル開発用の逃げ道。本番では必ずサイトキーを設定すること。
		log.Print("警告: RECAPTCHA_SITE_KEY が未設定のため報告の bot 検証を行わない")
	} else if s.recaptcha, err = recaptcha.NewClient(ctx); err != nil {
		log.Fatalf("reCAPTCHA クライアントの生成に失敗: %v", err)
	}

	static, err := fs.Sub(webFS, "web")
	if err != nil {
		log.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/lists", s.handleLists)
	mux.HandleFunc("POST /api/reports", s.handleReport)
	mux.HandleFunc("GET /api/config", s.handleConfig)
	mux.Handle("/", http.FileServerFS(static))

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	log.Printf("listening on :%s", port)
	log.Fatal((&http.Server{
		Addr:              ":" + port,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}).ListenAndServe())
}

// フロントに渡す公開設定。
// reCAPTCHA のサイトキーも Maps の API キーもクライアントに出る前提の値なのでそのまま返す。
// Maps の API キーは GCP コンソール側で HTTP リファラ制限をかけて守ること。
// 報告フォームの都道府県の選択肢もここで配り、フロントに同じ一覧を持たせない。
func (s *server) handleConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{
		"recaptchaSiteKey": s.siteKey,
		"mapsApiKey":       s.mapsKey,
		"mapId":            s.mapID,
		"prefectures":      prefectures,
	})
}

func (s *server) handleLists(w http.ResponseWriter, r *http.Request) {
	body, etag, err := s.cachedLists(r.Context())
	if err != nil {
		log.Printf("lists の読み取りに失敗: %v", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Cache-Control", "public, max-age=300")
	w.Header().Set("ETag", etag)
	if r.Header.Get("If-None-Match") == etag {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Write(body)
}

// lists 全件をプロセス内にキャッシュする。全件で数十 KB なのでページングしない。
func (s *server) cachedLists(ctx context.Context) ([]byte, string, error) {
	s.lists.mu.Lock()
	defer s.lists.mu.Unlock()
	if s.lists.body != nil && time.Since(s.lists.at) < listsTTL {
		return s.lists.body, s.lists.etag, nil
	}
	out := []map[string]any{}
	order := map[string]int{}
	it := s.fs.Collection("lists").Documents(ctx)
	defer it.Stop()
	for {
		doc, err := it.Next()
		if errors.Is(err, iterator.Done) {
			break
		}
		if err != nil {
			return nil, "", err
		}
		d := doc.Data()
		// updatedAt はフロントで使わないので落として転送量を削る。
		delete(d, "updatedAt")
		out = append(out, d)
		loc, _ := d["loc"].(string)
		area, _ := d["area"].(string)
		collectRanks(order, loc+area)
	}
	body, err := json.Marshal(map[string]any{"lists": out, "order": order})
	if err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(body)
	s.lists.body, s.lists.etag, s.lists.at = body, `"`+hex.EncodeToString(sum[:8])+`"`, time.Now()
	return s.lists.body, s.lists.etag, nil
}

// path とその全階層のうち、都道府県・市区町村に当たるものの並び順を order に集める。
// path (所在地 + エリア名) は「都道府県 + 市 + 区 + エリア」の連結なので、
// 前方から 1 文字ずつ切り出して表を引けば、リストを持たない中間ノードの分まで拾える。
// 表に無い繁華街などのエリアは載らず、フロント側で従来どおり緯度順に並ぶ。
func collectRanks(order map[string]int, path string) {
	for i := range path {
		if r, ok := muniRank[path[:i]]; ok {
			order[path[:i]] = r
		}
	}
	if r, ok := muniRank[path]; ok {
		order[path] = r
	}
}

type reportReq struct {
	Area     string `json:"area"`
	Pref     string `json:"pref"`
	ShareURL string `json:"shareUrl"`
	Comment  string `json:"comment"`
	Contact  string `json:"contact"`
	Token    string `json:"token"`
}

func (s *server) handleReport(w http.ResponseWriter, r *http.Request) {
	var req reportReq
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		http.Error(w, "body too large", http.StatusRequestEntityTooLarge)
		return
	}
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	if msg := validate(&req); msg != "" {
		http.Error(w, msg, http.StatusBadRequest)
		return
	}

	score, err := s.assess(r.Context(), req.Token)
	if err != nil {
		log.Printf("reCAPTCHA の検証に失敗: %v", err)
		http.Error(w, "verification failed", http.StatusForbidden)
		return
	}
	if score < s.minScore {
		log.Printf("reCAPTCHA スコア不足により拒否: %.2f", score)
		http.Error(w, "verification failed", http.StatusForbidden)
		return
	}

	if !s.allow(clientIP(r)) {
		http.Error(w, "too many requests", http.StatusTooManyRequests)
		return
	}

	// contact / comment は個人情報を含みうるのでログに中身を出さない。
	ref, _, err := s.fs.Collection("reports").Add(r.Context(), map[string]any{
		"area": req.Area, "pref": req.Pref, "shareUrl": req.ShareURL,
		"comment": req.Comment, "contact": req.Contact,
		"status": "new", "createdAt": firestore.ServerTimestamp,
		"recaptchaScore": score,
	})
	if err != nil {
		log.Printf("reports への書き込みに失敗: %v", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	log.Printf("報告を受理: %s", ref.ID)
	writeJSON(w, map[string]bool{"ok": true})
}

func validate(req *reportReq) string {
	req.Area = strings.TrimSpace(req.Area)
	req.Pref = strings.TrimSpace(req.Pref)
	req.ShareURL = strings.TrimSpace(req.ShareURL)
	if req.Area == "" {
		return "エリア名は必須です"
	}
	if !slices.Contains(prefectures, req.Pref) {
		return "都道府県が不正です"
	}
	for name, v := range map[string]string{
		"area": req.Area, "pref": req.Pref, "shareUrl": req.ShareURL,
		"comment": req.Comment, "contact": req.Contact,
	} {
		if len([]rune(v)) > maxLen[name] {
			return name + " が長すぎます"
		}
	}
	if req.ShareURL != "" &&
		!strings.HasPrefix(req.ShareURL, "https://maps.app.goo.gl/") &&
		!strings.HasPrefix(req.ShareURL, "https://www.google.com/maps/") {
		return "共有 URL は Google マップの URL を指定してください"
	}
	return ""
}

// reCAPTCHA Enterprise でトークンを評価する。サイトキー未設定なら検証を省く。
func (s *server) assess(ctx context.Context, token string) (float64, error) {
	if s.recaptcha == nil {
		return 1, nil
	}
	if token == "" {
		return 0, errors.New("トークンが空")
	}
	res, err := s.recaptcha.CreateAssessment(ctx, &recaptchapb.CreateAssessmentRequest{
		Parent: "projects/" + s.project,
		Assessment: &recaptchapb.Assessment{
			Event: &recaptchapb.Event{Token: token, SiteKey: s.siteKey},
		},
	})
	if err != nil {
		return 0, err
	}
	if p := res.GetTokenProperties(); !p.GetValid() {
		return 0, errors.New("トークンが無効: " + p.GetInvalidReason().String())
	}
	return float64(res.GetRiskAnalysis().GetScore()), nil
}

// IP 単位のレート制限。
// プロセス内カウンタなので、インスタンスが増えると上限が実質インスタンス数倍まで緩む。
// --max-instances を小さく保つ前提での割り切りで、効かなくなったら Firestore のカウンタへ移す。
func (s *server) allow(ip string) bool {
	now := time.Now()
	s.rateMu.Lock()
	defer s.rateMu.Unlock()
	// 窓を跨いだエントリを間引く。全 IP の掃除は窓ごとに 1 回で足りる。
	if now.Sub(s.rateSweep) > reportWindow {
		for k, ts := range s.rateSeen {
			if len(ts) == 0 || now.Sub(ts[len(ts)-1]) > reportWindow {
				delete(s.rateSeen, k)
			}
		}
		s.rateSweep = now
	}
	var kept []time.Time
	for _, t := range s.rateSeen[ip] {
		if now.Sub(t) <= reportWindow {
			kept = append(kept, t)
		}
	}
	if len(kept) >= reportLimit {
		s.rateSeen[ip] = kept
		return false
	}
	s.rateSeen[ip] = append(kept, now)
	return true
}

// Cloud Run は X-Forwarded-For の先頭にクライアント IP を入れる。
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		return strings.TrimSpace(strings.Split(xff, ",")[0])
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	json.NewEncoder(w).Encode(v)
}
