// Extracts the audited JS "route prefix" for the M3 short-full-run oracle:
// everything from the top of `pokelike_forked/js/bundle.deobfuscated.js`
// through the end of `loadBuffsIntoPokemon` (bundle.deobfuscated.js:87620-
// 87643), i.e. the statement immediately BEFORE
// `function checkMaxStatAchievements(` at line 87644.
//
// Why a longer prefix than tools/battle-oracle's:
//
//   tools/battle-oracle/extract-prefix.js cuts after `mergeBattleConfigs`
//   (line 81051) because a battle fixture only needs the battle math. A
//   short FULL-RUN route additionally needs the run/map/node lifecycle:
//     startNewRun            75442
//     selectStarter          76196   startMap            76228
//     generateMap            53214   generateSubMap      53508
//     advanceFromNode        53639   onNodeClick         77305
//     doBattleNode           77655   doBossNode          77742
//     doTrainerNode          80213   doSilverNode        77895
//     doAdminNode            77958   doCatchNode         78426
//     doItemNode             79260   doPokeCenterNode    79891
//     doLegendaryNode        80346   doMoveTutorNode     80464
//     doTradeNode            80580   doShinyNode         80872
//     enterSubMap            76687   returnFromSubMap    76708
//     doSubMapBoss           76752   doSubMapReward      76885
//     showSwapScreen         79141   catchPokemon        79026
//     checkAndEvolveTeam     70615   applyLevelGain      56791
//     grantPickupItem        77615   incrementStoryCounter 52099
//     runBattleScreen        81052   showBadgeScreen     81435
//     showGameOver           81507
//   plus the data/collection helpers those call unguarded, which live even
//   further down and would otherwise be ReferenceErrors:
//     captureMapStart        84309   syncMegaForm        86461
//     loadPersistentBuffs    86343   getTotalBuffPoints  86559
//     getEvoLineRoot         86584   isSpeciesOwned      86723
//     ownershipBadges        86728   loadBuffsIntoPokemon 87620
//   The last of those is `loadBuffsIntoPokemon`, so the cut lands right
//   after it. Including them for real is deliberate: `getEvoLineRoot` is an
//   evolution-line DATA lookup (gameplay), not a persistence call, and
//   hand-stubbing it would have replaced source behavior. The genuinely
//   persistence-shaped ones in that list read an empty `localStorage` and
//   correctly behave as a brand-new account.
//
// Why the cut is NOT simply "the whole file": the bundle's final top-level
// statement is an IIFE that ends with
//   document.readyState === "loading" ? document.addEventListener(...) : Bco();
// (bundle tail, the `__perfRig` block). Against an inert `document` stub
// `readyState` is not the string "loading", so merely LOADING a
// whole-file prefix would immediately execute that rig's `B71()/BcS()/BcJ()`
// bootstrap. Cutting at `showWinScreen` keeps every top-level statement in
// range auditable by scan-toplevel-danger.js and keeps the loaded program
// side-effect-free apart from the six already-documented `document[...]`
// UI-wiring statements around lines 63649-63818 (see run-scenario.js's
// `makeInertDomStub`).
//
// Deliberately dependency-free: unlike tools/battle-oracle/extract-prefix.js
// this does not require `acorn`, so a fresh checkout needs only `node` and
// `python`. The cut point is found by an assertion-guarded unique needle
// (the same convention the battle oracle uses for its round-counter
// instrumentation): if the needle stops matching exactly once, this fails
// loudly instead of silently producing a differently-shaped prefix.
// `route-oracle/scan-toplevel-danger.js` still performs the real AST audit
// and DOES need acorn; it is an audit step, not part of `compare.py`.
//
// Usage:
//   node extract-prefix.js <bundle.deobfuscated.js> <out-file.js>

'use strict';

const fs = require('fs');
const vm = require('vm');

const CUT_NEEDLE = '\nfunction checkMaxStatAchievements(';

const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) {
  console.error('Usage: node extract-prefix.js <bundle.deobfuscated.js> <out-file.js>');
  process.exit(1);
}

const src = fs.readFileSync(inPath, 'utf8');

const needleCount = src.split(CUT_NEEDLE).length - 1;
if (needleCount !== 1) {
  console.error(
    `route-prefix cut point expected exactly one match of ${JSON.stringify(CUT_NEEDLE)}, found ${needleCount}. ` +
      'The bundle changed shape -- re-audit the cut point before regenerating.',
  );
  process.exit(1);
}

const cutAt = src.indexOf(CUT_NEEDLE) + 1; // keep the newline with the prefix
const prefix = src.slice(0, cutAt);
const lineCount = prefix.split('\n').length - 1;

// Compile-only syntax check: throws SyntaxError if the slice is malformed
// (e.g. cut mid-statement), catching a bad boundary immediately rather than
// at oracle-run time.
new vm.Script(prefix, { filename: 'route-prefix-syntax-check.js' });

fs.writeFileSync(outPath, prefix);
console.log(`wrote ${outPath} (${lineCount} lines, ${prefix.length} bytes)`);
