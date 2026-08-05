"""Deterministic, bounded route search for the M3 route oracle.

`plan_route.py` is a **greedy authoring helper**: it takes a seed as input,
walks one path by a fixed type preference, and cannot search seeds, starters
or branch points. It therefore cannot re-derive a fragile multi-map fixture
such as `scenarios/story_gen3_admin.json`, whose provenance was a session
scratchpad. This module replaces that gap with a tracked tool (M3.3b
workstream 7).

Two subcommands:

    # derive a route that earns a coverage tag, within explicit bounds
    python route-oracle/search_route.py search --target admin --gen3 \\
        --seeds 240 --starters 2 --max-maps 2 --max-expansions 200000

    # re-run a scenario on BOTH runtimes and check the tag is really earned
    python route-oracle/search_route.py verify \\
        route-oracle/scenarios/story_gen3_admin.json --target admin

Design contract
---------------

* **Deterministic.** No RNG, no clock, no filesystem scan and no set/dict
  iteration order feeds a decision. Seeds are canonicalized with
  `sorted(set(...))`, so `--seeds 3,1,2` and `--seeds 1,2,3` search the same
  space in the same order and return the same route.
* **Bounded.** `--max-expansions`, `--max-depth`, `--max-maps` and the seed /
  starter lists are all explicit. Exhausting them is a clean exit 2 with the
  bounds and the counters printed, never a hang and never a silent "no".
* **Verified, not trusted.** `search` only proposes an action list; the tag is
  earned only when `coverage.derive` says so over a real observed checkpoint
  stream. `--verify` (on by default) re-runs the emitted scenario through
  `run_scenario.Runner` before printing it, and `verify` additionally runs the
  JavaScript runner so the *source* agrees the route reaches the target.
* **Non-destructive.** Writing into `scenarios/` is refused unless
  `--allow-fixture-overwrite` is passed, so ordinary validation can never
  rewrite a checked-in fixture.
* **Offline.** Nothing here opens a socket, and the only paths written are the
  ones named on the command line.

Why the search runs on the Python engine
----------------------------------------

A search has to evaluate thousands of partial runs. `plan_route.py` spawns one
`node` process per step, which is why it was only ever usable for a single
greedy walk. The Python engine runs a full 84-checkpoint route in ~23 ms and
branches by `copy.deepcopy` in ~0.02 ms, so the search explores in-process.
That makes the port the *proposer*; the source stays the *authority*, which is
what the `verify` subcommand exists to enforce.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Iterator, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import checkpoints as cp_mod  # noqa: E402
import coverage as coverage_mod  # noqa: E402
import run_scenario  # noqa: E402

from pokelike import engine, rng  # noqa: E402

SCHEMA_VERSION = 2

# Phases the driver has a `choice` bridge for. A route that parks anywhere
# else is unreplayable on the JavaScript side (see SCHEMA.md's "Phase <->
# screen" table), so the search treats it as a dead end rather than emitting
# an action the oracle cannot perform.
_BRIDGED_CHOICE_PHASES = (
    engine.Phase.CATCH_CHOICE,
    engine.Phase.ITEM_CHOICE,
    engine.Phase.SWAP_CHOICE,
)

# M4 repair item 1 added real bridges for five more phases: `driver.js` now
# detects `MOVE_TUTOR_CHOICE`/`ITEM_EQUIP_CHOICE`/`EVOLUTION_CHOICE`/
# `REWARD_TEAM_PICK` directly from the DOM (they never call `showScreen`) and
# `TRADE_CHOICE` via its own real `trade-screen`. `legendary_swap_*` and
# `shiny_resolved` need NO widening at all -- they resolve through
# `SWAP_CHOICE`/`CATCH_CHOICE`, already in the default set above -- and the
# Distortion targets are reached entirely through `visit` steps.
#
# Deliberately NOT one shared widened tuple: move-tutor and item nodes are
# common on an ordinary map, so walking into their full subtree instead of
# dead-ending immediately turns every search that widens ALL five phases at
# once into one that can still be running after minutes, even for a target
# that only needs ONE of them (confirmed: `move_tutor_resolved` alone,
# widened to all five, still hadn't found a route after 50000 expansions on
# a single seed). Each target therefore opts in to exactly the ONE extra
# phase its own predicate can fire inside -- `cmd_search` reads this table
# automatically so a caller doesn't have to know the phase/target mapping.
_TARGET_EXTRA_PHASE: dict[str, "engine.Phase"] = {
    "move_tutor_resolved": engine.Phase.MOVE_TUTOR_CHOICE,
    "trade_resolved": engine.Phase.TRADE_CHOICE,
    "item_equip_resolved": engine.Phase.ITEM_EQUIP_CHOICE,
    "branching_evolution_resolved": engine.Phase.EVOLUTION_CHOICE,
    "sacrifice_reward_resolved": engine.Phase.REWARD_TEAM_PICK,
    "stat10_reward_resolved": engine.Phase.REWARD_TEAM_PICK,
}


class SearchExhausted(Exception):
    """Raised when a bound is hit. Carries the counters for the report."""

    def __init__(self, reason: str, stats: dict) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stats = stats


# ---------------------------------------------------------------------------
# Target predicates
#
# Each predicate answers "did the transition that just happened earn this
# tag?" from real engine state. Most are deliberately CHEAP approximations
# used only to prune the search -- the authoritative answer always comes from
# `coverage.derive` over an observed stream, in `_verify_python` / `verify`.
#
# **Three of them are not** (M4.3). `shiny_accept_resolved`,
# `legendary_swap_full_replace` and `legendary_swap_full_decline` were added
# in M4.1 specifically to route branches nothing else reaches, so a malformed
# candidate they accept is a candidate the matrix would then be built on. They
# are held to the same semantic transition their `coverage.derive`
# counterparts require, proved from a real pre-step SNAPSHOT rather than from
# whatever `before` still points at.
#
# `ctx` carries the facts a predicate cannot recover from `before`/`after`
# alone. `before` is a SHALLOW copy, so a resolver that mutates `state.team`
# in place (`_resolve_swap_choice` splices) is already visible through it --
# and so is `state.map`, whose nodes `_advance` mutates in place, which is why
# the node's PRE-step flags have to be snapshotted or "it is visited now"
# proves nothing. Anything read strictly pre-step is snapshotted into `ctx` by
# `_pre_choice_facts`; the RNG stream belongs to `Engine` rather than
# `RunState`, so `_search_one` records it at the call site instead.
# ---------------------------------------------------------------------------


def _map_snapshot(gmap) -> Optional[dict]:
    """Everything `advanceFromNode` (engine.py:778-800) may touch, plus
    everything it must NOT, captured by value.

    `state.map` survives a step as the same object with mutated nodes, so a
    predicate that reads `after.map` for the "before" picture is reading the
    after-state twice. This is the only way a search-time progression check
    can tell "this choice consumed the node" from "the node was already
    consumed"."""
    if gmap is None:
        return None
    return {
        "map_index": gmap.map_index,
        "is_sub_map": gmap.is_sub_map,
        "edges": [tuple(e) for e in gmap.edges],
        "nodes": {
            node.id: {
                "type": node.type,
                "layer": node.layer,
                "col": node.col,
                "visited": bool(node.visited),
                "accessible": bool(node.accessible),
                "revealed": bool(node.revealed),
            }
            for node in gmap.nodes.values()
        },
    }


def _pre_choice_facts(state) -> dict:
    """The pre-step facts about a pending choice, by value, before any
    resolver has had a chance to mutate shared structure."""
    pending = state.pending
    if pending is None:
        return {}
    extra = pending.extra or {}
    return {
        "phase": pending.phase,
        "optional": bool(pending.optional),
        "team_size": len(state.team),
        # Set only by `_offer_swap_screen`; absent on `_try_add_to_team`'s
        # team-full-only SWAP_CHOICE (pokelike/engine.py:727-733, 754-762).
        "has_room": bool((extra or {}).get("has_room")),
        # -- M4.2 --------------------------------------------------------
        # `before` is a SHALLOW copy, so `before.team` is the very list the
        # resolvers append to and splice in place: after the step it already
        # shows the RESULT, and a predicate reading it would be comparing the
        # after-state with itself. This is an ordered snapshot of the member
        # references as they were before the step, which is what lets the
        # hardened targets below assert "every non-selected slot is the same
        # object, still in the same position".
        "team": list(state.team),
        # The offer and the node it belongs to, by reference, so a target can
        # require that the very object the player was shown is the object
        # that ends up installed -- not merely something of the same species.
        "node_id": extra.get("node_id"),
        "origin": extra.get("origin"),
        "candidates": list(extra.get("candidates") or []),
        "incoming": extra.get("incoming"),
        # Pre-step resume guards, so a target can check the branch-specific
        # clear/retain contract the `resume_state` projection compares.
        "saved_question_resolve": state.saved_question_resolve,
        "saved_catch": state.saved_catch,
        "saved_shiny_node": state.saved_shiny_node,
        # -- M4.3 --------------------------------------------------------
        # The engine's OWN option list, by value. `run_scenario.py` projects
        # the compared `pending.options`/`pending.context.team` from
        # `enumerate(st.team)` and then hard-fails if its cardinality
        # disagrees with this list (run_scenario.py:573-576), so requiring
        # these summaries to agree with the ordered pre-step team is the real
        # engine-side equivalent of "the six `swap_release` slots and the
        # projected context team agree". The port stores no literal
        # `pending.context.team` field, and none is invented here.
        "options": copy.deepcopy(pending.options),
        # `savedQuestionResolve`/`savedShinyNode` are keyed
        # `"m<currentMap>:<nodeId>"` (77319-77326 / 80879-80884), so the key
        # cannot be checked without the map index the offer was made on.
        "current_map": state.current_map,
        "current_node_id": state.current_node_id,
        "in_sub_map": state.in_sub_map,
        # The whole pre-step topology, for the exact-advance check below.
        "map": _map_snapshot(state.map),
    }


def _node_before(facts: dict, node_id: Optional[str]) -> Optional[dict]:
    """The raising node's PRE-step record, or None. Reading the family from
    here rather than from `pending.extra["origin"]` is what stops a synthetic
    origin string standing in for real map state."""
    pre_map = facts.get("map")
    if not pre_map or not node_id:
        return None
    return pre_map["nodes"].get(node_id)


def _exact_advance(facts: dict, after, node_id: Optional[str]) -> bool:
    """Is `after.map` exactly the pre-step map with `advanceFromNode(node_id)`
    applied, once?

    `advanceFromNode` (engine.py:778-800, bundle 53639-53655) does three
    things and nothing else: the node becomes visited and inaccessible; every
    OTHER accessible node on its layer becomes inaccessible; every successor
    across an outgoing edge becomes revealed and accessible. The node set,
    each node's id/type/layer/col, every other node's `visited`, the map
    index, the submap marker and the ORDERED edge list must all come back
    unchanged.

    Crucially the node must have been UNVISITED beforehand. `_advanced`, the
    predicate this replaces for the three hardened targets, asked only whether
    the node is visited afterwards -- which is true of a node consumed ten
    steps earlier, so it was not evidence that this choice advanced anything.
    """
    pre_map = facts.get("map")
    if not pre_map or after.map is None or not node_id:
        return False
    nodes_before = pre_map["nodes"]
    before_node = nodes_before.get(node_id)
    if before_node is None or before_node["visited"]:
        return False
    if pre_map["map_index"] != after.map.map_index:
        return False
    if pre_map["is_sub_map"] != after.map.is_sub_map:
        return False
    if pre_map["edges"] != [tuple(e) for e in after.map.edges]:
        return False
    nodes_after = after.map.nodes
    if set(nodes_before) != set(nodes_after):
        return False

    layer = before_node["layer"]
    successors = {dst for src, dst in pre_map["edges"] if src == node_id}

    for other_id, before in nodes_before.items():
        node = nodes_after[other_id]
        if (node.type, node.layer, node.col) != (before["type"], before["layer"], before["col"]):
            return False
        want_visited = before["visited"]
        want_accessible = before["accessible"]
        want_revealed = before["revealed"]
        if other_id == node_id:
            want_visited, want_accessible = True, False
        else:
            if before["layer"] == layer and before["accessible"]:
                want_accessible = False
            if other_id in successors:
                want_accessible, want_revealed = True, True
        if (bool(node.visited), bool(node.accessible), bool(node.revealed)) != (
            want_visited, want_accessible, want_revealed
        ):
            return False
    return True


def _rng_still(ctx: dict) -> bool:
    """No RNG movement across the resolver.

    None of `showSwapScreen`'s three exits and neither of `doShinyNode`'s
    calls `rng()` -- they are pure bookkeeping -- so a resolver that drew
    anything diverged even when the visible outcome matched. The stream is
    `Engine._rng_stream`, not `RunState`, so `_search_one` samples its real
    state either side of `branch.step(...)`; a Mulberry32 step always advances
    the state (it adds a nonzero constant, engine-side `rng._mulberry32_step`),
    so an unmoved state is proof no draw was taken."""
    before = ctx.get("rng_before")
    return before is not None and before == ctx.get("rng_after")


def _options_match_team(facts: dict) -> bool:
    """Do the pending's own option summaries agree, in order and identity,
    with the ordered pre-step team?

    `_offer_swap_screen` builds `[_mon_summary(m) for m in state.team]` on the
    full-team branch (engine.py:899), exactly one card per member in team
    order, which is `showSwapScreen`'s own release loop (79202-79246). The
    runner then projects THOSE positions as `swap_release` slots `0..5` and
    the same ordered team as `pending.context.team`, so agreement here is
    agreement there."""
    options = facts.get("options")
    team = facts.get("team")
    if not isinstance(options, list) or not team or len(options) != len(team):
        return False
    return all(options[i] == engine._mon_summary(m) for i, m in enumerate(team))


def _pre_current_node(facts: dict) -> bool:
    """M4.4. Was the raising node still the LIVE `currentNode` immediately
    before the resolver ran?

    `onNodeClick` assigns `state["currentNode"] = B` (77311) as its first
    statement and nothing between that and the screen's own exit rebinds it,
    so a choice raised by a node visit is pending WHILE that node is the
    current node. Both branches under repair then act on that pointer:
    `showSwapScreen`'s three exits null it (79186/79231/79256), and
    `doShinyNode`'s room branch deliberately leaves it alone (80961-80970).

    The post-state alone cannot carry this. "`currentNode` is null
    afterwards" is equally true of an exit that cleared it and of a state
    where it was never set, and "`currentNode` is still the node" is equally
    true of a retaining branch and of a step that touched nothing. Nor can
    `pending.extra["node_id"]`/`["origin"]` stand in: those are the port's
    own bookkeeping copies, written when the offer was built, not the live
    pointer the source reads. The identity has to be checked from real
    pre-step state, which `_pre_choice_facts` already snapshots."""
    node_id = facts.get("node_id")
    return bool(node_id) and facts.get("current_node_id") == node_id


def _advanced(after, node_id: Optional[str]) -> bool:
    """Did `advanceFromNode` really run over this node? Consumed nodes are
    `visited` and no longer `accessible` (bundle.deobfuscated.js:53639-53655).

    A cheap approximation, kept for the historical targets that only need
    pruning. It is NOT used by the three M4.3-hardened targets: it cannot
    tell a node this step consumed from one consumed earlier, because
    `after.map` is the same mutated object `before.map` points at. Those use
    `_exact_advance` against the pre-step snapshot instead."""
    if not node_id or after.map is None:
        return False
    node = after.map.nodes.get(node_id)
    return node is not None and bool(node.visited) and not node.accessible


def _target_admin(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """`coverage.derive`'s `admin`: a magma/aqua node that was advanced past
    with the run still alive (route-oracle/coverage.py:190-197)."""
    return (
        node_type in ("magma", "aqua")
        and not after.game_over
        and after.phase != engine.Phase.GAME_OVER
        and bool(after.fought_admin)
    )


def _target_silver(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """`silver` is earned on the WIN branch only."""
    return node_type == "silver" and after.silver_beaten > before.silver_beaten


def _target_submap_boss_win(before, after, node_type: Optional[str], ctx: dict) -> bool:
    return (
        before.in_sub_map is not None
        and node_type == "boss"
        and not after.game_over
        and after.phase != engine.Phase.GAME_OVER
    )


def _target_swap_release(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """`showSwapScreen`'s full-team REPLACE branch
    (bundle.deobfuscated.js:79202-79246).

    The release loop's guard is `B2a < state.team.length && !(iu || ip)` where
    `iu = state.team.length < 6` (79144), so a release card exists only with a
    FULL team; clicking card `i` splices `state.team[i]` out for the incoming
    Pokemon (79230). The target is therefore the CLICK, not merely arriving on
    the screen -- a route that only parks on it would leave the affordance
    built but never exercised, which is exactly the M3.4 Defect A gap.

    `has_room` false distinguishes this from `_offer_swap_screen`'s room
    branch, which offers the single incoming card instead.
    """
    facts = ctx.get("pre_choice") or {}
    return (
        facts.get("phase") is engine.Phase.SWAP_CHOICE
        and facts.get("team_size") == engine.TEAM_CAP
        and not facts.get("has_room")
        and ctx.get("choice_index") is not None
    )


def _target_silver_loss(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """Silver REACHED and lost to -- the encounter and its placement without
    the win branch. `coverage.derive` gives this no `silver` tag (that is the
    win branch only, 189-190); what it proves is the Gen2 rival placement plus
    `terminal_loss` on a non-Nuzlocke wipe."""
    return node_type == "silver" and bool(after.game_over)


def _target_submap_entry(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """`coverage.derive`'s `submap_entry` (route-oracle/coverage.py:209-214):
    the checkpoint where `in_sub_map` goes None -> not-None with a saved parent
    map present."""
    return (
        before.in_sub_map is None
        and after.in_sub_map is not None
        and bool(after.sub_map_return)
        and after.sub_map_return.get("map") is not None
    )


# The submap reward ids whose `doSubMapReward` branch suspends on
# `showSwapScreen` -- `case "fossil"` (bundle.deobfuscated.js:77065-77078) and
# the Distortion legendary cases (77079-77097). Every OTHER reward id resolves
# in place (`team_lvl2`, `rare_candy`, `three_items`, `attack_up`, `transform`,
# `skip`, unrecognized) or routes to the unbridged team-picker modal
# (`sacrifice`, `stat10`, 76845). Only the swap-screen ones can earn
# `pending_submap_reward` / `resolved_submap_reward`, which is why the
# lifecycle target has to name them instead of accepting any visited reward
# node -- an earlier M4 draft did the latter and produced a route whose reward
# resolved in place, silently losing both tags.
_SWAP_SCREEN_REWARD_IDS = frozenset(
    {"fossil"} | set(engine._DISTORTION_LEGEND_REWARD_SPECIES)
)


def _target_nuzlocke_permadeath(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """`coverage.derive`'s `nuzlocke_permadeath` (route-oracle/coverage.py:
    296-305): a WON battle after which the team is strictly smaller, which is
    the only branch `runBattleScreen`'s cull runs on (the win branch opened at
    81278). Since M4 it also implies `state.anyFainted` was set (81371-81372),
    so the same route is the natural place to observe that flag."""
    return (
        len(after.team) < len(before.team)
        and not after.game_over
        and after.phase != engine.Phase.GAME_OVER
        and bool(after.any_fainted)
    )


def _target_submap_boss_loss(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """A submap boss that ENDS the run. The mirror of `submap_boss_win`, and
    the reason a second submap scenario exists at all: it proves entry,
    submap generation/topology and the saved locked parent are all observed on
    a route that never returns, so none of them can depend on
    `returnFromSubMap` having run."""
    return (
        before.in_sub_map is not None
        and node_type == "boss"
        and bool(after.game_over)
    )


def _target_submap_full_lifecycle(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """The COMPLETE special-submap lifecycle in one route: entry, a won submap
    boss, a pending reward, its resolution, the subexit and the exact parent
    return.

    The predicate is the SUBEXIT transition, qualified by what the submap map
    itself records at that instant. `before.map` is still the submap here, so
    its own nodes are the evidence:

    - a `boss` node marked `visited` while the run is still alive can only
      have been WON (a loss ends the run and never reaches a subexit), which
      is `submap_boss_win`;
    - a `reward` node marked `visited` can only have been resolved, and the
      only reward branches the oracle can bridge are `fossil`/legendary, which
      go through `showSwapScreen` -- so it is observed as
      `pending_submap_reward` then `resolved_submap_reward`. The `sacrifice`
      and `stat10` branches route to the unbridged team-picker modal and the
      walk abandons those subtrees, so they cannot satisfy this by accident.

    Reaching the subexit with both visited therefore earns `submap_entry`,
    `submap_boss_win`, `pending_submap_reward`, `resolved_submap_reward`,
    `subexit` and `exact_parent_return` together.
    """
    if node_type != "subexit" or before.in_sub_map is None or after.in_sub_map is not None:
        return False
    sub = before.map
    if sub is None:
        return False
    nodes = sub.nodes.values()
    return (
        any(n.type == "boss" and n.visited for n in nodes)
        and any(
            n.type == "reward"
            and n.visited
            and n.extra.get("reward") in _SWAP_SCREEN_REWARD_IDS
            for n in nodes
        )
    )


def _origin_node_type(before, expected_phase) -> Optional[str]:
    """The TYPE of the node whose visit raised the CURRENTLY pending choice,
    read from `before` (the shallow-copy snapshot the search passes into
    every predicate at a CHOICE step, still holding the PRE-step
    `PendingChoice` object -- see this module's own "Target predicates"
    header). Returns `None` if the pending isn't the expected phase, has no
    `node_id`, or that node can't be found -- so callers can treat any of
    those uniformly as "not a match" rather than raising mid-search."""
    pending = before.pending
    if pending is None or pending.phase is not expected_phase or before.map is None:
        return None
    node_id = (pending.extra or {}).get("node_id")
    node = before.map.nodes.get(node_id) if node_id else None
    return node.type if node is not None else None


def _target_legendary_swap_accept(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4 repair. `_visit_legendary` (engine.py:2366-2421) ALWAYS raises
    `SWAP_CHOICE` on a won battle -- never auto-adding with room, unlike
    ordinary catch/shiny (see that function's own docstring). The predicate
    fires at the CHOICE step that resolves it with a real option picked
    (room-accept or full-team release), identified by walking back to the
    node that raised the pending via `_origin_node_type` -- an ordinary
    catch/shiny/submap-reward swap looks identical in every OTHER respect."""
    return ctx.get("choice_index") is not None and _origin_node_type(before, engine.Phase.SWAP_CHOICE) == "legendary"


def _target_legendary_swap_decline(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """The decline mirror of `_target_legendary_swap_accept`."""
    return ctx.get("choice_index") is None and _origin_node_type(before, engine.Phase.SWAP_CHOICE) == "legendary"


def _legendary_full_swap(before, ctx: dict):
    """The pre-step facts a legendary FULL-TEAM swap exit must show, or None.

    `iu = state.team.length < 6` (79144) is what decides which of
    `showSwapScreen`'s two accept handlers is wired up, and the per-member
    release loop runs only when `!(iu || ip)` (79202) -- so a full-team exit
    requires the team to be at `TEAM_CAP` at the PENDING, read from the
    pre-step snapshot rather than from `before.team` (which the splice has
    already mutated in place).

    M4.3 additionally requires the offer to be REAL rather than merely
    plausible: the raising node's own pre-step map record must be a
    `legendary` node that has not yet been consumed, the pending's option
    summaries must agree in order and identity with the ordered pre-step team
    (which is what the runner projects as the six `swap_release` slots and as
    `pending.context.team`), and the incoming Pokemon the release cards close
    over must exist. `_origin_node_type` alone was not enough: it reads the
    LIVE `before.map`, so it could not distinguish an unconsumed node from one
    already advanced past, and nothing checked the option list at all -- an
    empty one passed.

    M4.4 adds the pre-step `currentNode` identity. `_legendary_full_exit_ok`
    asserts the source's post-condition (`state.currentNode = null`,
    79231/79256), but a null pointer AFTER the step is not evidence an exit
    cleared it unless it named this node BEFORE the step."""
    facts = ctx.get("pre_choice") or {}
    if facts.get("phase") is not engine.Phase.SWAP_CHOICE:
        return None
    pre_team = facts.get("team") or []
    if len(pre_team) != engine.TEAM_CAP or facts.get("incoming") is None:
        return None
    # The ACTUAL raising node, from the pre-step topology -- not from an
    # `origin` string, and not from a post-step `visited` flag.
    node = _node_before(facts, facts.get("node_id"))
    if node is None or node["type"] != "legendary" or node["visited"]:
        return None
    if _origin_node_type(before, engine.Phase.SWAP_CHOICE) != "legendary":
        return None
    if not _options_match_team(facts):
        return None
    # M4.4. The swap screen is up while the legendary node is still the live
    # `currentNode`; all three exits then null it. Without this the post-step
    # `after.current_node_id is None` in `_legendary_full_exit_ok` proves
    # nothing -- a candidate whose pre-step pointer was ALREADY null passes
    # that check without any exit having cleared anything.
    if not _pre_current_node(facts):
        return None
    return facts


def _legendary_full_exit_ok(after, facts: dict, ctx: dict) -> bool:
    """The half both full-team exits share. All three `showSwapScreen` exits
    advance the node exactly once, null `currentNode` (79186/79231/79256),
    null all three resume records (79182-79184 / 79227-79229 / 79252-79254)
    and draw no RNG."""
    return bool(
        after.pending is None
        and after.phase is engine.Phase.ON_MAP
        and _exact_advance(facts, after, facts.get("node_id"))
        and after.current_node_id is None
        and after.saved_catch is None
        and after.saved_question_resolve is None
        and after.saved_shiny_node is None
        and _rng_still(ctx)
    )


def _target_legendary_swap_full_replace(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4.1, hardened in M4.2. The FULL-TEAM release/replace exit of
    `showSwapScreen` (bundle.deobfuscated.js:79202-79246) reached from an
    ordinary `doLegendaryNode` win (80457), which M4's matrix never routed --
    both of its legendary swap screens sat at team size 1, and the only
    full-team replace in the matrix was raised by a CATCH node.

    M4.1 checked only the choice index, the team size and the raising node,
    so it accepted any `after` at all. It now requires the exact transition
    `state.team.splice(B2j, 1, B)` produces (79226): cardinality unchanged,
    every non-selected slot still the SAME OBJECT in the same position, and
    the selected slot holding the very incoming instance the screen offered
    -- not merely a changed slot, and not merely a same-species one."""
    facts = _legendary_full_swap(before, ctx)
    index = ctx.get("choice_index")
    if facts is None or not isinstance(index, int) or isinstance(index, bool):
        return False
    if not 0 <= index < engine.TEAM_CAP:
        return False
    pre_team = facts["team"]
    return bool(
        _legendary_full_exit_ok(after, facts, ctx)
        and len(after.team) == engine.TEAM_CAP
        and all(after.team[k] is pre_team[k] for k in range(engine.TEAM_CAP) if k != index)
        and after.team[index] is facts["incoming"]
    )


def _target_legendary_swap_full_decline(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """The decline mirror of `_target_legendary_swap_full_replace` --
    `#btn-cancel-swap` (79247-79258) with a full team. It consumes the node
    exactly as an accept does, and leaves the team completely untouched."""
    facts = _legendary_full_swap(before, ctx)
    if facts is None or ctx.get("choice_index") is not None:
        return False
    pre_team = facts["team"]
    return bool(
        _legendary_full_exit_ok(after, facts, ctx)
        and len(after.team) == len(pre_team)
        and all(after.team[k] is pre_team[k] for k in range(len(pre_team)))
    )


def _target_shiny_accept_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4.1, hardened in M4.2. `doShinyNode`'s ACCEPT-WITH-ROOM branch
    (80961-80970), as opposed to `_target_shiny_resolved`, which M4 left
    satisfied by a decline.

    M4.1 required only a picked option, room, and the `shiny_node` origin --
    it never looked at `after`, so any resulting state passed. The whole
    room-branch transition is now required: the offered instance itself is
    appended (`state.team.push(B2B)` pushes the very object the card was
    built from, 80965), the earlier team is untouched, the node is consumed
    while `currentNode` is RETAINED (contrast `showSwapScreen`, which clears
    it), `savedShinyNode` is nulled (80962) while `savedQuestionResolve`
    survives, and `recordMonOrigin(B)` (80967) has set `gotViaQuestion` --
    `B` still being the QUESTION node `onNodeClick` dispatched on.

    M4.3 closes what was left. The raiser is now taken from the pre-step map
    -- an actual QUESTION-typed, not-yet-consumed node -- rather than trusted
    from the `origin` string; the offer must be the single card `doShinyNode`
    really builds, selected at index exactly 0; both pre-step resume records
    must pin THIS node under the source's own map-qualified key with the
    offered species; `savedCatch` must be untouched on both sides (no shiny
    path writes it); the node must advance exactly once with correct sibling
    and successor flags; and the resolver must draw no RNG.

    M4.4 closes the last two. The single option summary must be the card
    `doShinyNode` really built from THIS candidate -- counting one option
    proved only that one card existed, not whose -- and the node must have
    been the live `currentNode` before the step, without which the retained
    `after.current_node_id` proves nothing about this branch."""
    facts = ctx.get("pre_choice") or {}
    index = ctx.get("choice_index")
    if facts.get("phase") is not engine.Phase.CATCH_CHOICE or facts.get("origin") != "shiny_node":
        return False
    node_id = facts.get("node_id")
    # The real raising node. `doShinyNode` is reachable ONLY through
    # `onNodeClick`'s `case "shiny"` on a resolved QUESTION node (77318-77332,
    # 77384) -- there is no `NODE_TYPES.SHINY` -- and a node already consumed
    # cannot be the one this choice is resolving.
    node = _node_before(facts, node_id)
    if node is None or node["type"] != "question" or node["visited"]:
        return False
    candidates = facts.get("candidates") or []
    options = facts.get("options") or []
    pre_team = facts.get("team") or []
    # `doShinyNode` offers exactly one candidate as exactly one clickable
    # card (80972-80980), so index 0 is the only affordance that exists.
    # `bool` is excluded explicitly: `True == 1` and `isinstance(True, int)`.
    if len(candidates) != 1 or len(options) != 1:
        return False
    if not isinstance(index, int) or isinstance(index, bool) or index != 0:
        return False
    if len(pre_team) >= engine.TEAM_CAP:
        return False
    offered = candidates[index]
    # M4.4. The card the player clicked must be the card built from THIS
    # candidate. `doShinyNode` builds its single `#shiny-content .poke-card`
    # from the offered instance itself (80972-80980), and `_visit_shiny`
    # mirrors that exactly -- `options=[_mon_summary(mon)]` over the very
    # object it puts in `extra["candidates"]` (engine.py:2528-2533). So the
    # production summary is `engine._mon_summary(offered)` by construction,
    # and the whole dict is compared rather than the species alone: `name`,
    # `level`, `current_hp`, `max_hp`, `status`, `is_shiny` and `held_item`
    # are part of what the card showed. Counting the options (above) only
    # proved there was ONE card; this proves it was the offer's card. Note
    # `facts["options"]` is a deep copy taken pre-step, so this compares the
    # summary as SHOWN, not one rebuilt from a possibly-mutated instance.
    if options[index] != engine._mon_summary(offered):
        return False
    # M4.4. The shiny screen is up while its QUESTION node is still the live
    # `currentNode` (77311); the room branch is the one exit that RETAINS it.
    # Without this the post-step `after.current_node_id == node_id` is
    # satisfied just as well by a pre-step pointer that already named the
    # node with nothing having happened, or by a step that never set it.
    if not _pre_current_node(facts):
        return False
    # Both records are keyed `"m<currentMap>:<nodeId>"` (77319-77326 /
    # 80879-80884), and `savedShinyNode` stores the offered SPECIES (80919).
    key = f"m{facts.get('current_map')}:{node_id}"
    saved_shiny = facts.get("saved_shiny_node")
    saved_q = facts.get("saved_question_resolve")
    resume_pre_ok = (
        isinstance(saved_shiny, dict)
        and saved_shiny.get("key") == key
        and saved_shiny.get("species_id") == getattr(offered, "species_id", None)
        and isinstance(saved_q, dict)
        and saved_q.get("key") == key
        and saved_q.get("resolved_type") == "shiny"
        # No `doShinyNode` path reads or writes `savedCatch`.
        and facts.get("saved_catch") is None
    )
    return bool(
        resume_pre_ok
        and after.pending is None
        and after.phase is engine.Phase.ON_MAP
        and len(after.team) == len(pre_team) + 1
        and all(after.team[k] is pre_team[k] for k in range(len(pre_team)))
        and after.team[-1] is offered
        and _exact_advance(facts, after, node_id)
        # The room branch RETAINS `currentNode`; `showSwapScreen` clears it.
        and after.current_node_id == node_id
        and after.saved_shiny_node is None
        and after.saved_question_resolve == saved_q
        and after.saved_catch is None
        and bool(after.got_via_question)
        and _rng_still(ctx)
    )


def _target_shiny_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4 repair. Fires at the CHOICE step resolving a `doShinyNode` offer --
    identified by `_visit_shiny`'s own `extra["origin"] = "shiny_node"`
    (engine.py:2340-2377), the same marker `run_scenario.py`'s
    `_is_shiny_origin` uses, NOT by node type (a "shiny" node type only
    exists as a resolved QUESTION outcome, bundle.deobfuscated.js:77383-77385,
    so it never appears as `before.map.nodes[...].type` the way "legendary"
    does)."""
    pending = before.pending
    return bool(
        pending is not None
        and pending.phase is engine.Phase.CATCH_CHOICE
        and (pending.extra or {}).get("origin") == "shiny_node"
    )


def _target_move_tutor_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4 repair. Any resolution (accept or decline) of `doMoveTutorNode`'s
    modal (bundle.deobfuscated.js:80464-80563), including the empty-options
    all-mastered case (see `_visit_move_tutor`'s own docstring)."""
    facts = ctx.get("pre_choice") or {}
    return facts.get("phase") is engine.Phase.MOVE_TUTOR_CHOICE


def _target_trade_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4 repair. Any resolution of `doTradeNode`'s ordinary (non-Endless2)
    path (bundle.deobfuscated.js:80580-80638)."""
    facts = ctx.get("pre_choice") or {}
    return facts.get("phase") is engine.Phase.TRADE_CHOICE


def _target_item_equip_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4 repair. Any resolution of `openItemEquipModal` (79442-79570) as
    `doItemNode`'s passive-item branch reaches it -- equip (an index) or the
    real `#btn-equip-to-bag` decline (`None`, see `_resolve_item_equip_choice`
    for why that decline exists in the source at all)."""
    facts = ctx.get("pre_choice") or {}
    return facts.get("phase") is engine.Phase.ITEM_EQUIP_CHOICE


def _target_branching_evolution_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4 repair. `showBranchingChoice` (70560-70613) -- a branching
    evolution (e.g. Eevee) reaching its level threshold. `optional=False`
    (no decline exists), so `ctx["choice_index"]` is always a real index
    here."""
    facts = ctx.get("pre_choice") or {}
    return facts.get("phase") is engine.Phase.EVOLUTION_CHOICE


def _target_sacrifice_reward_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """M4 repair. The `sacrifice` submap reward, resolved through the real
    `showTeamPickerModal` bridge (76845-76884, `case "sacrifice"` at
    77021-77039)."""
    pending = before.pending
    return bool(
        pending is not None
        and pending.phase is engine.Phase.REWARD_TEAM_PICK
        and (pending.extra or {}).get("kind") == "sacrifice"
    )


def _target_stat10_reward_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """The `stat10` mirror of `_target_sacrifice_reward_resolved`
    (`case "stat10"` at bundle.deobfuscated.js:77040-77063)."""
    pending = before.pending
    return bool(
        pending is not None
        and pending.phase is engine.Phase.REWARD_TEAM_PICK
        and (pending.extra or {}).get("kind") == "stat10"
    )


def _target_distortion_entry(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """`_target_submap_entry`, qualified to the Distortion World specifically
    (Gen4-only, `enterSubMap(node, "distortion")`,
    bundle.deobfuscated.js:77374-77376) -- the generic `submap_entry` tag
    never discriminates Underground from Distortion."""
    return (
        before.in_sub_map is None
        and after.in_sub_map == "distortion"
        and bool(after.sub_map_return)
        and after.sub_map_return.get("map") is not None
    )


def _target_distortion_boss_win(before, after, node_type: Optional[str], ctx: dict) -> bool:
    return (
        before.in_sub_map == "distortion"
        and node_type == "boss"
        and not after.game_over
        and after.phase != engine.Phase.GAME_OVER
    )


def _target_distortion_boss_loss(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """The mirror of `_target_submap_boss_loss`, qualified to Distortion."""
    return before.in_sub_map == "distortion" and node_type == "boss" and bool(after.game_over)


def _target_distortion_reward_resolved(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """The Distortion World's guaranteed-legendary reward
    (`_DISTORTION_LEGEND_REWARD_SPECIES`, engine.py:2998-3011, offered ONLY
    on the run's SECOND-EVER Distortion visit -- `_distortion_legendary`,
    map_gen.py:912-927) is raised via the same `_offer_swap_screen`
    SWAP_CHOICE every catch/legendary/fossil swap uses. Discriminated here
    by the resolving node's type ("reward") AND `before.in_sub_map ==
    "distortion"`, mirroring `coverage.py`'s own derivation
    (`in_sub_map == "distortion" and screen == "swap-screen"`). Fires on
    either accept or decline, same as that derivation."""
    return before.in_sub_map == "distortion" and _origin_node_type(before, engine.Phase.SWAP_CHOICE) == "reward"


def _target_distortion_full_lifecycle(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """`_target_submap_full_lifecycle`, qualified to Distortion -- entry, a
    won boss, a pending+resolved reward and the subexit, all inside the
    Distortion World specifically."""
    if node_type != "subexit" or before.in_sub_map != "distortion" or after.in_sub_map is not None:
        return False
    sub = before.map
    if sub is None:
        return False
    nodes = sub.nodes.values()
    return (
        any(n.type == "boss" and n.visited for n in nodes)
        and any(
            n.type == "reward" and n.visited and n.extra.get("reward") in _SWAP_SCREEN_REWARD_IDS
            for n in nodes
        )
    )


def _target_second_map_progress(before, after, node_type: Optional[str], ctx: dict) -> bool:
    """A node resolved on a LATER map with the run still alive. Reaching it at
    all required a boss win (`winning_progression`) and an `advance_map`
    (`map_transition`) on the map before it, so this one predicate carries the
    ordinary-progression spine of a Story route."""
    if after.current_map <= 0 or node_type is None:
        return False
    if after.game_over or after.phase == engine.Phase.GAME_OVER:
        return False
    if after.map is None or not after.current_node_id:
        return False
    node = after.map.nodes.get(after.current_node_id)
    return node is not None and node.visited


TARGETS: dict[str, Callable] = {
    "admin": _target_admin,
    "silver": _target_silver,
    "silver_loss": _target_silver_loss,
    "submap_boss_win": _target_submap_boss_win,
    "submap_boss_loss": _target_submap_boss_loss,
    "submap_entry": _target_submap_entry,
    "submap_full_lifecycle": _target_submap_full_lifecycle,
    "second_map_progress": _target_second_map_progress,
    "swap_release": _target_swap_release,
    "nuzlocke_permadeath": _target_nuzlocke_permadeath,
    "legendary_swap_accept": _target_legendary_swap_accept,
    "legendary_swap_decline": _target_legendary_swap_decline,
    "legendary_swap_full_replace": _target_legendary_swap_full_replace,
    "legendary_swap_full_decline": _target_legendary_swap_full_decline,
    "shiny_resolved": _target_shiny_resolved,
    "shiny_accept_resolved": _target_shiny_accept_resolved,
    "move_tutor_resolved": _target_move_tutor_resolved,
    "trade_resolved": _target_trade_resolved,
    "item_equip_resolved": _target_item_equip_resolved,
    "branching_evolution_resolved": _target_branching_evolution_resolved,
    "sacrifice_reward_resolved": _target_sacrifice_reward_resolved,
    "stat10_reward_resolved": _target_stat10_reward_resolved,
    "distortion_entry": _target_distortion_entry,
    "distortion_boss_win": _target_distortion_boss_win,
    "distortion_boss_loss": _target_distortion_boss_loss,
    "distortion_reward_resolved": _target_distortion_reward_resolved,
    "distortion_full_lifecycle": _target_distortion_full_lifecycle,
}


# Which `coverage.REQUIRED_TAGS` entry a search target must be VERIFIED against
# on the observed stream. Most targets are named after the tag they earn and
# map to themselves; the M4 additions are search predicates over engine state
# that are not themselves tags, so each names the tag its route must actually
# earn. Verification therefore stays a check against `coverage.derive` on a
# real stream -- never against the search's own opinion of what it found.
TARGET_COVERAGE_TAG: dict[str, str] = {
    "admin": "admin",
    "silver": "silver",
    # A silver LOSS earns no `silver` tag by construction (that is the win
    # branch only), so the route is verified on the thing it does prove.
    "silver_loss": "terminal_loss",
    "submap_boss_win": "submap_boss_win",
    # A submap boss LOSS earns no `submap_boss_win`; what the route proves
    # is that entry and the saved locked parent are observed without any
    # return, so it is verified on `submap_entry`.
    "submap_boss_loss": "submap_entry",
    "submap_entry": "submap_entry",
    # The strictest tag in the chain: it can only be earned after entry, a won
    # boss, a resolved reward and a subexit, and it additionally re-compares
    # the whole restored parent topology (`coverage._is_exact_advance`).
    "submap_full_lifecycle": "exact_parent_return",
    "second_map_progress": "map_transition",
    "swap_release": "swap_release",
    "nuzlocke_permadeath": "nuzlocke_permadeath",
    "legendary_swap_accept": "legendary_swap_accept",
    "legendary_swap_decline": "legendary_swap_decline",
    "legendary_swap_full_replace": "legendary_swap_full_replace",
    "legendary_swap_full_decline": "legendary_swap_full_decline",
    "shiny_resolved": "shiny_resolved",
    "shiny_accept_resolved": "shiny_accept_resolved",
    "move_tutor_resolved": "move_tutor_resolved",
    "trade_resolved": "trade_resolved",
    "item_equip_resolved": "item_equip_resolved",
    "branching_evolution_resolved": "branching_evolution_resolved",
    "sacrifice_reward_resolved": "sacrifice_reward_resolved",
    "stat10_reward_resolved": "stat10_reward_resolved",
    "distortion_entry": "distortion_entry",
    "distortion_boss_win": "distortion_boss_win",
    # A Distortion boss LOSS earns no `distortion_boss_win`; mirrors
    # `submap_boss_loss` -> `submap_entry`.
    "distortion_boss_loss": "distortion_entry",
    "distortion_reward_resolved": "distortion_reward_resolved",
    "distortion_full_lifecycle": "distortion_exact_parent_return",
}


def _verify_tag(target: str) -> str:
    return TARGET_COVERAGE_TAG.get(target, target)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _sorted_accessible(state) -> list[str]:
    """Accessible node ids in a total, content-derived order. Sorting by
    `(layer, col, id)` rather than by dict order is what makes the walk
    independent of how `map.nodes` happens to be built."""
    if state.map is None:
        return []
    return [
        node_id
        for _, _, node_id in sorted(
            (n.layer, n.col, n.id) for n in state.map.nodes.values() if n.accessible
        )
    ]


def _choice_indices(state, max_options: int, order: str = "decline-first") -> list[Optional[int]]:
    """Option order at a bridged choice screen.

    `decline-first` (the default, and the order every pre-existing search
    result was derived under): decline first, since it is always legal where
    `optional`, then each option in offer order.

    `accept-first` is the same set in the mirror order. It exists because a
    target that requires the team to GROW (`swap_release` needs six members)
    is unreachable in practice under decline-first: DFS exhausts the entire
    decline-everything subtree before it ever accepts a catch. Both orders are
    total and content-derived, so each is deterministic on its own; the choice
    is recorded in the emitted scenario's provenance.
    """
    pending = state.pending
    if pending is None:
        return []
    decline: list[Optional[int]] = [None] if pending.optional else []
    accept: list[Optional[int]] = list(range(min(len(pending.options), max_options)))
    return decline + accept if order == "decline-first" else accept + decline


def _fresh_engine(seed: int, mode: dict, align: Optional[int], starter_index: int):
    eng = engine.Engine()
    eng.reset(
        nuzlocke_mode=bool(mode["nuzlocke"]), gen2_mode=bool(mode["gen2"]),
        gen3_mode=bool(mode["gen3"]), gen4_mode=bool(mode["gen4"]), seed=int(seed),
    )
    if align is not None:
        # The same symmetric instrument both runners use (SCHEMA.md).
        previous = rng.set_active_stream(eng._rng_stream)
        try:
            rng.seed_rng(int(align) & 0xFFFFFFFF)
        finally:
            rng.set_active_stream(previous)
    state = eng.state
    options = state.pending.options
    if not 0 <= starter_index < len(options):
        return None
    eng.step(engine.ChooseStarter(int(options[starter_index]["species_id"])))
    return eng


def _search_one(eng, target, bounds: dict, stats: dict) -> Iterator[list[dict]]:
    """Depth-first, deterministic, over `(node visit | choice | advance_map)`.

    Branching copies the whole `Engine` (its private RNG stream included), so
    a sibling branch replays from exactly the state its parent was in.

    YIELDS every solution in the search's total order rather than returning
    the first. The caller decides how many to look at: the Python engine is
    only the PROPOSER, and a route it likes can still be one the JavaScript
    source resolves differently (a node that offers a catch on one runtime and
    an item on the other is enough). Yielding lets `search_scenario` reject
    such a candidate and resume the walk exactly where it left off, instead of
    the search having to be right first time.
    """

    def walk(current, actions: list[dict], maps_done: int) -> Iterator[list[dict]]:
        stats["expansions"] += 1
        if stats["expansions"] > bounds["max_expansions"]:
            raise SearchExhausted("max-expansions reached", stats)
        if len(actions) >= bounds["max_depth"]:
            stats["depth_cutoffs"] += 1
            return

        state = current.state
        if state.game_over or state.phase == engine.Phase.GAME_OVER:
            stats["dead_ends"] += 1
            return
        if state.phase == engine.Phase.VICTORY:
            stats["dead_ends"] += 1
            return

        if state.phase == engine.Phase.NEXT_MAP_READY:
            if maps_done >= bounds["max_maps"]:
                stats["map_cutoffs"] += 1
                return
            branch = copy.deepcopy(current)
            branch.step(engine.AdvanceMap())
            yield from walk(branch, actions + [{"kind": "advance_map"}], maps_done + 1)
            return

        if state.phase in bounds.get("bridged_phases", _BRIDGED_CHOICE_PHASES):
            # `.get`, not `[...]`: `choice_order` is an M3.5 addition and
            # programmatic callers built their own `bounds` before it existed.
            # The default is the order every pre-existing result was derived
            # under, so an old caller keeps its old answer.
            for index in _choice_indices(
                state, bounds["max_choice_options"], bounds.get("choice_order", "decline-first")
            ):
                branch = copy.deepcopy(current)
                before = copy.copy(branch.state)
                facts = _pre_choice_facts(branch.state)
                # The RNG stream lives on `Engine`, not `RunState`, so it is
                # sampled HERE rather than snapshotted into `facts` -- the
                # hardened targets need the real stream either side of the
                # step, not a RunState-only proxy for it.
                rng_before = branch._rng_stream.state
                branch.step(engine.SelectOption(index))
                stats["steps"] += 1
                ctx = {
                    "pre_choice": facts, "choice_index": index,
                    "rng_before": rng_before, "rng_after": branch._rng_stream.state,
                }
                step_actions = actions + [{"kind": "choice", "index": index}]
                if target(before, branch.state, None, ctx):
                    yield step_actions
                    continue
                yield from walk(branch, step_actions, maps_done)
            return

        if state.phase != engine.Phase.ON_MAP:
            # An unbridged screen (move tutor, trade, evolution overlay, ...).
            # Not a failure of the search -- a route through it simply cannot
            # be replayed by the oracle, so this branch is abandoned.
            stats["unbridged"] += 1
            return

        for node_id in _sorted_accessible(state):
            node_type = state.map.nodes[node_id].type
            branch = copy.deepcopy(current)
            before = copy.copy(branch.state)
            branch.step(engine.VisitNode(node_id))
            stats["steps"] += 1
            step_actions = actions + [{"kind": "visit", "node": node_id}]
            # A node VISIT legitimately draws RNG (encounters, battles, map
            # rolls), so no target checks `_rng_still` here; the keys are
            # present only so `ctx` has one shape at both call sites.
            if target(before, branch.state, node_type, {
                "pre_choice": {}, "choice_index": None,
                "rng_before": None, "rng_after": None,
            }):
                yield step_actions
                continue
            yield from walk(branch, step_actions, maps_done)

    yield from walk(eng, [], 0)


def _scenario_from(actions, *, name, seed, starter_index, mode, align, target) -> dict:
    scenario = {
        "schema_version": SCHEMA_VERSION,
        "scenario": name,
        "description": (
            f"Derived by route-oracle/search_route.py: bounded deterministic search "
            f"for the `{target}` coverage tag over the real Python engine, verified "
            f"against observed checkpoints."
        ),
        "mode": dict(mode),
        "seed": int(seed),
        "starter_index": int(starter_index),
    }
    if align is not None:
        scenario["align_rng_after_starter_offer"] = int(align)
    scenario["actions"] = [dict(a) for a in actions]
    return scenario


def _verify_python(scenario: dict, target: str) -> tuple[bool, dict, str]:
    """Authoritative check on the Python side: run the scenario through the
    real runner and ask `coverage.derive` whether the tag was earned."""
    out = run_scenario.Runner(copy.deepcopy(scenario)).run()
    if out.get("error"):
        return False, {}, out["error"].strip().splitlines()[-1]
    evidence = coverage_mod.derive(out["checkpoints"])
    tag = _verify_tag(target)
    return tag in evidence, evidence, ""


# Field paths a newly searched scenario is permitted to differ on.
#
# **Empty since M4.** Under M3 this held the six paths the parity signature was
# then frozen on (`rng.draws`, `rng.state`, `map.nodes[i].accessible`,
# `current_node`, `event.battle.status_events[len]`,
# `pending.options[i].instance`), so a searched route could add new RECORDS of
# an already-approved blocker while still being rejected for introducing a new
# difference CLASS.
#
# M4 repaired all six and the gate is now strict zero-difference parity, so
# every one of those entries had to go: keeping them would mean a route search
# could re-admit a route exhibiting a regression in exactly the behavior M4
# repaired, and the tolerance would look like tooling rather than a weakened
# comparator. With the set empty this reads "a candidate route must agree with
# the source on every compared field", which is the same thing `compare.py`
# now demands.
FROZEN_DIFF_PATHS: frozenset = frozenset()


def _cross_runtime_gate(scenario: dict, scenario_path: str, target: str) -> tuple[bool, str]:
    """Does the SOURCE agree with this candidate route?

    Three conditions, all observed rather than assumed:

    1. the JavaScript runner replays the route without error;
    2. both runtimes derive the SAME coverage evidence, and both earn
       `target` -- `compare.py`'s coverage gate requires exactly this;
    3. every field path on which the two streams differ is already a frozen
       blocker path.

    (3) is what keeps a route-oracle repair from quietly becoming a parity
    regression: a candidate whose divergence is structural (a node that
    resolves to a catch on one runtime and an item on the other -- observed,
    and the reason this gate exists) is rejected here rather than discovered
    later by `--audit-frozen`.
    """
    proc = subprocess.run(
        ["node", os.path.join(HERE, "run-scenario.js"), scenario_path],
        capture_output=True, text=True, cwd=REPO,
    )
    if proc.returncode != 0:
        return False, f"js runner exit {proc.returncode}: " \
                      f"{(proc.stderr or proc.stdout).strip().splitlines()[-1]}"
    js = json.loads(proc.stdout)
    if js.get("error"):
        # First line, not last: the last line is the evalmachine stack frame,
        # which is identical for every failure and says nothing.
        return False, f"js runner error: {js['error'].strip().splitlines()[0]}"
    js_evidence = coverage_mod.derive(js["checkpoints"])
    tag = _verify_tag(target)
    if tag not in js_evidence:
        return False, f"js stream does not earn `{tag}` (earned {sorted(js_evidence)})"

    out = run_scenario.Runner(copy.deepcopy(scenario)).run()
    if out.get("error"):
        return False, f"python runner error: {out['error'].strip().splitlines()[-1]}"
    py_evidence = coverage_mod.derive(out["checkpoints"])
    if py_evidence != js_evidence:
        return False, "the two runtimes derived different coverage evidence"

    paths = {p for p, _, _ in cp_mod.field_path_summary(js["checkpoints"], out["checkpoints"])}
    novel = sorted(paths - FROZEN_DIFF_PATHS)
    if novel:
        return False, f"introduces non-frozen difference path(s) {novel}"
    return True, ""


def _verify_js(scenario_path: str, target: str) -> tuple[bool, dict, str]:
    proc = subprocess.run(
        ["node", os.path.join(HERE, "run-scenario.js"), scenario_path],
        capture_output=True, text=True, cwd=REPO,
    )
    if proc.returncode != 0:
        return False, {}, (proc.stderr or proc.stdout).strip().splitlines()[-1]
    out = json.loads(proc.stdout)
    if out.get("error"):
        return False, {}, out["error"].strip().splitlines()[-1]
    evidence = coverage_mod.derive(out["checkpoints"])
    tag = _verify_tag(target)
    return tag in evidence, evidence, ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _inputs_digest(spec: dict) -> str:
    """Every input that can change the answer, and nothing else. A cache entry
    is valid only for the exact bounds it was produced under."""
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_cache(path: str, digest: str) -> Optional[dict]:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        blob = json.load(handle)
    if blob.get("inputs_sha256") != digest:
        return None
    return blob.get("scenario")


def _write_cache(path: str, digest: str, scenario: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"inputs_sha256": digest, "scenario": scenario}, handle,
                  indent=2, sort_keys=True)
        handle.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _mode_from(args) -> dict:
    mode = {"nuzlocke": bool(args.nuzlocke), "gen2": bool(args.gen2),
            "gen3": bool(args.gen3), "gen4": bool(args.gen4)}
    if sum(1 for k in ("gen2", "gen3", "gen4") if mode[k]) > 1:
        raise SystemExit("at most one generation flag may be set")
    return mode


def _parse_seeds(text: str) -> list[int]:
    """Canonicalized: deduplicated and ascending, so the enumeration order --
    and therefore the result -- does not depend on how they were typed."""
    seeds: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo, hi = chunk.rsplit("-", 1)
            seeds.extend(range(int(lo), int(hi) + 1))
        else:
            seeds.append(int(chunk))
    return sorted(set(seeds))


def search_scenario(*, target: str, seeds: list[int], starters: list[int], mode: dict,
                    align: Optional[int], bounds: dict, name: str,
                    gate: Optional[Callable[[dict], tuple[bool, str]]] = None,
                    max_candidates: int = 1, max_candidates_per_root: Optional[int] = None,
                    log=None) -> tuple[Optional[dict], dict]:
    """The search. Returns `(scenario | None, stats)`; raises `SearchExhausted`
    when a bound stops it.

    With no `gate` this returns the first solution in the search's total order
    -- the original behaviour, and what every pre-existing result was derived
    under. With a `gate`, candidates are drawn in that same order and the first
    one the gate accepts is returned; `max_candidates` bounds how many are
    examined, so a gate that rejects everything still terminates cleanly.

    `max_candidates_per_root` additionally moves on to the next seed/starter
    after that many rejections from one root. Sibling solutions under a single
    root share almost all of their prefix, so when a root's rejection is caused
    by that prefix -- a node the two runtimes resolve differently -- every one
    of its thousands of descendants is rejected for the identical reason.
    Sampling roots broadly finds an admissible route; draining one root does
    not.
    """
    predicate = TARGETS[target]
    stats = {"roots": 0, "expansions": 0, "steps": 0, "dead_ends": 0,
             "depth_cutoffs": 0, "map_cutoffs": 0, "unbridged": 0,
             "candidates": 0, "candidates_rejected": 0}
    for seed in seeds:
        for starter_index in starters:
            eng = _fresh_engine(seed, mode, align, starter_index)
            if eng is None:
                continue
            stats["roots"] += 1
            from_this_root = 0
            for actions in _search_one(eng, predicate, bounds, stats):
                scenario = _scenario_from(
                    actions, name=name, seed=seed, starter_index=starter_index,
                    mode=mode, align=align, target=target,
                )
                stats["candidates"] += 1
                if gate is None:
                    return scenario, stats
                accepted, why = gate(scenario)
                if accepted:
                    return scenario, stats
                stats["candidates_rejected"] += 1
                from_this_root += 1
                if log is not None:
                    log(f"# candidate {stats['candidates']} (seed {seed}, starter "
                        f"{starter_index}, {len(actions)} actions) rejected: {why}")
                if stats["candidates"] >= max_candidates:
                    raise SearchExhausted("max-candidates examined without an accepted route", stats)
                if max_candidates_per_root is not None and from_this_root >= max_candidates_per_root:
                    stats["roots_abandoned"] = stats.get("roots_abandoned", 0) + 1
                    break
    return None, stats


def cmd_search(args) -> int:
    mode = _mode_from(args)
    seeds = _parse_seeds(args.seeds)
    starters = sorted(set(range(args.starters + 1))) if args.starters is not None else [0]
    bounds = {
        "max_expansions": args.max_expansions,
        "max_depth": args.max_depth,
        "max_maps": args.max_maps,
        "max_choice_options": args.max_choice_options,
        "choice_order": args.choice_order,
    }
    # A target that can only ever earn its tag inside one specific M4 phase
    # opts the search into walking just that one; every other target keeps
    # the narrow, fast default -- see `_TARGET_EXTRA_PHASE`'s own comment for
    # why this must stay narrowly scoped per target rather than unconditional.
    extra_phase = _TARGET_EXTRA_PHASE.get(args.target)
    if extra_phase is not None:
        bounds["bridged_phases"] = _BRIDGED_CHOICE_PHASES + (extra_phase,)
    spec = {"target": args.target, "seeds": seeds, "starters": starters, "mode": mode,
            "align": args.align, "bounds": bounds, "name": args.name,
            "cross_runtime": bool(args.cross_runtime),
            "max_candidates": args.max_candidates,
            "tool_version": 2}
    digest = _inputs_digest(spec)

    scenario = _read_cache(args.cache, digest) if args.cache else None
    stats: dict = {}
    if scenario is not None:
        print(f"# cache hit ({args.cache}, inputs {digest[:16]}...)", file=sys.stderr)
    else:
        gate = None
        tmp_dir = None
        if args.cross_runtime:
            tmp_dir = tempfile.mkdtemp(prefix="route-search-")

            def gate(candidate: dict, _dir=tmp_dir) -> tuple[bool, str]:
                # The gate needs a real file because run-scenario.js takes a
                # path. It is written into a private temp dir, never into
                # `scenarios/`, and removed with the dir below.
                probe = os.path.join(_dir, "candidate.json")
                with open(probe, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(candidate, handle, indent=2)
                return _cross_runtime_gate(candidate, probe, args.target)

        try:
            scenario, stats = search_scenario(
                target=args.target, seeds=seeds, starters=starters, mode=mode,
                align=args.align, bounds=bounds, name=args.name,
                gate=gate, max_candidates=args.max_candidates,
                max_candidates_per_root=args.max_candidates_per_root,
                log=lambda line: print(line, file=sys.stderr),
            )
        except SearchExhausted as exc:
            print(f"BOUNDED FAILURE: {exc.reason}", file=sys.stderr)
            print(f"  bounds {json.dumps(bounds, sort_keys=True)}", file=sys.stderr)
            print(f"  stats  {json.dumps(exc.stats, sort_keys=True)}", file=sys.stderr)
            return 2
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        if scenario is None:
            print("BOUNDED FAILURE: search space exhausted with no route found",
                  file=sys.stderr)
            print(f"  seeds {len(seeds)} x starters {len(starters)}", file=sys.stderr)
            print(f"  bounds {json.dumps(bounds, sort_keys=True)}", file=sys.stderr)
            print(f"  stats  {json.dumps(stats, sort_keys=True)}", file=sys.stderr)
            return 2
        print(f"# found after {json.dumps(stats, sort_keys=True)}", file=sys.stderr)

    if args.verify:
        earned, evidence, error = _verify_python(scenario, args.target)
        if error:
            print(f"VERIFICATION FAILED: python runner error: {error}", file=sys.stderr)
            return 3
        tag = _verify_tag(args.target)
        if not earned:
            print(f"VERIFICATION FAILED: `{tag}` not earned by the observed "
                  f"python stream; earned {sorted(evidence)}", file=sys.stderr)
            return 3
        print(f"# verified: python stream earns {tag} at {evidence[tag]}",
              file=sys.stderr)

    if args.cache and scenario is not None:
        _write_cache(args.cache, digest, scenario)

    text = json.dumps(scenario, indent=2) + "\n"
    if args.out and args.out != "-":
        target_dir = os.path.abspath(os.path.join(HERE, "scenarios"))
        if os.path.abspath(os.path.dirname(args.out)) == target_dir and not args.allow_fixture_overwrite:
            print(f"refusing to write into {target_dir} without "
                  f"--allow-fixture-overwrite", file=sys.stderr)
            return 1
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(f"# wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_verify(args) -> int:
    with open(args.scenario, encoding="utf-8") as handle:
        scenario = json.load(handle)
    ok = True

    tag = _verify_tag(args.target)

    earned, evidence, error = _verify_python(scenario, args.target)
    if error:
        print(f"python: ERROR {error}")
        ok = False
    else:
        print(f"python: {args.target} {'EARNED at ' + str(evidence[tag]) if earned else 'NOT EARNED'}"
              f" (all: {sorted(evidence)})")
        ok = ok and earned

    if not args.python_only:
        js_earned, js_evidence, js_error = _verify_js(args.scenario, args.target)
        if js_error:
            print(f"js:     ERROR {js_error}")
            ok = False
        else:
            print(f"js:     {args.target} {'EARNED at ' + str(js_evidence[tag]) if js_earned else 'NOT EARNED'}")
            ok = ok and js_earned
            if not js_error and not error and js_evidence != evidence:
                print("MISMATCH: the two runtimes derived different coverage evidence")
                print(f"  js     {json.dumps(js_evidence, sort_keys=True)}")
                print(f"  python {json.dumps(evidence, sort_keys=True)}")
                ok = False

    print("VERIFIED" if ok else "VERIFICATION FAILED")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="search_route.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="bounded deterministic search for a route")
    s.add_argument("--target", required=True, choices=sorted(TARGETS),
                   help="coverage tag the route must earn")
    s.add_argument("--seeds", required=True,
                   help="comma-separated seeds and/or LO-HI ranges; canonicalized ascending")
    s.add_argument("--starters", type=int, default=2,
                   help="highest starter index to try (0..N inclusive); default 2")
    s.add_argument("--align", type=int, default=None,
                   help="align_rng_after_starter_offer value for the emitted scenario")
    s.add_argument("--nuzlocke", action="store_true")
    s.add_argument("--gen2", action="store_true")
    s.add_argument("--gen3", action="store_true")
    s.add_argument("--gen4", action="store_true")
    s.add_argument("--max-maps", type=int, default=2, help="map transitions allowed")
    s.add_argument("--max-depth", type=int, default=48, help="actions per route")
    s.add_argument("--max-expansions", type=int, default=200000,
                   help="hard bound on search-tree expansions")
    s.add_argument("--max-choice-options", type=int, default=3,
                   help="options branched at each choice screen")
    s.add_argument("--choice-order", choices=("decline-first", "accept-first"),
                   default="decline-first",
                   help="deterministic branch order at a choice screen; "
                        "accept-first is required for targets that need the team to grow")
    s.add_argument("--cross-runtime", action="store_true",
                   help="reject any candidate the JavaScript source resolves differently, "
                        "and keep searching (see _cross_runtime_gate)")
    s.add_argument("--max-candidates", type=int, default=200,
                   help="bound on candidates examined when --cross-runtime is set")
    s.add_argument("--max-candidates-per-root", type=int, default=3,
                   help="rejections tolerated from one seed/starter before moving on")
    s.add_argument("--name", default="searched")
    s.add_argument("--out", default="-", help="output path, or - for stdout")
    s.add_argument("--cache", default=None,
                   help="JSON cache bound to the exact inputs by sha256")
    s.add_argument("--allow-fixture-overwrite", action="store_true",
                   help="permit --out inside scenarios/ (never used by validation)")
    s.add_argument("--no-verify", dest="verify", action="store_false",
                   help="skip the observed-stream verification (not recommended)")
    s.set_defaults(verify=True, func=cmd_search)

    v = sub.add_parser("verify", help="re-run a scenario and check a tag is earned")
    v.add_argument("scenario")
    v.add_argument("--target", required=True, choices=sorted(TARGETS))
    v.add_argument("--python-only", action="store_true",
                   help="skip the node runner (fast; proves less)")
    v.set_defaults(func=cmd_verify)
    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
