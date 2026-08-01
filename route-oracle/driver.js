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
  showScreen = function (id) { currentScreen = id; screenLog.push(id); };

  showMapScreen = function () { showScreen('map-screen'); };
  renderMap = function () {};
  renderBattleField = function () {};
  renderTeamBar = function () {};
  renderItemBadges = function () {};
  renderTrainerIcons = function () {};
  renderPokemonCard = function () { return '<div class="poke-card"></div>'; };
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
        __diagnostic_event_count: (res.detailedLog || []).length,
      });
      return res;
    };
  })();

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
    if (n.reward !== undefined && n.reward && typeof n.reward === 'object') {
      out.reward = { kind: n.reward.kind === undefined ? null : n.reward.kind };
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

  // The source has no `PendingChoice`; the player's pending decision is
  // implied by which screen is up and how many affordances it built. This
  // reconstructs the SAME (kind, cardinality, optional) triple the Python
  // port reports from `RunState.pending`, from the source's own DOM, so the
  // two are genuinely compared rather than one side reporting nothing.
  // Screens with no pending decision report null on both sides.
  function pendingState() {
    function childCount(id) {
      var el = document.getElementById(id);
      return ((el && el.__children) || []).length;
    }
    if (currentScreen === 'starter-screen') {
      return { phase: 'choose_starter', optional: false, option_count: childCount('starter-choices') };
    }
    if (currentScreen === 'swap-screen') {
      // With room the incoming card is the only accept affordance
      // (bundle.deobfuscated.js:79171-79201); full, it is one card per team
      // member (79202-79246). Cancel is always available -> optional.
      return {
        phase: 'swap_choice', optional: true,
        option_count: state.team.length < 6 ? 1 : childCount('swap-choices'),
      };
    }
    if (currentScreen === 'catch-screen') {
      return { phase: 'catch_choice', optional: true, option_count: childCount('catch-choices') };
    }
    if (currentScreen === 'item-screen') {
      return { phase: 'item_choice', optional: true, option_count: childCount('item-choices') };
    }
    return null;
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
        if (currentScreen === 'swap-screen') {
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
        } else if (currentScreen === 'battle-screen') {
          ok = clickEl(document.getElementById('btn-continue-battle'));
        } else {
          throw new Error('step ' + step + ': no choice bridge for screen ' + currentScreen);
        }
        if (!ok) throw new Error('step ' + step + ': choice index ' + idx + ' had no handler on ' + currentScreen);
        await pump();
        checkpoint('choice_post', { index: idx, step: step });
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
