"""M7.0 — focused tests for the M7 sweep tool's own EVIDENCE GATE.

`pokelike/tests/test_route_oracle.py` does this for the frozen 29-scenario
oracle; this file does it for `route-oracle/sweep.py`. It tests the TOOL, not
the engine: what the comparison projection carries, what the episode digest is
a function of, what the coverage denominator is derived from, and what the
coverage accounting will and will not credit.

The M7-A independent audit failed M7 because three deliberate mutants survived
at exactly these layers (`docs/audits/M7-A-independent-tool-audit.md` §7):

    1. remove `battle_stages` from `sweep.project()` on both sides
    2. make `sweep.digest()` return a constant
    3. delete the required `node.start` target from `sweep-targets.json`

Each one is killed below by a named test, and each of those tests also carries
the mutant as an explicit, checked-in demonstration — so the detector is not
merely asserted to exist, it is shown failing to fire once the thing it
detects is removed.

Deliberately dependency-free and fast: no `node`, no subprocess, no bundle.
The cross-runtime adapter fixtures that DO need the source runtime live in
`pokelike/tests/test_sweep_adapter.py`.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from unittest.mock import patch as mock_patch

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROUTE_ORACLE = os.path.join(_ROOT, "route-oracle")
for _p in (_ROOT, _ROUTE_ORACLE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sweep  # noqa: E402
import run_scenario  # noqa: E402

from pokelike import data, engine, map_gen  # noqa: E402


# ---------------------------------------------------------------------------
# Deterministic fixtures, built from the REAL Python runtime
# ---------------------------------------------------------------------------
# Everything below is derived from an actual `run_scenario.Runner` checkpoint
# rather than a hand-written dict, so "the projection carries every checkpoint
# field" is checked against the schema the frozen gate really emits, and a new
# checkpoint field added upstream is covered the moment it exists.


def _real_side(seed: int = 12345, after_starter: bool = True) -> dict:
    """One side of a comparison, exactly as `PyRuntime.projection` builds it."""
    py = sweep.PyRuntime()
    try:
        py.reset({"seed": seed, "mode": {}, "scenario": "sweep-test"})
        if after_starter:
            legal = py.legal()
            starters = sorted((a for a in legal if a["kind"] == "choose_starter"),
                              key=sweep.canon_action)
            py.apply(starters[0])
        return copy.deepcopy(py.projection(0, {"phase": "fixture"}))
    finally:
        py.close()


class _SideCache:
    """The runtime fixture is built once; every test deep-copies it."""

    _initial = None
    _on_map = None

    @classmethod
    def initial(cls) -> dict:
        if cls._initial is None:
            cls._initial = _real_side(after_starter=False)
        return copy.deepcopy(cls._initial)

    @classmethod
    def on_map(cls) -> dict:
        if cls._on_map is None:
            cls._on_map = _real_side(after_starter=True)
        return copy.deepcopy(cls._on_map)


# ===========================================================================
# 1. Projection integrity  (M7-A audit mutant 1)
# ===========================================================================


class SweepProjectionTests(unittest.TestCase):
    """`sweep.project()` is the entire definition of what M7 compares.

    Anything it drops is silently declared equal on both runtimes, which is
    why the audit's first mutant — deleting `battle_stages` from both sides —
    made the minimized F-B reproducer finish with no divergence at all.
    """

    def test_projection_carries_every_checkpoint_field_but_the_two_excluded(self):
        side = _SideCache.on_map()
        projected = sweep.project(side)
        self.assertEqual(
            set(projected["checkpoint"]),
            set(side["checkpoint"]) - set(sweep.EXCLUDED_CHECKPOINT_FIELDS),
        )
        for excluded in sweep.EXCLUDED_CHECKPOINT_FIELDS:
            self.assertIn(excluded, side["checkpoint"])  # the field really exists
            self.assertNotIn(excluded, projected["checkpoint"])
        # ...and the non-checkpoint members SWEEP.md's disposition table
        # promises. `battles` is the battle evidence and `rng_draws_total` the
        # RNG total; `battle_stages` is M7's own addition; `battle_abilities`
        # and `run_passives` are M7-COMBINED (A1)'s, closing the traits/
        # passives/abilities limitation the M7 record carried as finding F2.
        self.assertEqual(set(projected) - {"checkpoint"},
                         {"battles", "battle_stages", "battle_abilities",
                          "run_passives", "rng_draws_total"})

    def test_every_legacy_checkpoint_field_is_materially_compared(self):
        """Presence in the dict is not the property that matters — being able
        to FAIL on is. Each legacy field is perturbed on one side only and the
        diff must name it."""
        base = _SideCache.on_map()
        for field in sorted(set(base["checkpoint"]) - set(sweep.EXCLUDED_CHECKPOINT_FIELDS)):
            with self.subTest(field=field):
                js = copy.deepcopy(base)
                py = copy.deepcopy(base)
                py["checkpoint"][field] = _perturb(py["checkpoint"][field])
                diffs = sweep.compare_projection(js, py)
                self.assertTrue(diffs, f"{field} is carried but never compared")
                self.assertTrue(
                    any(d["path"] == f"checkpoint.{field}"
                        or d["path"].startswith(f"checkpoint.{field}.")
                        or d["path"].startswith(f"checkpoint.{field}[")
                        for d in diffs),
                    f"a change to {field} was reported as {[d['path'] for d in diffs]}")

    def test_excluded_checkpoint_fields_really_are_ignored(self):
        base = _SideCache.on_map()
        js = copy.deepcopy(base)
        py = copy.deepcopy(base)
        for field in sweep.EXCLUDED_CHECKPOINT_FIELDS:
            py["checkpoint"][field] = _perturb(py["checkpoint"][field])
        self.assertEqual(sweep.compare_projection(js, py), [])

    # -- the audit's mutant 1, and its detector ----------------------------

    def _f_b_stage_swap(self) -> tuple[dict, dict]:
        """The retained F-B reproducer's signature, as a minimal fixture.

        `findings/M7-divergence-story_gen4_0179.json` diverges on exactly two
        paths: `battle_stages[0].enemy[0].def` (js 1, py 0) and
        `battle_stages[0].enemy[0].special` (js 0, py 1) — one stat stage
        applied to a DIFFERENT stat on each runtime. Reproduced here as the
        two projections that divergence compared, with no source runtime and
        no battle: the projection layer is what is under test.
        """
        js = _SideCache.on_map()
        py = copy.deepcopy(js)
        zero = {"atk": 0, "def": 0, "speed": 0, "special": 0, "spdef": 0}
        js["battle_stages"] = [{"player": [dict(zero)],
                                "enemy": [dict(zero, **{"def": 1})]}]
        py["battle_stages"] = [{"player": [dict(zero)],
                                "enemy": [dict(zero, special=1)]}]
        return js, py

    def test_battle_stages_is_required_and_materially_compared(self):
        js, py = self._f_b_stage_swap()
        diffs = sweep.compare_projection(js, py)
        self.assertEqual(
            sorted(d["path"] for d in diffs),
            ["battle_stages[0].enemy[0].def", "battle_stages[0].enemy[0].special"],
        )
        # The same identity the minimizer preserves, so this fixture and the
        # saved reproducer are the same finding.
        self.assertEqual(
            sweep.divergence_signature({"kind": "state", "diffs": diffs}),
            ["state", "battle_stages[].enemy[].def", "battle_stages[].enemy[].special"],
        )

    def test_removing_battle_stages_from_the_projection_hides_F_B(self):
        """THE MUTANT, checked in.

        Literal substitution: `project()` no longer emits `battle_stages` on
        either side. Under it the F-B fixture compares equal, which is exactly
        what the audit observed on the real reproducer — so
        `test_battle_stages_is_required_and_materially_compared` above is a
        real detector and not a tautology.
        """
        js, py = self._f_b_stage_swap()
        original = sweep.project

        def mutant(side: dict) -> dict:
            out = original(side)
            del out["battle_stages"]
            return out

        with mock_patch.object(sweep, "project", mutant):
            self.assertEqual(sweep.compare_projection(js, py), [])
        # ...and the real projection still catches it once restored.
        self.assertTrue(sweep.compare_projection(js, py))

    def test_the_stage_projection_reads_the_five_stat_stages(self):
        """`_stages_of` is the Python half of the added field; it must total
        the same five keys the JS `stagesOf` emits (sweep-adapter.js)."""

        class _Mon:
            stages = {"atk": 2, "def": -1, "speed": 0, "special": 1, "spdef": 3}

        self.assertEqual(sweep._stages_of(_Mon()),
                         {"atk": 2, "def": -1, "speed": 0, "special": 1, "spdef": 3})
        self.assertEqual(sweep._stages_of(object()),
                         {"atk": 0, "def": 0, "speed": 0, "special": 0, "spdef": 0})


def _perturb(value):
    """A value guaranteed to differ from `value`, of a comparable shape."""
    if isinstance(value, bool) or value is None:
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return value + "_M7_0"
    if isinstance(value, list):
        return value + ["__M7_0__"] if value else ["__M7_0__"]
    if isinstance(value, dict):
        out = dict(value)
        out["__M7_0__"] = True
        return out
    return "__M7_0__"


# ===========================================================================
# 2. Episode-digest integrity  (M7-A audit mutant 2)
# ===========================================================================


def _episode(config=None, actions=None, states=None, outcome="loss") -> dict:
    """A minimal episode record run through the real `sweep._finish`."""
    ep = {
        "config": config if config is not None else {"seed": 1, "mode": {"nuzlocke": False}},
        "actions": list(actions if actions is not None else
                        [{"kind": "advance_map"}, {"kind": "visit_node", "node_id": "n1_0"}]),
        "steps": [{"index": i, "state_digest": d}
                  for i, d in enumerate(states if states is not None else ["sd0", "sd1"])],
        "outcome": outcome,
        "divergence": None,
    }
    return sweep._finish(copy.deepcopy(ep))


class SweepDigestTests(unittest.TestCase):
    """`episode_digest` is the whole order-independence gate: the corpus is
    run in three orders and the per-episode digests must match. A digest that
    is not a function of the episode makes that gate vacuous — the audit
    replaced `digest()` with a constant and no integrity check fired."""

    def test_each_identity_input_changes_the_digest(self):
        base = _episode()["episode_digest"]
        variants = {
            "config": _episode(config={"seed": 2, "mode": {"nuzlocke": False}}),
            "config.mode": _episode(config={"seed": 1, "mode": {"nuzlocke": True}}),
            "actions": _episode(actions=[{"kind": "advance_map"},
                                         {"kind": "visit_node", "node_id": "n1_1"}]),
            "action_count": _episode(actions=[{"kind": "advance_map"}]),
            "states": _episode(states=["sd0", "sd1-changed"]),
            "outcome": _episode(outcome="win"),
        }
        for name, ep in variants.items():
            with self.subTest(input=name):
                self.assertNotEqual(ep["episode_digest"], base,
                                    f"changing {name} left the digest unchanged")

    def test_action_order_changes_the_digest(self):
        a = [{"kind": "advance_map"}, {"kind": "visit_node", "node_id": "n1_0"}]
        self.assertNotEqual(_episode(actions=a)["episode_digest"],
                            _episode(actions=list(reversed(a)))["episode_digest"])

    def test_state_digest_order_changes_the_digest(self):
        self.assertNotEqual(_episode(states=["a", "b"])["episode_digest"],
                            _episode(states=["b", "a"])["episode_digest"])

    def test_distinct_episodes_have_distinct_digests(self):
        """The reorder/replay assertion. Sixteen genuinely different episodes
        must produce sixteen different digests; a constant `digest()` collapses
        them to one and fails here."""
        eps = []
        for seed in range(4):
            for outcome in ("loss", "win", "terminal", "step_cap"):
                eps.append(_episode(config={"seed": seed, "mode": {"nuzlocke": False}},
                                    actions=[{"kind": "visit_node", "node_id": f"n{seed}_0"}],
                                    states=[f"sd{seed}{outcome}"],
                                    outcome=outcome))
        digests = [e["episode_digest"] for e in eps]
        self.assertEqual(len(set(digests)), len(eps))

    def test_a_constant_digest_collapses_them_all(self):
        """THE MUTANT, checked in: `digest()` returns a constant.

        Under it every episode reports the same identity, so the three-order
        corpus comparison agrees no matter what the runtimes did. This is what
        `test_distinct_episodes_have_distinct_digests` exists to fail on.
        """
        with mock_patch.object(sweep, "digest", lambda value: "c0nstant"):
            a = _episode(config={"seed": 1, "mode": {}}, outcome="loss")
            b = _episode(config={"seed": 999, "mode": {"nuzlocke": True}}, outcome="win")
            self.assertEqual(a["episode_digest"], b["episode_digest"])
        # Restored: the same pair is distinguishable again.
        self.assertNotEqual(
            _episode(config={"seed": 1, "mode": {}}, outcome="loss")["episode_digest"],
            _episode(config={"seed": 999, "mode": {"nuzlocke": True}},
                     outcome="win")["episode_digest"])

    def test_batch_position_and_wall_clock_are_not_digest_inputs(self):
        """The intended identity rule, stated as a test: the SAME episode run
        at a different position in a differently-ordered batch, at a different
        time, keeps its digest. That is what makes `--order reverse|sorted`
        comparable at all."""
        first = _episode()
        later = _episode()
        self.assertEqual(first["episode_digest"], later["episode_digest"])
        # Fields that describe the RUN rather than the EPISODE must not leak in.
        polluted = copy.deepcopy(first)
        polluted["wall_clock_s"] = 12.5
        polluted["batch_index"] = 7
        polluted["episode_id"] = "somewhere_else"
        polluted.pop("episode_digest")
        polluted.pop("steps_taken")
        polluted.pop("max_depth")
        self.assertEqual(sweep._finish(polluted)["episode_digest"],
                         first["episode_digest"])

    def test_the_digest_is_over_exactly_the_four_declared_inputs(self):
        ep = _episode()
        self.assertEqual(
            ep["episode_digest"],
            sweep.digest({
                "config": ep["config"],
                "actions": ep["actions"],
                "states": [s["state_digest"] for s in ep["steps"]],
                "outcome": ep["outcome"],
            }),
        )

    def test_digest_is_a_real_hash_of_its_argument(self):
        self.assertNotEqual(sweep.digest({"a": 1}), sweep.digest({"a": 2}))
        self.assertEqual(sweep.digest({"a": 1}), sweep.digest({"a": 1}))


# ===========================================================================
# 3. Coverage denominator completeness  (M7-A audit mutant 3)
# ===========================================================================


def _manifest() -> dict:
    return copy.deepcopy(sweep.load_targets())


class SweepDenominatorTests(unittest.TestCase):
    """The denominator has to be derived from the runtime in BOTH directions.

    Before M7.0 `validate_targets` only rejected manifest values the runtime
    lacked. The audit deleted the required `node.start` target and the
    validator returned no problems at all, so a shrinking denominator was
    indistinguishable from a complete one.
    """

    def test_the_checked_in_manifest_validates(self):
        self.assertEqual(sweep.validate_targets(_manifest()), [])

    def test_node_types_are_derived_from_map_gen(self):
        derived = sweep.runtime_node_types()
        self.assertEqual(derived, {v for n, v in vars(map_gen).items()
                                   if n.isupper() and isinstance(v, str)})
        self.assertIn(map_gen.START, derived)
        declared = {t["node_type"] for t in _manifest()["targets"] if t.get("node_type")}
        self.assertEqual(declared, derived)

    def test_reward_kinds_are_derived_from_the_submap_reward_table(self):
        derived = sweep.runtime_reward_kinds()
        self.assertEqual(derived, {r.id for r in data.get_submap_rewards()})
        declared = {t["reward_kind"] for t in _manifest()["targets"] if t.get("reward_kind")}
        self.assertEqual(declared, derived)

    # -- unknown values are rejected ---------------------------------------

    def test_an_unknown_node_target_is_rejected(self):
        m = _manifest()
        m["targets"].append({"id": "node.casino", "stratum": "node", "evidence": "sweep",
                             "rationale": "invented", "node_type": "casino"})
        problems = sweep.validate_targets(m)
        self.assertTrue(any("node.casino" in p and "not a map_gen node type" in p
                            for p in problems), problems)

    def test_an_unknown_reward_target_is_rejected(self):
        m = _manifest()
        m["targets"].append({"id": "reward.jackpot", "stratum": "node", "evidence": "sweep",
                             "rationale": "invented", "reward_kind": "jackpot"})
        problems = sweep.validate_targets(m)
        self.assertTrue(any("reward.jackpot" in p and "not a SUBMAP_REWARDS id" in p
                            for p in problems), problems)

    # -- required values cannot be dropped ---------------------------------

    def test_removing_the_required_node_start_target_is_rejected(self):
        """THE MUTANT, checked in: `node.start` deleted from the manifest.

        This is the audit's third survivor verbatim. It must now be a
        validator failure naming the missing map_gen node type.
        """
        m = _manifest()
        m["targets"] = [t for t in m["targets"] if t["id"] != "node.start"]
        problems = sweep.validate_targets(m)
        self.assertIn("no target names map_gen node type start", problems)

    def test_removing_any_required_node_target_is_rejected(self):
        for node_type in sorted(sweep.runtime_node_types()):
            with self.subTest(node_type=node_type):
                m = _manifest()
                m["targets"] = [t for t in m["targets"]
                                if t.get("node_type") != node_type]
                self.assertIn(f"no target names map_gen node type {node_type}",
                              sweep.validate_targets(m))

    def test_removing_any_required_reward_target_is_rejected(self):
        for kind in sorted(sweep.runtime_reward_kinds()):
            with self.subTest(reward_kind=kind):
                m = _manifest()
                m["targets"] = [t for t in m["targets"] if t.get("reward_kind") != kind]
                self.assertIn(f"no target names submap reward kind {kind}",
                              sweep.validate_targets(m))

    def test_a_node_target_that_declares_no_node_type_is_rejected(self):
        """The other way to make a family invisible: keep the id, drop the
        derived key, and the two-way check stops looking at it."""
        m = _manifest()
        for t in m["targets"]:
            if t["id"] == "node.start":
                t.pop("node_type")
        problems = sweep.validate_targets(m)
        self.assertIn("node.start: a node.* target must declare node_type", problems)
        self.assertIn("no target names map_gen node type start", problems)

    def test_the_pre_M7_0_checks_still_hold(self):
        for field, bad, needle in (
            ("phase", "not_a_phase", "is not a Phase"),
            ("action_kind", "teleport", "is not an action"),
            ("route_tag", "not_a_tag", "is not a REQUIRED_TAG"),
        ):
            with self.subTest(field=field):
                m = _manifest()
                m["targets"].append({"id": f"probe.{field}", "stratum": "phase",
                                     "evidence": "sweep", "rationale": "probe",
                                     field: bad})
                self.assertTrue(any(needle in p for p in sweep.validate_targets(m)))
        m = _manifest()
        m["targets"].append(copy.deepcopy(m["targets"][0]))
        self.assertTrue(any("duplicate target ids" in p for p in sweep.validate_targets(m)))

    def test_excluded_targets_still_need_a_reason(self):
        m = _manifest()
        for t in m["targets"]:
            if t["evidence"] == "excluded":
                t.pop("exclusion_reason")
                break
        self.assertTrue(any("need an exclusion_reason" in p
                            for p in sweep.validate_targets(m)))


# ===========================================================================
# 4. Coverage accounting — observed evidence only
# ===========================================================================


class SweepAccountingTests(unittest.TestCase):
    """Coverage is credited from the state both runtimes agreed on, never
    from the plan, the scheduler's preference, or the manifest."""

    def setUp(self) -> None:
        self.targets = _manifest()
        self.ledger = sweep.CoverageLedger(self.targets)
        self.side = _SideCache.on_map()
        self.initial = _SideCache.initial()

    def _step(self, action: dict, before: dict, after: dict, legal=None) -> dict:
        return {"index": 0, "action": action,
                "legal": {"actions": legal if legal is not None else []},
                "state_before": before, "state_after": after}

    def _episode(self, mode=None) -> dict:
        return {"episode_id": "probe",
                "config": {"seed": 1, "mode": mode if mode is not None else {}},
                "starter_position": 0}

    # -- node.start, honestly earned ---------------------------------------

    def test_node_start_is_earned_from_the_observed_occupied_node(self):
        """The start node is where `startMap` puts the run; it is `visited`
        from that moment and is never an accessible visit target, so no
        `visit_node` action can earn it. It IS in the compared checkpoint."""
        cp = self.side["checkpoint"]
        occupied = next(n for n in cp["map"]["nodes"] if n["id"] == cp["current_node"])
        self.assertEqual(occupied["type"], map_gen.START)
        self.assertTrue(occupied["visited"])
        self.assertFalse(occupied["accessible"])  # ...hence unreachable by visit_node

        step = self._step({"kind": "choose_starter", "species_id": 1},
                          self.initial, self.side)
        sweep.observe_coverage(self.ledger, step, self._episode())
        self.assertIn("node.start", self.ledger.earned)
        self.assertEqual(self.ledger.earned["node.start"]["source"], "sweep")

    def test_no_visit_node_action_can_reach_the_start_node(self):
        """The property that makes the occupancy rule necessary rather than a
        convenience: the start node is never in `legal_actions`."""
        py = sweep.PyRuntime()
        try:
            py.reset({"seed": 12345, "mode": {}, "scenario": "sweep-test"})
            py.apply(sorted((a for a in py.legal() if a["kind"] == "choose_starter"),
                            key=sweep.canon_action)[0])
            start_ids = {n.id for n in py.state.map.nodes.values()
                         if n.type == map_gen.START}
            self.assertTrue(start_ids)
            visits = {a["node_id"] for a in py.legal() if a["kind"] == "visit_node"}
            self.assertEqual(visits & start_ids, set())
        finally:
            py.close()

    def test_occupancy_credit_needs_the_node_to_be_in_the_observed_map(self):
        side = copy.deepcopy(self.side)
        side["checkpoint"]["current_node"] = "n99_9"  # not in map.nodes
        step = self._step({"kind": "advance_map"}, self.initial, side)
        sweep.observe_coverage(self.ledger, step, self._episode())
        self.assertNotIn("node.start", self.ledger.earned)

    # -- intent is never evidence -------------------------------------------

    def test_a_visit_node_action_earns_nothing_the_observed_map_does_not_show(self):
        """THE MUTANT this guards against: crediting a target from the action
        the scheduler asked for rather than from the node it actually landed
        on. The action below names a node that is not in the observed map."""
        before = copy.deepcopy(self.side)
        after = copy.deepcopy(self.side)
        # Neither side is standing on a mapped node, so the ONLY thing that
        # could credit a `node.*` target here is the action's own node_id.
        before["checkpoint"]["current_node"] = None
        after["checkpoint"]["current_node"] = None
        step = self._step({"kind": "visit_node", "node_id": "n_does_not_exist"},
                          before, after)
        sweep.observe_coverage(self.ledger, step, self._episode())
        self.assertEqual([t for t in self.ledger.earned if t.startswith("node.")], [])
        self.assertEqual([t for t in self.ledger.earned if t.startswith("reward.")], [])
        self.assertIn("action.visit_node", self.ledger.earned)  # the action itself did happen

    def test_the_guided_policy_wants_targets_but_never_credits_them(self):
        """`guided_policy` re-orders candidates by what is still un-earned.
        Preferring a target must not mark it earned — only a run can."""
        import random as _random

        before = dict(self.ledger.earned)
        policy = sweep.guided_policy(_random.Random(7), self.ledger)
        legal = [{"kind": "select_option", "index": None, "cancel": True},
                 {"kind": "select_option", "index": 0, "cancel": False}]
        for _ in range(25):
            chosen = policy(0, legal, self.side)
            self.assertIn(chosen, legal)
        self.assertEqual(self.ledger.earned, before)
        self.assertEqual(self.ledger.earned, {})

    def test_the_episode_bucket_comes_from_the_compared_checkpoint_not_the_plan(self):
        """A plan entry is intent. `checkpoint.mode` is a compared field, so
        it is what both runtimes reported; if they disagreed the episode would
        already have failed before coverage was observed."""
        side = copy.deepcopy(self.side)
        side["checkpoint"]["mode"] = {"nuzlocke": True, "gen2": False,
                                      "gen3": False, "gen4": True}
        step = self._step({"kind": "advance_map"}, self.initial, side)
        # The PLAN claims a plain Gen1 story episode...
        sweep.observe_coverage(self.ledger, step,
                               self._episode(mode={"nuzlocke": False}))
        # ...and the OBSERVED state is what got credit.
        self.assertIn("episode.nuzlocke_gen4", self.ledger.earned)
        self.assertNotIn("episode.story_gen1", self.ledger.earned)

    def test_an_unknown_target_id_is_a_hard_error(self):
        with self.assertRaises(KeyError):
            self.ledger.hit("node.casino", "sweep", "probe#0")

    def test_the_ledger_reports_excluded_targets_as_never_earned(self):
        report = self.ledger.report()
        self.assertEqual(report["total"], len(self.targets["targets"]))
        self.assertEqual(
            report["required"],
            sum(1 for t in self.targets["targets"] if t["evidence"] != "excluded"))
        for tid in report["excluded"]:
            self.assertNotIn(tid, report["missing"])
            self.assertNotIn(tid, report["earned"])

    def test_the_cancel_exit_is_credited_from_the_offer_both_runtimes_made(self):
        legal = [{"kind": "select_option", "index": None, "cancel": True}]
        step = self._step({"kind": "select_option", "index": 0, "cancel": False},
                          self.side, self.side, legal=legal)
        sweep.observe_coverage(self.ledger, step, self._episode())
        self.assertIn("legality.item_equip_cancel_offered", self.ledger.earned)
        # ...but taking the cancel exit is a different target from being offered it.
        self.assertNotIn("exit.cancel", self.ledger.earned)


# ===========================================================================
# 5. Action-vocabulary invariants the comparison depends on
# ===========================================================================


class SweepActionVocabularyTests(unittest.TestCase):

    def test_provenance_is_reported_but_never_compared(self):
        a = {"kind": "advance_map", sweep.PROV_KEY: "one derivation"}
        b = {"kind": "advance_map", sweep.PROV_KEY: "another derivation"}
        self.assertEqual(sweep.canon_action(a), sweep.canon_action(b))
        self.assertIsNone(sweep.compare_legal([a], [b]))

    def test_a_duplicate_normalized_action_is_an_adapter_bug(self):
        dup = [{"kind": "visit_node", "node_id": "n1_0"},
               {"kind": "visit_node", "node_id": "n1_0", sweep.PROV_KEY: "elsewhere"}]
        self.assertIsNotNone(sweep.action_multiset_error(dup))
        self.assertIsNone(sweep.action_multiset_error(
            [{"kind": "visit_node", "node_id": "n1_0"},
             {"kind": "visit_node", "node_id": "n1_1"}]))

    def test_legal_sets_are_compared_as_sets_never_intersected(self):
        js = [{"kind": "advance_map"}]
        py = [{"kind": "advance_map"},
              {"kind": "select_option", "index": None, "cancel": True}]
        mismatch = sweep.compare_legal(js, py)
        self.assertIsNotNone(mismatch)
        self.assertEqual(mismatch["js_only"], [])
        self.assertEqual(len(mismatch["py_only"]), 1)
        self.assertIn('"cancel":true', mismatch["py_only"][0])

    def test_every_action_kind_the_manifest_names_is_produced_by_the_adapter(self):
        """The Python adapter and the manifest must agree on the vocabulary;
        a kind in one and not the other is an untestable target."""
        source = open(os.path.join(_ROUTE_ORACLE, "sweep.py"), encoding="utf-8").read()
        declared = {t["action_kind"] for t in _manifest()["targets"] if t.get("action_kind")}
        for kind in declared:
            with self.subTest(kind=kind):
                self.assertIn(f'"kind": "{kind}"', source)


# ===========================================================================
# 6. The `reorder_team` domain (M7-COMBINED A2 -- resolves finding F1)
# ===========================================================================


class SweepReorderDomainTests(unittest.TestCase):
    """M7 recorded, but did not resolve, the fact that the two runtimes
    describe team reordering with different breadth (**F1**).

    A2 resolves it by DECLARING the source's domain -- the transpositions --
    as the canonical compared one. The tests below are what keep that from
    being a silent intersection: the wider Python capability is shown to be
    real, the source is shown to be unable to express it, and the reduction
    is shown to fail loudly if the declaration it reduces FROM ever changes.
    """

    def test_the_declared_domain_is_the_sources_atomic_action(self):
        self.assertEqual(sweep.REORDER_DOMAIN, "transposition")

    def test_the_enumeration_is_exactly_the_transpositions(self):
        for n in (1, 2, 3, 4, 5, 6):
            with self.subTest(team_size=n):
                actions = sweep.reorder_transpositions({"team_size": n})
                pairs = [(a["i"], a["j"]) for a in actions]
                self.assertEqual(pairs, [(i, j) for i in range(n)
                                         for j in range(i + 1, n)])
                self.assertEqual(len(pairs), n * (n - 1) // 2)
                self.assertTrue(all(i < j for i, j in pairs), "canonical i < j")
                self.assertEqual(len(set(pairs)), len(pairs), "no duplicates")

    def test_a_full_six_member_team_offers_all_fifteen(self):
        """The full-team case the M7-combined brief names explicitly. The
        cross-runtime half -- that the SOURCE offers these same fifteen and
        executes each identically -- is `test_sweep_adapter.SixMemberTeamTests`
        against the checked-in `six_member_team` fixture."""
        self.assertEqual(len(sweep.reorder_transpositions({"team_size": 6})), 15)

    def test_the_python_capability_really_is_wider(self):
        """Half of what makes this a declared reduction rather than a hidden
        one: the engine genuinely accepts a permutation that is NOT a
        transposition, so there is a real breadth difference being decided
        about. A 3-cycle moves three members at once, which no single drag
        can do.
        """
        eng = engine.Engine()
        state = eng.reset(seed=4242)
        state = eng.step(engine.ChooseStarter(
            species_id=state.pending.options[0]["species_id"]))
        # Give the run a team big enough for a 3-cycle to exist.
        state.team = [state.team[0]] * 1 + [
            copy.deepcopy(state.team[0]) for _ in range(2)]
        for slot, mon in enumerate(state.team):
            mon.name = f"slot{slot}"
        state = eng.step(engine.ReorderTeam(order=(1, 2, 0)))
        self.assertEqual([m.name for m in state.team],
                         ["slot1", "slot2", "slot0"])

    def test_a_three_cycle_is_not_expressible_as_one_transposition(self):
        """...and the other half: that wider element is genuinely outside the
        compared domain, so declaring the domain is a real decision."""
        cycle = (1, 2, 0)
        moved = [i for i, target in enumerate(cycle) if target != i]
        self.assertEqual(len(moved), 3, "a transposition moves exactly two slots")

    def test_the_adapter_only_ever_builds_single_transpositions(self):
        """The wider capability is unreachable by CONSTRUCTION, not by
        filtering: `py_reorder_action` builds the identity order with exactly
        two positions exchanged, so no permutation is ever created that would
        then have to be intersected away."""
        for n in (2, 4, 6):
            for a in sweep.reorder_transpositions({"team_size": n}):
                with self.subTest(team_size=n, pair=(a["i"], a["j"])):
                    order = sweep.py_reorder_action(a, n).order
                    self.assertEqual(sorted(order), list(range(n)))
                    moved = [k for k in range(n) if order[k] != k]
                    self.assertEqual(moved, sorted((a["i"], a["j"])))

    def test_the_reduction_fails_loudly_if_the_engine_redeclares_the_domain(self):
        """The guard that keeps this honest over time. If `legal_actions`
        stops reporting `{"team_size": n}`, the transposition reduction is no
        longer known to be valid against what the engine now offers, and the
        tool must refuse rather than keep enumerating pairs."""
        with self.assertRaises(AssertionError):
            sweep.reorder_transpositions({"permutations": 720})
        with self.assertRaises(AssertionError):
            sweep.reorder_transpositions({})

    def test_the_engine_still_declares_the_wider_permutation_form(self):
        """The premise of the whole reduction, read off the live engine rather
        than assumed -- this is what the guard above is guarding."""
        eng = engine.Engine()
        state = eng.reset(seed=4242)
        state = eng.step(engine.ChooseStarter(
            species_id=state.pending.options[0]["species_id"]))
        # `reorder_team` is only declared once there is something to reorder
        # (`len(state.team) > 1`, engine.py), which is also why the source's
        # drag handler needs a second `.team-slot` to drop onto.
        self.assertNotIn("reorder_team", engine.legal_actions(state),
                         "a one-member team has no swap to offer")
        state.team.append(copy.deepcopy(state.team[0]))
        declared = engine.legal_actions(state)["reorder_team"]
        self.assertEqual(set(declared), {"team_size"})
        self.assertEqual(declared["team_size"], len(state.team))

    def test_the_source_has_exactly_one_order_mutation_and_it_is_a_swap(self):
        """The source fact the decision rests on, asserted against the real
        adapter rather than restated in prose: the JS side enumerates the same
        pairs, and its executor performs a two-slot swap."""
        adapter = open(os.path.join(_ROUTE_ORACLE, "sweep-adapter.js"),
                       encoding="utf-8").read()
        self.assertIn("64798-64806", adapter, "the drag handler is cited")
        self.assertIn("tm[a.i] = tm[a.j]", adapter, "the executor is a swap")
        self.assertNotIn("permutation of", adapter)


if __name__ == "__main__":
    unittest.main()
