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

import pathlib
from typing import Optional

from pokelike import battle, data, engine, map_gen
from pokelike.battle import Combatant

# Bump on any change to the *shape* of what `observation()` returns: a field
# added, removed, renamed, or given a different type. Not bumped for a value
# changing. Independent of route-oracle SCHEMA.md's version 2.
#
# 3 (R3): `pending` gained `context` -- see `pending_view`.
# 4 (R4): `battle` gained `player_team_start`/`enemy_team_start` (the
#         pre-battle rosters a replay's first frame needs) and `replay` (the
#         ordered presentation steps both renderers drain) -- see `battle_view`.
# 5 (M6): `battle.turns[*].events` can now carry `effect` (N10: the four
#         post-hit held-item/passive HP changes) and `faint` (N11: the ordinary
#         combat KO) records, not `attack` alone, and `replay` therefore gained
#         `effect`/`faint` steps inside a turn. No key was added to any
#         existing record; this is a new member of an existing family, which is
#         still a shape change a renderer switches on -- see BATTLE_EVENT_TYPES.
CONTRACT_VERSION = 5

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
    # R2. `getNodeSprite` (bundle.deobfuscated.js:53944-54025) is ported and
    # its paths are computed faithfully, but the FILES are not in this mirror:
    # `pokelike_forked/img/sprites/` holds only `pokemon/`. A renderer that
    # cannot load one should fall back to `icon` + `color`, which are the
    # source's own other branch (54315-54348), not to a broken image.
    "node_sprite_assets",
    # R2. `getSilverHoverLabel` (54596-54643) swaps Silver's LAST previewed
    # team slot for the player's counter-starter line, resolved live through
    # `resolveEvoForLevel`. The engine already performs that swap at battle
    # time; re-deriving it inside a tooltip would be a second implementation of
    # a rule the port already owns. The previewed roster is the fixed table's.
    "silver_hover_starter_line",
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
    "sprite_url", "ability",
    # M6/N24: the two lines the source's hover card adds beneath the card
    # itself -- `hoverCritLine` (64491-64497) and `hoverAugmentLine`
    # (64498-64505). Both are conditional in the source, so both are carried
    # unconditionally here and the RENDERER applies the condition; that is the
    # same read-side rule every other view field follows.
    "crit_chance", "augment_pct",
})

NODE_FIELDS = frozenset({
    "id", "type", "layer", "col",
    "visited", "accessible", "revealed",
    "encounter",
    # R2: the source's own node presentation, ported. See `node_view`.
    "sprite_url", "sprite_size", "icon", "color", "tooltip",
    "clickable", "dimmed", "unexplored", "is_current", "pos",
})

#: R2: one drawn connection between two nodes, with the source's own stroke
#: semantics already decided. See `edge_view`.
EDGE_FIELDS = frozenset({"from", "to", "active", "both_visited", "color", "width", "dashed"})

MAP_FIELDS = frozenset({
    "map_index", "current_node_id", "is_sub_map",
    "nodes", "edges", "question_cache",
    # R2: the two constants a renderer needs to turn `node.pos` fractions into
    # pixels, carried so neither renderer hard-codes them independently.
    "edge_margin", "layer_count",
})

#: R2: the source's own presentation constants, ported from `renderMap`
#: (bundle.deobfuscated.js:54109-54142). Exported so a renderer de-normalizes
#: `node.pos` with the source's numbers rather than its own.
#:
#: `MAP_EDGE_MARGIN` is `B2Q` (54127); the default viewport is the
#: `|| 0x258` / `|| 0x1f4` fallback `renderMap` itself uses when the container
#: has not been laid out yet (54113-54114).
MAP_EDGE_MARGIN = 28
MAP_DEFAULT_WIDTH = 600
MAP_DEFAULT_HEIGHT = 500

#: Column spacing divisor, `B6o = B2e / (layer.length + 0.2)` (54133). The
#: 0.2 is the source's own, and is what keeps a wide layer inside the viewport.
_MAP_COLUMN_SLACK = 0.2

ITEM_FIELDS = frozenset({"id", "name", "desc", "icon", "icon_url", "usable", "known"})

#: R3. The pending-choice view. `context` is new in contract version 3; the
#: other three are R1's and unchanged.
PENDING_FIELDS = frozenset({"phase", "optional", "options", "context"})

#: R3. `context`'s own keys. ALWAYS all five, `None` where the phase has
#: nothing to say -- a renderer can read `pending.context.title` on any phase
#: without an existence check, and a phase losing its context fails a detector
#: rather than silently rendering an empty screen. See `pending_view`.
PENDING_CONTEXT_FIELDS = frozenset({"title", "desc", "kind", "subject", "team_index"})

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
#: `effect`/`faint` are new in contract version 5 (M6/N10/N11).
BATTLE_EVENT_TYPES = frozenset({"attack", "effect", "faint"})
STATUS_EVENT_TYPES = frozenset({"status_tick", "faint", "poison_drain"})

#: M6/N10. `battle_loop._effect_event`'s stable cause keys -> the source's own
#: `reason` prose, which is what `animateBattleVisually` writes to the log for
#: an `effect` record (bundle.deobfuscated.js:69374, `BcT(Bch["reason"],
#: "log-item")` -- the RECORD's reason, not the earlier inline `Bco(...)` line,
#: which differs for `life_orb`).
#:
#: `{name}` is the combatant label this layer joins from the roster and `{n}`
#: the absolute HP amount. The source interpolates both directly into the
#: string it stores; the port keeps the cause key on the record (so no
#: presentation reaches the engine) and rebuilds the same text here.
#: Sources, in order: 56380, 56398, 56417, 56440. The minus signs are the
#: source's own U+2212, not a hyphen.
_EFFECT_REASON_TEXT = {
    "rocky_helmet": "Rocky Helmet hurt {name} for {n} HP!",
    "enemy_recoil": "Iron Thorns: −{n} HP recoil",
    "life_orb": "Life Orb: −{n} HP recoil",
    "shell_bell": "Shell Bell restored {n} HP to {name}!",
}

#: R4. `battle_view`'s own top level. `player_team_start`/`enemy_team_start`
#: and `replay` are new in contract version 4.
BATTLE_FIELDS = frozenset({
    "rounds", "player_won", "turns", "status_events",
    "player_team", "enemy_team",
    "player_team_start", "enemy_team_start",
    "replay",
})

#: R4. One presentation step in `battle.replay` -- ALWAYS all of these keys,
#: `None` where the step kind has nothing to say, for the same reason
#: `pending.context` is always-all-five: a renderer reads `step.hp_after`
#: without an existence check, and a key vanishing fails a detector rather
#: than silently drawing nothing.
REPLAY_STEP_FIELDS = frozenset({
    "kind", "turn", "text", "cls",
    "side", "idx", "hp_after", "hp_max",
    "damage", "popup", "crit", "type_eff", "delay_ms",
})

#: R4. `spawnDmgPopup`'s own kind strings (bundle.deobfuscated.js:68517-68521)
#: plus the `log-*` classes `animateBattleVisually`'s appender is called with.
#: Pinned for the same reason the event types are: a renderer switches on them.
REPLAY_POPUP_KINDS = frozenset({"crit", "se", "nve", "normal", "heal"})
REPLAY_LOG_CLASSES = frozenset({"log-player", "log-enemy", "log-faint", "log-item"})


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


def _move_preview(mon: Combatant, move_tier: Optional[int] = None) -> Optional[dict]:
    """The single move this Pokemon would actually attack with, from
    `battle.get_best_move` -- the same call the battle loop makes. This is
    what a move-tutor card needs to show "current move -> move after
    tutoring" (CODEX gap 10), and what a hover card needs to explain damage.

    `move_tier` overrides the mon's own tier. R7/N45 needs exactly that: the
    tutor card must show what the Pokemon would attack with *after* tutoring,
    which is this same call one tier up. Nothing else about the mon changes,
    so the override is a parameter rather than a mutated copy.

    `battle.get_best_move` is a pure data lookup over the ported move table
    (`battle.py:343-421`): same arguments always yield the same move, with no
    RNG and no run state. That is what makes a *preview* honest rather than a
    guess -- see R7's record.

    Returns None if the move cannot be built, rather than a placeholder.
    """
    tier = mon.move_tier if move_tier is None else move_tier
    try:
        move = battle.get_best_move(
            mon.types, mon.base_stats, mon.species_id,
            tier, mon.held_item,
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


#: M6/N24. `_BASE_CRIT` (bundle.deobfuscated.js:64449), as a PERCENT -- the
#: source's hover card talks in percent, while `battle.py`'s damage path uses
#: the same numbers as fractions.
_BASE_CRIT_PCT = 6.25


def _crit_chance_pct(mon: Combatant, passives: Sequence) -> float:
    """Port of `currentCritChance` (bundle.deobfuscated.js:64450-64489), the
    number the hover card's crit line shows.

    This is deliberately a SEPARATE computation from the one inside
    `battle.calc_damage` (`battle.py:605-621`), because the source keeps them
    separate too: `currentCritChance` is a display helper that reads the run's
    collected passives and the mon's own held item, while the damage path also
    folds in per-battle config, `force_crit` and the overflow rule. Reusing
    the damage path here would report a number the player never sees.

    The `state.isEndlessMode` Dark-team branch (64462-64479) is not ported:
    this port has no Endless mode, so the branch is unreachable rather than
    omitted. Same for `battle_config.dark_crit_floor`, which is battle-local
    and has no value outside one.
    """
    pct = _BASE_CRIT_PCT
    if mon.held_item is not None and mon.held_item.id == "scope_lens":
        pct += 30
    if battle.has_passive(passives, "crit_overflow"):
        pct += 35
    if battle.has_passive(passives, "crit_lifesteal"):
        pct += 10
    if battle.has_passive(passives, "crit_boost"):
        pct += 10
    if battle.has_passive(passives, "crit_flinch"):
        pct += 10
    if battle.has_passive(passives, "dark_lvlcrit") and "Dark" in (mon.types or ()):
        pct += (mon.level or 0) / 1.5
    return min(100.0, pct)


def mon_view(mon: Combatant, passives: Sequence = ()) -> dict:
    """One team/enemy member, fully presented. Superset of the old
    `state_json._mon_json`: every field that one emitted is still here with
    the same name and type, so an existing client keeps working.

    `passives` is the run's collected trait list, needed only by
    `crit_chance`. It defaults to empty so that the battle-replay call sites,
    which project rosters rather than live run state, keep working unchanged --
    and a battle roster's crit line is not something the source shows anyway.
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
        # M6/N24. The hover card's two extra lines. Both are carried always and
        # the renderer applies the source's own conditions: the crit line is
        # shown only when the value differs from `_BASE_CRIT` by >= 0.01
        # (64494-64496), the augment line only when `_augmentPct` is truthy
        # (64500-64504).
        "crit_chance": _crit_chance_pct(mon, passives),
        "augment_pct": mon.augment_pct,
        # R2/N1: CODEX section 3 item 8 names `ability` among the hover-card
        # fields, and it is load-bearing in the battle engine (`battle.py:307`
        # smack_down, `battle.py:362` multitype), so a hover card that omits it
        # cannot explain an immunity the player just watched happen.
        #
        # This is the engine's OWN field, carried verbatim. It is battle-local:
        # `engine.py:1593-1596` records that it "only reflects whatever a
        # Traced battle last set it to and is otherwise unset outside battle"
        # (CODEX issue 20), so on a `state.team` member it is usually None.
        # Where it is live is on N2's battle rosters below -- which is exactly
        # where a replay needs it. Re-deriving a species ability here instead
        # would be inventing a value the engine does not hold.
        "ability": mon.gen3_ability,
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


# ---------------------------------------------------------------------------
# R2 -- the source's own node presentation, ported.
#
# Before R2 both renderers invented their own node symbols, colours and
# positions, and said so in a comment. The source decides all three in four
# traceable functions plus one loop, all cited below:
#
#   renderMap        bundle.deobfuscated.js:54109-54473  layout + edges + flags
#   getNodeSprite    bundle.deobfuscated.js:53944-54025  per-type sprite path
#   getNodeColor     bundle.deobfuscated.js:54540-54573  circle fill
#   getNodeIcon      bundle.deobfuscated.js:54574-54595  circle glyph
#   getNodeLabel     bundle.deobfuscated.js:54686-54824  hover text
#
# What is NOT portable, and is deliberately approximated instead:
#
#   * The hover text is composed as an HTML STRING by `getNodeLabel`, styled
#     inline. A Python contract that emitted HTML would be dictating markup to
#     the console renderer, so `_node_tooltip` carries the same CONTENT in a
#     structure (`title`/`notes`/`team`) and each renderer formats it. Every
#     string inside is the source's own.
#   * Pixel positions depend on the live container size, which only a browser
#     knows (`it.clientWidth`/`clientHeight`, 54113-54114). The layout maths is
#     ported exactly, but expressed as FRACTIONS -- see `_node_positions`.
#   * The node sprite FILES (`img/sprites/g1/grass.png` and friends) are not in
#     this mirror: `pokelike_forked/img/sprites/` contains only `pokemon/`.
#     The paths are still computed faithfully, so they resolve if the assets
#     are ever added; until then the web renderer falls back to the source's
#     own circle+icon presentation on image load error. See `UNSUPPLIED`.
# ---------------------------------------------------------------------------

#: The node types `renderMap` draws at "character" size (54186-54190).
_CHARACTER_NODE_TYPES = (
    map_gen.TRAINER, map_gen.BOSS, map_gen.SILVER, map_gen.MAGMA, map_gen.AQUA,
)

#: `getNodeIcon`'s glyph table (54576-54593). UNDERGROUND and DISTORTION are
#: genuinely absent from it in the source and fall through to the default.
_NODE_ICONS = {
    map_gen.START: "★",
    map_gen.BATTLE: "⚔",
    map_gen.CATCH: "⬟",
    map_gen.ITEM: "✦",
    map_gen.QUESTION: "?",
    map_gen.BOSS: "♛",
    map_gen.POKECENTER: "+",
    map_gen.TRAINER: "⚑",
    map_gen.LEGENDARY: "⚝",
    map_gen.MOVE_TUTOR: "♪",
    map_gen.TRADE: "⇄",
    map_gen.SILVER: "⚔",
    map_gen.MAGMA: "\U0001f525",
    map_gen.AQUA: "\U0001f30a",
    map_gen.REWARD: "\U0001f381",
    map_gen.SUBEXIT: "\U0001f6aa",
}
_NODE_ICON_DEFAULT = "●"

#: `getNodeColor`'s fill table (54547-54566).
_NODE_COLORS = {
    map_gen.START: "#4a4a6a",
    map_gen.BATTLE: "#6a2a2a",
    map_gen.CATCH: "#2a6a2a",
    map_gen.ITEM: "#2a4a6a",
    map_gen.QUESTION: "#6a4a2a",
    map_gen.BOSS: "#8a2a8a",
    map_gen.POKECENTER: "#006666",
    map_gen.TRAINER: "#6a3a1a",
    map_gen.LEGENDARY: "#7a6a00",
    map_gen.MOVE_TUTOR: "#3a4a6a",
    map_gen.TRADE: "#1a5a5a",
    map_gen.SILVER: "#5a2a7a",
    map_gen.MAGMA: "#a83218",
    map_gen.AQUA: "#1a5aa8",
    map_gen.UNDERGROUND: "#7a5a2a",
    map_gen.DISTORTION: "#5a2a7a",
    map_gen.REWARD: "#7a6a1a",
    map_gen.SUBEXIT: "#1a5a5a",
}
_NODE_COLOR_DEFAULT = "#444"

#: `getNodeLabel`'s plain per-type strings (54800-54821). The entries that are
#: computed rather than constant (TRAINER, SILVER, MAGMA, AQUA) are built in
#: `_node_tooltip` instead.
_NODE_LABELS = {
    map_gen.START: "Start",
    map_gen.BATTLE: "Wild Battle — +1 level",
    map_gen.CATCH: "Catch Pokemon",
    map_gen.ITEM: "Item",
    map_gen.QUESTION: "Random Event",
    map_gen.POKECENTER: "Pokemon Center",
    map_gen.LEGENDARY: "Legendary Pokemon",
    map_gen.MOVE_TUTOR: "Move Tutor",
    map_gen.TRADE: "Trade — swap a Pokémon for one 3 levels higher",
}

_POKEAPI_ITEM_BASE = (
    "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/"
)

_SHOWDOWN_TRAINER_BASE = "https://play.pokemonshowdown.com/sprites/trainers/"

# ---------------------------------------------------------------------------
# R7.1 -- local cache for the node art the source HOT-LINKS.
#
# The source fetches two families of node art from third-party hosts at render
# time: PokeAPI item icons (`_POKEAPI_ITEM`, 46499-46502) and Showdown trainer
# sprites (the submap-boss `sprite` fields and the gym-leader tables). Emitting
# those URLs verbatim made ordinary rendering depend on two remote hosts being
# reachable, which is wrong for an offline port and wrong for a training loop.
#
# R7.1 shipped a verified local cache of every such URL reachable on the
# Story/Nuzlocke Gen1-4 surface (16 of them: 14 item icons plus the two submap
# bosses). This maps a remote URL onto its cached path.
#
# **Only when the cache file actually exists.** An unconditional rewrite would
# turn a missing cache entry into a guaranteed 404, and it would also break the
# gym-leader Showdown URLs that this surface cannot reach (Gen2 map indices
# 9-17, which Story mode's nine maps never produce) and which therefore have no
# cache file. Falling back to the original URL keeps those honest instead of
# inventing a local path that was never fetched. The reachable set is not left
# to chance: `test_asset_existence` enumerates it and fails if any reachable
# projection still points at either remote host.
#
# This changes contract VALUES, not the contract SHAPE -- `sprite_url` is still
# one optional string. CONTRACT_VERSION stays at 5.
# ---------------------------------------------------------------------------
_REMOTE_TO_LOCAL_CACHE = {
    _POKEAPI_ITEM_BASE: "img/sprites/items/",
    _SHOWDOWN_TRAINER_BASE: "img/sprites/showdown/",
}

_STATIC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "webui" / "static"


def _local_cache_url(url: Optional[str]) -> Optional[str]:
    """Remote node-art URL -> its R7.1 local cache path, when one was fetched.

    Returns `url` unchanged for anything that is already local, is on a host
    with no cache, or has no cache file on disk.
    """
    if not url:
        return url
    for remote_base, local_dir in _REMOTE_TO_LOCAL_CACHE.items():
        if url.startswith(remote_base):
            candidate = local_dir + url[len(remote_base):]
            if (_STATIC_ROOT / candidate).is_file():
                return candidate
            return url
    return url


def _pokeapi_item(slug: str) -> str:
    """`_POKEAPI_ITEM` (bundle.deobfuscated.js:46499-46502), projected onto the
    R7.1 local cache when the icon was fetched."""
    return _local_cache_url(f"{_POKEAPI_ITEM_BASE}{slug}.png")


class NodeContext:
    """The `state` fields `getNodeSprite`/`getNodeColor`/`getNodeLabel` read as
    a global. Gathered into one object so node projection is a pure function of
    (node, context) and can be tested without building a whole `RunState`.

    `challenge_gen4` is fixed False: it is a Challenges-mode flag
    (`state.challengeGen4`, read at 53977) and `RunState` has none. In Story
    Gen4 the source takes the `getTrainerSpritePath` branch instead, which
    checks `gen4Mode` and reaches the same `img/sprites/g4/...` path -- so the
    two agree for every route this port can reach. Flagged rather than assumed.
    """

    __slots__ = (
        "nuzlocke_mode", "gen2_mode", "gen3_mode", "gen4_mode",
        "current_map", "current_node_id", "challenge_gen4",
    )

    def __init__(self, *, nuzlocke_mode=False, gen2_mode=False, gen3_mode=False,
                 gen4_mode=False, current_map=0, current_node_id=None):
        self.nuzlocke_mode = bool(nuzlocke_mode)
        self.gen2_mode = bool(gen2_mode)
        self.gen3_mode = bool(gen3_mode)
        self.gen4_mode = bool(gen4_mode)
        self.current_map = int(current_map)
        self.current_node_id = current_node_id
        self.challenge_gen4 = False

    @property
    def generation(self) -> int:
        """1-4, for indexing the ported per-generation trainer tables."""
        if self.gen4_mode:
            return 4
        if self.gen3_mode:
            return 3
        if self.gen2_mode:
            return 2
        return 1


def node_context(state: engine.RunState) -> NodeContext:
    return NodeContext(
        nuzlocke_mode=state.nuzlocke_mode,
        gen2_mode=state.gen2_mode,
        gen3_mode=state.gen3_mode,
        gen4_mode=state.gen4_mode,
        current_map=state.current_map,
        current_node_id=state.current_node_id,
    )


# ---------------------------------------------------------------------------
# Sprite
# ---------------------------------------------------------------------------

def _trainer_sprite_path(key: str, gen2: bool, ctx: NodeContext) -> str:
    """`getTrainerSpritePath(B, O)` (bundle.deobfuscated.js:53774-53791).

    `gen2` is the source's own SECOND parameter, not `ctx.gen2_mode` -- only
    the TRAINER branch passes it (53986), the sub-boss branch passes `false`
    (53981), so the two callers genuinely differ and the parameter is kept.
    """
    tables = data.get_node_presentation()
    if ctx.gen4_mode and key in tables["GEN4_SPRITE_FILENAME"]:
        return f"img/sprites/g4/{tables['GEN4_SPRITE_FILENAME'][key]}.png"
    if ctx.gen3_mode and key in tables["GEN3_SPRITE_FILENAME"]:
        return f"img/sprites/g3/{tables['GEN3_SPRITE_FILENAME'][key]}.png"
    if gen2 and key in tables["GEN2_SPRITE_FILENAME"]:
        return f"img/sprites/g2/{tables['GEN2_SPRITE_FILENAME'][key]}.png"
    return f"img/sprites/g1/{tables['SPRITE_FILE'].get(key, key)}.png"


def _boss_sprite(node: map_gen.MapNode, ctx: NodeContext) -> Optional[str]:
    """The ordinary (non-sub-boss) BOSS branch, bundle.deobfuscated.js:
    53988-54015. `isEndlessMode` is not modelled -- Story/Nuzlocke only."""
    tables = data.get_node_presentation()
    map_index = (node.extra or {}).get("mapIndex")
    idx = -1 if map_index is None else int(map_index)
    # Every table below may carry a hot-linked Showdown URL, so the whole
    # branch result is projected through the R7.1 local cache on the way out.
    # The reachable Story/Nuzlocke values (map indices 0-8) are already local
    # paths in the ported tables; the remote ones are the Kanto gym/`red`
    # entries that only Gen2 indices 9-17 select, which this surface cannot
    # generate -- those stay remote and are proved unreachable by the detector.
    return _local_cache_url(_boss_sprite_raw(tables, idx, ctx))


def _boss_sprite_raw(tables, idx: int, ctx: NodeContext) -> Optional[str]:
    """The branch logic exactly as the source writes it, before R7.1's local
    cache projection. Split out so the ported branch structure stays readable
    and directly comparable to 53988-54015, and so a test can assert the
    pre-cache value independently of what happens to be on disk."""
    if ctx.gen2_mode:
        if idx == 17:
            return "https://play.pokemonshowdown.com/sprites/trainers/red.png"
        if idx == 8:
            return "img/sprites/g2/lance.png"
        if 9 <= idx < 17:
            return tables["KANTO_GYM_SHOWDOWN_SPRITES"][idx - 9]
        if 0 <= idx < 8:
            return tables["JOHTO_GYM_LEADER_SPRITES"][idx]
    if ctx.gen3_mode:
        hoenn = tables["HOENN_GYM_SHOWDOWN_SPRITES"]
        if idx == 8 or not (0 <= idx < len(hoenn)):
            return "img/sprites/g3/steven.png"
        return hoenn[idx]
    if ctx.gen4_mode:
        sinnoh = tables["SINNOH_GYM_SHOWDOWN_SPRITES"]
        if idx == 8 or not (0 <= idx < len(sinnoh)):
            return tables["SINNOH_CHAMPION_SPRITE"]
        return sinnoh[idx]
    kanto = tables["KANTO_GYM_LEADER_SPRITES"]
    if 0 <= idx < len(kanto):
        return kanto[idx]
    return "img/sprites/g1/champ.png"


def _node_sprite(node: map_gen.MapNode, ctx: NodeContext) -> Optional[str]:
    """`getNodeSprite` (bundle.deobfuscated.js:53944-54025), branch for branch
    and in the source's own order -- the per-type table is consulted only
    AFTER the REWARD/SUBEXIT/sub-boss special cases (53967-53983).

    Returns None only for START (and for MAGMA/AQUA with no encounter sprite),
    which is exactly when the source falls through to its circle+icon
    presentation at 54315-54348.
    """
    extra = node.extra or {}
    per_type = {
        map_gen.BATTLE: (
            "img/sprites/g3/grass.png" if ctx.gen3_mode
            else "img/sprites/g2/grass.png" if ctx.gen2_mode
            else "img/sprites/g1/grass.png"
        ),
        map_gen.CATCH: (
            "img/sprites/g2/pokeball.png" if ctx.gen2_mode
            else "img/sprites/g1/pokeball.png"
        ),
        map_gen.ITEM: "img/sprites/item-icon.png",
        map_gen.TRADE: "img/sprites/trade-icon.png",
        map_gen.LEGENDARY: "img/sprites/legendary-encounter.png",
        map_gen.QUESTION: "img/sprites/question-mark.png",
        map_gen.POKECENTER: "img/sprites/poke-center.png",
        map_gen.MOVE_TUTOR: "img/sprites/move-tutor.png",
        map_gen.UNDERGROUND: _pokeapi_item("explorer-kit"),
        map_gen.DISTORTION: _pokeapi_item("odd-keystone"),
    }

    if node.type == map_gen.REWARD:
        reward = data.get_submap_reward_by_id().get(extra.get("reward"))
        return _pokeapi_item(reward.sprite) if reward else "img/sprites/item-icon.png"
    if node.type == map_gen.SUBEXIT:
        return _pokeapi_item("escape-rope")
    if node.type == map_gen.BOSS and extra.get("subBoss"):
        key = extra.get("trainerKey")
        if key:
            return _trainer_sprite_path(key, False, ctx)
        # `bossSprite` comes straight from `submap_bosses.json`, whose `sprite`
        # fields are the source's own hot-linked Showdown URLs. R7.1 projects
        # the two reachable ones (`ruinmaniac`, `cyrus`) onto their local cache.
        return _local_cache_url(extra.get("bossSprite")) or "img/sprites/mistery-trainer.png"
    if node.type in per_type:
        return per_type[node.type]
    if node.type == map_gen.TRAINER:
        return _trainer_sprite_path(extra.get("trainerSprite") or "aceTrainer", ctx.gen2_mode, ctx)
    if node.type == map_gen.BOSS:
        return _boss_sprite(node, ctx)
    if node.type == map_gen.SILVER:
        return "img/sprites/g2/silver.png"
    if node.type in (map_gen.MAGMA, map_gen.AQUA):
        table = data.get_magma_encounters() if node.type == map_gen.MAGMA else data.get_aqua_encounters()
        entry = table.get(ctx.current_map) or table.get(2)
        if entry is not None and entry.sprite:
            return entry.sprite
    return None


def _node_sprite_size(node: map_gen.MapNode, ctx: NodeContext) -> dict:
    """`renderMap`'s per-type sprite box (bundle.deobfuscated.js:54192-54200)
    plus the circle radius its no-sprite branch uses (54316).

    The sprite is drawn centred, at `(-w/2, -h/2)` (54201-54209), so a renderer
    needs only the box. `circle_radius` travels in the same field because it is
    the same decision -- how big this node is -- taken on the other branch.
    """
    is_boss = node.type == map_gen.BOSS
    if node.type == map_gen.ITEM:
        width, height = 30, 42
    elif node.type in _CHARACTER_NODE_TYPES:
        width = 52 if is_boss else 46 if ctx.gen4_mode else 48 if ctx.gen3_mode else 38
        height = 52 if (is_boss or ctx.gen4_mode) else 48 if ctx.gen3_mode else 52
    else:
        width = height = 52 if is_boss else 40
    return {"w": width, "h": height, "circle_radius": 22 if is_boss else 18}


def _node_icon(node: map_gen.MapNode) -> str:
    """`getNodeIcon` (bundle.deobfuscated.js:54574-54595)."""
    if node.visited:
        return "✓"
    return _NODE_ICONS.get(node.type, _NODE_ICON_DEFAULT)


def _node_color(node: map_gen.MapNode, ctx: NodeContext) -> str:
    """`getNodeColor` (bundle.deobfuscated.js:54540-54573).

    The START test comes BEFORE the `visited` test in the source (54542 vs
    54546), so a visited START keeps its mode-specific colour instead of going
    grey. Preserved deliberately -- it is the source's behaviour, not a slip.
    """
    if node.type == map_gen.START:
        return "#6a4050" if ctx.nuzlocke_mode else "#3a4566"
    if node.visited:
        return "#333"
    if node.type == map_gen.BOSS and (node.extra or {}).get("subBoss"):
        return "#4a2a6a" if node.extra["subBoss"] == "distortion" else "#6a4a1a"
    return _NODE_COLORS.get(node.type, _NODE_COLOR_DEFAULT)


# ---------------------------------------------------------------------------
# Tooltip
# ---------------------------------------------------------------------------

def _species_name(species_id) -> str:
    entry = data.get_pokedex().get(species_id)
    return entry.name if entry is not None else f"#{species_id}"


def _tooltip(title: str, notes=(), team=()) -> dict:
    return {"title": title, "notes": list(notes), "team": list(team)}


def _team_entries(members) -> list:
    """`{name, level}` pairs, from whichever key shape the caller's table uses.
    Submap boss teams carry `species_id`, fixed rosters carry `name`."""
    out = []
    for member in members or ():
        if isinstance(member, dict):
            name = member.get("name") or _species_name(member.get("species_id"))
            level = member.get("level")
        else:
            name = getattr(member, "name", None) or _species_name(getattr(member, "species_id", None))
            level = getattr(member, "level", None)
        out.append({"name": name, "level": level})
    return out


def _trainer_tooltip(node: map_gen.MapNode, ctx: NodeContext) -> dict:
    """The TRAINER entry of `getNodeLabel`'s table (54806-54811) with its
    specialty lookup (54784-54797)."""
    tables = data.get_node_presentation()
    key = (node.extra or {}).get("trainerSprite")
    display = tables["TRAINER_SPRITE_NAMES"].get(key) if key else None
    if not display:
        return _tooltip("Trainer Battle — +2 Levels")
    per_gen = {
        4: "TRAINER_SPECIALTIES_GEN4",
        3: "TRAINER_SPECIALTIES_GEN3",
        2: "TRAINER_SPECIALTIES_GEN2",
    }.get(ctx.generation)
    specialty = None
    if per_gen:
        specialty = tables[per_gen].get(key)
    if specialty is None:
        specialty = tables["TRAINER_SPECIALTIES"].get(key, "Various Pokemon")
    return _tooltip(f"{display} — +2 Levels — {specialty}")


def _silver_tooltip(ctx: NodeContext) -> dict:
    """`getSilverHoverLabel` (bundle.deobfuscated.js:54596-54643), minus its
    starter-line substitution: that reads `state.starterSpeciesId` and the
    live evolution resolver to swap Silver's LAST team slot (54610-54627). The
    swap is gameplay the engine already performs at battle time
    (`engine._silver_encounter_index`); duplicating the resolver here to
    preview it would be a second implementation of a rule the port already
    owns, so the previewed roster is the fixed table's. Recorded in UNSUPPLIED.
    """
    stage = {1: 0, 3: 1, 5: 2, 7: 3}.get(ctx.current_map, 0)
    encounters = data.get_silver_encounters()
    entry = encounters[min(stage, len(encounters) - 1)]
    notes = ["+4 Levels (Double XP)", "Heals you after battle"]
    if ctx.nuzlocke_mode:
        notes.append("No Perma-Death")
    return _tooltip("Rival Silver", notes, _team_entries(entry.team))


def _admin_tooltip(node: map_gen.MapNode, ctx: NodeContext) -> dict:
    """`getAdminHoverLabel` (bundle.deobfuscated.js:54644-54685)."""
    is_magma = node.type == map_gen.MAGMA
    side = "Team Magma" if is_magma else "Team Aqua"
    table = data.get_magma_encounters() if is_magma else data.get_aqua_encounters()
    entry = table.get(ctx.current_map) or table.get(2)
    if entry is None:
        return _tooltip(side)
    notes = ["+4 Levels", "Heals you after battle"]
    if ctx.nuzlocke_mode:
        notes.append("No Perma-Death")
    return _tooltip(f"{entry.name} — {side}", notes, _team_entries(entry.team))


def _boss_tooltip(node: map_gen.MapNode, ctx: NodeContext) -> dict:
    """The ordinary BOSS branch of `getNodeLabel` (54730-54780)."""
    map_index = (node.extra or {}).get("mapIndex")
    idx = -1 if map_index is None else int(map_index)
    leaders = data.get_gym_leaders(ctx.generation)
    if leaders and 0 <= idx < len(leaders):
        leader = leaders[idx]
        return _tooltip(f"{leader.name} — {leader.type} Gym", (), _team_entries(leader.team))
    if idx == 8:
        return _tooltip({
            2: "Elite Four & Lance",
            3: "Elite Four & Champion Steven",
            4: "Elite Four & Champion Cynthia",
        }.get(ctx.generation, "Elite Four & Champion"))
    return _tooltip("Gym Leader")


def _node_tooltip(node: map_gen.MapNode, ctx: NodeContext) -> dict:
    """`getNodeLabel` (bundle.deobfuscated.js:54686-54824), as structure rather
    than the source's inline-styled HTML string. Branch order is the source's.
    """
    extra = node.extra or {}
    if node.visited:
        return _tooltip("Visited")
    if node.type == map_gen.BOSS and extra.get("subBoss"):
        where = "Distortion World" if extra["subBoss"] == "distortion" else "Sinnoh Underground"
        return _tooltip(
            f"{extra.get('bossName') or 'Boss'} — {where}",
            ["Defeat them to claim a reward"],
            _team_entries(extra.get("bossTeam")),
        )
    if node.type == map_gen.REWARD:
        reward = data.get_submap_reward_by_id().get(extra.get("reward"))
        return _tooltip(reward.label, [reward.desc]) if reward else _tooltip("Reward")
    if node.type == map_gen.SUBEXIT:
        return _tooltip("Exit", ["Return to where you left the map"])
    if node.type == map_gen.BOSS:
        return _boss_tooltip(node, ctx)
    if node.type == map_gen.TRAINER:
        return _trainer_tooltip(node, ctx)
    if node.type == map_gen.SILVER:
        return _silver_tooltip(ctx)
    if node.type in (map_gen.MAGMA, map_gen.AQUA):
        return _admin_tooltip(node, ctx)
    if node.type == map_gen.UNDERGROUND:
        return _tooltip("Sinnoh Underground", ["Beat a boss, claim a reward, then return"])
    if node.type == map_gen.DISTORTION:
        return _tooltip("Distortion World", ["Beat a boss, claim a reward, then return"])
    return _tooltip(_NODE_LABELS.get(node.type, node.type))


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _node_positions(gmap: map_gen.GeneratedMap) -> dict:
    """`renderMap`'s layout loop (bundle.deobfuscated.js:54126-54142), exactly,
    but normalized so it does not need a viewport.

    The source computes, for layer `i` of `n` and node `j` of `m` in it:

        y = n > 1 ? 28 + (i / (n - 1)) * (H - 2 * 28) : H / 2      (54132)
        spacing = W / (m + 0.2)                                     (54133)
        x = m === 1 ? W / 2 : W / 2 + (j - (m - 1) / 2) * spacing   (54135-54138)

    `x` is a pure multiple of `W`, so `x_frac = x / W` is exact and
    viewport-free. `y` is not -- the 28px margin is absolute -- so what
    travels is `y_frac`, the source's own `i / (n - 1)`, with `None` meaning
    the single-layer "centre it" case. `node_pixel_position` below is the one
    place that turns the pair back into pixels; both renderers call it (the web
    one through the same two lines, mirrored in JS with this citation) rather
    than re-deriving the maths.

    Note the source indexes by POSITION IN THE LAYER ARRAY (`B6W`), not by
    `node.col`. They agree for every map `map_gen` builds -- `_make_node` is
    called with `col` equal to the enumeration index in both `generate_map`
    (map_gen.py:449-451) and `generate_sub_map` (map_gen.py:1095-1136) -- but
    the array index is what is ported, because that is what the source reads.
    """
    layer_count = len(gmap.layers)
    positions = {}
    for layer_index, layer in enumerate(gmap.layers):
        y_frac = (layer_index / (layer_count - 1)) if layer_count > 1 else None
        width = len(layer)
        spacing = 1.0 / (width + _MAP_COLUMN_SLACK)
        for index_in_layer, node in enumerate(layer):
            if width == 1:
                x_frac = 0.5
            else:
                x_frac = 0.5 + (index_in_layer - (width - 1) / 2) * spacing
            positions[node.id] = {
                "x_frac": x_frac,
                "y_frac": y_frac,
                "layer_index": layer_index,
                "index_in_layer": index_in_layer,
                "layer_size": width,
            }
    return positions


def node_pixel_position(pos: dict, width: int, height: int) -> tuple:
    """`(x, y)` in pixels for a viewport, from a node view's `pos`.

    The inverse of `_node_positions`'s normalization, and the only place the
    28px margin is applied. Pass the container's real size; the source's own
    fallbacks (600x500, 54113-54114) are exported as `MAP_DEFAULT_*`.
    """
    x = pos["x_frac"] * width
    if pos["y_frac"] is None:
        y = height / 2
    else:
        y = MAP_EDGE_MARGIN + pos["y_frac"] * (height - 2 * MAP_EDGE_MARGIN)
    return x, y


def edge_view(src: map_gen.MapNode, dst: map_gen.MapNode) -> dict:
    """One connection, with `renderMap`'s own stroke decisions
    (bundle.deobfuscated.js:54143-54162) already taken.

    `active` is the source's `BcL`: BOTH endpoints visited-or-accessible. That
    is what thickens the line to 2.5 and removes the "4,5" dash pattern; a line
    is dashed exactly when it is not active (54160). `both_visited` (`Bch`,
    54149) darkens it further, to `#333` -- a travelled path reads as *behind*
    you, not ahead.
    """
    both_visited = bool(src.visited and dst.visited)
    active = bool((src.visited or src.accessible) and (dst.visited or dst.accessible))
    return {
        "from": src.id,
        "to": dst.id,
        "both_visited": both_visited,
        "active": active,
        "color": "#333" if both_visited else "#999" if active else "#222",
        "width": 2.5 if active else 1.5,
        "dashed": not active,
    }


def node_view(node: map_gen.MapNode, ctx: Optional[NodeContext] = None,
              pos: Optional[dict] = None) -> dict:
    """One map node, fully presented. Superset of R1's eight fields.

    `clickable`/`dimmed`/`unexplored` are `renderMap`'s own three node-state
    flags (`BcH`/`BcC`, 54172-54181) rather than re-derivations: a node is
    clickable when accessible AND NOT visited -- not merely accessible -- and
    a visited node is drawn dimmed (`brightness(0.72)`, 54181) while an
    unreached one is drawn at `opacity 0.75` (54180). Both renderers had
    invented their own, differing, rules for this before R2.
    """
    ctx = ctx or NodeContext()
    return {
        "id": node.id,
        "type": node.type,
        "layer": node.layer,
        "col": node.col,
        "visited": node.visited,
        "accessible": node.accessible,
        "revealed": node.revealed,
        "encounter": _encounter_hint(node),
        "sprite_url": _node_sprite(node, ctx),
        "sprite_size": _node_sprite_size(node, ctx),
        "icon": _node_icon(node),
        "color": _node_color(node, ctx),
        "tooltip": _node_tooltip(node, ctx),
        "clickable": bool(node.accessible and not node.visited),
        "dimmed": bool(node.visited),
        "unexplored": bool(not node.accessible and not node.visited),
        "is_current": node.id == ctx.current_node_id,
        "pos": pos,
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
    """The whole map, laid out and presented.

    R2 changed `edges` from `[[from, to], ...]` to a list of `edge_view`
    objects. That is a shape change, hence `CONTRACT_VERSION = 2` -- both
    endpoints are still the first two fields (`from`/`to`), but a client that
    unpacked the pair positionally must now read them by name.
    """
    if state.map is None:
        return None
    ctx = node_context(state)
    positions = _node_positions(state.map)
    nodes = state.map.nodes
    return {
        "map_index": state.map.map_index,
        "current_node_id": state.current_node_id,
        "is_sub_map": state.map.is_sub_map,
        "nodes": [node_view(n, ctx, positions.get(n.id)) for n in nodes.values()],
        "edges": [
            edge_view(nodes[src], nodes[dst])
            for src, dst in state.map.edges
            if src in nodes and dst in nodes
        ],
        "question_cache": _resolved_question_marks(state),
        "edge_margin": MAP_EDGE_MARGIN,
        "layer_count": len(state.map.layers),
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


#: R4. `animateBattleVisually`'s own per-step pauses, in milliseconds, exactly
#: as the source spells them (hex in the bundle, decimal here). Each is passed
#: to its local `BcF(ms)`, which is
#: `new Promise(r => setTimeout(r, ms / battleSpeedMultiplier))`
#: (bundle.deobfuscated.js:69109-69111) -- so these are the 1x-speed values and
#: a renderer applies its own speed divisor, the way the source's Skip button
#: does (`battleSpeedMultiplier = SKIP_SPEED`, 3, at 81257).
#:
#: They travel on the step rather than being hard-coded in each renderer for
#: the reason the whole contract exists: `console.py` and `app.js` disagreeing
#: about pacing is the same drift class as disagreeing about a field name.
_REPLAY_DELAY_MS = {
    # The post-log-line pause after an `attack` (69323, `await BcF(0x64)`).
    "attack": 100,
    # `await BcF(0x12c)` after a faint (69398).
    "faint": 300,
    # M6/N10. `await BcF(0x64)` closing the `effect` branch (69375).
    "effect": 100,
    # Every `status_tick` branch converges on `await BcF(0x64)` (69676).
    "status_tick": 100,
    # `poison_drain` has no branch of its own in the source's animation -- see
    # `_replay_steps`. Paced as the status tick it accompanies.
    "poison_drain": 100,
}


def _effectiveness_suffix(type_eff, crit: bool) -> str:
    """`animateBattleVisually`'s own suffix chain (bundle.deobfuscated.js:
    69301-69307), in its exact order and with its exact strings.

    Note this is the ANIMATION's version, which differs from the one
    `runBattle` builds inline while resolving (55960-55976): only this one
    appends `" Critical hit!"`. The animation's is the right source for a
    replay -- it is the text the site shows while replaying, and the inline
    one is built during resolution, before any of this is drawn.
    """
    suffix = ""
    if type_eff >= 2:
        suffix = " Super effective!"
    elif type_eff == 0:
        suffix = " No effect!"
    elif type_eff < 1:
        suffix = " Not very effective..."
    if crit:
        suffix += " Critical hit!"
    return suffix


def _popup_kind(damage, type_eff, crit: bool) -> Optional[str]:
    """`spawnDmgPopup`'s `kind` argument as the attack branch computes it
    (bundle.deobfuscated.js:69274-69281), guarded by the same `damage > 0`
    test (69273). `None` means the source spawns no popup at all.

    Deliberately NOT gated on the source's own `state.isEndlessMode` guard
    (68511-68517) -- see `battle_view`'s docstring for why that guard is
    reported rather than reproduced.
    """
    if not damage or damage <= 0:
        return None
    if crit:
        return "crit"
    if type_eff >= 2:
        return "se"
    if type_eff < 1:
        return "nve"
    return "normal"


def hp_bar_color(fraction: float) -> str:
    """`hpBarColor` (bundle.deobfuscated.js:64134-64137), verbatim. The three
    thresholds and the three hex values are the source's own; `renderHpBar`
    (64138-64150) pairs them with a `Math.floor(frac * 100)%` width, which is
    what `_replay_steps` leaves to the renderer since only a pixel renderer
    can use it.
    """
    if fraction > 0.5:
        return "#00FF4A"
    if fraction > 0.1:
        return "#EAFF00"
    return "#FF0000"


def _combatant_label(rosters: dict, side, idx) -> str:
    """The name an `attack`/`faint` record's `side` + index points at.

    This is the join the contract's section 2 rule names explicitly: the
    record carries no `attacker_name`, so the renderer resolves it against the
    roster in the SAME observation rather than the record growing a key.
    Falls back to a positional label if a roster is short or absent, because a
    replay must degrade rather than crash -- the same principle `fold_turns`
    applies to a malformed feed.
    """
    team = rosters.get(side) or []
    if isinstance(idx, int) and 0 <= idx < len(team):
        view = team[idx]
        return view.get("nickname") or view.get("name") or f"{side} #{idx}"
    return f"{side} #{idx}"


def _combatant_max_hp(rosters: dict, side, idx):
    team = rosters.get(side) or []
    if isinstance(idx, int) and 0 <= idx < len(team):
        return team[idx].get("max_hp")
    return None


def _replay_step(kind, *, turn=None, text="", cls=None, side=None, idx=None,
                 hp_after=None, hp_max=None, damage=None, popup=None,
                 crit=False, type_eff=None) -> dict:
    """One `REPLAY_STEP_FIELDS` record. Always every key -- see that set."""
    return {
        "kind": kind,
        "turn": turn,
        "text": text,
        "cls": cls,
        "side": side,
        "idx": idx,
        "hp_after": hp_after,
        "hp_max": hp_max,
        "damage": damage,
        "popup": popup,
        "crit": bool(crit),
        "type_eff": type_eff,
        "delay_ms": _REPLAY_DELAY_MS.get(kind, 100),
    }


#: R4. `animateBattleVisually`'s `status_tick` sub-branches, keyed by the
#: record's own `status` string, carrying (popup text, whether the branch
#: moves an HP bar). Sourced one-for-one from bundle.deobfuscated.js:
#: burn 69552-69579, poison 69580-69609, freeze_thaw 69610-69617,
#: freeze_shatter 69618-69634, freeze_skip 69635-69642, sleep_skip 69643-69653,
#: sleep_wake 69654-69668. `flinch` reaches this projection from the port's own
#: `_pre_turn_tick` (battle_loop.py:171-190) but has NO branch in the source's
#: animation -- it falls through to the shared trailing `BcF(0x64)` and shows
#: nothing, which is why its popup text is None rather than invented.
_STATUS_TICK_PRESENTATION = {
    "burn": ("Burn!", True),
    "poison": ("Poison!", True),
    "freeze_thaw": ("Thawed!", False),
    "freeze_shatter": ("Shattered!", False),
    "freeze_skip": (None, False),
    "sleep_skip": ("Asleep", False),
    "sleep_wake": ("Woke up!", False),
    "flinch": (None, False),
}


def _replay_steps(turns: list, status_events: list, rosters: dict) -> list:
    """The ordered presentation sequence both renderers drain.

    Pure enrichment, in the precise sense docs/renderer-contract.md section 2
    requires: it READS the `attack`/`status_tick`/`faint`/`poison_drain`
    records and joins them against the rosters in the same observation. It adds
    no key to any record -- `turns[*].events[*]` still projects `battle_loop`'s
    shape byte for byte, and this is a sibling list, exactly the way R2/N2's
    `player_team`/`enemy_team` are siblings rather than record fields.

    **Declared limitation, not an oversight.** `battle_events` and
    `status_events` are two separate streams and only the first carries
    `turn_start` markers, so a status tick CANNOT be attributed to the round it
    fired in. The source has no such problem: it animates one interleaved
    `detailedLog` (bundle.deobfuscated.js:69113-69116 walks a single array).
    Attributing them here would mean adding a round marker to the status
    stream, which is the oracle's compared surface and forbidden by section 2 /
    the R4 brief's section 3. So status steps are appended after the turn steps
    in their own stream order and carry `turn: None`, and the renderers label
    them as post-turn effects rather than implying a round they cannot know.
    """
    steps: list = []
    for turn in turns:
        number = turn.get("turn")
        for event in turn.get("events") or []:
            kind = event.get("type")
            if kind == "effect":
                # M6/N10. 69352-69375: popup (only when `hpChange` is truthy,
                # "heal" when positive and "normal" otherwise), HP bar animated
                # to `hpAfter`, then the reason line at `log-item`.
                side = event.get("side")
                idx = event.get("idx")
                change = event.get("hp_change") or 0
                template = _EFFECT_REASON_TEXT.get(event.get("reason"))
                text = (
                    template.format(name=_combatant_label(rosters, side, idx), n=abs(change))
                    if template else str(event.get("reason"))
                )
                steps.append(_replay_step(
                    "effect", turn=number, text=text, cls="log-item",
                    side=side, idx=idx,
                    hp_after=event.get("hp_after"),
                    hp_max=_combatant_max_hp(rosters, side, idx),
                    damage=change or None,
                    popup=(("heal" if change > 0 else "normal") if change else None),
                ))
                continue
            if kind == "faint":
                # M6/N11. The ordinary combat KO, presented exactly like the
                # status-tick faint below -- 69389-69402 is ONE branch in the
                # source's animation and does not care which stream the record
                # reached it on.
                side = event.get("side")
                idx = event.get("idx")
                steps.append(_replay_step(
                    "faint", turn=number,
                    text=f"{_combatant_label(rosters, side, idx)} fainted!",
                    cls="log-faint", side=side, idx=idx, hp_after=0,
                    hp_max=_combatant_max_hp(rosters, side, idx),
                ))
                continue
            if kind != "attack":
                # Unknown/new event family: show it rather than dropping it
                # silently, and let `BATTLE_EVENT_TYPES`' detector be the thing
                # that fails loudly.
                steps.append(_replay_step(
                    str(kind), turn=number,
                    text=str(kind), cls="log-item",
                    side=event.get("side"), idx=event.get("idx"),
                ))
                continue
            side = event.get("side")
            target_side = event.get("target_side")
            attacker = _combatant_label(rosters, side, event.get("attacker_idx"))
            target = _combatant_label(rosters, target_side, event.get("target_idx"))
            damage = event.get("damage")
            type_eff = event.get("type_eff")
            crit = bool(event.get("crit"))
            # bundle.deobfuscated.js:69309-69322 -- the "(enemy) " prefix, the
            # arrow, the " dmg." and the suffix are all the source's own.
            prefix = "" if side == "player" else "(enemy) "
            steps.append(_replay_step(
                "attack", turn=number,
                text=(f"{prefix}{attacker} used {event.get('move_name')}"
                      f" → {target} took {damage} dmg."
                      f"{_effectiveness_suffix(type_eff, crit)}"),
                cls="log-player" if side == "player" else "log-enemy",
                # The HP bar this step moves is the TARGET's -- 69286-69296
                # animates `Bcg` (the target element) from its tracked current
                # value to `targetHpAfter`.
                side=target_side, idx=event.get("target_idx"),
                hp_after=event.get("target_hp_after"),
                hp_max=_combatant_max_hp(rosters, target_side, event.get("target_idx")),
                damage=damage, popup=_popup_kind(damage, type_eff, crit),
                crit=crit, type_eff=type_eff,
            ))
    for event in status_events:
        kind = event.get("type")
        side = event.get("side")
        idx = event.get("idx")
        name = _combatant_label(rosters, side, idx)
        if kind == "faint":
            # 69389-69402: `<name> fainted!`, class `log-faint`.
            steps.append(_replay_step(
                "faint", text=f"{name} fainted!", cls="log-faint",
                side=side, idx=idx, hp_after=0,
                hp_max=_combatant_max_hp(rosters, side, idx),
            ))
            continue
        if kind == "poison_drain":
            # No branch of its own in the source's animation -- the port emits
            # it (battle_loop.py:1322-1328) as the `poison_drain` passive's
            # heal, and the source's nearest presentation is `spawnDmgPopup`'s
            # "heal" kind (68519). Named as a heal here, not as a status tick.
            change = event.get("hp_change")
            steps.append(_replay_step(
                "poison_drain", text=f"{name} drained {change} HP.",
                cls="log-item", side=side, idx=idx,
                hp_after=event.get("hp_after"),
                hp_max=_combatant_max_hp(rosters, side, idx),
                damage=change, popup="heal",
            ))
            continue
        status = event.get("status")
        popup_text, _moves_bar = _STATUS_TICK_PRESENTATION.get(status, (None, False))
        change = event.get("hp_change") or 0
        label = popup_text or str(status)
        text = f"{name}: {label}"
        if change:
            text += f" ({change:+d} HP)"
        steps.append(_replay_step(
            "status_tick", text=text, cls="log-item", side=side, idx=idx,
            hp_after=event.get("hp_after"),
            hp_max=_combatant_max_hp(rosters, side, idx),
            damage=change or None,
            popup="burn" if status == "burn" else ("poison" if status == "poison" else None),
        ))
    return steps


def battle_view(state: engine.RunState) -> Optional[dict]:
    """The most recent battle as a turn-by-turn replay, or None if no battle
    has resolved yet this run. This is what R4's animation track consumes.

    Every record is copied on the way out: a renderer holding this must not be
    able to mutate engine state through it.

    **R4's model, and it is the source's own.** `runBattleScreen` resolves the
    WHOLE battle first (`runBattle`, bundle.deobfuscated.js:81208-81222) and
    only then replays the finished `detailedLog` through
    `animateBattleVisually` (81272). The animation therefore never feeds back
    into resolution -- it is a pure replay of an already-fixed sequence, which
    is exactly why a renderer can pace it however it likes without touching
    `Engine.step`.

    **Two source behaviors reported rather than reproduced**, both confirmed by
    direct reading and both affecting what a faithful replay should show:

    1. The site's in-battle TEXT log is dead code in this mirror. The log
       container `animateBattleVisually` appends to is `const B2V = null`
       (69084) and its appender opens with `if (!B2V) return;` (69102), so
       every one of its ~12 log calls is a no-op. The strings are still built,
       and they are the only battle presentation a plain-ASCII renderer can
       show at all, so `replay`'s `text` carries them -- but the live site
       shows HP bars, popups and CSS classes here, not a scrolling log.
    2. Damage NUMBERS are Endless-mode-only. `spawnDmgPopup` returns early
       unless `state.isEndlessMode` (68511-68517), so Story/Nuzlocke -- this
       port's entire scope -- shows no damage number on the field. `popup`
       therefore carries the source's computed KIND, and it is the renderer's
       call whether to draw it; `console.py` shows the number because its only
       channel is text, and that is a documented deviation, not a port of the
       Endless branch.
    """
    feed = state.last_battle
    if not feed:
        return None
    # R2/N2: the rosters an `attack` record's `side` + index point INTO.
    # Projected through `mon_view` like any other Pokemon, so a replay
    # names and HP-scales combatants from the same shape the team bar uses.
    # This is the enrichment layer the contract's section 2 rule points at:
    # the record shape is untouched, the join target is supplied here.
    player_team = [mon_view(m) for m in (feed.get("player_team") or [])]
    enemy_team = [mon_view(m) for m in (feed.get("enemy_team") or [])]
    # R4: the PRE-battle rosters. `player_team`/`enemy_team` above are the
    # POST-battle state (engine.py's `_run_battle` says so), which is the
    # replay's LAST frame; these are its first. The source seeds
    # `animateBattleVisually`'s own HP trackers from exactly these
    # (69084-69092).
    player_start = [mon_view(m) for m in (feed.get("player_team_start") or [])]
    enemy_start = [mon_view(m) for m in (feed.get("enemy_team_start") or [])]
    turns = fold_turns(feed.get("battle_events") or [])
    status_events = [dict(e) for e in (feed.get("status_events") or [])]
    return {
        "rounds": feed.get("rounds"),
        "player_won": bool(feed.get("player_won")),
        "turns": turns,
        "status_events": status_events,
        "player_team": player_team,
        "enemy_team": enemy_team,
        "player_team_start": player_start,
        "enemy_team_start": enemy_start,
        # Joined against the START rosters: a replay names combatants as they
        # were when the battle opened (a member that fainted mid-battle still
        # has its name and max HP there).
        "replay": _replay_steps(
            turns, status_events,
            {"player": player_start or player_team, "enemy": enemy_start or enemy_team},
        ),
    }


# ---------------------------------------------------------------------------
# Pending choice
# ---------------------------------------------------------------------------

def _stat10_percent() -> int:
    """`doSubMapReward`'s own displayed percentage for the `stat10` reward,
    `Math.max(1, Math.round(2 * B2y)) * 5` (bundle.deobfuscated.js:77040).
    `B2y` is the submap stat multiplier (76920-76925), which the port carries
    as `engine._SUBMAP_REWARD_STAT_MULTIPLIER`; the `max(1, round(...))` half
    is the same expression `engine._apply_run_stat_buff` applies to the buff
    itself, so the label and the mechanic cannot drift apart. Computed here
    rather than hard-coded as "10%" precisely because the source's own number
    is NOT 10 on the only branch that reaches it -- see the R3 record.
    """
    scaled = max(1, map_gen._js_round(2 * engine._SUBMAP_REWARD_STAT_MULTIPLIER))
    return scaled * 5


#: R3. The source's own screen strings for the two `showTeamPickerModal`
#: rewards, `doSubMapReward`'s `sacrifice` (bundle.deobfuscated.js:77021-77024)
#: and `stat10` (77041-77044) cases -- the title/desc pair each passes as
#: `showTeamPickerModal(title, desc, onPick)` (76845, 76868-76873).
_REWARD_TEAM_PICK_TEXT = {
    "sacrifice": (
        "Choose a Pokemon to release",
        "The rest of your team gains +4 levels.",
    ),
    "stat10": (
        "Enhance a Pokemon",
        None,  # filled in from `_stat10_percent()` -- the source interpolates it
    ),
}


def _pending_context(pending: "engine.PendingChoice",
                     state: Optional["engine.RunState"]) -> dict:
    """The whitelisted, primitives-only projection of the parts of
    `PendingChoice.extra` a renderer genuinely cannot do without, plus the
    source's own screen text for the phases that have one.

    Deliberately a per-phase ALLOW-LIST, not a filtered copy of `extra`:
    `extra` holds live `Combatant`/`data.Trainer` references and a
    copy-then-drop approach would leak a new one the day someone adds it.
    Every key below is named explicitly and carries only primitives (or a
    `mon_view`, itself already a projection).
    """
    ctx = {"title": None, "desc": None, "kind": None, "subject": None, "team_index": None}
    extra = pending.extra or {}
    phase = pending.phase

    if phase == engine.Phase.EVOLUTION_CHOICE:
        # `showBranchingChoice` (bundle.deobfuscated.js:70567-70570) titles the
        # screen `displayName(mon) + " is evolving!"` and subtitles it "Choose
        # its evolution:" -- so the screen names WHO is evolving. The port's
        # option dicts carry only `{into, name}`, so without this a browser
        # player sees two target species and no idea which team member is
        # about to become one of them.
        idx = extra.get("team_index")
        ctx["team_index"] = idx
        if state is not None and idx is not None and 0 <= idx < len(state.team):
            mon = state.team[idx]
            ctx["subject"] = mon_view(mon)
            ctx["title"] = f"{mon.nickname or mon.name} is evolving!"
        else:
            ctx["title"] = "Evolving!"
        ctx["desc"] = "Choose its evolution:"
        ctx["kind"] = "branching"

    elif phase == engine.Phase.REWARD_TEAM_PICK:
        kind = extra.get("kind")
        ctx["kind"] = kind
        title, desc = _REWARD_TEAM_PICK_TEXT.get(kind, (None, None))
        ctx["title"] = title
        if kind == "stat10":
            desc = f"+{_stat10_percent()}% to all its stats for the rest of the run."
        ctx["desc"] = desc

    elif phase == engine.Phase.ESCAPE_ROPE_CHOICE:
        # The source has no dedicated screen: `runBattleScreen`'s LOSS branch
        # injects a second button next to `#btn-continue-battle`
        # (bundle.deobfuscated.js:81399-81424). Accepting consumes the rope and
        # relabels Continue; declining is the ordinary "Continue..." that ends
        # the run. Both labels are the source's own (81406-81408, 81394).
        ctx["kind"] = "escape_rope"
        ctx["title"] = "Defeat..."
        ctx["desc"] = "Use an Escape Rope to escape with your run intact?"

    return ctx


#: R7/N43. Which phases present a Pokemon *as the thing being chosen*, and how
#: the option at index `i` is addressed back to the real `Combatant`. Every
#: entry is a producer fact re-read in the current tree, not an inference:
#:
#: - `CATCH_CHOICE`  -- `_offer_catch_choice` builds `options` and
#:   `extra["candidates"]` from the SAME ordered `mons` list, in one
#:   expression each (`engine.py:2691-2696`); `_visit_shiny` does the same
#:   with a one-element list (`engine.py:2774-2779`).
#: - `SWAP_CHOICE`   -- `_try_add_to_team` builds options from `state.team`
#:   (`engine.py:1037-1042`). `_offer_swap_screen` builds them from
#:   `state.team` when the team is full, but from the INCOMING mon alone when
#:   there is room (`engine.py:1064-1071`) -- and records which case it is in
#:   `extra["has_room"]`, so this is addressed, not guessed.
#: - `TRADE_CHOICE` (`engine.py:2964-2969`), `ITEM_EQUIP_CHOICE`
#:   (`engine.py:3590-3595`) and `REWARD_TEAM_PICK` (`engine.py:3384-3399`)
#:   build options from `state.team` order directly.
#:
#: `MOVE_TUTOR_CHOICE` is deliberately absent: its option carries its own
#: `team_index` and is resolved below by that field instead of by position.
_TEAM_ORDER_CHOICE_PHASES = frozenset({
    "swap_choice", "trade_choice", "item_equip_choice", "reward_team_pick",
})

#: Every phase whose options `_enrich_from_subject` fills out to a full card.
#: `move_tutor_choice` is enriched too, but by its own `team_index` branch.
_ENRICHED_MON_CHOICE_PHASES = frozenset(
    {"catch_choice"} | set(_TEAM_ORDER_CHOICE_PHASES)
)

#: The move-tier ceiling `_resolve_move_tutor_choice` applies:
#: `mon.move_tier = min(2, mon.move_tier + 1)` (`engine.py:3558`, carrying
#: CODEX.md issue 11's "tier 0 -> 1, not -> 2" fix).
#:
#: This is a MIRRORED LITERAL, not a read of the engine's own constant --
#: `engine.py` has no such constant to import and R7 has no authority to add
#: one (its brief pins `engine.py` byte-identical). The mirror is therefore
#: pinned by execution instead: `test_renderer_contract` tutors a Pokemon
#: already at the ceiling through the real engine and asserts the tier does
#: not move, so this number cannot silently drift away from the engine's.
_MOVE_TIER_MAX = 2


def _option_subject(pending: "engine.PendingChoice",
                    state: Optional["engine.RunState"],
                    index: int) -> Optional[Combatant]:
    """The real `Combatant` the option at `index` describes, or None.

    R7/N43. R6 left catch/trade/item-equip un-enriched because the
    correspondence "is an assumption rather than a fact". Re-reading the three
    constructors settles it: it is a fact, established by each producer in a
    single list comprehension over the list it also stores. See
    `_TEAM_ORDER_CHOICE_PHASES` for the citations.

    The correspondence is nevertheless *verified* rather than trusted, by
    `_subject_matches` below. A card carrying another Pokemon's stats would be
    worse than a card carrying none, so the enrichment is skipped -- silently,
    and only ever losing detail -- if the resolved Combatant does not agree
    with the summary the engine itself wrote.
    """
    phase = pending.phase.value
    extra = pending.extra or {}

    if phase == "catch_choice":
        candidates = extra.get("candidates") or []
        if 0 <= index < len(candidates):
            return candidates[index]
        return None

    if phase == "swap_choice" and extra.get("has_room"):
        # `_offer_swap_screen`'s room branch: the single option IS the
        # incoming Pokemon, which is not on the team at all.
        return extra.get("incoming") if index == 0 else None

    if phase in _TEAM_ORDER_CHOICE_PHASES:
        if state is not None and 0 <= index < len(state.team):
            return state.team[index]
    return None


#: The identity fields `_mon_summary` (`engine.py:922-932`) and `mon_view`
#: spell identically. All five must agree before a subject is accepted.
_SUBJECT_IDENTITY_KEYS = ("species_id", "level", "current_hp", "max_hp", "is_shiny")


def _subject_matches(opt: dict, mon: Combatant) -> bool:
    for key in _SUBJECT_IDENTITY_KEYS:
        if key not in opt:
            continue
        if opt[key] != getattr(mon, key, None):
            return False
    return True


def _enrich_from_subject(opt: dict, mon: Combatant, passives: Sequence = ()) -> None:
    """Fill a choice option out to a drawable card, from the real Combatant.

    Only keys the ENGINE did not already write are added, which keeps R6/N33's
    rule intact: the producer is the authority on what it is offering, and the
    renderer may only add detail. Driving the key list off `MON_FIELDS` rather
    than a second hand-maintained list means a future field lands on the
    choice cards and the team bar at the same time instead of one of them.
    """
    if not _subject_matches(opt, mon):
        return
    view = mon_view(mon, passives)
    # Sorted so option dicts serialize in a stable key order regardless of
    # frozenset iteration order.
    for key in sorted(MON_FIELDS):
        if key not in opt:
            opt[key] = view[key]


def _pending_options(pending: "engine.PendingChoice",
                     state: Optional["engine.RunState"]) -> list:
    """Read-side enrichment of the engine's own option dicts (R1's rule: the
    renderer may drop, regroup or enrich, never require a new key inside a
    producer's record). Two phases need it:

    - `EVOLUTION_CHOICE`: `showBranchingChoice` renders each branch as
      sprite + name + `types.join("/")` (bundle.deobfuscated.js:70601-70603),
      and picks the SHINY sprite path from the evolving mon, not the branch
      (70578-70581). The engine's option is `{into, name}` only, so `types`
      comes from `data.Evolution.types` (populated exactly for branching
      evolutions) and `is_shiny` from the subject.
    - `ESCAPE_ROPE_CHOICE`: the engine's option is `{action, item_index}`,
      which renders as a raw dict. The button's own label is the source's
      (81407).

    R6/N33 and N34 add two more, for the same reason and by the same rule --
    no engine structure changes and no key is required inside a producer's
    record:

    - `ITEM_CHOICE`: the engine's option is `{id, name, usable}`. R3 built
      `item_view` so the browser would stop being handed bare string ids
      (CODEX gap 6) and wired it into the BAG (`observation()["items_info"]`),
      but the item *offer* screen's options were never routed through it -- so
      the icon and description existed on one surface and not on the other, and
      the item card could not have drawn them however it was written. Enriched
      here with `desc`/`icon`/`icon_url` from the same `item_view`, so the two
      surfaces cannot disagree about what an item is.
    - `MOVE_TUTOR_CHOICE`: the engine's option is `{team_index, species_id,
      name, move_tier}`. `move_tier` is the tutor's whole subject and it is an
      opaque integer -- the card said "tier 0" and could not say what the
      Pokemon would actually attack with, which is the decision. Enriched with
      `move_preview`, computed by the same `_move_preview` every `mon_view`
      uses, off the real `Combatant` `team_index` names.

      R6 deliberately did NOT extend it to `CATCH_CHOICE`, `TRADE_CHOICE` or
      `ITEM_EQUIP_CHOICE`, on the grounds that their option-to-Combatant
      correspondence "is an assumption rather than a fact".

    R7/N43 overturns that, by re-reading the producers instead of reasoning
    about them. Each one builds its options in a single comprehension over a
    list it also keeps -- `extra["candidates"]` for catch, `state.team` for
    trade/item-equip/reward/swap -- so the correspondence is a *producer
    fact*, cited per phase on `_TEAM_ORDER_CHOICE_PHASES`, and additionally
    re-checked per option by `_subject_matches`. Those five phases are
    therefore enriched with the full `MON_FIELDS` card projection (types,
    `base_stats`, `effective_stats`, `stages`, `stat_buffs`, `move_preview`,
    `status_flags`, `held_item_info`, `sprite_url`, ...), so a player choosing
    what to catch, trade away, release or equip can see what they are choosing
    between. This is read-side only: no engine structure changes, no key is
    required inside a producer's record, and `CONTRACT_VERSION` stays 5.

    R7/N45 adds the other half of CODEX gap 10 to `MOVE_TUTOR_CHOICE`:
    `move_preview_next`, the move the Pokemon would attack with AFTER
    tutoring. It is the same deterministic `_move_preview` one tier up, using
    the engine's own ceiling (`min(2, tier + 1)`, `engine.py:3558`).

    On the tier ceiling, checked rather than assumed: `move_tier_capped` is
    carried but is **structurally unreachable** on a real tutor option.
    `_visit_move_tutor` offers only `m.move_tier < 2` (`engine.py:2868`),
    porting the source's own behaviour of rendering an "Already mastered!"
    span instead of a button (`bundle.deobfuscated.js:80474-80492`,
    `80507-80515`). So the awkward case the R7 brief anticipated -- a
    "successive move" identical to the current one -- cannot be presented to a
    player, because the producer never offers that Pokemon. The flag stays as
    a defensive, honest answer for any future producer that does not filter,
    and is deliberately NOT covered by a detector claiming to reach it.
    """
    options = [dict(o) if isinstance(o, dict) else o for o in pending.options]
    extra = pending.extra or {}

    if pending.phase == engine.Phase.EVOLUTION_CHOICE:
        idx = extra.get("team_index")
        subject = None
        if state is not None and idx is not None and 0 <= idx < len(state.team):
            subject = state.team[idx]
        branches = {b.into: b for b in (extra.get("branches") or [])}
        for opt in options:
            if not isinstance(opt, dict):
                continue
            branch = branches.get(opt.get("into"))
            opt["types"] = list(branch.types) if branch is not None and branch.types else []
            opt["is_shiny"] = bool(subject.is_shiny) if subject is not None else False
            # So a card builder that keys on `species_id` (every other screen's
            # option shape) can draw this one too, without a special case.
            opt["species_id"] = opt.get("into")

    elif pending.phase == engine.Phase.ESCAPE_ROPE_CHOICE:
        for opt in options:
            if isinstance(opt, dict) and opt.get("action") == "use_escape_rope":
                opt["item_id"] = engine._ESCAPE_ROPE_ITEM_ID
                opt["label"] = "Use Escape Rope"

    elif pending.phase == engine.Phase.ITEM_CHOICE:
        # R6/N33. The same `item_view` the bag already goes through, so an
        # offered item and a carried one describe themselves identically.
        # `name` is left as the engine wrote it rather than overwritten: the
        # engine is the authority on what it is offering, and `item_view`
        # reports `known: False` with `name == id` for an item in neither
        # ported table, which would be a downgrade.
        for opt in options:
            if not isinstance(opt, dict) or "id" not in opt:
                continue
            view = item_view(opt["id"])
            opt["desc"] = view["desc"]
            opt["icon"] = view["icon"]
            opt["icon_url"] = view["icon_url"]
            opt["known"] = view["known"]

    elif pending.phase == engine.Phase.MOVE_TUTOR_CHOICE:
        # R6/N34. `team_index` is the engine's own field on this option, so the
        # Combatant is addressed rather than inferred.
        passives = state.passives if state is not None else ()
        for opt in options:
            if not isinstance(opt, dict):
                continue
            idx = opt.get("team_index")
            if state is None or idx is None or not (0 <= idx < len(state.team)):
                continue
            mon = state.team[idx]
            opt["move_preview"] = _move_preview(mon)
            # R7/N45. What tutoring this Pokemon would actually buy.
            #
            # `_resolve_move_tutor_choice` sets `mon.move_tier = min(2,
            # mon.move_tier + 1)` (engine.py:3558) -- +1, ceiling 2. See this
            # function's docstring for why `move_tier_capped` is carried but
            # is unreachable through the real producer.
            next_tier = min(_MOVE_TIER_MAX, (mon.move_tier or 0) + 1)
            opt["move_tier_next"] = next_tier
            opt["move_tier_capped"] = next_tier == (mon.move_tier or 0)
            opt["move_preview_next"] = _move_preview(mon, next_tier)
            # The tutor screen is also a Pokemon choice, so it gets the same
            # card projection as every other one (level, HP, types, stats).
            _enrich_from_subject(opt, mon, passives)

    elif pending.phase.value in _ENRICHED_MON_CHOICE_PHASES:
        # R7/N43. See this function's docstring and `_option_subject`.
        passives = state.passives if state is not None else ()
        for idx, opt in enumerate(options):
            if not isinstance(opt, dict):
                continue
            mon = _option_subject(pending, state, idx)
            if mon is not None:
                _enrich_from_subject(opt, mon, passives)

    return options


def pending_view(pending: Optional["engine.PendingChoice"],
                 state: Optional["engine.RunState"] = None) -> Optional[dict]:
    """`PendingChoice.options` is already plain dicts of primitives (see its
    docstring); `extra` is engine-internal and deliberately NOT exposed --
    it can hold live `Combatant`/`Trainer` references.

    R3 adds `context` (see `_pending_context`) because three phases were not
    renderable from `options` alone: `REWARD_TEAM_PICK`'s two branches are
    indistinguishable (the same team-summary list means "release this one" or
    "buff this one" depending on `extra["kind"]` -- picking wrong is
    destructive), `EVOLUTION_CHOICE` never said WHO is evolving, and
    `ESCAPE_ROPE_CHOICE`'s single option is an unlabelled `{action,
    item_index}` dict. `state` is optional so the R1 single-argument call
    still works; a caller that omits it gets the same context minus anything
    that needs the team.
    """
    if pending is None:
        return None
    return {
        "phase": pending.phase.value,
        "optional": pending.optional,
        "options": _pending_options(pending, state),
        "context": _pending_context(pending, state),
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
        "team": [mon_view(m, state.passives) for m in state.team],
        "items": list(state.items),
        "items_info": [item_view(i) for i in state.items],
        "map": map_view(state),
        "pending": pending_view(state.pending, state),
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
