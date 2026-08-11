;(async function () {
  var SC = globalThis.__SCENARIO__;
  var OUT = { checkpoints: [], notes: [], rng_draws_total: 0 };
  var seq = 0;

  // ======================= deterministic timers =======================
  // Installed AFTER the prefix has loaded (the load-time no-op stubs are what
  // keep the obfuscator's anti-tamper setTimeout/setInterval inert). Delays
  // are ignored and callbacks run in FIFO order: nothing in a Story/Nuzlocke
  // route depends on real elapsed time, and a virtual queue keeps repeated
  // runs byte-identical.
  var timerQueue = [];
  var timerSeq = 0;
  setTimeout = function (fn) { timerQueue.push({ fn: fn, seq: timerSeq++ }); return timerSeq; };
  setInterval = function () { return 0; };
  clearTimeout = function () {};
  clearInterval = function () {};
  requestAnimationFrame = function (fn) { return setTimeout(fn, 0); };
  cancelAnimationFrame = function () {};

  // Turn the microtask queue and then run every queued timer, repeatedly,
  // until both are empty. Bounded so a runaway source loop fails loudly.
  async function pump() {
    for (var round = 0; round < 5000; round++) {
      for (var i = 0; i < 40; i++) await Promise.resolve();
      if (!timerQueue.length) return;
      var due = timerQueue;
      timerQueue = [];
      for (var t of due) { try { t.fn(); } catch (e) { OUT.notes.push('timer threw: ' + (e && e.message)); } }
    }
    throw new Error('pump did not quiesce after 5000 rounds');
  }

  // ======================= persistence stubs =======================
  // A brand-new account: every read misses. See this file's header for why
  // this can only be installed now and not before the prefix ran.
  var _store = new Map();
  globalThis.localStorage = {
    getItem: function (k) { return _store.has(k) ? _store.get(k) : null; },
    setItem: function (k, v) { _store.set(k, String(v)); },
    removeItem: function (k) { _store.delete(k); },
    clear: function () { _store.clear(); },
    key: function () { return null; },
    length: 0,
  };
  // `saveRun` is the only persistence call stubbed by name: it serialises the
  // whole run to localStorage on every node click and has no gameplay effect.
  // `captureMapStart`, `isSpeciesOwned`, `ownershipBadges`,
  // `loadPersistentBuffs`, `loadBuffsIntoPokemon`, `getEvoLineRoot`,
  // `markPokedexCaught`, `recordUsedStarter`, ... are all REAL here (the
  // prefix reaches them) and correctly behave as a brand-new account against
  // the empty store above.
  saveRun = function () {};
  // `recordMonOrigin` is deliberately NOT stubbed. It looks like persistence
  // from its name, and the first M3 session stubbed it as such -- but its body
  // (bundle.deobfuscated.js:79047-79063) sets `state.usedBallCatch` /
  // `state.gotViaQuestion`, which are RUN state this schema compares, and only
  // then calls `incrementStoryCounter` (52099-52112), whose sole side effects
  // are a `localStorage.setItem` and a `typeof`-guarded achievement toast --
  // both already absorbed by the in-memory store and the inert DOM. Stubbing
  // it SUPPRESSED gameplay state, which is the mirror of the "no stub may
  // generate gameplay state" rule and just as wrong. Found by the M3.1 routes:
  // they are the first to ACCEPT a catch, and `usedBallCatch` then diverged on
  // 52 of 59 checkpoints.

  // ======================= presentation stubs =======================
  // `showScreen` is the source's own screen switch. Recording its argument is
  // how the driver learns which suspended-continuation the player is sitting
  // on -- it is an observation of a real source call, not a model of one.
  var currentScreen = null;
  var screenLog = [];
  // Ordered list of the Pokemon objects the source passed to
  // `renderPokemonCard` since the current screen was raised. `showScreen` is
  // the source's OWN first statement in every screen builder that renders
  // cards (`showStarterSelect` 75569, `doCatchNode` 78428, `showSwapScreen`
  // 79143), so clearing here scopes the log to exactly one screen build.
  // Only the STARTER screen reads it -- the catch/item/swap offers are read
  // from real run state instead (see `pendingState`). See M3.3b workstream 3.
  var cardRenderLog = [];
  showScreen = function (id) { currentScreen = id; screenLog.push(id); cardRenderLog = []; };

  showMapScreen = function () { showScreen('map-screen'); };
  renderMap = function () {};
  renderBattleField = function () {};
  renderTeamBar = function () {};
  renderItemBadges = function () {};
  renderTrainerIcons = function () {};
  // The source calls this once per offered card, with the very object that
  // card's click listener closes over (`showStarterSelect` 76176-76186:
  // `renderPokemonCard(BIv, ...)` then `addEventListener('click', () =>
  // selectStarter(BIv))`). Recording the argument is an observation of a real
  // source call, not a model of one, and it happens at BUILD time -- strictly
  // before any callback can be invoked.
  renderPokemonCard = function (mon) { cardRenderLog.push(mon); return '<div class="poke-card"></div>'; };
  renderTraitPreview = function () { return ''; };
  renderTraitDeltaRows = function () { return ''; };
  renderBattleTraitBars = function () {};
  clearBattleTraitBars = function () {};
  animateBattleVisually = async function () {};
  animateLevelUp = async function () {};
  showToast = function () {};
  showMapNotification = function () {};
  showElitePrepScreen = async function () {};
  makeTraitOverlay = function () { return null; };
  showTeamHoverCard = function () {};
  hideTeamHoverCard = function () {};
  makeMaxedStarsEl = function () { return null; };
  t = (typeof t === 'function') ? t : function (k) { return k; };

  // `showTrainerSelect` is the boy/girl avatar picker. `state.trainer` only
  // feeds renderTrainerIcons, and startNewRun itself defaults it to "boy"
  // (bundle.deobfuscated.js:75466), so the stub picks the same default rather
  // than inventing one.
  showTrainerSelect = async function () { state.trainer = state.trainer || 'boy'; await showStarterSelect(); };

  // End-of-run screen + account-level Hall-of-Fame/stat recording. Nothing
  // gameplay-bearing happens after it; the run is over.
  var gameOverSeen = false;
  showGameOver = async function () { gameOverSeen = true; showScreen('gameover-screen'); };

  // ======================= network guard =======================
  // The oracle runs strictly offline. `fetch` is installed as a recording
  // rejector rather than left undefined so that (a) every attempted URL is
  // captured for compare.py to judge, and (b) the source's own try/catch
  // fallbacks run exactly as they do in a real browser that is offline --
  // which is the behavior the Python port models.
  //
  // Two URL classes are expected on a Story/Nuzlocke route and are
  // allow-listed by compare.py:
  //   * "data/pokedex.json" - never actually requested, because the bundle
  //     sets `window.__POKEDEX__` itself at bundle.deobfuscated.js:38772 and
  //     `loadStaticPokedex` (47901-47919) prefers that offline path.
  //   * ".../api/v2/pokemon?limit=2000" - `fetchSpeciesList` (47955-47968).
  //     On failure it warns and returns null, and `getSpeciesPool`'s callers
  //     then take the FALLBACK_SPECIES_POOL branch. That fallback branch is
  //     precisely what the Python port implements (pokelike/data/
  //     fallback_species_pool.json), so offline is the *modelled* path here,
  //     not a degradation of it.
  // Anything else is an unexpected network dependency and fails the run.
  var networkAttempts = [];
  fetch = function (url) {
    networkAttempts.push(String(url));
    return Promise.reject(new Error('route-oracle: network disabled (' + url + ')'));
  };
  XMLHttpRequest = function () { throw new Error('route-oracle: XMLHttpRequest disabled'); };

  // ======================= RNG instrumentation =======================
  var rngDraws = 0;
  (function () {
    var sourceRng = rng;
    rng = function countedRouteOracleRng() { rngDraws++; return sourceRng(); };
  })();

  // ======================= battle observation =======================
  // Reuses tools/battle-oracle/run-fixture.js's normalization verbatim where
  // it is source-equivalent: winner, completed round count, RNG draws, final
  // per-combatant state, participant set, and the ordered status-tick /
  // poison-drain / status-faint stream derived from runBattle's own
  // detailedLog.
  //
  // What is deliberately NOT compared, and why: the Python port's
  // `battle_loop.BattleResult` exposes `status_events` and `hook_trace` only
  // -- narrow oracle instrumentation -- and has no full per-turn event log at
  // all. There is therefore no Python counterpart to compare the source's
  // complete `detailedLog` against. `event_count` is carried as a DIAGNOSTIC
  // (recorded, never compared), and the absence of a full Python battle event
  // stream is recorded as a schema limitation in SCHEMA.md rather than papered
  // over with a fabricated field. Closing it is renderer-track (R4) work.
  var battles = [];
  (function () {
    var realRunBattle = runBattle;
    runBattle = function () {
      var drawsBefore = rngDraws;
      globalThis.__ROUND_COUNT__ = 0;
      globalThis.__ROUND_MARKS__ = [];
      var res = realRunBattle.apply(this, arguments);
      var derived = deriveStatusEvents(res.detailedLog || []);
      battles.push({
        player_won: !!res.playerWon,
        rounds: globalThis.__ROUND_COUNT__ || 0,
        rng_draws: rngDraws - drawsBefore,
        player_team: (res.pTeam || []).map(normalizeMon),
        enemy_team: (res.eTeam || []).map(normalizeMon),
        player_participants: Array.from(res.playerParticipants || []).sort(function (a, b) { return a - b; }),
        status_events: derived,
        turns: deriveTurns(res.detailedLog || [], globalThis.__ROUND_MARKS__ || []),
        __diagnostic_event_count: (res.detailedLog || []).length,
      });
      return res;
    };
  })();

  // ---- ordered per-turn battle events (M3.3b workstream 5) ---------------
  // The source's `detailedLog` is one flat stream for the whole battle. The
  // round-boundary marks installed by run-scenario.js (see its header) say
  // where each round's slice begins, so the projection below is a real
  // partition of the source's own log -- not a reconstruction from final
  // state, and not a post-hoc synthesis.
  //
  // The compared FAMILY is `type === "attack"` and nothing else. Every source
  // attack site emits that one shape (the ordinary hit at 55979-55995, the
  // `noDamage` "nothing happened" hit at 55786-55800, and both extra-attack
  // hits at 56240-56255 / 56322-56338, which additionally set
  // `isExtraAttack`). Presentation is excluded: the log's own prose lines
  // live on `res.log`, not here, and `attackerName`/`targetName` are dropped
  // because `side` + index already identify the combatant exactly and the
  // teams themselves are compared field by field in the same checkpoint.
  function normalizeAttackEvent(e) {
    return {
      type: 'attack',
      side: e.side === undefined ? null : e.side,
      attacker_idx: num(e.attackerIdx),
      target_side: e.targetSide === undefined ? null : e.targetSide,
      target_idx: num(e.targetIdx),
      move_name: e.moveName === undefined ? null : e.moveName,
      move_type: e.moveType === undefined ? null : e.moveType,
      damage: num(e.damage),
      type_eff: num(e.typeEff),
      crit: !!e.crit,
      is_special: !!e.isSpecial,
      attacker_hp_after: num(e.attackerHpAfter),
      target_hp_after: num(e.targetHpAfter),
      extra_attack: !!e.isExtraAttack,
    };
  }

  function deriveTurns(log, marks) {
    // Nothing of the compared family may precede the first round: the events
    // before `marks[0]` are `runBattle`'s pre-loop setup (send_out at 55232,
    // trait/ability triggers at 55274+). Asserted rather than assumed, so a
    // source change that moves an attack out of the loop fails loudly instead
    // of silently dropping it from the projection.
    var firstRound = marks.length ? marks[0] : log.length;
    for (var pre = 0; pre < firstRound; pre++) {
      if (log[pre] && log[pre].type === 'attack') {
        throw new Error('attack event at log index ' + pre + ' precedes the first round boundary');
      }
    }
    var turns = [];
    for (var r = 0; r < marks.length; r++) {
      var end = (r + 1 < marks.length) ? marks[r + 1] : log.length;
      var events = [];
      for (var i = marks[r]; i < end; i++) {
        if (log[i] && log[i].type === 'attack') events.push(normalizeAttackEvent(log[i]));
      }
      turns.push({ turn: r + 1, events: events });
    }
    return turns;
  }

  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }

  // Verbatim port of tools/battle-oracle/run-fixture.js's status-event
  // derivation, so both oracles read the source's log the same way.
  function deriveStatusEvents(log) {
    var statusEvents = [];
    var pendingStatusFaints = {};
    for (var event of log) {
      if (event.type === 'status_tick') {
        statusEvents.push({
          type: 'status_tick', side: event.side, idx: num(event.idx),
          status: event.status, hp_change: num(event.hpChange), hp_after: num(event.hpAfter),
        });
        if (event.hpAfter <= 0) pendingStatusFaints[event.side + ':' + event.idx] = true;
      } else if (
        event.type === 'effect' && typeof event.reason === 'string' &&
        event.reason.indexOf('Pecha Berry:') === 0
      ) {
        statusEvents.push({
          type: 'poison_drain', side: event.side, idx: num(event.idx),
          hp_change: num(event.hpChange), hp_after: num(event.hpAfter),
        });
      } else if (event.type === 'faint') {
        var key = event.side + ':' + event.idx;
        if (pendingStatusFaints[key]) {
          statusEvents.push({ type: 'faint', side: event.side, idx: num(event.idx) });
          delete pendingStatusFaints[key];
        }
      }
    }
    return statusEvents;
  }

  function normalizeStages(s) {
    s = s || {};
    return {
      atk: num(s.atk) || 0, def: num(s.def) || 0, speed: num(s.speed) || 0,
      special: num(s.special) || 0, spdef: num(s.spdef) || 0,
    };
  }

  function normalizeMon(m) {
    if (!m) return null;
    return {
      species_id: num(m.speciesId),
      // `form_id` distinguishes alternate forms that share a dex number
      // (Origin Giratina is species 487 with a distinct identity).
      form_id: (m.formId === undefined ? (m.form === undefined ? null : m.form) : m.formId) || null,
      name: m.name === undefined ? null : m.name,
      level: num(m.level),
      max_hp: num(m.maxHp),
      current_hp: num(m.currentHp),
      types: (m.types || []).slice(),
      move_tier: num(m.moveTier),
      held_item: m.heldItem ? (m.heldItem.id === undefined ? null : m.heldItem.id) : null,
      is_shiny: !!m.isShiny,
      status: m.status === undefined ? null : (m.status || null),
      burned: !!m._burned,
      paralyzed: !!m._paralyzed,
      poison_stacks: num(m._poisonStacks) || 0,
      base_stats: m.baseStats ? {
        hp: num(m.baseStats.hp), atk: num(m.baseStats.atk), def: num(m.baseStats.def),
        speed: num(m.baseStats.speed), special: num(m.baseStats.special), spdef: num(m.baseStats.spdef),
      } : null,
      stat_buffs: normalizeStatBuffs(m.statBuffs),
    };
  }

  function normalizeStatBuffs(b) {
    b = b || {};
    var out = {};
    for (var k of ['hp', 'atk', 'def', 'speed', 'special', 'spdef']) {
      if (b[k]) out[k] = num(b[k]);
    }
    return out;
  }

  // ======================= pending-choice observation =======================
  // `showSwapScreen(incoming, node)` (bundle.deobfuscated.js:79141) is the ONE
  // choice screen whose offered object never lands in `state` -- the incoming
  // Pokemon lives only in the closure the accept/release listeners capture
  // (79173-79201 / 79202-79246). Wrapping the source function records its own
  // arguments BEFORE delegating, so the identity is captured strictly before
  // any listener can resolve it. The wrapper adds no behavior: it forwards
  // `arguments` unchanged and returns the real result.
  var swapOffer = null;
  (function () {
    var realShowSwapScreen = showSwapScreen;
    showSwapScreen = function (incoming, node) {
      swapOffer = { incoming: incoming, node_id: node && node.id !== undefined ? node.id : null };
      return realShowSwapScreen.apply(this, arguments);
    };
  })();

  // `showBranchingChoice(mon, branches)` (bundle.deobfuscated.js:70560-70613)
  // is one of the three overlays that never call `showScreen` -- its
  // `#eevee-choice-overlay` is toggled visible with `style.display = "flex"`
  // and the branch cards it offers are hypothetical evolution targets, not
  // objects state itself stores anywhere reachable after the call returns.
  // Wrapping it, exactly like `showSwapScreen` above, captures the real
  // arguments the source is about to act on (the evolving mon plus its
  // `BRANCHING_EVOLUTIONS` entry) strictly before any card can be clicked.
  var evoOffer = null;
  (function () {
    var realShowBranchingChoice = showBranchingChoice;
    showBranchingChoice = function (mon, branches) {
      evoOffer = { mon: mon, branches: branches };
      return realShowBranchingChoice.apply(this, arguments);
    };
  })();

  // ======================= showScreen-less overlay detection =======================
  // `openItemEquipModal` (79442), `doMoveTutorNode` (80464-80563, which reuses
  // the SAME `#item-equip-modal` id/class as `openItemEquipModal`) and
  // `showTeamPickerModal` (76845) never call `showScreen`, so `currentScreen`
  // cannot tell them apart or tell they exist at all -- this must detect them
  // DIRECTLY from the DOM the source itself built, never infer from
  // `currentScreen`.
  //
  // Two real properties of run-scenario.js's minimal DOM stub matter here and
  // are NOT what a real browser DOM would do (see its own M4 repair-item-1
  // comments): `document.getElementById(id)` auto-vivifies and returns a
  // TRUTHY stub for ANY id, seen or not, so truthiness alone can never mean
  // "this overlay is really open"; and `.remove()` only unlinks an element
  // from its parent's children, it does not erase the element or its stale
  // `innerHTML`. The real presence signal is therefore "is the fixed-id
  // element CURRENTLY a child of `document.body`" -- exactly the state
  // `appendChild`/`remove` already track correctly, reused here rather than
  // invented. `doMoveTutorNode` and `openItemEquipModal` share that one id,
  // so they are told apart by real, always-present content
  // (`openItemEquipModal`'s template literally contains `btn-skip-tutor`
  // only when it is NOT the tutor modal) rather than by querying for a
  // specific button, since `el.querySelector('#some-id')` unconditionally
  // fabricates a truthy stand-in regardless of whether that id's markup is
  // really there.
  function isMounted(el) {
    return !!el && document.body.__children.indexOf(el) !== -1;
  }

  function detectOverlay() {
    var equipModal = document.getElementById('item-equip-modal');
    if (isMounted(equipModal)) {
      if (equipModal.innerHTML.indexOf('btn-skip-tutor') !== -1) {
        var tutorButtons = Array.prototype.slice.call(equipModal.querySelectorAll('button[data-tutor]'));
        return { kind: 'move_tutor', el: equipModal, buttons: tutorButtons, skip: equipModal.querySelector('#btn-skip-tutor') };
      }
      var equipButtons = Array.prototype.slice.call(equipModal.querySelectorAll('button[data-idx]'));
      return { kind: 'item_equip', el: equipModal, buttons: equipButtons, skip: equipModal.querySelector('#btn-equip-to-bag') };
    }
    // `#eevee-choice-overlay` is a PRE-EXISTING element toggled visible, not
    // created on demand -- `display !== "flex"` means it is inert, exactly
    // the source's own visibility test, so no `isMounted` check applies here.
    var evoOverlay = document.getElementById('eevee-choice-overlay');
    if (evoOverlay && evoOverlay.style && evoOverlay.style.display === 'flex') {
      var evoContainer = document.getElementById('eevee-choices');
      var evoButtons = evoContainer ? Array.prototype.slice.call(evoContainer.__children || []) : [];
      return { kind: 'branching_evolution', el: evoOverlay, buttons: evoButtons, skip: null };
    }
    var pickerModal = document.getElementById('submap-pick-modal');
    if (isMounted(pickerModal)) {
      var pickButtons = Array.prototype.slice.call(pickerModal.querySelectorAll('button[data-idx]'));
      return { kind: 'team_pick', el: pickerModal, buttons: pickButtons, skip: null };
    }
    return null;
  }

  // ======================= checkpoint construction =======================
  function normalizeNode(n) {
    var out = {
      id: n.id, type: n.type, layer: num(n.layer), col: num(n.col),
      visited: !!n.visited, accessible: !!n.accessible, revealed: !!n.revealed,
    };
    // Node-carried gameplay identity that map generation decided. Excludes
    // `trainerSprite` (presentation) but keeps everything the engine reads.
    if (n.mapIndex !== undefined) out.map_index = num(n.mapIndex);
    if (n.legendarySpeciesId !== undefined) out.legendary_species_id = num(n.legendarySpeciesId);
    if (n.subKind !== undefined) out.sub_kind = n.subKind;
    if (n.rewardKind !== undefined) out.reward_kind = n.rewardKind;
    if (n.kind !== undefined) out.kind = n.kind;
    if (n.wildBoss !== undefined) out.wild_boss = !!n.wildBoss;
    if (n.bossTeam !== undefined) {
      out.boss_team = (n.bossTeam || []).map(function (b) {
        return { id: (b.id === undefined ? null : b.id), level: num(b.level) };
      });
    }
    if (n.trainerKey !== undefined) out.trainer_key = n.trainerKey;
    // A REWARD node's `.reward` (bundle.deobfuscated.js:53591-53611, e.g.
    // `B2Q["reward"] = "skip"`) is a plain STRING submap-reward-table id
    // ("sacrifice", "stat10", "fossil", "skip", ...), never an object -- the
    // old `typeof n.reward === 'object'` guard here was checking a shape
    // that never occurs in the real source, so `out.reward` was silently
    // NEVER populated on this side, found while deriving the M4
    // `sacrifice`/`stat10` route-oracle scenarios (`coverage.py`'s
    // `sacrifice_reward_resolved`/`stat10_reward_resolved` need the real id
    // to tell the two reward kinds apart).
    if (n.reward !== undefined) {
      out.reward = { kind: n.reward || null };
    }
    return out;
  }

  function normalizeMap(m) {
    if (!m) return null;
    var ids = Object.keys(m.nodes || {}).slice().sort();
    return {
      index: num(m.mapIndex),
      is_sub_map: m.isSubMap === undefined ? null : (m.isSubMap || null),
      nodes: ids.map(function (id) { return normalizeNode(m.nodes[id]); }),
      edges: (m.edges || []).map(function (e) { return [e.from, e.to]; }),
    };
  }

  function normalizeSubMapReturn(r) {
    if (!r) return null;
    return {
      kind: r.kind === undefined ? null : r.kind,
      map_index: num(r.mapIndex),
      node_id: r.nodeId === undefined ? null : r.nodeId,
      has_map: !!r.map,
      map_node_count: r.map && r.map.nodes ? Object.keys(r.map.nodes).length : null,
      // The COMPLETE saved parent map, normalized exactly the way the live
      // map is (`normalizeMap`), not a flags-only summary.
      //
      // M3.3: this used to be `map_flags` -- an {id, visited, accessible}
      // list. That was too weak for what `exact_parent_return` claims to
      // prove: with only those three fields, changing a restored node's TYPE,
      // its `revealed` flag, its gameplay extras, the map index, or an edge's
      // endpoint or ORDER all left the tag earned (6/6 such mutations were
      // demonstrated invisible before this change). Saving the full topology
      // is what lets coverage.py compare the returned map against the exact
      // `advanceFromNode(parent)` transformation and nothing looser.
      map_topology: normalizeMap(r.map),
    };
  }

  // ---- canonical pending-choice OPTION IDENTITY (M3.3b workstream 3) ------
  // One shape for every option on both runtimes, so a comparison is possible
  // at all. Every field is a semantic property of the offered object: no DOM
  // text, no object address, no closure identity, no opaque hash.
  //
  //   role       which affordance this is ("starter" | "catch" | "item" |
  //              "swap_accept" | "swap_release" | "team")
  //   kind       "mon" | "item"
  //   species_id/form_id/name   stable species identity, present even when the
  //              runtime has no instance to go with it
  //   item_id    the item's own id (items have no species)
  //   slot       team position, for options that name an EXISTING team member
  //   instance   the full normalized instance when the runtime built one, or
  //              null when it did not. Reported rather than omitted so an
  //              absent instance is a COMPARED difference (same discipline as
  //              `counters.any_fainted`) instead of a silent exclusion.
  function monOption(role, mon, slot) {
    var norm = normalizeMon(mon);
    return {
      role: role,
      kind: 'mon',
      species_id: norm ? norm.species_id : null,
      form_id: norm ? norm.form_id : null,
      name: norm ? norm.name : null,
      item_id: null,
      slot: slot === undefined ? null : slot,
      instance: norm,
    };
  }

  function itemOption(role, item) {
    return {
      role: role,
      kind: 'item',
      species_id: null,
      form_id: null,
      name: item && item.name !== undefined ? item.name : null,
      item_id: item && item.id !== undefined ? item.id : null,
      slot: null,
      instance: null,
    };
  }

  // `doItemNode` resolves an offered id back to its pool entry with exactly
  // this lookup (bundle.deobfuscated.js:79348-79358); reusing the source's own
  // two pools keeps the id -> entry mapping the source's, not the driver's.
  function resolveOfferedItem(id) {
    for (var pool of [ITEM_POOL, USABLE_ITEM_POOL]) {
      for (var entry of pool || []) {
        if (entry && entry.id === id) return entry;
      }
    }
    if (typeof MEGA_STONE_BY_ID !== 'undefined' && MEGA_STONE_BY_ID[id]) return makeMegaStoneItem(MEGA_STONE_BY_ID[id]);
    return { id: id, name: null };
  }

  // The source has no `PendingChoice`; the player's pending decision is
  // implied by which screen is up and which affordances it built. This
  // reconstructs the same (kind, cardinality, optional, ORDERED OPTION
  // IDENTITY) record the Python port reports from `RunState.pending`, from
  // the source's own run state and its own build-time calls, so the two are
  // genuinely compared rather than one side reporting nothing.
  //
  // Every option list below is read from state the source itself owns and
  // orders, at the same order the source appends the cards:
  //   starter  `renderPokemonCard`'s per-card argument   (76176-76186)
  //   catch    `state.savedCatch.instances`              (78765, 78949-78951)
  //   item     `state.itemOffer.ids`                     (79372, 79377-79429)
  //   swap     `showSwapScreen`'s own arguments + `state.team` (79141-79246)
  // Screens with no pending decision report null on both sides.
  function pendingState() {
    function childCount(id) {
      var el = document.getElementById(id);
      return ((el && el.__children) || []).length;
    }
    // ---- showScreen-less overlays (M4 repair item 1) ----------------------
    // Checked BEFORE `currentScreen`, and independent of it: none of these
    // three ever call `showScreen`, so `currentScreen` still names whatever
    // screen was up before the overlay was raised and can never be used to
    // detect them.
    var overlay = detectOverlay();
    if (overlay) {
      if (overlay.kind === 'move_tutor') {
        // `doMoveTutorNode` (80464-80563) builds one `[data-tutor]` button
        // per NON-mastered team member, in team order, reading the real
        // member back off `state.team` by its own `data-tutor` index -- the
        // identity the driver must also read, never the row's rendered text.
        var tutorIdxs = overlay.buttons.map(function (b) { return parseInt(b.getAttribute('data-tutor'), 10); });
        return {
          phase: 'move_tutor_choice', optional: true, option_count: tutorIdxs.length,
          options: tutorIdxs.map(function (idx) { return monOption('move_tutor', state.team[idx], idx); }),
          context: null,
        };
      }
      if (overlay.kind === 'item_equip') {
        // `openItemEquipModal` (79442-79570), reached only as `doItemNode`'s
        // `fromBagIdx=-1, fromPokemonIdx=-1` call (79423-79429) -- the only
        // configuration an ordinary node visit produces -- builds exactly one
        // `[data-idx]` button per team member, in team order, plus
        // `#btn-equip-to-bag` as a real decline (see run_scenario.py's
        // matching `_resolve_item_equip_choice`).
        var equipIdxs = overlay.buttons.map(function (b) { return parseInt(b.getAttribute('data-idx'), 10); });
        if (equipIdxs.length !== state.team.length || equipIdxs.some(function (v, i) { return v !== i; })) {
          throw new Error('item-equip option capture mismatch: buttons ' + JSON.stringify(equipIdxs) +
            ' vs ' + state.team.length + ' team member(s)');
        }
        return {
          phase: 'item_equip_choice', optional: true, option_count: equipIdxs.length,
          options: state.team.map(function (m, i) { return monOption('item_equip', m, i); }),
          context: null,
        };
      }
      if (overlay.kind === 'branching_evolution') {
        // `showBranchingChoice` (70560-70613) never stashes its arguments on
        // `state`; the wrapper above captures them at call time, before any
        // card can be clicked. Options are the BRANCH TARGETS the source
        // offers, not team members, and there is no cancel -- picking one is
        // mandatory (`optional: false`), same as the source itself.
        if (!evoOffer) throw new Error('eevee-choice-overlay is visible but showBranchingChoice was never observed');
        if (evoOffer.branches.length !== overlay.buttons.length) {
          throw new Error('branching-evolution option capture mismatch: ' + evoOffer.branches.length +
            ' branch(es) vs ' + overlay.buttons.length + ' card(s)');
        }
        return {
          phase: 'evolution_choice', optional: false, option_count: evoOffer.branches.length,
          options: evoOffer.branches.map(function (b) {
            return {
              role: 'evolution_branch', kind: 'mon', species_id: b.into, form_id: null,
              name: b.name, item_id: null, slot: null, instance: null,
            };
          }),
          context: { evolving: monOption('evolving', evoOffer.mon) },
        };
      }
      if (overlay.kind === 'team_pick') {
        // `showTeamPickerModal` (76845-76884) -- the `sacrifice`/`stat10`
        // submap-reward branches. One `[data-idx]` button per team member, in
        // team order (76852-76866); no cancel, matching `optional: false`.
        var pickIdxs = overlay.buttons.map(function (b) { return parseInt(b.getAttribute('data-idx'), 10); });
        if (pickIdxs.length !== state.team.length || pickIdxs.some(function (v, i) { return v !== i; })) {
          throw new Error('team-picker option capture mismatch: buttons ' + JSON.stringify(pickIdxs) +
            ' vs ' + state.team.length + ' team member(s)');
        }
        return {
          phase: 'reward_team_pick', optional: false, option_count: pickIdxs.length,
          options: state.team.map(function (m, i) { return monOption('team_pick', m, i); }),
          context: null,
        };
      }
    }
    if (currentScreen === 'starter-screen') {
      // `showStarterSelect` appends one `.poke-card` per offered starter, in
      // the order it renders them (76175-76194). The card count is the
      // independent cross-check that the render log is exactly this screen's
      // options and nothing else's.
      var cards = childCount('starter-choices');
      if (cardRenderLog.length !== cards) {
        throw new Error(
          'starter option capture mismatch: ' + cardRenderLog.length +
          ' rendered card(s) vs ' + cards + ' appended card(s)');
      }
      return {
        phase: 'choose_starter', optional: false, option_count: cards,
        options: cardRenderLog.map(function (m) { return monOption('starter', m); }),
        context: null,
      };
    }
    if (currentScreen === 'swap-screen') {
      // With room the incoming card is the only accept affordance
      // (bundle.deobfuscated.js:79171-79201); full, it is one card per team
      // member in `state.team` order (79202-79246), and the click releases
      // `state.team[i]` for that same i. Cancel is always available ->
      // optional. `challengeNoReplace` with a full team builds no cards at
      // all (the `ip` guard at 79145/79202) -- mirrored exactly rather than
      // assumed unreachable.
      if (!swapOffer) throw new Error('swap-screen is up but showSwapScreen was never observed');
      var hasRoom = state.team.length < 0x6;
      var noReplace = !!state.challengeNoReplace && !hasRoom;
      var swapOptions = hasRoom
        ? [monOption('swap_accept', swapOffer.incoming)]
        : (noReplace ? [] : state.team.map(function (m, i) { return monOption('swap_release', m, i); }));
      var builtCards = hasRoom ? (document.querySelector('#swap-incoming .poke-card') ? 1 : 0) : childCount('swap-choices');
      if (swapOptions.length !== builtCards) {
        throw new Error(
          'swap option capture mismatch: ' + swapOptions.length + ' option(s) vs ' + builtCards + ' card(s)');
      }
      return {
        phase: 'swap_choice', optional: true, option_count: swapOptions.length,
        options: swapOptions,
        context: {
          incoming: monOption('incoming', swapOffer.incoming),
          team: state.team.map(function (m, i) { return monOption('team', m, i); }),
        },
      };
    }
    if (currentScreen === 'catch-screen') {
      // `doCatchNode` renders `#catch-choices` from `B2n` and pins that exact
      // array on `state.savedCatch.instances` (78765), replacing it in place
      // on a reroll (78896) -- so the saved array is the live, ordered offer.
      var saved = state.savedCatch;
      if (!saved || !Array.isArray(saved.instances)) {
        throw new Error('catch-screen is up but state.savedCatch.instances is absent');
      }
      var catchCards = childCount('catch-choices');
      if (saved.instances.length !== catchCards) {
        throw new Error(
          'catch option capture mismatch: ' + saved.instances.length + ' instance(s) vs ' + catchCards + ' card(s)');
      }
      return {
        phase: 'catch_choice', optional: true, option_count: catchCards,
        options: saved.instances.map(function (m) { return monOption('catch', m); }),
        context: null,
      };
    }
    if (currentScreen === 'item-screen') {
      // `doItemNode` pins the offered ids on `state.itemOffer.ids` in the same
      // order it appends the cards (79372, 79377-79429).
      var offer = state.itemOffer;
      if (!offer || !Array.isArray(offer.ids)) {
        throw new Error('item-screen is up but state.itemOffer.ids is absent');
      }
      var itemCards = childCount('item-choices');
      if (offer.ids.length !== itemCards) {
        throw new Error(
          'item option capture mismatch: ' + offer.ids.length + ' id(s) vs ' + itemCards + ' card(s)');
      }
      return {
        phase: 'item_choice', optional: true, option_count: itemCards,
        options: offer.ids.map(function (id) { return itemOption('item', resolveOfferedItem(id)); }),
        context: null,
      };
    }
    if (currentScreen === 'shiny-screen') {
      // `doShinyNode` (80872-80990) writes `#shiny-content` via raw
      // `innerHTML` (not `appendChild`), so -- exactly like the swap
      // screen's `#swap-incoming` cross-check above -- presence comes from
      // the SAME single-element `document.querySelector` the source itself
      // uses (80972), not the `.__children` tracker and not
      // `document.querySelectorAll`, which run-scenario.js's DOM stub always
      // returns `[]` from at the `document` level. The single card is
      // already captured in `cardRenderLog` by the existing
      // `renderPokemonCard` wrapper (80940), since `showScreen` cleared the
      // log immediately before the card was rendered -- there is never more
      // than one, so presence (0 or 1) is the whole cross-check.
      var shinyCards = document.querySelector('#shiny-content .poke-card') ? 1 : 0;
      if (cardRenderLog.length !== shinyCards) {
        throw new Error(
          'shiny option capture mismatch: ' + cardRenderLog.length +
          ' rendered card(s) vs ' + shinyCards + ' card(s)');
      }
      return {
        phase: 'shiny_choice', optional: true, option_count: shinyCards,
        options: cardRenderLog.map(function (m) { return monOption('shiny', m); }),
        context: null,
      };
    }
    if (currentScreen === 'trade-screen') {
      // `doTradeNode`'s ordinary (non-Endless2) path (80580-80638) builds one
      // `li.trade-member-row` per team member via `createElement`+
      // `appendChild` (80606-80630), so `childCount` (the `.__children`
      // tracker) applies, same as catch/item.
      var tradeRows = childCount('trade-team-list');
      if (tradeRows !== state.team.length) {
        throw new Error(
          'trade option capture mismatch: ' + tradeRows + ' row(s) vs ' + state.team.length + ' team member(s)');
      }
      return {
        phase: 'trade_choice', optional: true, option_count: tradeRows,
        options: state.team.map(function (m, i) { return monOption('trade', m, i); }),
        context: null,
      };
    }
    return null;
  }

  // ======================= live resume guards (M4.2) =======================
  // The three node-scoped records the source keeps so a run saved while
  // parked on a choice screen resumes onto the SAME offer rather than
  // re-rolling it: `state.savedQuestionResolve` (written 77332, read 77328),
  // `state.savedCatch` (written 78765, read 78441) and
  // `state.savedShinyNode` (written 80919, read 80887). They are read here
  // straight off the source's own run state -- nothing is reconstructed.
  //
  // They matter to a PARITY oracle because which of them an exit clears is
  // branch-specific, and clearing the wrong one (or failing to clear the
  // right one) is invisible in every other compared field: a shiny room
  // accept clears `savedShinyNode` and RETAINS `savedQuestionResolve`
  // (80962), while a catch room accept clears `savedCatch` AND
  // `savedQuestionResolve` and leaves `savedShinyNode` alone (79041-79042).
  //
  // Only deterministic semantic identity is projected -- the key each record
  // is guarded by, plus the offer it pins. `savedCatch.rerollPool` /
  // `.rerolled` / `.level` are Endless-and-reroll bookkeeping the Python
  // port does not model; the offer's `instances` already carry every level
  // and identity the comparison needs, in the source's own order.
  //
  // These are LIVE fields. What `saveRun()` last persisted is a different
  // fact and is not projected: `saveRun` is stubbed out here (see the
  // persistence-stubs section), and neither accept nor decline handler calls
  // it, so the last snapshot on a shiny accept still holds the PRE-accept
  // team and a non-null `savedShinyNode`. See SCHEMA.md.
  function resumeState() {
    var q = state.savedQuestionResolve;
    var c = state.savedCatch;
    var s = state.savedShinyNode;
    return {
      saved_question_resolve: q
        ? {
            key: q.key === undefined ? null : q.key,
            resolved_type: q.resolvedType === undefined ? null : q.resolvedType,
          }
        : null,
      saved_catch: c
        ? {
            key: c.nodeId === undefined ? null : c.nodeId,
            instances: (c.instances || []).map(function (m, i) { return monOption('saved_catch', m, i); }),
          }
        : null,
      saved_shiny_node: s
        ? { key: s.key === undefined ? null : s.key, species_id: num(s.speciesId) }
        : null,
    };
  }

  function checkpoint(kind, event) {
    var cp = {
      schema_version: SC.schema_version,
      scenario: SC.scenario,
      seq: seq++,
      kind: kind,
      event: event || {},
      mode: {
        nuzlocke: !!state.nuzlockeMode, gen2: !!state.gen2Mode,
        gen3: !!state.gen3Mode, gen4: !!state.gen4Mode,
      },
      seed: num(SC.seed),
      rng: { state: getRngSeed(), draws: rngDraws },
      screen: currentScreen,
      map: normalizeMap(state.map),
      current_map: num(state.currentMap),
      current_node: state.currentNode ? state.currentNode.id : null,
      in_sub_map: state.inSubMap === undefined ? null : (state.inSubMap || null),
      sub_map_return: normalizeSubMapReturn(state.subMapReturn),
      counters: {
        badges: num(state.badges) || 0,
        elite_index: num(state.eliteIndex) || 0,
        starter_species_id: num(state.starterSpeciesId),
        max_team_size: num(state.maxTeamSize) || 0,
        silver_beaten: num(state.silverBeaten) || 0,
        fought_admin: !!state.foughtAdmin,
        used_pokecenter: !!state.usedPokecenter,
        picked_up_item: !!state.pickedUpItem,
        used_tm: !!state.usedTM,
        used_ball_catch: !!state.usedBallCatch,
        got_via_question: !!state.gotViaQuestion,
        any_fainted: !!state.anyFainted,
        escaped_via_rope: !!state._escapedViaRope,
        distortion_worlds_entered: num(state.distortionWorldsEntered) || 0,
        distortion_legendary_claimed: !!state.distortionLegendaryClaimed,
        entered_sub_map: !!state.enteredSubMap,
      },
      team: (state.team || []).map(function (m, i) {
        var n = normalizeMon(m);
        n.slot = i;
        return n;
      }),
      items: (state.items || []).map(function (it) {
        return typeof it === 'string' ? it : (it && it.id !== undefined ? it.id : null);
      }),
      game_over: gameOverSeen,
      pending: pendingState(),
      resume_state: resumeState(),
    };
    OUT.checkpoints.push(cp);
    return cp;
  }

  // ======================= DOM bridge =======================
  // The driver never fabricates a decision: it invokes the click handler the
  // SOURCE attached, at the element the SOURCE attached it to.
  function clickEl(el) {
    if (!el) return false;
    var fired = false;
    if (typeof el.onclick === 'function') { el.onclick({ preventDefault: function () {} }); fired = true; }
    var ls = el.__listeners && el.__listeners.click;
    if (ls && ls.length) { for (var fn of ls.slice()) fn({ preventDefault: function () {} }); fired = true; }
    return fired;
  }

  // M6. Address a generated button by the VALUE of its data attribute rather
  // than by its position in the `querySelectorAll` result. The item-equip
  // overlay is the case that forces this: the row for the member the overlay
  // was opened from carries `data-unequip` instead of `data-idx`
  // (bundle.deobfuscated.js:79490-79495), so the `[data-idx]` list has a hole
  // in it and position != team index.
  function byDataValue(elements, attr, value) {
    var wanted = String(value);
    for (var el of elements || []) {
      if (el && el.dataset && String(el.dataset[attr]) === wanted) return el;
    }
    return null;
  }

  function firstClickable(el, depth) {
    if (!el || (depth || 0) > 6) return null;
    if ((el.__listeners && el.__listeners.click && el.__listeners.click.length) || typeof el.onclick === 'function') return el;
    for (var c of el.__children || []) {
      var hit = firstClickable(c, (depth || 0) + 1);
      if (hit) return hit;
    }
    return null;
  }

  function nthChoice(containerId, index) {
    var container = document.getElementById(containerId);
    var kids = (container && container.__children) || [];
    if (index < 0 || index >= kids.length) return null;
    return firstClickable(kids[index], 0);
  }

  // Run a source entry point that may SUSPEND on a screen the player has to
  // dismiss, and press only the dismissals that carry no decision.
  //
  // This cannot simply `await` the entry point: `doBattleNode` awaits
  // `runBattleScreen`, whose Promise is resolved by the battle screen's own
  // Continue button (bundle.deobfuscated.js:81384-81387 on the win branch,
  // 81427-81429 on the loss branch). Awaiting first would deadlock, so the
  // call is started, then the UI is driven while it is still in flight.
  // Continue is the only auto-pressed control -- it chooses nothing. Every
  // real decision (which card to take, whether to decline) stays an explicit
  // scenario action.
  async function drive(startFn) {
    var done = false;
    var err = null;
    Promise.resolve().then(startFn).then(
      function () { done = true; },
      function (e) { err = e; done = true; }
    );
    for (var guard = 0; guard < 64; guard++) {
      await pump();
      if (done) break;
      if (currentScreen === 'battle-screen' && clickEl(document.getElementById('btn-continue-battle'))) continue;
      break; // genuinely suspended on a player choice
    }
    await pump();
    if (err) throw err;
    // The chain can also finish while the battle screen is still parked
    // (e.g. a loss that resolves through its own callback).
    for (var g2 = 0; g2 < 8 && currentScreen === 'battle-screen'; g2++) {
      if (!clickEl(document.getElementById('btn-continue-battle'))) break;
      await pump();
    }
    return done;
  }

  // ======================= run =======================
  try {
    // Deterministic entropy for startNewRun's own seed expression
    // (bundle.deobfuscated.js:75455: (Date.now() ^ ((Math.random()*2**32)|0)) >>> 0).
    // Pinning the two entropy sources -- NOT rewriting the expression -- makes
    // the source compute exactly SC.seed for itself.
    Date.now = function () { return SC.seed >>> 0; };
    Math.random = function () { return 0; };

    await startNewRun(!!SC.mode.nuzlocke, !!SC.mode.gen2, !!SC.mode.gen3, !!SC.mode.gen4);
    await pump();
    if ((state.runSeed >>> 0) !== (SC.seed >>> 0)) {
      throw new Error('startNewRun produced runSeed ' + state.runSeed + ', expected ' + SC.seed);
    }
    checkpoint('run_init', {});

    // Starter selection. showStarterSelect() has ALREADY run (via the
    // showTrainerSelect stub) and has already made its per-starter
    // rollShiny()/createInstance() calls -- those RNG draws are real source
    // behavior and are deliberately not stubbed away. Choosing = clicking the
    // card the source built.
    checkpoint('starter_offered', { screen: currentScreen });

    // ---- optional RNG alignment instrument (see SCHEMA.md) ----------------
    // NOT a behavior change and NOT a repair: both runners install the same
    // raw Stream-B state here, at the same point in the route, using the
    // source's own seedRng(). It exists because the starter OFFER itself
    // already diverges (the source calls rollShiny() once per offered
    // starter, bundle.deobfuscated.js:76175-76194 -> 3 draws; the port's
    // ChooseStarter handler makes 0), and without re-alignment every later
    // RNG-dependent decision is merely a downstream echo of that one
    // difference, which would hide any independent divergence behind it.
    // The pre-alignment divergence is still recorded, in the run_init and
    // starter_offered checkpoints, and is still compared.
    if (SC.align_rng_after_starter_offer !== undefined && SC.align_rng_after_starter_offer !== null) {
      seedRng(SC.align_rng_after_starter_offer >>> 0);
      checkpoint('rng_aligned', { to: SC.align_rng_after_starter_offer >>> 0 });
    }

    var starterIdx = SC.starter_index;
    var starterEl = nthChoice('starter-choices', starterIdx);
    if (!starterEl) throw new Error('no starter card at index ' + starterIdx);
    clickEl(starterEl);
    await pump();
    checkpoint('starter_selected', { starter_index: starterIdx });

    for (var step = 0; step < SC.actions.length; step++) {
      var act = SC.actions[step];
      if (act.kind === 'visit') {
        var node = state.map.nodes[act.node];
        if (!node) throw new Error('step ' + step + ': no node ' + act.node);
        checkpoint('node_pre', { node: act.node, node_type: node.type, step: step });
        var battlesBefore = battles.length;
        await drive(function () { return onNodeClick(node); });
        for (var b = battlesBefore; b < battles.length; b++) {
          checkpoint('battle', { node: act.node, battle_index: b, battle: battles[b] });
        }
        checkpoint('node_post', { node: act.node, step: step });
      } else if (act.kind === 'choice') {
        checkpoint('choice_pre', { screen: currentScreen, index: act.index === undefined ? null : act.index, step: step });
        var idx = (act.index === undefined || act.index === null) ? null : act.index;
        var ok = false;
        var ov = detectOverlay();
        if (ov && ov.kind === 'move_tutor') {
          // #btn-skip-tutor (80531-80534) vs the Nth [data-tutor] button.
          ok = idx === null ? clickEl(ov.skip) : clickEl(ov.buttons[idx]);
        } else if (ov && ov.kind === 'item_equip') {
          // #btn-equip-to-bag is the real decline (see run_scenario.py's
          // matching resolver) vs the Nth [data-idx] button.
          ok = idx === null ? clickEl(ov.skip) : clickEl(ov.buttons[idx]);
        } else if (ov && ov.kind === 'branching_evolution') {
          // No decline: `optional: false`, exactly like the source, which
          // never builds a cancel affordance here.
          if (idx === null) throw new Error('step ' + step + ': branching-evolution has no decline');
          ok = clickEl(ov.buttons[idx]);
        } else if (ov && ov.kind === 'team_pick') {
          // No decline: `optional: false`, matching `showTeamPickerModal`,
          // which never builds a cancel affordance either.
          if (idx === null) throw new Error('step ' + step + ': team-picker has no decline');
          ok = clickEl(ov.buttons[idx]);
        } else if (currentScreen === 'swap-screen') {
          if (idx === null) ok = clickEl(document.getElementById('btn-cancel-swap'));
          else if (state.team.length < 6) ok = clickEl(firstClickable(document.querySelector('#swap-incoming .poke-card'), 0));
          else ok = clickEl(nthChoice('swap-choices', idx));
        } else if (currentScreen === 'catch-screen') {
          // doCatchNode: cards appended to #catch-choices, decline via
          // #btn-skip-catch (bundle.deobfuscated.js:78426-78960).
          if (idx === null) ok = clickEl(document.getElementById('btn-skip-catch'));
          else ok = clickEl(nthChoice('catch-choices', idx));
        } else if (currentScreen === 'item-screen') {
          // doItemNode: choices appended to #item-choices, decline via
          // #btn-skip-item (bundle.deobfuscated.js:79260-79436).
          if (idx === null) ok = clickEl(document.getElementById('btn-skip-item'));
          else ok = clickEl(nthChoice('item-choices', idx));
        } else if (currentScreen === 'shiny-screen') {
          // doShinyNode: #btn-take-shiny (re-id'd from the rendered card) vs
          // #btn-skip-shiny (80937-80989).
          if (idx === null) ok = clickEl(document.getElementById('btn-skip-shiny'));
          else ok = clickEl(document.getElementById('btn-take-shiny'));
        } else if (currentScreen === 'trade-screen') {
          // doTradeNode: the Nth li.trade-member-row IS the clickable element
          // (the listener is on the row itself, 80621-80629), decline via
          // #btn-skip-trade (80632-80637).
          if (idx === null) ok = clickEl(document.getElementById('btn-skip-trade'));
          else ok = clickEl(nthChoice('trade-team-list', idx));
        } else if (currentScreen === 'battle-screen') {
          ok = clickEl(document.getElementById('btn-continue-battle'));
        } else {
          throw new Error('step ' + step + ': no choice bridge for screen ' + currentScreen);
        }
        if (!ok) throw new Error('step ' + step + ': choice index ' + idx + ' had no handler on ' + currentScreen);
        await pump();
        checkpoint('choice_post', { index: idx, step: step });
      } else if (act.kind === 'equip') {
        // M6. The item-equip overlay opened FROM THE BAG. The source reaches
        // it from an item-bar badge whose handler is
        // `openItemEquipModal(item, { fromBagIdx: idx })`
        // (bundle.deobfuscated.js:64857); the driver calls that same function
        // with that same argument shape.
        var bagIndex = act.bag_index;
        var bagItem = state.items[bagIndex];
        if (!bagItem) throw new Error('step ' + step + ': no bag item ' + bagIndex);
        checkpoint('equip_pre', {
          bag_index: bagIndex,
          member: act.member,
          item: bagItem.id === undefined ? null : bagItem.id,
          step: step,
        });
        openItemEquipModal(bagItem, { fromBagIdx: bagIndex });
        await pump();
        var eqOv = detectOverlay();
        if (!eqOv || eqOv.kind !== 'item_equip') {
          throw new Error('step ' + step + ': bag equip overlay did not open');
        }
        // By `data-idx` value, not position -- see the `held_item` branch.
        // With `fromBagIdx` no member is the "holding" one, so every member
        // does get a `[data-idx]` button here, but addressing them by value
        // keeps the two branches honest about the same thing.
        if (!clickEl(byDataValue(eqOv.buttons, 'idx', act.member))) {
          throw new Error('step ' + step + ': equip target ' + act.member + ' had no handler');
        }
        await pump();
        checkpoint('equip_post', { bag_index: bagIndex, member: act.member, step: step });
      } else if (act.kind === 'held_item') {
        // M6. The item-equip overlay opened FROM a member. The source reaches
        // this by clicking a held-item badge, whose handler is exactly
        // `openItemEquipModal(mon.heldItem, { fromPokemonIdx: idx })`
        // (bundle.deobfuscated.js:64702-64709 on the team bar, 78203 on the
        // party screen). The driver calls that same function with that same
        // argument shape rather than synthesizing a badge to click, for the
        // same reason every other bridge invokes the source's real handler.
        var member = act.member;
        var mon = state.team[member];
        if (!mon) throw new Error('step ' + step + ': no team member ' + member);
        var targetIdx = (act.target === undefined || act.target === null) ? null : act.target;
        checkpoint('held_item_pre', {
          member: member,
          target: targetIdx,
          held: mon.heldItem ? mon.heldItem.id : null,
          step: step,
        });
        if (!mon.heldItem) {
          throw new Error('step ' + step + ': member ' + member + ' holds nothing');
        }
        openItemEquipModal(mon.heldItem, { fromPokemonIdx: member });
        await pump();
        var heldOv = detectOverlay();
        if (!heldOv || heldOv.kind !== 'item_equip') {
          throw new Error('step ' + step + ': held-item overlay did not open');
        }
        // Buttons are addressed BY THEIR `data-idx` VALUE, not by position.
        // The source does not give every member a `[data-idx]` button: the row
        // for the member the overlay was opened from renders
        // `data-unequip="<i>"` and the label "Unequip" instead
        // (79490-79495), so `querySelectorAll('button[data-idx]')` skips it
        // and positional indexing would silently address the wrong member.
        var heldOk;
        if (targetIdx !== null) {
          // The hand-off row (79541-79545).
          heldOk = clickEl(byDataValue(heldOv.buttons, 'idx', targetIdx));
        } else if (act.via === 'row') {
          // The holder's OWN row, `[data-unequip]` (79521-79531) -- the other
          // spelling of held-item removal, and the one the M6 brief cites.
          var unequipButtons = Array.prototype.slice.call(
            heldOv.el.querySelectorAll('[data-unequip]'));
          heldOk = clickEl(byDataValue(unequipButtons, 'unequip', member));
        } else {
          // `#btn-equip-to-bag` with `fromPokemonIdx >= 0` (79549-79553).
          heldOk = clickEl(heldOv.skip);
        }
        if (!heldOk) {
          throw new Error('step ' + step + ': held-item target ' + targetIdx + ' had no handler');
        }
        await pump();
        checkpoint('held_item_post', { member: member, target: targetIdx, step: step });
      } else if (act.kind === 'advance_map') {
        checkpoint('map_transition_pre', { from_map: state.currentMap, step: step });
        if (currentScreen !== 'badge-screen') {
          throw new Error('step ' + step + ': advance_map but screen is ' + currentScreen);
        }
        if (!clickEl(document.getElementById('btn-next-map'))) {
          throw new Error('step ' + step + ': badge screen had no next-map handler');
        }
        await pump();
        checkpoint('map_transition_post', { to_map: state.currentMap, step: step });
      } else {
        throw new Error('step ' + step + ': unknown action kind ' + act.kind);
      }
    }

    checkpoint('terminal', {
      game_over: gameOverSeen,
      team_size: state.team.length,
      screen: currentScreen,
    });
  } catch (e) {
    OUT.error = (e && e.stack) || String(e);
  }

  OUT.rng_draws_total = rngDraws;
  OUT.network_attempts = networkAttempts;
  OUT.screen_log = screenLog;
  globalThis.__RESULT__ = JSON.stringify(OUT);
  globalThis.__DONE__ = true;
})().catch(function (e) {
  globalThis.__FATAL__ = (e && e.stack) || String(e);
  globalThis.__DONE__ = true;
});
