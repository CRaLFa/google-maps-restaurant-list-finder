# Google マップ レストランリスト検索

Google マップ公式のエリア別レストランリスト (「〇〇: トップリスト」等) を収集し、地図から探せる Web アプリとして公開する。

リポジトリの出自は「保存済み / フォロー中リストのエクスポート・管理」で、以下はその手順とスクリプト。
正データを Firestore へ移した現在、日常的に触るのは Web アプリ側になる。

## 収集の背景

Google マップの公式「Google データ エクスポート (Takeout)」では、
**自分が作成したリスト**のスポットは取得できるが、
**他人が作成し自分がフォロー (保存) したリスト**の中身は含まれない。
このリポジトリは、フォロー中リストの一覧・共有 URL の取得と、リストの一括削除 (フォロー解除) を行うための手順とスクリプトをまとめたもの。

当初は Web 版の DevTools コンソールで抽出する JS も用意していたが、
Web 版は一度に数十件しか描画しない仕様でリスト数が多いと全件取得しきれないため廃止した。
現在は adb 方式のみ。

## adb 方式 (実機 UI 自動操作)

実機の Google マップアプリを adb + uiautomator で自動操作する。描画上限を受けず全件を確実に扱える。

収集スクリプトは Firestore に読み書きする (移行済み)。実行前に `GOOGLE_CLOUD_PROJECT` と ADC を設定すること。

- [`scripts/fetch_share_urls.py`](./scripts/fetch_share_urls.py) — フォロー中リストを一巡し、各リストの共有 URL を Firestore に記録する。既知の名前はスキップするため resume-safe。
- [`scripts/fetch_missing_lists.py`](./scripts/fetch_missing_lists.py) — 3 種類 (トップリスト / トレンド / 地元で人気) が揃っていないエリアを算出し、エリア検索から未フォローのリストを開いて共有 URL を取得する。フォロー (保存) するのはトップリストのみ。`SEED=data/seed.tsv` を渡すと、まだ 1 件も無い新規エリアも対象にできる。
- [`scripts/delete_lists.py`](./scripts/delete_lists.py) — 「〇〇: トップリスト」は残し、それ以外 (トレンド / 地元で人気) を一括削除 (フォロー解除) する。**端末側の削除は不可逆**。Firestore のドキュメントは消さず `followed` を false にするので、共有 URL からの再フォローで復元できる。
- [`scripts/locations.py`](./scripts/locations.py) — エリア名から所在地を決める対応表。**新しいエリアを収集する前にここを更新する。** 更新しないと収集スクリプトがそのリストを記録できない。同名の区 (中央区・北区) は端末から区別できないため所在地を手で与える必要がある。`python3 scripts/locations.py` で自己チェック。
- [`scripts/set_locations.py`](./scripts/set_locations.py) — `data/share-urls.tsv` 3 列目 (所在地) を `locations.py` の対応表からセットする。TSV 側の整合を保つためのもので、フェーズ 5 で TSV を廃止したら不要になる。冪等。
- [`scripts/normalize_tsv.py`](./scripts/normalize_tsv.py) — `data/share-urls.tsv` を全行 3 カラムに揃え、codepoint 順に並べ直す。同じくフェーズ 5 で廃止。
- [`scripts/fetch_coords.py`](./scripts/fetch_coords.py) — **adb 不要。** エリアごとの代表 URL (トップリスト優先) を PC のブラウザで開き、リダイレクト後の URL から地図の中心座標を読んで、同一エリアの全リストの `lat`/`lng` を更新する。座標が入っているエリアはスキップするため resume-safe。[agent-browser](https://www.npmjs.com/package/agent-browser) が必要。
- [`tools/adb-clip/`](./tools/adb-clip/) — クリップボード読み書き用に vendor した [polygraphene/adb-clip](https://github.com/polygraphene/adb-clip)。共有 URL の取得に使う。

クリップボードは Android 10 以降フォアグラウンド以外から読めないため、adb-clip を `app_process` 経由で使って回避している。詳細は [`docs/adb-workflow.md`](./docs/adb-workflow.md) 参照。

すべてのスクリプトはリポジトリのルートから実行する想定 (例: `python3 scripts/fetch_missing_lists.py`)。`OUT` / `SEED` は相対パスなのでカレントディレクトリに依存する。

## データファイル

**正データは Firestore へ移行済み。** 以下の TSV は初回インポート元と復旧用として残している (フェーズ 5 で `data/archive/` へ移す)。

- [`data/share-urls.tsv`](./data/share-urls.tsv) — `リスト名 <TAB> 共有 URL <TAB> 所在地` の TSV。
  フォロー中かどうかに関わらず、収集したエリア別リストをすべて記録する。
  削除したリストのバックアップも兼ねる (URL から再フォローで復元可能)。
  1 列目は Google 上の実際のリスト名 (「中区」「渋谷」等、全国で同名になり得る)。
  3 列目は同名エリアを区別するための所在地で、都道府県から始まる住所表記
  (`北海道` / `愛知県名古屋市` / `東京都港区` のように粒度はエリア種別で変わる) を入れる。
  3 列目は必須。区別が不要なエリアも都道府県名だけは入れる (`東京都` エリアの所在地は `東京都`)。
  Firestore ではドキュメント ID の一部になるため空を許さない。
  一意キーは (1 列目, 3 列目) の組。
- [`data/coords.tsv`](./data/coords.tsv) — エリアごとの中心座標。`エリア名 <TAB> 所在地 <TAB> 緯度 <TAB> 経度` の TSV。
  キーは `share-urls.tsv` と同じ (エリア名, 所在地) の組で、リスト種別の接尾辞は付かない。
  同一エリアでも 3 リストの中心は数 km ずれるため、トップリストの中心を代表値としている。
- [`data/seed.tsv`](./data/seed.tsv) — `fetch_missing_lists.py` の `SEED` に渡す新規エリア候補の一覧。
  試した検索語・所在地の記録を兼ねるため、取得済みの行も削除せず残している。

### 現在の内容 (2026-07-26)

571 件 / 191 エリア。内訳はトップリスト 191・トレンド 189・地元で人気 191。

- **端末にフォローして残しているのはトップリストのみ。** トレンド / 地元で人気 は共有 URL だけを保持している。
- 未取得は `代官山町: トレンド` と `東京: トレンド` (どちらも Google 側にリストが存在しないことを確認済み) の 2 件。
- 政令指定都市の区、同名の区 (中区・北区・南区・東区・西区・中央区・港区等)、主要都市の繁華街エリアを調査・追加済み。
  詳細な調査結果は [`docs/missing-areas.md`](./docs/missing-areas.md) を参照。

## Web アプリ

**公開先: https://google-maps-restaurant-list-finder-823271554794.asia-northeast1.run.app**

収集済みリストを地図から探せる Web アプリ。
正データは Firestore。設計と移行手順は [`docs/webapp-design.md`](./docs/webapp-design.md) を参照。

- [`scripts/store.py`](./scripts/store.py) — Firestore アクセスの集約先。ドキュメント ID の組み立て、所在地からの都道府県導出、upsert。`python3 scripts/store.py` で自己チェックが走る。
- [`scripts/import_tsv.py`](./scripts/import_tsv.py) — `share-urls.tsv` と `coords.tsv` を結合して Firestore の `lists` へ投入する。冪等。`--dry-run` で Firestore に触らず検証だけ行う。移行後も TSV からの復旧用に残す。
- [`cmd/server/main.go`](./cmd/server/main.go) — Cloud Run で動かす静的配信 + API サーバ。`GET /api/lists` / `POST /api/reports` / `GET /api/config`。
- [`cmd/server/web/index.html`](./cmd/server/web/index.html) — 単一 HTML のフロント。Maps JavaScript API + 都道府県 ▶ エリア ▶ リストの 3 階層ツリー + 報告フォーム。ビルド不要。

```bash
uv venv && uv pip install -r requirements.txt   # Python 側の依存
gcloud auth application-default login           # ローカル実行時の認証
cp .env.example .env                            # 環境変数を埋める
python3 scripts/import_tsv.py --dry-run         # TSV 側の検証のみ
go test ./... && go run ./cmd/server            # サーバ
```

### 環境変数

`.env` に書く。雛形は [`.env.example`](./.env.example)。`.env` はコミットしない (API キーが入るため)。

| 変数 | 用途 |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | **必須。** Firestore を置いている GCP プロジェクト |
| `GOOGLE_CLOUD_QUOTA_PROJECT` | API 呼び出しのクォータの付け先。ADC が別プロジェクトを向いているとき用 |
| `MAPS_API_KEY` | Maps JavaScript API のキー。空にすると地図が出ない (ツリーと検索は動く) |
| `MAPS_MAP_ID` | 省くと開発用の `DEMO_MAP_ID` にフォールバックする |
| `RECAPTCHA_SITE_KEY` | 空にすると bot 検証を飛ばす。ローカル開発時のみ空にする |
| `PORT` | サーバの待ち受けポート。省略時 8080 |

読み込みは Go が [godotenv](https://github.com/joho/godotenv)、Python が [python-dotenv](https://github.com/theskumar/python-dotenv)。
どちらも**既存の環境変数を上書きしない**ので、`MAPS_MAP_ID=xxx go run ./cmd/server` のように前置きすれば一時的に差し替えられる。
本番 (Cloud Run) は `.env` を持たず、`--set-env-vars` で渡した値で動く。

Python 側は `scripts/store.py` の位置から親を辿るためカレントディレクトリに依存しないが、
**Go 側はカレントディレクトリの `.env` しか見ない**のでリポジトリのルートから起動すること。

`MAPS_API_KEY` はクライアントに露出するので、GCP コンソール側で HTTP リファラ制限をかけること。

## ドキュメント

- [`docs/adb-workflow.md`](./docs/adb-workflow.md) — adb + uiautomator による一連の手順。
  リスト一覧の全件収集 (スワイプの慣性による取りこぼしと対策)、共有 URL の取得 (adb-clip)、リストの削除、
  欠落しているエリア別リストの追加取得 (geo: インテント・曖昧なエリア名の扱い) の実証結果・ハマりどころ。
- [`docs/webapp-design.md`](./docs/webapp-design.md) — 収集済みリストを地図から探せる Web アプリの設計と、正データを TSV から Firestore へ移す移行ガイド。
  構成 (Cloud Run + Firestore)、データモデル、画面設計、報告フォーム、共有 URL から座標を取得する際のハマりどころ。

## 前提

- 実機を adb (ワイヤレスデバッグ) で接続。ワイヤレスはポートが毎回変わるので都度確認する。
- adb 操作中は端末を画面オン + ロック解除のままにする (クリップボード読み取り・スリープ対策)。
- 収集される UI dump には連絡先候補等の個人情報が混じり得るため、dump をリポジトリやログに残さないこと。
