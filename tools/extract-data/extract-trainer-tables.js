// Pulls the procedural mid-map trainer archetype pool table
// (TRAINER_BATTLE_CONFIG + the generation-gated archetype-key lists that
// feed doTrainerNode's/generateMap's trainerSprite hash) and the fixed
// Silver/Magma/Aqua special-rival rosters (SILVER_ENCOUNTERS,
// SILVER_STARTER_LINES, MAGMA_ENCOUNTERS, AQUA_ENCOUNTERS) out of the
// deobfuscated bundle as real JSON, instead of hand-transcribing.
//
// Why this is safe to just RUN: these tables all live within
// bundle.deobfuscated.js lines 1-81051, the exact range
// tools/battle-oracle/extract-prefix.js already audited (see that file's
// header) -- a manual re-derivation of the same span's top-level statement
// inventory would be redundant, so this script reuses the identical cutoff
// (right after the `mergeBattleConfigs` FunctionDeclaration) and the same
// inert `document` stub that audit's own `scan-toplevel-danger.js` found
// necessary (six real top-level `document[...]` UI-wiring calls around
// lines 63649-63818 -- unconditional at module-load time, but pure DOM
// wiring never reachable from the data tables we read here).
//
// Usage:
//   node extract-trainer-tables.js <bundle.deobfuscated.js> <out-dir>

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const acorn = require('acorn');

const [, , inPath, outDir] = process.argv;
if (!inPath || !outDir) {
  console.error('Usage: node extract-trainer-tables.js <bundle.deobfuscated.js> <out-dir>');
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
  const count = prefix.split(risky).length - 1;
  if (count) console.log(`NOTE: prefix textually contains "${risky}" ${count}x (expected -- see header, all inside function bodies never called by this script)`);
}

new vm.Script(prefix, { filename: 'trainer-tables-prefix-syntax-check.js' });

const EXTRACT_SNIPPET = `
function __asPlain(x) { return x instanceof Set ? [...x] : x; }
globalThis.__EXTRACTED__ = {
  trainerBattleConfig: typeof TRAINER_BATTLE_CONFIG !== 'undefined' ? TRAINER_BATTLE_CONFIG : undefined,
  trainerSpriteKeys: typeof TRAINER_SPRITE_KEYS !== 'undefined' ? __asPlain(TRAINER_SPRITE_KEYS) : undefined,
  gen2OnlyTrainerKeys: typeof GEN2_ONLY_TRAINER_KEYS !== 'undefined' ? __asPlain(GEN2_ONLY_TRAINER_KEYS) : undefined,
  gen1OnlyTrainerKeys: typeof GEN1_ONLY_TRAINER_KEYS !== 'undefined' ? __asPlain(GEN1_ONLY_TRAINER_KEYS) : undefined,
  gen3TrainerKeys: typeof GEN3_TRAINER_KEYS !== 'undefined' ? __asPlain(GEN3_TRAINER_KEYS) : undefined,
  gen4TrainerKeys: typeof GEN4_TRAINER_KEYS !== 'undefined' ? __asPlain(GEN4_TRAINER_KEYS) : undefined,
  silverEncounters: typeof SILVER_ENCOUNTERS !== 'undefined' ? SILVER_ENCOUNTERS : undefined,
  silverStarterLines: typeof SILVER_STARTER_LINES !== 'undefined' ? SILVER_STARTER_LINES : undefined,
  magmaEncounters: typeof MAGMA_ENCOUNTERS !== 'undefined' ? MAGMA_ENCOUNTERS : undefined,
  aquaEncounters: typeof AQUA_ENCOUNTERS !== 'undefined' ? AQUA_ENCOUNTERS : undefined,
};
`;

const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';

// Same inert DOM stub as tools/battle-oracle/run-fixture.js -- see that
// file's comment for the exact scan-toplevel-danger.js finding this covers.
function makeInertDomStub() {
  const target = function inertDomStub() {};
  const stub = new Proxy(target, {
    get(_t, prop) {
      if (prop === Symbol.toPrimitive || prop === 'then' || prop === Symbol.iterator) return undefined;
      return stub;
    },
    apply() {
      return stub;
    },
    set() {
      return true;
    },
  });
  return stub;
}

const sandbox = { console };
sandbox.window = sandbox;
sandbox.location = { hostname: 'localhost' };
sandbox.setTimeout = () => 0;
sandbox.setInterval = () => 0;
sandbox.clearTimeout = () => {};
sandbox.clearInterval = () => {};
sandbox.document = makeInertDomStub();
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
