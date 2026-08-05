"""The renderer observation/event contract (R1).

**One versioned surface, two renderers.** `pokelike/render/console.py` and
`pokelike/webui/state_json.py` are two consumers with different output
formats but the same information needs. Before R1 each reached into
`RunState` and `Combatant` independently and picked whatever fields it
happened to want, which is why the two disagreed about (for example) what a
"status" is. This module is the single place that decides what the engine
exposes to a renderer; both consumers project from here.

**This is NOT the route oracle's contract.** `route-oracle/SCHEMA.md`
(version 2) is a *cross-runtime parity* contract: its fields exist because
both the JavaScript and the Python runtime can produce them and they must
agree byte for byte. This module's consumer is a UI that only ever runs
against the Python engine, so it deliberately carries fields the oracle does
not compare and could not compare -- base and effective stats, stat stages,
item presentation metadata, node sprite hints, an overlay discriminator.

The two contracts are versioned independently. `CONTRACT_VERSION` below is
this surface's version and has nothing to do with the oracle's
`schema_version: 2`. **Adding a field here must never require touching
`SCHEMA.md`, the oracle's compared fields, or `frozen_signature.json`.**

**Battle feed ownership.** `battle_loop.run_battle` is the single producer of
battle events. The *shape of each record* is owned by the oracle --
`run_scenario._fold_turns` projects those same records into the compared
`turns` field. This module therefore only ever reads records on its way out:
it may drop fields, regroup them, or enrich a view from `RunState`, but it
must never require a new key inside a `battle_events` record. Wanting one is
the signal to add it to the enrichment layer here instead. See
docs/renderer-contract.md for the full rationale.

**Faithfulness.** Where the port cannot supply something the real site shows,
this module says so rather than inventing a value: see `UNSUPPLIED` and the
`unsupplied` key on the observation. A renderer must treat those as "unknown",
not as a real value.
"""

from __future__ import annotations

from typing import Optional

from pokelike import battle, data, engine, map_gen
from pokelike.battle import Combatant

# Bump on any change to the *shape* of what `observation()` returns: a field
# added, removed, renamed, or given a different type. Not bumped for a value
# changing. Independent of route-oracle SCHEMA.md's version 2.
CONTRACT_VERSION = 1

#: Things the real site displays that this port has no faithful source for.
#: Named here rather than filled with a plausible-looking placeholder, per
#: CLAUDE.md's "don't guess at game logic".
UNSUPPLIED = (
    # `getBestMove` picks ONE move per (species, tier); the source's move
    # *names* shown on the move-tutor card come from that same pool, so the
    # preview below is real. What is NOT modelled is a 4-move moveset -- the
    # site shows one move because the engine has one, not because we dropped
    # three.
    "move_pp",
    # The source's per-turn `log`/`detailedLog` flavor strings. The mechanical
    # events are carried; the prose that the site renders alongside them is
    # not ported (CLAUDE.md: `js/ui.js` is reference-only).
    "battle_flavor_text",
    # Encounter preview for an unvisited node: the source rolls a wild
    # encounter's species AT VISIT TIME from the live RNG stream, so there is
    # no pre-visit species to preview without drawing (and thereby changing
    # the run). `node_view`'s `encounter` carries only what map generation
    # already fixed -- see that function.
    "unvisited_wild_species",
)

_STAT_KEYS = ("atk", "def", "speed", "special", "spdef")


# ---------------------------------------------------------------------------
# Pinned field sets. The tests import these and assert the real projections
# match them exactly, so a field silently disappearing (or a stray one being
# added) fails rather than being noticed later by a renderer author.
# ---------------------------------------------------------------------------

MON_FIELDS = frozenset({
    "species_id", "name", "nickname", "level",
    "current_hp", "max_hp", "hp_pct", "fainted",
    "status", "status_flags", "types", "is_shiny",
    "held_item", "held_item_info", "move_tier", "move_preview",
    "base_stats", "effective_stats", "stages", "stat_buffs",
    "sprite_url",
})

NODE_FIELDS = frozenset({
    "id", "type", "layer", "col",
    "visited", "accessible", "revealed",
    "encounter",
})

ITEM_FIELDS = frozenset({"id", "name", "desc", "icon", "icon_url", "usable", "known"})

OBSERVATION_FIELDS = frozenset({
    "contract_version", "phase", "screen", "overlay",
    "current_map", "badges", "elite_index",
    "nuzlocke_mode", "gen2_mode", "gen3_mode", "gen4_mode",
    "in_sub_map", "team", "items", "items_info", "map",
    "pending", "legal_actions", "battle",
    "log", "log_total", "game_over", "won", "run_seed", "unsupplied",
})

#: Every `type` string a renderer can see in `battle.turns[*].events` or
#: `battle.status_events`. Pinned so renaming one in `battle_loop` fails here.
BATTLE_EVENT_TYPES = frozenset({"attack"})
STATUS_EVENT_TYPES = frozenset({"status_tick", "faint", "poison_drain"})


# ---------------------------------------------------------------------------
# Pokemon
# ---------------------------------------------------------------------------

def _effective_stats(mon: Combatant) -> dict:
    """Every stat as the battle engine would actually read it right now --
    stages, buffs and the mon's OWN held item folded in, via the same
    `battle.get_effective_stat` the damage formula calls. This is the number a
    hover card should show; `base_stats` alone is misleading mid-battle.
    """
    items = [mon.held_item] if mon.held_item is not None else []
    return {
        stat: battle.get_effective_stat(mon, stat, items, mon.stages)
        for stat in _STAT_KEYS
    }


def _base_stats(mon: Combatant) -> dict:
    bs = mon.base_stats
    return {
        "hp": bs.hp,
        "atk": bs.atk,
        "defense": bs.defense,
        "speed": bs.speed,
        "special": bs.special,
        # Genuinely absent on some fixed-trainer rosters -- see data.BaseStats.
        # Reported as None rather than backfilled, so a renderer can show the
        # same fallback the engine uses instead of a fabricated number.
        "spdef": getattr(bs, "spdef", None),
    }


def _status_flags(mon: Combatant) -> dict:
    """The FULL status picture. `Combatant.status` only ever holds
    "freeze"/"sleep"/None (see battle.Combatant) -- burn, paralysis and poison
    live in three separate fields. `_mon_json` used to emit `status` alone,
    so a burned Pokemon rendered as perfectly healthy. All four are carried
    here; `status` is kept for the exact source-shaped value.
    """
    return {
        "sleep_or_freeze": mon.status,
        "burned": bool(mon.burned),
        "paralyzed": bool(mon.paralyzed),
        "poison_stacks": int(mon.poison_stacks or 0),
    }


def _move_preview(mon: Combatant) -> Optional[dict]:
    """The single move this Pokemon would actually attack with, from
    `battle.get_best_move` -- the same call the battle loop makes. This is
    what a move-tutor card needs to show "current move -> move after
    tutoring" (CODEX gap 10), and what a hover card needs to explain damage.

    Returns None if the move cannot be built, rather than a placeholder.
    """
    try:
        move = battle.get_best_move(
            mon.types, mon.base_stats, mon.species_id,
            mon.move_tier, mon.held_item,
        )
    except Exception:
        return None
    if move is None:
        return None
    return {
        "name": move.name,
        "type": move.type,
        "power": move.power,
        "is_special": bool(move.is_special),
        "typeless": bool(move.typeless),
        "no_damage": bool(move.no_damage),
    }


def _sprite_url(mon: Combatant) -> Optional[str]:
    """The species sprite the site would draw, shiny-aware. Read from the
    ported pokedex rather than invented; None for a species absent from it.
    """
    entry = data.get_pokedex().get(mon.species_id)
    if entry is None:
        return None
    return entry.shiny_sprite_url if mon.is_shiny else entry.sprite_url


def mon_view(mon: Combatant) -> dict:
    """One team/enemy member, fully presented. Superset of the old
    `state_json._mon_json`: every field that one emitted is still here with
    the same name and type, so an existing client keeps working.
    """
    return {
        "species_id": mon.species_id,
        "name": mon.name,
        "nickname": mon.nickname,
        "level": mon.level,
        "current_hp": mon.current_hp,
        "max_hp": mon.max_hp,
        "hp_pct": round(100.0 * mon.current_hp / mon.max_hp, 1) if mon.max_hp else 0.0,
        "fainted": mon.current_hp <= 0,
        "status": mon.status,
        "status_flags": _status_flags(mon),
        "types": list(mon.types),
        "is_shiny": mon.is_shiny,
        "held_item": mon.held_item.id if mon.held_item is not None else None,
        "held_item_info": item_view(mon.held_item.id) if mon.held_item is not None else None,
        "move_tier": mon.move_tier,
        "move_preview": _move_preview(mon),
        "base_stats": _base_stats(mon),
        "effective_stats": _effective_stats(mon),
        "stages": dict(mon.stages),
        "stat_buffs": dict(mon.stat_buffs or {}),
        "sprite_url": _sprite_url(mon),
    }


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _item_table() -> dict:
    table = {}
    for item in data.get_passive_items():
        table[item.id] = item
    for item in data.get_usable_items():
        table[item.id] = item
    return table


def item_view(item_id: str) -> dict:
    """A bag/held item with the presentation metadata the source's own item
    table carries (CODEX gap 6: the browser was handed bare string ids).

    `known` is False for an id absent from both ported tables -- Mega Stones
    are built by `makeMegaStoneItem` from a SEPARATE source table with a
    different shape and are not members of either. Reported honestly rather
    than guessed at.
    """
    item = _item_table().get(item_id)
    if item is None:
        return {
            "id": item_id, "name": item_id, "desc": None,
            "icon": None, "icon_url": None, "usable": False, "known": False,
        }
    return {
        "id": item.id,
        "name": item.name,
        "desc": item.desc,
        "icon": item.icon,
        "icon_url": item.icon_url,
        "usable": bool(item.usable),
        "known": True,
    }


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

def _encounter_hint(node: map_gen.MapNode) -> Optional[dict]:
    """What map GENERATION already fixed about this node, for an encounter
    icon / hover preview (CODEX gaps 7 and 8).

    Only genuinely pre-determined facts appear here -- these are read off
    `node.extra`, which `map_gen` populated at generation time. An ordinary
    wild encounter's species is NOT among them: the source rolls it from the
    live RNG stream when the node is visited, so previewing it would mean
    drawing, which would change the run. See `UNSUPPLIED`.
    """
    extra = node.extra or {}
    hint = {
        "trainer_sprite": extra.get("trainerSprite"),
        "legendary_species_id": extra.get("legendarySpeciesId"),
        "sub_boss": extra.get("subBoss"),
        "reward": extra.get("reward"),
    }
    return hint if any(v is not None for v in hint.values()) else None


def node_view(node: map_gen.MapNode) -> dict:
    """One map node. Superset of the old `state_json._node_json` -- same seven
    fields, plus the generation-time presentation hints.
    """
    return {
        "id": node.id,
        "type": node.type,
        "layer": node.layer,
        "col": node.col,
        "visited": node.visited,
        "accessible": node.accessible,
        "revealed": node.revealed,
        "encounter": _encounter_hint(node),
    }


def _resolved_question_marks(state: engine.RunState) -> dict:
    """`{node_id: resolved_type}` for the question nodes on the map being
    drawn. Moved verbatim from `state_json` (M4.2 behavior preserved).

    The source keeps ONE `{key, resolvedType}` record, not a map -- a second
    question node overwrites the first (bundle.deobfuscated.js:77326-77332) --
    so this holds at most one entry. The record's key is map-qualified
    (`"m<currentMap>:<nodeId>"`) while a renderer indexes by BARE node id, so
    the prefix is stripped and a record belonging to another map contributes
    nothing.
    """
    record = state.saved_question_resolve
    if not record:
        return {}
    prefix = f"m{state.current_map}:"
    key = record.get("key") or ""
    if not key.startswith(prefix):
        return {}
    return {key[len(prefix):]: record["resolved_type"]}


def map_view(state: engine.RunState) -> Optional[dict]:
    if state.map is None:
        return None
    return {
        "map_index": state.map.map_index,
        "current_node_id": state.current_node_id,
        "is_sub_map": state.map.is_sub_map,
        "nodes": [node_view(n) for n in state.map.nodes.values()],
        "edges": [list(e) for e in state.map.edges],
        "question_cache": _resolved_question_marks(state),
    }


# ---------------------------------------------------------------------------
# Screen / overlay -- M5 finding F1
# ---------------------------------------------------------------------------

#: The four phases whose source modal opens WITHOUT a `showScreen` call, so
#: the source's own `currentScreen` still names the screen underneath while
#: the modal is up. Values are that underlying screen, matching
#: `route-oracle/run_scenario.py::_screen_for`. Kept in sync with it by test,
#: not by import -- the oracle must stay free of renderer concerns.
_OVERLAY_PHASES = {
    engine.Phase.ITEM_EQUIP_CHOICE: "item-equip-overlay",
    engine.Phase.MOVE_TUTOR_CHOICE: "move-tutor-overlay",
    engine.Phase.EVOLUTION_CHOICE: "branching-evolution-overlay",
    engine.Phase.REWARD_TEAM_PICK: "team-picker-overlay",
}


def overlay_for(state: engine.RunState) -> Optional[str]:
    """M5 finding F1, disposed on the RENDERER surface only.

    The four `showScreen`-less overlays leave the projected `screen` at the
    screen underneath (`map-screen` for three of them). That is CORRECT and is
    not a gameplay defect: in the source the modal itself is the guard --
    `.item-equip-overlay` is `position:fixed; inset:0; z-index:500` with no
    `pointer-events:none` (`style/main.css:2125-2139`), so it intercepts every
    map click, and the port's phase guard is equivalent for any
    player-reachable route. The oracle's compared `pending` field already
    distinguishes the states, so nothing there needs to change and nothing
    here may change it.

    What a *renderer* needs, and did not have, is an explicit answer to "is a
    modal up, and which one" -- so it can draw the overlay and suppress map
    interaction without inferring both from `phase`. That is what this adds,
    additively, on this surface alone.
    """
    return _OVERLAY_PHASES.get(state.phase)


def _screen_for(state: engine.RunState) -> str:
    """The screen a renderer should have showing UNDERNEATH any overlay.

    Deliberately a small, renderer-owned mapping rather than an import of
    `run_scenario._screen_for`: that function answers a parity question (what
    would the JS's `currentScreen` variable hold) and is owned by the oracle.
    Coupling the renderer to it would let a renderer need drag the oracle's
    projection around, which is exactly the conflation R1 forbids.
    """
    phase = state.phase
    if phase == engine.Phase.CHOOSE_STARTER:
        return "starter-screen"
    if phase == engine.Phase.GAME_OVER:
        return "gameover-screen"
    if phase == engine.Phase.VICTORY:
        return "victory-screen"
    if phase == engine.Phase.NEXT_MAP_READY:
        return "badge-screen"
    if phase == engine.Phase.ITEM_CHOICE:
        return "item-screen"
    if phase == engine.Phase.ITEM_EQUIP_CHOICE:
        # The overlay opens on top of `doItemNode`'s own item screen.
        return "item-screen"
    if phase == engine.Phase.TRADE_CHOICE:
        return "trade-screen"
    if phase in (engine.Phase.CATCH_CHOICE, engine.Phase.SWAP_CHOICE):
        return "catch-screen"
    if phase == engine.Phase.ESCAPE_ROPE_CHOICE:
        return "battle-screen"
    return "map-screen"


# ---------------------------------------------------------------------------
# Battle feed
# ---------------------------------------------------------------------------

def fold_turns(battle_events: list) -> list:
    """Flat `battle_events` -> `[{turn, events}]`.

    The renderer's OWN fold. It is intentionally a separate function from
    `run_scenario._fold_turns` even though both partition on `turn_start`:
    that one raises on a compared-family event before the first turn boundary,
    because for the oracle a dropped event is a parity failure that must be
    loud. A renderer must not crash on a malformed feed -- it opens a
    synthetic turn 0 instead, so the UI degrades to "shows the hits without a
    round number" rather than failing to draw.
    """
    turns: list[dict] = []
    for event in battle_events:
        if event.get("type") == "turn_start":
            turns.append({"turn": int(event["round"]), "events": []})
            continue
        if not turns:
            turns.append({"turn": 0, "events": []})
        turns[-1]["events"].append(dict(event))
    return turns


def battle_view(state: engine.RunState) -> Optional[dict]:
    """The most recent battle as a turn-by-turn replay, or None if no battle
    has resolved yet this run. This is what R4's animation track consumes.

    Every record is copied on the way out: a renderer holding this must not be
    able to mutate engine state through it.
    """
    feed = state.last_battle
    if not feed:
        return None
    return {
        "rounds": feed.get("rounds"),
        "player_won": bool(feed.get("player_won")),
        "turns": fold_turns(feed.get("battle_events") or []),
        "status_events": [dict(e) for e in (feed.get("status_events") or [])],
    }


# ---------------------------------------------------------------------------
# Pending choice
# ---------------------------------------------------------------------------

def pending_view(pending: Optional["engine.PendingChoice"]) -> Optional[dict]:
    """`PendingChoice.options` is already plain dicts of primitives (see its
    docstring); `extra` is engine-internal and deliberately NOT exposed --
    it can hold live `Combatant`/`Trainer` references.
    """
    if pending is None:
        return None
    return {
        "phase": pending.phase.value,
        "optional": pending.optional,
        "options": [dict(o) if isinstance(o, dict) else o for o in pending.options],
    }


# ---------------------------------------------------------------------------
# The observation
# ---------------------------------------------------------------------------

def observation(state: engine.RunState, *, recent_log: int = 5) -> dict:
    """The complete renderer-facing view of a run.

    `legal_actions` is included so the observation and action sides of the
    boundary travel together: a renderer that draws a button for an illegal
    action is the same bug class as one that reads a stale field.
    `engine.legal_actions` remains the single authority -- this only carries
    its answer, it does not re-derive it.

    Relationship to run-state serialization (P1.9, deliberately out of R1's
    scope): this is a LOSSY, presentation-oriented projection and must never
    be used to reconstruct a `RunState`. It drops `pending.extra`, `passives`,
    `_todo` and the resume guards, all of which a resumption format needs. The
    two surfaces are independent by design; adding a field here neither helps
    nor blocks adding one there.
    """
    return {
        "contract_version": CONTRACT_VERSION,
        "phase": state.phase.value,
        "screen": _screen_for(state),
        "overlay": overlay_for(state),
        "current_map": state.current_map,
        "badges": state.badges,
        "elite_index": state.elite_index,
        "nuzlocke_mode": state.nuzlocke_mode,
        "gen2_mode": state.gen2_mode,
        "gen3_mode": state.gen3_mode,
        "gen4_mode": state.gen4_mode,
        "in_sub_map": state.in_sub_map,
        "team": [mon_view(m) for m in state.team],
        "items": list(state.items),
        "items_info": [item_view(i) for i in state.items],
        "map": map_view(state),
        "pending": pending_view(state.pending),
        "legal_actions": engine.legal_actions(state),
        "battle": battle_view(state),
        "log": state.log[-recent_log:],
        # Monotonic counter -- lets a client detect "new" log entries across
        # trimmed responses.
        "log_total": len(state.log),
        "game_over": state.game_over,
        "won": state.won,
        "run_seed": state.run_seed,
        "unsupplied": list(UNSUPPLIED),
    }
