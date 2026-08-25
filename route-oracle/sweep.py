"""M7 -- the coverage-guided cross-runtime Story/Nuzlocke convergence sweep.

The pre-M7 route oracle replays 29 *pinned* action lists and compares the
resulting checkpoint streams. That proves a great deal about those 29 routes
and nothing about any other. This tool holds BOTH runtimes at the same step and

  1. enumerates each runtime's normalized LEGAL ACTION SET,
  2. compares those sets *before* anything picks an action,
  3. executes the same chosen action through each runtime's real path,
  4. compares the normalized state and battle evidence after every action,
  5. records coverage against a checked-in, source-derived target manifest,
  6. saves a deterministic replay at the first divergence and minimizes it.

Nothing here reimplements game logic. The Python side reads legality off
``engine.legal_actions`` and steps the real ``pokelike.engine``; the source
side is ``sweep-host.js`` + ``sweep-adapter.js``, which enumerate from the
source's own state/DOM/handlers and execute through the source's own handlers.
The compared state projection is ``run_scenario.Runner.checkpoint`` and
``driver.js``'s ``checkpoint`` -- the same normalization the frozen
29-scenario gate already trusts.

Commands::

    python route-oracle/sweep.py validate-targets
    python route-oracle/sweep.py plan   --episodes 200 --out plan.json
    python route-oracle/sweep.py run    --plan plan.json --out result.json
    python route-oracle/sweep.py run    --corpus --out corpus.json
    python route-oracle/sweep.py guided --budget 120 --out guided.json
    python route-oracle/sweep.py replay --record findings/<name>.json
    python route-oracle/sweep.py coverage --results a.json b.json

See ``SWEEP.md`` for the action vocabulary, the comparison projection and
every include/exclude disposition.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import re
import subprocess
import threading
import sys
import time
from typing import Any, Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import checkpoints  # noqa: E402
import coverage as route_coverage  # noqa: E402
import run_scenario  # noqa: E402

from pokelike import data, engine, map_gen  # noqa: E402

# Bumped whenever the action vocabulary, the compared projection, or the
# episode-record shape changes. Recorded in every result file, so a stale
# record can never be silently compared against a newer run.
SWEEP_VERSION = 1

TARGETS_PATH = os.path.join(_HERE, "sweep-targets.json")
FINDINGS_DIR = os.path.join(_HERE, "findings")

# These are not suppressed divergences. They are the two retained records
# whose source-side PokeAPI lookup is deliberately unavailable in the offline
# oracle. replay-set still reports both actual reproductions; this allowlist
# only distinguishes an adjudicated harness boundary from a new divergence.
ACCEPTED_HARNESS_BOUNDARY_EPISODES = frozenset({
    "legend_2779800549",
    "legend_3187443927",
})

# The eight episode strata the manifest requires. Story/Nuzlocke x Gen1-4.
MODE_BUCKETS = [
    {"nuzlocke": n, "gen2": g == 2, "gen3": g == 3, "gen4": g == 4}
    for n in (False, True)
    for g in (1, 2, 3, 4)
]


def bucket_name(mode: dict) -> str:
    gen = 4 if mode.get("gen4") else 3 if mode.get("gen3") else 2 if mode.get("gen2") else 1
    return ("nuzlocke" if mode.get("nuzlocke") else "story") + f"_gen{gen}"


def generation_of(mode: dict) -> int:
    return 4 if mode.get("gen4") else 3 if mode.get("gen3") else 2 if mode.get("gen2") else 1


# ===========================================================================
# Normalized action vocabulary
# ===========================================================================
# One dict shape per action, identical on both runtimes. `__prov` is
# provenance: reported, never compared (each side derives it from its own code
# path, so comparing it would compare the adapters rather than the runtimes).

PROV_KEY = "__prov"


def canon_action(a: dict) -> str:
    """The canonical comparable string for one normalized action."""
    return checkpoints.dumps({k: v for k, v in a.items() if k != PROV_KEY})


def canon_set(actions: Iterable[dict]) -> list[str]:
    return sorted(canon_action(a) for a in actions)


def action_multiset_error(actions: Iterable[dict]) -> Optional[str]:
    """A legal-action set with duplicates is an adapter bug, not a state fact:
    two identical normalized actions cannot be two different affordances."""
    seen = [canon_action(a) for a in actions]
    dupes = sorted({s for s in seen if seen.count(s) > 1})
    return f"duplicate normalized actions: {dupes}" if dupes else None


# ===========================================================================
# Python legal-action adapter
# ===========================================================================
# Expands `engine.legal_actions`'s heterogeneous RANGE descriptions into
# normalized concrete actions. Every expansion is deterministic and total.


# ---------------------------------------------------------------------------
# `reorder_team`: the canonical compared DOMAIN (M7-COMBINED A2, finding F1)
# ---------------------------------------------------------------------------
# The two runtimes do not describe team reordering with the same breadth, and
# M7 recorded that mismatch as finding **F1** without resolving it. A2 resolves
# it, in the direction the source dictates.
#
# THE SOURCE. Its only reorder affordance is the team-bar drag handler
# (bundle.deobfuscated.js:64798-64806). The whole mutation is one statement:
#
#     [O[_dragIdx], O[Bcq]] = [O[Bcq], O[_dragIdx]]
#
# a SWAP of two slots, guarded by `Bcq !== _dragIdx`. There is no other write
# to `state.team`'s ORDER anywhere in the bundle, and no callable function
# behind this one -- it is written inline in the pointerup listener. The source
# can therefore express exactly the transpositions, one drag at a time.
#
# THE PORT. `engine.legal_actions` reports `{"team_size": n}` and
# `engine.ReorderTeam(order=...)` accepts any permutation of `n`, which is a
# strictly wider action type: for n = 6 that is 720 permutations against 15
# transpositions.
#
# THE DECISION. The canonical compared domain is the SOURCE's: the
# transpositions `(i, j)`, `i < j`. That is the source's atomic action, it is
# expressible on both runtimes, and every element of it is a real affordance a
# player can actually perform. Enumerating all `n!` permutations instead was
# rejected on the merits, not for cost: the port would be the only runtime that
# could offer them, so every extra element would be a guaranteed legal-set
# divergence reporting a fact about the Python API rather than about the game.
#
# WHAT MAKES THIS NOT A SILENT INTERSECTION. Three things, all checked:
#
#   1. the reduction is declared here, as `REORDER_DOMAIN`, and named in
#      SWEEP.md's disposition table;
#   2. `reorder_transpositions` ASSERTS the engine's declaration is still the
#      wider permutation form it is reducing FROM. If `legal_actions` ever
#      stops reporting `team_size` -- or starts reporting the domain some other
#      way -- this raises instead of quietly enumerating something else;
#   3. `SweepReorderDomainTests` pins that the wider capability is real (the
#      engine really does execute a non-transposition permutation) and that no
#      source affordance can produce one. The breadth difference is therefore
#      recorded as a tested property, not discarded.
#
# The wider Python capability is unreachable through this harness by
# construction, not by filtering: `py_reorder_action` only ever BUILDS a
# permutation that is a single transposition, so there is no permutation to
# intersect away in the first place.
REORDER_DOMAIN = "transposition"


def reorder_transpositions(declared: dict) -> list[dict]:
    """Expand `legal_actions`'s `{"team_size": n}` into the compared domain."""
    if "team_size" not in declared:
        raise AssertionError(
            "engine.legal_actions no longer declares reorder_team as "
            f"{{'team_size': n}} (got {sorted(declared)!r}); the "
            f"{REORDER_DOMAIN} reduction this tool applies is only valid "
            "against the permutation declaration it was derived from")
    n = int(declared["team_size"])
    return [{"kind": "reorder_team", "i": i, "j": j,
             PROV_KEY: f"legal_actions.reorder_team team_size={n} "
                       f"reduced to the source's {REORDER_DOMAIN} domain "
                       "(team-bar drag swap, 64798-64806)"}
            for i in range(n) for j in range(i + 1, n)]


def py_legal_actions(state) -> list[dict]:
    la = engine.legal_actions(state)
    out: list[dict] = []

    if "choose_starter" in la:
        for sid in la["choose_starter"]["species_ids"]:
            out.append({"kind": "choose_starter", "species_id": int(sid),
                        PROV_KEY: "legal_actions.choose_starter"})
        return out

    if "select_option" in la:
        so = la["select_option"]
        for i in so["indices"]:
            out.append({"kind": "select_option", "index": int(i), "cancel": False,
                        PROV_KEY: f"legal_actions.select_option index {i}"})
        if so["optional"]:
            out.append({"kind": "select_option", "index": None, "cancel": False,
                        PROV_KEY: "legal_actions.select_option skip/decline"})
        # M7's corrected declaration. `_resolve_pending` accepts this only for
        # ITEM_EQUIP_CHOICE and `legal_actions` now says so.
        if so.get("cancel"):
            out.append({"kind": "select_option", "index": None, "cancel": True,
                        PROV_KEY: "legal_actions.select_option cancel (#btn-equip-cancel)"})
        return out

    if la.get("advance_map"):
        out.append({"kind": "advance_map", PROV_KEY: "legal_actions.advance_map"})
        return out

    if "visit_node" in la:
        for nid in la["visit_node"]["node_ids"]:
            out.append({"kind": "visit_node", "node_id": nid,
                        PROV_KEY: "legal_actions.visit_node"})

        # ---- reorder: TRANSPOSITIONS, not permutations --------------------
        # See `REORDER_DOMAIN` and `reorder_transpositions` -- the reduction is
        # DECLARED there and checked against the engine's own declaration on
        # every call, rather than being performed silently here.
        if "reorder_team" in la:
            out.extend(reorder_transpositions(la["reorder_team"]))

        for entry in la.get("use_item", []):
            for t in entry["target_indices"]:
                out.append({"kind": "use_item", "item_id": entry["item_id"],
                            "bag_index": int(entry["item_index"]), "target_index": int(t),
                            PROV_KEY: "legal_actions.use_item"})

        if "equip_item" in la:
            eq = la["equip_item"]
            for b in eq["bag_indices"]:
                for t in eq["team_indices"]:
                    out.append({"kind": "equip_item", "item_id": state.items[b],
                                "bag_index": int(b), "team_index": int(t),
                                PROV_KEY: "legal_actions.equip_item"})

        if "unequip_item" in la:
            for t in la["unequip_item"]["team_indices"]:
                out.append({"kind": "unequip_item", "team_index": int(t),
                            PROV_KEY: "legal_actions.unequip_item"})

        if "hand_off_item" in la:
            ho = la["hand_off_item"]
            for f in ho["from_indices"]:
                for t in range(int(ho["team_size"])):
                    if t == f:
                        continue
                    out.append({"kind": "hand_off_item", "from_index": int(f), "to_index": int(t),
                                PROV_KEY: "legal_actions.hand_off_item"})
        return out

    return out


def py_action_to_engine(a: dict):
    """Normalized action -> the real `engine.Action` the engine executes."""
    k = a["kind"]
    if k == "choose_starter":
        return engine.ChooseStarter(int(a["species_id"]))
    if k == "advance_map":
        return engine.AdvanceMap()
    if k == "visit_node":
        return engine.VisitNode(a["node_id"])
    if k == "select_option":
        return engine.SelectOption(index=a["index"], cancel=bool(a.get("cancel")))
    if k == "use_item":
        return engine.UseItem(item_index=int(a["bag_index"]), target_index=int(a["target_index"]))
    if k == "equip_item":
        return engine.EquipItem(bag_index=int(a["bag_index"]), team_index=int(a["team_index"]))
    if k == "unequip_item":
        return engine.UnequipItem(team_index=int(a["team_index"]))
    if k == "hand_off_item":
        return engine.HandOffItem(from_index=int(a["from_index"]), to_index=int(a["to_index"]))
    if k == "reorder_team":
        # A transposition expressed in `ReorderTeam`'s permutation form.
        raise AssertionError("reorder_team needs the team size; use py_reorder_action")
    raise ValueError(f"unknown normalized action kind {k}")


def py_reorder_action(a: dict, team_size: int) -> "engine.ReorderTeam":
    """One compared transposition, expressed in `ReorderTeam`'s permutation
    form. The identity order with exactly two positions exchanged is the same
    mutation the source's drag handler performs (64798-64806), so the wider
    permutation API is never used to express anything wider than the source
    can do -- see `REORDER_DOMAIN`."""
    order = list(range(team_size))
    order[a["i"]], order[a["j"]] = order[a["j"]], order[a["i"]]
    return engine.ReorderTeam(order=tuple(order))


# ===========================================================================
# Bounded search over the Python engine: honest snapshot/restore
# ===========================================================================
#
# A bounded search explores a tree of action prefixes: it steps the engine
# forward, and when a branch dies it must return to an ANCESTOR and try a
# different action from there. That backtrack is only sound if restoring a
# snapshot restores EVERYTHING the next `step()` reads -- most of all the
# RNG.
#
# `engine.Engine` owns a PRIVATE `rng.Mulberry32` (`_rng_stream`, engine.py:
# 677) and swaps it in as `pokelike.rng`'s module-level "active" stream for
# the exact duration of each `reset()`/`step()` call, restoring the previous
# active stream on the way out (engine.py:718, 789). So OUTSIDE a step --
# which is precisely when a search snapshots -- `rng.get_rng_seed()` does
# NOT read the engine's stream at all; it reads whatever is active by
# default, the module-level `_stream_b` singleton the engine never touches.
#
# A snapshotter written as `(deepcopy(engine.state), rng.get_rng_seed())`
# therefore captures a CONSTANT (0, on a tree where nothing else uses the
# default stream) and restores that constant into the wrong object, leaving
# `engine._rng_stream` wherever the abandoned branch happened to advance it.
# Re-running the same action from the same restored state then draws from a
# different RNG position and produces a DIFFERENT outcome: the search gets
# free re-rolls, and a route it "found" does not reproduce when replayed.
# Such a route is not evidence of anything, and no route produced that way
# is credited anywhere in this tool.
#
# The two functions below are the honest form: they snapshot and restore the
# engine's OWN stream object by value. `Mulberry32` holds a single 32-bit
# integer (`rng.py:92-115`) and `seed()` sets that raw state directly with no
# golden-ratio mixing, so `stream.seed(stream.state)` is an exact identity
# round-trip and the pair below is a complete, lossless checkpoint of
# everything a subsequent `step()` can read.
#
# `SearchSnapshotHonestyTests` (pokelike/tests/test_sweep_search.py) pins
# this executably, and fails against the broken form.


def engine_snapshot(eng: "engine.Engine") -> tuple:
    """A complete, restorable checkpoint of `eng`: its run state and its own
    private RNG stream position. See this section's header for why the RNG
    half must read `eng._rng_stream` and not `rng.get_rng_seed()`."""
    return (copy.deepcopy(eng.state), eng._rng_stream.state)


def engine_restore(eng: "engine.Engine", snap: tuple) -> None:
    """Restore a checkpoint taken by `engine_snapshot`. Deep-copies the state
    back so the SAME snapshot can be restored repeatedly -- a search tries
    several actions from one node -- without later steps mutating the stored
    copy."""
    state, rng_state = snap
    eng.state = copy.deepcopy(state)
    eng._rng_stream.seed(rng_state)


# ===========================================================================
# The two runtimes
# ===========================================================================


class JsRuntime:
    """One `node sweep-host.js` subprocess, reused across episodes (each
    `reset` builds a brand-new VM context inside it, so episodes stay
    independent -- see sweep-host.js)."""

    # stderr is DRAINED BY A THREAD, never left as an unread pipe. The source
    # prints "PokeAPI unavailable, using fallback data" on every episode reset
    # (`fetchSpeciesList`'s offline warning -- the MODELLED path, see driver.js's
    # network guard). Left undrained, the OS pipe buffer fills after roughly 25
    # episodes and the node process blocks forever on write while this side
    # blocks forever on read. That is exactly how the first corpus run died, and
    # it presented as a product hang rather than the plumbing bug it was.

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["node", os.path.join(_HERE, "sweep-host.js")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
            cwd=os.path.dirname(_HERE),
        )
        self._stderr: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        hello = self._call({"op": "hello"})
        if hello["protocol"] != 1:
            raise RuntimeError(f"sweep-host protocol {hello['protocol']}, expected 1")

    def _drain_stderr(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:
            # A bounded tail only: diagnostics for a dead host, not evidence.
            self._stderr.append(line)
            if len(self._stderr) > 200:
                del self._stderr[:100]

    def _call(self, req: dict) -> dict:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("sweep-host died: " + "".join(self._stderr[-20:])[:4000])
        resp = json.loads(line)
        if not resp.get("ok"):
            raise SourceRuntimeError(resp.get("error", "unknown"))
        return resp["value"]

    def reset(self, config: dict) -> None:
        self._call({"op": "reset", "config": config})

    def legal(self) -> dict:
        return self._call({"op": "legal"})

    def state(self, battles_seen: int, event: dict) -> dict:
        return self._call({"op": "state", "battles_seen": battles_seen, "event": event})

    def apply(self, action: dict) -> None:
        self._call({"op": "apply", "action": {k: v for k, v in action.items() if k != PROV_KEY}})

    def close(self) -> None:
        try:
            self._call({"op": "quit"})
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


class SourceRuntimeError(RuntimeError):
    """The source runtime refused or failed an operation."""


class PyRuntime:
    """The Python engine, projected exactly the way `run_scenario.Runner`
    projects it for the frozen 29-scenario gate."""

    def __init__(self) -> None:
        self.runner: Optional[run_scenario.Runner] = None
        self._restore = None

    def reset(self, config: dict) -> None:
        self.close()
        scenario = {
            "schema_version": run_scenario.SCHEMA_VERSION,
            "scenario": config.get("scenario", "sweep"),
            "seed": int(config["seed"]),
            "mode": config["mode"],
            "actions": [],
        }
        self.runner = run_scenario.Runner(scenario)
        self._restore = self.runner._install_battle_recorder()
        # M7's own stage capture, wrapped OUTSIDE the Runner's recorder so it
        # sees the same BattleResult at the same index. See sweep-adapter.js's
        # matching JS wrapper for why stages must be compared and why neither
        # pre-M7 gate carries them.
        self.stages: list[dict] = []
        # M7-COMBINED (A1): the ability half of the same capture. Read off the
        # `Combatant.gen3_ability` the engine's own `on_switch_in` assigned
        # (battle_abilities.py:203) -- never recomputed here, so a table drift
        # on one side cannot be cancelled out by the same drift on the other.
        self.abilities: list[dict] = []
        inner = engine._run_battle

        def stage_capturing(state, enemy_team):
            result = inner(state, enemy_team)
            self.stages.append({
                "player": [_stages_of(m) for m in result.player_team],
                "enemy": [_stages_of(m) for m in result.enemy_team],
            })
            self.abilities.append({
                "player": [_ability_of(m) for m in result.player_team],
                "enemy": [_ability_of(m) for m in result.enemy_team],
            })
            return result

        engine._run_battle = stage_capturing  # type: ignore[assignment]
        self.runner.engine.reset(
            nuzlocke_mode=bool(config["mode"].get("nuzlocke")),
            gen2_mode=bool(config["mode"].get("gen2")),
            gen3_mode=bool(config["mode"].get("gen3")),
            gen4_mode=bool(config["mode"].get("gen4")),
            seed=int(config["seed"]),
        )

    @property
    def state(self):
        assert self.runner is not None
        return self.runner.engine.state

    def legal(self) -> list[dict]:
        return py_legal_actions(self.state)

    def projection(self, battles_seen: int, event: dict) -> dict:
        assert self.runner is not None
        cp = self.runner.checkpoint("sweep", event)
        return {
            "checkpoint": cp,
            "battles": self.runner.battles[battles_seen:],
            "battle_stages": self.stages[battles_seen:],
            "battle_abilities": self.abilities[battles_seen:],
            "run_passives": _run_passives_of(self.state),
            "battles_total": len(self.runner.battles),
            "rng_draws_total": self.runner.counter.draws,
        }

    def apply(self, action: dict) -> None:
        assert self.runner is not None
        if action["kind"] == "reorder_team":
            act = py_reorder_action(action, len(self.state.team))
        else:
            act = py_action_to_engine(action)
        self.runner.engine.step(act)

    def close(self) -> None:
        if self._restore is not None:
            engine._run_battle = self._restore  # type: ignore[assignment]
            self._restore = None
        self.runner = None


# ===========================================================================
# Comparison
# ===========================================================================
# The M7 comparison projection is `run_scenario`/`driver.js`'s checkpoint --
# every field the frozen gate already compares -- PLUS the battles produced by
# the action just taken, PLUS the legal-action sets. `seq` is excluded (each
# side counts its own checkpoints and the sweep emits them in lockstep, so it
# is bookkeeping, not behaviour); `scenario` is excluded (it is the episode
# label, identical by construction). Everything else is compared. See SWEEP.md
# for the full disposition table.
EXCLUDED_CHECKPOINT_FIELDS = ("seq", "scenario")

# How many field-level diffs a divergence record keeps. Generous on purpose:
# the canonical key order puts `checkpoint.pending` (which can carry hundreds
# of fields across a multi-option offer) before `checkpoint.rng`, so a tight
# cap silently truncated away the RNG fields -- the single most diagnostic
# evidence for an RNG-stream divergence. Caught by M7's own M3 mutant, which
# survived purely because its attribution had been cut off.
MAX_RECORDED_DIFFS = 400


def _stages_of(mon) -> dict:
    st = getattr(mon, "stages", None) or {}
    return {k: int(st.get(k) or 0) for k in ("atk", "def", "speed", "special", "spdef")}


def _ability_of(mon) -> Optional[str]:
    """M7-COMBINED (A1). The ability the battle ACTUALLY resolved onto this
    combatant, read off the field `battle_abilities.on_switch_in` wrote
    (`Combatant.gen3_ability`, battle.py:228, default `None`). The source
    counterpart is `combatant._gen3Ability`, assigned by the same event
    (bundle.deobfuscated.js:57696-57702) onto the very clone `runBattle`
    returns as `res.pTeam`/`res.eTeam`. A combatant that never switched in
    carries no ability on either side -- Python `None`, JS `undefined` -- and
    both normalize to `null` rather than to a recomputed table lookup."""
    value = getattr(mon, "gen3_ability", None)
    return str(value) if value else None


def _run_passives_of(state) -> list:
    """M7-COMBINED (A1). The run-level passive ids.

    This is the ONLY trait/passive input to a battle that varies across the
    declared Story/Nuzlocke Gen1-4 surface: `runBattleScreen`'s non-Endless
    config branch is literally `buildTraitsConfig({}, {}, state.passives || [])`
    (bundle.deobfuscated.js:81076-81085), so both TIER maps are the constant
    `{}` and `state.passives` carries the whole varying part. `engine.
    _battle_configs` (engine.py:1247-1253) mirrors that exactly. Comparing the
    passive list is therefore comparing the real trait/passive state, and the
    tier maps are recorded as a limitation in SWEEP.md rather than compared as
    an invented value."""
    out = []
    for t in (getattr(state, "passives", None) or ()):
        tid = getattr(t, "id", None)
        out.append(str(tid) if tid else None)
    return out


def project(side: dict) -> dict:
    cp = {k: v for k, v in side["checkpoint"].items() if k not in EXCLUDED_CHECKPOINT_FIELDS}
    return {
        "checkpoint": cp,
        "battles": side["battles"],
        # M7 enrichment over the frozen schema -- see SWEEP.md.
        "battle_stages": side["battle_stages"],
        # M7-COMBINED (A1) enrichment -- see SWEEP.md's disposition table.
        "battle_abilities": side["battle_abilities"],
        "run_passives": side["run_passives"],
        "rng_draws_total": side["rng_draws_total"],
    }


def compare_projection(js: dict, py: dict) -> list[dict]:
    diffs = checkpoints.diff_values(checkpoints.canonical(project(js)),
                                    checkpoints.canonical(project(py)))
    return [{"path": p, "js": checkpoints.canonical(a), "py": checkpoints.canonical(b)}
            for p, a, b in diffs]


def compare_legal(js_actions: list[dict], py_actions: list[dict]) -> Optional[dict]:
    js_set, py_set = canon_set(js_actions), canon_set(py_actions)
    if js_set == py_set:
        return None
    js_only = [a for a in js_set if a not in set(py_set)]
    py_only = [a for a in py_set if a not in set(js_set)]
    return {"js_only": js_only, "py_only": py_only,
            "js_count": len(js_set), "py_count": len(py_set)}


def digest(value: Any) -> str:
    return checkpoints.sha256_of(value)


# ===========================================================================
# Coverage
# ===========================================================================


def load_targets() -> dict:
    with open(TARGETS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# The families the denominator is DERIVED from, read off the runtime rather
# than restated here. `validate_targets` checks each of them in BOTH
# directions: a manifest value the runtime does not have is rejected, and a
# runtime value no target names is rejected. One-directional checking is what
# let the audit delete `node.start` from the manifest and still pass.

# `map_gen`'s node-type constants are its module-level UPPERCASE `str`
# attributes -- the same 18 names the weight tables and the retype sites use
# (map_gen.py:70-86). Derived dynamically ON PURPOSE: a node type added to
# `map_gen` and not to the manifest must fail this gate rather than silently
# shrink the denominator. Nothing is subtracted; the allow-list below exists
# so that a future non-node uppercase `str` constant is an explicit decision
# with a reason, never a quiet exclusion.
NON_NODE_STRING_CONSTANTS: frozenset[str] = frozenset()


def runtime_node_types() -> set[str]:
    """Every `map_gen` node type the Story/Nuzlocke generator can emit."""
    found = {v for n, v in vars(map_gen).items()
             if n.isupper() and isinstance(v, str)}
    return found - NON_NODE_STRING_CONSTANTS


def runtime_reward_kinds() -> set[str]:
    """Every `SUBMAP_REWARDS` id a REWARD node can be baked with

    (`data.get_submap_rewards()`, bundle.deobfuscated.js:76303-76377). The
    observed form is `node.reward.kind` -- a plain string id, which is what
    `observe_coverage` credits `reward.<kind>` from.
    """
    return {r.id for r in data.get_submap_rewards()}


def validate_targets(targets: dict) -> list[str]:
    """The manifest must be internally consistent AND consistent with the code
    it claims to be derived from. A manifest that names a phase, action kind
    or route tag the runtime does not have is a coverage denominator built on
    fiction, so it is rejected rather than reported as 'missing'."""
    problems: list[str] = []
    ids = [t["id"] for t in targets["targets"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate target ids: {dupes}")

    phases = {p.value for p in engine.Phase}
    kinds = {"choose_starter", "advance_map", "visit_node", "select_option",
             "use_item", "equip_item", "unequip_item", "hand_off_item", "reorder_team"}
    tags = set(route_coverage.REQUIRED_TAGS)
    node_types = runtime_node_types()
    reward_kinds = runtime_reward_kinds()

    for t in targets["targets"]:
        for field in ("id", "stratum", "evidence", "rationale"):
            if not t.get(field):
                problems.append(f"{t.get('id', '?')}: missing {field}")
        ev = t.get("evidence")
        if ev not in ("sweep", "battle-oracle", "route-corpus", "excluded"):
            problems.append(f"{t['id']}: unknown evidence source {ev!r}")
        if t.get("phase") and t["phase"] not in phases:
            problems.append(f"{t['id']}: phase {t['phase']!r} is not a Phase")
        if t.get("action_kind") and t["action_kind"] not in kinds:
            problems.append(f"{t['id']}: action_kind {t['action_kind']!r} is not an action")
        if t.get("route_tag") and t["route_tag"] not in tags:
            problems.append(f"{t['id']}: route_tag {t['route_tag']!r} is not a REQUIRED_TAG")
        if t.get("node_type") and t["node_type"] not in node_types:
            problems.append(f"{t['id']}: node_type {t['node_type']!r} is not a map_gen node type")
        if t.get("reward_kind") and t["reward_kind"] not in reward_kinds:
            problems.append(f"{t['id']}: reward_kind {t['reward_kind']!r} is not a SUBMAP_REWARDS id")
        # An id in a derived family that carries no derived key is the same
        # silent hole from the other side: it would never be checked at all.
        for prefix, key in (("node.", "node_type"), ("reward.", "reward_kind")):
            if t["id"].startswith(prefix) and not t.get(key):
                problems.append(f"{t['id']}: a {prefix}* target must declare {key}")
        if ev == "excluded" and not t.get("exclusion_reason"):
            problems.append(f"{t['id']}: excluded targets need an exclusion_reason")

    # Every Phase and every action kind must appear somewhere, or the
    # denominator has a silent hole.
    covered_phases = {t.get("phase") for t in targets["targets"] if t.get("phase")}
    for p in sorted(phases - covered_phases):
        problems.append(f"no target names Phase {p}")
    covered_kinds = {t.get("action_kind") for t in targets["targets"] if t.get("action_kind")}
    for k in sorted(kinds - covered_kinds):
        problems.append(f"no target names action kind {k}")
    # ...and the same completeness rule for the two families the M7-A audit
    # found unenforced. Deleting `node.start` from the manifest used to pass.
    covered_nodes = {t.get("node_type") for t in targets["targets"] if t.get("node_type")}
    for n in sorted(node_types - covered_nodes):
        problems.append(f"no target names map_gen node type {n}")
    covered_rewards = {t.get("reward_kind") for t in targets["targets"] if t.get("reward_kind")}
    for r in sorted(reward_kinds - covered_rewards):
        problems.append(f"no target names submap reward kind {r}")
    return problems


class CoverageLedger:
    """Which targets an evidence stream actually earned, and from where."""

    def __init__(self, targets: dict) -> None:
        self.targets = targets
        self.by_id = {t["id"]: t for t in targets["targets"]}
        self.earned: dict[str, dict] = {}

    def hit(self, target_id: str, source: str, where: str) -> None:
        if target_id not in self.by_id:
            raise KeyError(f"unknown coverage target {target_id!r}")
        rec = self.earned.setdefault(target_id, {"source": source, "count": 0, "first": where})
        rec["count"] += 1

    def merge(self, other: "CoverageLedger") -> None:
        for tid, rec in other.earned.items():
            cur = self.earned.setdefault(tid, {"source": rec["source"], "count": 0,
                                               "first": rec["first"]})
            cur["count"] += rec["count"]

    def missing(self) -> list[str]:
        return sorted(t["id"] for t in self.targets["targets"]
                      if t["evidence"] != "excluded" and t["id"] not in self.earned)

    def report(self) -> dict:
        return {
            "manifest_version": self.targets["version"],
            "total": len(self.by_id),
            "required": sum(1 for t in self.targets["targets"] if t["evidence"] != "excluded"),
            "excluded": sorted(t["id"] for t in self.targets["targets"]
                               if t["evidence"] == "excluded"),
            "earned": {k: v for k, v in sorted(self.earned.items())},
            "missing": self.missing(),
        }


def observe_coverage(ledger: CoverageLedger, step: dict, episode: dict) -> None:
    """Derive coverage from what ACTUALLY happened in one compared step.

    Everything below reads the OBSERVED projection (the state both runtimes
    agreed on) or the observed legal set -- never the episode's intent, never
    the scheduler's target, never the manifest itself.
    """
    where = f"{episode['episode_id']}#{step['index']}"
    by = ledger.by_id

    def hit(tid: str) -> None:
        if tid in by:
            ledger.hit(tid, "sweep", where)

    cp = step["state_after"]["checkpoint"]
    before = step.get("state_before", {}).get("checkpoint") or {}
    action = step["action"]
    legal = step["legal"]

    # -- episode/configuration strata --
    # From the checkpoint's OWN `mode`, which is a compared field, rather
    # than from the plan entry that asked for the episode: the plan is
    # intent, the checkpoint is what both runtimes actually reported.
    observed_mode = cp.get("mode") or {}
    hit("episode." + bucket_name(observed_mode))
    if action["kind"] == "choose_starter":
        hit(f"starter.gen{generation_of(observed_mode)}."
            f"{episode['starter_position']}")

    # -- phase strata: the screen both runtimes agreed on --
    hit("phase." + str(cp.get("screen")))
    if before.get("screen"):
        hit("phase." + str(before["screen"]))

    # -- action strata --
    hit("action." + action["kind"])
    if action["kind"] == "select_option":
        pend = before.get("pending") or {}
        fam = pend.get("phase")
        if fam:
            hit(f"pending.{fam}")
            hit("optional.required" if not pend.get("optional") else "optional.optional")
        if action.get("cancel"):
            hit("exit.cancel")
        elif action["index"] is None:
            hit("exit.skip_or_bank")
        else:
            hit("exit.pick")

    # The legality surface itself is evidence: seeing the cancel exit OFFERED
    # (and agreed on by both runtimes) is what proves the M7 declaration fix,
    # independently of whether the scheduler ever chose it.
    for a in legal["actions"]:
        if a.get("kind") == "select_option" and a.get("cancel"):
            hit("legality.item_equip_cancel_offered")

    # -- node/content strata: the node the action actually resolved --
    if action["kind"] == "visit_node":
        nodes = {n["id"]: n for n in (before.get("map") or {}).get("nodes", [])}
        node = nodes.get(action["node_id"])
        if node:
            hit("node." + str(node.get("type")))
            if node.get("reward", {}) and node["reward"].get("kind"):
                hit("reward." + str(node["reward"]["kind"]))

    # -- which submap the run is standing IN -------------------------------
    # M7-COMBINED (A4). `submap.underground` / `submap.distortion` used to be
    # credited from a node's `sub_kind`, and were unearnable: BOTH normalizers
    # carry that field only if the runtime sets it (`driver.js:427`
    # `if (n.subKind !== undefined)`, `run_scenario.py:156`), and NEITHER
    # runtime ever sets it -- `subKind` does not appear anywhere in the bundle
    # or in `pokelike/`. So the two targets were keyed off a field that has
    # never existed, which is why runs that demonstrably entered a submap and
    # earned `node.reward`, `node.subexit`, `lifecycle.submap_enter/exit` and
    # four `reward.*` kinds still left these two unearned.
    #
    # The submap's kind IS observed, on the compared checkpoint itself:
    # `enterSubMap` sets `state.inSubMap` to the kind (`engine.py:3198`,
    # `state.in_sub_map = kind`) and `exitSubMap` clears it (3226), and
    # `checkpoint.in_sub_map` is a COMPARED field on both sides
    # (`driver.js:827`). Crediting from it is the same rule `node.start` uses:
    # state both runtimes produced and this step already proved identical --
    # not the manifest, and not the scheduler's intent.
    for checkpoint in (before, cp):
        kind = checkpoint.get("in_sub_map")
        if kind:
            hit("submap." + str(kind))

    # -- node occupancy: the node the AGREED state says the run stands on --
    # `node.start` is real, required and observable, but NO `visit_node`
    # action can ever earn it: the layer-0 entry node is where `startMap`
    # puts the run, it is `visited` from that moment, and it is never
    # offered as an accessible visit. Earning `node.*` only inside the
    # `visit_node` branch therefore left one required target unearnable --
    # and the M7-A audit deleted it from the manifest with nothing failing.
    # It is earned here from the COMPARED checkpoint's own `current_node`
    # and `map.nodes`: state both runtimes produced and this step already
    # proved identical, never the manifest and never the scheduler's target.
    def hit_occupied(checkpoint: dict) -> None:
        nid = checkpoint.get("current_node")
        if not nid:
            return
        for n in (checkpoint.get("map") or {}).get("nodes", []):
            if n.get("id") == nid and n.get("type"):
                hit("node." + str(n["type"]))
                return

    hit_occupied(before)
    hit_occupied(cp)

    # -- map/submap lifecycle --
    if action["kind"] == "advance_map":
        hit("lifecycle.map_advance")
    if not before.get("in_sub_map") and cp.get("in_sub_map"):
        hit("lifecycle.submap_enter")
    if before.get("in_sub_map") and not cp.get("in_sub_map"):
        hit("lifecycle.submap_exit")

    # -- resume-cache reuse --
    rs = cp.get("resume_state") or {}
    for key, tid in (("saved_catch", "resume.catch"),
                     ("saved_question_resolve", "resume.question"),
                     ("saved_shiny_node", "resume.shiny")):
        if rs.get(key):
            hit(tid)

    # -- terminal --
    if cp.get("game_over"):
        hit("outcome.loss")
    if cp.get("screen") == "win-screen":
        hit("outcome.win")

    # -- battle strata, from the battles this action produced --
    for b in step["state_after"]["battles"]:
        hit("battle.any")
        hit("battle.win" if b["player_won"] else "battle.loss")
        for ev in b.get("status_events", []):
            hit("battle.status_" + str(ev.get("type")))
        for turn in b.get("turns", []):
            for e in turn.get("events", []):
                if e.get("crit"):
                    hit("battle.crit")
                eff = e.get("type_eff")
                if eff is not None:
                    if eff == 0:
                        hit("battle.immune")
                    elif eff > 1:
                        hit("battle.super_effective")
                    elif eff < 1:
                        hit("battle.resisted")
        for mon in list(b.get("player_team", [])) + list(b.get("enemy_team", [])):
            if mon and mon.get("current_hp") == 0:
                hit("battle.faint")
            if mon and mon.get("held_item"):
                hit("battle.held_item_present")


# ===========================================================================
# Episode execution
# ===========================================================================


def run_episode(
    js: JsRuntime,
    py: PyRuntime,
    config: dict,
    policy,
    max_steps: int,
    ledger: Optional[CoverageLedger] = None,
    forced_actions: Optional[list[dict]] = None,
) -> dict:
    """Run one episode in lockstep. Returns a fully self-describing record.

    ``policy(step_index, legal_actions, state) -> action`` chooses;
    ``forced_actions`` replaces the policy entirely, which is what makes a
    replay independent of the scheduler that originally produced it.
    """
    episode: dict = {
        "sweep_version": SWEEP_VERSION,
        "schema_version": run_scenario.SCHEMA_VERSION,
        "episode_id": config["episode_id"],
        "config": {"seed": int(config["seed"]), "mode": config["mode"]},
        "policy_seed": config.get("policy_seed"),
        "starter_position": None,
        "steps": [],
        "actions": [],
        "outcome": None,
        "divergence": None,
    }

    js.reset(config)
    py.reset(config)

    js_battles = py_battles = 0
    before_js = js.state(js_battles, {"phase": "initial"})
    before_py = py.projection(py_battles, {"phase": "initial"})
    js_battles, py_battles = before_js["battles_total"], before_py["battles_total"]

    initial_diffs = compare_projection(before_js, before_py)
    if initial_diffs:
        episode["divergence"] = {"kind": "state", "index": -1, "diffs": initial_diffs[:MAX_RECORDED_DIFFS]}
        return _finish(episode)

    for index in range(max_steps):
        # ---- 1. enumerate BOTH sides -------------------------------------
        js_legal = js.legal()
        py_legal = py.legal()

        for side, actions in (("js", js_legal["actions"]), ("py", py_legal)):
            err = action_multiset_error(actions)
            if err:
                episode["divergence"] = {"kind": "legal_multiset", "index": index,
                                         "side": side, "detail": err}
                return _finish(episode)

        # ---- 2. compare them BEFORE anything chooses ----------------------
        mismatch = compare_legal(js_legal["actions"], py_legal)
        if mismatch:
            episode["divergence"] = {
                "kind": "legal", "index": index, "detail": mismatch,
                "js_screen": js_legal["screen"],
                "py_screen": before_py["checkpoint"]["screen"],
                "state": before_py,
            }
            return _finish(episode)

        if not py_legal:
            episode["outcome"] = "terminal"
            break

        # ---- 3. choose ONE action, for both -------------------------------
        if forced_actions is not None:
            if index >= len(forced_actions):
                episode["outcome"] = "replay_exhausted"
                break
            action = forced_actions[index]
            if canon_action(action) not in set(canon_set(py_legal)):
                episode["divergence"] = {"kind": "replay_illegal", "index": index,
                                         "detail": {"action": action}}
                return _finish(episode)
        else:
            action = policy(index, py_legal, before_py)

        if action["kind"] == "choose_starter" and episode["starter_position"] is None:
            offered = [a["species_id"] for a in _stable_sorted(
                [x for x in py_legal if x["kind"] == "choose_starter"])]
            episode["starter_position"] = offered.index(action["species_id"])

        bare = {k: v for k, v in action.items() if k != PROV_KEY}
        episode["actions"].append(bare)

        # ---- 4. execute through each runtime's REAL path -------------------
        js_err = py_err = None
        try:
            js.apply(action)
        except SourceRuntimeError as exc:
            js_err = str(exc)
        try:
            py.apply(action)
        except Exception as exc:  # noqa: BLE001 -- compared, never swallowed
            py_err = f"{type(exc).__name__}: {exc}"

        if bool(js_err) != bool(py_err):
            episode["divergence"] = {"kind": "apply_error_asymmetry", "index": index,
                                     "detail": {"action": bare, "js": js_err, "py": py_err}}
            return _finish(episode)
        if js_err and py_err:
            episode["divergence"] = {"kind": "apply_error_both", "index": index,
                                     "detail": {"action": bare, "js": js_err, "py": py_err}}
            return _finish(episode)

        # ---- 5. compare state after EVERY action ---------------------------
        after_js = js.state(js_battles, {"action": bare, "index": index})
        after_py = py.projection(py_battles, {"action": bare, "index": index})
        js_battles, py_battles = after_js["battles_total"], after_py["battles_total"]

        diffs = compare_projection(after_js, after_py)
        if diffs:
            episode["divergence"] = {"kind": "state", "index": index, "action": bare,
                                     "diffs": diffs[:MAX_RECORDED_DIFFS],
                                     "js": project(after_js), "py": project(after_py)}
            return _finish(episode)

        full_step = {
            "index": index,
            "action": bare,
            "legal": {"actions": [{k: v for k, v in a.items() if k != PROV_KEY}
                                  for a in py_legal]},
            "state_before": before_py,
            "state_after": after_py,
        }
        if ledger is not None:
            observe_coverage(ledger, full_step, episode)

        # Only DIGESTS are retained on the clean path, so a 200-episode result
        # file stays inspectable; the divergent step above keeps its full
        # projections, which is the step an auditor actually has to read.
        episode["steps"].append({
            "index": index,
            "action": bare,
            "legal_digest": digest(canon_set(py_legal)),
            "legal_count": len(py_legal),
            "state_digest": digest(project(after_py)),
            "screen": after_py["checkpoint"]["screen"],
            "rng": after_py["checkpoint"]["rng"],
        })

        before_py = after_py
        cp = after_py["checkpoint"]
        if cp.get("game_over"):
            episode["outcome"] = "loss"
            break
        if cp.get("screen") == "win-screen":
            episode["outcome"] = "win"
            break
    else:
        episode["outcome"] = "step_cap"

    return _finish(episode)


def _finish(episode: dict) -> dict:
    if episode["outcome"] is None:
        episode["outcome"] = "diverged" if episode["divergence"] else "terminal"
    episode["steps_taken"] = len(episode["actions"])
    episode["max_depth"] = max((s.get("index", -1) for s in episode["steps"]), default=-1) + 1
    # Order-independent per-episode identity: config + the exact ordered
    # actions + the ordered post-action state digests. Nothing about batch
    # position, wall clock or scheduler internals enters it.
    episode["episode_digest"] = digest({
        "config": episode["config"],
        "actions": episode["actions"],
        "states": [s["state_digest"] for s in episode["steps"]],
        "outcome": episode["outcome"],
    })
    return episode


# ===========================================================================
# Policies
# ===========================================================================
# Every policy is a pure function of (step index, legal set, state, seeded
# RNG). No clock, no filesystem, no dict-iteration order -- an episode is
# fully determined by its (seed, policy_seed) pair, which is what makes the
# replay and order-independence gates mean anything.

_PROGRESS_KINDS = ("visit_node", "advance_map", "choose_starter", "select_option")


def _stable_sorted(actions: list[dict]) -> list[dict]:
    return sorted(actions, key=canon_action)


def random_policy(rng: random.Random, prefer_progress: float = 0.75):
    """Uniform over the legal set, biased toward actions that ADVANCE the run.

    A strictly uniform policy is a poor sampler here, and not because of any
    correctness concern: the map screen offers O(team^2) utility actions
    against a handful of nodes, so uniform sampling spends nearly every step
    reshuffling the team and reaches almost no map depth. The bias is a
    coverage device. Every action it can pick is legal on BOTH runtimes, the
    utility families keep their own required targets, and they are still
    chosen a quarter of the time.
    """

    def policy(index: int, legal: list[dict], state: dict) -> dict:
        options = _stable_sorted(legal)
        progress = [a for a in options if a["kind"] in _PROGRESS_KINDS]
        if progress and rng.random() < prefer_progress:
            return rng.choice(progress)
        return rng.choice(options)

    return policy


def guided_policy(rng: random.Random, ledger: CoverageLedger):
    """Coverage-guided: prefer an action whose family is still un-earned.

    The preference is a heuristic over the SAME legal set the uniform policy
    draws from -- only the run itself can prove what a step actually earned,
    so this never asserts coverage, it only re-orders candidates.
    """
    base = random_policy(rng, prefer_progress=0.8)

    def wants(tid: str) -> bool:
        return tid in ledger.by_id and tid not in ledger.earned

    def policy(index: int, legal: list[dict], state: dict) -> dict:
        options = _stable_sorted(legal)
        hungry = [a for a in options if wants("action." + a["kind"])]
        if hungry:
            return rng.choice(hungry)
        if wants("exit.cancel"):
            cancels = [a for a in options if a.get("cancel")]
            if cancels:
                return cancels[0]
        if wants("exit.skip_or_bank"):
            skips = [a for a in options
                     if a["kind"] == "select_option" and a["index"] is None and not a.get("cancel")]
            if skips:
                return skips[0]
        return base(index, legal, state)

    return policy


# ===========================================================================
# The goal-directed bounded scheduler (M7-COMBINED A4)
# ===========================================================================
# `random_policy` and `guided_policy` are samplers. They reach shallow content
# reliably and deep content never: after 258 episodes they left 30 required
# targets unearned, every one of them behind either a map advance or a submap
# entry. That is a DEPTH shortfall, and no amount of extra uniform episodes
# fixes it, because the probability of a random walk surviving to map 3 and
# then picking one specific layer-4 node is not small, it is negligible.
#
# The source says exactly where the missing content lives, and the placement is
# deterministic rather than rolled:
#
#   * SILVER          gen2, layer 4, map indices 1/3/5/7   (map_gen.py:471-475)
#   * MAGMA + AQUA    gen3, layer 4, map indices 2/5/7     (map_gen.py:477-484)
#   * UNDERGROUND     gen4, layer 4, map indices 1/3/6     (map_gen.py:486-495)
#   * DISTORTION      gen4, layer 4, map indices 3/5/7     (map_gen.py:486-495)
#
# So `node.magma` is not "rare", it is BEHIND TWO MAP ADVANCES in exactly one
# generation. What is needed is a scheduler that survives that far and then
# walks to the right node -- not a luckier sampler.
#
# WHAT THIS SCHEDULER MAY AND MAY NOT DO. It only ever chooses among actions
# that `run_episode` has ALREADY enumerated on both runtimes and compared as
# equal sets (`run_episode` step 2 runs before step 3, which is where the
# policy is called). It re-orders that set; it cannot add to it, and it never
# sees the source's answer. Coverage is still credited only by
# `observe_coverage`, from the observed agreed step -- a target this policy
# steered toward but did not actually reach earns nothing, which is pinned by
# `SweepAccountingTests.test_the_guided_policy_wants_targets_but_never_credits_
# them`. The steering itself reads the COMPARED checkpoint projection (the
# state both runtimes already agreed on), never the Python engine directly.
#
# Determinism: the policy is a pure function of (the agreed projection, the
# still-unearned target set, a seeded `random.Random`). No clock, no
# filesystem, no batch position. The one input that is not per-episode is the
# ledger's earned set, which is why `hunt` records its plan and its per-episode
# digests -- see `hunt_plan`.


def _hunt_node_wants(ledger: "CoverageLedger") -> set[str]:
    """Node TYPES whose `node.<type>` target is still unearned."""
    return {t["node_type"] for t in ledger.targets["targets"]
            if t.get("node_type") and t["evidence"] != "excluded"
            and t["id"] not in ledger.earned}


def _hunt_submap_wants(ledger: "CoverageLedger") -> set[str]:
    """Submap KINDS still unearned, from `submap.*` and `reward.*` alike --
    every `reward.*` target lives inside a submap, so wanting any of them is
    wanting to be in one."""
    wants = set()
    for t in ledger.targets["targets"]:
        if t["evidence"] == "excluded" or t["id"] in ledger.earned:
            continue
        if t["id"].startswith("submap."):
            wants.add(t["id"].split(".", 1)[1])
        elif t["id"].startswith("reward.") or t["id"] in (
                "node.reward", "node.subexit", "lifecycle.submap_exit",
                "pending.reward_team_pick"):
            wants.update(("underground", "distortion"))
    return wants


def _team_health(checkpoint: dict) -> float:
    """Fraction of the team's total max HP currently standing, from the
    COMPARED checkpoint's own team projection."""
    team = checkpoint.get("team") or []
    total = sum(int(m.get("max_hp") or 0) for m in team)
    if not total:
        return 1.0
    have = sum(int(m.get("current_hp") or 0) for m in team)
    return have / total


def hunt_policy(rng: random.Random, ledger: "CoverageLedger"):
    """Coverage-guided, source-informed, and still a chooser over the AGREED
    legal set only.

    The ladder below is ordered by how strongly the source says the choice
    matters, not by taste:

      1. an offer that is still open gets ACCEPTED rather than skipped --
         declining is what keeps a team small and a run short, and several
         missing targets (`pending.*`, `optional.required`) only exist while
         an offer is live;
      2. a node whose OBSERVED type is one of the still-unearned node types
         gets visited, because that node type is the target;
      3. a submap gets entered when anything inside one is still wanted, and
         once inside, REWARD before SUBEXIT -- leaving early forfeits the
         twelve `reward.*` targets;
      4. a POKECENTER gets visited on a hurt team, because surviving to the
         next map is the only way any of the deep content is reachable at all;
      5. otherwise progress, so the run keeps moving toward the boss.
    """
    base = random_policy(rng, prefer_progress=0.9)

    def policy(index: int, legal: list[dict], state: dict) -> dict:
        options = _stable_sorted(legal)
        cp = state.get("checkpoint") or {}
        nodes = {n["id"]: n for n in (cp.get("map") or {}).get("nodes", [])}

        picks = [a for a in options
                 if a["kind"] == "select_option" and a["index"] is not None]
        if picks:
            return rng.choice(picks)

        visits = [a for a in options if a["kind"] == "visit_node"]
        if visits:
            def typed(kinds) -> list[dict]:
                return [a for a in visits
                        if (nodes.get(a["node_id"]) or {}).get("type") in kinds]

            if cp.get("in_sub_map"):
                # Inside a submap: rewards first, the exit last.
                inside = typed({map_gen.REWARD})
                if inside:
                    return rng.choice(inside)
                rest = [a for a in visits
                        if (nodes.get(a["node_id"]) or {}).get("type") != map_gen.SUBEXIT]
                if rest:
                    return rng.choice(rest)
                exits = typed({map_gen.SUBEXIT})
                if exits:
                    return rng.choice(exits)
            else:
                # -- survival before ambition ---------------------------------
                # The first version of this ladder put target-chasing at the
                # top and lost all 300 episodes, none of them reaching even
                # map 1 in Gen4. That is not bad luck: the deep content is
                # gated on map advances, a map advance is gated on beating a
                # gym leader, and a run that walks into every MAGMA/AQUA/
                # LEGENDARY node it can see arrives at that leader
                # under-levelled and dead. Everything below map 1 therefore
                # optimizes for ARRIVING, and target-chasing only outranks it
                # once the run can afford it.
                health = _team_health(cp)
                heal = typed({map_gen.POKECENTER})
                if heal and health < 0.75:
                    return rng.choice(heal)

                wanted = _hunt_node_wants(ledger) | _hunt_submap_wants(ledger)
                hot = typed(wanted)
                # A submap entrance is worth taking whenever it appears: it is
                # the gate in front of eighteen required targets, and its own
                # boss is optional once inside.
                gate = typed({map_gen.UNDERGROUND, map_gen.DISTORTION} & wanted)
                if gate:
                    return rng.choice(gate)
                if hot and health >= 0.6:
                    return rng.choice(hot)

                # Levels are the whole survival budget: a TRAINER win grants
                # +2 (customGain 0x2, 80327) against a wild win's +1 (0x1,
                # 77724), so a run that prefers trainers arrives at the boss
                # measurably stronger.
                if health >= 0.5:
                    for tier in ({map_gen.TRAINER}, {map_gen.BATTLE}):
                        strong = typed(tier)
                        if strong:
                            return rng.choice(strong)
                if heal:
                    return rng.choice(heal)
                # A bigger roster is more bodies between the run and a wipe.
                if len(cp.get("team") or []) < 4:
                    grow = typed({map_gen.CATCH})
                    if grow:
                        return rng.choice(grow)

        advance = [a for a in options if a["kind"] == "advance_map"]
        if advance:
            return advance[0]

        skips = [a for a in options
                 if a["kind"] == "select_option" and a["index"] is None
                 and not a.get("cancel")]
        if skips and not visits:
            return skips[0]
        if visits:
            return rng.choice(visits)
        return base(index, legal, state)

    return policy


# The generations that can produce each still-missing family, read off
# `map_gen`'s own placement rules rather than guessed. Used ONLY to spend the
# episode budget where the content can exist; it credits nothing.
HUNT_MODE_HINTS: dict[str, tuple[str, ...]] = {
    "node.silver": ("nuzlocke_gen2", "story_gen2"),
    "node.magma": ("story_gen3", "nuzlocke_gen3"),
    "node.aqua": ("story_gen3", "nuzlocke_gen3"),
    "node.underground": ("story_gen4", "nuzlocke_gen4"),
    "node.distortion": ("story_gen4", "nuzlocke_gen4"),
    "submap.underground": ("story_gen4", "nuzlocke_gen4"),
    "submap.distortion": ("story_gen4", "nuzlocke_gen4"),
    # The three DISTORTION legendary rewards are gen4-only for two
    # independent source reasons, so hinting them is placement, not taste:
    # `SUBMAP_REWARDS` gives each of them `kinds = ("distortion",)` alone
    # (data.get_submap_rewards), and DISTORTION is placed only on gen4 layer 4
    # of maps 3/5/7 (map_gen.py:465-495). They are also the only rewards that
    # never come out of `_pick_sub_map_rewards`' random pool at all -- it
    # excludes every id in `get_distortion_legend_rewards()`
    # (map_gen.py:1016-1020) -- and instead ride on `n2_0` as the submap's
    # `legendary_entry.reward` (map_gen.py:1126-1129).
    "reward.dialga": ("story_gen4", "nuzlocke_gen4"),
    "reward.giratina": ("story_gen4", "nuzlocke_gen4"),
    "reward.palkia": ("story_gen4", "nuzlocke_gen4"),
}


def hunt_plan(missing: list[str], episodes: int, base_seed: int,
              max_steps: int) -> dict:
    """A deterministic plan that spends its budget on the modes that can
    actually produce what is missing.

    Every episode is still a pure `(seed, policy_seed, mode)` triple, and the
    plan carries its own digest, so a hunt is replayable exactly like any other
    run and is independent of the order its episodes happen to execute in.
    """
    buckets = [bucket_name(m) for m in MODE_BUCKETS]
    wanted: list[str] = []
    for tid in sorted(missing):
        for bucket in HUNT_MODE_HINTS.get(tid, ()):
            if bucket not in wanted:
                wanted.append(bucket)
    # Anything with no generation hint can come from any bucket, so the rest of
    # the budget stays spread across all eight rather than narrowing blindly.
    order = wanted + [b for b in buckets if b not in wanted]

    rng = random.Random(base_seed)
    by_name = {bucket_name(m): m for m in MODE_BUCKETS}
    plan = {"sweep_version": SWEEP_VERSION, "base_seed": base_seed,
            "max_steps": max_steps, "hunt_for": sorted(missing), "episodes": []}
    for i in range(episodes):
        bucket = order[i % len(order)]
        plan["episodes"].append({
            "episode_id": f"hunt_{bucket}_{i:04d}",
            "seed": rng.getrandbits(32),
            "policy_seed": rng.getrandbits(32),
            "mode": dict(by_name[bucket]),
        })
    plan["plan_digest"] = digest(plan["episodes"])
    return plan


# ===========================================================================
# Planning
# ===========================================================================


def make_plan(episodes: int, base_seed: int, max_steps: int) -> dict:
    """A deterministic, machine-readable episode plan.

    Episodes are distributed round-robin across all eight mode-by-generation
    buckets, so the distribution is a property of the PLAN rather than of how
    a random draw happened to land.
    """
    rng = random.Random(base_seed)
    plan = {"sweep_version": SWEEP_VERSION, "base_seed": base_seed,
            "max_steps": max_steps, "episodes": []}
    for i in range(episodes):
        mode = MODE_BUCKETS[i % len(MODE_BUCKETS)]
        plan["episodes"].append({
            "episode_id": f"{bucket_name(mode)}_{i:04d}",
            "seed": rng.getrandbits(32),
            "policy_seed": rng.getrandbits(32),
            "mode": dict(mode),
        })
    plan["plan_digest"] = digest(plan["episodes"])
    return plan


def corpus_plan(max_steps: int) -> dict:
    """The pinned deterministic corpus: one episode per frozen route scenario's
    (seed, mode), so the sweep starts from configurations the 29-scenario gate
    already proves reachable, plus the compact legality cases M7 adds."""
    plan = {"sweep_version": SWEEP_VERSION, "base_seed": None,
            "max_steps": max_steps, "episodes": []}
    manifest_path = os.path.join(_HERE, "scenarios", "manifest.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    for entry in manifest["scenarios"]:
        with open(os.path.join(_HERE, "scenarios", entry["file"]), encoding="utf-8") as fh:
            sc = json.load(fh)
        plan["episodes"].append({
            "episode_id": "corpus_" + sc["scenario"],
            "seed": int(sc["seed"]),
            "policy_seed": int(sc["seed"]) ^ 0x5EED,
            "mode": dict(sc["mode"]),
            "source": entry["file"],
        })
    plan["plan_digest"] = digest(plan["episodes"])
    return plan


# ===========================================================================
# Runner + reduction
# ===========================================================================


def protected_hashes() -> dict:
    def sha(rel: str) -> str:
        path = os.path.join(os.path.dirname(_HERE), rel)
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    out = {"pokelike/engine.py": sha("pokelike/engine.py"),
           "pokelike/battle_loop.py": sha("pokelike/battle_loop.py"),
           "pokelike/battle_traits.py": sha("pokelike/battle_traits.py")}
    with open(os.path.join(_HERE, "prefix.sha256"), encoding="utf-8") as fh:
        out["route-prefix"] = fh.read().strip().split()[0]
    return out


def run_plan(plan: dict, ledger: CoverageLedger, guided: bool = False,
             verbose: bool = True, hunt: bool = False,
             stop_when_covered: bool = False) -> dict:
    started = time.time()
    js = JsRuntime()
    py = PyRuntime()
    results: list[dict] = []
    try:
        for spec in plan["episodes"]:
            rng = random.Random(spec["policy_seed"])
            if hunt:
                policy = hunt_policy(rng, ledger)
            elif guided:
                policy = guided_policy(rng, ledger)
            else:
                policy = random_policy(rng)
            ep = run_episode(js, py, spec, policy, plan["max_steps"], ledger=ledger)
            results.append(ep)
            if verbose:
                flag = "DIVERGED" if ep["divergence"] else ep["outcome"]
                print(f"  {spec['episode_id']:<28} steps={ep['steps_taken']:>4} {flag}",
                      flush=True)
            if ep["divergence"]:
                save_finding(ep, plan)
            # A hunt is BOUNDED: once nothing it was launched for is still
            # missing, the remaining budget is not spent. This shortens the
            # run; it cannot change any episode, because every episode is a
            # pure function of its own (seed, policy_seed) pair and the
            # per-episode digests are unaffected by how many follow.
            if stop_when_covered and not [t for t in plan.get("hunt_for", [])
                                          if t not in ledger.earned]:
                break
    finally:
        js.close()
        py.close()

    return {
        "sweep_version": SWEEP_VERSION,
        "plan_digest": plan["plan_digest"],
        "guided": guided,
        "hunt": hunt,
        "protected_hashes": protected_hashes(),
        "wall_clock_s": round(time.time() - started, 1),
        "episodes": results,
        "coverage": ledger.report(),
        "summary": summarize(results),
    }


def summarize(results: list[dict]) -> dict:
    by_bucket: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    by_starter: dict[str, int] = {}
    depth = 0
    steps = 0
    for ep in results:
        by_bucket[bucket_name(ep["config"]["mode"])] = \
            by_bucket.get(bucket_name(ep["config"]["mode"]), 0) + 1
        by_outcome[ep["outcome"]] = by_outcome.get(ep["outcome"], 0) + 1
        sp = str(ep.get("starter_position"))
        by_starter[sp] = by_starter.get(sp, 0) + 1
        steps += ep["steps_taken"]
        depth = max(depth, ep["steps_taken"])
    return {
        "episodes": len(results),
        "compared_action_steps": steps,
        "deepest_episode": depth,
        "by_bucket": dict(sorted(by_bucket.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_starter_position": dict(sorted(by_starter.items())),
        "diverged": sum(1 for e in results if e["divergence"]),
    }


def save_finding(ep: dict, plan: dict) -> str:
    """Durable, non-generated path so a reproducer survives the result file."""
    os.makedirs(FINDINGS_DIR, exist_ok=True)
    path = os.path.join(FINDINGS_DIR, f"M7-divergence-{ep['episode_id']}.json")
    record = {
        "sweep_version": SWEEP_VERSION,
        "protected_hashes": protected_hashes(),
        "config": ep["config"],
        "episode_id": ep["episode_id"],
        "policy_seed": ep.get("policy_seed"),
        "max_steps": plan["max_steps"],
        "actions": ep["actions"],
        "divergence": ep["divergence"],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    return path


def replay_record(record: dict, actions: Optional[list[dict]] = None) -> dict:
    """Re-run one saved reproducer ALONE, forcing its exact action list."""
    js = JsRuntime()
    py = PyRuntime()
    try:
        spec = dict(record["config"])
        spec["episode_id"] = record["episode_id"]
        return run_episode(js, py, spec, None, record["max_steps"],
                           forced_actions=actions if actions is not None else record["actions"])
    finally:
        js.close()
        py.close()


def replay_disposition(record: dict, episode: dict) -> str:
    """Classify a replay result without hiding its observed divergence."""
    if episode.get("divergence") is None:
        return "clean"

    if record.get("episode_id") not in ACCEPTED_HARNESS_BOUNDARY_EPISODES:
        return "unexpected-divergence"

    divergence = episode["divergence"]
    action = divergence.get("action", {})
    paths = {diff.get("path") for diff in divergence.get("diffs", [])}
    classification = record.get("classification", {})

    js_checkpoint = (divergence.get("js") or {}).get("checkpoint") or {}
    map_nodes = (js_checkpoint.get("map") or {}).get("nodes") or []
    has_giratina_wild_boss = any(
        node.get("id") == "n1_1"
        and node.get("wild_boss") is True
        and any(member.get("id") == "giratina-origin"
                for member in node.get("boss_team", []))
        for node in map_nodes
    )

    expected = (
        record.get("kind") == "harness-boundary-divergence"
        and action == {"kind": "visit_node", "node_id": "n1_1"}
        and {"battles[len]", "checkpoint.rng.draws"} <= paths
        and has_giratina_wild_boss
        and classification.get("verdict")
        == "NOT a port defect -- offline-harness boundary"
        and "network disabled" in classification.get("js_runtime_evidence", "")
    )
    return "accepted-harness-boundary" if expected else "unexpected-divergence"


def replay_records(paths: list[str], ledger: CoverageLedger) -> dict:
    """Replay saved reproducers with their OWN action lists and account for
    what the replays actually observe.

    M7-F-F. `replay` re-runs one record and reports whether it still
    diverges, then throws away everything the replay observed. That was an
    accounting gap rather than a safeguard: a checked-in reproducer is a
    deterministic plan -- a pinned `(seed, mode)` plus an ordered action list
    and a `max_steps` bound -- and replaying it drives both real runtimes
    through `run_episode`, the same lockstep loop every other result file is
    produced by. Its completed steps are observed, agreed evidence, and there
    was no way to merge them into a coverage number.

    Nothing about HOW coverage is credited changes. `run_episode` calls
    `observe_coverage` itself, per step, only after that step's legal sets and
    state projections have already compared equal; a record that diverges
    contributes exactly the steps it completed BEFORE diverging and nothing
    else, and a record whose action list stops short of a target earns
    nothing for it. The record file is never itself evidence -- it only says
    which episode to run.

    Deterministic: records are replayed in sorted path order, each episode
    stays a pure function of its own `(seed, mode)` plus its forced actions,
    and no clock or batch position enters any episode digest.
    """
    started = time.time()
    js = JsRuntime()
    py = PyRuntime()
    results: list[dict] = []
    records: list[dict] = []
    try:
        for path in sorted(paths):
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            spec = dict(record["config"])
            spec["episode_id"] = record["episode_id"]
            ep = run_episode(js, py, spec, None, record["max_steps"],
                             ledger=ledger, forced_actions=record["actions"])
            results.append(ep)
            records.append({
                "record": os.path.basename(path),
                "episode_id": record["episode_id"],
                "actions": len(record["actions"]),
                "reproduced": ep["divergence"] is not None,
                "disposition": replay_disposition(record, ep),
                "episode_digest": ep["episode_digest"],
                "outcome": ep["outcome"],
            })
    finally:
        js.close()
        py.close()

    summary = summarize(results)
    summary["accepted_harness_boundary"] = sum(
        r["disposition"] == "accepted-harness-boundary" for r in records)
    summary["unexpected_divergence"] = sum(
        r["disposition"] == "unexpected-divergence" for r in records)

    return {
        "sweep_version": SWEEP_VERSION,
        "replay_set": records,
        # The set's own identity, so a merged coverage claim names exactly
        # which reproducers produced it.
        "plan_digest": digest([r["record"] for r in records]),
        "protected_hashes": protected_hashes(),
        "wall_clock_s": round(time.time() - started, 1),
        "episodes": results,
        "coverage": ledger.report(),
        "summary": summary,
    }


def divergence_signature(div: dict) -> list:
    """A reduction-stable identity for one divergence: its kind plus, for a
    state divergence, the sorted set of diff paths with array indices
    normalized away (so the same defect at a different team slot or battle
    index is still the same finding)."""
    if div is None:
        return []
    kind = div.get("kind")
    if kind != "state":
        return [kind]
    paths = {re.sub(r"\[\d+\]", "[]", d["path"]) for d in div.get("diffs", [])}
    return [kind] + sorted(paths)


def minimize(record: dict) -> dict:
    """Shrink to the EARLIEST divergent prefix, then drop what is droppable.

    Two passes, both re-running the real cross-runtime episode rather than
    reasoning about it:
      1. binary search the shortest action PREFIX that still diverges;
      2. greedily delete individual earlier actions, keeping a deletion only
         when the shortened list still diverges AND every retained action is
         still legal at its new position.
    """
    full = record["actions"]
    target = divergence_signature(record["divergence"])

    def diverges(actions: list[dict]) -> bool:
        """Reproduces THE SAME divergence -- not merely *a* divergence.

        Requiring only `divergence is not None` is unsound and was caught
        doing exactly the wrong thing: dropping every action but the last left
        an action list whose first entry is illegal at step 0, the replay
        reported `replay_illegal`, and the minimizer happily accepted it as a
        1-action "reproducer" of an item-equip-cancel finding. A reduction is
        only valid when the SIGNATURE (kind plus, for a state divergence, the
        index-normalized set of diff paths) is unchanged.
        """
        ep = replay_record(record, actions)
        return ep["divergence"] is not None and divergence_signature(ep["divergence"]) == target

    if not diverges(full):
        return {"minimized": False, "reason": "the saved action list did not reproduce",
                "target_signature": target}

    lo, hi = 0, len(full)
    while lo < hi:
        mid = (lo + hi) // 2
        if diverges(full[:mid]):
            hi = mid
        else:
            lo = mid + 1
    prefix = full[:lo]

    reduced = list(prefix)
    i = 0
    while i < len(reduced) - 1:
        candidate = reduced[:i] + reduced[i + 1:]
        if diverges(candidate):
            reduced = candidate
        else:
            i += 1

    return {"minimized": True, "original_length": len(full),
            "prefix_length": len(prefix), "reduced_length": len(reduced),
            "signature": target, "actions": reduced}



# ===========================================================================
# External evidence sources
# ===========================================================================
# The target manifest says WHICH evidence source may earn each target. The
# sweep earns the `sweep` ones; these two functions earn the other two by
# RUNNING the real gates and reading their real output -- never by asserting
# that a gate "would" pass.


def route_corpus_evidence() -> dict:
    """Credit `route.<tag>` targets from the real frozen 29-scenario gate.

    Runs `compare.py --all --json` and reads the coverage the harness itself
    derived, independently, on BOTH runtimes. A tag is credited only if that
    run exits 0 (strict parity plus complete coverage on both sides).
    """
    cmd = [sys.executable, os.path.join(_HERE, "compare.py"), "--all", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=os.path.dirname(_HERE), encoding="utf-8")
    out: dict = {"command": " ".join(cmd), "returncode": proc.returncode,
                 "tags": [], "scenarios": None}
    if proc.returncode != 0:
        out["error"] = (proc.stderr or proc.stdout)[-3000:]
        return out
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out["error"] = "compare.py --json did not emit JSON"
        return out
    out["scenarios"] = len(report.get("scenarios", []))
    # `REQUIRED_TAGS` is the canonical list and `compare.py --all` hard-fails
    # unless every one of them is earned on both runtimes, so an exit-0 run IS
    # the evidence that each required tag was earned.
    out["tags"] = list(route_coverage.REQUIRED_TAGS)
    return out


def battle_oracle_evidence() -> dict:
    """Credit `battle.*` targets whose manifest entry names a battle-oracle
    fixture, from a real `tools/battle-oracle/compare.py --all` run."""
    root = os.path.dirname(_HERE)
    cmd = [sys.executable, os.path.join(root, "tools", "battle-oracle", "compare.py"), "--all"]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root, encoding="utf-8")
    out: dict = {"command": " ".join(cmd), "returncode": proc.returncode,
                 "fixtures": [], "stdout_tail": (proc.stdout or "")[-1500:]}
    if proc.returncode != 0:
        out["error"] = (proc.stderr or proc.stdout)[-3000:]
        return out
    fixdir = os.path.join(root, "tools", "battle-oracle", "fixtures")
    out["fixtures"] = sorted(f for f in os.listdir(fixdir) if f.endswith(".json"))
    return out


def apply_external_evidence(ledger: CoverageLedger) -> dict:
    """Run both external gates and credit whatever they really earned."""
    report: dict = {}
    route = route_corpus_evidence()
    report["route_corpus"] = route
    if route["returncode"] == 0:
        for tag in route["tags"]:
            tid = f"route.{tag}"
            if tid in ledger.by_id:
                ledger.hit(tid, "route-corpus", "compare.py --all")

    battle = battle_oracle_evidence()
    report["battle_oracle"] = battle
    if battle["returncode"] == 0:
        present = set(battle["fixtures"])
        for t in ledger.targets["targets"]:
            if t["evidence"] != "battle-oracle":
                continue
            if t.get("fixture") in present:
                ledger.hit(t["id"], "battle-oracle", "tools/battle-oracle/compare.py --all")
            else:
                report.setdefault("missing_fixtures", []).append(
                    {"target": t["id"], "fixture": t.get("fixture")})
    return report

# ===========================================================================
# CLI
# ===========================================================================


def _write(path: Optional[str], payload: dict) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {path}")
    else:
        print(text)


# ===========================================================================
# `search`: bounded, backtracking, goal-directed route search
# ===========================================================================
#
# `hunt` spends its budget on whole random episodes and keeps whichever ones
# happen to touch a wanted target. That works while targets are common and
# stops working once the remainder is deep and conjunctive -- the distortion
# legendary rewards need a gen4 run to reach its SECOND distortion world and
# then take one specific node, which a per-episode random policy essentially
# never assembles by chance.
#
# `search` explores the action tree instead: it steps the Python engine
# forward, and on a dead end BACKTRACKS to an ancestor and tries a different
# action from there. Backtracking is exactly the operation the honest
# snapshotter above exists for; see that section's header for what goes
# wrong without it.
#
# Two properties this search deliberately does NOT have:
#
#   * it never touches the source runtime, so it is fast but PROVES NOTHING.
#     Its output is a CANDIDATE -- a `(seed, mode, actions)` plan, the same
#     shape a retained reproducer has. Nothing is credited until that plan is
#     replayed through `run_episode`, the real lockstep loop, where the JS and
#     Python runtimes are compared step by step and `observe_coverage` runs
#     only on steps that already agreed. `search --verify` does that replay
#     itself; without it the emitted candidates are inert.
#   * it does not model the source's own legality. It proposes only actions
#     the PORT reports legal, which is why a candidate can still turn out to
#     diverge (or to be illegal) on replay -- and when it does, that replay is
#     a finding, not a failure of the search.


def _node_of(state, node_id):
    mp = getattr(state, "map", None)
    if mp is None:
        return None
    return (mp.nodes or {}).get(node_id)


def _goal_reward(reward_id: str):
    """Goal: the run TAKES a submap reward node carrying `reward_id`.

    Matches `observe_coverage`'s own rule for `reward.<kind>` (see that
    function): credit comes from the node the `visit_node` action resolved,
    read off the map BEFORE the step. The predicate is evaluated on the same
    pre-step state for the same reason."""

    def goal(state, action) -> bool:
        if action.get("kind") != "visit_node":
            return False
        node = _node_of(state, action.get("node_id"))
        return node is not None and str(node.extra.get("reward")) == reward_id

    return goal


def _goal_team_size(n: int):
    """Goal: the run reaches a submap reward node while holding >= `n` team
    members -- the shape `reward.sacrifice` needs, since `_pick_sub_map_
    rewards` filters that entry out below `min_team = 2`."""

    def goal(state, action) -> bool:
        if action.get("kind") != "visit_node":
            return False
        node = _node_of(state, action.get("node_id"))
        return node is not None and node.type == "reward" and len(state.team or []) >= n

    return goal


GOALS = {"reward": _goal_reward, "team": lambda v: _goal_team_size(int(v))}


# ---------------------------------------------------------------------------
# Steering: exploration ORDER, and nothing else
# ---------------------------------------------------------------------------
#
# The unsteered DFS shuffles each frame's legal set and tries it in that
# order. That is fine while the goal is shallow and fatal once it is not:
# `reward.fossil` lives on a REWARD node inside an UNDERGROUND submap, and
# `map_gen` places UNDERGROUND only on gen4 LAYER 4 of maps 1/3/6
# (map_gen.py:465-495). Reaching it therefore means clearing a gym leader,
# advancing a map, walking four layers, entering the submap and beating its
# boss -- and a uniformly random walk through gen4 dies to a battle long
# before that. Measured: 4 runs x 4 000 expansions produced ZERO candidates.
#
# A priority function fixes the ORDER the DFS tries siblings in. What it
# deliberately does NOT do, and what keeps a steered candidate exactly as
# honest as an unsteered one:
#
#   * it cannot ADD an action. `frame["untried"]` is whatever
#     `py_legal_actions` returned; the priority only sorts that list, so the
#     tree being searched is byte-for-byte the tree the unsteered search
#     explores;
#   * it cannot SKIP an action. A low-priority sibling is tried last, not
#     dropped, so the search stays complete within its expansion budget;
#   * it credits nothing. `search` never touches the source runtime at all
#     (see this section's header); a candidate is still inert until
#     `--verify` replays it through `run_episode` and `observe_coverage`
#     reads the steps both runtimes already agreed on.
#
# The ladder itself is `hunt_policy`'s -- survival before ambition, for the
# reason recorded there -- restated over the PORT's own run state, which is
# what a Python-only search has to read.


def _reward_submap_kinds(reward_id: str) -> frozenset:
    """Which submap kind(s) can carry `reward_id`.

    Read off `SUBMAP_REWARDS` itself (`data.get_submap_rewards()[i].kinds`,
    bundle.deobfuscated.js:76303-76377) rather than guessed: "fossil" is
    `("underground",)`, the three distortion legendaries are
    `("distortion",)`, everything else is both. An id the table does not
    have steers toward both kinds, which is the same as not steering."""
    entry = data.get_submap_reward_by_id().get(reward_id)
    if entry is None:
        return frozenset({map_gen.UNDERGROUND, map_gen.DISTORTION})
    return frozenset(entry.kinds)


def _py_team_health(state) -> float:
    """`_team_health`'s rule (fraction of the team's max HP still standing)
    read off the PORT's own team rather than a compared checkpoint -- the
    search has no checkpoint, because it never runs the source."""
    team = list(getattr(state, "team", None) or [])
    total = sum(int(getattr(m, "max_hp", 0) or 0) for m in team)
    if not total:
        return 1.0
    return sum(int(getattr(m, "current_hp", 0) or 0) for m in team) / total


def _reward_priority(reward_id: str):
    """Exploration order for a `reward:<id>` search. Higher is tried first."""
    kinds = _reward_submap_kinds(reward_id)

    def priority(state, action) -> int:
        kind = action.get("kind")
        if kind == "visit_node":
            node = _node_of(state, action.get("node_id"))
            ntype = getattr(node, "type", None)
            if getattr(state, "in_sub_map", None):
                if ntype == map_gen.REWARD:
                    # The goal node itself; any OTHER reward node spends the
                    # submap's single reward pick on something else.
                    return 100 if str((node.extra or {}).get("reward")) == reward_id else 20
                if ntype == map_gen.BOSS:
                    # `generate_sub_map`'s bipartite topology gates every
                    # reward node behind a boss node, so this is not
                    # ambition -- it is the only way to reach layer 2.
                    return 90
                if ntype == map_gen.SUBEXIT:
                    return 0
                return 30
            if ntype in kinds:
                return 85
            if ntype == map_gen.POKECENTER:
                return 80 if _py_team_health(state) < 0.75 else 40
            if ntype == map_gen.TRAINER:
                return 60          # +2 levels on a win, against a wild's +1
            if ntype == map_gen.BATTLE:
                return 55
            if ntype == map_gen.CATCH:
                # Above TRAINER while the roster is short. Measured, not
                # taste: over 80 greedy gen4 rollouts, ranking CATCH below
                # TRAINER reached map 1 eighteen times and ranking it above
                # reached map 1 twenty-seven times. More bodies is more
                # budget between the run and a wipe.
                return 62 if len(getattr(state, "team", None) or []) < 4 else 35
            if ntype in (map_gen.LEGENDARY, map_gen.MAGMA, map_gen.AQUA,
                         map_gen.SILVER):
                return 10          # over-levelled fights this run cannot afford
            return 30
        if kind == "advance_map":
            return 70
        if kind == "select_option":
            if action.get("cancel"):
                return 5
            return 65 if action.get("index") is not None else 35
        if kind == "reorder_team":
            return 1
        return 25

    return priority


def _reward_prune(reward_id: str):
    """A subtree the goal is not in: the run is standing inside a submap
    whose generated reward nodes do not carry `reward_id`.

    `generate_sub_map` bakes a submap's reward ids at ENTRY time
    (`_pick_sub_map_rewards`, map_gen.py:1005-1024) and nothing afterwards
    rewrites them, so a submap that came up without the wanted id will never
    produce it however the run walks inside. Backtracking ABOVE the entry --
    which is what returning True does -- re-rolls the submap from a different
    RNG position, and re-rolling is the only way to get a different draw.
    Measured: `fossil` is in an underground submap's two random reward slots
    roughly 27% of the time (8-id pool, 2 kept), so without this the search
    sinks its whole budget into the first submap it happens to enter.

    This makes the search deliberately INCOMPLETE: a run really could leave a
    fossil-less underground and reach a LATER one (map_gen places UNDERGROUND
    on gen4 maps 1, 3 and 6), and this gives that route up. That is a budget
    trade a PROPOSER is allowed to make -- `search` credits nothing on its
    own, and every candidate is still replayed through the real lockstep loop
    before anything is earned."""

    def prune(state) -> bool:
        if not getattr(state, "in_sub_map", None):
            return False
        mp = getattr(state, "map", None)
        nodes = list((getattr(mp, "nodes", None) or {}).values())
        return not any(str((n.extra or {}).get("reward")) == reward_id
                       for n in nodes)

    return prune


SEARCH_PRIORITIES = {"reward": _reward_priority}
SEARCH_PRUNES = {"reward": _reward_prune}


def search_episode(seed: int, mode: dict, goal, *, max_steps: int,
                   max_expansions: int, rnd: random.Random,
                   priority=None, prune=None) -> Optional[dict]:
    """Randomized bounded DFS with backtracking over one `(seed, mode)` run.

    Returns a candidate record (never credited on its own -- see the section
    header) or None when the budget is exhausted.

    Every restore goes through `engine_restore`, so a sibling action is always
    tried from the byte-identical engine state and RNG position its ancestor
    had. That is what makes an emitted action list replay to the same outcome
    in a fresh process.

    `priority`, when given, only ORDERS each frame's already-enumerated legal
    set -- it never adds an action, never drops one, and credits nothing.
    `prune`, when given, declares a reached state's subtree not worth the
    budget and is handled exactly like `game_over`: the frame is not pushed
    and the search backtracks. See the "Steering" header above.
    """
    eng = engine.Engine()
    eng.reset(nuzlocke_mode=bool(mode.get("nuzlocke")), gen2_mode=bool(mode.get("gen2")),
              gen3_mode=bool(mode.get("gen3")), gen4_mode=bool(mode.get("gen4")),
              seed=int(seed))

    # Each frame: the snapshot to return to, and the actions still untried
    # from it. `actions` mirrors the stack depth, so popping a frame pops the
    # action that produced it.
    stack: list[dict] = [{"snap": engine_snapshot(eng), "untried": None}]
    actions: list[dict] = []
    expansions = 0

    while stack and expansions < max_expansions:
        frame = stack[-1]
        if frame["untried"] is None:
            engine_restore(eng, frame["snap"])
            try:
                legal = py_legal_actions(eng.state)
            except Exception:
                legal = []
            rnd.shuffle(legal)
            if priority is not None:
                # `pop()` takes from the END, so sorting ASCENDING puts the
                # most promising action first in the try order. `sort` is
                # stable, so the shuffle above survives as the tie-break
                # WITHIN a priority class and the search stays randomized and
                # `rnd`-deterministic.
                frame_state = eng.state
                legal.sort(key=lambda a: priority(frame_state, a))
            frame["untried"] = legal
        if not frame["untried"] or len(actions) >= max_steps:
            stack.pop()
            if actions:
                actions.pop()
            continue

        engine_restore(eng, frame["snap"])
        action = frame["untried"].pop()
        bare = {k: v for k, v in action.items() if k != PROV_KEY}
        expansions += 1

        if goal(eng.state, bare):
            return {"sweep_version": SWEEP_VERSION,
                    "episode_id": "search_%s_%d" % (bucket_name(mode), seed),
                    "config": {"seed": int(seed), "mode": dict(mode)},
                    "max_steps": len(actions) + 16,
                    "actions": actions + [bare],
                    "search": {"expansions": expansions, "depth": len(actions) + 1}}

        try:
            eng.step(py_reorder_action(action, len(eng.state.team))
                     if action["kind"] == "reorder_team" else py_action_to_engine(action))
        except Exception:
            continue
        if getattr(eng.state, "game_over", False):
            continue
        if prune is not None and prune(eng.state):
            continue

        actions.append(bare)
        stack.append({"snap": engine_snapshot(eng), "untried": None})

    return None


def run_search(goal_spec: str, buckets: list[str], *, episodes: int, base_seed: int,
               max_steps: int, max_expansions: int, verify: bool,
               targets: dict, steer: bool = True) -> dict:
    """Search several `(seed, mode)` runs for `goal_spec`, then -- with
    `verify` -- replay every candidate through the REAL lockstep loop and
    report only what that replay observed."""
    kind, _, value = goal_spec.partition(":")
    if kind not in GOALS:
        raise SystemExit("unknown goal %r; expected one of %s"
                         % (goal_spec, sorted(k + ":<value>" for k in GOALS)))
    goal = GOALS[kind](value)
    maker = SEARCH_PRIORITIES.get(kind) if steer else None
    priority = maker(value) if maker is not None else None
    pruner = SEARCH_PRUNES.get(kind) if steer else None
    prune = pruner(value) if pruner is not None else None

    by_name = {bucket_name(m): m for m in MODE_BUCKETS}
    for b in buckets:
        if b not in by_name:
            raise SystemExit("unknown mode bucket %r; expected %s" % (b, sorted(by_name)))

    started = time.time()
    rnd = random.Random(base_seed)
    candidates: list[dict] = []
    for i in range(episodes):
        bucket = buckets[i % len(buckets)]
        seed = rnd.getrandbits(32)
        found = search_episode(seed, by_name[bucket], goal, max_steps=max_steps,
                               max_expansions=max_expansions,
                               rnd=random.Random(rnd.getrandbits(32)),
                               priority=priority, prune=prune)
        if found is not None:
            found["goal"] = goal_spec
            candidates.append(found)
            print("  candidate %s depth=%d expansions=%d"
                  % (found["episode_id"], found["search"]["depth"],
                     found["search"]["expansions"]))

    out = {"sweep_version": SWEEP_VERSION, "goal": goal_spec, "buckets": buckets,
           "base_seed": base_seed, "searched": episodes,
           "steered": priority is not None,
           "max_steps": max_steps, "max_expansions": max_expansions,
           "candidates": candidates, "verified": None,
           "wall_clock_s": round(time.time() - started, 1)}

    if verify and candidates:
        # The only step that can credit anything: both real runtimes, in
        # lockstep, through the same `run_episode` every other result file
        # comes from.
        ledger = CoverageLedger(targets)
        js, py = JsRuntime(), PyRuntime()
        try:
            eps = []
            for rec in candidates:
                spec = dict(rec["config"])
                spec["episode_id"] = rec["episode_id"]
                eps.append(run_episode(js, py, spec, None, rec["max_steps"],
                                       ledger=ledger, forced_actions=rec["actions"]))
        finally:
            js.close()
            py.close()
        out["verified"] = {
            "episodes": eps,
            "coverage": ledger.report(),
            "summary": summarize(eps),
            "diverged": [e["episode_id"] for e in eps if e["divergence"] is not None],
        }
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sweep.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate-targets", help="check the coverage target manifest")

    p = sub.add_parser("plan", help="emit a deterministic episode plan")
    p.add_argument("--episodes", type=int, default=200)
    p.add_argument("--base-seed", type=int, default=20260823)
    p.add_argument("--max-steps", type=int, default=120)
    p.add_argument("--out")

    h = sub.add_parser("hunt", help="goal-directed bounded search for the "
                                    "still-missing required coverage targets")
    h.add_argument("--missing", nargs="*",
                   help="target ids to hunt (default: everything the merged "
                        "--from results leave unearned)")
    h.add_argument("--from", dest="from_results", nargs="*", default=[],
                   help="existing result files whose coverage seeds the ledger")
    h.add_argument("--external", action="store_true",
                   help="also run the frozen route gate and the battle oracle")
    h.add_argument("--episodes", type=int, default=400)
    h.add_argument("--base-seed", type=int, default=20260823)
    h.add_argument("--max-steps", type=int, default=400)
    h.add_argument("--out")

    r = sub.add_parser("run", help="execute a plan and compare both runtimes")
    r.add_argument("--plan")
    r.add_argument("--corpus", action="store_true", help="use the pinned route corpus")
    r.add_argument("--guided", action="store_true", help="coverage-guided scheduler")
    r.add_argument("--max-steps", type=int, default=120)
    r.add_argument("--order", choices=("manifest", "reverse", "sorted"), default="manifest")
    r.add_argument("--out")

    se = sub.add_parser("search", help="bounded backtracking search for a goal, "
                                       "then verify candidates in lockstep")
    se.add_argument("--goal", required=True,
                    help="reward:<id> or team:<n> -- see GOALS")
    se.add_argument("--buckets", nargs="+", default=["story_gen4"],
                    help="mode buckets to search")
    se.add_argument("--episodes", type=int, default=40)
    se.add_argument("--base-seed", type=int, default=0)
    se.add_argument("--max-steps", type=int, default=140)
    se.add_argument("--max-expansions", type=int, default=20000)
    se.add_argument("--verify", action="store_true",
                    help="replay every candidate through the real lockstep loop; "
                         "without this the candidates are inert and credit nothing")
    se.add_argument("--no-steer", action="store_true",
                    help="explore each frame's legal set in shuffled order instead "
                         "of the goal's priority order; ordering only -- steering "
                         "adds no action and credits nothing either way")
    se.add_argument("--out")
    rp = sub.add_parser("replay", help="re-run one saved reproducer alone")
    rp.add_argument("--record", required=True)
    rp.add_argument("--minimize", action="store_true")
    rp.add_argument("--out")

    rs = sub.add_parser("replay-set", help="replay saved reproducers and "
                                           "account for what they observe")
    rs.add_argument("--records", nargs="+", required=True)
    rs.add_argument("--out")

    cv = sub.add_parser("coverage", help="merge coverage across result files")
    cv.add_argument("results", nargs="*")
    cv.add_argument("--external", action="store_true",
                    help="also RUN the frozen route gate and the battle oracle and "
                         "credit the targets they really earn")
    cv.add_argument("--out")

    args = ap.parse_args(argv[1:])
    targets = load_targets()

    if args.cmd == "validate-targets":
        problems = validate_targets(targets)
        if problems:
            for pb in problems:
                print("FAIL " + pb)
            return 1
        n = len(targets["targets"])
        req = sum(1 for t in targets["targets"] if t["evidence"] != "excluded")
        print(f"target manifest v{targets['version']}: OK -- {n} targets, {req} required, "
              f"{n - req} excluded")
        return 0

    if args.cmd == "plan":
        _write(args.out, make_plan(args.episodes, args.base_seed, args.max_steps))
        return 0

    if args.cmd == "hunt":
        ledger = CoverageLedger(targets)
        if args.external:
            apply_external_evidence(ledger)
        for path in args.from_results:
            with open(path, encoding="utf-8") as fh:
                res = json.load(fh)
            for tid, rec in res["coverage"]["earned"].items():
                ledger.earned.setdefault(tid, {"source": rec["source"], "count": 0,
                                               "first": rec["first"]})
                ledger.earned[tid]["count"] += rec["count"]
        missing = args.missing if args.missing else ledger.missing()
        if not missing:
            print("nothing missing; no hunt needed")
            return 0
        print(f"hunting {len(missing)} target(s): {sorted(missing)}")
        plan = hunt_plan(missing, args.episodes, args.base_seed, args.max_steps)
        result = run_plan(plan, ledger, hunt=True, stop_when_covered=True)
        result["hunt_for"] = sorted(missing)
        result["still_missing"] = sorted(t for t in missing if t not in ledger.earned)
        s_ = result["summary"]
        print(f"\n{s_['episodes']} episodes, {s_['compared_action_steps']} compared "
              f"action steps, {result['wall_clock_s']}s")
        print(f"diverged: {s_['diverged']}")
        print(f"still missing after the hunt: {result['still_missing']}")
        _write(args.out, result)
        if s_["diverged"]:
            return 1
        return 1 if result["still_missing"] else 0

    if args.cmd == "run":
        if args.corpus:
            plan = corpus_plan(args.max_steps)
        elif args.plan:
            with open(args.plan, encoding="utf-8") as fh:
                plan = json.load(fh)
        else:
            print("run needs --plan or --corpus", file=sys.stderr)
            return 1
        if args.order == "reverse":
            plan = dict(plan, episodes=list(reversed(plan["episodes"])))
        elif args.order == "sorted":
            plan = dict(plan, episodes=sorted(plan["episodes"],
                                              key=lambda e: digest(e)))
        ledger = CoverageLedger(targets)
        result = run_plan(plan, ledger, guided=args.guided)
        result["order"] = args.order
        s = result["summary"]
        print(f"\n{s['episodes']} episodes, {s['compared_action_steps']} compared action steps, "
              f"{result['wall_clock_s']}s")
        print(f"diverged: {s['diverged']}")
        print(f"coverage: {len(result['coverage']['earned'])}/{result['coverage']['required']} "
              f"required earned; missing={result['coverage']['missing']}")
        _write(args.out, result)
        return 1 if s["diverged"] else 0

    if args.cmd == "search":
        result = run_search(args.goal, list(args.buckets), episodes=args.episodes,
                            base_seed=args.base_seed, max_steps=args.max_steps,
                            max_expansions=args.max_expansions, verify=args.verify,
                            targets=targets, steer=not args.no_steer)
        print(f"\n{result['searched']} run(s) searched, "
              f"{len(result['candidates'])} candidate(s), {result['wall_clock_s']}s")
        v = result["verified"]
        if v is None:
            print("NOT VERIFIED: candidates credit nothing until replayed "
                  "(pass --verify)")
        else:
            print(f"verified in lockstep: {v['summary']['compared_action_steps']} "
                  f"compared action steps, diverged={v['diverged']}")
            print(f"coverage observed by the verified replays: "
                  f"{len(v['coverage']['earned'])}/{v['coverage']['required']}")
        _write(args.out, result)
        if v is not None and v["diverged"]:
            return 1
        return 0 if result["candidates"] else 1

    if args.cmd == "replay":
        with open(args.record, encoding="utf-8") as fh:
            record = json.load(fh)
        if args.minimize:
            out = minimize(record)
        else:
            ep = replay_record(record)
            out = {"reproduced": ep["divergence"] is not None,
                   "divergence": ep["divergence"],
                   "episode_digest": ep["episode_digest"]}
        _write(args.out, out)
        return 0

    if args.cmd == "replay-set":
        ledger = CoverageLedger(targets)
        result = replay_records(args.records, ledger)
        diverged = sum(1 for r in result["replay_set"] if r["reproduced"])
        for r in result["replay_set"]:
            status = ("ACCEPTED BOUNDARY" if
                      r["disposition"] == "accepted-harness-boundary"
                      else "REPRODUCED" if r["reproduced"] else "clean")
            print(f"  {r['record']:<48} {r['outcome']:<10} "
                  f"{status}")
        print(f"\n{len(result['replay_set'])} record(s), "
              f"{result['summary']['compared_action_steps']} compared action steps, "
              f"{result['wall_clock_s']}s")
        print(f"still reproducing: {diverged} "
              f"(accepted harness boundary: "
              f"{result['summary']['accepted_harness_boundary']}; "
              f"unexpected: {result['summary']['unexpected_divergence']})")
        print(f"coverage observed by these replays: "
              f"{len(result['coverage']['earned'])}/{result['coverage']['required']}")
        _write(args.out, result)
        return 1 if result["summary"]["unexpected_divergence"] else 0

    if args.cmd == "coverage":
        ledger = CoverageLedger(targets)
        external = None
        if args.external:
            external = apply_external_evidence(ledger)
        for path in args.results:
            with open(path, encoding="utf-8") as fh:
                res = json.load(fh)
            for tid, rec in res["coverage"]["earned"].items():
                ledger.earned.setdefault(tid, {"source": rec["source"], "count": 0,
                                               "first": rec["first"]})
                ledger.earned[tid]["count"] += rec["count"]
        report = ledger.report()
        if external is not None:
            report["external_evidence"] = external
        print(f"{len(report['earned'])}/{report['required']} required targets earned; "
              f"{len(report['missing'])} missing")
        _write(args.out, report)
        return 1 if report["missing"] else 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
