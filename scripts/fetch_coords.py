#!/usr/bin/env python3
"""エリアごとの中心座標を取得して data/coords.tsv に記録する。

share-urls.tsv からエリアごとに代表 1 件 (トップリスト優先) の共有 URL を選び、
ブラウザで開いて地図の中心座標を読み取る。
同一エリアの 3 リスト (トップリスト / トレンド / 地元で人気) は中心が数 km ずれるが、
エリアの代表座標としてはトップリストの中心で足りるという割り切り。

Google マップの共有 URL は最初 `/maps/@/data=...` と座標が空の状態で開き、
SPA のロード完了後に `history.replaceState` で `@lat,lng,zoom` 付きに書き換わる。
curl では書き換え前の HTML しか取れないため、agent-browser で実ブラウザを操作する。

同一タブで続けて開くと再センタリングが走らないことがあるので、毎回 about:blank を挟む。
また、リストの中心が反映されないとブラウザの既定センター (現在地) がそのまま残る。
素の google.com/maps を開いて既定センターを実測しておき、その値は「未確定」として弾く。

data/coords.tsv に追記していく resume-safe な作り。
既に取得済みのエリアはスキップするので、中断しても再実行すれば続きから走る。

リポジトリのルートから実行する想定 (例: `python3 scripts/fetch_coords.py`)。
"""

import os
import re
import subprocess
import sys
import time

SRC = os.environ.get("SRC", "data/share-urls.tsv")
OUT = os.environ.get("OUT", "data/coords.tsv")

# URL 中の `@lat,lng,zoomz` 部分
COORD_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+),\d+(?:\.\d+)?z")

POLL_INTERVAL = 0.5
POLL_MAX = 40  # 最大 20 秒
STABLE_COUNT = 4  # この回数だけ同じ値が続いたら確定とみなす


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


def fetch(url: str, default: tuple[str, str] | None) -> tuple[str, str] | None:
    """共有 URL を開いて (lat, lng) を返す。取れなければ None。

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
    # エリアの代表 URL。トップリストがあればそれを、無ければ最初に見つかった 1 件を使う。
    areas: dict[tuple[str, str], str] = {}
    for line in open(SRC, encoding="utf-8"):
        cols = line.rstrip("\n").split("\t")
        if len(cols) < 2 or not cols[1]:
            continue
        name, url, loc = cols[0], cols[1], cols[2] if len(cols) > 2 else ""
        area, _, kind = name.partition(": ")
        if kind == "トップリスト" or (area, loc) not in areas:
            areas[(area, loc)] = url

    done = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 2:
                done.add((cols[0], cols[1]))

    # ブラウザの既定センター (リストの中心が反映されなかったときに残る値) を実測しておく。
    default = fetch("https://www.google.com/maps", None)
    print(f"既定センター: {default}", flush=True)

    todo = [(k, u) for k, u in areas.items() if k not in done]
    print(f"全 {len(areas)} エリア / 取得済み {len(done)} / 今回 {len(todo)}", flush=True)

    failed = []
    with open(OUT, "a", encoding="utf-8") as f:
        for i, ((area, loc), url) in enumerate(todo, 1):
            got = fetch(url, default)
            if not got:
                failed.append(f"{loc} / {area}")
                print(f"[{i}/{len(todo)}] ★失敗 {loc} / {area}", flush=True)
                continue
            lat, lng = got
            f.write(f"{area}\t{loc}\t{lat}\t{lng}\n")
            f.flush()
            print(f"[{i}/{len(todo)}] {loc} / {area} -> {lat},{lng}", flush=True)

    print(f"完了 (失敗 {len(failed)} 件)", flush=True)
    if failed:
        print("未取得: " + ", ".join(failed), flush=True)


if __name__ == "__main__":
    sys.exit(main())
