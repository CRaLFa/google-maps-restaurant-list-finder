/**
 * Google マップの共有リスト・保存済みリスト（他人が作成したリスト）を CSV としてダウンロードするスクリプト
 * 
 * 【使い方】
 * 1. PCのブラウザ (Chrome / Edge / Firefox 等) で対象の Google マップ リストページを開きます。
 * 2. キーボードの F12 キー (Mac は Cmd + Option + I) を押して「デベロッパー ツール」を開きます。
 * 3. 「Console (コンソール)」タブを選択します。
 * 4. このスクリプト全体をコピーしてコンソールに貼り付け、Enter キーを押します。
 * 5. 全スポットが自動取得され、CSV ファイルがダウンロードされます。
 */

(async function exportGoogleMapsList() {
    console.log("🚀 Google マップのリスト抽出を開始します...");

    // 1. スクロール領域を取得して自動スクロール（画面外のスポットを読み込ませる）
    const scrollContainer = document.querySelector('div[role="main"]') || document.querySelector('.m6QEfd');
    if (scrollContainer) {
        console.log("🔄 全項目をロードするため自動スクロール中...");
        let lastHeight = 0;
        let sameHeightCount = 0;
        while (sameHeightCount < 6) {
            scrollContainer.scrollTop = scrollContainer.scrollHeight;
            await new Promise(r => setTimeout(r, 1200));
            if (scrollContainer.scrollHeight === lastHeight) {
                sameHeightCount++;
            } else {
                sameHeightCount = 0;
                lastHeight = scrollContainer.scrollHeight;
            }
        }
        console.log("✅ スクロール完了");
    } else {
        console.log("⚠️ スクロールコンテナが自動検出できませんでした。必要に応じて手動でリストを最後までスクロールしてください。");
    }

    // 2. スポット情報の要素を取得
    const results = [];
    const visitedUrls = new Set();
    const items = document.querySelectorAll('a[href*="/maps/place/"]');

    items.forEach((item, index) => {
        const href = item.href;
        if (!href || visitedUrls.has(href)) return;
        visitedUrls.add(href);

        // 親コンテナからテキスト情報を抽出
        const container = item.closest('div.Nv2pk') || item.closest('div[role="article"]') || item.parentElement;
        const rawText = container ? container.innerText : '';
        const lines = rawText.split('\n').map(s => s.trim()).filter(Boolean);

        // スポット名
        let name = item.getAttribute('aria-label') || (lines.length > 0 ? lines[0] : '名称不明');
        // 先頭の「1. 」「2. 」などの番号を綺麗に整理
        name = name.replace(/^\d+\.\s*/, '');

        results.push({
            id: results.length + 1,
            name: name,
            url: href,
            details: lines.join(' | ')
        });
    });

    if (results.length === 0) {
        console.error("❌ スポットが見つかりませんでした。リストが完全に表示されているか確認してください。");
        return;
    }

    console.log(`🎉 合計 ${results.length} 件のスポットを検出しました。CSVを作成します...`);

    // 3. CSV形式に変換（UTF-8 BOM付きでExcelの文字化けを防ぐ）
    let csvContent = "\uFEFF"; 
    csvContent += "No,スポット名,Google Maps URL,詳細テキスト\n";

    results.forEach(row => {
        const no = row.id;
        const name = `"${(row.name || '').replace(/"/g, '""')}"`;
        const url = `"${(row.url || '').replace(/"/g, '""')}"`;
        const details = `"${(row.details || '').replace(/"/g, '""')}"`;
        csvContent += `${no},${name},${url},${details}\n`;
    });

    // 4. CSVファイルとしてブラウザでダウンロード
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement("a");
    const blobUrl = URL.createObjectURL(blob);
    const filename = `gmaps_saved_list_${new Date().toISOString().slice(0, 10)}.csv`;

    link.setAttribute("href", blobUrl);
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(blobUrl);

    console.log(`✅ ダウンロード完了: ${filename}`);
})();
