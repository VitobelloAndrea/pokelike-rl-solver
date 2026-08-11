// Pulls the map-node *presentation* tables out of the deobfuscated bundle:
// the trainer sprite-filename maps per generation, the trainer display names,
// and the trainer type-specialty strings. These feed `getNodeSprite` and
// `getNodeLabel`, which R2 ports into `pokelike/render/contract.py`.
//
// Why a line slice rather than `extract-tables.js`'s acorn walk: these four
// sprite maps and five specialty maps are built by obfuscator-renamed locals
// (`B2K`, `B2E`, `B2X`, ...), not by the un-mangled top-level identifiers that
// tool relies on. The bundle is content-pinned for this repository, so a line
// slice is stable -- but the slice is anchored on the exact declaration lines
// it expects and refuses to run if the bundle has moved underneath it.
//
// Usage:
//   node extract-node-presentation.js <bundle.deobfuscated.js> <out.json>

const fs = require('fs');
const vm = require('vm');

const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) {
  console.error('usage: node extract-node-presentation.js <bundle> <out.json>');
  process.exit(2);
}

const lines = fs.readFileSync(inPath, 'utf8').split('\n');
const slice = (a, b) => lines.slice(a - 1, b).join('\n');
const at = (n) => (lines[n - 1] === undefined ? '' : lines[n - 1].trim());

// Fail loudly rather than silently extracting the wrong region.
const ANCHORS = [
  [53656, 'const B2K = {};'],
  [53711, 'const TRAINER_SPRITE_KEYS = ['],
  [53773, '];'],
  [53792, 'const B2E = {};'],
  [53827, 'const B2X = {};'],
  [53889, 'TRAINER_SPECIALTIES_GEN4 = B2f,'],
];
for (const [line, expected] of ANCHORS) {
  if (at(line) !== expected) {
    console.error(`ANCHOR MISMATCH at ${inPath}:${line}`);
    console.error(`  expected: ${expected}`);
    console.error(`  found:    ${at(line)}`);
    process.exit(1);
  }
}

const parts = [
  // 53656-53773: the four sprite-filename maps plus the per-generation
  // trainer key lists that index them.
  slice(53656, 53773),
  // 53792-53889: trainer display names (B2E) and the four specialty maps.
  // The region ends mid-declaration, so the const chain is closed here.
  slice(53792, 53889),
  '_tail = null;',
  'result = {',
  '  SPRITE_FILE: B2K,',
  '  GEN2_SPRITE_FILENAME: B2k,',
  '  GEN3_SPRITE_FILENAME: B2c,',
  '  GEN4_SPRITE_FILENAME: B2I,',
  '  TRAINER_SPRITE_NAMES: B2E,',
  '  TRAINER_SPECIALTIES: B2X,',
  '  TRAINER_SPECIALTIES_GEN2: B2m,',
  '  TRAINER_SPECIALTIES_GEN3: B2x,',
  '  TRAINER_SPECIALTIES_GEN4: B2f,',
  '  TRAINER_SPRITE_KEYS,',
  '  GEN3_TRAINER_KEYS,',
  '  GEN4_TRAINER_KEYS,',
  '};',
];

const sandbox = { result: null };
vm.runInNewContext(parts.join('\n'), sandbox, { filename: 'bundle-slice.js' });

// The gym-leader and champion sprite arrays are plain literals at
// 53893-53943; they are declared with un-mangled names, so read them from the
// same sandbox rather than re-slicing.
const spriteArrays = { result: null };
vm.runInNewContext(
  slice(53893, 53943).replace(/^\s*KANTO_GYM_LEADER_SPRITES = \[/, 'const KANTO_GYM_LEADER_SPRITES = [')
    .replace(/;\s*$/, ';')
  + '\nresult = { KANTO_GYM_LEADER_SPRITES, JOHTO_GYM_LEADER_SPRITES, HOENN_GYM_SHOWDOWN_SPRITES,'
  + ' SINNOH_GYM_SHOWDOWN_SPRITES, SINNOH_CHAMPION_SPRITE, KANTO_GYM_SHOWDOWN_SPRITES };',
  spriteArrays,
  { filename: 'bundle-slice-sprites.js' },
);

const out = JSON.parse(JSON.stringify({ ...sandbox.result, ...spriteArrays.result }));
for (const [k, v] of Object.entries(out)) {
  const n = Array.isArray(v) ? v.length : typeof v === 'string' ? 1 : Object.keys(v).length;
  console.error(`${k}: ${n}`);
}
fs.writeFileSync(outPath, JSON.stringify(out, null, 2) + '\n');
console.error(`wrote ${outPath}`);
