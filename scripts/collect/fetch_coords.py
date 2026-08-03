#!/usr/bin/env python3
"""エリアごとの中心座標を取得して Firestore の lists に記録する。

Firestore からエリアごとに代表 1 件 (トップリスト優先) の共有 URL を選び、
ブラウザで開いて地図の中心座標を読み取る。
座標はエリア単位の値なので、同一エリアの 3 リスト全部に同じ値を書き込む。
同一エリアの 3 リスト (トップリスト / トレンド / 地元で人気) は中心が数 km ずれるが、
エリアの代表座標としてはトップリストの中心で足りるという割り切り。

URL に載る中心はそのままでは使えない。
Google マップは左にリストのパネル (実測 480px) を重ねて表示し、リストの範囲は
パネルに隠れていない可視領域に合わせて収める。
一方 URL の `@lat,lng` は地図キャンバス全体の中心なので、パネル幅の半分だけ西にずれる。
ずれは画素で一定なのでズームが浅いエリアほど度数では大きくなり、
補正前のデータでは 155 エリア中 153 件が参照点より西、中央値 3.5km・最大 30km ずれていた。
パネル幅を実測して `@` の zoom と組み合わせ、東へ戻してから記録する。
パネルは全高を占めるため縦のずれは無く、緯度は補正しない (実測でも緯度に偏りは無かった)。

Google マップの共有 URL は最初 `/maps/@/data=...` と座標が空の状態で開き、
SPA のロード完了後に `history.replaceState` で `@lat,lng,zoom` 付きに書き換わる。
curl では書き換え前の HTML しか取れないため、agent-browser で実ブラウザを操作する。

同一タブで続けて開くと再センタリングが走らないことがあるので、毎回 about:blank を挟む。
また、リストの中心が反映されないとブラウザの既定センター (現在地) がそのまま残る。
素の google.com/maps を開いて既定センターを実測しておき、その値は「未確定」として弾く。

1 エリア取るごとに書き込む resume-safe な作り。
lat が既に入っているエリアはスキップするので、中断しても再実行すれば続きから走る。

リポジトリのルートから実行する想定 (例: `python3 scripts/collect/fetch_coords.py`)。
認証は `gcloud auth application-default login` で足りる。

オプション:
  --force     lat が既にあるエリアも取り直す (補正式を変えたときの入れ替え用)。
  --dry-run   Firestore に書かず、取得した値を表示するだけ。
  --only NAME エリア名が NAME に一致するものだけ処理する (検証用、複数指定可)。
"""

import os
import re
import subprocess
import sys
import time

# store は親ディレクトリ (scripts/) にある。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402

# URL 中の `@lat,lng,zoomz` 部分。zoom は経度の補正量の計算に使う。
COORD_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+),(\d+(?:\.\d+)?)z")

POLL_INTERVAL = 0.5
POLL_MAX = 40  # 最大 20 秒
STABLE_COUNT = 4  # この回数だけ同じ値が続いたら確定とみなす

# パネル幅の実測に失敗したときの既定値 (実測値)。
PANEL_PX_FALLBACK = 480
# ブラウザの窓は固定する。可視領域の幅が変わると Google が選ぶズームも変わり、取得値が再現しなくなる。
VIEWPORT = ("1280", "800")

# 左端に貼り付いた縦長の箱をリストのパネルとみなし、その幅を返す。
# Google の内部 id やクラスは変わるので、位置と大きさだけで拾う。
# 見つからなければ 0 を返して呼び出し側で既定値に落とす。
PANEL_JS = (
    "(() => { const rs = [...document.querySelectorAll('div')]"
    ".map(e => e.getBoundingClientRect())"
    ".filter(b => b.x < 5 && b.height > window.innerHeight * 0.8"
    " && b.width > 200 && b.width < window.innerWidth * 0.6);"
    " return rs.length ? Math.round(Math.min(...rs.map(b => b.width))) : 0; })()"
)


def ab(*args: str) -> str:
    """agent-browser を叩いて標準出力を返す。失敗しても空文字を返すだけにする。"""
    try:
        r = subprocess.run(
            ["agent-browser", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.stdout.strip()
    except subprocess.SubprocessError:
        return ""


def panel_px() -> int:
    """開いているページのリストパネルの幅 (px)。測れなければ既定値。"""
    out = ab("eval", PANEL_JS).strip().strip('"')
    try:
        w = int(out)
    except ValueError:
        w = 0
    return w if w > 0 else PANEL_PX_FALLBACK


def correct(lat: str, lng: str, zoom: str, panel: int) -> tuple[float, float]:
    """URL 中心をパネル幅の半分だけ東へ戻し、(lat, lng) を返す。

    Web メルカトルではタイル 1 枚が 256px なので、ズーム z での 1px は 360 / (256 * 2^z) 度。
    可視領域の中心はキャンバス中心より panel/2 px 東にあるので、その分を足す。
    """
    deg_per_px = 360.0 / (256.0 * 2.0 ** float(zoom))
    return float(lat), float(lng) + panel / 2 * deg_per_px


def fetch(url: str, default: tuple[str, ...] | None) -> tuple[str, str, str] | None:
    """共有 URL を開いて (lat, lng, zoom) を返す。取れなければ None。

    再センタリングが中途半端に終わると、既定センターの緯度か経度が片方だけ残ることがある。
    どちらかが既定センターと完全一致する間は未確定とみなして無視する。
    東京都内のエリアは既定センターの数 km 圏内に多数あるため、距離ではなく完全一致で判定する。
    """
    ab("open", "about:blank")
    ab("open", url)
    prev, same = None, 0
    for _ in range(POLL_MAX):
        time.sleep(POLL_INTERVAL)
        m = COORD_RE.search(ab("get", "url"))
        if not m:
            continue
        cur = m.groups()
        if default and (cur[0] == default[0] or cur[1] == default[1]):
            continue
        if cur == prev:
            same += 1
            if same >= STABLE_COUNT:
                return cur
        else:
            prev, same = cur, 0
    return prev


def main() -> None:
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv
    only = {sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--only" and i + 1 < len(sys.argv)}

    # エリアの代表 URL。トップリストがあればそれを、無ければ最初に見つかった 1 件を使う。
    # 併せて、既に座標が入っているエリアを取得済みとして拾う。
    areas: dict[tuple[str, str], str] = {}
    done = set()
    for r in store.all_lists():
        key = (r["area"], r["loc"])
        if r.get("lat") is not None:
            done.add(key)
        if not r.get("url"):
            continue
        if r["kind"] == "トップリスト" or key not in areas:
            areas[key] = r["url"]

    todo = [(k, u) for k, u in areas.items() if force or k not in done]
    if only:
        todo = [(k, u) for k, u in todo if k[0] in only]
    mode = " ".join(filter(None, ["--force" if force else "", "--dry-run" if dry else ""]))
    print(f"全 {len(areas)} エリア / 取得済み {len(done)} / 今回 {len(todo)} {mode}".rstrip(), flush=True)
    if not todo:
        return

    ab("set", "viewport", *VIEWPORT)
    # ブラウザの既定センター (リストの中心が反映されなかったときに残る値) を実測しておく。
    # 20 秒かかるので、取得対象が無いときは開かない。
    default = fetch("https://www.google.com/maps", None)
    print(f"既定センター: {default}", flush=True)

    failed = []
    for i, ((area, loc), url) in enumerate(todo, 1):
        got = fetch(url, default)
        if not got:
            # 取りこぼしは走行中の位置でほぼ 7 件ごとに起き、エリア固有の問題ではない。
            # 位置をずらせば通るので、その場で 1 度だけ取り直す。
            got = fetch(url, default)
        if not got:
            failed.append(f"{loc} / {area}")
            print(f"[{i}/{len(todo)}] ★失敗 {loc} / {area}", flush=True)
            continue
        raw_lat, raw_lng, zoom = got
        # パネル幅はページを開いたまま測る。セッション中は変わらないが、測り損ねても既定値で続行する。
        panel = panel_px()
        lat, lng = correct(raw_lat, raw_lng, zoom, panel)
        # 同一エリアの全リストに同じ座標を書き込む。1 件ずつ確定させて resume-safe を保つ。
        n = 0 if dry else store.update_coords(loc, area, lat, lng)
        print(
            f"[{i}/{len(todo)}] {loc} / {area} -> {lat:.7f},{lng:.7f}"
            f" (raw {raw_lng} z{zoom} panel {panel}px, {n} 件更新)",
            flush=True,
        )

    print(f"完了 (失敗 {len(failed)} 件)", flush=True)
    if failed:
        print("未取得: " + ", ".join(failed), flush=True)


if __name__ == "__main__":
    sys.exit(main())
