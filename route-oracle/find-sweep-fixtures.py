"""M7.0 -- derive the checked-in cross-runtime ADAPTER FIXTURES.

`pokelike/tests/test_sweep_adapter.py` needs episodes that actually reach six
specific source affordances (a six-member team, a non-usable bag item, a
usable item whose targets come from the source's own predicate, a held-item
overlay opened from a nonzero slot while another member also holds, a move
tutor whose offered positions differ from team identity, and the three
item-equip exits). Those states are deep: a random walk reaches almost none of
them, and hand-writing an action list would be inventing a route rather than
observing one.

This script SEARCHES for them, and it searches the only way that is sound here
(cf. the M3/M4 route search): the Python port only ever *proposes* a route,
and the source runtime *disposes* -- every candidate prefix is re-run in
lockstep against the real source before it is written out, so a fixture is a
route both runtimes agreed on step for step, not a Python-side claim.

    python route-oracle/find-sweep-fixtures.py            # all six
    python route-oracle/find-sweep-fixtures.py --goal six_member_team
    python route-oracle/find-sweep-fixtures.py --episodes 400

Output: `route-oracle/fixtures/sweep/<goal>.json`, each carrying the mode/seed,
the exact ordered normalized actions, the goal's own witness, and the protected
hashes at derivation time. Deterministic: the same command reproduces the same
files, because every episode is a pure function of `(seed, policy_seed)`.

Re-run this after any change that moves the routes (a map-generation or RNG
repair). A fixture that no longer reaches its goal must be re-derived, never
edited by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import sweep  # noqa: E402

from pokelike import engine  # noqa: E402

FIXTURE_DIR = os.path.join(_HERE, "fixtures", "sweep")


# ===========================================================================
# Goals
# ===========================================================================
# A goal is a predicate over (the Python engine state, the normalized legal
# set) plus a bias that tells the search which action families move toward it.
# The predicate is only ever used to STOP the search; what makes the resulting
# fixture evidence is the lockstep verification afterwards.


def _holders(state) -> list[int]:
    return [i for i, m in enumerate(state.team) if m.held_item]


def _use_targets(legal: list[dict]) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for a in legal:
        if a["kind"] == "use_item":
            out.setdefault(a["bag_index"], set()).add(a["target_index"])
    return out


def g_six_member_team(state, legal) -> bool:
    return len(state.team) >= 6 and any(a["kind"] == "reorder_team" for a in legal)


def g_nonusable_bag_item(state, legal) -> bool:
    # A bag item that produced equip_item and NO use_item is one the source's
    # own `it.usable` discriminator routed to `equipItemFromBag`.
    equips = {a["bag_index"] for a in legal if a["kind"] == "equip_item"}
    uses = set(_use_targets(legal))
    return bool(equips - uses) and len(state.team) >= 2


def g_usable_item_targets(state, legal) -> bool:
    # A usable item whose target set is a STRICT subset of the team proves the
    # source predicate (`usableItemCanTarget`) was consulted rather than the
    # adapter assuming "every member".
    targets = _use_targets(legal)
    return len(state.team) >= 2 and any(len(t) < len(state.team) for t in targets.values())


def _handoff_position(from_index: int, to_index: int) -> int:
    """Where `[data-idx="to_index"]` sits among `openItemEquipModal`'s buttons.

    The overlay renders one row per member, but the OPENING member's row
    carries `data-unequip` instead of `data-idx` (79521-79531), so the
    `[data-idx]` list has a hole in it: target `t` sits at position `t` when
    `t < from_index` and at `t - 1` when `t > from_index`. Value and position
    part ways exactly when `t > from_index` -- and that is the only shape in
    which a positional mis-map is observable at all (the M6 lesson, and why
    the M7 record's first M6 mutant survived on a two-member probe).
    """
    return to_index if to_index < from_index else to_index - 1


def g_held_item_nonzero_slot(state, legal) -> bool:
    """A hand-off where a POSITIONAL mis-map lands on a real, WRONG member.

    Value and position part ways when `to > from` (see `_handoff_position`),
    but that alone only proves the mutant would address a button that does not
    exist -- the source then refuses the action and the mis-map is caught as
    an execution error rather than as a wrong member. The sharper case also
    needs `to` to be a valid POSITION, i.e. `to <= len(team) - 2`, so the
    mutant clicks a genuine row belonging to somebody else and the held item
    lands on the wrong member. That needs a team of at least four.
    """
    holders = _holders(state)
    if len(holders) < 2 or max(holders) <= 0 or len(state.team) < 4:
        return False
    return any(a["kind"] == "hand_off_item"
               and a["from_index"] > 0
               and a["from_index"] in holders
               and _handoff_position(a["from_index"], a["to_index"]) != a["to_index"]
               and a["to_index"] <= len(state.team) - 2
               for a in legal)


def g_move_tutor_gap(state, legal) -> bool:
    if state.phase != engine.Phase.MOVE_TUTOR_CHOICE or state.pending is None:
        return False
    opts = state.pending.options
    return bool(opts) and any(o["team_index"] != i for i, o in enumerate(opts))


def g_item_equip_exits(state, legal) -> bool:
    if state.phase != engine.Phase.ITEM_EQUIP_CHOICE:
        return False
    picks = [a for a in legal if a["kind"] == "select_option" and a["index"] is not None]
    banks = [a for a in legal if a["kind"] == "select_option"
             and a["index"] is None and not a.get("cancel")]
    cancels = [a for a in legal if a.get("cancel")]
    # >= 2 team members so the overlay really offers a CHOICE of targets and
    # the "pick a target" exit is not degenerate.
    return bool(picks and banks and cancels) and len(picks) >= 2


def g_starter_select(state, legal) -> bool:
    """The starter screen itself.

    M7-COMBINED (A3). `choose_starter` was the one action family with no
    focused fixture: every other fixture's action list BEGINS with one, so the
    family was exercised everywhere and witnessed nowhere. This goal is
    satisfied by the initial state, so its fixture carries an EMPTY action
    list -- which is not a defect, it is what "the affordance is available
    before anything has happened" looks like. The execution half is the test
    appending a real `choose_starter` and replaying.
    """
    return (state.phase == engine.Phase.CHOOSE_STARTER
            and len([a for a in legal if a["kind"] == "choose_starter"]) >= 3)


def g_node_visit_fanout(state, legal) -> bool:
    """A map state offering a real CHOICE of accessible nodes.

    M7-COMBINED (A3). `visit_node` was likewise exercised by every fixture and
    witnessed by none. A fan-out of >= 2 is what makes the enumeration
    non-degenerate: with one accessible node, "the adapter enumerated the
    accessible set" and "the adapter enumerated everything" agree.
    """
    return (state.phase == engine.Phase.ON_MAP
            and len([a for a in legal if a["kind"] == "visit_node"]) >= 2)


def g_map_advance(state, legal) -> bool:
    """The badge screen's `#btn-next-map`.

    M7-COMBINED (A3). `advance_map` had no fixture at all -- it is the only
    family none of the six M7.0 fixtures reached, because getting there means
    actually beating the map's boss. `Phase.NEXT_MAP_READY` is
    `showBadgeScreen`'s counterpart, and the source offers exactly one button
    there, so the legal set must be the single `advance_map`.
    """
    return (state.phase == engine.Phase.NEXT_MAP_READY
            and [a["kind"] for a in legal] == ["advance_map"])


GOALS = {
    "starter_select": {
        "predicate": g_starter_select,
        "doc": "The starter screen: `showStarterSelect`'s three cards (76176-"
               "76186), enumerated before any action has been taken. The only "
               "fixture whose action list is legitimately empty.",
        "bias": ("choose_starter",),
    },
    "node_visit_fanout": {
        "predicate": g_node_visit_fanout,
        "doc": "A map state with at least two accessible nodes, so "
               "`onNodeClick`'s accessible-set enumeration (77312+) is "
               "non-degenerate.",
        "bias": ("choose_starter", "visit_node"),
    },
    "map_advance": {
        "predicate": g_map_advance,
        "doc": "The badge screen after a gym-leader win, where the source "
               "offers exactly one affordance: `#btn-next-map`.",
        "bias": ("visit_node", "select_option"),
        "prefer_index": True,
    },
    "six_member_team": {
        "predicate": g_six_member_team,
        "doc": "A full six-member team, so every one of the 15 transpositions is "
               "enumerated and `showSwapScreen`'s full-team branch is live.",
        # Growing a team means resolving offers, so bias hard toward progress.
        "bias": ("visit_node", "select_option", "advance_map"),
        "prefer_index": True,
    },
    "nonusable_bag_item": {
        "predicate": g_nonusable_bag_item,
        "doc": "A bag item the source's own `it.usable` flag routes to "
               "`equipItemFromBag` (64950) rather than `applyUsableItemTo` (64946).",
        "bias": ("visit_node", "select_option", "advance_map"),
        "prefer_index": True,
    },
    "usable_item_targets": {
        "predicate": g_usable_item_targets,
        "doc": "A usable item whose legal target set is a strict subset of the "
               "team -- i.e. `usableItemCanTarget` really gated it.",
        "bias": ("visit_node", "select_option", "advance_map"),
        "prefer_index": True,
    },
    "held_item_nonzero_slot": {
        "predicate": g_held_item_nonzero_slot,
        "doc": "A held-item overlay opened from a NONZERO member slot while "
               "another member also holds, on a team of >= 4, with a hand-off "
               "target whose `[data-idx]` VALUE differs from its position "
               "among the overlay's buttons (the M6 lesson).",
        "bias": ("visit_node", "select_option", "advance_map", "equip_item"),
        "prefer_index": True,
    },
    "move_tutor_gap": {
        "predicate": g_move_tutor_gap,
        "doc": "A move-tutor offer where a mastered member is filtered out, so "
               "`data-tutor` team identity != normalized option position (T2).",
        "bias": ("visit_node", "select_option", "advance_map"),
        "prefer_index": True,
    },
    "item_equip_exits": {
        "predicate": g_item_equip_exits,
        "doc": "An item-equip overlay offering all three source exits -- a "
               "`[data-idx]` target, `#btn-equip-to-bag`, `#btn-equip-cancel` "
               "-- with at least two distinct targets to choose between.",
        "bias": ("visit_node", "select_option", "advance_map", "equip_item"),
        "prefer_index": True,
    },
}


def goal_policy(rng: random.Random, spec: dict):
    """A biased-but-still-uniform-within-family sampler. Every action it can
    return is legal on both runtimes; the bias only changes which family is
    sampled, never what is allowed."""
    bias = spec["bias"]

    def policy(index: int, legal: list[dict], state: dict) -> dict:
        options = sorted(legal, key=sweep.canon_action)
        preferred = [a for a in options if a["kind"] in bias]
        pool = preferred if preferred and rng.random() < 0.9 else options
        if spec.get("prefer_index"):
            # Accepting an offer grows the team / fills the bag; skipping never
            # does. Take the offer four times out of five when one exists.
            picks = [a for a in pool
                     if a["kind"] == "select_option" and a["index"] is not None]
            if picks and rng.random() < 0.8:
                return rng.choice(picks)
        return rng.choice(pool)

    return policy


# ===========================================================================
# Search
# ===========================================================================


def search(goal: str, episodes: int, max_steps: int, base_seed: int,
           verbose: bool = True) -> dict | None:
    """Walk lockstep episodes until one satisfies the goal, then return the
    prefix that reached it. Lockstep from the first step, so a prefix can
    never contain a step the two runtimes disagreed on."""
    spec_goal = GOALS[goal]
    plan = sweep.make_plan(episodes, base_seed, max_steps)
    js = sweep.JsRuntime()
    py = sweep.PyRuntime()
    try:
        for n, entry in enumerate(plan["episodes"]):
            hit = _walk(js, py, entry, spec_goal, max_steps)
            if verbose:
                print(f"  [{n + 1}/{episodes}] {entry['episode_id']:<24} "
                      f"{'HIT at step ' + str(len(hit['actions'])) if hit else '-'}",
                      flush=True)
            if hit:
                return hit
    finally:
        js.close()
        py.close()
    return None


def _walk(js, py, entry: dict, spec_goal: dict, max_steps: int) -> dict | None:
    """One lockstep episode, stopping at the first state satisfying the goal.

    Mirrors `sweep.run_episode`'s loop, and deliberately reuses its own
    comparison functions rather than a looser copy: any legal-set or state
    disagreement abandons the episode instead of being walked past.
    """
    config = {"seed": int(entry["seed"]), "mode": dict(entry["mode"]),
              "episode_id": entry["episode_id"]}
    js.reset(config)
    py.reset(config)
    policy = goal_policy(random.Random(entry["policy_seed"]), spec_goal)

    js_seen = py_seen = 0
    before_js = js.state(js_seen, {"phase": "initial"})
    before_py = py.projection(py_seen, {"phase": "initial"})
    js_seen, py_seen = before_js["battles_total"], before_py["battles_total"]
    if sweep.compare_projection(before_js, before_py):
        return None

    actions: list[dict] = []
    for index in range(max_steps):
        js_legal = js.legal()["actions"]
        py_legal = py.legal()
        if sweep.compare_legal(js_legal, py_legal) is not None:
            return None
        if not py_legal:
            return None
        if spec_goal["predicate"](py.state, py_legal):
            return {"actions": actions, "config": config,
                    "witness": witness(py.state, py_legal)}
        action = policy(index, py_legal, before_py)
        bare = {k: v for k, v in action.items() if k != sweep.PROV_KEY}
        try:
            js.apply(action)
            py.apply(action)
        except Exception:  # noqa: BLE001 -- an asymmetric failure ends the walk
            return None
        actions.append(bare)
        after_js = js.state(js_seen, {"action": bare, "index": index})
        after_py = py.projection(py_seen, {"action": bare, "index": index})
        js_seen, py_seen = after_js["battles_total"], after_py["battles_total"]
        if sweep.compare_projection(after_js, after_py):
            return None
        before_py = after_py
        cp = after_py["checkpoint"]
        if cp.get("game_over") or cp.get("screen") == "win-screen":
            return None
    return None


def witness(state, legal: list[dict]) -> dict:
    """What the goal state actually looked like, recorded so the fixture can
    be read without re-running it (and so a drifted fixture is obvious)."""
    return {
        "phase": state.phase.value,
        "team_size": len(state.team),
        "held_item_slots": _holders(state),
        "bag": list(state.items),
        "legal_kinds": sorted({a["kind"] for a in legal}),
        "legal_count": len(legal),
        "pending_team_indices": (
            [o.get("team_index") for o in state.pending.options]
            if state.pending is not None else None),
    }


def verify(record: dict) -> dict:
    """Replay the saved prefix ALONE through `sweep.run_episode`'s real
    lockstep loop -- the same code path the test will use."""
    spec = dict(record["config"])
    spec["episode_id"] = record["goal"]
    js = sweep.JsRuntime()
    py = sweep.PyRuntime()
    try:
        ep = sweep.run_episode(js, py, spec, None, len(record["actions"]) + 1,
                               forced_actions=record["actions"])
        ok = ep["divergence"] is None
        reached = ok and GOALS[record["goal"]]["predicate"](py.state, py.legal())
        return {"clean": ok, "goal_reached": bool(reached),
                "outcome": ep["outcome"], "divergence": ep["divergence"]}
    finally:
        js.close()
        py.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="find-sweep-fixtures.py", description=__doc__)
    ap.add_argument("--goal", choices=sorted(GOALS), action="append")
    ap.add_argument("--episodes", type=int, default=600)
    ap.add_argument("--max-steps", type=int, default=140)
    ap.add_argument("--base-seed", type=int, default=20260823)
    args = ap.parse_args(argv[1:])

    os.makedirs(FIXTURE_DIR, exist_ok=True)
    failures = []
    for goal in (args.goal or sorted(GOALS)):
        print(f"\n== {goal} ==")
        hit = search(goal, args.episodes, args.max_steps, args.base_seed)
        if hit is None:
            print(f"  NOT FOUND in {args.episodes} episodes")
            failures.append(goal)
            continue
        record = {
            "goal": goal,
            "description": GOALS[goal]["doc"],
            "derived_by": "route-oracle/find-sweep-fixtures.py",
            "sweep_version": sweep.SWEEP_VERSION,
            "protected_hashes": sweep.protected_hashes(),
            "config": hit["config"],
            "actions": hit["actions"],
            "witness": hit["witness"],
            "search": {"base_seed": args.base_seed, "episodes": args.episodes,
                       "max_steps": args.max_steps},
        }
        checked = verify(record)
        record["verification"] = checked
        if not (checked["clean"] and checked["goal_reached"]):
            print(f"  VERIFY FAILED: {checked}")
            failures.append(goal)
            continue
        path = os.path.join(FIXTURE_DIR, goal + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"  wrote {path} ({len(hit['actions'])} actions)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
