#!/usr/bin/env python3
"""share-urls.tsv の 3 列目に所在地をセットする。

エリア名から所在地への対応表は scripts/locations.py に持つ。
新しいエリアを追加したときは locations.py の
CITIES / WARDS / DISTRICTS / METRO を更新してから実行すること。

3 列目は必ず都道府県名から始まる住所表記にする。
所在地は Firestore のドキュメント ID の一部になるため空を許さない。

1 列目・2 列目は一切変えず、3 列目だけを埋めることを検証してから書き戻す。
3 列目に旧形式 (都道府県が付かない「名古屋市」等) が入っていても移行できる。
何度実行しても同じ結果になる。

フェーズ 5 で TSV を廃止したらこのスクリプトも不要になる。
移行後に必要なのは locations.py のテーブル更新だけで、
所在地の解決は収集スクリプトが実行時に行う。
"""
import os
import sys

import locations
from locations import PREFECTURES

OUT = os.environ.get("OUT", "data/share-urls.tsv")

with open(OUT, encoding="utf-8") as f:
    before = [ln.rstrip("\n") for ln in f if ln.strip()]

after = []
touched = set()
for ln in before:
    cols = (ln.split("\t") + ["", ""])[:3]
    area = cols[0].rsplit(": ", 1)[0]
    # 同名の区は既存の 3 列目を手掛かりにしないと所在地が決まらない。
    loc = locations.resolve(area, cols[2])
    if loc is None and area in locations.ward_names:
        sys.exit(f"区の市名が未設定か不正: {ln!r}")
    if loc:
        # 旧形式は新形式の末尾に一致する (名古屋市 -> 愛知県名古屋市) ので移行を許す。
        if cols[2] and cols[2] != loc and not loc.endswith(cols[2]):
            sys.exit(f"3 列目が既に別の値になっている: {ln!r} (期待 {loc})")
        cols[2] = loc
        touched.add(area)
    after.append("\t".join(cols))

if len(before) != len(after):
    sys.exit("行数が変わっている。中止する。")
if sorted(tuple(r.split("\t")[:2]) for r in before) != \
   sorted(tuple(r.split("\t")[:2]) for r in after):
    sys.exit("1 列目または 2 列目が変わってしまっている。中止する。")
unused = (set(locations.district_loc) | set(locations.city_loc)) - touched
if unused:
    sys.exit(f"TSV に存在しないエリアを指定している: {sorted(unused)}")
# 逆に、TSV にあるのに locations.py が知らないエリアも落とす。
# 収集スクリプトはこのテーブルだけで所在地を決めるため、漏れると新規リストを記録できない。
unknown = sorted({r.split("\t")[0].rsplit(": ", 1)[0] for r in after} - touched
                 - locations.ward_names)
if unknown:
    sys.exit(f"locations.py に所在地の定義が無いエリアがある: {unknown}")
bad = {r.split("\t")[2] for r in after
       if r.split("\t")[2] and not r.split("\t")[2].startswith(PREFECTURES)}
if bad:
    sys.exit(f"都道府県から始まっていない所在地がある: {sorted(bad)}")
empty = [r for r in after if not r.split("\t")[2]]
if empty:
    sys.exit(f"所在地が空の行が残っている: {empty}")

after.sort()
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(after) + "\n")

changed = sum(1 for a, b in zip(sorted(before), sorted(after)) if a != b)
print(f"所在地をセットしたエリア: {len(touched)} 個 / 更新行数: {changed}")
