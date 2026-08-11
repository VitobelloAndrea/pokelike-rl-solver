// Pulls the special-submap tables (SUBMAP_BOSSES, SUBMAP_REWARDS,
// DISTORTION_LEGENDARY_POOL, DISTORTION_LEGEND_REWARDS,
// UNDERGROUND_TRAINER_KEYS) that back `generateSubMap`/`rollSubMapBoss`/
// `rollUndergroundTrainers`/`pickSubMapRewards`/`distortionLegendary`
// (bundle.deobfuscated.js:53508-53632, 76247-76837) out of the deobfuscated
// bundle as real JSON, instead of hand-transcribing.
//
// Why this is safe to just RUN: same reasoning and the same audited prefix
// cutoff as extract-trainer-tables.js in this directory -- these tables all
// live within bundle.deobfuscated.js lines 1-81051, the exact range
// tools/battle-oracle/extract-prefix.js already AST-audited for top-level
// side effects (see that file's header / scan-toplevel-danger.js). This
// script reuses the identical cutoff (right after the `mergeBattleConfigs`
// FunctionDeclaration) and the same inert `document` stub.
//
// Usage:
//   node extract-submap-tables.js <bundle.deobfuscated.js> <out-dir>

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const acorn = require('acorn');

const [, , inPath, outDir] = process.argv;
if (!inPath || !outDir) {
  console.error('Usage: node extract-submap-tables.js <bundle.deobfuscated.js> <out-dir>');
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

new vm.Script(prefix, { filename: 'submap-tables-prefix-syntax-check.js' });

const EXTRACT_SNIPPET = `
function __asPlain(x) { return x instanceof Set ? [...x] : x; }
globalThis.__EXTRACTED__ = {
  submapBosses: typeof SUBMAP_BOSSES !== 'undefined' ? SUBMAP_BOSSES : undefined,
  submapRewards: typeof SUBMAP_REWARDS !== 'undefined' ? SUBMAP_REWARDS : undefined,
  distortionLegendaryPool: typeof DISTORTION_LEGENDARY_POOL !== 'undefined' ? DISTORTION_LEGENDARY_POOL : undefined,
  distortionLegendRewards: typeof DISTORTION_LEGEND_REWARDS !== 'undefined' ? __asPlain(DISTORTION_LEGEND_REWARDS) : undefined,
  undergroundTrainerKeys: typeof UNDERGROUND_TRAINER_KEYS !== 'undefined' ? __asPlain(UNDERGROUND_TRAINER_KEYS) : undefined,
};
`;

const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';

// Same inert DOM stub as tools/battle-oracle/run-fixture.js and
// extract-trainer-tables.js in this directory.
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
