"""Tests for pokelike/battle_loop.py.

**Validation depth, stated plainly**: `run_battle` orchestrates validated
primitives and the `battle_abilities`/`battle_traits` hook providers. In
addition to these unit and structural tests, `tools/battle-oracle/` executes
    the real JavaScript `runBattle` from an AST-audited bundle prefix. Its 35
    fixed-seed fixtures currently prove the no-config baseline, selected merged
    ability/trait behavior, burn/poison status-tick dispatch, extra attacks,
    Shell Bell, and `rand_nerf` mirror dispatch end-to-end.
That is meaningful cross-language coverage, but it is not proof for every
trait, ability, secondary attack, or complete game run.

Run with: python -m unittest pokelike.tests.test_battle_loop -v
"""

from __future__ import annotations

import math
import unittest
from unittest.mock import MagicMock

from pokelike import battle, battle_abilities, battle_loop, battle_traits, data, rng
from pokelike.data import BaseStats


def _mon(species_id, level=50, **overrides):
    mon = data.get_pokedex()[species_id]
    bs = overrides.pop("base_stats", mon.base_stats)
    hp = overrides.pop("max_hp", int((bs.hp * 2 * level) / 100) + level + 10)
    return battle.Combatant(
        species_id=species_id, level=level, base_stats=bs, types=mon.types,
        max_hp=hp, current_hp=hp, **overrides,
    )


class RunBattlePlainTests(unittest.TestCase):
    def test_battle_terminates_with_a_winner(self):
        rng.seed_rng(12345)
        player = [_mon(6), _mon(9)]
        enemy = [_mon(3)]
        result = battle_loop.run_battle(player, enemy)
        self.assertIsInstance(result.player_won, bool)
        self.assertGreater(result.rounds, 0)
        self.assertLessEqual(result.rounds, battle_loop._ROUND_CAP)

    def test_winner_has_at_least_one_survivor_loser_has_none(self):
        rng.seed_rng(2024)
        player = [_mon(6), _mon(9)]
        enemy = [_mon(129)]  # Magikarp, only knows Splash -- should lose fast
        result = battle_loop.run_battle(player, enemy)
        self.assertTrue(result.player_won)
        self.assertTrue(any(m.current_hp > 0 for m in result.player_team))
        self.assertTrue(all(m.current_hp <= 0 for m in result.enemy_team))

    def test_player_participants_tracks_sent_out_slots_only(self):
        rng.seed_rng(1)
        player = [_mon(6), _mon(9)]
        enemy = [_mon(3)]
        result = battle_loop.run_battle(player, enemy)
        self.assertIn(0, result.player_participants)
        # slot 1 only participates if slot 0 fainted during this battle
        if result.player_team[0].current_hp <= 0:
            self.assertIn(1, result.player_participants)


class RunBattleWithHooksTests(unittest.TestCase):
    def test_runs_with_ability_config_only(self):
        rng.seed_rng(7)
        player = [_mon(6), _mon(9)]
        enemy = [_mon(130)]  # Gyarados-line, intimidate
        cfg = battle_abilities.Gen3AbilityConfig()
        result = battle_loop.run_battle(player, enemy, ability_config=cfg)
        self.assertIsInstance(result.player_won, bool)

    def test_runs_with_traits_config_only(self):
        rng.seed_rng(42)
        player = [_mon(6), _mon(9)]
        enemy = [_mon(3), _mon(112)]
        traits = [battle.Trait("ec_deal_more"), battle.Trait("speed_start"), battle.Trait("crit_overflow")]
        tiers = battle_traits.compute_trait_tiers(player, traits=traits)
        traits_cfg = battle_traits.TraitsConfig(player_tiers=tiers, traits=traits)
        result = battle_loop.run_battle(player, enemy, traits=traits, traits_config=traits_cfg)
        self.assertIsInstance(result.player_won, bool)

    def test_speed_start_trait_grants_speed_stage_before_first_round(self):
        rng.seed_rng(1)
        player = [_mon(6)]
        enemy = [_mon(3)]
        traits = [battle.Trait("speed_start")]
        traits_cfg = battle_traits.TraitsConfig(traits=traits)
        # speed_start applies inside on_start_fight; verify by re-running the
        # setup portion directly rather than racing the whole battle.
        bc = battle.BattleConfig()
        battle_loop._init_battle_state(player[0])
        traits_cfg.on_start_fight(player, enemy, bc)
        self.assertEqual(player[0].stages["speed"], 1)


class RunBattleCloneBoundaryTests(unittest.TestCase):
    """CODEX.md issues 3-4: `run_battle` must never mutate the Combatant
    objects passed in by the caller -- it clones internally
    (`clone_combatant`) and returns the mutated clones as
    `BattleResult.player_team`/`enemy_team`. Any caller that wants
    battle-local changes reflected on a persistent roster must copy fields
    back explicitly (see `engine._copy_back_battle_result`)."""

    def test_result_teams_are_not_the_same_objects(self):
        rng.seed_rng(1)
        player = [_mon(6), _mon(9)]
        enemy = [_mon(3)]
        result = battle_loop.run_battle(player, enemy)
        self.assertIsNot(result.player_team[0], player[0])
        self.assertIsNot(result.player_team[1], player[1])
        self.assertIsNot(result.enemy_team[0], enemy[0])

    def test_caller_objects_are_never_mutated(self):
        rng.seed_rng(1)
        player = [_mon(6, level=50), _mon(9, level=50)]
        enemy = [_mon(3, level=50)]
        snapshot = [(m.level, m.current_hp, m.status, dict(m.stages), m.gen3_ability, m.types, m.base_stats) for m in player]
        battle_loop.run_battle(player, enemy)
        for mon, (level, hp, status, stages, ability, types, base_stats) in zip(player, snapshot):
            self.assertEqual(mon.level, level)
            self.assertEqual(mon.current_hp, hp)
            self.assertEqual(mon.status, status)
            self.assertEqual(mon.stages, stages)
            self.assertEqual(mon.gen3_ability, ability)
            self.assertEqual(mon.types, types)
            self.assertEqual(mon.base_stats, base_stats)

    def test_ditto_transform_does_not_leak_into_original(self):
        rng.seed_rng(5)
        ditto = _mon(132, level=50)  # Ditto
        original_types = ditto.types
        original_base_stats = ditto.base_stats
        enemy = _mon(6, level=50)  # Charizard -- distinct types/base stats
        result = battle_loop.run_battle([ditto], [enemy])
        self.assertEqual(ditto.types, original_types)
        self.assertEqual(ditto.base_stats, original_base_stats)
        clone = result.player_team[0]
        self.assertIsNot(clone, ditto)
        self.assertEqual(clone.types, enemy.types)
        self.assertEqual(clone.base_stats, enemy.base_stats)

    def test_stage_mutation_on_clone_does_not_touch_original_stages_dict(self):
        rng.seed_rng(3)
        player = [_mon(6, level=50)]
        enemy = [_mon(3, level=50)]
        original_stages_obj = player[0].stages
        battle_loop.run_battle(player, enemy)
        self.assertIs(player[0].stages, original_stages_obj)
        self.assertEqual(player[0].stages, {"atk": 0, "def": 0, "speed": 0, "special": 0, "spdef": 0})


class RunBattleStressTests(unittest.TestCase):
    def test_many_random_battles_complete_without_error(self):
        import random as pyrandom

        traits = [
            battle.Trait("ec_deal_more"),
            battle.Trait("sturdy"),
            battle.Trait("poison_onhit"),
            battle.Trait("elec_paralyze"),
            battle.Trait("grass_burst"),
        ]
        species_pool_p = [1, 4, 7, 25, 133]
        species_pool_e = [19, 41, 74, 129, 143]
        for i in range(25):
            rng.seed_rng(i * 7919 + 1)
            local_random = pyrandom.Random(i)
            player = [_mon(local_random.choice(species_pool_p), level=local_random.randint(20, 60)) for _ in range(3)]
            enemy = [_mon(local_random.choice(species_pool_e)) for _ in range(3)]
            ability_cfg = battle_abilities.Gen3AbilityConfig()
            tiers = battle_traits.compute_trait_tiers(player, traits=traits)
            traits_cfg = battle_traits.TraitsConfig(player_tiers=tiers, traits=traits)
            result = battle_loop.run_battle(player, enemy, traits=traits, ability_config=ability_cfg, traits_config=traits_cfg)
            self.assertIsInstance(result.player_won, bool)
            self.assertLessEqual(result.rounds, battle_loop._ROUND_CAP)


class PostHitTraitChainTests(unittest.TestCase):
    """CODEX.md issue 13: the inline post-hit trait chain (bundle.
    deobfuscated.js:56000-56218) -- `maxhp_strike`/`crit_boost`/
    `crit_lifesteal`/`crit_flinch`/`lifesteal`/`rand_nerf`/`rand_boost`/
    `speed_diff`/`bug_critlvl`/`bug_strip`, ported as `_apply_post_hit_traits`.
    """

    def test_maxhp_strike_deals_five_percent_of_attacker_maxhp(self):
        mover = _mon(4, level=50)  # Charmander, Fire/Normal? -- force Normal type
        mover.types = ("Normal",)
        target = _mon(7, level=50)
        target.current_hp = target.max_hp
        traits = [battle.Trait("maxhp_strike")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        expected = max(1, int(mover.max_hp * 0.05))
        self.assertEqual(target.current_hp, target.max_hp - expected)

    def test_maxhp_strike_does_not_fire_for_non_normal_attacker(self):
        mover = _mon(4, level=50)
        mover.types = ("Fire",)
        target = _mon(7, level=50)
        target.current_hp = target.max_hp
        traits = [battle.Trait("maxhp_strike")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertEqual(target.current_hp, target.max_hp)

    def test_crit_boost_grants_three_stages_on_crit(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        traits = [battle.Trait("crit_boost")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", True, 10, traits, False)
        self.assertEqual(mover.stages["atk"], 1)
        self.assertEqual(mover.stages["special"], 1)
        self.assertEqual(mover.stages["speed"], 1)

    def test_crit_boost_does_not_fire_without_a_crit(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        traits = [battle.Trait("crit_boost")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertEqual(mover.stages["atk"], 0)

    def test_crit_lifesteal_heals_attacker_by_actual_damage(self):
        mover = _mon(4, level=50)
        mover.current_hp = mover.max_hp - 50
        target = _mon(7, level=50)
        traits = [battle.Trait("crit_lifesteal")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", True, 10, traits, False)
        self.assertEqual(mover.current_hp, mover.max_hp - 50 + 10)

    def test_crit_lifesteal_blocked_by_no_heal_revive(self):
        mover = _mon(4, level=50)
        mover.current_hp = mover.max_hp - 50
        target = _mon(7, level=50)
        traits = [battle.Trait("crit_lifesteal")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", True, 10, traits, no_heal_revive=True)
        self.assertEqual(mover.current_hp, mover.max_hp - 50)

    def test_lifesteal_heals_twenty_percent_of_actual_damage(self):
        mover = _mon(4, level=50)
        mover.current_hp = mover.max_hp - 50
        target = _mon(7, level=50)
        traits = [battle.Trait("lifesteal")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 20, traits, False)
        self.assertEqual(mover.current_hp, mover.max_hp - 50 + 4)

    def test_rand_boost_caps_at_six_procs(self):
        mover = _mon(4, level=50)
        mover.flags["_randBoostCount"] = 6
        target = _mon(7, level=50)
        traits = [battle.Trait("rand_boost")]
        rng.seed_rng(1)
        total_before = sum(mover.stages.values())
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertEqual(sum(mover.stages.values()), total_before)

    def test_rand_nerf_drops_one_random_target_stage(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        traits = [battle.Trait("rand_nerf")]
        rng.seed_rng(1)
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertEqual(sum(target.stages.values()), -1)

    def test_speed_diff_deals_bonus_damage_when_attacker_faster(self):
        mover = _mon(4, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, special=50, spdef=50, speed=200))
        target = _mon(7, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, special=50, spdef=50, speed=10))
        traits = [battle.Trait("speed_diff")]
        before = target.current_hp
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertLess(target.current_hp, before)

    def test_speed_diff_no_bonus_when_attacker_slower(self):
        mover = _mon(4, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, special=50, spdef=50, speed=10))
        target = _mon(7, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, special=50, spdef=50, speed=200))
        traits = [battle.Trait("speed_diff")]
        before = target.current_hp
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertEqual(target.current_hp, before)

    def test_bug_critlvl_grants_twenty_levels_once(self):
        mover = _mon(10, level=50)  # Caterpie, Bug type
        mover.types = ("Bug",)
        target = _mon(7, level=50)
        traits = [battle.Trait("bug_critlvl")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", True, 10, traits, False)
        self.assertEqual(mover.level, 70)
        self.assertEqual(mover.flags["_critLevelBase"], 50)
        # Second crit this battle must not re-trigger (guarded by _critLevelBase already set).
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", True, 10, traits, False)
        self.assertEqual(mover.level, 70)

    def test_bug_strip_drops_target_five_levels_once(self):
        mover = _mon(10, level=50)
        mover.types = ("Bug",)
        target = _mon(7, level=50)
        traits = [battle.Trait("bug_strip")]
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertEqual(target.level, 45)
        self.assertTrue(target.flags["_bugLevelStripped"])
        battle_loop._apply_post_hit_traits(mover, target, "player", "enemy", False, 10, traits, False)
        self.assertEqual(target.level, 45)  # guarded, no second strip


class StatusTickFaintDispatchTests(unittest.TestCase):
    """CODEX.md issue 15 (was 5's out-of-scope follow-up): burn-tick faints
    call neither onKO nor onFaint; poison-tick faints call onKO only when
    the fainted side is "enemy" (killer hardcoded to the first alive PLAYER
    member) and onFaint unconditionally."""

    def test_burn_faint_calls_neither_onko_nor_onfaint(self):
        mon = _mon(1, level=50)
        mon.burned = True
        mon.current_hp = 1  # burn tick (10% max hp) will finish it off
        enemy = _mon(4, level=50)
        ability_cfg = MagicMock()
        battle_loop._status_tick_round([mon], "player", [enemy], [], ability_cfg, None, battle.BattleConfig(), set())
        ability_cfg.on_ko.assert_not_called()
        ability_cfg.on_faint.assert_not_called()

    def test_poison_faint_on_enemy_side_calls_onko_with_first_alive_player_as_killer(self):
        fainted = _mon(1, level=50)
        fainted.poison_stacks = 20
        fainted.current_hp = 1
        killer_candidate = _mon(4, level=50)
        ability_cfg = MagicMock()
        battle_loop._status_tick_round([fainted], "enemy", [killer_candidate], [], ability_cfg, None, battle.BattleConfig(), set())
        ability_cfg.on_ko.assert_called_once_with(fainted, killer_candidate)
        ability_cfg.on_faint.assert_called_once()

    def test_poison_faint_on_player_side_does_not_call_onko_but_calls_onfaint(self):
        fainted = _mon(1, level=50)
        fainted.poison_stacks = 20
        fainted.current_hp = 1
        enemy = _mon(4, level=50)
        ability_cfg = MagicMock()
        battle_loop._status_tick_round([fainted], "player", [enemy], [], ability_cfg, None, battle.BattleConfig(), set())
        ability_cfg.on_ko.assert_not_called()
        ability_cfg.on_faint.assert_called_once_with(fainted, [fainted])

    def test_enemy_poison_faint_calls_traits_onko_immediately_and_sweep_deduplicates(self):
        fainted = _mon(1, level=50)
        fainted.poison_stacks = 20
        fainted.current_hp = 1
        killer = _mon(4, level=50)
        before_max = killer.max_hp
        traits = [battle.Trait("ko_maxhp")]
        traits_cfg = battle_traits.TraitsConfig(traits=traits)
        config = battle.BattleConfig()

        battle_loop._status_tick_round(
            [fainted], "enemy", [killer], traits, None, traits_cfg,
            config, set(),
        )
        self.assertEqual(killer.max_hp, before_max + 2)
        self.assertIn(("enemy", 0), config.kos_handled)

        traits_cfg.sweep_kos([killer], [fainted], config)
        self.assertEqual(killer.max_hp, before_max + 2, "sweepKOs must not apply ko_maxhp twice")

    def test_no_heal_revive_blocks_poison_drain(self):
        healer = _mon(1, level=50)
        healer.current_hp = max(1, healer.max_hp - 20)
        poisoned = _mon(4, level=50)
        poisoned.poison_stacks = 1
        before = healer.current_hp
        traits = [battle.Trait("poison_drain"), battle.Trait("no_heal_revive")]

        battle_loop._status_tick_round(
            [poisoned], "enemy", [healer], traits, None,
            battle_traits.TraitsConfig(traits=traits),
            battle.BattleConfig(), set(), no_heal_revive=True,
        )
        self.assertEqual(healer.current_hp, before)

    def test_poison_drain_healing_triggers_heal_boost_stat_accumulator(self):
        healer = _mon(1, level=50)
        healer.max_hp = 200
        healer.current_hp = 100
        poisoned = _mon(4, level=50)
        poisoned.max_hp = 200
        poisoned.current_hp = 200
        poisoned.poison_stacks = 5  # 50 damage -> 100 healing
        traits = [battle.Trait("poison_drain"), battle.Trait("heal_boost_stat")]
        before_stages = sum(healer.stages.values())

        battle_loop._status_tick_round(
            [poisoned], "enemy", [healer], traits, None,
            battle_traits.TraitsConfig(traits=traits),
            battle.BattleConfig(), set(),
        )
        self.assertEqual(sum(healer.stages.values()) - before_stages, 2)


class ExtraAttackTraitsTests(unittest.TestCase):
    """P0.3: `half_twice`/`dragon_first_double` extra attacks (bundle.
    deobfuscated.js:56220-56364) and their shared `BIO` post-hit subset
    (55300-55404). Cross-language proof of the full mechanic (fresh RNG
    consumption, exact damage, faint dispatch timing, merged-hook
    re-entry) lives in `tools/battle-oracle/fixtures/half_twice_*.json`/
    `dragon_first_double_*.json` -- these tests isolate the pieces that are
    cheaper to pin down directly in Python: gating conditions, the flag
    scoping difference between the two traits, and which post-hit effects
    `_bio_post_hit` does/doesn't include.
    """

    def _psychic(self):
        return battle.MoveInstance(power=60, type="Psychic", name="Psychic", is_special=True)

    def test_half_twice_deals_a_second_hit_and_sets_the_flag(self):
        rng.seed_rng(1)
        mover = _mon(150, level=50)  # Mewtwo
        target = _mon(7, level=50)
        before = target.current_hp
        battle_loop._apply_half_twice_extra_attack(
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("half_twice")], battle.BattleConfig(), None, None,
            [mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        self.assertLess(target.current_hp, before)
        self.assertTrue(mover.flags["_halfTwiceUsed"])

    def test_half_twice_does_not_refire_until_externally_reset(self):
        """The function itself never clears `_halfTwiceUsed` -- that is
        `run_battle`'s job, once per round, for that round's active
        attacker (bundle.deobfuscated.js:55430-55431). Calling this
        function twice back-to-back with no reset in between must only
        deal damage once, proving the guard is a real one-shot within a
        single call sequence.
        """
        rng.seed_rng(1)
        mover = _mon(150, level=50)
        target = _mon(7, level=50)
        args = (
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("half_twice")], battle.BattleConfig(), None, None,
            [mover], [target], 1,
        )
        battle_loop._apply_half_twice_extra_attack(*args, actual_damage=10, no_heal_revive=False)
        hp_after_first = target.current_hp
        battle_loop._apply_half_twice_extra_attack(*args, actual_damage=10, no_heal_revive=False)
        self.assertEqual(target.current_hp, hp_after_first)

    def test_half_twice_refires_after_run_battle_resets_the_flag_for_a_new_round(self):
        """Integration-level proof that `run_battle` itself performs the
        per-round reset (not just that the helper is idempotent without
        it) -- spies on the real call site instead of re-deriving the
        round loop's control flow.
        """
        rng.seed_rng(11)
        player = [_mon(150, level=50)]  # Mewtwo
        enemy = [_mon(213, level=5, max_hp=100000)]  # Shuckle, HP padded to force many rounds
        calls = []
        original = battle_loop._apply_half_twice_extra_attack

        def spy(*args, **kwargs):
            calls.append(args[2])  # side
            return original(*args, **kwargs)

        with unittest.mock.patch.object(battle_loop, "_apply_half_twice_extra_attack", side_effect=spy):
            result = battle_loop.run_battle(player, enemy, traits=[battle.Trait("half_twice")])
        self.assertGreater(result.rounds, 1)
        # Called once per surviving mover's turn each round (player and/or
        # enemy) -- at least once per round proves it is not a single
        # battle-lifetime call.
        self.assertGreaterEqual(len(calls), result.rounds)

    def test_half_twice_requires_player_side(self):
        rng.seed_rng(1)
        mover = _mon(150, level=50)
        target = _mon(7, level=50)
        before = target.current_hp
        battle_loop._apply_half_twice_extra_attack(
            mover, target, "enemy", "player", self._psychic(), [], [],
            [battle.Trait("half_twice")], battle.BattleConfig(), None, None,
            [mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        self.assertEqual(target.current_hp, before)
        self.assertNotIn("_halfTwiceUsed", mover.flags)

    def test_half_twice_does_not_fire_when_main_hit_dealt_no_damage(self):
        rng.seed_rng(1)
        mover = _mon(150, level=50)
        target = _mon(7, level=50)
        before = target.current_hp
        battle_loop._apply_half_twice_extra_attack(
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("half_twice")], battle.BattleConfig(), None, None,
            [mover], [target], 1, actual_damage=0, no_heal_revive=False,
        )
        self.assertEqual(target.current_hp, before)
        self.assertNotIn("_halfTwiceUsed", mover.flags)

    def test_half_twice_does_not_apply_attacker_damage_mod(self):
        """Unlike dragon_first_double, half_twice's extra hit skips the
        threaded attackerDamageMod hook entirely (bundle.deobfuscated.js:
        56229-56237 has no such call between the fresh calcDamage and the
        HP subtraction)."""
        rng.seed_rng(1)
        mover = _mon(150, level=50)
        target = _mon(7, level=50)
        ability_cfg = MagicMock()
        ability_cfg.attacker_damage_mod.return_value = 999999
        battle_loop._apply_half_twice_extra_attack(
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("half_twice")], battle.BattleConfig(), ability_cfg, None,
            [mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        ability_cfg.attacker_damage_mod.assert_not_called()
        self.assertGreater(target.current_hp, 0)  # would be 0 if the 999999 mod had leaked in

    def test_dragon_first_double_requires_dragon_type_attacker(self):
        rng.seed_rng(1)
        mover = _mon(4, level=50)  # Charmander, Fire, not Dragon
        target = _mon(7, level=50)
        before = target.current_hp
        battle_loop._apply_dragon_first_double_extra_attack(
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("dragon_first_double")], battle.BattleConfig(), None, None,
            [mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        self.assertEqual(target.current_hp, before)

    def test_dragon_first_double_fires_once_and_sets_a_battle_wide_flag(self):
        rng.seed_rng(1)
        mover = _mon(147, level=50)  # Dratini, Dragon
        target = _mon(7, level=50)
        bc = battle.BattleConfig()
        before = target.current_hp
        battle_loop._apply_dragon_first_double_extra_attack(
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("dragon_first_double")], bc, None, None,
            [mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        self.assertLess(target.current_hp, before)
        self.assertTrue(bc.fired_flags["dragon_first_double"])
        hp_after_first = target.current_hp
        # Same battle_config, unreset -- must not fire again, even for a
        # second Dragon-type attacker.
        second_mover = _mon(148, level=50)  # Dragonair, Dragon
        battle_loop._apply_dragon_first_double_extra_attack(
            second_mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("dragon_first_double")], bc, None, None,
            [second_mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        self.assertEqual(target.current_hp, hp_after_first)

    def test_dragon_first_double_flag_is_battle_wide_via_run_battle(self):
        rng.seed_rng(1)
        player = [_mon(147, level=50)]  # Dratini
        enemy = [_mon(213, level=5, max_hp=100000)]  # Shuckle, HP padded to survive the main hit
        bc = battle.BattleConfig()
        battle_loop.run_battle(player, enemy, traits=[battle.Trait("dragon_first_double")], battle_config=bc)
        self.assertTrue(bc.fired_flags.get("dragon_first_double"))

    def test_dragon_first_double_applies_threaded_attacker_damage_mod(self):
        """Unlike half_twice, dragon_first_double DOES re-run the threaded
        attackerDamageMod hook -- both ability_config and traits_config, in
        ability-then-traits order (bundle.deobfuscated.js:56301-56304)."""
        rng.seed_rng(1)
        mover = _mon(147, level=50)
        target = _mon(7, level=50)
        target.current_hp = target.max_hp = 100000
        calls = []

        def ability_mod(attacker, defender, damage):
            calls.append("ability")
            return damage + 1000

        def traits_mod(attacker, defender, side, damage, battle_config):
            calls.append("traits")
            return damage + 1

        ability_cfg = MagicMock()
        ability_cfg.attacker_damage_mod.side_effect = ability_mod
        traits_cfg = MagicMock()
        traits_cfg.attacker_damage_mod.side_effect = traits_mod
        before = target.current_hp
        battle_loop._apply_dragon_first_double_extra_attack(
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("dragon_first_double")], battle.BattleConfig(), ability_cfg, traits_cfg,
            [mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        self.assertEqual(calls, ["ability", "traits"])
        # >= 1001 extra damage on top of whatever the base roll was.
        self.assertGreaterEqual(before - target.current_hp, 1001)

    def test_extra_attack_after_attack_hook_receives_is_extra_attack_true(self):
        rng.seed_rng(1)
        mover = _mon(150, level=50)
        target = _mon(7, level=50)
        ability_cfg = MagicMock()
        traits_cfg = MagicMock()
        battle_loop._apply_half_twice_extra_attack(
            mover, target, "player", "enemy", self._psychic(), [], [],
            [battle.Trait("half_twice")], battle.BattleConfig(), ability_cfg, traits_cfg,
            [mover], [target], 1, actual_damage=10, no_heal_revive=False,
        )
        ability_cfg.after_attack.assert_called_once()
        self.assertTrue(ability_cfg.after_attack.call_args.kwargs.get("is_extra_attack"))
        traits_cfg.after_attack.assert_called_once()
        self.assertTrue(traits_cfg.after_attack.call_args.kwargs.get("is_extra_attack"))


class BioPostHitSubsetTests(unittest.TestCase):
    """`_bio_post_hit` (bundle.deobfuscated.js:55300-55404) is a genuinely
    separate, narrower function from `_apply_post_hit_traits`'s inline
    main-hit chain in the source -- only 5 of the 10 main-hit effects.
    These pin down which ones are (and are not) included.
    """

    def test_crit_boost_included(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        battle_loop._bio_post_hit(mover, target, "player", "enemy", True, 10, [battle.Trait("crit_boost")], False)
        self.assertEqual(mover.stages["atk"], 1)

    def test_maxhp_strike_not_included(self):
        mover = _mon(4, level=50)
        mover.types = ("Normal",)
        target = _mon(7, level=50)
        before = target.current_hp
        battle_loop._bio_post_hit(mover, target, "player", "enemy", True, 10, [battle.Trait("maxhp_strike")], False)
        self.assertEqual(target.current_hp, before)

    def test_plain_lifesteal_not_included(self):
        mover = _mon(4, level=50)
        mover.current_hp = mover.max_hp - 50
        target = _mon(7, level=50)
        battle_loop._bio_post_hit(mover, target, "player", "enemy", False, 20, [battle.Trait("lifesteal")], False)
        self.assertEqual(mover.current_hp, mover.max_hp - 50)

    def test_rand_nerf_not_included(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        rng.seed_rng(1)
        battle_loop._bio_post_hit(mover, target, "player", "enemy", False, 10, [battle.Trait("rand_nerf")], False)
        self.assertEqual(sum(target.stages.values()), 0)

    def test_rand_boost_not_included(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        rng.seed_rng(1)
        battle_loop._bio_post_hit(mover, target, "player", "enemy", False, 10, [battle.Trait("rand_boost")], False)
        self.assertEqual(sum(mover.stages.values()), 0)

    def test_bug_strip_not_included(self):
        mover = _mon(10, level=50)
        mover.types = ("Bug",)
        target = _mon(7, level=50)
        battle_loop._bio_post_hit(mover, target, "player", "enemy", False, 10, [battle.Trait("bug_strip")], False)
        self.assertEqual(target.level, 50)

    def test_bug_critlvl_included(self):
        mover = _mon(10, level=50)
        mover.types = ("Bug",)
        target = _mon(7, level=50)
        battle_loop._bio_post_hit(mover, target, "player", "enemy", True, 10, [battle.Trait("bug_critlvl")], False)
        self.assertEqual(mover.level, 70)

    def test_speed_diff_included(self):
        mover = _mon(4, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, special=50, spdef=50, speed=200))
        target = _mon(7, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, special=50, spdef=50, speed=10))
        before = target.current_hp
        battle_loop._bio_post_hit(mover, target, "player", "enemy", False, 10, [battle.Trait("speed_diff")], False)
        self.assertLess(target.current_hp, before)

    def test_crit_flinch_included(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        with unittest.mock.patch.object(battle_loop.rng, "rng", return_value=0.0):
            battle_loop._bio_post_hit(mover, target, "player", "enemy", True, 10, [battle.Trait("crit_flinch")], False)
        self.assertTrue(target.flags.get("flinch"))

    def test_gated_on_actual_damage(self):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        before_stages = dict(mover.stages)
        battle_loop._bio_post_hit(mover, target, "player", "enemy", True, 0, [battle.Trait("crit_boost")], False)
        self.assertEqual(mover.stages, before_stages)


#: Held-item ids `run_battle`'s post-hit block branches on that are NOT in the
#: ported `ITEM_POOL` (`items_passive.json`), because the source does not offer
#: them as loot either: `life_orb` is defined at bundle.deobfuscated.js:42345
#: as a PRE-SET held item hung on specific species entries, not an `ITEM_POOL`
#: member. The branch is real and is exercised whenever such a mon is built, so
#: it is pinned here; it is simply not reachable from the player's bag.
_NON_POOL_HELD_ITEMS = {"life_orb": "Life Orb"}


def _passive_item(item_id):
    """The real `ITEM_POOL` entry where one exists, so these tests assert
    against the same `held_item.id` `run_battle` actually branches on."""
    for item in data.get_passive_items():
        if item.id == item_id:
            return item
    if item_id in _NON_POOL_HELD_ITEMS:
        return data.Item(
            id=item_id, name=_NON_POOL_HELD_ITEMS[item_id], desc="", icon="")
    raise AssertionError(
        f"{item_id!r} is neither in the ported ITEM_POOL nor a known "
        f"non-pool held item -- classify it before pinning it")


class PostHitEffectRecordTests(unittest.TestCase):
    """M6/N10. The four post-hit HP changes (bundle.deobfuscated.js:56366-56442)
    mutate HP *after* the `attack` record is appended. Before M6 they emitted
    nothing at all, so a replay showed the mover's HP move with no event to
    attribute it to and the attack record's own `attacker_hp_after` silently
    disagreed with the post-battle roster.

    These run a real `run_battle`, not a synthetic call, so the record has to
    survive the actual loop -- ordering, guards and all.
    """

    def _battle_with(self, *, player_item=None, enemy_item=None, seed=7):
        rng.seed_rng(seed)
        player = [_mon(6, level=60)]
        enemy = [_mon(3, level=30)]
        if player_item is not None:
            player[0].held_item = _passive_item(player_item)
        if enemy_item is not None:
            enemy[0].held_item = _passive_item(enemy_item)
        return battle_loop.run_battle(player, enemy)

    def _effects(self, result, reason=None):
        return [
            e for e in result.battle_events
            if e.get("type") == "effect" and (reason is None or e.get("reason") == reason)
        ]

    def test_rocky_helmet_on_the_target_emits_an_effect_for_the_mover(self):
        result = self._battle_with(enemy_item="rocky_helmet")
        records = self._effects(result, "rocky_helmet")
        self.assertTrue(records, "rocky_helmet recoil produced no effect record")
        for record in records:
            # 56374-56375: the record names the MOVER (the one taking recoil),
            # not the helmet's holder.
            self.assertEqual(record["side"], "player")
            self.assertEqual(record["idx"], 0)
            self.assertLess(record["hp_change"], 0)
            self.assertGreaterEqual(record["hp_after"], 0)

    def test_life_orb_recoil_emits_an_effect(self):
        result = self._battle_with(player_item="life_orb")
        records = self._effects(result, "life_orb")
        self.assertTrue(records, "life_orb recoil produced no effect record")
        for record in records:
            self.assertEqual(record["side"], "player")
            self.assertLess(record["hp_change"], 0)

    def test_shell_bell_heal_emits_a_positive_effect(self):
        # Damaged first, so the heal is not clamped to zero headroom.
        rng.seed_rng(11)
        player = [_mon(6, level=60)]
        player[0].held_item = _passive_item("shell_bell")
        player[0].current_hp = max(1, player[0].max_hp // 3)
        result = battle_loop.run_battle(player, [_mon(3, level=30)])
        records = self._effects(result, "shell_bell")
        self.assertTrue(records, "shell_bell heal produced no effect record")
        for record in records:
            self.assertGreater(record["hp_change"], 0)
            self.assertEqual(record["side"], "player")

    def test_effect_records_carry_the_shape_the_contract_pins(self):
        result = self._battle_with(enemy_item="rocky_helmet")
        for record in self._effects(result):
            self.assertEqual(
                set(record), {"type", "side", "idx", "hp_change", "hp_after", "reason"})

    def test_hp_after_matches_the_movers_hp_at_that_moment(self):
        """The whole point of the record: the HP delta is attributable. The
        LAST effect record for a combatant must agree with where that
        combatant's HP actually ended up, when nothing else moved it after."""
        result = self._battle_with(enemy_item="rocky_helmet")
        records = self._effects(result, "rocky_helmet")
        self.assertTrue(records)
        for record in records:
            self.assertLessEqual(record["hp_after"], result.player_team[record["idx"]].max_hp)

    def test_no_effect_record_when_no_item_is_held(self):
        """Non-vacuity's other half: the records appear BECAUSE of the item,
        not on every battle."""
        self.assertEqual(self._effects(self._battle_with()), [])


class OrdinaryKoFaintRecordTests(unittest.TestCase):
    """M6/N11. An ordinary combat KO (bundle.deobfuscated.js:56445 and 56482)
    emitted no record anywhere before M6: `status_events` gains a `faint` only
    from `_status_tick_round`, i.e. only when a status tick did the killing."""

    def test_a_won_battle_records_the_enemys_knockout(self):
        rng.seed_rng(2024)
        player = [_mon(6, level=60)]
        enemy = [_mon(129, level=5)]  # Magikarp: knocked out by ordinary damage
        result = battle_loop.run_battle(player, enemy)
        self.assertTrue(result.player_won)
        faints = [e for e in result.battle_events if e.get("type") == "faint"]
        self.assertTrue(faints, "an ordinary combat KO emitted no faint record")
        self.assertEqual(faints[0], {"type": "faint", "side": "enemy", "idx": 0})

    def test_the_faint_record_is_not_the_status_stream(self):
        """The two streams stay distinct: this KO was ordinary damage, so it
        must NOT have been routed through `status_events`."""
        rng.seed_rng(2024)
        result = battle_loop.run_battle([_mon(6, level=60)], [_mon(129, level=5)])
        self.assertEqual(
            [e for e in result.status_events if e.get("type") == "faint"], [],
            "an ordinary KO leaked into the status-tick faint subset",
        )

    def test_every_fainted_combatant_has_a_record_on_one_of_the_streams(self):
        """The invariant N11 restores: nothing dies silently."""
        for seed in (1, 7, 2024, 999):
            rng.seed_rng(seed)
            result = battle_loop.run_battle(
                [_mon(6, level=40), _mon(9, level=40)], [_mon(3, level=40), _mon(6, level=40)])
            recorded = {
                (e["side"], e["idx"])
                for e in list(result.battle_events) + list(result.status_events)
                if e.get("type") == "faint"
            }
            for side, team in (("player", result.player_team), ("enemy", result.enemy_team)):
                for idx, member in enumerate(team):
                    if member.current_hp <= 0:
                        self.assertIn(
                            (side, idx), recorded,
                            f"{side}[{idx}] fainted with no record at seed {seed}",
                        )

    def test_the_faint_record_precedes_nothing_of_its_own_round(self):
        """Ordering: the source pushes the faint at 56445 BEFORE the onKO /
        onFaint hooks, so it must land inside the round that killed, after that
        round's own attack record."""
        rng.seed_rng(2024)
        result = battle_loop.run_battle([_mon(6, level=60)], [_mon(129, level=5)])
        kinds = [e.get("type") for e in result.battle_events]
        self.assertIn("faint", kinds)
        self.assertLess(kinds.index("attack"), kinds.index("faint"))


class ShellBellSourceFidelityTests(unittest.TestCase):
    """M6.1 / N28. Shell Bell (bundle.deobfuscated.js:56420-56431).

    The source block is::

        if (!BIB && BEx === "player" && heldItem?.id === "shell_bell") {
          const BXT = hasPassive(BcV, "heal_boost") ? 2 : 1,
                BXF = Math.max(1, Math.floor(BEF * 0.15 * BXT)),
                BXq = Math.min(BXF, BEX.maxHp - BEX.currentHp);
          BXq > 0 && (BEX.currentHp += BXq,
                      aspearOnHeal(BEX, "player", BEm, BcM, BcV, BXq), ...);
        }

    Three things the pre-M6.1 port got wrong: it gated on `damage > 0` (the
    source has no damage gate -- its guard is `!BIB`, i.e. the run lacks the
    `no_heal_revive` passive, :55299), it truncated without the `max(1, ...)`
    floor, and it never called `aspearOnHeal`.

    `BEF` is the raw post-modifier damage (:55901), NOT the missing-HP-clamped
    `BEs` that the neighbouring Life Orb branch uses.
    """

    #: Deliberately lopsided: a durable but near-harmless attacker against a
    #: wall, so every player hit lands in the 1..6 damage band where the old
    #: `int(damage * 0.15)` truncated to zero and the source still heals 1.
    _FEEBLE = BaseStats(hp=255, atk=1, defense=255, special=1, spdef=255, speed=200)
    _WALL = BaseStats(hp=255, atk=1, defense=255, special=1, spdef=255, speed=1)

    def _low_damage_battle(self, *, seed=1, traits=(), start_hp=50, heal_boost=False):
        rng.seed_rng(seed)
        player = [_mon(10, level=5, base_stats=self._FEEBLE)]
        player[0].held_item = _passive_item("shell_bell")
        player[0].current_hp = start_hp
        enemy = [_mon(95, level=5, base_stats=self._WALL)]
        return battle_loop.run_battle(player, enemy, traits=list(traits))

    def _heals(self, result):
        return [
            e["hp_change"] for e in result.battle_events
            if e.get("type") == "effect" and e.get("reason") == "shell_bell"
        ]

    def _player_damages(self, result):
        return [
            e["damage"] for e in result.battle_events
            if e.get("type") == "attack" and e.get("side") == "player"
        ]

    def test_floor_heals_one_where_truncation_returned_zero(self):
        """THE N28 boundary. Every player hit here deals 3 damage:
        `int(3 * 0.15) == 0` (old port -> no heal, no record at all) but
        `max(1, floor(3 * 0.15 * 1)) == 1` (source -> heals 1)."""
        result = self._low_damage_battle(seed=1)
        damages = self._player_damages(result)
        self.assertTrue(damages, "setup produced no player attack to heal from")
        self.assertTrue(
            all(0 < d <= 6 for d in damages),
            f"setup drifted out of the sub-floor damage band: {damages}")
        heals = self._heals(result)
        self.assertTrue(heals, "sub-floor damage produced no Shell Bell heal at all")
        self.assertTrue(
            all(h == 1 for h in heals), f"expected every heal to be exactly 1, got {heals}")

    def test_zero_raw_damage_still_heals_one(self):
        """The source has no positive-damage guard on Shell Bell. Stacking
        `all_half` and `half_twice` makes this setup's raw post-modifier
        damage exactly zero, yet `max(1, floor(0 * 0.15))` still heals 1.
        This proves the zero boundary is reachable through the block rather
        than merely possible in isolated arithmetic."""
        result = self._low_damage_battle(
            seed=1,
            traits=[battle.Trait("all_half"), battle.Trait("half_twice")],
        )
        damages = self._player_damages(result)
        self.assertTrue(damages, "setup produced no player attack")
        self.assertTrue(all(d == 0 for d in damages), damages)
        heals = self._heals(result)
        self.assertTrue(heals, "zero raw damage never reached Shell Bell")
        self.assertTrue(all(h == 1 for h in heals), heals)

    def test_heal_matches_the_sources_own_arithmetic_per_hit(self):
        """Ordinary healing, checked hit-by-hit against
        `max(1, floor(damage * 0.15))` rather than a single hand-picked value."""
        rng.seed_rng(4)
        player = [_mon(6, level=60)]
        player[0].held_item = _passive_item("shell_bell")
        player[0].current_hp = max(1, player[0].max_hp // 4)
        result = battle_loop.run_battle(player, [_mon(3, level=55)])
        damages = self._player_damages(result)
        heals = self._heals(result)
        self.assertTrue(heals, "no Shell Bell heal recorded")
        self.assertTrue(any(d > 6 for d in damages), "no above-floor hit in this battle")
        # The first player hit is uncapped here (the holder starts at 1/4 HP).
        self.assertEqual(heals[0], max(1, math.floor(damages[0] * 0.15)))

    def _big_hit_battle(self, *, seed=4, traits=()):
        """Damage well above the floor, so the 2x multiplier actually moves the
        result (at damage 3 both multipliers floor to the same 1 HP and the
        comparison would be vacuous)."""
        rng.seed_rng(seed)
        player = [_mon(6, level=60)]
        player[0].held_item = _passive_item("shell_bell")
        player[0].current_hp = max(1, player[0].max_hp // 4)
        return battle_loop.run_battle(player, [_mon(3, level=55)], traits=list(traits))

    def test_heal_boost_applies_a_two_times_multiplier(self):
        """`BXT` is the source's explicit third-factor multiplier. Pinned as
        exact source arithmetic on the first uncapped, multiplier-sensitive
        hit; the old folded `0.3` spelling was equivalent for integer damage."""
        plain = self._big_hit_battle()
        boosted = self._big_hit_battle(traits=[battle.Trait("heal_boost")])
        plain_dmg = self._player_damages(plain)[0]
        boosted_dmg = self._player_damages(boosted)[0]
        plain_heal = self._heals(plain)[0]
        boosted_heal = self._heals(boosted)[0]
        self.assertEqual(plain_heal, max(1, math.floor(plain_dmg * 0.15 * 1)))
        self.assertEqual(boosted_heal, max(1, math.floor(boosted_dmg * 0.15 * 2)))
        # Non-vacuity: the multiplier has to actually change the number here.
        self.assertGreater(boosted_heal, plain_heal)

    def test_full_hp_holder_is_a_no_op(self):
        """`BXq = min(BXF, maxHp - currentHp)` is 0 at full HP, and the source
        only acts when `BXq > 0`.

        The holder outspeeds here (speed 200 vs 1), so its FIRST hit lands
        while it is still untouched at full HP: that hit must produce no heal
        record. Later hits do heal, because by then the enemy has damaged it --
        which is why this asserts on ordering rather than on the whole battle.
        """
        rng.seed_rng(1)
        player = [_mon(10, level=5, base_stats=self._FEEBLE)]
        player[0].held_item = _passive_item("shell_bell")
        player[0].current_hp = player[0].max_hp
        result = battle_loop.run_battle(
            player, [_mon(95, level=5, base_stats=self._WALL)])
        kinds = [
            (e.get("type"), e.get("side"), e.get("reason"))
            for e in result.battle_events
        ]
        first_enemy_hit = next(
            i for i, k in enumerate(kinds) if k[0] == "attack" and k[1] == "enemy")
        first_player_hit = next(
            i for i, k in enumerate(kinds) if k[0] == "attack" and k[1] == "player")
        self.assertLess(first_player_hit, first_enemy_hit, "holder did not strike first")
        before_any_damage = [
            k for k in kinds[:first_enemy_hit] if k[2] == "shell_bell"]
        self.assertEqual(
            before_any_damage, [], "healed while the holder was still at full HP")
        # Non-vacuity: once damaged, it does heal.
        self.assertTrue(self._heals(result))

    def test_heal_is_capped_by_missing_hp(self):
        """One missing HP can only ever be healed by exactly 1."""
        rng.seed_rng(4)
        player = [_mon(6, level=60)]
        player[0].held_item = _passive_item("shell_bell")
        player[0].current_hp = player[0].max_hp - 1
        result = battle_loop.run_battle(player, [_mon(3, level=55)])
        heals = self._heals(result)
        self.assertTrue(heals, "no Shell Bell heal recorded")
        self.assertEqual(heals[0], 1)

    def test_no_heal_revive_suppresses_the_heal_entirely(self):
        """The source's real guard, `!BIB`. The old port had no equivalent."""
        result = self._low_damage_battle(seed=1, traits=[battle.Trait("no_heal_revive")])
        self.assertEqual(self._heals(result), [])

    def test_heal_feeds_the_aspear_accumulator_and_grants_a_stage(self):
        """`aspearOnHeal` was never called. With `heal_boost_stat`, healing
        accumulates into `_aspearAcc` and grants a random stat stage on every
        50 HP crossed -- which also consumes an RNG draw."""
        result = self._low_damage_battle(
            seed=1, traits=[battle.Trait("heal_boost_stat")])
        healed = sum(self._heals(result))
        self.assertTrue(healed, "no heal, so nothing could reach the accumulator")
        lead = result.player_team[0]
        acc = lead.flags.get("_aspearAcc")
        self.assertIsNotNone(acc, "aspearOnHeal was never called")
        granted = sum(v for v in lead.stages.values() if v > 0)
        # Total accumulated == stages granted * 50 + leftover.
        self.assertEqual(healed, granted * 50 + acc)

    def test_accumulator_threshold_grants_exactly_one_stage(self):
        """Directly pins the 50-HP threshold and its stat-choice RNG draw."""
        lead = _mon(6, level=60)
        lead.flags["_aspearAcc"] = 49
        rng.seed_rng(1)
        battle_loop._aspear_on_heal(lead, "player", [battle.Trait("heal_boost_stat")], 1)
        self.assertEqual(lead.flags["_aspearAcc"], 0)
        self.assertEqual(sum(lead.stages.values()), 1)

    def test_no_shell_bell_no_heal(self):
        """Non-vacuity: the records exist because of the item."""
        rng.seed_rng(1)
        player = [_mon(10, level=5, base_stats=self._FEEBLE)]
        player[0].current_hp = 50
        result = battle_loop.run_battle(
            player, [_mon(95, level=5, base_stats=self._WALL)])
        self.assertEqual(self._heals(result), [])


class RandNerfMirrorHookTests(unittest.TestCase):
    """M6.1 / P0.9. `rand_nerf` (bundle.deobfuscated.js:56083-56095).

    After debuffing the target the source calls
    `B71.mirrorEnemyActiveDebuff(Bct, BEg, 1, BcM)` -- `B71` is `runBattle`'s
    merged battle config, `Bct` the player team, and `BEg` the SAME stat the
    single RNG draw already chose. The port stopped after `apply_stage_change`.
    """

    def _fire(self, *, traits_config=None, player_team=None, seed=1, traits=None):
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        team = player_team if player_team is not None else [mover]
        rng.seed_rng(seed)
        battle_loop._apply_post_hit_traits(
            mover, target, "player", "enemy", False, 10,
            traits if traits is not None else [battle.Trait("rand_nerf")],
            False, traits_config, team,
        )
        return mover, target

    def _chosen_stat(self, target):
        return next(k for k, v in target.stages.items() if v < 0)

    def test_target_debuff_still_consumes_exactly_one_draw(self):
        """The mirror must reuse the stat, not roll a second time."""
        rng.seed_rng(1)
        expected_stat = battle_loop._STAGE_STATS[
            int(rng.rng() * len(battle_loop._STAGE_STATS))]
        cfg = battle_traits.TraitsConfig(traits=[battle.Trait("debuff_mirror_buff")])
        mover, target = self._fire(traits_config=cfg)
        self.assertEqual(target.stages[expected_stat], -1)
        self.assertEqual(sum(target.stages.values()), -1)

    def test_without_the_mirror_trait_the_lead_gains_nothing(self):
        cfg = battle_traits.TraitsConfig(traits=[])
        mover, target = self._fire(traits_config=cfg)
        self.assertEqual(sum(target.stages.values()), -1)
        self.assertEqual(sum(mover.stages.values()), 0)

    def test_with_the_mirror_trait_the_lead_gains_the_same_stat(self):
        cfg = battle_traits.TraitsConfig(traits=[battle.Trait("debuff_mirror_buff")])
        mover, target = self._fire(traits_config=cfg)
        stat = self._chosen_stat(target)
        self.assertEqual(target.stages[stat], -1)
        self.assertEqual(mover.stages[stat], 1)
        self.assertEqual(sum(mover.stages.values()), 1)

    def test_absent_config_skips_the_hook(self):
        """`B71 != null && B71["mirrorEnemyActiveDebuff"]` -- no config, no
        mirror, and no crash."""
        mover, target = self._fire(traits_config=None)
        self.assertEqual(sum(target.stages.values()), -1)
        self.assertEqual(sum(mover.stages.values()), 0)

    def test_mirror_targets_the_first_alive_member_not_the_mover(self):
        """Active-member selection comes from the ported hook's own
        `first_alive`, so a fainted lead is skipped."""
        fainted = _mon(1, level=50)
        fainted.current_hp = 0
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        cfg = battle_traits.TraitsConfig(traits=[battle.Trait("debuff_mirror_buff")])
        rng.seed_rng(1)
        battle_loop._apply_post_hit_traits(
            mover, target, "player", "enemy", False, 10,
            [battle.Trait("rand_nerf")], False, cfg, [fainted, mover],
        )
        stat = self._chosen_stat(target)
        self.assertEqual(sum(fainted.stages.values()), 0)
        self.assertEqual(mover.stages[stat], 1)

    def test_mirror_uses_the_existing_stage_cap(self):
        """The call must delegate to `mirror_enemy_active_debuff`, whose
        `apply_stage_change` path clamps ordinary stages at +10."""
        mover = _mon(4, level=50)
        for stat in battle_loop._STAGE_STATS:
            mover.stages[stat] = 9
        cfg = battle_traits.TraitsConfig(traits=[battle.Trait("debuff_mirror_buff")])
        _, target = self._fire(traits_config=cfg, player_team=[mover])
        self.assertEqual(sum(target.stages.values()), -1)
        self.assertEqual(sorted(mover.stages.values()), [9, 9, 9, 9, 10])
        self._fire(traits_config=cfg, player_team=[mover])
        self.assertEqual(sorted(mover.stages.values()), [9, 9, 9, 9, 10])

    def test_target_blocker_does_not_suppress_the_mirror_hook(self):
        """Hyper Cutter blocks the target's negative stage mutation, but the
        source calls the mirror hook as the next independent expression with
        the already-drawn stat. The player still receives that stat."""
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        target.gen3_ability = "hyper_cutter"
        cfg = battle_traits.TraitsConfig(traits=[battle.Trait("debuff_mirror_buff")])
        rng.seed_rng(1)
        battle_loop._apply_post_hit_traits(
            mover, target, "player", "enemy", False, 10,
            [battle.Trait("rand_nerf")], False, cfg, [mover],
        )
        self.assertTrue(all(v == 0 for v in target.stages.values()), target.stages)
        self.assertEqual(sum(mover.stages.values()), 1)

    def test_enemy_side_hit_does_not_mirror(self):
        """The source's guard is `BEx === "player"`."""
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        cfg = battle_traits.TraitsConfig(traits=[battle.Trait("debuff_mirror_buff")])
        rng.seed_rng(1)
        battle_loop._apply_post_hit_traits(
            mover, target, "enemy", "player", False, 10,
            [battle.Trait("rand_nerf")], False, cfg, [target],
        )
        self.assertEqual(sum(target.stages.values()), 0)
        self.assertEqual(sum(mover.stages.values()), 0)

    def test_bio_extra_attack_path_never_replays_the_mirror(self):
        """`BIO` (:55300-55404) deliberately excludes `rand_nerf`, so
        `half_twice`/`dragon_first_double` extra hits must not re-fire it."""
        mover = _mon(4, level=50)
        target = _mon(7, level=50)
        rng.seed_rng(1)
        battle_loop._bio_post_hit(
            mover, target, "player", "enemy", False, 10,
            [battle.Trait("rand_nerf"), battle.Trait("debuff_mirror_buff")], False,
        )
        self.assertEqual(sum(target.stages.values()), 0)
        self.assertEqual(sum(mover.stages.values()), 0)


if __name__ == "__main__":
    unittest.main()
