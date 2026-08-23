"""The game's state machine: run lifecycle, map traversal, and node-visit
resolution, tying `map_gen.py` + the battle engine (`battle.py`/
`battle_abilities.py`/`battle_traits.py`/`battle_loop.py`) together into a
single playable run. This is the first module in the repo that reads/writes
persistent run state rather than being a pure, stateless formula library.

Citations: `docs/logic-notes.md` sections 2-3 (state object shape, mode
differences) plus two deep-dive companion docs written this session,
`docs/logic-notes-nodes.md` (every node-visit handler,
`doBattleNode`/`doCatchNode`/`doBossNode`/etc.) and
`docs/logic-notes-runlifecycle.md` (`startNewRun`/`startMap`/
`checkAndEvolveTeam`/`applyLevelGain`/run-over conditions) -- read those
before touching this module, the same way `battle_loop.py` points at
`docs/logic-notes-runbattle.md`.

**The central design decision** (per CLAUDE.md's explicit callout): the JS
has **no persistent "in battle" state at all** -- "in battle" is pure control
flow, a `Promise`-returning screen function awaited by the map loop. Tracing
the node handlers (`docs/logic-notes-nodes.md`) shows this is true of far
more than just battle, though: `doCatchNode`'s candidate-pick, the team-full
swap screen, `checkAndEvolveTeam`'s branching-evolution modal, the move
tutor's target pick, the item-equip modal, and the trade-target pick are
*all* suspended `await`-a-click continuations in the source, not data. A
faithful `engine.step(action) -> new_state` API can't suspend a Python call
the way JS suspends a `Promise` -- so this module reifies **every one of
those suspension points** as an explicit `Phase` the state machine can be in,
not just a battle phase. `RunState.pending` (a `PendingChoice`) is what a
caller reads to know a decision is expected and what `SelectOption`/whichever
action resolves it; `RunState.phase` is the JS's collapsed-into-one-`Phase`-
enum answer to "which suspended continuation, if any, are we sitting at
right now." Battle itself does NOT need its own phase, because
`battle_loop.run_battle` (unlike the source's `runBattle`) is a synchronous
function that resolves an entire multi-round battle in one call -- there is
no per-turn player decision anywhere in this game (`getBestMove` picks both
sides' moves automatically, docs/logic-notes.md section 6.5), so a whole
battle fits inside a single `step()` call and needs no suspension of its own.

**Deliberately out of scope this session** (flagged, not silently dropped):
- **Endless mode / Battle Tower / Challenges.** `map_gen.py` itself only
  fully ports the Story/Nuzlocke path (its own docstring flags
  `generateSubMap`, the Endless buff-pool, and Endless2 overrides as
  unported); this module inherits that scope. `ChallengeFlags()` is always
  constructed with its Story-mode defaults.
- **Mid-map procedural trainer rosters** (`doTrainerNode`'s
  `TRAINER_BATTLE_CONFIG`, per-archetype species pools) and **the Gen2
  Silver rival / Gen3 Magma-Aqua fixed rosters** (`SILVER_ENCOUNTERS`/
  `MAGMA_ENCOUNTERS`/`AQUA_ENCOUNTERS`) are now real, extracted-table ports
  (`tools/extract-data/extract-trainer-tables.js`, `data.get_trainer_battle_config`/
  `get_silver_encounters`/`get_silver_starter_lines`/`get_magma_encounters`/
  `get_aqua_encounters`) -- see `_visit_trainer`/`_visit_silver`/
  `_visit_admin` below, and CODEX.md's addendum for full citations. The
  `trainerSprite` archetype-key assignment itself lives in
  `map_gen.generate_map` (`_assign_trainer_sprite`), since it's a
  map-generation-time deterministic hash, not a node-visit-time decision.
- **The submap system is now ported** (`generateSubMap`/`UNDERGROUND`/
  `DISTORTION`/`REWARD`/`SUBEXIT`, docs/logic-notes-submaps.md). SILVER
  (Gen2) and MAGMA/AQUA (Gen3) do NOT use this system at all -- confirmed by
  direct trace of `onNodeClick`'s dispatch switch (bundle.deobfuscated.js:
  77364-77382): only `NODE_TYPES.UNDERGROUND`/`NODE_TYPES.DISTORTION` (Gen4/
  Sinnoh-only) ever call `enterSubMap`; SILVER/MAGMA/AQUA dispatch straight
  to the already-ported `_visit_silver`/`_visit_admin` (fixed-roster,
  in-place boss fights on the PARENT map, CODEX.md P0.9), untouched by this
  session. `RunState.in_sub_map`/`sub_map_return`/
  `distortion_worlds_entered`/`distortion_legendary_claimed` are the new
  persistent fields this needs; `_enter_sub_map`/`_visit_sub_map_boss`/
  `_visit_reward`/`_visit_subexit`/`_return_from_sub_map` are the new
  handlers (see their own docstrings for citations). `Phase.ON_MAP` covers
  submap navigation too (no new Phase for that) -- the new
  `Phase.REWARD_TEAM_PICK` only covers the "sacrifice"/"stat10" submap
  rewards' own team-picker UI, a genuinely new suspended-continuation shape
  (`showTeamPickerModal`, no decline option, unlike every other pending
  choice in this module).
- **Trait/passive acquisition mid-run is genuinely NOT a Story/Nuzlocke
  mechanic -- traced and confirmed, not a gap.** `showPassiveItemChoice`
  (bundle.deobfuscated.js:84961-85049) -- the ONLY function in the entire
  bundle that ever grants a new trait -- opens with `if (!state ||
  !state["challengeId"]) { resolve(); return; }` and its single call site
  (bundle.deobfuscated.js:86193) is deep inside `runEndlessTrainerFight`'s
  post-boss-win handling, gated on `state.challengeEndless`/
  `endlessState`. Grepping the whole bundle confirms there is no second
  call site. Mid-run trait acquisition is therefore an **Endless-mode-only**
  mechanic; `RunState.passives` correctly models Story/Nuzlocke as
  "traits are a fixed pre-run loadout, never earned mid-run" (a `reset()`-
  time input a caller supplies, e.g. for RL scenarios studying a fixed
  trait build). CODEX.md's audit flagged this as an unmodeled gap without
  tracing this gate; corrected here -- see CODEX.md issue 17's resolution
  note.
- **Bag items are now a modeled action.** `UseItem`/`EquipItem` (this
  module's public `Action` union) port `applyUsableItemTo`/
  `equipItemFromBag` -- Rare Candy/Sacred Ash/Moon Stone/TM usage and
  bag<->held-item swapping. `ReorderTeam` ports the team bar's drag/click-
  to-swap reordering. See `_apply_use_item`/`_apply_equip_item`/
  `_apply_reorder_team` and their docstrings.
- **Three untraced numeric constants** get documented placeholders rather
  than fabricated "plausible" values: `doLegendaryNode`'s
  `legendaryShinyChanceFlat()`, standard-mode `doTradeNode`'s
  `rollShiny()`, and its `tradeOfferLevel`'s level-bonus term (used as `+0`
  here). See `docs/logic-notes-nodes.md` sections 8-9.

Deliberately NOT replicated (per CLAUDE.md's "js/ui.js is reference-only"):
the JS's per-turn `log`/`detailedLog` event arrays in full, including all
their flavor text. `RunState.log` remains this module's OWN coarse event
representation (one entry per node visit / evolution / badge / victory).

**R1 changed the battle half of that.** A battle still resolves atomically
inside one `step()`, but it is no longer opaque: `_run_battle` now carries
`battle_loop`'s ordered `battle_events`/`status_events` streams out to
`RunState.last_battle`, so a renderer can replay the fight turn by turn
rather than diffing a before/after snapshot. The streams are the SAME
objects the route oracle compares -- `battle_loop` is the single producer,
the oracle owns the record shape, and the renderer reads through its own
projection in `pokelike/render/contract.py`. See `RunState.last_battle` and
docs/renderer-contract.md. What is still absent is the source's flavor text
and any event for the non-battle parts of a node visit.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Optional, Sequence, Union

from pokelike import battle, battle_abilities, battle_loop, battle_traits, data, map_gen, rng
from pokelike.battle import BattleConfig, Combatant, HeldItem, Trait
from pokelike.battle_loop import BattleResult
from pokelike.map_gen import ChallengeFlags, MapNode

# doCatchNode/catchPokemon's hardcoded cap (bundle.deobfuscated.js:79035) --
# NOT the same thing as state.maxTeamSize, which is a watermark stat only
# (docs/logic-notes-nodes.md section 4, docs/logic-notes-runlifecycle.md
# section 8).
TEAM_CAP = 6

# startNewRun/pickForcedStarter/selectStarter all hardcode level 5 for a
# fresh starter (bundle.deobfuscated.js:75648).
_STARTER_LEVEL = 5

# `shiny_rate` trait id used by `roll_shiny`'s doubling check -- matches
# `rollShiny`/`legendaryShinyChanceFlat`'s own `hasPassive`-style
# `some(id === "shiny_rate" && enabled !== !0x1)` check. In the deobfuscated
# source `!0x1` evaluates to `false`, so this is the ordinary opt-out gate:
# active unless `enabled` is explicitly false.
_SHINY_RATE_TRAIT_ID = "shiny_rate"

# CODEX.md P0.6: bag item id `runBattleScreen`'s eligible-loss branch
# searches for (bundle.deobfuscated.js:81400-81402).
_ESCAPE_ROPE_ITEM_ID = "escape_rope"

# CODEX P0.7: Zigzagoon/Linoone dex ids -- `runBattleScreen`'s own immediate
# post-win Gen3-only Pickup branch checks these directly by species
# (bundle.deobfuscated.js:81230-81231), independent of the ability-driven
# `grantPickupItem` path (`_grant_pickup_item` below).
_GEN3_PICKUP_SPECIES = (0x107, 0x108)

# CODEX P0.8: `effort_ribbon`'s flat +10 stat-buff dict (bundle.deobfuscated.js:
# 81170-81176) -- battle-clone-only, never copied back to persistent state
# (`_copy_back_battle_result` never touches `stat_buffs`).
_EFFORT_RIBBON_STAT_BUFFS = {"hp": 10, "atk": 10, "def": 10, "speed": 10, "special": 10, "spdef": 10}

# `doCatchNode`'s Gen1-Nuzlocke, map-0-only restricted candidate set
# (bundle.deobfuscated.js:78538-78541) -- CODEX.md issue 10.
_GEN1_NUZLOCKE_MAP0_RESTRICTED = frozenset(
    {0xA, 0xB, 0x1B, 0x36, 0x38, 0x3C, 0x45, 0x48, 0x4A, 0x4F, 0x51, 0x56, 0x60, 0x62, 0x64, 0x66, 0x6F, 0x74, 0x76, 0x78, 0x81, 0x85}
)

# `doCatchNode`'s map-0/layer-1 "guarantee a Grass and a Water option"
# safety net (bundle.deobfuscated.js:78567-78578), indexed
# [gen1, gen2, gen3] -- unreachable in gen4 mode in the source too (the
# outer gate is `!state.gen4Mode`), so no gen4 entry is needed.
_MAP0_SAFETY_NET: dict[int, tuple[tuple[int, ...], tuple[int, ...]]] = {
    0: ((0x2B, 0x45, 0x66), (0x36, 0x3C, 0x48, 0x4F, 0x56, 0x62, 0x74, 0x76, 0x78, 0x81)),
    1: ((0xBB, 0xBF), (0xB7, 0xC2, 0xDF)),
    2: ((0x10E, 0x111, 0x11D), (0x116, 0x11B, 0x155)),
}


class Phase(str, Enum):
    """Every suspended-continuation point the source's node handlers can
    leave the player sitting at (see module docstring). `ON_MAP` is the only
    phase where `VisitNode` is a valid action; every other non-terminal
    phase expects `SelectOption` (or `AdvanceMap`/`ChooseStarter` for their
    own dedicated phases).
    """

    CHOOSE_STARTER = "choose_starter"
    ON_MAP = "on_map"
    CATCH_CHOICE = "catch_choice"
    SWAP_CHOICE = "swap_choice"
    EVOLUTION_CHOICE = "evolution_choice"
    MOVE_TUTOR_CHOICE = "move_tutor_choice"
    ITEM_CHOICE = "item_choice"
    ITEM_EQUIP_CHOICE = "item_equip_choice"
    TRADE_CHOICE = "trade_choice"
    ESCAPE_ROPE_CHOICE = "escape_rope_choice"
    REWARD_TEAM_PICK = "reward_team_pick"
    NEXT_MAP_READY = "next_map_ready"
    GAME_OVER = "game_over"
    VICTORY = "victory"


@dataclass
class PendingChoice:
    """A decision the caller must resolve before the state machine can
    proceed. `options` is the renderer/agent-facing view -- plain dicts of
    primitives, safe to print or feed to a UI/Gym observation. `extra` is
    engine-internal bookkeeping needed to actually apply the chosen option
    (may hold live `Combatant`/`data.Trainer` object references) -- callers
    outside this module shouldn't need to read it.
    """

    phase: Phase
    options: list = field(default_factory=list)
    optional: bool = False
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VisitNode:
    """Valid only when `RunState.phase == Phase.ON_MAP`. `node_id` must name
    a currently-accessible node in `RunState.map.nodes`."""

    node_id: str


@dataclass(frozen=True)
class AdvanceMap:
    """Valid only when `RunState.phase == Phase.NEXT_MAP_READY` (the badge
    screen's "Next Map" button, docs/logic-notes-runlifecycle.md section 3)."""


@dataclass(frozen=True)
class ChooseStarter:
    """Valid only when `RunState.phase == Phase.CHOOSE_STARTER`."""

    species_id: int


@dataclass(frozen=True)
class SelectOption:
    """The generic answer to any `PendingChoice`: an index into
    `RunState.pending.options`, or `None` to skip/cancel/decline -- valid
    only when `RunState.pending.optional` is True (CLAUDE.md flags this
    variable-cardinality "pick 1 of N, or skip" shape as a real Gym-design
    concern for Phase 3; this is the single action type every such decision
    in this engine funnels through).

    `cancel` (M5) is a THIRD exit, distinct from both a pick and a skip, and
    is currently valid only for `Phase.ITEM_EQUIP_CHOICE`. The item-equip
    overlay is the one screen where the source offers three genuinely
    different affordances rather than two: `[data-idx]` equips
    (bundle.deobfuscated.js:79535-79551), `#btn-equip-to-bag` banks the item
    and advances (79552-79562) -- which is what `index=None` means here --
    and `#btn-equip-cancel` (79563-79569) does neither. Its whole body is
    `B2O.remove()`: no equip, no bank, no `onComplete`, no
    `advanceFromNode`, and no clearing of `state.itemOffer`. Modelling it as
    `index=None` would silently bank an item the source did not, so it needs
    its own flag. `_resolve_pending` rejects it for every other phase rather
    than letting it degrade into a skip.
    """

    index: Optional[int] = None
    cancel: bool = False


@dataclass(frozen=True)
class ReorderTeam:
    """Valid only when `RunState.phase == Phase.ON_MAP`. `order` must be a
    permutation of `range(len(RunState.team))` -- `new_team[i] =
    old_team[order[i]]`. Port of the source's team-bar drag/click-to-swap
    reordering (CODEX.md issues 5/36): battle order is mechanically
    significant (`run_battle` always fights whichever living member is
    FIRST in list order), but the source models arbitrary reordering, not
    just adjacent swaps, so this takes a full permutation rather than a
    `(i, j)` swap pair -- a caller that only wants a two-element swap can
    build one by rotating a copy of `list(range(len(team)))`.
    """

    order: tuple[int, ...]


@dataclass(frozen=True)
class UseItem:
    """Valid only when `RunState.phase == Phase.ON_MAP`. Port of
    `applyUsableItemTo` (CODEX.md issue 16) -- uses a consumable bag item
    (`RunState.items[item_index]`) on `RunState.team[target_index]`. Every
    usable item (Rare Candy/Sacred Ash/Moon Stone/TM) requires a target;
    see `_usable_item_can_target` for the per-item eligibility rule the
    source itself uses to gray out invalid targets. May raise a
    `Phase.EVOLUTION_CHOICE` (Moon Stone forcing a branching evolution, or
    Rare Candy's level-up making one newly eligible) the same way a battle
    win's automatic evolution check does.
    """

    item_index: int
    target_index: int


@dataclass(frozen=True)
class EquipItem:
    """Valid only when `RunState.phase == Phase.ON_MAP`. Port of the source's
    public equip surface, not just the low-level `equipItemFromBag` helper in
    isolation (CODEX.md issues 9/16, P0.5). `equipItemFromBag` itself
    (bundle.deobfuscated.js:79652-79671) applies no item-type check -- but
    its only caller, the team-bar click handler
    (bundle.deobfuscated.js:64943-64950), routes `item.usable` items to
    `applyUsableItemTo` instead and never calls `equipItemFromBag` for them:
    `if (it.usable) { ... applyUsableItemTo ... } else equipItemFromBag(...)`.
    So a usable item can never actually reach `equipItemFromBag` in the real
    game. This action models that reachable public surface: moves
    `RunState.items[bag_index]` onto `RunState.team[team_index].held_item`,
    pushing whatever was already held back into the bag, but only for a
    recognized non-usable (passive/held) item id -- a usable item id, or an
    unrecognized item id, raises `ValueError` before any bag/held-item
    mutation.
    """

    bag_index: int
    team_index: int


@dataclass(frozen=True)
class UnequipItem:
    """Valid only when `RunState.phase == Phase.ON_MAP`. The item-equip
    overlay's own "⬇ Unequip (return to bag)" exit, which had no port until M6
    (R3 disclosed it and declared it out of scope).

    The source spells it twice, with identical effect:

    * the per-member `[data-unequip]` rows
      (bundle.deobfuscated.js:79521-79531): `heldItem && (state.items.push(
      heldItem), heldItem = null)`;
    * `#btn-equip-to-bag` when the overlay was opened FROM a member
      (`fromPokemonIdx >= 0`, bundle.deobfuscated.js:79549-79553):
      `state.team[iu].heldItem = null, state.items.push(B)` -- where `B` is by
      construction that member's held item, since that is what the team-bar
      (`64702`) and party-screen (`78203`) handlers pass in.

    Both are "move `team[team_index].held_item` back into the bag". A member
    holding nothing is a no-op in the source (the `heldItem &&` guard) and is
    rejected here instead, because unlike the source this is an action a caller
    chose rather than a row that happens to be on screen -- `legal_actions`
    only offers indices that are actually holding something.
    """

    team_index: int


@dataclass(frozen=True)
class HandOffItem:
    """Valid only when `RunState.phase == Phase.ON_MAP`. The overlay's
    member-to-member hand-off (bundle.deobfuscated.js:79541-79545), reached by
    opening the overlay from one member's held-item badge and then clicking
    another member's row.

    **This is a SWAP, and it is deliberately not modelled as unequip+equip.**
    The brief that scoped this suspected the two would be equivalent; traced,
    they are not. The source's branch is

        iu >= 0 ? (state.team[iu].heldItem = B2P) : ...
        ...
        B2D.heldItem = B

    where `B` is the item the overlay was opened with -- i.e.
    `team[from_index].held_item` -- and `B2P` is the TARGET's current held
    item. So the target's old item goes to the *source member*. Composing
    `UnequipItem(from)` with `EquipItem(bag, to)` would instead route the
    target's old item to the *bag* (`_apply_equip_item`'s own
    `state.items.append(old_item.id)`), leaving a different game state whenever
    the target was already holding something. They coincide only in the case
    where the target holds nothing.

    Hence its own action rather than a UI convenience over two existing ones.
    """

    from_index: int
    to_index: int


Action = Union[
    VisitNode, AdvanceMap, ChooseStarter, SelectOption, ReorderTeam,
    UseItem, EquipItem, UnequipItem, HandOffItem,
]


@dataclass
class RunState:
    """Everything a run needs, kept close to `startNewRun`'s own field list
    (docs/logic-notes.md section 2.1, docs/logic-notes-runlifecycle.md
    section 1) plus the extra bookkeeping this port's explicit `Phase`
    design needs that the source doesn't structurally have. `team` is a
    plain `list[battle.Combatant]` -- the source's roster Pokemon ARE
    battler objects (HP/status persist across battles, only in-battle-only
    fields like `stages` reset each fight via `initBattleState`), so no
    separate "roster wrapper" type is needed (CLAUDE.md's brief floated one
    as an option; this is the simpler one).
    """

    nuzlocke_mode: bool = False
    gen2_mode: bool = False
    gen3_mode: bool = False
    gen4_mode: bool = False
    shiny_charm: bool = False
    run_seed: int = 0

    current_map: int = 0
    map: Optional[map_gen.GeneratedMap] = None
    current_node_id: Optional[str] = None

    team: list = field(default_factory=list)  # list[Combatant]
    items: list = field(default_factory=list)  # list[str] -- bag item ids (usable + released held items)
    passives: list = field(default_factory=list)  # list[Trait] -- player's collected traits; acquisition not modeled, see module docstring

    badges: int = 0
    elite_index: int = 0
    starter_species_id: Optional[int] = None
    max_team_size: int = 1  # watermark stat, NOT a cap -- see TEAM_CAP
    silver_beaten: int = 0
    fought_admin: bool = False
    used_pokecenter: bool = False
    picked_up_item: bool = False
    used_tm: bool = False
    used_ball_catch: bool = False
    got_via_question: bool = False
    any_fainted: bool = False  # `state.anyFainted`, initialized false in `startNewRun`
    # (bundle.deobfuscated.js:75478) and set true ONLY by `runBattleScreen`'s Nuzlocke
    # cull (81371-81372) when that cull actually removed at least one member (`BI1.length
    # > 0`). It is NOT "a team member fainted during a battle": an ordinary-mode faint, a
    # Nuzlocke loss/wipe (the cull lives inside the WIN branch opened at 81278), a
    # `no_permadeath` fight and a win with nothing to cull all leave it false. Its only
    # source consumer is the `_no_tombstone` achievement check (81971); no achievement
    # system exists in this port yet, so this persists the source-equivalent flag for
    # that future consumer, exactly like `entered_sub_map` below. Never reset once set.
    escaped_via_rope: bool = False  # `state._escapedViaRope` (bundle.deobfuscated.js:81412), CODEX.md P0.6

    # -- live save/resume guards ------------------------------------------
    # The source's three node-scoped resume records, modelled as the SINGLE
    # slots they really are (`state.savedQuestionResolve` declared at
    # bundle.deobfuscated.js:20030, `state.savedCatch` at 16827,
    # `state.savedShinyNode` at 22178; all three nulled wholesale at run
    # teardown, 84447-84449). Each exists so a save parked on a choice screen
    # can be resumed without re-rolling the offer, so each is written at the
    # moment the offer is first computed and cleared by whichever exit
    # consumes it -- and WHICH of them an exit clears differs per branch,
    # which is exactly the behavior `route-oracle`'s `resume_state`
    # projection compares (see `_try_add_to_team`, `_resolve_catch_choice`
    # and `_resolve_swap_choice`).
    #
    # `saved_question_resolve` replaces the former `question_cache` dict
    # (M4.2). The source keeps ONE `{key, resolvedType}` record, not a map:
    # `onNodeClick` reuses it only when `savedQuestionResolve.key` equals the
    # node's own map-qualified key and otherwise overwrites it outright
    # (77326-77332). The dict was observationally equivalent for every route
    # this port can express -- a question node that raises a pending choice
    # is not revisitable until it advances, and the key is map-qualified
    # either way -- but it was a different shape from the field the oracle
    # now compares, so the single slot is modelled directly.
    saved_question_resolve: Optional[dict] = None  # {"key": str, "resolved_type": str}
    saved_catch: Optional[dict] = None  # {"key": node id, "instances": [Combatant, ...]}
    saved_shiny_node: Optional[dict] = None  # {"key": str, "species_id": int}
    # `state.itemOffer` (declared bundle.deobfuscated.js:22541) -- the FOURTH
    # member of the resume-guard family above, added in M5. `doItemNode` pins
    # the rolled offer at 79372 and restores it at 79361-79363 when
    # `state.itemOffer.nodeId === B.id`, rebuilding the cards from the saved
    # ids and drawing NO RNG; all three of its consuming exits null it
    # (79420 usable pick / 79425 equip-modal onComplete / 79437 skip).
    #
    # It is observable because `#btn-equip-cancel` (79563-79569) is only
    # `B2O.remove()`: it does not call `onComplete`, does not clear the
    # offer, and does not `advanceFromNode`, so the node stays unvisited and
    # accessible with the offer still pinned. Re-clicking it then restores
    # the SAME three items for zero draws. Directly observed on the real
    # source: seeds 333333333 and 222222222, first visit 20 draws, cancel 0
    # draws, second visit 0 draws with a byte-identical id list.
    #
    # Keyed by the BARE node id, exactly like `savedCatch` (78441) and
    # unlike the map-qualified `savedQuestionResolve`/`savedShinyNode`; and
    # `startMap` (76228-76245) clears none of the four, so a cancelled offer
    # survives a map advance and can be restored at the same node id on a
    # LATER map. That is the source's behaviour, mirrored rather than
    # "corrected".
    item_offer: Optional[dict] = None  # {"node_id": str, "item_ids": [str, ...]}

    # Special submap system (docs/logic-notes-submaps.md) -- Gen4/Sinnoh-only,
    # UNDERGROUND/DISTORTION node types (SILVER/MAGMA/AQUA never touch these).
    in_sub_map: Optional[str] = None  # "underground"/"distortion"/None -- `state.inSubMap`
    sub_map_return: Optional[dict] = None  # {"kind","map","map_index","node_id","no_advance"} -- `state.subMapReturn`
    distortion_worlds_entered: int = 0  # `state.distortionWorldsEntered`
    distortion_legendary_claimed: bool = False  # `state.distortionLegendaryClaimed`
    entered_sub_map: bool = False  # `state.enteredSubMap` (bundle.deobfuscated.js:76696) -- gates the "g4_surface"
    # achievement (bundle.deobfuscated.js:81933: awarded only if this is STILL false at run end); no achievement
    # system exists yet in this port, so this just persists the source-equivalent flag for that future consumer.

    phase: Phase = Phase.CHOOSE_STARTER
    pending: Optional[PendingChoice] = None
    game_over: bool = False
    won: bool = False

    log: list = field(default_factory=list)  # this module's own event log, see module docstring

    # R1 renderer contract. The MOST RECENT battle's raw event streams, carried
    # out of `battle_loop.run_battle` verbatim so a renderer can replay the
    # fight turn by turn instead of seeing only the coarse before/after
    # snapshot the module docstring describes. Replaced (never appended to) by
    # each `_run_battle`, so this stays bounded at one battle -- `search_route`
    # deep-copies `RunState` per explored branch and an accumulating log would
    # make that cost grow without limit.
    #
    # `battle_loop` is the SINGLE producer; the shape of the records inside is
    # owned by the route oracle (they are what `run_scenario._fold_turns`
    # projects into the compared `turns` field, SCHEMA.md). Nothing here may
    # add a field to a record. The renderer's own projection lives in
    # `pokelike/render/contract.py` and reshapes/enriches on the read side --
    # see docs/renderer-contract.md section "Battle feed ownership".
    #
    # Purely additive and behavior-neutral: written once, after `run_battle`
    # has already returned, read by nobody in this module, and no control flow
    # or RNG draw depends on it.
    #
    # R2/N2: `player_team`/`enemy_team` are carried too. An `attack` record
    # identifies its participants by `side` + index only (correctly -- the
    # oracle compares the rosters separately), so without them a replay can
    # report HP deltas but cannot name, sprite or HP-scale either combatant.
    # These are the post-battle `BattleResult` rosters, i.e. the same objects
    # `_copy_back_battle_result` reads.
    last_battle: Optional[dict] = None  # {"battle_events", "status_events", "rounds", "player_won", "player_team", "enemy_team", "player_team_start", "enemy_team_start"}

    _todo: list = field(default_factory=list)  # resumable post-battle work queue, see _run_todo


def accessible_nodes(state: RunState) -> list[MapNode]:
    """Convenience for `render/`/a future Gym wrapper: every node the player
    could legally `VisitNode` right now."""
    if state.map is None:
        return []
    return [n for n in state.map.nodes.values() if n.accessible]


def legal_actions(state: RunState) -> dict:
    """Phase 3 boundary prep (CODEX.md issue 32): a single authoritative
    answer to "what can `Engine.step` legally be called with right now,"
    spanning every phase this state machine can be in -- not just
    `accessible_nodes`'s map-only slice. Exists so a future Gym wrapper
    reads legality off the engine instead of re-deriving/duplicating it
    (and risking drift, per CODEX.md's own warning).

    Returns a dict keyed by action TYPE (not a flat list of concrete
    `Action` instances) because a couple of these have a combinatorially
    large or genuinely unbounded parameter space -- `ReorderTeam.order` is
    any permutation of the current team, `UseItem`/`EquipItem` are
    (bag index, team index) pairs -- enumerating every concrete instance
    would blow up for no benefit. Each entry instead gives the legal
    *parameter ranges/indices* for that action type, letting a caller build
    whatever concrete `Action`/action-mask representation it wants:

    - `{"choose_starter": {"species_ids": [...]}}` -- `Phase.CHOOSE_STARTER`.
    - `{"select_option": {"indices": [...], "optional": bool,
      "cancel": bool}}` -- any `PendingChoice` phase (catch/swap/evolution/
      move-tutor/item/trade/escape-rope). `indices` is
      `range(len(pending.options))`; `None` is also legal (skip/decline) iff
      `optional` is True -- for `Phase.ESCAPE_ROPE_CHOICE` (CODEX.md P0.6)
      this is always True: `index=0` accepts (consumes the rope), `None`
      declines (immediate `GAME_OVER`), matching the source's two
      `btn-continue-battle` click handlers.

      `cancel` (M7) declares the THIRD exit `SelectOption(index=None,
      cancel=True)`, which `_resolve_pending` has accepted since M5 for
      `Phase.ITEM_EQUIP_CHOICE` and rejects for every other phase. It was
      missing from this dict, so the one authoritative answer to "what can
      `step` legally be called with" silently omitted a real, already
      implemented affordance -- the source's `#btn-equip-cancel`
      (bundle.deobfuscated.js:79563-79569), whose whole body is
      `B2O.remove()` and which is therefore NOT the same exit as banking the
      item with `index=None, cancel=False` (`#btn-equip-to-bag`,
      79552-79562). This is a DECLARATION fix only: no resolver, no state
      transition and no RNG draw changes.
    - `{"advance_map": True}` -- `Phase.NEXT_MAP_READY`.
    - On `Phase.ON_MAP`: `"visit_node"` (accessible node ids), and, if
      applicable, `"reorder_team"` (current team size, so a caller knows
      what permutation length is legal), `"use_item"` (one entry per bag
      item that's usable AND has at least one eligible target, each with
      its own `target_indices` from `_usable_item_can_target`), and
      `"equip_item"` (bag indices restricted to recognized non-usable
      items only, paired with every team index -- CODEX.md P0.5: the
      source's team-bar click handler never routes a usable item to
      `equipItemFromBag`, see `EquipItem`'s docstring, so usable-item bag
      indices are excluded here the same way). M6 adds the item-equip
      overlay's other two exits, both offered only for members actually
      holding something: `"unequip_item"` (`team_indices`) and
      `"hand_off_item"` (`from_indices` plus `team_size`, since every OTHER
      member is a legal target -- see `HandOffItem`).
    - `{}` on `Phase.GAME_OVER`/`Phase.VICTORY` (or a `None` map) -- no
      legal actions, the run has ended.
    """
    if state.phase in (Phase.GAME_OVER, Phase.VICTORY):
        return {}
    if state.phase == Phase.CHOOSE_STARTER:
        return {"choose_starter": {"species_ids": [o["species_id"] for o in state.pending.options]}}
    if state.phase in _PENDING_RESOLVERS and state.pending is not None:
        return {
            "select_option": {
                "indices": list(range(len(state.pending.options))),
                "optional": state.pending.optional,
                # M7: mirrors `_resolve_pending`'s own gate exactly, so the
                # declaration cannot drift from the behaviour.
                "cancel": state.phase == Phase.ITEM_EQUIP_CHOICE,
            }
        }
    if state.phase == Phase.NEXT_MAP_READY:
        return {"advance_map": True}
    if state.phase == Phase.ON_MAP:
        result: dict = {"visit_node": {"node_ids": [n.id for n in accessible_nodes(state)]}}
        if len(state.team) > 1:
            result["reorder_team"] = {"team_size": len(state.team)}
        usable_ids = {item.id for item in data.get_usable_items()}
        use_item = []
        for item_idx, item_id in enumerate(state.items):
            if item_id not in usable_ids:
                continue
            targets = [i for i, mon in enumerate(state.team) if _usable_item_can_target(item_id, mon)]
            if targets:
                use_item.append({"item_index": item_idx, "item_id": item_id, "target_indices": targets})
        if use_item:
            result["use_item"] = use_item
        if state.team:
            passive_ids = _passive_item_ids()
            equip_bag_indices = [i for i, item_id in enumerate(state.items) if item_id in passive_ids]
            if equip_bag_indices:
                result["equip_item"] = {
                    "bag_indices": equip_bag_indices,
                    "team_indices": list(range(len(state.team))),
                }
            # M6. The overlay's other two exits. Both are only reachable from a
            # member that is actually holding something -- the source opens the
            # overlay from a held-item badge (64702 / 78203), so a member with
            # no item has no badge to click.
            holders = [i for i, mon in enumerate(state.team) if mon.held_item is not None]
            if holders:
                result["unequip_item"] = {"team_indices": holders}
                if len(state.team) > 1:
                    result["hand_off_item"] = {
                        "from_indices": holders,
                        "team_size": len(state.team),
                    }
        return result
    return {}


class Engine:
    """Thin stateful wrapper: `reset()`/`step()` mirror the eventual
    `gymnasium.Env` shape on purpose (CLAUDE.md's Phase 3 note), but nothing
    below imports `gymnasium` or knows about observation/action spaces --
    that's Phase 3's job, deliberately deferred.

    **Owns a private RNG stream (CODEX.md issue 15).** Every module this
    engine calls into (`battle.py`/`battle_loop.py`/`battle_abilities.py`/
    `battle_traits.py`/`map_gen.py`) draws randomness through
    `pokelike.rng`'s module-level `rng()`, which -- faithfully mirroring the
    JS's own single process-wide global -- defaults to one shared stream for
    every caller. Left alone, two `Engine` instances in the same process
    would silently share that stream: stepping one would advance/reseed the
    RNG state the other's next roll depends on. Each `Engine` instead
    creates its own `rng.Mulberry32` in `__init__` and swaps it in as the
    module's "active" stream (`rng.set_active_stream`) for the exact
    duration of every `reset()`/`step()` call, restoring whatever was active
    before on the way out. Since neither method is reentrant/async, two
    engines' calls never actually interleave mid-body -- swapping a single
    pointer around each top-level call is sufficient for full independence
    without threading an RNG instance through every function signature in
    the modules above (see `rng.py`'s own docstring for the same rationale).
    """

    def __init__(self) -> None:
        self.state: Optional[RunState] = None
        self._rng_stream = rng.new_stream()

    def reset(
        self,
        *,
        nuzlocke_mode: bool = False,
        gen2_mode: bool = False,
        gen3_mode: bool = False,
        gen4_mode: bool = False,
        shiny_charm: bool = False,
        seed: Optional[int] = None,
        passives: Sequence[Trait] = (),
    ) -> RunState:
        """Port of `startNewRun` (docs/logic-notes-runlifecycle.md section
        1), split into two steps the way the real game's UI is: this seeds
        the RNG and builds the empty-team `state`, leaving `Phase.CHOOSE_
        STARTER` pending; `step(ChooseStarter(...))` finishes what
        `selectStarter`+`startMap(0)` do in the source. Generation unlocks
        (`gen2_mode`/etc.) are plain caller-supplied booleans, matching
        `startNewRun`'s own parameters -- the account-level Hall-of-Fame
        unlock gate they come from in the source is out of scope for a
        per-run engine (docs/logic-notes-runlifecycle.md section 3).

        Exactly one of `gen2_mode`/`gen3_mode`/`gen4_mode` may be True at a
        time (or none, for Gen1) -- CODEX.md issue 14: the source's
        generation picker is a single account-level choice, never several
        generations "unlocked" simultaneously for one run.

        `shiny_charm` stands in for the source's `hasShinyCharm()`
        (bundle.deobfuscated.js:48965-48967), which is just
        `isPokedexComplete()` -- an account-level Hall-of-Fame/Pokedex-
        completion flag entirely outside any single run's `state`, the same
        category of out-of-episode account state as `gen2_mode`/etc.
        Doubles standard and legendary shiny chance when True (CODEX.md
        issue 6, `roll_shiny`).
        """
        if sum(bool(f) for f in (gen2_mode, gen3_mode, gen4_mode)) > 1:
            raise ValueError(
                "at most one of gen2_mode/gen3_mode/gen4_mode may be set -- "
                "generation selection is mutually exclusive (CODEX.md issue 14)"
            )
        previous = rng.set_active_stream(self._rng_stream)
        try:
            run_seed = seed if seed is not None else rng.new_run_seed()
            rng.seed_rng(run_seed)
            state = RunState(
                nuzlocke_mode=nuzlocke_mode,
                gen2_mode=gen2_mode,
                gen3_mode=gen3_mode,
                gen4_mode=gen4_mode,
                shiny_charm=shiny_charm,
                run_seed=run_seed,
                passives=list(passives),
            )
            generation = _generation(state)
            # `showStarterSelect`'s Story/Nuzlocke branch, bundle.deobfuscated.js:
            # 76175-76194. It materialises the offer BEFORE the player clicks:
            # for each fetched starter entry, in the fixed order of
            # `STARTER_IDS`/`GEN2_`/`GEN3_`/`GEN4_STARTER_IDS` (75649-75655), it
            # runs `const BIV = rollShiny(), BIv = createInstance(BIj, B2l, BIV,
            # 0x0)` and closes the card's own click listener over that exact
            # instance (`addEventListener("click", () => selectStarter(BIv))`,
            # 76186). `rollShiny` (74912-74923) ends in an unconditional `rng() <
            # O`, so THREE draws are consumed here, before any input, and each
            # offered starter carries whatever shininess its own roll produced.
            # `B2l` is the literal starter level 5 (75648) and the `0x0` is
            # `createInstance`'s `moveTier` argument.
            #
            # This is the whole of blocker 1/1(b): the port used to offer
            # species metadata, draw nothing, build the chosen starter at click
            # time and force `is_shiny = False`, which both made a shiny starter
            # unreachable and offset every later Stream-B draw by three.
            starter_offers = []
            for sid in data.get_starter_ids(generation):
                is_shiny = roll_shiny(state)
                starter_offers.append(
                    _make_wild_combatant(
                        sid,
                        _STARTER_LEVEL,
                        is_shiny=is_shiny,
                        move_tier=0,
                        gen2_mode=state.gen2_mode,
                        gen4_mode=state.gen4_mode,
                    )
                )
            state.pending = PendingChoice(
                phase=Phase.CHOOSE_STARTER,
                options=[{"species_id": m.species_id, "name": m.name} for m in starter_offers],
                optional=False,
                # The real pending instances, in displayed order. `step(
                # ChooseStarter(...))` hands back the object at the matching
                # index rather than building a fresh one, so the starter that
                # enters the team is the same object the offer screen showed --
                # including its already-rolled `is_shiny` and its offer-time
                # level/base stats/HP. The two non-selected instances are
                # discarded on selection and have no gameplay effect beyond the
                # RNG draws their `rollShiny` calls already consumed, exactly
                # like the source's two unclicked cards.
                extra={"instances": starter_offers},
            )
            state.phase = Phase.CHOOSE_STARTER
            self.state = state
            return state
        finally:
            rng.set_active_stream(previous)

    def step(self, action: Action) -> RunState:
        state = self.state
        if state is None:
            raise RuntimeError("call reset() before step()")
        if state.phase in (Phase.GAME_OVER, Phase.VICTORY):
            raise ValueError(f"run has ended ({state.phase.value}); call reset() to start a new run")
        previous = rng.set_active_stream(self._rng_stream)
        try:
            _dispatch_action(state, action)
            return state
        finally:
            rng.set_active_stream(previous)


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


def _generation(state: RunState) -> int:
    return 4 if state.gen4_mode else 3 if state.gen3_mode else 2 if state.gen2_mode else 1


def _dispatch_action(state: RunState, action: Action) -> None:
    if state.phase == Phase.CHOOSE_STARTER:
        if not isinstance(action, ChooseStarter):
            raise ValueError("expected ChooseStarter while choosing a starter")
        generation = _generation(state)
        if action.species_id not in data.get_starter_ids(generation):
            raise ValueError(f"{action.species_id} is not a valid starter for this run")
        # `selectStarter(BIv)` (bundle.deobfuscated.js:76194-76210) receives the
        # exact instance its own card closed over, and `state["team"] = [B]`
        # (76206) installs THAT object. So this picks the already-built,
        # already-shiny-rolled offer at the clicked index; it does NOT rebuild
        # the species, and it does not overwrite `is_shiny`. Offered species are
        # distinct by construction (three different starter ids), so the id
        # identifies the clicked card unambiguously.
        instances = state.pending.extra["instances"]
        mon = next(m for m in instances if m.species_id == action.species_id)
        state.team = [mon]
        state.starter_species_id = action.species_id
        state.max_team_size = 1
        state.pending = None
        _start_map(state, 0)
        return

    if state.phase == Phase.ON_MAP:
        if isinstance(action, VisitNode):
            _visit_node(state, action.node_id)
            return
        if isinstance(action, ReorderTeam):
            _apply_reorder_team(state, action)
            return
        if isinstance(action, UseItem):
            _apply_use_item(state, action)
            return
        if isinstance(action, EquipItem):
            _apply_equip_item(state, action)
            return
        if isinstance(action, UnequipItem):
            _apply_unequip_item(state, action)
            return
        if isinstance(action, HandOffItem):
            _apply_hand_off_item(state, action)
            return
        raise ValueError(
            "expected VisitNode/ReorderTeam/UseItem/EquipItem/UnequipItem/"
            "HandOffItem while on the map"
        )

    if state.phase == Phase.NEXT_MAP_READY:
        if not isinstance(action, AdvanceMap):
            raise ValueError("expected AdvanceMap after a boss win")
        # Badge-advance clamp, docs/logic-notes-runlifecycle.md section 3
        # (bundle.deobfuscated.js:81465-81471).
        if state.current_map >= 7:
            state.elite_index = 0
            _start_map(state, 8)
        else:
            _start_map(state, state.current_map + 1)
        return

    if not isinstance(action, SelectOption):
        raise ValueError(f"expected SelectOption while resolving {state.phase.value}")
    _resolve_pending(state, action)


def _start_map(state: RunState, map_index: int) -> None:
    """Port of `startMap` (docs/logic-notes-runlifecycle.md section 2) --
    the ONLY place a map gets (re)generated. Full-team heal is conditional
    on `map_index > 0`, matching the source exactly (map 0's team was just
    created full-HP, so it doesn't need it)."""
    state.current_map = map_index
    state.map = map_gen.generate_map(
        map_index,
        nuzlocke_mode=state.nuzlocke_mode,
        gen2_mode=state.gen2_mode,
        gen3_mode=state.gen3_mode,
        gen4_mode=state.gen4_mode,
        flags=ChallengeFlags(),
        run_seed=state.run_seed,
    )
    if map_index > 0:
        for mon in state.team:
            mon.current_hp = mon.max_hp
    # `saved_question_resolve` is deliberately NOT cleared here (M4.2): the
    # source's only clear sites for it are `#btn-skip-catch` (78957),
    # `catchPokemon`'s room accept (79042), `showSwapScreen`'s three exits
    # (79183/79228/79253) and run teardown (84449) -- a map advance is not
    # among them. A stale record is harmless because `_resolve_question`'s
    # key is map-qualified (CODEX.md issue 9), so a later map's question node
    # can never match it. The former `question_cache.clear()` here was
    # described as belt-and-suspenders for exactly that reason; with a single
    # slot there is nothing to bound, and keeping the clear would have been a
    # divergence the new `resume_state` projection compares.
    state.current_node_id = "n0_0"
    state.phase = Phase.ON_MAP
    state.pending = None
    _log(state, "start_map", map_index=map_index)


def _resolve_pending(state: RunState, action: SelectOption) -> None:
    pending = state.pending
    if pending is None:
        raise ValueError(f"no pending choice for phase {state.phase.value}")
    if action.cancel:
        # `#btn-equip-cancel` exists on exactly one screen; see SelectOption.
        # Rejected everywhere else so a caller cannot get a silent skip.
        if state.phase != Phase.ITEM_EQUIP_CHOICE:
            raise ValueError(f"{state.phase.value} has no cancel affordance")
        if action.index is not None:
            raise ValueError("cancel takes no option index")
        _PENDING_RESOLVERS[state.phase](state, action)
        return
    if action.index is not None and not (0 <= action.index < len(pending.options)):
        raise ValueError(f"index {action.index} out of range for {len(pending.options)} options")
    if action.index is None and not pending.optional:
        raise ValueError(f"a choice is required for {state.phase.value}, it cannot be skipped")

    resolver = _PENDING_RESOLVERS.get(state.phase)
    if resolver is None:
        raise ValueError(f"phase {state.phase.value} does not accept SelectOption")
    resolver(state, action)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _log(state: RunState, event_type: str, **fields) -> None:
    state.log.append({"type": event_type, **fields})


def _mon_summary(mon: Combatant) -> dict:
    return {
        "species_id": mon.species_id,
        "name": mon.name,
        "level": mon.level,
        "current_hp": mon.current_hp,
        "max_hp": mon.max_hp,
        "status": mon.status,
        "is_shiny": mon.is_shiny,
        "held_item": mon.held_item.id if mon.held_item is not None else None,
    }


def _shuffle(items: list) -> None:
    """Fisher-Yates via the global RNG stream, same idiom as
    `map_gen.get_catch_choices`'s inline shuffle (docs/logic-notes.md
    section 6.5)."""
    for i in range(len(items) - 1, 0, -1):
        j = int(rng.rng() * (i + 1))
        items[i], items[j] = items[j], items[i]


def _advance(state: RunState, node_id: str) -> None:
    """Port of `advanceFromNode` (docs/logic-notes-nodes.md section 12) --
    marks the node visited, locks its same-layer siblings, and reveals its
    successors, once the node RESOLVES.

    Its sibling-lock loop duplicates `onNodeClick`'s eager pre-dispatch one
    (`_lock_same_layer_siblings`, run from `_visit_node`) and the two are
    idempotent, exactly as they are in the source, which also carries both.
    The duplication is deliberate and is kept: `advanceFromNode` is called
    from paths that never went through a node click at all (submap return,
    reward resolution), so it cannot rely on the eager lock having run."""
    assert state.map is not None
    node = state.map.nodes[node_id]
    node.visited = True
    node.accessible = False
    for other in state.map.nodes.values():
        if other.layer == node.layer and other.id != node_id and other.accessible:
            other.accessible = False
    for src, dst in state.map.edges:
        if src == node_id:
            dst_node = state.map.nodes[dst]
            dst_node.revealed = True
            dst_node.accessible = True


def _try_add_to_team(state: RunState, mon: Combatant, node_id: str, origin: Optional[str] = None) -> None:
    """Port of `catchPokemon` (bundle.deobfuscated.js:79026-79046): add
    directly if there's room, else prompt a swap/release choice.

    `origin` mirrors `recordMonOrigin`'s node-type check (79047-79063),
    called ONLY from `catchPokemon`'s ROOM branch (79040) -- the full-team
    branch calls `showSwapScreen` directly (79045) and never reaches
    `recordMonOrigin` at all. So `used_ball_catch`/`got_via_question` must be
    set here, in the room branch only, never on the swap-offer branch below.
    Found tracing the exact source for the M4 route-oracle catch/shiny
    bridge work: a prior version set these flags unconditionally at CHOICE
    time (in `_resolve_catch_choice`, before this function even ran), which
    was a real divergence for a question/shiny catch with a full team, not a
    simplification.

    `origin` is exactly the RAISING NODE'S OWN `type`, which is what
    `recordMonOrigin` switches on -- `"catch"` is `NODE_TYPES.CATCH`, and
    both `"question"` and `"shiny_node"` are `NODE_TYPES.QUESTION`. A shiny
    encounter is reachable ONLY as a QUESTION resolution: `onNodeClick`
    computes `iu = resolveQuestionMark()` for a QUESTION node
    (77318-77332) and dispatches on `iu` via `case "shiny"` (77384), but
    never rebinds `B`, so `doShinyNode(B)` -- and therefore its
    `recordMonOrigin(B)` at 80967 -- still receives the QUESTION node.
    There is no `NODE_TYPES.SHINY`; `"shiny"` exists only as a resolved
    type. `"shiny_node"` is kept as a DISTINCT origin string purely so
    `run_scenario.py:_is_shiny_origin` can tell a shiny offer apart from an
    ordinary question-resolved catch (both use `Phase.CATCH_CHOICE`) -- it
    is a projection discriminator, not a different `recordMonOrigin`
    outcome.

    M4.1: this used to set NO flag for `"shiny_node"`, on the strength of a
    `_visit_shiny` docstring claiming `doShinyNode`'s accept handler "never
    calls `catchPokemon`/`recordMonOrigin` at all". That claim is false
    against the current source -- 80967 is a bare `recordMonOrigin(B)` in
    the room branch's comma sequence. The divergence was invisible to the
    whole 24-scenario matrix because all five of its shiny resolutions were
    DECLINES, and the decline handler (`#btn-skip-shiny`, 80984-80989)
    genuinely does not call `recordMonOrigin`. Routing a shiny
    accept-with-room (`story_gen1_shiny_accept`) exposed it immediately as
    `counters.got_via_question` js=true / py=false."""
    if len(state.team) < TEAM_CAP:
        state.team.append(mon)
        state.max_team_size = max(state.max_team_size, len(state.team))
        if origin == "catch":
            state.used_ball_catch = True
        elif origin in ("question", "shiny_node"):
            state.got_via_question = True
        # Which resume record a ROOM ACCEPT consumes is branch-specific, and
        # the two branches clear DIFFERENT fields (M4.2):
        #   * `doShinyNode`'s room branch (80962) clears `savedShinyNode`
        #     alone -- it leaves `savedQuestionResolve` set, even though a
        #     shiny node is always a QUESTION node, and never touches
        #     `savedCatch`;
        #   * `catchPokemon`'s room branch (79041-79042) clears `savedCatch`
        #     and `savedQuestionResolve`, and never touches `savedShinyNode`.
        # Collapsing the two into one "clear everything" would be invisible
        # to the team/counter projection and is precisely what the
        # `resume_state` checkpoint field exists to catch.
        if origin == "shiny_node":
            state.saved_shiny_node = None
        else:
            state.saved_catch = None
            state.saved_question_resolve = None
        _log(state, "catch", species_id=mon.species_id, name=mon.name, is_shiny=mon.is_shiny)
        _advance(state, node_id)
        state.phase = Phase.ON_MAP
        state.pending = None
    else:
        state.pending = PendingChoice(
            phase=Phase.SWAP_CHOICE,
            options=[_mon_summary(m) for m in state.team],
            optional=True,
            extra={"incoming": mon, "node_id": node_id},
        )
        state.phase = Phase.SWAP_CHOICE


def _offer_swap_screen(state: RunState, mon: Combatant, node_id: str) -> None:
    """Port of `showSwapScreen` (bundle.deobfuscated.js:79141-79259) --
    UNLIKE `_try_add_to_team`/`catchPokemon`'s room-based auto-add, this
    ALWAYS presents an explicit accept/decline choice, whether or not the
    team has room. With room, the only clickable option is the incoming
    Pokemon itself (`iu` branch, bundle.deobfuscated.js:79171-79201: click
    to add, no swap target needed); full, it's the ordinary per-member
    release choice `_resolve_swap_choice` already models. Either way, the
    cancel button ALWAYS advances the node without changing the team
    (bundle.deobfuscated.js:79247-79258) -- declining consumes the node
    visit exactly like accepting, matching `PendingChoice.optional=True`
    with `action.index is None`.

    Used by the submap fossil/Giratina/Dialga/Palkia rewards
    (`doSubMapReward`'s `case "fossil"`/`case "giratina"`/... call
    `showSwapScreen` directly, bundle.deobfuscated.js:77076/77096) -- NOT
    `catchPokemon`'s room-based auto-add path, which only ordinary
    catch/shiny/legendary/trade flows use."""
    has_room = len(state.team) < TEAM_CAP
    options = [_mon_summary(mon)] if has_room else [_mon_summary(m) for m in state.team]
    state.pending = PendingChoice(
        phase=Phase.SWAP_CHOICE,
        options=options,
        optional=True,
        extra={"incoming": mon, "node_id": node_id, "has_room": has_room},
    )
    state.phase = Phase.SWAP_CHOICE


def _shiny_chance(state: RunState) -> float:
    """Port of `rollShiny`/`legendaryShinyChanceFlat`'s shared probability
    formula (bundle.deobfuscated.js:74912-74923, 74946-74957): 1% base, 2%
    with the Shiny Charm (`state.shiny_charm`, an explicit stand-in for the
    source's account-level `hasShinyCharm()`), doubled again if the
    `shiny_rate` trait is active. Both source functions are identical up to
    this point -- `legendaryShinyChanceFlat` just returns the number instead
    of also rolling against it, for a call site
    (`doLegendaryNode`/bundle.deobfuscated.js:80427-80429) that wants the
    percentage for display before rolling. `roll_shiny` below covers both
    uses with a single RNG-consuming helper (CODEX.md issues 5/6)."""
    chance = 0.02 if state.shiny_charm else 0.01
    if battle.has_passive(state.passives, _SHINY_RATE_TRAIT_ID):
        chance *= 2
    return chance


def roll_shiny(state: RunState) -> bool:
    """Port of `rollShiny()` (bundle.deobfuscated.js:74912-74923). Consumes
    exactly one `rng()` draw, matching every catch/trade/legendary call site
    that calls it once per candidate."""
    return rng.rng() < _shiny_chance(state)


def _make_wild_combatant(
    species_id: int,
    level: int,
    *,
    is_shiny: bool = False,
    move_tier: int = 1,
    gen2_mode: bool = False,
    gen4_mode: bool = False,
) -> Combatant:
    """Port of `createInstance` (docs/logic-notes-nodes.md section 1) --
    the sole factory for a battle-ready Pokemon instance in the source,
    used for wild/trainer/boss/catch/legendary/trade/shiny mons alike.
    `held_item` is deliberately never set here -- callers that need one
    (fixed trainer rosters) set it themselves afterward, matching the
    source's own `{...createInstance(...), heldItem: ...}` spread pattern.
    """
    mon_data = data.get_pokedex()[species_id]
    base_stats = mon_data.base_stats
    if is_shiny and gen2_mode:
        # +20% base stats, Gen2-mode-only shiny bonus (docs/logic-notes-nodes.md
        # section 1, bundle.deobfuscated.js:49241-49249).
        base_stats = dataclasses.replace(
            base_stats,
            hp=round(base_stats.hp * 1.2),
            atk=round(base_stats.atk * 1.2),
            defense=round(base_stats.defense * 1.2),
            speed=round(base_stats.speed * 1.2),
            special=round(base_stats.special * 1.2),
            spdef=round(base_stats.spdef * 1.2) if base_stats.spdef is not None else None,
        )
    ability = battle_abilities.get_gen3_ability(species_id, gen4_mode)
    max_hp = 1 if ability == "wonder_guard" else map_gen.calc_hp(base_stats.hp, level)
    return Combatant(
        species_id=species_id,
        level=level,
        base_stats=base_stats,
        types=mon_data.types,
        max_hp=max_hp,
        current_hp=max_hp,
        name=mon_data.name,
        move_tier=max(0, min(2, move_tier)),
        is_shiny=is_shiny,
    )


def _build_roster_team(roster: Sequence[data.TrainerPokemon], move_tier: int, gen4_mode: bool) -> list:
    """Shared `createInstance`-plus-heldItem-spread team builder for every
    fixed (non-procedural) roster table -- gym leaders/Elite Four
    (`_build_fixed_team`, per-trainer `moveTier` field) as well as the
    Silver/Magma/Aqua special-rival rosters (`_visit_silver`/`_visit_admin`,
    move tier from `getMoveТierForMap` instead, since those tables carry no
    per-entry `moveTier` field of their own)."""
    team = []
    for tp in roster:
        held = HeldItem(id=tp.held_item["id"]) if tp.held_item else None
        ability = battle_abilities.get_gen3_ability(tp.species_id, gen4_mode)
        max_hp = 1 if ability == "wonder_guard" else map_gen.calc_hp(tp.base_stats.hp, tp.level)
        team.append(
            Combatant(
                species_id=tp.species_id,
                level=tp.level,
                base_stats=tp.base_stats,
                types=tp.types,
                max_hp=max_hp,
                current_hp=max_hp,
                name=tp.name,
                held_item=held,
                move_tier=move_tier,
            )
        )
    return team


def _build_fixed_team(trainer: data.Trainer, state: RunState) -> list:
    """Port of `doBossNode`'s team-construction spread (docs/logic-notes-
    nodes.md section 2) -- `resolveTrainerTeamEvolutions` is a confirmed
    no-op stub in the source, so it's not replicated (the fixed roster data
    is already the correct evolved species/level as-authored)."""
    move_tier = trainer.move_tier if trainer.move_tier is not None else 1
    return _build_roster_team(trainer.team, move_tier, state.gen4_mode)


def _gym_leader(state: RunState) -> data.Trainer:
    leaders = data.get_gym_leaders(_generation(state))
    return leaders[min(state.current_map, len(leaders) - 1)]


def _elite_four_roster(state: RunState) -> tuple:
    return data.get_elite_four(_generation(state))


def _battle_configs(state: RunState, enemy_team: Sequence[Combatant]):
    """Port of `runBattleScreen`'s battle-config construction
    (bundle.deobfuscated.js:81067-81085) -- **not** a fixed "always build
    both" wiring. The source only ever builds a non-null battle config in
    two disjoint branches:

    - `state.isEndlessMode` (out of scope this session, `map_gen.py`/
      `engine.py` don't model Endless): `computeTraitTiers` against the
      LIVE team + `buildTraitsConfig` with the ENEMY tier map left `{}`,
      optionally merged with `buildGen3AbilityConfig()` if a Gen3/Gen4
      challenge flag is set.
    - Ordinary (non-Endless) `gen3Mode || gen4Mode`: `buildGen3AbilityConfig()`
      merged with `buildTraitsConfig({}, {}, passives)` -- note BOTH tier
      maps are `{}` here, not `compute_trait_tiers(state.team)` -- ordinary
      Story/Nuzlocke battles never get the automatic per-type tier bonus,
      only whatever individually-named traits `state.passives` happens to
      contain (normally none, since trait acquisition is Endless-only).

    Ordinary Gen1/Gen2 (neither branch matches) gets **no battle config at
    all** -- `ability_config`/`traits_config` are both `None`. This does NOT
    disable every trait effect: `battle_loop.run_battle`'s own inline checks
    (`rand_start`/`sof_double`/`lead_speed`/etc, all directly `has_passive`
    against the `traits` argument, not through this hook object) still run
    regardless, matching the source's separate `hasPassive(passives, ...)`
    call sites inside `runBattle` itself that don't go through `B71`.

    **`traits_config` is `None`, not an empty object, when there is nothing
    for it to do.** `buildTraitsConfig` itself returns `null` when both tier
    maps AND the passives list are empty (bundle.deobfuscated.js:60733-
    60738) -- for ordinary Story/Nuzlocke that means `traits_config` is only
    ever non-`None` if the player has picked up at least one named passive
    (tier maps are always `{}` here). This matters beyond "skip a no-op
    call": whether `traits_config` is `None` decides whether `runBattle`'s
    battle config is `mergeBattleConfigs(ability, traits)` or bare `ability`
    -- and `mergeBattleConfigs` changes generic-hook (`beforeTurn`/
    `onBeforeAttack`/`isTrickRoom`) return-value semantics, see
    `battle_loop.run_battle`'s own docstring. Building a real (if inert)
    `TraitsConfig` here unconditionally, as an earlier version of this
    function did, silently forced every Gen3/Gen4 battle into the merged
    path even with zero passives -- itself a source of false "merged"
    results.
    """
    if not (state.gen3_mode or state.gen4_mode):
        return None, None
    ability_config = battle_abilities.Gen3AbilityConfig(gen4_mode=state.gen4_mode)
    player_tiers: dict = {}
    enemy_tiers: dict = {}
    if player_tiers or enemy_tiers or state.passives:
        traits_config = battle_traits.TraitsConfig(player_tiers=player_tiers, enemy_tiers=enemy_tiers, traits=state.passives)
    else:
        traits_config = None
    return ability_config, traits_config


def _wg_max_hp(species_id: int, gen4_mode: bool, computed: int) -> int:
    """Port of `wgMaxHp` (bundle.deobfuscated.js:49227-49234): Wonder Guard's
    1-HP clamp is derived from the SPECIES id via `getGen3Ability`, never
    from a battler's own possibly-Trace-mutated `_gen3Ability` field. Used
    only by the persistent-roster HP recomputation paths (post-battle
    copy-back, level gain, Rare Candy) -- in-battle checks correctly keep
    using the battle-local (possibly Traced) `Combatant.gen3_ability`
    (CODEX.md issue 20)."""
    return 1 if battle_abilities.get_gen3_ability(species_id, gen4_mode) == "wonder_guard" else computed


def _copy_back_battle_result(state: RunState, clone_team: Sequence[Combatant], player_won: bool) -> None:
    """Port of `runBattleScreen`'s selective post-battle copy-back from the
    battle-local clone (`Bch` = `runBattle`'s returned `pTeam`) onto the
    persistent `state.team` objects (bundle.deobfuscated.js:81283-81318 on a
    win, 81389-81391 on a loss). This is the ONLY channel through which
    battle-local mutations reach the persistent roster -- everything else
    the clone accumulated (types/base_stats changes from Ditto/Multitype/
    Forecast/Color Change/Deoxys, a Traced `gen3_ability`, `stages`,
    `status`, one-shot `flags`, ...) is discarded with the clone
    (CODEX.md issues 3-4). Iterates by `state.team` length/index, matching
    the source's `for (i=0; i<state.team.length; i++)` -- NOT the clone's
    length -- so a clone array padded/shaped differently never desyncs
    persistent indices.
    """
    for idx, orig in enumerate(state.team):
        if idx >= len(clone_team):
            continue
        clone = clone_team[idx]
        if not player_won:
            orig.current_hp = clone.current_hp
            continue
        if clone.flags.get("_runSpeedStage"):
            orig.flags["_runSpeedStage"] = clone.flags["_runSpeedStage"]
        if clone.flags.get("_runMaxHp"):
            orig.flags["_runMaxHp"] = clone.flags["_runMaxHp"]
        if clone.level != orig.level:
            orig.level = clone.level
            orig.max_hp = clone.max_hp
        if orig.flags.get("_runMaxHp"):
            hp_buff = (orig.stat_buffs or {}).get("hp", 0)
            computed = math.floor(map_gen.calc_hp(orig.base_stats.hp, orig.level) * (1 + 0.05 * hp_buff))
            computed += orig.flags.get("_runMaxHp", 0)
            orig.max_hp = _wg_max_hp(orig.species_id, state.gen4_mode, computed)
        orig.current_hp = min(clone.current_hp, orig.max_hp)


def _apply_mega_evolution(mon: Combatant) -> bool:
    """Port of `syncMegaForm`/`applyMegaEvolution` (bundle.deobfuscated.js:
    86461-86515, CODEX P0.8). Purely a function of `mon.held_item`:
    transforms in place when the held item is a Mega Stone matching
    `mon.species_id`, restores from the saved `_baseForm` when a
    previously-mega'd mon no longer qualifies (item removed/swapped since
    the last time this ran), and is a no-op in every other case (including
    an already-mega'd mon that still qualifies -- the source's own `if`
    chain never re-enters either branch then). `_megaEvolved`/`_baseForm`
    live in `flags` (matching the source's own ad-hoc field names), same
    convention as every other `Combatant.flags` entry.

    **Must reassign `mon.flags`, never mutate it in place.** This runs on a
    freshly `battle_loop.clone_combatant`'d member, BEFORE `battle_loop.
    run_battle`'s own `_init_battle_state` gets a chance to give the clone
    its own fresh `flags` dict -- until then, `clone.flags is
    state.team[i].flags` (a shallow `copy.copy` shares nested mutable
    fields). Writing `mon.flags[key] = ...` here would silently leak onto
    the PERSISTENT roster object through that shared dict, bypassing the
    entire `_copy_back_battle_result` contract -- exactly the "mutable
    dict/list alias" hazard this port's tests must rule out.

    Sprite/`spriteUrl` handling (`iu["megaSprite"]`/shiny-path substitution,
    bundle.deobfuscated.js:86484-86488) is deliberately NOT ported: no
    `Combatant` field anywhere in this engine tracks a sprite path
    (console/web renderers key off `species_id`/`name`; CLAUDE.md's "js/
    ui.js is reference-only"). Only the battle-relevant fields
    (`base_stats`, `types`, `name`) are transformed/restored.

    Acquisition (`isMegaBraceletUnlocked`/`ownsMegaStone`/MVP-count tiers,
    bundle.deobfuscated.js:86387-86442 -- whether/how a player is ALLOWED to
    equip a given Mega Stone at all) is explicitly out of scope: this
    function, like the source's own `applyMegaEvolution`, only cares
    whether `mon.held_item` already IS a qualifying Mega Stone.

    No generation/mode gate exists in the source for the mechanic itself --
    `BcV.forEach(applyMegaEvolution)` runs unconditionally for every
    Story/Nuzlocke battle regardless of gen1/2/3/4 mode.
    """
    item = mon.held_item
    eligible = bool(
        item is not None
        and item.is_mega_stone
        and item.mega_species is not None
        and item.mega_species == mon.species_id
        and item.mega_stats is not None
    )
    was_evolved = bool(mon.flags.get("_megaEvolved"))
    if eligible and not was_evolved:
        mon.flags = {
            **mon.flags,
            "_baseForm": {"base_stats": mon.base_stats, "types": mon.types, "name": mon.name},
            "_megaEvolved": True,
        }
        mon.base_stats = item.mega_stats
        if item.mega_types:
            mon.types = tuple(item.mega_types)
        mon.name = item.mega_name or mon.name
        return True
    if not eligible and was_evolved:
        base_form = mon.flags.get("_baseForm") or {}
        if base_form.get("base_stats") is not None:
            mon.base_stats = base_form["base_stats"]
        if base_form.get("types") is not None:
            mon.types = base_form["types"]
        if base_form.get("name") is not None:
            mon.name = base_form["name"]
        mon.flags = {**mon.flags, "_megaEvolved": False, "_baseForm": None}
        return True
    return False


def _shiny_first_active(passives: Sequence[Trait]) -> bool:
    """`shiny_first`'s gate (bundle.deobfuscated.js:81136-81139) is
    `enabled !== !0x1`. JavaScript `!0x1` is `false`, so this is the same
    active-unless-explicitly-disabled convention as `battle.has_passive`.
    The identical deobfuscation idiom appears for `team_reroll` and
    `legend_traits` at lines 58434/58439 and 60702/60707/60712, and for
    `shiny_rate` at lines 74919/74953.
    """
    return any(t.id == "shiny_first" and t.enabled is not False for t in (passives or ()))


def _effort_ribbon_active(passives: Sequence[Trait]) -> bool:
    """Plain presence check, `enabled` ignored entirely (bundle.deobfuscated.js:
    81178-81179: `B2y.some(Bcp => Bcp.id === "effort_ribbon")` -- no
    `enabled` read at all, unlike either convention above)."""
    return any(t.id == "effort_ribbon" for t in (passives or ()))


def _mini_team_reduction(passives: Sequence[Trait]) -> int:
    """`mini_focus`/`mini_blade`/`solo_blitz` roster-size reduction
    (bundle.deobfuscated.js:81154-81168) -- each a plain presence check
    (`enabled` ignored, same as `effort_ribbon` above), additive when
    several are owned at once (+2/+2/+5 respectively)."""
    ids = {t.id for t in (passives or ())}
    reduction = 0
    if "mini_focus" in ids:
        reduction += 2
    if "mini_blade" in ids:
        reduction += 2
    if "solo_blitz" in ids:
        reduction += 5
    return reduction


def _build_battle_clone(state: RunState) -> list[Combatant]:
    """Port of `runBattleScreen`'s battle-local player-team clone and its
    four pre-battle transforms, IN SOURCE ORDER (bundle.deobfuscated.js:
    81115-81188, CODEX P0.8):

    1. `applyMegaEvolution` on every cloned member;
    2. `shiny_first` -- clone INDEX 0 only (not "first alive"), and only
       if it isn't already shiny;
    3. `mini_focus`/`mini_blade`/`solo_blitz` -- additive roster
       truncation, keeping the front `max(1, len-reduction)` members
       (order preserved, never truncated below 1 member);
    4. `effort_ribbon` -- +10 to every stat buff and 1.5x max HP, applied
       to every NON-shiny member remaining after step 3.

    Order matters for two source-confirmed interactions this port's tests
    exercise directly: a Mega-evolved member's `effort_ribbon` HP recompute
    reads the ALREADY-swapped Mega `base_stats.hp` (step 1 precedes step
    4), and a member `shiny_first` just made shiny in step 2 is excluded
    from `effort_ribbon` in step 4 (both gate on the same `is_shiny` field,
    and step 2 runs first).

    This is `state.team`'s ONLY transform boundary for a battle: `state.
    team` itself is never mutated here (every member is `battle_loop.
    clone_combatant`'d first) -- the transformed clone this returns is fed
    to `battle_loop.run_battle` (which clones it AGAIN internally, its own
    separate clone boundary), and only the existing narrow
    `_copy_back_battle_result` contract ever writes anything back onto
    `state.team` afterward. Matches the source's own double-clone structure
    (`state.team.map(p=>({...p}))` here, `runBattle`'s own
    `initBattleState({...p})` again inside it).
    """
    clone = [battle_loop.clone_combatant(mon) for mon in state.team]
    for mon in clone:
        _apply_mega_evolution(mon)
    if clone and not clone[0].is_shiny and _shiny_first_active(state.passives):
        clone[0].is_shiny = True
    reduction = _mini_team_reduction(state.passives)
    if reduction > 0:
        clone = clone[: max(1, len(clone) - reduction)]
    if _effort_ribbon_active(state.passives):
        for mon in clone:
            if not mon.is_shiny:
                mon.stat_buffs = dict(_EFFORT_RIBBON_STAT_BUFFS)
                mon.max_hp = math.floor(map_gen.calc_hp(mon.base_stats.hp, mon.level) * 1.5)
                mon.current_hp = mon.max_hp
    return clone


def _grant_gen3_zigzagoon_pickup(state: RunState) -> None:
    """Port of `runBattleScreen`'s immediate post-`runBattle` Gen3-only
    Pickup branch (bundle.deobfuscated.js:81223-81245, CODEX P0.7) --
    INDEPENDENT from `_grant_pickup_item` below (both can fire off the same
    win). Called from `_run_battle`, i.e. BEFORE copy-back/level-gain have
    run, so it reads `state.team` exactly as it stood going INTO the
    battle -- though since it only checks species membership (never
    `current_hp`), that timing is observationally moot here. Species
    membership only (Zigzagoon/Linoone, ids 0x107/0x108) -- no alive check
    (unlike `_grant_pickup_item`), no bag de-dup filtering (unlike
    `_grant_pickup_item`, which filters against held ids), explicit
    `state.gen3_mode` gate only (NEVER fires in Gen4 mode, unlike
    `_grant_pickup_item` which works off whichever ability table
    `get_gen3_ability` selects). Exactly two possible `rng()` draws: one
    for the 10% gate, and -- only if that passes -- one more for the
    ITEM_POOL index, matching the source's own draw count exactly.
    """
    if not state.gen3_mode:
        return
    if not any(m.species_id in _GEN3_PICKUP_SPECIES for m in state.team):
        return
    pool = data.get_passive_items()
    if not pool:
        return
    if rng.rng() >= 0.1:
        return
    item = pool[int(rng.rng() * len(pool))]
    state.items.append(item.id)


def _grant_pickup_item(state: RunState) -> None:
    """Port of `grantPickupItem` (bundle.deobfuscated.js:77615-77641,
    CODEX P0.7) -- called unconditionally on every Story/Nuzlocke win
    (source line 81331, right after `applyLevelGain`), independent of and
    in addition to `_grant_gen3_zigzagoon_pickup` above. Reads `state.team`
    AFTER copy-back and level-gain have already applied (called from
    `_after_battle`, itself invoked after those steps in every call site).

    Requires a currently-ALIVE (`current_hp > 0`) team member whose species
    resolves to the `pickup` ability via `get_gen3_ability` (Gen3 or Gen4
    table depending on `state.gen4_mode`, exactly `getGen3Ability`'s own
    table-selection rule) -- NOT restricted to any specific species, and
    NOT gated on `gen3_mode`/`gen4_mode` at this call site (the source
    calls this for every win regardless of generation; the practical
    gen-gating comes entirely from which species can ever be on the team,
    since the ability tables only assign "pickup" to Gen3/Gen4 dex ids).

    De-duplicates against the bag: only ITEM_POOL entries whose id is NOT
    already in `state.items` are eligible. Draws exactly one `rng()` for
    the 10% gate; if the gate passes but every ITEM_POOL entry is already
    owned, returns WITHOUT a second draw (matching the source's own early
    `if (!ip.length) return;` before its index roll) -- otherwise exactly
    one more draw picks the index.
    """
    if not state.team:
        return
    if not any(
        m.current_hp > 0 and battle_abilities.get_gen3_ability(m.species_id, gen4_mode=state.gen4_mode) == "pickup"
        for m in state.team
    ):
        return
    if rng.rng() >= 0.1:
        return
    held = set(state.items)
    pool = [item for item in data.get_passive_items() if item.id not in held]
    if not pool:
        return
    item = pool[int(rng.rng() * len(pool))]
    state.items.append(item.id)


def _run_battle(state: RunState, enemy_team: Sequence[Combatant]) -> BattleResult:
    ability_config, traits_config = _battle_configs(state, enemy_team)
    player_clone = _build_battle_clone(state)
    result = battle_loop.run_battle(
        player_clone,
        list(enemy_team),
        traits=state.passives,
        ability_config=ability_config,
        traits_config=traits_config,
        battle_config=BattleConfig(),
    )
    # R1: carry the streams out to the renderer. Shallow-copies each record so
    # a later mutation of `result` cannot alias into the snapshot. No RNG, no
    # control flow -- see `RunState.last_battle`.
    # R2/N2: the two rosters travel with the streams. `list(...)` snapshots the
    # sequence, not the `Combatant`s -- `_copy_back_battle_result` below reads
    # the same objects, so a renderer sees the post-battle state, which is what
    # a replay's final frame needs. Still no RNG, no control flow.
    # R4: the PRE-battle rosters travel alongside the post-battle ones. The
    # source's animation is seeded from both -- `animateBattleVisually` builds
    # its HP trackers from the pre-battle clones it is handed
    # (bundle.deobfuscated.js:69086-69092, called at 81272 with `BcV`/`BcT`,
    # both pre-battle), and only after the replay finishes does
    # `renderBattleField(Bch, BcL)` (81278) draw the post-battle teams. A
    # replay that only has the post-battle HP cannot draw the FIRST frame.
    # `run_battle` clones its two arguments before touching anything
    # (battle_loop.py:289-290), so `player_clone`/`enemy_team` still hold
    # pre-battle HP here -- this is a pure read, no RNG and no control flow,
    # exactly like the two lines above it.
    state.last_battle = {
        "battle_events": [dict(e) for e in result.battle_events],
        "status_events": [dict(e) for e in result.status_events],
        "rounds": result.rounds,
        "player_won": bool(result.player_won),
        "player_team": list(result.player_team),
        "enemy_team": list(result.enemy_team),
        "player_team_start": list(player_clone),
        "enemy_team_start": list(enemy_team),
    }
    # CODEX P0.7: the immediate Gen3 Pickup branch runs BEFORE copy-back in
    # the source (bundle.deobfuscated.js:81223-81245 precedes 81278-81318),
    # reading `state.team` pre-battle -- matched here for exact `rng()` draw
    # ordering even though this particular branch never reads `current_hp`.
    if result.player_won:
        _grant_gen3_zigzagoon_pickup(state)
    _copy_back_battle_result(state, result.player_team, result.player_won)
    return result


def _after_battle(
    state: RunState,
    result: BattleResult,
    level_gain: int,
    *,
    all_team_xp: bool = False,
    no_permadeath: bool = False,
    rope_eligible: bool = False,
    rope_continuation: Optional[list] = None,
) -> bool:
    """Port of the sequence `runBattleScreen` runs immediately after a
    battle resolves, MINUS the evolution check (a separate resumable step,
    see `_run_todo`): apply level gain, then (Nuzlocke) cull fainted team
    members, then decide whether the run continues
    (docs/logic-notes-runlifecycle.md sections 5-6). Returns False if the
    run just ended (loss, or a Nuzlocke total wipe) OR an eligible loss
    raised `Phase.ESCAPE_ROPE_CHOICE` instead -- callers must stop
    processing immediately when this returns False; `state.phase` is
    already set to whichever of the two applies.

    `rope_eligible`/`rope_continuation` model `runBattleScreen`'s loss
    branch (bundle.deobfuscated.js:81388-81429), CODEX.md P0.6: on a loss,
    the source finds `state["items"].findIndex(id === "escape_rope")` ONLY
    when its own second `isBoss` argument is falsy AND `!isEndlessMode`
    AND `!nuzlockeMode` -- traced per call site (not inferred from "non-
    boss" prose):
    `doBattleNode`/wild (bundle.deobfuscated.js:77724, isBoss=false),
    `doTrainerNode`/regular trainer (bundle.deobfuscated.js:80327,
    isBoss=false), and, confirmed by direct read despite reading as
    unusual, `doLegendaryNode` (bundle.deobfuscated.js:80439, isBoss=false)
    ARE eligible; `doBossNode`/gym leader (bundle.deobfuscated.js:77780,
    77829), `doElite4`/`doGen2Elite4` (bundle.deobfuscated.js:77871,
    78379), `doSilverNode` (bundle.deobfuscated.js:77936), and
    `doAdminNode`/Magma-Aqua (bundle.deobfuscated.js:77983) all pass
    isBoss=true and so are NOT eligible regardless of a rope in the bag.
    `rope_continuation` is the exact `state._todo` list the source's own
    win-side success callback (`iu`) would install for this same call site
    -- e.g. `[{"kind": "advance", ...}]` for a wild/trainer win, minus the
    `evolve` step, since accepting the rope re-enters that SAME success
    callback without ever running any of the win-branch code (level gain,
    Nuzlocke fainted-cull, evolution check) that precedes it in the source.
    """
    _log(
        state,
        "battle",
        won=result.player_won,
        rounds=result.rounds,
        player_team=[_mon_summary(m) for m in state.team],
        enemy_team=[_mon_summary(m) for m in result.enemy_team],
    )
    if result.player_won:
        participants = set(range(len(state.team))) if all_team_xp else result.player_participants
        _apply_level_gain(state.team, participants, level_gain, gen4_mode=state.gen4_mode)
        # CODEX P0.7: `grantPickupItem()` (source line 81331) runs right
        # after `applyLevelGain`, BEFORE the Nuzlocke fainted-cull below.
        _grant_pickup_item(state)
        # bundle.deobfuscated.js:81358-81380: the fainted-cull/held-item
        # release only exists inside `runBattleScreen`'s WIN branch (`if
        # (..., BcF)` at line 81278, `BcF` = `playerWon`) -- the loss branch
        # (`else`, line 81388) never touches `state["team"]`/`state["items"]`
        # at all, so a Nuzlocke loss preserves the fainted roster and their
        # held items untouched (they're only released once the run is
        # actually continuing). Culling unconditionally (regardless of
        # `result.player_won`) would silently strip a lost run's roster/
        # items before `GAME_OVER` even has a chance to read them.
        if state.nuzlocke_mode and not no_permadeath:
            culled = [m for m in state.team if m.current_hp <= 0]
            for mon in culled:
                if mon.held_item is not None:
                    state.items.append(mon.held_item.id)
            state.team = [m for m in state.team if m.current_hp > 0]
            # bundle.deobfuscated.js:81371-81372: `BI1["length"] > 0x0 &&
            # ((state["anyFainted"] = !0x0), ...)`. Gated on the cull having
            # actually removed something, inside the win branch -- NOT on merely
            # observing a faint during the battle.
            if culled:
                state.any_fainted = True
    if not result.player_won and rope_eligible and not state.nuzlocke_mode:
        rope_index = next((i for i, item_id in enumerate(state.items) if item_id == _ESCAPE_ROPE_ITEM_ID), None)
        if rope_index is not None:
            state.pending = PendingChoice(
                phase=Phase.ESCAPE_ROPE_CHOICE,
                options=[{"action": "use_escape_rope", "item_index": rope_index}],
                optional=True,
                extra={"rope_index": rope_index, "continuation": list(rope_continuation or [])},
            )
            state.phase = Phase.ESCAPE_ROPE_CHOICE
            _log(state, "escape_rope_offered", rope_index=rope_index)
            return False
    if not state.team or not result.player_won:
        state.phase = Phase.GAME_OVER
        state.game_over = True
        state.pending = None
        state._todo = []
        _log(state, "game_over")
        return False
    return True


def _apply_level_gain(team: list, participants: set, base_gain: int, level_cap: int = 100, gen4_mode: bool = False) -> None:
    """Port of `applyLevelGain` (docs/logic-notes-runlifecycle.md section
    5). There is no XP curve in the source at all -- leveling is a flat
    "+N levels per battle win", `base_gain` already encodes which N for this
    encounter type (see each `_visit_*` call site). Endless-only trait
    bonuses (`post_combat_lvl`, `bug_relevel`) are not modeled, consistent
    with trait acquisition being out of scope this session.
    """
    for idx, mon in enumerate(team):
        if not (mon.current_hp > 0 or idx in participants):
            continue
        gain = base_gain
        if mon.held_item is not None and mon.held_item.id == "lucky_egg" and rng.rng() < 0.3:
            gain += 1
        new_level = min(mon.level + gain, level_cap)
        if new_level == mon.level:
            continue
        old_max_hp = mon.max_hp
        computed = map_gen.calc_hp(mon.base_stats.hp, new_level)
        hp_buff = (mon.stat_buffs or {}).get("hp", 0)
        if hp_buff:
            computed = math.floor(computed * (1 + 0.05 * hp_buff))
        # `_runMaxHp` (the persistent `ko_maxhp` trait bonus, CODEX.md issue
        # 8) must be folded back in here -- bundle.deobfuscated.js:56835-
        # 56841's `applyLevelGain` adds it to the recomputed HP curve for
        # exactly this reason: without it, a level-up recompute silently
        # erases the accumulated bonus instead of preserving it.
        computed += mon.flags.get("_runMaxHp", 0)
        # Wonder Guard's 1-HP clamp is species-derived (`wgMaxHp`), never
        # read off the persistent `mon.gen3_ability` field -- that field
        # only reflects whatever a Traced battle last set it to and is
        # otherwise unset outside battle (CODEX.md issue 20).
        new_max_hp = _wg_max_hp(mon.species_id, gen4_mode, computed)
        if mon.current_hp > 0:
            mon.current_hp += max(0, new_max_hp - old_max_hp)
        mon.max_hp = new_max_hp
        mon.level = new_level


_NINJASK_ID = 291  # 0x123 -- Nincada's sole evolution target
_SHEDINJA_ID = 292  # 0x124


def _maybe_spawn_shedinja(state: RunState, evolved: Combatant) -> None:
    """Port of `spawnShedinjaIfNinjask` (bundle.deobfuscated.js:79848-79882)
    -- called after EVERY evolution (both `applyEvolution`'s Moon-Stone path
    and `checkAndEvolveTeam`'s automatic path), it's a no-op unless the mon
    that just evolved is now Ninjask (species 291, i.e. Nincada just
    evolved) and the team has an open slot (< `TEAM_CAP`). Spawns a fresh,
    full-HP Shedinja (292) at the same level/shininess/move-tier -- Wonder
    Guard's 1-HP clamp is applied automatically by `_make_wild_combatant`'s
    own species-based check, not duplicated here. The source's `_fromReroll`
    gate (a trade-reroll flag) is not modeled -- this engine has no trade
    reroll flow, so it's always the "normal" branch (CODEX.md issue 17).
    """
    if evolved.species_id != _NINJASK_ID or len(state.team) >= TEAM_CAP:
        return
    shedinja = _make_wild_combatant(
        _SHEDINJA_ID,
        evolved.level,
        is_shiny=evolved.is_shiny,
        move_tier=evolved.move_tier,
        gen2_mode=state.gen2_mode,
        gen4_mode=state.gen4_mode,
    )
    state.team.append(shedinja)
    state.max_team_size = max(state.max_team_size, len(state.team))


def _apply_evolution(state: RunState, mon: Combatant, into_species_id: int, *, force: bool = False) -> None:
    """Port of the per-mon stat update shared by `checkAndEvolveTeam`
    (`force=False`, docs/logic-notes-runlifecycle.md section 4,
    bundle.deobfuscated.js:70648-70669) and `applyEvolution` (`force=True`,
    Moon Stone's path, bundle.deobfuscated.js:79798-79826) -- these are TWO
    DISTINCT source functions with different HP-recompute formulas, not one
    function called two ways (CODEX.md issue 18):

    - `checkAndEvolveTeam` (force=False): HP = `floor(calcHp(newBaseStats.hp,
      level) * (1+0.05*hpBuff))`, no augment. A fainted mon (checked BEFORE
      the recompute) stays at 0 HP after evolving.
    - `applyEvolution` (force=True): HP additionally multiplies by
      `(1 + (augment_pct||0)/100)`, and -- a real, source-confirmed
      discrepancy, not a guess -- current HP is UNCONDITIONALLY
      `max(1, floor(fraction*newMaxHp))` even if the mon was fainted, so a
      Moon-Stone-forced evolution can revive a fainted teammate to 1 HP as
      a side effect of the HP-curve recompute. Neither path Wonder-Guard-
      clamps HP (a confirmed source discrepancy relative to
      `_apply_level_gain`, replicated as-is).
    """
    was_fainted = mon.current_hp <= 0
    hp_fraction = (mon.current_hp / mon.max_hp) if mon.max_hp else 0.0
    new_species = data.get_pokedex()[into_species_id]
    mon.species_id = into_species_id
    mon.name = new_species.name
    mon.types = new_species.types
    mon.base_stats = new_species.base_stats
    max_hp = map_gen.calc_hp(mon.base_stats.hp, mon.level)
    hp_buff = (mon.stat_buffs or {}).get("hp", 0)
    if hp_buff:
        max_hp = math.floor(max_hp * (1 + 0.05 * hp_buff))
    if force:
        augment_pct = mon.augment_pct or 0
        max_hp = math.floor(max_hp * (1 + augment_pct / 100))
        mon.max_hp = max_hp
        mon.current_hp = max(1, math.floor(hp_fraction * max_hp))
    else:
        mon.max_hp = max_hp
        mon.current_hp = 0 if was_fainted else max(1, math.floor(hp_fraction * max_hp))
    _maybe_spawn_shedinja(state, mon)


def _maybe_evolve_one(state: RunState, idx: int, *, source: str, force: bool = False) -> bool:
    """Shared per-mon evolution check used by both `_evolve_step` (the
    resumable post-battle team scan, `source="todo"`) and Moon
    Stone/Rare-Candy bag-item usage (`source="item"`, see `_apply_use_item`).
    `force=True` is Moon Stone's `applyEvolution` behavior (bundle.
    deobfuscated.js:79783-79800): skip the level requirement entirely,
    forcing whatever evolution exists (branching still means a real choice,
    not skipped). Returns True if a `Phase.EVOLUTION_CHOICE` was raised
    (caller must stop and let the player resolve it via
    `_resolve_evolution_choice`, which reads `extra["source"]` to know
    whether to resume `state._todo` or just return to `Phase.ON_MAP`)."""
    mon = state.team[idx]
    # eviolite doubles as this game's Everstone-equivalent -- see
    # docs/logic-notes-runlifecycle.md section 4, a deliberate deviation
    # from mainline (where Eviolite is a pure stat item).
    if mon.held_item is not None and mon.held_item.id == "eviolite":
        return False
    branches = data.get_branching_evolutions().get(mon.species_id)
    if branches and (force or mon.level >= branches[0].level):
        state.pending = PendingChoice(
            phase=Phase.EVOLUTION_CHOICE,
            options=[{"into": b.into, "name": b.name} for b in branches],
            optional=False,
            extra={"team_index": idx, "branches": branches, "source": source, "force": force},
        )
        state.phase = Phase.EVOLUTION_CHOICE
        return True
    evo = data.get_evolutions().get(mon.species_id)
    if evo is not None and (force or mon.level >= evo.level) and evo.into != mon.species_id:
        _apply_evolution(state, mon, evo.into, force=force)
        _log(state, "evolve", team_index=idx, into=evo.into, name=evo.name)
    return False


def _evolve_step(state: RunState, step: dict) -> bool:
    """Port of `checkAndEvolveTeam` (docs/logic-notes-runlifecycle.md
    section 4), split into a resumable step: processes team members
    starting at `step["idx"]`, evolving non-branching species automatically
    and pausing (returns True) the first time it hits a branching-eligible
    species, leaving `step["idx"]` at that member so resuming re-checks it
    after the choice is applied. Returns False once the whole team has been
    checked with nothing left pending.
    """
    team = state.team
    idx = step["idx"]
    while idx < len(team):
        if _maybe_evolve_one(state, idx, source="todo"):
            step["idx"] = idx
            return True
        idx += 1
    return False


# ---------------------------------------------------------------------------
# Bag item use / equip (CODEX.md issues 5, 9, 16, 36) -- port of
# `applyUsableItemTo`/`usableItemCanTarget`/`equipItemFromBag`
# (bundle.deobfuscated.js:79571-79654, 79652-79671). Endless-only levers
# (`isEndlessMode`'s uncapped Rare Candy level, `challengeNoEvo`) are not
# modeled, consistent with the rest of this module's Story/Nuzlocke scope.
# ---------------------------------------------------------------------------


def _usable_item_ids() -> frozenset[str]:
    """Bag item ids from `data.get_usable_items()` -- items the source's
    team-bar dispatch (bundle.deobfuscated.js:64943-64950) routes to
    `applyUsableItemTo`/`UseItem`, never `equipItemFromBag`/`EquipItem`
    (CODEX.md P0.5)."""
    return frozenset(item.id for item in data.get_usable_items())


def _passive_item_ids() -> frozenset[str]:
    """Bag item ids from `data.get_passive_items()` -- the only ids the
    source's dispatch ever routes to `equipItemFromBag`/`EquipItem`
    (CODEX.md P0.5)."""
    return frozenset(item.id for item in data.get_passive_items())


def _usable_item_can_target(item_id: str, mon: Combatant) -> bool:
    """Port of `usableItemCanTarget` (bundle.deobfuscated.js:79571-79583)."""
    if item_id == "sacred_ash":
        return mon.current_hp < mon.max_hp
    if item_id == "moon_stone":
        if mon.current_hp <= 0:
            return False
        if mon.species_id in data.get_branching_evolutions():
            return True
        evo = data.get_evolutions().get(mon.species_id)
        return bool(evo and evo.into != mon.species_id)
    if item_id == "tm_normal":
        return mon.current_hp > 0 and mon.move_tier < 2
    return True  # rare_candy: unconditionally eligible, even fainted -- see _apply_rare_candy


def _apply_rare_candy(mon: Combatant, gen4_mode: bool = False) -> None:
    """Port of `applyUsableItemTo`'s `rare_candy` branch
    (bundle.deobfuscated.js:79598-79619): +3 levels (capped at 100),
    recomputed HP folding in `_runMaxHp` (CODEX.md issue 8) the same way
    `_apply_level_gain` does. Uses `(currentHp||0) + max(0, delta)` --
    exactly like the source, this can partially or fully "revive" a fainted
    mon purely as a side effect of the HP-curve recompute, not a dedicated
    revive branch; replicated as-is rather than special-cased away.
    """
    for _ in range(3):
        if mon.level < 100:
            mon.level += 1
    hp_buff = (mon.stat_buffs or {}).get("hp", 0)
    computed = math.floor(map_gen.calc_hp(mon.base_stats.hp, mon.level) * (1 + 0.05 * hp_buff))
    computed += mon.flags.get("_runMaxHp", 0)
    new_max_hp = _wg_max_hp(mon.species_id, gen4_mode, computed)
    delta = new_max_hp - (mon.max_hp or new_max_hp)
    mon.max_hp = new_max_hp
    mon.current_hp = min(new_max_hp, (mon.current_hp or 0) + max(0, delta))


def _apply_use_item(state: RunState, action: UseItem) -> None:
    if not (0 <= action.item_index < len(state.items)):
        raise ValueError(f"no such bag item index: {action.item_index}")
    if not (0 <= action.target_index < len(state.team)):
        raise ValueError(f"no such team index: {action.target_index}")
    item_id = state.items[action.item_index]
    mon = state.team[action.target_index]
    if not _usable_item_can_target(item_id, mon):
        raise ValueError(f"{item_id} cannot target {mon.name}")
    state.items.pop(action.item_index)

    if item_id == "sacred_ash":
        revived = mon.current_hp <= 0
        mon.current_hp = mon.max_hp
        _log(state, "use_item", item_id=item_id, team_index=action.target_index, revived=revived)
        state.phase = Phase.ON_MAP
    elif item_id == "rare_candy":
        # `applyUsableItemTo`'s `rare_candy` branch calls the FULL
        # `checkAndEvolveTeam()` afterward (bundle.deobfuscated.js:79619-
        # 79624), not a target-only check -- CODEX.md issue 19. Reuses the
        # same resumable `_todo`-queue "evolve" step the post-battle path
        # uses (source="todo", so branching-choice resumption correctly
        # continues scanning the REST of the team, not just this one mon),
        # with a trailing `finish_on_map` entry since there's no
        # node-advance step to fall through to afterward.
        _apply_rare_candy(mon, gen4_mode=state.gen4_mode)
        _log(state, "use_item", item_id=item_id, team_index=action.target_index, new_level=mon.level)
        state._todo = [{"kind": "evolve", "idx": 0}, {"kind": "finish_on_map"}]
        _run_todo(state)
    elif item_id == "moon_stone":
        _log(state, "use_item", item_id=item_id, team_index=action.target_index)
        if not _maybe_evolve_one(state, action.target_index, source="item", force=True):
            state.phase = Phase.ON_MAP
    elif item_id == "tm_normal":
        mon.move_tier = min(2, mon.move_tier + 1)  # nullish-safe: move_tier is never None, CODEX.md issue 11
        state.used_tm = True
        _log(state, "use_item", item_id=item_id, team_index=action.target_index, move_tier=mon.move_tier)
        state.phase = Phase.ON_MAP
    else:
        state.items.insert(action.item_index, item_id)
        raise ValueError(f"unknown usable item: {item_id}")


def _apply_equip_item(state: RunState, action: EquipItem) -> None:
    """Port of `equipItemFromBag` (bundle.deobfuscated.js:79652-79671), gated
    by the same item-type distinction the source's team-bar click handler
    applies before ever calling it (bundle.deobfuscated.js:64943-64950, see
    `EquipItem`'s docstring) -- CODEX.md P0.5. Validation happens before any
    mutation, so a rejected attempt leaves bag order and the target's
    current held item unchanged.
    """
    if not (0 <= action.bag_index < len(state.items)):
        raise ValueError(f"no such bag item index: {action.bag_index}")
    if not (0 <= action.team_index < len(state.team)):
        raise ValueError(f"no such team index: {action.team_index}")
    item_id = state.items[action.bag_index]
    if item_id in _usable_item_ids():
        raise ValueError(
            f"{item_id} is a usable item -- it is never routed to equipItemFromBag "
            f"in the source (bundle.deobfuscated.js:64943-64950); use UseItem instead"
        )
    if item_id not in _passive_item_ids():
        raise ValueError(f"unrecognized item id, cannot equip: {item_id}")
    state.items.pop(action.bag_index)
    mon = state.team[action.team_index]
    old_item = mon.held_item
    if old_item is not None:
        state.items.append(old_item.id)
    mon.held_item = HeldItem(id=item_id)
    _log(state, "equip_item", team_index=action.team_index, item_id=item_id, replaced=old_item.id if old_item else None)
    state.phase = Phase.ON_MAP


def _apply_unequip_item(state: RunState, action: UnequipItem) -> None:
    """Port of the overlay's unequip exits (bundle.deobfuscated.js:79521-79531
    and 79549-79553) -- see `UnequipItem`. Validation before mutation, like
    `_apply_equip_item`, so a rejected attempt leaves bag order and the held
    item untouched.

    The item goes to the END of the bag: the source's own `state.items.push`.
    That matters because bag order is addressable -- `EquipItem` takes a
    `bag_index` and the source's item bar is index-addressed (R3's trace at
    64834) -- so appending versus inserting is a real behavioural difference,
    not a formatting one.
    """
    if not (0 <= action.team_index < len(state.team)):
        raise ValueError(f"no such team index: {action.team_index}")
    mon = state.team[action.team_index]
    if mon.held_item is None:
        raise ValueError(f"team member {action.team_index} is not holding an item")
    state.items.append(mon.held_item.id)
    _log(state, "unequip_item", team_index=action.team_index, item_id=mon.held_item.id)
    mon.held_item = None
    state.phase = Phase.ON_MAP


def _apply_hand_off_item(state: RunState, action: HandOffItem) -> None:
    """Port of the overlay's member-to-member hand-off
    (bundle.deobfuscated.js:79541-79545) -- see `HandOffItem`. A two-member
    held-item SWAP; the bag is not touched at all on this path, which is
    exactly what distinguishes it from unequip-then-equip.
    """
    if not (0 <= action.from_index < len(state.team)):
        raise ValueError(f"no such team index: {action.from_index}")
    if not (0 <= action.to_index < len(state.team)):
        raise ValueError(f"no such team index: {action.to_index}")
    if action.from_index == action.to_index:
        # The source renders this member's own row as "Holding" (79470) and
        # its click would set `heldItem` to itself twice; there is no reachable
        # hand-off to yourself.
        raise ValueError("cannot hand an item off to the same team member")
    source = state.team[action.from_index]
    if source.held_item is None:
        raise ValueError(f"team member {action.from_index} is not holding an item")
    target = state.team[action.to_index]
    source.held_item, target.held_item = target.held_item, source.held_item
    _log(
        state, "hand_off_item",
        from_index=action.from_index, to_index=action.to_index,
        item_id=target.held_item.id,
        received=source.held_item.id if source.held_item else None,
    )
    state.phase = Phase.ON_MAP


def _apply_reorder_team(state: RunState, action: ReorderTeam) -> None:
    """Port of the team bar's drag/click-to-swap reordering (CODEX.md
    issues 5/36) -- battle order is mechanically significant since
    `run_battle` always fights whichever LIVING member is first in list
    order."""
    if sorted(action.order) != list(range(len(state.team))):
        raise ValueError("order must be a permutation of the current team indices")
    state.team = [state.team[i] for i in action.order]
    state.phase = Phase.ON_MAP


def _run_todo(state: RunState) -> None:
    """Drains `state._todo`, a small resumable work queue. Exists because a
    won battle's aftermath (evolution check -> node-specific finish action)
    can be interrupted mid-way by a branching-evolution choice, which is
    itself a suspended-continuation point per the module docstring --
    `_todo` is how that interruption survives across `step()` calls without
    reifying a bespoke `Phase` for every possible "what to do when the
    evolution choice comes back" continuation.
    """
    while state._todo:
        step = state._todo[0]
        kind = step["kind"]
        if kind == "evolve":
            if _evolve_step(state, step):
                return
            state._todo.pop(0)
        elif kind == "advance":
            _advance(state, step["node_id"])
            state.phase = Phase.ON_MAP
            state._todo.pop(0)
        elif kind == "grant_badge":
            state.badges += 1
            _advance(state, step["node_id"])
            state.phase = Phase.NEXT_MAP_READY
            _log(state, "badge", badges=state.badges)
            state._todo.pop(0)
        elif kind == "heal_and_mark":
            for mon in state.team:
                mon.current_hp = mon.max_hp
            if step.get("silver"):
                state.silver_beaten += 1
            if step.get("admin"):
                state.fought_admin = True
            _advance(state, step["node_id"])
            state.phase = Phase.ON_MAP
            state._todo.pop(0)
        elif kind == "offer_swap":
            # `showSwapScreen` called unconditionally, whether or not the team
            # has room -- `doLegendaryNode`'s win callback (80454-80457).
            state._todo.pop(0)
            _offer_swap_screen(state, step["mon"], step["node_id"])
            return
        elif kind == "elite4_fight":
            _elite4_fight_step(state, step)
            return
        elif kind == "finish_on_map":
            state.phase = Phase.ON_MAP
            state._todo.pop(0)
        else:  # pragma: no cover -- exhaustive by construction
            state._todo.pop(0)


def _elite4_fight_step(state: RunState, step: dict) -> None:
    """Port of `doElite4`/`doGen2Elite4`'s sequential gauntlet loop
    (docs/logic-notes-nodes.md section 2). Since `battle_loop.run_battle`
    resolves instantly (no player choice mid-fight), the whole gauntlet can
    run within a single `step()` call unless a branching evolution
    interrupts it -- exactly the case `_run_todo`'s queue exists for.
    `state.elite_index` is updated per-fight (not just at the end), matching
    the source's own resume-checkpoint behavior."""
    roster = step["roster"]
    idx = step["idx"]
    node_id = step["node_id"]
    if idx >= len(roster):
        state.elite_index = 0
        _advance(state, node_id)
        state.phase = Phase.VICTORY
        state.won = True
        _log(state, "victory")
        state._todo.pop(0)
        return
    trainer = roster[idx]
    enemy = _build_fixed_team(trainer, state)
    result = _run_battle(state, enemy)
    state.elite_index = idx
    level_gain = 1 if state.nuzlocke_mode else 2
    won = _after_battle(state, result, level_gain=level_gain)
    if not won:
        return  # _after_battle already cleared state._todo and set GAME_OVER
    state._todo.pop(0)
    state._todo.insert(0, {"kind": "elite4_fight", "idx": idx + 1, "roster": roster, "node_id": node_id})
    state._todo.insert(0, {"kind": "evolve", "idx": 0})
    _run_todo(state)


# ---------------------------------------------------------------------------
# Question-node resolution -- docs/logic-notes-nodes.md section 0
# ---------------------------------------------------------------------------


def _resolve_question(state: RunState, node: MapNode) -> str:
    """Port of `resolveQuestionMark` (Story-mode branch only; Endless mode's
    different cutoff ladder is out of scope), plus `onNodeClick`'s own
    `savedQuestionResolve` guard around it (bundle.deobfuscated.js:
    77318-77332).

    The guard is a SINGLE `{key, resolvedType}` slot keyed
    `"m<currentMap>:<nodeId>"` (Endless mode's region-qualified key variant
    is not modeled). The source reuses the stored `resolvedType` only when
    the key matches exactly, and otherwise rolls fresh and OVERWRITES the
    slot -- so a second question node does not merely add an entry, it
    evicts the first. M4.2 models that single slot directly; it used to be a
    `question_cache` dict, which is observationally equivalent for every
    route this port can express (a question node that suspends on a pending
    choice is not revisitable until it advances) but is a different shape
    from the field `route-oracle`'s `resume_state` now compares.

    CODEX.md issue 9: the key used to be the bare node id, and node ids
    repeat every map, so a question at `n4_1` on map 0 could pin every LATER
    map's `n4_1` question to the same resolved type without consuming RNG --
    the map-qualified key makes that collision impossible.
    """
    cache_key = f"m{state.current_map}:{node.id}"
    saved = state.saved_question_resolve
    if saved is not None and saved.get("key") == cache_key:
        return saved["resolved_type"]
    roll = rng.rng()
    # bundle.deobfuscated.js:77399-77406: additive +0.07 per condition, not the
    # multiplicative doubling `roll_shiny`/`_shiny_chance` use -- `hasShinyCharm()`
    # (state.shiny_charm) and an enabled `shiny_rate` passive each contribute
    # independently, so both present at once is +0.14, not +0.07.
    shiny_bonus = (0.07 if state.shiny_charm else 0.0) + (
        0.07 if battle.has_passive(state.passives, _SHINY_RATE_TRAIT_ID) else 0.0
    )
    if roll < 0.22:
        resolved = map_gen.BATTLE
    elif roll < 0.42:
        resolved = map_gen.TRAINER
    elif roll < 0.52:
        resolved = map_gen.BATTLE if state.nuzlocke_mode else map_gen.CATCH
    elif roll < 0.65:
        resolved = map_gen.ITEM
    elif roll < 0.72 + shiny_bonus:
        resolved = "shiny"
    else:
        resolved = "mega"
    state.saved_question_resolve = {"key": cache_key, "resolved_type": resolved}
    return resolved


# ---------------------------------------------------------------------------
# Node dispatch
# ---------------------------------------------------------------------------


def _visit_node(state: RunState, node_id: str) -> None:
    assert state.map is not None
    node = state.map.nodes.get(node_id)
    if node is None:
        raise ValueError(f"no such node: {node_id}")
    if not node.accessible:
        raise ValueError(f"node {node_id} is not accessible")
    # `onNodeClick` (bundle.deobfuscated.js:77305-77396) in exact statement
    # order: set `state.currentNode` (77311), then lock the already-accessible
    # same-layer siblings (77312-77316), THEN resolve a QUESTION node's real
    # type (77318-77333), THEN dispatch on it (the switch at 77334). The lock
    # is eager -- it has already happened while a catch/item/swap screen is up
    # and it survives a battle that ends the run, neither of which ever reaches
    # `_advance`. `_advance`'s own sibling-lock loop is idempotent with this
    # one, so an ordinary node that resolves immediately observes no
    # difference; a node that SUSPENDS or LOSES does.
    state.current_node_id = node_id
    _lock_same_layer_siblings(state.map, node_id)
    node_type = node.type
    if node_type == map_gen.QUESTION:
        node_type = _resolve_question(state, node)
    # onNodeClick's own `default: doBattleNode` fallback (bundle.deobfuscated.js:77389-77390).
    handler = _NODE_HANDLERS.get(node_type, _visit_battle)
    handler(state, node)


def _visit_battle(state: RunState, node: MapNode) -> None:
    """Port of `doBattleNode` (docs/logic-notes-nodes.md section 1) --
    species/level selection is already `map_gen.pick_wild_encounter`, so
    this only builds the combatant and resolves the fight."""
    species_id, level = map_gen.pick_wild_encounter(
        node.layer,
        state.current_map,
        player_team_types=[tuple(m.types) for m in state.team],
        gen2_mode=state.gen2_mode,
        gen3_mode=state.gen3_mode,
        gen4_mode=state.gen4_mode,
    )
    move_tier = map_gen.get_move_tier_for_map(state.current_map)
    enemy = [_make_wild_combatant(species_id, level, move_tier=move_tier, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)]
    result = _run_battle(state, enemy)
    # CODEX.md P0.6: `doBattleNode`'s own `runBattleScreen` call passes
    # isBoss=false (bundle.deobfuscated.js:77724-77732) -- Escape Rope
    # recovery is eligible on a loss here.
    if not _after_battle(
        state,
        result,
        level_gain=1,
        rope_eligible=True,
        rope_continuation=[{"kind": "advance", "node_id": node.id}],
    ):
        return
    state._todo = [{"kind": "evolve", "idx": 0}, {"kind": "advance", "node_id": node.id}]
    _run_todo(state)


def _trainer_fight_level(state: RunState, node: MapNode) -> int:
    """Port of `trainerFightLevel` (bundle.deobfuscated.js:80190-80212) --
    `doTrainerNode`'s OWN level formula, distinct from the shared
    `getLevelForNode` wild/boss formula: subtracts a per-map-index offset
    (bigger for Johto/Hoenn-arc maps than Sinnoh, zero in Gen1/Story) so
    procedural trainer levels trail slightly behind the raw map range,
    floored at 1. The source's `challengeOnlyFight` 1.5x multiplier is
    Endless-only, not modeled (out of scope); both operands are already
    integers in every modeled case, so `Math.round` is a no-op here."""
    base = map_gen.get_level_for_node(node.layer, state.current_map, state.gen2_mode, state.gen3_mode, state.gen4_mode)
    cm = state.current_map
    if state.gen2_mode or state.gen3_mode:
        offset = 3 if cm >= 4 else 2 if cm >= 2 else 1 if cm >= 1 else 0
    elif state.gen4_mode:
        offset = 2 if cm >= 5 else 1 if cm >= 1 else 0
    else:
        offset = 0
    return max(1, base - offset)


def _trainer_pool_for_generation(archetype: data.TrainerArchetype, state: RunState) -> Optional[tuple[int, ...]]:
    """Port of `doTrainerNode`'s per-generation pool selection plus the
    Gen4-only starter-line exclusion filter (bundle.deobfuscated.js:
    80234-80252). Returns `None`/empty when the archetype has no pool for
    the CURRENT generation -- a real source behavior (e.g. "aceTrainer"/
    "oldGuy" have an explicit `pool: null` in Gen1/Story mode despite having
    later-gen pools), not a data gap; `_visit_trainer` falls back to the
    ordinary wild catch-choices pool in that case, matching the source's own
    `else` branch."""
    if state.gen4_mode:
        pool = archetype.gen4_pool
    elif state.gen3_mode:
        pool = archetype.gen3_pool
    elif state.gen2_mode and archetype.gen2_pool:
        pool = archetype.gen2_pool
    else:
        pool = archetype.pool
    if state.gen4_mode and pool:
        starter_ids = set(data.get_starter_ids(4))
        filtered = tuple(sid for sid in pool if battle_abilities.get_evo_line_root(sid) not in starter_ids)
        if filtered:
            pool = filtered
    return pool


def _select_trainer_team_species(pool: Sequence[int], team_size: int, level: int) -> list[int]:
    """Port of `doTrainerNode`'s species-selection block when an archetype
    pool exists (bundle.deobfuscated.js:80260-80289): dedupe the raw pool,
    filter by min-level eligibility (falling back to the unfiltered set if
    that empties it), dedupe again by evolved-target-at-this-level (keep the
    first candidate per target), Fisher-Yates shuffle with `rng()`, then
    cycle `shuffled[i % len(shuffled)]` -- each independently re-resolved
    through `resolveEvoForLevel` -- to fill exactly `team_size` slots (a
    small pool can repeat species). `challengeBabyEnemies` is Endless-only,
    not modeled."""
    if not pool:
        return []
    unique = list(dict.fromkeys(pool))
    level_filtered = [sid for sid in unique if map_gen.min_level_for_species(sid) <= level]
    usable = level_filtered if level_filtered else unique

    seen_targets: set = set()
    deduped: list[int] = []
    for sid in usable:
        resolved = map_gen.resolve_evo_for_level(sid, level)
        if resolved in seen_targets:
            continue
        seen_targets.add(resolved)
        deduped.append(sid)

    shuffled = list(deduped)
    for i in range(len(shuffled) - 1, 0, -1):
        j = int(rng.rng() * (i + 1))
        shuffled[i], shuffled[j] = shuffled[j], shuffled[i]

    return [map_gen.resolve_evo_for_level(shuffled[i % len(shuffled)], level) for i in range(team_size)]


def _visit_trainer(state: RunState, node: MapNode) -> None:
    """Port of `doTrainerNode` (docs/logic-notes-nodes.md section 3,
    bundle.deobfuscated.js:80213-80345) -- the procedural mid-map trainer
    roster, keyed by the node's `trainerSprite` archetype
    (`map_gen.generate_map`'s own deterministic hash assignment, same
    non-RNG-stream design family as `_assign_legendary_species_id`) into
    `TRAINER_BATTLE_CONFIG`. Falls back to the `"aceTrainer"` archetype if
    the node somehow has no `trainerSprite` (defensive, mirrors the source's
    own `|| "aceTrainer"`), and falls back further to the ordinary wild
    catch-choices pool if that archetype has no species pool at all for the
    CURRENT generation (see `_trainer_pool_for_generation`'s docstring --
    also a real source behavior, not a gap)."""
    sprite = node.extra.get("trainerSprite") or "aceTrainer"
    config = data.get_trainer_battle_config()
    archetype = config.get(sprite) or config["aceTrainer"]

    team_size = 1 if state.current_map == 0 else 2 if state.current_map <= 2 else 3
    level = _trainer_fight_level(state, node)
    move_tier = map_gen.get_move_tier_for_map(state.current_map)
    pool = _trainer_pool_for_generation(archetype, state)

    if pool:
        species_ids = _select_trainer_team_species(pool, team_size, level)
    else:
        # bundle.deobfuscated.js:80289-80309: no archetype pool for this
        # generation -- fall back to the ordinary wild catch-choices pool,
        # requesting exactly 3 (the source's own hardcoded count) and
        # SLICING (not cycling) to team_size, so the resulting enemy team
        # can legitimately be shorter than team_size if the catch pool
        # itself came up short.
        min_dex, max_dex = map_gen.get_catch_gen_range(state.gen2_mode, state.gen3_mode, state.gen4_mode)
        candidates = map_gen.get_catch_choices(
            state.current_map,
            3,
            max_dex,
            min_dex,
            exclude_starters=True,
            gen2_mode=state.gen2_mode,
            gen3_mode=state.gen3_mode,
            gen4_mode=state.gen4_mode,
        )
        species_ids = [map_gen.resolve_evo_for_level(sid, level) for sid in candidates[:team_size]]

    if not species_ids:
        # bundle.deobfuscated.js:80312-80315: empty candidate pool -> the
        # node is silently skipped, no battle at all.
        _advance(state, node.id)
        state.phase = Phase.ON_MAP
        return

    enemy = [
        _make_wild_combatant(sid, level, move_tier=move_tier, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)
        for sid in species_ids
    ]
    result = _run_battle(state, enemy)
    # CODEX.md P0.6: `doTrainerNode`'s own `runBattleScreen` call passes
    # isBoss=false (bundle.deobfuscated.js:80327-80336) -- distinct from
    # the gym-leader/boss node's own call, which passes isBoss=true.
    if not _after_battle(
        state,
        result,
        level_gain=2,
        rope_eligible=True,
        rope_continuation=[{"kind": "advance", "node_id": node.id}],
    ):
        return
    state._todo = [{"kind": "evolve", "idx": 0}, {"kind": "advance", "node_id": node.id}]
    _run_todo(state)


_SILVER_STAGE_BY_MAP = {1: 0, 3: 1, 5: 2, 7: 3}


def _visit_silver(state: RunState, node: MapNode) -> None:
    """Port of `doSilverNode` (docs/logic-notes-nodes.md section 11,
    bundle.deobfuscated.js:77895-77957). The stage index is a fixed
    map-index lookup for maps 1/3/5/7 (bundle.deobfuscated.js:77899-77903);
    every OTHER map index falls back to `state.silver_beaten` (how many
    times Silver has already been beaten this run) -- both paths clamped to
    `SILVER_ENCOUNTERS`'s last valid index. The final team slot is then
    replaced by the player's starter's type-counter mirror line
    (`SILVER_STARTER_LINES`, mainline Gold/Silver/Crystal's rival mechanic),
    resolved to whatever evolution stage the ENCOUNTER's own (unmodified)
    final-slot level supports -- bundle.deobfuscated.js:77908-77926."""
    encounters = data.get_silver_encounters()
    stage_index = min(_SILVER_STAGE_BY_MAP.get(state.current_map, state.silver_beaten), len(encounters) - 1)
    encounter = encounters[stage_index]
    move_tier = map_gen.get_move_tier_for_map(state.current_map)
    team = _build_roster_team(encounter.team, move_tier, state.gen4_mode)

    starter_line = data.get_silver_starter_lines().get(state.starter_species_id)
    if starter_line and team:
        last_idx = len(team) - 1
        last_level = team[last_idx].level
        target_species = map_gen.resolve_evo_for_level(starter_line[0].species_id, last_level)
        idx = next((i for i, m in enumerate(starter_line) if m.species_id == target_species), 0)
        chosen = starter_line[idx]
        # Use the SILVER_STARTER_LINES record itself, not the general
        # Pokedex row. The source passes `B2a` directly to createInstance
        # (bundle.deobfuscated.js:77921-77924). These Gen2 rival records
        # intentionally omit `baseStats.spdef`, so battle stat resolution
        # falls back to `special`; substituting the Pokedex row here would
        # silently use its distinct explicit Sp. Def for most Johto
        # starters and change real battle damage despite matching species,
        # level, types, and name.
        ability = battle_abilities.get_gen3_ability(chosen.species_id, state.gen4_mode)
        max_hp = 1 if ability == "wonder_guard" else map_gen.calc_hp(chosen.base_stats.hp, last_level)
        team[last_idx] = Combatant(
            species_id=chosen.species_id,
            level=last_level,
            base_stats=chosen.base_stats,
            types=chosen.types,
            max_hp=max_hp,
            current_hp=max_hp,
            name=chosen.name,
            held_item=None,
            move_tier=move_tier,
        )

    result = _run_battle(state, team)
    # CODEX.md P0.6: doSilverNode's runBattleScreen call passes isBoss=true
    # (bundle.deobfuscated.js:77938) -- no Escape Rope recovery offer, same
    # as every other boss-tier fight. Silver's Nuzlocke-permadeath exemption
    # comes from a hardcoded `iS !== "silver"` sprite-name check inside
    # runBattleScreen's own win branch (bundle.deobfuscated.js:81360) --
    # a DIFFERENT mechanism from `doAdminNode`'s `state._noPermaDeath` flag
    # (see `_visit_admin`) with the same observable effect (always exempt),
    # modeled here with the same `no_permadeath=True` call-time flag.
    if not _after_battle(state, result, level_gain=4, all_team_xp=True, no_permadeath=True):
        return
    finish = {"kind": "heal_and_mark", "node_id": node.id, "silver": True, "admin": False}
    state._todo = [{"kind": "evolve", "idx": 0}, finish]
    _run_todo(state)


def _visit_admin(state: RunState, node: MapNode, kind: str) -> None:
    """Port of `doAdminNode` (docs/logic-notes-nodes.md section 11,
    bundle.deobfuscated.js:77958-78006). Roster keyed by map index (2/5/7);
    any OTHER map index falls back to the FIXED index 2
    (bundle.deobfuscated.js:77964, `iu[state.currentMap] || iu[0x2]`) --
    unlike Silver's `silver_beaten`-count fallback, Magma/Aqua's fallback is
    not battle-count-based. If even that fallback entry is missing
    (defensive; both tables always have index 2 in the extracted data), the
    node is a silent no-battle advance, matching the source's own
    `if (!iS) { advance; return; }` guard."""
    # bundle.deobfuscated.js:77960: `state.foughtAdmin = true` is the very
    # FIRST thing doAdminNode does, unconditionally -- set even on the
    # empty-roster no-battle path, and regardless of win/loss below.
    state.fought_admin = True
    encounters = data.get_magma_encounters() if kind == "magma" else data.get_aqua_encounters()
    encounter = encounters.get(state.current_map) or encounters.get(2)
    if encounter is None:
        _advance(state, node.id)
        state.phase = Phase.ON_MAP
        return
    move_tier = map_gen.get_move_tier_for_map(state.current_map)
    team = _build_roster_team(encounter.team, move_tier, state.gen4_mode)
    result = _run_battle(state, team)
    # CODEX.md P0.6: doAdminNode's runBattleScreen call passes isBoss=true
    # (bundle.deobfuscated.js:77985) -- no Escape Rope recovery offer.
    # Magma/Aqua's Nuzlocke-permadeath exemption is the source's own
    # `state._noPermaDeath = true` flag, held for the exact duration of the
    # fight (set bundle.deobfuscated.js:77980, reset to false right after
    # runBattleScreen resolves, 77996) -- modeled with the same
    # `no_permadeath=True` call-time flag `_visit_silver` uses for its own,
    # differently-mechanized exemption.
    if not _after_battle(state, result, level_gain=4, all_team_xp=True, no_permadeath=True):
        return
    finish = {"kind": "heal_and_mark", "node_id": node.id, "silver": False, "admin": True}
    state._todo = [{"kind": "evolve", "idx": 0}, finish]
    _run_todo(state)


def _visit_boss(state: RunState, node: MapNode) -> None:
    """Port of `doBossNode` (docs/logic-notes-nodes.md section 2). Map
    index 8 IS the Elite Four gauntlet (docs/logic-notes-runlifecycle.md
    section 3) -- dispatched to the resumable `elite4_fight` step since a
    branching evolution could interrupt the gauntlet mid-way.

    `node.extra["subBoss"]` (set by `map_gen.generate_sub_map`) means this
    BOSS-type node lives INSIDE a submap, not the parent map's own final
    boss layer -- `doBossNode`'s own very first check
    (bundle.deobfuscated.js:77744-77747: `if (iu.subBoss) { doSubMapBoss(iu);
    return; }`), ported here since `NODE_TYPES.BOSS` is shared between both
    node kinds."""
    if node.extra.get("subBoss"):
        _visit_sub_map_boss(state, node)
        return
    if state.current_map == 8:
        roster = _elite_four_roster(state)
        state._todo = [{"kind": "elite4_fight", "idx": state.elite_index, "roster": roster, "node_id": node.id}]
        _run_todo(state)
        return
    trainer = _gym_leader(state)
    enemy = _build_fixed_team(trainer, state)
    result = _run_battle(state, enemy)
    level_gain = 1 if state.nuzlocke_mode else 2  # docs/logic-notes-runlifecycle.md section 5
    if not _after_battle(state, result, level_gain=level_gain):
        return
    state._todo = [{"kind": "evolve", "idx": 0}, {"kind": "grant_badge", "node_id": node.id}]
    _run_todo(state)


def _visit_pokecenter(state: RunState, node: MapNode) -> None:
    """Port of `doPokeCenterNode` (docs/logic-notes-nodes.md section 6) --
    unconditional full-team heal, `challengeNoHeal` gate not modeled
    (challenge-mode only)."""
    for mon in state.team:
        mon.current_hp = mon.max_hp
    state.used_pokecenter = True
    _advance(state, node.id)
    state.phase = Phase.ON_MAP


def _dedup_by_evo_line(species_ids: Sequence[int]) -> list[int]:
    """Port of `doCatchNode`'s final generic dedup block (bundle.deobfuscated.js:
    78722-78729): keep only the FIRST candidate for each evolution-line
    root, dropping later candidates from the same line."""
    seen: set = set()
    result = []
    for sid in species_ids:
        root = battle_abilities.get_evo_line_root(sid)
        if root in seen:
            continue
        seen.add(root)
        result.append(sid)
    return result


def _visit_catch(state: RunState, node: MapNode) -> None:
    """Port of `doCatchNode`'s Story/Nuzlocke-relevant path
    (docs/logic-notes-nodes.md section 4; bundle.deobfuscated.js:78426-78760).
    Endless/Challenge-only branches (`challengeRandomizer`/submap-typed
    pools/`activeEncounterType`/`challengeNoEvo`/`challengeBabyOnly`/the
    Endless "reroll pool" bookkeeping and its own exclude-team-species
    branch) are out of scope, consistent with the rest of this module.

    Fixes applied here (CODEX.md issue 10 -- this function used to build
    its 1-or-3-candidate pool directly instead of rolling a larger pool and
    narrowing it the way the source does):
    - candidates are rolled non-shiny placeholder-chance and at
      `map_gen.get_move_tier_for_map`'s tier, not always non-shiny/tier 1;
    - Nuzlocke excludes species whose evolution line is already on the
      team, falling back to the unfiltered pool only if that would leave
      nothing (matching the source's `slice(0,1)` -- Nuzlocke still only
      ever offers ONE candidate);
    - the Gen1-Nuzlocke, map-0-only restricted starter-adjacent species set
      is applied;
    - the map-0/layer-1 "guarantee a Grass and a Water option" safety net
      applies outside Nuzlocke/Gen4;
    - the Gen3, non-Nuzlocke "prefer species not already on the team, but
      don't require it" preference applies;
    - candidates are deduplicated by evolution line before the final
      top-3 slice, matching the source's own dedup step.

    M4.2: `state.savedCatch` (78765) is now modelled. The source pins the
    rolled instances on it -- keyed by the BARE node id, not the
    map-qualified key `savedQuestionResolve`/`savedShinyNode` use (78441:
    `savedCatch?.nodeId === B.id`) -- and skips the whole pool computation
    when the key matches, so a save resumed on the catch screen re-offers
    the same three Pokemon instead of re-rolling. `rerollPool`/`rerolled`
    are Endless/reroll bookkeeping this port does not model and are not part
    of the record here.
    """
    saved = state.saved_catch
    if saved is not None and saved.get("key") == node.id and isinstance(saved.get("instances"), list):
        # bundle.deobfuscated.js:78440-78445 -- the saved offer is re-shown
        # verbatim, consuming no RNG. Unreachable in a single session (the
        # node cannot be re-clicked until it advances, and advancing clears
        # or strands the record), so this is exercised by focused tests
        # rather than by a route.
        _offer_catch_choice(state, node, list(saved["instances"]))
        return
    min_dex, max_dex = map_gen.get_catch_gen_range(state.gen2_mode, state.gen3_mode, state.gen4_mode)
    pool = list(
        map_gen.get_catch_choices(
            state.current_map,
            18,
            max_dex,
            min_dex,
            exclude_starters=True,
            gen2_mode=state.gen2_mode,
            gen3_mode=state.gen3_mode,
            gen4_mode=state.gen4_mode,
        )
    )
    if not pool:
        _advance(state, node.id)
        state.phase = Phase.ON_MAP
        return

    node_level = map_gen.get_level_for_node(node.layer, state.current_map, state.gen2_mode, state.gen3_mode, state.gen4_mode)
    level = max(4, node_level) if state.current_map == 0 else node_level

    matched = [sid for sid in pool if map_gen.min_level_for_species(sid) <= level]
    if matched:
        pool = matched if len(matched) >= 3 else (matched + [sid for sid in pool if sid not in matched])[:3]

    if (
        state.nuzlocke_mode
        and state.current_map == 0
        and not state.gen2_mode
        and not state.gen3_mode
        and not state.gen4_mode
    ):
        restricted = _GEN1_NUZLOCKE_MAP0_RESTRICTED
        narrowed = [sid for sid in pool if sid in restricted]
        if narrowed:
            pool = narrowed

    if not state.nuzlocke_mode and not state.gen4_mode and state.current_map == 0 and node.layer == 1:
        grass_pool, water_pool = _MAP0_SAFETY_NET[2 if state.gen3_mode else 1 if state.gen2_mode else 0]

        def has_type(species_id: int, type_name: str) -> bool:
            return type_name in {t.capitalize() for t in data.get_pokedex()[species_id].types}

        # The source REPLACES pool[0] (Grass) and the first non-Grass slot
        # (Water, falling back to index 2) in place -- not an insert -- see
        # bundle.deobfuscated.js:78567-78578.
        if not any(has_type(sid, "Grass") for sid in pool):
            grass_pick = grass_pool[int(rng.rng() * len(grass_pool))]
            if pool:
                pool[0] = grass_pick
            else:
                pool.append(grass_pick)
        if not any(has_type(sid, "Water") for sid in pool):
            water_pick = water_pool[int(rng.rng() * len(water_pool))]
            idx = next((i for i, sid in enumerate(pool) if not has_type(sid, "Grass")), None)
            if idx is None:
                idx = min(2, max(0, len(pool) - 1))
            if pool:
                pool[idx] = water_pick
            else:
                pool.append(water_pick)

    own_evo_roots = {battle_abilities.get_evo_line_root(m.species_id) for m in state.team}

    if state.nuzlocke_mode:
        excluded = [sid for sid in pool if battle_abilities.get_evo_line_root(sid) not in own_evo_roots]
        pool = (excluded if excluded else pool)[:1]
    elif state.gen3_mode:
        preferred = [sid for sid in pool if battle_abilities.get_evo_line_root(sid) not in own_evo_roots]
        if len(preferred) >= 3:
            pool = preferred
        elif preferred:
            preferred_set = set(preferred)
            pool = preferred + [sid for sid in pool if sid not in preferred_set]

    pool = _dedup_by_evo_line(pool)[:3]
    if not pool:
        _advance(state, node.id)
        state.phase = Phase.ON_MAP
        return

    # `createInstance` is called DIRECTLY on the pool candidates in the
    # source's `doCatchNode` -- unlike `doBattleNode`/`doTrainerNode`, it
    # never calls `resolveEvoForLevel` for a catch offer (confirmed by
    # grepping every `resolveEvoForLevel` call site in the bundle: none are
    # inside `doCatchNode`). The `_legendary`-species "+5 levels" bonus
    # `createInstance`'s caller applies there is also unreachable for this
    # port's in-scope pool: `map_gen.get_catch_choices` unconditionally
    # excludes `LEGENDARY_IDS` from its candidates (matching the source's
    # own `base_eligible`/`B6o` filter), so no catch candidate here is ever
    # legendary -- that bonus only matters for the Endless/Challenge
    # randomizer pools this module doesn't model.
    move_tier = map_gen.get_move_tier_for_map(state.current_map)
    mons = []
    for sid in pool:
        is_shiny = roll_shiny(state)
        mons.append(_make_wild_combatant(sid, level, is_shiny=is_shiny, move_tier=move_tier, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode))

    # `state.savedCatch = {nodeId, instances, rerollPool, level}` + `saveRun()`
    # (bundle.deobfuscated.js:78765-78771), written from the just-rolled
    # instances, immediately before the cards are built.
    state.saved_catch = {"key": node.id, "instances": list(mons)}
    _offer_catch_choice(state, node, mons)


def _offer_catch_choice(state: RunState, node: MapNode, mons: list) -> None:
    """Raise `doCatchNode`'s card screen over an already-decided offer.

    Split out of `_visit_catch` (M4.2) because the source reaches this point
    two ways -- from a freshly rolled pool, or from `state.savedCatch`'s
    restored `instances` (78440-78445) -- and both must produce the identical
    pending, since the source builds the same `#catch-choices` cards from
    `B2n` either way."""
    origin = "catch" if node.type == map_gen.CATCH else "question"
    state.pending = PendingChoice(
        phase=Phase.CATCH_CHOICE,
        options=[_mon_summary(m) for m in mons],
        optional=True,
        extra={"candidates": mons, "node_id": node.id, "origin": origin},
    )
    state.phase = Phase.CATCH_CHOICE


def _visit_shiny(state: RunState, node: MapNode) -> None:
    """Port of `doShinyNode` (bundle.deobfuscated.js:80872-80934,
    docs/logic-notes-nodes.md section 10) -- always-shiny, first (not
    random) candidate, no battle at all. Two source details this used to
    get wrong (CODEX.md issues 9-10): the candidate pool is 3 candidates
    (`getCatchChoices(..., 0x3, ...)`, exact RNG-consumption match), not 1;
    and the first candidate is passed DIRECTLY to `createInstance` -- the
    source never calls `resolveEvoForLevel` in this path (grepped every
    call site: none are inside `doShinyNode`), so an evolution-eligible
    candidate is NOT auto-evolved here the way catch/battle candidates
    elsewhere are.

    `extra["origin"]` is `"shiny_node"` rather than `"question"` so that
    `run_scenario.py`'s `_is_shiny_origin` can tell a shiny offer apart
    from an ordinary question-resolved catch -- both use
    `Phase.CATCH_CHOICE`, and only the shiny one must project as a
    `shiny-screen`. It is a PROJECTION DISCRIMINATOR ONLY.

    M4.1 correction. This docstring previously asserted that
    `doShinyNode`'s accept handler "never calls
    `catchPokemon`/`recordMonOrigin` at all", and `_try_add_to_team` set no
    origin flag for `"shiny_node"` on that basis. The assertion is FALSE
    against the current source. `doShinyNode`'s room branch
    (80961-80970) is a comma sequence that reads, in order:
    `savedShinyNode = null`, `loadBuffsIntoPokemon`, `team.push`,
    `maxTeamSize` update, **`recordMonOrigin(B)`** (80967),
    `advanceFromNode`, `showMapScreen`. It is true that it does not call
    `catchPokemon` -- it inlines an equivalent body -- but the inlined body
    includes `recordMonOrigin`, and the half-true first clause was allowed
    to carry the false second one.

    `B` here is the node `onNodeClick` passed in, whose `type` is still
    `NODE_TYPES.QUESTION`: the `case "shiny"` dispatch (77384) switches on
    the RESOLVED type `iu`, not on `B.type`, and never rebinds `B`. So
    `recordMonOrigin` takes its QUESTION branch and sets
    `state.gotViaQuestion = true`. See `_try_add_to_team` for the full
    trace and for why the 24-scenario matrix could not see this (every
    shiny resolution in it was a decline, and the decline handler really
    does skip `recordMonOrigin`).
    """
    level = map_gen.get_level_for_node(node.layer, state.current_map, state.gen2_mode, state.gen3_mode, state.gen4_mode)
    # `state.savedShinyNode` (M4.2), keyed exactly like `savedQuestionResolve`
    # -- `"m" + currentMap + ":" + node.id` (80879-80884). A matching record
    # short-circuits the whole candidate roll and re-fetches the pinned
    # species (80886-80890); otherwise the first candidate is rolled and
    # pinned with a `saveRun()` (80917-80923). Only the SPECIES is saved, not
    # the instance: the source rebuilds it with `createInstance` either way.
    # As with `saved_catch`, the reuse arm is unreachable inside one session
    # and is covered by focused tests rather than by a route.
    key = f"m{state.current_map}:{node.id}"
    saved = state.saved_shiny_node
    species_id = saved.get("species_id") if saved is not None and saved.get("key") == key else None
    if species_id is None:
        min_dex, max_dex = map_gen.get_catch_gen_range(state.gen2_mode, state.gen3_mode, state.gen4_mode)
        candidates = map_gen.get_catch_choices(
            state.current_map,
            3,
            max_dex,
            min_dex,
            exclude_starters=True,
            gen2_mode=state.gen2_mode,
            gen3_mode=state.gen3_mode,
            gen4_mode=state.gen4_mode,
        )
        if not candidates:
            # 80925-80928: no candidate at all just advances the node. The
            # source writes no record on this path, so none is written here.
            _advance(state, node.id)
            state.phase = Phase.ON_MAP
            return
        species_id = candidates[0]
        state.saved_shiny_node = {"key": key, "species_id": species_id}
    move_tier = map_gen.get_move_tier_for_map(state.current_map)
    mon = _make_wild_combatant(species_id, level, is_shiny=True, move_tier=move_tier, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)
    state.pending = PendingChoice(
        phase=Phase.CATCH_CHOICE,
        options=[_mon_summary(mon)],
        optional=True,
        extra={"candidates": [mon], "node_id": node.id, "origin": "shiny_node"},
    )
    state.phase = Phase.CATCH_CHOICE


def _visit_legendary(state: RunState, node: MapNode) -> None:
    """Port of `doLegendaryNode` (docs/logic-notes-nodes.md section 9).
    `node.extra["legendarySpeciesId"]` is already populated at map-
    generation time by `map_gen.generate_map`. A single-mon battle; there is
    no separate catch-rate roll. Shiny chance uses `roll_shiny`
    (`legendaryShinyChanceFlat` plus its own inline roll at the
    `doLegendaryNode` call site are the same formula/RNG draw as `rollShiny`,
    CODEX.md issue 5).

    **Winning ALWAYS presents the swap screen.** The win callback
    (bundle.deobfuscated.js:80446-80458) ends in a bare
    `showSwapScreen(B2P, B)` at 80457 -- there is no `state.team.length < 6`
    test anywhere in it. So with room the incoming legendary stays PENDING
    until the player explicitly clicks its card (accept, 79171-79201) or
    cancels (decline, 79249-79258); with a full team the ordered per-member
    release cards are presented. This is `offer_swap`, and it is a different
    lifecycle from `offer_catch`.

    That distinction is real source behavior, not a simplification: the
    ordinary CATCH path (`catchPokemon`, 79026-79046) and the SHINY path
    (`doShinyNode`'s inlined accept handler, 80962-80970) BOTH test
    `state.team.length < 6` and auto-add with room, reaching `showSwapScreen`
    only when full. Legendary is the one ordinary-map node that does not.
    Before M4 this path used `offer_catch` and auto-added with room, which
    was the M2.4 audit's carried finding.

    CODEX.md P0.6: `doLegendaryNode`'s own `runBattleScreen` call passes
    isBoss=false (bundle.deobfuscated.js:80439-80446) -- confirmed by direct
    read despite reading as an unusual case for a boss-tier fight -- so an
    eligible loss here offers Escape Rope recovery same as a wild/trainer
    battle. Accepting re-enters the same success callback a win would have,
    which is the source's actual behavior: the win callback here is "mark
    caught, show the swap screen", called regardless of which path (win or
    accepted rope) reached it -- so the rope continuation is `offer_swap`
    too.
    """
    species_id = node.extra.get("legendarySpeciesId")
    if species_id is None:
        _advance(state, node.id)
        state.phase = Phase.ON_MAP
        return
    level = data.get_map_level_ranges(_generation(state))[state.current_map].max
    is_shiny = roll_shiny(state)
    caught = _make_wild_combatant(species_id, level, is_shiny=is_shiny, move_tier=2, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)
    enemy = [_make_wild_combatant(species_id, level, is_shiny=is_shiny, move_tier=2, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)]
    result = _run_battle(state, enemy)
    if not _after_battle(
        state,
        result,
        # `doLegendaryNode`'s own `runBattleScreen` call passes its 7th
        # (level-gain) argument as the literal `0x0` (bundle.deobfuscated.js:
        # 80439-80462), not `null`/omitted -- winning a legendary battle
        # grants NO levels at all, unlike every other `_visit_*` battle site.
        # Found and repaired during the M4 route-oracle work tracing a real
        # cross-runtime level divergence: a prior version passed
        # `level_gain=1` here, which was invisible until a route actually
        # WON a legendary encounter cross-runtime for the first time.
        level_gain=0,
        rope_eligible=True,
        rope_continuation=[{"kind": "offer_swap", "mon": caught, "node_id": node.id}],
    ):
        return
    state._todo = [{"kind": "evolve", "idx": 0}, {"kind": "offer_swap", "mon": caught, "node_id": node.id}]
    _run_todo(state)


def _visit_move_tutor(state: RunState, node: MapNode) -> None:
    """Port of `doMoveTutorNode` (bundle.deobfuscated.js:80464-80563) --
    bumps one chosen team member's `move_tier` by 1 (cap 2); mons already at
    tier 2 aren't offered a button (80474-80492, 80507-80515 -- a mastered
    row renders a plain "Already mastered!" span instead).

    The source has NO early-bail: unlike `doShinyNode`/`doLegendaryNode`
    (which really do skip the node when nothing can be offered), the
    move-tutor modal always opens, even with the whole team at tier 2 --
    that case just means zero `[data-tutor]` buttons and only
    `#btn-skip-tutor` (80531-80534). A prior version of this function bailed
    straight to `Phase.ON_MAP` whenever `eligible` was empty, which was a
    real divergence from the source, not a simplification -- fixed here so a
    fully-mastered team still raises `MOVE_TUTOR_CHOICE` with an empty
    (but real, decline-only) options list."""
    # `move_tier` is a plain `int` field (never `None`, default 1) -- `or 1`
    # would wrongly treat a valid tier-0 mon (maps 0-2) as tier 1 (CODEX.md
    # issue 11: tier 0 is a real, nullish-vs-falsy-sensitive value, not an
    # "unset" sentinel).
    eligible = [(i, m) for i, m in enumerate(state.team) if m.move_tier < 2]
    options = [
        {"team_index": i, "species_id": m.species_id, "name": m.name, "move_tier": m.move_tier} for i, m in eligible
    ]
    state.pending = PendingChoice(phase=Phase.MOVE_TUTOR_CHOICE, options=options, optional=True, extra={"node_id": node.id})
    state.phase = Phase.MOVE_TUTOR_CHOICE


def _visit_item(state: RunState, node: MapNode) -> None:
    """Port of `doItemNode` (docs/logic-notes-nodes.md section 5). Also the
    handler for a `"mega"`-resolved question node, verbatim, matching the
    source's own no-branch-parameter dispatch -- Mega Stones themselves are
    Endless-mode-only and not modeled, so a `"mega"` visit behaves exactly
    like a plain item node here, same as in Story mode on the live site."""
    held_ids = {m.held_item.id for m in state.team if m.held_item is not None}
    owned_ids = held_ids | set(state.items)
    reverse_type_item = {item_id: type_name for type_name, item_id in data.get_type_item_map().items()}
    team_types = {t.capitalize() for m in state.team for t in m.types}

    def passive_eligible(item: data.Item) -> bool:
        if item.id in owned_ids:
            return False
        if item.min_map is not None and state.current_map < item.min_map:
            return False
        # CODEX.md issue 12: `"gen2Only": true` (Loaded Dice) was parsed out
        # of the JSON but dropped on the floor -- the item could be offered
        # outside Gen2 mode.
        if item.gen2_only and not state.gen2_mode:
            return False
        required_type = reverse_type_item.get(item.id)
        if required_type is not None and required_type.capitalize() not in team_types:
            return False
        return True

    def usable_eligible(item: data.Item) -> bool:
        if item.id == "sacred_ash":
            return any(m.current_hp < m.max_hp for m in state.team)
        if item.id == "moon_stone":
            has_pending_evo = any(
                m.species_id in data.get_evolutions() or m.species_id in data.get_branching_evolutions()
                for m in state.team
            )
            return has_pending_evo and state.current_map <= 2
        if item.id == "tm_normal":
            return any(m.move_tier < 2 for m in state.team)
        return True  # rare_candy: always eligible

    # `state.itemOffer` restore (bundle.deobfuscated.js:79360-79364), M5. A
    # pinned offer for THIS node id is rebuilt from its saved ids and short-
    # circuits the roll entirely -- so no `rng()` is drawn. Two source
    # details are mirrored exactly rather than tidied:
    #   * the ids are resolved through the COMBINED pools (`B2P`, 79347-79358)
    #     with NO eligibility re-test, so a restored offer may contain an item
    #     that would no longer be offered on a fresh roll;
    #   * unresolvable ids are dropped (`.filter(Boolean)`), and only if
    #     NOTHING survives does it fall through to a fresh roll (`!B2a ||
    #     !B2a.length`).
    restored: Optional[list] = None
    offer = state.item_offer
    if isinstance(offer, dict) and offer.get("node_id") == node.id:
        ids = offer.get("item_ids")
        if isinstance(ids, list):
            by_id = {it.id: it for it in data.get_passive_items()}
            by_id.update({it.id: it for it in data.get_usable_items()})
            resolved = [by_id[i] for i in ids if i in by_id]
            if resolved:
                restored = resolved

    if restored is not None:
        offered = restored
    else:
        pool = [it for it in data.get_passive_items() if passive_eligible(it)]
        pool += [it for it in data.get_usable_items() if usable_eligible(it)]
        if not pool:
            _advance(state, node.id)
            state.phase = Phase.ON_MAP
            return
        _shuffle(pool)
        offered = pool[:3]
        # `state.itemOffer = {nodeId, ids}` (79371-79375), pinned at the roll.
        state.item_offer = {"node_id": node.id, "item_ids": [it.id for it in offered]}
    state.pending = PendingChoice(
        phase=Phase.ITEM_CHOICE,
        options=[{"id": it.id, "name": it.name, "usable": it.usable} for it in offered],
        optional=True,
        extra={"items": offered, "node_id": node.id},
    )
    state.phase = Phase.ITEM_CHOICE


def _visit_trade(state: RunState, node: MapNode) -> None:
    """Port of `doTradeNode`'s standard-mode flow (docs/logic-notes-nodes.md
    section 8) -- the Endless2-only `showTradeReleaseScreen` variant is out
    of scope. Pick a team member to trade away, get back a random species
    from the general catch pool (see `_resolve_trade_choice` for the exact
    `rollStoryTradeReplacement` port)."""
    state.pending = PendingChoice(
        phase=Phase.TRADE_CHOICE,
        options=[_mon_summary(m) for m in state.team],
        optional=True,
        extra={"node_id": node.id},
    )
    state.phase = Phase.TRADE_CHOICE


# ---------------------------------------------------------------------------
# Special submaps -- Underground/Distortion World (docs/logic-notes-submaps.md,
# bundle.deobfuscated.js:53508-53632, 76687-77107). Gen4/Sinnoh-only; SILVER/
# MAGMA/AQUA never reach any of this (see module docstring).
# ---------------------------------------------------------------------------


def _make_giratina_origin_combatant(level: int, *, gen4_mode: bool) -> Combatant:
    """Builds the wild Distortion-World Giratina-Origin encounter from
    `data.get_giratina_origin_form()` -- source-equivalent to
    `createInstance(await fetchPokemonById("giratina-origin"), level, false,
    2)` (bundle.deobfuscated.js:76767-76778), NOT `_make_wild_combatant`'s
    ordinary `data.get_pokedex()[species_id]` lookup, which would silently
    substitute base/Altered Giratina's stats (see that data function's own
    docstring for exactly why the two diverge despite sharing `species_id`
    487). Never shiny -- `doSubMapBoss` passes `isShiny=false` explicitly,
    same as every other submap boss."""
    mon_data = data.get_giratina_origin_form()
    base_stats = mon_data.base_stats
    ability = battle_abilities.get_gen3_ability(mon_data.species_id, gen4_mode)
    max_hp = 1 if ability == "wonder_guard" else map_gen.calc_hp(base_stats.hp, level)
    return Combatant(
        species_id=mon_data.species_id,
        level=level,
        base_stats=base_stats,
        types=mon_data.types,
        max_hp=max_hp,
        current_hp=max_hp,
        name=mon_data.name,
        move_tier=2,
        is_shiny=False,
    )


def _build_sub_map_boss_team(bag_team: Sequence[dict], gen2_mode: bool, gen4_mode: bool) -> list[Combatant]:
    """Port of `doSubMapBoss`'s `fetchPokemonById`+`createInstance` loop
    (bundle.deobfuscated.js:76767-76778) -- `moveTier` hardcoded to 2
    REGARDLESS of map index (unlike ordinary wild/trainer encounters, which
    use `map_gen.get_move_tier_for_map`), `heldItem` always `None`. The
    `"giratina-origin"` string id (`data.DistortionLegendaryEntry.boss_id`
    for the Giratina entry) is special-cased to
    `_make_giratina_origin_combatant` rather than resolved to a plain int
    and handed to the ordinary pokedex-lookup factory -- every other
    `bossId` in `data.get_submap_bosses()`/`get_distortion_legendary_pool()`
    is already a plain `int` and takes the ordinary path."""
    return [
        _make_giratina_origin_combatant(m["level"], gen4_mode=gen4_mode)
        if m["species_id"] == "giratina-origin"
        else _make_wild_combatant(m["species_id"], m["level"], move_tier=2, gen2_mode=gen2_mode, gen4_mode=gen4_mode)
        for m in bag_team
    ]


def _lock_same_layer_siblings(map_obj: map_gen.GeneratedMap, node_id: str) -> None:
    """Port of `onNodeClick`'s eager pre-dispatch sibling lock
    (bundle.deobfuscated.js:77312-77316) -- runs before EVERY node's own
    dispatch, locking already-accessible same-layer siblings (NOT the
    clicked node itself, and NOT `visited`/edge-reveal -- that's
    `advanceFromNode`/`_advance`'s separate job once the node resolves).

    Called from exactly one place, `_visit_node`, at the same point
    `onNodeClick` does it: after `state.currentNode` is set and before the
    node type is resolved or dispatched. That single site covers every node
    type, which is what the source's own single site does.

    An ordinary node that resolves immediately cannot observe this
    separately from `_advance`'s own (idempotent) sibling-lock loop. Three
    families CAN, and are why the eager placement matters:

    - a node that SUSPENDS on a choice screen (catch/item/swap) -- the
      siblings are locked while the screen is up, long before the choice
      resolves and reaches `_advance`;
    - a battle that ENDS THE RUN -- `_advance` is never reached at all, yet
      the source has still locked the siblings;
    - `_enter_sub_map`, which swaps `state.map` out to the generated submap
      without ever calling `_advance` on the parent UNDERGROUND/DISTORTION
      node (only `_return_from_sub_map` does, on the way back out), so
      without the eager lock the parent siblings would be captured into
      `state.sub_map_return` still accessible."""
    node = map_obj.nodes[node_id]
    for other in map_obj.nodes.values():
        if other.layer == node.layer and other.id != node_id and other.accessible:
            other.accessible = False


def _enter_sub_map(state: RunState, node: MapNode, kind: str) -> None:
    """Port of `enterSubMap` (bundle.deobfuscated.js:76687-76706) -- the
    UNDERGROUND/DISTORTION node click handler. Generates the submap
    (`map_gen.generate_sub_map`) and swaps it in as `state.map`, saving
    enough of the PARENT map/position in `state.sub_map_return` to restore
    on `_visit_subexit`/`_return_from_sub_map`. Stays in `Phase.ON_MAP` --
    the player keeps issuing ordinary `VisitNode` actions inside the submap,
    no new Phase needed for navigation itself.

    `parent_node_level` (`map_gen.get_level_for_node`'s gen2/3/4 branch,
    ALWAYS taken here since UNDERGROUND/DISTORTION nodes only exist when
    `gen4_mode=True`, `map_gen.generate_map`'s own node-placement gate) is
    computed once here and threaded through -- that branch is deterministic
    (zero `rng()` draws), unlike the gen1 branch, so this call site never
    perturbs the RNG stream before `generate_sub_map`'s own draws begin. It's
    ALSO saved onto `state.sub_map_return` -- `subMapBaseLevel` (bundle.
    deobfuscated.js:76416-76452) always recomputes this same value live off
    `state.subMapReturn` for as long as the player stays inside the submap
    (used again by `doSubMapBoss`'s empty-`bossTeam` reroll path, see
    `_visit_sub_map_boss`), so caching it here is value-equivalent, not an
    approximation -- the source recomputation is itself RNG-free and
    depends only on the (unchanged, already-saved) parent node.

    No sibling lock happens here. The source's `enterSubMap` has none either
    (76687-76706) -- it relies wholly on `onNodeClick`'s eager pre-dispatch
    lock, which `_visit_node` now performs for EVERY node type, so the parent
    siblings are already locked before the parent map is captured into
    `state.sub_map_return`. The private call this function used to make (added
    by the M2.1-M2.3 repair, when `_visit_node` still had no lock) would now be
    a second, redundant application; removing it keeps one lock site, matching
    the source, so a mutation that deletes the eager lock cannot be masked here
    for submap routes only."""
    parent_level = map_gen.get_level_for_node(
        node.layer, state.current_map, state.gen2_mode, state.gen3_mode, state.gen4_mode
    )
    result = map_gen.generate_sub_map(
        kind,
        state.current_map,
        parent_level,
        len(state.team),
        distortion_worlds_entered=state.distortion_worlds_entered,
        distortion_legendary_claimed=state.distortion_legendary_claimed,
        gen4_mode=state.gen4_mode,
    )
    state.distortion_worlds_entered = result.distortion_worlds_entered
    state.sub_map_return = {
        "kind": kind,
        "map": state.map,
        "map_index": state.current_map,
        "node_id": node.id,
        "no_advance": False,
        "parent_node_level": parent_level,
    }
    state.in_sub_map = kind
    state.entered_sub_map = True
    state.map = result.map
    state.current_node_id = "n0_0"
    state.phase = Phase.ON_MAP
    _log(state, "enter_sub_map", kind=kind, node_id=node.id)


def _visit_underground(state: RunState, node: MapNode) -> None:
    _enter_sub_map(state, node, map_gen.UNDERGROUND)


def _visit_distortion(state: RunState, node: MapNode) -> None:
    _enter_sub_map(state, node, map_gen.DISTORTION)


def _return_from_sub_map(state: RunState) -> None:
    """Port of `returnFromSubMap` (bundle.deobfuscated.js:76708-76730) --
    restores the parent map/position, fully heals the team (unconditional in
    Story/Nuzlocke -- the `challengeNoHeal` gate is Challenge-mode-only, out
    of scope), and advances the originating node UNLESS `no_advance` is set
    (only ever true for the "reset power" `useShadowForce` call site this
    port doesn't model -- every `_enter_sub_map` call here sets it `False`).
    A missing `sub_map_return` (defensive; can't happen via this port's own
    `VisitNode`-driven flow, since `state.in_sub_map` is only ever set
    alongside it) just returns to the map unchanged, matching the source's
    own early-return guard."""
    sub_map_return = state.sub_map_return
    state.in_sub_map = None
    state.sub_map_return = None
    if sub_map_return is None:
        state.phase = Phase.ON_MAP
        return
    state.map = sub_map_return["map"]
    state.current_map = sub_map_return["map_index"]
    for mon in state.team:
        mon.current_hp = mon.max_hp
    if not sub_map_return.get("no_advance"):
        _advance(state, sub_map_return["node_id"])
    state.current_node_id = sub_map_return["node_id"]
    state.phase = Phase.ON_MAP
    _log(state, "return_from_sub_map")


def _visit_subexit(state: RunState, node: MapNode) -> None:
    """Port of the `NODE_TYPES.SUBEXIT` click dispatch
    (bundle.deobfuscated.js:77380-77382: `case SUBEXIT: returnFromSubMap();
    break`)."""
    _return_from_sub_map(state)


def _visit_sub_map_boss(state: RunState, node: MapNode) -> None:
    """Port of `doSubMapBoss` (bundle.deobfuscated.js:76752-76837) -- reached
    only via `_visit_boss`'s own `subBoss` check, mirroring `doBossNode`'s
    `if (iu.subBoss) { doSubMapBoss(iu); return; }`
    (bundle.deobfuscated.js:77744-77747). The enemy team is ALREADY baked
    into `node.extra["bossTeam"]` at submap-generation time
    (`map_gen.generate_sub_map`) -- the source's own re-roll fallback for a
    missing/empty `bossTeam` (bundle.deobfuscated.js:76756-76766) is
    unreachable via this port's own generator (which always populates it),
    ported here only as the defensive empty-team early-advance below.
    `runBattleScreen`'s own call here passes `isBoss=true` explicitly
    (bundle.deobfuscated.js:76816-76836, the 2nd positional arg `!0x0`) --
    no Escape Rope recovery offer, same convention as every other boss-tier
    fight (`_visit_silver`/`_visit_admin`/gym leader). Level gain is 2
    (the 7th positional arg `0x2`), participants-only (NOT `all_team_xp`,
    unlike Silver/Admin's whole-team-xp convention), and ordinary Nuzlocke
    permadeath applies (no `no_permadeath` flag set in the source here,
    unlike Silver/Admin's `_noPermaDeath` exemptions).

    An absent/empty `node.extra["bossTeam"]` does NOT safe-advance --
    source rerolls/rebuilds the encounter via `rollSubMapBoss` (bundle.
    deobfuscated.js:76756-76766: `B["bossTeam"] && B["bossTeam"]["length"]
    ? B["bossTeam"] : rollSubMapBoss(iS, mapIndex)["team"]`), consuming
    exactly the same one `rng()` draw `_roll_sub_map_boss` uses at
    generation time. The reroll result is NOT written back onto
    `node.extra` (the source's `it` is a local, never assigned to
    `B["bossTeam"]`) -- a later re-visit (a non-Nuzlocke loss that leaves
    the node accessible again) would reroll AGAIN with fresh RNG draws, so
    this port doesn't persist it either. The genuinely-defensive
    `if not enemy` safe-advance below is `doSubMapBoss`'s OWN separate
    guard for when even the (guaranteed non-empty, table-backed) rerolled
    team fails to resolve to any combatant (bundle.deobfuscated.js:76779-
    76785, the `fetchPokemonById` failure path) -- unreachable via this
    port's offline data, kept for citation fidelity same as elsewhere in
    this module."""
    bag_team = node.extra.get("bossTeam")
    if not bag_team:
        sub_map_return = state.sub_map_return or {}
        kind = node.extra.get("subBoss") or state.in_sub_map
        map_index = sub_map_return.get("map_index", state.current_map)
        parent_node_level = sub_map_return.get("parent_node_level")
        _, _, bag_team = map_gen._roll_sub_map_boss(kind, map_index, parent_node_level)
    enemy = _build_sub_map_boss_team(bag_team, state.gen2_mode, state.gen4_mode)
    if not enemy:
        _advance(state, node.id)
        state.phase = Phase.ON_MAP
        return
    result = _run_battle(state, enemy)
    if not _after_battle(state, result, level_gain=2):
        return
    state._todo = [{"kind": "evolve", "idx": 0}, {"kind": "advance", "node_id": node.id}]
    _run_todo(state)


# doSubMapReward's `B2y` gen4Mode-vs-Endless stat-buff multiplier
# (bundle.deobfuscated.js:76920-76926) -- ALWAYS the gen4Mode branch (0.5)
# here, since submaps only exist in gen4_mode and Endless mode is out of
# scope throughout this module.
_SUBMAP_REWARD_STAT_MULTIPLIER = 0.5

# "fossil" reward candidate species -- Cranidos(408)/Shieldon(410),
# bundle.deobfuscated.js:77066.
_FOSSIL_SPECIES_IDS = (0x198, 0x19A)

# distortion legendary reward -> fixed numeric dex id (bundle.deobfuscated.js:
# 77083-77086) -- NOTE this is base Giratina (487), NOT the wild boss
# encounter's "giratina-origin" alt forme (see `_resolve_sub_map_boss_species`).
_DISTORTION_LEGEND_REWARD_SPECIES = {"giratina": 0x1E7, "dialga": 0x1E3, "palkia": 0x1E4}


def _recompute_max_hp(mon: Combatant, gen4_mode: bool, *, full: bool) -> None:
    """Port of `_recomputeMaxHp` (bundle.deobfuscated.js:76731-76751)."""
    hp_buff = (mon.stat_buffs or {}).get("hp", 0)
    computed = max(1, math.floor(map_gen.calc_hp(mon.base_stats.hp, mon.level) * (1 + 0.05 * hp_buff)))
    mon.max_hp = _wg_max_hp(mon.species_id, gen4_mode, computed)
    if full:
        mon.current_hp = mon.max_hp
    else:
        mon.current_hp = min(mon.current_hp, mon.max_hp)


def _apply_run_level_gain(team: Sequence[Combatant], amount: int, gen4_mode: bool) -> None:
    """Port of `doSubMapReward`'s `B2Q` helper (bundle.deobfuscated.js:
    76914-76919) -- flat, UNSCALED level gain (no `_SUBMAP_REWARD_STAT_
    MULTIPLIER` scaling, unlike `_apply_run_stat_buff` below), used by the
    "team_lvl2"/"sacrifice" rewards. Full-heals afterward (`_recomputeMaxHp`
    called with its default `O=true`)."""
    for mon in team:
        mon.level = min(map_gen.sub_map_level_cap(), mon.level + amount)
        _recompute_max_hp(mon, gen4_mode, full=True)


def _apply_run_stat_buff(mon: Combatant, stat: str, amount: int) -> None:
    """Port of `doSubMapReward`'s `B2d` helper (bundle.deobfuscated.js:
    76927-76935) -- `amount` is halved+rounded (`_SUBMAP_REWARD_STAT_
    MULTIPLIER`, always 0.5 here) before being added, clamped to the same
    +-10 range `battle.apply_stage_change` uses for in-battle stages (a
    SEPARATE, persistent field -- see `Combatant.stat_buffs`'s own
    docstring)."""
    scaled = max(1, map_gen._js_round(amount * _SUBMAP_REWARD_STAT_MULTIPLIER)) if amount > 0 else amount
    mon.stat_buffs = dict(mon.stat_buffs or {})
    mon.stat_buffs[stat] = max(-10, min(10, mon.stat_buffs.get(stat, 0) + scaled))


def _sub_map_reward_level_basis(map_index: int) -> int:
    """Port of `doSubMapReward`'s own `B2P` level basis
    (bundle.deobfuscated.js:76900-76909, non-Endless branch) -- the PARENT
    map's raw level-range MAX, distinct from `map_gen._sub_map_base_level`'s
    BOSS formula (which adds +1 on top of the parent NODE's own level, not
    the whole map range's max)."""
    ranges = data.get_map_level_ranges(4)
    idx = min(max(map_index, 0), len(ranges) - 1)
    return ranges[idx].max


@lru_cache(maxsize=1)
def _transform_candidate_pool() -> tuple[int, ...]:
    """Port of the "transform" reward's candidate-pool filter
    (bundle.deobfuscated.js:76984-76992, `ALL_CATCHABLE_IDS` itself per
    bundle.deobfuscated.js:48896-48900: every dex id 1-721 EXCLUDING
    legendaries) -- evolution-line ROOTS only, Gen4-line-eligible,
    non-legendary. Cached: depends on no per-call state."""
    legendary_ids = data.get_legendary_ids()
    return tuple(
        sid
        for sid in range(1, 0x2D2)
        if sid not in legendary_ids and battle_abilities.get_evo_line_root(sid) == sid and map_gen.is_gen4_line_eligible(sid)
    )


def _apply_transform_reward(state: RunState, node: MapNode) -> None:
    """Port of the "transform" reward (bundle.deobfuscated.js:76983-77020) --
    re-rolls EVERY team member into an independently-random Gen4-eligible
    non-legendary species (one `rng()` draw per member when the candidate
    pool is non-empty), +4 levels. Level/HP-recompute apply UNCONDITIONALLY
    per the source's own comma-expression (species/name/types/base_stats
    swap only if the species resolves; here that's the whole of
    `data.get_pokedex()`, so the defensive "didn't resolve" branch is
    unreachable in practice, unlike the source's live-fetch failure mode)."""
    pokedex = data.get_pokedex()
    pool = _transform_candidate_pool()
    for mon in state.team:
        new_level = min(map_gen.sub_map_level_cap(), mon.level + 4)
        chosen = pool[int(rng.rng() * len(pool))] if pool else mon.species_id
        resolved = map_gen.resolve_evo_for_level(chosen, new_level)
        entry = pokedex.get(resolved)
        if entry is not None:
            mon.species_id = resolved
            mon.name = entry.name
            mon.nickname = None
            mon.types = entry.types
            mon.base_stats = entry.base_stats
            mon.flags = {**mon.flags, "_megaEvolved": False, "_baseForm": None}
        mon.level = new_level
        _recompute_max_hp(mon, state.gen4_mode, full=True)
    _finish_reward(state, node.id)


def _finish_reward(state: RunState, node_id: str) -> None:
    """Port of `_finishReward` (bundle.deobfuscated.js:76838-76844)."""
    _advance(state, node_id)
    state.phase = Phase.ON_MAP


def _visit_reward(state: RunState, node: MapNode) -> None:
    """Port of `doSubMapReward` (bundle.deobfuscated.js:76885-77107) -- the
    submap's REWARD-type node, dispatching on the reward id baked into
    `node.extra["reward"]` at submap-generation time. A missing/unrecognized
    reward id (`submapReward` returning `null`) is just a free advance
    (bundle.deobfuscated.js:76888-76892), same as the explicit "skip" id and
    any other unhandled id (bundle.deobfuscated.js:77099-77106)."""
    reward_id = node.extra.get("reward")
    reward = data.get_submap_reward_by_id().get(reward_id) if reward_id else None
    if reward is None:
        _finish_reward(state, node.id)
        return

    sub_map_return = state.sub_map_return or {}
    map_index = sub_map_return.get("map_index", state.current_map)
    level_basis = _sub_map_reward_level_basis(map_index)

    if reward.id == "team_lvl2":
        _apply_run_level_gain(state.team, 2, state.gen4_mode)
        _finish_reward(state, node.id)
        return
    if reward.id == "rare_candy":
        state.items.append("rare_candy")
        _finish_reward(state, node.id)
        return
    if reward.id == "three_items":
        # bundle.deobfuscated.js:76957-76970: sample WITHOUT replacement via
        # repeated splice from `ITEM_POOL` (`data.get_passive_items()`, NOT
        # the usable-items table), not a shuffle -- up to 3 draws, one
        # `rng()` draw per successful pick, stopping early if the
        # (bag-deduplicated) pool empties.
        bag_ids = set(state.items)
        pool = [item for item in data.get_passive_items() if item.id not in bag_ids]
        for _ in range(3):
            if not pool:
                break
            idx = int(rng.rng() * len(pool))
            item = pool.pop(idx)
            state.items.append(item.id)
        _finish_reward(state, node.id)
        return
    if reward.id == "attack_up":
        for mon in state.team:
            _apply_run_stat_buff(mon, "atk", 2)
            _apply_run_stat_buff(mon, "special", 2)
        _finish_reward(state, node.id)
        return
    if reward.id == "transform":
        _apply_transform_reward(state, node)
        return
    if reward.id == "sacrifice":
        if len(state.team) < 2:
            # bundle.deobfuscated.js:77027: re-checked at RESOLUTION time --
            # `pickSubMapRewards`'s own `minTeam` gate only guaranteed >=2
            # members at submap-GENERATION time; Nuzlocke's fainted-cull
            # after the boss fight can shrink it before the player gets here.
            _finish_reward(state, node.id)
            return
        state.pending = PendingChoice(
            phase=Phase.REWARD_TEAM_PICK,
            options=[_mon_summary(m) for m in state.team],
            optional=False,
            extra={"node_id": node.id, "kind": "sacrifice"},
        )
        state.phase = Phase.REWARD_TEAM_PICK
        return
    if reward.id == "stat10":
        state.pending = PendingChoice(
            phase=Phase.REWARD_TEAM_PICK,
            options=[_mon_summary(m) for m in state.team],
            optional=False,
            extra={"node_id": node.id, "kind": "stat10"},
        )
        state.phase = Phase.REWARD_TEAM_PICK
        return
    if reward.id == "fossil":
        # bundle.deobfuscated.js:77065-77078: straight into `showSwapScreen`
        # -- NO `checkAndEvolveTeam` pre-choice scan (unlike a battle win's
        # `_run_todo`/`evolve` step) and no room-based auto-add. `_offer_
        # swap_screen` (not `_try_add_to_team`) is the exact match for that.
        species = _FOSSIL_SPECIES_IDS[int(rng.rng() * len(_FOSSIL_SPECIES_IDS))]
        level = min(map_gen.sub_map_level_cap(), level_basis + 4)
        resolved = map_gen.resolve_evo_for_level(species, level)
        mon = _make_wild_combatant(resolved, level, move_tier=2, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)
        _offer_swap_screen(state, mon, node.id)
        return
    if reward.id in _DISTORTION_LEGEND_REWARD_SPECIES:
        # bundle.deobfuscated.js:77079-77097: `distortionLegendaryClaimed`
        # is set at OFFER time (before `showSwapScreen`, i.e. regardless of
        # the player's eventual accept/decline) -- `distortionLegendary()`
        # (bundle.deobfuscated.js:76399-76409) reads this same flag to gate
        # whether a FUTURE Distortion visit can roll another legendary at
        # all, so a decline still permanently forecloses a second offer this
        # run, same as an accept would.
        state.distortion_legendary_claimed = True
        species = _DISTORTION_LEGEND_REWARD_SPECIES[reward.id]
        level = min(map_gen.sub_map_level_cap(), max(1, level_basis + 1))
        mon = _make_wild_combatant(species, level, move_tier=2, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)
        _offer_swap_screen(state, mon, node.id)
        return
    # "skip" and any unrecognized id -- bundle.deobfuscated.js:77099-77106.
    _finish_reward(state, node.id)


def _resolve_reward_team_pick(state: RunState, action: SelectOption) -> None:
    """Resolves `Phase.REWARD_TEAM_PICK` -- the "sacrifice"/"stat10" submap
    rewards, both driven by `showTeamPickerModal` in the source
    (bundle.deobfuscated.js:76845-76884: only per-member click handlers, no
    cancel/skip button, matching `PendingChoice.optional=False`)."""
    extra = state.pending.extra
    node_id = extra["node_id"]
    kind = extra["kind"]
    idx = action.index
    if kind == "sacrifice":
        # bundle.deobfuscated.js:77021-77039: re-checked here too (not just
        # at `_visit_reward`'s offer-time gate) -- matches the source's own
        # `if (Bci != null && B2n.length>=2 && B2n[Bci])` guard.
        if len(state.team) >= 2 and idx is not None and 0 <= idx < len(state.team):
            state.team.pop(idx)
            _apply_run_level_gain(state.team, 4, state.gen4_mode)
    elif kind == "stat10":
        mon = state.team[idx]
        for stat in ("hp", "atk", "def", "speed", "special", "spdef"):
            _apply_run_stat_buff(mon, stat, 2)
        _recompute_max_hp(mon, state.gen4_mode, full=False)
    state.pending = None
    _finish_reward(state, node_id)


# ---------------------------------------------------------------------------
# Pending-choice resolvers (SelectOption while state.phase is a *_CHOICE phase)
# ---------------------------------------------------------------------------


def _resolve_catch_choice(state: RunState, action: SelectOption) -> None:
    extra = state.pending.extra
    node_id = extra["node_id"]
    origin = extra.get("origin")
    if action.index is None:
        # The two DECLINE buttons clear different resume records, exactly
        # like the two room accepts above (M4.2): `#btn-skip-shiny`
        # (80984-80989) nulls `savedShinyNode` and nothing else, while
        # `#btn-skip-catch` (78953-78959) nulls `savedCatch` and
        # `savedQuestionResolve` and leaves `savedShinyNode` alone.
        if origin == "shiny_node":
            state.saved_shiny_node = None
        else:
            state.saved_catch = None
            state.saved_question_resolve = None
        state.pending = None
        _advance(state, node_id)
        state.phase = Phase.ON_MAP
        return
    mon = extra["candidates"][action.index]
    state.pending = None
    _try_add_to_team(state, mon, node_id, origin=origin)


def _resolve_swap_choice(state: RunState, action: SelectOption) -> None:
    """`extra["has_room"]` (set only by `_offer_swap_screen`, never by
    `_try_add_to_team`'s own team-full-only SWAP_CHOICE) selects between
    `showSwapScreen`'s two accept handlers (bundle.deobfuscated.js:79171-
    79201 with room / 79202-79246 full) -- clicking the single incoming-mon
    option APPENDS rather than releasing a team member.

    All THREE of `showSwapScreen`'s exits clear `state.currentNode` right
    after `advanceFromNode` -- the accept-with-room handler (79185-79186),
    the release-and-replace handler (79230-79231) and the cancel button
    (79255-79256) each read `advanceFromNode(state["map"], O["id"]),
    (state["currentNode"] = null),`. Every SWAP_CHOICE this port raises is a
    real `showSwapScreen` (`_offer_swap_screen`, plus `_try_add_to_team`'s
    team-full branch, which is exactly `catchPokemon`'s own
    `: showSwapScreen(B, O)` fallthrough at 79045), so the clear is
    unconditional here.

    It is deliberately NOT copied to `catchPokemon`'s room path
    (79036-79044) -- that branch calls `advanceFromNode` and `showMapScreen`
    and leaves `currentNode` set. The asymmetry is source behavior, not a
    typo; `_try_add_to_team`'s own room branch matches it."""
    extra = state.pending.extra
    node_id = extra["node_id"]
    if action.index is not None:
        incoming = extra["incoming"]
        if extra.get("has_room"):
            state.team.append(incoming)
            state.max_team_size = max(state.max_team_size, len(state.team))
            _log(state, "catch", species_id=incoming.species_id, name=incoming.name, is_shiny=incoming.is_shiny)
        else:
            released = state.team[action.index]
            if released.held_item is not None:
                state.items.append(released.held_item.id)
            state.team[action.index] = incoming
            state.max_team_size = max(state.max_team_size, len(state.team))
            _log(state, "catch", species_id=incoming.species_id, name=incoming.name, is_shiny=incoming.is_shiny, released=released.name)
    # UNLIKE the two room accepts and the two decline buttons in
    # `_try_add_to_team`/`_resolve_catch_choice`, all THREE of
    # `showSwapScreen`'s exits clear ALL THREE resume records -- the
    # room accept (79182-79184), the release-and-replace handler
    # (79227-79229) and `#btn-cancel-swap` (79252-79254) each read the same
    # `savedCatch = savedQuestionResolve = savedShinyNode = null` sequence.
    # That is why the swap screen, whichever node raised it, is the one
    # place the branch-specific asymmetry disappears (M4.2).
    state.saved_catch = None
    state.saved_question_resolve = None
    state.saved_shiny_node = None
    state.pending = None
    _advance(state, node_id)
    state.current_node_id = None
    state.phase = Phase.ON_MAP


def _resolve_evolution_choice(state: RunState, action: SelectOption) -> None:
    extra = state.pending.extra
    idx = extra["team_index"]
    branches = extra["branches"]
    chosen = branches[action.index]
    _apply_evolution(state, state.team[idx], chosen.into, force=extra.get("force", False))
    _log(state, "evolve", team_index=idx, into=chosen.into, name=chosen.name)
    state.pending = None
    if extra.get("source", "todo") == "item":
        # Raised by `_apply_use_item` (Moon Stone/Rare Candy), not the
        # post-battle `_todo` queue -- just return to the map.
        state.phase = Phase.ON_MAP
        return
    state._todo[0]["idx"] = idx + 1
    _run_todo(state)


def _resolve_move_tutor_choice(state: RunState, action: SelectOption) -> None:
    extra = state.pending.extra
    node_id = extra["node_id"]
    if action.index is not None:
        team_index = state.pending.options[action.index]["team_index"]
        mon = state.team[team_index]
        mon.move_tier = min(2, mon.move_tier + 1)  # CODEX.md issue 11: tier 0 -> 1, not -> 2
        state.used_tm = True
        _log(state, "move_tutor", team_index=team_index, name=mon.name, move_tier=mon.move_tier)
    state.pending = None
    _advance(state, node_id)
    state.phase = Phase.ON_MAP


def _resolve_item_choice(state: RunState, action: SelectOption) -> None:
    extra = state.pending.extra
    node_id = extra["node_id"]
    if action.index is None:
        # `#btn-skip-item` (79433-79440): `state.itemOffer = null` FIRST,
        # then advance.
        state.item_offer = None
        state.pending = None
        _advance(state, node_id)
        state.phase = Phase.ON_MAP
        return
    item = extra["items"][action.index]
    state.picked_up_item = True
    if item.usable:
        state.items.append(item.id)
        _log(state, "item", name=item.name, usable=True)
        # The usable branch's own `state.itemOffer = null` (79419-79422).
        state.item_offer = None
        state.pending = None
        _advance(state, node_id)
        state.phase = Phase.ON_MAP
        return
    state.pending = PendingChoice(
        phase=Phase.ITEM_EQUIP_CHOICE,
        options=[_mon_summary(m) for m in state.team],
        optional=True,
        extra={"item_id": item.id, "node_id": node_id},
    )
    state.phase = Phase.ITEM_EQUIP_CHOICE


def _resolve_item_equip_choice(state: RunState, action: SelectOption) -> None:
    """`openItemEquipModal`'s `#btn-equip-to-bag` button (79552-79562) is a
    real decline: `doItemNode`'s non-usable branch always calls it with
    `fromBagIdx=-1, fromPokemonIdx=-1` (79423-79429, the only configuration
    reachable from an ordinary node visit), so `O < 0 && state.items.push(B)`
    is the live branch -- clicking it banks the item instead of equipping it,
    then advances exactly like an equip would. Found tracing the exact
    source for the M4 route-oracle bridge work: `PendingChoice.optional` was
    `False` and this resolver had no `action.index is None` branch at all, a
    real gap since the source always offers this exit, not a simplification.
    `#btn-equip-cancel` (79563-79569) is `SelectOption(cancel=True)` as of
    M5. It is NOT a decline: its entire body is `B2O.remove()`, so it does
    not equip, does not bank the item, does not call `onComplete` and
    therefore never reaches `onComplete`'s `state.itemOffer = null;
    advanceFromNode(...)` pair (79424-79429). The node stays unvisited and
    accessible with `state.itemOffer` still pinned, so re-visiting it
    restores the same three items for zero RNG draws -- which is the whole
    reason `RunState.item_offer` exists. A previous version of this
    docstring called it "a genuine dead end"; that was right about the click
    and wrong about the run, and the offer-pinning consequence was missed."""
    extra = state.pending.extra
    node_id = extra["node_id"]
    if action.cancel:
        # Back to the map with NOTHING consumed: no bag change, no held-item
        # change, the node not advanced, and `item_offer` deliberately kept.
        _log(state, "item_equip_cancelled", name=extra["item_id"], node_id=node_id)
        state.pending = None
        state.phase = Phase.ON_MAP
        return
    if action.index is None:
        state.items.append(extra["item_id"])
        _log(state, "item", name=extra["item_id"], usable=False, kept_in_bag=True)
        # `onComplete` (79424-79428): clear the pinned offer, then advance.
        state.item_offer = None
        state.pending = None
        _advance(state, node_id)
        state.phase = Phase.ON_MAP
        return
    mon = state.team[action.index]
    if mon.held_item is not None:
        state.items.append(mon.held_item.id)
    mon.held_item = HeldItem(id=extra["item_id"])
    _log(state, "item", name=extra["item_id"], usable=False, equipped_on=mon.name)
    state.item_offer = None
    state.pending = None
    _advance(state, node_id)
    state.phase = Phase.ON_MAP


def _resolve_trade_choice(state: RunState, action: SelectOption) -> None:
    """Port of `doTradeNode`'s click handler + `rollStoryTradeReplacement` +
    `completeTrade` (bundle.deobfuscated.js:80620-80628, 80785-80839) --
    the ordinary (non-Endless2) story-mode trade path. CODEX.md issue 1/21's
    level fix (+`tradeLevelGain()`, capped 100 outside Endless) is preserved;
    this additionally fixes issue 7 (pool size/exclude-starters args) and
    issue 8 (held-item transfer must happen only after a replacement is
    actually constructed, matching `completeTrade`, not unconditionally
    up front)."""
    extra = state.pending.extra
    node_id = extra["node_id"]
    if action.index is None:
        state.pending = None
        _advance(state, node_id)
        state.phase = Phase.ON_MAP
        return
    outgoing = state.team[action.index]
    # `rollStoryTradeReplacement`'s real pool is `getCatchChoices(
    # getEncounterMapIndex(), 0x12, maxGenId, !isEndlessMode, minGenId,
    # isEndlessMode)` -- 18 candidates, starters EXCLUDED in ordinary
    # (non-Endless) play, unlike this module's other trade-adjacent
    # approximations that reuse `get_catch_choices` with different args.
    offer_level = min(100, outgoing.level + 3)
    min_dex, max_dex = map_gen.get_catch_gen_range(state.gen2_mode, state.gen3_mode, state.gen4_mode)
    pool = list(
        map_gen.get_catch_choices(
            state.current_map,
            18,
            max_dex,
            min_dex,
            exclude_starters=True,
            gen2_mode=state.gen2_mode,
            gen3_mode=state.gen3_mode,
            gen4_mode=state.gen4_mode,
        )
    )
    excluded = [sid for sid in pool if sid != outgoing.species_id]
    if excluded:
        pool = excluded
    if not pool:
        # `rollStoryTradeReplacement` returns null; `doTradeNode`'s handler
        # just advances past the node without calling `completeTrade` at
        # all -- the outgoing Pokemon's held item is NEVER transferred here.
        state.pending = None
        _advance(state, node_id)
        state.phase = Phase.ON_MAP
        return
    new_species = pool[int(rng.rng() * len(pool))]
    move_tier = max(map_gen.get_move_tier_for_map(state.current_map), outgoing.move_tier)
    is_shiny = roll_shiny(state)
    incoming = _make_wild_combatant(new_species, offer_level, is_shiny=is_shiny, move_tier=move_tier, gen2_mode=state.gen2_mode, gen4_mode=state.gen4_mode)
    # `completeTrade` transfers the held item only once `incoming` exists.
    if outgoing.held_item is not None:
        state.items.append(outgoing.held_item.id)
    state.team[action.index] = incoming
    state.got_via_question = True
    _log(state, "trade", gave=outgoing.name, received=incoming.name)
    state.pending = None
    _advance(state, node_id)
    state.phase = Phase.ON_MAP


def _resolve_escape_rope_choice(state: RunState, action: SelectOption) -> None:
    """Port of the two `btn-continue-battle` click handlers wired up in
    `runBattleScreen`'s eligible-loss branch (bundle.deobfuscated.js:81388-
    81429), CODEX.md P0.6. `action.index is None` (decline) is the button's
    DEFAULT handler -- `B2D() || (ip && ip(), B2a(!0x1))` -- the same loss/
    game-over callback an ineligible loss uses. `action.index == 0` (accept)
    is the "Use Escape Rope" button's handler: consumes exactly the bag
    entry found at offer time, zeroes every team member's HP, then sets
    ONLY the final team-list member back to 1 HP (`state["team"][BI7]`
    where `BI7 = length - 1`) -- an intentionally UNCHANGED, if unusual,
    replication of the source's own behavior, not "every survivor" or "the
    first member". Continuing then re-enters the SAME success callback
    (`iu`) the original battle would have called on an actual win, via
    `rope_continuation` stashed on `pending.extra` by `_after_battle` --
    critically not the `evolve` step, since none of the win-branch
    processing that precedes `iu()` in the source ever runs here.
    """
    extra = state.pending.extra
    if action.index is None:
        state.pending = None
        state.phase = Phase.GAME_OVER
        state.game_over = True
        state._todo = []
        _log(state, "game_over")
        return
    rope_index = extra["rope_index"]
    state.items.pop(rope_index)
    for mon in state.team:
        mon.current_hp = 0
    if state.team:
        state.team[-1].current_hp = 1
    state.escaped_via_rope = True
    _log(state, "escape_rope_used", rope_index=rope_index)
    state.pending = None
    state._todo = list(extra["continuation"])
    _run_todo(state)


_PENDING_RESOLVERS = {
    Phase.CATCH_CHOICE: _resolve_catch_choice,
    Phase.SWAP_CHOICE: _resolve_swap_choice,
    Phase.EVOLUTION_CHOICE: _resolve_evolution_choice,
    Phase.MOVE_TUTOR_CHOICE: _resolve_move_tutor_choice,
    Phase.ITEM_CHOICE: _resolve_item_choice,
    Phase.ITEM_EQUIP_CHOICE: _resolve_item_equip_choice,
    Phase.TRADE_CHOICE: _resolve_trade_choice,
    Phase.ESCAPE_ROPE_CHOICE: _resolve_escape_rope_choice,
    Phase.REWARD_TEAM_PICK: _resolve_reward_team_pick,
}


_NODE_HANDLERS = {
    map_gen.BATTLE: _visit_battle,
    map_gen.CATCH: _visit_catch,
    map_gen.ITEM: _visit_item,
    map_gen.BOSS: _visit_boss,
    map_gen.POKECENTER: _visit_pokecenter,
    map_gen.TRAINER: _visit_trainer,
    map_gen.LEGENDARY: _visit_legendary,
    map_gen.MOVE_TUTOR: _visit_move_tutor,
    map_gen.TRADE: _visit_trade,
    "shiny": _visit_shiny,
    # source: `case "mega": doItemNode(B);` -- no branch param, verbatim
    # (docs/logic-notes-nodes.md section 0).
    "mega": _visit_item,
    map_gen.SILVER: _visit_silver,
    map_gen.MAGMA: lambda state, node: _visit_admin(state, node, "magma"),
    map_gen.AQUA: lambda state, node: _visit_admin(state, node, "aqua"),
    # Special submaps (docs/logic-notes-submaps.md) -- Gen4/Sinnoh-only.
    map_gen.UNDERGROUND: _visit_underground,
    map_gen.DISTORTION: _visit_distortion,
    map_gen.REWARD: _visit_reward,
    map_gen.SUBEXIT: _visit_subexit,
}
