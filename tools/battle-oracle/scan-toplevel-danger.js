// Rigorous top-level side-effect scanner for the battle-prefix slice.
// The manual column-0 grep this session started with MISSED a real
// top-level `document["addEventListener"](...)` call (bundle.deobfuscated.js
// ~63649-63662) because it's a bracket-notation reference buried inside a
// multi-line chained comma-expression that only STARTS at column 0 -- grep
// can't see "not inside a function" that way. This script instead walks the
// real AST: for every top-level Program.body statement, it recursively
// visits every node EXCEPT inside nested function/arrow-function bodies
// (those only run when called, which the oracle controls), and flags any
// Identifier matching a risky global name. This is what actually decides
// "does merely loading this script touch the network/DOM/storage" --
// column-based text scanning does not.
//
// Usage: node scan-toplevel-danger.js <prefix.js>

const fs = require('fs');
const path = require('path');
const acorn = require(path.join(__dirname, '..', 'extract-data', 'node_modules', 'acorn'));

const [, , inPath] = process.argv;
const src = fs.readFileSync(inPath, 'utf8');
const ast = acorn.parse(src, { ecmaVersion: 'latest', sourceType: 'script', locations: true });

const RISKY = new Set([
  'document', 'localStorage', 'sessionStorage', 'navigator', 'fetch',
  'XMLHttpRequest', 'Audio', 'WebSocket', 'indexedDB', 'Notification',
  'geolocation', 'Worker', 'EventSource',
]);

const FUNCTION_TYPES = new Set([
  'FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression',
]);

const hits = [];

function walk(node, insideFunction) {
  if (!node || typeof node.type !== 'string') return;
  if (node.type === 'Identifier' && RISKY.has(node.name) && !insideFunction) {
    hits.push({ name: node.name, line: node.loc.start.line });
  }
  const nextInsideFunction = insideFunction || FUNCTION_TYPES.has(node.type);
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'start' || key === 'end' || key === 'range') continue;
    const val = node[key];
    if (Array.isArray(val)) {
      for (const item of val) if (item && typeof item.type === 'string') walk(item, nextInsideFunction);
    } else if (val && typeof val.type === 'string') {
      walk(val, nextInsideFunction);
    }
  }
}

for (const stmt of ast.body) walk(stmt, false);

if (hits.length === 0) {
  console.log('No top-level (outside any function body) references to risky globals found.');
} else {
  console.log(`Found ${hits.length} top-level reference(s) to risky globals:`);
  for (const h of hits) console.log(`  line ${h.line}: ${h.name}`);
}
