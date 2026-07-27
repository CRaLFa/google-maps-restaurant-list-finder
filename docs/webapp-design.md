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
┌──────────────────┬──────────────────────────────────────┐
│ [🔍 地名で検索  ] │                                      │
├──────────────────┤                                      │
│ ▶ 東京都 (60)     │        Google マップ                  │
│ ▼ 愛知県 (14)     │      ピン 191 個 (エリア単位)          │
│   ▼ 名古屋市 (10) │                                      │
│     ▼ 中区 (4)    │   ピンをクリック → そのエリアの        │
│       ▼ 栄        │   3 リストへのリンクをポップアップ       │
│         トップリスト│                                      │
│         トレンド   │                                      │
│         地元で人気 │                                      │
│ ▶ 京都府 (14)     │                                      │
├──────────────────┤                                      │
│ [漏れているリストを報告] │                                │
└──────────────────┴──────────────────────────────────────┘
        約 20%                        約 80%
```

- ツリーを左、地図を右に置く。
  地図が主役の不動産系 (Zillow / Redfin) は地図が左だが、このアプリは「リストのインデックスを引く」道具で、地図は位置の確認用。
  検索結果を読ませる系 (Google マップ本体・Airbnb) の並びに揃える。
  地図を Google マップにした理由と同じく、本家と同じ配置にしておく方が迷わない。

- ツリーは **都道府県 ▶ (市 ▶ 区 ▶) エリア ▶ リスト 3 種** の可変段数。
  `<details>` / `<summary>` を使い、開閉のための JS を書かない。
- 都道府県の並びは代表緯度の降順 (北から南)。
  座標があるのでタダで出せる。JIS 都道府県コードの配列を持たずに済む。
  中間のノードも同じ規則で並べる。代表緯度は自身と子孫の最大値とする。
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
- 報告フォームは左パネル下部のボタンから開くダイアログ。
  `<dialog>` 要素を使う。

### ツリーの階層は所在地から導ける

`loc` は **そのエリアの親のフルパス** になっている。
池袋の所在地は `東京都豊島区`、豊島区の所在地は `東京都`、栄の所在地は `愛知県名古屋市中区` という具合。

つまり `loc + area` を各ノードのフルパスとして扱えば、親子関係がそのまま木になる。
**データモデルもドキュメント ID も変更しなくてよい。**
親は「自分より短い既知のパスのうち最長のもの」、ラベルは親のパスを差し引いた残りで機械的に決まる。

- 191 エリアのうち 114 件は親が都道府県で、従来と同じ深さのまま。最大の深さは 都道府県 ▶ 市 ▶ 区 ▶ エリア の 4 段。
- **リストを持たない中間ノードが 6 件必要になる。**
  `東京都台東区` / `東京都大田区` / `神奈川県横浜市中区` / `愛知県名古屋市中村区` / `兵庫県神戸市中央区` は上野や浅草の親だが、区そのものは収集対象になっていない。
  `loc` の値もノードとして起こしておけば同じ規則で埋まる。
- 件数は子孫のエリア数の合計を表示する。子を持たない末端には出さない。
- 検索でヒットしたノードを表示する際は、祖先を全部開く。
  ピンのクリックからツリーを開くときも同様に親を辿る。

階層の組み立ては壊れても画面が出てしまうため、`cmd/server/tree_check.js` で実データに対して形を検証する。
`index.html` の `<script>` をそのまま読み込んで動かすので、検証用のロジックの二重管理にならない。

### ダークモード

ブラウザがダークモードなら地図もダークにする。
スタイル JSON も Cloud Styling も要らず、`Map` のオプション 1 行で済む。

```js
colorScheme: google.maps.ColorScheme.FOLLOW_SYSTEM,
```

`InfoWindow` やコントロール類も一緒に切り替わる。
**初期化時にしか効かない**ので、表示中に OS の設定が変わっても地図は追随しない。
追随させるにはマップを作り直すことになるが、そこまではやらない。

自前の UI 側は CSS 変数を `@media (prefers-color-scheme: dark)` で上書きする。
あわせて `:root` に `color-scheme: light dark` を宣言する。
これでフォーム部品とスクロールバーが OS の設定に追随するため、入力欄の配色を自分で書かずに済む。

### スマートフォン表示

幅 2:8 の横並びは幅 400px で確実に破綻する。
別レイアウトは作らず、メディアクエリ 1 個で縦積みに切り替える。

```
@media (max-width: 768px) {
  /* 縦積み。地図 50vh、残りをツリー */
}
```

- 地図を上 50vh、ツリーを下に置く。
  DOM の順序はツリーが先なので、`#map { order: -1 }` で入れ替える。
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
gcloud firestore databases create --database=restaurant-lists \
  --location=asia-northeast1 --type=firestore-native
```

**`--database` を付けて名前付きデータベースにする。**
`--database` を省くと `(default)` になるが、`(default)` は**リネームできない**。
`gcloud firestore databases` に `rename` は無く、`update` で変えられるのは
`--concurrency-mode` / `--delete-protection` / `--enable-pitr` / `--type` だけ。
後から名前を付けたくなったら、新しいデータベースを作って移し替え、`(default)` を消すしかない。

同じプロジェクトに別用途のデータベースを足す可能性があるなら、最初から名前を付けておくこと。
名前付きにするとクライアント側で明示指定が必要になる (`FIRESTORE_DATABASE` 環境変数)。

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
| `set_locations.py` | 3 列目をセット | 対応表を `locations.py` へ切り出したうえで**廃止** (フェーズ 5)。TSV が無くなり入力を失った |
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
- 逆方向の漏れ (データにあるのに `locations.py` が知らないエリア) は移行中は `set_locations.py` が検出していた。
  フェーズ 5 で同スクリプトを廃止したため、この検査は無くなっている。
  収集時に `resolve()` が `None` を返せばその場で警告が出るので、実害が出るのは
  すでに記録済みのエリアの対応表を後から壊した場合だけ。必要になったら書き直す。

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
  --set-env-vars GOOGLE_CLOUD_PROJECT=<PROJECT>,FIRESTORE_DATABASE=restaurant-lists,MAPS_API_KEY=<KEY>,MAPS_MAP_ID=<MAP_ID>,RECAPTCHA_SITE_KEY=<SITE_KEY>
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

### フェーズ 5: TSV 廃止 (実施済み)

着手前に前提を確認した。
Firestore に 571 件 / 191 エリア、座標と所在地の欠損 0。
`import_tsv.py --dry-run` も同じ件数で通り、TSV からの復旧経路が生きていることを確認済み。

実施した内容。

- `data/share-urls.tsv` と `data/coords.tsv` を `data/archive/` へ移し、凍結した。
  参照しているのは `import_tsv.py` (復旧用) と `cmd/server/tree_check.js` (階層のフィクスチャ) だけ。
- `docs/development.md` の「データファイル」節を Firestore のデータモデルの説明に差し替えた。
  README には移行前から該当の節が無い (公開向けに整理した際に開発ガイドへ移してある)。
- `normalize_tsv.py` を削除した。
- `set_locations.py` も削除した。
  TSV の 3 列目を埋めるだけのスクリプトで、入力が無くなった。
  移行後に必要なのは `locations.py` のテーブル更新だけで、所在地の解決は収集スクリプトが実行時に行う。
- 定期バックアップを設定した (後述)。日次・保持 7 日。

#### バックアップは GCS へのエクスポートではなくスケジュールバックアップにした

当初はエクスポートを Cloud Scheduler で回す想定だったが、Firestore に組み込みのスケジュールバックアップがある。

```bash
gcloud firestore backups schedules create --database=restaurant-lists \
  --recurrence=daily --retention=7d
```

- **GCS バケットも Cloud Scheduler もサービスアカウントも要らない。** コマンド 1 個で完結する。
- リストアは `gcloud firestore databases restore` で行う。

引き換えに、**リストア先は必ず新しいデータベースになる。**
既存の `restaurant-lists` に上書きはできない。
復旧時は新しい名前でリストアして `FIRESTORE_DATABASE` を向け直す。
データベース名を環境変数にしてあるのがそのまま効く。

`import_tsv.py` による `data/archive/` からの復元は最後の手段として残す。
移行時点の 571 件に戻るだけで、それ以降に増えたエリアは復旧できない。

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

## 今後やりたいこと

移行が済んで動き始めた後の課題。
優先順位は付けていない。

### CI/CD の整備 (Cloud Build)

現状はローカルから `gcloud run deploy --source .` を手で叩いている。
テストも手で流しているため、通し忘れたまま push できてしまう。

**Cloud Build を使う。**
GitHub Actions ではなく Cloud Build を選ぶ理由は 3 つ。

- **すでに使っている。** `gcloud run deploy --source .` は内部で Cloud Build を回している。
  API は有効化済みで、Artifact Registry の `cloud-run-source-deploy` も出来上がっている。
- **Firestore に触るテストが回せる。**
  これが一番大きい。
  Cloud Build のサービスアカウントに `roles/datastore.user` を付ければ、CI から実際の Firestore を読める。
  GitHub Actions から同じことをするには Workload Identity 連携の設定が要る。
- **デプロイ先が同じプロジェクトにある。** 権限を跨がない。

GitHub Actions の利点は public リポジトリなら実行が無料な点だが、
Cloud Build にも無料枠があり、この規模の実行頻度なら問題にならない。

```yaml
# cloudbuild.yaml (骨子)
steps:
  - name: golang:1.26
    args: [go, vet, ./...]
  - name: golang:1.26
    args: [go, test, ./...]
  - name: python:3.12
    script: pip install -r requirements.txt && python scripts/locations.py && python scripts/store.py
  - name: python:3.12
    script: pip install -r requirements.txt && python scripts/import_tsv.py --dry-run
  - name: node:22
    args: [node, cmd/server/tree_check.js]
  # デプロイは main への push のときだけ
```

GitHub のリポジトリと繋ぐには Cloud Build のトリガを作る。
`master` への push で発火させ、PR では検査だけ回してデプロイしない。

Firestore に接続しないチェック (`locations.py` / `store.py` の自己チェック、`import_tsv.py --dry-run`、`tree_check.js`) だけでも、
所在地の解決・ドキュメント ID の組み立て・TSV の整合・ツリーの階層はカバーできる。
サービスアカウントに権限を付けるかどうかは、実データを読むテストを書きたくなってから決めればよい。

### フロントの TypeScript 化と CSS ライブラリの導入

目的は 2 つある。

- **型による検査と補完。**
  現状 `areas` / `prefs` の要素の形はコメントでしか表現されていない。
  Firestore のドキュメントの形が変わっても、フロントは実行するまで壊れたことに気付けない。
- **CSS ライブラリを入れる。**
  現在の CSS は手書きで、レイアウトは flexbox とメディアクエリ 1 個。

決定事項として「ビルド無し」を選んでいたが、**これを撤回する**ことになる。

#### ビルドが要るかは CSS ライブラリの選択で決まる

TypeScript 化だけなら `tsc --noEmit` で型検査だけ行い、配信は手書き JS のまま、という逃げ道がある。
だが CSS ライブラリを入れるなら選択肢で分かれる。

| ライブラリ | ビルド | 備考 |
| --- | --- | --- |
| Pico.css / Water.css | 不要 | クラスレス。CDN の 1 行で既存の HTML の見た目が変わる |
| Bulma / Bootstrap | 不要 | CDN の 1 行。クラスを付けて回る必要がある |
| Tailwind CSS | **必要** | PostCSS を通す。CDN 版 (Play CDN) は本番非推奨 |

**「見た目を整えたいだけ」ならクラスレス CSS で足りる。**
現在の画面は地図・ツリー・ダイアログの 3 要素しかなく、凝ったコンポーネントを必要としていない。
Pico.css なら `<link>` 1 行で、`<details>` も `<dialog>` も整った見た目になる。

ビルドを入れると決めたなら、その先は素直に進む。

#### ビルドを入れる場合の構成

**Vite を使う。** TypeScript と CSS の両方を 1 つの設定で扱えるため。
esbuild 単体だと Tailwind の PostCSS を別に回すことになる。

```
web/            ← ソース (index.html / main.ts / style.css)
cmd/server/web/ ← Vite の出力先。//go:embed all:web の対象はここのまま
```

**帰結として `gcloud run deploy --source .` が使えなくなる。**
Go の buildpack は Node のビルドを知らないので、`cmd/server/web/` が空のままイメージが作られてしまう。
対処は 2 つ。

- **Cloud Build の複数ステップにする。** Node でビルド → Go でビルド → デプロイ。
  上記の CI/CD を Cloud Build で組むなら、そこにステップを足すだけで済む。
- **Dockerfile のマルチステージにする。** `--source` デプロイのまま使えるが、Dockerfile を持つことになる。

CI/CD を Cloud Build にする判断と噛み合うので、前者を推す。
生成物をリポジトリにコミットせずに済む点も良い (`cmd/server/web/` を `.gitignore` に入れる)。

### 報告が追加されたときの通知

現在 `reports` は `status == "new"` のまま溜まるだけで、気付く手段が無い。
トリアージ用の `scripts/list_reports.py` も未実装。

**`POST /api/reports` のハンドラ内から直接送る (決定)。**
Firestore トリガの Cloud Functions (Eventarc) や Pub/Sub 経由も考えられるが、
報告が日に数件あるかどうかの規模でデプロイ単位と権限設定を増やす価値がない。

守ること。

- **通知の失敗で報告そのものを落とさない。**
  Firestore への書き込みが成功した後に通知を試み、失敗してもレスポンスは 200 のままログに残す。
- **`contact` と `comment` を通知本文に載せない。**
  `contact` は個人情報で、ログにも出さない方針にしてある。
  通知にはドキュメント ID・`area`・`pref` だけを載せ、中身は Firestore を直接見に行く。
- **通知の送信でレスポンスを待たせない。**
  goroutine に逃がし、`context.Background()` を使う (リクエストの context は応答後に切れる)。

#### 送信手段: メールにするなら外部サービスを使う

蓄積されたものを後から確認できる点でメールが向いているが、
**Google Cloud にメール送信サービスは無い。**
これが「面倒」の正体で、回避策も限られる。

| 手段 | 評価 |
| --- | --- |
| 外部のメール API (Resend / SendGrid / Mailgun) | **これが素直。** HTTPS で POST 1 回。無料枠で足りる |
| Gmail API + サービスアカウント | ドメイン全体の委任が要る。Workspace 前提で個人プロジェクトには重い |
| SMTP を直接叩く | Cloud Run は 25 番ポートを塞いでいる。587 は通るが認証情報の管理が増える |

外部サービスを 1 つ挟むことになるが、実装量は Webhook とほぼ同じ (JSON を POST するだけ)。
API キーは Secret Manager に置き、Cloud Run から参照する。
環境変数に直書きしてもよいが、他のキーと違いこれは**公開前提の値ではない**ので分けて扱う。

Slack / Discord の Webhook なら外部サービスの登録すら要らず、URL に POST するだけで済む。
履歴も残るので「蓄積されたものを確認する」目的は満たせる。
メールの受信箱に集めたいかどうかの好みで決めればよい。
