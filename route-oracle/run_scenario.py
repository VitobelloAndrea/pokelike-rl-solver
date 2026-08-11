"""The Python side of the M3 short-full-run route oracle.

Runs the same deterministic Story/Nuzlocke route as ``run-scenario.js``
through the real ``pokelike.engine`` and emits the same versioned checkpoint
stream on stdout::

    python route-oracle/run_scenario.py route-oracle/scenarios/<name>.json

Nothing here reimplements engine behavior. The only things this module owns
are (a) an RNG-draw counter wrapped around the engine's own private
``Mulberry32`` stream, (b) a ``_run_battle`` wrapper that records each
``BattleResult`` as it is produced, and (c) the normalization that turns
``RunState`` into a checkpoint. See ``SCHEMA.md`` for the schema contract and
for every field that is deliberately excluded.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pokelike import battle_loop, engine, map_gen, rng  # noqa: E402

SCHEMA_VERSION = 2

# `RunState.phase` -> the source screen id `showScreen(...)` would be showing.
# This is what makes the two streams comparable at all: the JS side has no
# `phase` and the Python side has no DOM, but both sides agree on which
# suspended-continuation the player is sitting on. Any phase not listed here
# has no route-matrix counterpart and is reported verbatim so a surprise is
# visible rather than silently mapped onto something plausible.
_PHASE_TO_SCREEN = {
    engine.Phase.CHOOSE_STARTER: "starter-screen",
    engine.Phase.ON_MAP: "map-screen",
    engine.Phase.SWAP_CHOICE: "swap-screen",
    engine.Phase.CATCH_CHOICE: "catch-screen",
    # `doItemNode` opens with `showScreen("item-screen")`
    # (bundle.deobfuscated.js:79261), so this phase has an exact source
    # counterpart. The remaining phases deliberately stay unmapped: their
    # source counterparts are OVERLAYS/MODALS that never call `showScreen`
    # (`openItemEquipModal`, `showBranchingChoice`'s `#eevee-choice-overlay`
    # at 70560, `showTeamPickerModal`'s `#submap-pick-modal` at 76845), or
    # screens no route in the matrix reaches. Leaving them unmapped is what
    # makes an unexpected one show up as `<unmapped:...>` instead of being
    # silently folded onto a plausible-looking screen id.
    engine.Phase.ITEM_CHOICE: "item-screen",
    # `doTradeNode`'s ordinary (non-Endless2) path opens with
    # `showScreen("trade-screen")` (bundle.deobfuscated.js:80587). M4.
    engine.Phase.TRADE_CHOICE: "trade-screen",
    engine.Phase.NEXT_MAP_READY: "badge-screen",
    engine.Phase.GAME_OVER: "gameover-screen",
    engine.Phase.VICTORY: "win-screen",
}


class CountingStream:
    """Delegating wrapper around the engine's private ``Mulberry32`` that
    counts draws. Mirrors the JS side's ``rng`` wrapper exactly: it counts,
    it never substitutes values, and it exposes the same ``state``/``seed``
    surface ``rng.py`` requires."""

    __slots__ = ("_inner", "draws")

    def __init__(self, inner: rng.Mulberry32) -> None:
        self._inner = inner
        self.draws = 0

    def __call__(self) -> float:
        self.draws += 1
        return self._inner()

    @property
    def state(self) -> int:
        return self._inner.state

    def seed(self, value: int) -> None:
        self._inner.seed(value)


def _num(value: Any) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _normalize_stat_buffs(buffs: Optional[dict]) -> dict:
    buffs = buffs or {}
    return {k: buffs[k] for k in ("hp", "atk", "def", "speed", "special", "spdef") if buffs.get(k)}


def _normalize_mon(mon, slot: Optional[int] = None) -> Optional[dict]:
    if mon is None:
        return None
    base = getattr(mon, "base_stats", None)
    held = getattr(mon, "held_item", None)
    out = {
        "species_id": _num(getattr(mon, "species_id", None)),
        # Alternate-form identity. The port carries a distinct form only for
        # Origin Giratina today (species 487, distinct name/stats); anything
        # without one reports null on both sides.
        "form_id": getattr(mon, "form_id", None),
        "name": getattr(mon, "name", None) or None,
        "level": _num(getattr(mon, "level", None)),
        "max_hp": _num(getattr(mon, "max_hp", None)),
        "current_hp": _num(getattr(mon, "current_hp", None)),
        "types": list(getattr(mon, "types", ()) or ()),
        "move_tier": _num(getattr(mon, "move_tier", None)),
        "held_item": getattr(held, "id", None) if held is not None else None,
        "is_shiny": bool(getattr(mon, "is_shiny", False)),
        "status": getattr(mon, "status", None) or None,
        "burned": bool(getattr(mon, "burned", False)),
        "paralyzed": bool(getattr(mon, "paralyzed", False)),
        "poison_stacks": _num(getattr(mon, "poison_stacks", 0)) or 0,
        "base_stats": {
            "hp": _num(getattr(base, "hp", None)),
            "atk": _num(getattr(base, "atk", None)),
            # `data.BaseStats` spells it `defense` (`def` is a Python keyword);
            # the source field is `baseStats.def`.
            "def": _num(getattr(base, "defense", None)),
            "speed": _num(getattr(base, "speed", None)),
            "special": _num(getattr(base, "special", None)),
            "spdef": _num(getattr(base, "spdef", None)),
        }
        if base is not None
        else None,
        "stat_buffs": _normalize_stat_buffs(getattr(mon, "stat_buffs", None)),
    }
    if slot is not None:
        out["slot"] = slot
    return out


def _normalize_node(node: map_gen.MapNode) -> dict:
    out = {
        "id": node.id,
        "type": node.type,
        "layer": _num(node.layer),
        "col": _num(node.col),
        "visited": bool(node.visited),
        "accessible": bool(node.accessible),
        "revealed": bool(node.revealed),
    }
    extra = node.extra or {}
    # Same key set the JS side lifts off its node objects, minus
    # `trainerSprite` (presentation-only, and the source itself deletes it
    # when it retypes a node into SILVER/MAGMA/AQUA/UNDERGROUND/DISTORTION).
    if "mapIndex" in extra or "map_index" in extra:
        out["map_index"] = _num(extra.get("mapIndex", extra.get("map_index")))
    if "legendarySpeciesId" in extra or "legendary_species_id" in extra:
        out["legendary_species_id"] = _num(
            extra.get("legendarySpeciesId", extra.get("legendary_species_id"))
        )
    for js_key, py_key, out_key in (
        ("subKind", "sub_kind", "sub_kind"),
        ("rewardKind", "reward_kind", "reward_kind"),
        ("kind", "kind", "kind"),
        ("trainerKey", "trainer_key", "trainer_key"),
    ):
        if js_key in extra or py_key in extra:
            out[out_key] = extra.get(js_key, extra.get(py_key))
    if "wildBoss" in extra or "wild_boss" in extra:
        out["wild_boss"] = bool(extra.get("wildBoss", extra.get("wild_boss")))
    if "bossTeam" in extra or "boss_team" in extra:
        team = extra.get("bossTeam", extra.get("boss_team")) or []
        # The source's entries are `{id, level}`; `map_gen` spells the id
        # `species_id` for table-backed rosters and `id` for the string form
        # ids (e.g. "giratina-origin"), so accept either.
        out["boss_team"] = [
            {"id": b.get("id", b.get("species_id")), "level": _num(b.get("level"))} for b in team
        ]
    # A REWARD node's `extra["reward"]` (set by `map_gen.generate_sub_map`) is
    # a plain STRING submap-reward-table id ("sacrifice", "stat10", "fossil",
    # "skip", ...), matching the source's own `node.reward` (bundle.
    # deobfuscated.js:53591-53611, e.g. `B2Q["reward"] = "skip"`) exactly --
    # never a dict. The old `isinstance(reward, dict)` guard here checked a
    # shape that never occurs on either side, so `out["reward"]` was silently
    # NEVER populated, found while deriving the M4 `sacrifice`/`stat10`
    # route-oracle scenarios (`coverage.py`'s `sacrifice_reward_resolved`/
    # `stat10_reward_resolved` need the real id to tell the two apart).
    if "reward" in extra:
        out["reward"] = {"kind": extra["reward"]}
    return out


def _normalize_map(gmap: Optional[map_gen.GeneratedMap]) -> Optional[dict]:
    if gmap is None:
        return None
    return {
        "index": _num(gmap.map_index),
        "is_sub_map": gmap.is_sub_map or None,
        "nodes": [_normalize_node(gmap.nodes[nid]) for nid in sorted(gmap.nodes)],
        "edges": [[src, dst] for src, dst in gmap.edges],
    }


def _normalize_sub_map_return(ret: Optional[dict]) -> Optional[dict]:
    if not ret:
        return None
    parent = ret.get("map")
    return {
        "kind": ret.get("kind"),
        "map_index": _num(ret.get("map_index")),
        "node_id": ret.get("node_id"),
        "has_map": bool(parent),
        "map_node_count": len(parent.nodes) if parent is not None else None,
        # The COMPLETE saved parent map, normalized exactly the way the live
        # map is (`_normalize_map`), not a flags-only summary. See the matching
        # comment in driver.js: the old `map_flags` list carried only
        # id/visited/accessible, which was too weak to let `exact_parent_return`
        # tell an exact `advanceFromNode` restore apart from a map whose types,
        # extras, `revealed` flags, map index or edge order had changed.
        "map_topology": _normalize_map(parent),
    }


def _normalize_item(item) -> Optional[str]:
    if isinstance(item, str):
        return item
    return getattr(item, "id", None)


def _fold_turns(events: list) -> list:
    """`BattleResult.battle_events` (a flat ordered list with `turn_start`
    markers) -> the same `[{turn, events}]` shape `driver.js`'s `deriveTurns`
    folds the source's `detailedLog` into.

    A compared-family event before the first `turn_start` is a hard error, the
    exact counterpart of the driver's own pre-first-round assertion: both
    runtimes must place every attack inside a round or fail loudly rather than
    drop it from the projection.

    **The compared family is `attack` and nothing else**, exactly as on the
    JavaScript side, where `deriveTurns` keeps only `log[i].type === 'attack'`
    (route-oracle/driver.js:244) out of the source's single flat `detailedLog`.
    `battle_loop.BattleResult.battle_events` is the Python counterpart of that
    same flat log, and since M6 it also carries the `effect` and `faint`
    presentation families (N10/N11) that the renderer needs. Those have no
    counterpart in the JS projection -- `deriveTurns` drops the source's own
    `effect`/`faint`/`send_out` entries -- so projecting them here would
    manufacture a difference out of a record BOTH runtimes produce and NEITHER
    previously compared. The filter keeps the two projections symmetric.

    This is deliberately STRICTER than the JS side: an unrecognized type is a
    hard error rather than a silent drop, so a family added to `battle_events`
    later cannot quietly slip out of the comparison the way it could if this
    were a bare `if type == "attack"`.
    """
    compared = {"attack"}
    presentation_only = {"effect", "faint"}
    turns: list[dict] = []
    for event in events:
        kind = event.get("type")
        if kind == "turn_start":
            turns.append({"turn": int(event["round"]), "events": []})
            continue
        if kind in presentation_only:
            continue
        if kind not in compared:
            raise RuntimeError(
                f"battle event {kind!r} is neither compared nor a known "
                f"presentation-only family; classify it in _fold_turns and "
                f"SCHEMA.md before it reaches the projection"
            )
        if not turns:
            raise RuntimeError(f"battle event {kind!r} precedes the first turn_start")
        turns[-1]["events"].append(dict(event))
    return turns


def _normalize_battle(result: battle_loop.BattleResult, draws: int) -> dict:
    return {
        "player_won": bool(result.player_won),
        "rounds": _num(result.rounds),
        "rng_draws": draws,
        "player_team": [_normalize_mon(m) for m in result.player_team],
        "enemy_team": [_normalize_mon(m) for m in result.enemy_team],
        "player_participants": sorted(result.player_participants),
        "status_events": [dict(e) for e in result.status_events],
        # Ordered, turn-delimited attack stream -- see `_fold_turns` and
        # driver.js's `deriveTurns`. M3.3b workstream 5.
        "turns": _fold_turns(result.battle_events),
        # Diagnostic only, never compared: the Python port has no full
        # per-turn battle event log to count. See SCHEMA.md.
        "__diagnostic_event_count": None,
    }


# ---------------------------------------------------------------------------
# Canonical pending-choice OPTION IDENTITY (M3.3b workstream 3)
#
# Mirrors `driver.js`'s `monOption`/`itemOption`/`pendingState` field for
# field. Every option is a semantic property of the offered object -- no
# renderer strings, no object identity, no opaque hash.
#
# `instance` is the full normalized instance the runtime built for that
# option, or None when it built none. It is REPORTED rather than omitted so
# that an absent instance is a compared difference instead of a silent
# exclusion (the same discipline `counters.any_fainted` follows).
# ---------------------------------------------------------------------------


def _mon_option(role: str, mon, slot: Optional[int] = None) -> dict:
    norm = _normalize_mon(mon)
    return {
        "role": role,
        "kind": "mon",
        "species_id": norm["species_id"] if norm else None,
        "form_id": norm["form_id"] if norm else None,
        "name": norm["name"] if norm else None,
        "item_id": None,
        "slot": slot,
        "instance": norm,
    }


def _item_option(role: str, item) -> dict:
    return {
        "role": role,
        "kind": "item",
        "species_id": None,
        "form_id": None,
        "name": getattr(item, "name", None),
        "item_id": getattr(item, "id", None),
        "slot": None,
        "instance": None,
    }


def _is_shiny_origin(pending) -> bool:
    """`_visit_shiny` (engine.py:2340-2377) reuses `Phase.CATCH_CHOICE`
    wholesale rather than a dedicated phase, but the source's `doShinyNode`
    (bundle.deobfuscated.js:80872-80990) uses its OWN screen
    (`showScreen("shiny-screen")`), never `catch-screen`. `_visit_shiny`
    tags its pending with `extra["origin"] = "shiny_node"` precisely so this
    projection can tell the two apart -- without it, a shiny scenario would
    fail on `screen`/`pending.phase`/option `role` alone even with correct
    game logic, since `_PHASE_TO_SCREEN`/`_pending_projection` would report
    the ordinary catch shape for both.

    NOT `"question"`: `_visit_catch` (engine.py:2330) ALSO tags a
    question-resolved-to-ordinary-catch pending with `origin="question"`,
    and that one really does belong on `catch-screen`/role `"catch"` (the
    source dispatches it to `doCatchNode`, not `doShinyNode`) -- collapsing
    the two onto the same origin string was a real bug this projection had
    until the M4 route-oracle bridge work traced the exact source and gave
    `_visit_shiny` its own distinct origin value."""
    return bool(pending) and pending.phase == engine.Phase.CATCH_CHOICE and (pending.extra or {}).get("origin") == "shiny_node"


def _resume_state(st) -> dict:
    """`RunState`'s three live resume guards -> the same record `driver.js`'s
    `resumeState` reads off the source's own `state` (M4.2).

    Read from real engine fields (`saved_question_resolve` / `saved_catch` /
    `saved_shiny_node`), which `_resolve_question`, `_visit_catch`,
    `_visit_shiny`, `_try_add_to_team`, `_resolve_catch_choice` and
    `_resolve_swap_choice` maintain at the source's own write and clear
    points -- nothing is synthesized here. Projecting them is what makes the
    branch-specific clearing asymmetry observable at all: `doShinyNode`'s
    room accept clears `savedShinyNode` and RETAINS `savedQuestionResolve`
    (80962), `catchPokemon`'s clears `savedCatch` and `savedQuestionResolve`
    (79041-79042), and `showSwapScreen`'s three exits clear all three
    (79182-79184 / 79227-79229 / 79252-79254). None of those differences
    reaches the team, counter, screen, node or RNG projections.

    LIVE state only. What `saveRun()` last wrote to storage is a separate
    fact this port has no persistence layer for; see SCHEMA.md.
    """
    q = st.saved_question_resolve
    c = st.saved_catch
    s = st.saved_shiny_node
    return {
        "saved_question_resolve": None
        if not q
        else {"key": q.get("key"), "resolved_type": q.get("resolved_type")},
        "saved_catch": None
        if not c
        else {
            "key": c.get("key"),
            "instances": [
                _mon_option("saved_catch", m, i) for i, m in enumerate(c.get("instances") or [])
            ],
        },
        "saved_shiny_node": None
        if not s
        else {"key": s.get("key"), "species_id": _num(s.get("species_id"))},
    }


def _screen_for(st) -> str:
    """The screen the source would actually be showing underneath, including
    the four M4-bridged phases that used to report `<unmapped:...>`.

    `MOVE_TUTOR_CHOICE`/`ITEM_EQUIP_CHOICE`/`EVOLUTION_CHOICE`/
    `REWARD_TEAM_PICK` never call `showScreen` themselves (see SCHEMA.md's
    "Phase <-> screen"), so `currentScreen` on the JS side is whatever the
    LAST real screen change left it at -- and unlike JS, Python's dispatch is
    constrained enough that every one of these has exactly one (or, for
    `EVOLUTION_CHOICE`, exactly two) real callers, so the correct value can be
    read off `pending.extra` rather than needing a general "last screen"
    field. Confirmed against a real cross-runtime probe during the M4 route-
    oracle bridge work: before this fix, a real move-tutor route agreed on
    every compared field EXCEPT this one (`<unmapped:move_tutor_choice>` vs
    the source's real `"map-screen"`)."""
    pending = st.pending
    if _is_shiny_origin(pending):
        return "shiny-screen"
    if pending is not None:
        extra = pending.extra or {}
        if pending.phase == engine.Phase.MOVE_TUTOR_CHOICE:
            # `doMoveTutorNode` is dispatched straight from `onNodeClick` on
            # an ordinary node visit (bundle.deobfuscated.js:77356-77358),
            # which always starts from `map-screen`.
            return "map-screen"
        if pending.phase == engine.Phase.ITEM_EQUIP_CHOICE:
            # `openItemEquipModal` is called from `doItemNode`'s own
            # non-usable-item click handler (79423-79429) with no screen
            # change first -- `currentScreen` is still `doItemNode`'s own
            # `showScreen("item-screen")` (79263).
            return "item-screen"
        if pending.phase == engine.Phase.REWARD_TEAM_PICK:
            # `doSubMapReward` is dispatched straight from `onNodeClick` too
            # (77377-77379).
            return "map-screen"
        if pending.phase == engine.Phase.EVOLUTION_CHOICE:
            # `showBranchingChoice` is awaited from `checkAndEvolveTeam`,
            # called from two contexts: `runBattleScreen`'s win-branch
            # `_run_todo`/evolve step (81381) -- BEFORE any `showScreen` call
            # ever leaves the battle results screen, so `currentScreen` is
            # still `"battle-screen"` -- or `_apply_use_item`'s Moon Stone /
            # Rare Candy bag-item path (79624). The route-oracle's action
            # vocabulary (visit/choice/advance_map, SCHEMA.md) has no "use
            # item" action, so the bag-item source is UNREACHABLE through
            # this harness and its screen value is not cross-runtime
            # verified; `_maybe_evolve_one`'s own `extra["source"]`
            # ("todo" | "item") is what tells the two apart.
            return "battle-screen" if extra.get("source") == "todo" else "map-screen"
    return _PHASE_TO_SCREEN.get(st.phase, f"<unmapped:{st.phase.value}>")


def _pending_projection(st) -> Optional[dict]:
    """`RunState.pending` -> the same record `driver.js`'s `pendingState`
    builds from the source's own run state.

    Options are read from `PendingChoice.extra`, which holds the live
    `Combatant`/item objects the engine will actually act on, in the order it
    will act on them -- the exact counterpart of reading
    `state.savedCatch.instances` / `state.itemOffer.ids` / `showSwapScreen`'s
    arguments on the source side. `PendingChoice.options` (the renderer-facing
    summary) is used as an independent cardinality cross-check, and a
    disagreement is a hard error rather than a silently preferred side.
    """
    pending = st.pending
    if pending is None:
        return None
    phase = pending.phase
    extra = pending.extra or {}
    options: list[dict]
    context: Optional[dict] = None
    # Overridden below only for the shiny-origin CATCH_CHOICE case, to match
    # `_screen_for`'s `screen`/`driver.js`'s `pendingState` `shiny_choice`.
    phase_name = phase.value

    if phase == engine.Phase.CHOOSE_STARTER:
        # The three real pending `Combatant`s `Engine.reset` built from
        # `showStarterSelect`'s own `rollShiny`/`createInstance` loop
        # (bundle.deobfuscated.js:76175-76194), in displayed order -- the same
        # discipline as every other branch here: read the live objects the
        # engine will act on, never the renderer-facing summary. Before M4 the
        # port built none and this projected `instance: null`, which was frozen
        # blocker 1(b).
        instances = extra.get("instances")
        if instances is None:
            raise RuntimeError("choose_starter pending has no `instances` in extra")
        options = [_mon_option("starter", m) for m in instances]
    elif phase == engine.Phase.CATCH_CHOICE:
        candidates = extra.get("candidates")
        if candidates is None:
            raise RuntimeError("catch_choice pending has no `candidates` in extra")
        if _is_shiny_origin(pending):
            # `doShinyNode` (bundle.deobfuscated.js:80872-80990) is its own
            # screen and its own card role ("shiny"), never the ordinary
            # "catch" one -- see `_is_shiny_origin`/`_screen_for`.
            phase_name = "shiny_choice"
            options = [_mon_option("shiny", m) for m in candidates]
        else:
            options = [_mon_option("catch", m) for m in candidates]
    elif phase == engine.Phase.ITEM_CHOICE:
        items = extra.get("items")
        if items is None:
            raise RuntimeError("item_choice pending has no `items` in extra")
        options = [_item_option("item", it) for it in items]
    elif phase == engine.Phase.SWAP_CHOICE:
        incoming = extra.get("incoming")
        if incoming is None:
            raise RuntimeError("swap_choice pending has no `incoming` in extra")
        # `showSwapScreen`'s own room test (bundle.deobfuscated.js:79143).
        # Both port entry points agree with it: `_offer_swap_screen` records
        # `has_room` explicitly, and `_try_add_to_team`'s swap branch is only
        # reached with a full team.
        has_room = len(st.team) < engine.TEAM_CAP
        if has_room:
            options = [_mon_option("swap_accept", incoming)]
        else:
            # DECLARED ASYMMETRY, see SCHEMA.md "one declared asymmetry".
            # The source suppresses every release card under
            # `ip = challengeNoReplace && !iu` (79145, loop guard 79202), and
            # driver.js mirrors that. There is no counterpart here because the
            # ported engine has no `challenge_no_replace` field: the flag is
            # set only by `case "noreplace"` of the Challenges setup switch
            # (82796), and Challenges mode is out of M3 scope. This tripwire
            # exists so that if the port ever grows the flag, the divergence
            # is a loud error at exactly this line instead of a silent
            # cardinality disagreement between the two runners.
            if getattr(st, "challenge_no_replace", False):
                raise RuntimeError(
                    "swap_choice: the engine now sets `challenge_no_replace`, but this "
                    "projection has no `ip` guard while driver.js does "
                    "(bundle.deobfuscated.js:79145/79202). Port the guard here before "
                    "running a Challenges route -- see SCHEMA.md."
                )
            options = [_mon_option("swap_release", m, i) for i, m in enumerate(st.team)]
        context = {
            "incoming": _mon_option("incoming", incoming),
            "team": [_mon_option("team", m, i) for i, m in enumerate(st.team)],
        }
    elif phase == engine.Phase.MOVE_TUTOR_CHOICE:
        # `_visit_move_tutor`'s `extra` carries only `node_id`; the live
        # identity is `st.team[team_index]`, and `pending.options[i][
        # "team_index"]` is exactly the index `_resolve_move_tutor_choice`
        # itself reads at resolution time (engine.py:3083) -- reading it back
        # here keeps the two in lockstep by construction rather than by
        # assuming the same filter/order twice. Mirrors `doMoveTutorNode`'s
        # one `[data-tutor]` button per non-mastered member, in team order
        # (bundle.deobfuscated.js:80470-80517); may legitimately be empty
        # (a fully-mastered team) -- the source still opens the modal with
        # only its skip control, see `_visit_move_tutor`.
        options = [_mon_option("move_tutor", st.team[o["team_index"]], o["team_index"]) for o in pending.options]
    elif phase == engine.Phase.TRADE_CHOICE:
        # `doTradeNode`'s ordinary path (bundle.deobfuscated.js:80580-80638)
        # builds one `li.trade-member-row` per team member, in team order,
        # and `_resolve_trade_choice` reads `action.index` directly as the
        # team slot (engine.py:3147) -- no separate candidate list exists
        # until the replacement is rolled at click time.
        options = [_mon_option("trade", m, i) for i, m in enumerate(st.team)]
    elif phase == engine.Phase.ITEM_EQUIP_CHOICE:
        # `openItemEquipModal` as `doItemNode` always calls it
        # (`fromBagIdx=-1, fromPokemonIdx=-1`, bundle.deobfuscated.js:
        # 79423-79429): one `[data-idx]` button per team member, in team
        # order, plus a real decline (`#btn-equip-to-bag`, see
        # `_resolve_item_equip_choice`). `action.index` is the team slot
        # directly (engine.py:3121).
        options = [_mon_option("item_equip", m, i) for i, m in enumerate(st.team)]
    elif phase == engine.Phase.REWARD_TEAM_PICK:
        # `showTeamPickerModal` (bundle.deobfuscated.js:76845-76884) -- the
        # `sacrifice`/`stat10` submap rewards. One `[data-idx]` button per
        # team member, in team order; `_resolve_reward_team_pick` reads
        # `action.index` directly as the team slot for both reward kinds
        # (engine.py:2985, 2989).
        options = [_mon_option("team_pick", m, i) for i, m in enumerate(st.team)]
    elif phase == engine.Phase.EVOLUTION_CHOICE:
        # `showBranchingChoice` (bundle.deobfuscated.js:70560-70613): the
        # options are hypothetical evolution TARGETS, not team members or
        # existing instances -- `extra["branches"]` is the exact
        # `BRANCHING_EVOLUTIONS[speciesId]` entry list `_maybe_evolve_one`
        # raised the choice from (engine.py:1513-1521), in the same order the
        # source builds its cards.
        branches = extra.get("branches")
        if branches is None:
            raise RuntimeError("evolution_choice pending has no `branches` in extra")
        team_index = extra.get("team_index")
        options = [
            {
                "role": "evolution_branch", "kind": "mon", "species_id": b.into, "form_id": None,
                "name": b.name, "item_id": None, "slot": None, "instance": None,
            }
            for b in branches
        ]
        context = {"evolving": _mon_option("evolving", st.team[team_index])} if team_index is not None else None
    else:
        # A phase with no route-matrix counterpart. Report the cardinality the
        # engine itself declares and no invented identities, so an unexpected
        # screen is visible rather than silently shaped into a known one.
        options = [
            {
                "role": f"<unprojected:{phase.value}>",
                "kind": None,
                "species_id": None,
                "form_id": None,
                "name": None,
                "item_id": None,
                "slot": None,
                "instance": None,
            }
            for _ in pending.options
        ]

    if len(options) != len(pending.options):
        raise RuntimeError(
            f"{phase.value}: projected {len(options)} option(s) but "
            f"PendingChoice.options has {len(pending.options)}"
        )
    return {
        "phase": phase_name,
        "optional": bool(pending.optional),
        "option_count": len(options),
        "options": options,
        "context": context,
    }


class Runner:
    def __init__(self, scenario: dict) -> None:
        self.sc = scenario
        self.seq = 0
        self.checkpoints: list[dict] = []
        self.battles: list[dict] = []
        self.notes: list[str] = []
        self.error: Optional[str] = None

        self.engine = engine.Engine()
        # Per-instance, no global state: the counter wraps this Engine's own
        # private Mulberry32 stream.
        self.counter = CountingStream(self.engine._rng_stream)
        self.engine._rng_stream = self.counter  # type: ignore[assignment]

    def _install_battle_recorder(self):
        """Record every BattleResult as the engine produces it, without
        changing what the engine does with it.

        Installed for the duration of `run()` ONLY, never in `__init__`:
        `engine._run_battle` is a module global, so patching it at
        construction time meant that building two Runners before stepping
        either one left the second one's wrapper stranded (the first one's
        restore put the original back and silently dropped it). Constructing
        a Runner therefore has no global side effect at all.
        """
        real_run_battle = engine._run_battle

        def counting_run_battle(state, enemy_team):
            before = self.counter.draws
            result = real_run_battle(state, enemy_team)
            self.battles.append(_normalize_battle(result, self.counter.draws - before))
            return result

        engine._run_battle = counting_run_battle  # type: ignore[assignment]
        return real_run_battle

    # -- checkpoint ------------------------------------------------------
    def checkpoint(self, kind: str, event: Optional[dict] = None) -> dict:
        st = self.engine.state
        assert st is not None
        cp = {
            "schema_version": SCHEMA_VERSION,
            "scenario": self.sc["scenario"],
            "seq": self.seq,
            "kind": kind,
            "event": event or {},
            "mode": {
                "nuzlocke": bool(st.nuzlocke_mode),
                "gen2": bool(st.gen2_mode),
                "gen3": bool(st.gen3_mode),
                "gen4": bool(st.gen4_mode),
            },
            "seed": _num(self.sc["seed"]),
            "rng": {"state": self.counter.state, "draws": self.counter.draws},
            "screen": _screen_for(st),
            "map": _normalize_map(st.map),
            "current_map": _num(st.current_map),
            "current_node": st.current_node_id,
            "in_sub_map": st.in_sub_map or None,
            "sub_map_return": _normalize_sub_map_return(st.sub_map_return),
            "counters": {
                "badges": _num(st.badges) or 0,
                "elite_index": _num(st.elite_index) or 0,
                "starter_species_id": _num(st.starter_species_id),
                "max_team_size": _num(st.max_team_size) or 0,
                "silver_beaten": _num(st.silver_beaten) or 0,
                "fought_admin": bool(st.fought_admin),
                "used_pokecenter": bool(st.used_pokecenter),
                "picked_up_item": bool(st.picked_up_item),
                "used_tm": bool(st.used_tm),
                "used_ball_catch": bool(st.used_ball_catch),
                "got_via_question": bool(st.got_via_question),
                "any_fainted": bool(getattr(st, "any_fainted", False)),
                "escaped_via_rope": bool(st.escaped_via_rope),
                "distortion_worlds_entered": _num(st.distortion_worlds_entered) or 0,
                "distortion_legendary_claimed": bool(st.distortion_legendary_claimed),
                "entered_sub_map": bool(st.entered_sub_map),
            },
            "team": [_normalize_mon(m, i) for i, m in enumerate(st.team)],
            "items": [_normalize_item(i) for i in st.items],
            "game_over": bool(st.game_over) or st.phase == engine.Phase.GAME_OVER,
            # Pending-choice shape: phase, cardinality, optionality, and the
            # ORDERED semantic identity of every offered option (M3.3b
            # workstream 3). Captured pre-resolution, from the objects each
            # runtime will actually act on. See `_pending_projection` and
            # SCHEMA.md.
            "pending": _pending_projection(st),
            # The three live save/resume guards, compared normally -- see
            # `_resume_state` and SCHEMA.md.
            "resume_state": _resume_state(st),
        }
        self.checkpoints.append(cp)
        self.seq += 1
        return cp

    # -- route -----------------------------------------------------------
    def run(self) -> dict:
        sc = self.sc
        real_run_battle = self._install_battle_recorder()
        try:
            self.engine.reset(
                nuzlocke_mode=bool(sc["mode"]["nuzlocke"]),
                gen2_mode=bool(sc["mode"]["gen2"]),
                gen3_mode=bool(sc["mode"]["gen3"]),
                gen4_mode=bool(sc["mode"]["gen4"]),
                seed=int(sc["seed"]),
            )
            self.checkpoint("run_init", {})

            st = self.engine.state
            assert st is not None and st.pending is not None
            self.checkpoint("starter_offered", {"screen": _PHASE_TO_SCREEN[st.phase]})

            # Optional RNG alignment instrument -- see SCHEMA.md and the
            # matching block in driver.js. Symmetric, source-API-only
            # (`seed_rng` is the port of the source's own `seedRng`), applied
            # at the same route point on both sides, and never a repair.
            align = sc.get("align_rng_after_starter_offer")
            if align is not None:
                previous = rng.set_active_stream(self.counter)  # type: ignore[arg-type]
                try:
                    rng.seed_rng(int(align) & 0xFFFFFFFF)
                finally:
                    rng.set_active_stream(previous)
                self.checkpoint("rng_aligned", {"to": int(align) & 0xFFFFFFFF})

            starter_index = int(sc["starter_index"])
            options = st.pending.options
            if not (0 <= starter_index < len(options)):
                raise IndexError(
                    f"starter_index {starter_index} out of range (offered {len(options)})"
                )
            self.engine.step(engine.ChooseStarter(int(options[starter_index]["species_id"])))
            self.checkpoint("starter_selected", {"starter_index": starter_index})

            for step, act in enumerate(sc["actions"]):
                kind = act["kind"]
                if kind == "visit":
                    node_id = act["node"]
                    st = self.engine.state
                    assert st is not None and st.map is not None
                    node = st.map.nodes.get(node_id)
                    if node is None:
                        raise KeyError(f"step {step}: no node {node_id}")
                    self.checkpoint(
                        "node_pre", {"node": node_id, "node_type": node.type, "step": step}
                    )
                    before = len(self.battles)
                    self.engine.step(engine.VisitNode(node_id))
                    for b in range(before, len(self.battles)):
                        self.checkpoint(
                            "battle",
                            {"node": node_id, "battle_index": b, "battle": self.battles[b]},
                        )
                    self.checkpoint("node_post", {"node": node_id, "step": step})
                elif kind == "choice":
                    st = self.engine.state
                    assert st is not None
                    index = act.get("index")
                    self.checkpoint(
                        "choice_pre",
                        {
                            "screen": _screen_for(st),
                            "index": index,
                            "step": step,
                        },
                    )
                    before = len(self.battles)
                    self.engine.step(engine.SelectOption(index))
                    for b in range(before, len(self.battles)):
                        self.checkpoint(
                            "battle", {"node": None, "battle_index": b, "battle": self.battles[b]}
                        )
                    self.checkpoint("choice_post", {"index": index, "step": step})
                elif kind == "advance_map":
                    st = self.engine.state
                    assert st is not None
                    self.checkpoint(
                        "map_transition_pre", {"from_map": st.current_map, "step": step}
                    )
                    if st.phase != engine.Phase.NEXT_MAP_READY:
                        raise RuntimeError(
                            f"step {step}: advance_map but phase is {st.phase.value}"
                        )
                    self.engine.step(engine.AdvanceMap())
                    st = self.engine.state
                    assert st is not None
                    self.checkpoint(
                        "map_transition_post", {"to_map": st.current_map, "step": step}
                    )
                elif kind == "equip":
                    # M6. The item-equip overlay opened FROM THE BAG -- the
                    # source's `openItemEquipModal(item, {fromBagIdx: i})`,
                    # reached from an item-bar badge
                    # (bundle.deobfuscated.js:64857). Distinct entry point from
                    # the item NODE's own equip offer, which the `choice`
                    # bridge already covers.
                    st = self.engine.state
                    assert st is not None
                    bag_index = int(act["bag_index"])
                    member = int(act["member"])
                    self.checkpoint(
                        "equip_pre",
                        {
                            "bag_index": bag_index,
                            "member": member,
                            "item": st.items[bag_index] if 0 <= bag_index < len(st.items) else None,
                            "step": step,
                        },
                    )
                    self.engine.step(
                        engine.EquipItem(bag_index=bag_index, team_index=member))
                    self.checkpoint(
                        "equip_post", {"bag_index": bag_index, "member": member, "step": step})
                elif kind == "held_item":
                    # M6. The item-equip overlay opened FROM a member -- the
                    # source's `openItemEquipModal(team[i].heldItem,
                    # {fromPokemonIdx: i})`, reached from a held-item badge
                    # (bundle.deobfuscated.js:64702-64709 team bar, 78203 party
                    # screen). `target: null` takes `#btn-equip-to-bag`
                    # (79549-79553); an integer takes that member's
                    # `[data-idx]` row, which is the hand-off (79541-79545).
                    st = self.engine.state
                    assert st is not None
                    member = int(act["member"])
                    target = act.get("target")
                    self.checkpoint(
                        "held_item_pre",
                        {
                            "member": member,
                            "target": target,
                            "held": (
                                st.team[member].held_item.id
                                if 0 <= member < len(st.team) and st.team[member].held_item
                                else None
                            ),
                            "step": step,
                        },
                    )
                    if target is None:
                        self.engine.step(engine.UnequipItem(team_index=member))
                    else:
                        self.engine.step(
                            engine.HandOffItem(from_index=member, to_index=int(target)))
                    self.checkpoint(
                        "held_item_post", {"member": member, "target": target, "step": step})
                else:
                    raise ValueError(f"step {step}: unknown action kind {kind}")

            st = self.engine.state
            assert st is not None
            self.checkpoint(
                "terminal",
                {
                    "game_over": bool(st.game_over) or st.phase == engine.Phase.GAME_OVER,
                    "team_size": len(st.team),
                    "screen": _screen_for(st),
                },
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced in the output, not swallowed
            import traceback

            self.error = "".join(traceback.format_exception(exc))
        finally:
            engine._run_battle = real_run_battle  # type: ignore[assignment]

        out: dict = {
            "checkpoints": self.checkpoints,
            "notes": self.notes,
            "rng_draws_total": self.counter.draws,
            "network_attempts": [],
            "screen_log": [c["screen"] for c in self.checkpoints],
        }
        if self.error:
            out["error"] = self.error
        return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python run_scenario.py <scenario.json>", file=sys.stderr)
        return 1
    with open(argv[1], encoding="utf-8") as handle:
        scenario = json.load(handle)
    if scenario.get("schema_version") != SCHEMA_VERSION:
        print(
            f"scenario {argv[1]} declares schema_version {scenario.get('schema_version')}, "
            f"runner speaks {SCHEMA_VERSION}",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(json.dumps(Runner(scenario).run()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
