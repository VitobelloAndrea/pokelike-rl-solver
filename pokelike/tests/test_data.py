"""Smoke tests for pokelike/data.py -- sanity-checks the ported tables
against known facts from docs/logic-notes.md and real Pokemon trivia
(species counts, specific stat lines, type chart spot checks), not
exhaustive validation of every entry.

Run with: python -m unittest pokelike.tests.test_data -v
(stdlib unittest only -- no pytest/other deps required, since none were
available in this environment at the time this was written.)
"""

from __future__ import annotations

import unittest

from pokelike import data


class PokedexTests(unittest.TestCase):
    def test_species_count(self):
        pokedex = data.get_pokedex()
        self.assertEqual(len(pokedex), 721)

    def test_bulbasaur(self):
        mon = data.get_pokedex()[1]
        self.assertEqual(mon.name, "Bulbasaur")
        self.assertEqual(mon.types, ("Grass", "Poison"))
        self.assertEqual(mon.base_stats.hp, 45)
        self.assertEqual(mon.base_stats.spdef, 65)

    def test_mewtwo(self):
        mon = data.get_pokedex()[150]
        self.assertEqual(mon.name, "Mewtwo")
        self.assertEqual(mon.base_stats.special, 154)

    def test_cached_singleton(self):
        self.assertIs(data.get_pokedex(), data.get_pokedex())


class MovePoolTests(unittest.TestCase):
    def test_all_18_types_present(self):
        pool = data.get_move_pool()
        self.assertEqual(len(pool), 18)

    def test_fire_moves(self):
        fire = data.get_move_pool()["Fire"]
        self.assertEqual(len(fire["physical"]), 3)
        self.assertEqual(len(fire["special"]), 3)
        names = {m.name for m in fire["physical"]} | {m.name for m in fire["special"]}
        self.assertIn("Flamethrower", names)
        flamethrower = next(m for m in fire["special"] if m.name == "Flamethrower")
        self.assertEqual(flamethrower.power, 90)
        self.assertEqual(flamethrower.category, "special")
        self.assertEqual(flamethrower.type, "Fire")

    def test_legendary_signature_moves(self):
        moves = data.get_legendary_signature_moves()
        articuno = moves[144]
        self.assertEqual(articuno.name, "Freeze-Dry")
        self.assertEqual(articuno.type, "Ice")


class TypeChartTests(unittest.TestCase):
    def test_fire_resists_fire(self):
        chart = data.get_type_chart()
        self.assertEqual(chart["Fire"]["Fire"], 0.5)
        self.assertEqual(chart["Fire"]["Grass"], 2)
        self.assertEqual(chart["Fire"]["Water"], 0.5)

    def test_get_type_effectiveness_single_type(self):
        self.assertEqual(data.get_type_effectiveness("Fire", ["Grass"]), 2.0)
        self.assertEqual(data.get_type_effectiveness("Water", ["Fire"]), 2.0)

    def test_get_type_effectiveness_dual_type_stacks(self):
        # Ice vs Dragon/Flying (e.g. a Dragonite-like target): both weak to Ice -> 4x
        self.assertEqual(data.get_type_effectiveness("Ice", ["Dragon", "Flying"]), 4.0)

    def test_missing_pairing_is_neutral(self):
        chart = {"Weird": {}}
        self.assertEqual(data.get_type_effectiveness("Weird", ["Normal"], type_chart=chart), 1.0)

    def test_inverse_mode_flips_and_immunity_becomes_supereffective(self):
        # Ghost vs Normal is a 0x immunity in mainline-style charts.
        chart = data.get_type_chart()
        normal_effectiveness = data.get_type_effectiveness("Ghost", ["Normal"], type_chart=chart)
        self.assertEqual(normal_effectiveness, 0)
        inverse_effectiveness = data.get_type_effectiveness("Ghost", ["Normal"], type_chart=chart, inverse=True)
        self.assertEqual(inverse_effectiveness, 2.0)  # immunity -> super-effective under Inverse, per logic-notes.md


class ItemTests(unittest.TestCase):
    def test_usable_items(self):
        items = data.get_usable_items()
        self.assertTrue(any(i.id == "sacred_ash" for i in items))
        self.assertTrue(all(i.usable for i in items))

    def test_passive_items_have_no_drop_weight_field(self):
        items = data.get_passive_items()
        self.assertGreater(len(items), 0)
        # tier is only meaningful on Mega Stones (unlock requirement, not a
        # drop weight) -- most passive items should have tier=None.
        self.assertTrue(any(i.tier is None for i in items))

    def test_type_item_map(self):
        m = data.get_type_item_map()
        self.assertEqual(m["Fire"], "charcoal")
        self.assertEqual(len(m), 18)


class EvolutionTests(unittest.TestCase):
    def test_bulbasaur_evolves_to_ivysaur(self):
        evo = data.get_evolutions()[1]
        self.assertEqual(evo.into, 2)
        self.assertEqual(evo.name, "Ivysaur")
        self.assertEqual(evo.level, 16)

    def test_eevee_branches(self):
        branches = data.get_branching_evolutions()[133]
        self.assertEqual(len(branches), 8)
        names = {b.name for b in branches}
        self.assertIn("Vaporeon", names)
        self.assertIn("Sylveon", names)

    def test_bulbasaur_not_in_branching(self):
        self.assertNotIn(1, data.get_branching_evolutions())


class TrainerTests(unittest.TestCase):
    def test_gen1_gym_leaders(self):
        leaders = data.get_gym_leaders(1)
        self.assertEqual(len(leaders), 8)
        brock = leaders[0]
        self.assertEqual(brock.name, "Brock")
        self.assertEqual(brock.badge, "Boulder Badge")
        self.assertEqual(brock.team[0].name, "Geodude")

    def test_gen1_trainer_mons_lack_spdef(self):
        # A confirmed, real quirk of the source data -- not a bug in this
        # port. See BaseStats/TrainerPokemon docstrings in data.py.
        leaders = data.get_gym_leaders(1)
        for leader in leaders:
            for mon in leader.team:
                self.assertIsNone(mon.base_stats.spdef)

    def test_effective_base_spdef_falls_back_to_special(self):
        # Brock's Geodude: hp=40, atk=80, def=100, speed=20, special=30, no spdef.
        geodude = data.get_gym_leaders(1)[0].team[0]
        self.assertIsNone(geodude.base_stats.spdef)
        self.assertEqual(data.effective_base_spdef(geodude.base_stats), 30)

    def test_effective_base_spdef_prefers_real_spdef_when_present(self):
        mon = data.get_pokedex()[1]  # Bulbasaur: spdef=65, special=65 (same by coincidence)
        stats = data.BaseStats(hp=1, atk=1, defense=1, speed=1, special=99, spdef=65)
        self.assertEqual(data.effective_base_spdef(stats), 65)

    def test_elite_four_has_no_badge(self):
        elite4 = data.get_elite_four(1)
        self.assertEqual(len(elite4), 5)
        self.assertIsNone(elite4[0].badge)


class MapConfigTests(unittest.TestCase):
    def test_bst_ranges(self):
        ranges = data.get_map_bst_ranges(1)
        self.assertEqual(len(ranges), 9)
        self.assertEqual(ranges[0].min, 200)
        self.assertEqual(ranges[0].max, 310)

    def test_level_ranges_normalized_from_pairs(self):
        ranges = data.get_map_level_ranges(1)
        self.assertEqual(len(ranges), 9)
        self.assertEqual(ranges[0].min, 1)
        self.assertEqual(ranges[0].max, 5)

    def test_fallback_species_pool_tiers(self):
        pool = data.get_fallback_species_pool()
        self.assertEqual(len(pool.low), 212)
        self.assertIn(1, pool.low)  # Bulbasaur


class WildEncounterEligibilityTests(unittest.TestCase):
    def test_never_wild_ids(self):
        self.assertEqual(data.get_never_wild_ids(), frozenset({292}))

    def test_legendary_ids_count_matches_signature_moves(self):
        # every legendary has a signature-move override, so the two tables
        # should be the same size (both loaded independently from source).
        self.assertEqual(len(data.get_legendary_ids()), len(data.get_legendary_signature_moves()))
        self.assertIn(150, data.get_legendary_ids())  # Mewtwo

    def test_legendary_egg_ids_excludes_egg_excluded(self):
        egg_ids = set(data.get_legendary_egg_ids())
        excluded = data.get_egg_excluded_legendary_ids()
        self.assertTrue(egg_ids.isdisjoint(excluded))
        self.assertTrue(egg_ids.issubset(data.get_legendary_ids()))

    def test_legendary_pools_are_subsets_of_legendary_ids(self):
        legendary_ids = data.get_legendary_ids()
        self.assertTrue(set(data.get_legendary_pool_high()).issubset(legendary_ids))
        self.assertTrue(set(data.get_legendary_pool_very_high()).issubset(legendary_ids))

    def test_starter_ids_per_generation(self):
        self.assertEqual(data.get_starter_ids(1), (1, 4, 7))  # Bulbasaur/Charmander/Squirtle
        self.assertEqual(len(data.get_starter_ids(2)), 3)
        self.assertEqual(len(data.get_starter_ids(3)), 3)
        self.assertEqual(len(data.get_starter_ids(4)), 3)

    def test_gen1_with_gen2_evo_is_10_species(self):
        self.assertEqual(len(data.get_gen1_with_gen2_evo()), 10)

    def test_gen4_route1_banned_and_forced_disjoint(self):
        banned = data.get_gen4_route1_banned()
        forced = set(data.get_gen4_route1_forced())
        self.assertTrue(banned.isdisjoint(forced))


class TraitRequirementTests(unittest.TestCase):
    def test_trait_required_type(self):
        req = data.get_trait_required_type()
        self.assertEqual(req["elec_chain"], "Electric")

    def test_trait_required_cond(self):
        req = data.get_trait_required_cond()
        self.assertEqual(req["shiny_dmg"], "shiny")
        self.assertEqual(req["legend_dmg"], "legendary")


class PokemonFormTests(unittest.TestCase):
    def test_form_slug_resolves_to_base_species(self):
        slugs = data.get_pokemon_form_slugs()
        self.assertEqual(slugs["charizard-mega-x"], 6)  # base Charizard
        self.assertIn(slugs["charizard-mega-x"], data.get_pokedex())

    def test_form_dex_ids_are_synthetic_pokeapi_ids(self):
        ids = data.get_pokemon_form_dex_ids()
        self.assertGreaterEqual(ids["deoxys-attack"], 10000)


if __name__ == "__main__":
    unittest.main()
