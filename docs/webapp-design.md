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
| フロント | 単一 HTML + Maps JavaScript API + Pico.css (CDN)。ビルド無し |
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
`scripts/collect/fetch_coords.py` が agent-browser 経由でこれを行う。

嵌まりどころが 3 つある。

- **同一タブで続けて開くと再センタリングが走らない。** SPA の soft navigation では地図の中心が更新されず、ブラウザの既定センター (東京駅付近) が残る。
  毎回 `about:blank` を挟んでからロードすること。
- **座標は 2 段階で変化する。** 既定センターが一度入ってから実際のリスト中心へ移る。
  値が安定するまでポーリングし、既定センターと緯度か経度が完全一致する間は未確定として捨てる。
- **URL の中心は左パネルの分だけ西にずれている (要補正)。**
  Google マップは左にリストのパネル (実測 480px) を重ねて表示し、リストの範囲はパネルに隠れていない可視領域に合わせて収める。
  一方 URL の `@lat,lng` は地図キャンバス全体の中心なので、常にパネル幅の半分 = 240px だけ西を指す。
  ずれは画素で一定なので、度数ではズームが浅い (= 広い) エリアほど大きくなる。
  補正式は `lng += panel/2 * 360 / (256 * 2^zoom)` (Web メルカトルのタイルは 256px)。
  パネルは全高を占めるため縦のずれは無く、緯度は補正しない。
  可視領域の幅が変わると Google が選ぶズームも変わって値が再現しないので、窓の大きさは固定して取得する。

3 つ目は当初見落としていた。
補正前のデータは OSM の行政境界と突き合わせると 155 エリア中 153 件が参照点より西に寄り (緯度には偏りが無い)、
ずれの中央値は 3.5km、最大は 30km (大津市) だった。
同じ共有 URL を開き直すと緯度は完全に一致するのに経度だけ動くことが、ずれが場所の性質ではなく採取時のブラウザの都合であることの裏付けになる。

補正を入れて 191 エリアを取り直した結果、境界の外に出ていた 34 エリアが 0 になり、
「参照点より西」は 153/155 件から 78/155 件 (ほぼ半々) に、経度差の中央値は -0.0385 度 (3.5km) から -0.0003 度 (27m) になった。
偏りが消えて残りがばらつきだけになったので、補正式は妥当と判断した。

curl で取得した HTML 内にも座標が 1 組だけ埋まっているが、これは全リストで同じ既定値であり使えない。
個別スポットの座標 (`!3d`/`!4d` 形式) は DOM に出てこない。

取得結果は 191 エリア分そろっている (復旧用のスナップショットは [`data/archive/coords.tsv`](../data/archive/coords.tsv)。パネル補正後の値に更新済み)。
緯度 26.22〜43.21 / 経度 127.69〜143.15 で全件が日本国内、重複なし、`share-urls.tsv` のエリアと過不足なく一致することを確認済み。

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

リスト 1 件 = 1 ドキュメント。移行時点で 571 件 (現在の件数は [README](../README.md) を参照)。

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

`{"lists": [...], "order": {...}}` を返す。
`lists` は全件、`order` はツリーの並び順に使う「都道府県・市区町村のフルパス -> 順位」で、`lists` に出てくるパスの分だけ入る。
597 件で 140KB 程度なのでページングしない。
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
- 都道府県と市区町村の並びは**全国地方公共団体コード順**。住所の並びとして見慣れた順序に揃える。
  表は総務省の Excel から起こした `cmd/server/muni-order.txt` (`コード<TAB>フルパス` を 1,955 行)。
  比べるのは兄弟どうしだけなので、コードをそのまま順位に使える。
  ブラウザには全件 (54KB) を送らず、リストが実在するパスの分だけ `/api/lists` の `order` に載せる (約 6KB)。
  完全な表をサーバに置くことで、新しいエリアを追加したときの表の追記が要らなくなる。
- **静的な地名データの持ち場はこのファイル 1 つ**にする。
  報告フォームの 47 都道府県も、市区町村コードの下 3 桁が `000` の行として同じファイルから取り出し、`/api/config` でフロントに配る。
  同じ一覧を Go とフロントの両方に書くと、片方だけ古くなる。
- 繁華街などのエリアと、表に無い自治体の並びは代表緯度の降順 (北から南)。
  座標があるのでタダで出せる。代表緯度は自身と子孫の最大値とする。
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
- **リストを持たない中間ノードが 5 件必要になる。**
  `東京都台東区` / `東京都大田区` / `神奈川県横浜市中区` / `愛知県名古屋市中村区` / `兵庫県神戸市中央区` は上野や浅草の親だが、区そのものは収集対象になっていない。
  `loc` の値もノードとして起こしておけば同じ規則で埋まる。
- 件数は子孫のエリア数の合計を表示する。子を持たない末端には出さない。
- 検索でヒットしたノードを表示する際は、祖先を全部開く。
  ピンのクリックからツリーを開くときも同様に親を辿る。

階層の組み立ては壊れても画面が出てしまうため、`cmd/server/tree_check.js` で実データに対して形を検証する。
`index.html` の `<script>` をそのまま読み込んで動かすので、検証用のロジックの二重管理にならない。

### CSS ライブラリの選定

クラスレス CSS の **Pico.css** を CDN から読み込む。
候補は Simple.css / Water.css / Pico.css の 3 つだった。

| | Water.css | Simple.css | Pico.css |
| --- | --- | --- | --- |
| レイアウトへの介入 | `body { max-width: 800px }` | `body` を grid 化して中央 1 カラム | クラスレス版のみ。通常版は介入なし |
| `<dialog>` | ほぼ素のまま | ほぼ素のまま | モーダルとして完成 |
| `<details>` | 素のまま | 軽く装飾 | アコーディオンとして装飾 |
| 保守状況 | ほぼ停滞 | 活発 | 活発 |

報告フォームは `<dialog>`、ツリーは `<details>` を使っているため、この 2 つが最初から仕上がっている Pico を選んだ。
Simple.css は `body` を中央 1 カラムに押し込むので、全画面 2 ペインのこの画面とは相性が悪い。

**クラスレス版 (`pico.classless.css`) ではなく通常版を使う。**
クラスレス版は `body` 直下の `header` / `main` / `footer` を中央寄せのコンテナにしてしまう。
通常版はコンテナが `.container` を付けたときだけ効くため、レイアウトに触られずに素の要素の見た目だけを受け取れる。

Pico の既定と噛み合わず、打ち消しが必要だった箇所が 4 つある。
いずれも `index.html` の `<style>` にコメント付きで残してある。

- **ピンの背後に青い矩形が出る。**
  `AdvancedMarkerElement` は `<gmp-advanced-marker role="button">` として描かれるため、Pico の `[role=button]` の規則に巻き込まれる。
  `padding` も付いて当たり判定が広がり、ピンの指す座標までずれる。
  地図のコントロール (`.gm-control-active`) は Google 側の CSS が詳細度で勝つため無傷。
- **入れ子のツリーで `details` の下線が階層ごとに引かれる。**
- **リンクに行頭記号が出る。**
  Pico は `li` 自身に `list-style` を持たせるため、`ul` 側で `none` にしても消えない。
- **検索欄に虫眼鏡が 2 つ出る。**
  Pico が `input[type=search]` に描くので、プレースホルダの絵文字は置かない。

**字の大きさはサイドバーと `InfoWindow` の中だけ 16px に固定する。**
Pico は画面幅に応じて root の `font-size` を上げる (1280px 以上で 125%)。
本文を読ませるページ向けの挙動で、一覧を詰めて並べる用途には大きすぎる。
`#side` に指定するだけではフォーム部品に届かないため (Pico が `input` / `button` に `font-size: 1rem` を明示している)、`#side input, #side button { font-size: inherit }` を併せて入れる。

### ダークモード

ブラウザがダークモードなら地図もダークにする。
スタイル JSON も Cloud Styling も要らず、`Map` のオプション 1 行で済む。

```js
colorScheme: google.maps.ColorScheme.FOLLOW_SYSTEM,
```

`InfoWindow` やコントロール類も一緒に切り替わる。
**初期化時にしか効かない**ので、表示中に OS の設定が変わっても地図は追随しない。
追随させるにはマップを作り直すことになるが、そこまではやらない。

自前の UI 側は Pico.css に任せる。
`color-scheme: light dark` の宣言も `@media (prefers-color-scheme: dark)` での上書きも Pico が持っているため、こちらは `--bg` 等を `--pico-*` の別名として定義するだけでよい。

ただし地図の `colorScheme` はタイルしか暗くせず、`InfoWindow` の吹き出しは白のまま残る。
中の文字だけダークモードの明るい色になって白地に白文字で消えるため、吹き出し側は `gm-style-iw-*` を名指しして暗くしている。
Google の内部クラスなので、変わって白に戻ることがありうる。
そのとき文字が消えないよう `.popup` 自身にも背景を持たせ、最悪でも白い吹き出しの中の暗い箱として読めるようにしてある。

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

**縦積みでは `#side` に `min-height: 0` が要る。**
flex アイテムの `min-height: auto` は「中身の最小高さ」に解決されるため、`#side` の下限が `#search` + `#tree` の中身ぜんぶ + `footer` になり、`flex: 1` で縮められなくなる。
そうなると `#tree { overflow-y: auto }` の出番が無く、ページ全体がスクロールして地図が画面外に流れ、報告ボタンもツリーの末尾まで送られてしまう。
PC 表示で起きないのは、この下限が効くのが flex の主軸だけだから (`row` では横方向)。

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
3. `scripts/collect/fetch_coords.py` を流して `data/coords.tsv` を最新にする (現状 191 エリア分は取得済み)。

### フェーズ 2: 初回インポート

`scripts/restore/import_tsv.py` (新規) で `share-urls.tsv` と `coords.tsv` を結合して Firestore に投入する。

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

`GET /api/config` がフロントへ `MAPS_API_KEY` / `MAPS_MAP_ID` / `RECAPTCHA_SITE_KEY` と、報告フォーム用の 47 都道府県を返す。
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

## CI/CD (Cloud Build)

導入済み。
トリガの構成と作り直しの手順は [`development.md`](./development.md) にある。ここには選定の理由だけ残す。

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

Firestore に接続しないチェック (`locations.py` / `store.py` の自己チェック、`import_tsv.py --dry-run`、`tree_check.js`) だけでも、
所在地の解決・ドキュメント ID の組み立て・TSV の整合・ツリーの階層はカバーできる。
実際それで足りたので、CI 用のサービスアカウントに Firestore への権限は付けていない。
実データを読むテストを書きたくなったら `roles/datastore.user` を足す。

## 今後やりたいこと

移行が済んで動き始めた後の課題。
優先順位は付けていない。

### フロントの TypeScript 化

**CSS ライブラリの導入は Pico.css で完了した (上の「CSS ライブラリの選定」を参照)。**
クラスレス CSS を選んだので「ビルド無し」の決定はそのまま維持できている。
残っているのは TypeScript 化だけ。

目的は型による検査と補完。
現状 `areas` / `prefs` の要素の形はコメントでしか表現されていない。
Firestore のドキュメントの形が変わっても、フロントは実行するまで壊れたことに気付けない。

`tsc --noEmit` で型検査だけ行い、配信は手書き JS のまま、という逃げ道がある。
**この形なら「ビルド無し」を維持したまま型検査だけを得られるので、まずこれを試す。**
バンドルまで必要になったら下記の構成になる。

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

## Favicon と OGP (実施済み)

`cmd/server/web/favicon.svg` に絵文字 (🍽️) を `<text>` で描いただけの SVG を置き、
`index.html` から `<link rel="icon" type="image/svg+xml" href="/favicon.svg">` で参照している。
`//go:embed all:web` の対象なので配信は勝手に付いてくる。

字形は OS の絵文字フォント任せなので環境ごとに見た目が変わる。
Safari と古いブラウザは SVG の favicon を読まないため既定アイコンにフォールバックする。
そこまで面倒を見るなら PNG か ICO を併置する。

OGP のメタタグも `index.html` に入れてあり、画像は `cmd/server/web/og.png`。
`og:url` と `og:image` はクローラが相対パスを解決しないため、公開 URL を直書きしている。
カスタムドメインを当てるなら (「未決事項」参照) ここも書き換えが要る。
