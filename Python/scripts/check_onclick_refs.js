#!/usr/bin/env node
// index.html のインラインイベント属性（onclick 等）が呼ぶ関数が、同ファイル内または
// 読み込む js/ 配下に定義されているかを確認する。未定義があれば一覧して exit 1。
// 使い方: node Python/scripts/check_onclick_refs.js web/static/index.html
const fs = require('fs');
const path = require('path');
const file = process.argv[2] || 'web/static/index.html';
const html = fs.readFileSync(file, 'utf8');
const dir = path.dirname(file);
let js = html;
for (const m of html.matchAll(/<script[^>]+src="\/static\/js\/([^"?]+)/g)) {
  const p = path.join(dir, 'js', m[1]);
  if (fs.existsSync(p)) js += '\n' + fs.readFileSync(p, 'utf8');
}
const defined = new Set();
for (const m of js.matchAll(/(?:^|[\s;{}])(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g)) defined.add(m[1]);
for (const m of js.matchAll(/(?:^|[\s;{}])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>/g)) defined.add(m[1]);
for (const m of js.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=/g)) defined.add(m[1]);
for (const m of js.matchAll(/window\.E2UI\s*=\s*\{([^}]*)\}/g)) m[1].split(',').forEach(x => defined.add(x.trim().split(':')[0]));
const builtins = new Set(['event','this','window','document','location','history','navigator','console','alert','confirm','prompt','setTimeout','clearTimeout','fetch','open','close','E2UI','AnalysisJ2','Promise','Array','Object','String','Number','JSON','Math','Date','encodeURIComponent','decodeURIComponent','requestAnimationFrame','parseInt','parseFloat','isNaN']);
const missing = new Map();
// index.html と、読み込む js 内のテンプレート文字列の on*= 属性も検査する（段階 7 で主スクリプトを外部化したため）
for (const m of js.matchAll(/\son[a-z]+=(\\?["'])(.*?)\1/g)) {
  for (const c of m[2].matchAll(/(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(/g)) {
    const name = c[1];
    if (builtins.has(name) || defined.has(name)) continue;
    if (['if','for','while','switch','catch','return','function','typeof','new','var','let','const','await'].includes(name)) continue;
    if (!missing.has(name)) missing.set(name, 0);
    missing.set(name, missing.get(name) + 1);
  }
}
if (missing.size) {
  console.log('undefined handlers:');
  for (const [k, v] of [...missing.entries()].sort()) console.log(`  ${k} (${v})`);
  process.exit(1);
}
console.log('ok: all inline handlers resolve');
