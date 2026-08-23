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

from pokelike import data, engine  # noqa: E402

# Bumped whenever the action vocabulary, the compared projection, or the
# episode-record shape changes. Recorded in every result file, so a stale
# record can never be silently compared against a newer run.
SWEEP_VERSION = 1

TARGETS_PATH = os.path.join(_HERE, "sweep-targets.json")
FINDINGS_DIR = os.path.join(_HERE, "findings")

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
        # `legal_actions` reports `{"team_size": n}`, i.e. "any permutation of
        # n". The SOURCE's only reorder affordance is the team-bar drag
        # handler (bundle.deobfuscated.js:64798-64806), whose entire mutation
        # is `[team[a], team[b]] = [team[b], team[a]]` -- a transposition. The
        # canonical compared domain is therefore the transpositions, which is
        # exactly the source's atomic action and a strict subset of what
        # `ReorderTeam` accepts. The breadth difference is REPORTED as finding
        # F1, not silently intersected away; see SWEEP.md.
        if "reorder_team" in la:
            n = int(la["reorder_team"]["team_size"])
            for i in range(n):
                for j in range(i + 1, n):
                    out.append({"kind": "reorder_team", "i": i, "j": j,
                                PROV_KEY: "legal_actions.reorder_team (transposition)"})

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
    order = list(range(team_size))
    order[a["i"]], order[a["j"]] = order[a["j"]], order[a["i"]]
    return engine.ReorderTeam(order=tuple(order))


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
        inner = engine._run_battle

        def stage_capturing(state, enemy_team):
            result = inner(state, enemy_team)
            self.stages.append({
                "player": [_stages_of(m) for m in result.player_team],
                "enemy": [_stages_of(m) for m in result.enemy_team],
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


def project(side: dict) -> dict:
    cp = {k: v for k, v in side["checkpoint"].items() if k not in EXCLUDED_CHECKPOINT_FIELDS}
    return {
        "checkpoint": cp,
        "battles": side["battles"],
        # M7 enrichment over the frozen schema -- see SWEEP.md.
        "battle_stages": side["battle_stages"],
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
    hit("episode." + bucket_name(episode["config"]["mode"]))
    if action["kind"] == "choose_starter":
        hit(f"starter.gen{generation_of(episode['config']['mode'])}."
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
            if node.get("sub_kind"):
                hit("submap." + str(node["sub_kind"]))
            if node.get("reward", {}) and node["reward"].get("kind"):
                hit("reward." + str(node["reward"]["kind"]))

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
             verbose: bool = True) -> dict:
    started = time.time()
    js = JsRuntime()
    py = PyRuntime()
    results: list[dict] = []
    try:
        for spec in plan["episodes"]:
            rng = random.Random(spec["policy_seed"])
            policy = guided_policy(rng, ledger) if guided else random_policy(rng)
            ep = run_episode(js, py, spec, policy, plan["max_steps"], ledger=ledger)
            results.append(ep)
            if verbose:
                flag = "DIVERGED" if ep["divergence"] else ep["outcome"]
                print(f"  {spec['episode_id']:<28} steps={ep['steps_taken']:>4} {flag}",
                      flush=True)
            if ep["divergence"]:
                save_finding(ep, plan)
    finally:
        js.close()
        py.close()

    return {
        "sweep_version": SWEEP_VERSION,
        "plan_digest": plan["plan_digest"],
        "guided": guided,
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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sweep.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate-targets", help="check the coverage target manifest")

    p = sub.add_parser("plan", help="emit a deterministic episode plan")
    p.add_argument("--episodes", type=int, default=200)
    p.add_argument("--base-seed", type=int, default=20260823)
    p.add_argument("--max-steps", type=int, default=120)
    p.add_argument("--out")

    r = sub.add_parser("run", help="execute a plan and compare both runtimes")
    r.add_argument("--plan")
    r.add_argument("--corpus", action="store_true", help="use the pinned route corpus")
    r.add_argument("--guided", action="store_true", help="coverage-guided scheduler")
    r.add_argument("--max-steps", type=int, default=120)
    r.add_argument("--order", choices=("manifest", "reverse", "sorted"), default="manifest")
    r.add_argument("--out")

    rp = sub.add_parser("replay", help="re-run one saved reproducer alone")
    rp.add_argument("--record", required=True)
    rp.add_argument("--minimize", action="store_true")
    rp.add_argument("--out")

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
