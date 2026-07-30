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


def _choose_action(state: engine.RunState, *, interactive: bool, prefer_boss: bool = True):
    if state.phase == engine.Phase.CHOOSE_STARTER:
        options = state.pending.options
        idx = _prompt_index(options, optional=False) if interactive else random.randrange(len(options))
        return engine.ChooseStarter(species_id=options[idx]["species_id"])

    if state.phase == engine.Phase.ON_MAP:
        nodes = engine.accessible_nodes(state)
        if interactive:
            print("Accessible nodes:")
            for i, n in enumerate(nodes):
                print(f"  {i}: {n.id} (type on click may differ if it's a question node)")
            idx = _prompt_index([{"name": n.id} for n in nodes], optional=False)
        else:
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
    while state.phase not in (engine.Phase.GAME_OVER, engine.Phase.VICTORY) and steps < max_steps:
        steps += 1
        action = _choose_action(state, interactive=interactive)
        state = eng.step(action)
        print()
        print(console.render_state(state))
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
