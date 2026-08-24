"""Port of `runBattle` (docs/logic-notes-runbattle.md,
bundle.deobfuscated.js:55164-56787) -- the per-battle round loop tying
`battle.py`'s damage/stat/turn-order/status functions together with the
optional `battle_abilities.Gen3AbilityConfig`/`battle_traits.TraitsConfig`
hook providers.

**This is a TEAM battle loop, not a single 1v1 pair fought to completion**
(docs/logic-notes-runbattle.md section 1.1): every round re-derives the
active pair by scanning each roster for the first Pokemon with
`current_hp > 0`, in list order -- no player choice, no AI switch decision,
pure automatic advancement. Nuzlocke permadeath (removing fainted Pokemon
from the roster entirely) is NOT this module's job -- it happens after
`run_battle` returns, wherever `engine.py` ends up owning that (docs/logic-
notes-runbattle.md section 7).

**Hook dispatch mirrors the visible source merge.** The two providers remain
separate Python objects, but ordinary Gen3/4 call order, threaded modifier
returns, and the source's generic-return-discard quirk are covered by the
JS-vs-Python fixtures under `tools/battle-oracle/`. P0.1 fixtures cover
Truant, Mirror Coat, Own Tempo, Trick Room, trait skips, and combined order;
P0.2 fixtures cover status-tick hooks and `sweepKOs` deduplication. This is
measured coverage for those cases, not a claim that every hook combination
has been oracle-verified.

**Complete event/log plumbing is still deferred.** `BattleResult` carries
the mechanical outcome plus two narrow diagnostic streams
(`status_events`/`hook_trace`) used by the P0.2 parity oracle. It still does
not mirror the source's full `log`/`detailedLog` arrays or provide the
renderer-facing animation contract.

**Not yet ported / explicitly out of scope here**: per-move contact flags
(this engine treats every landed hit as "contact" for on-hit ability
purposes, matching the source, docs/logic-notes-gen3abilities.md), and any
UI-facing flavor text.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from pokelike import rng
from pokelike.battle import (
    BattleConfig,
    Combatant,
    DamageResult,
    HeldItem,
    MoveInstance,
    apply_stage_change,
    burn_tick_damage,
    calc_damage,
    decide_turn_order,
    get_best_move,
    get_effective_stat,
    has_passive,
    poison_tick_damage,
    resolve_pre_turn_status,
)

_STAGE_STATS = ("atk", "def", "speed", "special", "spdef")

_ROUND_CAP = 300  # BI5, bundle.deobfuscated.js:55294
_OVERTIME_ROUND = 100  # BI6, bundle.deobfuscated.js:55294 -- overtime kicks in the round AFTER this
_STALEMATE_ROUNDS = 12  # bundle.deobfuscated.js:55414, "stuck" detector threshold


def _own_items(pokemon: Optional[Combatant]) -> list:
    return [pokemon.held_item] if pokemon is not None and pokemon.held_item is not None else []


def _first_alive_index(team: Sequence[Combatant]) -> Optional[int]:
    for idx, member in enumerate(team):
        if member.current_hp > 0:
            return idx
    return None


@dataclass
class BattleResult:
    player_won: bool
    player_team: list
    enemy_team: list
    player_participants: set
    rounds: int
    # Narrow diagnostic streams for the JS parity oracle. These cover
    # status/KO sequencing only; they are not yet the public animation log.
    status_events: list = field(default_factory=list)
    hook_trace: list = field(default_factory=list)
    # Ordered, turn-delimited presentation stream. A flat list of
    # `{"type": "turn_start", "round": n}` markers interleaved with the
    # records `run_battle` produces, in production order -- the counterpart of
    # the source's own single flat `detailedLog` (`BcM`), partitioned by its
    # round loop. Purely additive: every write is an `append` to this list, no
    # control flow, no state mutation and no RNG draw depends on it.
    #
    # Record families, and who consumes them:
    #   * `attack`  -- M3.3b workstream 5. The ONLY family the route oracle
    #     compares: `run_scenario._fold_turns` projects it and `driver.js`'s
    #     `deriveTurns` (route-oracle/driver.js:244) filters the source's log
    #     to `type === 'attack'` on the other side. Adding a family here does
    #     NOT widen the compared surface -- see `_fold_turns`.
    #   * `effect` -- M6/N10. Held-item and passive HP changes that happen
    #     AFTER the attack record is appended (rocky_helmet, enemy_recoil,
    #     life_orb, shell_bell; source 56366-56442), so a renderer can
    #     attribute the HP delta to its real cause instead of silently
    #     disagreeing with the attack record's `attacker_hp_after`.
    #   * `faint` -- M6/N11. The ordinary combat KO (source 56445 / 56482).
    #     `status_events` carries only the status-tick-caused subset, which is
    #     what `driver.js`'s `deriveStatusEvents` filters the source log down
    #     to; an ordinary KO produced no record anywhere before M6.
    battle_events: list = field(default_factory=list)


def _attack_event(
    side, mover, mover_idx, target_side, target, target_idx,
    move, move_type, damage, type_eff, crit, is_special,
) -> dict:
    """One `type: "attack"` record, shaped like the source's own
    (bundle.deobfuscated.js:55979-55995). Pure construction: it reads the
    combatants it is handed and mutates nothing.

    `attacker_name`/`target_name` are deliberately absent: `side` plus the
    index identifies the combatant exactly, the teams themselves are compared
    field by field in the same checkpoint, and the source's names go through
    `nickname || name` -- a display concern the port does not model.
    """
    return {
        "type": "attack",
        "side": side,
        "attacker_idx": mover_idx,
        "target_side": target_side,
        "target_idx": target_idx,
        "move_name": move.name,
        "move_type": move_type,
        "damage": damage,
        "type_eff": type_eff,
        "crit": bool(crit),
        "is_special": bool(is_special),
        "attacker_hp_after": mover.current_hp,
        "target_hp_after": target.current_hp,
        # The source sets `isExtraAttack` only on the half_twice /
        # dragon_first_double follow-up hits (56255 / 56338). Those are
        # emitted from `_apply_half_twice_extra_attack` /
        # `_apply_dragon_first_double_extra_attack`, which are not
        # instrumented; both require a passive the Story/Nuzlocke route
        # matrix never grants, and if one ever fired the projections would
        # disagree in length rather than silently agree.
        "extra_attack": False,
    }


def _effect_event(side, idx, hp_change, hp_after, reason) -> dict:
    """One `type: "effect"` record, shaped like the source's own
    (bundle.deobfuscated.js:56372-56381, 56391-56399, 56410-56418,
    56433-56441). Pure construction: it mutates nothing.

    `name` is deliberately absent for the same reason `_attack_event` drops
    `attacker_name`: `side` plus `idx` identifies the combatant exactly and
    the source's names go through `nickname || name`, which the port does not
    model.

    `reason` is the STABLE CAUSE KEY, not the source's prose. The source's
    own `reason` strings are display text that interpolate both the name and
    the rolled amount ("Rocky Helmet hurt <name> for <n> HP!"); reproducing
    them would re-introduce exactly the naming the port does not model. The
    renderer builds its own prose from this key plus `hp_change` -- the same
    read-side enrichment rule R1 set for every other record family.
    """
    return {
        "type": "effect",
        "side": side,
        "idx": idx,
        "hp_change": hp_change,
        "hp_after": hp_after,
        "reason": reason,
    }


def _counter_attack_event(side, mover, mover_idx, target_side, target, target_idx, hit) -> dict:
    """The `mirror_coat` counter-hit as an ordinary `type: "attack"` record.

    The source builds it inline in the ability's own `beforeTurn`
    (bundle.deobfuscated.js:58116-58132) rather than at an attack site, but the
    shape it pushes is the ordinary attack shape, so it is projected through
    the same fields as `_attack_event`. `extra_attack` is false: the source
    sets `isExtraAttack` only on the half_twice / dragon_first_double follow-up
    hits (56255 / 56338) and does not set it here.

    Pure construction: `hit` already carries the values the hook computed, and
    the HP fields are read from the combatants after the hook applied them.
    """
    return {
        "type": "attack",
        "side": side,
        "attacker_idx": mover_idx,
        "target_side": target_side,
        "target_idx": target_idx,
        "move_name": hit["move_name"],
        "move_type": hit["move_type"],
        "damage": hit["damage"],
        "type_eff": hit["type_eff"],
        "crit": bool(hit["crit"]),
        "is_special": bool(hit["is_special"]),
        "attacker_hp_after": mover.current_hp,
        "target_hp_after": target.current_hp,
        "extra_attack": False,
    }


def _pre_turn_tick(side, idx, mover, status) -> dict:
    """One pre-turn `status_tick` record, shaped like the source's own
    (bundle.deobfuscated.js:55655-55668 flinch, 55676-55685 freeze_skip,
    55689-55698 sleep_wake, 55699-55709 sleep_skip). All four carry
    `hpChange: 0` and `hpAfter: currentHp` literally -- none of them touches
    HP -- so the projection carries the same constants rather than deriving
    them.

    Pure construction, like `_attack_event`: it reads the combatant it is
    handed and mutates nothing. `name` is excluded for the same reason
    `attacker_name` is, and the oracle's `status_events` projection does not
    carry it either.
    """
    return {
        "type": "status_tick",
        "side": side,
        "idx": idx,
        "status": status,
        "hp_change": 0,
        "hp_after": mover.current_hp,
    }


_PERSISTENT_FLAGS = ("_runSpeedStage", "_runMaxHp")


def clone_combatant(pokemon: Combatant) -> Combatant:
    """Port of the battle-local clone boundary the source builds via two
    nested shallow spreads -- `runBattleScreen`'s
    `state.team.map(p => ({...p}))` (bundle.deobfuscated.js:81115) feeding
    `runBattle`'s own `B6J.map(p => initBattleState({...p}))` (line 55193).
    Both are plain JS object spreads: a NEW top-level object whose fields
    start out equal to the source's, but reassigning a field on the clone
    (`clone.types = ...`, `clone.baseStats = ...`, `clone._gen3Ability =
    ...`) never touches the original. `copy.copy` on a dataclass gives the
    exact same semantics in Python -- a new instance, fields shallow-copied
    -- so Ditto/Multitype/Forecast/Color-Change type changes, a Deoxys form
    swap's base-stat change, and a Traced ability all land on the clone
    only, never leaking into the persistent roster object (CODEX.md issues
    3-4) as long as callers mutate by REASSIGNING these fields (`mon.types =
    x`), which is what every trait/ability implementation here already does
    -- none of them mutate `types`/`base_stats` in place.
    """
    return copy.copy(pokemon)


def _init_battle_state(pokemon: Combatant) -> None:
    """Port of `initBattleState` (bundle.deobfuscated.js:54829-54849) --
    resets in-battle-only state; `stat_buffs` (persistent) is untouched.

    **`flags` reset, and why (CODEX.md issue 8/37).** The source builds a
    fresh battle-local CLONE of each roster Pokemon for `runBattle` and only
    ever writes a small, explicit set of fields back onto the persistent
    `state.team[i]` object afterward -- `level`/`maxHp`/`currentHp` always,
    plus `_runSpeedStage`/`_runMaxHp` when non-zero (bundle.deobfuscated.js:
    81283-81294). Every other one-shot/bookkeeping field the battle loop
    accumulates (`_g3Entered`, `_sturdyUsed`, `_electricBonusFired`,
    `_burned`, `_paralyzed`, `_poisonStacks`, `status`, ...) lives ONLY on
    that discarded clone and is gone by the next battle. This module's
    `Combatant` instances ARE the persistent roster objects (`engine.py`
    mutates them in place across battles, no clone-and-selectively-copy
    step) -- so one-shot fields must be replaced on this battle clone.
    `_g3Entered` is one such field: a Pokemon that entered an earlier battle
    must still count as a fresh switch-in participant in the next.

    Burn, paralysis, and poison are deliberately NOT reset here. The real
    `initBattleState` resets only `stages`, `status`, and Loaded Dice state;
    `_burned`, `_paralyzed`, and `_poisonStacks` on an input battler survive
    initialization (bundle.deobfuscated.js:54829-54849). Normal engine
    battles still start clean because battle-acquired status lives on the
    discarded clone and is never copied back to the persistent roster.
    `_runSpeedStage`/`_runMaxHp` are the two fields explicitly
    EXCLUDED from this reset, since -- unlike everything else -- the source
    really does carry them forward (see `TraitsConfig.on_start_fight`'s
    `_seed_persistent_bonuses`, which re-applies `_runSpeedStage` to battle
    stages every fight, and `engine._apply_level_gain`, which folds
    `_runMaxHp` into the recomputed HP curve so leveling up can't erase it).
    """
    pokemon.stages = {"atk": 0, "def": 0, "speed": 0, "special": 0, "spdef": 0}
    pokemon.status = None
    pokemon.flags = {k: v for k, v in pokemon.flags.items() if k in _PERSISTENT_FLAGS}
    if pokemon.held_item is not None and pokemon.held_item.id == "loaded_dice":
        pokemon.flags["_loadedDiceRoll"] = 2 if rng.rng() < 0.37 else -1


def run_battle(
    player_team: Sequence[Combatant],
    enemy_team: Sequence[Combatant],
    traits: Sequence = (),
    ability_config=None,
    traits_config=None,
    battle_config: Optional[BattleConfig] = None,
    trick_room_challenge: bool = False,
) -> BattleResult:
    """Port of `runBattle` (bundle.deobfuscated.js:55164-56787).

    `ability_config`/`traits_config` are optional `Gen3AbilityConfig`/
    `TraitsConfig` instances (either or both may be omitted -- every hook
    call site below is a no-op if its provider is None, matching the
    source's `battleConfig?.hook && ...` pattern). `trick_room_challenge`
    stands in for the source's separate `CHALLENGE_TRICKROOM` global,
    OR'd with whatever `battle_config.trick_room` ends up being (see
    `battle.BattleConfig`'s docstring).

    **Clone boundary (CODEX.md issues 3-4)**: `player_team`/`enemy_team` are
    never mutated -- this function clones every member (`clone_combatant`)
    before touching anything, matching the source's own clone-then-
    `initBattleState` construction of its battle-local `Bct`/`Bcg` arrays.
    `BattleResult.player_team`/`enemy_team` are these CLONES, not the
    caller's originals; a caller that needs battle-local mutations
    reflected onto a persistent roster must apply its own narrow copy-back
    afterward (see `engine._copy_back_battle_result`), exactly like
    `runBattleScreen` does -- this function has no opinion on which fields a
    caller considers persistent.
    """
    if battle_config is None:
        battle_config = BattleConfig()
    battle_config.dark_crit_floor = getattr(traits_config, "dark_crit_floor", 0.0)

    p_team = [clone_combatant(m) for m in player_team]
    e_team = [clone_combatant(m) for m in enemy_team]
    for member in p_team:
        _init_battle_state(member)
    for member in e_team:
        _init_battle_state(member)

    if has_passive(traits, "rand_start"):
        for member in p_team:
            member.uncap_stages = True

    for member in p_team + e_team:
        if member.flags.get("_loadedDiceRoll"):
            delta = member.flags["_loadedDiceRoll"]
            for stat in ("atk", "def", "speed", "special", "spdef"):
                apply_stage_change(member, stat, delta)

    no_heal_revive = has_passive(traits, "no_heal_revive")
    sof_double = has_passive(traits, "sof_double")
    if traits_config is not None:
        traits_config.on_start_fight(p_team, e_team, battle_config)
        if sof_double:
            traits_config.on_start_fight(p_team, e_team, battle_config, second_call=True)

    if has_passive(traits, "lead_speed") and p_team and p_team[0].current_hp > 0:
        reps = 2 if sof_double else 1
        for _ in range(reps):
            apply_stage_change(p_team[0], "speed", 2)

    if has_passive(traits, "rand_start"):
        roll_count = 4 if sof_double else 2
        stats = ("atk", "def", "speed", "special", "spdef")
        for member in p_team:
            if member.current_hp > 0:
                for _ in range(roll_count):
                    stat = stats[int(rng.rng() * len(stats))]
                    apply_stage_change(member, stat, 1)

    # The initial participant is the first ALIVE slot (`Bct.findIndex(alive)`,
    # bundle.deobfuscated.js:55220), not hardcoded index 0 -- a team that
    # entered this battle with slot 0 already fainted (e.g. a non-Nuzlocke
    # run that lost its lead in a previous fight) still credits whichever
    # slot actually leads.
    player_participants: set = set()
    initial_idx = _first_alive_index(p_team)
    if initial_idx is not None:
        player_participants.add(initial_idx)

    def team_alive(team: Sequence[Combatant]) -> bool:
        return any(m.current_hp > 0 for m in team)

    def player_still_in_it() -> bool:
        if has_passive(traits, "team_hp_pool"):
            return bool(p_team) and p_team[0].current_hp > 0
        return team_alive(p_team)

    trick_room = trick_room_challenge
    merged = ability_config is not None and traits_config is not None

    def resolve_trick_room() -> bool:
        """`isTrickRoom` is a generic (non-threaded) hook in
        `mergeBattleConfigs` -- when BOTH `ability_config` and
        `traits_config` exist (i.e. `runBattle` was handed a real merged
        `battleConfig`, matching the source's `mergeBattleConfigs(O, iu)`
        with both args non-null), the merge wrapper for every hook not in
        its small threaded-hook set discards the hook's return value
        (bundle.deobfuscated.js:81011-81015: `typeof B2n=="function" &&
        O[hook](...), typeof B2l=="function" && iu[hook](...)` -- a plain
        comma expression, no `return`). `isTrickRoom` is one of those
        discarded hooks (it's not in the `ip` map at lines 80996-80999),
        so a merged battle's turn-order check
        (`Bcp = () => Bcu || (typeof B71?.isTrickRoom=="function" &&
        !!B71.isTrickRoom())`, line 55188-55191) always reads `undefined`
        from the merged call -- an ability-set Trick Room flag becomes
        unreadable for turn-order purposes whenever the player has ANY
        passive active in Gen3/4 story mode, even though the flag itself
        (`battle_config.trick_room`) was set correctly by the ability's
        `onSwitchIn` side effect. Only `trick_room_challenge` (a separate,
        always-readable global per `BattleConfig`'s own docstring) survives
        merging. When NOT merged (only one of `ability_config`/
        `traits_config` present, or neither), there is no wrapper at all --
        `battle_config.trick_room` reads normally.
        """
        if merged:
            return trick_room
        return trick_room or battle_config.trick_room

    overtime_mult = 1
    round_num = 0
    stalemate_counter = 0
    last_total_hp = -1
    status_events: list = []
    hook_trace: list = []
    battle_events: list = []

    def total_hp() -> int:
        return sum(max(0, m.current_hp) for m in p_team) + sum(max(0, m.current_hp) for m in e_team)

    while player_still_in_it() and team_alive(e_team) and round_num < _ROUND_CAP:
        round_num += 1
        # Oracle observation only (M3.3b workstream 5). Emitted at the same
        # point the source's round counter increments (bundle.deobfuscated.js:
        # 55415-55418), i.e. before any of this round's events exist, so the
        # two turn partitions line up exactly.
        battle_events.append({"type": "turn_start", "round": round_num})
        if round_num == _OVERTIME_ROUND + 1:
            overtime_mult = 3

        p_idx = _first_alive_index(p_team)
        e_idx = _first_alive_index(e_team)
        if p_idx is None or e_idx is None:
            break
        attacker_p = p_team[p_idx]
        attacker_e = e_team[e_idx]
        # `_halfTwiceUsed` is reset every round for THIS round's active
        # leads (bundle.deobfuscated.js:55430-55431), not a battle-lifetime
        # flag despite its name -- half_twice can fire once per round (for
        # whichever Pokemon is the current active attacker), not once ever.
        # The enemy side is reset too in the source even though half_twice's
        # own gate is player-only (dead code for the enemy, reproduced
        # anyway for fidelity).
        attacker_p.flags["_halfTwiceUsed"] = False
        attacker_e.flags["_halfTwiceUsed"] = False

        for pokemon, own_team, opp_pokemon, opp_team in (
            (attacker_p, p_team, attacker_e, e_team),
            (attacker_e, e_team, attacker_p, p_team),
        ):
            if not pokemon.flags.get("_g3Entered"):
                pokemon.flags["_g3Entered"] = True
                if ability_config is not None:
                    ability_config.on_switch_in(pokemon, own_team, opp_pokemon, opp_team, battle_config)

        # Ditto transform (species 132), bundle.deobfuscated.js:55463-55480
        if attacker_p.species_id == 132 and not attacker_p.flags.get("_transformed"):
            attacker_p.flags["_transformed"] = True
            attacker_p.types = attacker_e.types
            attacker_p.base_stats = attacker_e.base_stats

        player_items = _own_items(attacker_p)
        enemy_items = _own_items(attacker_e)
        player_speed, enemy_speed = _compute_speeds(
            attacker_p, attacker_e, player_items, enemy_items, traits, ability_config, battle_config
        )

        player_move = get_best_move(
            attacker_p.types, attacker_p.base_stats, attacker_p.species_id, attacker_p.move_tier, attacker_p.held_item,
            has_multitype=attacker_p.gen3_ability == "multitype",
        )
        enemy_move = get_best_move(
            attacker_e.types, attacker_e.base_stats, attacker_e.species_id, attacker_e.move_tier, attacker_e.held_item,
            has_multitype=attacker_e.gen3_ability == "multitype",
        )

        # 1.5(a) mutual-immunity Wonder Guard softlock (approximation: a
        # move that would deal 0 type-effectiveness damage against a
        # Wonder Guard holder, on both sides at once)
        if attacker_p.gen3_ability == "wonder_guard" or attacker_e.gen3_ability == "wonder_guard":
            if _would_whiff(player_move, attacker_p, attacker_e) and _would_whiff(enemy_move, attacker_e, attacker_p):
                attacker_p.current_hp = 0
                attacker_e.current_hp = 0
                continue

        first_actor = decide_turn_order(
            attacker_p, attacker_e, player_items, enemy_items, traits, resolve_trick_room(),
            player_speed=player_speed, enemy_speed=enemy_speed,
        )
        _p_turn = ("player", attacker_p, p_idx, p_team, "enemy", attacker_e, e_team)
        _e_turn = ("enemy", attacker_e, e_idx, e_team, "player", attacker_p, p_team)
        order = [_p_turn, _e_turn] if first_actor == "player" else [_e_turn, _p_turn]

        for side, mover, mover_idx, mover_team, target_side, target, target_team in order:
            if mover.current_hp <= 0 or target.current_hp <= 0:
                continue
            # Pre-turn status ticks, bundle.deobfuscated.js:55647-55710. The
            # source pushes a `status_tick` onto its detailed log at each of
            # these four points, with `{side, idx, status, hpChange: 0,
            # hpAfter: currentHp}`; the oracle's `status_events` projection
            # (tools/battle-oracle/run-fixture.js:189-201) carries exactly that
            # shape. Observation only -- no branch, no RNG draw and no
            # combatant state below is altered by emitting them, which is what
            # keeps the already-agreeing winner/rounds/draw-count/final-state
            # results untouched.
            if mover.flags.pop("flinch", False):
                # 55655-55668: push, clear the flag, skip the turn.
                status_events.append(_pre_turn_tick(side, mover_idx, mover, "flinch"))
                continue
            if mover.flags.pop("_frozenOnEntry", False):
                mover.status = "freeze"
            # 55673-55686 (freeze) and 55687-55710 (sleep) are two separate
            # branches of the same block, and `resolve_pre_turn_status` folds
            # both into one boolean, so read the status BEFORE calling it:
            # afterwards a woken sleeper is indistinguishable from a combatant
            # that was never asleep.
            status_before = mover.status
            skipped = resolve_pre_turn_status(mover)
            if status_before == "freeze":
                # 55676-55685: always a skip, no draw.
                status_events.append(_pre_turn_tick(side, mover_idx, mover, "freeze_skip"))
            elif status_before == "sleep":
                # 55688: one `rng() < 0.5` draw, already consumed inside
                # `resolve_pre_turn_status`. Waking clears the status and the
                # turn PROCEEDS (55689-55698); failing to wake skips it
                # (55699-55709).
                status_events.append(
                    _pre_turn_tick(side, mover_idx, mover, "sleep_skip" if skipped else "sleep_wake")
                )
            if skipped:
                continue

            # `mergeBattleConfigs` semantics (bundle.deobfuscated.js:80991-
            # 81051, faithfully reproduced here -- see `resolve_trick_room`'s
            # docstring above for the full citation): `beforeTurn` and
            # `onBeforeAttack` are both generic (non-threaded) hooks. When
            # `merged` (both configs present), the true source calls BOTH
            # providers in ability-then-traits order for their side effects
            # (Truant's loaf-counter flip, Mirror Coat's stored-damage
            # counter-hit, a trait's one-shot stat change, ...) but the
            # merge wrapper discards whatever either one returns -- so
            # `"skip"`/a truthy abort NEVER actually skips a turn in a
            # merged battle, even though the exact same ability or trait
            # WOULD skip it in an ability-only (no passives) or
            # (Endless-only, out of scope) traits-only battle. Order matters
            # too: the source's `O` (first arg to `mergeBattleConfigs`) is
            # always the ability config, `iu` the traits config -- ability
            # hooks fire before trait hooks, not the reverse.
            #
            # `counter_hit` is an observation sink only (M4). The source's
            # `mirror_coat` branch pushes a real `type: "attack"` entry into
            # its `detailedLog` at 58116-58132 -- an event of the SAME family
            # `event.battle.turns` already compares, not a new one -- while
            # the port applied the damage silently. That showed up as
            # `event.battle.turns[i].events[len]` js=2 / py=1 on a Gen3 route
            # that fights a Wynaut, with rounds, RNG draws, winner and every
            # final HP agreeing exactly. Passing the sink changes no branch
            # and consumes no draw.
            counter_hit: list = []
            if merged:
                ability_config.before_turn(mover, target, counter_hit)
                traits_config.before_turn(mover, target, side, battle_config)
                skip = None
            elif ability_config is not None:
                skip = ability_config.before_turn(mover, target, counter_hit)
            elif traits_config is not None:
                skip = traits_config.before_turn(mover, target, side, battle_config)
            else:
                skip = None
            for hit in counter_hit:
                battle_events.append(_counter_attack_event(
                    side, mover, mover_idx, target_side, target,
                    e_idx if target_side == "enemy" else p_idx, hit,
                ))
            if skip == "skip":
                continue

            if merged:
                ability_config.on_before_attack(mover, target)
                # `traits_config.on_before_attack` is a permanent stub in
                # the source (unconditional `return false`, no side effects,
                # docs/logic-notes-traitsconfig.md section 0.1) -- calling it
                # would be a functional no-op, so it's not called here.
                aborted = False
            elif ability_config is not None:
                aborted = bool(ability_config.on_before_attack(mover, target))
            else:
                aborted = False
            if aborted:
                # M7 (F-K): the source has a THIRD "was sent out" site, and
                # the port had only two. When `onBeforeAttack` aborts the turn
                # AND the hook killed the mover in doing so, the source finds
                # the first alive member on the mover's own side and, for the
                # player, adds it to `playerParticipants`
                # (bundle.deobfuscated.js:55723-55742):
                #
                #     if (BEX["currentHp"] <= 0x0) {
                #       const BEA = (BEx === "player" ? Bct : Bcg)
                #         .map((BEH, BEC) => ({ p: BEH, idx: BEC }))
                #         .find((BEH) => BEH["p"]["currentHp"] > 0x0);
                #       if (BEA) {
                #         BEx === "player" && BcW["add"](BEA["idx"]);
                #         ... push a `send_out` log entry ...
                #       }
                #     }
                #
                # `BcW` is the same set `runBattle` returns as
                # `playerParticipants` (56784), and this is the only one of
                # its three `add` sites (55729, 56456, 56493) the port did not
                # carry -- `_handle_faint` implements the other two.
                #
                # The hook that reaches it is `own_tempo`: a 20% roll deals
                # `max(1, floor(maxHp * 0.1))` self-damage and returns truthy
                # (`battle_abilities.on_before_attack`), so a mover already
                # under 10% HP faints without any combat faint ever occurring.
                # Observed as `battles[0].player_participants[len]` js 2 / py 1
                # on a Story Gen4 route, with winner, rounds and RNG draws all
                # agreeing exactly -- participation bookkeeping only.
                #
                # The `send_out` log entry has no port counterpart: the port
                # keeps no full battle event log, and the compared per-turn
                # projection carries `type === "attack"` events only
                # (driver.js's `normalizeAttackEvent`). Inventing one here
                # would be comparing the adapters, so only the participant set
                # -- which IS compared -- is reproduced.
                if mover.current_hp <= 0:
                    next_idx = _first_alive_index(p_team if side == "player" else e_team)
                    if next_idx is not None and side == "player":
                        player_participants.add(next_idx)
                continue

            move = get_best_move(
                mover.types, mover.base_stats, mover.species_id, mover.move_tier, mover.held_item,
                has_multitype=mover.gen3_ability == "multitype",
            )
            if ability_config is not None:
                overridden = ability_config.override_move(mover)
                if overridden is not None:
                    move = overridden
            if _struggle_stalemate(player_move, enemy_move, attacker_p, attacker_e):
                move = MoveInstance(power=50, type="Normal", name="Struggle", is_special=False, typeless=True)
            elif not move.no_damage and _is_immune(mover, move, target):
                # M7-COMBINED (F-G): `!noDamage` is the FIRST conjunct of the
                # source's immunity substitution, not an afterthought
                # (bundle.deobfuscated.js:55768-55771):
                #
                #     !BEe["noDamage"] &&
                #       !hasPassive(BcV, "neutralize") &&
                #       _immuneTo(BEX, BEe, BEf) &&
                #       (BEe = BEP)
                #
                # A no-damage move is never replaced by Struggle -- it falls
                # through to the "But nothing happened!" branch at 55774-55801
                # and is logged as a zero-damage attack under its OWN name.
                #
                # The port checked immunity first and swapped unconditionally,
                # so Magikarp's hardcoded Splash (a Normal-type `no_damage`
                # move) became Struggle whenever the target was a Ghost, which
                # Normal is immune to. The M7-combined goal-directed sweep
                # reached exactly that: a Nuzlocke Gen1 Magikarp against a
                # Gastly, js `Splash` for 0 damage versus py `Struggle` for 5,
                # which then diverged the whole rest of the battle (rounds 7 vs
                # 6, rng_draws 22 vs 18).
                if not (has_passive(traits, "neutralize")):
                    move = MoveInstance(power=50, type="Normal", name="Struggle", is_special=False, typeless=True)

            mover_idx = p_idx if side == "player" else e_idx
            target_idx = e_idx if target_side == "enemy" else p_idx

            if move.no_damage:
                # bundle.deobfuscated.js:55774-55801 logs a zero-damage
                # `attack` event on this branch before moving on; mirrored
                # here so the projected family is complete, not truncated.
                battle_events.append(_attack_event(
                    side, mover, mover_idx, target_side, target, target_idx,
                    move, move.type, 0, 1, False, False,
                ))
                continue

            if side == "player" and "Dragon" in {t.capitalize() for t in mover.types} and has_passive(traits, "dragon_first_crit") and not battle_config.fired_flags.get("dragon_first_crit"):
                battle_config.fired_flags["dragon_first_crit"] = True
                mover.force_crit = True

            mover_items = _own_items(mover)
            target_items = _own_items(target)
            result = calc_damage(mover, target, move, mover_items, target_items, traits, side, battle_config)
            mover.force_crit = False
            target.flags["_incomingCrit"] = result.crit
            target.flags["_incomingSuperEffective"] = result.type_eff >= 2
            mover.flags["_lastMoveIsSpecial"] = bool(move.is_special)
            mover.flags["_lastMoveName"] = move.name
            mover.flags["_lastMoveType"] = result.move_type

            # Damage modifier chain, exact source order (bundle.deobfuscated.js:55822-55899):
            # attackerDamageMod -> all_more/all_half/half_twice (attacker==player)
            # -> life_orb -> beforeDamage -> overtime multiplier ->
            # all_more/all_half/dmg_cap/team_damage_spread (DEFENDER==player).
            # all_more/all_half read the same trait id at BOTH points, gated on
            # opposite sides (attacker-is-player vs defender-is-player) -- since
            # attacker/defender are always on opposite sides in a player-vs-enemy
            # battle, at most one of the two blocks ever actually fires per hit.
            damage = result.damage
            if ability_config is not None:
                damage = ability_config.attacker_damage_mod(mover, target, damage)
            if traits_config is not None:
                damage = traits_config.attacker_damage_mod(mover, target, side, damage, battle_config)
            if side == "player":
                if has_passive(traits, "all_more"):
                    damage = int(damage * 1.5)
                if has_passive(traits, "all_half"):
                    damage = int(damage * 0.5)
                if has_passive(traits, "half_twice"):
                    damage = int(damage * 0.5)
            if mover.held_item is not None and mover.held_item.id == "life_orb":
                damage = int(damage * 1.3)

            if ability_config is not None:
                damage = ability_config.before_damage(target, mover, damage, battle_config)
            if traits_config is not None:
                damage = traits_config.before_damage(target, target_side, mover, damage, battle_config)
            damage = damage * overtime_mult

            if target_side == "player":
                if has_passive(traits, "all_more"):
                    damage = int(damage * 1.5)
                if has_passive(traits, "all_half"):
                    damage = int(damage * 0.5)
                if has_passive(traits, "dmg_cap") and damage > 200:
                    damage = 200
                if has_passive(traits, "team_damage_spread") and damage > 0:
                    alive_indices = [i for i, m in enumerate(p_team) if m.current_hp > 0]
                    if len(alive_indices) > 1:
                        per_member = damage // len(alive_indices)
                        redirected = 0
                        target_idx = p_team.index(target)
                        for i in alive_indices:
                            if i == target_idx or per_member <= 0:
                                continue
                            before = p_team[i].current_hp
                            p_team[i].current_hp = max(0, p_team[i].current_hp - per_member)
                            redirected += before - p_team[i].current_hp
                        damage = max(0, damage - redirected)

            damage = max(0, damage)
            pre_hp = target.current_hp
            target.current_hp = max(0, target.current_hp - damage)
            target.flags["_lastOverkill"] = max(0, damage - pre_hp)

            survived_at_one = False
            if damage > 0 and target.current_hp == 0:
                if target_side == "player":
                    if has_passive(traits, "sturdy") and not target.flags.get("_sturdyUsed"):
                        target.flags["_sturdyUsed"] = True
                        target.current_hp = 1
                        survived_at_one = True
                    elif has_passive(traits, "rock_sturdy") and "Rock" in {t.capitalize() for t in target.types} and not target.flags.get("_sturdyUsed"):
                        target.flags["_sturdyUsed"] = True
                        target.current_hp = 1
                        survived_at_one = True
                    elif has_passive(traits, "fighting_survive") and "Fighting" in {t.capitalize() for t in target.types} and not target.flags.get("_fightSurviveUsed"):
                        target.flags["_fightSurviveUsed"] = True
                        target.current_hp = 1
                        survived_at_one = True
                        if traits_config is not None:
                            traits_config.trigger_fighting_rally("player", p_team, e_team)

            if not survived_at_one and mover.held_item is not None and mover.held_item.id == "kings_rock" and target.current_hp > 0:
                if rng.rng() < 0.3:
                    target.flags["flinch"] = True

            # `actual_damage` (JS `BEs = BEq - target.currentHp`) is the HP
            # actually lost AFTER sturdy/Focus-Sash/Fighting-Spirit clamp the
            # target to 1 HP -- afterAttack (and every recoil/gate below
            # keyed on "did this hit really connect") reads THIS value, not
            # the raw pre-clamp `damage`; whenAttacked still reads raw
            # `damage`, matching the source's own BEF/BEs split
            # (bundle.deobfuscated.js:55990-55998, 56354-56404).
            actual_damage = pre_hp - target.current_hp

            # Oracle observation only (M3.3b workstream 5). Emitted at the
            # source's own push point: bundle.deobfuscated.js:55979-55995
            # pushes the `attack` event AFTER the hit has landed (so
            # `targetHpAfter` already reflects the sturdy/Fighting-Spirit
            # clamps) and BEFORE `whenAttacked` runs. `damage` is the source's
            # `BEF` -- the raw post-modifier value `whenAttacked` also reads --
            # not the post-clamp `BEs`/`actual_damage`.
            battle_events.append(_attack_event(
                side, mover, mover_idx, target_side, target, target_idx,
                move, result.move_type, damage, result.type_eff, result.crit, move.is_special,
            ))

            if ability_config is not None:
                ability_config.when_attacked(target, mover, damage)
            if traits_config is not None:
                traits_config.when_attacked(target, target_side, mover, damage)
            # M7 (F-J): `afterAttack` is GATED on the hit having actually
            # connected. The source's main-hit call is
            # (bundle.deobfuscated.js:55998-56001):
            #
            #     const BEs = BEq - BEf["currentHp"];
            #     if (BEs > 0x0 && B71 != null && B71["afterAttack"] &&
            #         B71["afterAttack"](BEX, BEm, BEx, BEf, BEn, BEl, BEs, ...))
            #
            # `whenAttacked` immediately above it is deliberately NOT gated
            # (55995-55998) and still is not, here or in the source. Both
            # extra-attack sites already carried this gate on their own actual
            # damage (56264-56282 / 56343-56362, mirrored below in
            # `_apply_half_twice_extra_attack` and
            # `_apply_dragon_first_double_extra_attack`), and
            # `_apply_post_hit_traits` carries it too; the MAIN hit was the
            # one place the port called the hook unconditionally.
            #
            # It matters whenever a hit computes damage but removes no HP.
            # The observed case is `wonder_guard`: `calcDamage` zeroes the
            # result for a non-super-effective, non-critical hit (55063), so
            # a Seviper's Acid on a Shedinja deals 0. The source therefore
            # never runs `afterAttack`, and Seviper's `venom_strike` never
            # poisons it. The port ran the hook, applied 2 poison stacks to a
            # 1-max-HP Shedinja, and lost the battle to the next status tick:
            # js 13 rounds / 50 draws / player won, py 6 rounds / 22 draws /
            # game over.
            if actual_damage > 0:
                if ability_config is not None:
                    ability_config.after_attack(mover, target, actual_damage, mover_team)
                if traits_config is not None:
                    traits_config.after_attack(mover, side, target, target_side, actual_damage, mover_team, target_team)

            _apply_post_hit_traits(
                mover, target, side, target_side, result.crit, actual_damage, traits,
                no_heal_revive, traits_config, p_team,
            )

            # `half_twice`/`dragon_first_double` extra attacks (bundle.
            # deobfuscated.js:56220-56364) -- sequential, not mutually
            # exclusive: half_twice's own HP mutation happens FIRST and
            # dragon_first_double's guard re-checks post-half_twice state,
            # matching the source's own back-to-back (not nested) `if`
            # blocks. Both key off `actual_damage` (the MAIN hit's, gated
            # inside each function), not each other's extra-hit damage.
            _apply_half_twice_extra_attack(
                mover, target, side, target_side, move, mover_items, target_items,
                traits, battle_config, ability_config, traits_config, mover_team, target_team,
                overtime_mult, actual_damage, no_heal_revive,
            )
            _apply_dragon_first_double_extra_attack(
                mover, target, side, target_side, move, mover_items, target_items,
                traits, battle_config, ability_config, traits_config, mover_team, target_team,
                overtime_mult, actual_damage, no_heal_revive,
            )

            # The four post-hit HP changes, source 56366-56442. Each mutates
            # HP AFTER the attack record above was appended, so each emits its
            # own `effect` record (M6/N10) -- without one, a replay shows the
            # mover's HP moving with no event to attribute it to, and the
            # attack record's own `attacker_hp_after` silently disagrees with
            # the post-battle roster.
            if target.held_item is not None and target.held_item.id == "rocky_helmet":
                recoil = max(1, int(mover.max_hp * 0.12))
                mover.current_hp = max(0, mover.current_hp - recoil)
                battle_events.append(_effect_event(
                    side, mover_idx, -recoil, mover.current_hp, "rocky_helmet",
                ))
            # `mover.current_hp > 0` is the source's own guard (56386), which
            # this port omitted before M6. It is HP-neutral -- the only way to
            # reach it at 0 HP is the rocky_helmet block just above, and
            # `max(0, 0 - recoil)` is 0 either way -- but it decides whether
            # the record above exists, so it has to be right now that the
            # record does.
            if (
                side == "enemy"
                and actual_damage > 0
                and mover.current_hp > 0
                and has_passive(traits, "enemy_recoil")
            ):
                recoil = max(1, int(actual_damage * 0.2))
                mover.current_hp = max(0, mover.current_hp - recoil)
                battle_events.append(_effect_event(
                    side, mover_idx, -recoil, mover.current_hp, "enemy_recoil",
                ))
            if mover.held_item is not None and mover.held_item.id == "life_orb" and actual_damage > 0 and mover.current_hp > 0:
                recoil = max(1, int(mover.max_hp * 0.1))
                mover.current_hp = max(0, mover.current_hp - recoil)
                battle_events.append(_effect_event(
                    side, mover_idx, -recoil, mover.current_hp, "life_orb",
                ))
            # Shell Bell (bundle.deobfuscated.js:56420-56431). N28. Four
            # source facts the earlier port missed:
            #   1. the source's guard is `!BIB` -- `BIB` is
            #      `hasPassive(BcV, "no_heal_revive")` (:55299), NOT a
            #      `damage > 0` test. There is no damage gate here at all.
            #   2. the amount is `max(1, floor(BEF * 0.15 * BXT))`: the
            #      `max(1, ...)` floor means a hit too small to earn a whole
            #      percent still heals exactly 1 HP, and -- because the
            #      source has no damage gate -- so does a fully-absorbed
            #      0-damage hit. The old `int(damage * heal_pct)` returned 0
            #      for both.
            #   3. `BXT` is a 2x/1x MULTIPLIER applied as a third factor. The
            #      source-exact spelling is retained here; for the integer
            #      damage values this engine produces, folding the boosted
            #      case to `damage * 0.3` was not itself a behavioral defect.
            #   4. a positive applied heal calls `aspearOnHeal`, which is the
            #      `heal_boost_stat` accumulator -- it can consume RNG draws
            #      and grant stat stages, so omitting it desynchronised the
            #      RNG stream as well as the stats.
            # `damage` here is the source's `BEF` (raw post-modifier damage,
            # :55901), deliberately NOT the missing-HP-clamped `BEs`
            # (`actual_damage`) that the Life Orb branch above uses.
            if not no_heal_revive and mover.held_item is not None and mover.held_item.id == "shell_bell" and side == "player":
                heal_mult = 2 if has_passive(traits, "heal_boost") else 1
                heal = max(1, math.floor(damage * 0.15 * heal_mult))
                heal = min(heal, mover.max_hp - mover.current_hp)
                if heal > 0:
                    mover.current_hp += heal
                    _aspear_on_heal(mover, "player", traits, heal)
                    battle_events.append(_effect_event(
                        side, mover_idx, heal, mover.current_hp, "shell_bell",
                    ))

            # Two INDEPENDENT faint blocks in the source (bundle.deobfuscated.js:
            # 56442/56480) -- when the target dies from the hit AND the
            # mover simultaneously dies from its own recoil, both onKO/
            # onFaint/send-out sequences fire, not just the target's (a
            # confirmed defect this `if`/`if` -- not `if`/`elif` -- fixes,
            # CODEX.md issue 5).
            if target.current_hp <= 0:
                _handle_faint(target, target_side, p_team, e_team, mover, side, ability_config, traits_config, battle_config, player_participants, battle_events)
            if mover.current_hp <= 0:
                _handle_faint(mover, side, p_team, e_team, None, None, ability_config, traits_config, battle_config, player_participants, battle_events)

        p_alive = _first_alive_index(p_team)
        e_alive = _first_alive_index(e_team)
        if p_alive is not None:
            leftover_heal(p_team[p_alive], "player", traits, has_boost=has_passive(traits, "heal_boost"))
        if e_alive is not None:
            leftover_heal(e_team[e_alive], "enemy", traits, has_boost=False)
        if has_passive(traits, "normal_heal"):
            for member in p_team:
                if member.current_hp > 0 and "Normal" in {t.capitalize() for t in member.types}:
                    heal = max(1, int(member.max_hp * 0.1))
                    member.current_hp = min(member.max_hp, member.current_hp + heal)

        _status_tick_round(
            p_team, "player", e_team, traits, ability_config, traits_config,
            battle_config, player_participants, no_heal_revive,
            status_events, hook_trace,
        )
        _status_tick_round(
            e_team, "enemy", p_team, traits, ability_config, traits_config,
            battle_config, player_participants, no_heal_revive,
            status_events, hook_trace,
        )

        if traits_config is not None:
            hook_trace.append({"hook": "sweep_kos"})
            traits_config.sweep_kos(p_team, e_team, battle_config)
        if team_alive(p_team) and team_alive(e_team):
            if ability_config is not None:
                ability_config.end_of_round(p_team, e_team, battle_config)
            if traits_config is not None:
                traits_config.end_of_round(p_team, e_team, battle_config)

        current_total = total_hp()
        if current_total == last_total_hp:
            stalemate_counter += 1
        else:
            stalemate_counter = 0
            last_total_hp = current_total
        if stalemate_counter >= _STALEMATE_ROUNDS:
            pi = _first_alive_index(p_team)
            ei = _first_alive_index(e_team)
            if pi is not None:
                p_team[pi].current_hp = 0
            if ei is not None:
                e_team[ei].current_hp = 0
            stalemate_counter = 0

    for member in p_team + e_team:
        if member.flags.get("_critLevelBase") is not None:
            original_level = member.flags.pop("_critLevelBase")
            member.level = original_level

    player_won = player_still_in_it() and not team_alive(e_team)
    return BattleResult(
        player_won=player_won,
        player_team=p_team,
        enemy_team=e_team,
        player_participants=player_participants,
        rounds=round_num,
        status_events=status_events,
        hook_trace=hook_trace,
        battle_events=battle_events,
    )


def _compute_speeds(player, enemy, player_items, enemy_items, traits, ability_config, battle_config):
    """Port of the per-round speed computation inside `runBattle`
    (bundle.deobfuscated.js:55481-55491) -- exact source order: base
    `getEffectiveStat` -> paralysis halving -> the `spdef_stage_speed` trait
    (always keyed off the PLAYER's own stages, matching the source) ->
    `Gen3AbilityConfig.modify_speed` (Swift Swim/Chlorophyll/Mirror Coat)
    LAST. `decide_turn_order` previously recomputed a modify_speed-less
    speed from scratch and silently discarded this function's result --
    CODEX.md issue 1, fixed by threading these two return values into
    `decide_turn_order`'s `player_speed`/`enemy_speed` kwargs instead.
    Deliberately not floored to `int` here: Mirror Coat's `modify_speed`
    can return `float('inf')`/`0.0`, and `int(float('inf'))` raises --
    `decide_turn_order`'s comparisons work fine on floats.
    """
    from pokelike.battle import get_effective_stat, has_passive

    player_speed = get_effective_stat(player, "speed", player_items, player.stages)
    enemy_speed = get_effective_stat(enemy, "speed", enemy_items, enemy.stages)
    if player.paralyzed:
        player_speed = player_speed // 2
    if enemy.paralyzed:
        enemy_speed = enemy_speed // 2
    if has_passive(traits, "spdef_stage_speed"):
        spdef_stage = player.stages.get("spdef") or 0
        if spdef_stage > 0:
            player_speed = int(player_speed * (1 + 0.1 * spdef_stage))
    if ability_config is not None:
        player_speed = ability_config.modify_speed(player, player_speed, enemy, battle_config)
        enemy_speed = ability_config.modify_speed(enemy, enemy_speed, player, battle_config)
    return player_speed, enemy_speed


def _can_damage(move: MoveInstance, attacker: Combatant, defender: Combatant,
                *, mirror_coat_blocks: bool) -> bool:
    """Port of the source's two "could this move actually hurt that target?"
    predicates, which differ in exactly one clause.

    `runBattle` builds both, back to back
    (bundle.deobfuscated.js:55513-55538):

        BIG = (move, defender, attacker) => {
          if (move["noDamage"] || BIN(move)) return false;   // <-- BIN only here
          const wg = defender["_gen3Ability"] === "wonder_guard";
          if (_immuneTo(attacker, move, defender)) return !wg;
          const eff = getTypeEffectiveness(move["type"], defender["types"] || ["Normal"]);
          return !(wg && eff < 0x2);
        }
        BIu = (move, defender, attacker) => {           // identical but for BIN
          if (move["noDamage"]) return false;
          ... same three lines ...
        }

    with `BIN = (m) => m["name"] === "Mirror Coat"`. `mirror_coat_blocks`
    selects between them: `BIG` (the Struggle stalemate, which treats Mirror
    Coat as unable to damage on its own) and `BIu` (the Wonder Guard softlock,
    which does not).

    M7 (F-L) also corrected the immunity branch. It used to read
    ``attacker.gen3_ability != "wonder_guard" and defender.gen3_ability !=
    "wonder_guard"`` for the "whiffed" answer, where both source predicates
    say the move can damage exactly when the DEFENDER lacks Wonder Guard --
    the attacker's own ability is not consulted at all. Under the softlock
    guard (which already requires at least one side to hold Wonder Guard) the
    two agree except when the defender is the holder, where the old form
    reported a hit the source calls a whiff.
    """
    if move.no_damage:
        return False
    if mirror_coat_blocks and move.name == "Mirror Coat":
        return False
    from pokelike import data

    is_wonder_guard = defender.gen3_ability == "wonder_guard"
    if _is_immune(attacker, move, defender):
        return not is_wonder_guard
    type_eff = data.get_type_effectiveness(move.type or "Normal", defender.types or ("Normal",))
    return not (is_wonder_guard and type_eff < 2)


def _would_whiff(move: MoveInstance, attacker: Combatant, defender: Combatant) -> bool:
    """`!BIu` -- the Wonder Guard softlock's half of the pair above."""
    return not _can_damage(move, attacker, defender, mirror_coat_blocks=False)


def _struggle_stalemate(player_move: MoveInstance, enemy_move: MoveInstance,
                        attacker_p: Combatant, attacker_e: Combatant) -> bool:
    """The source's `BIw` -- "neither side can damage the other", which makes
    BOTH movers use Struggle (`BIw && (BEe = BED)`, 55767).

    M7 (F-L). The port drove this off `_would_whiff`, i.e. off `BIu`, and so
    missed both of the things that make `BIw` different
    (bundle.deobfuscated.js:55524-55528):

        BIU = BIG(BIC, BIi, BIh),                 // player's move vs the enemy
        BIb = BIG(BIr, BIh, BIi),                 // enemy's move vs the player
        BIR = BIN(BIC) ? BIb : BIU,
        BIZ = BIN(BIr) ? BIU : BIb,
        BIw = !BIR && !BIZ,

    1. `BIG` treats a **Mirror Coat** move as unable to damage on its own,
       which `BIu` does not; and
    2. when a side's move IS Mirror Coat, its entry is replaced by the OTHER
       side's capability -- Mirror Coat only deals damage if something damaged
       it first, so its reach is the opponent's reach.

    Observed as js `Struggle` (Normal, typeless, 4 damage) against py `Mirror
    Coat` (Psychic, 0.5 effective, 3 damage) on a Story Gen3 route, which then
    diverged the rest of the battle: 4 rounds / 9 draws against 5 / 11.
    Reproducer `findings/M7-divergence-hunt_story_gen3_0066.json`.
    """
    player_can = _can_damage(player_move, attacker_p, attacker_e, mirror_coat_blocks=True)
    enemy_can = _can_damage(enemy_move, attacker_e, attacker_p, mirror_coat_blocks=True)
    reach_p = enemy_can if player_move.name == "Mirror Coat" else player_can
    reach_e = player_can if enemy_move.name == "Mirror Coat" else enemy_can
    return not reach_p and not reach_e


def _is_immune(attacker: Combatant, move: MoveInstance, defender: Combatant) -> bool:
    from pokelike import data
    from pokelike.battle import smack_down_hits

    if move.typeless:
        return False
    type_eff = data.get_type_effectiveness(move.type or "Normal", defender.types or ("Normal",))
    if type_eff == 0 and not smack_down_hits(attacker, move.type or "Normal"):
        return True
    if defender.gen3_ability == "levitate" and (move.type or "").capitalize() == "Ground" and attacker.gen3_ability != "smack_down":
        return True
    if defender.gen3_ability == "storm_drain" and (move.type or "").capitalize() == "Water":
        return True
    return False


def _aspear_on_heal(pokemon: Combatant, side: str, traits, heal_amount: int = 0) -> None:
    """Port of `aspearOnHeal` (bundle.deobfuscated.js:55140-55163): the
    `heal_boost_stat` trait's on-heal stat-gain hook. Player-only. Two
    branches in the source: when called with a positive heal amount (every
    call site below), the heal accumulates into `_aspearAcc` and grants a
    random stat stage every time 50 accumulated HP is crossed; a separate
    zero-argument "count" branch (capped at 6 total procs) exists in the
    source for call sites that don't have a heal amount handy, already
    covered for the Grass-tier heal by `battle_traits.TraitsConfig`'s own
    `heal_boost_stat` handling -- not duplicated here.
    """
    if side != "player" or not has_passive(traits, "heal_boost_stat") or heal_amount <= 0:
        return
    acc = pokemon.flags.get("_aspearAcc", 0) + heal_amount
    while acc >= 50:
        acc -= 50
        stat = _STAGE_STATS[int(rng.rng() * len(_STAGE_STATS))]
        apply_stage_change(pokemon, stat, 1)
    pokemon.flags["_aspearAcc"] = acc


def _apply_post_hit_traits(
    mover: Combatant,
    target: Combatant,
    side: str,
    target_side: str,
    is_crit: bool,
    actual_damage: int,
    traits,
    no_heal_revive: bool,
    traits_config=None,
    player_team: Optional[Sequence[Combatant]] = None,
) -> None:
    """Port of the inline post-hit trait chain in `runBattle`'s main attack
    branch (bundle.deobfuscated.js:56000-56218) -- ten trait effects that
    live directly in the battle loop body, not in `buildTraitsConfig`'s
    hook object (matching where the source itself puts them), and that were
    completely unported before this (CODEX.md issue 13; `crit_flinch`/
    `lifesteal`/`rand_nerf`/`rand_boost`/`speed_diff`/`bug_critlvl`/
    `bug_strip` were found while tracing that issue, not separately listed
    in the audit). Exact source order: `maxhp_strike` -> `crit_boost` ->
    `crit_lifesteal` -> `crit_flinch` -> `lifesteal` -> `rand_nerf` ->
    `rand_boost` -> `speed_diff` -> `bug_critlvl` -> `bug_strip`. Runs only
    when `actual_damage > 0` and the relevant Pokemon is still alive at each
    step, matching the source's own per-effect guards. Any resulting faint
    is picked up by the caller's own `target.current_hp <= 0`/
    `mover.current_hp <= 0` checks immediately after this returns -- none of
    these ten effects call a KO/faint hook themselves in the source either.

    **`half_twice`/`dragon_first_double` are deliberately NOT called from
    here** (P0.3, `tools/battle-oracle/fixtures/half_twice_*.json`/
    `dragon_first_double_*.json`): those two extra-attack traits re-invoke
    only a 5-effect subset of this chain (`crit_boost`/`crit_lifesteal`/
    `crit_flinch`/`speed_diff`/`bug_critlvl`) via the source's own separate
    `BIO` helper function (bundle.deobfuscated.js:55300-55404) -- textually
    DISTINCT from this function's inline 10-effect chain, not a shared call
    this function also makes. See `_bio_post_hit` (that subset, ported
    separately) and `_apply_half_twice_extra_attack`/
    `_apply_dragon_first_double_extra_attack` (the two callers, invoked from
    `run_battle` right after this function returns) below. Still
    unaddressed at the same "simplified secondary attack" level (see
    `battle_traits.py`'s module docstring): `elec_lead`, `fairy_opening_
    volley`, `rock_explode` call `calc_damage` directly but do not re-invoke
    `when_attacked`/`after_attack`.
    """
    if actual_damage <= 0:
        return

    if side == "player" and target.current_hp > 0 and has_passive(traits, "maxhp_strike") and "Normal" in {t.capitalize() for t in mover.types}:
        extra = max(1, int(mover.max_hp * 0.05))
        target.current_hp = max(0, target.current_hp - extra)

    if is_crit and mover.current_hp > 0 and has_passive(traits, "crit_boost"):
        apply_stage_change(mover, "atk", 1)
        apply_stage_change(mover, "special", 1)
        apply_stage_change(mover, "speed", 1)

    if not no_heal_revive and is_crit and side == "player" and mover.current_hp > 0 and has_passive(traits, "crit_lifesteal"):
        heal = max(1, int(actual_damage))
        heal = min(heal, mover.max_hp - mover.current_hp)
        if heal > 0:
            mover.current_hp += heal
            _aspear_on_heal(mover, side, traits, heal)

    if is_crit and target.current_hp > 0 and has_passive(traits, "crit_flinch") and rng.rng() < 0.5:
        target.flags["flinch"] = True

    if not no_heal_revive and side == "player" and mover.current_hp > 0 and has_passive(traits, "lifesteal"):
        heal = max(1, int(actual_damage * 0.2))
        heal = min(heal, mover.max_hp - mover.current_hp)
        if heal > 0:
            mover.current_hp += heal
            _aspear_on_heal(mover, side, traits, heal)

    # `rand_nerf` (bundle.deobfuscated.js:56083-56095). P0.9. The source does
    # NOT stop after debuffing the target: it then mirrors the SAME chosen
    # stat onto the player's active member via the active config's
    # `mirrorEnemyActiveDebuff(Bct, BEg, 1, BcM)` hook, where `B71` is
    # `runBattle`'s merged battle config and `Bct` is the player team. Only
    # one RNG draw is consumed -- the stat choice -- and the mirror reuses it
    # rather than rolling again. The hook itself (stage caps, blockers, and
    # first-alive active-member selection) is already ported as
    # `TraitsConfig.mirror_enemy_active_debuff`; it is called here, not
    # reimplemented, and is skipped entirely when the source's config is
    # absent (`B71 != null && B71["mirrorEnemyActiveDebuff"]`).
    if side == "player" and target.current_hp > 0 and has_passive(traits, "rand_nerf"):
        stat = _STAGE_STATS[int(rng.rng() * len(_STAGE_STATS))]
        apply_stage_change(target, stat, -1)
        if traits_config is not None and player_team is not None:
            traits_config.mirror_enemy_active_debuff(player_team, stat, 1)

    if mover.current_hp > 0 and has_passive(traits, "rand_boost") and mover.flags.get("_randBoostCount", 0) < 6:
        stat = _STAGE_STATS[int(rng.rng() * len(_STAGE_STATS))]
        apply_stage_change(mover, stat, 1)
        mover.flags["_randBoostCount"] = mover.flags.get("_randBoostCount", 0) + 1

    if side == "player" and target.current_hp > 0 and has_passive(traits, "speed_diff"):
        mover_speed = get_effective_stat(mover, "speed", _own_items(mover), mover.stages)
        target_speed = get_effective_stat(target, "speed", _own_items(target), target.stages)
        if mover_speed > target_speed:
            extra = min(200, int(4.5 * math.sqrt(mover_speed - target_speed)))
            reps = 2 if has_passive(traits, "ice_onhit_double") else 1
            for _ in range(reps):
                if target.current_hp <= 0:
                    break
                target.current_hp = max(0, target.current_hp - extra)

    if is_crit and side == "player" and mover.current_hp > 0 and "Bug" in {t.capitalize() for t in mover.types} and has_passive(traits, "bug_critlvl") and mover.flags.get("_critLevelBase") is None:
        mover.flags["_critLevelBase"] = mover.level
        mover.level += 20
        hp_buff = (mover.stat_buffs or {}).get("hp", 0)
        from pokelike import map_gen

        new_max_hp = int(map_gen.calc_hp(mover.base_stats.hp, mover.level) * (1 + 0.05 * hp_buff))
        delta = new_max_hp - mover.max_hp
        mover.max_hp = new_max_hp
        if delta > 0:
            mover.current_hp = min(mover.current_hp + delta, new_max_hp)

    if side == "player" and target.current_hp > 0 and "Bug" in {t.capitalize() for t in mover.types} and has_passive(traits, "bug_strip") and not target.flags.get("_bugLevelStripped"):
        target.flags["_bugLevelStripped"] = True
        target.level = max(1, target.level - 5)
        hp_buff = (target.stat_buffs or {}).get("hp", 0)
        from pokelike import map_gen

        new_max_hp = int(map_gen.calc_hp(target.base_stats.hp, target.level) * (1 + 0.05 * hp_buff))
        drop = max(0, target.max_hp - new_max_hp)
        target.max_hp = new_max_hp
        target.current_hp = max(0, target.current_hp - drop)


def _bio_post_hit(
    mover: Combatant,
    target: Combatant,
    side: str,
    target_side: str,
    is_crit: bool,
    actual_damage: int,
    traits,
    no_heal_revive: bool,
) -> None:
    """Port of the `BIO` helper (bundle.deobfuscated.js:55300-55404) -- the
    narrower 5-effect post-hit subset (`crit_boost`/`crit_lifesteal`/
    `crit_flinch`/`speed_diff`/`bug_critlvl`) that `half_twice`'s and
    `dragon_first_double`'s own extra attacks re-invoke. Deliberately
    excludes `maxhp_strike`/plain `lifesteal`/`rand_nerf`/`rand_boost`/
    `bug_strip` -- `BIO` is a genuinely separate, textually distinct
    function from `_apply_post_hit_traits`'s inline main-hit chain in the
    source, not a shared call the main hit also makes, so this is its own
    standalone port rather than a call into that function. `actual_damage`
    here is the EXTRA hit's own dealt damage (JS `BIA`), not the main hit's
    -- the top-level `if (!(BIA<=0))` guard wraps this function's entire
    body.
    """
    if actual_damage <= 0:
        return

    if is_crit and mover.current_hp > 0 and has_passive(traits, "crit_boost"):
        apply_stage_change(mover, "atk", 1)
        apply_stage_change(mover, "special", 1)
        apply_stage_change(mover, "speed", 1)

    if not no_heal_revive and is_crit and side == "player" and mover.current_hp > 0 and has_passive(traits, "crit_lifesteal"):
        heal = max(1, int(actual_damage))
        heal = min(heal, mover.max_hp - mover.current_hp)
        if heal > 0:
            mover.current_hp += heal
            _aspear_on_heal(mover, side, traits, heal)

    if is_crit and target.current_hp > 0 and has_passive(traits, "crit_flinch") and rng.rng() < 0.5:
        target.flags["flinch"] = True

    if side == "player" and target.current_hp > 0 and has_passive(traits, "speed_diff"):
        mover_speed = get_effective_stat(mover, "speed", _own_items(mover), mover.stages)
        target_speed = get_effective_stat(target, "speed", _own_items(target), target.stages)
        if mover_speed > target_speed:
            extra = min(200, int(4.5 * math.sqrt(mover_speed - target_speed)))
            reps = 2 if has_passive(traits, "ice_onhit_double") else 1
            for _ in range(reps):
                if target.current_hp <= 0:
                    break
                target.current_hp = max(0, target.current_hp - extra)

    if is_crit and side == "player" and mover.current_hp > 0 and "Bug" in {t.capitalize() for t in mover.types} and has_passive(traits, "bug_critlvl") and mover.flags.get("_critLevelBase") is None:
        mover.flags["_critLevelBase"] = mover.level
        mover.level += 20
        hp_buff = (mover.stat_buffs or {}).get("hp", 0)
        from pokelike import map_gen

        new_max_hp = int(map_gen.calc_hp(mover.base_stats.hp, mover.level) * (1 + 0.05 * hp_buff))
        delta = new_max_hp - mover.max_hp
        mover.max_hp = new_max_hp
        if delta > 0:
            mover.current_hp = min(mover.current_hp + delta, new_max_hp)


def _apply_half_twice_extra_attack(
    mover: Combatant,
    target: Combatant,
    side: str,
    target_side: str,
    move: MoveInstance,
    mover_items,
    target_items,
    traits,
    battle_config: BattleConfig,
    ability_config,
    traits_config,
    mover_team,
    target_team,
    overtime_mult: int,
    actual_damage: int,
    no_heal_revive: bool,
) -> None:
    """Port of the `half_twice` extra-attack block (bundle.deobfuscated.js:
    56220-56284). Gated on `actual_damage` -- the MAIN hit's actual dealt
    damage (JS `BEs`), fixed before this call, NOT this extra hit's own --
    and a PER-ROUND, per-current-active-attacker one-shot flag
    (`_halfTwiceUsed`; despite the name it is reset every round for
    whichever Pokemon is that round's active player attacker,
    bundle.deobfuscated.js:55430-55431 -- see `run_battle`'s own reset next
    to where it computes `attacker_p`/`attacker_e` -- so half_twice can fire
    once per round, not once per battle; unlike `dragon_first_double`'s
    truly battle-wide flag below, a second half_twice Pokemon taking over
    the lead after the first one faints still gets its own extra hit).

    Recalculates a FRESH `calcDamage` (its own crit/variance RNG draws,
    same move/items/side/battle_config as the main hit), halves it, scales
    by the overtime multiplier, and subtracts directly -- deliberately
    skipping `attackerDamageMod`/`beforeDamage`/life_orb/every defender-side
    modifier (`dmg_cap`/`team_damage_spread`/`all_more`/`all_half`) that the
    main hit's own pipeline runs; the source has no call to any of them
    between the fresh `calcDamage` and the HP subtraction
    (bundle.deobfuscated.js:56229-56237). Then re-enters only `_bio_post_
    hit` (not the full `_apply_post_hit_traits` chain) and, only if the
    extra hit itself actually dealt damage, `whenAttacked`/`afterAttack`
    with `is_extra_attack=True` (so `TraitsConfig.after_attack`'s electric/
    ice double-hit loop does not itself re-trigger from this extra hit).
    """
    if side != "player" or actual_damage <= 0:
        return
    if mover.current_hp <= 0 or target.current_hp <= 0:
        return
    if not has_passive(traits, "half_twice") or mover.flags.get("_halfTwiceUsed"):
        return
    mover.flags["_halfTwiceUsed"] = True

    result = calc_damage(mover, target, move, mover_items, target_items, traits, side, battle_config)
    extra_damage = overtime_mult * int(result.damage * 0.5)

    pre_hp = target.current_hp
    target.current_hp = max(0, target.current_hp - extra_damage)
    extra_actual = pre_hp - target.current_hp

    _bio_post_hit(mover, target, side, target_side, bool(result.crit), extra_actual, traits, no_heal_revive)

    if extra_actual > 0:
        if ability_config is not None:
            ability_config.when_attacked(target, mover, extra_actual)
        if traits_config is not None:
            traits_config.when_attacked(target, target_side, mover, extra_actual)
        if ability_config is not None:
            ability_config.after_attack(mover, target, extra_actual, mover_team, is_extra_attack=True)
        if traits_config is not None:
            traits_config.after_attack(mover, side, target, target_side, extra_actual, mover_team, target_team, is_extra_attack=True)


def _apply_dragon_first_double_extra_attack(
    mover: Combatant,
    target: Combatant,
    side: str,
    target_side: str,
    move: MoveInstance,
    mover_items,
    target_items,
    traits,
    battle_config: BattleConfig,
    ability_config,
    traits_config,
    mover_team,
    target_team,
    overtime_mult: int,
    actual_damage: int,
    no_heal_revive: bool,
) -> None:
    """Port of `dragon_first_double` (bundle.deobfuscated.js:56285-56364).
    Gated on `actual_damage` (same MAIN-hit value as `half_twice`, see
    above), a BATTLE-WIDE one-shot flag (`battle_config.fired_flags[
    "dragon_first_double"]`, matching the source's closure-local `BI9` and
    this module's existing `fired_flags["dragon_first_crit"]` convention --
    unlike `half_twice`'s per-attacker flag, only ONE dragon_first_double
    extra attack ever happens per battle, from whichever player Dragon-type
    lands a hit first while the flag is still unset), and the attacker
    being a Dragon type.

    Unlike `half_twice`, this DOES re-run the threaded `attackerDamageMod`
    hook -- both `ability_config` and `traits_config`, in the same ability-
    then-traits order the main hit's own threaded call uses
    (`attackerDamageMod` is one of `mergeBattleConfigs`'s four threaded
    hooks per P0.1, so it applies identically whether or not a traits
    config also exists) -- plus `all_more`/`all_half` (no side check needed;
    this whole block already requires `side == "player"`), then the
    overtime multiplier. Still no `beforeDamage`, life_orb, or defender-side
    modifiers, matching the source's own narrower pipeline for this block
    (bundle.deobfuscated.js:56301-56309). Faint dispatch, `_bio_post_hit`,
    and `whenAttacked`/`afterAttack` re-entry are otherwise identical to
    `half_twice`'s -- see that function's docstring.
    """
    if side != "player" or actual_damage <= 0:
        return
    if battle_config.fired_flags.get("dragon_first_double"):
        return
    if mover.current_hp <= 0 or target.current_hp <= 0:
        return
    if not has_passive(traits, "dragon_first_double"):
        return
    if "Dragon" not in {t.capitalize() for t in mover.types}:
        return
    battle_config.fired_flags["dragon_first_double"] = True

    result = calc_damage(mover, target, move, mover_items, target_items, traits, side, battle_config)
    extra_damage = result.damage
    if ability_config is not None:
        extra_damage = ability_config.attacker_damage_mod(mover, target, extra_damage)
    if traits_config is not None:
        extra_damage = traits_config.attacker_damage_mod(mover, target, side, extra_damage, battle_config)
    if has_passive(traits, "all_more"):
        extra_damage = int(extra_damage * 1.5)
    if has_passive(traits, "all_half"):
        extra_damage = int(extra_damage * 0.5)
    extra_damage = overtime_mult * extra_damage

    pre_hp = target.current_hp
    target.current_hp = max(0, target.current_hp - extra_damage)
    extra_actual = pre_hp - target.current_hp

    _bio_post_hit(mover, target, side, target_side, bool(result.crit), extra_actual, traits, no_heal_revive)

    if extra_actual > 0:
        if ability_config is not None:
            ability_config.when_attacked(target, mover, extra_actual)
        if traits_config is not None:
            traits_config.when_attacked(target, target_side, mover, extra_actual)
        if ability_config is not None:
            ability_config.after_attack(mover, target, extra_actual, mover_team, is_extra_attack=True)
        if traits_config is not None:
            traits_config.after_attack(mover, side, target, target_side, extra_actual, mover_team, target_team, is_extra_attack=True)


def leftover_heal(pokemon: Combatant, side: str, traits, has_boost: bool) -> None:
    if pokemon.held_item is None or pokemon.held_item.id != "leftovers" or pokemon.current_hp <= 0:
        return
    mult = 2 if (side == "player" and has_boost) else 1
    heal = max(1, int(pokemon.max_hp * 0.1 * mult))
    pokemon.current_hp = min(pokemon.max_hp, pokemon.current_hp + heal)


def _status_tick_round(
    team,
    side,
    opposing_team,
    traits,
    ability_config,
    traits_config,
    battle_config,
    player_participants,
    no_heal_revive=False,
    status_events=None,
    hook_trace=None,
) -> None:
    """Port of the burn/poison status-tick faint dispatch
    (bundle.deobfuscated.js:56590-56712) -- deliberately NOT symmetric with
    `_handle_faint`'s direct-hit hook set, which this function used to
    (incorrectly) route every status-tick faint through (CODEX.md issue 15,
    now fixed):

    - A burn-tick faint (56606-56615) calls NEITHER `onKO` NOR `onFaint` at
      all -- just a `continue` to the next team member, skipping this
      member's poison check too (already fainted).
    - A poison-tick faint (56672-56704) calls the FINAL config's `onKO`
      only when the fainted side is "enemy", with killer = the first living
      player member. In an ordinary merged Gen3/4 battle that dispatches
      ability first and traits second. It then calls `onFaint` regardless
      of side (only the ability provider defines that hook).
    - `sweepKOs` still runs after both teams' ticks. `TraitsConfig.on_ko`
      records `(side, index)`, so the immediate poison KO and later sweep do
      not double-apply effects.
    - `poison_drain` is disabled by `no_heal_revive` and successful healing
      calls `aspearOnHeal`, including its 50-HP stat-stage accumulator.

    These paths are compared against the real source by the P0.2 fixtures in
    `tools/battle-oracle/fixtures`.
    """
    status_events = status_events if status_events is not None else []
    hook_trace = hook_trace if hook_trace is not None else []
    player_team, enemy_team = (team, opposing_team) if side == "player" else (opposing_team, team)
    for idx, member in enumerate(team):
        if member.current_hp <= 0:
            continue
        if member.burned:
            damage = burn_tick_damage(member.max_hp)
            member.current_hp = max(0, member.current_hp - damage)
            status_events.append({
                "type": "status_tick",
                "side": side,
                "idx": idx,
                "status": "burn",
                "hp_change": -damage,
                "hp_after": member.current_hp,
            })
            if member.current_hp <= 0:
                status_events.append({"type": "faint", "side": side, "idx": idx})
                continue
        if (member.poison_stacks or 0) > 0:
            damage = poison_tick_damage(member.max_hp, member.poison_stacks)
            if side == "enemy" and has_passive(traits, "fragile_status"):
                damage = int(damage * 1.5)
            if side == "enemy" and has_passive(traits, "nonattack_amp"):
                damage = int(damage * 1.3)
            member.current_hp = max(0, member.current_hp - damage)
            status_events.append({
                "type": "status_tick",
                "side": side,
                "idx": idx,
                "status": "poison",
                "hp_change": -damage,
                "hp_after": member.current_hp,
            })
            if (
                not no_heal_revive
                and side == "enemy"
                and has_passive(traits, "poison_drain")
                and damage > 0
            ):
                healer = next((m for m in opposing_team if m.current_hp > 0 and "Poison" in {t.capitalize() for t in m.types}), None)
                if healer is not None:
                    heal = min(damage * 2, healer.max_hp - healer.current_hp)
                    if heal > 0:
                        healer.current_hp += heal
                        _aspear_on_heal(healer, "player", traits, heal)
                        status_events.append({
                            "type": "poison_drain",
                            "side": "player",
                            "idx": opposing_team.index(healer),
                            "hp_change": heal,
                            "hp_after": healer.current_hp,
                        })
            if member.current_hp <= 0:
                status_events.append({"type": "faint", "side": side, "idx": idx})
                if side == "enemy" and (ability_config is not None or traits_config is not None):
                    killer = _first_alive_index(player_team)
                    if killer is not None:
                        hook_trace.append({
                            "hook": "on_ko",
                            "fainted_side": side,
                            "fainted_idx": idx,
                            "killer_side": "player",
                            "killer_idx": killer,
                        })
                        killer_mon = player_team[killer]
                        # The source invokes the FINAL battle config here.
                        # For ordinary Gen3/4 with passives that means the
                        # generic merge wrapper calls ability first, then
                        # traits, while discarding both return values.
                        if ability_config is not None:
                            ability_config.on_ko(member, killer_mon)
                        if traits_config is not None:
                            traits_config.on_ko(
                                member,
                                side,
                                idx,
                                killer_mon,
                                "player",
                                killer,
                                player_team,
                                enemy_team,
                                battle_config,
                            )
                if ability_config is not None:
                    hook_trace.append({"hook": "on_faint", "side": side, "idx": idx})
                    ability_config.on_faint(member, team)
                continue
            if traits_config is not None:
                hook_trace.append({"hook": "after_status_tick", "side": side, "idx": idx})
                traits_config.after_status_tick(member, side)


def _handle_faint(
    fainted: Combatant,
    fainted_side: str,
    player_team: list,
    enemy_team: list,
    killer: Optional[Combatant],
    killer_side: Optional[str],
    ability_config,
    traits_config,
    battle_config: BattleConfig,
    player_participants: set,
    battle_events: Optional[list] = None,
) -> None:
    """`player_team`/`enemy_team` are always the FULL rosters (available
    throughout `run_battle`) regardless of whether this faint came from a
    direct attack or a DoT tick with no attributable "killer" -- several
    onKO traits (`ko_boost`, `normal_grow`, `ko_maxhp`, ...) only check
    `fainted_side`, not whether a killer exists, so both full team arrays
    must always be passed through (a bug found and fixed while porting:
    an earlier draft tried to reconstruct the "other" team from the killer
    alone, which silently emptied it for DoT-caused faints).
    """
    fainted_team = player_team if fainted_side == "player" else enemy_team
    fainted_idx = fainted_team.index(fainted)

    # M6/N11: the ordinary combat KO's own record. The source pushes it at
    # 56445 (target-of-a-direct-hit) and 56482 (mover killed by its own
    # recoil), in both cases BEFORE the onKO/onFaint hooks below run -- so
    # this append has to stay above them to preserve the source's order.
    # `status_events` only ever carried the status-tick-caused subset, which
    # is all `driver.js`'s `deriveStatusEvents` keeps from the source log
    # (route-oracle/driver.js:273-278: a `faint` is projected only when a
    # preceding `status_tick` on the same combatant left `hpAfter <= 0`).
    if battle_events is not None:
        battle_events.append({"type": "faint", "side": fainted_side, "idx": fainted_idx})

    if ability_config is not None and killer is not None:
        ability_config.on_ko(fainted, killer)
    if traits_config is not None:
        killer_idx = None
        if killer is not None:
            killer_team = player_team if killer_side == "player" else enemy_team
            killer_idx = killer_team.index(killer)
        traits_config.on_ko(fainted, fainted_side, fainted_idx, killer, killer_side, killer_idx, player_team, enemy_team, battle_config)
    if killer is None and traits_config is not None:
        traits_config.sweep_faint_rallies(player_team, enemy_team, battle_config)

    if ability_config is not None:
        ability_config.on_faint(fainted, fainted_team)

    # Hazard application only happens on the TARGET-of-a-direct-hit faint
    # block in the source (bundle.deobfuscated.js:56442-56455) -- the
    # mover-recoil-faint block (56480-56505) and the status-tick faint path
    # never call `applyEnemySwitchInHazards`. `killer is not None` is
    # exactly the direct-hit call site (see `run_battle`'s two `_handle_faint`
    # calls and `_status_tick_round`, both of which pass `killer=None`).
    if fainted_side == "enemy" and killer is not None:
        next_idx = _first_alive_index(enemy_team)
        if next_idx is not None and traits_config is not None:
            traits_config.apply_enemy_switch_in_hazards(enemy_team[next_idx], player_team, battle_config)

    # `player_participants`: the source's OWN "was sent out" bookkeeping
    # (bundle.deobfuscated.js:56452-56456 for the target-fainted block,
    # 56489-56493 for the mover-recoil-fainted block -- `_handle_faint`'s
    # only two callers, run_battle:750/752) adds the next alive player slot
    # HERE, at a COMBAT faint, and only here. `_status_tick_round`'s own
    # pre-turn poison/status fainting has NO equivalent "was sent out" call
    # in the source at all, so a member whose only path onto the field was
    # a predecessor's STATUS-tick death is never counted as a participant,
    # even if it goes on to attack -- a real, narrow source quirk, not a
    # simplification. Found and repaired during the M4 route-oracle work:
    # a prior version added a slot to `player_participants` unconditionally
    # the first time it became the active attacker (case-insensitive to WHY
    # its predecessor left), which is broader than the source and diverged
    # on any route where a status-tick faint forced a mid-battle switch --
    # invisible until a long enough multi-battle route finally poisoned a
    # team member to death.
    if fainted_side == "player":
        next_idx = _first_alive_index(player_team)
        if next_idx is not None:
            player_participants.add(next_idx)
