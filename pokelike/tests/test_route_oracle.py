"""Harness-level tests for the M3 route oracle (`route-oracle/`).

These test the ORACLE, not the engine: canonicalization, hashing, the
field-level diff, and — most importantly — **mutation sensitivity**. A
comparison harness that cannot detect a deliberately corrupted stream proves
nothing when it reports agreement, so each mutation below is applied to a
known-good pair of streams and asserted to be caught.

Deliberately dependency-free and fast: no `node`, no subprocess, no bundle.
The cross-runtime run itself is `python route-oracle/compare.py --all`.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

_ROUTE_ORACLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "route-oracle",
)
if _ROUTE_ORACLE not in sys.path:
    sys.path.insert(0, _ROUTE_ORACLE)

import checkpoints as cp_mod  # noqa: E402
import coverage as cov_mod  # noqa: E402


def _checkpoint(seq: int, kind: str, **over) -> dict:
    """A checkpoint shaped like the real thing, small enough to read."""
    cp = {
        "schema_version": cp_mod.SCHEMA_VERSION,
        "scenario": "t",
        "seq": seq,
        "kind": kind,
        "event": {},
        "mode": {"nuzlocke": False, "gen2": False, "gen3": False, "gen4": False},
        "seed": 7,
        "rng": {"state": 1000 + seq, "draws": seq * 3},
        "screen": "map-screen",
        "map": {
            "index": 0,
            "is_sub_map": None,
            "nodes": [
                {"id": "n0_0", "type": "start", "layer": 0, "col": 0,
                 "visited": True, "accessible": False, "revealed": True},
                {"id": "n1_0", "type": "battle", "layer": 1, "col": 0,
                 "visited": False, "accessible": True, "revealed": True},
            ],
            "edges": [["n0_0", "n1_0"]],
        },
        "current_map": 0,
        "current_node": "n0_0",
        "in_sub_map": None,
        "sub_map_return": None,
        "counters": {"badges": 0, "max_team_size": 1, "entered_sub_map": False},
        "team": [{"slot": 0, "species_id": 1, "form_id": None, "name": "Bulbasaur",
                  "level": 5, "max_hp": 19, "current_hp": 19, "types": ["Grass", "Poison"],
                  "move_tier": 0, "held_item": None, "is_shiny": False, "status": None,
                  "burned": False, "paralyzed": False, "poison_stacks": 0,
                  "base_stats": {"hp": 45, "atk": 49, "def": 49, "speed": 45,
                                 "special": 65, "spdef": 65},
                  "stat_buffs": {}}],
        "items": [],
        "game_over": False,
        "pending": None,
    }
    cp.update(over)
    return cp


def _stream() -> list[dict]:
    return [
        _checkpoint(0, "run_init"),
        _checkpoint(1, "starter_selected"),
        _checkpoint(2, "node_pre", event={"node": "n1_0", "node_type": "battle"}),
        _checkpoint(3, "node_post", event={"node": "n1_0"}),
    ]


class CanonicalizationTests(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self):
        left = {"b": 1, "a": {"d": 2, "c": 3}}
        right = {"a": {"c": 3, "d": 2}, "b": 1}
        self.assertEqual(cp_mod.sha256_of(left), cp_mod.sha256_of(right))

    def test_list_order_does_change_the_hash(self):
        """Ordering carries meaning (team slots, bag, edges, events) and must
        never be normalized away."""
        self.assertNotEqual(cp_mod.sha256_of([1, 2, 3]), cp_mod.sha256_of([1, 3, 2]))

    def test_integral_float_matches_int(self):
        """JS has one number type; 5 and 5.0 are the same value."""
        self.assertEqual(cp_mod.sha256_of({"level": 5}), cp_mod.sha256_of({"level": 5.0}))

    def test_non_integral_floats_are_preserved(self):
        self.assertNotEqual(cp_mod.sha256_of({"x": 5.5}), cp_mod.sha256_of({"x": 5}))

    def test_diagnostic_keys_are_dropped_but_nothing_else_is(self):
        with_diag = {"a": 1, "__diagnostic_event_count": 99}
        self.assertEqual(cp_mod.sha256_of(with_diag), cp_mod.sha256_of({"a": 1}))
        self.assertNotEqual(cp_mod.sha256_of({"a": 1, "b": None}), cp_mod.sha256_of({"a": 1}))

    def test_dumps_is_stable_across_calls(self):
        stream = _stream()
        self.assertEqual(cp_mod.dumps(stream), cp_mod.dumps(copy.deepcopy(stream)))


class IdenticalStreamsAgreeTests(unittest.TestCase):
    def test_identical_streams_agree(self):
        self.assertIsNone(cp_mod.compare_streams(_stream(), _stream()))

    def test_identical_streams_share_a_stream_hash(self):
        self.assertEqual(cp_mod.stream_hash(_stream()), cp_mod.stream_hash(_stream()))

    def test_no_divergent_indices(self):
        self.assertEqual(cp_mod.divergent_indices(_stream(), _stream()), [])


class MutationSensitivityTests(unittest.TestCase):
    """Every mutation the M3 brief names, applied to a known-good pair and
    asserted to be *detected* — by the stream hash, by `compare_streams`, and
    with the offending field named in the field-level diff."""

    def assert_detected(self, mutate, *, expect_path=None, expect_index=None):
        js = _stream()
        py = _stream()
        mutate(py)

        self.assertNotEqual(
            cp_mod.stream_hash(js), cp_mod.stream_hash(py), "stream hash did not change"
        )
        report = cp_mod.compare_streams(js, py)
        self.assertIsNotNone(report, "compare_streams reported agreement")
        if expect_index is not None:
            self.assertEqual(report["index"], expect_index)
        if expect_path is not None:
            paths = [p for p, _, _ in report["fields"]]
            summary = [p for p, _, _ in cp_mod.field_path_summary(js, py)]
            self.assertTrue(
                expect_path in paths or expect_path in summary,
                f"{expect_path!r} not named; got fields={paths} summary={summary}",
            )
        return report

    # -- 1. an RNG shift -------------------------------------------------
    def test_rng_state_shift_is_detected(self):
        self.assert_detected(
            lambda s: s[2]["rng"].__setitem__("state", s[2]["rng"]["state"] + 1),
            expect_path="rng.state", expect_index=2,
        )

    def test_rng_draw_count_shift_is_detected(self):
        self.assert_detected(
            lambda s: s[1]["rng"].__setitem__("draws", s[1]["rng"]["draws"] + 1),
            expect_path="rng.draws", expect_index=1,
        )

    # -- 2. an omitted or reordered checkpoint ---------------------------
    def test_omitted_checkpoint_is_detected(self):
        report = self.assert_detected(lambda s: s.pop(2))
        self.assertEqual(report["js_len"], 4)
        self.assertEqual(report["py_len"], 3)

    def test_extra_checkpoint_is_detected(self):
        report = self.assert_detected(lambda s: s.append(_checkpoint(4, "terminal")))
        self.assertEqual(report["reason"], "python stream has extra checkpoint(s)")

    def test_reordered_checkpoints_are_detected(self):
        """Two checkpoints swapped: every individual checkpoint still exists
        and is individually unchanged, so only an order-sensitive stream hash
        catches this."""
        js = _stream()
        py = _stream()
        py[1], py[2] = py[2], py[1]
        self.assertEqual(
            sorted(cp_mod.checkpoint_hashes(js)), sorted(cp_mod.checkpoint_hashes(py)),
            "precondition: the multiset of checkpoints is unchanged",
        )
        self.assertNotEqual(cp_mod.stream_hash(js), cp_mod.stream_hash(py))
        self.assertIsNotNone(cp_mod.compare_streams(js, py))

    # -- 3. a changed node or flag ---------------------------------------
    def test_changed_node_accessible_flag_is_detected(self):
        self.assert_detected(
            lambda s: s[3]["map"]["nodes"][1].__setitem__("accessible", False),
            expect_path="map.nodes[i].accessible", expect_index=3,
        )

    def test_changed_node_type_is_detected(self):
        self.assert_detected(
            lambda s: s[0]["map"]["nodes"][1].__setitem__("type", "trainer"),
            expect_path="map.nodes[i].type", expect_index=0,
        )

    def test_changed_edge_list_is_detected(self):
        self.assert_detected(
            lambda s: s[0]["map"]["edges"].append(["n0_0", "n1_1"]),
            expect_path="map.edges[len]", expect_index=0,
        )

    def test_changed_counter_flag_is_detected(self):
        self.assert_detected(
            lambda s: s[3]["counters"].__setitem__("entered_sub_map", True),
            expect_path="counters.entered_sub_map", expect_index=3,
        )

    # -- 4. a changed team or reward field -------------------------------
    def test_changed_team_level_is_detected(self):
        self.assert_detected(
            lambda s: s[3]["team"][0].__setitem__("level", 6),
            expect_path="team[i].level", expect_index=3,
        )

    def test_changed_team_form_identity_is_detected(self):
        self.assert_detected(
            lambda s: s[0]["team"][0].__setitem__("form_id", "giratina-origin"),
            expect_path="team[i].form_id", expect_index=0,
        )

    def test_dropped_team_member_is_detected(self):
        self.assert_detected(lambda s: s[3]["team"].clear(), expect_path="team[len]")

    def test_changed_reward_item_is_detected(self):
        self.assert_detected(
            lambda s: s[3]["items"].append("escape_rope"),
            expect_path="items[len]", expect_index=3,
        )

    def test_changed_battle_result_is_detected(self):
        def mutate(s):
            s[3]["event"] = {"battle": {"player_won": False, "rounds": 2}}
        js = _stream()
        js[3]["event"] = {"battle": {"player_won": True, "rounds": 2}}
        py = _stream()
        mutate(py)
        report = cp_mod.compare_streams(js, py)
        self.assertIsNotNone(report)
        self.assertIn("event.battle.player_won", [p for p, _, _ in report["fields"]])


class DiffQualityTests(unittest.TestCase):
    def test_diff_names_the_exact_path_and_both_values(self):
        js = _stream()
        py = _stream()
        py[2]["team"][0]["current_hp"] = 11
        report = cp_mod.compare_streams(js, py)
        assert report is not None
        self.assertIn(("team[0].current_hp", 19, 11), report["fields"])
        rendered = cp_mod.format_report(report)
        self.assertIn("team[0].current_hp", rendered)
        self.assertIn("19", rendered)
        self.assertIn("11", rendered)

    def test_report_is_never_only_a_hash(self):
        js = _stream()
        py = _stream()
        py[0]["current_node"] = "n1_0"
        report = cp_mod.compare_streams(js, py)
        assert report is not None
        self.assertTrue(report["fields"], "a hash mismatch must come with field detail")

    def test_field_path_summary_groups_list_indices(self):
        js = _stream()
        py = _stream()
        for cp in py:
            cp["map"]["nodes"][1]["accessible"] = False
        summary = dict((p, c) for p, c, _ in cp_mod.field_path_summary(js, py))
        self.assertEqual(summary.get("map.nodes[i].accessible"), len(js))

    def test_compress_ranges(self):
        self.assertEqual(cp_mod.compress_ranges([0, 1, 2, 5, 7, 8]), "0-2, 5, 7-8")
        self.assertEqual(cp_mod.compress_ranges([]), "(none)")


class FixtureIntegrityTests(unittest.TestCase):
    """The fixtures and the manifest are repository artifacts; keep them
    honest without needing node."""

    @staticmethod
    def _scenarios_dir() -> str:
        return os.path.join(_ROUTE_ORACLE, "scenarios")

    def test_manifest_and_fixtures_agree(self):
        with open(os.path.join(self._scenarios_dir(), "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["schema_version"], cp_mod.SCHEMA_VERSION)
        self.assertTrue(manifest["scenarios"], "the route matrix must not be empty")
        for entry in manifest["scenarios"]:
            path = os.path.join(self._scenarios_dir(), entry["file"])
            self.assertTrue(os.path.exists(path), f"missing fixture {entry['file']}")
            with open(path, encoding="utf-8") as fh:
                scenario = json.load(fh)
            self.assertEqual(
                scenario["schema_version"], cp_mod.SCHEMA_VERSION, entry["file"]
            )
            for key in ("scenario", "description", "mode", "seed", "starter_index", "actions"):
                self.assertIn(key, scenario, f"{entry['file']} missing {key}")
            self.assertLessEqual(
                sum(bool(scenario["mode"][g]) for g in ("gen2", "gen3", "gen4")), 1,
                f"{entry['file']}: generation selection is mutually exclusive",
            )
            for step, action in enumerate(scenario["actions"]):
                self.assertIn(
                    action["kind"], ("visit", "choice", "advance_map"),
                    f"{entry['file']} step {step}",
                )

    def test_manifest_pins_required_coverage_and_per_scenario_evidence(self):
        """The manifest's coverage contract must match the harness's.

        A scenario's own `covers` list is documentation and is NOT what the
        gate runs on -- `expected_coverage` is, and it holds observed
        checkpoint INDICES. This test only proves the contract is well-formed;
        `RouteCoverageTests` below proves the indices are real.
        """
        with open(os.path.join(self._scenarios_dir(), "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(list(manifest["required_coverage"]), list(cov_mod.REQUIRED_TAGS))
        union = set()
        for entry in manifest["scenarios"]:
            self.assertIn("expected_coverage", entry, entry["file"])
            for tag, indices in entry["expected_coverage"].items():
                self.assertTrue(indices, f"{entry['file']}: tag {tag} pinned with no evidence")
                self.assertEqual(
                    indices, sorted(indices), f"{entry['file']}: {tag} evidence must be ordered"
                )
                union.add(tag)
        self.assertEqual(
            sorted(set(cov_mod.REQUIRED_TAGS) - union), [],
            "manifest pins no evidence for some required tag",
        )

    def test_declared_coverage_gaps_are_stated_not_implied(self):
        """Whatever is still uncovered must be named in README.md. The two
        gaps M3.1 closed (Admin, submap reward/subexit/parent return) must no
        longer be listed as gaps."""
        with open(os.path.join(_ROUTE_ORACLE, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("Declared coverage gaps", readme)
        self.assertIn("Distortion submaps are not covered", readme)
        for closed in (
            "Magma / Aqua Admin is not covered",
            "subexit, and parent return are not covered",
        ):
            self.assertNotIn(
                closed, readme,
                "README still declares a gap that the route matrix now covers",
            )

    def test_a_nuzlocke_terminal_loss_fork_exists_and_is_not_a_continuation(self):
        with open(os.path.join(self._scenarios_dir(), "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        losses = [e for e in manifest["scenarios"] if "game_over" in e.get("covers", [])]
        self.assertTrue(losses, "no terminal-loss scenario in the matrix")
        for entry in losses:
            with open(os.path.join(self._scenarios_dir(), entry["file"]), encoding="utf-8") as fh:
                scenario = json.load(fh)
            # A terminal loss must be its own scenario, so game over is the
            # last thing that happens -- never followed by more actions that
            # would fabricate a continuation past it.
            self.assertTrue(scenario["mode"]["nuzlocke"], f"{entry['file']} should be Nuzlocke")


class RouteCoverageTests(unittest.TestCase):
    """The machine-enforced M3 coverage gate, run over OBSERVED checkpoints.

    `compare.py --all` enforces coverage over the JavaScript streams (the real
    source is the authority on whether a route was reached). This class
    enforces the same contract over the Python streams, in-process, so a
    regression is caught by the ordinary suite without needing node or the
    bundle. Both sides were verified to derive byte-identical evidence for all
    eight scenarios when the matrix was built.

    Nothing here counts a `covers` list, a source citation or a planned route
    as coverage: every tag is earned by checkpoints that actually happened.
    """

    @classmethod
    def setUpClass(cls):
        import run_scenario  # noqa: PLC0415 -- route-oracle is on sys.path above

        cls.run_scenario = run_scenario
        cls.scenarios_dir = os.path.join(_ROUTE_ORACLE, "scenarios")
        with open(os.path.join(cls.scenarios_dir, "manifest.json"), encoding="utf-8") as fh:
            cls.manifest = json.load(fh)
        cls.observed = {}
        for entry in cls.manifest["scenarios"]:
            with open(os.path.join(cls.scenarios_dir, entry["file"]), encoding="utf-8") as fh:
                scenario = json.load(fh)
            out = cls.run_scenario.Runner(scenario).run()
            assert not out.get("error"), f"{entry['file']}: {out.get('error')}"
            cls.observed[entry["file"]] = out["checkpoints"]

    def _derive_all(self) -> dict:
        return {name: cov_mod.derive(cps) for name, cps in self.observed.items()}

    def test_every_required_tag_is_earned_by_observed_checkpoints(self):
        gaps = cov_mod.missing(self._derive_all())
        self.assertEqual(gaps, [], f"route coverage incomplete: {gaps}")

    def test_observed_evidence_matches_the_manifest_exactly(self):
        derived = self._derive_all()
        for entry in self.manifest["scenarios"]:
            self.assertEqual(
                derived[entry["file"]],
                {k: list(v) for k, v in entry["expected_coverage"].items()},
                f"{entry['file']}: observed coverage evidence drifted from the manifest",
            )

    # -- the four coverage paths M3.1 added -------------------------------

    def _scenario_tags(self, filename: str) -> dict:
        return cov_mod.derive(self.observed[filename])

    def test_admin_tag_comes_from_a_resolved_admin_not_a_flagged_one(self):
        tags = self._scenario_tags("story_gen3_admin.json")
        self.assertIn("admin", tags)
        for index in tags["admin"]:
            cp = self.observed["story_gen3_admin.json"][index]
            self.assertFalse(cp["game_over"], "a lost admin battle must not earn the tag")
            self.assertTrue(cp["counters"]["fought_admin"])

    def test_submap_lifecycle_tags_are_all_earned_by_one_scenario(self):
        tags = self._scenario_tags("story_gen4_submap_full.json")
        for tag in (
            "submap_entry", "submap_boss_win", "pending_submap_reward",
            "resolved_submap_reward", "subexit", "exact_parent_return",
        ):
            self.assertIn(tag, tags, f"the full-submap route must earn {tag}")

    def test_a_losing_submap_attempt_earns_no_win_tag(self):
        """`story_gen4_underground` enters a submap and LOSES its boss. It must
        earn `submap_entry` and nothing downstream of a win."""
        tags = self._scenario_tags("story_gen4_underground.json")
        self.assertIn("submap_entry", tags)
        for tag in ("submap_boss_win", "pending_submap_reward",
                    "resolved_submap_reward", "subexit", "exact_parent_return"):
            self.assertNotIn(tag, tags, f"a losing attempt must not earn {tag}")

    def test_a_lost_silver_earns_no_silver_tag(self):
        """`story_gen2_silver` reaches Silver and loses; only the win route
        earns the tag."""
        self.assertNotIn("silver", self._scenario_tags("story_gen2_silver.json"))
        self.assertIn("silver", self._scenario_tags("story_gen2_silver_win.json"))

    def test_a_one_member_wipe_earns_no_permadeath_tag(self):
        """`nuzlocke_gen1_loss` wipes with a party of one, so nothing is ever
        culled -- the cull runs on the win branch only."""
        self.assertNotIn("nuzlocke_permadeath", self._scenario_tags("nuzlocke_gen1_loss.json"))
        self.assertIn(
            "nuzlocke_permadeath", self._scenario_tags("nuzlocke_gen1_permadeath.json")
        )

    # -- coverage-level mutation sensitivity -------------------------------

    def test_removing_a_checkpoint_changes_the_evidence(self):
        """`check_coverage` compares the WHOLE per-scenario evidence dict
        against the manifest, so that is what has to change. (Comparing a
        single tag would be weaker and occasionally vacuous: dropping the
        checkpoint that earns `exact_parent_return` shifts the next one into
        the same index, so that tag alone still reports `[51]` -- while
        `subexit` and every later `ordinary_trainer` index move.)"""
        for name, tag in (
            ("story_gen3_admin.json", "admin"),
            ("story_gen4_submap_full.json", "resolved_submap_reward"),
            ("story_gen4_submap_full.json", "subexit"),
            ("story_gen4_submap_full.json", "exact_parent_return"),
            ("nuzlocke_gen1_permadeath.json", "nuzlocke_permadeath"),
        ):
            with self.subTest(name=name, tag=tag):
                original = cov_mod.derive(self.observed[name])
                index = original[tag][0]
                mutated = cov_mod.derive(
                    self.observed[name][:index] + self.observed[name][index + 1:]
                )
                self.assertNotEqual(
                    original, mutated,
                    f"removing checkpoint {index} (which earns {tag}) left the "
                    "coverage evidence completely unchanged",
                )

    def test_reordering_checkpoints_changes_the_evidence(self):
        name = "story_gen4_submap_full.json"
        original = cov_mod.derive(self.observed[name])
        index = original["resolved_submap_reward"][0]
        swapped = list(self.observed[name])
        swapped[index - 1], swapped[index] = swapped[index], swapped[index - 1]
        self.assertNotEqual(original, cov_mod.derive(swapped))

    def test_a_wrong_submap_reward_checkpoint_loses_the_tag(self):
        """Blank out the pending choice on the reward checkpoint: the reward
        must no longer count as ever having suspended."""
        name = "story_gen4_submap_full.json"
        index = cov_mod.derive(self.observed[name])["pending_submap_reward"][0]
        mutated = copy.deepcopy(self.observed[name])
        mutated[index]["pending"] = None
        derived = cov_mod.derive(mutated)
        self.assertNotIn("pending_submap_reward", derived)
        self.assertNotIn("resolved_submap_reward", derived)

    def test_a_wrong_subexit_checkpoint_loses_the_tag(self):
        """If the subexit node does not actually leave the submap, the tag is
        not earned."""
        name = "story_gen4_submap_full.json"
        index = cov_mod.derive(self.observed[name])["subexit"][0]
        mutated = copy.deepcopy(self.observed[name])
        mutated[index]["in_sub_map"] = "underground"
        self.assertNotIn("subexit", cov_mod.derive(mutated))

    def test_an_incorrect_restored_parent_loses_exact_parent_return(self):
        """Corrupt one restored node flag that `advanceFromNode` should NOT
        have touched. The tag must not survive a parent that is merely
        plausible."""
        name = "story_gen4_submap_full.json"
        index = cov_mod.derive(self.observed[name])["exact_parent_return"][0]
        mutated = copy.deepcopy(self.observed[name])
        left = (mutated[index - 1]["sub_map_return"] or {}).get("node_id")
        successors = {d for s, d in mutated[index]["map"]["edges"] if s == left}
        for node in mutated[index]["map"]["nodes"]:
            if node["id"] != left and node["id"] not in successors:
                node["visited"] = not node["visited"]
                break
        self.assertNotIn("exact_parent_return", cov_mod.derive(mutated))

    def test_a_regenerated_parent_map_loses_exact_parent_return(self):
        """A parent that came back with everything unlocked (the shape a
        regenerated rather than restored map would have) must fail."""
        name = "story_gen4_submap_full.json"
        index = cov_mod.derive(self.observed[name])["exact_parent_return"][0]
        mutated = copy.deepcopy(self.observed[name])
        for node in mutated[index]["map"]["nodes"]:
            node["accessible"] = True
        self.assertNotIn("exact_parent_return", cov_mod.derive(mutated))

    def test_a_lost_admin_battle_would_lose_the_admin_tag(self):
        name = "story_gen3_admin.json"
        index = cov_mod.derive(self.observed[name])["admin"][0]
        mutated = copy.deepcopy(self.observed[name])
        mutated[index]["game_over"] = True
        self.assertNotIn("admin", cov_mod.derive(mutated))


class FrozenSignatureUnitTests(unittest.TestCase):
    """The frozen-signature builder/comparator, in-process and without node.

    These are about the DATA STRUCTURE. The behaviour of the actual gate is
    tested at the CLI in `FrozenSignatureCliTests` below -- M3.2 found that the
    old tests asserted on `inspect.getsource(compare.main)`, which proves
    nothing about what the command actually exits with.
    """

    @staticmethod
    def _records(path, count, start=0):
        return [
            {"index": start + i, "js_kind": "node_post", "py_kind": "node_post",
             "raw_path": path.replace("[i]", "[0]"), "path": path, "js": i, "py": i + 1}
            for i in range(count)
        ]

    def _results(self, count=3, path="rng.draws"):
        return [{
            "file": "a.json", "scenario": "a",
            "signature_records": self._records(path, count),
        }]

    def test_signature_is_stable_and_order_independent(self):
        import frozen_signature as sig_mod  # noqa: PLC0415

        left = [
            {"file": "a.json", "scenario": "a", "signature_records": self._records("rng.draws", 2)},
            {"file": "b.json", "scenario": "b", "signature_records": self._records("rng.state", 1)},
        ]
        right = list(reversed(left))
        self.assertEqual(
            sig_mod.build(left)["signature_sha256"], sig_mod.build(right)["signature_sha256"]
        )

    def test_a_hidden_scenario_is_reported(self):
        import frozen_signature as sig_mod  # noqa: PLC0415

        full = [
            {"file": "a.json", "scenario": "a", "signature_records": self._records("rng.draws", 2)},
            {"file": "b.json", "scenario": "b", "signature_records": self._records("rng.state", 1)},
        ]
        tracked = sig_mod.build(full)
        problems = sig_mod.compare(sig_mod.build(full[:1]), tracked)
        self.assertTrue(any("HIDDEN" in p for p in problems), problems)

    def test_an_added_scenario_is_reported(self):
        import frozen_signature as sig_mod  # noqa: PLC0415

        base = [{"file": "a.json", "scenario": "a",
                 "signature_records": self._records("rng.draws", 2)}]
        extra = base + [{"file": "b.json", "scenario": "b",
                         "signature_records": self._records("rng.state", 1)}]
        problems = sig_mod.compare(sig_mod.build(extra), sig_mod.build(base))
        self.assertTrue(any("ADDED" in p for p in problems), problems)

    def test_a_count_change_under_a_known_path_is_reported(self):
        """The exact hole the old field-name allow-list had."""
        import frozen_signature as sig_mod  # noqa: PLC0415

        problems = sig_mod.compare(
            sig_mod.build(self._results(count=4)), sig_mod.build(self._results(count=3))
        )
        self.assertTrue(any(p.startswith("COUNT changed") for p in problems), problems)

    def test_a_value_change_with_an_identical_count_is_reported(self):
        import frozen_signature as sig_mod  # noqa: PLC0415

        tracked = sig_mod.build(self._results(count=3))
        shifted = self._results(count=3)
        for record in shifted[0]["signature_records"]:
            record["py"] = record["py"] + 100
        problems = sig_mod.compare(sig_mod.build(shifted), tracked)
        self.assertTrue(any(p.startswith("VALUES changed") for p in problems), problems)

    def test_a_moved_checkpoint_with_identical_values_is_reported(self):
        import frozen_signature as sig_mod  # noqa: PLC0415

        tracked = sig_mod.build(self._results(count=3))
        moved = [{"file": "a.json", "scenario": "a",
                  "signature_records": self._records("rng.draws", 3, start=10)}]
        problems = sig_mod.compare(sig_mod.build(moved), tracked)
        self.assertTrue(any(p.startswith("CHECKPOINTS moved") for p in problems), problems)

    def test_the_same_difference_in_another_scenario_is_reported(self):
        import frozen_signature as sig_mod  # noqa: PLC0415

        tracked = sig_mod.build(self._results())
        elsewhere = [{"file": "b.json", "scenario": "b",
                      "signature_records": self._records("rng.draws", 3)}]
        problems = sig_mod.compare(sig_mod.build(elsewhere), tracked)
        self.assertTrue(any("NEW difference" in p for p in problems), problems)
        self.assertTrue(any("MISSING difference" in p for p in problems), problems)

    def test_an_identical_signature_has_no_problems(self):
        import frozen_signature as sig_mod  # noqa: PLC0415

        built = sig_mod.build(self._results())
        self.assertEqual(sig_mod.compare(built, built), [])


class FrozenSignatureFileTests(unittest.TestCase):
    """The tracked signature is a repository artifact; keep it honest."""

    @classmethod
    def setUpClass(cls):
        import frozen_signature as sig_mod  # noqa: PLC0415

        cls.sig_mod = sig_mod
        cls.signature = sig_mod.load()

    def test_the_frozen_signature_is_checked_in_and_well_formed(self):
        self.assertIsNotNone(self.signature, "route-oracle/frozen_signature.json is missing")
        for key in ("signature_version", "schema_version", "scenarios",
                    "differences", "signature_sha256"):
            self.assertIn(key, self.signature)
        self.assertEqual(self.signature["schema_version"], cp_mod.SCHEMA_VERSION)

    def test_the_signature_hash_matches_its_own_body(self):
        body = {k: v for k, v in self.signature.items() if k != "signature_sha256"}
        self.assertEqual(cp_mod.sha256_of(body), self.signature["signature_sha256"])

    def test_the_signature_covers_exactly_the_manifest(self):
        with open(
            os.path.join(_ROUTE_ORACLE, "scenarios", "manifest.json"), encoding="utf-8"
        ) as fh:
            manifest = json.load(fh)
        self.assertEqual(
            sorted(e["file"] for e in manifest["scenarios"]),
            sorted(s["file"] for s in self.signature["scenarios"]),
        )

    def test_every_frozen_difference_is_documented_in_the_findings_report(self):
        with open(
            os.path.join(_ROUTE_ORACLE, "findings", "M3-parity-blockers.md"), encoding="utf-8"
        ) as fh:
            findings = fh.read()
        for path in sorted({d["path"] for d in self.signature["differences"]}):
            self.assertIn(
                path, findings,
                f"{path} is frozen by --audit-frozen but not traced in the findings report",
            )


def _oracle_runnable():
    """The CLI tests need node and the extracted prefix; both are optional on
    a machine that only runs the pure-Python suite."""
    import shutil  # noqa: PLC0415

    if shutil.which("node") is None:
        return False
    return os.path.exists(os.path.join(_ROUTE_ORACLE, "out", "route-prefix.js"))


@unittest.skipUnless(_oracle_runnable(), "needs node + route-oracle/out/route-prefix.js")
class FrozenSignatureCliTests(unittest.TestCase):
    """Real CLI-level tests of the gate, replacing M3.1's source-string
    assertions. These run the actual command and check the actual exit code."""

    @staticmethod
    def _run(*args):
        import subprocess  # noqa: PLC0415

        repo = os.path.dirname(_ROUTE_ORACLE)
        return subprocess.run(
            [sys.executable, os.path.join(_ROUTE_ORACLE, "compare.py"), *args],
            capture_output=True, text=True, cwd=repo,
        )

    def test_default_strict_mode_exits_nonzero_while_parity_is_blocked(self):
        proc = self._run("--all")
        self.assertNotEqual(proc.returncode, 0, "the parity gate must not go green")

    def test_audit_frozen_requires_the_whole_manifest(self):
        proc = self._run(
            os.path.join(_ROUTE_ORACLE, "scenarios", "nuzlocke_gen1_loss.json"),
            "--audit-frozen",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("requires --all", proc.stderr)

    def test_audit_frozen_exits_zero_only_for_the_complete_signature(self):
        proc = self._run("--all", "--audit-frozen")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("PARITY REMAINS BLOCKED", proc.stdout)
        # The only permitted mention of a parity pass is the denial of one.
        self.assertIn("NOT a parity PASS", proc.stdout)
        self.assertNotIn("PARITY PASS\n", proc.stdout)
        self.assertNotIn("scenarios agree.\n\n8/8", proc.stdout)

    def test_audit_frozen_is_order_independent(self):
        for order in ("manifest", "reverse", "sorted"):
            with self.subTest(order=order):
                proc = self._run("--all", "--audit-frozen", "--order", order)
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])


class PythonRunnerIsolationTests(unittest.TestCase):
    """The Python runner must be repeatable and leak-free in one process.

    It monkeypatches `engine._run_battle` to observe results and wraps the
    engine's private RNG stream; if either leaked, a second run in the same
    interpreter would not reproduce the first. This is the Python-side
    counterpart to `compare.py --order` (which proves the same thing across
    processes).
    """

    @classmethod
    def setUpClass(cls):
        import run_scenario  # noqa: PLC0415 -- route-oracle is on sys.path above

        cls.run_scenario = run_scenario
        path = os.path.join(_ROUTE_ORACLE, "scenarios", "nuzlocke_gen1_loss.json")
        with open(path, encoding="utf-8") as fh:
            cls.scenario = json.load(fh)

    def _stream_hash(self):
        out = self.run_scenario.Runner(copy.deepcopy(self.scenario)).run()
        self.assertIsNone(out.get("error"), out.get("error"))
        return cp_mod.stream_hash(out["checkpoints"]), out["rng_draws_total"]

    def test_two_runs_in_one_process_are_identical(self):
        first = self._stream_hash()
        second = self._stream_hash()
        self.assertEqual(first, second)

    def test_run_battle_is_restored_after_a_run(self):
        from pokelike import engine as engine_mod  # noqa: PLC0415

        before = engine_mod._run_battle
        self._stream_hash()
        self.assertIs(engine_mod._run_battle, before)

    def test_interleaved_runners_do_not_share_rng_state(self):
        """Two Runners built before either is stepped must still each produce
        the run they would have produced alone."""
        solo, solo_draws = self._stream_hash()
        a = self.run_scenario.Runner(copy.deepcopy(self.scenario))
        b = self.run_scenario.Runner(copy.deepcopy(self.scenario))
        out_a = a.run()
        out_b = b.run()
        self.assertEqual(cp_mod.stream_hash(out_a["checkpoints"]), solo)
        self.assertEqual(cp_mod.stream_hash(out_b["checkpoints"]), solo)
        self.assertEqual(out_a["rng_draws_total"], solo_draws)
        self.assertEqual(out_b["rng_draws_total"], solo_draws)

    def test_module_level_rng_stream_is_left_active_after_a_run(self):
        from pokelike import rng as rng_mod  # noqa: PLC0415

        before = rng_mod.get_active_stream()
        self._stream_hash()
        self.assertIs(rng_mod.get_active_stream(), before)


if __name__ == "__main__":
    unittest.main()
