"""Ported data tables: Pokemon species, moves, type chart, items, evolutions,
fixed trainers, and map generation ranges.

This is a straight, near-verbatim port of the static tables described in
docs/logic-notes.md (section 5). The JSON files in pokelike/data/ were pulled
directly out of the deobfuscated game bundle by tools/extract-data/ -- not
hand-transcribed -- so field names/values should match the live tables
exactly as of the mirror's bundle. Regenerate them with:

    node tools/extract-data/extract-tables.js pokelike_forked/js/bundle.deobfuscated.js tools/extract-data/out
    node tools/extract-data/extract-trainer-tables.js pokelike_forked/js/bundle.deobfuscated.js tools/extract-data/out

...then re-copy the relevant files into pokelike/data/ (see that directory's
layout for where each JSON file lands). The second script (added 2026-07-31,
CODEX.md P0.9) covers `TRAINER_BATTLE_CONFIG` plus the Silver/Magma/Aqua
special-rival rosters -- see `tools/extract-data/README.md`'s own section
on it for why it needs a different (further-extended) safe-execution cutoff
than the first script.

Nothing in this module touches RNG, battle resolution, or the state machine
-- those belong in rng.py / battle.py / engine.py (Phase 2, not yet written).
This module only has to be correct as *data*, not as *logic*.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

_DATA_DIR = Path(__file__).parent / "data"


def _load_json(*relative_path: str):
    path = _DATA_DIR.joinpath(*relative_path)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Pokemon species (docs/logic-notes.md section 5, row 1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseStats:
    hp: int
    atk: int
    defense: int
    speed: int
    special: int
    # Some fixed-trainer rosters (gym leaders / elite four, all four
    # generations) omit spdef entirely -- a real quirk of the source data,
    # not a bug in this port (confirmed across all 27 Gen-1 gym leader/elite
    # four team members). This is intentional, not an oversight: JS's
    # getEffectiveStat (bundle.deobfuscated.js:55071-55080) falls back
    # `baseStats.spdef ?? baseStats.special ?? 50` for exactly this case --
    # it's Gen-1-style single-"Special"-stat data, and the engine already
    # knows how to treat it that way. Use effective_base_spdef() below
    # rather than reading .spdef directly when you need a concrete value.
    spdef: Optional[int] = None

    @classmethod
    def from_json(cls, d: dict) -> "BaseStats":
        return cls(
            hp=d["hp"],
            atk=d["atk"],
            defense=d["def"],
            speed=d["speed"],
            special=d["special"],
            spdef=d.get("spdef"),
        )


def effective_base_spdef(base_stats: BaseStats) -> int:
    """Port of getEffectiveStat's fallback for a missing spdef base stat
    (bundle.deobfuscated.js:55071-55080): `baseStats.spdef ?? baseStats.special ?? 50`.

    This is only the BASE-STAT-level fallback confirmed in Phase 1 -- level
    scaling, stat stages, and held-item modifiers (eviolite, assault_vest,
    etc.) are the rest of getEffectiveStat and belong in battle.py, not
    here. The JS's final `?? 50` is unreachable through this port since
    `special` is a required (non-Optional) field on BaseStats -- kept in
    this docstring for exactness, not because it can fire in practice.
    """
    return base_stats.spdef if base_stats.spdef is not None else base_stats.special


@dataclass(frozen=True)
class Pokemon:
    species_id: int
    name: str
    types: tuple[str, ...]
    base_stats: BaseStats
    base_experience: int
    sprite_url: str
    shiny_sprite_url: str
    growth_rate: str
    flavor_text: str


@lru_cache(maxsize=1)
def get_pokedex() -> dict[int, Pokemon]:
    """dex-id -> Pokemon, for all 721 species (Gen 1-6 through Volcanion)."""
    raw = _load_json("pokedex.json")
    result = {}
    for species_id_str, entry in raw.items():
        species_id = int(species_id_str)
        result[species_id] = Pokemon(
            species_id=species_id,
            name=entry["name"],
            types=tuple(entry["types"]),
            base_stats=BaseStats.from_json(entry["baseStats"]),
            base_experience=entry["base_experience"],
            sprite_url=entry["spriteUrl"],
            shiny_sprite_url=entry["shinySpriteUrl"],
            growth_rate=entry["growthRate"],
            flavor_text=entry["flavorText"],
        )
    return result


# Origin Forme Giratina's own base stats (Atk/Def and Special/SpDef swapped
# relative to the Altered Forme entry `get_pokedex()[0x1E7]` carries --
# HP/Speed unchanged, same 680 BST). `fetchPokemonById("giratina-origin")`
# (bundle.deobfuscated.js:48620-48719) is a LIVE-PokeAPI-only call this
# offline port has no network access to replay byte-for-byte at extraction
# time -- these are Origin Forme's well-established canonical stats (same
# published numbers pokeapi.co/api/v2/pokemon/10007 and Bulbapedia both
# carry), flagged as a residual verification risk in PLAN.md rather than
# silently presented as a byte-identical extraction.
_GIRATINA_ORIGIN_BASE_STATS = BaseStats(hp=150, atk=120, defense=100, speed=90, special=120, spdef=100)


@lru_cache(maxsize=1)
def get_giratina_origin_form() -> Pokemon:
    """Port of `fetchPokemonById("giratina-origin")`'s actual return shape
    (bundle.deobfuscated.js:48620-48719). Two source details make this NOT
    a simple "different dex id" case:

    - `POKEMON_FORM_SLUGS["giratina-origin"]` (`pP`, bundle.deobfuscated.js:
      48016) is `0x1e7` -- the SAME numeric id as base/Altered Giratina, so
      the `id` field `fetchPokemonById` returns for this string form is 487,
      not a distinct dex slot (`B2d ? POKEMON_FORM_SLUGS[B] : B2P["id"]`,
      bundle.deobfuscated.js:48698-48700). This matches how the live game
      treats Origin/Altered as the same species for dex/achievement
      purposes -- `species_id` here is deliberately `0x1E7`, identical to
      `get_pokedex()[0x1E7]`.
    - `name`/`types`/`baseStats` come from the ACTUAL PokeAPI `pokemon/
      giratina-origin` response, not the id-487 static dex entry --
      `formatFormName("giratina-origin")` (bundle.deobfuscated.js:48609-
      48619) produces the display name "Giratina (Origin)" (matching
      `DISTORTION_LEGENDARY_POOL`'s own hardcoded `name`, bundle.
      deobfuscated.js:76393-76396), and Origin Forme's stats/types are its
      own (types unchanged: Ghost/Dragon; base_stats swapped, see
      `_GIRATINA_ORIGIN_BASE_STATS`).

    Silently reusing `get_pokedex()[0x1E7]` wholesale for this encounter
    (same numeric id, but ALSO the Altered Forme's stats) is the exact
    source-vs-port discrepancy this function exists to close -- callers
    that need a "giratina-origin" combatant must read stats/name from HERE,
    not from a bare `get_pokedex()` lookup keyed by the shared id."""
    base = get_pokedex()[0x1E7]
    return Pokemon(
        species_id=base.species_id,
        name="Giratina (Origin)",
        types=base.types,
        base_stats=_GIRATINA_ORIGIN_BASE_STATS,
        base_experience=base.base_experience,
        sprite_url="img/sprites/pokemon/10007.png",
        shiny_sprite_url="img/sprites/pokemon/shiny/10007.png",
        growth_rate=base.growth_rate,
        flavor_text=base.flavor_text,
    )


# ---------------------------------------------------------------------------
# Moves (docs/logic-notes.md section 5, row 2 + section 7.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """A single move. NOTE ON `category`: this is NOT a property of the move
    in the source data -- MOVE_POOL only nests moves under "physical"/
    "special" buckets per type. A Pokemon's *entire* moveset is uniformly
    physical or uniformly special, decided once by usesSpecialAttack()
    (base Special >= base Atk, ties go special; two hardcoded species-id
    exceptions). See docs/logic-notes.md section 7.1 -- do not reintroduce
    a per-move category switch when this feeds into battle.py.
    """

    name: str
    power: int
    desc: str
    type: str
    category: str  # "physical" | "special", injected from which MOVE_POOL bucket this came from


@dataclass(frozen=True)
class LegendarySignatureMove:
    name: str
    # Optional: Arceus's entry (dex 493, "Judgment") has no type field in the
    # source -- presumably because its real type is plate-dependent rather
    # than fixed. Confirmed the only such case in Phase 1; don't assume a
    # default type for it.
    type: Optional[str] = None


@lru_cache(maxsize=1)
def get_move_pool() -> dict[str, dict[str, tuple[Move, ...]]]:
    """type name -> {"physical": (Move, ...), "special": (Move, ...)}."""
    raw = _load_json("moves.json")
    result = {}
    for type_name, buckets in raw.items():
        result[type_name] = {
            category: tuple(
                Move(name=m["name"], power=m["power"], desc=m["desc"], type=type_name, category=category)
                for m in moves
            )
            for category, moves in buckets.items()
        }
    return result


@lru_cache(maxsize=1)
def get_legendary_signature_moves() -> dict[int, LegendarySignatureMove]:
    """dex-id -> a signature move name/type override for that legendary.

    Only overrides the move's flavor (name/type); it does not change the
    power/category system above -- see docs/logic-notes.md section 5.
    """
    raw = _load_json("legendary_signature_moves.json")
    return {
        int(species_id): LegendarySignatureMove(name=entry["name"], type=entry.get("type"))
        for species_id, entry in raw.items()
    }


# ---------------------------------------------------------------------------
# Type chart (docs/logic-notes.md section 5, row 3 + section 7.4)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_type_chart() -> dict[str, dict[str, float]]:
    """attacker type -> {defender type -> multiplier}."""
    return _load_json("type_chart.json")


@lru_cache(maxsize=1)
def get_type_ids() -> dict[str, int]:
    """type name -> numeric id (separate from the effectiveness chart)."""
    return _load_json("type_ids.json")


def get_type_effectiveness(
    attacker_type: str,
    defender_types: Sequence[str],
    type_chart: Optional[dict[str, dict[str, float]]] = None,
    inverse: bool = False,
) -> float:
    """Faithful port of getTypeEffectiveness (bundle.deobfuscated.js:41440).

    Multiplies across every one of the defender's types; a pairing missing
    from the chart is implicitly neutral (contributes 1x, not an error).
    When inverse=True (the "Inverse" challenge mode's CHALLENGE_INVERSE
    flag), the FINAL accumulated multiplier is remapped once:
    1 -> 1, <1 -> 2, else -> 0.5 -- note this means a 0x immunity (which is
    < 1) becomes a 2x super-effective hit under Inverse mode, not neutral.
    """
    if type_chart is None:
        type_chart = get_type_chart()
    multiplier = 1.0
    atk_key = attacker_type.capitalize()
    for defender_type in defender_types:
        row = type_chart.get(atk_key)
        if row is None:
            continue
        value = row.get(defender_type.capitalize())
        if value is not None:
            multiplier *= value
    if inverse:
        if multiplier == 1:
            return 1.0
        return 2.0 if multiplier < 1 else 0.5
    return multiplier


# ---------------------------------------------------------------------------
# Items (docs/logic-notes.md section 5, row 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    desc: str
    icon: str
    usable: bool = False
    hook: Optional[str] = None
    min_map: Optional[int] = None
    icon_url: Optional[str] = None
    tier: Optional[int] = None  # Mega Stones only -- an UNLOCK requirement (MVP wins needed), not a drop weight. See logic-notes.md section 5.
    gen2_only: bool = False  # CODEX.md issue 12 -- Loaded Dice's `"gen2Only": true` was silently discarded

    @classmethod
    def from_json(cls, d: dict, *, usable: bool) -> "Item":
        return cls(
            id=d["id"],
            name=d["name"],
            desc=d["desc"],
            icon=d.get("icon", ""),
            usable=usable or d.get("usable", False),
            hook=d.get("hook"),
            min_map=d.get("minMap"),
            icon_url=d.get("iconUrl"),
            tier=d.get("tier"),
            gen2_only=d.get("gen2Only", False),
        )


@lru_cache(maxsize=1)
def get_usable_items() -> tuple[Item, ...]:
    """Consumable items (Rare Candy, Sacred Ash, ...)."""
    raw = _load_json("items_usable.json")
    return tuple(Item.from_json(d, usable=True) for d in raw)


@lru_cache(maxsize=1)
def get_passive_items() -> tuple[Item, ...]:
    """Passive/held/global-effect items (type-boost items, Rocky Helmet,
    Choice Band, ...) -- NOT the same table as get_usable_items(). This is
    an exact, order-preserving port of `ITEM_POOL` (bundle.deobfuscated.js:
    46461-46498, confirmed by direct id-for-id/order comparison) -- the pool
    both of `runBattleScreen`'s Pickup reward paths draw from (CODEX P0.7).
    No weight/drop-rate field exists on this table; loot selection logic
    lives elsewhere and was not traced in Phase 1.

    Mega Stones are a SEPARATE source table (`MEGA_STONES`,
    bundle.deobfuscated.js:48573-48602) with a materially different shape
    (species/megaStats/megaTypes/megaName, no `hook`/`minMap`) and are never
    members of `ITEM_POOL` -- see `get_mega_stones()` below, not this table.
    """
    raw = _load_json("items_passive.json")
    return tuple(Item.from_json(d, usable=False) for d in raw)


@lru_cache(maxsize=1)
def get_type_item_map() -> dict[str, str]:
    """type name -> held-item id that boosts that type's STAB moves (e.g.
    Fire -> charcoal), mirroring TYPE_ITEM_MAP in the source.
    """
    return _load_json("type_item_map.json")


@dataclass(frozen=True)
class MegaStone:
    """Port of one `MEGA_STONES` entry (bundle.deobfuscated.js:48573-48602,
    28 entries total, with every battle/runtime field plus `formId` retained
    exactly -- CODEX P0.8). `species` is the
    dex id the holder must currently be for the stone to activate
    (`syncMegaForm`'s `megaSpecies === speciesId` check); `tier`/`starter`
    are ACQUISITION metadata (MVP-win-count unlock threshold / "one of the
    three starter-line stones") that this port does not model any
    equip/unlock flow for -- see `engine._apply_mega_evolution`'s docstring
    for the acquisition-scope limitation. `form_id` is retained so the
    source's `img/sprites/pokemon/{formId}.png` metadata can be audited,
    although no `Combatant` field in this engine tracks or applies a sprite
    path.
    """

    id: str
    name: str
    species: int
    form_id: int
    mega_name: str
    mega_types: tuple[str, ...]
    mega_stats: BaseStats
    tier: int
    starter: bool = False

    @classmethod
    def from_json(cls, d: dict) -> "MegaStone":
        return cls(
            id=d["id"],
            name=d["name"],
            species=d["species"],
            form_id=d["formId"],
            mega_name=d["megaName"],
            mega_types=tuple(d["megaTypes"]),
            mega_stats=BaseStats.from_json(d["megaStats"]),
            tier=d["tier"],
            starter=d.get("starter", False),
        )

    @property
    def mega_sprite(self) -> str:
        """Exact sprite path produced by source `makeMegaStoneItem`."""
        return f"img/sprites/pokemon/{self.form_id}.png"


@lru_cache(maxsize=1)
def get_mega_stones() -> tuple[MegaStone, ...]:
    """All 28 Mega Stones (bundle.deobfuscated.js:48573-48602)."""
    raw = _load_json("mega_stones.json")
    return tuple(MegaStone.from_json(d) for d in raw)


@lru_cache(maxsize=1)
def get_mega_stone_by_species() -> dict[int, MegaStone]:
    """dex id -> its Mega Stone, mirroring `MEGA_STONE_BY_SPECIES`
    (bundle.deobfuscated.js:48606-48608)."""
    return {stone.species: stone for stone in get_mega_stones()}


# ---------------------------------------------------------------------------
# Evolutions (docs/logic-notes.md section 5, row 6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evolution:
    into: int
    name: str
    level: int
    types: Optional[tuple[str, ...]] = None  # only populated for branching evolutions (e.g. Eeveelutions)


@lru_cache(maxsize=1)
def get_evolutions() -> dict[int, Evolution]:
    """dex-id -> single linear evolution. Only `level` gates evolution here
    -- no item/trade/friendship fields exist in this table (confirmed by
    exhaustive grep, see docs/logic-notes.md section 5 and section 9 item 2).
    """
    raw = _load_json("evolutions.json")
    return {
        int(species_id): Evolution(into=entry["into"], name=entry["name"], level=entry["level"])
        for species_id, entry in raw.items()
    }


@lru_cache(maxsize=1)
def get_branching_evolutions() -> dict[int, tuple[Evolution, ...]]:
    """dex-id -> multiple possible evolutions (e.g. Eevee, dex 133 -> 8 branches)."""
    raw = _load_json("branching_evolutions.json")
    return {
        int(species_id): tuple(
            Evolution(
                into=e["into"],
                name=e["name"],
                level=e["level"],
                types=tuple(e["types"]) if "types" in e else None,
            )
            for e in entries
        )
        for species_id, entries in raw.items()
    }


# ---------------------------------------------------------------------------
# Fixed trainers (docs/logic-notes.md section 5, row 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainerPokemon:
    """A hand-authored fixed trainer's team member.

    NOTE: baseStats on these entries consistently omits spdef (see
    BaseStats docstring) -- confirmed across all 27 Gen-1 gym leader/elite
    four team members in Phase 1. This is intentional: use
    effective_base_spdef(mon.base_stats) rather than mon.base_stats.spdef
    directly, since the source data's own getEffectiveStat treats a missing
    spdef as equal to `special` (Gen-1-style single stat), not zero.
    """

    species_id: int
    name: str
    types: tuple[str, ...]
    base_stats: BaseStats
    level: int
    held_item: Optional[dict] = None  # {id, name, icon} -- no separate held-item lookup table exists for these


@dataclass(frozen=True)
class Trainer:
    """A hand-authored fixed trainer. Gym leaders and Elite Four members
    have DIFFERENT field shapes in the source (confirmed across all four
    generations): gym leaders have `badge`/`moveTier` and no `title`; Elite
    Four members have `title` ("Elite Four") and no `badge`/`moveTier`.
    Both are modeled here with the union of fields, each side leaving its
    inapplicable fields as None.
    """

    name: str
    type: str
    team: tuple[TrainerPokemon, ...]
    badge: Optional[str] = None  # gym leaders only
    move_tier: Optional[int] = None  # gym leaders only
    title: Optional[str] = None  # Elite Four members only (e.g. "Elite Four")


def _trainer_pokemon_from_json(m: dict) -> TrainerPokemon:
    return TrainerPokemon(
        species_id=m["speciesId"],
        name=m["name"],
        types=tuple(m["types"]),
        base_stats=BaseStats.from_json(m["baseStats"]),
        level=m["level"],
        held_item=m.get("heldItem"),
    )


def _load_trainers(filename: str) -> tuple[Trainer, ...]:
    raw = _load_json("trainers", filename)
    trainers = []
    for entry in raw:
        team = tuple(_trainer_pokemon_from_json(m) for m in entry["team"])
        trainers.append(
            Trainer(
                name=entry["name"],
                type=entry["type"],
                team=team,
                badge=entry.get("badge"),
                move_tier=entry.get("moveTier"),
                title=entry.get("title"),
            )
        )
    return tuple(trainers)


@lru_cache(maxsize=None)
def get_gym_leaders(generation: int = 1) -> tuple[Trainer, ...]:
    """generation in {1, 2, 3, 4} -> that generation's fixed gym leader roster."""
    return _load_trainers(f"gen{generation}_gym_leaders.json")


@lru_cache(maxsize=None)
def get_elite_four(generation: int = 1) -> tuple[Trainer, ...]:
    """generation in {1, 2, 3, 4} -> that generation's fixed Elite Four roster."""
    return _load_trainers(f"gen{generation}_elite4.json")


# ---------------------------------------------------------------------------
# Procedural mid-map trainer archetypes (docs/logic-notes-nodes.md section 3,
# bundle.deobfuscated.js:79903-80189 TRAINER_BATTLE_CONFIG, 53711-53773 the
# generation-gated archetype-key lists `generateMap`'s trainerSprite hash
# picks from) and the fixed Gen2 Silver rival / Gen3 Magma-Aqua rosters
# (docs/logic-notes-nodes.md section 11, bundle.deobfuscated.js:45447-46103
# SILVER_ENCOUNTERS/SILVER_STARTER_LINES/MAGMA_ENCOUNTERS/AQUA_ENCOUNTERS).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainerArchetype:
    """One `TRAINER_BATTLE_CONFIG[sprite]` entry -- the per-trainer-sprite
    species pool `doTrainerNode` draws its procedural mid-map roster from.

    `pool` (the Gen1/Story-default field) is `None` for archetypes that only
    exist from a later generation onward -- e.g. "aceTrainer"/"oldGuy" have
    an explicit `pool: null` in the source (bundle.deobfuscated.js:80109,
    80124) despite having gen2/3/4 pools, and several Gen4-only archetypes
    ("artist", "youngster", ...) never had a `pool` key at all. `doTrainerNode`
    falls back to the ordinary wild catch-choices pool in that case
    (bundle.deobfuscated.js:80289-80310) -- not a gap in this table, a real
    source behavior, replicated by `engine._visit_trainer`'s own fallback.
    """

    name: str
    sprite: str
    pool: Optional[tuple[int, ...]] = None
    gen2_pool: Optional[tuple[int, ...]] = None
    gen3_pool: Optional[tuple[int, ...]] = None
    gen4_pool: Optional[tuple[int, ...]] = None


@lru_cache(maxsize=1)
def get_trainer_battle_config() -> dict[str, TrainerArchetype]:
    """`TRAINER_BATTLE_CONFIG` keyed by trainer-sprite archetype id (e.g.
    "aceTrainer", "bugCatcher", ...) -- 34 entries, source order preserved
    (dict insertion order, matching JS object key order)."""
    raw = _load_json("trainers", "trainer_battle_config.json")
    return {
        key: TrainerArchetype(
            name=entry["name"],
            sprite=entry["sprite"],
            pool=tuple(entry["pool"]) if entry.get("pool") is not None else None,
            gen2_pool=tuple(entry["gen2Pool"]) if entry.get("gen2Pool") is not None else None,
            gen3_pool=tuple(entry["gen3Pool"]) if entry.get("gen3Pool") is not None else None,
            gen4_pool=tuple(entry["gen4Pool"]) if entry.get("gen4Pool") is not None else None,
        )
        for key, entry in raw.items()
    }


@lru_cache(maxsize=1)
def get_trainer_sprite_keys() -> tuple[str, ...]:
    """`TRAINER_SPRITE_KEYS` (bundle.deobfuscated.js:53711-53727) -- the
    Gen1/Story candidate archetype-id list `generateMap`'s trainerSprite hash
    picks from (before the aceTrainer/policeman map-index exclusion and the
    GEN1_ONLY/GEN2_ONLY filtering `map_gen._trainer_sprite_candidates`
    applies)."""
    return tuple(_load_json("map_config", "trainer_sprite_keys.json"))


@lru_cache(maxsize=1)
def get_gen1_only_trainer_keys() -> frozenset[str]:
    """`GEN1_ONLY_TRAINER_KEYS` (bundle.deobfuscated.js:53736) -- archetype
    ids excluded from the candidate list when `gen2Mode` is active."""
    return frozenset(_load_json("map_config", "gen1_only_trainer_keys.json"))


@lru_cache(maxsize=1)
def get_gen2_only_trainer_keys() -> frozenset[str]:
    """`GEN2_ONLY_TRAINER_KEYS` (bundle.deobfuscated.js:53728-53735) --
    archetype ids excluded from the candidate list unless `gen2Mode` is
    active."""
    return frozenset(_load_json("map_config", "gen2_only_trainer_keys.json"))


@lru_cache(maxsize=1)
def get_gen3_trainer_keys() -> tuple[str, ...]:
    """`GEN3_TRAINER_KEYS` (bundle.deobfuscated.js:53740-53754) -- the
    candidate archetype-id list used in place of `TRAINER_SPRITE_KEYS` when
    `gen3Mode` is active."""
    return tuple(_load_json("map_config", "gen3_trainer_keys.json"))


@lru_cache(maxsize=1)
def get_gen4_trainer_keys() -> tuple[str, ...]:
    """`GEN4_TRAINER_KEYS` (bundle.deobfuscated.js:53756-53773) -- the
    candidate archetype-id list used in place of `TRAINER_SPRITE_KEYS` when
    `gen4Mode` is active (unconditionally, no map-index/gen1-2-only
    exclusions applied in that branch)."""
    return tuple(_load_json("map_config", "gen4_trainer_keys.json"))


@dataclass(frozen=True)
class SilverEncounter:
    """One `SILVER_ENCOUNTERS[stageIndex]` entry (bundle.deobfuscated.js:
    45447-45737) -- a fixed Silver-rival roster. No `name` field in the
    source (unlike Magma/Aqua) -- Silver's battle title is a hardcoded
    translation string (`t("battle.silverWants")`,
    bundle.deobfuscated.js:77930), not data-driven."""

    team: tuple[TrainerPokemon, ...]


@lru_cache(maxsize=1)
def get_silver_encounters() -> tuple[SilverEncounter, ...]:
    """`SILVER_ENCOUNTERS` -- 4 stages (indices 0-3), source order preserved.
    `doSilverNode` (bundle.deobfuscated.js:77900-77906) picks the stage index
    via a fixed `{1:0, 3:1, 5:2, 7:3}` map-index lookup, falling back to
    `state.silverBeaten` (clamped to this tuple's last index) for every other
    map -- see `engine._silver_encounter_index`."""
    raw = _load_json("trainers", "silver_encounters.json")
    return tuple(SilverEncounter(team=tuple(_trainer_pokemon_from_json(m) for m in entry["team"])) for entry in raw)


@dataclass(frozen=True)
class StarterLineSpecies:
    """One `SILVER_STARTER_LINES[starterSpeciesId]` entry -- a bare species
    reference (no level/held item: `doSilverNode` resolves the level from
    the fixed encounter's OWN final-slot level instead,
    bundle.deobfuscated.js:77913-77925)."""

    species_id: int
    name: str
    types: tuple[str, ...]
    base_stats: BaseStats


@lru_cache(maxsize=1)
def get_silver_starter_lines() -> dict[int, tuple[StarterLineSpecies, ...]]:
    """`SILVER_STARTER_LINES` (bundle.deobfuscated.js:45734-45737) -- keyed
    by the PLAYER's Johto starter dex id, mapping to the type-counter
    evolution line Silver's own starter mirrors (mainline Gold/Silver/
    Crystal's rival mechanic: Chikorita(152)->Cyndaquil line,
    Cyndaquil(155)->Totodile line, Totodile(158)->Chikorita line -- verified
    directly against the extracted base stats/types, not assumed from
    mainline convention)."""
    raw = _load_json("trainers", "silver_starter_lines.json")
    return {
        int(starter_id): tuple(
            StarterLineSpecies(
                species_id=m["speciesId"],
                name=m["name"],
                types=tuple(m["types"]),
                base_stats=BaseStats.from_json(m["baseStats"]),
            )
            for m in entries
        )
        for starter_id, entries in raw.items()
    }


@dataclass(frozen=True)
class AdminEncounter:
    """One `MAGMA_ENCOUNTERS`/`AQUA_ENCOUNTERS` entry
    (bundle.deobfuscated.js:45820-46103) -- keyed by map index (2, 5, 7)."""

    name: str
    sprite: str
    team: tuple[TrainerPokemon, ...]


def _load_admin_encounters(filename: str) -> dict[int, AdminEncounter]:
    raw = _load_json("trainers", filename)
    return {
        int(map_index): AdminEncounter(
            name=entry["name"],
            sprite=entry["sprite"],
            team=tuple(_trainer_pokemon_from_json(m) for m in entry["team"]),
        )
        for map_index, entry in raw.items()
    }


@lru_cache(maxsize=1)
def get_magma_encounters() -> dict[int, AdminEncounter]:
    """`MAGMA_ENCOUNTERS` (bundle.deobfuscated.js:45919-45920) -- keyed by
    map index; `doAdminNode` falls back to index 2 for any other map
    (bundle.deobfuscated.js:77964)."""
    return _load_admin_encounters("magma_encounters.json")


@lru_cache(maxsize=1)
def get_aqua_encounters() -> dict[int, AdminEncounter]:
    """`AQUA_ENCOUNTERS` (bundle.deobfuscated.js:46102-46103) -- keyed by
    map index; `doAdminNode` falls back to index 2 for any other map
    (bundle.deobfuscated.js:77964)."""
    return _load_admin_encounters("aqua_encounters.json")


# ---------------------------------------------------------------------------
# Special submaps -- Underground/Distortion World (docs/logic-notes-submaps.md,
# bundle.deobfuscated.js:53508-53632, 76247-76837 `generateSubMap`/
# `rollSubMapBoss`/`rollUndergroundTrainers`/`pickSubMapRewards`/
# `distortionLegendary`). Gen4/Sinnoh-only -- `generate_map` only ever places
# UNDERGROUND/DISTORTION node types when `gen4_mode=True` (map_gen.py), so
# every table here is reached exclusively from that mode.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubMapBossMember:
    """One `SUBMAP_BOSSES[kind]["teams"][i]` entry -- a bare species id plus a
    level OFFSET (added to `subMapBaseLevel(...)`, not a fixed level like the
    hand-authored gym/Elite-Four/Silver/Magma/Aqua rosters). No name/types/
    base_stats baked in -- `_roll_sub_map_boss`/`engine._visit_sub_map_boss`
    resolve those from the ordinary Pokedex, same as any wild/trainer
    encounter (`engine._make_wild_combatant`)."""

    species_id: int
    level_offset: int


@dataclass(frozen=True)
class SubMapBoss:
    """One `SUBMAP_BOSSES[kind]` entry (bundle.deobfuscated.js:76265-76302) --
    the fixed named trainer (Ruin Maniac for "underground", Cyrus for
    "distortion") whose TEAM is randomly picked (one of 3 fixed 3-member
    teams) per submap generation."""

    name: str
    sprite: str
    teams: tuple[tuple[SubMapBossMember, ...], ...]


@lru_cache(maxsize=1)
def get_submap_bosses() -> dict[str, SubMapBoss]:
    """`SUBMAP_BOSSES` (bundle.deobfuscated.js:76301-76302) -- keyed by
    submap kind ("underground"/"distortion"). `rollSubMapBoss` falls back to
    `SUBMAP_BOSSES["underground"]` for any OTHER kind string
    (bundle.deobfuscated.js:76456) -- defensive in the source itself, since
    `generateSubMap`'s own two call sites only ever pass "underground" or
    "distortion" (see `docs/logic-notes-submaps.md` section 1)."""
    raw = _load_json("submaps", "submap_bosses.json")
    return {
        kind: SubMapBoss(
            name=entry["name"],
            sprite=entry["sprite"],
            teams=tuple(
                tuple(SubMapBossMember(species_id=m["id"], level_offset=m["off"]) for m in team)
                for team in entry["teams"]
            ),
        )
        for kind, entry in raw.items()
    }


@dataclass(frozen=True)
class SubMapReward:
    """One `SUBMAP_REWARDS[i]` entry (bundle.deobfuscated.js:76303-76377) --
    a reward a submap's REWARD-type node can be baked with at generation
    time. `min_team` is only set on "sacrifice" (requires >=2 team members to
    be offered at all, bundle.deobfuscated.js:76309); `kinds` restricts which
    submap kind(s) can roll this reward ("fossil" is underground-only,
    "giratina"/"dialga"/"palkia" are distortion-only, everything else is
    shared)."""

    id: str
    label: str
    desc: str
    sprite: str
    kinds: tuple[str, ...]
    min_team: Optional[int] = None


@lru_cache(maxsize=1)
def get_submap_rewards() -> tuple[SubMapReward, ...]:
    """`SUBMAP_REWARDS` -- 12 entries, source order preserved (matches
    `pickSubMapRewards`'s own filter-then-shuffle order dependency)."""
    raw = _load_json("submaps", "submap_rewards.json")
    return tuple(
        SubMapReward(
            id=entry["id"],
            label=entry["label"],
            desc=entry["desc"],
            sprite=entry["sprite"],
            kinds=tuple(entry["kinds"]),
            min_team=entry.get("minTeam"),
        )
        for entry in raw
    )


@lru_cache(maxsize=1)
def get_submap_reward_by_id() -> dict[str, SubMapReward]:
    """Port of `submapReward(id)`'s lookup table (bundle.deobfuscated.js:
    76378-76381: `SUBMAP_REWARDS.find(o => o.id === id) || null`)."""
    return {reward.id: reward for reward in get_submap_rewards()}


@dataclass(frozen=True)
class DistortionLegendaryEntry:
    """One `DISTORTION_LEGENDARY_POOL[i]` entry (bundle.deobfuscated.js:
    76382-76398) -- the WILD boss encountered (and the reward it unlocks) on
    a player's second-ever Distortion World visit. `boss_id` is an `int`
    dex id for dialga/palkia, but a STRING ("giratina-origin") for
    giratina -- a live-PokeAPI-only alternate-forme lookup
    (`fetchPokemonById`, bundle.deobfuscated.js:48620-48719 branches on
    `typeof id=="string"`). Unlike every OTHER alternate-form code path
    (CLAUDE.md's "Open points" item 2, still out of scope), this one IS
    reachable from the ordinary Story/Nuzlocke Distortion World encounter
    loop, so it's in scope -- `engine._build_sub_map_boss_team` special-
    cases this exact string id and builds the combatant from
    `data.get_giratina_origin_form()`, not a bare numeric-id pokedex
    lookup (see that function's own docstring)."""

    reward: str
    boss_id: object  # int | str -- see docstring
    sprite: str
    name: str


@lru_cache(maxsize=1)
def get_distortion_legendary_pool() -> tuple[DistortionLegendaryEntry, ...]:
    """`DISTORTION_LEGENDARY_POOL` -- 3 entries (dialga, palkia, giratina),
    source order preserved (irrelevant to the uniform-random pick
    `distortionLegendary()` makes, but kept for citation fidelity)."""
    raw = _load_json("submaps", "distortion_legendary_pool.json")
    return tuple(DistortionLegendaryEntry(reward=e["reward"], boss_id=e["bossId"], sprite=e["sprite"], name=e["name"]) for e in raw)


@lru_cache(maxsize=1)
def get_distortion_legend_rewards() -> frozenset[str]:
    """`DISTORTION_LEGEND_REWARDS` (bundle.deobfuscated.js:76397) -- the
    reward ids `pickSubMapRewards` excludes from its ordinary random pool
    (they're only ever assigned directly by `generateSubMap` itself when a
    distortion-legendary encounter is rolled, never handed out as a "normal"
    random submap reward)."""
    return frozenset(_load_json("submaps", "distortion_legend_rewards.json"))


@lru_cache(maxsize=1)
def get_underground_trainer_keys() -> tuple[str, ...]:
    """`UNDERGROUND_TRAINER_KEYS` (bundle.deobfuscated.js:76551-76561) -- the
    candidate archetype-id list `rollUndergroundTrainers` uses in place of
    `GEN4_TRAINER_KEYS` when NEITHER `gen4Mode` NOR `challengeGen4` is active
    (bundle.deobfuscated.js:76587-76621). **Dead in this port's actual
    reachable scope**: `map_gen.generate_map` only ever places an
    UNDERGROUND-type node when `gen4_mode=True` (see that module's own Gen4
    node-placement block), so `map_gen._roll_underground_trainers` always
    takes the `GEN4_TRAINER_KEYS` branch in practice. Ported for citation
    completeness, not wired into that branch choice."""
    return tuple(_load_json("submaps", "underground_trainer_keys.json"))


# ---------------------------------------------------------------------------
# Map generation ranges (docs/logic-notes.md section 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MapRange:
    min: int
    max: int


@lru_cache(maxsize=None)
def get_map_bst_ranges(generation: int = 1) -> tuple[MapRange, ...]:
    """Per-map-depth wild-encounter BST (base-stat-total) windows, one
    generation's variant (MAP_BST_RANGES / GEN2_MAP_BST_RANGES / ...).
    """
    raw = _load_json("map_config", f"gen{generation}_bst_ranges.json")
    return tuple(MapRange(min=r["min"], max=r["max"]) for r in raw)


@lru_cache(maxsize=None)
def get_map_level_ranges(generation: int = 1) -> tuple[MapRange, ...]:
    """Per-map-depth wild-encounter level windows (MAP_LEVEL_RANGES / ...).

    Source JSON is [[min, max], ...] pairs rather than {min, max} objects
    (a genuine shape difference from the BST ranges table) -- normalized to
    the same MapRange type here for a consistent Python API.
    """
    raw = _load_json("map_config", f"gen{generation}_level_ranges.json")
    return tuple(MapRange(min=pair[0], max=pair[1]) for pair in raw)


@dataclass(frozen=True)
class FallbackSpeciesPool:
    """The static six-tier BST-bucketed species table (`Sl`/`GEN1_BST_APPROX`
    in the source) that `getBstBucket()` reads directly. Despite its JS name
    suggesting a fallback, this is NOT merely used when the live PokeAPI
    fetch fails -- tracing the actual call graph (see docs/logic-notes.md
    section 4's correction) shows every wild-encounter/catch-choice code
    path (`doBattleNode`, `getCatchChoices`) uses this table exclusively;
    the live `fetchSpeciesList()` result is awaited but never read
    (dead code). This is the sole, faithful source for wild-encounter
    species selection, not an approximation of something more "live".
    """

    low: tuple[int, ...]
    mid_low: tuple[int, ...]
    mid: tuple[int, ...]
    mid_high: tuple[int, ...]
    high: tuple[int, ...]
    very_high: tuple[int, ...]


@lru_cache(maxsize=1)
def get_fallback_species_pool() -> FallbackSpeciesPool:
    raw = _load_json("fallback_species_pool.json")
    return FallbackSpeciesPool(
        low=tuple(raw["low"]),
        mid_low=tuple(raw["midLow"]),
        mid=tuple(raw["mid"]),
        mid_high=tuple(raw["midHigh"]),
        high=tuple(raw["high"]),
        very_high=tuple(raw["veryHigh"]),
    )


# ---------------------------------------------------------------------------
# Wild-encounter eligibility & legendary pools (docs/logic-notes.md section 9,
# item 7 -- small tables found in a post-implementation review, all confirmed
# real/load-bearing in getCatchChoices/getRandomLegendary but not previously
# ported). None of these gate a formula -- they're all just id sets/lists
# consumed as filters, matching the pattern of everything else in this file.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_never_wild_ids() -> frozenset[int]:
    """Species dex-ids permanently excluded from every wild/catch pool
    (NEVER_WILD_IDS, bundle.deobfuscated.js:48895). A one-entry set in the
    current data -- still a real filter, not dead code (checked directly in
    getCatchChoices, line 49104, and getCatchChoicesForType, line 49164).
    """
    return frozenset(_load_json("never_wild_ids.json"))


@lru_cache(maxsize=1)
def get_legendary_ids() -> frozenset[int]:
    """All legendary/mythical species dex-ids (LEGENDARY_IDS,
    bundle.deobfuscated.js:48886) -- excluded from normal wild/catch pools,
    used as the base set for egg and dedicated-legendary-encounter logic.
    """
    return frozenset(_load_json("legendary_ids.json"))


@lru_cache(maxsize=1)
def get_egg_excluded_legendary_ids() -> frozenset[int]:
    """Legendary dex-ids that can never hatch from an egg
    (EGG_EXCLUDED_LEGENDARY_IDS, bundle.deobfuscated.js:48901)."""
    return frozenset(_load_json("egg_excluded_legendary_ids.json"))


@lru_cache(maxsize=1)
def get_legendary_egg_ids() -> tuple[int, ...]:
    """get_legendary_ids() minus get_egg_excluded_legendary_ids() -- the
    legendaries actually eligible to hatch from an egg
    (LEGENDARY_EGG_IDS, bundle.deobfuscated.js:48904, itself derived this
    way in the source; ported as its own table rather than recomputed here
    since the source computes it once at load time too).
    """
    return tuple(_load_json("legendary_egg_ids.json"))


@lru_cache(maxsize=1)
def get_legendary_pool_high() -> tuple[int, ...]:
    """Legendaries eligible to spawn at the "high" map-BST tier
    (LEGENDARY_POOL_HIGH, bundle.deobfuscated.js:48968), consumed by
    getRandomLegendary (48973).
    """
    return tuple(_load_json("legendary_pool_high.json"))


@lru_cache(maxsize=1)
def get_legendary_pool_very_high() -> tuple[int, ...]:
    """Legendaries eligible to spawn at the "veryHigh" map-BST tier
    (LEGENDARY_POOL_VERYHIGH, bundle.deobfuscated.js:48969).
    """
    return tuple(_load_json("legendary_pool_very_high.json"))


@lru_cache(maxsize=None)
def get_starter_ids(generation: int = 1) -> tuple[int, ...]:
    """generation in {1, 2, 3, 4} -> that generation's 3 starter dex-ids
    (STARTER_IDS / GEN2_STARTER_IDS / GEN3_STARTER_IDS / GEN4_STARTER_IDS,
    bundle.deobfuscated.js:49296-49299) -- excluded from wild/catch pools.
    """
    return tuple(_load_json(f"starter_ids_gen{generation}.json"))


@lru_cache(maxsize=1)
def get_gen1_with_gen2_evo() -> frozenset[int]:
    """10 Gen-1 species dex-ids that evolve into a Gen-2 species
    (GEN1_WITH_GEN2_EVO, bundle.deobfuscated.js:49001) -- let back into
    Gen2-restricted wild pools specifically because of that evolution line,
    an exception to the normal dex-range eligibility check in
    getCatchChoices (line 49112).
    """
    return frozenset(_load_json("gen1_with_gen2_evo.json"))


@lru_cache(maxsize=1)
def get_gen4_route1_banned() -> frozenset[int]:
    """Species dex-ids banned from Gen4 "Route 1" wild encounters
    specifically (GEN4_ROUTE1_BANNED, bundle.deobfuscated.js:49045)."""
    return frozenset(_load_json("gen4_route1_banned.json"))


@lru_cache(maxsize=1)
def get_gen4_route1_forced() -> tuple[int, ...]:
    """Species dex-ids force-included in Gen4 "Route 1" wild encounters
    (GEN4_ROUTE1_FORCED, bundle.deobfuscated.js:49048)."""
    return tuple(_load_json("gen4_route1_forced.json"))


# ---------------------------------------------------------------------------
# Trait unlock requirements (docs/logic-notes.md section 9, items 6-7).
# NOTE: despite the source names PASSIVE_REQUIRED_TYPE/PASSIVE_REQUIRED_COND
# ("passive" being this game's UI term for what the rest of this codebase
# calls a "trait"), the keys here are TRAIT ids from the same
# buildTraitsConfig system inventoried in docs/logic-notes.md section 9 item
# 1 (e.g. "elec_chain", "crit_overflow") -- NOT item ids. They gate which
# trait choices get OFFERED to the player based on current team composition
# (showPassiveItemChoice, bundle.deobfuscated.js:84978-85001), not a battle
# formula -- keep that distinction in mind if this feeds into engine.py's
# trait-offer logic rather than battle.py.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_trait_required_type() -> dict[str, str]:
    """trait id -> a Pokemon type the player's team must include for that
    trait to be offered (PASSIVE_REQUIRED_TYPE, bundle.deobfuscated.js:46325-46393).
    """
    return _load_json("trait_required_type.json")


@lru_cache(maxsize=1)
def get_trait_required_cond() -> dict[str, str]:
    """trait id -> a non-type condition the player's team must satisfy for
    that trait to be offered -- observed values are "shiny" and "legendary"
    (PASSIVE_REQUIRED_COND, bundle.deobfuscated.js:46395-46403).
    """
    return _load_json("trait_required_cond.json")


# ---------------------------------------------------------------------------
# Alternate forms (docs/logic-notes.md section 9, item 7). Mega Evolutions,
# regional forms, Deoxys formes, Rotom appliances, etc. -- resolved via a
# live PokeAPI string-id lookup in the source (fetchPokemonById, see
# docs/logic-notes.md section 4's correction), NOT part of the standard
# wild-encounter/catch loop. Ported here for completeness; low priority
# until whatever mechanic actually surfaces alternate forms gets traced.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_pokemon_form_slugs() -> dict[str, int]:
    """form slug (e.g. "charizard-mega-x") -> base species dex-id
    (POKEMON_FORM_SLUGS, bundle.deobfuscated.js:48571)."""
    return _load_json("pokemon_form_slugs.json")


@lru_cache(maxsize=1)
def get_pokemon_form_dex_ids() -> dict[str, int]:
    """form slug -> a synthetic PokeAPI-style id (10001+) used to look the
    form up via the live PokeAPI fetch path (POKEMON_FORM_SPRITE_IDS,
    bundle.deobfuscated.js:48572, despite the "sprite" in its source name
    this is an id, not a URL).
    """
    return _load_json("pokemon_form_dex_ids.json")


# ---------------------------------------------------------------------------
# Gen3-ability-analog system (docs/logic-notes-gen3abilities.md). A SEPARATE
# system from the roguelike trait/passive tables above: one ability id per
# SPECIES (not player-collected), symmetric (enemies use it too), assigned at
# battle-start via species lookup with evolution-line-root fallback. These
# three JSON files were pulled directly out of the bundle's own
# GEN3_ABILITY_LINES/GEN4_ABILITY_LINES/DEOXYS_FORMS array/object literals
# (bundle.deobfuscated.js:56855-57118) via a standalone Node eval of just
# those self-contained literals (see git history / session notes for the
# one-off extraction snippet -- not part of tools/extract-data's regular
# pipeline since these tables sit well past that tool's safe-prefix cutoff).
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_gen3_ability_by_species() -> dict[int, str]:
    """dex-id -> Gen3-ability-analog id (GEN3_ABILITY_BY_SPECIES,
    bundle.deobfuscated.js:56934-56938, flattened from GEN3_ABILITY_LINES).
    Used when NOT in Gen4 mode (see get_gen4_ability_by_species)."""
    raw = _load_json("gen3_ability_by_species.json")
    return {int(k): v for k, v in raw.items()}


@lru_cache(maxsize=1)
def get_gen4_ability_by_species() -> dict[int, str]:
    """dex-id -> Gen3-ability-analog id, Gen4-mode variant
    (GEN4_ABILITY_BY_SPECIES, bundle.deobfuscated.js:57029-57033, flattened
    from GEN4_ABILITY_LINES)."""
    raw = _load_json("gen4_ability_by_species.json")
    return {int(k): v for k, v in raw.items()}


@lru_cache(maxsize=1)
def get_deoxys_forms() -> dict[str, "BaseStats"]:
    """"normal"/"attack"/"defense"/"speed" -> flat base-stat block for the
    `form_change` ability's Deoxys-analog (DEOXYS_FORMS,
    bundle.deobfuscated.js:57049-57081)."""
    raw = _load_json("deoxys_forms.json")
    return {form: BaseStats.from_json(d) for form, d in raw.items()}
