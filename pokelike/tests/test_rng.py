"""Tests for pokelike/rng.py.

The seed -> sequence pairs below are NOT hand-derived -- they were captured
by running a verbatim transcription of seededRng/rng/seedRng/weightedRandom
(bundle.deobfuscated.js:59917-59923 and 74818-74833, 53206-53213) via Node
against the actual bundle arithmetic, then asserted here bit-for-bit. See
docs/logic-notes.md section 6 for the source line citations.

Run with: python -m unittest pokelike.tests.test_rng -v
(stdlib unittest only -- no pytest/other deps required.)
"""

from __future__ import annotations

import unittest

from pokelike import rng


class SeededRngTests(unittest.TestCase):
    """Stream A -- docs/logic-notes.md 6.1, JS seededRng at line 59917."""

    def test_seed_0(self):
        gen = rng.seeded_rng(0)
        draws = [gen() for _ in range(5)]
        self.assertEqual(
            draws,
            [
                0.3588899802416563,
                0.10590326134115458,
                0.675290479324758,
                0.9179345588199794,
                0.10157715040259063,
            ],
        )

    def test_seed_1(self):
        gen = rng.seeded_rng(1)
        draws = [gen() for _ in range(5)]
        self.assertEqual(
            draws,
            [
                0.18967728852294385,
                0.31778763560578227,
                0.7830808095168322,
                0.5346624867524952,
                0.7819289951585233,
            ],
        )

    def test_seed_12345(self):
        gen = rng.seeded_rng(12345)
        draws = [gen() for _ in range(5)]
        self.assertEqual(
            draws,
            [
                0.5975837109144777,
                0.6314287825953215,
                0.6228211561683565,
                0.4621145336423069,
                0.8787191782612354,
            ],
        )

    def test_seed_max_uint32(self):
        gen = rng.seeded_rng(0xFFFFFFFF)
        draws = [gen() for _ in range(5)]
        self.assertEqual(
            draws,
            [
                0.9919345872476697,
                0.46276153065264225,
                0.22555770655162632,
                0.53311463794671,
                0.9203745848499238,
            ],
        )

    def test_independent_generators_dont_share_state(self):
        gen_a = rng.seeded_rng(5)
        gen_b = rng.seeded_rng(5)
        self.assertEqual([gen_a() for _ in range(3)], [gen_b() for _ in range(3)])
        gen_a()
        self.assertNotEqual(gen_a(), gen_b())

    def test_rollregion_style_seeding(self):
        # rollRegion (bundle.deobfuscated.js:60607): seededRng(stageNum*1000 + regionIndex)
        gen = rng.seeded_rng(1 * 1000 + 1)
        draws = [gen() for _ in range(3)]
        self.assertEqual(
            draws,
            [0.7379179219715297, 0.07395282201468945, 0.05443135625682771],
        )
        gen = rng.seeded_rng(10 * 1000 + 5)
        draws = [gen() for _ in range(3)]
        self.assertEqual(
            draws,
            [0.9414034320507199, 0.1855313580017537, 0.5464905947446823],
        )


class GlobalStreamTests(unittest.TestCase):
    """Stream B -- docs/logic-notes.md 6.1, JS rng()/seedRng()/getRngSeed()
    at lines 74818-74833. Same update step as Stream A, but seedRng does NOT
    golden-ratio-mix the seed -- these sequences differ from Stream A's even
    for the same seed value (verified below via test_diverges_from_stream_a).
    """

    def test_seed_0(self):
        rng.seed_rng(0)
        draws = [rng.rng() for _ in range(5)]
        self.assertEqual(
            draws,
            [
                0.26642920868471265,
                0.0003297457005828619,
                0.2232720274478197,
                0.1462021479383111,
                0.46732782293111086,
            ],
        )
        self.assertEqual(rng.get_rng_seed(), 567894473)

    def test_seed_12345(self):
        rng.seed_rng(12345)
        self.assertEqual(rng.get_rng_seed(), 12345)
        draws = [rng.rng() for _ in range(5)]
        self.assertEqual(
            draws,
            [
                0.9797282677609473,
                0.3067522644996643,
                0.484205421525985,
                0.817934412509203,
                0.5094283693470061,
            ],
        )
        self.assertEqual(rng.get_rng_seed(), 567906818)

    def test_seed_max_uint32(self):
        rng.seed_rng(0xFFFFFFFF)
        draws = [rng.rng() for _ in range(5)]
        self.assertEqual(
            draws,
            [
                0.8964226141106337,
                0.189478256739676,
                0.7156526781618595,
                0.9440599093213677,
                0.8452364315744489,
            ],
        )

    def test_reseeding_reproduces_sequence(self):
        rng.seed_rng(999)
        first = [rng.rng() for _ in range(5)]
        rng.seed_rng(999)
        second = [rng.rng() for _ in range(5)]
        self.assertEqual(first, second)

    def test_diverges_from_stream_a_for_same_seed(self):
        rng.seed_rng(12345)
        stream_b_draw = rng.rng()
        stream_a_draw = rng.seeded_rng(12345)()
        self.assertNotEqual(stream_a_draw, stream_b_draw)


class WeightedRandomTests(unittest.TestCase):
    """docs/logic-notes.md 6.5; JS weightedRandom at bundle.deobfuscated.js:53206."""

    def test_matches_js_sequence_for_seed_42(self):
        rng.seed_rng(42)
        bag = {"a": 1, "b": 2, "c": 7}
        picks = [rng.weighted_random(bag) for _ in range(10)]
        self.assertEqual(
            picks, ["c", "c", "c", "c", "b", "c", "b", "c", "c", "c"]
        )

    def test_zero_weight_never_picked(self):
        rng.seed_rng(1)
        bag = {"never": 0, "always": 1}
        picks = {rng.weighted_random(bag) for _ in range(50)}
        self.assertEqual(picks, {"always"})

    def test_single_entry_always_picked(self):
        rng.seed_rng(7)
        bag = {"only": 3}
        for _ in range(10):
            self.assertEqual(rng.weighted_random(bag), "only")


class NewRunSeedTests(unittest.TestCase):
    def test_returns_uint32(self):
        seed = rng.new_run_seed()
        self.assertGreaterEqual(seed, 0)
        self.assertLessEqual(seed, 0xFFFFFFFF)

    def test_not_deterministic_across_calls(self):
        seeds = {rng.new_run_seed() for _ in range(5)}
        self.assertGreater(len(seeds), 1)


class Mulberry32Tests(unittest.TestCase):
    def test_matches_module_level_stream_b(self):
        gen = rng.Mulberry32(0)
        gen.seed(12345)
        draws = [gen() for _ in range(5)]
        rng.seed_rng(12345)
        module_draws = [rng.rng() for _ in range(5)]
        self.assertEqual(draws, module_draws)


if __name__ == "__main__":
    unittest.main()
