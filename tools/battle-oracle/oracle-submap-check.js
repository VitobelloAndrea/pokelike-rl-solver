// One-off cross-check (not part of the permanent fixture suite) proving
// map_gen.generate_sub_map/_roll_underground_trainers/_roll_sub_map_boss/
// _pick_sub_map_rewards/_distortion_legendary against the REAL
// generateSubMap/rollUndergroundTrainers/rollSubMapBoss/pickSubMapRewards/
// distortionLegendary functions, executed via the SAME already-audited
// prefix tools/battle-oracle/run-fixture.js uses (out/battle-prefix.js,
// which already covers these functions -- they live at
// bundle.deobfuscated.js:53508-53632/76399-76837, well inside the audited
// range that ends near line 81051). Prints one JSON object per scenario to
// stdout; compare-submap.py (or manual inspection) diffs it against the
// Python port for the same seed/state.
//
// Usage:
//   node oracle-submap-check.js <scenario.json>
//
// scenario.json: {
//   "seed": number, "kind": "underground"|"distortion", "mapIndex": number,
//   "parentNodeLevel": number, "teamSize": number,
//   "distortionWorldsEntered": number, "distortionLegendaryClaimed": bool
// }

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const [, , scenarioPath] = process.argv;
if (!scenarioPath) {
  console.error('Usage: node oracle-submap-check.js <scenario.json>');
  process.exit(1);
}
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));

const prefixPath = path.join(__dirname, 'out', 'battle-prefix.js');
const prefix = fs.readFileSync(prefixPath, 'utf8');
const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';

const DRIVER = `
;(function () {
  var scenario = globalThis.__SCENARIO__;
  state.currentMap = scenario.mapIndex;
  state.gen4Mode = true;
  state.isEndlessMode = false;
  state.team = new Array(scenario.teamSize).fill(0).map(() => ({}));
  state.distortionWorldsEntered = scenario.distortionWorldsEntered || 0;
  state.distortionLegendaryClaimed = !!scenario.distortionLegendaryClaimed;
  state.subMapReturn = scenario.parentNodeLevel != null
    ? { map: { nodes: { anchor: { layer: 0 } } }, nodeId: 'anchor', mapIndex: scenario.mapIndex }
    : null;

  // subMapBaseLevel calls getLevelForNode(subMapReturn.map.nodes[nodeId])+1;
  // stub getLevelForNode so it returns the requested parent level directly
  // (isolating this check from getLevelForNode's own, separately-validated
  // RNG-consuming gen1 branch -- irrelevant here since submaps are
  // gen4Mode-only, where the real getLevelForNode is already RNG-free).
  var realGetLevelForNode = getLevelForNode;
  getLevelForNode = function (node) {
    if (scenario.parentNodeLevel != null) return scenario.parentNodeLevel;
    return realGetLevelForNode(node);
  };

  seedRng(scenario.seed >>> 0);
  var rngDraws = 0;
  var sourceRng = rng;
  rng = function countedOracleRng() {
    rngDraws++;
    return sourceRng();
  };

  var map = generateSubMap(scenario.kind);

  function normNode(n) {
    return {
      id: n.id, type: n.type, layer: n.layer, col: n.col,
      visited: n.visited, accessible: n.accessible,
      extra: Object.fromEntries(
        Object.entries(n).filter(([k]) => !['id','type','layer','col','visited','accessible','revealed'].includes(k)),
      ),
    };
  }

  globalThis.__RESULT__ = {
    isSubMap: map.isSubMap,
    mapIndex: map.mapIndex,
    nodes: Object.fromEntries(Object.entries(map.nodes).map(([id, n]) => [id, normNode(n)])),
    edges: map.edges,
    rngDraws: rngDraws,
    distortionWorldsEnteredAfter: state.distortionWorldsEntered,
  };
})();
`;

const sandbox = { console };
sandbox.window = sandbox;
sandbox.location = { hostname: 'localhost' };
sandbox.setTimeout = () => 0;
sandbox.setInterval = () => 0;
sandbox.clearTimeout = () => {};
sandbox.clearInterval = () => {};
const target = function inertDomStub() {};
sandbox.document = new Proxy(target, {
  get(_t, prop) {
    if (prop === Symbol.toPrimitive || prop === 'then' || prop === Symbol.iterator) return undefined;
    return sandbox.document;
  },
  apply() { return sandbox.document; },
  set() { return true; },
});
sandbox.__SCENARIO__ = scenario;
vm.createContext(sandbox);
vm.runInContext(DECODER_STUBS + prefix + '\n' + DRIVER, sandbox, { timeout: 30000 });

console.log(JSON.stringify(sandbox.__RESULT__, null, 2));
