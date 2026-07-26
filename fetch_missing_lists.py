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

OUT = os.environ.get("OUT", "share-urls.tsv")
MAX = int(os.environ.get("MAX", "0"))  # 0 なら全件

TYPES = ("トップリスト", "トレンド", "地元で人気")
FOLLOW_TYPES = ("トップリスト",)  # フォロー (保存) するのはこれだけ

COPY_FALLBACK = (114, 2153)  # 共有シートの「クリップボードにコピー」

# 同名のエリアが複数あって geo: が別のエリアを開いてしまう場合の検索語の上書き。
AREA_QUERY = {
    "伏見": "伏見 名古屋",  # 「伏見」だけだと京都の伏見区が開く
}


def read_tsv(path):
    """TSV を {リスト名: URL} で読む。"""
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "\t" in line:
                    name, url = line.rstrip("\n").split("\t", 1)
                    done[name] = url
    return done


def missing_lists(done):
    """3 種類揃っていないエリアの欠落リスト名を返す。"""
    areas = {}
    for name in done:
        if ": " in name:
            area, kind = name.rsplit(": ", 1)
            areas.setdefault(area, set()).add(kind)
    out = []
    for area in sorted(areas):
        for kind in TYPES:
            if kind not in areas[area]:
                out.append(f"{area}: {kind}")
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


def search_area(area):
    """geo: インテントでエリアページを直接開く。

    `am start -n MapsActivity` は直前の画面を復元してしまい、
    検索ボックスの座標を決め打ちでタップするとスポットを開く事故になる。
    geo: インテントならどの画面からでも一発で目的のエリアページに飛べる。
    """
    q = urllib.parse.quote(AREA_QUERY.get(area, area))
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


def locate_card(name):
    """縦スクロールでカルーセルを出し、横スクロールで目的のカードを探す。"""
    row_y = None
    for _ in range(7):
        xml = dump()
        pt = find_card(xml, name)
        if pt:
            return pt
        row_y = card_row_y(xml)
        if row_y:
            break
        swipe(540, 2000, 540, 900, 700)
        time.sleep(2)

    if row_y is None:
        return None

    # カルーセルは横スクロール。3 枚全部は同時に見えないので送りながら探す。
    for _ in range(5):
        swipe(900, row_y, 200, row_y, 600)
        time.sleep(2)
        pt = find_card(dump(), name)
        if pt:
            return pt
    return None


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


def grab(name, known_urls):
    """1 件分。カードを開き、必要ならフォローして共有 URL を返す。"""
    area, kind = name.rsplit(": ", 1)
    search_area(area)

    pt = locate_card(name)
    if not pt and pick_search_result(area):
        # 候補一覧だった場合はエリアを選び直してもう一度探す。
        pt = locate_card(name)
    if not pt:
        return None, "カードが見つからない (リスト自体が存在しない可能性)"

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
    targets = [n for n in missing_lists(done) if n not in done]
    log(f"開始: 取得済み {len(done)} 件 / 対象 {len(targets)} 件")

    clip_write(SENTINEL)
    if clip_read() != SENTINEL:
        log("adb-clip が動作しない。push と画面オン/ロック解除を確認すること。")
        return 1

    got = 0
    failed = []
    for i, name in enumerate(targets, 1):
        url, reason = grab(name, known_urls)
        if url:
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(f"{name}\t{url}\n")
            known_urls.add(url)
            got += 1
            log(f"[{i}/{len(targets)}] {name}\t{url}")
        else:
            failed.append((name, reason))
            log(f"[{i}/{len(targets)}] FAIL {name}: {reason}")
        if MAX and got >= MAX:
            log(f"MAX={MAX} に到達したため終了")
            break

    log(f"完了: 取得 {got} 件 / 失敗 {len(failed)} 件")
    for name, reason in failed:
        log(f"  未取得: {name} ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
