// web/index.html のツリー構築 (buildAreas / buildTree) を実データで検証する。
// 所在地からの階層の組み立ては壊れても画面が出てしまうため、形を機械的に確かめる。
// 実行: node cmd/server/tree_check.js (リポジトリ直下から)
const fs = require("fs");
const vm = require("vm");

// index.html の <script> をそのまま読み込んで動かす。DOM と fetch はスタブで足りる。
const src = fs.readFileSync("cmd/server/web/index.html", "utf8").match(/<script>([\s\S]*)<\/script>/)[1];
const stub = new Proxy({}, { get: (t, k) => (k === "value" ? "" : () => stub), set: () => true });
const ctx = vm.createContext({
  document: { getElementById: () => stub, createElement: () => stub, head: stub },
  fetch: () => new Promise(() => {}),
  setTimeout, clearTimeout, console,
});
// let 宣言は vm のグローバルに載らないので、取り出す口を足しておく。
vm.runInContext(src + "\nglobalThis.getRoots = () => roots;\nglobalThis.getAreas = () => areas;", ctx);

// data/archive/ の TSV から /api/lists 相当の行を組み立てる。
// 正データは Firestore だが、こちらは移行時点で凍結されているぶん
// 期待値を固定できるので、階層のフィクスチャとしてはむしろ都合が良い。
const PREF = /^(北海道|東京都|大阪府|京都府|.{2,3}?県)/;
const coords = new Map();
for (const line of fs.readFileSync("data/archive/coords.tsv", "utf8").trim().split("\n")) {
  const [area, pref, lat, lng] = line.split("\t");
  coords.set(pref + "|" + area, { lat: +lat, lng: +lng });
}
const rows = fs.readFileSync("data/archive/share-urls.tsv", "utf8").trim().split("\n").map(line => {
  const [name, url, loc = ""] = line.split("\t");
  const [area, kind] = name.split(": ");
  const pref = (PREF.exec(loc) || PREF.exec(name) || [""])[0];
  const c = coords.get(pref + "|" + area) || { lat: 0, lng: 0 };
  return { name, area, kind, loc: loc || pref, pref, url, lat: c.lat, lng: c.lng };
});

ctx.buildAreas(rows);
const roots = ctx.getRoots();

const count = n => (n.area ? 1 : 0) + n.children.reduce((s, c) => s + count(c), 0);
const depth = n => 1 + Math.max(0, ...n.children.map(depth));
// エリア名を根から辿って "東京都 > 豊島区 > 池袋" の形にする。
const pathOf = (nodes, area, trail = []) => {
  for (const n of nodes) {
    const t = [...trail, n.label];
    if (n.area && n.area.area === area) return t.join(" > ");
    const found = pathOf(n.children, area, t);
    if (found) return found;
  }
  return null;
};

const total = roots.reduce((s, r) => s + count(r), 0);
const fail = [];
const eq = (got, want, what) => { if (got !== want) fail.push(`${what}: ${got} (期待 ${want})`); };

eq(total, 191, "エリア数");
eq(roots.length, 45, "都道府県数");
eq(pathOf(roots, "池袋"), "東京都 > 豊島区 > 池袋", "池袋の階層");
eq(pathOf(roots, "栄"), "愛知県 > 名古屋市 > 中区 > 栄", "栄の階層");
eq(pathOf(roots, "上野"), "東京都 > 台東区 > 上野", "上野の階層 (台東区はリスト無しの中間ノード)");
// 東京都 のリストは 東京 のリストとパスが前方一致するため、素朴な推測だと 東京 > 都 に化ける。
eq(pathOf(roots, "東京都"), "東京都", "東京都のリストの階層 (根の都道府県ノードそのもの)");
eq(pathOf(roots, "東京"), "東京都 > 東京", "東京のリストの階層");
// リストの並びは トップリスト → 地元で人気 → トレンド の固定順。
const order = ["トップリスト", "地元で人気", "トレンド"];
const badOrder = ctx.getAreas().filter(a => {
  const r = a.lists.map(l => order.indexOf(l.kind));
  return r.some((v, i) => i > 0 && v < r[i - 1]);
});
eq(badOrder.length, 0, `リストの並び順が崩れているエリア (例: ${badOrder[0]?.area})`);
eq(roots[0].label, "北海道", "最北の都道府県");
eq(roots.at(-1).label, "沖縄県", "最南の都道府県");

if (fail.length) {
  console.error("NG\n" + fail.join("\n"));
  process.exit(1);
}
console.log(`OK エリア ${total} / 都道府県 ${roots.length} / 最大深さ ${Math.max(...roots.map(depth))}`);
