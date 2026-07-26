# Google マップ「保存済み / フォロー中リスト」エクスポート・管理

Google マップの公式「Google データ エクスポート (Takeout)」では、
**自分が作成したリスト**のスポットは取得できるが、
**他人が作成し自分がフォロー (保存) したリスト**の中身は含まれない。
このリポジトリは、フォロー中リストの一覧・共有 URL の取得と、リストの一括削除 (フォロー解除) を行うための手順とスクリプトをまとめたもの。

当初は Web 版の DevTools コンソールで抽出する JS も用意していたが、
Web 版は一度に数十件しか描画しない仕様でリスト数が多いと全件取得しきれないため廃止した。
現在は adb 方式のみ。

## adb 方式 (実機 UI 自動操作)

実機の Google マップアプリを adb + uiautomator で自動操作する。描画上限を受けず全件を確実に扱える。

- [`scripts/fetch_share_urls.py`](./scripts/fetch_share_urls.py) — フォロー中リストを一巡し、各リストの共有 URL を [`data/share-urls.tsv`](./data/share-urls.tsv) に追記する。既知の名前はスキップするため resume-safe。
- [`scripts/fetch_missing_lists.py`](./scripts/fetch_missing_lists.py) — `data/share-urls.tsv` で 3 種類 (トップリスト / トレンド / 地元で人気) が揃っていないエリアを算出し、エリア検索から未フォローのリストを開いて共有 URL を取得する。フォロー (保存) するのはトップリストのみ。`SEED=data/seed.tsv` を渡すと、まだ 1 件も無い新規エリアも対象にできる。
- [`scripts/delete_lists.py`](./scripts/delete_lists.py) — 「〇〇: トップリスト」は残し、それ以外 (トレンド / 地元で人気) を一括削除 (フォロー解除) する。**不可逆操作**。
- [`scripts/set_locations.py`](./scripts/set_locations.py) — `data/share-urls.tsv` 3 列目 (所在地) を都道府県から始まる住所表記でセットする。新しいエリアを追加したら `CITIES` / `WARDS` / `DISTRICTS` / `METRO` を更新して実行する。冪等。
- [`scripts/normalize_tsv.py`](./scripts/normalize_tsv.py) — `data/share-urls.tsv` を全行 3 カラムに揃え、codepoint 順に並べ直す。取得スクリプトの実行後に流す想定。
- [`tools/adb-clip/`](./tools/adb-clip/) — クリップボード読み書き用に vendor した [polygraphene/adb-clip](https://github.com/polygraphene/adb-clip)。共有 URL の取得に使う。

クリップボードは Android 10 以降フォアグラウンド以外から読めないため、adb-clip を `app_process` 経由で使って回避している。詳細は [`docs/adb-workflow.md`](./docs/adb-workflow.md) 参照。

すべてのスクリプトはリポジトリのルートから実行する想定 (例: `python3 scripts/fetch_missing_lists.py`)。`OUT` / `SEED` は相対パスなのでカレントディレクトリに依存する。

## データファイル

- [`data/share-urls.tsv`](./data/share-urls.tsv) — **正データ。** `リスト名 <TAB> 共有 URL <TAB> 所在地` の TSV。
  フォロー中かどうかに関わらず、収集したエリア別リストをすべて記録する。
  削除したリストのバックアップも兼ねる (URL から再フォローで復元可能)。
  1 列目は Google 上の実際のリスト名 (「中区」「渋谷」等、全国で同名になり得る)。
  3 列目は同名エリアを区別するための所在地で、都道府県から始まる住所表記
  (`北海道` / `愛知県名古屋市` / `東京都港区` のように粒度はエリア種別で変わる) を入れる。
  区別が不要なエリアは 3 列目を空にする (行末はタブで終わる)。
  一意キーは (1 列目, 3 列目) の組。
- [`data/seed.tsv`](./data/seed.tsv) — `fetch_missing_lists.py` の `SEED` に渡す新規エリア候補の一覧。
  試した検索語・所在地の記録を兼ねるため、取得済みの行も削除せず残している。

### 現在の内容 (2026-07-26)

571 件 / 191 エリア。内訳はトップリスト 191・トレンド 189・地元で人気 191。

- **端末にフォローして残しているのはトップリストのみ。** トレンド / 地元で人気 は共有 URL だけを保持している。
- 未取得は `代官山町: トレンド` と `東京: トレンド` (どちらも Google 側にリストが存在しないことを確認済み) の 2 件。
- 政令指定都市の区、同名の区 (中区・北区・南区・東区・西区・中央区・港区等)、主要都市の繁華街エリアを調査・追加済み。
  詳細な調査結果は [`docs/missing-areas.md`](./docs/missing-areas.md) を参照。

## ドキュメント

- [`docs/adb-workflow.md`](./docs/adb-workflow.md) — adb + uiautomator による一連の手順。
  リスト一覧の全件収集 (スワイプの慣性による取りこぼしと対策)、共有 URL の取得 (adb-clip)、リストの削除、
  欠落しているエリア別リストの追加取得 (geo: インテント・曖昧なエリア名の扱い) の実証結果・ハマりどころ。

## 前提

- 実機を adb (ワイヤレスデバッグ) で接続。ワイヤレスはポートが毎回変わるので都度確認する。
- adb 操作中は端末を画面オン + ロック解除のままにする (クリップボード読み取り・スリープ対策)。
- 収集される UI dump には連絡先候補等の個人情報が混じり得るため、dump をリポジトリやログに残さないこと。
