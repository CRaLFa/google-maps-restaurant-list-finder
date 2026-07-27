#!/usr/bin/env python3
"""share-urls.tsv と coords.tsv を結合して Firestore の lists へ投入する。

初回移行に使い、移行後は「TSV からの復旧」用に残している。
冪等なので何度実行しても同じ結果になる。

    python3 scripts/import_tsv.py --dry-run   # 検証のみ。Firestore に触らない
    python3 scripts/import_tsv.py             # 投入

読むのは data/archive/ に凍結した移行時点の TSV で、以後更新されない。
移行後に増えたエリアは復旧できないため、これは最後の手段。
通常の復旧には Firestore のスケジュールバックアップからのリストアを使う。

結合キーはエリア名と所在地の組。
どちらか片方にしか無いエリアがあれば投入前に落とす。
"""
import os
import sys

# store は親ディレクトリ (scripts/) にある。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402

URLS = os.environ.get("URLS", "data/archive/share-urls.tsv")
COORDS = os.environ.get("COORDS", "data/archive/coords.tsv")
DRY_RUN = "--dry-run" in sys.argv

EXPECTED_ROWS = 571
EXPECTED_AREAS = 191


def read_tsv(path, cols):
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, ln in enumerate(f, 1):
            ln = ln.rstrip("\n")
            if not ln.strip():
                continue
            c = ln.split("\t")
            if len(c) != cols:
                sys.exit(f"{path}:{i} カラム数が {len(c)} 個: {ln!r}")
            rows.append(c)
    return rows


# エリアの代表座標。キーは (エリア名, 所在地)。
coords = {}
for area, loc, lat, lng in read_tsv(COORDS, 4):
    if not loc:
        sys.exit(f"{COORDS}: 所在地が空のエリアがある: {area!r}")
    if (area, loc) in coords:
        sys.exit(f"{COORDS}: エリアが重複している: {area!r} {loc!r}")
    coords[(area, loc)] = (lat, lng)

docs = []
areas = set()
for name, url, loc in read_tsv(URLS, 3):
    if not loc:
        sys.exit(f"{URLS}: 所在地が空の行がある: {name!r}")
    area = name.split(": ", 1)[0]
    key = (area, loc)
    if key not in coords:
        sys.exit(f"{COORDS} に座標が無い: {area!r} {loc!r}")
    areas.add(key)
    lat, lng = coords[key]
    docs.append((store.doc_id(loc, name), store.build(name, url, loc, lat, lng)))

# 投入前に件数と欠損を検証する。ここを通らないと Firestore に一切書かない。
if len({d for d, _ in docs}) != len(docs):
    sys.exit("ドキュメント ID が重複している。所在地とリスト名の組が一意になっていない。")
orphan = set(coords) - areas
if orphan:
    sys.exit(f"{URLS} に存在しないエリアの座標がある: {sorted(orphan)}")
if len(docs) != EXPECTED_ROWS:
    sys.exit(f"リスト件数が {len(docs)} 件。想定は {EXPECTED_ROWS} 件。"
             " 増減が意図的なら EXPECTED_ROWS を更新すること。")
if len(areas) != EXPECTED_AREAS:
    sys.exit(f"エリア数が {len(areas)} 個。想定は {EXPECTED_AREAS} 個。"
             " 増減が意図的なら EXPECTED_AREAS を更新すること。")
missing = [d for d, f in docs if f.get("lat") is None or f.get("lng") is None]
if missing:
    sys.exit(f"座標が欠損しているドキュメントがある: {missing[:5]}")

print(f"検証 OK: {len(docs)} 件 / {len(areas)} エリア / "
      f"{len({f['pref'] for _, f in docs})} 都道府県 / 座標欠損 0")

if DRY_RUN:
    print("--dry-run のため Firestore には書き込まない。")
    sys.exit()

n = store.upsert(docs)
print(f"Firestore の {store.LISTS} に upsert: {n} 件")

# 書き込み後、Firestore 側の実体を数えて突き合わせる。
after = store.all_lists()
if len(after) != len(docs):
    sys.exit(f"投入後の件数が {len(after)} 件で一致しない。想定 {len(docs)} 件。"
             " 過去に投入した古いドキュメントが残っている可能性がある。")
print(f"投入後の検証 OK: {len(after)} 件 / "
      f"{len({(d['area'], d['loc']) for d in after})} エリア")
