package main

import (
	"net/http"
	"strings"
	"testing"
	"time"
)

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
