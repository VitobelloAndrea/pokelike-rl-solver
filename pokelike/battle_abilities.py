"""Port of `buildGen3AbilityConfig` (docs/logic-notes-gen3abilities.md,
bundle.deobfuscated.js:57143-58278) -- the Gen3-ability-analog system: one
ability id per SPECIES (not player-collected), assigned at first switch-in
via `GEN3_ABILITY_BY_SPECIES`/`GEN4_ABILITY_BY_SPECIES` (species lookup with
evolution-line-root fallback), symmetric between player and enemy. A
structurally SEPARATE system from the roguelike trait system in
`battle_traits.py`: abilities are keyed by species and work identically for
either side; traits are a list the PLAYER collects as meta-progression and
never apply to the enemy (docs/logic-notes-gen3abilities.md section 8's
comparison table).

Deliberately NOT replicated: the JS's one-time flavor-text log pushes
(`Bci`/`B2j` helpers). Every hook below is mechanics-only -- it mutates
`Combatant`/`BattleConfig` state and returns whatever value `calc_damage`/
`battle_loop.run_battle` need, nothing more (same convention as
`battle.py`'s `apply_stage_change`/`apply_status`, per CLAUDE.md's
"js/ui.js is reference-only").

**Validation approach**: cross-checked against
docs/logic-notes-gen3abilities.md (an exhaustive, chunk-by-chunk read of the
real source, not sampled) and spot-validated against the live bundle via
Node for a representative subset -- see
pokelike/tests/test_battle_abilities.py's module docstring for exactly
which mechanics got bit-for-bit Node validation vs. transcription-only.

**Flagged gaps** (documented rather than guessed past):
- `lucky_dance`'s Fisher-Yates shuffle is implemented as a standard
  Fisher-Yates (matching the idiom already confirmed elsewhere in this
  codebase, e.g. `getCatchChoices`'s shuffle per docs/logic-notes.md
  section 6.5) -- the exact loop direction at bundle.deobfuscated.js:57387
  was not individually re-read to confirm it matches this implementation
  bit-for-bit.
- `sand_veil` is ported (see `before_damage`) even though
  docs/logic-notes-gen3abilities.md section 7/9 confirms it is never
  assigned to any species in the current tables -- included for
  completeness, expect it to never fire in practice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional, Sequence

from pokelike import data, rng
from pokelike.battle import (
    BattleConfig,
    Combatant,
    DamageResult,
    HeldItem,
    MoveInstance,
    apply_stage_change,
    get_effective_stat,
    get_type_boost_item,
    stage_multiplier,
    uses_special_attack,
)

_STATS = ("atk", "def", "speed", "special", "spdef")
_BASE_STAT_ATTRS = {"atk": "atk", "def": "defense", "speed": "speed", "special": "special"}


def _base_stat(base_stats, stat: str) -> int:
    if stat == "spdef":
        return base_stats.spdef if base_stats.spdef is not None else base_stats.special
    return getattr(base_stats, _BASE_STAT_ATTRS[stat])


def _own_items(pokemon: Optional[Combatant]) -> list[HeldItem]:
    return [pokemon.held_item] if pokemon is not None and pokemon.held_item is not None else []


# ---------------------------------------------------------------------------
# Ability assignment -- bundle.deobfuscated.js:56855-57048, 86574-86589
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _evo_parent_map() -> dict[int, int]:
    """Port of `_getEvoParentMap` (bundle.deobfuscated.js:86574-86583):
    child species id -> parent (pre-evolution) species id."""
    parents: dict[int, int] = {}
    for species_id, evo in data.get_evolutions().items():
        parents[evo.into] = species_id
    for species_id, branches in data.get_branching_evolutions().items():
        for evo in branches:
            parents[evo.into] = species_id
    return parents


def get_evo_line_root(species_id: int) -> int:
    """Port of `getEvoLineRoot` (bundle.deobfuscated.js:86584-86589)."""
    parents = _evo_parent_map()
    current = species_id
    seen = set()
    while current in parents and current not in seen:
        seen.add(current)
        current = parents[current]
    return current


def get_gen3_ability(species_id: int, gen4_mode: bool = False) -> Optional[str]:
    """Port of `getGen3Ability` (bundle.deobfuscated.js:57034-57048).
    `gen4_mode` stands in for the source's `state.gen4Mode ||
    state.challengeGen4` (and NOT `state.gen3Mode`/`state.challengeGen3`)
    check -- passed explicitly since engine.py's state machine isn't built
    yet.
    """
    table = data.get_gen4_ability_by_species() if gen4_mode else data.get_gen3_ability_by_species()
    ability = table.get(species_id)
    if ability is None:
        ability = table.get(get_evo_line_root(species_id))
    return ability


def ability_id_of(pokemon: Optional[Combatant], gen4_mode: bool = False) -> Optional[str]:
    """Port of `abilityIdOf` (bundle.deobfuscated.js:57123-57126)."""
    if pokemon is None:
        return None
    return pokemon.gen3_ability or get_gen3_ability(pokemon.species_id, gen4_mode)


def team_has_species(team: Sequence[Combatant], species_id: int) -> bool:
    """Port of `teamHasSpecies` (bundle.deobfuscated.js:57133-57136) --
    does NOT check alive status."""
    return any(m.species_id == species_id for m in team)


def count_species(team: Sequence[Combatant], species_ids: Sequence[int], exclude: Combatant) -> int:
    """Port of `countSpecies` (bundle.deobfuscated.js:57137-57142)."""
    return sum(1 for m in team if m is not exclude and m.species_id in species_ids)


def first_alive(team: Sequence[Combatant]) -> Optional[tuple[Combatant, int]]:
    """Port of `firstAlive` (bundle.deobfuscated.js:57127-57132)."""
    for idx, member in enumerate(team):
        if member.current_hp > 0:
            return member, idx
    return None


# ---------------------------------------------------------------------------
# Static data tables ported alongside the ability logic
# ---------------------------------------------------------------------------

_GENERIC_SWITCH_IN_TABLE = {
    # ability id -> (stat, delta, applies_to_self)
    "quick_feet": ("speed", 1, True),
    "iron_body": ("def", 1, True),
    "inner_focus": ("spdef", 1, True),
    "strong_jaw": ("atk", 1, True),
    "competitive": ("special", 1, True),
    "storm_drain": ("special", 1, True),
    "fairy_charm": ("special", -1, False),
}

_WEATHER_SETTERS = {"drizzle": "rain", "drought": "sun", "snow_warning": "hail", "sand_stream": "sandstorm"}
_FORECAST_TYPES = {"rain": "Water", "sun": "Fire", "sandstorm": "Rock", "hail": "Ice"}

_REGI_FAMILY_IDS = frozenset({0x179, 0x17A, 0x17B, 0x1E6})  # Regirock, Regice, Registeel, Regigigas
_REGI_TIER_TABLE = {
    "regi_rock": (("def", 2),),
    "regi_ice": (("spdef", 2),),
    "regi_steel": (("def", 1), ("spdef", 1)),
}
_LAKE_GUARDIAN_IDS = frozenset({0x1E0, 0x1E1, 0x1E2})  # Uxie, Mesprit, Azelf
_MANAPHY_ID = 0x1EA
_MINUN_ID, _PLUSLE_ID = 0x138, 0x137
_LATIAS_ID, _LATIOS_ID = 0x17C, 0x17D
_LUNATONE_ID, _SOLROCK_ID = 0x151, 0x152

_ON_HIT_ABILITY_IDS = frozenset({"static", "flame_body", "effect_spore", "cute_charm", "poison_point"})


def _fisher_yates(items: list, rng_fn) -> None:
    for i in range(len(items) - 1, 0, -1):
        j = int(rng_fn() * (i + 1))
        items[i], items[j] = items[j], items[i]


@dataclass
class Gen3AbilityConfig:
    """One instance per battle. `gen4_mode` stands in for the source's
    `state.gen4Mode || state.challengeGen4` check (see `get_gen3_ability`).
    """

    gen4_mode: bool = False

    # -----------------------------------------------------------------
    # onSwitchIn -- bundle.deobfuscated.js:57349-57810 (dispatcher `Bcv`),
    # 57696-57702 (`_gen3Ability` assignment, `BcT`)
    # -----------------------------------------------------------------

    def on_switch_in(
        self,
        pokemon: Combatant,
        own_team: Sequence[Combatant],
        opponent: Optional[Combatant],
        opponent_team: Sequence[Combatant],
        battle_config: BattleConfig,
    ) -> None:
        ability = get_gen3_ability(pokemon.species_id, self.gen4_mode)
        pokemon.gen3_ability = ability
        if not ability:
            return
        if ability_id_of(opponent, self.gen4_mode) == "shield_dust" and ability != "shield_dust":
            return
        self._dispatch_switch_in(ability, pokemon, own_team, opponent, opponent_team, battle_config)

    def _dispatch_switch_in(
        self,
        ability: str,
        pokemon: Combatant,
        own_team: Sequence[Combatant],
        opponent: Optional[Combatant],
        opponent_team: Sequence[Combatant],
        battle_config: BattleConfig,
    ) -> None:
        generic = _GENERIC_SWITCH_IN_TABLE.get(ability)
        if generic is not None:
            stat, delta, is_self = generic
            if is_self:
                apply_stage_change(pokemon, stat, delta)
            elif opponent is not None and opponent.current_hp > 0:
                apply_stage_change(opponent, stat, delta)
            return

        if ability == "intimidate":
            if opponent is not None and opponent.current_hp > 0:
                apply_stage_change(opponent, "atk", -1)
            return
        if ability == "rattled":
            opp_types = {t.capitalize() for t in (opponent.types if opponent else ())}
            if opp_types & {"Ghost", "Dark", "Bug"}:
                apply_stage_change(pokemon, "speed", 2)
            return
        if ability == "lucky_dance":
            stats = list(_STATS)
            _fisher_yates(stats, rng.rng)
            for s in stats[:2]:
                apply_stage_change(pokemon, s, 1)
            return
        if ability == "hive_order":
            if rng.rng() < 0.5:
                apply_stage_change(pokemon, "def", 1)
                apply_stage_change(pokemon, "spdef", 1)
            else:
                apply_stage_change(pokemon, "special", 1)
            return
        if ability == "extreme_evoboost":
            primary = "special" if uses_special_attack(pokemon.species_id, pokemon.base_stats) else "atk"
            candidates = [s for s in _STATS if s != primary]
            lowest = min(candidates, key=lambda s: _base_stat(pokemon.base_stats, s))
            apply_stage_change(pokemon, lowest, 1)
            return
        if ability == "unaware":
            for s in _STATS:
                pokemon.stages[s] = 0
                if opponent is not None:
                    opponent.stages[s] = 0
            return
        if ability == "take_heart":
            if team_has_species(own_team, _MANAPHY_ID):
                for s in _STATS:
                    apply_stage_change(pokemon, s, 2)
            return
        if ability == "plus":
            if team_has_species(own_team, _MINUN_ID):
                for s in _STATS:
                    apply_stage_change(pokemon, s, 2)
            return
        if ability == "minus":
            if team_has_species(own_team, _PLUSLE_ID):
                for s in _STATS:
                    apply_stage_change(pokemon, s, 2)
            return
        if ability in _REGI_TIER_TABLE:
            for member in own_team:
                if member is pokemon or member.current_hp <= 0:
                    continue
                if member.species_id in _REGI_FAMILY_IDS:
                    for stat, delta in _REGI_TIER_TABLE[ability]:
                        apply_stage_change(member, stat, delta)
            return
        if ability == "eon_latias":
            if team_has_species(own_team, _LATIOS_ID):
                apply_stage_change(pokemon, "special", 2)
            return
        if ability == "eon_latios":
            if team_has_species(own_team, _LATIAS_ID):
                apply_stage_change(pokemon, "spdef", 2)
            return
        if ability == "lunatone_dawn":
            if team_has_species(own_team, _SOLROCK_ID) and not pokemon.flags.get("_statDoubled"):
                pokemon.flags["_statDoubled"] = True
                apply_stage_change(pokemon, "spdef", 6)
            return
        if ability == "solrock_sunrise":
            if team_has_species(own_team, _LUNATONE_ID) and not pokemon.flags.get("_statDoubled"):
                pokemon.flags["_statDoubled"] = True
                apply_stage_change(pokemon, "def", 6)
            return
        if ability == "download":
            if opponent is not None and opponent.current_hp > 0:
                opp_items = _own_items(opponent)
                opp_def = get_effective_stat(opponent, "def", opp_items, opponent.stages)
                opp_spdef = get_effective_stat(opponent, "spdef", opp_items, opponent.stages)
                apply_stage_change(pokemon, "atk" if opp_def < opp_spdef else "special", 1)
            return
        if ability == "mystical_power":
            count = count_species(own_team, _LAKE_GUARDIAN_IDS, pokemon)
            if count <= 0:
                return
            triggered = any(rng.rng() < 0.1 for _ in range(count))
            if not triggered:
                return
            for member in own_team:
                if member.current_hp > 0:
                    member.level = (member.level or 1) + 1
            return
        weather = _WEATHER_SETTERS.get(ability)
        if weather is not None:
            self._set_weather(battle_config, weather)
            return
        if ability == "trick_room":
            if not battle_config.trick_room:
                battle_config.trick_room = True
                battle_config.weather = None
            return
        if ability == "cloud_nine":
            battle_config.weather = None
            battle_config.trick_room = False
            return
        if ability == "trace":
            opp_ability = ability_id_of(opponent, self.gen4_mode)
            if opp_ability and opp_ability != "trace":
                pokemon.gen3_ability = opp_ability
                self._dispatch_switch_in(opp_ability, pokemon, own_team, opponent, opponent_team, battle_config)
            return
        if ability == "multitype":
            item_type = data.get_type_item_map()
            inv = {v: k for k, v in item_type.items()}
            resolved = inv.get(pokemon.held_item.id) if pokemon.held_item else None
            pokemon.types = (resolved.capitalize(),) if resolved else ("Normal",)
            return
        if ability == "forecast":
            self._apply_forecast(pokemon, battle_config)
            return
        if ability == "toxic_boost":
            self._apply_toxic_boost(pokemon, opponent)
            return
        if ability == "form_change":
            self._apply_form_change(pokemon, opponent)
            return
        # storm_drain's onSwitchIn is handled by the generic table above;
        # every other ability id has no onSwitchIn effect.

    def _set_weather(self, battle_config: BattleConfig, weather: str) -> None:
        if battle_config.weather == weather:
            return
        battle_config.weather = weather
        battle_config.trick_room = False

    def _apply_forecast(self, pokemon: Combatant, battle_config: BattleConfig) -> None:
        new_type = _FORECAST_TYPES.get(battle_config.weather, "Normal")
        if tuple(pokemon.types) != (new_type,):
            pokemon.types = (new_type,)

    def _apply_toxic_boost(self, pokemon: Combatant, opponent: Optional[Combatant]) -> None:
        if pokemon.flags.get("_toxicBoosted"):
            return
        if opponent is not None and opponent.poison_stacks > 0:
            pokemon.flags["_toxicBoosted"] = True
            apply_stage_change(pokemon, "atk", 5)

    def _apply_form_change(self, pokemon: Combatant, opponent: Optional[Combatant]) -> None:
        if opponent is None:
            return
        opp_items = _own_items(opponent)
        own_speed = get_effective_stat(pokemon, "speed", (), pokemon.stages)
        opp_speed = get_effective_stat(opponent, "speed", opp_items, opponent.stages)
        own_spdef = get_effective_stat(pokemon, "spdef", (), pokemon.stages)
        opp_spdef = get_effective_stat(opponent, "spdef", opp_items, opponent.stages)
        own_atk = get_effective_stat(pokemon, "atk", (), pokemon.stages)
        opp_atk = get_effective_stat(opponent, "atk", opp_items, opponent.stages)
        if own_speed < opp_speed:
            form = "speed"
        elif own_spdef < opp_spdef:
            form = "defense"
        elif own_atk < opp_atk:
            form = "attack"
        else:
            form = "normal"
        pokemon.base_stats = data.get_deoxys_forms()[form]

    # -----------------------------------------------------------------
    # modifySpeed -- bundle.deobfuscated.js:57817-57830
    # -----------------------------------------------------------------

    def modify_speed(self, pokemon: Combatant, speed: float, opponent: Optional[Combatant], battle_config: BattleConfig) -> float:
        ability = ability_id_of(pokemon, self.gen4_mode)
        if not ability or ability_id_of(opponent, self.gen4_mode) == "shield_dust":
            return speed
        if ability == "swift_swim" and battle_config.weather == "rain":
            return speed * 2
        if ability == "chlorophyll" and battle_config.weather == "sun":
            return speed * 2
        if ability == "mirror_coat":
            return float("inf") if battle_config.trick_room else 0.0
        return speed

    # -----------------------------------------------------------------
    # overrideMove -- bundle.deobfuscated.js:57831-57842
    # -----------------------------------------------------------------

    def override_move(self, pokemon: Combatant) -> Optional[MoveInstance]:
        if pokemon.flags.get("_forceStruggle"):
            return MoveInstance(power=50, type="Normal", name="Struggle", is_special=False, typeless=True)
        return None

    # -----------------------------------------------------------------
    # attackerDamageMod -- bundle.deobfuscated.js:57843-57874
    # -----------------------------------------------------------------

    def attacker_damage_mod(self, attacker: Combatant, defender: Combatant, damage: int) -> int:
        ability = ability_id_of(attacker, self.gen4_mode)
        if not ability or ability_id_of(defender, self.gen4_mode) == "shield_dust":
            return damage
        if ability in ("overgrow", "blaze", "torrent", "swarm"):
            if attacker.current_hp <= attacker.max_hp * 0.5:
                return int(damage * 1.5)
            return damage
        if ability == "pure_power":
            return int(damage * 1.5)
        if ability in ("tough_claws", "smash_head"):
            return int(damage * 1.3)
        return damage

    # -----------------------------------------------------------------
    # beforeDamage -- bundle.deobfuscated.js:57875-57934
    # -----------------------------------------------------------------

    def before_damage(self, defender: Combatant, attacker: Combatant, damage: int, battle_config: BattleConfig) -> int:
        ability = ability_id_of(defender, self.gen4_mode)
        if not ability or ability_id_of(attacker, self.gen4_mode) == "shield_dust":
            return damage
        last_move_type = (attacker.flags.get("_lastMoveType") or "").capitalize()
        if ability == "thick_fat":
            return int(damage * 0.5) if last_move_type in ("Fire", "Ice") else damage
        if ability == "levitate":
            if last_move_type == "Ground" and ability_id_of(attacker, self.gen4_mode) != "smack_down":
                return 0
            return damage
        if ability == "water_absorb":
            if last_move_type == "Water":
                heal = max(1, int(defender.max_hp * 0.25))
                defender.current_hp = min(defender.max_hp, defender.current_hp + heal)
                return 0
            return damage
        if ability == "storm_drain":
            return 0 if last_move_type == "Water" else damage
        if ability == "wall_head":
            return int(damage * 0.7)
        if ability == "sturdy":
            if not defender.flags.get("_sturdyUsed") and defender.current_hp >= defender.max_hp and damage >= defender.current_hp:
                defender.flags["_sturdyUsed"] = True
                return max(0, defender.current_hp - 1)
            return damage
        if ability == "sand_veil":
            return 0 if battle_config.weather == "sandstorm" and rng.rng() < 0.25 else damage
        if ability == "truant":
            return int(damage * 0.5)
        return damage

    # -----------------------------------------------------------------
    # whenAttacked -- bundle.deobfuscated.js:57935-57996 ("defender's
    # ability procs on the attacker")
    # -----------------------------------------------------------------

    def when_attacked(self, defender: Combatant, attacker: Combatant, damage: int) -> None:
        ability = ability_id_of(defender, self.gen4_mode)
        if not ability or ability_id_of(attacker, self.gen4_mode) == "shield_dust":
            return
        is_physical_hit = attacker.flags.get("_lastMoveIsSpecial") is False
        if ability == "mirror_coat":
            defender.flags["_mirrorLast"] = damage
            return
        if ability == "color_change":
            last_type = (attacker.flags.get("_lastMoveType") or "Normal").capitalize()
            if last_type and tuple(defender.types) != (last_type,):
                defender.types = (last_type,)
            return
        if ability == "cursed_body":
            if attacker.current_hp > 0 and not attacker.flags.get("_forceStruggle"):
                attacker.flags["_forceStruggle"] = True
            return
        if ability == "wall_head":
            if damage > 0 and attacker.current_hp > 0:
                recoil = max(1, int(damage * 0.1))
                attacker.current_hp = max(0, attacker.current_hp - recoil)
            return
        if not is_physical_hit or attacker.current_hp <= 0:
            return
        if ability == "rough_skin":
            recoil = max(1, int(attacker.max_hp * 0.2))
            attacker.current_hp = max(0, attacker.current_hp - recoil)
            return
        if ability in _ON_HIT_ABILITY_IDS:
            self._on_contact_proc(ability, attacker)

    # -----------------------------------------------------------------
    # afterAttack -- bundle.deobfuscated.js:57997-58080 ("attacker's own
    # ability procs on the target")
    # -----------------------------------------------------------------

    def after_attack(
        self,
        attacker: Combatant,
        defender: Combatant,
        damage: int,
        own_team: Sequence[Combatant],
        is_extra_attack: bool = False,
    ) -> None:
        ability = ability_id_of(attacker, self.gen4_mode)
        if not ability or ability_id_of(defender, self.gen4_mode) == "shield_dust":
            return
        if ability == "venom_strike":
            if defender.current_hp > 0:
                defender.poison_stacks = (defender.poison_stacks or 0) + 2
            return
        if ability == "smash_head":
            if damage > 0:
                recoil = max(1, int(damage * 0.1))
                attacker.current_hp = max(0, attacker.current_hp - recoil)
            return
        if ability == "bad_dreams":
            if defender.current_hp > 0 and not defender.status and rng.rng() < 0.2:
                defender.status = "sleep"
            return
        if ability == "serene_grace":
            if rng.rng() < 0.2:
                for member in own_team:
                    if member.current_hp > 0:
                        member.current_hp = member.max_hp
            return
        if attacker.flags.get("_lastMoveIsSpecial") is False:
            self._on_contact_proc(ability, defender)

    def _on_contact_proc(self, ability: str, target: Combatant) -> None:
        """Port of the shared on-contact ability switch `B6W`
        (bundle.deobfuscated.js:57252-57337) -- the mechanism behind both
        `whenAttacked` ("defender's ability procs on the attacker that just
        hit it") and `afterAttack` ("attacker's own ability procs on the
        target it just hit"), the latter a deliberate deviation from
        mainline (docs/logic-notes-runbattle.md section 4, section 9 item
        4) confirmed intentional-per-source, not "fixed" here.
        """
        if target.current_hp <= 0:
            return
        if ability == "static":
            if rng.rng() < 0.3 and not target.paralyzed:
                target.paralyzed = True
        elif ability == "flame_body":
            if rng.rng() < 0.2 and not target.burned:
                target.burned = True
        elif ability == "effect_spore":
            if rng.rng() < 0.3 and not target.status:
                target.status = "sleep"
        elif ability == "cute_charm":
            apply_stage_change(target, "atk", -1)
            apply_stage_change(target, "special", -1)
        elif ability == "poison_point":
            target.poison_stacks = (target.poison_stacks or 0) + 2

    # -----------------------------------------------------------------
    # beforeTurn -- bundle.deobfuscated.js:58081-58146
    # -----------------------------------------------------------------

    def before_turn(
        self,
        attacker: Combatant,
        defender: Optional[Combatant],
        counter_hit: Optional[list] = None,
    ) -> Optional[str]:
        """`counter_hit` is an optional observation sink, mirroring the log
        array the source's own hook receives and pushes into (`BcU`,
        bundle.deobfuscated.js:58116-58132). It is appended to only by the
        `mirror_coat` counter-hit, which is a real `type: "attack"` entry in
        the source's `detailedLog` and therefore belongs to the ordered
        attack family the route oracle compares. Callers that do not pass it
        get byte-identical behavior -- nothing here branches on it.
        """
        ability = ability_id_of(attacker, self.gen4_mode)
        if not ability or ability_id_of(defender, self.gen4_mode) == "shield_dust":
            return None
        if ability == "truant":
            attacker.flags["_truantActed"] = not attacker.flags.get("_truantActed")
            return "skip" if not attacker.flags["_truantActed"] else None
        if ability == "mirror_coat":
            stored = attacker.flags.get("_mirrorLast") or 0
            if stored > 0 and defender is not None and defender.current_hp > 0:
                counter = stored * 2
                before_hp = defender.current_hp
                defender.current_hp = max(0, defender.current_hp - counter)
                attacker.flags["_mirrorLast"] = 0
                if counter_hit is not None:
                    # The source's own constants at 58124-58130: the move is
                    # named "Mirror Coat", Psychic, never a crit, always
                    # special, `typeEff` a literal 1, and `damage` the CLAMPED
                    # delta (`Bcw - BcA.currentHp`) rather than `stored * 2`,
                    # which differ whenever the counter overkills.
                    counter_hit.append({
                        "move_name": "Mirror Coat",
                        "move_type": "Psychic",
                        "damage": before_hp - defender.current_hp,
                        "type_eff": 1,
                        "crit": False,
                        "is_special": True,
                    })
                return "skip"
            return None
        return None

    # -----------------------------------------------------------------
    # onBeforeAttack -- bundle.deobfuscated.js:58147-58186
    # -----------------------------------------------------------------

    def on_before_attack(self, attacker: Combatant, defender: Combatant) -> bool:
        if ability_id_of(defender, self.gen4_mode) != "own_tempo" or ability_id_of(attacker, self.gen4_mode) == "shield_dust":
            return False
        if rng.rng() < 0.2:
            self_damage = max(1, int(attacker.max_hp * 0.1))
            attacker.current_hp = max(0, attacker.current_hp - self_damage)
            return True
        return False

    # -----------------------------------------------------------------
    # endOfRound -- bundle.deobfuscated.js:58187-58228
    # -----------------------------------------------------------------

    def end_of_round(self, player_team: Sequence[Combatant], enemy_team: Sequence[Combatant], battle_config: BattleConfig) -> None:
        self._weather_chip_damage(player_team, enemy_team, battle_config)
        for side_team, opponent_team in ((player_team, enemy_team), (enemy_team, player_team)):
            active = first_alive(side_team)
            if active is None:
                continue
            pokemon = active[0]
            ability = ability_id_of(pokemon, self.gen4_mode)
            if not ability:
                continue
            opponent_active = first_alive(opponent_team)
            if opponent_active is not None and ability_id_of(opponent_active[0], self.gen4_mode) == "shield_dust":
                continue
            if ability == "speed_boost":
                apply_stage_change(pokemon, "speed", 1)
            elif ability == "rain_dish" and battle_config.weather == "rain":
                heal = max(1, int(pokemon.max_hp * 0.25))
                pokemon.current_hp = min(pokemon.max_hp, pokemon.current_hp + heal)
            elif ability == "ice_body" and battle_config.weather == "hail":
                heal = max(1, int(pokemon.max_hp * 0.25))
                pokemon.current_hp = min(pokemon.max_hp, pokemon.current_hp + heal)
            elif ability == "toxic_boost":
                opponent = opponent_active[0] if opponent_active else None
                self._apply_toxic_boost(pokemon, opponent)
            elif ability == "forecast":
                self._apply_forecast(pokemon, battle_config)

    def _weather_chip_damage(self, player_team: Sequence[Combatant], enemy_team: Sequence[Combatant], battle_config: BattleConfig) -> None:
        weather = battle_config.weather
        if weather not in ("sandstorm", "hail"):
            return
        immune_types = {"Rock", "Steel", "Ground"} if weather == "sandstorm" else {"Ice"}
        for team in (player_team, enemy_team):
            active = first_alive(team)
            if active is None:
                continue
            pokemon = active[0]
            if any(t.capitalize() in immune_types for t in pokemon.types):
                continue
            chip = max(1, int(pokemon.max_hp * 0.1))
            pokemon.current_hp = max(0, pokemon.current_hp - chip)

    # -----------------------------------------------------------------
    # onKO -- bundle.deobfuscated.js:58229-58244
    # -----------------------------------------------------------------

    def on_ko(self, fainted: Combatant, killer: Optional[Combatant]) -> None:
        if killer is None or killer.current_hp <= 0:
            return
        if ability_id_of(killer, self.gen4_mode) != "moxie":
            return
        if ability_id_of(fainted, self.gen4_mode) == "shield_dust":
            return
        apply_stage_change(killer, "atk", 1)

    # -----------------------------------------------------------------
    # onFaint -- bundle.deobfuscated.js:58245-58276
    # -----------------------------------------------------------------

    # Chimecho (dex 358) -- the sole holder of cleansing_bell -- is excluded
    # from its own revival sweep (docs/logic-notes-gen3abilities.md's own
    # transcription names this species "Mew"; cross-checked against
    # pokelike/data.py's real pokedex table, species 0x166 = 358 is
    # Chimecho, not Mew -- corrected here, flagging the discrepancy rather
    # than silently reproducing it).
    _CLEANSING_BELL_HOLDER_ID = 0x166

    def on_faint(self, fainted: Combatant, own_team: Sequence[Combatant]) -> None:
        if ability_id_of(fainted, self.gen4_mode) != "cleansing_bell":
            return
        revived_any = False
        for member in own_team:
            if member is fainted or member.species_id == self._CLEANSING_BELL_HOLDER_ID:
                continue
            if member.current_hp <= 0:
                member.current_hp = 1
                revived_any = True
        # revived_any is intentionally unused beyond mirroring the source's
        # own log-gating condition -- no event log in this port.
