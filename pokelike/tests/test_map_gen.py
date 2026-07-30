"""Tests for pokelike/map_gen.py.

Every seed->output pair below (full layer/type layouts, the edge list, the
legendary-node hash, `get_bst_bucket`, `get_catch_choices`,
`get_level_for_node`) was validated bit-for-bit against a verbatim
transcription of `generateMap`/`getBstBucket`/`getCatchChoices`/
`getLevelForNode` run through Node with `state` left undefined (so every
`typeof state != "undefined"` guard in the source short-circuits false,
matching this port's own "no global state object yet" scope) -- not
eyeballed. See pokelike/map_gen.py's module docstring for exactly what's
in scope vs. explicitly deferred (generateSubMap, the Endless-mode
buff-pool path, etc).

Run with: python -m unittest pokelike.tests.test_map_gen -v
"""

from __future__ import annotations

import unittest

from pokelike import map_gen as mg, rng


class GenerateMapStructureTests(unittest.TestCase):
    def test_layer_widths(self):
        rng.seed_rng(1)
        m = mg.generate_map(0)
        self.assertEqual([len(layer) for layer in m.layers], [1, 2, 3, 4, 3, 4, 3, 2, 1])

    def test_seed_1_full_layout_matches_js(self):
        rng.seed_rng(1)
        m = mg.generate_map(0)
        types = [[n.type for n in layer] for layer in m.layers]
        self.assertEqual(
            types,
            [
                ["start"],
                ["catch", "battle"],
                ["item", "battle", "catch"],
                ["trade", "trade", "catch", "trainer"],
                ["question", "trainer", "trade"],
                ["trainer", "trainer", "battle", "trainer"],
                ["catch", "catch", "trainer"],
                ["battle", "pokecenter"],
                ["boss"],
            ],
        )

    def test_edges_match_js_for_seed_1(self):
        rng.seed_rng(1)
        m = mg.generate_map(0)
        expected = [
            ("n0_0", "n1_0"), ("n0_0", "n1_1"),
            ("n1_0", "n2_0"), ("n1_0", "n2_1"), ("n1_1", "n2_1"), ("n1_1", "n2_2"),
            ("n2_0", "n3_0"), ("n2_0", "n3_1"), ("n2_1", "n3_1"), ("n2_1", "n3_2"), ("n2_2", "n3_2"), ("n2_2", "n3_3"),
            ("n3_0", "n4_0"), ("n3_1", "n4_0"), ("n3_1", "n4_1"), ("n3_2", "n4_1"), ("n3_2", "n4_2"), ("n3_3", "n4_2"),
            ("n4_0", "n5_0"), ("n4_0", "n5_1"), ("n4_1", "n5_1"), ("n4_1", "n5_2"), ("n4_2", "n5_2"), ("n4_2", "n5_3"),
            ("n5_0", "n6_0"), ("n5_1", "n6_0"), ("n5_1", "n6_1"), ("n5_2", "n6_1"), ("n5_2", "n6_2"), ("n5_3", "n6_2"),
            ("n6_0", "n7_0"), ("n6_1", "n7_0"), ("n6_1", "n7_1"), ("n6_2", "n7_1"),
            ("n7_0", "n8_0"), ("n7_1", "n8_0"),
        ]
        self.assertEqual(m.edges, expected)

    def test_start_node_visited_and_neighbors_accessible(self):
        rng.seed_rng(1)
        m = mg.generate_map(0)
        self.assertTrue(m.nodes["n0_0"].visited)
        self.assertTrue(m.nodes["n1_0"].accessible)
        self.assertTrue(m.nodes["n1_1"].accessible)
        self.assertFalse(m.nodes["n2_0"].accessible)

    def test_pokecenter_always_present_in_last_middle_layer(self):
        for seed in range(50):
            rng.seed_rng(seed)
            m = mg.generate_map(0)
            with self.subTest(seed=seed):
                self.assertTrue(any(n.type == mg.POKECENTER for n in m.layers[7]))

    def test_nuzlocke_mode_forces_matching_layer1_pair(self):
        rng.seed_rng(2)
        m = mg.generate_map(0, nuzlocke_mode=True)
        types = {n.type for n in m.layers[1]}
        self.assertEqual(types, {"catch"})

    def test_gen2_silver_node_on_odd_maps_only(self):
        rng.seed_rng(5)
        m = mg.generate_map(3, gen2_mode=True)
        self.assertEqual([n.type for n in m.layers[4]], ["trainer", "silver", "battle"])

    def test_gen2_no_silver_node_on_even_maps(self):
        rng.seed_rng(5)
        m = mg.generate_map(2, gen2_mode=True)
        self.assertNotIn("silver", [n.type for n in m.layers[4]])


class LegendaryNodeAssignmentTests(unittest.TestCase):
    def test_legendary_hash_matches_js(self):
        rng.seed_rng(1)
        m = mg.generate_map(7, run_seed=12345)
        legendary_nodes = {n.id: n.extra["legendarySpeciesId"] for layer in m.layers for n in layer if n.type == mg.LEGENDARY}
        self.assertEqual(legendary_nodes, {"n4_2": 145})

    def test_legendary_species_in_gen_range(self):
        rng.seed_rng(1)
        m = mg.generate_map(7, run_seed=999, gen2_mode=True)
        for layer in m.layers:
            for n in layer:
                if n.type == mg.LEGENDARY:
                    self.assertTrue(0x98 <= n.extra["legendarySpeciesId"] <= 0xFB)


class BstBucketTests(unittest.TestCase):
    def test_matches_js_bucket_none_mode(self):
        bucket = mg.get_bst_bucket(0x190, "none")
        self.assertEqual(bucket[:5], (26, 36, 40, 44, 55))
        self.assertEqual(len(bucket), 113)

    def test_endless_mode_unions_extra_tier(self):
        none_bucket = mg.get_bst_bucket(0x154, "none")
        endless_bucket = mg.get_bst_bucket(0x154, "endless")
        self.assertGreater(len(endless_bucket), len(none_bucket))


class GetCatchChoicesTests(unittest.TestCase):
    def test_matches_js_for_known_seeds(self):
        cases = {7: [23, 11, 118], 42: [96, 27, 98], 100: [129, 102, 116]}
        for seed, expected in cases.items():
            rng.seed_rng(seed)
            with self.subTest(seed=seed):
                self.assertEqual(mg.get_catch_choices(0), expected)

    def test_never_includes_legendaries_or_starters(self):
        rng.seed_rng(123)
        choices = mg.get_catch_choices(5, count=9)
        legendary_ids = set(__import__("pokelike.data", fromlist=["data"]).get_legendary_ids())
        starter_ids = set(__import__("pokelike.data", fromlist=["data"]).get_starter_ids(1))
        for species_id in choices:
            self.assertNotIn(species_id, legendary_ids)
            self.assertNotIn(species_id, starter_ids)


class GetLevelForNodeTests(unittest.TestCase):
    def test_matches_js_sequence(self):
        rng.seed_rng(3)
        levels = [mg.get_level_for_node(layer, 4) for layer in range(1, 9)]
        self.assertEqual(levels, [29, 30, 32, 33, 34, 36, 37, 37])

    def test_boss_layer_uses_map_max_level(self):
        rng.seed_rng(1)
        max_level = 37
        self.assertEqual(mg.get_level_for_node(8, 4), max_level)


class ResolveEvoForLevelTests(unittest.TestCase):
    def test_evolves_up_to_final_stage_at_high_level(self):
        self.assertEqual(mg.resolve_evo_for_level(1, 50), 3)  # Bulbasaur -> Venusaur

    def test_devolves_down_at_low_level(self):
        self.assertEqual(mg.resolve_evo_for_level(3, 5), 1)  # Venusaur -> Bulbasaur

    def test_mid_level_lands_on_middle_stage(self):
        self.assertEqual(mg.resolve_evo_for_level(3, 20), 2)  # Venusaur -> Ivysaur


class CalcHpTests(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(mg.calc_hp(45, 50), 105)
        self.assertEqual(mg.calc_hp(100, 1), 13)


if __name__ == "__main__":
    unittest.main()
