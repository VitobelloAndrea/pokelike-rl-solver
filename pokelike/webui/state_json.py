"""RunState <-> JSON conversion for the web UI.

**The encode half is now a thin adapter over `pokelike.render.contract`**
(R1). It used to own its own field selection, which made it a second,
undocumented contract that drifted from `pokelike/render/console.py`'s -- the
two disagreed about what a Pokemon's "status" is, among other things. The
renderer observation/event contract is now defined in exactly one place and
both renderers project from it; see docs/renderer-contract.md.

The decode half below is unchanged and stays here: turning an HTTP request
body into an `engine.Action` is a web-transport concern, not part of the
observation contract, and `render/console.py` has no use for it.
"""

from __future__ import annotations

from pokelike import engine
from pokelike.render import contract


def encode_state(state: engine.RunState, *, recent_log: int = 5) -> dict:
    """The full state a browser client needs to render every core-loop
    screen. `recent_log` trims `state.log` to its trailing N entries (the
    UI only ever needs to react to what just happened, not the whole run's
    history).

    A strict SUPERSET of what this emitted before R1: every previously-emitted
    key survives with the same name and type, so an existing `app.js` keeps
    working unchanged. See `contract.OBSERVATION_FIELDS`.
    """
    return contract.observation(state, recent_log=recent_log)


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
        # R3: `cancel` is `SelectOption`'s THIRD exit, not a synonym for a skip
        # -- see `engine.SelectOption`'s docstring and M5's `#btn-equip-cancel`
        # finding (bundle.deobfuscated.js:79563-79569). It was unreachable from
        # this transport before R3: the key was silently dropped here, so the
        # browser's only way to leave the equip overlay was `index=None`, which
        # BANKS the item -- exactly the divergence M5 proved the engine apart
        # from. Rejected as a non-boolean rather than coerced, for the same
        # reason `_to_bool` exists in server.py (CODEX.md issue 47).
        cancel = payload.get("cancel", False)
        if not isinstance(cancel, bool):
            raise ActionDecodeError(f"'cancel' must be a boolean, got {cancel!r}")
        return engine.SelectOption(
            index=None if index is None else _to_int(index, "index"),
            cancel=cancel,
        )
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
    if kind == "UnequipItem":
        if "team_index" not in payload:
            raise ActionDecodeError("UnequipItem requires 'team_index'")
        return engine.UnequipItem(
            team_index=_to_int(payload["team_index"], "team_index"),
        )
    if kind == "HandOffItem":
        if "from_index" not in payload or "to_index" not in payload:
            raise ActionDecodeError("HandOffItem requires 'from_index' and 'to_index'")
        return engine.HandOffItem(
            from_index=_to_int(payload["from_index"], "from_index"),
            to_index=_to_int(payload["to_index"], "to_index"),
        )
    raise ActionDecodeError(f"unknown action type: {kind!r}")
