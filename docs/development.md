# 開発ガイド

収集スクリプトの実行方法、データファイルの形式、環境変数をまとめる。
Web アプリの設計と移行の経緯は [`webapp-design.md`](./webapp-design.md) を参照。

## セットアップ

```bash
uv venv && uv pip install -r requirements.txt   # Python 側の依存
gcloud auth application-default login           # ローカル実行時の認証
cp .env.example .env                            # 環境変数を埋める
go test ./... && go run ./cmd/server            # サーバ (http://localhost:8080)
```

すべてのスクリプトはリポジトリのルートから実行する想定 (例: `python3 scripts/adb/fetch_missing_lists.py`)。

## 環境変数

`.env` に書く。雛形は [`../.env.example`](../.env.example)。`.env` はコミットしない (API キーが入るため)。

| 変数 | 用途 |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | **必須。** Firestore を置いている GCP プロジェクト |
| `GOOGLE_CLOUD_QUOTA_PROJECT` | API 呼び出しのクォータの付け先。ADC が別プロジェクトを向いているとき用 |
| `FIRESTORE_DATABASE` | **必須。** 使う Firestore データベース |
| `MAPS_API_KEY` | Maps JavaScript API のキー。空にすると地図が出ない (ツリーと検索は動く) |
| `MAPS_MAP_ID` | 省くと開発用の `DEMO_MAP_ID` にフォールバックする |
| `RECAPTCHA_SITE_KEY` | 空にすると bot 検証を飛ばす。ローカル開発時のみ空にする |
| `PORT` | サーバの待ち受けポート。省略時 8080 |
| `DEV` | 収集スクリプト用の端末シリアル。`adb devices` で確認する |

読み込みは Go が [godotenv](https://github.com/joho/godotenv)、Python が [python-dotenv](https://github.com/theskumar/python-dotenv)。
どちらも**既存の環境変数を上書きしない**ので、`MAPS_MAP_ID=xxx go run ./cmd/server` のように前置きすれば一時的に差し替えられる。
本番 (Cloud Run) は `.env` を持たず、`--set-env-vars` で渡した値で動く。

Python 側は `scripts/store.py` の位置から親を辿るためカレントディレクトリに依存しないが、
**Go 側はカレントディレクトリの `.env` しか見ない**のでリポジトリのルートから起動すること。

`MAPS_API_KEY` はクライアントに露出するので、GCP コンソール側で HTTP リファラ制限をかけること。
ローカルで地図を表示するには `http://localhost:8080/*` を、
報告フォームを試すには reCAPTCHA のサイトキーに `localhost` を許可しておく必要がある。

Firestore は `(default)` ではなく名前付きデータベースに置いている。
同じプロジェクトに別用途のデータベースを足せるようにするため。
`(default)` はリネームできないので、名前付きにするなら作り直すしかない (詳細は [`webapp-design.md`](./webapp-design.md))。

## Web アプリ

- [`cmd/server/main.go`](../cmd/server/main.go) — Cloud Run で動かす静的配信 + API サーバ。
  `GET /api/lists` / `POST /api/reports` / `GET /api/config`。
- [`cmd/server/web/index.html`](../cmd/server/web/index.html) — 単一 HTML のフロント。
  Maps JavaScript API + 都道府県 ▶ 市区 ▶ エリア ▶ リストの入れ子ツリー + 報告フォーム。ビルド不要。
  階層は所在地 (`loc`) が親のフルパスになっていることを利用して組み立てる。
- [`cmd/server/tree_check.js`](../cmd/server/tree_check.js) — 上記の階層の組み立てを実データで検証する。
  `node cmd/server/tree_check.js` で実行。`index.html` の `<script>` をそのまま読み込んで動かす。
- [`scripts/store.py`](../scripts/store.py) — Firestore アクセスの集約先。
  ドキュメント ID の組み立て、所在地からの都道府県導出、upsert。
  `python3 scripts/store.py` で自己チェックが走る。
- [`scripts/import_tsv.py`](../scripts/import_tsv.py) — `data/archive/` の TSV を結合して Firestore の `lists` へ投入する。
  冪等。`--dry-run` で Firestore に触らず検証だけ行う。移行時点への復旧用に残している。

## 収集スクリプト (adb 方式)

実機の Google マップアプリを adb + uiautomator で自動操作する。
Web 版の DevTools コンソールで抽出する方式も試したが、一度に数十件しか描画されず全件取得できないため廃止した。

読み書きはすべて Firestore に対して行う。実行前に `.env` を用意すること。

端末を繋ぐ 3 本は [`scripts/adb/`](../scripts/adb/) にまとめてある。
新しいエリアを足すときにしか動かさず、日常的に触るのは Web アプリの方だから。
`scripts/` 直下に残しているのは、端末を必要としないもの (`store.py` / `locations.py` / `fetch_coords.py` / `import_tsv.py`) だけ。

- [`scripts/adb/fetch_share_urls.py`](../scripts/adb/fetch_share_urls.py) — フォロー中リストを一巡し、各リストの共有 URL を記録する。
  既知の名前はスキップするため resume-safe。
- [`scripts/adb/fetch_missing_lists.py`](../scripts/adb/fetch_missing_lists.py) — 3 種類 (トップリスト / トレンド / 地元で人気) が揃っていないエリアを算出し、
  エリア検索から未フォローのリストを開いて共有 URL を取得する。
  フォロー (保存) するのはトップリストのみ。
  `SEED=data/seed.tsv` を渡すと、まだ 1 件も無い新規エリアも対象にできる。
- [`scripts/adb/delete_lists.py`](../scripts/adb/delete_lists.py) — 「〇〇: トップリスト」は残し、それ以外を一括削除 (フォロー解除) する。
  **端末側の削除は不可逆。**
  Firestore のドキュメントは消さず `followed` を false にするので、共有 URL からの再フォローで復元できる。
- [`scripts/fetch_coords.py`](../scripts/fetch_coords.py) — **adb 不要。**
  エリアごとの代表 URL (トップリスト優先) を PC のブラウザで開き、リダイレクト後の URL から地図の中心座標を読んで、
  同一エリアの全リストの `lat` / `lng` を更新する。
  座標が入っているエリアはスキップするため resume-safe。
  [agent-browser](https://www.npmjs.com/package/agent-browser) が必要。
- [`scripts/locations.py`](../scripts/locations.py) — エリア名から所在地を決める対応表。
  **新しいエリアを収集する前にここを更新する。**
  更新しないと収集スクリプトがそのリストを記録できない。
  同名の区 (中央区・北区) は端末から区別できないため所在地を手で与える必要がある。
  `python3 scripts/locations.py` で自己チェック。
- [`tools/adb-clip/`](../tools/adb-clip/) — クリップボード読み書き用に vendor した [polygraphene/adb-clip](https://github.com/polygraphene/adb-clip)。

クリップボードは Android 10 以降フォアグラウンド以外から読めないため、adb-clip を `app_process` 経由で使って回避している。
手順の詳細とハマりどころは [`adb-workflow.md`](./adb-workflow.md) を参照。

### 前提

- 実機を adb (ワイヤレスデバッグ) で接続し、端末シリアルを `DEV` に設定する。
  ワイヤレスはポートが毎回変わるので、繋ぎ直すたびに `adb devices` で確認して更新すること。
  未設定のまま実行すると、端末を触る前にその旨を出して止まる。
- adb 操作中は端末を画面オン + ロック解除のままにする (クリップボード読み取り・スリープ対策)。
- 収集される UI dump には連絡先候補等の個人情報が混じり得るため、**dump をリポジトリやログに残さないこと。**

## データ

正データは Firestore の名前付きデータベース `restaurant-lists`。
コレクションは `lists` (収集したリスト) と `reports` (閲覧者からの報告) の 2 つ。
フィールドの定義は [`webapp-design.md`](./webapp-design.md) のデータモデルを参照。

`lists` の要点だけ再掲する。

- ドキュメント ID は `{所在地}|{リスト名}` の決定論的な値。
  一意キー (リスト名, 所在地) をそのまま ID にしているので、同期は冪等な upsert で済む。
- リスト名は Google 上の実際の名前 (「中区」「渋谷」等、全国で同名になり得る)。
- 所在地 (`loc`) は同名エリアを区別するための、都道府県から始まる住所表記。
  粒度はエリア種別で変わる (`北海道` / `愛知県名古屋市` / `東京都港区`)。
  **必須。** ドキュメント ID の一部なので空を許さない。
  区別が不要なエリアも都道府県名だけは入れる (`東京都` エリアの所在地は `東京都`)。
  この値がそのまま親のフルパスになり、フロントのツリーの階層になる。
- 座標 (`lat` / `lng`) はエリア単位の値をリスト側に非正規化して持つ。
  同一エリアでも 3 リストの中心は数 km ずれるため、トップリストの中心を代表値としている。
- フォロー中かどうかに関わらず、収集したエリア別リストをすべて記録する。
  端末から削除したリストも `followed` を false にして残す (URL から再フォローで復元可能)。

### ファイルとして残しているもの

- [`data/seed.tsv`](../data/seed.tsv) — `fetch_missing_lists.py` の `SEED` に渡す新規エリア候補の一覧。
  試した検索語・所在地の記録を兼ねるため、取得済みの行も削除せず残している。
- [`data/archive/`](../data/archive/) — Firestore へ移行した時点の TSV を凍結したもの。
  `share-urls.tsv` (リスト名 / 共有 URL / 所在地) と `coords.tsv` (エリア名 / 所在地 / 緯度 / 経度)。
  以後更新しない。読むのは `import_tsv.py` と `tree_check.js` だけ。

### バックアップと復旧

Firestore のスケジュールバックアップを日次で取っている (保持 7 日)。
設定済みなので、作り直すとき以外は実行しなくてよい。

```bash
gcloud firestore backups schedules create --database=restaurant-lists \
  --recurrence=daily --retention=7d
```

設定内容と取得済みのバックアップの確認。

```bash
gcloud firestore backups schedules list --database=restaurant-lists
gcloud firestore backups list --location=asia-northeast1
```

リストアは既存のデータベースに上書きできず、**新しいデータベースが作られる。**

```bash
gcloud firestore databases restore \
  --source-backup=projects/<PROJECT>/locations/asia-northeast1/backups/<BACKUP_ID> \
  --destination-database=restaurant-lists-restored
```

復旧後は `FIRESTORE_DATABASE` を新しい名前に向ける。
Cloud Run 側は `gcloud run services update --set-env-vars FIRESTORE_DATABASE=...` で切り替わる。

バックアップが失われた場合の最後の手段が `import_tsv.py` による `data/archive/` からの復元。
移行時点の 571 件に戻るだけで、それ以降に増えたエリアは復旧できない。

### 収録内容 (2026-07-27 時点)

571 件 / 191 エリア。内訳はトップリスト 191・トレンド 189・地元で人気 191。

- **端末にフォローして残しているのはトップリストのみ。** トレンド / 地元で人気 は共有 URL だけを保持している。
- 未取得は `代官山町: トレンド` と `東京: トレンド` の 2 件。どちらも Google 側にリストが存在しないことを確認済み。
- 政令指定都市の区、同名の区 (中区・北区・南区・東区・西区・中央区・港区等)、主要都市の繁華街エリアを調査・追加済み。
  詳細な調査結果は [`missing-areas.md`](./missing-areas.md) を参照。
