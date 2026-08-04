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
        """Whatever is still uncovered must be NAMED in README.md, and
        whatever the matrix now covers must NOT be listed as open.

        The guard is the point, not the exact prose: a family that no route
        reaches must appear under "Declared coverage gaps" -- specifically
        under "Still open", not merely somewhere in the section -- so that
        silence, or a stale leftover mention, can never be mistaken for
        agreement. M4-repair closed every family the independent closure
        audit (`docs/audits/M4-independent-closure-audit.md`) found unbridged
        or unrouted (legendary/shiny/move-tutor/trade/Distortion/sacrifice/
        stat10/the three showScreen-less overlays); only `ESCAPE_ROPE_CHOICE`
        remains genuinely out of scope.
        """
        with open(os.path.join(_ROUTE_ORACLE, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("Declared coverage gaps", readme)
        self.assertIn("## Current result", readme)
        gaps = readme[readme.index("Declared coverage gaps"): readme.index("## Current result")]
        self.assertIn("Still open", gaps)
        closed_section = gaps[: gaps.index("Still open")]
        still_open_section = gaps[gaps.index("Still open"):]

        # M4-repair closed every family the audit found unbridged -- each
        # must be credited under Closed, and must NOT still be listed open.
        for closed in (
            "Distortion submaps",
            "move-tutor",
            "trade",
            "legendary",
            "shiny",
            "sacrifice",
            "stat10",
            "openItemEquipModal",
            "showBranchingChoice",
            "showTeamPickerModal",
        ):
            self.assertIn(
                closed, closed_section,
                f"README no longer credits the M4-repair-closed family {closed!r}",
            )
            self.assertNotIn(
                closed, still_open_section,
                f"README still lists the CLOSED family {closed!r} as open",
            )

        # The one family the repair scope explicitly excluded must still be named.
        self.assertIn("ESCAPE_ROPE_CHOICE", still_open_section)
        self.assertIn("no parity claim is made", gaps)
        for closed in (
            "Magma / Aqua Admin is not covered",
            "subexit, and parent return are not covered",
            "Distortion submaps are not covered",  # superseded wording
        ):
            self.assertNotIn(
                closed, readme,
                "README still declares a gap that the route matrix now covers, "
                "or uses wording this test has superseded",
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

    def test_default_strict_mode_exits_zero_now_that_parity_is_clean(self):
        """M4 inverted this test, deliberately.

        Under M3 it asserted `assertNotEqual(returncode, 0)` -- "the parity
        gate must not go green" -- because five source-backed differences were
        knowingly unrepaired and a green gate would have meant the comparator
        had been weakened. M4 repaired all five (plus the ordinary-legendary
        lifecycle and the Mirror Coat counter-hit event), so the same
        assertion would now REQUIRE the port to stay wrong. The gate must go
        green, and it must go green with complete coverage rather than by
        running fewer scenarios.

        M4-repair grew the matrix again (11 -> 24 scenarios, 16 -> 32 required
        tags) closing the ordinary-legendary/shiny/move-tutor/trade/Distortion/
        overlay families the independent closure audit found unbridged --
        updating these literals is exactly the same "grow the count, don't
        shrink the scope" motion this docstring already describes.
        """
        proc = self._run("--all")
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertIn("24/24 scenarios agree", proc.stdout)
        self.assertIn("32/32 required tags", proc.stdout)

    def test_no_scenario_uses_post_starter_rng_alignment(self):
        """`align_rng_after_starter_offer` is retired from the primary matrix.

        It was an oracle instrument that re-seeded both runtimes past the
        three-draw starter-offer divergence (frozen blocker 1). M4 repaired
        the divergence itself -- `Engine.reset` now performs the same three
        `rollShiny` draws the source does -- so the instrument has nothing
        left to isolate, and leaving it in place would let that exact
        regression return invisibly.
        """
        sd = os.path.join(_ROUTE_ORACLE, "scenarios")
        for name in sorted(os.listdir(sd)):
            if not name.endswith(".json") or name == "manifest.json":
                continue
            with self.subTest(scenario=name), open(os.path.join(sd, name), encoding="utf-8") as fh:
                self.assertNotIn("align_rng_after_starter_offer", json.load(fh))

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
        # M4's signature is EMPTY, so the audit's meaning inverted with it:
        # under M3 it asserted "these 40 known differences and no others",
        # now it asserts "no difference has reappeared". The M3 wording is
        # gone because a passing gate must not print a false claim.
        self.assertNotIn("PARITY REMAINS BLOCKED", proc.stdout)
        self.assertIn("ZERO difference records", proc.stdout)

    def test_the_frozen_signature_itself_has_no_difference_records(self):
        """A nonzero M4 signature must never be re-frozen.

        `--audit-frozen` only compares the observed signature to the tracked
        one; if a future session re-froze a signature that carried differences
        again, that command would still exit 0. This asserts the tracked
        artifact's own content, which is the thing that would have to change.
        """
        with open(os.path.join(_ROUTE_ORACLE, "frozen_signature.json"), encoding="utf-8") as fh:
            frozen = json.load(fh)
        self.assertEqual(frozen["differences"], [])
        # 11 -> 24 with the M4-repair scenarios; see the sibling strict-mode
        # test's docstring for why growing this literal is expected, not drift.
        self.assertEqual(len(frozen["scenarios"]), 24)

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


def _instance(species_id: int, name: str, level: int, *, is_shiny: bool = False) -> dict:
    """A normalized instance shaped exactly like the runners' `normalizeMon` /
    `_normalize_mon` output."""
    return {
        "species_id": species_id, "form_id": None, "name": name, "level": level,
        "max_hp": 20 + level, "current_hp": 20 + level, "types": ["Normal"],
        "move_tier": 0, "held_item": None, "is_shiny": is_shiny, "status": None,
        "burned": False, "paralyzed": False, "poison_stacks": 0,
        "base_stats": {"hp": 40, "atk": 40, "def": 40, "speed": 40, "special": 40, "spdef": 40},
        "stat_buffs": {},
    }


def _option(role: str, species_id: int, name: str, level: int, slot=None, instance=True) -> dict:
    return {
        "role": role, "kind": "mon", "species_id": species_id, "form_id": None,
        "name": name, "item_id": None, "slot": slot,
        "instance": _instance(species_id, name, level) if instance else None,
    }


def _swap_pending() -> dict:
    """A full-team swap-screen pending record: three release options plus the
    incoming/team context."""
    team = [_option("swap_release", 1, "Bulbasaur", 12, slot=0),
            _option("swap_release", 4, "Charmander", 11, slot=1),
            _option("swap_release", 7, "Squirtle", 13, slot=2)]
    return {
        "phase": "swap_choice", "optional": True, "option_count": 3,
        "options": copy.deepcopy(team),
        "context": {
            "incoming": _option("incoming", 133, "Eevee", 15),
            "team": [dict(o, role="team") for o in copy.deepcopy(team)],
        },
    }


class PendingOptionIdentityMutationTests(unittest.TestCase):
    """M3.3b workstream 3. The oracle must detect a pending choice whose
    ORDERED OPTION IDENTITY is wrong, not merely one whose option COUNT is
    wrong. Each mutation below keeps the cardinality constant.
    """

    def _pair(self, pending: dict):
        js = _stream()
        py = _stream()
        for stream in (js, py):
            stream[0] = _checkpoint(0, "run_init", screen="swap-screen",
                                    pending=copy.deepcopy(pending))
        return js, py

    def assert_detected(self, pending: dict, mutate, expect_path: str):
        js, py = self._pair(pending)
        mutate(py[0]["pending"])
        self.assertNotEqual(cp_mod.stream_hash(js), cp_mod.stream_hash(py),
                            "stream hash did not change")
        report = cp_mod.compare_streams(js, py)
        self.assertIsNotNone(report, "compare_streams reported agreement")
        paths = [p for p, _, _ in report["fields"]]
        summary = [p for p, _, _ in cp_mod.field_path_summary(js, py)]
        self.assertTrue(expect_path in paths or expect_path in summary,
                        f"{expect_path!r} not named; got fields={paths} summary={summary}")
        return report

    def test_same_length_wrong_option_substitution_is_detected(self):
        """A different Pokemon offered in the same slot, option count unchanged."""
        def mutate(pending):
            pending["options"][1] = _option("swap_release", 25, "Pikachu", 11, slot=1)
        report = self.assert_detected(_swap_pending(), mutate, "pending.options[1].species_id")
        self.assertNotIn("pending.option_count", [p for p, _, _ in report["fields"]],
                         "the substitution must be caught by identity, not by cardinality")

    def test_option_reorder_is_detected(self):
        def mutate(pending):
            pending["options"] = list(reversed(pending["options"]))
        self.assert_detected(_swap_pending(), mutate, "pending.options[0].species_id")

    def test_wrong_incoming_team_role_is_detected(self):
        """Role inversion: the incoming Pokemon presented as a team member and
        vice versa. Cardinality, species set and ordering all stay the same."""
        def mutate(pending):
            ctx = pending["context"]
            ctx["incoming"], ctx["team"][0] = (
                dict(ctx["team"][0], role="incoming"),
                dict(ctx["incoming"], role="team", slot=0),
            )
        self.assert_detected(_swap_pending(), mutate, "pending.context.incoming.species_id")

    def test_swapping_a_role_label_alone_is_detected(self):
        def mutate(pending):
            pending["options"][0]["role"] = "swap_accept"
        self.assert_detected(_swap_pending(), mutate, "pending.options[0].role")

    def test_a_dropped_option_is_detected(self):
        def mutate(pending):
            pending["options"].pop(1)
            pending["option_count"] = 2
        self.assert_detected(_swap_pending(), mutate, "pending.options[len]")

    def test_options_sharing_a_display_name_are_still_distinguished(self):
        """Identity must not collapse to the display name. Two options that
        share `name` and `species_id` are told apart by their instance."""
        pending = {
            "phase": "catch_choice", "optional": True, "option_count": 2,
            "options": [_option("catch", 19, "Rattata", 8),
                        _option("catch", 19, "Rattata", 14)],
            "context": None,
        }
        # Precondition: the two options are indistinguishable by display name.
        self.assertEqual(pending["options"][0]["name"], pending["options"][1]["name"])
        self.assertEqual(pending["options"][0]["species_id"], pending["options"][1]["species_id"])

        def mutate(p):
            p["options"][1] = _option("catch", 19, "Rattata", 9)
        self.assert_detected(pending, mutate, "pending.options[1].instance.level")

    def test_a_shiny_only_difference_is_detected(self):
        pending = {
            "phase": "catch_choice", "optional": True, "option_count": 1,
            "options": [_option("catch", 19, "Rattata", 8)], "context": None,
        }

        def mutate(p):
            p["options"][0]["instance"]["is_shiny"] = True
        self.assert_detected(pending, mutate, "pending.options[0].instance.is_shiny")

    def test_an_item_offer_reorder_is_detected(self):
        def item(item_id, name):
            return {"role": "item", "kind": "item", "species_id": None, "form_id": None,
                    "name": name, "item_id": item_id, "slot": None, "instance": None}
        pending = {
            "phase": "item_choice", "optional": True, "option_count": 3,
            "options": [item("potion", "Potion"), item("eviolite", "Eviolite"),
                        item("escape_rope", "Escape Rope")],
            "context": None,
        }

        def mutate(p):
            p["options"][0], p["options"][2] = p["options"][2], p["options"][0]
        self.assert_detected(pending, mutate, "pending.options[0].item_id")


class PendingOptionProjectionTests(unittest.TestCase):
    """M3.3b workstream 3, Python side. The projection must read the objects
    the engine will actually act on, in the order it will act on them, and it
    must do so BEFORE the choice is resolved.
    """

    @classmethod
    def setUpClass(cls):
        import run_scenario  # noqa: PLC0415 -- route-oracle is on sys.path above

        from pokelike import engine as engine_mod  # noqa: PLC0415

        cls.rs = run_scenario
        cls.engine_mod = engine_mod

    class _State:
        """Minimal stand-in carrying the two attributes the projection reads."""

        def __init__(self, pending, team=()):
            self.pending = pending
            self.team = list(team)

    def _mon(self, species_id, level, **kw):
        return self.engine_mod._make_wild_combatant(species_id, level, **kw)

    # -- shape ------------------------------------------------------------

    def test_no_pending_projects_to_none(self):
        self.assertIsNone(self.rs._pending_projection(self._State(None)))

    def test_starter_options_carry_the_real_pending_instances_in_order(self):
        """M4 repair 1: `Engine.reset` now materialises a real `Combatant` per
        offered starter, the way `showStarterSelect` materialises a real
        `createInstance` per card (bundle.deobfuscated.js:76175-76194), so the
        projection reports those instances instead of the `null` that was
        frozen blocker 1(b)."""
        engine_mod = self.engine_mod
        mons = [self._mon(1, 5), self._mon(4, 5)]
        pending = engine_mod.PendingChoice(
            phase=engine_mod.Phase.CHOOSE_STARTER,
            options=[{"species_id": m.species_id, "name": m.name} for m in mons],
            optional=False,
            extra={"instances": mons},
        )
        out = self.rs._pending_projection(self._State(pending))
        self.assertEqual([o["species_id"] for o in out["options"]], [1, 4])
        self.assertEqual([o["role"] for o in out["options"]], ["starter", "starter"])
        self.assertEqual([o["instance"]["level"] for o in out["options"]], [5, 5])
        self.assertEqual([o["instance"]["species_id"] for o in out["options"]], [1, 4])
        self.assertEqual(out["option_count"], 2)

    def test_starter_projection_refuses_a_pending_with_no_instances(self):
        """A starter offer that built no instances is a real regression (it is
        exactly the pre-M4 state), so the projection must fail loudly rather
        than fall back to a species-only option with a null instance."""
        engine_mod = self.engine_mod
        pending = engine_mod.PendingChoice(
            phase=engine_mod.Phase.CHOOSE_STARTER,
            options=[{"species_id": 1, "name": "Bulbasaur"}],
            optional=False,
        )
        with self.assertRaises(RuntimeError):
            self.rs._pending_projection(self._State(pending))

    def test_catch_options_follow_the_engine_candidate_order(self):
        engine_mod = self.engine_mod
        mons = [self._mon(19, 8), self._mon(19, 14), self._mon(16, 9)]
        pending = engine_mod.PendingChoice(
            phase=engine_mod.Phase.CATCH_CHOICE,
            options=[engine_mod._mon_summary(m) for m in mons],
            optional=True,
            extra={"candidates": mons, "node_id": "n1_0", "origin": "catch"},
        )
        out = self.rs._pending_projection(self._State(pending))
        self.assertEqual([o["species_id"] for o in out["options"]], [19, 19, 16])
        self.assertEqual([o["instance"]["level"] for o in out["options"]], [8, 14, 9])
        self.assertEqual({o["role"] for o in out["options"]}, {"catch"})

    def test_two_same_name_candidates_project_differently(self):
        """The 'identity reduced to display name' failure, on the real
        projection rather than on a fixture."""
        engine_mod = self.engine_mod
        a, b = self._mon(19, 8), self._mon(19, 14)
        self.assertEqual(a.name, b.name)
        pending = engine_mod.PendingChoice(
            phase=engine_mod.Phase.CATCH_CHOICE,
            options=[engine_mod._mon_summary(m) for m in (a, b)],
            optional=True,
            extra={"candidates": [a, b], "node_id": "n1_0", "origin": "catch"},
        )
        out = self.rs._pending_projection(self._State(pending))
        self.assertNotEqual(cp_mod.dumps(out["options"][0]), cp_mod.dumps(out["options"][1]))

    def test_swap_projection_separates_the_incoming_from_the_team(self):
        engine_mod = self.engine_mod
        team = [self._mon(1, 12), self._mon(4, 11), self._mon(7, 13),
                self._mon(25, 10), self._mon(16, 9), self._mon(19, 8)]
        incoming = self._mon(133, 15)
        pending = engine_mod.PendingChoice(
            phase=engine_mod.Phase.SWAP_CHOICE,
            options=[engine_mod._mon_summary(m) for m in team],
            optional=True,
            extra={"incoming": incoming, "node_id": "n2_1"},
        )
        out = self.rs._pending_projection(self._State(pending, team))
        self.assertEqual(out["context"]["incoming"]["species_id"], 133)
        self.assertEqual([o["species_id"] for o in out["options"]],
                         [1, 4, 7, 25, 16, 19])
        self.assertEqual([o["slot"] for o in out["options"]], [0, 1, 2, 3, 4, 5])
        self.assertEqual({o["role"] for o in out["options"]}, {"swap_release"})
        self.assertEqual([o["species_id"] for o in out["context"]["team"]],
                         [1, 4, 7, 25, 16, 19])

    def test_swap_with_room_offers_the_incoming_itself(self):
        engine_mod = self.engine_mod
        team = [self._mon(1, 12)]
        incoming = self._mon(408, 22)
        pending = engine_mod.PendingChoice(
            phase=engine_mod.Phase.SWAP_CHOICE,
            options=[engine_mod._mon_summary(incoming)],
            optional=True,
            extra={"incoming": incoming, "node_id": "n2_1", "has_room": True},
        )
        out = self.rs._pending_projection(self._State(pending, team))
        self.assertEqual(out["option_count"], 1)
        self.assertEqual(out["options"][0]["role"], "swap_accept")
        self.assertEqual(out["options"][0]["species_id"], 408)
        # The team is still reported, so a role inversion is visible.
        self.assertEqual([o["species_id"] for o in out["context"]["team"]], [1])

    def test_a_cardinality_disagreement_is_a_hard_error(self):
        """The projection cross-checks itself against `PendingChoice.options`
        rather than silently preferring one side."""
        engine_mod = self.engine_mod
        mons = [self._mon(19, 8), self._mon(16, 9)]
        pending = engine_mod.PendingChoice(
            phase=engine_mod.Phase.CATCH_CHOICE,
            options=[engine_mod._mon_summary(mons[0])],  # one summary, two candidates
            optional=True,
            extra={"candidates": mons, "node_id": "n1_0", "origin": "catch"},
        )
        with self.assertRaises(RuntimeError):
            self.rs._pending_projection(self._State(pending))

    # -- pre-resolution ----------------------------------------------------

    def test_the_projection_is_captured_before_resolution(self):
        """Runs a real engine to a real catch screen, projects, then resolves.

        Two things are asserted: the option the projection named at index k is
        the Pokemon the engine actually added at index k (so the capture is of
        the real offer), and the projection AFTER resolution no longer carries
        the offer at all (so capturing post-resolution would lose the evidence
        entirely rather than merely shifting it).
        """
        engine_mod = self.engine_mod
        path = os.path.join(_ROUTE_ORACLE, "scenarios", "nuzlocke_gen1_permadeath.json")
        with open(path, encoding="utf-8") as fh:
            scenario = json.load(fh)

        eng = engine_mod.Engine()
        eng.reset(
            nuzlocke_mode=True, gen2_mode=False, gen3_mode=False, gen4_mode=False,
            seed=int(scenario["seed"]),
        )
        st = eng.state
        # No RNG alignment. `align_rng_after_starter_offer` was retired from
        # every fixture in M4: `Engine.reset` now consumes the same three
        # `rollShiny` draws `showStarterSelect` does, so the stream the
        # fixture's route was derived against is the one `reset` already
        # leaves behind. Re-seeding here would put the engine on a DIFFERENT
        # stream from the one the checked-in actions were searched on.
        eng.step(engine_mod.ChooseStarter(int(st.pending.options[scenario["starter_index"]]["species_id"])))

        # Walk the fixture until the first accepted catch.
        accepted_index = None
        for act in scenario["actions"]:
            if act["kind"] == "visit":
                eng.step(engine_mod.VisitNode(act["node"]))
            elif act["kind"] == "advance_map":
                eng.step(engine_mod.AdvanceMap())
            else:
                st = eng.state
                if (st.pending is not None
                        and st.pending.phase == engine_mod.Phase.CATCH_CHOICE
                        and act.get("index") is not None):
                    accepted_index = int(act["index"])
                    break
                eng.step(engine_mod.SelectOption(act.get("index")))
        self.assertIsNotNone(accepted_index, "fixture never accepts a catch")

        st = eng.state
        before = self.rs._pending_projection(st)
        self.assertIsNotNone(before)
        self.assertEqual(before["phase"], "catch_choice")
        chosen = before["options"][accepted_index]
        team_before = len(st.team)

        eng.step(engine_mod.SelectOption(accepted_index))
        st = eng.state

        # The projected option is the object the engine acted on.
        self.assertEqual(len(st.team), team_before + 1)
        added = st.team[-1]
        self.assertEqual(added.species_id, chosen["species_id"])
        self.assertEqual(added.level, chosen["instance"]["level"])
        self.assertEqual(added.name, chosen["name"])

        # And a post-resolution capture would have nothing to report.
        after = self.rs._pending_projection(st)
        self.assertNotEqual(cp_mod.dumps(after), cp_mod.dumps(before))
        self.assertTrue(after is None or after["phase"] != "catch_choice")


def _attack(side, a_idx, t_side, t_idx, move, dmg, *, crit=False, eff=1,
            move_type="Normal", is_special=False, a_hp=30, t_hp=20) -> dict:
    return {
        "type": "attack", "side": side, "attacker_idx": a_idx,
        "target_side": t_side, "target_idx": t_idx, "move_name": move,
        "move_type": move_type, "damage": dmg, "type_eff": eff, "crit": crit,
        "is_special": is_special, "attacker_hp_after": a_hp,
        "target_hp_after": t_hp, "extra_attack": False,
    }


def _battle_with_turns(turns: list) -> dict:
    """A `battle` checkpoint's `event.battle` block: identical final state on
    both sides, differing only in the per-turn event stream."""
    return {
        "player_won": True, "rounds": len(turns), "rng_draws": 12,
        "player_team": [], "enemy_team": [], "player_participants": [0],
        "status_events": [], "turns": turns,
    }


def _two_turn_battle() -> list:
    return [
        {"turn": 1, "events": [_attack("player", 0, "enemy", 0, "Tackle", 7),
                               _attack("enemy", 0, "player", 0, "Ember", 5)]},
        {"turn": 2, "events": [_attack("player", 0, "enemy", 0, "Vine Whip", 9),
                               _attack("enemy", 0, "player", 0, "Scratch", 4)]},
    ]


class BattleTurnProjectionMutationTests(unittest.TestCase):
    """M3.3b workstream 5. The ordered, turn-delimited attack projection must
    distinguish event ORDER, not merely final state. Every mutation below
    leaves `player_won`, `rounds`, `rng_draws` and the final teams identical.
    """

    def assert_detected(self, mutate, expect_path: str):
        js = _stream()
        py = _stream()
        for stream in (js, py):
            stream[2] = _checkpoint(2, "battle",
                                    event={"node": "n1_0", "battle_index": 0,
                                           "battle": _battle_with_turns(_two_turn_battle())})
        mutate(py[2]["event"]["battle"])

        # Final state is untouched by every mutation in this class.
        self.assertEqual(js[2]["event"]["battle"]["player_won"],
                         py[2]["event"]["battle"]["player_won"])
        self.assertEqual(js[2]["event"]["battle"]["rounds"],
                         py[2]["event"]["battle"]["rounds"])
        self.assertEqual(js[2]["team"], py[2]["team"])

        self.assertNotEqual(cp_mod.stream_hash(js), cp_mod.stream_hash(py),
                            "stream hash did not change")
        report = cp_mod.compare_streams(js, py)
        self.assertIsNotNone(report, "compare_streams reported agreement")
        paths = [p for p, _, _ in report["fields"]]
        summary = [p for p, _, _ in cp_mod.field_path_summary(js, py)]
        self.assertTrue(expect_path in paths or expect_path in summary,
                        f"{expect_path!r} not named; got fields={paths} summary={summary}")

    def test_event_reorder_within_a_turn_is_detected(self):
        def mutate(battle):
            battle["turns"][0]["events"].reverse()
        self.assert_detected(mutate, "event.battle.turns[0].events[0].side")

    def test_a_wrong_turn_boundary_is_detected(self):
        """Same events, same order, moved across the turn boundary: turn 1
        keeps three events and turn 2 keeps one."""
        def mutate(battle):
            moved = battle["turns"][1]["events"].pop(0)
            battle["turns"][0]["events"].append(moved)
        self.assert_detected(mutate, "event.battle.turns[0].events[len]")

    def test_a_relabelled_turn_number_is_detected(self):
        def mutate(battle):
            battle["turns"][1]["turn"] = 5
        self.assert_detected(mutate, "event.battle.turns[1].turn")

    def test_a_dropped_event_is_detected(self):
        def mutate(battle):
            battle["turns"][1]["events"].pop(0)
        self.assert_detected(mutate, "event.battle.turns[1].events[len]")

    def test_a_duplicated_event_is_detected(self):
        def mutate(battle):
            battle["turns"][0]["events"].append(dict(battle["turns"][0]["events"][0]))
        self.assert_detected(mutate, "event.battle.turns[0].events[len]")

    def test_a_same_count_event_substitution_is_detected(self):
        """Cardinality is preserved on every turn; only the move changes."""
        def mutate(battle):
            battle["turns"][1]["events"][0] = _attack(
                "player", 0, "enemy", 0, "Razor Leaf", 9)
        self.assert_detected(mutate, "event.battle.turns[1].events[0].move_name")

    def test_a_wrong_event_type_is_detected(self):
        def mutate(battle):
            battle["turns"][0]["events"][0]["type"] = "effect"
        self.assert_detected(mutate, "event.battle.turns[0].events[0].type")

    def test_a_crit_only_difference_is_detected(self):
        def mutate(battle):
            battle["turns"][0]["events"][0]["crit"] = True
        self.assert_detected(mutate, "event.battle.turns[0].events[0].crit")

    def test_a_type_effectiveness_only_difference_is_detected(self):
        def mutate(battle):
            battle["turns"][0]["events"][0]["type_eff"] = 2
        self.assert_detected(mutate, "event.battle.turns[0].events[0].type_eff")

    def test_a_wrong_acting_side_is_detected(self):
        def mutate(battle):
            event = battle["turns"][0]["events"][0]
            event["side"], event["target_side"] = event["target_side"], event["side"]
        self.assert_detected(mutate, "event.battle.turns[0].events[0].side")

    def test_final_state_equality_does_not_imply_event_equality(self):
        """The explicit statement of what this projection buys: two battles
        agreeing on winner, rounds, draws, participants, status events and
        both final teams still fail if their turn streams differ."""
        same_final = _battle_with_turns(_two_turn_battle())
        reordered = copy.deepcopy(same_final)
        reordered["turns"][0]["events"].reverse()
        for key in ("player_won", "rounds", "rng_draws", "player_team",
                    "enemy_team", "player_participants", "status_events"):
            self.assertEqual(same_final[key], reordered[key])
        self.assertTrue(cp_mod.diff_values(same_final, reordered),
                        "an event-order-only difference was not reported")


class BattleTurnProjectionShapeTests(unittest.TestCase):
    """The fold from each runtime's flat stream into the shared per-turn
    shape, and the invariants the real Python streams must satisfy."""

    @classmethod
    def setUpClass(cls):
        import run_scenario  # noqa: PLC0415

        cls.rs = run_scenario
        cls.observed = {}
        for name in ("story_gen3_admin.json", "nuzlocke_gen1_permadeath.json"):
            with open(os.path.join(_ROUTE_ORACLE, "scenarios", name), encoding="utf-8") as fh:
                out = run_scenario.Runner(json.load(fh)).run()
            assert not out.get("error"), f"{name}: {out.get('error')}"
            cls.observed[name] = out["checkpoints"]

    def _battles(self):
        for name, cps in self.observed.items():
            for cp in cps:
                battle = (cp.get("event") or {}).get("battle")
                if battle:
                    yield name, cp["seq"], battle

    def test_fold_preserves_order_and_boundaries(self):
        flat = [
            {"type": "turn_start", "round": 1},
            _attack("player", 0, "enemy", 0, "Tackle", 7),
            {"type": "turn_start", "round": 2},
            _attack("enemy", 0, "player", 0, "Ember", 5),
            _attack("player", 0, "enemy", 0, "Tackle", 6),
        ]
        turns = self.rs._fold_turns(flat)
        self.assertEqual([t["turn"] for t in turns], [1, 2])
        self.assertEqual([len(t["events"]) for t in turns], [1, 2])
        self.assertEqual(turns[1]["events"][0]["move_name"], "Ember")

    def test_an_event_before_the_first_turn_is_a_hard_error(self):
        with self.assertRaises(RuntimeError):
            self.rs._fold_turns([_attack("player", 0, "enemy", 0, "Tackle", 7)])

    def test_turn_numbers_are_contiguous_and_match_the_round_count(self):
        seen = 0
        for name, seq, battle in self._battles():
            seen += 1
            turns = battle["turns"]
            self.assertEqual([t["turn"] for t in turns],
                             list(range(1, len(turns) + 1)),
                             f"{name} seq {seq}: turn numbers are not 1..N")
            self.assertEqual(len(turns), battle["rounds"],
                             f"{name} seq {seq}: turn count != rounds")
        self.assertGreater(seen, 0, "no battle checkpoints observed")

    def test_every_battle_actually_carries_attack_events(self):
        for name, seq, battle in self._battles():
            total = sum(len(t["events"]) for t in battle["turns"])
            self.assertGreater(total, 0, f"{name} seq {seq}: empty attack projection")

    def test_attack_events_carry_the_full_semantic_field_set(self):
        required = {"type", "side", "attacker_idx", "target_side", "target_idx",
                    "move_name", "move_type", "damage", "type_eff", "crit",
                    "is_special", "attacker_hp_after", "target_hp_after",
                    "extra_attack"}
        for name, seq, battle in self._battles():
            for turn in battle["turns"]:
                for event in turn["events"]:
                    self.assertEqual(set(event), required, f"{name} seq {seq}")
                    self.assertIn(event["side"], ("player", "enemy"))
                    self.assertNotEqual(event["side"], event["target_side"])


class BattleEventObservationIsNeutralTests(unittest.TestCase):
    """`battle_loop.battle_events` must be observation only: appends to a list
    nothing else reads, with no RNG, state or control-flow consequence."""

    def _run(self, seed):
        from pokelike import battle_loop, rng as rng_mod  # noqa: PLC0415

        from pokelike import engine as engine_mod  # noqa: PLC0415

        stream = rng_mod.Mulberry32(seed)
        previous = rng_mod.set_active_stream(stream)
        try:
            player = [engine_mod._make_wild_combatant(1, 20),
                      engine_mod._make_wild_combatant(4, 19)]
            enemy = [engine_mod._make_wild_combatant(7, 20),
                     engine_mod._make_wild_combatant(25, 18)]
            result = battle_loop.run_battle(player, enemy)
        finally:
            rng_mod.set_active_stream(previous)
        return result, stream.state

    def test_the_same_seed_gives_the_same_outcome_and_rng_state(self):
        first, first_state = self._run(4242)
        second, second_state = self._run(4242)
        self.assertEqual(first_state, second_state)
        self.assertEqual(first.player_won, second.player_won)
        self.assertEqual(first.rounds, second.rounds)
        self.assertEqual(cp_mod.dumps(first.battle_events),
                         cp_mod.dumps(second.battle_events))

    def test_events_are_produced_and_are_append_only_records(self):
        result, _ = self._run(4242)
        self.assertTrue(result.battle_events)
        self.assertEqual(result.battle_events[0]["type"], "turn_start")
        starts = [e for e in result.battle_events if e["type"] == "turn_start"]
        self.assertEqual([e["round"] for e in starts],
                         list(range(1, result.rounds + 1)))

    def test_the_event_list_is_per_result_not_shared(self):
        first, _ = self._run(4242)
        second, _ = self._run(99)
        self.assertIsNot(first.battle_events, second.battle_events)
        from pokelike import battle_loop  # noqa: PLC0415

        self.assertEqual(battle_loop.BattleResult(
            player_won=False, player_team=[], enemy_team=[],
            player_participants=set(), rounds=0).battle_events, [])


class PendingOptionObservedStreamTests(unittest.TestCase):
    """The projection, as it actually appears in the real Python streams."""

    @classmethod
    def setUpClass(cls):
        import run_scenario  # noqa: PLC0415

        cls.observed = {}
        for name in ("story_gen4_submap_full.json", "story_gen3_admin.json",
                     "nuzlocke_gen1_permadeath.json"):
            with open(os.path.join(_ROUTE_ORACLE, "scenarios", name), encoding="utf-8") as fh:
                out = run_scenario.Runner(json.load(fh)).run()
            assert not out.get("error"), f"{name}: {out.get('error')}"
            cls.observed[name] = out["checkpoints"]

    def _pendings(self, name, phase):
        return [cp["pending"] for cp in self.observed[name]
                if cp["pending"] and cp["pending"]["phase"] == phase]

    def test_every_required_choice_class_is_actually_observed(self):
        """Asserted from the streams, not from a manifest `covers` list."""
        seen = {p["phase"]
                for cps in self.observed.values()
                for cp in cps if cp["pending"]
                for p in [cp["pending"]]}
        for phase in ("choose_starter", "catch_choice", "item_choice", "swap_choice"):
            self.assertIn(phase, seen, f"no {phase} pending observed in any stream")

    def test_option_count_always_matches_the_option_list(self):
        for name, cps in self.observed.items():
            for cp in cps:
                if cp["pending"]:
                    self.assertEqual(cp["pending"]["option_count"],
                                     len(cp["pending"]["options"]),
                                     f"{name} seq {cp['seq']}")

    def test_swap_pendings_carry_both_incoming_and_team(self):
        swaps = self._pendings("story_gen4_submap_full.json", "swap_choice")
        self.assertTrue(swaps, "no swap screen observed")
        for pending in swaps:
            self.assertIsNotNone(pending["context"])
            self.assertIsNotNone(pending["context"]["incoming"]["species_id"])
            self.assertEqual(pending["context"]["incoming"]["role"], "incoming")
            self.assertEqual([o["slot"] for o in pending["context"]["team"]],
                             list(range(len(pending["context"]["team"]))))

    def test_item_offers_carry_item_ids_and_no_species(self):
        offers = self._pendings("story_gen3_admin.json", "item_choice")
        self.assertTrue(offers, "no item screen observed")
        for pending in offers:
            for option in pending["options"]:
                self.assertEqual(option["kind"], "item")
                self.assertIsNotNone(option["item_id"])
                self.assertIsNone(option["species_id"])

    def test_catch_offers_carry_full_instances(self):
        offers = self._pendings("nuzlocke_gen1_permadeath.json", "catch_choice")
        self.assertTrue(offers, "no catch screen observed")
        for pending in offers:
            for option in pending["options"]:
                self.assertIsNotNone(option["instance"], "a catch option lost its instance")
                self.assertIsNotNone(option["instance"]["level"])


class SwapReleaseObservationTests(unittest.TestCase):
    """The full-team *replace* branch, as it actually appears in the stream.

    M3.4 **Defect A**: `swap_release` (bundle.deobfuscated.js:79202-79246) was
    implemented on both runtimes but no scenario ever reached a six-member
    team, so the release cards were never built and never clicked -- the
    observed role census over all eight scenarios was `starter`/`catch`/`item`/
    `swap_accept`/`incoming`/`team` and nothing else. These tests pin the
    observation itself, on the Python stream, in ordinary discovery.
    """

    FIXTURE = "story_gen1_swap_release.json"

    @classmethod
    def setUpClass(cls):
        import run_scenario  # noqa: PLC0415
        from pokelike import engine  # noqa: PLC0415

        cls.team_cap = engine.TEAM_CAP
        with open(os.path.join(_ROUTE_ORACLE, "scenarios", cls.FIXTURE), encoding="utf-8") as fh:
            out = run_scenario.Runner(json.load(fh)).run()
        assert not out.get("error"), f"{cls.FIXTURE}: {out.get('error')}"
        cls.cps = out["checkpoints"]
        cls.evidence = cov_mod.derive(cls.cps)

    def _release_screen(self):
        """The `choice_pre` that offers the release cards, and the
        `choice_post` that resolves it."""
        for i, cp in enumerate(self.cps):
            pending = cp["pending"]
            if (
                cp["kind"] == "choice_pre"
                and cp["screen"] == "swap-screen"
                and pending
                and pending["options"]
                and all(o["role"] == "swap_release" for o in pending["options"])
            ):
                post = next(
                    c for c in self.cps[i + 1:] if c["kind"] == "choice_post"
                )
                return cp, post
        self.fail("no full-team swap_release screen observed in the stream")

    def test_swap_release_role_is_observed(self):
        roles = {o["role"]
                 for cp in self.cps if cp["pending"]
                 for o in cp["pending"]["options"]}
        self.assertIn("swap_release", roles)

    def test_release_cards_exist_only_with_a_full_team(self):
        """The source's loop guard is `!(iu || ip)` where `iu` is
        `state.team.length < 6` (79144/79202), so a release card cannot exist
        unless the team is at `TEAM_CAP`."""
        pre, _ = self._release_screen()
        self.assertEqual(len(pre["team"]), self.team_cap)
        self.assertEqual(len(pre["pending"]["options"]), self.team_cap)

    def test_release_options_are_the_team_in_order(self):
        """One card per `state.team[i]`, in team order, each carrying its own
        slot. A reversal or a rotation changes this."""
        pre, _ = self._release_screen()
        options = pre["pending"]["options"]
        self.assertEqual([o["slot"] for o in options], list(range(len(pre["team"]))))
        self.assertEqual(
            [o["species_id"] for o in options],
            [m["species_id"] for m in pre["team"]],
        )
        # The normalized team member carries its own `slot`; the option carries
        # that separately, so the instance is the member without it.
        self.assertEqual(
            [o["instance"] for o in options],
            [{k: v for k, v in m.items() if k != "slot"} for m in pre["team"]],
        )

    def test_clicking_index_i_releases_team_i(self):
        """`state.team.splice(B2j, 1, B)` at 79230: the clicked slot -- and no
        other -- is replaced, and the team's cardinality does not change."""
        pre, post = self._release_screen()
        clicked = post["event"]["index"]
        self.assertIsNotNone(clicked, "the release screen was cancelled, not clicked")
        self.assertEqual(len(post["team"]), len(pre["team"]))
        changed = [s for s, (a, b) in enumerate(zip(pre["team"], post["team"])) if a != b]
        self.assertEqual(
            changed, [clicked],
            f"clicking index {clicked} changed slot(s) {changed}",
        )
        incoming = pre["pending"]["context"]["incoming"]
        self.assertEqual(post["team"][clicked]["species_id"], incoming["species_id"])

    def test_incoming_is_not_one_of_the_release_options(self):
        """The incoming Pokemon lives only in `showSwapScreen`'s arguments and
        the listener closures -- it is never a team slot, so it must appear as
        `context.incoming` and never carry the `swap_release` role."""
        pre, _ = self._release_screen()
        incoming = pre["pending"]["context"]["incoming"]
        self.assertEqual(incoming["role"], "incoming")
        self.assertIsNone(incoming["slot"])
        self.assertNotIn(incoming["instance"], [o["instance"] for o in pre["pending"]["options"]])

    def test_coverage_tag_is_earned_at_the_click(self):
        """Not at the screen: parking on a swap screen builds the affordance
        but never exercises it."""
        _, post = self._release_screen()
        self.assertIn("swap_release", self.evidence)
        self.assertEqual(self.evidence["swap_release"], [post["seq"]])

    def test_swap_release_is_a_required_tag(self):
        self.assertIn("swap_release", cov_mod.REQUIRED_TAGS)


class SwapReleaseDerivationTests(unittest.TestCase):
    """`coverage.derive`'s `swap_release`, against hand-built streams. These
    are the negative cases the real fixture cannot demonstrate."""

    @staticmethod
    def _mon(species_id, level=5):
        """Only the fields `coverage.derive` actually reads off a team member.
        `species_id`/`level` matter because the evolution rule inspects them."""
        return {"slot": 0, "species_id": species_id, "level": level}

    def _team(self, species):
        return [dict(self._mon(s), slot=i) for i, s in enumerate(species)]

    def _stream(self, *, roles="swap_release", team_before=None, team_after=None,
                pending_after=None, kind_pre="choice_pre", screen="swap-screen"):
        before = team_before if team_before is not None else self._team(range(10, 16))
        after = team_after if team_after is not None else self._team([99, 11, 12, 13, 14, 15])
        options = [{"role": roles, "slot": i} for i in range(len(before))]
        return [
            _checkpoint(0, kind_pre, screen=screen, team=before,
                        pending={"phase": "swap_choice", "optional": True,
                                 "option_count": len(options), "options": options,
                                 "context": None}),
            _checkpoint(1, "choice_post", screen="map-screen", team=after,
                        pending=pending_after),
        ]

    def test_the_baseline_earns_the_tag(self):
        self.assertEqual(cov_mod.derive(self._stream()).get("swap_release"), [1])

    def test_a_screen_that_is_never_clicked_earns_nothing(self):
        """No `choice_post` at all -- the affordance was built and abandoned."""
        stream = self._stream()[:1]
        self.assertNotIn("swap_release", cov_mod.derive(stream))

    def test_a_cancelled_screen_earns_nothing(self):
        """Cancel advances the node without touching the team (79249-79258),
        so no slot changes."""
        stream = self._stream(team_after=self._team(range(10, 16)))
        self.assertNotIn("swap_release", cov_mod.derive(stream))

    def test_the_accept_branch_does_not_count(self):
        stream = self._stream(roles="swap_accept")
        self.assertNotIn("swap_release", cov_mod.derive(stream))

    def test_a_multi_slot_change_does_not_count(self):
        """`splice(i, 1, incoming)` replaces exactly one slot. Two changed
        slots is not that operation."""
        stream = self._stream(team_after=self._team([99, 98, 12, 13, 14, 15]))
        self.assertNotIn("swap_release", cov_mod.derive(stream))

    def test_a_shrinking_team_does_not_count(self):
        """A release that dropped a member instead of replacing it."""
        stream = self._stream(team_after=self._team(range(11, 16)))
        self.assertNotIn("swap_release", cov_mod.derive(stream))

    def test_options_that_do_not_cover_the_team_do_not_count(self):
        stream = self._stream(team_before=self._team(range(10, 16)))
        stream[0]["pending"]["options"] = stream[0]["pending"]["options"][:3]
        self.assertNotIn("swap_release", cov_mod.derive(stream))

    def test_a_still_pending_post_does_not_count(self):
        stream = self._stream(pending_after={"phase": "swap_choice", "optional": True,
                                             "option_count": 0, "options": [],
                                             "context": None})
        self.assertNotIn("swap_release", cov_mod.derive(stream))

    def test_lingering_on_the_screen_earns_the_tag_once(self):
        """Several checkpoints can sit on the same swap screen; the tag is
        keyed on the choice_pre/choice_post PAIR, so the evidence cannot be
        inflated by the extra ones."""
        stream = self._stream()
        stream.insert(0, _checkpoint(0, "node_post", screen="swap-screen",
                                     team=stream[0]["team"],
                                     pending=stream[0]["pending"]))
        for seq, cp in enumerate(stream):
            cp["seq"] = seq
        self.assertEqual(cov_mod.derive(stream).get("swap_release"), [2])


class CoverageAttributionTests(unittest.TestCase):
    """`compare.py --all` must derive coverage on BOTH runtimes and require
    them to agree. These pin the two guards directly, so removing either one
    fails here rather than only in a cross-runtime run that happens to notice.
    """

    @classmethod
    def setUpClass(cls):
        import compare  # noqa: PLC0415 -- route-oracle is on sys.path above

        cls.compare = compare

    def _failure_text(self, results):
        """`compare._fail` prints the diagnosis and raises `SystemExit(1)`, so
        the message lives on stderr, not on the exception."""
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            with self.assertRaises(SystemExit):
                self.compare.check_coverage(results)
        return buffer.getvalue()

    def _result(self, js_cov, py_cov, expected=None):
        return [{
            "file": "fake.json",
            "coverage": js_cov,
            "py_coverage": py_cov,
            "expected_coverage": expected if expected is not None else js_cov,
        }]

    def _complete(self):
        return {tag: [1] for tag in cov_mod.REQUIRED_TAGS}

    def test_complete_and_agreeing_coverage_passes(self):
        full = self._complete()
        self.compare.check_coverage(self._result(full, dict(full)))

    def test_a_python_only_gap_is_rejected(self):
        """JS-only attribution: the source reaches every path but the port
        does not. Must fail, not pass on the JS stream's strength."""
        full = self._complete()
        short = {k: v for k, v in full.items() if k != "admin"}
        with self.assertRaises(SystemExit):
            self.compare.check_coverage(self._result(full, short, expected=full))

    def test_a_js_only_gap_is_rejected(self):
        full = self._complete()
        short = {k: v for k, v in full.items() if k != "admin"}
        with self.assertRaises(SystemExit):
            self.compare.check_coverage(self._result(short, full, expected=full))

    def test_disagreeing_evidence_indices_are_rejected(self):
        """Both runtimes earn every tag, but from different checkpoints."""
        full = self._complete()
        shifted = {tag: [2] for tag in cov_mod.REQUIRED_TAGS}
        with self.assertRaises(SystemExit):
            self.compare.check_coverage(self._result(full, shifted, expected=full))

    # The two guards below are mutually redundant for most inputs -- either one
    # catches an ordinary one-sided gap on its own. These isolate them, so
    # removing EITHER fails here. Without that isolation each guard hides the
    # other's removal and both survive mutation.

    def test_the_python_stream_is_attributed_in_its_own_right(self):
        """Isolates the per-runtime derivation: the failure must name the PY
        stream, which only the `py` iteration can produce. If coverage were
        derived on the JS stream alone, the same input would fail (via the
        cross-runtime check) naming something else."""
        full = self._complete()
        short = {k: v for k, v in full.items() if k != "admin"}
        text = self._failure_text(self._result(full, short, expected=full))
        self.assertIn("PY", text, "the python stream was not independently attributed")

    def test_the_cross_runtime_check_catches_what_the_manifest_cannot(self):
        """Isolates the agreement guard: with no `expected_coverage` pinned for
        this entry the per-runtime loop skips it entirely, so only the
        js-versus-python comparison can reject a divergence."""
        full = self._complete()
        shifted = {tag: [2] for tag in cov_mod.REQUIRED_TAGS}
        results = self._result(full, shifted)
        results[0]["expected_coverage"] = None
        self.assertIn("DISAGREEMENT", self._failure_text(results))

    def test_an_edge_reorder_alone_loses_the_exact_parent_return_tag(self):
        """`advanceFromNode` never touches the edge list, so a restored map
        whose edges merely changed ORDER is not the saved object."""
        saved = {
            "index": 1, "is_sub_map": None,
            "nodes": [
                {"id": "n4_0", "type": "underground", "layer": 4, "col": 0,
                 "visited": False, "accessible": True, "revealed": True},
                {"id": "n5_0", "type": "battle", "layer": 5, "col": 0,
                 "visited": False, "accessible": False, "revealed": False},
                {"id": "n5_1", "type": "trainer", "layer": 5, "col": 1,
                 "visited": False, "accessible": False, "revealed": False},
            ],
            "edges": [["n4_0", "n5_0"], ["n4_0", "n5_1"]],
        }
        restored = copy.deepcopy(saved)
        nodes = {n["id"]: n for n in restored["nodes"]}
        nodes["n4_0"].update(visited=True, accessible=False)
        nodes["n5_0"].update(accessible=True, revealed=True)
        nodes["n5_1"].update(accessible=True, revealed=True)
        self.assertTrue(cov_mod._is_exact_advance(saved, restored, "n4_0"),
                        "the correct advanceFromNode restore must still pass")

        reordered = copy.deepcopy(restored)
        reordered["edges"] = list(reversed(reordered["edges"]))
        self.assertFalse(cov_mod._is_exact_advance(saved, reordered, "n4_0"),
                         "an edge REORDER still earned exact_parent_return")

        rewritten = copy.deepcopy(restored)
        rewritten["edges"][1] = ["n4_0", "n5_0"]
        self.assertFalse(cov_mod._is_exact_advance(saved, rewritten, "n4_0"),
                         "a rewritten edge endpoint still earned exact_parent_return")


class RouteSearchTests(unittest.TestCase):
    """M3.3b workstream 7. The Admin-route search must be deterministic,
    bounded, order-independent, self-verifying, and non-destructive.

    Fast by construction: one bounded gen3 search from the fixture's own seed
    (~0.3 s) is reused by the whole class. The exhaustive sweep is documented
    separately in `route-oracle/README.md` and is not part of discovery.
    """

    SEED = 240
    ALIGN = 26457513
    GEN3 = {"nuzlocke": False, "gen2": False, "gen3": True, "gen4": False}
    BOUNDS = {"max_expansions": 200000, "max_depth": 48, "max_maps": 2,
              "max_choice_options": 3}

    @classmethod
    def setUpClass(cls):
        import search_route  # noqa: PLC0415 -- route-oracle is on sys.path above

        cls.sr = search_route
        cls.scenario, cls.stats = search_route.search_scenario(
            target="admin", seeds=[cls.SEED], starters=[0, 1, 2], mode=cls.GEN3,
            align=cls.ALIGN, bounds=dict(cls.BOUNDS), name="searched",
        )

    # -- it finds something, and the something is real --------------------

    def test_the_search_derives_an_admin_route(self):
        self.assertIsNotNone(self.scenario, "no admin route found from the fixture seed")
        self.assertEqual(self.scenario["seed"], self.SEED)
        self.assertTrue(self.scenario["mode"]["gen3"])
        self.assertTrue(self.scenario["actions"])

    def test_the_derived_route_actually_earns_the_tag_on_an_observed_stream(self):
        """The route is trusted only because `coverage.derive` says so over a
        real run, not because the search claimed it."""
        earned, evidence, error = self.sr._verify_python(self.scenario, "admin")
        self.assertEqual(error, "")
        self.assertTrue(earned, f"admin not earned; got {sorted(evidence)}")

    def test_the_checked_in_admin_fixture_is_reproduced_by_verification(self):
        path = os.path.join(_ROUTE_ORACLE, "scenarios", "story_gen3_admin.json")
        with open(path, encoding="utf-8") as fh:
            fixture = json.load(fh)
        earned, evidence, error = self.sr._verify_python(fixture, "admin")
        self.assertEqual(error, "")
        self.assertTrue(earned)
        self.assertEqual(evidence["admin"], [71])

    def test_verification_rejects_a_corrupted_route(self):
        """A generated action list is never taken on trust: break the route
        and the same verification path must refuse it."""
        broken = copy.deepcopy(self.scenario)
        broken["actions"] = broken["actions"][:2]
        earned, _, error = self.sr._verify_python(broken, "admin")
        self.assertFalse(earned and not error,
                         "a truncated route still verified as earning admin")

    def test_verification_rejects_an_invalid_action(self):
        broken = copy.deepcopy(self.scenario)
        broken["actions"].insert(0, {"kind": "visit", "node": "n9_9"})
        earned, _, error = self.sr._verify_python(broken, "admin")
        self.assertNotEqual(error, "", "an impossible node did not surface as an error")
        self.assertFalse(earned)

    # -- determinism -------------------------------------------------------

    def _search(self, **over):
        kwargs = dict(target="admin", seeds=[self.SEED], starters=[0, 1, 2],
                      mode=self.GEN3, align=self.ALIGN, bounds=dict(self.BOUNDS),
                      name="searched")
        kwargs.update(over)
        return self.sr.search_scenario(**kwargs)[0]

    def test_repeated_searches_are_identical(self):
        self.assertEqual(cp_mod.dumps(self._search()), cp_mod.dumps(self.scenario))

    def test_seed_iteration_order_does_not_change_the_result(self):
        forward = self.sr._parse_seeds("240,241,242")
        shuffled = self.sr._parse_seeds("242,240,241")
        ranged = self.sr._parse_seeds("240-242")
        self.assertEqual(forward, shuffled)
        self.assertEqual(forward, ranged)
        self.assertEqual(cp_mod.dumps(self._search(seeds=shuffled)),
                         cp_mod.dumps(self._search(seeds=forward)))

    def test_duplicate_seeds_are_collapsed(self):
        self.assertEqual(self.sr._parse_seeds("240,240,240"), [240])

    def test_the_node_tie_break_is_content_derived_not_dict_order(self):
        """`_sorted_accessible` must order by (layer, col, id), so the walk
        does not depend on how `map.nodes` happened to be built."""
        from pokelike import map_gen  # noqa: PLC0415

        class _Map:
            def __init__(self, nodes):
                self.nodes = nodes

        def node(node_id, layer, col):
            n = map_gen.MapNode(id=node_id, type="battle", layer=layer, col=col)
            n.accessible = True
            return n

        ordered = {"n2_1": node("n2_1", 2, 1), "n1_2": node("n1_2", 1, 2),
                   "n1_0": node("n1_0", 1, 0)}
        reversed_insert = {k: ordered[k] for k in reversed(list(ordered))}

        class _State:
            def __init__(self, m):
                self.map = m

        self.assertEqual(self.sr._sorted_accessible(_State(_Map(ordered))),
                         ["n1_0", "n1_2", "n2_1"])
        self.assertEqual(self.sr._sorted_accessible(_State(_Map(reversed_insert))),
                         ["n1_0", "n1_2", "n2_1"])

    # -- the search inputs actually matter ---------------------------------

    def test_a_different_seed_gives_a_different_route(self):
        other = self._search(seeds=[241])
        if other is None:
            self.skipTest("seed 241 has no admin route within these bounds")
        self.assertNotEqual(cp_mod.dumps(other), cp_mod.dumps(self.scenario))
        self.assertEqual(other["seed"], 241)

    def test_a_different_target_gives_a_different_route(self):
        silver = self.sr.search_scenario(
            target="silver", seeds=[self.SEED], starters=[0, 1, 2],
            mode={"nuzlocke": False, "gen2": True, "gen3": False, "gen4": False},
            align=self.ALIGN, bounds=dict(self.BOUNDS), name="searched",
        )[0]
        if silver is None:
            self.skipTest("no silver route from this seed within these bounds")
        self.assertNotEqual(cp_mod.dumps(silver), cp_mod.dumps(self.scenario))

    def test_an_unknown_target_is_rejected(self):
        self.assertNotIn("not_a_tag", self.sr.TARGETS)

    def test_tightening_max_maps_makes_the_target_unreachable(self):
        """The admin node lives on map 2, so zero transitions cannot reach it.
        The search must exhaust cleanly and report no route -- not raise, not
        hang, and not return something it did not prove."""
        bounds = dict(self.BOUNDS, max_maps=0, max_expansions=20000)
        found, stats = self.sr.search_scenario(
            target="admin", seeds=[self.SEED], starters=[0], mode=self.GEN3,
            align=self.ALIGN, bounds=bounds, name="searched",
        )
        self.assertIsNone(found)
        self.assertGreater(stats["expansions"], 0)

    def test_tightening_max_depth_makes_the_target_unreachable(self):
        found, _ = self.sr.search_scenario(
            target="admin", seeds=[self.SEED], starters=[0, 1, 2], mode=self.GEN3,
            align=self.ALIGN, bounds=dict(self.BOUNDS, max_depth=4), name="searched",
        )
        self.assertIsNone(found)

    def test_exceeding_max_expansions_is_a_bounded_failure_not_a_wrong_answer(self):
        with self.assertRaises(self.sr.SearchExhausted) as caught:
            self.sr.search_scenario(
                target="admin", seeds=[self.SEED], starters=[0, 1, 2], mode=self.GEN3,
                align=self.ALIGN, bounds=dict(self.BOUNDS, max_expansions=25),
                name="searched",
            )
        self.assertIn("max-expansions", caught.exception.reason)
        self.assertLessEqual(caught.exception.stats["expansions"], 26)

    # -- cache -------------------------------------------------------------

    def test_the_cache_is_bound_to_its_inputs(self):
        import tempfile  # noqa: PLC0415

        spec = {"target": "admin", "seeds": [240], "bounds": dict(self.BOUNDS)}
        digest = self.sr._inputs_digest(spec)
        other = self.sr._inputs_digest(dict(spec, seeds=[241]))
        self.assertNotEqual(digest, other)
        # A single changed BOUND also invalidates it.
        self.assertNotEqual(
            digest,
            self.sr._inputs_digest(dict(spec, bounds=dict(self.BOUNDS, max_maps=1))),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cache.json")
            self.sr._write_cache(path, digest, self.scenario)
            self.assertEqual(cp_mod.dumps(self.sr._read_cache(path, digest)),
                             cp_mod.dumps(self.scenario))
            self.assertIsNone(self.sr._read_cache(path, other),
                              "a stale cache entry was served for different inputs")

    def test_a_tampered_cache_entry_is_not_served(self):
        import tempfile  # noqa: PLC0415

        digest = self.sr._inputs_digest({"target": "admin"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cache.json")
            self.sr._write_cache(path, digest, self.scenario)
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            blob["inputs_sha256"] = "0" * 64
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            self.assertIsNone(self.sr._read_cache(path, digest))

    # -- non-destructive ---------------------------------------------------

    def test_writing_into_scenarios_needs_an_explicit_flag(self):
        parser = self.sr.build_parser()
        args = parser.parse_args([
            "search", "--target", "admin", "--gen3", "--seeds", "240",
            "--out", os.path.join(_ROUTE_ORACLE, "scenarios", "clobber.json"),
        ])
        self.assertFalse(args.allow_fixture_overwrite)
        before = sorted(os.listdir(os.path.join(_ROUTE_ORACLE, "scenarios")))
        self.assertEqual(self.sr.cmd_search(args), 1)
        self.assertEqual(sorted(os.listdir(os.path.join(_ROUTE_ORACLE, "scenarios"))),
                         before, "the search wrote into scenarios/")

    def test_the_search_defaults_to_verifying(self):
        args = self.sr.build_parser().parse_args(
            ["search", "--target", "admin", "--gen3", "--seeds", "240"])
        self.assertTrue(args.verify)

    def test_verify_uses_both_runtimes_by_default(self):
        """`--python-only` proves strictly less: the source is the authority on
        whether a route reaches the target, so the JS runner must run unless
        the operator explicitly opts out."""
        args = self.sr.build_parser().parse_args(
            ["verify", "some.json", "--target", "admin"])
        self.assertFalse(args.python_only)

    def test_cmd_verify_actually_invokes_the_js_runner(self):
        """`verify` must really run both runtimes, not merely accept a
        `--python-only` flag it then ignores in the other direction."""
        calls = []
        real = self.sr._verify_js

        def spy(path, target):
            calls.append((path, target))
            return True, {}, "spy"

        args = self.sr.build_parser().parse_args([
            "verify", os.path.join(_ROUTE_ORACLE, "scenarios", "story_gen3_admin.json"),
            "--target", "admin",
        ])
        self.sr._verify_js = spy
        try:
            self.sr.cmd_verify(args)
        finally:
            self.sr._verify_js = real
        self.assertEqual(len(calls), 1, "the js runner was never invoked by `verify`")
        self.assertEqual(calls[0][1], "admin")

    def test_cmd_verify_fails_when_the_js_side_disagrees(self):
        real = self.sr._verify_js

        def disagreeing(path, target):
            return False, {}, ""

        args = self.sr.build_parser().parse_args([
            "verify", os.path.join(_ROUTE_ORACLE, "scenarios", "story_gen3_admin.json"),
            "--target", "admin",
        ])
        self.sr._verify_js = disagreeing
        try:
            self.assertEqual(self.sr.cmd_verify(args), 1)
        finally:
            self.sr._verify_js = real

    @unittest.skipUnless(_oracle_runnable(), "needs node + route-oracle/out/route-prefix.js")
    def test_the_js_side_independently_earns_the_tag(self):
        """The both-runtime half of `verify`, exercised rather than assumed."""
        path = os.path.join(_ROUTE_ORACLE, "scenarios", "story_gen3_admin.json")
        earned, evidence, error = self.sr._verify_js(path, "admin")
        self.assertEqual(error, "")
        self.assertTrue(earned, "the source stream did not earn admin")
        self.assertEqual(evidence["admin"], [71])

    @unittest.skipUnless(_oracle_runnable(), "needs node + route-oracle/out/route-prefix.js")
    def test_the_js_side_rejects_a_corrupted_route(self):
        import tempfile  # noqa: PLC0415

        broken = copy.deepcopy(self.scenario)
        broken["actions"] = broken["actions"][:2]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(broken, fh)
            earned, _, error = self.sr._verify_js(path, "admin")
        self.assertFalse(earned and not error)

    def test_at_most_one_generation_flag(self):
        args = self.sr.build_parser().parse_args(
            ["search", "--target", "admin", "--gen2", "--gen3", "--seeds", "240"])
        with self.assertRaises(SystemExit):
            self.sr._mode_from(args)


class SearchRouteVerifyTargetMappingTests(unittest.TestCase):
    """M4 repair item 4. `cmd_verify` must key its printed evidence by
    `TARGET_COVERAGE_TAG[target]`, never by the raw `--target` name.

    `second_map_progress`, `silver_loss`, `submap_full_lifecycle` and
    `submap_boss_loss` are search predicates that each earn a DIFFERENTLY
    NAMED coverage tag (map_transition / terminal_loss / exact_parent_return
    / submap_entry respectively -- see `TARGET_COVERAGE_TAG`). Before the
    fix, `cmd_verify` indexed `evidence[args.target]` directly
    (route-oracle/search_route.py:819/829), which raised `KeyError` for all
    four the moment `evidence` was actually earned, because the earned key
    was the mapped tag, not the target name. A target that happens to map to
    itself (e.g. "admin") could never have caught this.
    """

    # target -> (checked-in fixture verified against it, mapped tag)
    _CASES = {
        "second_map_progress": ("story_gen1_map0_to_map1.json", "map_transition"),
        "silver_loss": ("story_gen2_silver.json", "terminal_loss"),
        "submap_full_lifecycle": ("story_gen4_submap_full.json", "exact_parent_return"),
        "submap_boss_loss": ("story_gen4_underground.json", "submap_entry"),
    }

    @classmethod
    def setUpClass(cls):
        import search_route  # noqa: PLC0415 -- route-oracle is on sys.path above

        cls.sr = search_route

    def test_every_mapped_target_actually_exercises_the_alias_path(self):
        """A regression here is worthless if the target happens to map to
        itself, since `evidence[args.target]` would have worked anyway."""
        for target, (_fixture, tag) in self._CASES.items():
            self.assertEqual(self.sr._verify_tag(target), tag)
            self.assertNotEqual(target, tag, f"{target} does not exercise the alias path")

    def test_cmd_verify_does_not_crash_on_any_mapped_target(self):
        for target, (fixture, _tag) in self._CASES.items():
            with self.subTest(target=target):
                args = self.sr.build_parser().parse_args([
                    "verify", os.path.join(_ROUTE_ORACLE, "scenarios", fixture),
                    "--target", target, "--python-only",
                ])
                try:
                    rc = self.sr.cmd_verify(args)
                except KeyError as exc:
                    self.fail(f"cmd_verify raised KeyError({exc!r}) for target {target!r}")
                self.assertEqual(
                    rc, 0, f"{target}: the checked-in fixture should verify on its own tag"
                )

    @unittest.skipUnless(_oracle_runnable(), "needs node + route-oracle/out/route-prefix.js")
    def test_cmd_verify_passes_cross_runtime_for_every_mapped_target(self):
        for target, (fixture, _tag) in self._CASES.items():
            with self.subTest(target=target):
                args = self.sr.build_parser().parse_args([
                    "verify", os.path.join(_ROUTE_ORACLE, "scenarios", fixture),
                    "--target", target,
                ])
                self.assertEqual(self.sr.cmd_verify(args), 0)


if __name__ == "__main__":
    unittest.main()
