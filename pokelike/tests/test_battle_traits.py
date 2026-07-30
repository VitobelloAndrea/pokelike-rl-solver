"""Tests for pokelike/battle_traits.py.

**Validation depth, stated plainly** (same caveat as
test_battle_abilities.py): individual formulas were cross-checked against
docs/logic-notes-traitsconfig.md's exhaustive, chunk-by-chunk read of the
real source, not against a fresh live Node execution of this ~2,878-line
closure. Tests below check the ported formulas against that doc's
transcriptions and basic invariants.

Run with: python -m unittest pokelike.tests.test_battle_traits -v
"""

from __future__ import annotations

import unittest

from pokelike import battle, battle_traits as bt, rng
from pokelike.data import BaseStats


def _mon(species_id=1, atk=50, defense=50, speed=50, special=50, spdef=50, types=("Normal",), level=50, is_shiny=False):
    bs = BaseStats(hp=100, atk=atk, defense=defense, speed=speed, special=special, spdef=spdef)
    return battle.Combatant(
        species_id=species_id, level=level, base_stats=bs, types=types,
        max_hp=100, current_hp=100, is_shiny=is_shiny,
    )


class ComputeTraitTiersTests(unittest.TestCase):
    def test_single_type_team_weight_and_tier(self):
        # 3 Fire-type teammates, weight 1 each -> accumulated 3 -> tier = floor(3/2) = 1
        team = [_mon(types=("Fire",)) for _ in range(3)]
        tiers = bt.compute_trait_tiers(team)
        self.assertEqual(tiers.get("Fire"), 1)

    def test_dual_type_counts_toward_both(self):
        team = [_mon(types=("Fire", "Flying"))] * 1
        # duplicate weight manually since list of same object would double-count oddly; use distinct instances
        team = [_mon(types=("Fire", "Flying")) for _ in range(4)]
        tiers = bt.compute_trait_tiers(team)
        self.assertEqual(tiers.get("Fire"), 2)
        self.assertEqual(tiers.get("Flying"), 2)

    def test_shiny_adds_extra_weight(self):
        team = [_mon(types=("Water",), is_shiny=True), _mon(types=("Water",))]
        # weights: shiny=2, normal=1 -> total 3 -> tier=1
        tiers = bt.compute_trait_tiers(team)
        self.assertEqual(tiers.get("Water"), 1)

    def test_capped_at_trait_max_tier(self):
        team = [_mon(types=("Grass",)) for _ in range(40)]
        tiers = bt.compute_trait_tiers(team)
        self.assertEqual(tiers.get("Grass"), bt.TRAIT_MAX_TIER)

    def test_team_reroll_and_legend_traits_add_weight(self):
        legendary_id = next(iter(__import__("pokelike.data", fromlist=["data"]).get_legendary_ids()))
        team = [_mon(species_id=legendary_id, types=("Psychic",))]
        traits = [battle.Trait("legend_traits")]
        tiers = bt.compute_trait_tiers(team, traits=traits)
        # weight = 1 (base) + 1 (legendary) = 2 -> tier = 1
        self.assertEqual(tiers.get("Psychic"), 1)


class AttackerDamageModTests(unittest.TestCase):
    def setUp(self):
        self.bc = battle.BattleConfig()

    def test_ec_deal_more_only_applies_when_player_attacks(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("ec_deal_more")])
        attacker = _mon(1)
        defender = _mon(2)
        self.assertEqual(cfg.attacker_damage_mod(attacker, defender, "player", 100, self.bc), 110)
        self.assertEqual(cfg.attacker_damage_mod(attacker, defender, "enemy", 100, self.bc), 100)

    def test_execute_dmg_requires_target_not_full_hp(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("execute_dmg")])
        attacker = _mon(1)
        defender = _mon(2)
        defender.current_hp = 50
        self.assertEqual(cfg.attacker_damage_mod(attacker, defender, "player", 100, self.bc), 135)
        defender.current_hp = defender.max_hp
        self.assertEqual(cfg.attacker_damage_mod(attacker, defender, "player", 100, self.bc), 100)

    def test_lvl_overpower_scales_with_level_advantage(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("lvl_overpower")])
        attacker = _mon(1, level=60)
        defender = _mon(2, level=50)
        # +10 levels -> +100% (1 + 0.1*10)
        self.assertEqual(cfg.attacker_damage_mod(attacker, defender, "player", 100, self.bc), 200)


class BeforeDamageTests(unittest.TestCase):
    def setUp(self):
        self.bc = battle.BattleConfig()

    def test_ec_take_less_reduces_incoming_damage_for_player(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("ec_take_less")])
        defender = _mon(1)
        attacker = _mon(2)
        self.assertEqual(cfg.before_damage(defender, "player", attacker, 100, self.bc), 85)
        self.assertEqual(cfg.before_damage(defender, "enemy", attacker, 100, self.bc), 100)

    def test_def_onhit_all_grants_defensive_stages(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("def_onhit_all")])
        defender = _mon(1)
        attacker = _mon(2)
        cfg.before_damage(defender, "player", attacker, 10, self.bc)
        self.assertEqual(defender.stages["def"], 1)
        self.assertEqual(defender.stages["spdef"], 1)


class TierGatedHooksTests(unittest.TestCase):
    def test_steel_tier_reduces_damage(self):
        cfg = bt.TraitsConfig(player_tiers={"Steel": 4})
        defender = _mon(1)
        attacker = _mon(2)
        bc = battle.BattleConfig()
        result = cfg.before_damage(defender, "player", attacker, 100, bc)
        # reduction = floor(100 * min(0.9, 0.15*4)) = floor(100*0.6) = 60
        self.assertEqual(result, 40)

    def test_trigger_fighting_rally_requires_fighting_tier(self):
        cfg = bt.TraitsConfig(player_tiers={})
        team = [_mon(1)]
        cfg.trigger_fighting_rally("player", team, [])
        self.assertEqual(team[0].stages["atk"], 0)

        cfg2 = bt.TraitsConfig(player_tiers={"Fighting": 3})
        cfg2.trigger_fighting_rally("player", team, [])
        self.assertEqual(team[0].stages["atk"], 3)
        self.assertEqual(team[0].stages["special"], 3)


class OnKOTests(unittest.TestCase):
    def test_ko_maxhp_applies_to_player_lead_on_enemy_faint(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("ko_maxhp")])
        bc = battle.BattleConfig()
        player_team = [_mon(1)]
        enemy_team = [_mon(2)]
        enemy_team[0].current_hp = 0
        cfg.on_ko(enemy_team[0], "enemy", 0, player_team[0], "player", 0, player_team, enemy_team, bc)
        self.assertEqual(player_team[0].max_hp, 102)

    def test_dedup_via_kos_handled(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("ko_maxhp")])
        bc = battle.BattleConfig()
        player_team = [_mon(1)]
        enemy_team = [_mon(2)]
        enemy_team[0].current_hp = 0
        cfg.on_ko(enemy_team[0], "enemy", 0, player_team[0], "player", 0, player_team, enemy_team, bc)
        cfg.on_ko(enemy_team[0], "enemy", 0, player_team[0], "player", 0, player_team, enemy_team, bc)
        self.assertEqual(player_team[0].max_hp, 102)  # not applied twice


class BeforeTurnTests(unittest.TestCase):
    def test_sword_charm_one_shot_atk_boost_and_skip(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("sword_charm")])
        bc = battle.BattleConfig()
        attacker = _mon(1, types=("Normal",))
        result = cfg.before_turn(attacker, None, "player", bc)
        self.assertEqual(result, "skip")
        self.assertEqual(attacker.stages["atk"], 4)
        # one-shot: second call should not fire again
        attacker2 = _mon(1, types=("Normal",))
        result2 = cfg.before_turn(attacker2, None, "player", bc)
        self.assertIsNone(result2)

    def test_before_turn_is_player_only(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("sword_charm")])
        bc = battle.BattleConfig()
        attacker = _mon(1, types=("Normal",))
        result = cfg.before_turn(attacker, None, "enemy", bc)
        self.assertIsNone(result)
        self.assertEqual(attacker.stages["atk"], 0)


class PsychicSplashNestedEffectsTests(unittest.TestCase):
    """CODEX.md issue 13: Psychic type-tier splash (bundle.deobfuscated.js:
    61876-61976) re-runs a NARROWER Ghost-execute-heal-only pass and a
    NARROWER Grass-heal-only pass for each splash victim, nested inside the
    splash loop -- neither `ghost_heal`/`ghost_curse` nor `grass_burst`/
    `heal_boost_stat` apply in the nested versions."""

    def test_nested_ghost_execute_heals_attacker_by_tier_formula(self):
        attacker = _mon(species_id=1)
        attacker.current_hp = 50
        target = _mon(species_id=2)
        victim = _mon(species_id=3)
        # Splash (50 dmg, computed below) must land FIRST, then the
        # POST-splash fraction must be under the tier-4 threshold (0.5):
        # 70 - 50 = 20, 20/100 = 0.2 < 0.5.
        victim.current_hp = 70
        cfg = bt.TraitsConfig(player_tiers={"Psychic": 5, "Ghost": 4})
        cfg.after_attack(attacker, "player", target, "enemy", 100, [attacker], [target, victim])
        self.assertEqual(victim.current_hp, 0)
        # psychic splash amount = max(1, int(100 * min(1, 0.5))) = 50
        # ghost heal-back = min(int(victim.maxHp * 0.1 * 4), missing=50) = 40
        self.assertEqual(attacker.current_hp, 50 + 40)

    def test_nested_ghost_execute_does_not_apply_ghost_heal_or_curse(self):
        attacker = _mon(species_id=1)
        attacker.current_hp = 50
        target = _mon(species_id=2)
        victim = _mon(species_id=3)
        # Post-splash: 55 - 50 = 5, 5/100 = 0.05 < threshold min(0.5, 0.15) = 0.15.
        victim.current_hp = 55
        teammate = _mon(species_id=4)
        cfg = bt.TraitsConfig(player_tiers={"Psychic": 5, "Ghost": 1}, traits=[battle.Trait("ghost_curse")])
        cfg.after_attack(attacker, "player", target, "enemy", 100, [attacker], [target, victim, teammate])
        self.assertEqual(victim.current_hp, 0)
        # Ghost tier 1 < 4 -> no tier heal-back; ghost_curse must NOT fire
        # from the nested call (only the main, non-splash Ghost-execute does).
        self.assertEqual(attacker.current_hp, 50)
        self.assertEqual(sum(teammate.stages.values()), 0)

    def test_nested_grass_heal_uses_splash_amount_not_original_damage(self):
        # Grass tier >= 1 fires BOTH the main (non-splash) heal AND the
        # Psychic-splash-nested heal in the same `after_attack` call -- they
        # are independent mechanisms, not mutually exclusive.
        attacker = _mon(species_id=1)
        attacker.current_hp = 10
        target = _mon(species_id=2)
        victim = _mon(species_id=3)
        cfg = bt.TraitsConfig(player_tiers={"Psychic": 5, "Grass": 2})
        cfg.after_attack(attacker, "player", target, "enemy", 100, [attacker], [target, victim])
        # main heal = max(1, int(100*0.15*2)) = 30 -> 10+30 = 40
        # splash amount = max(1, int(100*min(1,0.5))) = 50 (target excluded, victim hit)
        # nested grass heal = max(1, int(50*0.15*2)) = 15 -> 40+15 = 55
        self.assertEqual(attacker.current_hp, 55)

    def test_nested_grass_heal_does_not_apply_grass_burst_or_stat_gain(self):
        attacker = _mon(species_id=1)
        attacker.current_hp = 10
        target = _mon(species_id=2)
        victim = _mon(species_id=3)
        cfg = bt.TraitsConfig(
            player_tiers={"Psychic": 5, "Grass": 2},
            traits=[battle.Trait("grass_burst"), battle.Trait("heal_boost_stat")],
        )
        cfg.after_attack(attacker, "player", target, "enemy", 100, [attacker], [target, victim])
        # main heal (30) triggers grass_burst ONCE, hitting every opposing
        # member (including the original target) for 30; the nested grass
        # heal (triggered separately by the Psychic splash) must NOT apply
        # a second grass_burst hit to `victim`.
        self.assertEqual(target.current_hp, target.max_hp - 30)
        self.assertEqual(victim.current_hp, victim.max_hp - 30 - 50)  # burst(30) + splash(50), no double burst
        # heal_boost_stat must fire exactly once (from the main heal), not
        # a second time from the nested grass heal.
        self.assertEqual(attacker.flags.get("_aspearCount"), 1)

    def test_main_grass_heal_blocked_by_no_heal_revive(self):
        attacker = _mon(species_id=1)
        attacker.current_hp = 10
        target = _mon(species_id=2)
        cfg = bt.TraitsConfig(player_tiers={"Grass": 2}, traits=[battle.Trait("no_heal_revive")])
        cfg.after_attack(attacker, "player", target, "enemy", 100, [attacker], [target])
        self.assertEqual(attacker.current_hp, 10)


class SecondaryAttackHookReentryTests(unittest.TestCase):
    """`elec_lead`/`fairy_opening_volley`/`rock_explode` hook re-entry
    (fixed 2026-07-29) -- see `_apply_elec_lead`/`_apply_fairy_opening_volley`/
    the `rock_explode` block in `on_ko`'s own docstrings/comments in
    battle_traits.py for the exact source citations. The real cross-language
    proof is `tools/battle-oracle/fixtures/elec_lead_*.json`,
    `fairy_opening_volley_multi.json`, and `rock_explode_fanout.json` -- the
    tests here isolate gates/boundaries an oracle fixture can't cheaply
    pin down on its own (the `calc_damage(..., None)` argument specifically,
    and which internal helper fires under which condition).
    """

    def setUp(self):
        self.bc = battle.BattleConfig()
        rng.seed_rng(12345)

    def _spy_calc_damage(self):
        calls = []
        original = bt.calc_damage

        def spy(attacker, defender, move, attacker_items, defender_items, traits, side, battle_config):
            calls.append(battle_config)
            return original(attacker, defender, move, attacker_items, defender_items, traits, side, battle_config)

        bt.calc_damage = spy
        self.addCleanup(setattr, bt, "calc_damage", original)
        return calls

    def test_elec_lead_calls_calc_damage_with_none_battle_config(self):
        # bundle.deobfuscated.js:61212 passes a literal `null`, not the real
        # battleConfig -- no weather/darkCritFloor bonus on this hit even if
        # the real battle_config has them (self.bc here has none active
        # either way; this only proves the ARGUMENT passed, not a
        # crit/weather outcome).
        calls = self._spy_calc_damage()
        cfg = bt.TraitsConfig(traits=[battle.Trait("elec_lead")])
        player = [_mon(25, types=("Electric",))]
        enemy = [_mon(2)]
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0])

    def test_fairy_opening_volley_calls_calc_damage_with_none_battle_config(self):
        calls = self._spy_calc_damage()
        cfg = bt.TraitsConfig(traits=[battle.Trait("fairy_opening_volley")])
        player = [_mon(35, types=("Fairy",))]
        enemy = [_mon(2)]
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0])

    def test_rock_explode_calls_calc_damage_with_none_battle_config(self):
        calls = self._spy_calc_damage()
        cfg = bt.TraitsConfig(traits=[battle.Trait("rock_explode")])
        fainted = _mon(74, types=("Rock",))
        fainted.current_hp = 0
        player_team = [fainted]
        enemy_team = [_mon(2)]
        cfg.on_ko(fainted, "player", 0, None, None, None, player_team, enemy_team, self.bc)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0])

    def test_elec_lead_selects_first_alive_electric_not_team_lead(self):
        # bundle.deobfuscated.js:61188-61190: `BIi["findIndex"](currentHp>0
        # && Electric)` -- the first ALIVE Electric member, not index 0.
        cfg = bt.TraitsConfig(traits=[battle.Trait("elec_lead")])
        lead = _mon(1, types=("Normal",))
        sparky = _mon(25, types=("Electric",))
        player = [lead, sparky]
        enemy = [_mon(2)]
        enemy[0].current_hp = 100000  # survive regardless of roll
        enemy[0].max_hp = 100000
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertTrue(self.bc.elec_lead_fired)
        self.assertLess(enemy[0].current_hp, 100000)

    def test_elec_lead_nonfatal_calls_after_attack_but_not_on_ko(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("elec_lead"), battle.Trait("elec_chain")])
        player = [_mon(25, types=("Electric",))]
        enemy = [_mon(2)]
        enemy[0].current_hp = 100000
        enemy[0].max_hp = 100000
        cfg.on_start_fight(player, enemy, self.bc)
        # elec_chain grants exactly +1 speed stage from the one connecting
        # hit -- proves after_attack fired exactly once, not zero (missing)
        # or twice (double-fire).
        self.assertEqual(player[0].stages["speed"], 1)

    def test_elec_lead_fatal_still_calls_on_ko(self):
        # `onKO` fires whenever `target.current_hp == 0` (bundle.
        # deobfuscated.js:61278-61290: `BEZ["currentHp"] === 0x0 &&
        # this["onKO"] && this["onKO"](...)`), independent of whether
        # afterAttack's own internal effects had anything to fire.
        cfg = bt.TraitsConfig(traits=[battle.Trait("elec_lead"), battle.Trait("poison_pass")])
        attacker = _mon(25, types=("Electric",))
        player = [attacker]
        victim = _mon(2)
        victim.current_hp = 1
        victim.poison_stacks = 4
        survivor = _mon(3)
        enemy = [victim, survivor]
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertEqual(victim.current_hp, 0)
        self.assertEqual(survivor.poison_stacks, 2)  # on_ko's poison_pass fired

    def test_elec_lead_fatal_hit_does_not_trigger_elec_chain(self):
        # `after_attack` IS still called on a fatal elec_lead hit (see
        # `test_elec_lead_fatal_still_calls_on_ko`'s poison_pass proof and
        # `_apply_elec_lead`'s docstring: `actual_damage > 0` alone gates the
        # outer call), but elec_chain's OWN internal effect requires the
        # target still alive (bundle.deobfuscated.js:61441-61444, see
        # `test_after_attack_gates_elec_chain_on_target_still_alive`) -- a
        # fatal hit must NOT grant the speed stage, unlike a nonfatal one
        # (`test_elec_lead_nonfatal_calls_after_attack_but_not_on_ko`).
        cfg = bt.TraitsConfig(traits=[battle.Trait("elec_lead"), battle.Trait("elec_chain")])
        attacker = _mon(25, types=("Electric",))
        player = [attacker]
        victim = _mon(2)
        victim.current_hp = 1  # guaranteed kill
        enemy = [victim]
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertEqual(victim.current_hp, 0)
        self.assertEqual(attacker.stages["speed"], 0)

    def test_fairy_opening_volley_is_multi_attacker_with_target_relookup(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("fairy_opening_volley")])
        fairy1 = _mon(35, types=("Fairy",))
        fairy2 = _mon(35, types=("Fairy",))
        player = [fairy1, fairy2]
        victim0 = _mon(2)
        victim0.current_hp = 1  # dies to fairy1, forcing fairy2 to re-find the first alive enemy
        victim1 = _mon(3)
        victim1.current_hp = 100000
        victim1.max_hp = 100000
        enemy = [victim0, victim1]
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertEqual(victim0.current_hp, 0)
        self.assertLess(victim1.current_hp, 100000)  # fairy2 hit victim1, not the already-dead victim0

    def test_fairy_opening_volley_skips_after_attack_on_fatal_hit(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("fairy_opening_volley"), battle.Trait("fairy_hit_two")])
        player = [_mon(35, types=("Fairy",))]
        victim = _mon(2)
        victim.current_hp = 1  # guaranteed kill
        bystander = _mon(3)
        bystander.current_hp = 100000
        bystander.max_hp = 100000
        enemy = [victim, bystander]
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertEqual(victim.current_hp, 0)
        # fairy_hit_two's splash lives inside after_attack, which the
        # source's ternary skips entirely on a fatal hit -- bystander must
        # be untouched.
        self.assertEqual(bystander.current_hp, 100000)

    def test_fairy_opening_volley_fires_after_attack_on_nonfatal_hit(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("fairy_opening_volley"), battle.Trait("fairy_hit_two")])
        player = [_mon(35, types=("Fairy",))]
        target = _mon(2)
        target.current_hp = 100000
        target.max_hp = 100000
        bystander = _mon(3)
        bystander.current_hp = 100000
        bystander.max_hp = 100000
        enemy = [target, bystander]
        cfg.on_start_fight(player, enemy, self.bc)
        self.assertLess(bystander.current_hp, 100000)

    def test_rock_explode_computes_damage_per_target_not_a_shared_flat_value(self):
        cfg = bt.TraitsConfig(traits=[battle.Trait("rock_explode")])
        fainted = _mon(74, types=("Rock",), atk=100)
        fainted.current_hp = 0
        frail = _mon(2, defense=1)
        frail.current_hp = 100000
        frail.max_hp = 100000
        tanky = _mon(3, defense=1000)
        tanky.current_hp = 100000
        tanky.max_hp = 100000
        player_team = [fainted]
        enemy_team = [frail, tanky]
        cfg.on_ko(fainted, "player", 0, None, None, None, player_team, enemy_team, self.bc)
        frail_loss = 100000 - frail.current_hp
        tanky_loss = 100000 - tanky.current_hp
        self.assertGreater(frail_loss, 0)
        self.assertGreater(tanky_loss, 0)
        # A correct per-target roll against wildly different defense stats
        # must NOT produce the same splash damage on both -- the pre-fix bug
        # computed one shared value (against the first alive enemy) and
        # applied it flat to every enemy.
        self.assertNotEqual(frail_loss, tanky_loss)
        self.assertGreater(frail_loss, tanky_loss)

    def test_after_attack_gates_elec_chain_on_target_still_alive(self):
        # bundle.deobfuscated.js:61441-61444: poison_onhit/ground_slow_onhit/
        # elec_chain/elec_paralyze are all inside a `&&` chain requiring
        # `target.currentHp > 0` -- a fatal hit (from ANY caller, not just
        # elec_lead) must not trigger elec_chain.
        cfg = bt.TraitsConfig(traits=[battle.Trait("elec_chain")])
        attacker = _mon(1, types=("Electric",))
        target = _mon(2)
        target.current_hp = 0  # already fainted from this same hit
        cfg.after_attack(attacker, "player", target, "enemy", 50, [attacker], [target])
        self.assertEqual(attacker.stages["speed"], 0)

    def test_after_attack_gates_ghost_execute_on_target_still_alive(self):
        # Same gate, independently applied to the Ghost-execute splash
        # (bundle.deobfuscated.js:61486: `B2e("Ghost",BIY) && ... &&
        # BIz["currentHp"] > 0x0`) -- also guards against a ZeroDivisionError-
        # shaped read of `target.current_hp / target.max_hp` on an already-0
        # target if this gate were ever removed.
        cfg = bt.TraitsConfig(player_tiers={"Ghost": 5})
        attacker = _mon(1)
        attacker.current_hp = 10
        target = _mon(2)
        target.current_hp = 0
        cfg.after_attack(attacker, "player", target, "enemy", 50, [attacker], [target])
        self.assertEqual(attacker.current_hp, 10)


if __name__ == "__main__":
    unittest.main()
