"""Fixture-authoring helper for the M3 route oracle.

NOT part of either M3 gate -- `compare.py` never calls this. It exists so
that the checked-in scenario fixtures are *derived from the real generated
maps* rather than guessed, and so a future session can re-derive or extend
them reproducibly.

It grows a route one node at a time: run the JavaScript runner with the
actions chosen so far, read the map that the source actually generated, pick
the next node from the accessible set by a preference order, and repeat.
When a boss win parks the run on the badge screen it emits `advance_map`;
when the run ends it stops.

    python route-oracle/plan_route.py --seed 123456789 --align 987654321 \
        --maps 2 --prefer battle,trainer,pokecenter,boss

Print the resulting JSON scenario and paste it into `scenarios/`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Node types the oracle can traverse without a player choice. Everything else
# needs an explicit `choice` action, so the planner stops rather than guess.
NON_INTERACTIVE = (
    "battle", "trainer", "boss", "pokecenter", "silver", "magma", "aqua",
    "underground", "distortion", "subexit",
)
# Node types that park the run on a screen the driver has a bridge for and
# that can be resolved by declining (index null). The planner will traverse
# these, emitting the decline explicitly, so a route can reach deeper layers.
DECLINABLE = ("catch", "item")
# Everything else parks on a screen with no bridge, or one where declining
# would skip required coverage. The planner stops rather than guess.


def run_js(scenario: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "probe.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(scenario, handle)
        proc = subprocess.run(
            ["node", os.path.join(HERE, "run-scenario.js"), path],
            capture_output=True, text=True, cwd=REPO,
        )
    if proc.returncode != 0:
        raise SystemExit(f"js runner failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--align", type=int, default=None)
    ap.add_argument("--starter-index", type=int, default=0)
    ap.add_argument("--nuzlocke", action="store_true")
    ap.add_argument("--gen2", action="store_true")
    ap.add_argument("--gen3", action="store_true")
    ap.add_argument("--gen4", action="store_true")
    ap.add_argument("--maps", type=int, default=1, help="how many map transitions to attempt")
    ap.add_argument("--prefer", default=",".join(NON_INTERACTIVE))
    ap.add_argument("--name", default="planned")
    ap.add_argument("--max-steps", type=int, default=80)
    args = ap.parse_args(argv[1:])

    prefer = [p.strip() for p in args.prefer.split(",") if p.strip()]
    base = {
        "schema_version": 2,
        "scenario": args.name,
        "description": "planned by plan_route.py",
        "mode": {
            "nuzlocke": args.nuzlocke, "gen2": args.gen2,
            "gen3": args.gen3, "gen4": args.gen4,
        },
        "seed": args.seed,
        "starter_index": args.starter_index,
        "actions": [],
    }
    if args.align is not None:
        base["align_rng_after_starter_offer"] = args.align

    actions: list[dict] = []
    transitions = 0
    for _ in range(args.max_steps):
        probe = dict(base, actions=list(actions))
        out = run_js(probe)
        if out.get("error"):
            print(f"# stopped: js error after {len(actions)} actions:\n# {out['error'].splitlines()[0]}")
            break
        last = out["checkpoints"][-1]
        screen = last["screen"]
        if screen == "gameover-screen" or last["game_over"]:
            print(f"# run ended (game over) after {len(actions)} actions")
            break
        if screen == "badge-screen":
            if transitions >= args.maps:
                print(f"# reached badge screen with {transitions} transition(s) already done; stopping")
                break
            actions.append({"kind": "advance_map"})
            transitions += 1
            continue
        if screen in ("catch-screen", "item-screen"):
            # Declining is an explicit, source-supported action (the skip
            # button), not a way of pretending the node did not happen.
            actions.append({"kind": "choice", "index": None})
            continue
        if screen == "swap-screen":
            # Submap fossil/legendary rewards suspend here. Accept (index 0)
            # so the route continues through reward -> subexit -> parent
            # return; a hand-written scenario can still use null to decline.
            actions.append({"kind": "choice", "index": 0})
            continue
        if screen != "map-screen":
            print(f"# stopped: parked on {screen} after {len(actions)} actions (needs an explicit choice)")
            break
        gmap = last["map"]
        if gmap is None:
            print("# stopped: no map")
            break
        options = [n for n in gmap["nodes"] if n["accessible"]]
        if not options:
            print(f"# stopped: no accessible node after {len(actions)} actions")
            break
        pick = None
        for want in list(prefer) + list(DECLINABLE):
            for node in options:
                if node["type"] == want:
                    pick = node
                    break
            if pick:
                break
        if pick is None:
            kinds = sorted({n["type"] for n in options})
            print(f"# stopped: only unbridged options {kinds} after {len(actions)} actions")
            break
        actions.append({"kind": "visit", "node": pick["id"], "_type": pick["type"]})

    final = run_js(dict(base, actions=[{k: v for k, v in a.items() if not k.startswith("_")} for a in actions]))
    types = [a.get("_type") for a in actions if a.get("_type")]
    print(f"# {len(actions)} actions, node types visited: {sorted(set(types))}")
    print(f"# final screen {final['checkpoints'][-1]['screen']}, "
          f"map {final['checkpoints'][-1]['current_map']}, "
          f"team {[m['name'] + '@' + str(m['level']) for m in final['checkpoints'][-1]['team']]}, "
          f"rng draws {final['rng_draws_total']}")
    base["actions"] = [{k: v for k, v in a.items() if not k.startswith("_")} for a in actions]
    print(json.dumps(base, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
