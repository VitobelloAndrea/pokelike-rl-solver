// Extracts a self-contained, audited JS "battle prefix" from the local
// deobfuscated bundle: everything from the top of the file through the end
// of `mergeBattleConfigs` (bundle.deobfuscated.js:80991-81051). Executing
// this prefix in a `vm` sandbox defines every function the battle oracle
// needs (rng/seedRng, calcHp, calcDamage, getBestMove, initBattleState,
// buildGen3AbilityConfig, buildTraitsConfig, mergeBattleConfigs, runBattle,
// ...) plus every data table they close over (MOVE_POOL, TYPE_CHART,
// GEN3_ABILITY_BY_SPECIES, ...), without hand-transcribing any of it.
//
// Why this is safe to just RUN (same reasoning as
// tools/extract-data/extract-tables.js, extended further into the file):
//
//   tools/extract-data already established that bundle.deobfuscated.js's
//   first ~51,757 lines are pure data-literal construction -- zero
//   `document.`/`localStorage.`/`fetch(`/`navigator.`/`Audio(` calls at the
//   TOP LEVEL (i.e. outside any function body, so they'd run just from
//   loading the script, not from us calling anything).
//
//   This tool needs the battle engine too, which lives much further down
//   (`runBattle` at line 55164, `mergeBattleConfigs` at line 80991) -- i.e.
//   roughly 88% of the file, not extract-data's ~57%. Before trusting that
//   range, this session manually re-ran extract-data's own safety check
//   over the NEW span (lines 51757-81051) plus a full inventory of every
//   column-0 (top-level) statement in that span:
//     - grep for `^(document\.|window\.|localStorage|fetch\(|Audio\(|
//       XMLHttpRequest|navigator\.|addEventListener)` at column 0: zero hits.
//     - grep for a top-level IIFE (`^\(function`, `^\(async`, `^\(\)? *=>`):
//       zero hits in that span (there IS one earlier, at line 74835,
//       `IS_DEV_MODE`, already covered below).
//     - every column-0 statement in the span is a `function`/`async
//       function` declaration, a `const`/`let`/`var` binding (plain data
//       literals or, per the existing convention, chained property
//       assignments like `(B6u["name"] = "...", ...)`), or `class` -- no
//       bare top-level function-call statements.
//     - `state` (the game's central mutable object) is declared at line
//       74982 as `let state = BOg;` -- inside this prefix, so it's simply
//       defined normally; this tool overwrites the handful of fields the
//       battle functions actually read (`gen3Mode`/`gen4Mode`/
//       `challengeGen3`/`challengeGen4`) per fixture, same pattern as
//       `sandbox.window`/`sandbox.location` below.
//     - the one top-level IIFE anywhere in 1-81051 (`IS_DEV_MODE`, line
//       74835) calls `new URLSearchParams(location["search"])` inside a
//       try/catch -- `URLSearchParams` is a real Node global, and the
//       `location` stub below (copied from extract-data) satisfies the
//       property read even without a `.search` field (`undefined` is a
//       valid `URLSearchParams` constructor argument).
//   None of the ~184 `addEventListener`/`fetch(`/`localStorage`/`new Audio`
//   hits found by a non-anchored grep over that span are at column 0 --
//   every one is inside a function body, so it only matters if we actually
//   CALL that function, which the oracle never does (it only calls the
//   battle-math functions listed above).
//
// Usage:
//   node extract-prefix.js <bundle.deobfuscated.js> <out-file.js>

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const acorn = require(path.join(__dirname, '..', 'extract-data', 'node_modules', 'acorn'));

const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) {
  console.error('Usage: node extract-prefix.js <bundle.deobfuscated.js> <out-file.js>');
  process.exit(1);
}

const src = fs.readFileSync(inPath, 'utf8');
const ast = acorn.parse(src, { ecmaVersion: 'latest', sourceType: 'script' });

const mergeIdx = ast.body.findIndex(
  (node) => node.type === 'FunctionDeclaration' && node.id && node.id.name === 'mergeBattleConfigs',
);
if (mergeIdx === -1) throw new Error('could not find the mergeBattleConfigs function declaration');
const mergeNode = ast.body[mergeIdx];
const cutoffNode = ast.body[mergeIdx + 1];
if (!cutoffNode) throw new Error('mergeBattleConfigs was the last statement in the file?!');
console.log(
  `cutting after mergeBattleConfigs (ends offset ${mergeNode.end}), next statement is a ${cutoffNode.type} at offset ${cutoffNode.start}`,
);

const prefix = src.slice(0, cutoffNode.start);

for (const risky of ['document.', 'localStorage.', 'fetch(', 'navigator.', 'Audio(', 'addEventListener(']) {
  // Heads-up only, matching extract-tables.js's convention -- these WILL
  // show up (inside function bodies we never call), this just documents
  // that the textual scan saw them, same as extract-data's own tool.
  const count = prefix.split(risky).length - 1;
  if (count) console.log(`NOTE: prefix textually contains "${risky}" ${count}x (expected -- all inside function bodies never called by the oracle)`);
}

// Compile-only syntax check: this throws SyntaxError if the slice is
// malformed (e.g. cut mid-statement), catching a bad boundary immediately
// rather than at oracle-run time.
new vm.Script(prefix, { filename: 'battle-prefix-syntax-check.js' });

fs.writeFileSync(outPath, prefix);
console.log(`wrote ${outPath} (${prefix.split('\n').length} lines)`);
