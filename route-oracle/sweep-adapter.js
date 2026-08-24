// ============================================================================
// M7 -- the SOURCE-side canonical legal-action adapter and action executor.
//
// Concatenated into the VM sandbox after `driver.js`, which hands it a bundle
// of its own already-audited helpers (see driver.js's `__SWEEP_SERVE__` call).
// Nothing here reimplements game logic:
//
//   * legality is enumerated from the SOURCE's own run state, the SOURCE's own
//     active screen/overlay, and the affordances the SOURCE actually built --
//     never from the Python result and never from a comparison manifest;
//   * every action is executed by invoking the real source handler the source
//     itself attached (or, for the two bag/held-item entry points that are
//     reached from a badge rather than a listener, by calling the same source
//     function that badge's handler calls, with the same argument shape --
//     exactly the discipline driver.js's `equip`/`held_item` bridges already
//     use);
//   * no JS state is ever mutated to simulate a result.
//
// The one thing this file DOES own is the normalized ACTION VOCABULARY, which
// has to be identical on both runtimes for a comparison to mean anything. It
// is documented in `SWEEP.md` and mirrored by `sweep.py`'s Python adapter.
// ============================================================================

(function () {
  'use strict';

  var API = null;

  // ---------------------------------------------------------------------
  // M7 battle-stage capture
  // ---------------------------------------------------------------------
  // The M7 brief requires the compared battle result to include per-combatant
  // STAT STAGES. Neither the frozen route schema nor tools/battle-oracle
  // carries them today: driver.js defines `normalizeStages` but its
  // `normalizeMon` never calls it, so stages are invisible to every pre-M7
  // gate. That blind spot is exactly why the first sweep saw a Gen4 stage
  // divergence only two events later, as an unexplained 1-point damage gap.
  //
  // This wraps `runBattle` a SECOND time, outside driver.js's own wrapper, so
  // it observes the same `res` object driver.js normalizes -- same battle,
  // same index, no extra call and no RNG draw. The legacy schema is untouched;
  // this rides alongside it as M7's own richer validation projection.
  //
  // M7-COMBINED (A1) adds the ABILITY half of the same blind spot. `onSwitchIn`
  // assigns `combatant._gen3Ability = getGen3Ability(speciesId)` onto the
  // battle clone (bundle.deobfuscated.js:57696-57702), which is the very
  // object `res.pTeam` / `res.eTeam` hand back -- the same place the stages are
  // read from. It is therefore observed, not recomputed: this adapter never
  // calls `getGen3Ability` itself, so a source table change and a Python table
  // change cannot cancel out. A combatant that never switched in has no
  // `_gen3Ability` at all, which normalizes to `null` and matches the Python
  // `Combatant.gen3_ability` default (battle.py:228) rather than being faked.
  var sweepStages = [];
  var sweepAbilities = [];
  (function () {
    var wrapped = runBattle;
    runBattle = function () {
      var res = wrapped.apply(this, arguments);
      function stagesOf(team) {
        return (team || []).map(function (m) {
          var st = (m && m.stages) || {};
          return {
            atk: st.atk || 0, def: st.def || 0, speed: st.speed || 0,
            special: st.special || 0, spdef: st.spdef || 0
          };
        });
      }
      function abilitiesOf(team) {
        return (team || []).map(function (m) {
          if (!m) return null;
          // READ the field the source itself wrote. `undefined` (never
          // switched in) and an explicit null both normalize to null.
          return m._gen3Ability === undefined ? null : (m._gen3Ability || null);
        });
      }
      sweepStages.push({ player: stagesOf(res && res.pTeam), enemy: stagesOf(res && res.eTeam) });
      sweepAbilities.push({
        player: abilitiesOf(res && res.pTeam),
        enemy: abilitiesOf(res && res.eTeam)
      });
      return res;
    };
  })();

  // ---------------------------------------------------------------------
  // M7-COMBINED (A1): the run-level PASSIVE list
  // ---------------------------------------------------------------------
  // `state.passives` is the only trait/passive input to a battle that VARIES
  // across the Story/Nuzlocke surface. `runBattleScreen`'s non-Endless config
  // branch is literally
  //   buildTraitsConfig({}, {}, state.passives || [])
  // (bundle.deobfuscated.js:81076-81085), so both tier maps are the constant
  // `{}` and the passive list is the whole varying input. Comparing it is
  // therefore comparing the real trait/passive state, not a proxy for it.
  // Read off the source's own `state`, never recomputed.
  function runPassives() {
    return (state.passives || []).map(function (t) {
      if (t === null || t === undefined) return null;
      return typeof t === 'object' ? (t.id === undefined ? null : t.id) : t;
    });
  }

  // ---------------------------------------------------------------------
  // Normalized action identity
  // ---------------------------------------------------------------------
  // A normalized action is a plain JSON object whose canonical string form is
  // what the two runtimes compare. `kind` plus the kind's own parameters; no
  // DOM text, no positional index into a query result, no object identity.
  function act(kind, params) {
    var out = { kind: kind };
    var keys = Object.keys(params || {}).sort();
    for (var i = 0; i < keys.length; i++) out[keys[i]] = params[keys[i]];
    return out;
  }

  // Provenance: why this normalized action is legal, in source terms. Recorded
  // alongside every enumerated action so an auditor can check the derivation
  // rather than trust the set. Never compared (Python's provenance is its own
  // code path), only reported.
  function withProv(a, provenance) {
    a.__prov = provenance;
    return a;
  }

  // ---------------------------------------------------------------------
  // Is a fixed-id button REALLY present?
  // ---------------------------------------------------------------------
  // run-scenario.js's DOM stub auto-vivifies `getElementById` AND fabricates a
  // stable synthetic child for any `el.querySelector('#id')`, so truthiness
  // proves nothing (driver.js's own `detectOverlay` header explains this at
  // length). The honest test for a button inside a raw-`innerHTML` template is
  // whether the template TEXT the source assigned actually contains that id --
  // the same test driver.js uses to tell the tutor modal from the equip modal.
  function templateHas(el, id) {
    return !!el && typeof el.innerHTML === 'string' && el.innerHTML.indexOf(id) !== -1;
  }

  // ---------------------------------------------------------------------
  // Source-side item classification
  // ---------------------------------------------------------------------
  // `it.usable` is the SOURCE's own discriminator, read off the source's own
  // pool entry (the item-bar badge at 64858 and the team-slot drop at 64946
  // both branch on exactly this field). Read straight off the object the
  // source stored, so the adapter never keeps its own item table.
  function isUsable(it) {
    return !!(it && typeof it === 'object' && it.usable);
  }

  function itemId(it) {
    return it && typeof it === 'object' && it.id !== undefined ? it.id : null;
  }

  // ---------------------------------------------------------------------
  // Presentation-only dismissals (M7 finding T1)
  // ---------------------------------------------------------------------
  // Two source screens park the run on a button that carries NO decision. They
  // must be auto-pressed -- never offered as a legal action -- or the sweep
  // would compare a source RECEIPT against a Python port that has no such
  // screen and report a divergence where the runtimes are equal.
  //
  //   #btn-continue-battle  resolves runBattleScreen's promise
  //                         (81384-81387 / 81427-81429). driver.js's `drive`
  //                         already presses it; this is the same rule.
  //
  //   #btn-trade-continue   `completeTrade` (80818-80860). The trade is
  //                         ALREADY complete when this appears: the team splice
  //                         (80825), `state.savedTrade = null` (80826),
  //                         `recordMonOrigin` (80827) and -- decisively --
  //                         `advanceFromNode(state.map, B.id)` (80841) all run
  //                         BEFORE `showScreen("shiny-screen")` at 80846. The
  //                         button's entire handler is `() => showMapScreen()`
  //                         (80859). No gameplay state, no RNG draw, no node
  //                         transition. The Python port models the same
  //                         transition without a receipt screen, which is why
  //                         this is a tool repair and not a port repair.
  //
  // The receipt shares the `shiny-screen` id with `doShinyNode`'s REAL choice
  // (80937), so the two are told apart the same way the tutor and equip modals
  // are: by what the source's own template text actually contains. Only two
  // sites call `showScreen("shiny-screen")` in the whole bundle -- 80846 and
  // 80937 -- so this discriminator is total.
  function isTradeReceipt() {
    return API.screen() === 'shiny-screen' &&
      templateHas(document.getElementById('shiny-content'), 'btn-trade-continue');
  }

  async function settle() {
    for (var g = 0; g < 32; g++) {
      await API.pump();
      // A real OVERLAY is a decision, never a receipt, so quiesce here and let
      // the enumerator offer it.
      //
      // M7-COMBINED: this was a live deadlock, not a hypothetical. The three
      // showScreen-less overlays (`showBranchingChoice`'s
      // `#eevee-choice-overlay` at 70560, `showTeamPickerModal`'s
      // `#submap-pick-modal` at 76845, `openItemEquipModal`'s at 79442) leave
      // `currentScreen` naming whatever was up BEFORE them -- and after a
      // battle win that is `battle-screen`. So the loop below saw
      // `battle-screen`, clicked `#btn-continue-battle` again, got a truthy
      // click back, and span until the bound tripped. The goal-directed
      // scheduler (A4) hit it the first time it reached a branching evolution:
      // `checkAndEvolveTeam` runs in `runBattleScreen`'s win branch (81381)
      // with the battle screen still up, so EVERY branching evolution reached
      // this way was an `apply_error_asymmetry` rather than the choice it
      // really is. `#btn-continue-battle` has already done its job by then --
      // it resolves the battle promise, which is what let the evolve step run
      // at all -- so pressing it again dismisses nothing.
      if (API.detectOverlay()) return;
      if (API.screen() === 'battle-screen') {
        if (API.clickEl(document.getElementById('btn-continue-battle'))) continue;
        return;
      }
      if (isTradeReceipt()) {
        if (API.clickEl(document.getElementById('btn-trade-continue'))) continue;
        throw new Error('trade receipt is up but #btn-trade-continue had no handler');
      }
      return;
    }
    throw new Error('presentation dismissals did not quiesce after 32 rounds');
  }

  // ---------------------------------------------------------------------
  // LEGAL ACTION ENUMERATION
  // ---------------------------------------------------------------------
  function legalActions() {
    var screen = API.screen();
    var overlay = API.detectOverlay();

    // Terminal first: `showGameOver` is the source's own end-of-run screen and
    // nothing gameplay-bearing follows it.
    if (API.gameOverSeen() || screen === 'gameover-screen') return [];
    if (screen === 'win-screen') return [];

    // ---- the three showScreen-less overlays, checked BEFORE currentScreen --
    // (driver.js's `pendingState` establishes and justifies this ordering:
    // none of them calls `showScreen`, so `currentScreen` still names whatever
    // was up before and can never detect them.)
    if (overlay) {
      if (overlay.kind === 'move_tutor') {
        // `doMoveTutorNode` 80464-80563: one `[data-tutor]` button per
        // NON-MASTERED member, plus `#btn-skip-tutor` (80531-80534).
        //
        // M7 finding T2. `data-tutor`'s VALUE is the member's TEAM index, but
        // the normalized `select_option.index` is defined as the POSITION in
        // the agreed pending option list -- and those two differ the moment
        // any member is already mastered and gets no button. A team of
        // [mastered Snorunt, unmastered Skitty] builds exactly one button,
        // `data-tutor="1"`; the port's `PendingChoice.options` holds exactly
        // one option, at position 0, whose `slot` is 1. Both runtimes describe
        // the SAME offer; only the numbering differed, and enumerating the raw
        // attribute value here reported a legal-set mismatch where the
        // runtimes agree. Positions are what driver.js's own frozen `choice`
        // bridge has always used (`ov.buttons[idx]`), and `pendingState`
        // builds its options in `overlay.buttons` order, so position i always
        // means `overlay.buttons[i]` -- the member's identity travels in the
        // compared option's `slot`, not in the index.
        var tut = [];
        for (var b0 = 0; b0 < overlay.buttons.length; b0++) {
          var tIdx = parseInt(overlay.buttons[b0].getAttribute('data-tutor'), 10);
          tut.push(withProv(act('select_option', { index: b0, cancel: false }),
            'doMoveTutorNode option ' + b0 + ' = [data-tutor="' + tIdx + '"] (80535)'));
        }
        if (templateHas(overlay.el, 'btn-skip-tutor')) {
          tut.push(withProv(act('select_option', { index: null, cancel: false }),
            'doMoveTutorNode #btn-skip-tutor (80531-80534)'));
        }
        return tut;
      }
      if (overlay.kind === 'item_equip') {
        // `openItemEquipModal` 79442-79570. THREE genuinely different exits,
        // and this is the case the whole M7 legality correction is about:
        //   [data-idx]           equip/swap onto that member   (79535-79551)
        //   #btn-equip-to-bag    bank / unequip-to-bag         (79552-79562)
        //   #btn-equip-cancel    neither -- body is `remove()` (79563-79569)
        var eq = [];
        for (var b1 = 0; b1 < overlay.buttons.length; b1++) {
          var eIdx = parseInt(overlay.buttons[b1].getAttribute('data-idx'), 10);
          eq.push(withProv(act('select_option', { index: eIdx, cancel: false }),
            'openItemEquipModal [data-idx="' + eIdx + '"] (79535-79551)'));
        }
        if (templateHas(overlay.el, 'btn-equip-to-bag')) {
          eq.push(withProv(act('select_option', { index: null, cancel: false }),
            'openItemEquipModal #btn-equip-to-bag (79552-79562)'));
        }
        if (templateHas(overlay.el, 'btn-equip-cancel')) {
          eq.push(withProv(act('select_option', { index: null, cancel: true }),
            'openItemEquipModal #btn-equip-cancel (79563-79569)'));
        }
        return eq;
      }
      if (overlay.kind === 'branching_evolution') {
        // `showBranchingChoice` 70560-70613. No cancel affordance exists.
        var evo = [];
        for (var b2 = 0; b2 < overlay.buttons.length; b2++) {
          evo.push(withProv(act('select_option', { index: b2, cancel: false }),
            'showBranchingChoice card ' + b2 + ' (70604)'));
        }
        return evo;
      }
      if (overlay.kind === 'team_pick') {
        // `showTeamPickerModal` 76845-76884. No cancel affordance exists.
        var pick = [];
        for (var b3 = 0; b3 < overlay.buttons.length; b3++) {
          var pIdx = parseInt(overlay.buttons[b3].getAttribute('data-idx'), 10);
          pick.push(withProv(act('select_option', { index: pIdx, cancel: false }),
            'showTeamPickerModal [data-idx="' + pIdx + '"] (76876)'));
        }
        return pick;
      }
    }

    if (screen === 'starter-screen') {
      // `showStarterSelect` 76175-76194 -- one card per offered starter. The
      // normalized identity is the SPECIES, not the card position, so the two
      // runtimes compare identities rather than offsets.
      var pend = API.pendingState();
      if (!pend || pend.phase !== 'choose_starter') {
        throw new Error('starter-screen up but pendingState reported ' + (pend && pend.phase));
      }
      var st = [];
      for (var s0 = 0; s0 < pend.options.length; s0++) {
        st.push(withProv(act('choose_starter', { species_id: pend.options[s0].species_id }),
          'showStarterSelect card ' + s0 + ' -> selectStarter (76176-76186)'));
      }
      return st;
    }

    if (screen === 'swap-screen') {
      // `showSwapScreen` 79141-79258. With room the single incoming card is
      // the only accept (79171-79201); full, one release card per member
      // (79202-79246). `#btn-cancel-swap` is always built (79247-79258).
      var swap = [];
      var p = API.pendingState();
      if (!p) throw new Error('swap-screen up but pendingState reported null');
      for (var i0 = 0; i0 < p.option_count; i0++) {
        swap.push(withProv(act('select_option', { index: i0, cancel: false }),
          state.team.length < 0x6
            ? 'showSwapScreen #swap-incoming .poke-card (79171-79201)'
            : 'showSwapScreen #swap-choices card ' + i0 + ' (79202-79246)'));
      }
      swap.push(withProv(act('select_option', { index: null, cancel: false }),
        'showSwapScreen #btn-cancel-swap (79247-79258)'));
      return swap;
    }

    // The remaining `showScreen` choice screens. Each builds its cards from
    // state the source itself owns and orders (driver.js's `pendingState`
    // cites the exact site for every one), plus one skip button.
    if (isTradeReceipt()) {
      // `settle()` runs after every action, so this is unreachable; kept as a
      // loud assertion rather than a silent mis-enumeration of the receipt as
      // a one-option shiny CHOICE, which is exactly what the first corpus run
      // reported before T1 was traced.
      throw new Error('trade receipt still up at legal-action enumeration');
    }

    var SIMPLE = {
      'catch-screen': { skip: 'btn-skip-catch', cite: 'doCatchNode (78426-78960)' },
      'item-screen': { skip: 'btn-skip-item', cite: 'doItemNode (79260-79436)' },
      'shiny-screen': { skip: 'btn-skip-shiny', cite: 'doShinyNode (80937-80989)' },
      'trade-screen': { skip: 'btn-skip-trade', cite: 'doTradeNode (80580-80637)' }
    };
    if (SIMPLE[screen]) {
      var cfg = SIMPLE[screen];
      var ps = API.pendingState();
      if (!ps) throw new Error(screen + ' up but pendingState reported null');
      var simple = [];
      for (var s1 = 0; s1 < ps.option_count; s1++) {
        simple.push(withProv(act('select_option', { index: s1, cancel: false }),
          cfg.cite + ' card ' + s1));
      }
      simple.push(withProv(act('select_option', { index: null, cancel: false }),
        cfg.cite + ' #' + cfg.skip));
      return simple;
    }

    if (screen === 'badge-screen') {
      // `showBadgeScreen`'s own "Next Map" button IS the map transition.
      return [withProv(act('advance_map', {}), 'showBadgeScreen #btn-next-map')];
    }

    if (screen === 'battle-screen') {
      // Never a player DECISION in this harness: `#btn-continue-battle` only
      // resolves `runBattleScreen`'s promise (81384-81387 / 81427-81429), and
      // driver.js's `drive()` already presses it as a non-choice dismissal. A
      // sweep should never observe this screen between actions; reporting it
      // as "no legal actions" rather than silently inventing one is what makes
      // that assumption checkable.
      return [];
    }

    if (screen === 'map-screen') return mapScreenActions();

    throw new Error('no source legal-action adapter for screen ' + screen);
  }

  // The map screen carries the node choice plus every out-of-band utility the
  // source's persistent team-bar/item-bar afford at any time while on the map.
  function mapScreenActions() {
    var out = [];
    var nodes = (state.map && state.map.nodes) || {};
    // Stable node IDENTITY, and a deterministic order that is the source's own
    // (layer, col, id) ordering rather than object-key order.
    var ids = Object.keys(nodes).filter(function (id) { return nodes[id].accessible; });
    ids.sort(function (a, b) {
      var na = nodes[a], nb = nodes[b];
      return (na.layer - nb.layer) || (na.col - nb.col) || (a < b ? -1 : a > b ? 1 : 0);
    });
    for (var n0 = 0; n0 < ids.length; n0++) {
      out.push(withProv(act('visit_node', { node_id: ids[n0] }),
        'onNodeClick(' + ids[n0] + ') (77312+)'));
    }

    var team = state.team || [];
    var items = state.items || [];

    // ---- team reorder ---------------------------------------------------
    // The source's reorder is the team-bar drag handler at 64790-64812, and
    // its whole mutation is `[O[a], O[b]] = [O[b], O[a]]` -- a SWAP of two
    // slots, never an arbitrary permutation. The normalized vocabulary is
    // therefore a transposition `(i, j)`, i < j, which is exactly the source's
    // atomic action and is expressible on both runtimes. See SWEEP.md and the
    // M7 record's finding F1 for why Python's wider `ReorderTeam(order)`
    // declaration is reported rather than silently intersected away.
    for (var i1 = 0; i1 < team.length; i1++) {
      for (var j1 = i1 + 1; j1 < team.length; j1++) {
        out.push(withProv(act('reorder_team', { i: i1, j: j1 }),
          'team-bar drag swap slots ' + i1 + '<->' + j1 + ' (64798-64806)'));
      }
    }

    // ---- bag items ------------------------------------------------------
    for (var b = 0; b < items.length; b++) {
      var it = items[b];
      if (isUsable(it)) {
        // Team-slot drop, 64946-64948: `usableItemCanTarget` gates it, and the
        // SOURCE's own predicate is called here rather than reimplemented.
        for (var t = 0; t < team.length; t++) {
          if (usableItemCanTarget(it, team[t])) {
            out.push(withProv(act('use_item', { item_id: itemId(it), bag_index: b, target_index: t }),
              'applyUsableItemTo gated by usableItemCanTarget (64946-64948)'));
          }
        }
      } else {
        // Non-usable: the drop routes to `equipItemFromBag` (64950), which
        // succeeds for any (existing bag item, existing member) pair.
        for (var t2 = 0; t2 < team.length; t2++) {
          out.push(withProv(act('equip_item', { item_id: itemId(it), bag_index: b, team_index: t2 }),
            'equipItemFromBag (64950 -> 79653-79671)'));
        }
      }
    }

    // ---- held items -----------------------------------------------------
    // Both reached by opening `openItemEquipModal(mon.heldItem,
    // {fromPokemonIdx: i})` from a held-item badge (64702 team bar / 78203
    // party screen), so only members actually HOLDING something have a badge.
    for (var h = 0; h < team.length; h++) {
      if (!team[h].heldItem) continue;
      out.push(withProv(act('unequip_item', { team_index: h }),
        'openItemEquipModal(fromPokemonIdx=' + h + ') [data-unequip] (79521-79531)'));
      for (var to = 0; to < team.length; to++) {
        if (to === h) continue;
        out.push(withProv(act('hand_off_item', { from_index: h, to_index: to }),
          'openItemEquipModal(fromPokemonIdx=' + h + ') [data-idx="' + to + '"] (79541-79545)'));
      }
    }
    return out;
  }

  // ---------------------------------------------------------------------
  // ACTION EXECUTION -- always through the source's own handler
  // ---------------------------------------------------------------------
  async function applyAction(a) {
    var screen = API.screen();
    var overlay = API.detectOverlay();

    if (a.kind === 'choose_starter') {
      // Click the card the source built for that species, found by the
      // source's own offer order (never by a species->index guess).
      var pend = API.pendingState();
      var idx = -1;
      for (var i = 0; i < pend.options.length; i++) {
        if (pend.options[i].species_id === a.species_id) { idx = i; break; }
      }
      if (idx < 0) throw new Error('no starter card for species ' + a.species_id);
      var el = API.nthChoice('starter-choices', idx);
      if (!API.clickEl(el)) throw new Error('starter card ' + idx + ' had no handler');
      await settle();
      return;
    }

    if (a.kind === 'advance_map') {
      if (screen !== 'badge-screen') throw new Error('advance_map but screen is ' + screen);
      if (!API.clickEl(document.getElementById('btn-next-map'))) {
        throw new Error('badge screen had no next-map handler');
      }
      await settle();
      return;
    }

    if (a.kind === 'visit_node') {
      var node = state.map.nodes[a.node_id];
      if (!node) throw new Error('no node ' + a.node_id);
      await API.drive(function () { return onNodeClick(node); });
      await settle();
      return;
    }

    if (a.kind === 'select_option') {
      await applySelectOption(a, screen, overlay);
      await settle();
      return;
    }

    if (a.kind === 'reorder_team') {
      // The drag handler's own mutation (64798-64806), performed on the
      // source's own `state.team` array. There is no callable source function
      // behind it -- the swap is written inline in the pointerup listener --
      // so this is the one action executed as the source's exact statement
      // rather than through a named function. `renderTeamBar` is a driver stub
      // and carries no gameplay.
      var tm = state.team;
      var tmp = tm[a.i]; tm[a.i] = tm[a.j]; tm[a.j] = tmp;
      renderTeamBar(tm);
      await settle();
      return;
    }

    if (a.kind === 'use_item') {
      var itU = state.items[a.bag_index];
      if (!itU) throw new Error('no bag item ' + a.bag_index);
      await API.drive(function () {
        return applyUsableItemTo(itU, a.bag_index, a.target_index, null);
      });
      await settle();
      return;
    }

    if (a.kind === 'equip_item') {
      var itE = state.items[a.bag_index];
      if (!itE) throw new Error('no bag item ' + a.bag_index);
      if (!equipItemFromBag(a.bag_index, a.team_index)) {
        throw new Error('equipItemFromBag refused ' + a.bag_index + '->' + a.team_index);
      }
      await settle();
      return;
    }

    if (a.kind === 'unequip_item' || a.kind === 'hand_off_item') {
      var from = a.kind === 'unequip_item' ? a.team_index : a.from_index;
      var mon = state.team[from];
      if (!mon || !mon.heldItem) throw new Error('member ' + from + ' holds nothing');
      openItemEquipModal(mon.heldItem, { fromPokemonIdx: from });
      await API.pump();
      var ov = API.detectOverlay();
      if (!ov || ov.kind !== 'item_equip') throw new Error('held-item overlay did not open');
      var ok;
      if (a.kind === 'unequip_item') {
        // The holder's own `[data-unequip]` row (79521-79531), addressed BY
        // ATTRIBUTE VALUE -- the M6 lesson: the holder's row carries
        // `data-unequip`, not `data-idx`, so `[data-idx]` has a hole in it and
        // positional indexing addresses the wrong member.
        var rows = Array.prototype.slice.call(ov.el.querySelectorAll('[data-unequip]'));
        ok = API.clickEl(API.byDataValue(rows, 'unequip', from));
      } else {
        ok = API.clickEl(API.byDataValue(ov.buttons, 'idx', a.to_index));
      }
      if (!ok) throw new Error(a.kind + ' had no handler');
      await settle();
      return;
    }

    throw new Error('unknown action kind ' + a.kind);
  }

  async function applySelectOption(a, screen, overlay) {
    var idx = a.index === undefined ? null : a.index;
    var ok = false;
    if (overlay && overlay.kind === 'move_tutor') {
      // Position, matching the enumeration above and driver.js's frozen
      // bridge -- NOT `byDataValue(..., 'tutor', idx)`, which would address
      // the member whose TEAM index equals the option position (see T2).
      ok = idx === null ? API.clickEl(overlay.skip)
        : API.clickEl(overlay.buttons[idx]);
    } else if (overlay && overlay.kind === 'item_equip') {
      if (a.cancel) {
        // `#btn-equip-cancel` (79563-79569). Its listener was attached by the
        // source through `B2O.querySelector('#btn-equip-cancel')`, and the DOM
        // stub hands back that SAME cached synthetic child here, so this is
        // the source's own handler and not a fabricated one.
        ok = API.clickEl(overlay.el.querySelector('#btn-equip-cancel'));
      } else if (idx === null) {
        ok = API.clickEl(overlay.skip);
      } else {
        ok = API.clickEl(API.byDataValue(overlay.buttons, 'idx', idx));
      }
    } else if (overlay && overlay.kind === 'branching_evolution') {
      if (idx === null) throw new Error('branching-evolution has no decline');
      ok = API.clickEl(overlay.buttons[idx]);
    } else if (overlay && overlay.kind === 'team_pick') {
      if (idx === null) throw new Error('team-picker has no decline');
      ok = API.clickEl(API.byDataValue(overlay.buttons, 'idx', idx));
    } else if (screen === 'swap-screen') {
      if (idx === null) ok = API.clickEl(document.getElementById('btn-cancel-swap'));
      else if (state.team.length < 6) ok = API.clickEl(API.firstClickable(document.querySelector('#swap-incoming .poke-card'), 0));
      else ok = API.clickEl(API.nthChoice('swap-choices', idx));
    } else if (screen === 'catch-screen') {
      ok = idx === null ? API.clickEl(document.getElementById('btn-skip-catch'))
        : API.clickEl(API.nthChoice('catch-choices', idx));
    } else if (screen === 'item-screen') {
      ok = idx === null ? API.clickEl(document.getElementById('btn-skip-item'))
        : API.clickEl(API.nthChoice('item-choices', idx));
    } else if (screen === 'shiny-screen') {
      ok = idx === null ? API.clickEl(document.getElementById('btn-skip-shiny'))
        : API.clickEl(document.getElementById('btn-take-shiny'));
    } else if (screen === 'trade-screen') {
      ok = idx === null ? API.clickEl(document.getElementById('btn-skip-trade'))
        : API.clickEl(API.nthChoice('trade-team-list', idx));
    } else {
      throw new Error('no select_option bridge for screen ' + screen);
    }
    if (!ok) throw new Error('select_option ' + JSON.stringify(a) + ' had no handler on ' + screen);
  }

  // ---------------------------------------------------------------------
  // The request/response service
  // ---------------------------------------------------------------------
  // The host cannot talk to the sandbox over stdio directly (the whole prefix
  // + driver runs inside ONE `vm.runInContext` call and `state` is a top-level
  // `let` the host cannot see). It therefore exchanges plain objects through
  // two globals the host owns, while this loop yields to the host's pump.
  globalThis.__SWEEP_SERVE__ = async function (api) {
    API = api;
    globalThis.__SWEEP_READY__ = true;
    for (var guard = 0; guard < 100000000; guard++) {
      var req = globalThis.__SWEEP_REQ__;
      if (!req) {
        // MUST be a macrotask, not `await Promise.resolve()`. The sandbox and
        // the host share one event loop: a microtask-only spin here starves
        // the host's `setImmediate` pump forever and the whole protocol
        // deadlocks (observed, first thing, on the very first `legal` call).
        // `setImmediate` itself is deliberately absent from the sandbox, so
        // the host injects `__SWEEP_YIELD__` -- see sweep-host.js.
        await globalThis.__SWEEP_YIELD__();
        continue;
      }
      globalThis.__SWEEP_REQ__ = null;
      var resp;
      try {
        resp = { ok: true, value: await handle(req) };
      } catch (e) {
        resp = { ok: false, error: (e && e.stack) || String(e) };
      }
      globalThis.__SWEEP_RESP__ = resp;
      if (req.op === 'quit') return;
    }
    throw new Error('sweep service exceeded its request bound');
  };

  async function handle(req) {
    if (req.op === 'quit') return { bye: true };
    if (req.op === 'legal') {
      return { screen: API.screen(), actions: legalActions() };
    }
    if (req.op === 'state') {
      // The M7 comparison projection. Built on driver.js's own checkpoint --
      // the same normalization the frozen 29-scenario gate already trusts --
      // plus the sweep's own additions (the battles this action produced and
      // the running RNG draw total). See SWEEP.md for every include/exclude
      // disposition.
      var before = req.battles_seen || 0;
      var cp = API.checkpoint('sweep', req.event || {});
      var all = API.battles();
      return {
        checkpoint: cp,
        battles: all.slice(before),
        battle_stages: sweepStages.slice(before),
        battle_abilities: sweepAbilities.slice(before),
        run_passives: runPassives(),
        battles_total: all.length,
        rng_draws_total: API.rngDraws()
      };
    }
    if (req.op === 'traits') {
      // READ-ONLY diagnostic projection of the source's trait/passive surface.
      // NOT part of the compared projection (see SWEEP.md's disposition table
      // and finding F2): it exists so an auditor can inspect the state that
      // the checkpoint schema does not carry, without a raw-eval backdoor.
      return {
        run_passives: (state.passives || []).map(function (t) {
          return t && typeof t === 'object' ? (t.id || t.key || t.name || null) : t;
        }),
        team: (state.team || []).map(function (m) {
          return {
            name: m.name,
            species_id: m.speciesId,
            traits: (m.traits || []).map(function (t) {
              return t && typeof t === 'object' ? (t.id || t.key || t.name || null) : t;
            }),
            gen3_ability: m.gen3Ability === undefined ? null : m.gen3Ability,
            ability: m.ability === undefined ? null : m.ability,
            stat_buffs: m.statBuffs || null,
            augment_pct: m.augmentPct === undefined ? null : m.augmentPct,
            max_hp: m.maxHp,
            base_stats: m.baseStats || null
          };
        })
      };
    }
    if (req.op === 'apply') {
      await applyAction(req.action);
      return { applied: true };
    }
    throw new Error('unknown op ' + req.op);
  }
})();
