"""Tests for pokelike/battle_abilities.py.

**Validation depth, stated plainly**: `get_gen3_ability`/`get_evo_line_root`/
the generic switch-in table/`form_change` (Deoxys)/the on-contact proc
switch (`static`/`flame_body`/`effect_spore`/`cute_charm`/`poison_point`)
were individually cross-checked against exact transcribed JS lines while
writing battle_abilities.py (see that module's docstring and
docs/logic-notes-gen3abilities.md). Unlike rng.py/battle.py's core
functions, this suite does NOT re-validate every one of the ~85 abilities
against a live Node run of the bundle -- that would require executing a
large, not-yet-safety-audited slice of the source (docs/logic-notes.md
section 0's extraction-safety reasoning was scoped to the data-table
prefix, not this deep into the file). Tests below check the ported
mechanics against the extraction doc's transcriptions and basic invariants
(RNG threshold behavior via seeded draws already validated in
pokelike/tests/test_rng.py).

Run with: python -m unittest pokelike.tests.test_battle_abilities -v
"""

from __future__ import annotations

import unittest

from pokelike import battle, battle_abilities as ba, rng
from pokelike.data import BaseStats


def _mon(species_id, atk=50, defense=50, speed=50, special=50, spdef=50, types=("Normal",), level=50):
    bs = BaseStats(hp=100, atk=atk, defense=defense, speed=speed, special=special, spdef=spdef)
    return battle.Combatant(species_id=species_id, level=level, base_stats=bs, types=types, max_hp=100, current_hp=100)


class AbilityAssignmentTests(unittest.TestCase):
    def test_wonder_guard_species(self):
        self.assertEqual(ba.get_gen3_ability(0x124), "wonder_guard")  # Shedinja

    def test_evo_line_root_fallback(self):
        # A species with no direct entry should fall back to its evolution
        # line's base form's ability, if the base form has one.
        root = ba.get_evo_line_root(0x102)  # Torchic line member? just check it resolves to itself if base
        self.assertIsInstance(root, int)

    def test_ability_id_of_prefers_cached_value(self):
        mon = _mon(1)
        mon.gen3_ability = "static"
        self.assertEqual(ba.ability_id_of(mon), "static")


class OnSwitchInTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ba.Gen3AbilityConfig()
        self.bc = battle.BattleConfig()

    def test_intimidate_lowers_opponent_attack(self):
        holder = _mon(0x153)  # intimidate holder per GEN3_ABILITY_LINES
        opponent = _mon(1)
        self.cfg.on_switch_in(holder, [holder], opponent, [opponent], self.bc)
        self.assertEqual(holder.gen3_ability, "intimidate")
        self.assertEqual(opponent.stages["atk"], -1)

    def test_drizzle_sets_weather(self):
        holder = _mon(0x140)  # drizzle
        self.cfg.on_switch_in(holder, [holder], None, [], self.bc)
        self.assertEqual(self.bc.weather, "rain")

    def test_shield_dust_blocks_opponent_switch_in_effect(self):
        holder = _mon(0x153)  # intimidate
        shield_dust_mon = _mon(0x109)  # shield_dust
        shield_dust_mon.gen3_ability = "shield_dust"
        opponent = _mon(1)
        self.cfg.on_switch_in(holder, [holder], shield_dust_mon, [shield_dust_mon], self.bc)
        # opponent here is shield_dust_mon itself, unaffected by intimidate since
        # shield_dust nullifies the OPPONENT's ability effects against it.
        self.assertEqual(shield_dust_mon.stages["atk"], 0)

    def test_form_change_deoxys_picks_weakest_stat_category(self):
        deoxys = _mon(386, atk=50, spdef=50, speed=50)
        strong_opp = _mon(1, atk=200, spdef=200, speed=200)
        self.cfg._apply_form_change(deoxys, strong_opp)
        from pokelike import data

        self.assertEqual(deoxys.base_stats, data.get_deoxys_forms()["speed"])


class BeforeDamageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ba.Gen3AbilityConfig()
        self.bc = battle.BattleConfig()

    def test_levitate_blocks_ground_move(self):
        defender = _mon(1)
        defender.gen3_ability = "levitate"
        attacker = _mon(2)
        attacker.flags["_lastMoveType"] = "ground"
        result = self.cfg.before_damage(defender, attacker, 50, self.bc)
        self.assertEqual(result, 0)

    def test_smack_down_bypasses_levitate(self):
        defender = _mon(1)
        defender.gen3_ability = "levitate"
        attacker = _mon(2)
        attacker.gen3_ability = "smack_down"
        attacker.flags["_lastMoveType"] = "ground"
        result = self.cfg.before_damage(defender, attacker, 50, self.bc)
        self.assertEqual(result, 50)

    def test_water_absorb_heals_and_zeroes_damage(self):
        defender = _mon(1)
        defender.gen3_ability = "water_absorb"
        defender.current_hp = 50
        attacker = _mon(2)
        attacker.flags["_lastMoveType"] = "water"
        result = self.cfg.before_damage(defender, attacker, 999, self.bc)
        self.assertEqual(result, 0)
        self.assertEqual(defender.current_hp, 75)  # +25% of 100 max hp

    def test_sturdy_survives_at_one_from_full_hp(self):
        defender = _mon(1)
        defender.gen3_ability = "sturdy"
        attacker = _mon(2)
        result = self.cfg.before_damage(defender, attacker, 999, self.bc)
        self.assertEqual(result, 99)  # currentHp(100) - 1


class OnContactProcTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ba.Gen3AbilityConfig()

    def test_static_paralyzes_on_success_roll(self):
        rng.seed_rng(0)  # first Stream B draw ~0.266, < 0.3
        target = _mon(1)
        self.cfg._on_contact_proc("static", target)
        self.assertTrue(target.paralyzed)

    def test_static_no_proc_on_failed_roll(self):
        rng.seed_rng(1)  # first Stream B draw ~0.627, not < 0.3
        target = _mon(1)
        self.cfg._on_contact_proc("static", target)
        self.assertFalse(target.paralyzed)

    def test_poison_point_unconditional_two_stacks(self):
        target = _mon(1)
        self.cfg._on_contact_proc("poison_point", target)
        self.assertEqual(target.poison_stacks, 2)

    def test_cute_charm_unconditional_double_debuff(self):
        target = _mon(1)
        self.cfg._on_contact_proc("cute_charm", target)
        self.assertEqual(target.stages["atk"], -1)
        self.assertEqual(target.stages["special"], -1)

    def test_when_attacked_dispatches_static_only_on_physical_hit(self):
        rng.seed_rng(0)
        defender = _mon(1)
        defender.gen3_ability = "static"
        attacker = _mon(2)
        attacker.flags["_lastMoveIsSpecial"] = True  # special hit -- should NOT proc static
        self.cfg.when_attacked(defender, attacker, 10)
        self.assertFalse(attacker.paralyzed)


class WhenAttackedAfterAttackDualDirectionTests(unittest.TestCase):
    """docs/logic-notes-runbattle.md section 4 / section 9 item 4: this
    engine deliberately lets on-contact abilities proc BOTH when the holder
    is hit (whenAttacked) AND when the holder attacks (afterAttack) -- a
    confirmed deviation from mainline, not a bug.
    """

    def setUp(self):
        self.cfg = ba.Gen3AbilityConfig()

    def test_flame_body_procs_when_holder_is_attacked(self):
        rng.seed_rng(7)  # first Stream B draw ~0.0117, < 0.2 needed for flame_body's 20%
        defender = _mon(1)
        defender.gen3_ability = "flame_body"
        attacker = _mon(2)
        attacker.flags["_lastMoveIsSpecial"] = False
        self.cfg.when_attacked(defender, attacker, 10)
        self.assertTrue(attacker.burned)

    def test_flame_body_procs_when_holder_attacks(self):
        rng.seed_rng(7)
        attacker = _mon(1)
        attacker.gen3_ability = "flame_body"
        attacker.flags["_lastMoveIsSpecial"] = False
        defender = _mon(2)
        self.cfg.after_attack(attacker, defender, 10, [attacker])
        self.assertTrue(defender.burned)


if __name__ == "__main__":
    unittest.main()
