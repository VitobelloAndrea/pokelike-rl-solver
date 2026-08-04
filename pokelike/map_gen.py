"""Port of map/node generation and wild-encounter selection
(docs/logic-notes.md section 4, bundle.deobfuscated.js:53090-53632,
48973-49150, 77576-77720).

**Scope of this module.** `generate_map()` is a faithful, complete port of
`generateMap` (line 53214-53507) -- the per-map 9-layer node/edge graph
(start -> a 2-node pair -> six depth-varying middle layers -> boss), plus
its Nuzlocke/Gen2/Gen3/Gen4/Endless node-type overrides AND two
deterministic (non-RNG-stream) hashes: the legendary-node species hash
(`_assign_legendary_species_id`, ported after finding `getRandomLegendary`
is dead code -- see that function's docstring) and the mid-map
TRAINER-node archetype hash (`_assign_trainer_sprite`, CODEX.md P0.9,
2026-07-31 -- a DIFFERENT mixing function, FNV-1a+murmur3-fmix32 rather
than the legendary hash's DJB2-ish one, populating
`node.extra["trainerSprite"]` for `engine._visit_trainer`'s
`TRAINER_BATTLE_CONFIG` lookup). Encounter selection
(`get_bst_bucket`, `get_catch_choices`, `get_level_for_node`,
`resolve_evo_for_level`, `pick_wild_encounter`) is a faithful port of the
STANDARD (non-Endless-buff-pool) path used by Story/Nuzlocke/Battle-Tower.

**`generateSubMap` is now ported** (bundle.deobfuscated.js:53508-53632, see
"Special submaps" section below, docs/logic-notes-submaps.md) -- the small
side-map (start -> 1-2 boss(es) -> 2-3 reward(s) -> subexit) generated when a
player visits an UNDERGROUND/DISTORTION node specifically. SILVER (Gen2) and
MAGMA/AQUA (Gen3) do NOT use this system at all -- confirmed by direct trace
of `onNodeClick`'s dispatch switch (bundle.deobfuscated.js:77364-77382):
those three dispatch straight to `doSilverNode`/`doAdminNode` (already ported
in `engine._visit_silver`/`_visit_admin`, CODEX.md P0.9), fixed-roster
in-place boss fights on the PARENT map with no submap involved, while only
`NODE_TYPES.UNDERGROUND`/`NODE_TYPES.DISTORTION` call `enterSubMap`.

**Explicitly NOT ported here, flagged rather than guessed at:**
- `getCatchChoicesForType` (line 49151) -- type-restricted encounter pools
  used inside Ghost/Underground submaps; depends on the submap system
  above, deferred with it.
- The Endless-mode "buff pool" remapping branch of `getCatchChoices` (line
  49123-49146, gated on its own `allow_buff_pool` parameter, which this
  port always treats as unavailable) and the deeper Endless2
  elite-progress overrides inside the node-type weight builder (gated
  behind not-yet-traced `endless2PastFirstElite`/`endless2PastSecondElite`
  predicates, accepted here as caller-supplied booleans defaulting False
  rather than guessed at).
- `doBattleNode`'s full battle-screen orchestration (line 77655-77730+) --
  only its pure "which species, at what level" decision is ported here as
  `pick_wild_encounter()`; the actual `Combatant` construction is
  `battle.py`'s job (`calc_hp()` below is the one small formula common to
  both).

Every function here takes its inputs explicitly (gen mode flags, challenge
flags, etc.) rather than reading a hidden global `state` object, consistent
with `battle.py`'s existing convention -- the source's `runBattle`/
`buildTraitsConfig` read a real module-level `state`, this port doesn't
have one yet (that's `engine.py`'s job).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional, Sequence

from pokelike import data, rng
from pokelike.battle_abilities import get_evo_line_root

# ---------------------------------------------------------------------------
# Node types & per-depth weight tables -- bundle.deobfuscated.js:53114-53205
# ---------------------------------------------------------------------------

START = "start"
BATTLE = "battle"
CATCH = "catch"
ITEM = "item"
QUESTION = "question"
BOSS = "boss"
POKECENTER = "pokecenter"
TRAINER = "trainer"
LEGENDARY = "legendary"
MOVE_TUTOR = "move_tutor"
TRADE = "trade"
SILVER = "silver"
MAGMA = "magma"
AQUA = "aqua"
UNDERGROUND = "underground"
DISTORTION = "distortion"
REWARD = "reward"
SUBEXIT = "subexit"

# NODE_WEIGHTS[depth], depth 0-5 (bundle.deobfuscated.js:53114-53204). Gen1 only.
NODE_WEIGHTS: tuple[dict[str, float], ...] = (
    {"battle": 25, "catch": 30, "item": 15, "trainer": 30, "question": 0, "pokecenter": 0, "move_tutor": 0, "trade": 0, "legendary": 0},
    {"battle": 20, "catch": 20, "item": 15, "trainer": 30, "question": 10, "pokecenter": 0, "move_tutor": 0, "trade": 5, "legendary": 0},
    {"battle": 16, "catch": 14, "item": 12, "trainer": 27, "question": 13, "pokecenter": 0, "move_tutor": 9, "trade": 9, "legendary": 0},
    {"battle": 13, "catch": 12, "item": 10, "trainer": 27, "question": 13, "pokecenter": 0, "move_tutor": 8, "trade": 8, "legendary": 0},
    {"battle": 13, "catch": 10, "item": 8, "trainer": 27, "question": 18, "pokecenter": 0, "move_tutor": 8, "trade": 7, "legendary": 0},
    {"battle": 20, "catch": 9, "item": 14, "trainer": 18, "question": 9, "pokecenter": 0, "move_tutor": 0, "trade": 0, "legendary": 0},
)

# GEN2_NODE_WEIGHTS -- one flat table shared by gen2/gen3/gen4 modes (NOT
# depth-indexed, unlike NODE_WEIGHTS above -- confirmed by the source using
# the same table object at every depth when any gen2/3/4 flag is set).
GEN2_NODE_WEIGHTS: dict[str, float] = {
    "battle": 25, "catch": 5, "item": 10, "trainer": 40, "question": 10,
    "pokecenter": 0, "move_tutor": 5, "trade": 5, "legendary": 0,
}

# The 6 middle-layer widths (bundle.deobfuscated.js:53216, `B2B`).
_LAYER_WIDTHS = (3, 4, 3, 4, 3, 2)

# Layer-1-only legendary weight overrides (bundle.deobfuscated.js:53332-53340).
_LEGENDARY_WEIGHT_GEN1 = 6
_LEGENDARY_WEIGHT_GEN4 = 9

# GEN2_LAYER_OFFSETS, bundle.deobfuscated.js:47732 -- level-scaling offsets
# for layers 1-7 in gen2/3/4 mode (see get_level_for_node).
GEN2_LAYER_OFFSETS = (0, 1, 2, 4, 5, 7, 8)


@dataclass
class MapNode:
    id: str
    type: str
    layer: int
    col: int
    visited: bool = False
    accessible: bool = False
    revealed: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class GeneratedMap:
    nodes: dict[str, MapNode]
    edges: list[tuple[str, str]]
    layers: list[list[MapNode]]
    map_index: int
    is_sub_map: Optional[str] = None  # "underground"/"distortion"/None -- see generate_sub_map


@dataclass
class ChallengeFlags:
    """Every non-gen-mode challenge/mode flag `generate_map` reads from
    `state` in the source, gathered into one place rather than passed as a
    long parameter list. All default to the ordinary Story-mode "off"
    behavior. `endless2_past_first_elite`/`endless2_past_second_elite` stand
    in for the source's own not-yet-traced predicates of the same name
    (bundle.deobfuscated.js:53352, 53359) -- pass explicit booleans if your
    caller can compute them, otherwise leave False.
    """

    is_endless_mode: bool = False
    challenge_one_catch: bool = False
    challenge_no_heal: bool = False
    challenge_only_fight: bool = False
    challenge_baby_only: bool = False
    challenge_endless2: bool = False
    endless2_region_index: int = 0
    endless2_past_first_elite: bool = False
    endless2_past_second_elite: bool = False


def _node_type_weights(depth: int, map_index: int, gen2: bool, gen3: bool, gen4: bool, flags: ChallengeFlags) -> dict[str, float]:
    """Port of `B2d`'s weight-table construction (bundle.deobfuscated.js:
    53321-53360), excluding the final `weightedRandom` pick and the
    post-pick endless catch->legendary override (see `_pick_node_type`).
    """
    if gen2 or gen3 or gen4:
        weights = dict(GEN2_NODE_WEIGHTS)
    else:
        weights = dict(NODE_WEIGHTS[min(depth, len(NODE_WEIGHTS) - 1)])

    if map_index >= 5 and depth >= 2 and not flags.is_endless_mode:
        weights["legendary"] = _LEGENDARY_WEIGHT_GEN1
    if gen4 and depth >= 2 and map_index >= 3 and not flags.is_endless_mode:
        weights["legendary"] = _LEGENDARY_WEIGHT_GEN4

    # nuzlocke_mode zeroes catch/trade -- applied by the caller before this
    # (see generate_map's `nuzlocke_mode` handling) since it's independent
    # of gen mode; folded in here for a single weights dict either way.
    if flags.is_endless_mode:
        weights["trade"] = 0
        weights["catch"] = weights.get("catch", 0) / 2
    if flags.challenge_one_catch:
        weights["catch"] = 0
        weights["legendary"] = 0

    if flags.challenge_endless2:
        if flags.endless2_past_first_elite:
            combined = weights.get("catch", 0) + weights.get("trade", 0)
            weights["catch"] = combined * 0.65
            weights["trade"] = combined * 0.35
            weights["underground"] = 3
        if depth >= 2:
            weights["legendary"] = 4
        if flags.endless2_past_second_elite:
            weights["question"] = 0

    return weights


def _pick_node_type(depth: int, map_index: int, gen2: bool, gen3: bool, gen4: bool, flags: ChallengeFlags, nuzlocke_mode: bool) -> str:
    weights = _node_type_weights(depth, map_index, gen2, gen3, gen4, flags)
    if nuzlocke_mode:
        weights["catch"] = 0
        weights["trade"] = 0
    picked = rng.weighted_random(weights)

    if picked == CATCH and flags.is_endless_mode and not flags.challenge_baby_only:
        region_step = flags.endless2_region_index + 1
        if 5 <= region_step <= 9 and rng.rng() < 1 / 3:
            return LEGENDARY
    return picked


def _js_round(x: float) -> int:
    """JS `Math.round(x)` for `x >= 0` (every call site here is a level or a
    level-range span, always non-negative): rounds half AWAY FROM ZERO
    (equivalently, half UP for non-negative x), unlike Python's builtin
    `round()`, which rounds half TO EVEN (banker's rounding). Found via the
    battle-oracle cross-check while validating `_trainer_fight_level`
    (CODEX.md addendum): `get_level_for_node`'s gen1 branch computes
    `lo + frac*(hi-lo)` which lands on an exact `.5` at map index 8
    (53 + 0.5*(64-53) = 58.5) -- Python's `round(58.5)` gives 58,
    `Math.round(58.5)` gives 59, a genuine pre-existing divergence
    unrelated to trainer/Silver/Magma/Aqua specifically but load-bearing
    for `_trainer_fight_level`'s own exact parity, so fixed here rather
    than left as a silent off-by-one."""
    return math.floor(x + 0.5)


def _imul32(a: int, b: int) -> int:
    """Signed 32-bit multiply matching JS `Math.imul` (see rng.py's
    `_mulberry32_step` docstring for why masked-unsigned arithmetic
    reproduces this bit-for-bit regardless of signedness)."""
    return ((a * b) & 0xFFFFFFFF)


def _to_int32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def _assign_legendary_species_id(node_id: str, map_index: int, run_seed: int, gen2_mode: bool, gen3_mode: bool, gen4_mode: bool) -> Optional[int]:
    """Port of `B2P`, the legendary-node species assigner
    (bundle.deobfuscated.js:53250-53303) -- called once per LEGENDARY node
    at map-generation time in the source (`B2a`'s dispatch, line 53310),
    **not** RNG-stream-based: a deterministic hash of `run_seed ^ map_index`
    (see below -- NOT `imul(map_index, 0x9e3779b1)`, a real divergence this
    docstring itself used to claim) further mixed with the node's own id
    string (DJB2-ish: `hash = hash*31 + charCode` per character), indexed
    into the gen-appropriate legendary id range. This is what actually
    determines a map's legendary encounter (cached onto the node and
    reused if the node is visited more than once, bundle.deobfuscated.js:
    80405-80409) -- **not** `get_random_legendary()` below, which is
    confirmed dead code in the source (declared, never called anywhere in
    the bundle).

    **Repaired during M4 route-oracle work.** The source's `Math.imul(Bcs+1,
    0x9e3779b1)` mixing (53290-53293) applies ONLY in Endless mode, to
    `Bcs` (a region/stage-derived index), never to `map_index`. The
    ordinary (non-Endless) branch that Story/Nuzlocke always takes is bare
    `Bcq = O | 0x0` (53283) -- `O` is `map_index` itself, unmultiplied. A
    prior version of this function applied the golden-ratio multiply to
    `map_index` UNCONDITIONALLY, which happened to be invisible for every
    previously-tested route because `imul(0, k) == 0 == 0` -- map_index 0
    hides the bug -- and only a route reaching a LEGENDARY node on map
    index >= 1 (which no scenario did until the M4 route-oracle work added
    one) could ever observe it diverge from the real source.

    Endless-mode's region-hash mixing and `activeEncounterType()` themed-
    type filtering (both gated behind `state`/`endlessState` in the
    source) are NOT modeled here -- flagged rather than guessed at.
    """
    if gen4_mode:
        lo, hi = 0x183, 0x1ED
    elif gen3_mode:
        lo, hi = 0xFC, 0x182
    elif gen2_mode:
        lo, hi = 0x98, 0xFB
    else:
        lo, hi = 0x1, 0x97
    pool = tuple(sid for sid in data.get_legendary_ids() if lo <= sid <= hi)
    if not pool:
        return None
    pool = tuple(sorted(pool))

    seed = _to_int32(map_index) ^ _to_int32(run_seed)
    seed = _to_int32(seed)
    for ch in node_id:
        seed = _to_int32(_imul32(seed, 0x1F) + ord(ch))
    return pool[abs(seed) % len(pool)]


def _urshift32(x: int, bits: int) -> int:
    """JS `x >>> bits` (unsigned right shift): treats `x` as an unsigned
    32-bit value before shifting, regardless of Python's arbitrary-precision
    signed representation."""
    return (x & 0xFFFFFFFF) >> bits


def _trainer_sprite_candidates(map_index: int, gen2_mode: bool, gen3_mode: bool, gen4_mode: bool) -> tuple[str, ...]:
    """Port of the candidate-archetype-list filter inside `generateMap`'s
    trainerSprite-assignment closure (`B2D`, bundle.deobfuscated.js:
    53223-53237). `gen4Mode` bypasses every exclusion (`GEN4_TRAINER_KEYS`
    used as-is); every other mode first drops "aceTrainer"/"policeman" past
    their own map-index cutoffs (map_index>=6 / >=4 respectively -- true
    even in gen3Mode), then gen3Mode allows everything else through
    unconditionally, and Gen1/Gen2 apply the GEN1_ONLY/GEN2_ONLY exclusion
    sets on top.
    """
    if gen4_mode:
        return data.get_gen4_trainer_keys()

    base = data.get_gen3_trainer_keys() if gen3_mode else data.get_trainer_sprite_keys()

    def excluded_by_map_index(key: str) -> bool:
        return (key == "aceTrainer" and map_index >= 6) or (key == "policeman" and map_index >= 4)

    if gen3_mode:
        return tuple(key for key in base if not excluded_by_map_index(key))

    gen1_only = data.get_gen1_only_trainer_keys()
    gen2_only = data.get_gen2_only_trainer_keys()
    return tuple(
        key
        for key in base
        if not excluded_by_map_index(key)
        and not ((not gen2_mode and key in gen2_only) or (gen2_mode and key in gen1_only))
    )


def _trainer_sprite_hash(node_id: str, map_index: int, run_seed: int) -> int:
    """Port of the deterministic (non-RNG-stream) hash `generateMap`'s
    trainerSprite-assignment closure computes (`B2D`, bundle.deobfuscated.js:
    53238-53248) -- an FNV-1a-style accumulation over the node id's
    characters seeded from `imul(map_index, 0x9e3779b1) ^ run_seed`, finished
    with a MurmurHash3 `fmix32`-style avalanche (two xor-shifts around an
    `imul` by `0x7feb352d`). Same non-RNG-stream design family as
    `_assign_legendary_species_id`, but a different mixing function -- ported
    separately rather than reused, since the two hashes are confirmed
    distinct in the source (DJB2-ish `hash*31+ch` there vs FNV-1a+fmix32
    here). Returns the raw signed int32 hash; callers index
    `abs(hash) % len(candidates)`.
    """
    seed = _to_int32(_imul32(_to_int32(map_index), 0x9E3779B1)) ^ _to_int32(run_seed)
    seed = _to_int32(seed)
    for ch in node_id:
        seed = _to_int32(_imul32(seed ^ ord(ch), 0x1000193))
    seed = _to_int32(seed ^ _urshift32(seed, 0x10))
    seed = _to_int32(_imul32(seed, 0x7FEB352D))
    seed = _to_int32(seed ^ _urshift32(seed, 0xF))
    return seed


def _assign_trainer_sprite(node_id: str, map_index: int, run_seed: int, gen2_mode: bool, gen3_mode: bool, gen4_mode: bool) -> Optional[str]:
    """Port of `B2D` in full (bundle.deobfuscated.js:53221-53248) -- called
    once per TRAINER-type node at map-generation time (`B2a`'s dispatch,
    line 53309), populating `node.extra["trainerSprite"]`. This is what
    `doTrainerNode`/`engine._visit_trainer` key into
    `TRAINER_BATTLE_CONFIG` with -- **not** a live per-visit roll, matching
    the source's own design (revisiting the node never changes its
    archetype, since the node object itself carries the field).
    """
    candidates = _trainer_sprite_candidates(map_index, gen2_mode, gen3_mode, gen4_mode)
    if not candidates:
        return None
    h = _trainer_sprite_hash(node_id, map_index, run_seed)
    return candidates[abs(h) % len(candidates)]


def _make_node(node_id: str, node_type: str, layer: int, col: int, **extra) -> MapNode:
    return MapNode(id=node_id, type=node_type, layer=layer, col=col, extra=extra)


def _connect_layers(from_layer: Sequence[MapNode], to_layer: Sequence[MapNode]) -> list[tuple[str, str]]:
    """Port of `B2j` (bundle.deobfuscated.js:53386-53416): connects every
    node in `from_layer` to 1 or 2 nodes in `to_layer` so every path stays
    traversable, distributing proportionally by relative position when
    neither layer is a single node.
    """
    n_from = len(from_layer)
    n_to = len(to_layer)
    if n_from == 1:
        return [(from_layer[0].id, node.id) for node in to_layer]

    edges: list[tuple[str, str]] = []
    for i in range(n_from):
        if n_to == 1:
            lo = hi = 0
        elif n_to < n_from and i == 0:
            lo = hi = 0
        elif n_to < n_from and i == n_from - 1:
            lo = hi = n_to - 1
        else:
            frac = (i * (n_to - 1)) / (n_from - 1)
            lo = int(frac)
            hi = lo + 1
            if hi >= n_to:
                hi = n_to - 1
                lo = n_to - 2
        edges.append((from_layer[i].id, to_layer[lo].id))
        if lo != hi:
            edges.append((from_layer[i].id, to_layer[hi].id))
    return edges


def generate_map(
    map_index: int,
    nuzlocke_mode: bool = False,
    gen2_mode: bool = False,
    gen3_mode: bool = False,
    gen4_mode: bool = False,
    flags: Optional[ChallengeFlags] = None,
    run_seed: int = 0,
) -> GeneratedMap:
    """Port of `generateMap(mapIndex, nuzlockeMode, gen2Mode, gen3Mode,
    gen4Mode)` (bundle.deobfuscated.js:53214-53507). Builds ONE map's full
    node graph: layer 0 (start) -> layer 1 (a catch/trade-or-battle pair)
    -> six middle layers of widths (3, 4, 3, 4, 3, 2) -> a final boss
    layer. `map_index` is which of the 9 maps (0-8) within the current
    generation this is -- NOT a layer index (docs/logic-notes.md section 4
    on why it's always 0-8, never an 18-map arc).

    `run_seed` is `state.runSeed` (the save's `rngSeed`, see `rng.py`'s
    `get_rng_seed()`) -- consumed only by the deterministic (non-RNG-
    stream) legendary-node species hash, `_assign_legendary_species_id`;
    every LEGENDARY-type node gets `node.extra["legendarySpeciesId"]`
    populated at generation time, matching the source's `B2P`/`B2a`
    dispatch (line 53309-53310).
    """
    flags = flags or ChallengeFlags()
    layers: list[list[MapNode]] = [[_make_node("n0_0", START, 0, 0)]]

    force_same_type = nuzlocke_mode or flags.challenge_one_catch
    layer1_type = (
        TRADE
        if (flags.challenge_endless2 and flags.endless2_past_first_elite and rng.rng() >= 0.65)
        else CATCH
    )
    layers.append([
        _make_node("n1_0", layer1_type, 1, 0),
        _make_node("n1_1", layer1_type if force_same_type else BATTLE, 1, 1),
    ])

    for depth in range(len(_LAYER_WIDTHS)):
        layer_idx = depth + 2
        width = _LAYER_WIDTHS[depth]
        layer_nodes = [
            _make_node(f"n{layer_idx}_{col}", _pick_node_type(depth, map_index, gen2_mode, gen3_mode, gen4_mode, flags, nuzlocke_mode), layer_idx, col)
            for col in range(width)
        ]
        for node in layer_nodes:
            if node.type == LEGENDARY:
                node.extra["legendarySpeciesId"] = _assign_legendary_species_id(node.id, map_index, run_seed, gen2_mode, gen3_mode, gen4_mode)
            elif node.type == TRAINER:
                node.extra["trainerSprite"] = _assign_trainer_sprite(node.id, map_index, run_seed, gen2_mode, gen3_mode, gen4_mode)
        if (
            not flags.challenge_no_heal
            and depth == len(_LAYER_WIDTHS) - 1
            and not any(n.type == POKECENTER for n in layer_nodes)
        ):
            layer_nodes[int(rng.rng() * width)].type = POKECENTER
        layers.append(layer_nodes)

    # Gen2 Silver rival node -- layer index 4, odd map indices only.
    # `del ...trainerSprite` mirrors the source dropping the field when a
    # node that happened to roll TRAINER gets overridden to a special type
    # (bundle.deobfuscated.js:53449, 53460-53462, 53470-53473) -- inert for
    # engine.py (a SILVER/MAGMA/AQUA-typed node is never dispatched to
    # `_visit_trainer`), kept for node-state fidelity.
    if gen2_mode and map_index in (1, 3, 5, 7):
        silver_layer = layers[4]
        if len(silver_layer) == 3:
            silver_layer[1].type = SILVER
            silver_layer[1].extra.pop("trainerSprite", None)

    # Gen3 Team Magma/Aqua nodes -- layer index 4, specific map indices.
    if gen3_mode and not flags.is_endless_mode and map_index in (2, 5, 7):
        magma_layer = layers[4]
        if len(magma_layer) == 3:
            magma_layer[0].type = MAGMA
            magma_layer[0].extra.pop("trainerSprite", None)
            magma_layer[2].type = AQUA
            magma_layer[2].extra.pop("trainerSprite", None)

    # Gen4 Underground/Distortion nodes -- layer index 4, specific map indices.
    if gen4_mode and not flags.is_endless_mode:
        gen4_layer = layers[4]
        if len(gen4_layer) == 3:
            if map_index in (1, 3, 6):
                gen4_layer[0].type = UNDERGROUND
                gen4_layer[0].extra.pop("trainerSprite", None)
            if map_index in (3, 5, 7):
                gen4_layer[2].type = DISTORTION
                gen4_layer[2].extra.pop("trainerSprite", None)

    if flags.is_endless_mode and flags.challenge_only_fight:
        for layer in layers:
            for node in layer:
                if node.layer >= 2 and node.type != BOSS:
                    node.type = TRAINER

    boss_layer_idx = 2 + len(_LAYER_WIDTHS)
    layers.append([_make_node(f"n{boss_layer_idx}_0", BOSS, boss_layer_idx, 0, mapIndex=map_index)])

    edges: list[tuple[str, str]] = []
    for i in range(len(layers) - 1):
        edges.extend(_connect_layers(layers[i], layers[i + 1]))

    nodes: dict[str, MapNode] = {node.id: node for layer in layers for node in layer}
    nodes["n0_0"].visited = True
    for src, dst in edges:
        if src == "n0_0":
            nodes[dst].accessible = True

    return GeneratedMap(nodes=nodes, edges=edges, layers=layers, map_index=map_index)


# ---------------------------------------------------------------------------
# Encounter selection -- bundle.deobfuscated.js:48973-49150, 77576-77720
# ---------------------------------------------------------------------------


def calc_hp(base_hp: int, level: int) -> int:
    """Port of `calcHp` (bundle.deobfuscated.js:77224-77226):
    `floor(base_hp*level/50) + level + 10`."""
    return (base_hp * level) // 50 + level + 10


def get_catch_gen_range(gen2_mode: bool = False, gen3_mode: bool = False, gen4_mode: bool = False) -> tuple[int, int]:
    """Port of `getCatchGenRange` (bundle.deobfuscated.js:77475-77497),
    Endless-mode branch excluded (that reads `getStageGenRange`, an
    Endless-only table not covered here)."""
    if gen4_mode:
        return (0x183, 0x1ED)
    if gen3_mode:
        return (0xFC, 0x182)
    if gen2_mode:
        return (0x98, 0xFB)
    return (1, 0x97)


def get_bst_bucket(bst_min: int, mode: str = "none") -> tuple[int, ...]:
    """Port of `getBstBucket` (bundle.deobfuscated.js:49004-49021).
    `mode` is one of "none"/"gen2"/"endless" (the source's 3rd
    "which extra tier to union in" behavior, keyed off a string built from
    `state.gen2Mode`/`state.isEndlessMode` at the call site).
    """
    pool = data.get_fallback_species_pool()

    def union_if(cond: bool, a: Sequence[int], b: Sequence[int]) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*a, *b))) if cond else tuple(a)

    gen2_or_endless = mode in ("gen2", "endless")
    endless_only = mode == "endless"

    if bst_min >= 0x212:
        return union_if(gen2_or_endless, pool.very_high, pool.high)
    if bst_min >= 0x1CC:
        return union_if(gen2_or_endless, pool.high, pool.mid_high)
    if bst_min >= 0x190:
        return union_if(gen2_or_endless, pool.mid_high, pool.mid)
    if bst_min >= 0x154:
        return union_if(endless_only, pool.mid, pool.mid_low)
    if bst_min >= 0x118:
        return union_if(endless_only, pool.mid_low, pool.low)
    return pool.low


def get_random_legendary(
    map_index: int,
    use_very_high_pool: bool = True,
    gen2_mode: bool = False,
    gen3_mode: bool = False,
    gen4_mode: bool = False,
) -> Optional[int]:
    """Port of `getRandomLegendary` (bundle.deobfuscated.js:48973-48999),
    minus the `fetchPokemonById` resolution step -- returns a dex-id or
    None (no legendary available at this map's BST tier), not a resolved
    Pokemon.

    **This function is confirmed DEAD CODE in the source** -- grepped the
    entire bundle for call sites and found none besides its own
    declaration. Ported anyway for completeness/citation, but do NOT wire
    this up as "what a LEGENDARY map node encounters" -- that's
    `_assign_legendary_species_id`/`generate_map`'s
    `node.extra["legendarySpeciesId"]` instead (a deterministic hash, not
    an RNG-stream pick -- see that function's docstring). Kept in case a
    later pass finds a live call site this grep missed.
    """
    generation = 4 if gen4_mode else 3 if gen3_mode else 2 if gen2_mode else 1
    ranges = data.get_map_bst_ranges(generation)
    bst_range = ranges[min(map_index, len(ranges) - 1)]

    base_pool = data.get_legendary_pool_very_high() if use_very_high_pool else (0x96, 0x97)
    if bst_range.min >= 0x212:
        pool = base_pool
    elif bst_range.min >= 0x1CC:
        pool = (*data.get_legendary_pool_high(), *base_pool)
    else:
        return None
    return pool[int(rng.rng() * len(pool))]


def get_level_for_node(
    layer: int,
    current_map: int,
    gen2_mode: bool = False,
    gen3_mode: bool = False,
    gen4_mode: bool = False,
) -> int:
    """Port of `getLevelForNode` (bundle.deobfuscated.js:77582-77614),
    Endless-mode branch excluded (reads Endless-only globals not modeled
    here)."""
    if gen2_mode or gen3_mode or gen4_mode:
        generation = 4 if gen4_mode else 3 if gen3_mode else 2
        map_range = data.get_map_level_ranges(generation)[current_map]
        lo, hi = map_range.min, map_range.max
        if layer >= len(GEN2_LAYER_OFFSETS) + 1:
            return hi
        offset_idx = min(len(GEN2_LAYER_OFFSETS), max(1, layer)) - 1
        return lo + GEN2_LAYER_OFFSETS[offset_idx]

    map_range = data.get_map_level_ranges(1)[current_map]
    lo, hi = map_range.min, map_range.max
    frac = min(1.0, max(0.0, (layer - 1) / 6))
    base = _js_round(lo + frac * (hi - lo))
    jitter_span = max(1, _js_round((hi - lo) / 8))
    return min(hi, max(lo, base + int(rng.rng() * jitter_span)))


def get_move_tier_for_map(current_map: int) -> int:
    """Port of `getMoveТierForMap` (bundle.deobfuscated.js:41985-41987):
    tier 0 on maps 0-2, tier 1 from map 3 onward. The sole source of the
    "default" move tier a newly-created wild/trainer/catch/trade Pokemon
    gets everywhere in the source (`createInstance`'s own default parameter
    is a plain `1`, but virtually every real call site overrides it with
    this function's result instead -- CODEX.md issue 11)."""
    return 0 if current_map <= 2 else 1


def _evolution_family(root_species_id: int) -> frozenset[int]:
    """Port of `_evolutionFamily` (bundle.deobfuscated.js:49023-49042): every
    species reachable by walking FORWARD (linear + branching) from
    `root_species_id` -- meant to be called with an evolution-line ROOT
    (see `is_gen4_line_eligible`), not an arbitrary mid-line species."""
    evolutions = data.get_evolutions()
    branching = data.get_branching_evolutions()
    family = {root_species_id}
    stack = [root_species_id]
    while stack:
        current = stack.pop()
        evo = evolutions.get(current)
        if evo is not None and evo.into not in family:
            family.add(evo.into)
            stack.append(evo.into)
        for branch in branching.get(current, ()):
            if branch.into not in family:
                family.add(branch.into)
                stack.append(branch.into)
    return frozenset(family)


@lru_cache(maxsize=1)
def _gen4_eligible_ids() -> frozenset[int]:
    """Port of `isGen4LineEligible`'s memoized `_gen4EligibleIds` set
    (bundle.deobfuscated.js:49049-49057): the union of the full evolution
    FAMILY (root + every descendant, possibly spanning earlier-numbered
    dex ids for cross-gen baby forms) rooted from every species in the
    Gen4 dex-id range (387-493 inclusive) -- broader than that raw numeric
    range itself, which is exactly why `getCatchChoices`'s naive
    `min_dex_id <= id <= max_dex_id` range check (used for every other
    generation) can't be reused for Gen4 (CODEX.md issue 13)."""
    eligible: set[int] = set()
    for dex_id in range(0x183, 0x1EE):  # 387..493 inclusive
        eligible.update(_evolution_family(get_evo_line_root(dex_id)))
    return frozenset(eligible)


def is_gen4_line_eligible(species_id: int) -> bool:
    """Port of `isGen4LineEligible` (bundle.deobfuscated.js:49049-49057)."""
    return species_id in _gen4_eligible_ids()


def get_catch_choices(
    map_index: int,
    count: int = 3,
    max_dex_id: int = 0x97,
    min_dex_id: int = 1,
    exclude_starters: bool = True,
    gen2_mode: bool = False,
    gen3_mode: bool = False,
    gen4_mode: bool = False,
    is_endless_mode: bool = False,
) -> list[int]:
    """Port of `getCatchChoices`'s standard (non-buff-pool) path
    (bundle.deobfuscated.js:49059-49150). `exclude_starters` is the
    source's 4th positional param (`iS` -- true in Story/Nuzlocke, false in
    Endless mode): when True, the current generation's 3 starter species
    (chosen by `min_dex_id`'s range) are excluded from the pool, matching
    the source naming this parameter for what it does, not what the
    obfuscated source called it.
    """
    generation = 4 if gen4_mode else 3 if gen3_mode else 2 if gen2_mode else 1
    bst_ranges = data.get_map_bst_ranges(generation)
    bst_range = bst_ranges[min(map_index, len(bst_ranges) - 1)]

    bst_mode = "endless" if is_endless_mode else "gen2" if gen2_mode else "none"
    bucket = list(get_bst_bucket(bst_range.min, bst_mode))
    if gen4_mode and map_index <= 0:
        bucket = list(dict.fromkeys((*bucket, *data.get_gen4_route1_forced())))

    starter_ids: frozenset[int] = frozenset()
    if exclude_starters:
        starter_gen = 4 if min_dex_id >= 0x183 else 3 if min_dex_id >= 0xFC else 2 if min_dex_id >= 0x98 else 1
        starter_ids = frozenset(data.get_starter_ids(starter_gen))

    never_wild = data.get_never_wild_ids()
    legendary_ids = data.get_legendary_ids()
    route1_banned = data.get_gen4_route1_banned()
    gen1_with_gen2_evo = data.get_gen1_with_gen2_evo()
    tyranitar_line = frozenset({0xF6, 0xF7, 0xF8})

    def base_eligible(species_id: int) -> bool:
        if species_id in legendary_ids or species_id in starter_ids or species_id in never_wild:
            return False
        # `GEN4_ROUTE1_BANNED.has(getEvoLineRoot(id))` in the source -- the
        # ban applies to the whole evolution FAMILY (keyed by its root),
        # not to the individual candidate species id.
        if gen4_mode and map_index <= 0 and get_evo_line_root(species_id) in route1_banned:
            return False
        if species_id in tyranitar_line and gen2_mode and map_index < 2:
            return False
        return True

    def in_gen_range(species_id: int) -> bool:
        if gen2_mode and species_id in gen1_with_gen2_evo:
            return True
        if gen4_mode:
            return is_gen4_line_eligible(species_id)
        return min_dex_id <= species_id <= max_dex_id

    eligible = [sid for sid in bucket if base_eligible(sid) and (is_endless_mode or in_gen_range(sid))]

    for i in range(len(eligible) - 1, 0, -1):
        j = int(rng.rng() * (i + 1))
        eligible[i], eligible[j] = eligible[j], eligible[i]

    return eligible[:count]


def resolve_evo_for_level(species_id: int, level: int) -> int:
    """Port of `resolveEvoForLevel` (bundle.deobfuscated.js:50409-50432):
    walks a species forward through LINEAR evolutions while `level` meets
    each step's requirement, then walks back down if the result ends up
    higher than `level` actually supports (branching evolutions only
    considered on the way down, matching the source)."""
    evolutions = data.get_evolutions()
    branching = data.get_branching_evolutions()

    current = species_id
    while current in evolutions and level >= evolutions[current].level:
        current = evolutions[current].into

    changed = True
    while changed:
        changed = False
        for base_id, evo in evolutions.items():
            if evo.into == current and level < evo.level:
                current = base_id
                changed = True
                break
        if not changed:
            for base_id, branches in branching.items():
                match = next((b for b in branches if b.into == current), None)
                if match is not None and level < match.level:
                    current = base_id
                    changed = True
                    break
    return current


def pick_wild_encounter(
    layer: int,
    current_map: int,
    player_team_types: Sequence[Sequence[str]] = (),
    gen2_mode: bool = False,
    gen3_mode: bool = False,
    gen4_mode: bool = False,
    is_endless_mode: bool = False,
) -> tuple[int, int]:
    """Port of `doBattleNode`'s species/level decision (bundle.deobfuscated.js:
    77655-77714) -- NOT the full battle-screen orchestration, just "which
    species, at what level". Returns `(species_id, level)`; the caller
    builds the actual `Combatant` (`battle.calc_hp` there is the same
    formula as `calc_hp` above).

    `player_team_types` is only consulted for the map-0/layer-1 "safe
    first encounter" filter (bundle.deobfuscated.js:77682-77702): on the
    very first encounter of a run, wild candidates that would hit any of
    the player's own team[0]'s types super-effectively are excluded,
    falling back to a guaranteed Eevee (dex 133) if that empties the pool.
    """
    level_offset = (
        min(4, (current_map + 1) // 2) if (gen2_mode or gen3_mode)
        else 1 if (not is_endless_mode and current_map >= 1)
        else 0
    )
    min_level = max(1, get_level_for_node(layer, current_map, gen2_mode, gen3_mode, gen4_mode) - level_offset)

    min_dex, max_dex = get_catch_gen_range(gen2_mode, gen3_mode, gen4_mode)
    candidates = get_catch_choices(
        current_map, 3, max_dex, min_dex,
        exclude_starters=not is_endless_mode,
        gen2_mode=gen2_mode, gen3_mode=gen3_mode, gen4_mode=gen4_mode,
        is_endless_mode=is_endless_mode,
    )

    # CODEX.md issue 2: the source filters/resolves/creates the wild mon at
    # the REDUCED `min_level` (`ip` in `doBattleNode`, bundle.deobfuscated.js:
    # 77655-77714), not the raw per-node level -- this used to compute
    # `min_level` and then never use it, returning/filtering against the
    # unreduced level instead, making every standard-map wild encounter one
    # level too high (up to four for Gen2/Gen3).
    filtered = [c for c in candidates if min_level_for_species(c) <= min_level]
    if filtered:
        candidates = filtered

    if current_map == 0 and layer == 1 and player_team_types:
        own_types = {t.capitalize() for t in player_team_types[0]}
        chart = data.get_type_chart()

        def is_safe(species_id: int) -> bool:
            species_types = data.get_pokedex()[species_id].types
            for move_type in species_types:
                for own_type in own_types:
                    if chart.get(move_type.capitalize(), {}).get(own_type, 1) >= 2:
                        return False
            return True

        safe = [c for c in candidates if is_safe(c)]
        candidates = safe if safe else [133]  # Eevee fallback

    chosen = candidates[int(rng.rng() * len(candidates))]
    resolved = resolve_evo_for_level(chosen, min_level)
    return resolved, min_level


def min_level_for_species(species_id: int) -> int:
    """Port of `minLevelForSpecies` (bundle.deobfuscated.js:50347-50354):
    the level requirement of whichever evolution step produces this
    species (1 if it's never an evolution target, i.e. a base form)."""
    for evo in data.get_evolutions().values():
        if evo.into == species_id:
            return evo.level
    for branches in data.get_branching_evolutions().values():
        for evo in branches:
            if evo.into == species_id:
                return evo.level
    return 1


# ---------------------------------------------------------------------------
# Special submaps -- Underground/Distortion World (docs/logic-notes-submaps.md,
# bundle.deobfuscated.js:53508-53632 `generateSubMap`, 76399-76837
# `distortionLegendary`/`subMapBaseLevel`/`rollSubMapBoss`/
# `rollUndergroundTrainers`/`pickSubMapRewards`). Gen4/Sinnoh-only: SILVER/
# MAGMA/AQUA do NOT use this system (see this module's own docstring).
# Endless-mode branches throughout are excluded, same convention as the rest
# of this module; every function here stays state-free (explicit params,
# no hidden global `state`) -- `engine.py` owns `subMapReturn`/
# `distortionWorldsEntered`/`distortionLegendaryClaimed` persistence.
# ---------------------------------------------------------------------------


def _fisher_yates(items: list) -> None:
    """The same Fisher-Yates idiom used inline throughout this module
    (`get_catch_choices`, `generate_map`'s trainerSprite candidates, ...) --
    extracted as a helper here since `_roll_underground_trainers` alone
    needs it 3 separate times."""
    for i in range(len(items) - 1, 0, -1):
        j = int(rng.rng() * (i + 1))
        items[i], items[j] = items[j], items[i]


def sub_map_level_cap(is_endless_mode: bool = False) -> float:
    """Port of `subMapLevelCap` (bundle.deobfuscated.js:76410-76415) -- always
    100 in this port's reachable (non-Endless) scope."""
    return math.inf if is_endless_mode else 100


def _sub_map_base_level(kind: str, map_index: int, parent_node_level: Optional[int]) -> int:
    """Port of `subMapBaseLevel` (bundle.deobfuscated.js:76416-76452),
    Endless-mode branch excluded. `parent_node_level` is
    `get_level_for_node(...)` already computed by the CALLER for the node
    the player is entering the submap FROM (`state.subMapReturn` in the
    source, bundle.deobfuscated.js:76434-76442: `getLevelForNode(iS.map.
    nodes[iS.nodeId]) + 1`) -- every reachable call site in this port
    populates this (`engine.enter_sub_map` always sets it before generating
    the submap), so the `None` fallback below (the source's OWN defensive
    branch for when `state.subMapReturn` is unset -- unconditionally reads
    `GEN4_MAP_LEVEL_RANGES` regardless of generation, since this whole
    subsystem only exists in gen4Mode) is ported for completeness rather
    than expected to actually run."""
    if parent_node_level is not None:
        return parent_node_level + 1
    ranges = data.get_map_level_ranges(4)
    idx = min(max(map_index, 0), len(ranges) - 1)
    return ranges[idx].max + (-5 if kind == UNDERGROUND else 2)


def _distortion_legendary(
    distortion_worlds_entered_after: int, distortion_legendary_claimed: bool
) -> Optional["data.DistortionLegendaryEntry"]:
    """Port of `distortionLegendary` (bundle.deobfuscated.js:76399-76409).
    `distortion_worlds_entered_after` is the counter value AFTER the current
    visit's increment (see `generate_sub_map`'s docstring for the exact
    increment-then-check order the source uses) -- only the player's
    SECOND-EVER Distortion visit can roll a legendary, and only if it hasn't
    already been claimed this run. Consumes exactly one `rng()` draw when
    triggered, ZERO when short-circuited (matching the source's `cond ? null
    : pool[floor(rng()*len)]` -- the `rng()` call is inside the untaken
    branch when the guard fails)."""
    if distortion_legendary_claimed or distortion_worlds_entered_after != 2:
        return None
    pool = data.get_distortion_legendary_pool()
    return pool[int(rng.rng() * len(pool))]


def _roll_sub_map_boss(kind: str, map_index: int, parent_node_level: Optional[int]) -> tuple[str, str, list[dict]]:
    """Port of `rollSubMapBoss` (bundle.deobfuscated.js:76453-76470). Returns
    `(name, sprite, team)`, `team` a list of `{"species_id":..., "level":...}`
    dicts (source `id`/`level` keys renamed for this port's convention).
    Consumes exactly one `rng()` draw (the fixed-team pick)."""
    level = _sub_map_base_level(kind, map_index, parent_node_level)
    bosses = data.get_submap_bosses()
    boss = bosses.get(kind) or bosses[UNDERGROUND]
    chosen_team = boss.teams[int(rng.rng() * len(boss.teams))]
    team = [{"species_id": m.species_id, "level": min(100, max(5, level + m.level_offset))} for m in chosen_team]
    return boss.name, boss.sprite, team


def _roll_underground_trainers(map_index: int, parent_node_level: Optional[int], gen4_mode: bool = True) -> list[dict]:
    """Port of `rollUndergroundTrainers`'s non-Endless path
    (bundle.deobfuscated.js:76584-76669) -- the Endless-mode/`challengeGen4`
    branch (76587-76615) is out of scope, same convention as the rest of
    this module. `gen4_mode` selects `GEN4_TRAINER_KEYS` vs
    `UNDERGROUND_TRAINER_KEYS` (bundle.deobfuscated.js:76616-76622) --
    always `True` in this port's actual reachable scope, since an
    UNDERGROUND-type node only ever exists when `gen4_mode=True`
    (`generate_map`'s own node-placement gate) -- kept as a parameter for
    citation fidelity, not because the `False` branch is reachable here.

    Returns one `{"key":..., "name":..., "team": [{"species_id":...,
    "level":...}, ...]}` dict per selected trainer -- always exactly 2 in
    this (non-Endless) path. RNG draw order: shuffle the candidate
    archetype-key list, THEN for each of the 2 selected trainers, shuffle
    that trainer's own species pool -- exact source order, load-bearing for
    RNG-draw-count parity (bundle.deobfuscated.js:76623-76657)."""
    level = _sub_map_base_level(UNDERGROUND, map_index, parent_node_level)
    keys = list(data.get_gen4_trainer_keys() if gen4_mode else data.get_underground_trainer_keys())
    _fisher_yates(keys)

    level_offsets = (-2, -1, 0)
    level_cap = sub_map_level_cap()
    used_species: set = set()
    config = data.get_trainer_battle_config()

    results = []
    for trainer_key in keys[:2]:
        archetype = config.get(trainer_key) or config["aceTrainer"]
        pool = list(dict.fromkeys(archetype.gen4_pool or (0x183,)))
        filtered = [sid for sid in pool if min_level_for_species(sid) <= level]
        candidates = filtered if filtered else pool
        _fisher_yates(candidates)

        resolved: list[int] = []
        seen: set = set()
        for raw_id in candidates:
            evo_id = resolve_evo_for_level(raw_id, level)
            if evo_id not in seen:
                seen.add(evo_id)
                resolved.append(evo_id)

        preferred = [sid for sid in resolved if sid not in used_species]
        already_used = [sid for sid in resolved if sid in used_species]
        team_species = (preferred + already_used)[:3]
        for raw_id in candidates:
            if len(team_species) >= 3:
                break
            if raw_id not in team_species:
                team_species.append(raw_id)
        while team_species and len(team_species) < 3:
            team_species.append(team_species[-1])
        used_species.update(team_species)

        team = [
            {"species_id": sid, "level": min(level_cap, max(5, level + level_offsets[i]))}
            for i, sid in enumerate(team_species)
        ]
        results.append({"key": trainer_key, "name": archetype.name, "team": team})
    return results


def _pick_sub_map_rewards(kind: str, team_size: int, count: int = 2) -> list[str]:
    """Port of `pickSubMapRewards` (bundle.deobfuscated.js:76671-76686).
    "skip" is always excluded from this pool (`generate_sub_map` appends it
    separately, unconditionally, as the submap's final reward node) and so
    are the `DISTORTION_LEGEND_REWARDS` ids (`generate_sub_map` assigns
    those directly only via `_distortion_legendary`, never through this
    random pool). The Fisher-Yates shuffle runs over the FULL filtered
    candidate pool regardless of `count` -- consumes `len(pool)-1` `rng()`
    draws even when only 1 or 2 results are kept, a real RNG-order detail
    (not just an implementation artifact), since it's interleaved with
    every OTHER `rng()`-consuming step inside `generate_sub_map`."""
    legend_rewards = data.get_distortion_legend_rewards()
    pool = [
        r.id
        for r in data.get_submap_rewards()
        if kind in r.kinds and r.id not in legend_rewards and r.id != "skip" and (r.min_team is None or team_size >= r.min_team)
    ]
    _fisher_yates(pool)
    return pool[:count]


@dataclass
class SubMapResult:
    """Return value of `generate_sub_map` -- the generated submap plus the
    UPDATED `distortion_worlds_entered` counter. `generateSubMap` mutates
    `state.distortionWorldsEntered` as a side effect in the source
    (bundle.deobfuscated.js:53522-53527); this port returns the new value
    instead of touching hidden state, matching every other function in this
    module -- the caller (`engine.enter_sub_map`) is responsible for
    persisting it back onto `RunState`."""

    map: GeneratedMap
    distortion_worlds_entered: int


def generate_sub_map(
    kind: str,
    map_index: int,
    parent_node_level: Optional[int],
    team_size: int,
    distortion_worlds_entered: int = 0,
    distortion_legendary_claimed: bool = False,
    gen4_mode: bool = True,
) -> SubMapResult:
    """Port of `generateSubMap` (bundle.deobfuscated.js:53508-53632) -- the
    small side-map (start -> 1-2 boss node(s) -> 2-3 reward node(s) ->
    subexit) generated when a player visits an UNDERGROUND/DISTORTION node.
    `kind` is `"underground"` or `"distortion"` -- `generateSubMap`'s own two
    real call sites (bundle.deobfuscated.js:77372/77375) never pass anything
    else (`rollSubMapBoss`'s "underground" fallback for an unrecognized kind
    is ported there for citation, not reachable from here).

    Node/edge topology (exact, NOT `_connect_layers`'s proportional
    distribution -- a genuinely different shape from the main map):
    `n0_0` (start) connects to EVERY boss node; EVERY boss node connects to
    EVERY reward node (a full bipartite graph, regardless of boss/reward
    count); EVERY reward node connects to `n3_0` (the single subexit).
    Boss count is 1 (ordinary underground/distortion) or 2 (underground
    always rolls exactly 2 trainers; distortion rolls a 2nd, wild-legendary
    boss node only on the legendary-eligible visit). Reward count is always
    exactly 3: 2 "real" reward nodes (`n2_0`/`n2_1`) plus one more,
    unconditionally appended with a hardcoded `"skip"` reward
    (bundle.deobfuscated.js:53610-53614).

    This function stays state-free: `parent_node_level` is the caller's
    already-computed `get_level_for_node(...)` for the node being entered
    FROM (see `_sub_map_base_level`), `team_size` is `len(state.team)` (for
    `pickSubMapRewards`'s "sacrifice" `minTeam` gate), and
    `distortion_worlds_entered`/`distortion_legendary_claimed` are the
    CALLER's current values of those `RunState` fields -- this function does
    the source's own +1 increment itself (matching the exact
    increment-then-check order, bundle.deobfuscated.js:53522-53527) and
    returns the incremented counter in `SubMapResult`, it does not mutate
    anything.

    RNG draw order (exact, load-bearing):
    - `"underground"`: `_roll_underground_trainers` THEN
      `_pick_sub_map_rewards`.
    - `"distortion"`: `_distortion_legendary` (0 or 1 draws) THEN
      `_roll_sub_map_boss` (1 draw) THEN `_pick_sub_map_rewards`.
    """
    entered = distortion_worlds_entered + 1 if kind == DISTORTION else distortion_worlds_entered

    legendary_entry: Optional["data.DistortionLegendaryEntry"] = None
    if kind == DISTORTION:
        legendary_entry = _distortion_legendary(entered, distortion_legendary_claimed)

    boss_nodes: list[MapNode] = []
    if kind == UNDERGROUND:
        trainers = _roll_underground_trainers(map_index, parent_node_level, gen4_mode=gen4_mode)
        for i, trainer in enumerate(trainers):
            boss_nodes.append(
                _make_node(
                    f"n1_{i}", BOSS, 1, i,
                    subBoss=kind, trainerKey=trainer["key"], bossName=trainer["name"], bossTeam=trainer["team"],
                )
            )
    else:
        name, sprite, team = _roll_sub_map_boss(DISTORTION, map_index, parent_node_level)
        if legendary_entry is not None:
            wild_level = min(100, _sub_map_base_level(DISTORTION, map_index, parent_node_level) + 5)
            boss_nodes.append(
                _make_node("n1_0", BOSS, 1, 0, subBoss=kind, bossName=name, bossSprite=sprite, bossTeam=team)
            )
            boss_nodes.append(
                _make_node(
                    "n1_1", BOSS, 1, 1,
                    subBoss=kind, wildBoss=True, bossName=legendary_entry.name, bossSprite=legendary_entry.sprite,
                    bossTeam=[{"species_id": legendary_entry.boss_id, "level": wild_level}],
                )
            )
        else:
            boss_nodes.append(
                _make_node("n1_0", BOSS, 1, 0, subBoss=kind, bossName=name, bossSprite=sprite, bossTeam=team)
            )

    reward_nodes: list[MapNode] = []
    if legendary_entry is not None:
        # bundle.deobfuscated.js:53585-53597: n2_0 is ALWAYS the legendary's
        # own guaranteed reward; n2_1 is a random pick, falling back to that
        # SAME legendary reward if the random pool came up empty.
        picked = _pick_sub_map_rewards(kind, team_size, count=1)
        second = picked[0] if picked else legendary_entry.reward
        reward_nodes.append(_make_node("n2_0", REWARD, 2, 0, reward=legendary_entry.reward))
        reward_nodes.append(_make_node("n2_1", REWARD, 2, 1, reward=second))
    else:
        picked = _pick_sub_map_rewards(kind, team_size, count=2)
        first = picked[0] if len(picked) > 0 else None
        second = picked[1] if len(picked) > 1 else first
        reward_nodes.append(_make_node("n2_0", REWARD, 2, 0, reward=first))
        reward_nodes.append(_make_node("n2_1", REWARD, 2, 1, reward=second))
    reward_nodes.append(_make_node(f"n2_{len(reward_nodes)}", REWARD, 2, len(reward_nodes), reward="skip"))

    subexit = _make_node("n3_0", SUBEXIT, 3, 0)
    start = _make_node("n0_0", START, 0, 0)

    layers = [[start], boss_nodes, reward_nodes, [subexit]]
    edges: list[tuple[str, str]] = []
    for boss in boss_nodes:
        edges.append((start.id, boss.id))
    for boss in boss_nodes:
        for reward in reward_nodes:
            edges.append((boss.id, reward.id))
    for reward in reward_nodes:
        edges.append((reward.id, subexit.id))

    nodes: dict[str, MapNode] = {node.id: node for layer in layers for node in layer}
    nodes["n0_0"].visited = True
    for src, dst in edges:
        if src == "n0_0":
            nodes[dst].accessible = True

    generated = GeneratedMap(nodes=nodes, edges=edges, layers=layers, map_index=map_index, is_sub_map=kind)
    return SubMapResult(map=generated, distortion_worlds_entered=entered)
