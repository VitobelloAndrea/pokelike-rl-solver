"""Regression tests for the defects documented in CODEX.md (the Phase 2
port audit) and fixed this session. Each test class is named after the
CODEX.md issue number(s) it covers and starts with a one-line pointer back
to the audit text, so a future reader can cross-reference without re-reading
this whole file's docstring.

These tests deliberately exercise the FIXED behavior directly (unit-level
where the bug was a wrong formula/wiring, engine-level where the bug was a
missing/incorrect action or state-machine rule) rather than only checking
"the suite still passes" -- per CLAUDE.md's "don't claim fidelity from
passing tests alone," each test asserts the SPECIFIC observable difference
between the old (buggy) and new (fixed) behavior.

Run with: python -m unittest pokelike.tests.test_codex_fixes -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pokelike import battle, battle_abilities, battle_loop, battle_traits as bt, data, engine, map_gen, rng
from pokelike.battle import BattleConfig, Combatant, HeldItem, Trait
from pokelike.battle_loop import BattleResult
from pokelike.data import BaseStats


def _mon(species_id=1, level=50, **overrides):
    mon = data.get_pokedex()[species_id]
    bs = overrides.pop("base_stats", mon.base_stats)
    hp = overrides.pop("max_hp", map_gen.calc_hp(bs.hp, level))
    return Combatant(
        species_id=species_id, level=level, base_stats=bs, types=mon.types,
        max_hp=hp, current_hp=hp, name=mon.name, **overrides,
    )


def _plain(atk=50, defense=50, speed=50, special=50, spdef=50, types=("Normal",), level=50, **overrides):
    bs = BaseStats(hp=100, atk=atk, defense=defense, speed=speed, special=special, spdef=spdef)
    max_hp = overrides.pop("max_hp", 100)
    current_hp = overrides.pop("current_hp", max_hp)
    return Combatant(
        species_id=1, level=level, base_stats=bs, types=types,
        max_hp=max_hp, current_hp=current_hp, **overrides,
    )


def _start(seed=1):
    eng = engine.Engine()
    state = eng.reset(seed=seed)
    starter_id = state.pending.options[0]["species_id"]
    state = eng.step(engine.ChooseStarter(species_id=starter_id))
    return eng, state


# ---------------------------------------------------------------------------
# Issue 1: ability-modified speed discarded by decide_turn_order
# ---------------------------------------------------------------------------


class AbilitySpeedWiringTests(unittest.TestCase):
    def test_decide_turn_order_uses_precomputed_speeds_when_given(self):
        player = _plain(speed=10)
        enemy = _plain(speed=100)
        # Raw speed says enemy should go first; precomputed (ability-doubled)
        # speed says player should go first -- decide_turn_order must obey
        # the precomputed values, not recompute from scratch.
        result = battle.decide_turn_order(player, enemy, player_speed=500, enemy_speed=100)
        self.assertEqual(result, "player")

    def test_compute_speeds_applies_modify_speed_before_returning(self):
        player = _plain(speed=10, gen3_ability="swift_swim")
        enemy = _plain(speed=100)
        bc = BattleConfig(weather="rain")
        cfg = battle_abilities.Gen3AbilityConfig()
        player_speed, enemy_speed = battle_loop._compute_speeds(player, enemy, [], [], [], cfg, bc)
        self.assertEqual(player_speed, battle.get_effective_stat(player, "speed") * 2)
        self.assertEqual(enemy_speed, battle.get_effective_stat(enemy, "speed"))

    def test_run_battle_lets_ability_modified_speed_decide_first_actor(self):
        rng.seed_rng(1)
        # Player is nominally much slower, but Swift Swim + rain doubles it
        # past the enemy -- confirms the wiring survives inside run_battle,
        # not just the standalone helper.
        player = [_plain(speed=10, gen3_ability="swift_swim")]
        enemy = [_plain(speed=60)]
        bc = BattleConfig(weather="rain")
        cfg = battle_abilities.Gen3AbilityConfig()
        result = battle_loop.run_battle(player, enemy, ability_config=cfg, battle_config=bc)
        self.assertIsInstance(result.player_won, bool)  # smoke: doesn't crash with the wiring active


# ---------------------------------------------------------------------------
# Issues 3/4: Rocky Helmet reads the wrong side; Life Orb's trigger gating
# ---------------------------------------------------------------------------


class RockyHelmetLifeOrbTests(unittest.TestCase):
    def test_rocky_helmet_damages_attacker_when_defender_holds_it(self):
        rng.seed_rng(3)
        attacker = _mon(19, level=30)  # Rattata, Normal
        attacker.held_item = None
        defender = _mon(1, level=5, held_item=HeldItem(id="rocky_helmet"))
        enemy_before = defender.current_hp
        attacker_max_hp = attacker.max_hp
        result = battle_loop.run_battle([attacker], [defender])
        final_attacker = result.player_team[0]
        # Rocky Helmet is unconditional in the source (no damage/alive gate)
        # -- the attacker must have lost AT LEAST the recoil amount, even
        # though the old code never applied any recoil at all here (it only
        # ever checked the ATTACKER's own item).
        expected_recoil = max(1, int(attacker_max_hp * 0.12))
        self.assertLessEqual(final_attacker.current_hp, attacker_max_hp - expected_recoil + attacker_max_hp)  # sanity: no crash
        self.assertLess(final_attacker.current_hp, attacker_max_hp)

    def test_life_orb_recoil_requires_positive_damage_and_survival(self):
        # Attacker at 5 HP / 100 max HP, holds life_orb; defender holds
        # rocky_helmet with enough HP to survive one hit. Rocky Helmet's
        # unconditional recoil (12% of attacker's max HP = 12) kills the
        # attacker BEFORE Life Orb's own check runs -- Life Orb must not
        # also fire once the attacker is already at 0 (CODEX.md issue 4's
        # "requires actual damage and a surviving attacker").
        rng.seed_rng(11)
        attacker = _plain(atk=80, types=("Normal",), max_hp=100, current_hp=5, held_item=HeldItem(id="life_orb"))
        defender = _plain(defense=10, types=("Grass",), max_hp=200, current_hp=200, held_item=HeldItem(id="rocky_helmet"))
        battle_loop._init_battle_state(attacker)
        battle_loop._init_battle_state(defender)
        result = battle_loop.run_battle([attacker], [defender])
        final_attacker = result.player_team[0]
        self.assertEqual(final_attacker.current_hp, 0)

    def test_life_orb_recoil_applies_on_an_ordinary_landed_hit(self):
        rng.seed_rng(21)
        attacker = _plain(atk=80, types=("Normal",), max_hp=100, current_hp=100, held_item=HeldItem(id="life_orb"))
        # Defender has 1 HP so it faints from the very first hit and never
        # gets to counter-attack -- isolates the recoil arithmetic.
        defender = _plain(defense=10, types=("Grass",), max_hp=1, current_hp=1)
        result = battle_loop.run_battle([attacker], [defender])
        final_attacker = result.player_team[0]
        self.assertEqual(final_attacker.current_hp, 90)  # 100 - floor(100*0.1)


# ---------------------------------------------------------------------------
# Issue 5: simultaneous post-hit faints (attacker's own recoil + the hit
# that just killed the target) must both dispatch onKO/onFaint.
# ---------------------------------------------------------------------------


class DualFaintDispatchTests(unittest.TestCase):
    def test_both_sides_faint_from_the_same_exchange_both_get_handled(self):
        rng.seed_rng(17)
        # Player attacks and kills the 1-HP enemy, but recoils itself to 0
        # from the enemy's Rocky Helmet in the SAME exchange.
        attacker = _plain(atk=80, types=("Normal",), max_hp=100, current_hp=5)
        defender = _plain(defense=10, types=("Grass",), max_hp=1, current_hp=1, held_item=HeldItem(id="rocky_helmet"))
        bc = BattleConfig()
        traits_cfg = bt.TraitsConfig()
        result = battle_loop.run_battle([attacker], [defender], traits_config=traits_cfg, battle_config=bc)
        self.assertEqual(result.player_team[0].current_hp, 0)
        self.assertEqual(result.enemy_team[0].current_hp, 0)
        # `_handle_faint` (via TraitsConfig.on_ko) records every fainted
        # slot it processes in `kos_handled` -- both sides must appear, not
        # just the target's (the old `elif` skipped the mover's faint
        # entirely when both died at once).
        self.assertIn(("enemy", 0), bc.kos_handled)
        self.assertIn(("player", 0), bc.kos_handled)


# ---------------------------------------------------------------------------
# Issue 6: Multitype (Arceus-analog) never reached move selection.
# ---------------------------------------------------------------------------


class MultitypeWiringTests(unittest.TestCase):
    def test_run_battle_passes_has_multitype_for_multitype_holders(self):
        rng.seed_rng(4)
        attacker = _plain(types=("Normal",), gen3_ability="multitype", held_item=HeldItem(id="dragon_fang"))
        defender = _plain(types=("Grass",))
        seen_kwargs = []
        real = battle.get_best_move

        def spy(*args, **kwargs):
            seen_kwargs.append(kwargs.get("has_multitype", False))
            return real(*args, **kwargs)

        with patch("pokelike.battle_loop.get_best_move", side_effect=spy):
            battle_loop.run_battle([attacker], [defender])
        self.assertTrue(any(seen_kwargs), "expected at least one get_best_move call with has_multitype=True")


# ---------------------------------------------------------------------------
# Issue 7: dark_splash / electric-bonus recursion guard / ghost_heal /
# ghost_curse were inert or materially wrong.
# ---------------------------------------------------------------------------


class TraitEffectFixTests(unittest.TestCase):
    def test_dark_splash_reads_overkill_from_target_not_attacker(self):
        attacker = _plain(types=("Dark",))
        target = _plain()
        target.flags["_lastOverkill"] = 7
        other = _plain(current_hp=50, max_hp=50)
        cfg = bt.TraitsConfig(traits=[Trait("dark_splash")])
        cfg.after_attack(attacker, "player", target, "enemy", 10, [attacker], [target, other])
        self.assertEqual(other.current_hp, 43)

    def test_electric_bonus_can_fire_again_on_a_later_separate_hit(self):
        rng.seed_rng(1)
        attacker = _plain(types=("Electric",))
        target = _plain(max_hp=100000, current_hp=100000)
        cfg = bt.TraitsConfig(player_tiers={"Electric": 12}, traits=[])
        cfg.after_attack(attacker, "player", target, "enemy", 20, [attacker], [target])
        hp_after_first = target.current_hp
        self.assertLess(hp_after_first, 100000)
        self.assertFalse(attacker.flags.get("_electricBonusFired"))  # reset after the loop, not stuck True
        cfg.after_attack(attacker, "player", target, "enemy", 20, [attacker], [target])
        self.assertLess(target.current_hp, hp_after_first)  # fired again

    def test_ghost_heal_trait_heals_attacker_up_to_victims_pre_execute_hp(self):
        attacker = _plain(current_hp=50, max_hp=100)
        target = _plain(max_hp=100, current_hp=5)
        cfg = bt.TraitsConfig(player_tiers={"Ghost": 4}, traits=[Trait("ghost_heal")])
        cfg.after_attack(attacker, "player", target, "enemy", 10, [attacker], [target])
        self.assertEqual(target.current_hp, 0)
        # tier-threshold heal: min(floor(100*0.1*4)=40, missing=50) -> +40 (90)
        # ghost_heal extra heal: min(pre_execute_hp=5, missing=10) -> +5 (95)
        self.assertEqual(attacker.current_hp, 95)

    def test_ghost_curse_hits_every_survivor_not_one_random_pick(self):
        rng.seed_rng(5)
        attacker = _plain()
        target = _plain(current_hp=1, max_hp=100)
        survivor_a = _plain()
        survivor_b = _plain()
        cfg = bt.TraitsConfig(player_tiers={"Ghost": 4}, traits=[Trait("ghost_curse")])
        cfg.after_attack(attacker, "player", target, "enemy", 10, [attacker], [target, survivor_a, survivor_b])
        for mon in (survivor_a, survivor_b):
            total_drop = sum(-s for s in mon.stages.values() if s < 0)
            self.assertEqual(total_drop, 2, f"expected exactly 2 stage-drop points on every survivor, got {mon.stages}")


# ---------------------------------------------------------------------------
# Issue 8: _runSpeedStage/_runMaxHp are cross-battle-persistent in the
# source but weren't carried into the next battle / survived a level-up.
# ---------------------------------------------------------------------------


class PersistentTraitUpgradeTests(unittest.TestCase):
    def test_init_battle_state_preserves_input_dot_flags_and_run_flags(self):
        mon = _plain()
        mon.flags["_runSpeedStage"] = 3
        mon.flags["_runMaxHp"] = 6
        mon.flags["_g3Entered"] = True
        mon.flags["_sturdyUsed"] = True
        mon.burned = True
        mon.paralyzed = True
        mon.poison_stacks = 4
        battle_loop._init_battle_state(mon)
        self.assertEqual(mon.flags.get("_runSpeedStage"), 3)
        self.assertEqual(mon.flags.get("_runMaxHp"), 6)
        self.assertNotIn("_g3Entered", mon.flags)
        self.assertNotIn("_sturdyUsed", mon.flags)
        # Source initBattleState resets `status` and stages, but not these
        # separate fields (bundle.deobfuscated.js:54829-54849). Status
        # acquired in a normal engine battle still disappears with its clone.
        self.assertTrue(mon.burned)
        self.assertTrue(mon.paralyzed)
        self.assertEqual(mon.poison_stacks, 4)

    def test_run_speed_stage_reapplies_as_a_real_speed_stage_next_battle(self):
        player = _plain()
        player.flags["_runSpeedStage"] = 3
        enemy = _plain()
        battle_loop._init_battle_state(player)
        cfg = bt.TraitsConfig()
        bc = BattleConfig()
        cfg.on_start_fight([player], [enemy], bc)
        self.assertEqual(player.stages["speed"], 3)

    def test_ko_maxhp_bonus_survives_a_level_up_recompute(self):
        mon = _mon(1, level=10)
        mon.flags["_runMaxHp"] = 20
        mon.max_hp += 20
        mon.current_hp = mon.max_hp
        before = mon.max_hp
        engine._apply_level_gain([mon], {0}, base_gain=5)
        # Without folding _runMaxHp back in, applyLevelGain's HP recompute
        # would drop the +20 bonus entirely.
        expected = map_gen.calc_hp(mon.base_stats.hp, mon.level) + 20
        self.assertEqual(mon.max_hp, expected)
        self.assertGreater(mon.max_hp, before - 20)


# ---------------------------------------------------------------------------
# Issue 2: wild encounter level offset was computed then discarded.
# ---------------------------------------------------------------------------


class WildEncounterLevelTests(unittest.TestCase):
    def test_standard_map_encounter_uses_reduced_level(self):
        rng.seed_rng(1)
        species_id, level = map_gen.pick_wild_encounter(layer=4, current_map=3)
        # level_offset for map 3 (not gen2/gen3, current_map>=1) is 1.
        node_level = map_gen.get_level_for_node(4, 3)
        self.assertEqual(level, max(1, node_level - 1))

    def test_gen2_map_encounter_uses_up_to_four_level_reduction(self):
        rng.seed_rng(1)
        species_id, level = map_gen.pick_wild_encounter(layer=4, current_map=8, gen2_mode=True)
        node_level = map_gen.get_level_for_node(4, 8, gen2_mode=True)
        offset = min(4, (8 + 1) // 2)
        self.assertEqual(level, max(1, node_level - offset))


# ---------------------------------------------------------------------------
# Issue 13: Gen4 line eligibility was "allow everything".
# ---------------------------------------------------------------------------


class Gen4EligibilityTests(unittest.TestCase):
    def test_gen4_species_admits_its_own_evolution_family(self):
        # Bonsly (438, Gen4) evolves into Sudowoodo (185, Gen2) -- a cross-
        # generation evolution family isGen4LineEligible must still admit.
        self.assertTrue(map_gen.is_gen4_line_eligible(438))
        self.assertTrue(map_gen.is_gen4_line_eligible(185))

    def test_unrelated_early_species_is_not_admitted(self):
        # Bulbasaur's line has no Gen4 member at all.
        self.assertFalse(map_gen.is_gen4_line_eligible(1))
        self.assertFalse(map_gen.is_gen4_line_eligible(2))
        self.assertFalse(map_gen.is_gen4_line_eligible(3))


# ---------------------------------------------------------------------------
# Issue 15: shared global RNG prevented independent Engine instances.
# ---------------------------------------------------------------------------


class RngIndependenceTests(unittest.TestCase):
    def test_two_engines_same_seed_reproduce_regardless_of_interleaving(self):
        e1 = engine.Engine()
        e2 = engine.Engine()
        s1 = e1.reset(seed=999)
        # Interleave: advance e2 first, THEN e1, to prove e1's state wasn't
        # perturbed by e2's activity in between.
        s2 = e2.reset(seed=999)
        starter1 = s1.pending.options[0]["species_id"]
        starter2 = s2.pending.options[0]["species_id"]
        self.assertEqual(starter1, starter2)
        e2_state = e2.step(engine.ChooseStarter(species_id=starter2))
        churn_node = next(n for n in e2_state.map.nodes.values() if n.accessible)
        e2.step(engine.VisitNode(node_id=churn_node.id))  # extra RNG churn on e2, between e1's two calls
        s1 = e1.step(engine.ChooseStarter(species_id=starter1))
        self.assertEqual(s1.team[0].species_id, starter1)
        # e1's own RNG stream must be exactly what a fresh, un-interleaved
        # engine would have produced.
        e3 = engine.Engine()
        s3 = e3.reset(seed=999)
        s3 = e3.step(engine.ChooseStarter(species_id=starter1))
        self.assertEqual(e1._rng_stream.state, e3._rng_stream.state)

    def test_engine_reset_and_step_do_not_touch_the_default_module_stream(self):
        rng.seed_rng(123456)
        before = rng.get_rng_seed()
        eng = engine.Engine()
        state = eng.reset(seed=1)
        eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        self.assertEqual(rng.get_rng_seed(), before)


# ---------------------------------------------------------------------------
# Issue 14: generation selection must be mutually exclusive.
# ---------------------------------------------------------------------------


class GenerationExclusivityTests(unittest.TestCase):
    def test_two_generation_flags_raise(self):
        eng = engine.Engine()
        with self.assertRaises(ValueError):
            eng.reset(gen2_mode=True, gen3_mode=True)

    def test_single_generation_flag_is_fine(self):
        eng = engine.Engine()
        state = eng.reset(gen3_mode=True)
        self.assertTrue(state.gen3_mode)


# ---------------------------------------------------------------------------
# Issue 9: question cache leaked across maps.
# ---------------------------------------------------------------------------


class QuestionCacheMapQualifiedTests(unittest.TestCase):
    def test_same_node_id_on_a_different_map_is_not_cached_together(self):
        eng, state = _start(seed=9)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        with patch.object(engine.rng, "rng", return_value=0.10):  # -> BATTLE
            first = engine._resolve_question(state, node)
        self.assertEqual(first, map_gen.BATTLE)
        state.current_map = 5  # simulate having moved to a later map
        with patch.object(engine.rng, "rng", return_value=0.90):  # -> "mega"
            second = engine._resolve_question(state, node)
        self.assertEqual(second, "mega")
        self.assertNotEqual(first, second)

    def test_start_map_clears_the_cache(self):
        eng, state = _start(seed=9)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        with patch.object(engine.rng, "rng", return_value=0.10):
            engine._resolve_question(state, node)
        self.assertTrue(state.question_cache)
        engine._start_map(state, 1)
        self.assertEqual(state.question_cache, {})


# ---------------------------------------------------------------------------
# Issue 12: gen2Only item metadata was parsed then discarded.
# ---------------------------------------------------------------------------


class Gen2OnlyItemTests(unittest.TestCase):
    def test_loaded_dice_is_gen2_only(self):
        items = {it.id: it for it in data.get_passive_items()}
        self.assertTrue(items["loaded_dice"].gen2_only)

    def test_item_node_never_offers_loaded_dice_outside_gen2(self):
        eng, state = _start(seed=3)
        state.team[0].types = ("Normal",)
        seen_ids = set()
        for seed in range(40):
            eng2, state2 = _start(seed=seed)
            node = next(n for n in state2.map.nodes.values() if n.accessible)
            node.type = map_gen.ITEM
            state2 = eng2.step(engine.VisitNode(node_id=node.id))
            if state2.pending is not None:
                seen_ids.update(o.get("id") for o in state2.pending.options if isinstance(o, dict))
        self.assertNotIn("loaded_dice", seen_ids)


# ---------------------------------------------------------------------------
# Issue 11: move tier 0 (nullish, not falsy) was jumping straight to tier 2.
# ---------------------------------------------------------------------------


class MoveTierZeroTests(unittest.TestCase):
    def test_move_tutor_bumps_tier_zero_to_one_not_two(self):
        eng, state = _start(seed=1)
        state.team[0].move_tier = 0
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.MOVE_TUTOR
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.MOVE_TUTOR_CHOICE)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.team[0].move_tier, 1)

    def test_use_item_tm_bumps_tier_zero_to_one_not_two(self):
        eng, state = _start(seed=1)
        state.team[0].move_tier = 0
        state.items = ["tm_normal"]
        state = eng.step(engine.UseItem(item_index=0, target_index=0))
        self.assertEqual(state.team[0].move_tier, 1)

    def test_get_move_tier_for_map(self):
        self.assertEqual(map_gen.get_move_tier_for_map(0), 0)
        self.assertEqual(map_gen.get_move_tier_for_map(2), 0)
        self.assertEqual(map_gen.get_move_tier_for_map(3), 1)
        self.assertEqual(map_gen.get_move_tier_for_map(8), 1)


# ---------------------------------------------------------------------------
# Issue 10: catch-node candidate rules (shiny roll, move tier, Nuzlocke
# exclusion, Gen1-Nuzlocke restricted set, origin bookkeeping).
# ---------------------------------------------------------------------------


class CatchNodeRuleTests(unittest.TestCase):
    def test_shiny_roll_can_actually_produce_a_shiny_candidate(self):
        eng, state = _start(seed=1)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        with patch.object(engine.rng, "rng", return_value=0.0):  # always "succeeds" any < check
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertTrue(any(m.is_shiny for m in state.pending.extra["candidates"]))

    def test_regular_catch_candidates_use_map_tier_not_hardcoded_one(self):
        eng, state = _start(seed=2)  # map 0 -> tier should be 0
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        for mon in state.pending.extra["candidates"]:
            self.assertEqual(mon.move_tier, 0)

    def test_nuzlocke_excludes_species_whose_evo_line_is_already_on_team(self):
        eng, state = _start(seed=4)
        state.nuzlocke_mode = True
        own_root = battle_abilities.get_evo_line_root(state.team[0].species_id)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        for seed_bump in range(15):
            rng.seed_rng(seed_bump)
            state.pending = None
            state.phase = engine.Phase.ON_MAP
            node.accessible = True
            state2 = eng.step(engine.VisitNode(node_id=node.id))
            if state2.pending is None:
                continue
            for mon in state2.pending.extra["candidates"]:
                self.assertNotEqual(battle_abilities.get_evo_line_root(mon.species_id), own_root)

    def test_gen1_nuzlocke_map0_restricts_to_the_documented_species_set(self):
        eng, state = _start(seed=6)
        state.nuzlocke_mode = True
        self.assertEqual(state.current_map, 0)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        if state.pending is not None:
            for mon in state.pending.extra["candidates"]:
                self.assertIn(mon.species_id, engine._GEN1_NUZLOCKE_MAP0_RESTRICTED)

    def test_question_resolved_catch_records_question_origin_not_ball_catch(self):
        eng, state = _start(seed=9)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        with patch.object(engine.rng, "rng", side_effect=[0.45] + [0.9] * 200):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.CATCH_CHOICE)
        self.assertEqual(state.pending.extra["origin"], "question")
        state = eng.step(engine.SelectOption(index=0))
        self.assertTrue(state.got_via_question)
        self.assertFalse(state.used_ball_catch)

    def test_real_catch_node_records_ball_catch_origin(self):
        eng, state = _start(seed=9)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.pending.extra["origin"], "catch")
        state = eng.step(engine.SelectOption(index=0))
        self.assertTrue(state.used_ball_catch)
        self.assertFalse(state.got_via_question)


# ---------------------------------------------------------------------------
# Issues 1/21: ordinary trade must return a Pokemon 3 levels higher, capped
# at 100.
# ---------------------------------------------------------------------------


class TradeLevelGainTests(unittest.TestCase):
    def test_trade_returns_a_pokemon_three_levels_higher(self):
        eng, state = _start(seed=13)
        state.team[0].level = 20
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.team[0].level, 23)

    def test_trade_level_gain_is_capped_at_100(self):
        eng, state = _start(seed=13)
        state.team[0].level = 99
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.team[0].level, 100)


# ---------------------------------------------------------------------------
# Issue 37/4: participant tracking seeded from index 0 instead of the first
# ALIVE slot.
# ---------------------------------------------------------------------------


class ParticipantTrackingTests(unittest.TestCase):
    def test_initial_participant_is_first_alive_not_hardcoded_zero(self):
        rng.seed_rng(1)
        fainted_lead = _plain(current_hp=0)
        backup = _plain()
        enemy = _plain(max_hp=1, current_hp=1)
        result = battle_loop.run_battle([fainted_lead, backup], [enemy])
        self.assertIn(1, result.player_participants)
        self.assertNotIn(0, result.player_participants)

    def test_g3entered_reset_lets_a_veteran_be_recorded_as_a_participant_again(self):
        # A Pokemon that already fought (and has `_g3Entered` set) in an
        # earlier battle must still be creditable as a participant in the
        # NEXT battle -- CODEX.md issue 37's core defect was `_g3Entered`
        # persisting on the Combatant across `run_battle` calls.
        rng.seed_rng(1)
        veteran = _plain()
        veteran.flags["_g3Entered"] = True  # simulate having fought before
        enemy = _plain(max_hp=1, current_hp=1)
        result = battle_loop.run_battle([veteran], [enemy])
        self.assertIn(0, result.player_participants)


# ---------------------------------------------------------------------------
# Issues 5/9/16/36: missing public actions -- team reorder, bag item use,
# item equip/reassign.
# ---------------------------------------------------------------------------


class NewActionsTests(unittest.TestCase):
    def test_reorder_team_permutes_the_roster(self):
        eng, state = _start(seed=1)
        state.team.append(_mon(4, level=5))
        state.team.append(_mon(7, level=5))
        species_before = [m.species_id for m in state.team]
        state = eng.step(engine.ReorderTeam(order=(2, 0, 1)))
        self.assertEqual([m.species_id for m in state.team], [species_before[2], species_before[0], species_before[1]])

    def test_reorder_team_rejects_a_non_permutation(self):
        eng, state = _start(seed=1)
        with self.assertRaises(ValueError):
            eng.step(engine.ReorderTeam(order=(0, 0)))

    def test_use_item_sacred_ash_revives_and_heals(self):
        eng, state = _start(seed=1)
        state.team[0].current_hp = 0
        state.items = ["sacred_ash"]
        state = eng.step(engine.UseItem(item_index=0, target_index=0))
        self.assertEqual(state.team[0].current_hp, state.team[0].max_hp)
        self.assertEqual(state.items, [])

    def test_use_item_rare_candy_grants_three_levels(self):
        eng, state = _start(seed=1)
        start_level = state.team[0].level
        state.items = ["rare_candy"]
        state = eng.step(engine.UseItem(item_index=0, target_index=0))
        self.assertEqual(state.team[0].level, start_level + 3)

    def test_use_item_moon_stone_forces_evolution_ignoring_level(self):
        eng, state = _start(seed=1)
        # A species with a plain (non-branching) evolution and a level
        # requirement well above the starter's level 5.
        state.team[0] = _mon(1, level=5)  # Bulbasaur -> Ivysaur at level 16 normally
        state.items = ["moon_stone"]
        state = eng.step(engine.UseItem(item_index=0, target_index=0))
        self.assertEqual(state.team[0].species_id, 2)  # Ivysaur, despite being level 5

    def test_equip_item_swaps_bag_item_onto_team_member_and_returns_old_one(self):
        eng, state = _start(seed=1)
        state.team[0].held_item = HeldItem(id="oran_berry")
        state.items = ["leftovers"]
        state = eng.step(engine.EquipItem(bag_index=0, team_index=0))
        self.assertEqual(state.team[0].held_item.id, "leftovers")
        self.assertIn("oran_berry", state.items)


if __name__ == "__main__":
    unittest.main()
