#!/usr/bin/env python3
"""share-urls.tsv に欠けているエリア別リストの共有 URL を取得する。

fetch_share_urls.py は端末にフォロー済みのリストしか辿れないため、
未フォローのリスト (「〇〇: トレンド」等) は取得できない。
本スクリプトはエリア名で検索し、エリアページ下部のカルーセルから
目的のリストを開いて共有 URL を取得する。

**フォローするのはトップリストのみ。**
トレンド / 地元で人気 は共有 URL を取るだけでフォローしない
(リスト詳細画面の共有ボタンは未フォローでも押せる)。

結果は share-urls.tsv に逐次追記するので、中断しても再開できる。
"""
import os
import re
import sys
import time
import urllib.parse

from fetch_share_urls import (
    SENTINEL,
    adb,
    center,
    clip_read,
    clip_write,
    dump,
    find_desc,
    find_text,
    iter_nodes,
    key,
    log,
    swipe,
    tap,
)

OUT = os.environ.get("OUT", "data/share-urls.tsv")
MAX = int(os.environ.get("MAX", "0"))  # 0 なら全件
SEED = os.environ.get("SEED", "")  # 新規に追加したいエリアの一覧ファイル

TYPES = ("トップリスト", "トレンド", "地元で人気")
FOLLOW_TYPES = ("トップリスト",)  # フォロー (保存) するのはこれだけ

COPY_FALLBACK = (114, 2153)  # 共有シートの「クリップボードにコピー」
NO_CAROUSEL = "エリアページにリストのカルーセルが無い"

# 同名のエリアが複数あって geo: が別のエリアを開いてしまう場合の検索語の上書き。
# キーは (エリア名, 市名)。市名は TSV 3 列目と同じもの。
AREA_QUERY = {
    # 「伏見」だけだと京都の伏見区が開く。キーは (エリア名, TSV 3 列目の所在地)。
    ("伏見", "愛知県名古屋市中区"): "伏見 名古屋",
}


def read_tsv(path):
    """TSV を {(リスト名, 市名): URL} で読む。

    3 列目の市名は「中区」「北区」のように全国に同名の区があるリストを
    区別するためだけの列。1 列目は Google 上の実際のリスト名のままにする。
    区別が不要なリストは 3 列目を空にする (行末がタブで終わる)。
    """
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 2:
                    done[(cols[0], cols[2] if len(cols) > 2 else "")] = cols[1]
    return done


def read_seed(path):
    """シードファイルから (エリア名, 市名) を読む。

    1 行 1 エリア。`エリア名<TAB>市名<TAB>検索語` のタブ区切りで、2 列目以降は省略可。
    市名は TSV 3 列目に入る区別用の値。
    検索語を省くと「市名 + エリア名」(市名が無ければエリア名) で検索する。
    """
    out = []
    if not path:
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = (line.rstrip("\n").split("\t") + ["", ""])[:3]
            area, city, query = (c.strip() for c in cols)
            out.append((area, city))
            if query:
                AREA_QUERY[(area, city)] = query
    return out


def missing_lists(done, seed=()):
    """3 種類揃っていない (エリア, 市) の欠落リスト名を返す。"""
    areas = {}
    for name, city in done:
        if ": " in name:
            area, kind = name.rsplit(": ", 1)
            areas.setdefault((area, city), set()).add(kind)
    for key in seed:
        areas.setdefault(key, set())
    out = []
    for area, city in sorted(areas):
        for kind in TYPES:
            if kind not in areas[(area, city)]:
                out.append((f"{area}: {kind}", city))
    return out


def is_card(desc):
    """エリアページのリストカードの content-desc かどうか。

    「六本木: トレンド. 📢. Google Maps. 14 か所」のような形式になっている。
    """
    if "Google Maps" not in desc or ": " not in desc:
        return False
    head = desc.split(".", 1)[0]
    return head.rsplit(": ", 1)[-1] in TYPES


def find_card(xml, name):
    """目的のリストのカードのタップ座標を返す。"""
    for _t, d, b in iter_nodes(xml):
        if b and d.startswith(name + ".") and is_card(d):
            return center(b)
    return None


def card_row_y(xml):
    """カルーセルが見えていれば、その行の y 中心を返す。"""
    for _t, d, b in iter_nodes(xml):
        if b and is_card(d):
            return center(b)[1]
    return None


def search_area(area, city=""):
    """geo: インテントでエリアページを直接開く。

    `am start -n MapsActivity` は直前の画面を復元してしまい、
    検索ボックスの座標を決め打ちでタップするとスポットを開く事故になる。
    geo: インテントならどの画面からでも一発で目的のエリアページに飛べる。

    検索語は「市名 + エリア名」。同名の区を狙い撃ちするのに必要で、
    「名古屋市中区」のような住所表記はそのまま検索語として通る。
    """
    q = urllib.parse.quote(AREA_QUERY.get((area, city)) or (city + area if city else area))
    adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"'geo:0,0?q={q}'")
    time.sleep(8)


def pick_search_result(area):
    """検索候補一覧が出ている場合に、エリア名が完全一致する候補をタップする。

    「恵比寿」「東京」のような曖昧な語では、geo: はエリアページではなく
    候補一覧を返す。この場合カルーセルが存在しないので候補を選んで先へ進む。
    """
    hit = find_desc(dump(), area)
    if not hit:
        return False
    tap(hit)
    time.sleep(7)
    return True


def on_place_page(xml):
    """検索候補一覧ではなく、スポット/エリアのページが既に開いているか。"""
    return "までの経路" in xml


def locate_card(name):
    """縦スクロールでカルーセルを出し、横スクロールで目的のカードを探す。

    戻り値は (タップ座標, カルーセルを見たか)。
    カルーセル自体が無いエリアはリストが 1 つも存在しないので、
    呼び出し側で残り 2 種類の探索を丸ごと省くために区別して返す。
    """
    row_y = None
    prev = None
    stuck = 0
    for _ in range(8):
        xml = dump()
        pt = find_card(xml, name)
        if pt:
            return pt, True
        row_y = card_row_y(xml)
        if row_y:
            break
        # 送っても画面が変わらなければ最下部。ただし 1 回の空振りでは決めない
        # (スワイプが取りこぼされただけの場合にカルーセルまで届かなくなる)。
        stuck = stuck + 1 if xml == prev else 0
        if stuck >= 2:
            break
        prev = xml
        # 速い swipe は fling になって 1 回で深く送れる。
        # 開始 y は下部ナビ帯 (y>=2060) を踏まないよう 2000 に留める。
        # ここを下げるとナビをタップしただけになり本文がスクロールしない。
        swipe(540, 2000, 540, 500, 200)
        time.sleep(1)

    if row_y is None:
        return None, False

    # カルーセルは横スクロール。3 枚全部は同時に見えないので送りながら探す。
    for _ in range(4):
        swipe(900, row_y, 200, row_y, 250)
        time.sleep(1)
        pt = find_card(dump(), name)
        if pt:
            return pt, True
    return None, True


def share_url_here(known_urls):
    """開いているリスト詳細画面から共有 URL を取る。"""
    clip_write(SENTINEL)

    share = find_desc(dump(), "共有")
    if not share:
        return None, "共有ボタンが見つからない"
    tap(share)
    time.sleep(4)

    xml = dump()
    copy = find_text(xml, "クリップボードにコピー")
    if copy is None:
        if "共有" not in xml:
            key(4)
            return None, "共有シートが開かない"
        copy = COPY_FALLBACK
    tap(copy)
    time.sleep(3)

    val = clip_read()
    if val == SENTINEL or not val:
        return None, "クリップボードが更新されない"
    urls = re.findall(r"https://maps\.app\.goo\.gl/[A-Za-z0-9]+", val)
    if not urls:
        return None, "URL 形式でない"
    if urls[0] in known_urls:
        return None, f"URL が既出 ({urls[0]})"
    return urls[0], ""


def grab(name, city, known_urls):
    """1 件分。カードを開き、必要ならフォローして共有 URL を返す。"""
    area, kind = name.rsplit(": ", 1)
    search_area(area, city)

    pt, seen = locate_card(name)
    if not pt and not on_place_page(dump()) and pick_search_result(area):
        # 候補一覧だった場合だけエリアを選び直してもう一度探す。
        # 既にエリアページが開いているのに探し直すと同じ空振りを 2 回やることになる。
        pt, seen = locate_card(name)
    if not pt:
        return None, "カードが見つからない" if seen else NO_CAROUSEL

    tap(pt)
    time.sleep(5)
    if not find_text(dump(), name):
        return None, "リスト画面が開かない"

    if kind in FOLLOW_TYPES:
        save = find_text(dump(), "リストを保存")
        if save:
            tap(save)
            time.sleep(3)

    return share_url_here(known_urls)


def main():
    done = read_tsv(OUT)
    known_urls = set(done.values())
    targets = [n for n in missing_lists(done, read_seed(SEED)) if n not in done]
    log(f"開始: 取得済み {len(done)} 件 / 対象 {len(targets)} 件")

    clip_write(SENTINEL)
    if clip_read() != SENTINEL:
        log("adb-clip が動作しない。push と画面オン/ロック解除を確認すること。")
        return 1

    got = 0
    failed = []
    empty_areas = set()  # カルーセルが無かった = リストが存在しないエリア
    for i, (name, city) in enumerate(targets, 1):
        label = f"{city}{name}" if city else name
        area = name.rsplit(": ", 1)[0]
        if (area, city) in empty_areas:
            # 同じエリアの残り 2 種類も必ず空振りするので検索ごと省く。
            failed.append((label, "エリアにリストが存在しない"))
            log(f"[{i}/{len(targets)}] SKIP {label}")
            continue

        url, reason = grab(name, city, known_urls)
        if reason == NO_CAROUSEL:
            empty_areas.add((area, city))
        if url:
            # 市名が空でも 3 カラム目 (末尾のタブ) は必ず書く。
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(f"{name}\t{url}\t{city}\n")
            known_urls.add(url)
            got += 1
            log(f"[{i}/{len(targets)}] {label}\t{url}")
        else:
            failed.append((label, reason))
            log(f"[{i}/{len(targets)}] FAIL {label}: {reason}")
        if MAX and got >= MAX:
            log(f"MAX={MAX} に到達したため終了")
            break

    log(f"完了: 取得 {got} 件 / 失敗 {len(failed)} 件")
    for label, reason in failed:
        log(f"  未取得: {label} ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
