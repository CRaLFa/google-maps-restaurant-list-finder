# Google マップ「保存済み / フォロー中リスト」エクスポート・管理

Google マップの公式「Google データ エクスポート (Takeout)」では、
**自分が作成したリスト**のスポットは取得できるが、
**他人が作成し自分がフォロー (保存) したリスト**の中身は含まれない。
このリポジトリは、フォロー中リストの一覧・共有 URL の取得と、リストの一括削除 (フォロー解除) を行うための手順とスクリプトをまとめたもの。

アプローチは 2 系統ある。

## 1. ブラウザコンソール方式 (スポット単位の抽出)

Web 版 Google マップの DevTools コンソールに貼り付けて実行する JS。

- [`export_gmaps_lists_index.js`](./export_gmaps_lists_index.js) — 保存済み・フォロー中の「リスト一覧 (タイトル + URL)」を自動スクロールで取得。
- [`export_gmaps_list.js`](./export_gmaps_list.js) — 特定のリストを開いた状態で実行し、含まれるスポット (店名・住所・評価・URL) を取得。

Web 版は一度に数十件しか描画しない仕様のため、リスト数が非常に多いと画面上では全件取得しきれないことがある。
リスト「一覧」の全件は Takeout (`takeout.google.com` → 保存済みのみ選択 → エクスポート) でも取得できる。

## 2. adb 方式 (実機 UI 自動操作)

実機の Google マップアプリを adb + uiautomator で自動操作する。ブラウザ方式の描画上限を受けず全件を確実に扱える。

- [`fetch_share_urls.py`](./fetch_share_urls.py) — フォロー中リストを一巡し、各リストの共有 URL を [`share-urls.tsv`](./share-urls.tsv) に追記する。既知の名前はスキップするため resume-safe。
- [`delete_lists.py`](./delete_lists.py) — 「〇〇: トップリスト」は残し、それ以外 (トレンド / 地元で人気) を一括削除 (フォロー解除) する。**不可逆操作**。
- [`tools/adb-clip/`](./tools/adb-clip/) — クリップボード読み書き用に vendor した [polygraphene/adb-clip](https://github.com/polygraphene/adb-clip)。共有 URL の取得に使う。

クリップボードは Android 10 以降フォアグラウンド以外から読めないため、adb-clip を `app_process` 経由で使って回避している。詳細は各ドキュメント参照。

## データファイル

- [`share-urls.tsv`](./share-urls.tsv) — **正データ。** フォロー中リストの `リスト名 <TAB> 共有 URL`。削除前のバックアップも兼ねる (削除したリストは URL から再フォローで復元可能)。
- [`saved-lists.txt`](./saved-lists.txt) — 初期に取得したリスト名のみの一覧 (名前ベース。`share-urls.tsv` に置き換わった旧データ)。

## ドキュメント

- [`docs/collect-saved-lists.md`](./docs/collect-saved-lists.md) — adb + uiautomator でフォロー中リスト一覧を全件収集する手順。スワイプの慣性による取りこぼしと対策。
- [`docs/next-steps.md`](./docs/next-steps.md) — 共有 URL の取得 (adb-clip) と、リストの削除の手順・実証結果・ハマりどころ。

## 前提

- 実機を adb (ワイヤレスデバッグ) で接続。ワイヤレスはポートが毎回変わるので都度確認する。
- adb 操作中は端末を画面オン + ロック解除のままにする (クリップボード読み取り・スリープ対策)。
- 収集される UI dump には連絡先候補等の個人情報が混じり得るため、dump をリポジトリやログに残さないこと。
