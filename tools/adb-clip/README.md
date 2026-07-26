# adb-clip (vendored)

[polygraphene/adb-clip](https://github.com/polygraphene/adb-clip) v0.0.3 の `clip` と `clip.jar` を同梱している。
Android のクリップボードを adb から読み書きするためのツール。
`app_process` で shell UID として実行することで、Android 10 以降のバックグラウンドクリップボード読み取り制限を回避する。

## セットアップ

```bash
DEV=100.64.1.35:42931   # 端末シリアル。ワイヤレスデバッグはポートが毎回変わる
adb.exe -s $DEV push clip clip.jar /data/local/tmp/
adb.exe -s $DEV shell chmod 755 /data/local/tmp/clip
```

## 使い方

```bash
adb.exe -s $DEV shell /data/local/tmp/clip              # 読み取り (stdout)
adb.exe -s $DEV shell "/data/local/tmp/clip 'text'"     # 書き込み
```

制約は「画面オン + ロック解除」のみ。
実機 Xperia XQ-GE44 (Android 16) で往復動作を確認済み。

共有 URL 収集での使い方は [`../../docs/next-steps.md`](../../docs/next-steps.md) を参照。
ライセンスは MIT (上流リポジトリ参照)。
