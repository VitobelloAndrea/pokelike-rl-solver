"""Battle math: damage formula, effective stats, turn order, and status-effect
ticks (docs/logic-notes.md sections 7-8, `calcDamage`/`getEffectiveStat`/
turn-order slice of `runBattle`, bundle.deobfuscated.js:54825-55711).

**Scope of this module today.** This is the damage/stat/turn-order/status
math only -- the self-contained pure functions that don't need the rest of
the battle loop's plumbing (move selection, hazards, KO/faint handling,
event logging) to be correct. `runBattle` itself (bundle.deobfuscated.js:
55164 onward, well over a thousand lines) and the ~95-trait
`buildTraitsConfig` hook system (`onKO`, `endOfRound`, hazard triggers,
elemental specials -- docs/logic-notes.md section 9 item 1) are NOT ported
yet; they're the natural follow-on once this piece is validated. Six trait
checks that live directly inline in `calcDamage`/`getEffectiveStat` (not
routed through `buildTraitsConfig`'s hooks) ARE ported here since they're
self-contained -- see docs/logic-notes.md section 7.9 for exactly which six
and why.

Every formula below was cross-checked against a verbatim transcription of
the JS run through Node, not eyeballed -- see pokelike/tests/test_battle.py's
module docstring for the seed/input -> output pairs used.

Deliberately NOT replicated here (out of scope for battle *math*, belongs to
a future event-log/UI layer per CLAUDE.md's "js/ui.js is reference-only"):
the `type: "stat_change"`/`"status_apply"`/etc. battle-log push side effects
that accompany `applyStageChange`/`applyStatus` in the source.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Optional, Sequence

from pokelike import data, rng
from pokelike.data import BaseStats

_DEFAULT_STAGES = ("atk", "def", "speed", "special", "spdef")


@dataclass(frozen=True)
class HeldItem:
    """Minimal held-item reference -- battle math only ever reads `.id`
    (JS `hasItem`/`hasPassive`, bundle.deobfuscated.js:55125-55139). Use
    `data.Item.id` when wiring this up to the real item tables.
    """

    id: str


@dataclass(frozen=True)
class Trait:
    """One entry in the player's collected trait/passive list (roguelike
    meta-progression -- the `buildTraitsConfig` system, docs/logic-notes.md
    section 9 item 1). `enabled` defaults to True: the source's `hasPassive`
    (line 55136-55139) treats a trait as active unless its `enabled` field
    is explicitly `false` -- not an opt-in flag, an opt-out one.
    """

    id: str
    enabled: bool = True


@dataclass(frozen=True)
class MoveInstance:
    """A move as calc_damage sees it. `data.Move` has no `typeless` field
    (nothing in MOVE_POOL needs it) -- it's a runtime-only flag for moves
    built outside that table (e.g. a typeless Struggle fallback), so it
    lives here instead of on `data.Move`.
    """

    power: Optional[int] = None
    type: Optional[str] = None
    typeless: bool = False
    name: str = ""
    is_special: bool = False
    no_damage: bool = False

    @classmethod
    def from_move(cls, move: "data.Move") -> "MoveInstance":
        return cls(power=move.power, type=move.type, name=move.name, is_special=move.category == "special")


@dataclass(frozen=True)
class DamageResult:
    damage: int
    type_eff: float
    move_type: str
    crit: bool


@dataclass
class BattleConfig:
    """Port of the `battleConfig` object threaded through `calcDamage`
    (param `B2n`) and `runBattle`'s turn-order code. `dark_crit_floor` is a
    player-side meta-progression bonus computed once per battle inside
    `buildTraitsConfig` (line ~60856: `0.25 * playerDarkTraitTier`) -- an
    earlier draft of this research mischaracterized it as an enemy-side
    ability config value; it isn't (docs/logic-notes.md 7.5).

    `trick_room` folds together the source's two OR'd trick-room signals
    (a challenge-mode global `CHALLENGE_TRICKROOM` and this config's own
    `isTrickRoom()`) into one flag -- whichever system tracks challenge-mode
    state (engine.py, not battle.py) should set this True if either
    condition holds.

    **Engine-level consolidation (a design decision, not a literal port):**
    the JS keeps weather/Trick-Room state in `buildGen3AbilityConfig`'s
    private closure (docs/logic-notes-gen3abilities.md section 6) and a
    completely separate set of per-battle counters/flags in
    `buildTraitsConfig`'s private closure (`_fightState`,
    docs/logic-notes-traitsconfig.md section 2.2) -- two closures, two
    private objects, never sharing state directly, only merged at the hook
    level. Nothing about their actual semantics requires that separation
    (abilities never read the traits counters and vice versa), so this
    class holds both: it's what `battle_loop.run_battle` passes to
    `calc_damage`/`decide_turn_order` AND what `battle_abilities.
    Gen3AbilityConfig`/`battle_traits.TraitsConfig` read and mutate as
    their shared battle-wide state. The fields below `trick_room` are the
    `buildTraitsConfig`-side counters; see `battle_traits.py`'s module
    docstring for which trait writes each one.
    """

    dark_crit_floor: float = 0.0
    weather: Optional[str] = None  # "rain" | "sun" | "sandstorm" | "hail" | None
    trick_room: bool = False

    sunny_turns: int = 0
    rain_turns: int = 0
    reflect_turns: int = 0
    light_screen_turns: int = 0
    enemy_stealth_rock_pct: float = 0.0
    enemy_toxic_spikes: bool = False
    enemy_sticky_web: bool = False
    fired_flags: dict = field(default_factory=dict)
    elec_lead_fired: bool = False
    team_hp_pool_done: bool = False
    revive_done: bool = False
    fairy_volley_fired: bool = False
    rallied_faints: set = field(default_factory=set)
    kos_handled: set = field(default_factory=set)
    dragon_faint_fired: bool = False
    ghost_revenge_used: bool = False
    no_item_empty: Optional[int] = None
    last_player_team: list = field(default_factory=list)

    def get_weather(self) -> Optional[str]:
        return self.weather

    def is_trick_room(self) -> bool:
        return self.trick_room


@dataclass
class Combatant:
    """A Pokemon's in-battle state. Deliberately close to the JS battler
    object's own shape (snake_case field-for-field) rather than a more
    "designed" API -- Phase 2's state representation isn't settled yet
    (CLAUDE.md defers Gym observation/action space design until then), and
    staying close to the source reduces port risk for the pieces (`stages`,
    `_gen3Ability`, etc.) that battle.py's damage math reads directly.

    `stat_buffs` is a SEPARATE, persistent (not reset between battles) stat
    modifier from `stages` (in-battle-only, reset every fight by
    `initBattleState`, capped +-10) -- confirmed a distinct mechanic by
    tracing `getEffectiveStat` (lines 55084-55098) against where
    `statBuffs` gets written (line ~76930, capped 0-10, looks like a
    roster/training investment system). Populating it is out of scope here;
    battle.py only needs to apply the formula that reads it.

    `flags` is a catch-all for the many single-purpose, one-shot, or
    bookkeeping fields the JS battler object accumulates ad hoc across
    `runBattle`/`buildGen3AbilityConfig`/`buildTraitsConfig` (`_sturdyUsed`,
    `_forceStruggle`, `_mirrorLast`, `_frozenOnEntry`, `_lastMoveIsSpecial`,
    `_lastMoveType`, `_incomingCrit`, `_electricBonusFired`, `_aspearCount`,
    `_critLevelBase`, ... -- see docs/logic-notes-runbattle.md and
    docs/logic-notes-gen3abilities.md/logic-notes-traitsconfig.md for the
    full roster). Each is read/written by exactly one ability/trait/turn-
    loop mechanic, so promoting all ~40 of them to named dataclass fields
    would bloat this class for little benefit -- keyed by the same string
    names as the source (underscore included) for direct cross-referencing
    against the JS.
    """

    species_id: int
    level: int
    base_stats: BaseStats
    types: tuple[str, ...]
    max_hp: int
    current_hp: int
    name: str = ""
    nickname: Optional[str] = None
    held_item: Optional[HeldItem] = None
    move_tier: int = 1
    stages: dict = field(default_factory=lambda: {k: 0 for k in _DEFAULT_STAGES})
    stat_buffs: dict = field(default_factory=dict)
    status: Optional[str] = None  # "freeze" | "sleep" | None -- see module docstring on applyStatus
    burned: bool = False
    paralyzed: bool = False
    poison_stacks: int = 0
    gen3_ability: Optional[str] = None
    is_shiny: bool = False
    force_crit: bool = False
    uncap_stages: bool = False
    augment_pct: Optional[float] = None
    pos_stat_more: Optional[float] = None
    neg_stat_more: Optional[float] = None
    flags: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small helpers -- bundle.deobfuscated.js:54825-54876, 55125-55139, 50355-50357
# ---------------------------------------------------------------------------


def stage_multiplier(stage: float) -> float:
    """Port of `stageMultiplier` (line 54825-54828). NOT mainline's stat-
    stage formula: mainline scales in ratios of 2 (e.g. +6 -> 4x); this
    engine scales in ratios of 10, +3-per-stage (e.g. +6 -> 2.8x, -6 ->
    ~0.357x). Uncapped past +-6 (in-battle cap is +-10, or unbounded with
    the `rand_start` trait's `_uncapStages` flag -- see `apply_stage_change`).
    """
    if stage >= 0:
        return (10 + 3 * stage) / 10
    return 10 / (10 + 3 * abs(stage))


def has_item(items: Optional[Iterable[HeldItem]], item_id: str) -> bool:
    """Port of `hasItem` (line 55125-55127)."""
    return bool(items) and any(item.id == item_id for item in items)


def has_passive(traits: Optional[Iterable[Trait]], trait_id: str) -> bool:
    """Port of `hasPassive` (line 55136-55139). Note the `enabled` check is
    an opt-OUT: a trait counts as active unless `enabled` is explicitly
    False, matching the source's `iu.enabled !== true /* i.e. !== false, see
    module docstring */`.
    """
    return bool(traits) and any(t.id == trait_id and t.enabled is not False for t in traits)


def get_type_boost_item(move_type: str, items: Optional[Iterable[HeldItem]]) -> bool:
    """Port of `getTypeBoostItem` (line 55128-55135) -- does the holder
    carry the Gen3-style type-boost item (Charcoal/Mystic Water/etc, see
    `data.get_type_item_map`) matching `move_type`?
    """
    if not items:
        return False
    item_id = data.get_type_item_map().get(move_type.capitalize())
    if not item_id:
        return False
    return any(item.id == item_id for item in items)


def can_evolve(species_id: int) -> bool:
    """Port of `canEvolve` (line 50355-50357): does this species have any
    entry (linear or branching) in the evolution tables at all?
    """
    return species_id in data.get_evolutions() or species_id in data.get_branching_evolutions()


def uses_special_attack(species_id: int, base_stats: BaseStats) -> bool:
    """Port of `usesSpecialAttack` (bundle.deobfuscated.js:41988-41995,
    docs/logic-notes.md 7.1). A Pokemon's ENTIRE moveset is uniformly
    physical or special -- decided once here, not per-move. Two hardcoded
    species-id exceptions (307, 308 -- Gen3 dex ids) are forced physical
    regardless of stats.
    """
    if species_id in (307, 308):
        return False
    special = base_stats.special or 0
    atk = base_stats.atk or 0
    return special >= atk


def smack_down_hits(attacker: Combatant, move_type: str) -> bool:
    """Port of `_smackDownHits` (line 54851-54859): does the attacker's
    Smack-Down-analog ability un-immunize an otherwise-immune Ground move?
    """
    return attacker.gen3_ability == "smack_down" and move_type.lower() == "ground"


# ---------------------------------------------------------------------------
# Move selection -- `getBestMove`, bundle.deobfuscated.js:42160-42247
# ---------------------------------------------------------------------------

# Geodude/Graveler/Golem (dual Rock/Ground) and Onix (dual Rock/Ground) are
# hardcoded to prefer a Rock-type move over the normal type-iteration order;
# Voltorb/Electrode (pure Electric already, but hardcoded anyway) to Electric.
_ROCK_FORCED_SPECIES = frozenset({74, 75, 76, 95})
_ELECTRIC_FORCED_SPECIES = frozenset({170, 171})

# Three species get a hardcoded move object instead of a MOVE_POOL lookup:
# Magikarp (Splash, no-op), Abra (Teleport, no-op), Wynaut (Mirror Coat --
# a real damage-shaped move object, but its actual battle behavior is the
# `mirror_coat` ability's own beforeTurn/whenAttacked hooks bypassing
# calc_damage entirely, docs/logic-notes-gen3abilities.md section 4.2/4.8).
_HARDCODED_MOVES = {
    129: MoveInstance(power=0, type="Normal", name="Splash", is_special=False, no_damage=True),
    63: MoveInstance(power=0, type="Normal", name="Teleport", is_special=False, no_damage=True),
    360: MoveInstance(power=60, type="Psychic", name="Mirror Coat", is_special=True),
}


@lru_cache(maxsize=1)
def _type_item_type_map() -> dict[str, str]:
    """item id -> type name; inverse of `data.get_type_item_map()`
    (`TYPE_ITEM_TYPE`, bundle.deobfuscated.js:47535-47537, itself built in
    the source as `Object.fromEntries(Object.entries(TYPE_ITEM_MAP).map(
    ([type, itemId]) => [itemId, type]))` -- so this is just the inverse of
    an already-ported table, not separately extracted data).
    """
    return {item_id: type_name for type_name, item_id in data.get_type_item_map().items()}


def get_best_move(
    types: Sequence[str],
    base_stats: BaseStats,
    species_id: int,
    move_tier: int = 1,
    held_item: Optional[HeldItem] = None,
    *,
    has_multitype: bool = False,
) -> MoveInstance:
    """Port of `getBestMove` (line 42160-42247). Picks a single type (the
    Pokemon's entire "moveset" is one move per category/tier, per
    `uses_special_attack`'s per-species-not-per-move category system,
    docs/logic-notes.md 7.1) and builds a `MoveInstance` from
    `data.get_move_pool()`.

    `has_multitype` stands in for `getGen3Ability(species_id) === "multitype"`
    (Arceus-analog) -- passed in rather than looked up here to keep this
    function decoupled from the ability-species tables; a caller wiring up
    `battle_abilities.Gen3AbilityConfig` should pass
    `combatant.gen3_ability == "multitype"`.
    """
    hardcoded = _HARDCODED_MOVES.get(species_id)
    if hardcoded is not None:
        return hardcoded

    uses_special = uses_special_attack(species_id, base_stats)
    tier = max(0, min(2, move_tier if move_tier is not None else 1))
    move_pool = data.get_move_pool()

    def build(type_name: str) -> Optional[MoveInstance]:
        bucket = move_pool.get(type_name.capitalize())
        if not bucket:
            return None
        moves = bucket["special"] if uses_special else bucket["physical"]
        m = moves[tier]
        return MoveInstance(power=m.power, type=type_name.capitalize(), name=m.name, is_special=uses_special)

    if species_id in _ROCK_FORCED_SPECIES:
        move_type = "Rock"
    elif species_id in _ELECTRIC_FORCED_SPECIES:
        move_type = "Electric"
    else:
        move_type = "Normal"
        for t in types:
            if t.lower() == "normal" and len(types) > 1:
                continue
            if move_pool.get(t.capitalize()):
                move_type = t.capitalize()
                break

    type_item_override = False
    if held_item is not None:
        item_type = _type_item_type_map().get(held_item.id)
        if item_type and move_pool.get(item_type.capitalize()):
            if has_multitype or any(t.capitalize() == item_type.capitalize() for t in types):
                move_type = item_type.capitalize()
                type_item_override = True

    if held_item is not None and held_item.id == "metronome" and len(types) >= 2:
        for t in types:
            capped = t.capitalize()
            if capped != move_type:
                alt = build(capped)
                if alt is not None:
                    return alt

    move = build(move_type) or MoveInstance(power=40, type="Normal", name="Tackle", is_special=False)
    if type_item_override:
        return move
    if tier == 2:
        signature = data.get_legendary_signature_moves().get(species_id)
        if signature is not None:
            return MoveInstance(
                power=move.power,
                type=signature.type or move.type,
                name=signature.name,
                is_special=move.is_special,
            )
    return move


# ---------------------------------------------------------------------------
# Effective stats -- bundle.deobfuscated.js:55067-55124
# ---------------------------------------------------------------------------

_BASE_STAT_FIELDS = {"atk": "atk", "def": "defense", "speed": "speed", "special": "special"}


def _raw_base_stat(base_stats: BaseStats, stat: str) -> int:
    if stat == "spdef":
        value = base_stats.spdef if base_stats.spdef is not None else base_stats.special
    else:
        value = getattr(base_stats, _BASE_STAT_FIELDS[stat])
    return value if value is not None else 50


def get_effective_stat(
    combatant: Combatant,
    stat: str,
    items: Optional[Iterable[HeldItem]] = (),
    stages: Optional[dict] = None,
) -> int:
    """Port of `getEffectiveStat` (line 55067-55124). `stat` is one of
    "atk"/"def"/"speed"/"special"/"spdef". `items` is the COMBATANT'S OWN
    held item(s) -- always its own, never the opponent's (see calc_damage's
    call sites). `stages` is normally `combatant.stages`; pass it explicitly
    to match the JS call sites, which do the same.
    """
    base = _raw_base_stat(combatant.base_stats, stat)
    stat_buff = (combatant.stat_buffs or {}).get(stat, 0)
    effective = math.floor((base * combatant.level) / 50) + 5
    if stat_buff != 0:
        extra = 0.05
        if stat_buff > 0 and combatant.pos_stat_more:
            extra += combatant.pos_stat_more
        elif stat_buff < 0 and combatant.neg_stat_more:
            extra += combatant.neg_stat_more
        effective = max(1, math.floor(effective * max(0.1, 1 + extra * stat_buff)))
    if combatant.augment_pct:
        effective = max(1, math.floor(effective * (1 + combatant.augment_pct / 100)))
    if stat == "def":
        if has_item(items, "eviolite") and can_evolve(combatant.species_id):
            effective = math.floor(effective * 1.5)
        if has_item(items, "choice_band"):
            effective = math.floor(effective * 0.8)
    if stat == "spdef":
        if has_item(items, "eviolite") and can_evolve(combatant.species_id):
            effective = math.floor(effective * 1.5)
        if has_item(items, "assault_vest"):
            effective = math.floor(effective * 1.5)
    if stat == "speed" and has_item(items, "choice_scarf"):
        effective = math.floor(effective * 1.5)
    if stages is not None:
        stage_val = stages.get(stat)
        if stage_val:
            effective = math.floor(effective * stage_multiplier(stage_val))
    return max(1, effective)


# ---------------------------------------------------------------------------
# Damage formula -- bundle.deobfuscated.js:54923-55066
# ---------------------------------------------------------------------------


def calc_damage(
    attacker: Combatant,
    defender: Combatant,
    move: MoveInstance,
    attacker_items: Sequence[HeldItem] = (),
    defender_items: Sequence[HeldItem] = (),
    traits: Sequence[Trait] = (),
    side: str = "player",
    battle_config: Optional[BattleConfig] = None,
) -> DamageResult:
    """Port of `calcDamage` (line 54923-55066), transcribed in exact source
    order -- each modifier below is its own separate `math.floor()` call,
    matching the JS (docs/logic-notes.md 7.3, 7.9). `side` is which side is
    ATTACKING this exchange ("player" or "enemy"); `traits` is always the
    PLAYER's own collected trait list regardless of `side` -- several trait
    checks below are gated on `side` for exactly that reason (they only
    have meaning for the player's own Pokemon, whichever role it's playing
    in this exchange). See docs/logic-notes.md 7.9 for the six pre-formula
    trait modifiers ported here that an earlier pass of this research
    missed.
    """
    level = attacker.level
    uses_special = uses_special_attack(attacker.species_id, attacker.base_stats)

    atk_stat = "special" if uses_special else "atk"
    atk = get_effective_stat(attacker, atk_stat, attacker_items, attacker.stages)
    if not uses_special and attacker.burned:
        atk = math.floor(atk * 0.5)

    def_stat = "spdef" if uses_special else "def"
    if (
        side == "enemy"
        and uses_special
        and has_passive(traits, "ground_def_as_spdef")
        and "Ground" in (defender.types or ())
    ):
        def_stat = "def"
    de = get_effective_stat(defender, def_stat, defender_items, defender.stages)

    if (
        side == "enemy"
        and has_passive(traits, "rock_def_amp")
        and "Rock" in (defender.types or ())
        and (defender.stages.get(def_stat) or 0) > 0
    ):
        de = math.floor(de * 1.5)
    if side == "enemy" and has_passive(traits, "stat_amp"):
        stage = defender.stages.get(def_stat) or 0
        if stage > 0:
            de = math.floor(de * (stage_multiplier(stage * 1.3) / stage_multiplier(stage)))

    if side == "player":
        if not uses_special and has_passive(traits, "atk_band"):
            atk = math.floor(atk * 1.35)
        if uses_special and has_passive(traits, "spa_specs"):
            atk = math.floor(atk * 1.35)
    if side == "enemy":
        if not uses_special and has_passive(traits, "atk_band"):
            de = math.floor(de * 0.65)
        if uses_special and has_passive(traits, "spa_specs"):
            de = math.floor(de * 0.65)
    if side == "player" and has_passive(traits, "fire_amp") and "Fire" in (attacker.types or ()):
        atk_stage = attacker.stages.get(atk_stat) or 0
        if atk_stage > 0:
            atk = math.floor(atk * 1.5)
    if side == "player" and has_passive(traits, "stat_amp"):
        atk_stage = attacker.stages.get(atk_stat) or 0
        if atk_stage > 0:
            atk = math.floor(atk * (stage_multiplier(atk_stage * 1.3) / stage_multiplier(atk_stage)))

    if (
        "Normal" in (defender.types or ())
        and side == "enemy"
        and has_passive(traits, "normal_bulk")
    ):
        de += math.floor((defender.max_hp or 100) * 0.1)

    power = move.power or 40
    move_type = move.type or "Normal"
    base = math.floor((((2 * level) / 5 + 2) * power * atk) / de / 50 + 2)

    type_eff = 1.0 if move.typeless else data.get_type_effectiveness(move_type, defender.types or ("Normal",))
    if type_eff == 0 and smack_down_hits(attacker, move_type):
        type_eff = 1.0
    if not move.typeless and has_passive(traits, "neutralize"):
        type_eff = 1.0
    if attacker.gen3_ability == "scrappy" and 0 < type_eff < 1:
        type_eff = 1.0
    base = math.floor(base * type_eff)

    weather = battle_config.get_weather() if battle_config else None
    move_type_lower = move_type.lower()
    if weather == "rain" and move_type_lower == "water":
        base = math.floor(base * 1.5)
    if weather == "sun" and move_type_lower == "fire":
        base = math.floor(base * 1.5)
    if attacker.gen3_ability == "tinted_lens" and 0 < type_eff < 1:
        base = math.floor(base * 2)
    if attacker.types and any(t.lower() == move_type_lower for t in attacker.types):
        base = math.floor(base * 1.5)
    if get_type_boost_item(move_type, attacker_items):
        base = math.floor(base * 1.4)
    if has_item(attacker_items, "wide_lens"):
        base = math.floor(base * 1.2)
    if has_item(attacker_items, "metronome"):
        base = math.floor(base * 1.2)
    if uses_special:
        if has_item(attacker_items, "choice_specs"):
            base = math.floor(base * 1.3)
    elif has_item(attacker_items, "choice_band"):
        base = math.floor(base * 1.4)
    if has_item(attacker_items, "lagging_tail"):
        base = math.floor(base * 2)
    if has_item(attacker_items, "expert_belt") and type_eff >= 2:
        base = math.floor(base * 2)
    if has_item(defender_items, "red_card") and type_eff >= 2:
        base = math.floor(base * 0.5)

    crit_chance = 0.0625
    if has_item(attacker_items, "scope_lens"):
        crit_chance += 0.3
    if side == "player" and battle_config is not None and battle_config.dark_crit_floor:
        crit_chance += battle_config.dark_crit_floor
    if side == "player":
        if has_passive(traits, "crit_overflow"):
            crit_chance += 0.35
        if has_passive(traits, "crit_lifesteal"):
            crit_chance += 0.1
        if has_passive(traits, "crit_boost"):
            crit_chance += 0.1
        if has_passive(traits, "crit_flinch"):
            crit_chance += 0.1
        if has_passive(traits, "dark_lvlcrit") and "Dark" in (attacker.types or ()):
            crit_chance += level / 150
    if attacker.force_crit:
        crit_chance = max(crit_chance, 1.0)

    overflow_mult = 0.0
    if side == "player" and crit_chance > 1:
        dark_floor = battle_config.dark_crit_floor if battle_config else 0
        if has_passive(traits, "crit_overflow") or dark_floor > 1:
            overflow_mult = crit_chance - 1
        crit_chance = 1.0
    if attacker.gen3_ability == "super_luck":
        crit_chance *= 4
    if defender.gen3_ability == "shell_armor":
        crit_chance = 0.0

    is_crit = rng.rng() < crit_chance
    if is_crit:
        mult = 2.0
        if side == "player":
            mult += overflow_mult
        base = math.floor(base * mult)

    variance = 0.85 + rng.rng() * 0.15
    damage = 0 if type_eff == 0 else max(1, math.floor(base * variance))
    if defender.gen3_ability == "wonder_guard" and type_eff < 2 and not is_crit:
        damage = 0

    return DamageResult(damage=damage, type_eff=type_eff, move_type=move_type, crit=is_crit)


# ---------------------------------------------------------------------------
# Turn order -- bundle.deobfuscated.js:55481-55614 (docs/logic-notes.md 7.8)
# ---------------------------------------------------------------------------


def decide_turn_order(
    player: Combatant,
    enemy: Combatant,
    player_items: Sequence[HeldItem] = (),
    enemy_items: Sequence[HeldItem] = (),
    traits: Sequence[Trait] = (),
    trick_room: bool = False,
    *,
    player_speed: Optional[float] = None,
    enemy_speed: Optional[float] = None,
) -> str:
    """Port of the turn-order precedence chain inside `runBattle` (line
    55481-55614). Returns "player" or "enemy" -- whichever acts first this
    round. `trick_room` should already fold in both of the source's OR'd
    trick-room signals (see `BattleConfig.trick_room`'s docstring).

    `player_speed`/`enemy_speed`, if BOTH given, are used as-is instead of
    being recomputed here -- this is how `battle_loop.run_battle` threads
    ability-modified speed (`Gen3AbilityConfig.modify_speed`, e.g. Swift
    Swim/Chlorophyll/Mirror Coat) into the precedence chain: the source
    computes base speed, applies paralysis and `spdef_stage_speed`, THEN
    calls `modifySpeed` (bundle.deobfuscated.js:55481-55491), all BEFORE
    turn order is decided -- a caller that already ran that sequence should
    pass the final values here rather than let this function recompute a
    modify_speed-less base speed from scratch (a confirmed defect this
    parameter exists to fix, see CODEX.md issue 1). When either is omitted,
    both are computed internally exactly as before (paralysis +
    `spdef_stage_speed` only, no ability hook), preserving old callers.

    RNG-call-order note: `quick_claw`'s roll is evaluated via short-circuit
    (only actually calls `rng()` if the item is held, both sides,
    unconditionally, BEFORE the precedence chain runs) -- preserved here
    with the same `and`-short-circuit structure so the RNG stream is
    consumed in the same order/count as the source, not just producing the
    same win/lose outcome.
    """
    if player_speed is None or enemy_speed is None:
        player_speed = get_effective_stat(player, "speed", player_items, player.stages)
        enemy_speed = get_effective_stat(enemy, "speed", enemy_items, enemy.stages)
        if player.paralyzed:
            player_speed = math.floor(player_speed / 2)
        if enemy.paralyzed:
            enemy_speed = math.floor(enemy_speed / 2)
        if has_passive(traits, "spdef_stage_speed"):
            spdef_stage = player.stages.get("spdef") or 0
            if spdef_stage > 0:
                player_speed = math.floor(player_speed * (1 + 0.1 * spdef_stage))

    player_quick_claw = has_item(player_items, "quick_claw") and rng.rng() < 0.5
    enemy_quick_claw = has_item(enemy_items, "quick_claw") and rng.rng() < 0.5
    player_lagging_tail = has_item(player_items, "lagging_tail")
    enemy_lagging_tail = has_item(enemy_items, "lagging_tail")
    hp_priority = has_passive(traits, "hp_priority")

    if player_lagging_tail and not enemy_lagging_tail:
        return "enemy"
    if enemy_lagging_tail and not player_lagging_tail:
        return "player"
    if player_quick_claw and not enemy_quick_claw:
        return "player"
    if enemy_quick_claw and not player_quick_claw:
        return "enemy"
    if hp_priority:
        player_hp_pct = player.current_hp / max(1, player.max_hp)
        enemy_hp_pct = enemy.current_hp / max(1, enemy.max_hp)
        if player_hp_pct != enemy_hp_pct:
            return "player" if player_hp_pct > enemy_hp_pct else "enemy"
    if trick_room:
        return "player" if player_speed <= enemy_speed else "enemy"
    return "player" if player_speed >= enemy_speed else "enemy"


# ---------------------------------------------------------------------------
# Status effects -- docs/logic-notes.md section 8
# ---------------------------------------------------------------------------


def burn_tick_damage(max_hp: int) -> int:
    """Unconditional 10%-max-HP burn DoT, once per round
    (bundle.deobfuscated.js:56593-56606)."""
    return max(1, math.floor(max_hp * 0.1))


def poison_tick_damage(max_hp: int, stacks: int) -> int:
    """Uncapped stacking poison DoT (bundle.deobfuscated.js:56617-56622):
    `max(1, floor(maxHp*0.05*stacks))`. Deliberately does NOT include the
    `fragile_status` (x1.5) / `nonattack_amp` (x1.3) enemy-side trait
    multipliers from lines 56624-56629 -- those belong to the full
    trait-hook system, not this standalone formula; apply them at the call
    site once traits are wired in. Also not the same thing as the separate
    `poison_armor` passive (caps damage TAKEN FROM a poisoned enemy at 60%,
    lines ~62342-62347) -- that's an unrelated defensive mechanic, see
    docs/logic-notes.md section 8.
    """
    return max(1, math.floor(max_hp * 0.05 * stacks))


def resolve_pre_turn_status(combatant: Combatant) -> bool:
    """Port of the freeze/sleep pre-turn skip check inside `runBattle`
    (line 55670-55711). Returns True if the combatant's turn is skipped
    this round. Has a side effect: a successful sleep wake-up roll clears
    `combatant.status` -- check the return value AFTER calling this, not
    before.

    Freeze never randomly thaws in this engine (only a separate Ice-trait
    "shatter" mechanic can break it, not ported here); sleep has a flat 50%
    wake chance every turn (not mainline's 1-3 turn counter). Does not
    handle flinch (a one-shot per-turn flag, not a persistent `status`) or
    paralysis (turn-order-only, see `decide_turn_order` -- no full-
    paralysis/can't-act chance exists despite the elec_paralyze trait's own
    tooltip text, docs/logic-notes.md section 8).
    """
    if combatant.status == "freeze":
        return True
    if combatant.status == "sleep":
        if rng.rng() < 0.5:
            combatant.status = None
            return False
        return True
    return False


def apply_status(combatant: Combatant, status: str) -> bool:
    """Port of `applyStatus` (line 54911-54922) -- only ever called for
    "freeze" and "sleep" in the source; burn/paralysis are separate boolean
    flags applied via duplicated inline logic at ability-trigger sites, not
    through this helper (docs/logic-notes.md section 8). A status already
    set blocks any other from being applied. Returns whether it was applied.
    """
    if combatant.status:
        return False
    combatant.status = status
    return True


def apply_stage_change(combatant: Combatant, stat: str, delta: int) -> bool:
    """Port of `applyStageChange`'s state mutation (line 54878-54909) --
    the JS's battle-log push (a UI event, `type: "stat_change"`) is
    intentionally not replicated (see module docstring). Stat stages are
    capped at [-10, +10] normally; the upper cap becomes unbounded if
    `combatant.uncap_stages` is set (the `rand_start` trait). Returns False
    if a negative change was blocked entirely by the `hyper_cutter`
    ability; True otherwise (change applied, possibly clamped).
    """
    if delta < 0 and combatant.gen3_ability == "hyper_cutter":
        return False
    upper = math.inf if combatant.uncap_stages else 10
    current = combatant.stages.get(stat, 0)
    combatant.stages[stat] = max(-10, min(upper, current + delta))
    return True
