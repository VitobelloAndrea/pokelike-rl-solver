"""Tests for pokelike/battle.py.

Every damage/turn-order/stat number asserted below was captured from a
verbatim transcription of calcDamage/getEffectiveStat/the runBattle
turn-order chain (bundle.deobfuscated.js:54825-55711) run through Node
against pokelike/data's real type_chart.json/type_item_map.json/
evolutions.json, then asserted here bit-for-bit -- not hand-derived. See
docs/logic-notes.md sections 7-8 for the source line citations and
section 7.9 for the pre-formula trait modifiers this validates.

Run with: python -m unittest pokelike.tests.test_battle -v
(stdlib unittest only -- no pytest/other deps required.)
"""

from __future__ import annotations

import unittest

from pokelike import battle, rng
from pokelike.data import BaseStats


def _mon(species_id=25, level=50, atk=55, defense=40, speed=90, special=50,
         spdef=50, types=("Electric",), stages=None, **kw) -> battle.Combatant:
    bs = BaseStats(hp=35, atk=atk, defense=defense, speed=speed, special=special, spdef=spdef)
    return battle.Combatant(
        species_id=species_id, level=level, base_stats=bs, types=tuple(types),
        max_hp=100, current_hp=100,
        stages=stages or {"atk": 0, "def": 0, "speed": 0, "special": 0, "spdef": 0},
        **kw,
    )


class CalcDamageTests(unittest.TestCase):
    def test_plain_neutral_hit(self):
        rng.seed_rng(12345)
        attacker = _mon(25, types=("Electric",))
        defender = _mon(1, atk=49, defense=49, speed=45, special=65, spdef=65, types=("Grass", "Poison"))
        move = battle.MoveInstance(power=90, type="Electric")
        result = battle.calc_damage(attacker, defender, move)
        self.assertEqual(result, battle.DamageResult(damage=30, type_eff=0.5, move_type="Electric", crit=False))

    def test_stab_super_effective_burn_halves_physical_atk(self):
        rng.seed_rng(999)
        attacker = _mon(4, atk=52, defense=43, speed=65, special=60, spdef=50, types=("Fire",), burned=True)
        defender = _mon(1, atk=49, defense=49, speed=45, special=65, spdef=65, types=("Grass", "Poison"))
        move = battle.MoveInstance(power=90, type="Fire")
        result = battle.calc_damage(attacker, defender, move)
        self.assertEqual(result, battle.DamageResult(damage=107, type_eff=2.0, move_type="Fire", crit=False))

    def test_choice_band_and_eviolite_and_type_boost_item(self):
        rng.seed_rng(42)
        attacker = _mon(4, atk=52, defense=43, speed=65, special=60, spdef=50, types=("Fire",))
        defender = _mon(1, atk=49, defense=49, speed=45, special=65, spdef=65, types=("Grass", "Poison"))
        move = battle.MoveInstance(power=90, type="Fire")
        attacker_items = [battle.HeldItem("choice_band"), battle.HeldItem("charcoal")]
        defender_items = [battle.HeldItem("eviolite")]  # Bulbasaur (dex 1) can evolve -> applies
        result = battle.calc_damage(attacker, defender, move, attacker_items, defender_items)
        self.assertEqual(result, battle.DamageResult(damage=99, type_eff=2.0, move_type="Fire", crit=False))

    def test_force_crit_bypasses_wonder_guard(self):
        # Deviation from mainline (docs/logic-notes.md 7.4): a crit bypasses
        # Wonder Guard entirely in this engine.
        rng.seed_rng(7)
        attacker = _mon(25, types=("Electric",), force_crit=True)
        defender = _mon(1, atk=49, defense=49, speed=45, special=65, spdef=65,
                         types=("Grass", "Poison"), gen3_ability="wonder_guard")
        move = battle.MoveInstance(power=40, type="Normal")
        result = battle.calc_damage(attacker, defender, move)
        self.assertEqual(result, battle.DamageResult(damage=36, type_eff=1.0, move_type="Normal", crit=True))

    def test_enemy_side_atk_band_and_stat_amp_nerf_player_defense(self):
        # traits belongs to the PLAYER regardless of side; when the enemy
        # attacks, atk_band/stat_amp instead nerf/rescale the player's own
        # defense stat (docs/logic-notes.md 7.9 items 3-4).
        rng.seed_rng(2024)
        attacker = _mon(7, atk=48, defense=65, speed=43, special=50, spdef=64, types=("Water",))
        defender = _mon(4, atk=52, defense=43, speed=65, special=60, spdef=50, types=("Fire",),
                         stages={"atk": 0, "def": 2, "speed": 0, "special": 0, "spdef": 0})
        traits = [battle.Trait("atk_band"), battle.Trait("stat_amp")]
        move = battle.MoveInstance(power=40, type="Water")
        bc = battle.BattleConfig(weather="rain")
        result = battle.calc_damage(attacker, defender, move, traits=traits, side="enemy", battle_config=bc)
        self.assertEqual(result, battle.DamageResult(damage=81, type_eff=2.0, move_type="Water", crit=False))

    def test_crit_chance_overflow_converts_to_bonus_damage(self):
        rng.seed_rng(555)
        attacker = _mon(197, level=60, atk=65, defense=110, speed=45, special=60, spdef=130, types=("Dark",))
        defender = _mon(1)
        traits = [battle.Trait("crit_overflow"), battle.Trait("crit_lifesteal"),
                  battle.Trait("crit_boost"), battle.Trait("dark_lvlcrit")]
        move = battle.MoveInstance(power=60, type="Dark")
        result = battle.calc_damage(attacker, defender, move, traits=traits)
        self.assertEqual(result, battle.DamageResult(damage=153, type_eff=1.0, move_type="Dark", crit=True))

    def test_type_immunity_forces_zero_regardless_of_min_one(self):
        rng.seed_rng(1)
        attacker = _mon(25, types=("Electric",))  # Electric vs Ground -> 0x
        defender = _mon(1, types=("Ground",))
        move = battle.MoveInstance(power=90, type="Electric")
        result = battle.calc_damage(attacker, defender, move)
        self.assertEqual(result.damage, 0)
        self.assertEqual(result.type_eff, 0.0)


class GetEffectiveStatTests(unittest.TestCase):
    def test_positive_stage(self):
        mon = _mon(1, atk=49, defense=49, speed=45, special=65, spdef=65, types=("Grass", "Poison"),
                    stages={"atk": 2, "def": -1, "speed": 0, "special": 0, "spdef": 0})
        self.assertEqual(battle.get_effective_stat(mon, "atk", (), mon.stages), 86)

    def test_negative_stage_and_choice_band_def_penalty(self):
        mon = _mon(1, atk=49, defense=49, speed=45, special=65, spdef=65, types=("Grass", "Poison"),
                    stages={"atk": 2, "def": -1, "speed": 0, "special": 0, "spdef": 0})
        result = battle.get_effective_stat(mon, "def", [battle.HeldItem("choice_band")], mon.stages)
        self.assertEqual(result, 33)

    def test_eviolite_on_evolvable_species(self):
        mon = _mon(1, atk=49, defense=49, speed=45, special=65, spdef=65, types=("Grass", "Poison"))
        result = battle.get_effective_stat(mon, "spdef", [battle.HeldItem("eviolite")], mon.stages)
        self.assertEqual(result, 105)

    def test_eviolite_no_effect_on_fully_evolved_species(self):
        # Venusaur (dex 3) has no further evolution -- can_evolve() is False.
        mon = _mon(3, atk=82, defense=83, speed=80, special=100, spdef=100)
        result = battle.get_effective_stat(mon, "def", [battle.HeldItem("eviolite")], mon.stages)
        self.assertEqual(result, 88)


class TurnOrderTests(unittest.TestCase):
    def _mon(self, speed=90, current_hp=100, max_hp=100, paralyzed=False):
        bs = BaseStats(hp=100, atk=50, defense=50, speed=speed, special=50, spdef=50)
        return battle.Combatant(species_id=1, level=50, base_stats=bs, types=("Normal",),
                                 max_hp=max_hp, current_hp=current_hp, paralyzed=paralyzed)

    def test_plain_speed_compare(self):
        rng.seed_rng(1)
        self.assertEqual(battle.decide_turn_order(self._mon(speed=100), self._mon(speed=90)), "player")

    def test_trick_room_flips_speed_compare(self):
        rng.seed_rng(1)
        self.assertEqual(
            battle.decide_turn_order(self._mon(speed=100), self._mon(speed=90), trick_room=True), "enemy"
        )

    def test_paralysis_halves_speed(self):
        rng.seed_rng(1)
        result = battle.decide_turn_order(self._mon(speed=50), self._mon(speed=90, paralyzed=True))
        self.assertEqual(result, "player")

    def test_quick_claw_both_sides(self):
        rng.seed_rng(3)
        result = battle.decide_turn_order(
            self._mon(speed=10), self._mon(speed=200),
            [battle.HeldItem("quick_claw")], [battle.HeldItem("quick_claw")],
        )
        self.assertEqual(result, "enemy")

    def test_lagging_tail_forces_last_regardless_of_speed(self):
        rng.seed_rng(1)
        result = battle.decide_turn_order(
            self._mon(speed=200), self._mon(speed=10),
            [battle.HeldItem("lagging_tail")], [],
        )
        self.assertEqual(result, "enemy")

    def test_hp_priority_by_percent_not_raw_hp(self):
        rng.seed_rng(1)
        result = battle.decide_turn_order(
            self._mon(speed=50, current_hp=50, max_hp=100), self._mon(speed=90, current_hp=80, max_hp=100),
            traits=[battle.Trait("hp_priority")],
        )
        self.assertEqual(result, "enemy")

    def test_hp_priority_tie_falls_through_to_speed(self):
        rng.seed_rng(1)
        result = battle.decide_turn_order(
            self._mon(speed=120, current_hp=50, max_hp=100), self._mon(speed=90, current_hp=50, max_hp=100),
            traits=[battle.Trait("hp_priority")],
        )
        self.assertEqual(result, "player")


class StatusEffectTests(unittest.TestCase):
    def test_burn_tick(self):
        self.assertEqual(battle.burn_tick_damage(100), 10)

    def test_burn_tick_minimum_one(self):
        self.assertEqual(battle.burn_tick_damage(7), 1)

    def test_poison_tick_scales_with_stacks_uncapped(self):
        self.assertEqual(battle.poison_tick_damage(100, 3), 15)

    def test_poison_tick_minimum_one(self):
        self.assertEqual(battle.poison_tick_damage(5, 1), 1)

    def test_freeze_always_skips_turn(self):
        mon = battle.Combatant(
            species_id=1, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, speed=50, special=50, spdef=50),
            types=("Normal",), max_hp=100, current_hp=100, status="freeze",
        )
        self.assertTrue(battle.resolve_pre_turn_status(mon))
        self.assertEqual(mon.status, "freeze")

    def test_sleep_wake_roll_fails_stays_asleep_and_skips(self):
        rng.seed_rng(1)  # first Stream B draw for seed 1 is ~0.627, NOT < 0.5 -> stays asleep
        mon = battle.Combatant(
            species_id=1, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, speed=50, special=50, spdef=50),
            types=("Normal",), max_hp=100, current_hp=100, status="sleep",
        )
        skipped = battle.resolve_pre_turn_status(mon)
        self.assertTrue(skipped)
        self.assertEqual(mon.status, "sleep")

    def test_sleep_wake_roll_succeeds_clears_status_and_does_not_skip(self):
        rng.seed_rng(0)  # first Stream B draw for seed 0 is ~0.266, < 0.5 -> wakes up
        mon = battle.Combatant(
            species_id=1, level=50, base_stats=BaseStats(hp=100, atk=50, defense=50, speed=50, special=50, spdef=50),
            types=("Normal",), max_hp=100, current_hp=100, status="sleep",
        )
        skipped = battle.resolve_pre_turn_status(mon)
        self.assertFalse(skipped)
        self.assertIsNone(mon.status)


class ApplyStageChangeTests(unittest.TestCase):
    def _mon(self):
        bs = BaseStats(hp=100, atk=50, defense=50, speed=50, special=50, spdef=50)
        return battle.Combatant(species_id=1, level=50, base_stats=bs, types=("Normal",), max_hp=100, current_hp=100)

    def test_capped_at_ten(self):
        mon = self._mon()
        battle.apply_stage_change(mon, "atk", 15)
        self.assertEqual(mon.stages["atk"], 10)

    def test_uncap_stages_allows_beyond_ten(self):
        mon = self._mon()
        mon.uncap_stages = True
        battle.apply_stage_change(mon, "atk", 15)
        self.assertEqual(mon.stages["atk"], 15)

    def test_hyper_cutter_blocks_negative_change(self):
        mon = self._mon()
        mon.gen3_ability = "hyper_cutter"
        applied = battle.apply_stage_change(mon, "atk", -3)
        self.assertFalse(applied)
        self.assertEqual(mon.stages["atk"], 0)


class HelperTests(unittest.TestCase):
    def test_uses_special_attack_hardcoded_exceptions(self):
        bs = BaseStats(hp=1, atk=10, defense=1, speed=1, special=999, spdef=1)
        self.assertFalse(battle.uses_special_attack(307, bs))
        self.assertFalse(battle.uses_special_attack(308, bs))
        self.assertTrue(battle.uses_special_attack(1, bs))

    def test_has_passive_opt_out_semantics(self):
        traits = [battle.Trait("foo", enabled=False), battle.Trait("bar")]
        self.assertFalse(battle.has_passive(traits, "foo"))
        self.assertTrue(battle.has_passive(traits, "bar"))

    def test_stage_multiplier(self):
        self.assertEqual(battle.stage_multiplier(0), 1.0)
        self.assertAlmostEqual(battle.stage_multiplier(1), 1.3)
        self.assertAlmostEqual(battle.stage_multiplier(-1), 10 / 13)


if __name__ == "__main__":
    unittest.main()
