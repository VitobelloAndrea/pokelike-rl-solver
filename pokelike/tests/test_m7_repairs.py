"""Focused regressions for the M7-combined runtime repairs F-B, F-C and F-D.

Each of the three defects below was found by the M7 cross-runtime sweep
(`route-oracle/sweep.py`), retained as a durable reproducer under
`route-oracle/findings/`, and traced to a specific line of the source before
anything here was written. The sweep replays are the end-to-end evidence; this
file is the *focused* half — one test per source fact, small enough to name the
defect rather than merely notice it.

    F-B  route-oracle/findings/M7-divergence-story_gen4_0179.json
         `extreme_evoboost` boosted the wrong stat because the source's
         "which stat do I EXCLUDE" ternary was ported inverted.
         Source: bundle.deobfuscated.js:57410-57437.

    F-C  route-oracle/findings/M7-divergence-nuzlocke_gen3_0142.json
         The Gen3 Pickup roll ran inside `_run_battle`, so its `rng()` draws
         were attributed to the battle. The source draws them in
         `runBattleScreen`, AFTER `runBattle` has returned.
         Source: bundle.deobfuscated.js:81210-81245.

    F-D  route-oracle/findings/M7-divergence-nuzlocke_gen2_0077.json
         A Gen2 gym leader passes an explicit `0x2` level gain that overrides
         the Nuzlocke halving; the port applied the Nuzlocke rule to all four
         generations.
         Source: bundle.deobfuscated.js:56803, 77780-77797, 77829-77843.

Run with: python -m unittest pokelike.tests.test_m7_repairs -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pokelike import battle_abilities, battle_loop, data, engine, map_gen
from pokelike.battle import BattleConfig, Combatant
from pokelike.battle_loop import BattleResult


def _mon(species_id=1, level=50, **overrides):
    spec = data.get_pokedex()[species_id]
    bs = overrides.pop("base_stats", spec.base_stats)
    hp = overrides.pop("max_hp", map_gen.calc_hp(bs.hp, level))
    return Combatant(
        species_id=species_id, level=level, base_stats=bs, types=spec.types,
        max_hp=hp, current_hp=hp, name=spec.name, **overrides,
    )


def _quiet_win(player_team, enemy_team, **kwargs):
    """A battle that resolves without drawing a single `rng()`.

    That is the whole point for F-C: with the battle itself contributing zero
    draws, every draw observed across a call is unambiguously attributable to
    what the call does BESIDES the battle.
    """
    p_clone = [battle_loop.clone_combatant(m) for m in player_team]
    e_clone = [battle_loop.clone_combatant(m) for m in enemy_team]
    for m in e_clone:
        m.current_hp = 0
    return BattleResult(
        player_won=True,
        player_team=p_clone,
        enemy_team=e_clone,
        player_participants=set(range(len(p_clone))),
        rounds=1,
    )


# ===========================================================================
# F-B -- `extreme_evoboost` picks the stat the SOURCE picks
# ===========================================================================


class ExtremeEvoboostStatSelectionTests(unittest.TestCase):
    """bundle.deobfuscated.js:57410-57437.

        const BI6 = usesSpecialAttack(speciesId, baseStats) ? "atk" : "special",
              BI7 = BcM.filter((s) => s !== BI6);
        let BI8 = BI7[0];
        for (const s of BI7) if (base[s] < base[BI8]) BI8 = s;
        applyStageChange(BcY, BI8, 1, ...);

    `BI6` is the stat EXCLUDED from the candidate list, and it is the
    offensive stat the species does NOT use. The +1 then lands on the lowest
    remaining base stat.

    The port had the ternary inverted, excluding the stat the species DOES
    use. The table below is chosen so that every row would move if the
    inversion came back, and so that BOTH branches of the ternary are
    discriminated -- Glaceon is a special attacker, the rest are physical.
    """

    # species -> (uses_special_attack, source stat, stat the inverted port picked)
    CASES = {
        133: (False, "def", "special"),    # Eevee     atk 55 def 50 spd 55 spa 45 spdef 65
        197: (False, "atk", "special"),    # Umbreon   atk 65 def 110 spd 65 spa 60 spdef 130
        470: (False, "spdef", "special"),  # Leafeon   atk 110 def 130 spd 95 spa 60 spdef 65
        471: (True, "speed", "atk"),       # Glaceon   atk 60 def 110 spd 65 spa 130 spdef 95
    }

    def _switch_in(self, species_id):
        cfg = battle_abilities.Gen3AbilityConfig(gen4_mode=True)
        mon = _mon(species_id, level=50)
        opponent = _mon(1, level=50)
        cfg.on_switch_in(mon, [mon], opponent, [opponent], BattleConfig())
        return mon

    def test_the_boost_lands_on_the_stat_the_source_chooses(self):
        for species_id, (_, expected, _) in self.CASES.items():
            with self.subTest(species=data.get_pokedex()[species_id].name):
                mon = self._switch_in(species_id)
                raised = [s for s, v in mon.stages.items() if v]
                self.assertEqual(raised, [expected])
                self.assertEqual(mon.stages[expected], 1)

    def test_every_case_would_move_if_the_ternary_were_inverted_again(self):
        """The property that makes the table above a detector rather than a
        transcript: source choice and inverted-port choice must differ in
        every row, or that row proves nothing."""
        for species_id, (_, expected, inverted) in self.CASES.items():
            with self.subTest(species=data.get_pokedex()[species_id].name):
                self.assertNotEqual(expected, inverted)

    def test_the_excluded_stat_is_the_offensive_stat_the_species_does_not_use(self):
        """Stated as the source states it, so the intent survives a reader who
        never opens the bundle: a special attacker excludes `atk`."""
        from pokelike.battle import uses_special_attack
        for species_id, (uses_special, expected, _) in self.CASES.items():
            spec = data.get_pokedex()[species_id]
            with self.subTest(species=spec.name):
                self.assertEqual(uses_special_attack(species_id, spec.base_stats),
                                 uses_special)
                excluded = "atk" if uses_special else "special"
                self.assertNotEqual(expected, excluded,
                                    "the excluded stat can never be the boosted one")

    def test_the_boost_lands_on_the_switching_in_member_not_a_bystander(self):
        """The retained F-B record differs on `enemy[0]`, so "right stat, wrong
        member" has to fail too."""
        cfg = battle_abilities.Gen3AbilityConfig(gen4_mode=True)
        mon = _mon(133, level=50)
        bystander = _mon(133, level=50)
        opponent = _mon(1, level=50)
        cfg.on_switch_in(mon, [mon, bystander], opponent, [opponent], BattleConfig())
        self.assertEqual(mon.stages["def"], 1)
        self.assertEqual([s for s, v in bystander.stages.items() if v], [],
                         "a bystander must not gain a stage")
        self.assertEqual([s for s, v in opponent.stages.items() if v], [],
                         "extreme_evoboost is a self-buff, not a debuff")

    def test_the_candidate_order_is_the_sources_own_stat_order(self):
        """`BcM` is `["atk","def","speed","special","spdef"]` (57356) and the
        loop's strict `<` keeps the FIRST minimum, which is what `min()` over
        the same ordered sequence does. A reordered `_STATS` would break ties
        differently, so the order itself is pinned."""
        self.assertEqual(battle_abilities._STATS,
                         ("atk", "def", "speed", "special", "spdef"))

    def test_a_tie_between_two_candidates_keeps_the_earlier_one(self):
        """Directly exercises that tie rule rather than trusting it: two
        candidate stats share the minimum, and the one earlier in the source's
        order must win."""
        spec = data.get_pokedex()[133]
        tied = type(spec.base_stats)(hp=55, atk=99, defense=10, speed=10,
                                     special=99, spdef=99)
        cfg = battle_abilities.Gen3AbilityConfig(gen4_mode=True)
        mon = _mon(133, level=50, base_stats=tied)
        opponent = _mon(1, level=50)
        cfg.on_switch_in(mon, [mon], opponent, [opponent], BattleConfig())
        # uses_special_attack: special 99 >= atk 99 -> True -> excludes "atk".
        # Candidates def(10) speed(10) special(99) spdef(99); def precedes speed.
        self.assertEqual([s for s, v in mon.stages.items() if v], ["def"])


# ===========================================================================
# F-C -- the Pickup roll belongs to `runBattleScreen`, not to `runBattle`
# ===========================================================================


class PickupDrawAttributionTests(unittest.TestCase):
    """bundle.deobfuscated.js:81210-81245.

    `runBattleScreen` destructures `runBattle`'s result (81210-81220) and only
    then evaluates `... && rng() < 0.1` (81223-81245). Both Pickup draws are
    therefore consumed after `runBattle` has returned, which is why the
    source's battle observer counts them outside the battle and the port's
    used to count them inside it.

    Every test below patches `battle_loop.run_battle` to a battle that draws
    nothing, so any draw seen across a call is attributable to the Pickup.
    """

    def _state(self):
        return engine.RunState(team=[_mon(263, level=10)], gen3_mode=True)

    def test_run_battle_draws_nothing_for_the_pickup(self):
        state = self._state()
        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win), \
             patch.object(engine.rng, "rng") as mock_rng:
            engine._run_battle(state, [_mon(16, level=5)])
        mock_rng.assert_not_called()
        self.assertEqual(state.items, [], "no Pickup item may appear inside runBattle")

    def test_run_battle_screen_draws_exactly_the_pickup(self):
        state = self._state()
        pool = data.get_passive_items()
        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win), \
             patch.object(engine.rng, "rng", side_effect=[0.05, 0.5]) as mock_rng:
            engine._run_battle_screen(state, [_mon(16, level=5)])
        self.assertEqual(mock_rng.call_count, 2, "the gate draw and the index draw")
        self.assertEqual(state.items, [pool[int(0.5 * len(pool))].id])

    def test_a_failed_gate_still_costs_exactly_one_draw_outside_the_battle(self):
        state = self._state()
        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win), \
             patch.object(engine.rng, "rng", side_effect=[0.5]) as mock_rng:
            engine._run_battle_screen(state, [_mon(16, level=5)])
        self.assertEqual(mock_rng.call_count, 1)
        self.assertEqual(state.items, [])

    def test_the_battle_window_an_observer_measures_excludes_the_pickup(self):
        """The property the sweep actually compares.

        `run_scenario.Runner._install_battle_recorder` and `driver.js`'s own
        `runBattle` wrapper both count draws across the call they wrap. This
        reproduces that measurement: wrapping `_run_battle` must see zero
        Pickup draws, while the surrounding `_run_battle_screen` call still
        consumes them from the same stream. Totals equal, attribution
        different -- which is exactly what finding F-C reported (js
        `rng_draws` 2, py 3, identical post-step RNG state).
        """
        state = self._state()
        seen = {"inside": 0}
        real_run_battle = engine._run_battle

        def observing(st, enemy_team):
            with patch.object(engine.rng, "rng", side_effect=_counting(seen)):
                return real_run_battle(st, enemy_team)

        def _counting(box):
            def draw():
                box["inside"] += 1
                return 0.5
            return draw

        total = {"n": 0}

        def counted():
            total["n"] += 1
            return 0.05 if total["n"] == 1 else 0.5

        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win), \
             patch.object(engine, "_run_battle", side_effect=observing), \
             patch.object(engine.rng, "rng", side_effect=counted):
            engine._run_battle_screen(state, [_mon(16, level=5)])

        self.assertEqual(seen["inside"], 0, "the battle window must contain no Pickup draw")
        self.assertEqual(total["n"], 2, "but the run still consumes both of them")
        self.assertEqual(len(state.items), 1)

    def test_pickup_still_precedes_copy_back(self):
        """Source order: the Pickup block (81223-81245) precedes the copy-back
        block (81278-81318). Splitting the two functions must not reorder
        them, so the Pickup still reads the PRE-copy-back team.
        """
        order = []
        state = self._state()
        real_pickup = engine._grant_gen3_zigzagoon_pickup
        real_copy = engine._copy_back_battle_result

        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win), \
             patch.object(engine, "_grant_gen3_zigzagoon_pickup",
                          side_effect=lambda st: (order.append("pickup"), real_pickup(st))[1]), \
             patch.object(engine, "_copy_back_battle_result",
                          side_effect=lambda *a, **k: (order.append("copy_back"),
                                                       real_copy(*a, **k))[1]), \
             patch.object(engine.rng, "rng", side_effect=[0.5]):
            engine._run_battle_screen(state, [_mon(16, level=5)])
        self.assertEqual(order, ["pickup", "copy_back"])

    def test_a_loss_runs_no_pickup_but_still_copies_back(self):
        """`BcF &&` is the first conjunct of the source's gate (81225), so a
        loss skips the Pickup entirely -- and must still not skip copy-back."""
        def quiet_loss(player_team, enemy_team, **kwargs):
            res = _quiet_win(player_team, enemy_team, **kwargs)
            res.player_won = False
            return res

        state = self._state()
        order = []
        real_copy = engine._copy_back_battle_result
        with patch.object(engine.battle_loop, "run_battle", side_effect=quiet_loss), \
             patch.object(engine, "_copy_back_battle_result",
                          side_effect=lambda *a, **k: (order.append("copy_back"),
                                                       real_copy(*a, **k))[1]), \
             patch.object(engine.rng, "rng") as mock_rng:
            engine._run_battle_screen(state, [_mon(16, level=5)])
        mock_rng.assert_not_called()
        self.assertEqual(order, ["copy_back"])

    def test_every_battle_call_site_goes_through_the_screen_wrapper(self):
        """The split is only faithful if nothing still calls the inner
        function directly. `_run_battle_screen` is the sole caller of
        `_run_battle` in the module; every node handler calls the wrapper.
        """
        import inspect
        src = inspect.getsource(engine)
        # The one legitimate inner call lives in `_run_battle_screen`.
        self.assertEqual(src.count("_run_battle(state, enemy_team)"), 1,
                         "exactly one internal call, inside _run_battle_screen")
        # Every OTHER `_run_battle(state, ...)` would be a node handler that
        # skipped the Pickup/copy-back step -- there must be none.
        stray = src.count("_run_battle(state, ") - src.count("_run_battle(state, enemy_team)")
        self.assertEqual(stray, 0,
                         "node handlers must call _run_battle_screen, not _run_battle")
        self.assertGreaterEqual(src.count("_run_battle_screen(state, "), 8)


# ===========================================================================
# F-D -- a Gen2 gym leader overrides the Nuzlocke level halving
# ===========================================================================


class Gen2GymLevelGainTests(unittest.TestCase):
    """`applyLevelGain`'s gain is `it !== null ? it : (iS ? 1 : getLevelGain())`
    (bundle.deobfuscated.js:56803), `iS` being `state.nuzlockeMode` (81325) and
    `getLevelGain` the constant 2 (56788-56790).

    `doBossNode` has two gym branches and they do not pass the same thing:

        Gen2          runBattleScreen(team, true, win, lose, name, [], 0x2)
                      (77780-77797)  -- 7 args, explicit gain 2
        Gen1/3/4      runBattleScreen(team, true, win, lose, name)
                      (77829-77843)  -- 5 args, gain falls through to Nuzlocke

    So Gen2 grants +2 even in Nuzlocke. The port applied `nuzlocke ? 1 : 2`
    to all four generations.
    """

    def _gain_for(self, *, gen2, nuzlocke):
        """Drive the real `_visit_boss` and capture what it hands
        `_after_battle`, which is the value `applyLevelGain` would receive."""
        eng = engine.Engine()
        state = eng.reset(seed=7, gen2_mode=gen2, nuzlocke_mode=nuzlocke)
        state = eng.step(engine.ChooseStarter(
            species_id=state.pending.options[0]["species_id"]))
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BOSS

        seen = {}
        real_after = engine._after_battle

        def capture(st, result, level_gain, **kw):
            seen["level_gain"] = level_gain
            return real_after(st, result, level_gain, **kw)

        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win), \
             patch.object(engine, "_after_battle", side_effect=capture):
            eng.step(engine.VisitNode(node_id=node.id))
        return seen["level_gain"]

    def test_a_gen2_gym_grants_two_levels_even_in_nuzlocke(self):
        self.assertEqual(self._gain_for(gen2=True, nuzlocke=True), 2)

    def test_a_gen2_gym_grants_two_levels_outside_nuzlocke_too(self):
        self.assertEqual(self._gain_for(gen2=True, nuzlocke=False), 2)

    def test_a_gen1_gym_still_halves_under_nuzlocke(self):
        """The control. Without it, "always 2" would pass the two tests above
        while silently breaking the branch that has no explicit argument."""
        self.assertEqual(self._gain_for(gen2=False, nuzlocke=True), 1)

    def test_a_gen1_gym_grants_two_levels_outside_nuzlocke(self):
        self.assertEqual(self._gain_for(gen2=False, nuzlocke=False), 2)

    def test_the_level_and_max_hp_actually_move_by_two(self):
        """The observable the retained F-D record compares. The finding's own
        numbers are level 16 / max HP 44 on the source versus level 15 / 42 in
        the port, so a fix that changed the argument without changing the
        outcome would not be a fix.
        """
        eng = engine.Engine()
        state = eng.reset(seed=7, gen2_mode=True, nuzlocke_mode=True)
        state = eng.step(engine.ChooseStarter(
            species_id=state.pending.options[0]["species_id"]))
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BOSS

        before_level = state.team[0].level
        before_max_hp = state.team[0].max_hp
        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win):
            state = eng.step(engine.VisitNode(node_id=node.id))

        self.assertEqual(state.team[0].level, before_level + 2)
        self.assertGreater(state.team[0].max_hp, before_max_hp)
        self.assertEqual(
            state.team[0].max_hp,
            map_gen.calc_hp(state.team[0].base_stats.hp, before_level + 2),
            "max HP must be recomputed at the NEW level, not merely incremented",
        )

    def _gauntlet_gain_for(self, *, gen2, nuzlocke):
        """Drive the real `_elite4_fight_step` and capture what the FIRST
        gauntlet fight hands `_after_battle`. This is the behavioural twin of
        `_gain_for`, aimed at the Elite Four instead of a gym."""
        eng = engine.Engine()
        state = eng.reset(seed=7, gen2_mode=gen2, nuzlocke_mode=nuzlocke)
        state = eng.step(engine.ChooseStarter(
            species_id=state.pending.options[0]["species_id"]))
        state.current_map = 8
        node = state.map.layers[-1][0]
        node.accessible = True

        seen = {}
        real_after = engine._after_battle

        def capture(st, result, level_gain, **kw):
            seen.setdefault("level_gain", level_gain)
            return real_after(st, result, level_gain, **kw)

        with patch.object(engine.battle_loop, "run_battle", side_effect=_quiet_win),              patch.object(engine, "_after_battle", side_effect=capture):
            eng.step(engine.VisitNode(node_id=node.id))
        return seen["level_gain"]

    def test_the_gen2_elite_four_still_halves_under_nuzlocke(self):
        """`doGen2Elite4` omits the argument (78379) exactly like `doElite4`
        (77871), so the gauntlet keeps the Nuzlocke rule. Pinned because the
        obvious over-broad fix -- "Gen2 always grants 2" -- would break it.

        M7 (F-I) NOTE ON THIS TEST'S OWN SHAPE. It used to assert
        `"gen2_mode" not in inspect.getsource(engine._elite4_fight_step)`.
        That string search was a proxy for "the gauntlet has no Gen2 LEVEL-GAIN
        special case", and it stopped being a faithful proxy once the gauntlet
        acquired a Gen2 branch for something else entirely -- `doGen2Elite4`
        resets `state["eliteIndex"] = 0x0` before `showWinScreen()` (78394)
        while `doElite4` does not (77887-77893). The evidence this test exists
        to protect is the LEVEL GAIN, so it is now asserted where it lives:
        behaviourally, by driving the real gauntlet across the (gen2, nuzlocke)
        matrix, and structurally, on the `level_gain` expression alone rather
        than on the whole function body. That is strictly stronger than the
        string search it replaces -- the old test could not have caught a Gen2
        gain special case introduced without the literal token `gen2_mode`.
        """
        import inspect
        src = inspect.getsource(engine._elite4_fight_step)
        self.assertIn("level_gain = 1 if state.nuzlocke_mode else 2", src)
        # No `level_gain` line may branch on the generation at all.
        for line in src.splitlines():
            if "level_gain" in line and not line.lstrip().startswith("#"):
                self.assertNotIn("gen2", line,
                                 "the gauntlet must not acquire a Gen2 LEVEL-GAIN special case")
        # The behaviour itself, over the full 2x2 -- including the two
        # controls, without which "the gauntlet always grants 2" would pass.
        self.assertEqual(self._gauntlet_gain_for(gen2=True, nuzlocke=True), 1)
        self.assertEqual(self._gauntlet_gain_for(gen2=False, nuzlocke=True), 1)
        self.assertEqual(self._gauntlet_gain_for(gen2=True, nuzlocke=False), 2)
        self.assertEqual(self._gauntlet_gain_for(gen2=False, nuzlocke=False), 2)


# ===========================================================================
# F-E -- an evolved member is named by the EVOLUTION record, not by the dex
# ===========================================================================


class EvolvedNameTests(unittest.TestCase):
    """Found by the M7-combined goal-directed sweep, which reached a Nuzlocke
    Gen1 run that evolved a Porygon2 at step 54 (reproducer
    `route-oracle/findings/M7-divergence-hunt_nuzlocke_gen1_0294.json`).

    Both source evolution paths take the new name from the evolution record:

        checkAndEvolveTeam  `iS["name"] = B2B["name"]`   (70651-70652)
        applyEvolution      `B["name"]  = iS["name"]`    (79801-79802)

    `fetchPokemonById` is consulted in both, but only for `types` and
    `baseStats` (70658, 79812). The port was reading the NAME off the fetched
    species too, which agrees for almost every line and disagrees for exactly
    the case below.
    """

    #: The species whose dex name and evolution-record name really differ.
    FROM_ID, INTO_ID = 233, 474

    def test_the_two_names_really_differ_for_this_line(self):
        """The premise. Without a line where they disagree, this whole class
        would pass against either implementation."""
        evo = data.get_evolutions()[self.FROM_ID]
        self.assertEqual(evo.into, self.INTO_ID)
        dex_name = data.get_pokedex()[self.INTO_ID].name
        self.assertNotEqual(evo.name, dex_name)
        self.assertEqual(evo.name, "Porygon-Z")
        self.assertEqual(dex_name, "Porygon-z")

    def test_evolving_uses_the_evolution_records_name(self):
        state = engine.RunState(team=[_mon(self.FROM_ID, level=40)])
        engine._maybe_evolve_one(state, 0, source="todo")
        self.assertEqual(state.team[0].species_id, self.INTO_ID)
        self.assertEqual(state.team[0].name,
                         data.get_evolutions()[self.FROM_ID].name)

    def test_the_forced_moon_stone_path_uses_it_too(self):
        """`applyEvolution` (force=True) is a different source function with a
        different HP formula, but the same name assignment."""
        state = engine.RunState(team=[_mon(self.FROM_ID, level=5)])
        engine._maybe_evolve_one(state, 0, source="item", force=True)
        self.assertEqual(state.team[0].name, "Porygon-Z")

    def test_types_and_base_stats_still_come_from_the_dex(self):
        """Only the NAME moved. `fetchPokemonById` is still what supplies
        `types`/`baseStats` in both source paths, so taking those from the
        evolution record instead would be a different bug."""
        state = engine.RunState(team=[_mon(self.FROM_ID, level=40)])
        engine._maybe_evolve_one(state, 0, source="todo")
        target = data.get_pokedex()[self.INTO_ID]
        self.assertEqual(state.team[0].types, target.types)
        self.assertEqual(state.team[0].base_stats, target.base_stats)

    def test_a_branching_evolution_is_named_by_the_branch_it_chose(self):
        """The other caller. `showBranchingChoice` returns one BRANCH record
        and `applyEvolution` names the mon from that same record, so choosing
        branch `k` must produce branch `k`'s name -- not the dex name, and not
        another branch's."""
        branching = data.get_branching_evolutions()
        from_id, branches = next(
            (sid, bs) for sid, bs in branching.items() if len(bs) > 1)
        for index, branch in enumerate(branches):
            with self.subTest(branch=branch.name):
                state = engine.RunState(team=[_mon(from_id, level=50)])
                # `source="item"` is Moon Stone's path: resolving it returns
                # straight to the map instead of resuming a `_todo` queue this
                # hand-built state does not have.
                raised = engine._maybe_evolve_one(state, 0, source="item", force=True)
                self.assertTrue(raised, "a branching line must raise a choice")
                engine._resolve_evolution_choice(
                    state, engine.SelectOption(index=index))
                self.assertEqual(state.team[0].species_id, branch.into)
                self.assertEqual(state.team[0].name, branch.name)


if __name__ == "__main__":
    unittest.main()
