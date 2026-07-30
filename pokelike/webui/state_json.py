"""RunState <-> JSON conversion for the web UI. Pure encoding/decoding, no
engine calls -- mirrors `pokelike.render.console`'s role but produces JSON
for `app.js` instead of formatted text. Keeping this separate from
`server.py` makes it independently testable without spinning up an HTTP
server.
"""

from __future__ import annotations

from typing import Optional

from pokelike import engine
from pokelike.battle import Combatant


def _mon_json(mon: Combatant) -> dict:
    return {
        "species_id": mon.species_id,
        "name": mon.name,
        "level": mon.level,
        "current_hp": mon.current_hp,
        "max_hp": mon.max_hp,
        "hp_pct": round(100.0 * mon.current_hp / mon.max_hp, 1) if mon.max_hp else 0.0,
        "status": mon.status,
        "is_shiny": mon.is_shiny,
        "types": list(mon.types),
        "held_item": mon.held_item.id if mon.held_item is not None else None,
        "move_tier": mon.move_tier,
    }


def _node_json(node) -> dict:
    return {
        "id": node.id,
        "type": node.type,
        "layer": node.layer,
        "col": node.col,
        "visited": node.visited,
        "accessible": node.accessible,
        "revealed": node.revealed,
    }


def _pending_json(pending: Optional["engine.PendingChoice"]) -> Optional[dict]:
    if pending is None:
        return None
    return {
        "phase": pending.phase.value,
        "optional": pending.optional,
        "options": pending.options,  # already plain dicts of primitives, see engine.PendingChoice's docstring
    }


def encode_state(state: engine.RunState, *, recent_log: int = 5) -> dict:
    """The full state a browser client needs to render every core-loop
    screen. `recent_log` trims `state.log` to its trailing N entries (the
    UI only ever needs to react to what just happened, not the whole run's
    history)."""
    map_json = None
    if state.map is not None:
        map_json = {
            "map_index": state.map.map_index,
            "current_node_id": state.current_node_id,
            "nodes": [_node_json(n) for n in state.map.nodes.values()],
            "edges": [list(e) for e in state.map.edges],
            "question_cache": dict(state.question_cache),
        }
    return {
        "phase": state.phase.value,
        "current_map": state.current_map,
        "badges": state.badges,
        "elite_index": state.elite_index,
        "nuzlocke_mode": state.nuzlocke_mode,
        "gen2_mode": state.gen2_mode,
        "gen3_mode": state.gen3_mode,
        "gen4_mode": state.gen4_mode,
        "team": [_mon_json(m) for m in state.team],
        "items": list(state.items),
        "map": map_json,
        "pending": _pending_json(state.pending),
        "log": state.log[-recent_log:],
        "log_total": len(state.log),  # monotonic counter -- lets a client detect "new" log entries across trimmed responses
        "game_over": state.game_over,
        "won": state.won,
        "run_seed": state.run_seed,
    }


class ActionDecodeError(ValueError):
    pass


def _to_int(value, field: str) -> int:
    """CODEX.md issue 47: a raw `int(...)` call on attacker-controlled JSON
    (a string, float, `None`, list, ...) raises an uncaught `ValueError`/
    `TypeError`, which `server.py` would let escape as an unhandled 500
    instead of the intended 400. Every scalar coercion below goes through
    this instead.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ActionDecodeError(f"{field!r} must be an integer, got {value!r}") from None


def decode_action(payload: dict) -> "engine.Action":
    """Turns a POSTed JSON body (`{"type": "...", ...}`) into an
    `engine.Action`. Raises `ActionDecodeError` on a malformed payload --
    `server.py` turns that into an HTTP 400, distinct from `engine.py`'s
    own `ValueError` for a well-formed-but-illegal action (which becomes a
    409, see `server.py`)."""
    if not isinstance(payload, dict) or "type" not in payload:
        raise ActionDecodeError("action payload must be an object with a 'type' field")
    kind = payload["type"]
    if kind == "ChooseStarter":
        if "species_id" not in payload:
            raise ActionDecodeError("ChooseStarter requires 'species_id'")
        return engine.ChooseStarter(species_id=_to_int(payload["species_id"], "species_id"))
    if kind == "VisitNode":
        if "node_id" not in payload:
            raise ActionDecodeError("VisitNode requires 'node_id'")
        return engine.VisitNode(node_id=str(payload["node_id"]))
    if kind == "AdvanceMap":
        return engine.AdvanceMap()
    if kind == "SelectOption":
        index = payload.get("index", None)
        return engine.SelectOption(index=None if index is None else _to_int(index, "index"))
    if kind == "ReorderTeam":
        order = payload.get("order")
        if not isinstance(order, list) or not order:
            raise ActionDecodeError("ReorderTeam requires a non-empty 'order' list")
        return engine.ReorderTeam(order=tuple(_to_int(v, "order") for v in order))
    if kind == "UseItem":
        if "item_index" not in payload or "target_index" not in payload:
            raise ActionDecodeError("UseItem requires 'item_index' and 'target_index'")
        return engine.UseItem(
            item_index=_to_int(payload["item_index"], "item_index"),
            target_index=_to_int(payload["target_index"], "target_index"),
        )
    if kind == "EquipItem":
        if "bag_index" not in payload or "team_index" not in payload:
            raise ActionDecodeError("EquipItem requires 'bag_index' and 'team_index'")
        return engine.EquipItem(
            bag_index=_to_int(payload["bag_index"], "bag_index"),
            team_index=_to_int(payload["team_index"], "team_index"),
        )
    raise ActionDecodeError(f"unknown action type: {kind!r}")
