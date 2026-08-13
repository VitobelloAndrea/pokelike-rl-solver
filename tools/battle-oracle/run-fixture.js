// The JS side of the battle-equivalence oracle. Loads a fixture (JSON),
// builds battler objects in the exact shape `runBattle`/`buildGen3AbilityConfig`/
// `buildTraitsConfig` expect, seeds the module-global RNG stream deterministically,
// calls the REAL source functions (via the audited prefix built by
// extract-prefix.js -- see that file's header for the safety reasoning),
// and prints one normalized JSON result line to stdout.
//
// Usage:
//   node run-fixture.js <fixture.json>
//
// **Why the driver code is a big string concatenated onto the prefix
// instead of a second `vm.runInContext` call**: `state` (the field
// `getGen3Ability` reads) is declared `let state = BOg;` inside the prefix
// (bundle.deobfuscated.js:74982). Node's `vm` module only reflects top-level
// `var`/`function` declarations onto the contextified sandbox object --
// `let`/`const` bindings live in a lexical scope private to that ONE
// `runInContext` call and are invisible to a later separate call, even
// against the same context object. So the fixture -> battler-object ->
// runBattle -> normalized-result driver below has to run as part of the
// SAME script execution as the prefix (one string, one `runInContext` call),
// communicating in via `sandbox.__FIXTURE__` (set before running) and out
// via `globalThis.__RESULT__` (a plain assignment, unaffected by the
// let/const hoisting gap since it's not a declaration).
//
// Stubs reused/extended from tools/extract-data/extract-tables.js (window/
// location/setTimeout/setInterval/k/K) -- same reasoning, see that file --
// plus a `document` stub this session added after `scan-toplevel-danger.js`
// (AST-based, not textual grep) found real top-level `document[...]` calls
// the original textual safety scan had missed; see that stub's own comment
// below for what it covers and why it's inert.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const [, , fixturePath] = process.argv;
if (!fixturePath) {
  console.error('Usage: node run-fixture.js <fixture.json>');
  process.exit(1);
}

const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const prefixPath = path.join(__dirname, 'out', 'battle-prefix.js');
if (!fs.existsSync(prefixPath)) {
  console.error(`missing ${prefixPath} -- run: node extract-prefix.js ../../pokelike_forked/js/bundle.deobfuscated.js ./out/battle-prefix.js`);
  process.exit(1);
}
const prefix = fs.readFileSync(prefixPath, 'utf8');
const ROUND_COUNTER_NEEDLE = '(BI4++,\n      BI4 === BI6 + 0x1 &&';
const roundNeedleCount = prefix.split(ROUND_COUNTER_NEEDLE).length - 1;
if (roundNeedleCount !== 1) {
  throw new Error(`round-counter instrumentation expected one source match, found ${roundNeedleCount}`);
}
const instrumentedPrefix = prefix.replace(
  ROUND_COUNTER_NEEDLE,
  '(BI4++, globalThis.__ROUND_COUNT__ = BI4,\n      BI4 === BI6 + 0x1 &&',
);

const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';

const DRIVER = `
;(function () {
  var fixture = globalThis.__FIXTURE__;

  // Only 4 fields on \`state\` are read anywhere in the runBattle/
  // buildGen3AbilityConfig/buildTraitsConfig/mergeBattleConfigs call graph
  // (getGen3Ability, bundle.deobfuscated.js:57034-57048).
  state.gen3Mode = fixture.gen === 3;
  state.gen4Mode = fixture.gen === 4;
  state.challengeGen3 = false;
  state.challengeGen4 = false;
  // hasPassive (bundle.deobfuscated.js:55136-55139, and buildTraitsConfig's
  // own re-implementation, B2Q) reads \`trait.id\`/\`trait.enabled\` off each
  // list entry -- a bare string here would silently make every B2Q(...)
  // check false (\`"sword_charm"["id"]\` is undefined), while still counting
  // toward buildTraitsConfig's \`passives.length > 0\` null-return guard. So
  // a bare-string passives list would falsely force the merge path (Stage 4)
  // while making every individual trait a no-op -- normalize fixture
  // passives (plain id strings, for fixture readability) into the real
  // {id, enabled} shape here.
  state.passives = (fixture.passives || []).map((p) =>
    typeof p === 'string' ? { id: p, enabled: true } : p,
  );
  state.team = [];
  state.items = [];

  function buildBattler(spec) {
    var baseStats = spec.base_stats;
    var maxHp = spec.max_hp !== undefined ? spec.max_hp : calcHp(baseStats.hp, spec.level);
    return {
      speciesId: spec.species_id,
      name: spec.name || ('Species' + spec.species_id),
      nickname: spec.name || null,
      level: spec.level,
      baseStats: baseStats,
      types: spec.types,
      moveTier: spec.move_tier !== undefined ? spec.move_tier : 1,
      heldItem: spec.held_item ? { id: spec.held_item } : null,
      isShiny: !!spec.is_shiny,
      currentHp: spec.current_hp !== undefined ? spec.current_hp : maxHp,
      maxHp: maxHp,
      statBuffs: spec.stat_buffs || {},
      _burned: !!spec.burned,
      _paralyzed: !!spec.paralyzed,
      _poisonStacks: spec.poison_stacks || 0,
    };
  }

  var playerTeam = fixture.player_team.map(buildBattler);
  var enemyTeam = fixture.enemy_team.map(buildBattler);

  seedRng(fixture.seed >>> 0);
  var rngDraws = 0;
  var sourceRng = rng;
  rng = function countedOracleRng() {
    rngDraws++;
    return sourceRng();
  };

  // Battle-config branch selection -- faithful reproduction of
  // \`runBattleScreen\`'s ordinary (non-Endless) Story/Nuzlocke branch,
  // bundle.deobfuscated.js:81075-81085. Gen1/Gen2 -> no config at all
  // (null). Gen3/Gen4 -> buildGen3AbilityConfig() ALWAYS, merged with
  // buildTraitsConfig({}, {}, passives) -- buildTraitsConfig itself returns
  // null when its tier maps AND passives are all empty
  // (bundle.deobfuscated.js:60733-60738), and mergeBattleConfigs(O, null)
  // just returns O unchanged (line 80994), so this one call faithfully
  // covers both the "merged" and "ability-only" cases without a separate
  // branch here.
  //
  // M6.2: \`buildTraitsConfig\`'s FIRST TWO parameters are the player/enemy
  // trait-tier maps (bundle.deobfuscated.js:60733). The ordinary
  // Story/Nuzlocke branch above genuinely passes \`{}, {}\`, so every
  // tier-gated trait (Ground/Fairy onStartFight, the Water afterAttack
  // block, water_retaliate) is structurally unreachable there. Other REAL
  // source call sites do pass computed maps -- 76812 (elite prep), 81069
  // (Endless), 86059 (Endless trainer), 90734 -- so a fixture may declare
  // \`player_tiers\`/\`enemy_tiers\` explicitly to exercise those already-ported
  // traits against the real source. Absent both keys this is byte-equivalent
  // to the previous \`{}, {}\` call, so every pre-M6.2 fixture is unaffected.
  var battleConfig = null;
  if (fixture.gen === 3 || fixture.gen === 4) {
    battleConfig = mergeBattleConfigs(
      buildGen3AbilityConfig(),
      buildTraitsConfig(
        fixture.player_tiers || {},
        fixture.enemy_tiers || {},
        state.passives,
      ),
    );
  }

  // Oracle-only hook instrumentation. Record only status-relevant calls so
  // ordinary attack KOs in older fixtures do not pollute this narrow P0.2
  // trace. Wrapping the FINAL config observes the actual merged call site.
  var hookTrace = [];
  function wrapHook(name, makeEvent) {
    if (!battleConfig || typeof battleConfig[name] !== 'function') return;
    var original = battleConfig[name];
    battleConfig[name] = function () {
      var args = Array.prototype.slice.call(arguments);
      var event = makeEvent(args);
      if (event) hookTrace.push(event);
      return original.apply(battleConfig, args);
    };
  }
  wrapHook('onKO', function (args) {
    var fainted = args[0];
    if (!fainted || !(fainted._poisonStacks > 0)) return null;
    return {
      hook: 'on_ko',
      fainted_side: args[2],
      fainted_idx: args[1],
      killer_side: args[5],
      killer_idx: args[4],
    };
  });
  wrapHook('onFaint', function (args) {
    var fainted = args[0];
    if (!fainted || !(fainted._poisonStacks > 0)) return null;
    return { hook: 'on_faint', side: args[2], idx: args[1] };
  });
  wrapHook('afterStatusTick', function (args) {
    return { hook: 'after_status_tick', side: args[2], idx: args[1] };
  });
  wrapHook('sweepKOs', function () {
    return { hook: 'sweep_kos' };
  });

  var result = runBattle(
    playerTeam,
    enemyTeam,
    state.items,
    [],
    null,
    battleConfig,
    state.passives,
  );

  var statusEvents = [];
  var pendingStatusFaints = {};
  var statusFaintKeys = {};
  for (var event of result.detailedLog) {
    if (event.type === 'status_tick') {
      statusEvents.push({
        type: 'status_tick',
        side: event.side,
        idx: event.idx,
        status: event.status,
        hp_change: event.hpChange,
        hp_after: event.hpAfter,
      });
      if (event.hpAfter <= 0) pendingStatusFaints[event.side + ':' + event.idx] = true;
    } else if (
      event.type === 'effect' &&
      typeof event.reason === 'string' &&
      event.reason.indexOf('Pecha Berry:') === 0
    ) {
      statusEvents.push({
        type: 'poison_drain',
        side: event.side,
        idx: event.idx,
        hp_change: event.hpChange,
        hp_after: event.hpAfter,
      });
    } else if (event.type === 'faint') {
      var key = event.side + ':' + event.idx;
      if (pendingStatusFaints[key]) {
        statusEvents.push({ type: 'faint', side: event.side, idx: event.idx });
        statusFaintKeys[key] = true;
        delete pendingStatusFaints[key];
      }
    }
  }
  hookTrace = hookTrace.filter(function (event) {
    if (event.hook === 'on_ko') {
      return !!statusFaintKeys[event.fainted_side + ':' + event.fainted_idx];
    }
    if (event.hook === 'on_faint') {
      return !!statusFaintKeys[event.side + ':' + event.idx];
    }
    return true;
  });
  var rounds = globalThis.__ROUND_COUNT__ || 0;

  function normalizeMon(m) {
    return {
      species_id: m.speciesId,
      level: m.level,
      current_hp: m.currentHp,
      max_hp: m.maxHp,
      status: m.status || null,
      burned: !!m._burned,
      paralyzed: !!m._paralyzed,
      poison_stacks: m._poisonStacks || 0,
      stages: {
        atk: (m.stages && m.stages.atk) || 0,
        def: (m.stages && m.stages.def) || 0,
        speed: (m.stages && m.stages.speed) || 0,
        special: (m.stages && m.stages.special) || 0,
        spdef: (m.stages && m.stages.spdef) || 0,
      },
    };
  }

  globalThis.__RESULT__ = JSON.stringify({
    player_won: result.playerWon,
    player_team: result.pTeam.map(normalizeMon),
    enemy_team: result.eTeam.map(normalizeMon),
    player_participants: Array.from(result.playerParticipants).sort(function (a, b) { return a - b; }),
    rounds: rounds,
    rng_draws: rngDraws,
    final_rng_seed: getRngSeed(),
    status_events: statusEvents,
    hook_trace: hookTrace,
    event_count: result.detailedLog.length,
  });
})();
`;

const sandbox = { console };
sandbox.window = sandbox;
sandbox.location = { hostname: 'localhost' };
sandbox.setTimeout = () => 0;
sandbox.setInterval = () => 0;
sandbox.clearTimeout = () => {};
sandbox.clearInterval = () => {};

// `scan-toplevel-danger.js` (AST-based, not textual) found 6 real top-level
// `document["addEventListener"|"getElementById"|"documentElement"|...]`
// references (bundle.deobfuscated.js ~63649-63818: hover-tooltip wiring and
// a fullscreen-button visibility IIFE) -- unconditional at module-load time,
// unlike everything else in the prefix which is gated behind a function call
// the oracle never makes. None of it is reachable from `runBattle`/
// `buildGen3AbilityConfig`/`buildTraitsConfig`/`mergeBattleConfigs` (pure UI
// wiring), so a fully inert stub -- every property access and every call
// returns itself, nothing throws, nothing touches a real DOM (there isn't
// one in Node) -- satisfies it without altering any battle-math code path.
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
sandbox.document = makeInertDomStub();
sandbox.__FIXTURE__ = fixture;

vm.createContext(sandbox);
vm.runInContext(DECODER_STUBS + instrumentedPrefix + '\n' + DRIVER, sandbox, { timeout: 30000 });

if (sandbox.__RESULT__ === undefined) {
  console.error('oracle produced no result (see stderr above for a thrown error)');
  process.exit(1);
}
process.stdout.write(sandbox.__RESULT__);
