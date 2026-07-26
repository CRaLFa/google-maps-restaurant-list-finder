#!/usr/bin/env python3
"""share-urls.tsv を全行 3 カラムに正規化し、codepoint 順に並べ直す。

2 カラムの行に空の 3 カラム目 (行末のタブ) を足すだけ。
URL を落とさないよう、行数と URL 集合が変わっていないことを検証してから書き戻す。
"""
import os
import sys

OUT = os.environ.get("OUT", "share-urls.tsv")

with open(OUT, encoding="utf-8") as f:
    lines = [ln.rstrip("\n") for ln in f if ln.strip()]

rows = []
for ln in lines:
    cols = ln.split("\t")
    if len(cols) < 2:
        sys.exit(f"カラム数が不正な行: {ln!r}")
    cols = (cols + ["", ""])[:3]
    rows.append("\t".join(cols))

if len({r.split("\t")[1] for r in rows}) != len(rows):
    sys.exit("URL が重複している。手で確認すること。")
if any(len(r.split("\t")) != 3 for r in rows):
    sys.exit("3 カラムになっていない行がある。")

rows.sort()
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(rows) + "\n")

print(f"正規化完了: {len(rows)} 行 / 全行 3 カラム / codepoint 順")
