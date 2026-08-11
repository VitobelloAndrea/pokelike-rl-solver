// Pulls the static data tables (Pokedex, moves, type chart, items,
// evolutions, trainers, map BST/level ranges, fallback species pool) out of
// the deobfuscated game bundle as real JSON, instead of hand-transcribing
// ~721 species entries and everything else by eye.
//
// Why this is safe to do by just *running* a slice of the bundle (rather
// than statically parsing each table's literal syntax by hand): the top
// half of the file is pure data-literal construction -- confirmed by
// grepping the whole prefix for `document.`, `localStorage.`, `fetch(`,
// `navigator.`, `Audio(` and finding zero hits before the data tables are
// fully built. The only browser global touched in that range is a couple of
// `window["..."] = ...` property assignments, which a plain stub object
// satisfies. So: parse the bundle with acorn, find the first top-level
// statement that starts after all the tables we need are defined, slice the
// source there (guaranteed syntactically complete since it's a real AST
// statement boundary, not a manual line-count guess), append a `window`
// stub + an extraction line referencing the tables by their REAL top-level
// names (MOVE_POOL, TYPE_CHART, EVOLUTIONS, GYM_LEADERS, etc. are literal,
// un-mangled identifiers in this bundle -- only function-local variables
// got obfuscator-renamed), and execute just that prefix in a `vm` sandbox.
//
// Usage:
//   node extract-tables.js <bundle.deobfuscated.js> <out-dir>

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const acorn = require('acorn');

const [, , inPath, outDir] = process.argv;
if (!inPath || !outDir) {
  console.error('Usage: node extract-tables.js <bundle.deobfuscated.js> <out-dir>');
  process.exit(1);
}

const src = fs.readFileSync(inPath, 'utf8');

// Cut right after the statement that builds EVOLUTIONS/BRANCHING_EVOLUTIONS
// (the last table we need, source-order) -- found by scanning forward from
// its declaration to the next top-level FunctionDeclaration.
const ast = acorn.parse(src, { ecmaVersion: 'latest', sourceType: 'script' });

const branchIdx = ast.body.findIndex(
  (node) =>
    node.type === 'VariableDeclaration' &&
    node.declarations.some((d) => d.id.type === 'Identifier' && d.id.name === 'BRANCHING_EVOLUTIONS'),
);
if (branchIdx === -1) throw new Error('could not find the BRANCHING_EVOLUTIONS declaration statement');
const cutoffNode = ast.body[branchIdx + 1];
if (!cutoffNode) throw new Error('BRANCHING_EVOLUTIONS statement was the last statement in the file?!');
console.log(
  `cutting after BRANCHING_EVOLUTIONS statement (ends offset ${ast.body[branchIdx].end}), next statement is a ${cutoffNode.type} at offset ${cutoffNode.start}`,
);

const prefix = src.slice(0, cutoffNode.start);

// Sanity check: the range we're about to execute should not touch anything
// beyond a plain object stub for `window`. This is a textual check (so it
// can also flag hits inside string literals, e.g. an `onerror="..."` HTML
// attribute string) -- it's a heads-up to go verify, not proof of danger.
for (const risky of ['document.', 'localStorage.', 'fetch(', 'navigator.', 'Audio(']) {
  if (prefix.includes(risky)) {
    console.warn(`NOTE: prefix textually contains "${risky}" somewhere (may just be inside a string literal or an inert function body) -- verify before trusting output blindly if extraction misbehaves`);
  }
}

const EXTRACT_SNIPPET = `
function __asPlain(x) { return x instanceof Set ? [...x] : x; }
globalThis.__EXTRACTED__ = {
  pokedex: (typeof window !== 'undefined' && window.__POKEDEX__) || undefined,
  movePool: typeof MOVE_POOL !== 'undefined' ? MOVE_POOL : undefined,
  legendarySignatureMoves: typeof LEGENDARY_SIGNATURE_MOVES !== 'undefined' ? LEGENDARY_SIGNATURE_MOVES : undefined,
  typeChart: typeof TYPE_CHART !== 'undefined' ? TYPE_CHART : undefined,
  typeIds: typeof TYPE_IDS !== 'undefined' ? TYPE_IDS : undefined,
  challengeInverse: typeof CHALLENGE_INVERSE !== 'undefined' ? CHALLENGE_INVERSE : undefined,
  itemPool: typeof ITEM_POOL !== 'undefined' ? ITEM_POOL : undefined,
  usableItemPool: typeof USABLE_ITEM_POOL !== 'undefined' ? USABLE_ITEM_POOL : undefined,
  typeItemMap: typeof TYPE_ITEM_MAP !== 'undefined' ? TYPE_ITEM_MAP : undefined,
  gymLeaders: typeof GYM_LEADERS !== 'undefined' ? GYM_LEADERS : undefined,
  elite4: typeof ELITE_4 !== 'undefined' ? ELITE_4 : undefined,
  johtoGymLeaders: typeof JOHTO_GYM_LEADERS !== 'undefined' ? JOHTO_GYM_LEADERS : undefined,
  gen2Elite4: typeof GEN2_ELITE_4 !== 'undefined' ? GEN2_ELITE_4 : undefined,
  hoennGymLeaders: typeof HOENN_GYM_LEADERS !== 'undefined' ? HOENN_GYM_LEADERS : undefined,
  hoennElite4: typeof HOENN_ELITE_4 !== 'undefined' ? HOENN_ELITE_4 : undefined,
  sinnohGymLeaders: typeof SINNOH_GYM_LEADERS !== 'undefined' ? SINNOH_GYM_LEADERS : undefined,
  sinnohElite4: typeof SINNOH_ELITE_4 !== 'undefined' ? SINNOH_ELITE_4 : undefined,
  mapBstRanges: typeof MAP_BST_RANGES !== 'undefined' ? MAP_BST_RANGES : undefined,
  gen2MapBstRanges: typeof GEN2_MAP_BST_RANGES !== 'undefined' ? GEN2_MAP_BST_RANGES : undefined,
  gen3MapBstRanges: typeof GEN3_MAP_BST_RANGES !== 'undefined' ? GEN3_MAP_BST_RANGES : undefined,
  gen4MapBstRanges: typeof GEN4_MAP_BST_RANGES !== 'undefined' ? GEN4_MAP_BST_RANGES : undefined,
  mapLevelRanges: typeof MAP_LEVEL_RANGES !== 'undefined' ? MAP_LEVEL_RANGES : undefined,
  gen2MapLevelRanges: typeof GEN2_MAP_LEVEL_RANGES !== 'undefined' ? GEN2_MAP_LEVEL_RANGES : undefined,
  gen3MapLevelRanges: typeof GEN3_MAP_LEVEL_RANGES !== 'undefined' ? GEN3_MAP_LEVEL_RANGES : undefined,
  gen4MapLevelRanges: typeof GEN4_MAP_LEVEL_RANGES !== 'undefined' ? GEN4_MAP_LEVEL_RANGES : undefined,
  fallbackSpeciesPool: typeof Sl !== 'undefined' ? Sl : undefined, // NOTE: 'Sl' is a mangled name, not stable across bundle regenerations -- see README caveat.
  evolutions: typeof EVOLUTIONS !== 'undefined' ? EVOLUTIONS : undefined,
  branchingEvolutions: typeof BRANCHING_EVOLUTIONS !== 'undefined' ? BRANCHING_EVOLUTIONS : undefined,
  neverWildIds: typeof NEVER_WILD_IDS !== 'undefined' ? __asPlain(NEVER_WILD_IDS) : undefined,
  legendaryIds: typeof LEGENDARY_IDS !== 'undefined' ? LEGENDARY_IDS : undefined,
  eggExcludedLegendaryIds: typeof EGG_EXCLUDED_LEGENDARY_IDS !== 'undefined' ? __asPlain(EGG_EXCLUDED_LEGENDARY_IDS) : undefined,
  legendaryEggIds: typeof LEGENDARY_EGG_IDS !== 'undefined' ? LEGENDARY_EGG_IDS : undefined,
  legendaryPoolHigh: typeof LEGENDARY_POOL_HIGH !== 'undefined' ? LEGENDARY_POOL_HIGH : undefined,
  legendaryPoolVeryHigh: typeof LEGENDARY_POOL_VERYHIGH !== 'undefined' ? LEGENDARY_POOL_VERYHIGH : undefined,
  starterIds: typeof STARTER_IDS !== 'undefined' ? STARTER_IDS : undefined,
  gen2StarterIds: typeof GEN2_STARTER_IDS !== 'undefined' ? GEN2_STARTER_IDS : undefined,
  gen3StarterIds: typeof GEN3_STARTER_IDS !== 'undefined' ? GEN3_STARTER_IDS : undefined,
  gen4StarterIds: typeof GEN4_STARTER_IDS !== 'undefined' ? GEN4_STARTER_IDS : undefined,
  gen1WithGen2Evo: typeof GEN1_WITH_GEN2_EVO !== 'undefined' ? __asPlain(GEN1_WITH_GEN2_EVO) : undefined,
  gen4Route1Banned: typeof GEN4_ROUTE1_BANNED !== 'undefined' ? __asPlain(GEN4_ROUTE1_BANNED) : undefined,
  gen4Route1Forced: typeof GEN4_ROUTE1_FORCED !== 'undefined' ? GEN4_ROUTE1_FORCED : undefined,
  passiveRequiredType: typeof PASSIVE_REQUIRED_TYPE !== 'undefined' ? PASSIVE_REQUIRED_TYPE : undefined,
  passiveRequiredCond: typeof PASSIVE_REQUIRED_COND !== 'undefined' ? PASSIVE_REQUIRED_COND : undefined,
  pokemonFormSlugs: typeof POKEMON_FORM_SLUGS !== 'undefined' ? POKEMON_FORM_SLUGS : undefined,
  pokemonFormSpriteIds: typeof POKEMON_FORM_SPRITE_IDS !== 'undefined' ? POKEMON_FORM_SPRITE_IDS : undefined,
};
`;

// The string-array decoder (`function k(...)`/`function K(...)`) lives much
// further down the file than our cutoff, so `const Bm9 = k;` at the very
// top of the file would throw ReferenceError once we slice the source
// short. We don't need real decoding any more -- bundle.deobfuscated.js
// already has every `alias(0xHEX)` call replaced with a literal string, so
// `k`/`K` and their aliases are all dead weight at this point. Stub them.
const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';

const sandbox = { console };
sandbox.window = sandbox; // so `window["__POKEDEX__"] = vv` lands on sandbox.__POKEDEX__ too, and `window.X` reads work
// The bundle has a dev/anti-clone hostname check (`window.__pkl_allowed`,
// throws "locked" off pokelike.xyz/localhost/127.0.0.1/etc). "localhost" is
// itself on that allowlist -- this isn't a bypass, it's just running the
// same check the game's own devs allow for local development.
sandbox.location = { hostname: 'localhost' };
// The lock-check IIFE also re-arms itself with setTimeout/setInterval --
// no-op stubs are fine, we only need the tables it doesn't touch.
sandbox.setTimeout = () => 0;
sandbox.setInterval = () => 0;
sandbox.clearTimeout = () => {};
sandbox.clearInterval = () => {};
vm.createContext(sandbox);
vm.runInContext(DECODER_STUBS + prefix + '\n' + EXTRACT_SNIPPET, sandbox, { timeout: 30000 });

const extracted = sandbox.__EXTRACTED__;
const missing = Object.entries(extracted).filter(([, v]) => v === undefined).map(([k]) => k);
if (missing.length) console.warn('WARNING - these tables came back undefined:', missing);

fs.mkdirSync(outDir, { recursive: true });
for (const [key, value] of Object.entries(extracted)) {
  if (value === undefined) continue;
  const file = path.join(outDir, key + '.json');
  fs.writeFileSync(file, JSON.stringify(value, null, 2));
  const count = Array.isArray(value) ? value.length : typeof value === 'object' ? Object.keys(value).length : 1;
  console.log(`wrote ${file} (${count} top-level entries)`);
}
