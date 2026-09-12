package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

// ふりがな (rPh) を本文と一緒に取り出すと、区名が「相模原市緑区サガミハラシミドリク」のようになり
// 「区」で終わらなくなるため、政令市の区が丸ごと表から抜け落ちていた。
// 混入そのものは init の接尾辞チェックが弾くが、抜け落ちは件数を見ないと気付けない。
func TestMuniOrderData(t *testing.T) {
	for _, p := range []string{
		"神奈川県相模原市緑区", "静岡県浜松市浜名区", "熊本県熊本市中央区", "岩手県滝沢市",
	} {
		if _, ok := muniRank[p]; !ok {
			t.Errorf("%s が muni-order.txt に無い", p)
		}
	}
	// 1,741 市区町村 + 175 政令市の区 + 47 都道府県 から、同名パスで潰れる分を引いた数。
	if len(muniRank) < 1900 {
		t.Errorf("muni-order.txt から取れたパスが %d 件しかない", len(muniRank))
	}
}

func TestCollectRanks(t *testing.T) {
	order := map[string]int{}
	// 栄 (愛知県名古屋市中区) はリストを持たない中間ノード 愛知県名古屋市 を経由する。
	collectRanks(order, "愛知県名古屋市中区栄")
	for _, p := range []string{"愛知県", "愛知県名古屋市", "愛知県名古屋市中区"} {
		if _, ok := order[p]; !ok {
			t.Errorf("%s の並び順が拾えていない", p)
		}
	}
	if _, ok := order["愛知県名古屋市中区栄"]; ok {
		t.Error("自治体でない 栄 に並び順が付いた")
	}
	if order["愛知県"] >= order["愛知県名古屋市"] {
		t.Error("都道府県が市区町村より後ろに並んでいる")
	}
	// 同じ都道府県の中ではコード順 (千種区 23101 < 中区 23106)。
	collectRanks(order, "愛知県名古屋市千種区")
	if order["愛知県名古屋市千種区"] >= order["愛知県名古屋市中区"] {
		t.Error("千種区が中区より後ろに並んでいる")
	}
}

func TestValidate(t *testing.T) {
	ok := reportReq{Area: "川越市", Pref: "埼玉県"}
	if msg := validate(&ok); msg != "" {
		t.Fatalf("必須項目だけの報告が弾かれた: %s", msg)
	}

	bad := []struct {
		name string
		req  reportReq
	}{
		{"エリア名が空", reportReq{Pref: "埼玉県"}},
		{"エリア名が空白のみ", reportReq{Area: "  ", Pref: "埼玉県"}},
		{"都道府県が空", reportReq{Area: "川越市"}},
		{"都道府県が実在しない", reportReq{Area: "川越市", Pref: "埼玉"}},
		{"エリア名が長すぎる", reportReq{Area: strings.Repeat("あ", 51), Pref: "埼玉県"}},
		{"コメントが長すぎる", reportReq{Area: "川越市", Pref: "埼玉県", Comment: strings.Repeat("あ", 1001)}},
		{"共有 URL が別ドメイン", reportReq{Area: "川越市", Pref: "埼玉県", ShareURL: "https://example.com/x"}},
		{"共有 URL が http", reportReq{Area: "川越市", Pref: "埼玉県", ShareURL: "http://maps.app.goo.gl/x"}},
	}
	for _, c := range bad {
		req := c.req
		if validate(&req) == "" {
			t.Errorf("%s が通ってしまった: %+v", c.name, c.req)
		}
	}

	// 許可されている 2 種類の共有 URL は通す。
	for _, u := range []string{
		"https://maps.app.goo.gl/abc", "https://www.google.com/maps/@1,2,3z",
	} {
		req := reportReq{Area: "川越市", Pref: "埼玉県", ShareURL: u}
		if msg := validate(&req); msg != "" {
			t.Errorf("共有 URL %s が弾かれた: %s", u, msg)
		}
	}
}

func TestAllow(t *testing.T) {
	s := &server{rateSeen: map[string][]time.Time{}}
	for i := range reportLimit {
		if !s.allow("1.2.3.4") {
			t.Fatalf("上限内の %d 回目が拒否された", i+1)
		}
	}
	if s.allow("1.2.3.4") {
		t.Error("上限を超えた分が拒否されていない")
	}
	if !s.allow("5.6.7.8") {
		t.Error("別の IP まで巻き込んで拒否している")
	}

	// 窓を跨いだ記録は数えない。
	s.rateSeen["1.2.3.4"] = []time.Time{time.Now().Add(-2 * reportWindow)}
	if !s.allow("1.2.3.4") {
		t.Error("窓を過ぎた記録が残り続けている")
	}
}

func TestClientIP(t *testing.T) {
	r := &http.Request{Header: http.Header{}, RemoteAddr: "10.0.0.1:5555"}
	if got := clientIP(r); got != "10.0.0.1" {
		t.Errorf("RemoteAddr からの IP が %q", got)
	}
	r.Header.Set("X-Forwarded-For", "203.0.113.9, 10.0.0.1")
	if got := clientIP(r); got != "203.0.113.9" {
		t.Errorf("X-Forwarded-For 先頭の IP が %q", got)
	}
}

// 通知の本文に載せてよいのはドキュメント ID と公開情報だけで、
// comment と contact は notify の引数に無い (渡しようがない) ことで担保している。
// ここで見るのは、宛先が未設定なら送らないことと、エリア名の記号が Slack 用にエスケープされること。
func TestNotify(t *testing.T) {
	var hits int
	var payload struct {
		Text string `json:"text"`
	}
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		b, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(b, &payload); err != nil {
			t.Errorf("通知の本文が JSON ではない: %v", err)
		}
	}))
	defer ts.Close()

	(&server{}).notify("doc1", "東京都", "渋谷")
	if hits != 0 {
		t.Errorf("SLACK_WEBHOOK_URL が空なのに %d 回送信した", hits)
	}

	s := &server{webhookURL: ts.URL, project: "proj", database: "db"}
	s.notify("doc1", "東京都", "<渋谷> & 新宿")
	if hits != 1 {
		t.Fatalf("送信回数が %d", hits)
	}
	for _, want := range []string{"doc1", "東京都", "&lt;渋谷&gt; &amp; 新宿", "proj", "db"} {
		if !strings.Contains(payload.Text, want) {
			t.Errorf("通知の本文に %q が無い: %s", want, payload.Text)
		}
	}
	if strings.Contains(payload.Text, "<渋谷>") {
		t.Errorf("エリア名がエスケープされていない: %s", payload.Text)
	}
}

// 通知に失敗したときのエラーには宛先の URL がそのまま入る (url.Error の仕様)。
// Webhook URL は Secret Manager に置いている値なので、素通しするとログから読めてしまう。
func TestNotifyHidesWebhookURL(t *testing.T) {
	// 閉じたサーバに投げて送信エラーを起こす。
	ts := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := ts.URL + "/services/T0/B0/SHOULD_NOT_APPEAR"
	ts.Close()

	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	(&server{webhookURL: url}).notify("doc1", "東京都", "渋谷")

	if !strings.Contains(buf.String(), "通知に失敗") {
		t.Fatalf("送信の失敗がログに出ていない: %s", buf.String())
	}
	if strings.Contains(buf.String(), "SHOULD_NOT_APPEAR") {
		t.Errorf("Webhook URL がログに漏れている: %s", buf.String())
	}
}
