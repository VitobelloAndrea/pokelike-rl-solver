"""Runs one full episode through `engine.py`, printed via `console.py`.

Two modes:
- Autopilot (default) -- picks legal actions itself (prefers the boss node
  when accessible, accepts the first offered option otherwise, occasionally
  declines optional choices). This is what CLAUDE.md's "actually run it and
  eyeball the output" instruction calls for -- a real episode driven start
  to finish, not just unit tests.
- `--interactive` -- prompts on stdin for every decision, for a human to
  actually play a run through the console.

Usage:
    python -m pokelike.render.play
    python -m pokelike.render.play --interactive
    python -m pokelike.render.play --seed 42 --nuzlocke --gen2
"""

from __future__ import annotations

import argparse
import random

from pokelike import engine
from pokelike.render import console


def _prompt_index(options: list, *, optional: bool) -> int | None:
    for i, opt in enumerate(options):
        print(f"  {i}: {console._format_option(opt)}")
    if optional:
        print("  (blank to skip/decline)")
    while True:
        raw = input("> ").strip()
        if raw == "" and optional:
            return None
        try:
            idx = int(raw)
        except ValueError:
            print("  not a number, try again")
            continue
        if 0 <= idx < len(options):
            return idx
        print(f"  out of range [0, {len(options) - 1}]")


def _party_action(state: engine.RunState, legal: dict):
    """Interactive ON_MAP party/bag management. Offered as an occasional extra
    prompt before the node list, not as a step of its own -- pressing enter
    falls straight through to "which node do I visit", which is what a player
    wants almost every turn. Returns an `Action` or `None`.

    Every option is built from `legal` (`engine.legal_actions`), never
    re-derived: an item with no eligible target simply is not offered, and the
    target list for one that is comes from the engine's own
    `_usable_item_can_target` answer.
    """
    offers = []
    if "reorder_team" in legal:
        offers.append("r")
    if "use_item" in legal:
        offers.append("u")
    if "equip_item" in legal:
        offers.append("e")
    if "unequip_item" in legal:
        offers.append("x")
    if "hand_off_item" in legal:
        offers.append("h")
    if not offers:
        return None
    print(
        f"Party actions: {'/'.join(offers)} (r=reorder, u=use item, e=equip item, "
        f"x=unequip to bag, h=hand off held item), or enter to move on"
    )
    choice = input("> ").strip().lower()
    if choice not in offers:
        return None

    if choice == "r":
        size = legal["reorder_team"]["team_size"]
        for i, mon in enumerate(state.team):
            print(f"  {i}: {mon.nickname or mon.name} Lv{mon.level}")
        a = _prompt_int(f"swap which slot? [0, {size - 1}]", 0, size - 1)
        b = _prompt_int(f"with which slot? [0, {size - 1}]", 0, size - 1)
        if a == b:
            return None
        # The source's reorder is a straight transposition, not a general
        # reinsertion -- renderTeamBar's drop handler swaps two slots
        # (bundle.deobfuscated.js:64805). Built as the full permutation
        # `ReorderTeam` takes.
        order = list(range(size))
        order[a], order[b] = order[b], order[a]
        return engine.ReorderTeam(order=tuple(order))

    if choice == "u":
        entries = legal["use_item"]
        for i, e in enumerate(entries):
            targets = ", ".join(
                f"{t}:{state.team[t].nickname or state.team[t].name}" for t in e["target_indices"]
            )
            print(f"  {i}: {e['item_id']} (bag slot {e['item_index']}) -> eligible: {targets}")
        i = _prompt_int(f"use which item? [0, {len(entries) - 1}]", 0, len(entries) - 1)
        entry = entries[i]
        allowed = entry["target_indices"]
        while True:
            t = _prompt_int(f"on which team member? {allowed}", 0, len(state.team) - 1)
            if t in allowed:
                break
            print("  that target is not eligible for this item")
        return engine.UseItem(item_index=entry["item_index"], target_index=t)

    if choice == "x":
        # `#btn-equip-to-bag` with the overlay opened from a member
        # (bundle.deobfuscated.js:79549-79553), and the `[data-unequip]` rows
        # (79521-79531) -- same effect, one action.
        holders = legal["unequip_item"]["team_indices"]
        for i in holders:
            mon = state.team[i]
            print(f"  {i}: {mon.nickname or mon.name} holding {mon.held_item.id}")
        t = None
        while t not in holders:
            t = _prompt_int(f"unequip whose item? {holders}", 0, len(state.team) - 1)
        return engine.UnequipItem(team_index=t)

    if choice == "h":
        # The member-to-member hand-off (79541-79545). A SWAP, not an unequip
        # followed by an equip -- see engine.HandOffItem.
        ho = legal["hand_off_item"]
        holders = ho["from_indices"]
        for i, mon in enumerate(state.team):
            held = f" (holding {mon.held_item.id})" if mon.held_item is not None else " (empty)"
            print(f"  {i}: {mon.nickname or mon.name}{held}")
        src = None
        while src not in holders:
            src = _prompt_int(f"hand off whose item? {holders}", 0, ho["team_size"] - 1)
        dst = None
        while dst is None or dst == src:
            dst = _prompt_int(f"to which member? (not {src})", 0, ho["team_size"] - 1)
        return engine.HandOffItem(from_index=src, to_index=dst)

    eq = legal["equip_item"]
    for b in eq["bag_indices"]:
        print(f"  bag slot {b}: {state.items[b]}")
    bag = None
    while bag not in eq["bag_indices"]:
        bag = _prompt_int(f"equip which bag slot? {eq['bag_indices']}", 0, len(state.items) - 1)
    for i, mon in enumerate(state.team):
        held = f" (holding {mon.held_item.id})" if mon.held_item is not None else ""
        print(f"  {i}: {mon.nickname or mon.name}{held}")
    t = _prompt_int(f"onto which team member? {eq['team_indices']}", 0, len(state.team) - 1)
    return engine.EquipItem(bag_index=bag, team_index=t)


def _autopilot_party_action(state: engine.RunState, legal: dict):
    """Autopilot's occasional party/bag move. Deliberately low-probability so
    an episode still makes map progress, but frequent enough that a long run
    exercises all three actions -- which is the point: before R3 nothing but a
    unit test ever drove them, so a mutation in `_apply_reorder_team` and
    friends could not be caught by playing the game.
    """
    if random.random() >= 0.12:
        return None
    candidates = [
        k for k in ("reorder_team", "use_item", "equip_item", "unequip_item", "hand_off_item")
        if k in legal
    ]
    if not candidates:
        return None
    kind = random.choice(candidates)
    if kind == "unequip_item":
        return engine.UnequipItem(
            team_index=random.choice(legal["unequip_item"]["team_indices"]))
    if kind == "hand_off_item":
        ho = legal["hand_off_item"]
        src = random.choice(ho["from_indices"])
        others = [i for i in range(ho["team_size"]) if i != src]
        if not others:
            return None
        return engine.HandOffItem(from_index=src, to_index=random.choice(others))
    if kind == "reorder_team":
        size = legal["reorder_team"]["team_size"]
        if size < 2:
            return None
        a, b = random.sample(range(size), 2)
        order = list(range(size))
        order[a], order[b] = order[b], order[a]
        return engine.ReorderTeam(order=tuple(order))
    if kind == "use_item":
        entry = random.choice(legal["use_item"])
        return engine.UseItem(
            item_index=entry["item_index"],
            target_index=random.choice(entry["target_indices"]),
        )
    eq = legal["equip_item"]
    return engine.EquipItem(
        bag_index=random.choice(eq["bag_indices"]),
        team_index=random.choice(eq["team_indices"]),
    )


def _prompt_int(label: str, low: int, high: int) -> int:
    while True:
        raw = input(f"{label} > ").strip()
        try:
            value = int(raw)
        except ValueError:
            print("  not a number, try again")
            continue
        if low <= value <= high:
            return value
        print(f"  out of range [{low}, {high}]")


def _choose_action(state: engine.RunState, *, interactive: bool, prefer_boss: bool = True):
    if state.phase == engine.Phase.CHOOSE_STARTER:
        options = state.pending.options
        idx = _prompt_index(options, optional=False) if interactive else random.randrange(len(options))
        return engine.ChooseStarter(species_id=options[idx]["species_id"])

    if state.phase == engine.Phase.ON_MAP:
        # R3: `ReorderTeam`/`UseItem`/`EquipItem` have been legal on every
        # ON_MAP step since before R1, and until now nothing in this
        # repository outside the tests ever constructed one -- so neither a
        # human playtest nor an autopilot episode exercised them. Both paths
        # below can now issue all three. `VisitNode` stays dominant: these are
        # additional legal moves on the map, not a replacement for advancing.
        legal = engine.legal_actions(state)
        nodes = engine.accessible_nodes(state)
        if interactive:
            party = _party_action(state, legal)
            if party is not None:
                return party
            print("Accessible nodes:")
            for i, n in enumerate(nodes):
                print(f"  {i}: {n.id} (type on click may differ if it's a question node)")
            idx = _prompt_index([{"name": n.id} for n in nodes], optional=False)
        else:
            party = _autopilot_party_action(state, legal)
            if party is not None:
                return party
            boss = next((n for n in nodes if n.type == "boss"), None) if prefer_boss else None
            node = boss if boss is not None else random.choice(nodes)
            idx = nodes.index(node)
        return engine.VisitNode(node_id=nodes[idx].id)

    if state.phase == engine.Phase.NEXT_MAP_READY:
        if interactive:
            input("Press enter to advance to the next map...")
        return engine.AdvanceMap()

    # every remaining phase expects a SelectOption
    pending = state.pending
    if interactive:
        idx = _prompt_index(pending.options, optional=pending.optional)
    else:
        if pending.optional and random.random() < 0.25:
            idx = None
        else:
            idx = random.randrange(len(pending.options)) if pending.options else None
    return engine.SelectOption(index=idx)


def run_episode(
    *,
    seed: int | None = None,
    interactive: bool = False,
    nuzlocke: bool = False,
    gen2: bool = False,
    gen3: bool = False,
    gen4: bool = False,
    max_steps: int = 2000,
) -> engine.RunState:
    eng = engine.Engine()
    state = eng.reset(nuzlocke_mode=nuzlocke, gen2_mode=gen2, gen3_mode=gen3, gen4_mode=gen4, seed=seed)
    print(console.render_state(state))
    steps = 0
    # R4: `Engine.step` can append SEVERAL log entries in one call -- a battle
    # win followed by an evolve, a badge, an item grant or victory. This loop
    # used to take `render_state`'s default of one, so on any such step every
    # entry but the last was never shown, and a battle followed by anything
    # else in the same step printed no battle line (and now, no replay) at all.
    # That is the console-side twin of `app.js`'s interstitial-detection bug
    # (CODEX section 7.7), which R4 fixes on the browser side; both come from
    # assuming one step means one log entry.
    #
    # R5/N14: the delta is now the TRUE delta, with no `max(1, ...)` floor. The
    # floor existed to keep `render_state`'s `state.log[-recent_log:]` off the
    # `-0:` slice (which returns the whole log, not none of it) -- but on a
    # zero-delta step, e.g. entering a pending-choice screen, it re-showed the
    # PREVIOUS step's entry, and when that entry was a `"battle"` it re-ran the
    # whole replay. Measured before the fix: seed 7 printed 13 replay blocks for
    # 7 distinct battles, one of them 3x. `console.render_state` now treats
    # `recent_log <= 0` as "show nothing", which is what the floor was standing
    # in for, so a genuinely empty delta prints no log section at all.
    seen_log_total = len(state.log)
    while state.phase not in (engine.Phase.GAME_OVER, engine.Phase.VICTORY) and steps < max_steps:
        steps += 1
        action = _choose_action(state, interactive=interactive)
        state = eng.step(action)
        new_entries = len(state.log) - seen_log_total
        seen_log_total = len(state.log)
        print()
        print(console.render_state(state, recent_log=new_entries))
    print()
    print(f"=== run finished: {state.phase.value} after {steps} steps, seed={state.run_seed} ===")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--nuzlocke", action="store_true")
    parser.add_argument("--gen2", action="store_true")
    parser.add_argument("--gen3", action="store_true")
    parser.add_argument("--gen4", action="store_true")
    parser.add_argument("--max-steps", type=int, default=2000)
    args = parser.parse_args()
    run_episode(
        seed=args.seed,
        interactive=args.interactive,
        nuzlocke=args.nuzlocke,
        gen2=args.gen2,
        gen3=args.gen3,
        gen4=args.gen4,
        max_steps=args.max_steps,
    )


if __name__ == "__main__":
    main()
