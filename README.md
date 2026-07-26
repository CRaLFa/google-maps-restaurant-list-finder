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

- [`fetch_share_urls.py`](./fetch_share_urls.py) — フォロー中リストを一巡し、各リストの共有 URL を [`share-urls.tsv`](./share-urls.tsv) に追記する。既知の名前はスキップするため resume-safe。
- [`delete_lists.py`](./delete_lists.py) — 「〇〇: トップリスト」は残し、それ以外 (トレンド / 地元で人気) を一括削除 (フォロー解除) する。**不可逆操作**。
- [`tools/adb-clip/`](./tools/adb-clip/) — クリップボード読み書き用に vendor した [polygraphene/adb-clip](https://github.com/polygraphene/adb-clip)。共有 URL の取得に使う。

クリップボードは Android 10 以降フォアグラウンド以外から読めないため、adb-clip を `app_process` 経由で使って回避している。詳細は [`docs/adb-workflow.md`](./docs/adb-workflow.md) 参照。

## データファイル

- [`share-urls.tsv`](./share-urls.tsv) — **正データ。** フォロー中リストの `リスト名 <TAB> 共有 URL`。削除前のバックアップも兼ねる (削除したリストは URL から再フォローで復元可能)。

## ドキュメント

- [`docs/adb-workflow.md`](./docs/adb-workflow.md) — adb + uiautomator による一連の手順。リスト一覧の全件収集 (スワイプの慣性による取りこぼしと対策)、共有 URL の取得 (adb-clip)、リストの削除の実証結果・ハマりどころ。

## 前提

- 実機を adb (ワイヤレスデバッグ) で接続。ワイヤレスはポートが毎回変わるので都度確認する。
- adb 操作中は端末を画面オン + ロック解除のままにする (クリップボード読み取り・スリープ対策)。
- 収集される UI dump には連絡先候補等の個人情報が混じり得るため、dump をリポジトリやログに残さないこと。
