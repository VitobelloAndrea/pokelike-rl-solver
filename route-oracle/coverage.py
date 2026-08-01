"""Machine-enforced M3 route-coverage derivation.

The M3 tooling gate requires the checked-in route matrix to *demonstrate* a
fixed list of acceptance paths. This module derives that demonstration from
the **observed normalized checkpoint stream** -- never from a scenario's own
`covers` list, a source citation, a planned route, or a unit fixture. A tag is
earned only by checkpoints that actually happened.

Every derivation below is a TRANSITION between consecutive checkpoints (or a
checkpoint plus a later confirming checkpoint), so removing or reordering a
checkpoint changes the evidence indices it reports. `manifest.json` pins those
indices per scenario, which is what makes the gate fail on removal/reorder
rather than merely on absence.

`compare.py --all` enforces this over the **JavaScript** streams -- the real
source is the authority on whether a path was reached. `test_route_oracle.py`
independently enforces it over the **Python** streams, in-process and without
node, so a regression is caught by the ordinary test suite too.
"""

from __future__ import annotations

from typing import Any, Optional

SCHEMA_VERSION = 1

# The M3 acceptance surface. Every one of these must be earned by observed
# checkpoints somewhere in the matrix or the tooling gate fails.
REQUIRED_TAGS = (
    "starter_selection",
    "ordinary_trainer",
    "silver",
    "admin",
    "submap_entry",
    "submap_boss_win",
    "pending_submap_reward",
    "resolved_submap_reward",
    "subexit",
    "exact_parent_return",
    "evolution_or_reward_transition",
    "map_transition",
    "winning_progression",
    "nuzlocke_permadeath",
    "terminal_loss",
)


def _nodes_by_id(cp: dict) -> dict:
    gmap = cp.get("map")
    if not gmap:
        return {}
    return {n["id"]: n for n in gmap.get("nodes", [])}


def _node_type(cp: dict, node_id: Optional[str]) -> Optional[str]:
    if node_id is None:
        return None
    node = _nodes_by_id(cp).get(node_id)
    return node["type"] if node else None


def _visited(cp: dict, node_id: str) -> bool:
    node = _nodes_by_id(cp).get(node_id)
    return bool(node and node["visited"])


def _add(evidence: dict, tag: str, index: int) -> None:
    evidence.setdefault(tag, []).append(index)


def _is_exact_advance(saved: Optional[dict], restored: Optional[dict], left: str) -> bool:
    """Is `restored` exactly `saved` with `advanceFromNode(saved, left)` applied?

    `returnFromSubMap` (bundle.deobfuscated.js:76708-76730) restores the saved
    parent map object and then calls `advanceFromNode(state.map, B.nodeId)`.
    `advanceFromNode` (53639-53655) does exactly three things and nothing else:

      1. `left`.visited = True, `left`.accessible = False;
      2. every node on `left`'s LAYER, other than `left` itself, that was
         accessible becomes inaccessible;
      3. for every edge whose `from` is `left`, the `to` node gets
         revealed = True and accessible = True.

    Everything else -- the node set, every node's id/type/layer/col and its
    gameplay extras, every other node's `visited`, the map index, the
    `is_sub_map` marker, and the complete ORDERED edge list -- must come back
    byte-identical to what was saved. Anything looser also passes for a map
    that was regenerated rather than restored, which is precisely the failure
    this tag exists to be able to see.

    M3.3 rewrote this. The previous version compared only
    `{id, visited, accessible}` per node, so a restored map with a changed node
    TYPE, a changed `revealed` flag, changed extras, a changed map index, a
    swapped EDGE ORDER, or a rewritten edge endpoint still earned the tag --
    all six demonstrated before the change.
    """
    if not saved or not restored:
        return False
    if saved.get("index") != restored.get("index"):
        return False
    if saved.get("is_sub_map") != restored.get("is_sub_map"):
        return False
    # Edges are compared as an ORDERED list: `advanceFromNode` never touches
    # them, so even a reordering means this is not the same map object.
    if [list(e) for e in saved.get("edges", [])] != [list(e) for e in restored.get("edges", [])]:
        return False

    saved_nodes = {n["id"]: n for n in saved.get("nodes", [])}
    restored_nodes = {n["id"]: n for n in restored.get("nodes", [])}
    if set(saved_nodes) != set(restored_nodes) or left not in saved_nodes:
        return False

    layer = saved_nodes[left].get("layer")
    successors = {dst for src, dst in saved.get("edges", []) if src == left}

    for node_id, before in saved_nodes.items():
        after = restored_nodes[node_id]
        # Identity and every gameplay extra are untouched by advanceFromNode.
        if {k: v for k, v in before.items() if k not in ("visited", "accessible", "revealed")} != \
           {k: v for k, v in after.items() if k not in ("visited", "accessible", "revealed")}:
            return False

        want_visited = before["visited"]
        want_accessible = before["accessible"]
        want_revealed = before["revealed"]

        if node_id == left:
            want_visited = True
            want_accessible = False
        else:
            if before.get("layer") == layer and before["accessible"]:
                want_accessible = False
            if node_id in successors:
                want_accessible = True
                want_revealed = True

        if (after["visited"], after["accessible"], after["revealed"]) != (
            want_visited, want_accessible, want_revealed
        ):
            return False
    return True


def derive(checkpoints: list[dict]) -> dict[str, list[int]]:
    """Return `{tag: [checkpoint indices that earned it]}`.

    Indices are the positions in the stream, so any removal or reordering
    shifts them and fails the manifest comparison.
    """
    ev: dict[str, list[int]] = {}
    if not checkpoints:
        return ev

    # -- starter selection ------------------------------------------------
    # The `starter_selected` checkpoint must actually have produced a team of
    # one with a recorded starter species; the kind alone proves nothing.
    for i, cp in enumerate(checkpoints):
        if (
            cp["kind"] == "starter_selected"
            and len(cp["team"]) == 1
            and cp["counters"].get("starter_species_id") is not None
        ):
            _add(ev, "starter_selection", i)

    # -- per-node resolutions ---------------------------------------------
    # A `node_pre` names the node and its type; the matching `node_post` (the
    # next checkpoint of that kind for the same node) proves how it resolved.
    for i, cp in enumerate(checkpoints):
        if cp["kind"] != "node_pre":
            continue
        node_id = cp["event"].get("node")
        node_type = cp["event"].get("node_type")
        post = None
        post_i = None
        for j in range(i + 1, len(checkpoints)):
            nxt = checkpoints[j]
            if nxt["kind"] == "node_post" and nxt["event"].get("node") == node_id:
                post, post_i = nxt, j
                break
            if nxt["kind"] == "node_pre":
                break

        if node_type == "trainer" and post is not None and _visited(post, node_id):
            _add(ev, "ordinary_trainer", post_i)

        if node_type == "silver" and post is not None:
            # `silver_beaten` is incremented on the win branch only.
            if post["counters"].get("silver_beaten", 0) > cp["counters"].get("silver_beaten", 0):
                _add(ev, "silver", post_i)

        if node_type in ("magma", "aqua") and post is not None:
            # `fought_admin` is set at doAdminNode's FIRST statement
            # (bundle.deobfuscated.js:77960), i.e. before the battle -- so it
            # is true even on a loss. A RESOLVED admin is one whose node was
            # advanced past with the run still alive.
            if _visited(post, node_id) and not post["game_over"]:
                _add(ev, "admin", post_i)

        if node_type == "subexit" and post is not None and not post["in_sub_map"]:
            _add(ev, "subexit", post_i)

    # -- submap lifecycle --------------------------------------------------
    entry_topology: Optional[dict] = None
    entry_index: Optional[int] = None
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]

        if prev["in_sub_map"] is None and cp["in_sub_map"] is not None:
            smr = cp.get("sub_map_return") or {}
            if smr.get("has_map"):
                _add(ev, "submap_entry", i)
                entry_topology = smr.get("map_topology")
                entry_index = i

        # Exact parent return -- see `_is_exact_advance` for the full contract.
        if prev["in_sub_map"] is not None and cp["in_sub_map"] is None:
            left = (prev.get("sub_map_return") or {}).get("node_id")
            if entry_topology is not None and entry_index is not None and entry_index < i and left:
                if _is_exact_advance(entry_topology, cp.get("map"), left):
                    _add(ev, "exact_parent_return", i)

        # Submap boss win: a won battle at a submap BOSS node whose node_pre
        # was inside the submap, confirmed by the node being visited after.
        if cp["kind"] == "battle" and cp["in_sub_map"] is not None:
            battle = cp["event"].get("battle") or {}
            node_id = cp["event"].get("node")
            if battle.get("player_won") and _node_type(cp, node_id) == "boss":
                for j in range(i + 1, len(checkpoints)):
                    if checkpoints[j]["kind"] == "node_post" and checkpoints[j]["event"].get("node") == node_id:
                        if _visited(checkpoints[j], node_id):
                            _add(ev, "submap_boss_win", j)
                        break

        # Pending submap reward: parked on a real choice screen, raised BY a
        # reward node, with the reward node not yet consumed.
        if (
            cp["in_sub_map"] is not None
            and cp["pending"] is not None
            and cp["screen"] == "swap-screen"
            and prev["kind"] == "node_pre"
            and prev["event"].get("node_type") == "reward"
            and not _visited(cp, prev["event"]["node"])
        ):
            _add(ev, "pending_submap_reward", i)

    # Resolved submap reward: the choice_post that consumes a reward node
    # whose pending state was observed above.
    pending_at = ev.get("pending_submap_reward", [])
    for p in pending_at:
        reward_node = checkpoints[p - 1]["event"]["node"]
        for j in range(p + 1, len(checkpoints)):
            cp = checkpoints[j]
            if cp["kind"] == "choice_post" and cp["pending"] is None and _visited(cp, reward_node):
                _add(ev, "resolved_submap_reward", j)
                break

    # -- evolution or reward transition ------------------------------------
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]
        # Evolution: an existing slot changes species while keeping its slot.
        for slot, mon in enumerate(cp["team"]):
            if slot < len(prev["team"]):
                before = prev["team"][slot]
                if (
                    before["species_id"] != mon["species_id"]
                    and before["level"] <= mon["level"]
                    and cp["screen"] != "swap-screen"
                    and prev["screen"] != "swap-screen"
                ):
                    _add(ev, "evolution_or_reward_transition", i)
                    break
        else:
            # Reward: the bag grew.
            if len(cp["items"]) > len(prev["items"]):
                _add(ev, "evolution_or_reward_transition", i)

    # -- map transition + winning progression ------------------------------
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]
        if cp["kind"] == "map_transition_post" and cp["current_map"] > prev["current_map"]:
            _add(ev, "map_transition", i)
        # Winning progression: a badge earned (the source's own boss-win
        # counter) -- not merely arriving on a new map.
        if cp["counters"]["badges"] > prev["counters"]["badges"]:
            _add(ev, "winning_progression", i)

    # -- Nuzlocke permadeath -----------------------------------------------
    # The fainted-cull and held-item release run on the WIN branch only
    # (bundle.deobfuscated.js:81358-81380, inside the win branch opened at
    # 81278; the loss `else` at 81388-81430 never touches state.team). So a
    # real permadeath is: a WON battle, at least one player combatant on 0 HP
    # in that battle's own final state, and a team that is shorter afterwards.
    # Reading the pre-battle checkpoint's HP would never see it -- the member
    # faints and is culled entirely inside the battle.
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]
        if not cp["mode"]["nuzlocke"] or cp["kind"] != "battle":
            continue
        battle = cp["event"].get("battle") or {}
        if not battle.get("player_won"):
            continue
        fainted = [m for m in battle.get("player_team", []) if m and m["current_hp"] == 0]
        if fainted and len(cp["team"]) < len(prev["team"]):
            _add(ev, "nuzlocke_permadeath", i)

    # -- terminal loss ------------------------------------------------------
    for i, cp in enumerate(checkpoints):
        if cp["kind"] == "terminal" and cp["event"].get("game_over"):
            _add(ev, "terminal_loss", i)

    return ev


def merge(per_scenario: dict[str, dict[str, list[int]]]) -> set[str]:
    """Union of tags earned across the whole matrix."""
    earned: set[str] = set()
    for tags in per_scenario.values():
        earned |= set(tags)
    return earned


def missing(per_scenario: dict[str, dict[str, list[int]]]) -> list[str]:
    earned = merge(per_scenario)
    return [tag for tag in REQUIRED_TAGS if tag not in earned]
