"""Port of `buildTraitsConfig` (docs/logic-notes-traitsconfig.md,
bundle.deobfuscated.js:60733-63610) -- the roguelike trait/meta-progression
system. Structurally SEPARATE from `battle_abilities.Gen3AbilityConfig`
(species-keyed, symmetric): traits are a list the PLAYER collects
(`state.passives`), and (per docs/logic-notes-traitsconfig.md section 0)
`hasPassive`/`has_trait` only ever reads the PLAYER's own list -- there is
no enemy equivalent, which is why almost every named-trait check below is
explicitly `side == "player"`-gated.

**Two-layer architecture** (section 0): on top of the named trait list,
`compute_trait_tiers` derives an automatic per-type "tier" from team
composition alone (no player choice) -- Fire/Ground/Fairy/Flying/Normal/
Electric/Ghost/Grass/Ice/Poison/Rock/Water/Psychic/Steel/Dragon/Fighting/
Dark all have a coded tier bonus. Many named traits modify what an
automatic tier bonus does rather than being independent effects (e.g.
`fire_share` mirrors the Fire-tier ATK/SpA gain to teammates) -- called out
per-trait below.

**Traits NOT here, on purpose**: `poison_drain`, `normal_heal`, `sturdy`,
`rock_sturdy`, `fighting_survive`, `all_more`, `all_half`, `half_twice`,
`dragon_first_double`, `dragon_first_crit`, `team_damage_spread`,
`rand_start`, `sof_double`, `lead_speed`, `bug_relevel`, `bug_strip` are all
implemented directly inline in `runBattle`'s own body, not inside
`buildTraitsConfig` -- ported in `battle_loop.py`, not here (docs/logic-
notes-traitsconfig.md section 6.3).

**Confirmed-dead code, NOT ported** (docs/logic-notes-traitsconfig.md
section 6.1): a 19-flag cluster (Iron Plate, Heavy-Duty Boots, White Herb,
Razor Fang, Smoke Ball Haze, Adamant Orb one-shot, Rocky Helmet
bracing+2x-counter, Shell Bell thorns) whose gating flags were hardcoded
literals, never wired to a real trait id -- permanently unreachable in the
source itself. Also skipped: a dead second `rock_def_amp` check and a dead
duplicate `lvl_overpower` check inside this closure (the real effects of
both live in `calc_damage`/`battle_loop.py` respectively).

**`elec_lead`/`fairy_opening_volley`/`rock_explode` hook re-entry (fixed,
2026-07-29, see `tools/battle-oracle/README.md`'s matching section)**:
`elec_lead` and `fairy_opening_volley` (free bonus attacks at battle start,
`_apply_elec_lead`/`_apply_fairy_opening_volley` below) and `rock_explode`
(a KO-triggered splash attack, inside `on_ko` below) now re-invoke
`when_attacked`/`after_attack`/`on_ko` exactly where and how the source
does. The source itself never routes any of these three through
`Gen3AbilityConfig` at all -- `elec_lead`/`fairy_opening_volley` run inside
`onStartFight`, which JS dispatches via a real method call on this
`TraitsConfig` instance (`Gen3AbilityConfig` never defines `onStartFight`,
so `this` inside it is always bound to `iu`, i.e. this object, never the
merged wrapper), so every `this["whenAttacked"]`/`this["afterAttack"]`/
`this["onKO"]` reference in those two blocks is `self.when_attacked`/
`self.after_attack`/`self.on_ko` and NOTHING from `battle_abilities.py`.
`rock_explode` lives inside this class's own `on_ko`, already reached
through the existing `battle_loop._handle_faint`/`_status_tick_round`
wiring -- no new dispatch path was needed there, only the per-target
`calc_damage` fan-out fix (see `on_ko`'s inline comment). Full behavior
citations are in each function's own docstring below.

**Validation approach**: cross-checked against docs/logic-notes-
traitsconfig.md (an exhaustive, chunk-by-chunk read of the real source) and
spot-validated against the live bundle via Node for a representative
subset -- see pokelike/tests/test_battle_traits.py's module docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from pokelike import data, rng
from pokelike.battle import (
    BattleConfig,
    Combatant,
    HeldItem,
    MoveInstance,
    apply_stage_change,
    calc_damage,
    get_best_move,
    get_effective_stat,
    has_passive,
)

TRAIT_MAX_TIER = 12  # TRAIT_MAX_TIER, bundle.deobfuscated.js:58334

_STATS = ("atk", "def", "speed", "special", "spdef")


def _types_of(pokemon: Combatant) -> set:
    return {t.capitalize() for t in pokemon.types}


def _own_items(pokemon: Optional[Combatant]) -> list:
    return [pokemon.held_item] if pokemon is not None and pokemon.held_item is not None else []


def first_alive(team: Sequence[Combatant]):
    for idx, member in enumerate(team):
        if member.current_hp > 0:
            return member, idx
    return None


def compute_trait_tiers(team: Sequence[Combatant], base_tier: int = 0, traits: Sequence = ()) -> dict[str, int]:
    """Port of `computeTraitTiers` (bundle.deobfuscated.js:60697-60732)."""
    team_reroll = has_passive(traits, "team_reroll")
    legend_traits = has_passive(traits, "legend_traits")
    shiny_first = has_passive(traits, "shiny_first")
    legendary_ids = data.get_legendary_ids()

    weights: dict[str, int] = {}
    for i, member in enumerate(team):
        is_shiny = member.is_shiny or (shiny_first and i == 0)
        is_legendary = legend_traits and member.species_id in legendary_ids
        weight = 1 + (1 if is_shiny else 0) + (1 if team_reroll else 0) + (1 if is_legendary else 0)
        for t in _types_of(member):
            weights[t] = weights.get(t, 0) + weight

    tiers: dict[str, int] = {}
    for type_name, w in weights.items():
        if w == 0:
            continue
        tier = min(TRAIT_MAX_TIER, w // 2 + base_tier)
        if tier > 0:
            tiers[type_name] = tier
    return tiers


@dataclass
class TraitsConfig:
    """One instance per battle, constructed like `buildTraitsConfig(ip, iS,
    traits)`: `player_tiers`/`enemy_tiers` from `compute_trait_tiers`,
    `traits` the player's collected trait list. `dark_crit_floor` is
    computed once here and read directly by `calc_damage`
    (docs/logic-notes.md 7.5) -- pass `traits_config.dark_crit_floor` into
    whatever `BattleConfig` your battle loop uses, or read it off the
    `BattleConfig` if `battle_loop.run_battle` has already copied it there.
    """

    player_tiers: dict[str, int] = field(default_factory=dict)
    enemy_tiers: dict[str, int] = field(default_factory=dict)
    traits: Sequence = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.dark_crit_floor = 0.25 * self._tier("Dark", "player")

    # -----------------------------------------------------------------
    # Internal lookup primitives -- bundle.deobfuscated.js:60751-60757
    # -----------------------------------------------------------------

    def _tier(self, type_name: str, side: str) -> int:
        tiers = self.player_tiers if side == "player" else self.enemy_tiers
        return tiers.get(type_name, 0)

    def _has_tier(self, type_name: str, side: str) -> bool:
        return self._tier(type_name, side) >= 1

    def _has_trait(self, trait_id: str) -> bool:
        return has_passive(self.traits, trait_id)

    # -----------------------------------------------------------------
    # Shared mirror/rally helpers -- bundle.deobfuscated.js:60830-60930
    # -----------------------------------------------------------------

    def _mirror_fire_share(self, team: Sequence[Combatant], source: Combatant, stat: str, tier_amount: int) -> None:
        """`BIT`, bundle.deobfuscated.js:60857-60872 (`fire_share`)."""
        if tier_amount <= 0 or not self._has_trait("fire_share") or "Fire" not in _types_of(source):
            return
        base = tier_amount * 0.5
        whole = int(base)
        for member in team:
            if member is source or member.current_hp <= 0 or "Fire" not in _types_of(member):
                continue
            amount = whole
            if rng.rng() < base - whole:
                amount += 1
            if amount:
                apply_stage_change(member, stat, amount)

    def _mirror_atk(self, enemy_team: Sequence[Combatant], stat: str, tier_amount: int) -> None:
        """`BIF`, bundle.deobfuscated.js:60873-60878 (`mirror_atk`)."""
        if not self._has_trait("mirror_atk"):
            return
        active = first_alive(enemy_team)
        if active is not None:
            apply_stage_change(active[0], stat, -tier_amount)

    def mirror_enemy_active_debuff(self, player_team: Sequence[Combatant], stat: str, amount: int) -> None:
        """`BIq`, bundle.deobfuscated.js:60879-60890
        (`debuff_mirror_buff`/`water_mirror_stage`).

        The source's two branches are a comma expression, so they are gated
        **independently**: `BcU` (`debuff_mirror_buff`) grants `amount` for
        every stat, while `BcA` (`water_mirror_stage`) grants `amount * 2`
        on its own -- but only for `atk`/`special` and only when the first
        living player member is Water-type. With both traits on a Water
        active and an eligible stat the source therefore performs two
        sequential `applyStageChange` calls totalling `3 * amount` before
        caps. The helper consumes no RNG draw.
        """
        if amount <= 0:
            return
        active = first_alive(player_team)
        if active is None:
            return
        lead = active[0]
        if self._has_trait("debuff_mirror_buff"):
            apply_stage_change(lead, stat, amount)
        if self._has_trait("water_mirror_stage") and stat in ("atk", "special") and "Water" in _types_of(lead):
            apply_stage_change(lead, stat, amount * 2)

    def trigger_fighting_rally(self, side: str, player_team: Sequence[Combatant], enemy_team: Sequence[Combatant]) -> None:
        """`BIh`, bundle.deobfuscated.js:60891-60916."""
        if not self._has_tier("Fighting", side):
            return
        team = player_team if side == "player" else enemy_team
        tier = self._tier("Fighting", side)
        for member in team:
            if member.current_hp > 0:
                apply_stage_change(member, "atk", tier)
                apply_stage_change(member, "special", tier)
        if side == "player":
            self._mirror_atk(enemy_team, "atk", tier)
            self._mirror_atk(enemy_team, "special", tier)

    def sweep_faint_rallies(self, player_team: Sequence[Combatant], enemy_team: Sequence[Combatant], battle_config: BattleConfig) -> None:
        """`BIL`, bundle.deobfuscated.js:60917-60930."""
        for side, team in (("player", player_team), ("enemy", enemy_team)):
            for idx, member in enumerate(team):
                key = (side, idx)
                if member.current_hp <= 0 and key not in battle_config.rallied_faints:
                    battle_config.rallied_faints.add(key)
                    self.trigger_fighting_rally(side, player_team, enemy_team)

    def sweep_kos(self, player_team: Sequence[Combatant], enemy_team: Sequence[Combatant], battle_config: BattleConfig) -> None:
        """Line 60939-60948: re-runs `on_ko` for every currently-fainted
        Pokemon not yet de-duplicated via `battle_config.kos_handled`."""
        for side, team in (("player", player_team), ("enemy", enemy_team)):
            for idx, member in enumerate(team):
                if member.current_hp <= 0:
                    self.on_ko(member, side, idx, None, None, None, player_team, enemy_team, battle_config)

    def _ghost_execute_heal(self, attacker: Optional[Combatant], victim_max_hp: int, ghost_tier: int, side: str) -> None:
        """`BIV`, bundle.deobfuscated.js:60830-60855 (`ghost_heal`)."""
        if side != "player" or ghost_tier < 4 or self._has_trait("no_heal_revive"):
            return
        if attacker is None or attacker.current_hp <= 0:
            return
        missing = attacker.max_hp - attacker.current_hp
        heal = min(int(victim_max_hp * 0.1 * ghost_tier), missing)
        if heal > 0:
            attacker.current_hp += heal

    # -----------------------------------------------------------------
    # onStartFight -- bundle.deobfuscated.js:60952-61424
    # -----------------------------------------------------------------

    def on_start_fight(
        self,
        player_team: Sequence[Combatant],
        enemy_team: Sequence[Combatant],
        battle_config: BattleConfig,
        *,
        second_call: bool = False,
    ) -> None:
        battle_config.last_player_team = list(player_team)

        if not second_call:
            self._seed_persistent_bonuses(player_team)

        sides = [("player", player_team, enemy_team)]
        if not second_call:
            sides.append(("enemy", enemy_team, player_team))

        for side, team, opp_team in sides:
            members = [(0, team[0])] if (second_call and team) else list(enumerate(team))
            self._start_fight_type_tiers(side, team, opp_team, members)

        if second_call:
            return

        if self._has_trait("grass_bulk"):
            for member in player_team:
                if member.current_hp > 0 and "Grass" in _types_of(member):
                    new_max = member.max_hp * 2
                    member.current_hp = int(new_max * 0.5)
                    member.max_hp = new_max
        if self._has_trait("fire_start_atk"):
            for member in player_team:
                if member.current_hp > 0 and "Fire" in _types_of(member):
                    apply_stage_change(member, "atk", 2)
                    apply_stage_change(member, "special", 2)
        if self._has_trait("speed_start"):
            for member in player_team:
                if member.current_hp > 0:
                    apply_stage_change(member, "speed", 1)

        self._apply_elec_lead(player_team, enemy_team, battle_config)

        if not battle_config.team_hp_pool_done and self._has_trait("team_hp_pool"):
            battle_config.team_hp_pool_done = True
            total = sum(max(0, m.max_hp) for m in player_team)
            bonus = int(total * 0.5)
            if player_team:
                player_team[0].max_hp += bonus
                player_team[0].current_hp += bonus

        if not battle_config.revive_done and self._has_trait("no_heal_revive"):
            battle_config.revive_done = True
            for member in player_team:
                if member.current_hp <= 0:
                    member.current_hp = 1

        if not battle_config.fairy_volley_fired and self._has_trait("fairy_opening_volley"):
            battle_config.fairy_volley_fired = True
            self._apply_fairy_opening_volley(player_team, enemy_team, battle_config)

    def _seed_persistent_bonuses(self, player_team: Sequence[Combatant]) -> None:
        """Port of the `_runSpeedStage`/`_runMaxHp` carry-over loop at the
        very top of `onStartFight`, before any per-type tier logic
        (bundle.deobfuscated.js:60966-60992) -- CODEX.md issue 8. Both
        fields are cross-battle-persistent (see `battle_loop._init_battle_
        state`'s docstring); this is where that persistence actually
        becomes a battle effect: `_runSpeedStage` re-applies as a real
        `+N speed` stage every fight (`initBattleState` unconditionally
        zeroes `stages`, so without this the stage bonus would otherwise
        only ever exist for the one battle it was earned in), and
        `_runMaxHp` re-grows `max_hp`/`current_hp` by the same delta if
        `max_hp` doesn't already reflect it (normally a no-op in this port,
        since Python's `Combatant` is the persistent object and `ko_maxhp`
        already mutated `max_hp` directly -- this exists mainly for the
        edge case of an evolution/level-gain HP recompute in between fights
        that didn't fold `_runMaxHp` back in, see `engine._apply_level_gain`
        for the half of this fix that keeps that recompute honest).
        """
        from pokelike.map_gen import calc_hp

        for member in player_team:
            speed_stage = member.flags.get("_runSpeedStage", 0)
            if speed_stage > 0:
                apply_stage_change(member, "speed", speed_stage)
            run_max_hp = member.flags.get("_runMaxHp", 0)
            if run_max_hp > 0:
                hp_buff = (member.stat_buffs or {}).get("hp", 0)
                target_max_hp = int(calc_hp(member.base_stats.hp, member.level) * (1 + 0.05 * hp_buff)) + run_max_hp
                if member.max_hp < target_max_hp:
                    delta = target_max_hp - member.max_hp
                    member.max_hp += delta
                    member.current_hp += delta

    def _start_fight_type_tiers(self, side, team, opp_team, members) -> None:
        tier_fire = self._tier("Fire", side)
        if tier_fire >= 1:
            for _, member in members:
                if member.current_hp > 0 and "Fire" in _types_of(member):
                    apply_stage_change(member, "atk", tier_fire)
                    apply_stage_change(member, "special", tier_fire)
                    if side == "player":
                        self._mirror_fire_share(team, member, "atk", tier_fire)
                        self._mirror_fire_share(team, member, "special", tier_fire)
                        self._mirror_atk(opp_team, "atk", tier_fire)
                        self._mirror_atk(opp_team, "special", tier_fire)

        tier_ground = self._tier("Ground", side)
        if tier_ground >= 1:
            for enemy in opp_team:
                if enemy.current_hp > 0:
                    apply_stage_change(enemy, "speed", -tier_ground)
            if side == "player":
                self.mirror_enemy_active_debuff(team, "speed", tier_ground)

        tier_fairy = self._tier("Fairy", side)
        if tier_fairy >= 1:
            for enemy in opp_team:
                if enemy.current_hp > 0:
                    apply_stage_change(enemy, "atk", -tier_fairy)
                    apply_stage_change(enemy, "special", -tier_fairy)
            if side == "player":
                self.mirror_enemy_active_debuff(team, "atk", tier_fairy)
                self.mirror_enemy_active_debuff(team, "special", tier_fairy)

        tier_flying = self._tier("Flying", side)
        if tier_flying >= 1:
            for _, member in members:
                if member.current_hp > 0 and "Flying" in _types_of(member):
                    apply_stage_change(member, "speed", tier_flying)

        tier_normal = self._tier("Normal", side)
        if tier_normal >= 1:
            frac = 0.25 * tier_normal
            for _, member in members:
                if member.current_hp > 0 and "Normal" in _types_of(member):
                    bonus = int(member.max_hp * frac)
                    member.max_hp += bonus
                    member.current_hp += bonus

    def _apply_elec_lead(self, player_team, enemy_team, battle_config: BattleConfig) -> None:
        """Port of the `elec_lead` block (bundle.deobfuscated.js:61187-
        61292). Called from `onStartFight`, itself dispatched through
        `mergeBattleConfigs`'s generic (non-threaded) hook wrapper -- but
        since `Gen3AbilityConfig` never defines `onStartFight`, the wrapper's
        `typeof B2n=="function" && O[...]` branch is always false and only
        `iu["onStartFight"](...)` (`iu` = this `TraitsConfig` instance) ever
        actually runs. That is a real JS method call on `iu`, so `this`
        inside `onStartFight`'s body -- and therefore every `this["whenAttacked"]`/
        `this["afterAttack"]`/`this["onKO"]` reference below -- is bound to
        `iu` itself, NEVER to the merged wrapper and NEVER to
        `Gen3AbilityConfig`. `ability_config`'s own `when_attacked`/
        `after_attack`/`on_ko` are therefore never invoked for this hit, in
        or out of a merged Gen3/4 battle -- only this object's own methods
        (`self.when_attacked`/`self.after_attack`/`self.on_ko` below).

        `calcDamage`'s battle_config-like final arg is a literal `null` at
        this call site (bundle.deobfuscated.js:61212), not the real
        `battleConfig` -- so this extra hit gets no weather bonus and no
        `darkCritFloor` crit-chance bump even in a battle where those would
        normally apply to the main hit (`calc_damage` already treats
        `battle_config=None` exactly like the source treats a `null` final
        arg, see `battle.calc_damage`'s own `battle_config is not None`
        guards).

        Unlike the source's normal attack chain, there is no `move.no_damage`
        check here (or in `_apply_fairy_opening_volley`/the `rock_explode`
        block in `on_ko` below) -- the source calls `calcDamage` unconditionally.
        Unreachable with real Pokedex data (no Electric/Fairy/Rock species'
        best move is ever Splash/Teleport), so this is a documented fidelity
        note, not a functional change.

        All FOUR consequences of a fatal hit are independent JS comma-expression
        statements, not an if/else: a fatal hit still fires `afterAttack`
        (`actual_damage > 0` is enough, regardless of whether the target is
        now at 0 HP) AND `onKO` (gated on `target.current_hp == 0`) --
        contrast `_apply_fairy_opening_volley`'s ternary, which skips
        `afterAttack` entirely on a fatal hit and never calls `onKO` at all.
        There is no `onFaint` call here either way -- `TraitsConfig` has no
        `onFaint` method in the source, so `this["onFaint"]` would be
        `undefined` even if referenced, and the source simply never
        references it in this block.
        """
        if battle_config.elec_lead_fired or not self._has_trait("elec_lead"):
            return
        battle_config.elec_lead_fired = True
        attacker = None
        attacker_idx = None
        for idx, member in enumerate(player_team):
            if member.current_hp > 0 and "Electric" in _types_of(member):
                attacker = member
                attacker_idx = idx
                break
        target_pair = first_alive(enemy_team)
        if attacker is None or target_pair is None:
            return
        target, target_idx = target_pair
        move = get_best_move(
            attacker.types, attacker.base_stats, attacker.species_id, attacker.move_tier, attacker.held_item,
            has_multitype=attacker.gen3_ability == "multitype",
        )
        result = calc_damage(attacker, target, move, _own_items(attacker), _own_items(target), self.traits, "player", None)
        pre_hp = target.current_hp
        target.current_hp = max(0, target.current_hp - result.damage)
        actual_damage = pre_hp - target.current_hp

        if actual_damage > 0:
            self.when_attacked(target, "enemy", attacker, actual_damage)
            self.after_attack(attacker, "player", target, "enemy", actual_damage, player_team, enemy_team, is_extra_attack=True)
        if target.current_hp == 0:
            self.on_ko(target, "enemy", target_idx, attacker, "player", attacker_idx, player_team, enemy_team, battle_config)

    def _apply_fairy_opening_volley(self, player_team, enemy_team, battle_config: BattleConfig) -> None:
        """Port of the `fairy_opening_volley` block (bundle.deobfuscated.js:
        61336-61419) -- also dispatched through `onStartFight`'s `this`
        binding (see `_apply_elec_lead`'s docstring), so its `afterAttack`
        call below is `self.after_attack`, never `ability_config.after_attack`.

        Genuinely MULTI-attacker, unlike `elec_lead`'s single lead: the
        source loops over every alive player Fairy-type member (not just
        the team's active lead), gated by a single battle-wide
        `fairyVolleyFired` flag checked once by the caller
        (`on_start_fight`), not reset per-attacker. Each iteration
        re-finds the first alive enemy fresh (`BIs["findIndex"](...)`) --
        an earlier Fairy in this same loop that just KO'd the previous
        target changes who the NEXT Fairy hits.

        Unlike `elec_lead`, a fatal hit here is a ternary, not independent
        statements (bundle.deobfuscated.js:61398-61405): if the target's
        `current_hp` reaches 0, only a `faint` log entry is pushed and
        `afterAttack` is skipped entirely -- no hook re-entry of any kind
        on a kill, and no `onKO` call ever, fatal or not (the source
        simply never references `this["onKO"]` in this block).
        """
        for member in player_team:
            if member.current_hp <= 0 or "Fairy" not in _types_of(member):
                continue
            target_pair = first_alive(enemy_team)
            if target_pair is None:
                break
            target, _target_idx = target_pair
            move = get_best_move(
                member.types, member.base_stats, member.species_id, member.move_tier, member.held_item,
                has_multitype=member.gen3_ability == "multitype",
            )
            result = calc_damage(member, target, move, _own_items(member), _own_items(target), self.traits, "player", None)
            damage = min(target.current_hp, max(1, int(result.damage * 0.1)))
            target.current_hp = max(0, target.current_hp - damage)
            if target.current_hp == 0:
                continue
            if damage > 0:
                self.after_attack(member, "player", target, "enemy", damage, player_team, enemy_team, is_extra_attack=True)

    # -----------------------------------------------------------------
    # afterAttack -- bundle.deobfuscated.js:61428-62089
    # -----------------------------------------------------------------

    def after_attack(
        self,
        attacker: Combatant,
        side: str,
        target: Combatant,
        target_side: str,
        damage: int,
        own_team: Sequence[Combatant],
        opposing_team: Sequence[Combatant],
        is_extra_attack: bool = False,
    ) -> None:
        """`own_team`/`opposing_team` are the ATTACKER's own roster and the
        roster it just hit (matching the JS signature's trailing
        `playerTeam, enemyTeam` args, bundle.deobfuscated.js:61428) --
        needed by `ghost_curse` (pick 2 random OTHER survivors on the
        executed side), `dark_splash`/`fairy_hit_two`/psychic splash (hit
        other members of the opposing roster), and `grass_burst` (hit
        every member of the opposing roster).
        """
        if damage <= 0:
            return

        loop_count = 1
        if not is_extra_attack and side == "player":
            electric_double = self._has_trait("elec_double") and "Electric" in _types_of(attacker)
            if electric_double or self._has_trait("ice_onhit_double"):
                loop_count = 2

        for _ in range(loop_count):
            self._after_attack_once(attacker, side, target, target_side, damage, own_team, opposing_team, is_extra_attack)

    def _after_attack_once(self, attacker, side, target, target_side, damage, own_team, opposing_team, is_extra_attack: bool = False) -> None:
        opposing = target_side != side
        # `poison_onhit`/`ground_slow_onhit`/`elec_chain`/`elec_paralyze` are
        # all inside the JS `&&` chain gated on `target.currentHp > 0`
        # (bundle.deobfuscated.js:61441-61444) -- a fatal hit (main OR extra,
        # e.g. `_apply_elec_lead`'s own kill) must NOT trigger any of these,
        # a gate this port was previously missing entirely (found via
        # tools/battle-oracle/fixtures/elec_lead_fatal.json: Pikachu's final
        # speed stage doubled without it, since every subsequent connecting
        # hit -- fatal or not -- was wrongly re-triggering elec_chain).
        if side == "player" and opposing and target.current_hp > 0:
            if self._has_trait("poison_onhit"):
                target.poison_stacks = (target.poison_stacks or 0) + 2
            if self._has_trait("ground_slow_onhit"):
                apply_stage_change(target, "speed", -1)
                self.mirror_enemy_active_debuff(own_team, "speed", 1)
            if "Electric" in _types_of(attacker) and self._has_trait("elec_chain"):
                current = attacker.flags.get("_runSpeedStage", 0)
                if current < 6:
                    attacker.flags["_runSpeedStage"] = current + 1
                    apply_stage_change(attacker, "speed", 1)
            if "Electric" in _types_of(attacker) and self._has_trait("elec_paralyze"):
                if rng.rng() < 0.2:
                    target.paralyzed = True

        ghost_tier = self._tier("Ghost", side)
        # Same `target.currentHp > 0` gate (bundle.deobfuscated.js:61486:
        # `B2e("Ghost", BIY) && BIH !== BIY && BIz["currentHp"] > 0x0`) --
        # independent of the poison_onhit cluster above, but the same fix.
        if ghost_tier >= 1 and opposing and target.current_hp > 0:
            threshold = min(0.5, 0.15 * ghost_tier)
            if target.max_hp > 0 and target.current_hp / target.max_hp < threshold:
                victim_max_hp = target.max_hp
                pre_execute_hp = target.current_hp
                target.current_hp = 0
                self._ghost_execute_heal(attacker, victim_max_hp, ghost_tier, side)
                # "Spooky Plate" heal (bundle.deobfuscated.js:61511-61521) --
                # a SEPARATE heal from the tier-threshold one above, gated on
                # the named `ghost_heal` trait itself (not a tier), healing
                # the attacker up to the VICTIM's pre-execute HP rather than
                # a percentage of its max HP. CODEX.md issue 7: this port
                # previously only had the tier-threshold heal.
                if side == "player" and self._has_trait("ghost_heal") and not self._has_trait("no_heal_revive") and attacker.current_hp > 0:
                    heal = min(pre_execute_hp, attacker.max_hp - attacker.current_hp)
                    if heal > 0:
                        attacker.current_hp += heal
                if side == "player" and self._has_trait("ghost_curse"):
                    self.apply_ghost_curse(opposing_team, target)

        if self._has_trait("dark_splash") and side == "player" and "Dark" in _types_of(attacker):
            # `_lastOverkill` is recorded on the TARGET by `run_battle` right
            # after applying damage (bundle.deobfuscated.js:55901-55902 sets
            # it on the defender, `BEf`, not the attacker) -- a confirmed
            # defect fixed here (CODEX.md issue 7): this used to read
            # `attacker.flags`, where nothing ever wrote it, so dark_splash
            # could never fire.
            overkill = target.flags.pop("_lastOverkill", 0)
            if overkill:
                other = next((m for m in opposing_team if m is not target and m.current_hp > 0), None)
                if other is not None:
                    other.current_hp = max(0, other.current_hp - overkill)

        grass_tier = self._tier("Grass", side)
        # `!(side==="player" && no_heal_revive)` (bundle.deobfuscated.js:
        # 61573-61576) -- a gate this port was missing entirely (found while
        # tracing CODEX.md issue 13's Psychic-splash nested-call chain): the
        # Grass-tier heal-back is blocked, like every other named/tiered
        # heal-back in this closure, once `no_heal_revive` is active. Also
        # `heal_boost`'s ×2 only applies when `side==="player"`
        # (`BIY==="player" ? Bcv : 1`, line 61580) -- previously applied
        # regardless of side.
        if grass_tier >= 1 and attacker.current_hp > 0 and not (side == "player" and self._has_trait("no_heal_revive")):
            heal_mult = 2 if (side == "player" and self._has_trait("heal_boost")) else 1
            heal = max(1, int(damage * 0.15 * grass_tier * heal_mult))
            heal = min(heal, attacker.max_hp - attacker.current_hp)
            if heal > 0:
                attacker.current_hp += heal
                if side == "player" and self._has_trait("grass_burst"):
                    for enemy in opposing_team:
                        if enemy.current_hp > 0:
                            enemy.current_hp = max(0, enemy.current_hp - heal)
                if side == "player" and self._has_trait("heal_boost_stat"):
                    count = attacker.flags.get("_aspearCount", 0)
                    if count < 6:
                        attacker.flags["_aspearCount"] = count + 1
                        stat = _STATS[int(rng.rng() * len(_STATS))]
                        apply_stage_change(attacker, stat, 1)

        ice_tier = self._tier("Ice", side)
        if ice_tier >= 1 and opposing:
            if target.status == "freeze":
                shatter = max(1, int(target.max_hp * 0.08 * ice_tier))
                if self._has_trait("ice_shatter_double"):
                    shatter *= 2
                target.current_hp = max(0, target.current_hp - shatter)
                target.status = None
                if target.current_hp > 0 and self._has_trait("ice_refreeze"):
                    if rng.rng() < min(1.0, 0.12 * ice_tier):
                        target.status = "freeze"
            else:
                chance = min(1.0, 0.12 * ice_tier)
                if self._has_trait("ice_freeze_chance"):
                    chance *= 1.5
                if rng.rng() < chance:
                    target.status = "freeze"

        poison_tier = self._tier("Poison", side)
        # N55: the source's Poison entry condition is
        # `B2e("Poison", BIY) && BIH !== BIY && BIz["currentHp"] > 0x0`
        # (bundle.deobfuscated.js:61734), where `BIz` is the TARGET -- the
        # `afterAttack(BIi, BIs, BIY, BIz, BIA, BIH, ...)` signature at line
        # 61428 binds it to the 4th argument, and the main-hit call site at
        # 56004 passes the defender there (55993-55994's `attackerHpAfter:
        # BEX`/`targetHpAfter: BEf` fix the same two objects independently of
        # their names). This port omitted the target-alive gate, so a killing
        # blow added stacks to an already-fainted target. Unlike the Rock and
        # Water blocks this one draws no RNG, so the defect is state-only: the
        # stream stays aligned and only `_poisonStacks` on the corpse diverges.
        # The gate belongs at this block's own entry, NOT as an early return --
        # the Rock block below gates on the ATTACKER instead and must still run
        # for a live attacker that has just killed its target. The
        # `poison_double` amount logic stays inside the same gate, exactly as
        # the source nests it at 61737-61741.
        if poison_tier >= 1 and opposing and target.current_hp > 0:
            amount = poison_tier
            if self._has_trait("poison_double") and "Poison" in _types_of(attacker) and (target.poison_stacks or 0) > 0:
                amount *= 2
            target.poison_stacks = (target.poison_stacks or 0) + amount

        rock_tier = self._tier("Rock", side)
        # N56: the source's Rock entry condition is
        # `B2e("Rock", BIY) && BIi["currentHp"] > 0x0`
        # (bundle.deobfuscated.js:61770). Two things distinguish it from its
        # Poison and Water neighbours. First, it gates on `BIi` -- the
        # ATTACKER, the 1st argument, which is the object Rock buffs -- not on
        # the target `BIz`. Second, it carries NO `BIH !== BIY` term: the
        # source really does let this block run on a same-side hit, so no
        # opposing-side condition may be copied over from Poison/Water.
        # This port omitted the attacker-alive gate, so a dead attacker still
        # consumed the proc `rng()` draw at 61775 and, at a chance of 1.0,
        # buffed its own corpse -- the same RNG-desynchronising severity class
        # as N54. The check must precede the draw below, since the source
        # evaluates it in the enclosing `if` before reaching `rng() < BEa`.
        # This is reachable on the real main-hit path: `whenAttacked` runs at
        # 55998 BEFORE `afterAttack` at 56000-56004, and Gen3 `rough_skin`
        # (57980-57992) subtracts 20% of the attacker's max HP on a physical
        # contact hit, so an attacker can be dead by the time this block is
        # entered while `afterAttack` still fires on positive dealt damage.
        # Like N55 this gates only THIS block, not the function: the Water
        # block below has its own independent target-alive gate and must still
        # run when the attacker is the one that died.
        if rock_tier >= 1 and attacker.current_hp > 0:
            if rng.rng() < min(1.0, rock_tier / 3):
                apply_stage_change(attacker, "def", rock_tier)
                apply_stage_change(attacker, "spdef", rock_tier)

        water_tier = self._tier("Water", side)
        # N54: the source's Water entry condition is
        # `B2e("Water", BIY) && BIH !== BIY && BIz["currentHp"] > 0x0`
        # (bundle.deobfuscated.js:61791), where `BIz` is the TARGET -- the
        # `afterAttack(BIi, BIs, BIY, BIz, BIA, BIH, ...)` signature at line
        # 61428 binds it to the 4th argument, and the call site at 61406-61417
        # passes the defender there. The Rock block immediately above gates on
        # `BIi["currentHp"]` (the ATTACKER, since Rock buffs itself), which is
        # what makes the two objects distinguishable at this line. This port
        # omitted the target-alive gate, so a fatal hit consumed one extra
        # proc RNG draw and could debuff an already-fainted target and, via
        # the mirror, raise the player active's stages. The check must precede
        # the `rng.rng()` call below, and it gates only THIS block: the later
        # Psychic splash (line 61873) carries no target-alive condition in the
        # source and must still run after a fatal hit.
        if water_tier >= 1 and opposing and target.current_hp > 0:
            if rng.rng() < min(1.0, water_tier / 3):
                apply_stage_change(target, "speed", -water_tier)
                apply_stage_change(target, "atk", -water_tier)
                apply_stage_change(target, "special", -water_tier)
                if self._has_trait("water_def_debuff"):
                    apply_stage_change(target, "def", -water_tier)
                    apply_stage_change(target, "spdef", -water_tier)
                if side == "player":
                    # bundle.deobfuscated.js:61815-61820 -- the source mirrors
                    # speed/atk/special unconditionally, then def/spdef under
                    # the same `BcY` (`water_def_debuff`) flag that added the
                    # two extra enemy debuffs above.
                    self.mirror_enemy_active_debuff(own_team, "speed", water_tier)
                    self.mirror_enemy_active_debuff(own_team, "atk", water_tier)
                    self.mirror_enemy_active_debuff(own_team, "special", water_tier)
                    if self._has_trait("water_def_debuff"):
                        self.mirror_enemy_active_debuff(own_team, "def", water_tier)
                        self.mirror_enemy_active_debuff(own_team, "spdef", water_tier)

        if side == "player" and "Fairy" in _types_of(attacker) and opposing:
            if self._has_trait("fairy_attract") and rng.rng() < 0.3:
                target.flags["flinch"] = True
            if self._has_trait("fairy_hit_two"):
                second = next((m for m in opposing_team if m is not target and m.current_hp > 0), None)
                if second is not None:
                    second_damage = max(1, int(damage * 0.3))
                    second.current_hp = max(0, second.current_hp - second_damage)

        psychic_tier = self._tier("Psychic", side)
        if psychic_tier >= 1 and opposing:
            mult = (1.5 if self._has_trait("splash_more") else 1.0) if side == "player" else 1.0
            amount = max(1, int(damage * min(1.0, 0.1 * psychic_tier) * mult))
            # Exact source order (bundle.deobfuscated.js:61882-61890):
            # splash_crit -> fragile_status -> nonattack_amp. Each is its own
            # `Math.floor` pass, so the order can matter by rounding, not
            # just which traits happen to be active.
            if side == "player" and self._has_trait("splash_crit") and rng.rng() < 0.0625:
                amount = int(amount * 2)
            if target_side == "enemy" and self._has_trait("fragile_status"):
                amount = int(amount * 1.5)
            if side == "player" and target_side == "enemy" and self._has_trait("nonattack_amp"):
                amount = int(amount * 1.3)
            ghost_tier_side = self._tier("Ghost", side)
            grass_tier_side = self._tier("Grass", side)
            grass_blocked = side == "player" and self._has_trait("no_heal_revive")
            for other in opposing_team:
                if other is target or other.current_hp <= 0:
                    continue
                other.current_hp = max(0, other.current_hp - amount)
                # Nested Ghost-execute (bundle.deobfuscated.js:61922-61954):
                # re-runs ONLY the tier-gated heal-back (`_ghost_execute_heal`)
                # for the attacker -- NOT the separately-named `ghost_heal`
                # trait's own extra heal, nor `ghost_curse`; those are
                # exclusive to the main (non-splash) Ghost-execute check.
                if ghost_tier_side >= 1 and other.current_hp > 0 and other.max_hp > 0:
                    threshold = min(0.5, 0.15 * ghost_tier_side)
                    if other.current_hp / other.max_hp < threshold:
                        other.current_hp = 0
                        self._ghost_execute_heal(attacker, other.max_hp, ghost_tier_side, side)
                # Nested Grass-heal (bundle.deobfuscated.js:61955-61974): a
                # simpler heal-only pass on the ATTACKER using the splash
                # damage amount -- no `grass_burst`/`heal_boost_stat` side
                # effects here, matching the source's narrower nested block.
                if grass_tier_side >= 1 and attacker.current_hp > 0 and not grass_blocked:
                    heal = max(1, int(amount * 0.15 * grass_tier_side))
                    heal = min(heal, attacker.max_hp - attacker.current_hp)
                    if heal > 0:
                        attacker.current_hp += heal

        electric_tier = self._tier("Electric", side)
        if electric_tier >= 1 and not is_extra_attack and not attacker.flags.get("_electricBonusFired") and target.current_hp > 0:
            # Port of bundle.deobfuscated.js:62010-62086. Two confirmed
            # defects fixed here (CODEX.md issue 7):
            # 1. `_electricBonusFired` is a same-hit RECURSION GUARD in the
            #    source, not a lifetime one-shot -- it's reset to False
            #    right after this block's loop finishes (62085), every time.
            #    This port used to set it once and never clear it, so the
            #    bonus could only ever fire once per `Combatant` for the
            #    rest of the battle.
            # 2. Each bonus hit re-enters `whenAttacked`/`afterAttack`
            #    (62060-62078) so contact abilities/other traits see it as a
            #    real attack, instead of directly subtracting HP.
            cap = 8 if (side == "player" and self._has_trait("elec_chain")) else 6
            base = 0.2 * electric_tier
            whole = int(base)
            count = min(cap, whole)
            if count < cap and rng.rng() < base - whole:
                count += 1
            attacker.flags["_electricBonusFired"] = True
            try:
                hits = 0
                while hits < count and target.current_hp > 0:
                    hits += 1
                    before_hp = target.current_hp
                    target.current_hp = max(0, target.current_hp - damage)
                    dealt = before_hp - target.current_hp
                    if dealt > 0:
                        self.when_attacked(target, target_side, attacker, dealt)
                        self.after_attack(attacker, side, target, target_side, dealt, own_team, opposing_team, is_extra_attack=True)
            finally:
                attacker.flags["_electricBonusFired"] = False

    def apply_ghost_curse(self, executed_side_team: Sequence[Combatant], excluded: Combatant) -> None:
        """`ghost_curse` (bundle.deobfuscated.js:61527-61537): EVERY other
        living member of the executed Pokemon's own roster gets 2 separate
        random `-1` stage procs each -- not 2 procs split across 2 randomly
        chosen survivors total (a confirmed defect, CODEX.md issue 7)."""
        if not self._has_trait("ghost_curse"):
            return
        for member in executed_side_team:
            if member is excluded or member.current_hp <= 0:
                continue
            for _ in range(2):
                stat = _STATS[int(rng.rng() * len(_STATS))]
                apply_stage_change(member, stat, -1)

    # -----------------------------------------------------------------
    # afterStatusTick -- bundle.deobfuscated.js:62090-62128
    # -----------------------------------------------------------------

    def after_status_tick(self, pokemon: Combatant, side: str) -> None:
        opposing_side = "enemy" if side == "player" else "player"
        ghost_tier = self._tier("Ghost", opposing_side)
        if ghost_tier < 1 or pokemon.max_hp <= 0:
            return
        threshold = min(0.5, 0.15 * ghost_tier)
        if pokemon.current_hp / pokemon.max_hp < threshold:
            pokemon.current_hp = 0

    # -----------------------------------------------------------------
    # beforeDamage -- bundle.deobfuscated.js:62129-62456
    # -----------------------------------------------------------------

    def before_damage(self, defender: Combatant, defender_side: str, attacker: Combatant, damage: int, battle_config: BattleConfig) -> int:
        if defender_side == "player" and "Flying" in _types_of(defender):
            own_speed = get_effective_stat(defender, "speed", _own_items(defender), defender.stages) / 2
            atk_speed = get_effective_stat(attacker, "speed", _own_items(attacker), attacker.stages)
            dodge_chance = own_speed / (own_speed + atk_speed) if (own_speed + atk_speed) else 0
            if self._has_trait("flying_dodge") and rng.rng() < dodge_chance:
                return 0

        if defender.flags.pop("_dodgeNext", False):
            if rng.rng() < 0.5:
                return 0

        if self._has_trait("ec_take_less") and defender_side == "player":
            damage = int(damage * 0.85)

        last_team = battle_config.last_player_team
        if defender_side == "player" and last_team:
            shiny_count = sum(1 for m in last_team if m.is_shiny)
            legend_count = sum(1 for m in last_team if m.species_id in data.get_legendary_ids())
            if self._has_trait("shiny_def") and shiny_count:
                damage = int(damage * max(0.05, 1 - 0.1 * shiny_count))
            if self._has_trait("legend_def") and legend_count:
                damage = int(damage * max(0.05, 1 - 0.1 * legend_count))

        if defender_side == "player":
            dodge_formula = 0.0625
            if self._has_trait("crit_overflow"):
                dodge_formula += 0.35
            if self._has_trait("crit_lifesteal"):
                dodge_formula += 0.1
            if self._has_trait("crit_boost"):
                dodge_formula += 0.1
            if self._has_trait("crit_flinch"):
                dodge_formula += 0.1
            dodge_formula += 0.1
            if self._has_trait("dark_lvlcrit") and "Dark" in _types_of(attacker):
                dodge_formula += attacker.level / 150
            dodge_formula = min(1.0, dodge_formula)
            if self._has_trait("dark_dodge") and rng.rng() < dodge_formula * 0.5:
                return 0

        if defender_side == "player" and self._has_trait("def_onhit_all"):
            apply_stage_change(defender, "def", 1)
            apply_stage_change(defender, "spdef", 1)

        if defender_side == "player":
            def_speed = get_effective_stat(defender, "speed", _own_items(defender), defender.stages)
            atk_speed = get_effective_stat(attacker, "speed", _own_items(attacker), attacker.stages)
            if def_speed < atk_speed:
                if self._has_trait("super_immune"):
                    damage = int(damage * 0.5)
                if self._has_trait("slow_armor"):
                    reduction = min(0.6, (atk_speed - def_speed) / atk_speed) if atk_speed else 0
                    damage = int(damage * (1 - reduction))
            if self._has_trait("steel_team_armor") and last_team:
                steel_count = sum(1 for m in last_team if "Steel" in _types_of(m))
                damage = int(damage * (1 - min(0.4, 0.1 * steel_count)))

        steel_tier = self._tier("Steel", defender_side)
        if steel_tier >= 1:
            reduction = int(damage * min(0.9, 0.15 * steel_tier))
            damage -= reduction
            if defender_side == "player" and self._has_trait("steel_reflect"):
                attacker.current_hp = max(0, attacker.current_hp - reduction)
            if steel_tier >= 4:
                second_reflect = int(reduction * 0.01 * steel_tier)
                if second_reflect > 0:
                    attacker.current_hp = max(0, attacker.current_hp - second_reflect)

        if defender_side == "player" and self._has_trait("enemy_slow_dmg"):
            atk_speed_stage = attacker.stages.get("speed", 0)
            if atk_speed_stage < 0:
                damage = int(damage * (1 - min(0.9, 0.1 * abs(atk_speed_stage))))

        if defender_side == "player" and self._has_trait("poison_armor"):
            damage = int(damage * (1 - min(0.6, 0.05 * (attacker.poison_stacks or 0))))

        if defender_side == "player":
            if attacker.flags.get("_lastMoveIsSpecial") is False and battle_config.reflect_turns > 0:
                damage = int(damage * 0.5)
            elif attacker.flags.get("_lastMoveIsSpecial") is True and battle_config.light_screen_turns > 0:
                damage = int(damage * 0.5)
            if defender.flags.pop("_protectedThisTurn", False):
                return 0

        return max(0, damage)

    # -----------------------------------------------------------------
    # attackerDamageMod -- bundle.deobfuscated.js:62457-62543
    # -----------------------------------------------------------------

    def attacker_damage_mod(self, attacker: Combatant, defender: Combatant, side: str, damage: int, battle_config: BattleConfig) -> int:
        if side != "player":
            return damage

        if self._has_trait("ec_deal_more"):
            damage = int(damage * 1.1)
        if self._has_trait("power_bracer"):
            damage = int(damage * 1.2)
        if self._has_trait("execute_dmg") and defender.current_hp < defender.max_hp:
            damage = int(damage * 1.35)
        if self._has_trait("lvl_overpower") and attacker.level > defender.level:
            damage = int(damage * (1 + 0.1 * (attacker.level - defender.level)))

        if self._has_trait("nerf_punish"):
            negative_sum = sum(-s for s in defender.stages.values() if s < 0)
            damage = int(damage * (1 + 0.1 * negative_sum))

        if self._has_trait("def_stage_dmg"):
            def_stage = attacker.stages.get("def", 0)
            if def_stage > 0:
                damage = int(damage * (1 + 0.1 * def_stage))

        if self._has_trait("dmg_cap"):
            last_team = battle_config.last_player_team
            distinct_types = {t for m in last_team for t in _types_of(m)} if last_team else set()
            damage = int(damage * (1 + 0.05 * len(distinct_types)))

        last_team = battle_config.last_player_team
        if self._has_trait("shiny_dmg") and last_team:
            damage = int(damage * (1 + 0.1 * sum(1 for m in last_team if m.is_shiny)))
        if self._has_trait("legend_dmg") and last_team:
            legendary_ids = data.get_legendary_ids()
            damage = int(damage * (1 + 0.2 * sum(1 for m in last_team if m.species_id in legendary_ids)))
        if self._has_trait("flying_stage_dmg"):
            speed_stage = attacker.stages.get("speed", 0)
            if speed_stage > 0:
                damage = int(damage * (1 + 0.1 * speed_stage))

        if self._has_trait("ground_outspeed_dmg") and "Ground" in _types_of(attacker):
            atk_speed = get_effective_stat(attacker, "speed", _own_items(attacker), attacker.stages)
            def_speed = get_effective_stat(defender, "speed", _own_items(defender), defender.stages)
            if atk_speed > def_speed:
                damage = int(damage * 1.5)

        if self._has_trait("no_item_buff"):
            if battle_config.no_item_empty is None and last_team:
                battle_config.no_item_empty = sum(1 for m in last_team if m.held_item is None)
            damage = int(damage * (1 + 0.1 * (battle_config.no_item_empty or 0)))

        if self._has_trait("flying_speed") and "Flying" in _types_of(attacker):
            atk_speed = get_effective_stat(attacker, "speed", _own_items(attacker), attacker.stages)
            def_speed = get_effective_stat(defender, "speed", _own_items(defender), defender.stages)
            if atk_speed > def_speed:
                diff = atk_speed - def_speed
                damage = int(damage * (1 + min(0.5, 0.5 * diff / max(1, def_speed))))

        return damage

    # -----------------------------------------------------------------
    # onKO -- bundle.deobfuscated.js:62544-63092
    # -----------------------------------------------------------------

    def on_ko(
        self,
        fainted: Combatant,
        fainted_side: str,
        fainted_idx: int,
        killer: Optional[Combatant],
        killer_side: Optional[str],
        killer_idx: Optional[int],
        player_team: Sequence[Combatant],
        enemy_team: Sequence[Combatant],
        battle_config: BattleConfig,
    ) -> None:
        self.sweep_faint_rallies(player_team, enemy_team, battle_config)
        key = (fainted_side, fainted_idx)
        if key in battle_config.kos_handled:
            return
        battle_config.kos_handled.add(key)

        same_side_team = player_team if fainted_side == "player" else enemy_team
        if self._has_trait("poison_pass") and (fainted.poison_stacks or 0) > 0:
            other = next((m for m in same_side_team if m is not fainted and m.current_hp > 0), None)
            if other is not None:
                other.poison_stacks = (other.poison_stacks or 0) + fainted.poison_stacks // 2

        if killer is not None:
            dragon_tier = self._tier("Dragon", killer_side)
            if dragon_tier >= 1:
                apply_stage_change(killer, "atk", dragon_tier)
                apply_stage_change(killer, "special", dragon_tier)
                killer_team = player_team if killer_side == "player" else enemy_team
                opp_team = enemy_team if killer_side == "player" else player_team
                self._mirror_fire_share(killer_team, killer, "atk", dragon_tier)
                self._mirror_fire_share(killer_team, killer, "special", dragon_tier)
                if killer_side == "player":
                    self._mirror_atk(opp_team, "atk", dragon_tier)
                    self._mirror_atk(opp_team, "special", dragon_tier)

        if fainted_side == "player":
            if "Rock" in _types_of(fainted) and self._has_trait("rock_legacy"):
                for stat in ("def", "spdef"):
                    stage = fainted.stages.get(stat, 0)
                    if stage > 0:
                        for member in player_team:
                            if member is not fainted and member.current_hp > 0:
                                apply_stage_change(member, stat, stage)

            if "Rock" in _types_of(fainted) and self._has_trait("rock_explode"):
                # Port of bundle.deobfuscated.js:62622-62684. The source
                # recomputes a FRESH calcDamage PER alive enemy target
                # (fainted's move against THAT target's own defense/held
                # item, its own crit/variance RNG draws) -- not one shared
                # roll against the first alive enemy applied flat to
                # everyone (a confirmed defect this fixes, see
                # tools/battle-oracle/fixtures/rock_explode_fanout.json).
                # `calcDamage`'s final battle_config-like arg is a literal
                # `null` here too (line 62657), same reasoning as
                # `_apply_elec_lead`'s docstring -- no weather/darkCritFloor
                # bonus on any of these splash hits.
                move = get_best_move(
                    fainted.types, fainted.base_stats, fainted.species_id, fainted.move_tier, fainted.held_item,
                    has_multitype=fainted.gen3_ability == "multitype",
                )
                fainted_items = _own_items(fainted)
                for enemy in enemy_team:
                    if enemy.current_hp <= 0:
                        continue
                    result = calc_damage(fainted, enemy, move, fainted_items, _own_items(enemy), self.traits, "player", None)
                    splash = max(1, int(result.damage * 0.25))
                    enemy.current_hp = max(0, enemy.current_hp - splash)

            if "Bug" in _types_of(fainted) and self._has_trait("bug_legacy"):
                gain = round(fainted.level * 0.2)
                for member in player_team:
                    if member is not fainted and member.current_hp > 0 and "Bug" in _types_of(member):
                        member.flags.setdefault("_critLevelBase", member.level)
                        member.level += gain

            if self._has_trait("fighting_faint_speed"):
                for member in player_team:
                    if member.current_hp > 0 and "Fighting" in _types_of(member):
                        apply_stage_change(member, "speed", 1)

            if killer is not None and killer_side == "player" and "Fighting" in _types_of(killer) and self._has_trait("fighting_ko_trigger"):
                self.trigger_fighting_rally("player", player_team, enemy_team)

            if "Dragon" in _types_of(fainted) and self._has_trait("dragon_first_faint_trigger") and not battle_config.dragon_faint_fired:
                battle_config.dragon_faint_fired = True
                lead = first_alive(player_team)
                dragon_tier = self._tier("Dragon", "player")
                if lead is not None and dragon_tier >= 1:
                    apply_stage_change(lead[0], "atk", dragon_tier)
                    apply_stage_change(lead[0], "special", dragon_tier)

            if "Ghost" in _types_of(fainted) and self._has_trait("ghost_revenge") and not battle_config.ghost_revenge_used:
                battle_config.ghost_revenge_used = True
                target_pair = first_alive(enemy_team)
                if target_pair is not None:
                    target_pair[0].current_hp = 0

            if "Flying" in _types_of(fainted) and self._has_trait("flying_legacy"):
                for member in player_team:
                    if member.current_hp > 0:
                        apply_stage_change(member, "speed", 1)
                        apply_stage_change(member, "speed", 1)

            if "Fire" in _types_of(fainted) and self._has_trait("fire_legacy"):
                candidates = [m for m in player_team if m is not fainted and m.current_hp > 0 and "Fire" in _types_of(m)]
                if candidates:
                    chosen = candidates[int(rng.rng() * len(candidates))]
                    for stat in ("atk", "special"):
                        stage = fainted.stages.get(stat, 0) // 2
                        if stage:
                            apply_stage_change(chosen, stat, stage)

        if fainted_side == "enemy":
            if self._has_trait("stat_carryover"):
                other = next((m for m in enemy_team if m is not fainted and m.current_hp > 0), None)
                if other is not None:
                    for stat, stage in fainted.stages.items():
                        if stage < 0:
                            apply_stage_change(other, stat, stage // 2)

            if self._has_trait("ko_boost") and any(m is not fainted and m.current_hp > 0 for m in enemy_team):
                lead_pair = first_alive(player_team)
                if lead_pair is not None:
                    lead = lead_pair[0]
                    remaining = list(_STATS)
                    for _ in range(3):
                        if not remaining:
                            break
                        stat = remaining[int(rng.rng() * len(remaining))]
                        apply_stage_change(lead, stat, 1)
                        if stat in ("atk", "special"):
                            self._mirror_fire_share(player_team, lead, stat, 1)
                            self._mirror_atk(enemy_team, stat, 1)

            if self._has_trait("normal_grow"):
                for member in player_team:
                    if member.current_hp > 0 and "Normal" in _types_of(member):
                        bonus = max(1, int(member.max_hp * 0.1))
                        member.max_hp += bonus
                        if self._has_trait("no_heal_revive"):
                            member.current_hp = min(member.max_hp, member.current_hp + bonus)
                        else:
                            member.current_hp = member.max_hp

            lead_pair = first_alive(player_team)
            if lead_pair is not None:
                lead = lead_pair[0]
                if self._has_trait("ko_maxhp"):
                    lead.flags["_runMaxHp"] = lead.flags.get("_runMaxHp", 0) + 2
                    lead.max_hp += 2
                    lead.current_hp += 2
                if self._has_trait("dragon_ko_speed") and "Dragon" in _types_of(lead):
                    apply_stage_change(lead, "speed", 1)
                    apply_stage_change(lead, "speed", 1)
                if self._has_trait("fairy_speed_steal") and "Fairy" in _types_of(lead):
                    for stat, stage in fainted.stages.items():
                        if stage < 0:
                            apply_stage_change(lead, stat, -stage)
                if self._has_trait("ground_ko_boost") and "Ground" in _types_of(lead):
                    speed_stage = fainted.stages.get("speed", 0)
                    procs = 2 * abs(min(0, speed_stage))
                    remaining = list(_STATS)
                    for _ in range(procs):
                        if not remaining:
                            break
                        stat = remaining[int(rng.rng() * len(remaining))]
                        apply_stage_change(lead, stat, 1)
                if self._has_trait("ghost_dodge") and "Ghost" in _types_of(lead):
                    lead.flags["_dodgeNext"] = True
                if self._has_trait("fight_revive") and "Fighting" in _types_of(lead):
                    for member in player_team:
                        if member.current_hp <= 0 and "Fighting" in _types_of(member):
                            member.current_hp = 1
                if self._has_trait("ice_ko_freeze") and "Ice" in _types_of(lead) and rng.rng() < 0.5:
                    candidates = [m for m in enemy_team if m is not fainted and m.current_hp > 0]
                    if candidates:
                        candidates[0].flags["_frozenOnEntry"] = True

    # -----------------------------------------------------------------
    # beforeTurn -- bundle.deobfuscated.js:63093-63400
    # -----------------------------------------------------------------

    def before_turn(self, attacker: Combatant, target: Optional[Combatant], side: str, battle_config: BattleConfig) -> Optional[str]:
        if side != "player":
            return None

        def_stage = attacker.stages.get("def", 0)
        spdef_stage = attacker.stages.get("spdef", 0)
        if def_stage >= 10 and spdef_stage >= 10 and not attacker.flags.get("_rockOvercapDone") and self._has_trait("rock_overcap"):
            attacker.flags["_rockOvercapDone"] = True
            apply_stage_change(attacker, "speed", 10)
            apply_stage_change(attacker, "atk", 10)
            apply_stage_change(attacker, "special", 10)

        if self._has_trait("stockpile_band") and (def_stage < 10 or spdef_stage < 10):
            count = attacker.flags.get("_stockpileTurnCount", 0)
            attacker.flags["_stockpileTurnCount"] = count + 1
            if count % 2 == 0:
                apply_stage_change(attacker, "def", 2)
                apply_stage_change(attacker, "spdef", 2)
                return "skip"

        if self._has_trait("protect_band"):
            count = attacker.flags.get("_protectTurnCount", 0)
            attacker.flags["_protectTurnCount"] = count + 1
            if count % 2 == 0:
                attacker.flags["_protectedThisTurn"] = True
                return "skip"

        if self._has_trait("flying_roost") and "Flying" in _types_of(attacker) and attacker.current_hp < attacker.max_hp * 0.5:
            heal = min(int(attacker.max_hp * 0.5), attacker.max_hp - attacker.current_hp)
            attacker.current_hp += heal
            return "skip"

        if self._has_trait("grass_spore") and "Grass" in _types_of(attacker) and target is not None and not target.status:
            target.status = "sleep"
            return "skip"

        if self._has_trait("bulk_charm") and "Fighting" in _types_of(attacker) and target is not None:
            move = get_best_move(
                attacker.types, attacker.base_stats, attacker.species_id, attacker.move_tier, attacker.held_item,
                has_multitype=attacker.gen3_ability == "multitype",
            )
            if not move.no_damage:
                speculative = calc_damage(attacker, target, move, _own_items(attacker), _own_items(target), self.traits, side, battle_config)
                if speculative.damage < target.current_hp:
                    for stat in ("atk", "special", "def", "spdef"):
                        apply_stage_change(attacker, stat, 1)
                    return "skip"

        if self._has_trait("sword_charm") and not battle_config.fired_flags.get("sword_charm") and "Normal" in _types_of(attacker):
            battle_config.fired_flags["sword_charm"] = True
            apply_stage_change(attacker, "atk", 4)
            return "skip"

        if self._has_trait("sun_charm") and not battle_config.fired_flags.get("sun_charm") and "Fire" in _types_of(attacker):
            battle_config.fired_flags["sun_charm"] = True
            battle_config.sunny_turns = 5
            return "skip"
        if self._has_trait("rain_charm") and not battle_config.fired_flags.get("rain_charm") and "Water" in _types_of(attacker):
            battle_config.fired_flags["rain_charm"] = True
            battle_config.rain_turns = 5
            return "skip"

        if self._has_trait("stealth_rock") and not battle_config.fired_flags.get("stealth_rock") and "Rock" in _types_of(attacker):
            battle_config.fired_flags["stealth_rock"] = True
            battle_config.enemy_stealth_rock_pct = 0.2
            return "skip"
        if self._has_trait("toxic_spikes") and not battle_config.fired_flags.get("toxic_spikes") and "Poison" in _types_of(attacker):
            battle_config.fired_flags["toxic_spikes"] = True
            battle_config.enemy_toxic_spikes = True
            return "skip"
        if self._has_trait("sticky_web") and not battle_config.fired_flags.get("sticky_web") and "Bug" in _types_of(attacker):
            battle_config.fired_flags["sticky_web"] = True
            battle_config.enemy_sticky_web = True
            return "skip"

        if self._has_trait("screen_card") and not battle_config.fired_flags.get("screen_card"):
            battle_config.fired_flags["screen_card"] = True
            move = get_best_move(
                attacker.types, attacker.base_stats, attacker.species_id, attacker.move_tier, attacker.held_item,
                has_multitype=attacker.gen3_ability == "multitype",
            )
            if move.is_special:
                battle_config.light_screen_turns = 5
            else:
                battle_config.reflect_turns = 5
            return "skip"

        return None

    # -----------------------------------------------------------------
    # endOfRound -- bundle.deobfuscated.js:63401-63461
    # -----------------------------------------------------------------

    def end_of_round(self, player_team: Sequence[Combatant], enemy_team: Sequence[Combatant], battle_config: BattleConfig) -> None:
        self.sweep_faint_rallies(player_team, enemy_team, battle_config)
        for turns_attr in ("sunny_turns", "rain_turns", "reflect_turns", "light_screen_turns"):
            value = getattr(battle_config, turns_attr)
            if value > 0:
                setattr(battle_config, turns_attr, value - 1)
        for team in (player_team, enemy_team):
            for member in team:
                member.flags.pop("_protectedThisTurn", None)

        if not self._has_trait("no_heal_revive") and self._has_trait("grass_drain"):
            player_lead = first_alive(player_team)
            enemy_lead = first_alive(enemy_team)
            if player_lead is not None and enemy_lead is not None and "Grass" in _types_of(player_lead[0]):
                enemy = enemy_lead[0]
                lead = player_lead[0]
                drain = max(1, int(enemy.max_hp * 0.1))
                enemy.current_hp = max(0, enemy.current_hp - drain)
                heal = min(drain, lead.max_hp - lead.current_hp)
                if heal > 0:
                    lead.current_hp += heal

    # -----------------------------------------------------------------
    # applyEnemySwitchInHazards -- bundle.deobfuscated.js:63462-63541
    # -----------------------------------------------------------------

    def apply_enemy_switch_in_hazards(self, pokemon: Optional[Combatant], player_team: Sequence[Combatant], battle_config: BattleConfig) -> None:
        if pokemon is None or pokemon.current_hp <= 0:
            return
        if battle_config.enemy_stealth_rock_pct > 0:
            damage = max(1, int(pokemon.max_hp * battle_config.enemy_stealth_rock_pct))
            pokemon.current_hp = max(0, pokemon.current_hp - damage)
        if battle_config.enemy_toxic_spikes and (pokemon.poison_stacks or 0) == 0:
            base = self._tier("Poison", "player") or 1
            pokemon.poison_stacks = base
        if battle_config.enemy_sticky_web:
            apply_stage_change(pokemon, "speed", -2)
            self.mirror_enemy_active_debuff(player_team, "speed", 2)

    # -----------------------------------------------------------------
    # whenAttacked -- bundle.deobfuscated.js:63542-63607
    # -----------------------------------------------------------------

    def when_attacked(self, defender: Combatant, defender_side: str, attacker: Combatant, damage: int) -> None:
        if defender_side != "player" or damage <= 0:
            return
        if "Steel" in _types_of(defender) and self._has_trait("steel_def_onhit"):
            apply_stage_change(defender, "def", 1)
            apply_stage_change(defender, "spdef", 1)
        if attacker.current_hp > 0 and "Water" in _types_of(defender) and self._has_tier("Water", "player"):
            if self._has_trait("water_retaliate"):
                tier = self._tier("Water", "player")
                apply_stage_change(attacker, "speed", -tier)
                apply_stage_change(attacker, "atk", -tier)
                apply_stage_change(attacker, "special", -tier)
                if self._has_trait("water_def_debuff"):
                    apply_stage_change(attacker, "def", -tier)
                    apply_stage_change(attacker, "spdef", -tier)
        # Rocky Helmet 2x-counter / Shell Bell thorns: both permanently dead
        # in the source (docs/logic-notes-traitsconfig.md section 6.1) --
        # not ported.
