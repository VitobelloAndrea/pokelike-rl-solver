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

# R2: the plain-ASCII stand-in for the source's Unicode `getNodeIcon` glyphs
# (bundle.deobfuscated.js:54576-54593). This module is deliberately ASCII-only
# (see the docstring above), so the glyphs themselves cannot be used -- but the
# MAPPING is now one-to-one with the source's, keyed off the same node types,
# and the full type name travels in the node table below, so nothing is lost to
# a one-letter abbreviation the way it was before R2.
_ASCII_NODE_SYMBOLS = {
    map_gen.START: "*",
    map_gen.BATTLE: "x",
    map_gen.CATCH: "o",
    map_gen.ITEM: "i",
    map_gen.QUESTION: "?",
    map_gen.BOSS: "K",
    map_gen.POKECENTER: "+",
    map_gen.TRAINER: "F",
    map_gen.LEGENDARY: "L",
    map_gen.MOVE_TUTOR: "n",
    map_gen.TRADE: "=",
    map_gen.SILVER: "S",
    map_gen.MAGMA: "M",
    map_gen.AQUA: "A",
    map_gen.UNDERGROUND: "U",
    map_gen.DISTORTION: "D",
    map_gen.REWARD: "G",
    map_gen.SUBEXIT: "E",
}
#: `getNodeIcon`'s own default for a type absent from its table -- which in the
#: source really is UNDERGROUND and DISTORTION (54594).
_ASCII_NODE_DEFAULT = "."
#: `getNodeIcon` returns this for a visited node before consulting the table.
_ASCII_VISITED = "v"


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
        view = contract.mon_view(mon, state.passives)
        frac = (view["current_hp"] / view["max_hp"]) if view["max_hp"] else 0.0
        shiny = " (shiny)" if view["is_shiny"] else ""
        held = f" @{view['held_item']}" if view["held_item"] is not None else ""
        lines.append(
            f"  {i}. {view['name']} Lv{view['level']}{shiny}{held}"
            f"{_status_text(view['status_flags'])}  "
            f"{hp_bar(frac)} {view['current_hp']}/{view['max_hp']}"
        )
        lines.append(_stat_line(view))
    return "\n".join(lines)


def _stat_line(view: dict, include_move: bool = True) -> str:
    """M6/N24, the console side.

    `include_move=False` is R7/N43's caller: `_format_option` already prints
    the move (and, on the tutor screen, its successor) ahead of the stats, so
    repeating it on the stat line would say the same thing twice.

    **Decision, and why.** The web client got the source's real hover card
    (`showTeamHoverCard`, bundle.deobfuscated.js:64506-64564). A terminal has
    no pointer, so a literal port is meaningless here -- but "do nothing" would
    leave this renderer unable to show data the other one shows, and the whole
    finding was that R1 built `effective_stats`/`stat_buffs`/`crit_chance`
    for a card nobody drew. So the equivalent information is shown INLINE,
    always, rather than behind an interaction this renderer cannot offer.

    That is a different choice from R2's map-parity ceiling, where the console
    genuinely could not reach the source's presentation at all. Here it can
    reach the *content*; only the trigger is inexpressible.

    The number shown is the EFFECTIVE stat, because that is what the battle
    engine would actually read right now.

    **`base_stats` is deliberately not shown next to it as a delta.** The two
    are different quantities on different scales, not a before/after pair:
    `base_stats` is the species' base table (Bulbasaur's Atk 49) while
    `effective_stats` is the computed battle stat at this level (Atk 9 at
    Lv5). Subtracting them produces "-40", which reads as a crippling debuff
    and means nothing. What genuinely modifies the effective number is
    `stages` and `stat_buffs`, so those are what get annotated.
    """
    eff = view["effective_stats"]
    stages, buffs = view["stages"], view["stat_buffs"]
    parts = []
    for label, key in (
        ("Atk", "atk"), ("Def", "def"), ("SpA", "special"),
        ("SpD", "spdef"), ("Spe", "speed"),
    ):
        value = eff.get(key)
        if value is None:
            continue
        marks = ""
        stage = stages.get(key) or 0
        if stage:
            marks += f" {stage:+d}stg"
        buff = buffs.get(key) or 0
        if buff:
            marks += f" {buff:+d}buf"
        parts.append(f"{label} {value}{marks}")
    extra = ""
    # The source's own conditions: 64494-64496 and 64500-64504.
    if abs((view["crit_chance"] or 0) - contract._BASE_CRIT_PCT) >= 0.01:
        extra += f"  crit {round(view['crit_chance'])}%"
    if view["augment_pct"]:
        extra += f"  augment +{view['augment_pct']}%"
    # R6/N34. The move the web client now draws on every card. On the team
    # line it is the same fact the browser shows, in the form this medium has.
    if include_move:
        move = _move_text(view.get("move_preview"))
        if move:
            extra += f"  move {move}"
    return "       " + "  ".join(parts) + extra


def _node_cell(view: dict) -> str:
    """One node in the grid, using the contract's own state flags.

    R2: `clickable`, `dimmed` and `is_current` come from `render.contract`,
    which ports `renderMap`'s `BcH`/`BcC`/`Bcr` (bundle.deobfuscated.js:
    54172-54181). Before R2 this module derived its own -- and derived them
    differently from the browser client, which is exactly the drift the
    contract exists to prevent. Note in particular that "clickable" is
    accessible AND NOT visited, not merely accessible.
    """
    if not view["revealed"]:
        return "[ ? ]"
    if view["is_current"]:
        symbol = _ASCII_VISITED
    elif view["dimmed"]:
        symbol = _ASCII_VISITED
    else:
        symbol = _ASCII_NODE_SYMBOLS.get(view["type"], _ASCII_NODE_DEFAULT)
    if view["clickable"]:
        marker = ">"
    elif view["visited"]:
        marker = "."
    else:
        marker = " "
    return f"{marker}[{symbol}]{marker}"


def _tooltip_text(tooltip: dict) -> str:
    """The contract's structured hover text as one line.

    `render.contract._node_tooltip` carries `getNodeLabel`'s CONTENT
    (bundle.deobfuscated.js:54686-54824) as `{title, notes, team}` rather than
    the source's inline-styled HTML, precisely so a text renderer can use it.
    """
    parts = [tooltip["title"]]
    parts.extend(tooltip["notes"])
    parts.extend(f"{m['name']} Lv{m['level']}" for m in tooltip["team"])
    return " | ".join(str(p) for p in parts)


def render_map(state: engine.RunState) -> str:
    """The per-layer grid, plus a table of the nodes you can actually reach.

    R2 changed two things. The grid's column ORDER is now the source's own
    layer-array order with the source's centring (`render.contract`'s
    `pos.x_frac`, ported from bundle.deobfuscated.js:54133-54138), so a layer
    of 4 no longer reads as if it were flush-left. And a bare one-letter
    symbol is no longer the only thing said about a node: every reachable node
    gets its full type and its real hover text, which the source has always
    shown on hover and this renderer previously had no way to express.
    """
    if state.map is None:
        return "(no map yet)"
    view = contract.map_view(state)
    by_id = {n["id"]: n for n in view["nodes"]}

    lines = [f"Map {state.current_map} -- currently at {state.current_node_id}"]
    # Centre each layer the way the source does: its own x is
    # `W/2 + (i - (m-1)/2) * W/(m + 0.2)`, so a narrow layer sits inboard of a
    # wide one. Scaled here to the widest layer's cell count.
    widest = max((len(l) for l in state.map.layers if l), default=1)
    for layer in state.map.layers:
        if not layer:
            continue
        views = [by_id[n.id] for n in layer]
        indent = " " * int(round((widest - len(views)) * 3.5))
        cells = "  ".join(_node_cell(v) for v in views)
        lines.append(f"  layer {layer[0].layer}: {indent}{cells}")

    lines.append("  legend: " + ", ".join(
        f"{sym}={node_type}" for node_type, sym in _ASCII_NODE_SYMBOLS.items()
    ) + f", {_ASCII_VISITED}=visited, {_ASCII_NODE_DEFAULT}=other")
    lines.append("  '>' clickable now, '.' visited, ' ' revealed-not-visited, '?' not yet revealed")

    reachable = [n for n in view["nodes"] if n["clickable"]]
    if reachable:
        lines.append("  reachable:")
        for node in sorted(reachable, key=lambda n: (n["layer"], n["col"])):
            lines.append(f"    {node['id']:>6} {node['type']:<11} {_tooltip_text(node['tooltip'])}")
    return "\n".join(lines)


def _move_text(mp) -> str:
    """R6/N34, the console side of the source's `.poke-move` block
    (bundle.deobfuscated.js:64348-64366).

    The web client renders that block as real markup -- category icon, type
    badge, power badge. All three are *values*, and a terminal can carry
    values; only the badges' shape is inexpressible. So the same four facts
    are rendered as one text run, in the source's own order (name, category,
    type, power) and with the source's own em dash for a no-damage move
    (64362-64363).

    Returns "" when the contract supplied no `move_preview`, so a caller can
    omit the field entirely rather than print a placeholder.
    """
    if not mp or not mp.get("name"):
        return ""
    category = "Spe" if mp.get("is_special") else "Phy"
    move_type = mp.get("type") or "--"
    power = "--" if mp.get("no_damage") else f"{mp.get('power')} PWR"
    return f"{mp['name']} {category} {move_type} {power}"


def _format_option(opt: dict) -> str:
    # R3. The escape-rope option is `{"action": ..., "item_index": ...}`
    # (engine.py:1546-1551), which matched no branch below and fell through to
    # `str(opt)` -- the console printed a raw Python dict at the one decision
    # that ends the run if answered wrong. `label`/`item_id` are the renderer
    # contract's read-side enrichment (contract._pending_options).
    if opt.get("action") == "use_escape_rope":
        label = opt.get("label", "Use Escape Rope")
        return f"{label} (bag slot {opt['item_index']})"
    if "into" in opt:
        types = opt.get("types") or []
        suffix = f" [{'/'.join(types)}]" if types else ""
        return f"{opt['name']}{suffix}"
    if "id" in opt and "usable" in opt:
        # R6/N33, the console side. The web client's item card was ignoring
        # `icon`/`icon_url`/`desc`; this renderer was ignoring `desc` for the
        # same reason -- nobody read the fields `contract.item_view` had been
        # supplying since R3. The icon is a 36px sprite and the emoji fallback
        # is decorative, so neither is ported here; the DESCRIPTION is plain
        # text and is exactly the thing that makes an item choice decidable,
        # so it is. That is the §7.5 split: port what the medium can carry,
        # and say plainly what it cannot.
        label = opt["name"] + (" (usable)" if opt["usable"] else " (held item)")
        if opt.get("desc"):
            label += f" -- {opt['desc']}"
        return label
    if "species_id" in opt or "team_index" in opt:
        label = opt.get("name", "?")
        if opt.get("is_shiny"):
            label += " (shiny)"
        if "level" in opt:
            label += f" Lv{opt['level']}"
        # R7/N43, the console side. The types and the HP the web card draws in
        # its first zone. Both are plain values, so this medium carries them
        # unchanged; only the sprite and the coloured bar are inexpressible,
        # and `hp_bar` already answers the bar.
        types = opt.get("types") or []
        if types:
            label += f" [{'/'.join(types)}]"
        if opt.get("max_hp"):
            frac = (opt.get("current_hp") or 0) / opt["max_hp"]
            label += f"  {hp_bar(frac)} {opt['current_hp']}/{opt['max_hp']}"
        if opt.get("held_item"):
            label += f"  @{opt['held_item']}"
        if "move_tier" in opt:
            label += f" (tier {opt['move_tier']})"
        # R6/N34, the console side. The move block the web client now draws on
        # every card is text here, and on the move-tutor screen it is the whole
        # decision -- "tier 3" alone says nothing about what the Pokemon would
        # actually attack with.
        move = _move_text(opt.get("move_preview"))
        if move:
            label += f" [{move}]"
        # R7/N45, the console side. The OTHER half of CODEX gap 10: what the
        # Pokemon would attack with AFTER tutoring. This is the whole decision
        # on that screen -- a current move alone cannot say whether tutoring
        # this member buys anything -- and it is text, so it is built here.
        # `move_tier_capped` is stated rather than hidden: a fully-tutored
        # Pokemon previews its own current move, and saying so is the honest
        # presentation of the engine's `min(2, tier + 1)` ceiling.
        if "move_preview_next" in opt:
            nxt = _move_text(opt.get("move_preview_next"))
            if opt.get("move_tier_capped"):
                label += "  -> (already at max tier)"
            elif nxt:
                label += f"  -> tier {opt.get('move_tier_next')} [{nxt}]"
        # R7/N43. The stat block, on the same "port what the medium can carry"
        # rule `_stat_line` itself was built on -- the numbers are what make a
        # catch/swap/trade/release choice decidable, and they are numbers.
        # Indented onto its own continuation line because `render_pending`
        # prefixes the first one with the option index.
        stats = _stat_line(opt, include_move=False) if "effective_stats" in opt else ""
        if stats.strip():
            label += "\n    " + stats.strip()
        return label
    return str(opt)


def render_pending(state: engine.RunState) -> str:
    if state.pending is None:
        return ""
    lines = [f"-- choose ({state.phase.value}) --"]
    # R3. The phase name alone was not enough for every phase: the two
    # `REWARD_TEAM_PICK` branches print an IDENTICAL team list and mean
    # opposite things (release a member vs buff one, engine.py:3288-3299), so
    # the header was actively misleading rather than merely terse. The
    # contract's `pending.context` carries the source's own title/desc for
    # exactly the phases that have one; phases that don't are unchanged.
    view = contract.pending_view(state.pending, state)
    ctx = view["context"]
    if ctx["title"]:
        lines.append(f"  {ctx['title']}")
    if ctx["desc"]:
        lines.append(f"  {ctx['desc']}")
    # R6/N39. The OPTIONS come from the same projection as the context.
    #
    # This used to iterate `state.pending.options` -- the engine's own raw
    # dicts -- while taking `context` from the contract, so the console read
    # half of the projection and half of the producer. Every read-side option
    # enrichment therefore stopped at the browser: R3's `escape_rope`
    # `label`/`item_id` never reached this renderer (it only appeared to work
    # because `_format_option` carries a hard-coded default for that one
    # label), and R6's item `desc` and move-tutor `move_preview` would not
    # have either. That is precisely the drift `render.contract` exists to
    # make impossible, so the fix is to read the contract, not to re-enrich
    # here.
    for i, opt in enumerate(view["options"]):
        lines.append(f"  {i}: {_format_option(opt)}")
    if state.pending.optional:
        lines.append("  (or skip / decline)")
    return "\n".join(lines)


def _replay_roster(label: str, team: list) -> str:
    parts = []
    for i, view in enumerate(team):
        parts.append(f"{i}:{view['name']} {view['current_hp']}/{view['max_hp']}")
    return f"  {label}: " + (", ".join(parts) if parts else "(none)")


def render_battle_replay(state: engine.RunState) -> str:
    """The most recent battle, replayed turn by turn.

    R4. Both renderers finally consume `contract.battle_view`, which has
    carried this feed since R1 and been fully rostered since R2/N2 while being
    read by nothing. A text renderer's right ceiling is a sequential printout,
    not an animation: this module is deliberately plain-ASCII (see the module
    docstring), so the source's HP tweens, hit flashes and particle canvases
    have no expression here. What IS portable is the ORDER and the TEXT, and
    both come from `contract.battle_view`'s `replay` -- the same steps the
    browser client drains, so the two renderers cannot drift about what
    happened in a battle.

    Deviation, stated rather than hidden: the damage NUMBER is printed on every
    hit. The source only ever spawns a damage popup in Endless mode
    (`spawnDmgPopup`, bundle.deobfuscated.js:68511-68517), so a Story/Nuzlocke
    battle on the real site shows no number at all. Text is this renderer's
    only channel -- dropping the number would leave "took dmg." -- so the
    number stays and the divergence is declared here.
    """
    view = contract.battle_view(state)
    if view is None:
        return ""
    outcome = "WON" if view["player_won"] else "LOST"
    lines = [f"-- battle replay -- {outcome} in {view['rounds']} rounds"]
    lines.append(_replay_roster("start  player", view["player_team_start"]))
    lines.append(_replay_roster("start  enemy ", view["enemy_team_start"]))

    current_turn = object()  # a sentinel no `turn` value can equal
    for step in view["replay"]:
        if step["turn"] != current_turn:
            current_turn = step["turn"]
            # A status step carries `turn: None` on purpose -- the port's two
            # event streams cannot be interleaved (see `contract._replay_steps`),
            # so they are labelled honestly instead of being filed under a round
            # this renderer would be guessing at.
            header = f"  turn {current_turn}:" if current_turn is not None else "  post-turn effects:"
            lines.append(header)
        hp = ""
        if step["hp_after"] is not None and step["hp_max"]:
            frac = step["hp_after"] / step["hp_max"]
            hp = f"  {hp_bar(frac, 12)} {step['hp_after']}/{step['hp_max']}"
        lines.append(f"    {step['text']}{hp}")

    lines.append(_replay_roster("final  player", view["player_team"]))
    lines.append(_replay_roster("final  enemy ", view["enemy_team"]))
    # The source does exactly this: `animateBattleVisually` replays the log,
    # then `renderBattleField(Bch, BcL)` (bundle.deobfuscated.js:81278) redraws
    # from the real post-battle teams.
    #
    # M6/N10 closed the reason this used to matter most. Held-item recoil and
    # healing now emit their own `effect` records, so the replay accounts for
    # those HP changes instead of silently disagreeing with the final roster.
    # The redraw stays, because it is what the source does and because the
    # remaining unrecorded families (send-outs, transforms) can still move a
    # roster the replay never mentions.
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


def render_state(state: engine.RunState, *, recent_log: int = 1, battle_replay: bool = True) -> str:
    """The full snapshot: recent event(s), map, team, inventory, and
    whatever decision is currently pending. `recent_log` controls how many
    trailing `state.log` entries to show (default: just the latest).

    R4: when one of the shown entries is a `"battle"`, the turn-by-turn replay
    is printed under it. Gated on the LOG rather than on `state.last_battle`
    being non-None, because `last_battle` is replaced and never cleared
    (docs/renderer-contract.md section 2) -- keying off it directly would
    reprint the same battle on every subsequent step.

    R5/N14: `recent_log <= 0` means show NOTHING, not everything. `state.log[-0:]`
    is the whole list, which is the trap that made `play.run_episode` floor its
    delta at `max(1, ...)` -- and that floor is exactly what re-printed a
    finished battle on a zero-delta step. Handling 0 here is what lets the
    caller drop the floor without the slice silently dumping the entire run.
    """
    sections = [f"=== {state.phase.value} === (map {state.current_map}, badges {state.badges})"]
    if state.log and recent_log > 0:
        shown = state.log[-recent_log:]
        for entry in shown:
            sections.append(render_log_entry(entry))
        if battle_replay and any(e.get("type") == "battle" for e in shown):
            replay = render_battle_replay(state)
            if replay:
                sections.append(replay)
    sections.append(render_map(state))
    sections.append("Team:")
    sections.append(render_team(state))
    items = ", ".join(state.items) if state.items else "(none)"
    sections.append(f"Items: {items}")
    pending = render_pending(state)
    if pending:
        sections.append(pending)
    return "\n".join(sections)
