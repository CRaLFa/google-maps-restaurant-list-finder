# レストランリスト検索 Web アプリ — 設計と Firestore 移行ガイド

収集済みの Google マップ公式レストランリストを地図から探せる Web アプリを作る。
あわせて、一覧から漏れているリストを閲覧者が報告できるフォームを持たせる。

報告を受け付ける = 書き込みが発生するため、TSV の静的配信では成立しない。
これを機に正データを TSV から Firestore へ移す。

- 対象リポジトリ: このリポジトリ (`google-maps-restaurant-list-finder`) に統合する。
- 当初は Web アプリだけ別リポジトリに切ろうとしたが、TSV 受け渡しでの疎結合が前提だった。
  Firestore が正になると収集スクリプトと Web アプリが同じスキーマ・同じ GCP プロジェクト・同じ認証情報を共有するため、分けるとスキーマ定義が二重管理になる。
  デプロイと CI も 2 セット必要になる。
  よって統合し、リポジトリ名も Web アプリ側に寄せた (旧 `gmaps-list-manager`)。
  収集・削除スクリプトは移行後ほとんど使わない見込みで、日常的に触るのは Web アプリの方になるため。

## 決定事項

| 項目 | 決定 |
| --- | --- |
| 正データ | Firestore (TSV は初回インポートのみ、以後廃止) |
| インフラ | Cloud Run 1 サービス (静的配信 + API) + Firestore Native モード |
| フロント | 単一 HTML + Maps JavaScript API。ビルド無し |
| スパム対策 | reCAPTCHA Enterprise + サーバ側レート制限 |
| 報告項目 | エリア名・都道府県 (必須) / 共有 URL (任意) / コメント (任意) / 連絡先 (任意) |
| 地図 | Google マップ (Maps JavaScript API) + エリア単位のピン 191 個 |

### 却下した案

- **Leaflet + OpenStreetMap タイル** — 無料でキー不要だが、扱っているデータが Google マップのリストである以上、
  地図そのものも Google マップに揃える方が見た目とリンク先が一致する。
  Maps JavaScript API は従量課金だが、この規模なら無料枠に収まる。

- **Cloud SQL (PostgreSQL)** — 571 件 + 報告数十件に対して最小構成でも常時課金。この規模には重い。
- **Firebase Hosting から Firestore へ直接書き込み** — バックエンドコードが不要になる代わり、公開コレクションへの書き込み防御がセキュリティルールと App Check だけになる。
  サーバ側でのレート制限と項目検証を効かせたいので Cloud Run を挟む。
- **BigQuery** — 分析用途ではないうえ、単件書き込みに向かない。
- **静的 TSV のまま** — 報告の書き込みができない。

## 前提となる調査結果

### 共有 URL から緯度経度を取得できる (重要)

`https://maps.app.goo.gl/...` は最初 `https://www.google.com/maps/@/data=!4m3!11m2!2s<リストID>!3e3` へリダイレクトする。
この時点では `@` の後の座標が空。
その後 SPA のロードが完了すると `history.replaceState` で `@lat,lng,zoom` 付きの URL に書き換わる。

つまり **curl では座標を取得できない**。
実ブラウザで JS を実行させる必要がある。
`scripts/fetch_coords.py` が agent-browser 経由でこれを行う。

嵌まりどころが 2 つある。

- **同一タブで続けて開くと再センタリングが走らない。** SPA の soft navigation では地図の中心が更新されず、ブラウザの既定センター (東京駅付近) が残る。
  毎回 `about:blank` を挟んでからロードすること。
- **座標は 2 段階で変化する。** 既定センターが一度入ってから実際のリスト中心へ移る。
  値が安定するまでポーリングし、既定センターと緯度か経度が完全一致する間は未確定として捨てる。

curl で取得した HTML 内にも座標が 1 組だけ埋まっているが、これは全リストで同じ既定値であり使えない。
個別スポットの座標 (`!3d`/`!4d` 形式) は DOM に出てこない。

取得結果は `data/coords.tsv` に 191 エリア分そろっている。
緯度 26.21〜43.20 / 経度 127.65〜142.98 で全件が日本国内、重複なし、`share-urls.tsv` のエリアと過不足なく一致することを確認済み。

### 所在地から都道府県を機械的に取れる

3 列目 (所在地) は都道府県から始まる住所表記なので、先頭マッチで都道府県を切り出せる。

```
^(北海道|東京都|大阪府|京都府|.{2,3}?県)
```

現データの所在地 77 種のうち 76 種はこれで 45 都道府県に分類できる。
残り 1 種は所在地が空の `東京都` エリア (リスト名が「東京都: トップリスト」等) で、リスト名側から取れる。

**移行時に所在地を必須にすればこの例外は消える。**
`東京都` エリアの所在地を `東京都` で埋めること (後述)。

## データモデル

### コレクション `lists`

リスト 1 件 = 1 ドキュメント。現行 571 件。

| フィールド | 型 | 説明 |
| --- | --- | --- |
| `name` | string | Google 上の実際のリスト名。例 `渋谷: トップリスト` |
| `area` | string | エリア名。`name` の `: ` より前。例 `渋谷` |
| `kind` | string | `トップリスト` / `トレンド` / `地元で人気` |
| `loc` | string | 所在地。都道府県から始まる住所表記。例 `東京都渋谷区`。**必須** |
| `pref` | string | 都道府県。`loc` から導出して非正規化 |
| `url` | string | 共有 URL |
| `lat` / `lng` | number | エリア代表座標。同一エリアの 3 リストで同じ値 |
| `followed` | bool | 端末にフォローを残しているか (現状トップリストのみ true) |
| `updatedAt` | timestamp | 最終更新 |

ドキュメント ID は `{loc}|{name}` の決定論的な値にする。
一意キーが (リスト名, 所在地) の組であることをそのまま ID に落とすと、同期処理が単純な冪等 upsert で済む。
自動 ID にすると重複検出のためにクエリとトランザクションが要るため採用しない。

`lat` / `lng` はエリア単位の値をリスト側に非正規化して持つ。
`areas` コレクションを分けて join する構成も考えたが、読み取りが 1 コレクションで完結する利点の方が大きい。
571 件のうち座標が変わるのは新規エリア追加時だけで、更新コストは無視できる。

### コレクション `reports`

閲覧者からの報告。

| フィールド | 型 | 説明 |
| --- | --- | --- |
| `area` | string | 報告されたエリア名。**必須** |
| `pref` | string | 都道府県。**必須** |
| `shareUrl` | string | 共有 URL。任意 |
| `comment` | string | 自由記述。任意 |
| `contact` | string | 連絡先。任意 |
| `status` | string | `new` / `accepted` / `rejected` |
| `createdAt` | timestamp | 受信日時 |
| `recaptchaScore` | number | reCAPTCHA Enterprise のスコア |

**`contact` は個人情報。**
以下を守ること。

- Firestore セキュリティルールでクライアントからの `reports` の読み書きを全拒否する。
  書き込みは Cloud Run のサービスアカウント経由のみとする。
- ログに `contact` と `comment` の中身を出力しない。件数とドキュメント ID のみ記録する。
- TTL ポリシーで `createdAt` から 90 日後に自動削除する。

## API

Cloud Run の 1 サービスが静的ファイルと API の両方を返す。
静的ファイルは `//go:embed` でバイナリに埋め込み、単一成果物にする。

### `GET /api/lists`

`lists` 全件を JSON で返す。
571 件で数十 KB 程度なのでページングしない。
プロセス内メモリにキャッシュし、`Cache-Control: public, max-age=300` と `ETag` を付ける。

### `POST /api/reports`

1. reCAPTCHA Enterprise のトークンを検証する。スコアが閾値未満なら 403。
2. 項目を検証する。`area` / `pref` 必須、`pref` は 47 都道府県のいずれか、各項目の最大長を制限、`shareUrl` は指定時のみ `https://maps.app.goo.gl/` または `https://www.google.com/maps/` で始まることを確認する。
3. レート制限に引っかかれば 429。
4. `reports` に書き込み、`{"ok": true}` を返す。ドキュメント ID は返さない。

レート制限はプロセス内カウンタで実装する。
Cloud Run のインスタンスが複数に増えると上限が実質インスタンス数倍になるが、`--max-instances` を小さく設定すれば実用上問題ない。
これが効かなくなるほど報告が来るようなら Firestore か Memorystore のカウンタへ移行する。

## 画面設計

```
┌──────────────────────────────────────┬──────────────────┐
│ [🔍 地名で検索                    ]   │ ▶ 東京都 (83)     │
│                                      │ ▶ 大阪府 (57)     │
│        Google マップ                  │ ▼ 京都府 (39)     │
│      ピン 191 個 (エリア単位)          │    京都市         │
│                                      │      トップリスト  │
│   ピンをクリック → そのエリアの        │      トレンド      │
│   3 リストへのリンクをポップアップ       │      地元で人気    │
│                                      │ ▶ 神奈川県 (21)   │
│                                      ├──────────────────┤
│                                      │ [漏れているリストを報告] │
└──────────────────────────────────────┴──────────────────┘
        約 80%                                  約 20%
```

- 右ツリーは **都道府県 ▶ エリア ▶ リスト 3 種** の 3 階層。
  `<details>` / `<summary>` を使い、開閉のための JS を書かない。
- 都道府県の並びは代表緯度の降順 (北から南)。
  座標があるのでタダで出せる。JIS 都道府県コードの配列を持たずに済む。
- 検索ボックスは地名のインクリメンタル検索。
  ヒットしたエリアだけにツリーとピンを絞る。
  判定はエリア名の部分一致、**都道府県名の先頭一致**、所在地の市区部分の部分一致の 3 本。
  所在地を丸ごと部分一致にすると「京都」が「東京都」に当たり、京都府の 14 件が東京都の 60 件に埋もれる。
  都道府県だけ先頭一致にすると 73 件が 15 件に減り、「愛知」で名古屋市の区を引ける利点は残る。
- ピンのクリックとツリーの項目クリックは相互に連動させる。
- ピンは `AdvancedMarkerElement`、吹き出しは `InfoWindow` を使う。
  `AdvancedMarkerElement` には Map ID が要るので、環境変数 `MAPS_MAP_ID` で渡す。
  未設定時は開発用の `DEMO_MAP_ID` にフォールバックする。
- 191 個のピンは東京周辺で重なるが、クラスタリングライブラリは入れない。
  ズームで分離できる。実際に使いづらければ後から入れる。
- 報告フォームは右パネル下部のボタンから開くダイアログ。
  `<dialog>` 要素を使う。

### スマートフォン表示

幅 8:2 の横並びは幅 400px で確実に破綻する。
別レイアウトは作らず、メディアクエリ 1 個で縦積みに切り替える。

```
@media (max-width: 768px) {
  /* 縦積み。地図 50vh、残りをツリー */
}
```

- 地図を上 50vh、ツリーを下に置く。
- 検索ボックスは地図の上に固定する。
- 報告フォームのダイアログは全画面表示にする。

## 移行ガイド

### フェーズ 0: GCP 準備

1. プロジェクトを作成し、課金を有効にする。
2. API を有効化する。

```bash
gcloud services enable \
  firestore.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  recaptchaenterprise.googleapis.com \
  maps-backend.googleapis.com
```

3. Firestore を Native モードで作成する (リージョンは `asia-northeast1`)。

```bash
gcloud firestore databases create --location=asia-northeast1 --type=firestore-native
```

4. Cloud Run 用のサービスアカウントを作り、`roles/datastore.user` を付与する。
5. reCAPTCHA Enterprise のスコアベースのサイトキーを作成する。
6. Maps JavaScript API の API キーを作る。
   キーはクライアントに露出するので、**必ず HTTP リファラ制限をかけ、対象 API を Maps JavaScript API だけに絞る**。
   あわせてコンソールで Map ID (ラスター/ベクター) を作る。`AdvancedMarkerElement` に必要。
7. Firestore セキュリティルールを「全拒否」にする。
   クライアントは Firestore に直接触らず、必ず Cloud Run の API を通す。

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} { allow read, write: if false; }
  }
}
```

### フェーズ 1: データ整備 (TSV のまま実施)

Firestore へ入れる前に、TSV 側で以下を済ませておく。

1. **所在地を必須にする。**
   現在は「区別が不要なエリアは 3 列目を空にする」仕様だが、Firestore ではドキュメント ID の一部になるため空を許さない。
   該当は `東京都` エリアの 3 件のみ。所在地を `東京都` で埋める。
   `scripts/set_locations.py` の `METRO` にエントリを追加して再実行する。
2. `scripts/normalize_tsv.py` を流して 3 カラムに揃える。
3. `scripts/fetch_coords.py` を流して `data/coords.tsv` を最新にする (現状 191 エリア分は取得済み)。

### フェーズ 2: 初回インポート

`scripts/import_tsv.py` (新規) で `share-urls.tsv` と `coords.tsv` を結合して Firestore に投入する。

- 結合キーはエリア名と所在地の組。
- ドキュメント ID は `{loc}|{name}`。
- `pref` は所在地の先頭マッチで導出する。
- `followed` は現状のルール (`kind == "トップリスト"` のみ true) で埋める。
- 冪等にする。再実行しても差分だけが当たるよう `set()` で upsert する。
- 投入後、件数が 571、エリア数が 191、`lat`/`lng` の欠損が 0 であることを検証してから次へ進む。

このスクリプトは移行後も「TSV からの復旧」用に残す。

### フェーズ 3: 収集スクリプトの改修

TSV 読み書きを Firestore アクセスに差し替える。
各スクリプトに Firestore クライアントを直接書くと重複するため、共通の `scripts/store.py` を作って読み書きをそこへ集約する。

| スクリプト | 現状 | 移行後 |
| --- | --- | --- |
| `fetch_share_urls.py` | `data/share-urls.tsv` に追記。既知の名前をスキップ | `store.all_lists()` で既知を読み、`store.upsert()` で記録 |
| `fetch_missing_lists.py` | TSV を読んで欠落エリアを算出、追記 | 同上。欠落算出は Firestore からの読み取りに |
| `delete_lists.py` | TSV を読み、未記録のものは追記 | 同上。削除時は `store.set_followed()` で false に更新 |
| `fetch_coords.py` | `data/coords.tsv` に追記 | `store.update_coords()` で該当エリアの全リストの `lat`/`lng` を更新 |
| `set_locations.py` | 3 列目をセット | 対応表を `locations.py` へ切り出し。移行後は新規エリア追加時のみ使用 |
| `normalize_tsv.py` | 3 カラム正規化と並べ替え | **廃止。** Firestore にカラムと行順の概念が無い |

改修の順序は `store.py` → `fetch_coords.py` (最小) → `fetch_share_urls.py` → `fetch_missing_lists.py` → `delete_lists.py` を推奨する。
`fetch_coords.py` が一番小さく、Firestore アクセス層の検証台として使える。

ローカル実行時の認証は `gcloud auth application-default login` で足りる。

#### 所在地の解決を `locations.py` に切り出す

移行前は「共有 URL だけ先に TSV へ追記し、あとで `set_locations.py` が 3 列目を埋める」二段構えだった。
Firestore では所在地がドキュメント ID の一部なので、記録の時点で決まっていなければならない。

そこで `set_locations.py` が持っていた対応表 (`CITIES` / `WARDS` / `DISTRICTS` / `METRO`) を `scripts/locations.py` へ切り出し、収集スクリプトが `locations.resolve(エリア名)` で所在地を引けるようにする。

- 端末の UI 上は同名の区を見分けられないため、**中央区・北区は `resolve()` が `None` を返す**。
  この 2 種は所在地を手で与えるしかない。
- 所在地が決まらないリストは Firestore に書かず、名前と URL を警告に出して飛ばす。
  保留の仕組みは持たない。`locations.py` を直して再実行すれば取り直せるうえ、1 件数秒で済むため。
- `fetch_missing_lists.py` は端末を触る前に対象を絞り、決まらないエリアを先にまとめて報告する。
- 逆方向の漏れ (データにあるのに `locations.py` が知らないエリア) は `set_locations.py` が検出して落とす。

### フェーズ 4: Web アプリ

1. `cmd/server/web/index.html` — 単一 HTML。Maps JavaScript API を CDN から読む。
2. `cmd/server/main.go` — 静的配信 + 上記 2 エンドポイント。`//go:embed all:web`。
3. デプロイ。

```bash
gcloud run deploy google-maps-restaurant-list-finder \
  --source . \
  --region asia-northeast1 \
  --service-account <SA> \
  --max-instances 2 \
  --allow-unauthenticated \
  --set-build-env-vars GOOGLE_BUILDABLE=./cmd/server \
  --set-env-vars GOOGLE_CLOUD_PROJECT=<PROJECT>,MAPS_API_KEY=<KEY>,MAPS_MAP_ID=<MAP_ID>,RECAPTCHA_SITE_KEY=<SITE_KEY>
```

`GOOGLE_BUILDABLE` は必須。
Go の buildpack はリポジトリ直下の main パッケージを探すが、ここでは `cmd/server` にしかないため、指定しないとビルドが失敗する。

`GET /api/config` がフロントへ `MAPS_API_KEY` / `MAPS_MAP_ID` / `RECAPTCHA_SITE_KEY` を返す。
いずれも公開前提の値だが、Maps の API キーだけはリファラ制限が唯一の防御なので必ずかけること。
`RECAPTCHA_SITE_KEY` を空にすると bot 検証を飛ばす。ローカル開発専用の逃げ道であり、本番では必ず設定する。

キーの発行はサービスの URL が確定してから行う。
リファラ制限も reCAPTCHA のドメイン登録も URL が要るため、先にキーを作ると制限のかかっていない期間ができてしまう。

1. 環境変数は `GOOGLE_CLOUD_PROJECT` だけ渡して一度デプロイし、`*.run.app` の URL を確定させる。
2. その URL を条件に Maps の API キー (リファラ制限 + Maps JavaScript API のみ) と reCAPTCHA のサイトキーを作る。
3. `gcloud run services update` で残りの環境変数を入れる。

### フェーズ 5: TSV 廃止

Firestore が正として回り始めたことを確認してから実施する。

- `data/share-urls.tsv` と `data/coords.tsv` を `data/archive/` へ移し、履歴として残す。
- `README.md` の「データファイル」節を Firestore のデータモデルの説明に差し替える。
- `normalize_tsv.py` を削除する。
- 定期バックアップとして Firestore のエクスポートをスケジュールする。

```bash
gcloud firestore export gs://<BUCKET>/backup/$(date +%Y%m%d)
```

**フェーズ 5 は不可逆。**
バックアップの取得と復旧手順 (`import_tsv.py` またはエクスポートからのリストア) の動作確認を済ませてから進めること。

## 運用: 報告のトリアージ

1. `reports` の `status == "new"` を一覧する管理コマンドを用意する (`scripts/list_reports.py`)。
2. 報告に共有 URL が付いていればそのまま `lists` に登録できる。
   付いていなければ `data/seed.tsv` にエリアを追記し、`fetch_missing_lists.py` で adb 経由で取得する。
3. 反映したら `status` を `accepted` に、不要なら `rejected` に更新する。

管理画面は作らない。
報告が日に数件あるかどうかの規模で、CLI で足りる。
件数が増えて CLI が苦しくなったら考える。

## 未決事項

- Cloud Run にカスタムドメインを当てるか、`*.run.app` のままにするか。
- 報告の受付を身内に限定するか、URL を公開するか。
  公開する場合は reCAPTCHA のスコア閾値を実データを見ながら調整する必要がある。
- 191 個のピンの重なりが実使用で許容できるか。
  許容できなければ `@googlemaps/markerclusterer` を追加する。
