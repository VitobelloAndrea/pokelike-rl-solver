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

SCHEMA_VERSION = 2

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
    "swap_release",
    # -- M4 repair (docs/prompts/M4-repair.md item 3): the families the
    # M4-independent-closure-audit found reachable but unenforced --
    # ordinary LEGENDARY, SHINY, MOVE_TUTOR, TRADE, the Distortion submap,
    # the `sacrifice`/`stat10` team-picker rewards, and the item-equip /
    # branching-evolution overlays. Every one below is earned the same way
    # as everything above: a real TRANSITION in the observed stream, never a
    # scenario name or a search predicate's own opinion.
    "legendary_swap_accept",
    "legendary_swap_decline",
    "shiny_resolved",
    "move_tutor_resolved",
    "trade_resolved",
    "item_equip_resolved",
    "branching_evolution_resolved",
    "sacrifice_reward_resolved",
    "stat10_reward_resolved",
    "distortion_entry",
    "distortion_boss_win",
    "distortion_boss_loss",
    "distortion_reward_resolved",
    "distortion_subexit",
    "distortion_exact_parent_return",
    "distortion_continued_progress",
    # -- M4.1 (docs/prompts/M4.1-implementation.md): branch-specific tags for
    # the three lifecycle exits the M4 repair left certified only by a
    # SIBLING branch. `shiny_resolved` above is now tightened to the DECLINE
    # only, so an acceptance can no longer inherit a decline's credit, and
    # each legendary full-team exit is separated from its room-sized
    # namesake by the observed team cardinality at the pending -- which is
    # exactly the `state.team.length < 6` test (79144) the source itself uses
    # to pick between `showSwapScreen`'s two accept handlers.
    "shiny_accept_resolved",
    "legendary_swap_full_replace",
    "legendary_swap_full_decline",
    # -- M4.2 (docs/prompts/M4.2-implementation.md): the ordinary-catch
    # CONTROL for the branch-specific resume-record clearing. `doShinyNode`'s
    # room accept clears `savedShinyNode` and RETAINS `savedQuestionResolve`
    # (80962); `catchPokemon`'s clears `savedCatch` AND `savedQuestionResolve`
    # and never touches `savedShinyNode` (79041-79042). Without a tag that
    # pins the catch half, a port that simply cleared everything everywhere
    # would satisfy the shiny tags and no other compared field would notice.
    "catch_room_accept_resume_cleared",
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


# ---------------------------------------------------------------------------
# M4.2 shared transition predicates
#
# Every helper below reads ONLY observed checkpoints. They exist because the
# M4.1 branch tags were satisfiable by malformed transitions: a phase/role
# alone is not proof of the raising family, and a team transition alone is not
# proof that the node resolved. Each of the four branch tags now has to show
# the whole transition -- who raised it, what was offered, what the resume
# guards did, and that the node really advanced with RNG untouched.
# ---------------------------------------------------------------------------


def _raising_node_pre(checkpoints: list[dict], index: int) -> Optional[dict]:
    """The `node_pre` whose visit raised the choice resolved at `index`.

    Walks back to the nearest `node_pre`. A `choice_post` encountered first
    means an EARLIER choice on the same node already resolved and this one is
    a CASCADE (a catch/shiny offer whose acceptance opened a swap screen), so
    the raising node is deliberately reported as unknown rather than
    mis-attributed to the node_pre further back. That is what stops a
    catch-raised full-team swap from being read as a legendary one."""
    for k in range(index - 1, -1, -1):
        kind = checkpoints[k]["kind"]
        if kind == "node_pre":
            return checkpoints[k]
        if kind == "choice_post":
            return None
    return None


def _raised_by(checkpoints: list[dict], index: int, node_type: str) -> Optional[str]:
    """The id of the node that raised the choice at `index`, but only when it
    really is of `node_type` -- asserted BOTH from the `node_pre` event and
    from that checkpoint's own map, so tampering with either one alone drops
    the tag. Returns None when the family does not match."""
    pre = _raising_node_pre(checkpoints, index)
    if pre is None:
        return None
    node_id = pre["event"].get("node")
    if not node_id or pre["event"].get("node_type") != node_type:
        return None
    if _node_type(pre, node_id) != node_type:
        return None
    return node_id


def _resume(cp: dict) -> Optional[dict]:
    rs = cp.get("resume_state")
    return rs if isinstance(rs, dict) else None


def _node_key(cp: dict, node_id: str) -> str:
    """`savedQuestionResolve`/`savedShinyNode`'s guard key -- the source's own
    `"m" + currentMap + ":" + node.id` (77319-77326 / 80879-80884). The
    Endless variant is out of scope, exactly as in `_resolve_question`."""
    return f"m{int(cp['current_map'])}:{node_id}"


def _without_slot(mon: Optional[dict]) -> Optional[dict]:
    """A team entry minus the positional `slot` the team projection adds and
    an option's `instance` projection does not. Everything else must match:
    this is identity comparison, not a species check."""
    if not isinstance(mon, dict):
        return None
    return {k: v for k, v in mon.items() if k != "slot"}


def _option_index(value: Any, count: int) -> Optional[int]:
    """`event.index` as a REAL position in an offer of `count` options, or
    None when it is not one (M4.3).

    Every accept branch below used to test `index is not None` and then read
    the offer with it -- or, worse, only checked that the installed member
    appeared SOMEWHERE in the offer. `999`, `-1`, `True` and `"0"` are all
    non-null, so all four could still certify a branch whose card the player
    could not have clicked: the source builds exactly one clickable card per
    candidate (`doCatchNode` 78776-78789, one `data-shortcut` per index; the
    single `#shiny-content .poke-card` at 80972) and each card's listener
    closes over THAT candidate, so an out-of-range or non-integer index does
    not name any affordance that exists.

    `bool` is rejected explicitly because `isinstance(True, int)` is True in
    Python and `True == 1` -- so an unguarded range test would silently read
    `True` as option 1. Strings are never coerced: `"0"` is a malformed
    stream, not a zeroth option.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value < count:
        return None
    return value


def _resolved_node(pre: dict, post: dict, node_id: str) -> bool:
    """Did the node really resolve across this choice, and nothing else?

    Requires the map screen back, the node unvisited before and visited
    after, and `post.map` to be EXACTLY `advanceFromNode(pre.map, node_id)`
    -- which covers the node's own visited/accessible flags, its same-layer
    siblings being locked, and its successors being revealed/accessible, with
    every other node, the map index and the ordered edge list untouched.
    `_is_exact_advance` is reused verbatim; it is the same contract the
    submap parent-return tags already hold themselves to."""
    return (
        post.get("screen") == "map-screen"
        and not _visited(pre, node_id)
        and _visited(post, node_id)
        and _is_exact_advance(pre.get("map"), post.get("map"), node_id)
    )


def _rng_unchanged(pre: dict, post: dict) -> bool:
    """No draw and no state movement across a click handler. Every branch
    hardened here is pure bookkeeping in the source -- none of the accept,
    replace or decline handlers calls `rng()` -- so a handler that rolled
    anything is a divergence even if the visible outcome matched."""
    return pre.get("rng") == post.get("rng") and pre.get("rng") is not None


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

    # -- ordinary LEGENDARY swap lifecycle (M4 repair) ----------------------
    # `_visit_legendary` (engine.py:2366-2421) ALWAYS presents `showSwapScreen`
    # on a won battle, never auto-adding even with room -- a different
    # lifecycle from the ordinary catch/shiny auto-add (see that function's
    # own docstring). The pending is raised by a node_pre whose type is
    # "legendary"; unlike admin/trainer/silver, the node is NOT yet `visited`
    # at the immediately-following node_post (the swap choice is still
    # pending), so the resolution has to be found at the LATER `choice_post`
    # that actually clears `pending`, exactly like the submap reward pattern
    # above -- not at node_post.
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]
        if (
            cp["in_sub_map"] is None
            and cp["pending"] is not None
            and cp["screen"] == "swap-screen"
            and prev["kind"] == "node_pre"
            and prev["event"].get("node_type") == "legendary"
        ):
            for j in range(i + 1, len(checkpoints)):
                nxt = checkpoints[j]
                if nxt["kind"] == "choice_post" and nxt["pending"] is None:
                    if nxt["event"].get("index") is None:
                        _add(ev, "legendary_swap_decline", j)
                    else:
                        _add(ev, "legendary_swap_accept", j)
                    break

    # -- ordinary LEGENDARY full-team replace / decline (M4.2) --------------
    # Split out of the block above and anchored on the `choice_pre`/
    # `choice_post` PAIR that actually resolves the screen, so `before` and
    # `after` are both really observed. M4.1 derived these from the pending
    # checkpoint plus a forward scan, which meant a missing
    # `pending.context.incoming`, a corrupted `context.team` order, or an
    # arbitrary WRONG Pokemon installed in the selected slot all still earned
    # the tag -- the transition was never checked, only its cardinality.
    for j, cp in enumerate(checkpoints):
        if cp["kind"] != "choice_post" or cp["pending"] is not None:
            continue
        pre = checkpoints[j - 1] if j else None
        if pre is None or pre["kind"] != "choice_pre" or pre.get("screen") != "swap-screen":
            continue
        # The raiser must be an ordinary LEGENDARY node. `doLegendaryNode`'s
        # win callback ends in a bare `showSwapScreen(B2P, B)` with NO room
        # test (80457) -- that missing test is the whole difference between
        # this lifecycle and catch/shiny's room-based auto-add. A
        # catch-raised full-team swap is a CASCADE and `_raising_node_pre`
        # reports no raiser for it at all.
        node_id = _raised_by(checkpoints, j, "legendary")
        if node_id is None or cp["in_sub_map"] is not None:
            continue
        pending = pre.get("pending") or {}
        options = pending.get("options") or []
        context = pending.get("context") or {}
        team = pre.get("team") or []
        after_team = cp.get("team") or []
        incoming = (context.get("incoming") or {}).get("instance")
        bare_team = [_without_slot(m) for m in team]
        # `iu = state.team.length < 6` (79144) picks which accept handler is
        # wired up, and the per-member release loop runs only when
        # `!(iu || ip)` (79202): one card per `state.team[i]`, in team order.
        # Both observable halves of that test are required, and each release
        # option must be the identity of the team member it releases -- a
        # slot list alone says nothing about what is in those slots.
        full_team = (
            len(team) == 6
            and len(options) == 6
            and all(o.get("role") == "swap_release" for o in options)
            and [o.get("slot") for o in options] == list(range(6))
            and [o.get("instance") for o in options] == bare_team
            # `showSwapScreen`'s incoming Pokemon lives only in the closure
            # its listeners capture, so an absent one means the offer was
            # never really observed and nothing below can be verified.
            and isinstance(incoming, dict)
            and [o.get("slot") for o in context.get("team") or []] == list(range(6))
            and [o.get("instance") for o in context.get("team") or []] == bare_team
        )
        # All three exits advance the node, clear `currentNode` (79186 /
        # 79231 / 79256) and null all three resume records
        # (79182-79184 / 79227-79229 / 79252-79254). None of them draws RNG.
        exit_ok = (
            full_team
            and cp.get("current_node") is None
            and _resolved_node(pre, cp, node_id)
            and _rng_unchanged(pre, cp)
            and _resume(cp) == {
                "saved_question_resolve": None,
                "saved_catch": None,
                "saved_shiny_node": None,
            }
        )
        if not exit_ok:
            continue
        idx = cp["event"].get("index")
        if idx is None:
            # `#btn-cancel-swap` (79247-79258): the node is consumed exactly
            # as an accept consumes it, and the team is byte-identical.
            if after_team == team:
                _add(ev, "legendary_swap_full_decline", j)
        elif isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < 6:
            # `state.team.splice(B2j, 1, B)` (79226): cardinality unchanged,
            # every other slot identical AND still in order, and the selected
            # slot holding EXACTLY the pending incoming instance -- not merely
            # "some slot changed", which an arbitrary wrong Pokemon satisfies.
            if (
                len(after_team) == 6
                and all(after_team[k] == team[k] for k in range(6) if k != idx)
                and _without_slot(after_team[idx]) == incoming
            ):
                _add(ev, "legendary_swap_full_replace", j)

    # -- resolved pending-choice families bridged by M4 ---------------------
    # `doShinyNode`/`doMoveTutorNode`/`doTradeNode`/`openItemEquipModal`/
    # `showBranchingChoice` each earn their tag at the `choice_post` that
    # follows a `choice_pre` parked on the matching `pending.phase` -- the
    # phase identity is compared cross-runtime already (SCHEMA.md), so this
    # is a real observed transition, not a name lookup. `phase` is read off
    # the PRE-resolution checkpoint, since `choice_post` may already show a
    # DIFFERENT pending (e.g. shiny/item-equip cascading into a full-team
    # swap) or none at all.
    # M4.1: `shiny_choice` is deliberately NOT in this table any more. It used
    # to earn `shiny_resolved` for ANY resolution, which meant five declines
    # certified a branch none of them took -- and deleting the room auto-add
    # from `_try_add_to_team` left the whole shiny route at zero difference.
    # The two exits are now derived separately, below, from the resolving
    # checkpoint's own `event.index` plus the observed team transition.
    _PENDING_PHASE_TAG = {
        "move_tutor_choice": "move_tutor_resolved",
        "trade_choice": "trade_resolved",
        "item_equip_choice": "item_equip_resolved",
        "evolution_choice": "branching_evolution_resolved",
    }
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]
        if cp["kind"] != "choice_post" or prev["kind"] != "choice_pre":
            continue
        pre_pending = prev.get("pending") or {}
        tag = _PENDING_PHASE_TAG.get(pre_pending.get("phase"))
        if tag is not None:
            _add(ev, tag, i)
        # -- SHINY: the two exits of `doShinyNode`, separated (M4.1) --------
        # Both are read off the same observed pending->resolved transition,
        # never off a scenario name. The offer itself is proved by the
        # PRE-resolution checkpoint really being parked on a `shiny_choice`
        # with a single `shiny` option, so an ordinary question-resolved
        # catch (same `CATCH_CHOICE` phase in the port) cannot earn either.
        if pre_pending.get("phase") == "shiny_choice":
            # M4.2 hardening. Before this, both shiny tags were satisfiable by
            # a malformed transition: the raising node family was never
            # checked (a `catch`-typed raiser still earned), a decline that
            # left a NEW pending and stayed on the swap screen still earned,
            # and a null/corrupt offered instance still earned the decline.
            # The whole transition is now required.
            #
            # There is no `NODE_TYPES.SHINY`. A shiny encounter exists ONLY as
            # a QUESTION resolution: `onNodeClick` replaces `iu` with
            # `resolveQuestionMark()` (77318-77332) and dispatches `case
            # "shiny"` (77384) without rebinding the node, so the raiser is
            # always a QUESTION node -- which is also why `recordMonOrigin(B)`
            # at 80967 sets `gotViaQuestion`.
            node_id = _raised_by(checkpoints, i, "question")
            pre_options = pre_pending.get("options") or []
            pre_team = prev.get("team") or []
            post_team = cp.get("team") or []
            pre_rs, post_rs = _resume(prev), _resume(cp)
            offered = pre_options[0].get("instance") if len(pre_options) == 1 else None
            shiny_offer = (
                node_id is not None
                and len(pre_options) == 1
                and pre_options[0].get("role") == "shiny"
                # A card was really built over a real instance. `doShinyNode`
                # returns early without raising the screen when it has no
                # candidate (80925-80928), so a null instance here is a
                # malformed offer, never a legitimate one.
                and isinstance(offered, dict)
                # Node identity is stable across the whole boundary. Both
                # shiny exits RETAIN `currentNode` (contrast `showSwapScreen`,
                # which clears it on all three of its exits).
                and prev.get("current_node") == node_id
                and cp.get("current_node") == node_id
                and _resolved_node(prev, cp, node_id)
                and _rng_unchanged(prev, cp)
            )
            # `savedShinyNode` pins the offer and `savedQuestionResolve` pins
            # the QUESTION resolution that produced it, both under the same
            # map-qualified key. Requiring the species to agree with the card
            # is what makes this the offer's own record rather than a
            # leftover from some other node.
            key = _node_key(prev, node_id) if node_id else None
            saved_shiny = (pre_rs or {}).get("saved_shiny_node")
            saved_q = (pre_rs or {}).get("saved_question_resolve")
            resume_pre_ok = (
                pre_rs is not None
                and isinstance(saved_shiny, dict)
                and saved_shiny.get("key") == key
                and isinstance(offered, dict)
                and saved_shiny.get("species_id") == offered.get("species_id")
                and isinstance(saved_q, dict)
                and saved_q.get("key") == key
                and saved_q.get("resolved_type") == "shiny"
                # `doShinyNode` never touches `savedCatch` on any path.
                and (pre_rs or {}).get("saved_catch") is None
            )
            # BOTH shiny exits null `savedShinyNode` (the room accept at
            # 80962, `#btn-skip-shiny` at 80986) and NEITHER touches
            # `savedQuestionResolve` -- it survives the resolution byte for
            # byte. That retention is the exact behavioural difference from
            # `catchPokemon`'s room accept, which nulls it (79042), and is
            # why the `catch_room_accept_resume_cleared` control exists.
            resume_post_ok = (
                post_rs is not None
                and post_rs.get("saved_shiny_node") is None
                and post_rs.get("saved_catch") is None
                and post_rs.get("saved_question_resolve") == saved_q
            )
            if shiny_offer and resume_pre_ok and resume_post_ok:
                raw_index = cp["event"].get("index")
                # M4.3. The two exits are told apart by `event.index`, and a
                # value that is neither `None` nor the ONE real card must earn
                # NEITHER. `doShinyNode` builds exactly one clickable card --
                # `#shiny-content .poke-card` (80972-80980), rebound to
                # `#btn-take-shiny` -- so the only accept index that exists is
                # 0. Previously any non-null value took the accept branch, so
                # `999`/`-1`/`True`/`"0"` all still certified the room
                # auto-add. `_option_index` is bounded by the OFFER, which
                # `shiny_offer` has already pinned at exactly one option.
                picked = _option_index(raw_index, len(pre_options))
                if raw_index is None:
                    # `#btn-skip-shiny` (80984-80989): advances the node, team
                    # byte-identical, nothing left pending. This is the ONLY
                    # way to earn `shiny_resolved`.
                    if post_team == pre_team and cp["pending"] is None:
                        _add(ev, "shiny_resolved", i)
                elif picked == 0:
                    # The room auto-add (80961-80970): `team.push` of exactly
                    # the offered instance, with room available beforehand. A
                    # full-team shiny instead falls through to
                    # `showSwapScreen` (80970) and leaves a pending behind, so
                    # it earns neither tag. The installed member is compared
                    # against `options[picked]` specifically, so the tag is
                    # bound to the card that was actually selected.
                    installed = post_team[-1] if post_team else None
                    if (
                        len(pre_team) < 6
                        and cp["pending"] is None
                        and len(post_team) == len(pre_team) + 1
                        and post_team[:-1] == pre_team
                        and _without_slot(installed) == pre_options[picked].get("instance")
                    ):
                        _add(ev, "shiny_accept_resolved", i)
        if pre_pending.get("phase") == "catch_choice":
            # -- M4.2 CONTROL: `catchPokemon`'s room accept (79026-79045) ----
            # The counterpart of the shiny accept above, and the reason both
            # can be told apart at all. Same shape of proof -- real raiser,
            # real offer, real advance, RNG untouched -- but the OPPOSITE
            # resume contract: `savedCatch` and `savedQuestionResolve` are
            # BOTH nulled (79041-79042) and `savedShinyNode` is left exactly
            # as it was. A port that cleared all three here, or none, would
            # change no other compared field.
            node_id = _raised_by(checkpoints, i, "catch") or _raised_by(checkpoints, i, "question")
            pre_options = pre_pending.get("options") or []
            pre_team = prev.get("team") or []
            post_team = cp.get("team") or []
            pre_rs, post_rs = _resume(prev), _resume(cp)
            saved_catch = (pre_rs or {}).get("saved_catch")
            offered = [o.get("instance") for o in pre_options]
            # M4.3. `doCatchNode` builds one clickable card per candidate, and
            # card `BcJ` closes over candidate `Bcg`: its listener is literally
            # `() => catchPokemon(Bcg, B)` (78776-78789). So the selected index
            # does not merely have to BE something -- the member that ends up
            # on the team has to be `options[index]` specifically. Before this
            # the tag required only a non-null index plus "the appended member
            # occurs somewhere in the offered list", which any of the other
            # cards satisfies just as well, so the tag never proved that the
            # clicked card caused the installation.
            picked = _option_index(cp["event"].get("index"), len(pre_options))
            if (
                node_id is not None
                and picked is not None
                and pre_options
                and all(o.get("role") == "catch" for o in pre_options)
                and all(isinstance(o, dict) for o in offered)
                and prev.get("current_node") == node_id
                # `catchPokemon`'s room branch deliberately does NOT clear
                # `currentNode` (contrast `showSwapScreen`, 79186/79231/79256).
                and cp.get("current_node") == node_id
                and _resolved_node(prev, cp, node_id)
                and _rng_unchanged(prev, cp)
                # Room accept: exactly one member appended, the earlier team
                # untouched, and the appended member is EXACTLY the instance
                # behind the selected card -- `state.team.push(B)` pushes the
                # very object that card closed over (79036).
                and len(pre_team) < 6
                and cp["pending"] is None
                and len(post_team) == len(pre_team) + 1
                and post_team[:-1] == pre_team
                and _without_slot(post_team[-1]) == offered[picked]
                # The pinned offer really is this node's, in the source's own
                # order. `savedCatch` is keyed by the BARE node id (78441),
                # not the map-qualified key the other two records use.
                and isinstance(saved_catch, dict)
                and saved_catch.get("key") == node_id
                and [o.get("instance") for o in saved_catch.get("instances") or []] == offered
                # A stale question resolution really was pending, and is gone.
                and isinstance((pre_rs or {}).get("saved_question_resolve"), dict)
                and post_rs is not None
                and post_rs.get("saved_catch") is None
                and post_rs.get("saved_question_resolve") is None
                # ... while `savedShinyNode` is passed through untouched.
                and post_rs.get("saved_shiny_node") == (pre_rs or {}).get("saved_shiny_node")
            ):
                _add(ev, "catch_room_accept_resume_cleared", i)
        if pre_pending.get("phase") == "reward_team_pick":
            # `sacrifice`/`stat10` share one phase; which reward this is
            # comes from the REWARD NODE's own `extra.reward.kind`, read off
            # the most recent `node_pre` that raised a "reward" node -- the
            # same node the picker itself is currently resolving.
            reward_kind = None
            for k in range(i - 1, -1, -1):
                if checkpoints[k]["kind"] == "node_pre" and checkpoints[k]["event"].get("node_type") == "reward":
                    reward_node_id = checkpoints[k]["event"].get("node")
                    node = _nodes_by_id(checkpoints[k]).get(reward_node_id) or {}
                    reward_kind = (node.get("reward") or {}).get("kind")
                    break
            if reward_kind == "sacrifice":
                _add(ev, "sacrifice_reward_resolved", i)
            elif reward_kind == "stat10":
                _add(ev, "stat10_reward_resolved", i)

    # -- Distortion-specific submap lifecycle (M4 repair) --------------------
    # Mirrors the generic submap-lifecycle block above exactly, but qualified
    # on `in_sub_map == "distortion"` specifically: the generic tags never
    # discriminate Underground from Distortion (both are just "not None"),
    # so a matrix that only ever entered Underground could otherwise pass
    # them without Distortion ever having been observed.
    distortion_entry_topology: Optional[dict] = None
    distortion_entry_index: Optional[int] = None
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]

        if prev["in_sub_map"] is None and cp["in_sub_map"] == "distortion":
            smr = cp.get("sub_map_return") or {}
            if smr.get("has_map"):
                _add(ev, "distortion_entry", i)
                distortion_entry_topology = smr.get("map_topology")
                distortion_entry_index = i

        if prev["in_sub_map"] == "distortion" and cp["in_sub_map"] is None:
            left = (prev.get("sub_map_return") or {}).get("node_id")
            if (
                distortion_entry_topology is not None
                and distortion_entry_index is not None
                and distortion_entry_index < i
                and left
                and _is_exact_advance(distortion_entry_topology, cp.get("map"), left)
            ):
                _add(ev, "distortion_exact_parent_return", i)

        if cp["kind"] == "battle" and cp["in_sub_map"] == "distortion":
            battle = cp["event"].get("battle") or {}
            node_id = cp["event"].get("node")
            if _node_type(cp, node_id) == "boss":
                for j in range(i + 1, len(checkpoints)):
                    nxt = checkpoints[j]
                    if nxt["kind"] != "node_post" or nxt["event"].get("node") != node_id:
                        continue
                    if battle.get("player_won") and _visited(nxt, node_id):
                        _add(ev, "distortion_boss_win", j)
                    elif not battle.get("player_won") and nxt["game_over"]:
                        _add(ev, "distortion_boss_loss", j)
                    break

    # Distortion subexit: the same per-node "subexit" resolution as the
    # generic tag above, qualified on having been raised FROM a Distortion
    # submap specifically.
    for i, cp in enumerate(checkpoints):
        if cp["kind"] != "node_pre" or cp["event"].get("node_type") != "subexit" or cp["in_sub_map"] != "distortion":
            continue
        for j in range(i + 1, len(checkpoints)):
            nxt = checkpoints[j]
            if nxt["kind"] == "node_post" and nxt["event"].get("node") == cp["event"].get("node"):
                if not nxt["in_sub_map"]:
                    _add(ev, "distortion_subexit", j)
                break
            if nxt["kind"] == "node_pre":
                break

    # Distortion reward resolved: the same choice_post pattern as the generic
    # `resolved_submap_reward` tag, qualified on the submap being Distortion.
    for i in range(1, len(checkpoints)):
        prev, cp = checkpoints[i - 1], checkpoints[i]
        if (
            cp["in_sub_map"] == "distortion"
            and cp["pending"] is not None
            and cp["screen"] == "swap-screen"
            and prev["kind"] == "node_pre"
            and prev["event"].get("node_type") == "reward"
            and not _visited(cp, prev["event"]["node"])
        ):
            reward_node = prev["event"]["node"]
            for j in range(i + 1, len(checkpoints)):
                nxt = checkpoints[j]
                if nxt["kind"] == "choice_post" and nxt["pending"] is None and _visited(nxt, reward_node):
                    _add(ev, "distortion_reward_resolved", j)
                    break

    # Continued parent progress: a node resolved on the PARENT map, with the
    # run still alive, strictly after an exact Distortion return -- proves
    # the run doesn't just return and stop, it keeps going. "Resolved" is
    # NOT always `node_post`: a node type that suspends on a pending choice
    # (e.g. `item`) only gets `visited=True` at the later `choice_post` that
    # actually resolves it, exactly like every other node type this module
    # already handles that way (legendary, catch, submap reward, ...) -- an
    # earlier version checked only `node_post` and missed this route.
    for r in ev.get("distortion_exact_parent_return", []):
        for j in range(r + 1, len(checkpoints)):
            cp = checkpoints[j]
            if cp["kind"] != "node_pre":
                continue
            node_id = cp["event"].get("node")
            if not node_id:
                break
            # Keep scanning PAST an unresolved `node_post` (a node type that
            # suspends on a pending choice reports it there, not yet
            # visited) until the node is really visited or a NEW node_pre
            # shows the walk moved on without resolving it.
            for k in range(j + 1, len(checkpoints)):
                nxt = checkpoints[k]
                if nxt["kind"] == "node_pre":
                    break
                if nxt["kind"] not in ("node_post", "choice_post"):
                    continue
                if not nxt["game_over"] and nxt["in_sub_map"] is None and _visited(nxt, node_id):
                    _add(ev, "distortion_continued_progress", k)
                    break
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

    # -- full-team swap RELEASE --------------------------------------------
    # `showSwapScreen`'s *replace* branch (bundle.deobfuscated.js:79202-79246).
    # The release loop is guarded by `!(iu || ip)` where `iu` is
    # `state.team.length < 6` (79144), so cards exist ONLY with a full team;
    # each card is `state.team[B2a]`, and its click handler splices that same
    # index out for the incoming Pokemon (79230: `state.team.splice(B2j,1,B)`).
    #
    # Merely PARKING on the screen proves nothing -- the affordance has to be
    # clicked -- so the tag is earned at the `choice_post` that resolves it,
    # and only when the observed team really shows a single-slot in-place
    # replacement: same cardinality, exactly one slot changed. That is the
    # unique observable signature of `splice(i, 1, incoming)`, and it is what
    # distinguishes this branch from the room branch's append (79171-79201),
    # from cancel (79249-79258, which changes nothing) and from a release that
    # dropped or duplicated a member.
    # Keyed on the `choice_pre`/`choice_post` PAIR -- one record per click, so
    # a route that lingers on the screen across several checkpoints cannot
    # inflate the evidence.
    for j, cp in enumerate(checkpoints):
        if cp["kind"] != "choice_post" or cp["pending"] is not None:
            continue
        pre = None
        for k in range(j - 1, -1, -1):
            if checkpoints[k]["kind"] == "choice_pre":
                pre = checkpoints[k]
                break
            if checkpoints[k]["kind"] == "choice_post":
                break
        if pre is None or pre.get("screen") != "swap-screen":
            continue
        pending = pre.get("pending")
        options = (pending or {}).get("options") or []
        if not options or any(o.get("role") != "swap_release" for o in options):
            continue
        # One card per team member, in team order -- the source builds exactly
        # `state.team.length` of them, and only when the team is full.
        if [o.get("slot") for o in options] != list(range(len(pre["team"]))):
            continue
        if len(cp["team"]) != len(pre["team"]):
            continue
        changed = [
            slot for slot, (was, now) in enumerate(zip(pre["team"], cp["team"])) if was != now
        ]
        if len(changed) == 1:
            _add(ev, "swap_release", j)

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
