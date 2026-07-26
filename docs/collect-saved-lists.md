# Google マップ「保存したリスト」タイトル収集手順

adb + uiautomator で Google マップの「保存済み」→「保存したリスト」のタイトルを
スクロールしながら全件収集する手順とスクリプトを記録する。

## 前提

- Windows 上の `adb.exe` を WSL から呼び出す。
- 端末が複数繋がっているため `-s <serial>` で必ず対象を指定する。
  - `adb.exe devices -l` で確認。
  - 例: `emulator-5554` (エミュレーター) / `100.64.1.35:43347` (実機ワイヤレスデバッグ)。
- 対象アプリ: `com.google.android.apps.maps`。
- 事前に端末側で Google マップの「保存済み」タブ →「保存したリスト」を開いた状態にしておく。
- 「保存済み」タブは上下 2 つの recycler_view に分かれている。
  上が「自分のリスト」(お気に入り・スター付き等)、下が収集対象の「保存したリスト」。
  上部が「もっと見る」で展開されていると下部の bounds がずれるため、
  「折りたたむ」を押して下部だけが全画面になった状態から始めること。

## 画面情報の取得 (単発確認)

```bash
DEV=emulator-5554
adb.exe -s $DEV shell uiautomator dump /sdcard/window_dump.xml
adb.exe -s $DEV shell "cat /sdcard/window_dump.xml" > dump.xml
```

- リストのタイトルは、直後に `作成者: ... · N か所` という text を持つ要素の
  直前の text として現れる。
- 「保存したリスト」の recycler_view の bounds は dump の
  `resource-id="com.google.android.apps.maps:id/recycler_view"` から取得できる。
  複数ヒットするので一番大きいものを使う
  (エミュレーター 1600x900: `[0,411][800,804]` / 実機 1080x2340: `[0,241][1080,2180]`)。
  この領域内でスワイプしてスクロールする。
- 画面解像度は `adb.exe -s $DEV shell wm size` で確認
  (例: エミュレーター 1600x900 / 実機 Xperia XQ-GE44 1080x2340)。
  dump の bounds 座標系は解像度と同じ。
- 実機ではリスト 1 件の高さが約 181px。スワイプ量の上限を決める際の基準になる。

## 全件収集スクリプト

新規タイトルが出なくなる (stable が一定回数続く) まで dump とスワイプを繰り返す。

```bash
DEV=100.64.1.35:43347   # 実機。エミュレーターなら emulator-5554
ACC=acc.txt             # タイトル累積ファイル
> "$ACC"
stable=0
for i in $(seq 1 400); do
  adb.exe -s $DEV shell uiautomator dump /sdcard/d.xml >/dev/null 2>&1
  adb.exe -s $DEV shell "cat /sdcard/d.xml" > d.xml
  before=$(wc -l < "$ACC")
  python3 - d.xml "$ACC" <<'PY'
import re,sys
xml=open(sys.argv[1],encoding="utf-8").read()
texts=[m.group(1) for m in re.finditer(r'text="([^"]+)"',xml)]
# 「作成者:」を持つ要素の直前の text をリストタイトルとみなす。
# タブ名「保存済み」が紛れることがあるので除外する。
titles=[texts[i-1] for i,t in enumerate(texts)
        if t.startswith("作成者:") and i>0 and texts[i-1]!="保存済み"]
seen=set(l.strip() for l in open(sys.argv[2],encoding="utf-8"))
with open(sys.argv[2],"a",encoding="utf-8") as f:
    for t in titles:
        if t not in seen:
            f.write(t+"\n"); seen.add(t)
PY
  after=$(wc -l < "$ACC")
  if [ "$before" -eq "$after" ]; then stable=$((stable+1)); else stable=0; fi
  echo "iter $i: 累計 $after 件 (stable=$stable)"
  if [ "$stable" -ge 6 ]; then echo "最下部に到達"; break; fi
  # リスト領域内でスワイプアップ (下方向へスクロール)。
  # duration を長め (800ms) にして慣性スクロールを殺すのが重要 (下記「既知の問題」参照)。
  adb.exe -s $DEV shell input swipe 540 1900 540 1300 800 &> /dev/null
done
```

## 実機での検証結果 (2026-07-25)

Xperia XQ-GE44 (1080x2340) で全件収集を実施し、**385 件**を取得した。
取得結果は [`saved-lists.txt`](../saved-lists.txt) に保存してある。

- **エミュレーターの 99 件頭打ちは実機では発生しない。** 全件収集は実機で行うこと。
- **スワイプの duration が短いと慣性スクロールで項目を飛ばす。**
  同一端末・同一リストで 3 条件を比較した実測値:

  | スワイプ量 / duration | 取得件数 | 備考 |
  | --- | --- | --- |
  | 1100px / 250ms | 314 | **71 件 (18%) 欠落** |
  | 700px / 600ms | 385 | 全件 |
  | 600px / 800ms | 385 | 700px 版と差分ゼロで収束 |

  700px 版と 600px 版は集合として完全一致し、3 条件の和集合も 385 件だった。
  当時はこれを根拠に「385 件が全件」と判断したが、これは誤りだった (下記参照)。
  距離そのものより duration が効く。250ms のフリックは慣性で指定量以上スクロールする。

- **【訂正】385 件は全件ではなかった。実際は 461 件。**
  後日 adb-clip 方式の [`fetch_share_urls.py`](../fetch_share_urls.py) で全件収集したところ 461 件取得でき、
  385 件はその部分集合 (76 件を取りこぼし、欠落は種別 26/25/25 と一様) だった。
  誤判定の原因は「700px 版と 600px 版が一致したから全件」という推論の穴にある。
  両者は「固定量スワイプ → 1 回 dump」という同一構造の盲点 (スワイプの継ぎ目に入った項目を飛ばす) を共有しており、
  同じ 16% を同じように取りこぼして一致していただけで、独立した検証にはなっていなかった。
  `fetch_share_urls.py` は「毎ループ dump して見えている項目を全て拾い、名前で dedup。
  見えている分を処理し切ってから画面高より小さくスワイプする」方式で重なりを持たせ、この継ぎ目落ちを解消している。

## 既知の問題 / TODO

- 収集後は `sort -u` 等で重複確認するとよい (スクリプト内でも dedup 済み)。
- 逆方向スワイプでリスト先頭に戻そうとすると、行き過ぎたときに
  ボトムシート自体が畳まれてしまう。
  その場合は `input swipe 540 1960 540 300 400` でシートを引き上げ直す。
- 385 件の内訳は全て `〇〇市: トップリスト` 等の Google 自動生成リストだった。
  他人が作成したリストをフォローしている場合に同じ抽出条件で取れるかは未検証。
