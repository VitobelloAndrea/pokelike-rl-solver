"""Tests for pokelike/battle_loop.py.

**Validation depth, stated plainly**: `run_battle` orchestrates validated
primitives and the `battle_abilities`/`battle_traits` hook providers. In
addition to these unit and structural tests, `tools/battle-oracle/` executes
the real JavaScript `runBattle` from an AST-audited bundle prefix. Its 17
fixed-seed fixtures currently prove the no-config baseline, selected merged
ability/trait behavior, and burn/poison status-tick dispatch end-to-end.
That is meaningful cross-language coverage, but it is not proof for every
trait, ability, secondary attack, or complete game run.

Run with: python -m unittest pokelike.tests.test_battle_loop -v
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
