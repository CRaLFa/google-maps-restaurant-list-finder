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
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"cloud.google.com/go/firestore"
	recaptcha "cloud.google.com/go/recaptchaenterprise/v2/apiv1"
	recaptchapb "cloud.google.com/go/recaptchaenterprise/v2/apiv1/recaptchaenterprisepb"
	"google.golang.org/api/iterator"
)

//go:embed all:web
var webFS embed.FS

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

var prefectures = map[string]bool{}

func init() {
	for p := range strings.FieldsSeq(`北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県
		茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 新潟県 富山県 石川県 福井県
		山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 奈良県
		和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県
		佐賀県 長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県`) {
		prefectures[p] = true
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
	ctx := context.Background()
	project := os.Getenv("GOOGLE_CLOUD_PROJECT")
	if project == "" {
		log.Fatal("GOOGLE_CLOUD_PROJECT が未設定")
	}
	fsClient, err := firestore.NewClient(ctx, project)
	if err != nil {
		log.Fatalf("Firestore クライアントの生成に失敗: %v", err)
	}
	defer fsClient.Close()

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
func (s *server) handleConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]string{
		"recaptchaSiteKey": s.siteKey,
		"mapsApiKey":       s.mapsKey,
		"mapId":            s.mapID,
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

// lists 全件をプロセス内にキャッシュする。571 件で数十 KB なのでページングしない。
func (s *server) cachedLists(ctx context.Context) ([]byte, string, error) {
	s.lists.mu.Lock()
	defer s.lists.mu.Unlock()
	if s.lists.body != nil && time.Since(s.lists.at) < listsTTL {
		return s.lists.body, s.lists.etag, nil
	}
	out := []map[string]any{}
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
	}
	body, err := json.Marshal(out)
	if err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(body)
	s.lists.body, s.lists.etag, s.lists.at = body, `"`+hex.EncodeToString(sum[:8])+`"`, time.Now()
	return s.lists.body, s.lists.etag, nil
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
	if !prefectures[req.Pref] {
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

// IP 単位のレート制限。プロセス内カウンタなのでインスタンス数倍まで緩む。
// ponytail: --max-instances を小さく保つ前提。効かなくなったら Firestore のカウンタへ。
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
