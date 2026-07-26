#!/usr/bin/env python3
"""フォロー中リストの削除 (フォロー解除)。

方針:
- 「〇〇: トップリスト」は残す (削除しない)。
- それ以外 (トレンド / 地元で人気 等) のうち share-urls.tsv に記録済みのものを削除する。
- share-urls.tsv に無いリストは削除せず、共有 URL を収集して share-urls.tsv に追記する
  (未記録のものを消さないための安全網)。

削除は不可逆 (確認ダイアログも Undo も無い。「リストを削除」タップで即フォロー解除)。
先頭固定ではなく、上から順に「残す/削除/追記」を判定して処理する。
残す項目・追記済み項目は processed に入れて二度触らない。

fetch_share_urls.py のヘルパーを再利用する。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_share_urls as g  # noqa: E402

g.DEV = os.environ.get("DEV", g.DEV)
TSV = os.environ.get("OUT", "share-urls.tsv")
LIMIT = int(os.environ.get("LIMIT", "0"))  # 0 なら全件。削除件数の上限
KEEP_SUFFIX = "トップリスト"  # この接尾辞のリストは削除しない


def load_known(path):
    names = set()
    urls = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "\t" in line:
                    n, u = line.rstrip("\n").split("\t", 1)
                    names.add(n)
                    urls.add(u)
    return names, urls


def suffix(name):
    # 「〇〇市: トレンド」→「トレンド」。": " が無ければ全体を返す。
    return name.split(": ", 1)[1] if ": " in name else name


def delete_one(name, pt):
    """1 件をフォロー解除する。成功なら (True, "")。"""
    g.tap(pt)
    time.sleep(2)
    xml = g.dump()
    dele = g.find_text(xml, "リストを削除")
    if not dele:
        # メニューが開かなかった。戻して呼び出し側でリカバリさせる。
        return False, "メニューが開かない"
    g.tap(dele)
    # 「フォローを解除しています...」の間はリストが一時的に 0 件になり、
    # 数秒後にリストが再表示されて先頭へ戻る。
    # 0 件の瞬間を「消えた」と誤判定しないよう、リストが再populate され、
    # かつ対象が消えていることを確認するまで待つ (最大 12 秒)。
    for _ in range(12):
        time.sleep(1)
        items = g.list_options(g.dump())
        if items and all(n != name for n, _ in items):
            return True, ""
    return False, "削除確認できず"


def main():
    known_names, known_urls = load_known(TSV)
    print(f"開始: 記録済み {len(known_names)} 件 / KEEP={KEEP_SUFFIX} / LIMIT={LIMIT or '全件'}")

    # adb-clip の動作確認。clip は未記録リストの URL 追記にしか使わないので、
    # 使えなくても削除自体は進める (その場合、未記録リストは追記せず [skip-unknown] で残す)。
    g.clip_write(g.SENTINEL)
    clip_ok = g.clip_read() == g.SENTINEL
    if not clip_ok:
        print(f"警告: adb-clip ({g.CLIP}) が動作しない。未記録リストの URL 追記はスキップし、削除のみ行う。")

    if not g.ensure_list_screen():
        print("リスト画面を用意できなかったため中止")
        return 1

    processed = set()  # 残した/追記した/削除試行済みの名前
    attempts = {}
    deleted = 0
    kept = 0
    appended = 0
    stable = 0

    for _loop in range(20000):
        xml = g.dump()
        items = g.list_options(xml)
        if not items:
            # フォロー解除直後の一時的な再描画 (0 件) かもしれない。
            # 即 am start で再構築すると連打になり ANR を招くので、まず数秒待って再 dump する。
            for _ in range(5):
                time.sleep(2)
                items = g.list_options(g.dump())
                if items:
                    break
            if not items:
                # それでも空なら本当に画面を見失っている。シート引き上げ → 最後の手段で再構築。
                g.raise_sheet()
                if not g.list_options(g.dump()) and not g.ensure_list_screen():
                    print("リスト画面を復元できないため終了")
                    break
            continue

        todo = [(n, p) for n, p in items if n not in processed and attempts.get(n, 0) < 2]
        if not todo:
            # 見えている範囲は処理済み。スクロールして次へ。
            g.swipe(540, 1900, 540, 1300, 800)
            time.sleep(1)
            stable += 1
            if stable >= 6:
                print("最下部に到達")
                break
            continue

        stable = 0
        name, pt = todo[0]

        if suffix(name) == KEEP_SUFFIX:
            processed.add(name)
            kept += 1
            print(f"[keep] {name}")
            continue

        if name not in known_names:
            # 記録に無い → 削除せず共有 URL を収集して追記する。
            if not clip_ok:
                # clip が使えないと URL 収集できない。安全側に倒して削除せず残す。
                processed.add(name)
                kept += 1
                print(f"[skip-unknown] {name} (clip 不可のため追記せず保持)")
                continue
            attempts[name] = attempts.get(name, 0) + 1
            url, reason = g.get_share_url(pt, known_urls)
            if url:
                with open(TSV, "a", encoding="utf-8") as f:
                    f.write(f"{name}\t{url}\n")
                known_names.add(name)
                known_urls.add(url)
                processed.add(name)
                appended += 1
                print(f"[append] {name}\t{url}")
            else:
                print(f"[append FAIL {attempts[name]}/2] {name}: {reason}")
                if not g.ensure_list_screen():
                    print("リスト画面を復元できないため終了")
                    break
            continue

        # 記録済み かつ トップリストでない → 削除する。
        attempts[name] = attempts.get(name, 0) + 1
        ok, reason = delete_one(name, pt)
        if ok:
            processed.add(name)
            deleted += 1
            print(f"[{deleted}] delete {name}")
        else:
            print(f"[delete FAIL {attempts[name]}/2] {name}: {reason}")
            if not g.ensure_list_screen():
                print("リスト画面を復元できないため終了")
                break

        if LIMIT and deleted >= LIMIT:
            print(f"LIMIT={LIMIT} に到達したため終了")
            break

    print(f"完了: 削除 {deleted} 件 / 保持 {kept} 件 / 追記 {appended} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
