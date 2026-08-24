"""Regression tests for CODEX P0.7 (Gen3 Pickup reward paths) and P0.8
(`runBattleScreen` pre-battle player-team transformations).

Both defects were previously undocumented-as-two-separate-paths (P0.7) or
entirely unported (P0.8) -- see CODEX.md's P0.7/P0.8 entries for the fixed
write-up. Every test here either exercises the FIXED behavior directly
against a scripted `rng()` sequence (proving exact draw count/order, not
just "an item eventually appears"), or proves a specific persistent-state
leak that the old code (no transform layer at all) could not have leaked
because it never built the transform in the first place -- these tests
would have passed vacuously against the old code for the wrong reason, so
each one asserts the SPECIFIC transformed value, not just "no crash".

M7-COMBINED (F-C) re-pointed every call below from `engine._run_battle` to
`engine._run_battle_screen`. Nothing about what these tests ASSERT changed;
the function that models `runBattleScreen`'s post-battle sequence -- the Gen3
Pickup roll and the copy-back -- simply has that name now, because the source
draws Pickup's `rng()` in `runBattleScreen` (bundle.deobfuscated.js:81223-
81245) and not in `runBattle`, and the port had folded the two together. Left
pointing at the inner `_run_battle`, the four "does not leak after a win"
tests in `NoLeakThroughRealBattleTests` would have kept passing for the wrong
reason: with no copy-back running at all, nothing can leak through it.

Run with: python -m unittest pokelike.tests.test_pickup_and_prebattle -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pokelike import battle, battle_loop, data, engine, map_gen, rng
from pokelike.battle import Combatant, HeldItem, Trait
from pokelike.battle_loop import BattleResult


def _mon(species_id=1, level=50, **overrides):
    mon = data.get_pokedex()[species_id]
    bs = overrides.pop("base_stats", mon.base_stats)
    hp = overrides.pop("max_hp", map_gen.calc_hp(bs.hp, level))
    return Combatant(
        species_id=species_id, level=level, base_stats=bs, types=mon.types,
        max_hp=hp, current_hp=hp, name=mon.name, **overrides,
    )


def _state(**overrides) -> engine.RunState:
    return engine.RunState(**overrides)


def _echo_run_battle(player_team, enemy_team, **kwargs):
    """Stand-in for `battle_loop.run_battle` that clones its input teams
    (matching the source's own inner clone boundary) and reports a win
    with every player slot as a participant -- used to isolate
    `_run_battle`'s OWN wiring/ordering without depending on real combat
    math, per this task's "mock battle arithmetic only when isolating
    engine wrapper ordering" instruction. Distinct clone objects (not the
    same references) so copy-back tests can't pass by aliasing accident.
    """
    p_clone = [battle_loop.clone_combatant(m) for m in player_team]
    e_clone = [battle_loop.clone_combatant(m) for m in enemy_team]
    return BattleResult(
        player_won=True,
        player_team=p_clone,
        enemy_team=e_clone,
        player_participants=set(range(len(p_clone))),
        rounds=1,
    )


def _echo_loss_run_battle(player_team, enemy_team, **kwargs):
    p_clone = [battle_loop.clone_combatant(m) for m in player_team]
    for m in p_clone:
        m.current_hp = 0
    e_clone = [battle_loop.clone_combatant(m) for m in enemy_team]
    return BattleResult(
        player_won=False,
        player_team=p_clone,
        enemy_team=e_clone,
        player_participants=set(),
        rounds=1,
    )


# ---------------------------------------------------------------------------
# P0.8: applyMegaEvolution / syncMegaForm (bundle.deobfuscated.js:86461-86515)
# ---------------------------------------------------------------------------


class MegaStoneDataTests(unittest.TestCase):
    def test_28_mega_stones_extracted(self):
        self.assertEqual(len(data.get_mega_stones()), 28)

    def test_every_stone_id_species_and_form_sprite_matches_source_order(self):
        # MEGA_STONES declarations and array order, source lines
        # 48071-48602. `makeMegaStoneItem` turns each formId into the exact
        # path asserted below (86443-86458).
        expected = [
            ("venusaurite", 3, 10033),
            ("blastoisinite", 9, 10036),
            ("charizardite-y", 6, 10035),
            ("kangaskhanite", 115, 10039),
            ("ampharosite", 181, 10045),
            ("houndoominite", 229, 10048),
            ("blazikenite", 257, 10050),
            ("mawilite", 303, 10052),
            ("aggronite", 306, 10053),
            ("medichamite", 308, 10054),
            ("manectite", 310, 10055),
            ("banettite", 354, 10056),
            ("abomasite", 460, 10060),
            ("pinsirite", 127, 10040),
            ("heracronite", 214, 10047),
            ("lucarionite", 448, 10059),
            ("gardevoirite", 282, 10051),
            ("tyranitarite", 248, 10049),
            ("absolite", 359, 10057),
            ("garchompite", 445, 10058),
            ("scizorite", 212, 10046),
            ("gyaradosite", 130, 10041),
            ("gengarite", 94, 10038),
            ("alakazite", 65, 10037),
            ("aerodactylite", 142, 10042),
            ("mewtwonite-y", 150, 10044),
            ("latiasite", 380, 10062),
            ("latiosite", 381, 10063),
        ]
        stones = data.get_mega_stones()
        self.assertEqual([(s.id, s.species, s.form_id) for s in stones], expected)
        self.assertEqual(
            [s.mega_sprite for s in stones],
            [f"img/sprites/pokemon/{form_id}.png" for _, _, form_id in expected],
        )

    def test_venusaurite_matches_source_species_and_stats(self):
        stone = data.get_mega_stone_by_species()[3]
        self.assertEqual(stone.id, "venusaurite")
        self.assertEqual(stone.mega_name, "Mega Venusaur")
        self.assertEqual(stone.mega_types, ("Grass", "Poison"))
        self.assertEqual(stone.mega_stats.hp, 80)
        self.assertEqual(stone.mega_stats.atk, 100)
        self.assertEqual(stone.mega_stats.defense, 123)
        self.assertEqual(stone.mega_stats.speed, 80)
        self.assertEqual(stone.mega_stats.special, 122)
        self.assertEqual(stone.mega_stats.spdef, 120)
        self.assertTrue(stone.starter)
        self.assertEqual(stone.tier, 1)


class ApplyMegaEvolutionTests(unittest.TestCase):
    def test_matching_mega_stone_transforms_base_stats_types_and_name(self):
        stone = data.get_mega_stone_by_species()[3]  # Venusaurite
        mon = _mon(3, level=50, held_item=battle.make_mega_stone_item(stone))
        original_base_stats = mon.base_stats
        original_types = mon.types
        original_name = mon.name
        changed = engine._apply_mega_evolution(mon)
        self.assertTrue(changed)
        self.assertEqual(mon.base_stats, stone.mega_stats)
        self.assertEqual(mon.types, stone.mega_types)
        self.assertEqual(mon.name, "Mega Venusaur")
        self.assertTrue(mon.flags.get("_megaEvolved"))
        self.assertEqual(mon.flags["_baseForm"]["base_stats"], original_base_stats)
        self.assertEqual(mon.flags["_baseForm"]["types"], original_types)
        self.assertEqual(mon.flags["_baseForm"]["name"], original_name)

    def test_wrong_species_for_stone_does_not_transform(self):
        stone = data.get_mega_stone_by_species()[3]  # Venusaurite, species 3
        mon = _mon(9, level=50, held_item=battle.make_mega_stone_item(stone))  # Blastoise holding it
        changed = engine._apply_mega_evolution(mon)
        self.assertFalse(changed)
        self.assertNotEqual(mon.base_stats, stone.mega_stats)
        self.assertFalse(mon.flags.get("_megaEvolved"))

    def test_non_mega_stone_held_item_does_not_transform(self):
        mon = _mon(3, level=50, held_item=HeldItem(id="leftovers"))
        changed = engine._apply_mega_evolution(mon)
        self.assertFalse(changed)
        self.assertFalse(mon.flags.get("_megaEvolved"))

    def test_no_held_item_does_not_transform(self):
        mon = _mon(3, level=50)
        changed = engine._apply_mega_evolution(mon)
        self.assertFalse(changed)

    def test_already_mega_evolved_and_still_eligible_is_a_noop(self):
        stone = data.get_mega_stone_by_species()[3]
        mon = _mon(3, level=50, held_item=battle.make_mega_stone_item(stone))
        engine._apply_mega_evolution(mon)
        mega_stats_after_first = mon.base_stats
        changed_again = engine._apply_mega_evolution(mon)
        self.assertFalse(changed_again)  # neither branch re-enters
        self.assertEqual(mon.base_stats, mega_stats_after_first)

    def test_restores_base_form_when_no_longer_eligible(self):
        stone = data.get_mega_stone_by_species()[3]
        mon = _mon(3, level=50, held_item=battle.make_mega_stone_item(stone))
        original_base_stats = mon.base_stats
        original_types = mon.types
        original_name = mon.name
        engine._apply_mega_evolution(mon)
        mon.held_item = HeldItem(id="leftovers")  # stone swapped out
        changed = engine._apply_mega_evolution(mon)
        self.assertTrue(changed)
        self.assertEqual(mon.base_stats, original_base_stats)
        self.assertEqual(mon.types, original_types)
        self.assertEqual(mon.name, original_name)
        self.assertFalse(mon.flags.get("_megaEvolved"))
        self.assertIsNone(mon.flags.get("_baseForm"))

    def test_never_evolved_and_currently_ineligible_is_a_noop(self):
        mon = _mon(3, level=50, held_item=HeldItem(id="leftovers"))
        changed = engine._apply_mega_evolution(mon)
        self.assertFalse(changed)
        self.assertFalse(mon.flags.get("_megaEvolved"))


# ---------------------------------------------------------------------------
# P0.8: the four pre-battle transforms, in source order
# (bundle.deobfuscated.js:81115-81188)
# ---------------------------------------------------------------------------


class BuildBattleCloneTests(unittest.TestCase):
    def test_state_team_is_never_mutated(self):
        stone = data.get_mega_stone_by_species()[3]
        mon = _mon(3, level=50, held_item=battle.make_mega_stone_item(stone))
        state = _state(team=[mon], passives=[Trait(id="shiny_first")])
        original_base_stats = mon.base_stats
        original_types = mon.types
        original_is_shiny = mon.is_shiny
        clone = engine._build_battle_clone(state)
        self.assertIsNot(clone[0], mon)
        self.assertEqual(mon.base_stats, original_base_stats)  # Mega did NOT leak onto state.team
        self.assertEqual(mon.types, original_types)
        self.assertEqual(mon.is_shiny, original_is_shiny)  # shiny_first did NOT leak onto state.team
        self.assertTrue(clone[0].is_shiny)
        self.assertEqual(clone[0].base_stats, stone.mega_stats)

    def test_mutating_clone_flags_does_not_alias_persistent_flags_dict(self):
        # The specific "mutable dict/list alias" hazard: `clone_combatant`
        # is a shallow `copy.copy`, so `clone.flags is mon.flags` right
        # after cloning -- `_apply_mega_evolution` must REASSIGN `.flags`,
        # never `.flags[key] = ...` in place, or this leaks silently.
        stone = data.get_mega_stone_by_species()[3]
        mon = _mon(3, level=50, held_item=battle.make_mega_stone_item(stone))
        mon.flags["existing"] = "untouched"
        state = _state(team=[mon])
        clone = engine._build_battle_clone(state)
        self.assertTrue(clone[0].flags.get("_megaEvolved"))
        self.assertNotIn("_megaEvolved", mon.flags)
        self.assertEqual(mon.flags, {"existing": "untouched"})

    def test_shiny_first_only_affects_index_zero(self):
        first = _mon(1, level=50)
        second = _mon(4, level=50)
        state = _state(team=[first, second], passives=[Trait(id="shiny_first")])
        clone = engine._build_battle_clone(state)
        self.assertTrue(clone[0].is_shiny)
        self.assertFalse(clone[1].is_shiny)

    def test_shiny_first_active_when_enabled_is_true(self):
        # Source: `enabled !== !0x1`; JavaScript `!0x1` is false, so the
        # default enabled=True value is active.
        first = _mon(1, level=50)
        state = _state(team=[first], passives=[Trait(id="shiny_first")])  # enabled=True default
        clone = engine._build_battle_clone(state)
        self.assertTrue(clone[0].is_shiny)

    def test_shiny_first_inactive_when_enabled_is_false(self):
        first = _mon(1, level=50)
        state = _state(team=[first], passives=[Trait(id="shiny_first", enabled=False)])
        clone = engine._build_battle_clone(state)
        self.assertFalse(clone[0].is_shiny)

    def test_shiny_first_absent_does_nothing(self):
        first = _mon(1, level=50)
        state = _state(team=[first], passives=[])
        clone = engine._build_battle_clone(state)
        self.assertFalse(clone[0].is_shiny)

    def test_mini_focus_reduces_team_by_two(self):
        team = [_mon(1, level=50), _mon(4, level=50), _mon(7, level=50)]
        state = _state(team=team, passives=[Trait(id="mini_focus")])
        clone = engine._build_battle_clone(state)
        self.assertEqual(len(clone), 1)
        self.assertEqual(clone[0].species_id, 1)  # front member kept, order preserved

    def test_mini_blade_reduces_team_by_two(self):
        team = [_mon(1, level=50), _mon(4, level=50), _mon(7, level=50)]
        state = _state(team=team, passives=[Trait(id="mini_blade")])
        clone = engine._build_battle_clone(state)
        self.assertEqual(len(clone), 1)

    def test_solo_blitz_reduces_team_by_five_but_never_below_one(self):
        team = [_mon(1, level=50), _mon(4, level=50), _mon(7, level=50)]
        state = _state(team=team, passives=[Trait(id="solo_blitz")])
        clone = engine._build_battle_clone(state)
        self.assertEqual(len(clone), 1)
        self.assertEqual(clone[0].species_id, 1)

    def test_mini_focus_and_mini_blade_stack_additively(self):
        team = [_mon(sid, level=50) for sid in (1, 4, 7, 152, 155, 158)]
        state = _state(team=team, passives=[Trait(id="mini_focus"), Trait(id="mini_blade")])
        clone = engine._build_battle_clone(state)
        self.assertEqual(len(clone), 2)  # 6 - (2+2) = 2
        self.assertEqual([m.species_id for m in clone], [1, 4])

    def test_all_three_mini_traits_stack_to_reduction_of_nine(self):
        team = [_mon(1, level=50) for _ in range(6)]
        state = _state(
            team=team,
            passives=[Trait(id="mini_focus"), Trait(id="mini_blade"), Trait(id="solo_blitz")],
        )
        clone = engine._build_battle_clone(state)
        self.assertEqual(len(clone), 1)  # max(1, 6-9)

    def test_mini_traits_ignore_enabled_false(self):
        team = [_mon(sid, level=50) for sid in (1, 4, 7, 152)]
        for trait_id in ("mini_focus", "mini_blade"):
            with self.subTest(trait_id=trait_id):
                state = _state(team=team, passives=[Trait(id=trait_id, enabled=False)])
                self.assertEqual(len(engine._build_battle_clone(state)), 2)
        state = _state(team=team, passives=[Trait(id="solo_blitz", enabled=False)])
        self.assertEqual(len(engine._build_battle_clone(state)), 1)

    def test_no_reduction_without_the_traits(self):
        team = [_mon(1, level=50), _mon(4, level=50)]
        state = _state(team=team, passives=[])
        clone = engine._build_battle_clone(state)
        self.assertEqual(len(clone), 2)

    def test_effort_ribbon_buffs_every_nonshiny_member(self):
        team = [_mon(1, level=50), _mon(4, level=50)]
        team[0].current_hp = 1  # should be fully healed by the buff
        state = _state(team=team, passives=[Trait(id="effort_ribbon")])
        clone = engine._build_battle_clone(state)
        for mon in clone:
            self.assertEqual(mon.stat_buffs, {"hp": 10, "atk": 10, "def": 10, "speed": 10, "special": 10, "spdef": 10})
            expected_max_hp = int(map_gen.calc_hp(mon.base_stats.hp, mon.level) * 1.5)
            self.assertEqual(mon.max_hp, expected_max_hp)
            self.assertEqual(mon.current_hp, mon.max_hp)

    def test_effort_ribbon_ignores_enabled_field(self):
        # Plain presence check -- unlike shiny_first, `enabled` is never
        # read at all (bundle.deobfuscated.js:81178-81179).
        team = [_mon(1, level=50)]
        state = _state(team=team, passives=[Trait(id="effort_ribbon", enabled=False)])
        clone = engine._build_battle_clone(state)
        self.assertEqual(clone[0].stat_buffs.get("hp"), 10)

    def test_effort_ribbon_skips_already_shiny_members(self):
        team = [_mon(1, level=50, is_shiny=True), _mon(4, level=50)]
        state = _state(team=team, passives=[Trait(id="effort_ribbon")])
        clone = engine._build_battle_clone(state)
        self.assertEqual(clone[0].stat_buffs, {})  # shiny member untouched
        self.assertEqual(clone[1].stat_buffs.get("hp"), 10)

    def test_shiny_first_excludes_its_own_target_from_effort_ribbon(self):
        # Interaction the source's own ordering produces: shiny_first (step
        # 2) makes clone[0] shiny BEFORE effort_ribbon (step 4) runs, so
        # clone[0] is excluded from the ribbon buff while clone[1] still
        # gets it.
        team = [_mon(1, level=50), _mon(4, level=50)]
        state = _state(
            team=team,
            passives=[Trait(id="shiny_first"), Trait(id="effort_ribbon")],
        )
        clone = engine._build_battle_clone(state)
        self.assertTrue(clone[0].is_shiny)
        self.assertEqual(clone[0].stat_buffs, {})
        self.assertEqual(clone[1].stat_buffs.get("hp"), 10)

    def test_mega_evolution_base_stats_feed_effort_ribbon_maxhp_calc(self):
        # Interaction: effort_ribbon's 1.5x maxHp recompute (step 4) must
        # read `mon.base_stats.hp` AS IT STANDS AFTER Mega Evolution (step
        # 1) already ran and reassigned it -- not a value captured earlier.
        # Every Mega Stone in this table happens to leave `hp` numerically
        # unchanged from the base species (matching mainline Pokemon, where
        # Mega Evolution never changes the HP stat either) -- proven here by
        # asserting `clone[0].base_stats` IS the stone's own `mega_stats`
        # object (i.e. the post-Mega state, not a stale pre-Mega reference)
        # at the moment the maxHp formula reads it, rather than relying on
        # a numeric HP difference that this data doesn't have.
        stone = data.get_mega_stone_by_species()[3]  # Venusaurite
        mon = _mon(3, level=50, held_item=battle.make_mega_stone_item(stone))
        state = _state(team=[mon], passives=[Trait(id="effort_ribbon")])
        clone = engine._build_battle_clone(state)
        self.assertIs(clone[0].base_stats, stone.mega_stats)
        expected_max_hp = int(map_gen.calc_hp(stone.mega_stats.hp, 50) * 1.5)
        self.assertEqual(clone[0].max_hp, expected_max_hp)

    def test_effort_ribbon_applies_only_to_post_truncation_survivors(self):
        team = [_mon(1, level=50), _mon(4, level=50), _mon(7, level=50)]
        state = _state(team=team, passives=[Trait(id="mini_focus"), Trait(id="effort_ribbon")])
        clone = engine._build_battle_clone(state)
        self.assertEqual(len(clone), 1)
        self.assertEqual(clone[0].stat_buffs.get("hp"), 10)


# ---------------------------------------------------------------------------
# P0.8: copy-back must not leak any pre-battle transform onto state.team
# ---------------------------------------------------------------------------


class NoLeakThroughRealBattleTests(unittest.TestCase):
    def test_truncated_roster_is_not_persisted_after_a_win(self):
        team = [_mon(1, level=50), _mon(4, level=50), _mon(7, level=50)]
        state = _state(team=team, passives=[Trait(id="solo_blitz")], gen3_mode=False)
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
            engine._run_battle_screen(state, [_mon(1, level=1)])
        self.assertEqual(len(state.team), 3)  # all 3 members still on the roster
        self.assertEqual([m.species_id for m in state.team], [1, 4, 7])

    def test_mega_transform_does_not_leak_after_a_win(self):
        stone = data.get_mega_stone_by_species()[3]
        mon = _mon(3, level=50, held_item=battle.make_mega_stone_item(stone))
        original_base_stats = mon.base_stats
        original_types = mon.types
        original_name = mon.name
        state = _state(team=[mon])
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
            engine._run_battle_screen(state, [_mon(1, level=1)])
        self.assertEqual(state.team[0].base_stats, original_base_stats)
        self.assertEqual(state.team[0].types, original_types)
        self.assertEqual(state.team[0].name, original_name)

    def test_shiny_first_does_not_leak_after_a_win(self):
        mon = _mon(1, level=50)
        state = _state(team=[mon], passives=[Trait(id="shiny_first")])
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
            engine._run_battle_screen(state, [_mon(1, level=1)])
        self.assertFalse(state.team[0].is_shiny)

    def test_effort_ribbon_stat_buffs_and_maxhp_do_not_leak_after_a_win(self):
        mon = _mon(1, level=50)
        original_max_hp = mon.max_hp
        state = _state(team=[mon], passives=[Trait(id="effort_ribbon")])
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
            engine._run_battle_screen(state, [_mon(1, level=1)])
        self.assertEqual(state.team[0].stat_buffs, {})
        self.assertEqual(state.team[0].max_hp, original_max_hp)

    def test_short_win_result_updates_only_matching_prefix_member(self):
        team = [_mon(1, level=50), _mon(4, level=50), _mon(7, level=50)]
        original_tail = [(m.species_id, m.current_hp) for m in team[1:]]
        state = _state(team=team, passives=[Trait(id="solo_blitz")])

        def short_win(player_team, enemy_team, **kwargs):
            p_clone = [battle_loop.clone_combatant(player_team[0])]
            p_clone[0].current_hp = 11
            return BattleResult(
                player_won=True,
                player_team=p_clone,
                enemy_team=[battle_loop.clone_combatant(m) for m in enemy_team],
                player_participants={0},
                rounds=1,
            )

        with patch.object(engine.battle_loop, "run_battle", side_effect=short_win):
            engine._run_battle_screen(state, [_mon(1, level=1)])
        self.assertEqual(state.team[0].current_hp, 11)
        self.assertEqual([(m.species_id, m.current_hp) for m in state.team[1:]], original_tail)

    def test_short_loss_result_does_not_cull_or_reindex_tail(self):
        # bundle.deobfuscated.js:81358-81380: the Nuzlocke fainted-cull only
        # runs in `runBattleScreen`'s WIN branch (`if (..., BcF)` at line
        # 81278) -- the loss branch (`else`, line 81388) never touches
        # `state["team"]`/`state["items"]` at all, so a Nuzlocke LOSS leaves
        # the whole roster (including the fainted member, at 0 HP) exactly
        # as `_run_battle`'s own copy-back left it.
        team = [_mon(1, level=50), _mon(4, level=50), _mon(7, level=50)]
        state = _state(
            team=team,
            passives=[Trait(id="solo_blitz"), Trait(id="effort_ribbon")],
            nuzlocke_mode=True,
        )
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_loss_run_battle):
            result = engine._run_battle_screen(state, [_mon(1, level=99)])
        self.assertEqual([m.species_id for m in state.team], [1, 4, 7])
        self.assertEqual(state.team[0].current_hp, 0)
        self.assertEqual(state.team[1].current_hp, state.team[1].max_hp)
        self.assertEqual(state.team[2].current_hp, state.team[2].max_hp)
        engine._after_battle(state, result, level_gain=2)
        self.assertEqual([m.species_id for m in state.team], [1, 4, 7])  # loss: NOT culled
        self.assertEqual(state.team[0].current_hp, 0)  # fainted member preserved, not removed
        self.assertTrue(state.game_over)

    def test_truncated_participants_limit_normal_xp_but_all_team_xp_uses_persistent_roster(self):
        for all_team_xp, expected_levels in ((False, [12, 10, 10]), (True, [12, 12, 12])):
            with self.subTest(all_team_xp=all_team_xp):
                team = [_mon(1, level=10), _mon(4, level=10), _mon(7, level=10)]
                # applyLevelGain levels every living persistent member even
                # if it did not participate. Fainted members distinguish
                # the normal participant set from Silver/Admin's all-team
                # override after the battle clone was truncated to slot 0.
                team[1].current_hp = 0
                team[2].current_hp = 0
                state = _state(team=team, passives=[Trait(id="solo_blitz")])
                with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
                    result = engine._run_battle_screen(state, [_mon(1, level=1)])
                engine._after_battle(state, result, level_gain=2, all_team_xp=all_team_xp)
                self.assertEqual([m.level for m in state.team], expected_levels)


# ---------------------------------------------------------------------------
# P0.7 Path A: the immediate Gen3-only species-check Pickup branch
# (bundle.deobfuscated.js:81223-81245)
# ---------------------------------------------------------------------------


class Gen3ZigzagoonPickupTests(unittest.TestCase):
    def test_gate_pass_consumes_two_draws_and_adds_an_item(self):
        state = _state(team=[_mon(263, level=10)], gen3_mode=True)
        pool = data.get_passive_items()
        with patch.object(engine.rng, "rng", side_effect=[0.05, 0.5]) as mock_rng:
            engine._grant_gen3_zigzagoon_pickup(state)
        self.assertEqual(mock_rng.call_count, 2)
        expected = pool[int(0.5 * len(pool))]
        self.assertEqual(state.items, [expected.id])

    def test_gate_fail_consumes_exactly_one_draw(self):
        state = _state(team=[_mon(263, level=10)], gen3_mode=True)
        with patch.object(engine.rng, "rng", side_effect=[0.1]) as mock_rng:  # exactly 0.1 -- NOT < 0.1
            engine._grant_gen3_zigzagoon_pickup(state)
        self.assertEqual(mock_rng.call_count, 1)
        self.assertEqual(state.items, [])

    def test_gate_just_below_threshold_triggers(self):
        state = _state(team=[_mon(263, level=10)], gen3_mode=True)
        pool = data.get_passive_items()
        with patch.object(engine.rng, "rng", side_effect=[0.0999999, 0.0]):
            engine._grant_gen3_zigzagoon_pickup(state)
        self.assertEqual(state.items, [pool[0].id])

    def test_gen4_mode_never_fires_this_branch(self):
        state = _state(team=[_mon(263, level=10)], gen4_mode=True)
        with patch.object(engine.rng, "rng") as mock_rng:
            engine._grant_gen3_zigzagoon_pickup(state)
        mock_rng.assert_not_called()
        self.assertEqual(state.items, [])

    def test_gen1_mode_never_fires_this_branch(self):
        state = _state(team=[_mon(263, level=10)])  # no gen flags at all
        with patch.object(engine.rng, "rng") as mock_rng:
            engine._grant_gen3_zigzagoon_pickup(state)
        mock_rng.assert_not_called()

    def test_species_absent_never_fires(self):
        state = _state(team=[_mon(1, level=10)], gen3_mode=True)
        with patch.object(engine.rng, "rng") as mock_rng:
            engine._grant_gen3_zigzagoon_pickup(state)
        mock_rng.assert_not_called()

    def test_fainted_zigzagoon_still_qualifies_no_alive_check(self):
        mon = _mon(263, level=10)
        mon.current_hp = 0
        state = _state(team=[mon], gen3_mode=True)
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]) as mock_rng:
            engine._grant_gen3_zigzagoon_pickup(state)
        self.assertEqual(mock_rng.call_count, 2)
        self.assertEqual(len(state.items), 1)

    def test_linoone_also_qualifies(self):
        state = _state(team=[_mon(264, level=10)], gen3_mode=True)
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]):
            engine._grant_gen3_zigzagoon_pickup(state)
        self.assertEqual(len(state.items), 1)

    def test_duplicates_allowed_no_bag_dedup(self):
        pool = data.get_passive_items()
        state = _state(team=[_mon(263, level=10)], gen3_mode=True, items=[pool[0].id])
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]):
            engine._grant_gen3_zigzagoon_pickup(state)
        self.assertEqual(state.items, [pool[0].id, pool[0].id])

    def test_only_fires_on_a_win_via_run_battle_wiring(self):
        state = _state(team=[_mon(263, level=10)], gen3_mode=True)
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_loss_run_battle):
            with patch.object(engine.rng, "rng", return_value=0.0) as mock_rng:
                engine._run_battle_screen(state, [_mon(1, level=99)])
        mock_rng.assert_not_called()  # loss -- branch never even reaches its own rng draws
        self.assertEqual(state.items, [])

    def test_fires_before_copy_back_ordering_via_run_battle(self):
        # Exercise the real `_run_battle` wiring: a win with gen3_mode and a
        # qualifying species should append exactly one item when the single
        # gate roll passes and the immediate index roll is scripted.
        state = _state(team=[_mon(263, level=10)], gen3_mode=True)
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
            with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]):
                engine._run_battle_screen(state, [_mon(1, level=1)])
        self.assertEqual(len(state.items), 1)


# ---------------------------------------------------------------------------
# P0.7 Path B: grantPickupItem (bundle.deobfuscated.js:77615-77641)
# ---------------------------------------------------------------------------


class GrantPickupItemTests(unittest.TestCase):
    def test_qualifying_alive_pickup_holder_gets_an_item(self):
        state = _state(team=[_mon(263, level=10)])  # Zigzagoon -- pickup in Gen3 table (default)
        pool = data.get_passive_items()
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.5]) as mock_rng:
            engine._grant_pickup_item(state)
        self.assertEqual(mock_rng.call_count, 2)
        expected = pool[int(0.5 * len(pool))]
        self.assertEqual(state.items, [expected.id])

    def test_gate_at_exactly_0_1_does_not_fire(self):
        state = _state(team=[_mon(263, level=10)])
        with patch.object(engine.rng, "rng", side_effect=[0.1]) as mock_rng:
            engine._grant_pickup_item(state)
        self.assertEqual(mock_rng.call_count, 1)
        self.assertEqual(state.items, [])

    def test_non_qualifying_species_draws_no_rng_at_all(self):
        state = _state(team=[_mon(1, level=10)])  # Bulbasaur -- no pickup ability
        with patch.object(engine.rng, "rng") as mock_rng:
            engine._grant_pickup_item(state)
        mock_rng.assert_not_called()

    def test_fainted_pickup_holder_does_not_qualify(self):
        mon = _mon(263, level=10)
        mon.current_hp = 0
        state = _state(team=[mon])
        with patch.object(engine.rng, "rng") as mock_rng:
            engine._grant_pickup_item(state)
        mock_rng.assert_not_called()
        self.assertEqual(state.items, [])

    def test_second_alive_qualifying_member_still_fires(self):
        fainted = _mon(263, level=10)
        fainted.current_hp = 0
        alive = _mon(264, level=10)
        state = _state(team=[fainted, alive])
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]) as mock_rng:
            engine._grant_pickup_item(state)
        self.assertEqual(mock_rng.call_count, 2)
        self.assertEqual(len(state.items), 1)

    def test_multiple_alive_pickup_members_still_get_one_chance_and_one_reward(self):
        state = _state(team=[_mon(263, level=10), _mon(264, level=10)])
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]) as mock_rng:
            engine._grant_pickup_item(state)
        self.assertEqual(mock_rng.call_count, 2)
        self.assertEqual(len(state.items), 1)

    def test_gen4_mode_uses_gen4_ability_table(self):
        # species 399 (Bidoof) is pickup only in the GEN4 table, not Gen3.
        state = _state(team=[_mon(399, level=10)], gen4_mode=True)
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]):
            engine._grant_pickup_item(state)
        self.assertEqual(len(state.items), 1)

    def test_gen4_species_does_not_qualify_without_gen4_mode(self):
        state = _state(team=[_mon(399, level=10)])  # gen4_mode False -- Gen3 table used
        with patch.object(engine.rng, "rng") as mock_rng:
            engine._grant_pickup_item(state)
        mock_rng.assert_not_called()

    def test_dedup_filters_already_held_items(self):
        pool = data.get_passive_items()
        held = [item.id for item in pool[1:]]  # every item except pool[0] already owned
        state = _state(team=[_mon(263, level=10)], items=list(held))
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.999999]) as mock_rng:
            engine._grant_pickup_item(state)
        self.assertEqual(mock_rng.call_count, 2)
        self.assertEqual(state.items[-1], pool[0].id)  # only remaining candidate, regardless of the 2nd roll

    def test_full_bag_gate_passes_but_no_second_draw_and_no_item(self):
        pool = data.get_passive_items()
        state = _state(team=[_mon(263, level=10)], items=[item.id for item in pool])
        with patch.object(engine.rng, "rng", side_effect=[0.0]) as mock_rng:
            engine._grant_pickup_item(state)
        self.assertEqual(mock_rng.call_count, 1)  # early-returns before the index roll
        self.assertEqual(len(state.items), len(pool))  # unchanged

    def test_wired_into_after_battle_right_after_level_gain(self):
        state = _state(team=[_mon(263, level=10)])
        result = BattleResult(
            player_won=True,
            player_team=[_mon(263, level=10)],
            enemy_team=[_mon(1, level=1)],
            player_participants={0},
            rounds=1,
        )
        with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]):
            engine._after_battle(state, result, level_gain=2)
        self.assertEqual(len(state.items), 1)

    def test_not_wired_on_a_loss(self):
        state = _state(team=[_mon(263, level=10)])
        fainted = _mon(263, level=10)
        fainted.current_hp = 0
        result = BattleResult(
            player_won=False,
            player_team=[fainted],
            enemy_team=[_mon(1, level=50)],
            player_participants=set(),
            rounds=1,
        )
        with patch.object(engine.rng, "rng") as mock_rng:
            engine._after_battle(state, result, level_gain=2)
        mock_rng.assert_not_called()
        self.assertEqual(state.items, [])


# ---------------------------------------------------------------------------
# Both Pickup paths in one eligible (Gen3, Zigzagoon-on-team) win
# ---------------------------------------------------------------------------


class BothPickupPathsTests(unittest.TestCase):
    def test_both_paths_fire_off_a_single_win_with_one_alive_zigzagoon(self):
        # Zigzagoon (263) satisfies BOTH gates at once: Path A's species
        # check AND Path B's alive+ability check (getGen3Ability(263) ==
        # "pickup" in the default/Gen3 table) -- both fire independently off
        # the SAME win, in source order: Path A (inside _run_battle) then
        # (zero draws inside _apply_level_gain -- no lucky_egg here) then
        # Path B (inside _after_battle).
        pool = data.get_passive_items()
        state = _state(team=[_mon(263, level=10)], gen3_mode=True)
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
            with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0, 0.0, 0.25]) as mock_rng:
                result = engine._run_battle_screen(state, [_mon(1, level=1)])
                engine._after_battle(state, result, level_gain=2)
        self.assertEqual(mock_rng.call_count, 4)
        self.assertEqual(len(state.items), 2)
        expected_a = pool[int(0.0 * len(pool))]
        expected_b = pool[int(0.25 * len(pool))]
        self.assertEqual(state.items, [expected_a.id, expected_b.id])

    def test_first_path_reward_is_removed_from_second_paths_filtered_pool(self):
        pool = data.get_passive_items()
        state = _state(team=[_mon(263, level=10)], gen3_mode=True)
        with patch.object(engine.battle_loop, "run_battle", side_effect=_echo_run_battle):
            with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0, 0.0, 0.0]) as mock_rng:
                result = engine._run_battle_screen(state, [_mon(1, level=1)])
                engine._after_battle(state, result, level_gain=2)
        self.assertEqual(mock_rng.call_count, 4)
        self.assertEqual(state.items, [pool[0].id, pool[1].id])

    def test_nuzlocke_fainted_zigzagoon_gets_immediate_path_only_before_cull(self):
        zigzagoon = _mon(263, level=10)
        teammate = _mon(1, level=10)
        state = _state(team=[zigzagoon, teammate], gen3_mode=True, nuzlocke_mode=True)

        def win_with_fainted_zigzagoon(player_team, enemy_team, **kwargs):
            p_clone = [battle_loop.clone_combatant(m) for m in player_team]
            p_clone[0].current_hp = 0
            return BattleResult(
                player_won=True,
                player_team=p_clone,
                enemy_team=[battle_loop.clone_combatant(m) for m in enemy_team],
                player_participants={1},
                rounds=1,
            )

        with patch.object(engine.battle_loop, "run_battle", side_effect=win_with_fainted_zigzagoon):
            with patch.object(engine.rng, "rng", side_effect=[0.0, 0.0]) as mock_rng:
                result = engine._run_battle_screen(state, [_mon(1, level=1)])
                engine._after_battle(state, result, level_gain=2)
        self.assertEqual(mock_rng.call_count, 2)
        self.assertEqual(len(state.items), 1)
        self.assertEqual([m.species_id for m in state.team], [1])


if __name__ == "__main__":
    unittest.main()
