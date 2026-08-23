// The JavaScript side of the M3 short-full-run route oracle.
//
// Executes a deterministic Story/Nuzlocke route through the REAL source
// functions in the audited route prefix (`out/route-prefix.js`, produced by
// extract-prefix.js -- see that file's header for the cut point and its
// safety reasoning) and emits a normalized checkpoint stream on stdout.
//
//   node run-scenario.js <scenario.json>        -> one JSON object on stdout
//
// ---------------------------------------------------------------------------
// WHAT IS REAL AND WHAT IS STUBBED
// ---------------------------------------------------------------------------
//
// Real (never reimplemented here): startNewRun, showStarterSelect,
// selectStarter, startMap, generateMap, generateSubMap, onNodeClick,
// advanceFromNode, doBattleNode, doBossNode, doTrainerNode, doSilverNode,
// doAdminNode, doCatchNode, doItemNode, doPokeCenterNode, doLegendaryNode,
// doMoveTutorNode, doTradeNode, doShinyNode, enterSubMap, doSubMapBoss,
// doSubMapReward, returnFromSubMap, showSwapScreen, catchPokemon,
// runBattleScreen, runBattle, applyLevelGain, checkAndEvolveTeam,
// grantPickupItem, incrementStoryCounter, showBadgeScreen (its "next map"
// handler IS the map transition), rollShiny, createInstance,
// fetchPokemonById, weightedRandom, rng/seedRng/getRngSeed.
//
// Stubbed, and ONLY these categories:
//   * presentation  - showScreen (also used as the source's own "which
//                     screen is the player sitting on" signal), renderMap,
//                     showMapScreen, renderBattleField, renderTeamBar,
//                     renderItemBadges, renderTrainerIcons, renderPokemonCard,
//                     animateBattleVisually, animateLevelUp, showToast,
//                     showMapNotification, showElitePrepScreen,
//                     showTrainerSelect, makeTraitOverlay,
//                     renderTraitPreview/renderTraitDeltaRows,
//                     showTeamHoverCard/hideTeamHoverCard, makeMaxedStarsEl,
//                     showGameOver (pure end-of-run screen + account stats).
//   * persistence   - saveRun, and the `localStorage` object itself (an
//                     in-memory store installed AFTER load; see below).
//                     `captureMapStart`, `isSpeciesOwned`, `ownershipBadges`,
//                     `loadPersistentBuffs`, `markPokedexCaught` and friends
//                     are REAL and correctly behave as a brand-new account
//                     against that empty store.
//                     `recordMonOrigin` is REAL and deliberately NOT stubbed:
//                     despite the name, its body (bundle.deobfuscated.js:
//                     79047-79063) sets `state.usedBallCatch` /
//                     `state.gotViaQuestion`, which are RUN state this schema
//                     compares. Only the story counter it then increments is
//                     absorbed by the in-memory store. Stubbing it SUPPRESSED
//                     gameplay state, which is as wrong as inventing some.
//                     (M3.3: this header previously still listed it as
//                     stubbed, contradicting driver.js.)
//   * timers/audio  - setTimeout/setInterval/requestAnimationFrame are routed
//                     through a deterministic virtual queue; there is no audio.
//   * network       - none is reachable on the routes used; `fetch` is left
//                     undefined so any attempt is a hard ReferenceError rather
//                     than a silent fallback (see NETWORK GUARD below).
//
// No stub generates gameplay state and no stub calls rng(). The RNG counter
// wraps the source's own `rng` binding, so any draw a stub accidentally made
// would show up in the checkpoint stream rather than hide.
//
// ---------------------------------------------------------------------------
// WHY `localStorage` IS DEFINED ONLY AFTER THE PREFIX HAS LOADED
// ---------------------------------------------------------------------------
//
// Defining `localStorage` on the sandbox BEFORE running the prefix makes the
// prefix hang at load (>25s, reproduced this session; tools/battle-oracle hit
// the same wall). `scan-toplevel-danger.js` (extended this session to descend
// into immediately-invoked function expressions, which the battle-oracle
// scanner did not) shows why: 10 load-reachable `localStorage` references
// live inside two top-level IIFEs -- the storage-migration block at
// bundle.deobfuscated.js:38813-38863 and the progress-backup block at
// 38864+, each opening with `if (typeof localStorage == "undefined") return;`,
// the second of which sits behind the obfuscator's self-defending wrapper.
// Leaving the global undefined satisfies both guards, so both IIFEs return
// immediately. The driver then installs a plain in-memory stub for the
// FUNCTION bodies that need one (`getHallOfFame`, `getSettings`,
// `unlockAchievement`, ... all called later, by us). Empty storage is the
// correct model here anyway: it is a brand-new account with no Hall of Fame,
// no persistent stat buffs and no saved settings, which is what the Python
// port represents.
//
// ---------------------------------------------------------------------------
// DRIVER SHAPE
// ---------------------------------------------------------------------------
//
// As in tools/battle-oracle/run-fixture.js, the driver is concatenated onto
// the prefix and executed as ONE `vm.runInContext` call: `state` is declared
// `let state = BOg;` (bundle.deobfuscated.js:74982) and Node's vm does not
// reflect top-level `let`/`const` bindings onto the sandbox object, so a
// separate second call could not see it. Communication is `__SCENARIO__` in,
// `__RESULT__` out.
//
// The route is driven the way a player drives it: by invoking the source's
// OWN handlers (`onNodeClick`, the swap screen's card click listeners, the
// catch screen's card listeners, `btn-continue-battle`, `btn-next-map`),
// never by calling gameplay internals directly.

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeSandbox } = require('./sandbox.js');

const SCHEMA_VERSION = 2;

const [, , scenarioPath] = process.argv;
if (!scenarioPath) {
  console.error('Usage: node run-scenario.js <scenario.json>');
  process.exit(1);
}

const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));
if (scenario.schema_version !== SCHEMA_VERSION) {
  console.error(
    `scenario ${scenarioPath} declares schema_version ${scenario.schema_version}, runner speaks ${SCHEMA_VERSION}`,
  );
  process.exit(1);
}

const prefixPath = path.join(__dirname, 'out', 'route-prefix.js');
if (!fs.existsSync(prefixPath)) {
  console.error(
    `missing ${prefixPath} -- run: node route-oracle/extract-prefix.js ` +
      'pokelike_forked/js/bundle.deobfuscated.js route-oracle/out/route-prefix.js',
  );
  process.exit(1);
}
const rawPrefix = fs.readFileSync(prefixPath, 'utf8');

// Round-counter instrumentation, identical to tools/battle-oracle/
// run-fixture.js: an exact, assertion-guarded source edit. If the needle
// stops matching exactly once the runner fails loudly rather than silently
// reporting `rounds: 0` for every battle.
//
// M3.3b workstream 5 extends the SAME site with a turn-boundary mark. The
// source's `detailedLog` is a flat stream with no per-round delimiter -- the
// only `overtime_start` marker is pushed once, at the overtime threshold
// (bundle.deobfuscated.js:55418-55422) -- so an ordered PER-TURN projection
// needs to know where each round begins. `BI4++` is the round counter's own
// increment at the very top of the round loop (55415-55418), before any of
// that round's events exist, and `BcM` (the detailed log) is in scope there.
// Recording `BcM.length` at that instant yields the exact turn boundaries
// without touching the log, the loop, any source state, or the RNG.
const ROUND_COUNTER_NEEDLE = '(BI4++,\n      BI4 === BI6 + 0x1 &&';
const roundNeedleCount = rawPrefix.split(ROUND_COUNTER_NEEDLE).length - 1;
if (roundNeedleCount !== 1) {
  console.error(`round-counter instrumentation expected one source match, found ${roundNeedleCount}`);
  process.exit(1);
}
const prefix = rawPrefix.replace(
  ROUND_COUNTER_NEEDLE,
  '(BI4++, globalThis.__ROUND_COUNT__ = BI4, globalThis.__ROUND_MARKS__.push(BcM.length),\n      BI4 === BI6 + 0x1 &&',
);

// Same two decoder shims tools/extract-data and tools/battle-oracle install:
// the obfuscator's string-decoder helpers are already inlined in the
// deobfuscated bundle, but a handful of `const Bxx = k;` / `K()` residues
// remain as no-ops.
const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';

const DRIVER = fs.readFileSync(path.join(__dirname, 'driver.js'), 'utf8');
// ---------------------------------------------------------------------------
// Sandbox
// ---------------------------------------------------------------------------
//
// M7 moved this verbatim into `sandbox.js` so the interactive sweep host
// (`sweep-host.js`) builds the SAME sandbox from the same definition rather
// than a second copy that can drift. Nothing about it changed; the frozen
// parity signature and the 29/29 strict result prove that.

const sandbox = makeSandbox();
sandbox.__SCENARIO__ = scenario;

vm.createContext(sandbox);
vm.runInContext(DECODER_STUBS + prefix + '\n' + DRIVER, sandbox, { timeout: 300000 });

(async () => {
  // The driver is async; give its microtasks a chance to complete.
  for (let i = 0; i < 200000 && !sandbox.__DONE__; i++) {
    await new Promise((r) => setImmediate(r));
  }
  if (!sandbox.__DONE__) {
    console.error('route oracle did not finish (driver never signalled completion)');
    process.exit(1);
  }
  if (sandbox.__FATAL__) {
    console.error('route oracle fatal error:\n' + sandbox.__FATAL__);
    process.exit(1);
  }
  process.stdout.write(sandbox.__RESULT__);
})();
