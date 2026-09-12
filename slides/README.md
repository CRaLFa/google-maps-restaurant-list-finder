# LT 用スライド

このプロジェクトの開発経緯を話す LT のスライド。[Slidev](https://sli.dev/) で作っている。
原稿は [`slides.md`](./slides.md) の 1 ファイル。`---` でスライドを区切り、見出し 1 つと箇条書きで書く。

## 使い方

```bash
cd slides
vp install          # 初回だけ
vp run dev          # http://localhost:3030/ で表示。原稿を直すと即反映される
vp run build        # dist/ に静的サイトを出す
vp run export       # PDF に書き出す。初回は Playwright の Chromium が要る
```

発表者ビューは `http://localhost:3030/presenter/`。ノート・次のスライド・タイマーが出る。
本体を外部出力に、発表者ビューを手元に置く。

## 構成上の注意

- **リポジトリ直下ではなくこのディレクトリに閉じている。**
  直下に `package.json` を置くと、Cloud Run の `--source .` デプロイで buildpack が Go ではなく Node と誤検出しうるため。
- `@slidev/cli` とテーマはここの `devDependencies` に入れている。
  `vp add -g` で入れたグローバル版は Vite+ の隔離ストアに置かれ、Slidev からテーマを解決できない。
- 画像は `img/` に置き、原稿から `![](./img/xxx.png)` で参照する。
- `node_modules/` と `dist/` はリポジトリ直下の `.gitignore` で除外している。
