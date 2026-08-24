"""M7 — the bounded search's snapshot honesty.

A backtracking search is only admissible if returning to an ancestor returns
the engine to EXACTLY the state that ancestor had -- including its RNG
position. Otherwise a sibling action is tried from a different RNG stream than
the one the ancestor really occupied, the search gets free re-rolls, and the
"route" it emits does not reproduce when replayed. Nothing found that way is
evidence of anything.

This file pins that property executably, and it is written so that each test
FAILS against the broken snapshotter rather than merely asserting the correct
one exists. The broken form is checked in below as `_broken_snapshot` /
`_broken_restore` and is exercised directly, so the detector is demonstrated
firing, not just claimed.

The defect being pinned, concretely (see `sweep.engine_snapshot`'s section
header for the full trace): `engine.Engine` owns a PRIVATE `rng.Mulberry32`
(`engine.py:677`) that it makes `pokelike.rng`'s active stream only for the
duration of each `reset()`/`step()` (`engine.py:718, 789`). Outside a step --
exactly when a search snapshots -- `rng.get_rng_seed()` reads the module-level
default stream instead, which the engine never touches.

Dependency-free and fast: no `node`, no subprocess, no bundle. The
cross-runtime half (that a searched route replays identically through BOTH
runtimes) lives in `test_sweep_adapter.py`.
"""

from __future__ import annotations

import copy
import os
import random
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROUTE_ORACLE = os.path.join(_ROOT, "route-oracle")
for _p in (_ROOT, _ROUTE_ORACLE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sweep  # noqa: E402
from pokelike import engine, rng  # noqa: E402


_GEN4 = {"nuzlocke": False, "gen2": False, "gen3": False, "gen4": True}


def _fresh(seed: int = 20260824, mode: dict = None) -> engine.Engine:
    mode = mode or _GEN4
    eng = engine.Engine()
    eng.reset(nuzlocke_mode=mode["nuzlocke"], gen2_mode=mode["gen2"],
              gen3_mode=mode["gen3"], gen4_mode=mode["gen4"], seed=seed)
    return eng


def _to_engine(eng: engine.Engine, action: dict):
    if action["kind"] == "reorder_team":
        return sweep.py_reorder_action(action, len(eng.state.team))
    return sweep.py_action_to_engine(action)


def _step(eng: engine.Engine, action: dict) -> None:
    eng.step(_to_engine(eng, action))


# --- the broken snapshotter, kept so the tests can demonstrate the defect ---

def _broken_snapshot(eng: engine.Engine) -> tuple:
    """What a search that reaches for the module-level RNG API captures.
    `rng.get_rng_seed()` does NOT read `eng._rng_stream` outside a step."""
    return (copy.deepcopy(eng.state), rng.get_rng_seed())


def _broken_restore(eng: engine.Engine, snap: tuple) -> None:
    eng.state = copy.deepcopy(snap[0])
    rng.seed_rng(snap[1])


class SearchSnapshotHonestyTests(unittest.TestCase):
    """The snapshot/restore contract the bounded search rests on."""

    def test_the_engine_rng_is_not_the_module_default_outside_a_step(self):
        """The root cause, stated directly: the two are different objects, so
        reading the module default in place of the engine's own stream
        captures a value that has nothing to do with the engine.

        The module default is a process-wide singleton that other tests in
        this suite legitimately advance (anything calling `battle.py` /
        `map_gen.py` directly), so this pins the RELATIONSHIP rather than any
        particular default value -- it must hold whatever else has run first.
        A sentinel is written into the default here precisely so the test is
        independent of suite order."""
        eng = _fresh()
        self.assertIsNot(rng.get_active_stream(), eng._rng_stream)
        # The engine really has advanced its own stream (three `rollShiny`
        # draws in `reset`).
        self.assertNotEqual(eng._rng_stream.state, 0)

        sentinel = 0x5EED_1234
        rng.seed_rng(sentinel)
        # Writing the module default did NOT touch the engine's stream, and
        # reading it back does NOT report the engine's position: the two are
        # fully decoupled outside a step.
        self.assertEqual(rng.get_rng_seed(), sentinel)
        self.assertNotEqual(eng._rng_stream.state, sentinel)
        self.assertNotEqual(rng.get_rng_seed(), eng._rng_stream.state)

    def test_the_honest_snapshot_round_trips_the_engine_stream_exactly(self):
        """`Mulberry32` holds one 32-bit word and `seed()` sets it raw, so
        snapshot/restore is a lossless identity round-trip."""
        eng = _fresh()
        snap = sweep.engine_snapshot(eng)
        before = eng._rng_stream.state
        legal = sweep.py_legal_actions(eng.state)
        _step(eng, legal[0])
        self.assertNotEqual(eng._rng_stream.state, before)
        sweep.engine_restore(eng, snap)
        self.assertEqual(eng._rng_stream.state, before)

    def test_restore_then_repeat_the_same_action_is_deterministic(self):
        """The property the search actually depends on: the same action, taken
        twice from the same restored snapshot, must produce the same RNG
        position and the same team."""
        eng = _fresh()
        legal = sweep.py_legal_actions(eng.state)
        action = legal[0]
        snap = sweep.engine_snapshot(eng)

        _step(eng, action)
        first = (eng._rng_stream.state,
                 [(m.species_id, m.level, m.current_hp) for m in eng.state.team])

        sweep.engine_restore(eng, snap)
        _step(eng, action)
        second = (eng._rng_stream.state,
                  [(m.species_id, m.level, m.current_hp) for m in eng.state.team])

        self.assertEqual(first, second)

    def test_the_broken_snapshotter_grants_a_free_rng_reroll(self):
        """The mutant, demonstrated firing. With the broken pair the SAME
        action from the SAME restored state lands on a different RNG position
        -- which is precisely how a search invents routes that do not replay.

        If this test ever starts failing, the broken form has stopped being
        broken and the test above has stopped proving anything."""
        eng = _fresh()
        action = sweep.py_legal_actions(eng.state)[0]
        snap = _broken_snapshot(eng)

        _step(eng, action)
        first = eng._rng_stream.state

        _broken_restore(eng, snap)
        _step(eng, action)
        second = eng._rng_stream.state

        self.assertNotEqual(
            first, second,
            "the broken snapshotter unexpectedly round-tripped; this test can "
            "no longer demonstrate the defect it exists to pin")

    def test_restoring_one_snapshot_repeatedly_stays_stable(self):
        """A search tries several siblings from one node, so the same snapshot
        is restored many times. A shallow restore would let step N mutate the
        stored state and silently corrupt siblings N+1..k."""
        eng = _fresh()
        snap = sweep.engine_snapshot(eng)
        legal = sweep.py_legal_actions(eng.state)[:3]
        seen = []
        for action in legal:
            sweep.engine_restore(eng, snap)
            _step(eng, action)
            seen.append(eng._rng_stream.state)
        # Re-run the identical sweep; every sibling must land where it did.
        again = []
        for action in legal:
            sweep.engine_restore(eng, snap)
            _step(eng, action)
            again.append(eng._rng_stream.state)
        self.assertEqual(seen, again)

    def test_restore_deep_copies_so_a_later_step_cannot_mutate_the_snapshot(self):
        """The stored RunState must not alias the live one."""
        eng = _fresh()
        snap = sweep.engine_snapshot(eng)
        sweep.engine_restore(eng, snap)
        self.assertIsNot(eng.state, snap[0])
        team_before = len(snap[0].team)
        for action in sweep.py_legal_actions(eng.state)[:1]:
            _step(eng, action)
        self.assertEqual(len(snap[0].team), team_before)


class SearchEmitsReplayableRoutesTests(unittest.TestCase):
    """A searched route is a plan: replaying it in a FRESH engine must
    reproduce the search's own trajectory exactly. This is the end-to-end
    consequence of the snapshot contract above."""

    def _goal_after_n_offers(self, n: int):
        box = {"seen": 0}

        def goal(state, action):
            box["seen"] += 1
            return box["seen"] > n and action.get("kind") == "visit_node"

        return goal

    def _replay(self, seed: int, actions: list) -> list:
        eng = _fresh(seed)
        trace = []
        for a in actions:
            _step(eng, a)
            trace.append((eng._rng_stream.state,
                          tuple((m.species_id, m.level, m.current_hp)
                                for m in eng.state.team)))
        return trace

    def test_a_searched_route_replays_identically_in_a_fresh_engine(self):
        found = sweep.search_episode(
            seed=1234567, mode=_GEN4, goal=self._goal_after_n_offers(90),
            max_steps=40, max_expansions=2500, rnd=random.Random(7))
        self.assertIsNotNone(found, "search found nothing at this budget")
        actions = found["actions"]
        self.assertGreater(len(actions), 3)
        first = self._replay(found["config"]["seed"], actions)
        second = self._replay(found["config"]["seed"], actions)
        self.assertEqual(first, second)

    def test_search_is_deterministic_for_a_fixed_rnd(self):
        """Same seed, same policy rnd, same route -- so a candidate can be
        re-derived, not just re-run."""
        kw = dict(seed=1234567, mode=_GEN4, max_steps=40, max_expansions=2500)
        a = sweep.search_episode(goal=self._goal_after_n_offers(90),
                                 rnd=random.Random(7), **kw)
        b = sweep.search_episode(goal=self._goal_after_n_offers(90),
                                 rnd=random.Random(7), **kw)
        self.assertIsNotNone(a)
        self.assertEqual(a["actions"], b["actions"])

    def test_a_candidate_carries_a_replayable_record_shape(self):
        """`search` output must be consumable by `replay-set` unchanged."""
        found = sweep.search_episode(
            seed=1234567, mode=_GEN4, goal=self._goal_after_n_offers(90),
            max_steps=40, max_expansions=2500, rnd=random.Random(7))
        self.assertIsNotNone(found)
        for key in ("episode_id", "config", "max_steps", "actions"):
            self.assertIn(key, found)
        self.assertIn("seed", found["config"])
        self.assertIn("mode", found["config"])
        self.assertGreaterEqual(found["max_steps"], len(found["actions"]))


class SearchSteeringIsOrderOnlyTests(unittest.TestCase):
    """Steering decides the ORDER siblings are tried in. It must not change
    WHICH actions exist, and it must not credit anything.

    The unsteered search demonstrably cannot reach `reward.fossil`: the
    reward lives on a REWARD node inside an UNDERGROUND submap, and
    `map_gen` places UNDERGROUND only on gen4 layer 4 of maps 1/3/6
    (map_gen.py:465-495), so the goal sits behind a gym leader, a map
    advance and a submap boss. These tests pin the two properties that keep
    a steered candidate exactly as honest as an unsteered one.
    """

    def test_the_priority_only_reorders_the_legal_set(self):
        """Sorting by priority is a permutation of `py_legal_actions`, so
        the tree the steered search explores is the tree the unsteered one
        explores."""
        eng = _fresh()
        _step(eng, {"kind": "choose_starter",
                    "species_id": sweep.py_legal_actions(eng.state)[0]["species_id"]})
        legal = sweep.py_legal_actions(eng.state)
        self.assertGreater(len(legal), 1)
        priority = sweep._reward_priority("fossil")
        ordered = sorted(legal, key=lambda a: priority(eng.state, a))
        self.assertEqual(sweep.canon_set(ordered), sweep.canon_set(legal))

    def test_the_priority_prefers_the_goal_node_inside_a_submap(self):
        """The ladder's top rung, checked against the source's own reward
        table rather than a hand-written list of node types."""
        priority = sweep._reward_priority("fossil")

        class _Node:
            def __init__(self, ntype, reward=None):
                self.type = ntype
                self.extra = {"reward": reward} if reward else {}

        class _Map:
            def __init__(self, nodes):
                self.nodes = nodes

        class _State:
            in_sub_map = "underground"
            team = []

            def __init__(self, nodes):
                self.map = _Map(nodes)

        state = _State({"n2_0": _Node("reward", "stat10"),
                        "n2_1": _Node("reward", "fossil"),
                        "n3_0": _Node("subexit")})
        want = priority(state, {"kind": "visit_node", "node_id": "n2_1"})
        other = priority(state, {"kind": "visit_node", "node_id": "n2_0"})
        exit_ = priority(state, {"kind": "visit_node", "node_id": "n3_0"})
        self.assertGreater(want, other)
        self.assertGreater(other, exit_)

    def test_the_submap_kinds_come_from_the_source_reward_table(self):
        """`fossil` is underground-only and the three distortion legendaries
        are distortion-only, per `SUBMAP_REWARDS` itself -- not per a list
        restated in the search."""
        self.assertEqual(sweep._reward_submap_kinds("fossil"), frozenset({"underground"}))
        self.assertEqual(sweep._reward_submap_kinds("giratina"), frozenset({"distortion"}))
        self.assertEqual(sweep._reward_submap_kinds("rare_candy"),
                         frozenset({"underground", "distortion"}))

    def test_prune_fires_only_inside_a_submap_without_the_reward(self):
        """The pruner's whole claim: a submap's reward ids are baked at
        generation time, so one that came up without the wanted id can never
        produce it."""
        prune = sweep._reward_prune("fossil")

        class _Node:
            def __init__(self, reward):
                self.extra = {"reward": reward}

        class _Map:
            def __init__(self, nodes):
                self.nodes = nodes

        class _State:
            def __init__(self, sub, nodes):
                self.in_sub_map = sub
                self.map = _Map(nodes)

        self.assertFalse(prune(_State(None, {})))
        self.assertTrue(prune(_State("underground", {"a": _Node("stat10"),
                                                     "b": _Node("skip")})))
        self.assertFalse(prune(_State("underground", {"a": _Node("fossil"),
                                                      "b": _Node("skip")})))

    def test_a_steered_route_still_replays_identically_in_a_fresh_engine(self):
        """The snapshot contract is unchanged by steering: a steered
        candidate is still a deterministic plan."""
        found = sweep.search_episode(
            seed=3678945229, mode=_GEN4, goal=sweep._goal_reward("fossil"),
            max_steps=45, max_expansions=800, rnd=random.Random(2828355564),
            priority=sweep._reward_priority("fossil"),
            prune=sweep._reward_prune("fossil"))
        self.assertIsNotNone(found, "the retained fossil candidate did not reproduce")
        actions = found["actions"]

        def trace() -> list:
            eng = _fresh(found["config"]["seed"])
            out = []
            for a in actions:
                _step(eng, a)
                out.append((eng._rng_stream.state,
                            tuple((m.species_id, m.level, m.current_hp)
                                  for m in eng.state.team)))
            return out

        self.assertEqual(trace(), trace())

    def test_the_retained_fossil_candidate_lands_on_a_fossil_reward_node(self):
        """The route retained as `M7-target-reward_fossil.json` really does
        end on an underground submap's `fossil` REWARD node -- checked by
        walking the PORT, independently of the lockstep replay that credited
        it."""
        import json

        path = os.path.join(_ROOT, "route-oracle", "findings",
                            "M7-target-reward_fossil.json")
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["credited_target"], "reward.fossil")

        eng = _fresh(record["config"]["seed"])
        actions = record["actions"]
        for a in actions[:-1]:
            _step(eng, a)
        last = actions[-1]
        self.assertEqual(last["kind"], "visit_node")
        self.assertEqual(eng.state.in_sub_map, "underground")
        node = sweep._node_of(eng.state, last["node_id"])
        self.assertIsNotNone(node)
        self.assertEqual(node.type, "reward")
        self.assertEqual(node.extra.get("reward"), "fossil")


if __name__ == "__main__":
    unittest.main()
