#!/usr/bin/env python3
"""Firestore へのアクセスをここに集約する。

各収集スクリプトが直接 Firestore クライアントを持つとスキーマ定義が散らばるため、
読み書きはすべてこのモジュール経由にする。

認証はローカルでは `gcloud auth application-default login`、
Cloud Run 上ではサービスアカウントの ADC で通る。
プロジェクトは環境変数 `GOOGLE_CLOUD_PROJECT` か gcloud の既定を使う。

環境変数はリポジトリルートの `.env` からも読む (`.env.example` を参照)。
すでに設定されている値は上書きしないので、export した値の方が勝つ。
"""
import re

from dotenv import load_dotenv
from google.cloud import firestore

LISTS = "lists"
REPORTS = "reports"

# 所在地の先頭から都道府県を切り出す。
# 「県」は 2〜3 文字 (神奈川県・和歌山県・鹿児島県) なので最短一致にする。
PREF_RE = re.compile(r"^(北海道|東京都|大阪府|京都府|.{2,3}?県)")

_db = None

# import した時点で .env を読む。各スクリプトが個別に呼ばなくて済むようにする。
# 既存の環境変数は上書きしないので、export した値や実行時の前置きの方が勝つ。
# find_dotenv() はこのファイルの位置から親を辿るため、実行時のカレントディレクトリに依存しない。
load_dotenv()


def db():
    """Firestore クライアントを遅延生成して使い回す。"""
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def pref_of(loc):
    """所在地から都道府県を導出する。取れなければ例外にする。"""
    m = PREF_RE.match(loc or "")
    if not m:
        raise ValueError(f"所在地から都道府県を取れない: {loc!r}")
    return m.group(1)


def doc_id(loc, name):
    """一意キー (所在地, リスト名) をそのままドキュメント ID にする。

    決定論的な ID なので、同期は重複検出なしの冪等 upsert で済む。
    """
    if not loc or not name:
        raise ValueError(f"所在地とリスト名は必須: {loc!r} {name!r}")
    if "/" in loc or "/" in name:
        raise ValueError(f"ドキュメント ID に / は使えない: {loc!r} {name!r}")
    return f"{loc}|{name}"


def build(name, url, loc, lat=None, lng=None):
    """TSV 由来の値から lists のドキュメント本体を組み立てる。"""
    area, _, kind = name.partition(": ")
    doc = {
        "name": name,
        "area": area,
        "kind": kind,
        "loc": loc,
        "pref": pref_of(loc),
        "url": url,
        # 現状フォローを端末に残しているのはトップリストだけ。
        "followed": kind == "トップリスト",
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    if lat is not None:
        doc["lat"] = float(lat)
        doc["lng"] = float(lng)
    return doc


def upsert(docs, batch_size=400):
    """lists へ upsert する。docs は (doc_id, フィールド辞書) の列。

    merge=True なので、座標だけ・followed だけといった部分更新もできる。
    Firestore のバッチ上限は 500 なので余裕を持って刻む。
    """
    n = 0
    batch = db().batch()
    for i, (did, fields) in enumerate(docs, 1):
        batch.set(db().collection(LISTS).document(did), fields, merge=True)
        n = i
        if i % batch_size == 0:
            batch.commit()
            batch = db().batch()
    if n % batch_size:
        batch.commit()
    return n


def all_lists():
    """lists 全件を辞書のリストで返す。571 件程度なので一括で読む。"""
    return [d.to_dict() | {"id": d.id} for d in db().collection(LISTS).stream()]


def set_followed(name, locs, followed):
    """同名リストの followed を更新する。更新件数を返す。

    端末の UI 上は同名の区を見分けられないため、所在地の候補をまとめて受ける。
    """
    return upsert((doc_id(loc, name),
                   {"followed": followed, "updatedAt": firestore.SERVER_TIMESTAMP})
                  for loc in locs)


def update_coords(loc, area, lat, lng):
    """同一エリアの全リストに代表座標を書き込む。更新件数を返す。"""
    q = (db().collection(LISTS)
         .where(filter=firestore.FieldFilter("loc", "==", loc))
         .where(filter=firestore.FieldFilter("area", "==", area)))
    docs = list(q.stream())
    fields = {"lat": float(lat), "lng": float(lng),
              "updatedAt": firestore.SERVER_TIMESTAMP}
    return upsert((d.id, fields) for d in docs) if docs else 0


if __name__ == "__main__":
    # Firestore に触らない部分の自己チェック。`python3 scripts/store.py` で走る。
    assert pref_of("東京都渋谷区") == "東京都"
    assert pref_of("愛知県名古屋市中区") == "愛知県"
    assert pref_of("神奈川県横浜市") == "神奈川県"    # 3 文字の県
    assert pref_of("和歌山県和歌山市") == "和歌山県"
    assert pref_of("京都府京都市北区") == "京都府"
    assert pref_of("北海道") == "北海道"
    for bad in ("", None, "渋谷区"):
        try:
            pref_of(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"都道府県を取れないはずの所在地が通った: {bad!r}")

    assert doc_id("東京都", "渋谷: トップリスト") == "東京都|渋谷: トップリスト"
    for bad in (("", "渋谷: トップリスト"), ("東京都", ""), ("東京/都", "渋谷")):
        try:
            doc_id(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"不正なドキュメント ID が通った: {bad!r}")

    d = build("渋谷: トップリスト", "https://maps.app.goo.gl/x", "東京都渋谷区", 35.66, 139.7)
    assert (d["area"], d["kind"], d["pref"]) == ("渋谷", "トップリスト", "東京都")
    assert d["followed"] and d["lat"] == 35.66
    assert not build("渋谷: トレンド", "u", "東京都渋谷区")["followed"]
    assert "lat" not in build("渋谷: トレンド", "u", "東京都渋谷区")
    print("store.py の自己チェック OK")
