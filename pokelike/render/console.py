"""A text/console renderer over `engine.RunState`. Pure formatting -- reads
`RunState`'s public fields and produces strings, no engine calls, no
mutation. Deliberately plain-ASCII (no emoji/color) so it renders
predictably in any terminal, including the one this was validated in
(Windows `PowerShell`/`git-bash`, see `docs/handover.md`'s environment
gotchas). Polish (color, a pygame view, etc.) is a natural follow-up once
this baseline is validated -- CLAUDE.md asks to start simple here.
"""

from __future__ import annotations

from pokelike import engine, map_gen
from pokelike.render import contract

_NODE_SYMBOLS = {
    map_gen.START: "0",
    map_gen.BATTLE: "B",
    map_gen.CATCH: "C",
    map_gen.ITEM: "I",
    map_gen.QUESTION: "?",
    map_gen.BOSS: "S",
    map_gen.POKECENTER: "P",
    map_gen.TRAINER: "T",
    map_gen.LEGENDARY: "L",
    map_gen.MOVE_TUTOR: "M",
    map_gen.TRADE: "R",
    map_gen.SILVER: "Y",
    map_gen.MAGMA: "G",
    map_gen.AQUA: "A",
    map_gen.UNDERGROUND: "U",
    map_gen.DISTORTION: "D",
    map_gen.REWARD: "W",
    map_gen.SUBEXIT: "X",
    "shiny": "*",
    "mega": "+",
}


def hp_bar(fraction: float, width: int = 20) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _status_text(flags: dict) -> str:
    """Every status the engine actually tracks, not just `Combatant.status`.

    R1: this used to print `mon.status` alone, which only ever holds
    "freeze"/"sleep"/None -- burn, paralysis and poison live in three separate
    fields, so a burned Pokemon rendered as perfectly healthy. The contract's
    `status_flags` carries all four; this shows all four.
    """
    parts = []
    if flags["sleep_or_freeze"]:
        parts.append(flags["sleep_or_freeze"])
    if flags["burned"]:
        parts.append("burn")
    if flags["paralyzed"]:
        parts.append("par")
    if flags["poison_stacks"]:
        parts.append(f"psn x{flags['poison_stacks']}")
    return f" [{', '.join(parts)}]" if parts else ""


def render_team(state: engine.RunState) -> str:
    """Projects through `render.contract`, the single renderer contract, so
    this and the browser client cannot drift about what a team member is.
    """
    if not state.team:
        return "  (no team)"
    lines = []
    for i, mon in enumerate(state.team):
        view = contract.mon_view(mon)
        frac = (view["current_hp"] / view["max_hp"]) if view["max_hp"] else 0.0
        shiny = " (shiny)" if view["is_shiny"] else ""
        held = f" @{view['held_item']}" if view["held_item"] is not None else ""
        lines.append(
            f"  {i}. {view['name']} Lv{view['level']}{shiny}{held}"
            f"{_status_text(view['status_flags'])}  "
            f"{hp_bar(frac)} {view['current_hp']}/{view['max_hp']}"
        )
    return "\n".join(lines)


def _node_cell(node: map_gen.MapNode) -> str:
    if not node.revealed:
        return "[ ? ]"
    symbol = _NODE_SYMBOLS.get(node.type, "?")
    if node.accessible:
        marker = ">"
    elif node.visited:
        marker = "."
    else:
        marker = " "
    return f"{marker}[{symbol}]{marker}"


def render_map(state: engine.RunState) -> str:
    if state.map is None:
        return "(no map yet)"
    lines = [f"Map {state.current_map} -- currently at {state.current_node_id}"]
    for layer in state.map.layers:
        if not layer:
            continue
        cells = "  ".join(_node_cell(n) for n in layer)
        lines.append(f"  layer {layer[0].layer}: {cells}")
    lines.append("  legend: " + ", ".join(f"{v}={k}" for k, v in _NODE_SYMBOLS.items()))
    lines.append("  '>' accessible now, '.' visited, ' ' revealed-not-visited, '?' not yet revealed")
    return "\n".join(lines)


def _format_option(opt: dict) -> str:
    if "into" in opt:
        return opt["name"]
    if "id" in opt and "usable" in opt:
        return opt["name"] + (" (usable)" if opt["usable"] else " (held item)")
    if "species_id" in opt or "team_index" in opt:
        label = opt.get("name", "?")
        if opt.get("is_shiny"):
            label += " (shiny)"
        if "level" in opt:
            label += f" Lv{opt['level']}"
        if "move_tier" in opt:
            label += f" (tier {opt['move_tier']})"
        return label
    return str(opt)


def render_pending(state: engine.RunState) -> str:
    if state.pending is None:
        return ""
    lines = [f"-- choose ({state.phase.value}) --"]
    for i, opt in enumerate(state.pending.options):
        lines.append(f"  {i}: {_format_option(opt)}")
    if state.pending.optional:
        lines.append("  (or skip / decline)")
    return "\n".join(lines)


def render_log_entry(entry: dict) -> str:
    kind = entry["type"]
    if kind == "battle":
        outcome = "WON" if entry["won"] else "LOST"
        enemies = ", ".join(f"{m['name']} Lv{m['level']}" for m in entry["enemy_team"])
        return f"Battle vs [{enemies}] -- {outcome} ({entry['rounds']} rounds)"
    if kind == "evolve":
        return f"Team member #{entry['team_index']} evolved into {entry['name']}!"
    if kind == "badge":
        return f"Badge earned! ({entry['badges']} total)"
    if kind == "start_map":
        return f"Entered map {entry['map_index']}"
    if kind == "catch":
        shiny = " (shiny)" if entry.get("is_shiny") else ""
        released = f", released {entry['released']}" if "released" in entry else ""
        return f"Caught {entry['name']}{shiny}{released}!"
    if kind == "item":
        if entry.get("usable"):
            return f"Picked up {entry['name']}"
        return f"Picked up {entry['name']}, equipped on {entry.get('equipped_on', '?')}"
    if kind == "move_tutor":
        return f"{entry['name']} learned a new move (tier {entry['move_tier']})!"
    if kind == "trade":
        return f"Traded {entry['gave']} for {entry['received']}"
    if kind == "victory":
        return "*** VICTORY! Elite Four defeated! ***"
    if kind == "game_over":
        return "*** GAME OVER ***"
    return str(entry)


def render_state(state: engine.RunState, *, recent_log: int = 1) -> str:
    """The full snapshot: recent event(s), map, team, inventory, and
    whatever decision is currently pending. `recent_log` controls how many
    trailing `state.log` entries to show (default: just the latest)."""
    sections = [f"=== {state.phase.value} === (map {state.current_map}, badges {state.badges})"]
    if state.log:
        for entry in state.log[-recent_log:]:
            sections.append(render_log_entry(entry))
    sections.append(render_map(state))
    sections.append("Team:")
    sections.append(render_team(state))
    items = ", ".join(state.items) if state.items else "(none)"
    sections.append(f"Items: {items}")
    pending = render_pending(state)
    if pending:
        sections.append(pending)
    return "\n".join(sections)
