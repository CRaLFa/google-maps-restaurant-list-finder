/**
 * Google マップの「保存済み」画面から、保存済みリスト（他人のリスト / フォロー中リスト）自体のタイトルとURL一覧を CSV 出力するスクリプト
 * 
 * 【使い方】
 * 1. PCのブラウザで Google マップ (https://www.google.com/maps) を開きます。
 * 2. 左上のメニュー (≡) またはプロフィールアイコンの横から「保存済み」を開き、「リスト」または「フォロー中」タブを表示します。
 * 3. F12 キー (Mac: Cmd + Option + I) でデベロッパー ツールを開き、「Console」タブを選択します。
 * 4. このスクリプトを貼り付けて Enter を押します。
 * 5. 自動スクロール後にリスト一覧の CSV ファイルがダウンロードされます。
 */

(async function exportGoogleMapsListsCollection() {
    console.log("🚀 保存済みリスト一覧の抽出を開始します...");

    // 1. スクロールエリアを取得して自動スクロール（動的読み込み）
    const scrollContainer = document.querySelector('div[role="main"]') || document.querySelector('.m6QEfd') || document.querySelector('#pane');
    if (scrollContainer) {
        console.log("🔄 すべてのリストを表示させるため自動スクロール中...");
        let lastHeight = 0;
        let sameCount = 0;
        while (sameCount < 8) {
            scrollContainer.scrollTop = scrollContainer.scrollHeight;
            await new Promise(r => setTimeout(r, 1500));
            if (scrollContainer.scrollHeight === lastHeight) {
                sameCount++;
            } else {
                sameCount = 0;
                lastHeight = scrollContainer.scrollHeight;
            }
        }
        console.log("✅ スクロール処理完了");
    } else {
        console.log("⚠️ スクロールエリアを自動認識できませんでした。リストが表示されているか確認してください。");
    }

    // 2. リスト要素を抽出
    const results = [];
    const visited = new Set();
    const anchors = Array.from(document.querySelectorAll('a[href*="playlist"], a[href*="list"], a[href*="/maps/"]'));

    anchors.forEach(a => {
        const href = a.href;
        if (!href || visited.has(href)) return;
        
        // 個別スポットや検索、システムリンクを除外
        if (href.includes('/maps/place/') || href.includes('/maps/search/') || href.includes('/maps/dir/')) return;

        const container = a.closest('div.Nv2pk') || a.closest('div[role="article"]') || a.parentElement;
        const text = container ? container.innerText.trim() : a.innerText.trim();
        if (!text) return;

        visited.add(href);
        const lines = text.split('\n').map(s => s.trim()).filter(Boolean);

        results.push({
            id: results.length + 1,
            title: lines[0] || '無題のリスト',
            url: href,
            info: lines.slice(1).join(' | ')
        });
    });

    if (results.length === 0) {
        console.error("❌ リストが見つかりませんでした。Google マップの「保存済み」画面が開かれているか確認してください。");
        return;
    }

    console.log(`🎉 合計 ${results.length} 件のリストを検出しました。CSVを作成します...`);

    // 3. UTF-8 BOM付き CSV
    let csvContent = "\uFEFFNo,リスト名,リストURL,補足情報\n";
    results.forEach(row => {
        const title = `"${(row.title || '').replace(/"/g, '""')}"`;
        const url = `"${(row.url || '').replace(/"/g, '""')}"`;
        const info = `"${(row.info || '').replace(/"/g, '""')}"`;
        csvContent += `${row.id},${title},${url},${info}\n`;
    });

    // 4. ダウンロード
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement("a");
    const blobUrl = URL.createObjectURL(blob);
    const filename = `gmaps_saved_lists_index_${new Date().toISOString().slice(0, 10)}.csv`;

    link.href = blobUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(blobUrl);

    console.log(`✅ ダウンロード完了: ${filename}`);
})();
