#!/usr/bin/env python3
"""Google マップの「フォロー中のリスト」から共有 URL を全件取得する。

クリップボードの読み取りには adb-clip (/data/local/tmp/clip) を使う。
検索ボックスへの貼り付けや UI dump からの抽出は不要になり、
共有シートの連絡先候補 (PII) を dump する必要もなくなった。

クリップボードが更新されないまま前のリストの URL を読んでしまう事故を防ぐため、
コピー前に番兵文字列を書き込み、読み取り結果が番兵のままなら失敗として扱う。
さらに既に記録済みの URL と一致した場合も失敗とする。
結果は Firestore の lists へ 1 件ずつ書き込むので、中断しても再開できる。

所在地は locations.py のテーブルから引く。
端末の UI 上は同名の区 (中央区・北区) を見分けられないため所在地が決まらない。
その場合は書き込まずに警告を出すので、locations.py を更新してから再実行すること。
再取得は 1 件数秒なので、保留の仕組みは持たない。
"""
import os
import re
import subprocess
import sys
import time

import locations
import store

# 端末シリアル。ワイヤレスデバッグはポートが毎回変わるので .env か実行時に指定する。
DEV = os.environ.get("DEV", "")
MAX = int(os.environ.get("MAX", "0"))  # 0 なら全件
CLIP = "/data/local/tmp/clip"  # 端末上の adb-clip

COPY_FALLBACK = (114, 2153)  # 共有シートの「クリップボードにコピー」
SAVED_TAB = (540, 2140)  # 下部ナビの「保存済み」
SENTINEL = "GMAPS_LIST_SENTINEL"  # コピー前にクリップボードへ置く番兵


def log(msg):
    print(msg, flush=True)


def adb(*args, timeout=90):
    if not DEV:
        sys.exit("環境変数 DEV に端末シリアルを設定すること"
                 " (例: DEV=192.0.2.1:5555)。`adb devices` で確認できる。")
    try:
        r = subprocess.run(
            ["adb.exe", "-s", DEV, *args], capture_output=True, text=True, timeout=timeout
        )
        return r.stdout
    except subprocess.TimeoutExpired:
        return ""


def dump():
    """UI 階層を取得する。ファイルには残さない。"""
    for _ in range(3):
        adb("shell", "uiautomator", "dump", "/sdcard/w.xml")
        xml = adb("shell", "cat /sdcard/w.xml")
        if "<node" in xml:
            return xml
        time.sleep(1)
    return ""


def center(bounds):
    x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", bounds))
    return (x1 + x2) // 2, (y1 + y2) // 2


def iter_nodes(xml):
    for m in re.finditer(r"<node[^>]*>", xml):
        n = m.group(0)
        t = re.search(r'text="([^"]*)"', n)
        d = re.search(r'content-desc="([^"]*)"', n)
        b = re.search(r'bounds="([^"]*)"', n)
        yield (t.group(1) if t else ""), (d.group(1) if d else ""), (b.group(1) if b else "")


def find_text(xml, target):
    for t, _d, b in iter_nodes(xml):
        if t == target and b:
            return center(b)
    return None


def find_desc(xml, target):
    for _t, d, b in iter_nodes(xml):
        if d == target and b:
            return center(b)
    return None


def list_options(xml):
    """画面上に見えているリスト項目を [(リスト名, タップ座標), ...] で返す。"""
    out = []
    for _t, d, b in iter_nodes(xml):
        m = re.match(r"^(.*?)\s*に関するオプション$", d)
        if m and b:
            x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", b))
            # 画面端で切れている項目はタップしても開かないので除外する。
            if y2 - y1 < 60:
                continue
            # 下部ナビ「投稿」タブ (x[720,1080] y[2060,2220]) と重なる位置にある
            # オーバーフローボタン (x≈1000) をタップすると投稿タブへ遷移してしまう。
            # ナビ帯にかかる項目はスクロールで上に来てから拾うため、ここでは除外する。
            if (y1 + y2) // 2 >= 2000:
                continue
            out.append((m.group(1).strip(), center(b)))
    return out


def tap(pt):
    adb("shell", "input", "tap", str(pt[0]), str(pt[1]))


def key(code):
    adb("shell", "input", "keyevent", str(code))


def swipe(x1, y1, x2, y2, ms):
    adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))


def raise_sheet():
    swipe(540, 1900, 540, 300, 400)
    time.sleep(2)


def clip_write(text):
    adb("shell", f"{CLIP} '{text}'")


def clip_read():
    return adb("shell", CLIP).strip()


def ensure_list_screen(tries=4):
    """どんな画面からでも「フォロー中のリスト」が見える状態まで戻す。

    戻るキーの連打は Google マップ自体を抜けてしまうので使わない。
    """
    for attempt in range(tries):
        xml = dump()
        if len(list_options(xml)) >= 2:
            return True

        if "クリップボードにコピー" in xml:  # 共有シートが開いている
            key(4)
            time.sleep(2)
            continue
        if find_text(xml, "リストを共有"):  # オプションメニューが開いている
            key(4)
            time.sleep(2)
            continue

        # それ以外は保存済みタブから作り直す。
        log(f"  [recover] 画面を再構築 (試行 {attempt + 1})")
        adb("shell", "am", "start", "-n",
            "com.google.android.apps.maps/com.google.android.maps.MapsActivity")
        time.sleep(4)
        tap(SAVED_TAB)
        time.sleep(4)
        raise_sheet()
        xml = dump()
        more = find_desc(xml, "フォロー中のリストをもっと見る")
        if more:
            tap(more)
            time.sleep(3)
        if len(list_options(dump())) >= 2:
            return True
    return False


def get_share_url(pt, known_urls):
    """1 件分の共有 URL を取得する。失敗したら (None, 理由) を返す。"""
    # コピー前に番兵を置き、クリップボードが確実に更新されたかを判定できるようにする。
    clip_write(SENTINEL)

    tap(pt)
    time.sleep(2)
    xml = dump()
    share = find_text(xml, "リストを共有")
    if not share:
        return None, "メニューが開かない"

    tap(share)
    time.sleep(4)
    xml = dump()
    copy = find_text(xml, "クリップボードにコピー")
    if copy is None:
        if "共有" not in xml:
            # 自分のリストは共有同意ダイアログが先に出る。フォロー中リストでは通常起きない。
            key(4)
            return None, "共有シートが開かない (自分のリストの可能性)"
        copy = COPY_FALLBACK
    tap(copy)
    time.sleep(3)

    val = clip_read()
    # 「クリップボードにコピー」をタップすると共有シートは自動で閉じてリスト画面へ戻る。
    # ここで無条件に戻るキーを押すとボトムシートまで畳んでしまい、
    # 毎回 top から再構築する羽目になる。シートが残っている場合だけ閉じる。
    xml = dump()
    if "クリップボードにコピー" in xml or find_text(xml, "リストを共有"):
        key(4)
        time.sleep(1)

    if val == SENTINEL or not val:
        return None, "クリップボードが更新されない"
    urls = re.findall(r"https://maps\.app\.goo\.gl/[A-Za-z0-9]+", val)
    if not urls:
        return None, "URL 形式でない"
    if urls[0] in known_urls:
        # 番兵は変わったが前と同じ URL が入っているケース (念のため)。
        return None, f"URL が既出 ({urls[0]})"
    return urls[0], ""


def record(name, url):
    """取得した 1 件を Firestore へ記録する。所在地が決まらなければ False。"""
    area = name.split(": ", 1)[0]
    loc = locations.resolve(area)
    if loc is None:
        return False
    store.upsert([(store.doc_id(loc, name), store.build(name, url, loc))])
    return True


def main():
    # 端末の UI 上は同名の区を見分けられないため、
    # 名前が一致すれば取得済みとして扱う (所在地違いの重複を作らない)。
    done = {}
    urls_seen = set()
    for r in store.all_lists():
        done[r["name"]] = r.get("url")
        if r.get("url"):
            urls_seen.add(r["url"])
    log(f"開始: 取得済み {len(done)} 件")

    # adb-clip が使えるか事前確認する。
    clip_write(SENTINEL)
    if clip_read() != SENTINEL:
        log(f"adb-clip ({CLIP}) が動作しない。push と画面オン/ロック解除を確認すること。")
        return 1

    if not ensure_list_screen():
        log("リスト画面を用意できなかったため中止")
        return 1

    attempts = {}
    unresolved = []
    stable = 0
    got = 0
    for _loop in range(20000):
        xml = dump()
        items = list_options(xml)
        if not items:
            if not ensure_list_screen():
                log("リスト画面を復元できないため終了")
                break
            continue

        todo = [(n, p) for n, p in items if n not in done and attempts.get(n, 0) < 2]
        if not todo:
            # 見えている範囲は処理済み。スクロールして次へ。
            swipe(540, 1900, 540, 1300, 800)
            time.sleep(1)
            stable += 1
            if stable >= 6:
                log("最下部に到達")
                break
            continue

        stable = 0
        name, pt = todo[0]
        attempts[name] = attempts.get(name, 0) + 1
        url, reason = get_share_url(pt, urls_seen)
        if url and record(name, url):
            done[name] = url
            urls_seen.add(url)
            got += 1
            log(f"[{len(done)}] {name}\t{url}")
        elif url:
            # URL は取れたが所在地が決まらない。locations.py を直して再実行させる。
            unresolved.append(f"{name}\t{url}")
            log(f"[SKIP] {name}: 所在地が未定義 -> {url}")
        else:
            log(f"[FAIL {attempts[name]}/2] {name}: {reason}")
            if not ensure_list_screen():
                log("リスト画面を復元できないため終了")
                break

        if MAX and got >= MAX:
            log(f"MAX={MAX} に到達したため終了")
            break

    failed = sorted(n for n, c in attempts.items() if c >= 2 and n not in done)
    log(f"完了: 合計 {len(done)} 件 / 未取得 {len(failed)} 件"
        f" / 所在地未定義 {len(unresolved)} 件")
    if failed:
        log("未取得: " + ", ".join(failed))
    if unresolved:
        log("所在地が決まらず記録しなかったリスト。"
            "locations.py の CITIES / WARDS / DISTRICTS / METRO を更新して再実行すること:")
        for u in unresolved:
            log("  " + u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
