"""The Python side of the M3 short-full-run route oracle.

Runs the same deterministic Story/Nuzlocke route as ``run-scenario.js``
through the real ``pokelike.engine`` and emits the same versioned checkpoint
stream on stdout::

    python route-oracle/run_scenario.py route-oracle/scenarios/<name>.json

Nothing here reimplements engine behavior. The only things this module owns
are (a) an RNG-draw counter wrapped around the engine's own private
``Mulberry32`` stream, (b) a ``_run_battle`` wrapper that records each
``BattleResult`` as it is produced, and (c) the normalization that turns
``RunState`` into a checkpoint. See ``SCHEMA.md`` for the schema contract and
for every field that is deliberately excluded.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pokelike import battle_loop, engine, map_gen, rng  # noqa: E402

SCHEMA_VERSION = 1

# `RunState.phase` -> the source screen id `showScreen(...)` would be showing.
# This is what makes the two streams comparable at all: the JS side has no
# `phase` and the Python side has no DOM, but both sides agree on which
# suspended-continuation the player is sitting on. Any phase not listed here
# has no route-matrix counterpart and is reported verbatim so a surprise is
# visible rather than silently mapped onto something plausible.
_PHASE_TO_SCREEN = {
    engine.Phase.CHOOSE_STARTER: "starter-screen",
    engine.Phase.ON_MAP: "map-screen",
    engine.Phase.SWAP_CHOICE: "swap-screen",
    engine.Phase.CATCH_CHOICE: "catch-screen",
    # `doItemNode` opens with `showScreen("item-screen")`
    # (bundle.deobfuscated.js:79261), so this phase has an exact source
    # counterpart. The remaining phases deliberately stay unmapped: their
    # source counterparts are OVERLAYS/MODALS that never call `showScreen`
    # (`openItemEquipModal`, `showBranchingChoice`'s `#eevee-choice-overlay`
    # at 70560, `showTeamPickerModal`'s `#submap-pick-modal` at 76845), or
    # screens no route in the matrix reaches. Leaving them unmapped is what
    # makes an unexpected one show up as `<unmapped:...>` instead of being
    # silently folded onto a plausible-looking screen id.
    engine.Phase.ITEM_CHOICE: "item-screen",
    engine.Phase.NEXT_MAP_READY: "badge-screen",
    engine.Phase.GAME_OVER: "gameover-screen",
    engine.Phase.VICTORY: "win-screen",
}


class CountingStream:
    """Delegating wrapper around the engine's private ``Mulberry32`` that
    counts draws. Mirrors the JS side's ``rng`` wrapper exactly: it counts,
    it never substitutes values, and it exposes the same ``state``/``seed``
    surface ``rng.py`` requires."""

    __slots__ = ("_inner", "draws")

    def __init__(self, inner: rng.Mulberry32) -> None:
        self._inner = inner
        self.draws = 0

    def __call__(self) -> float:
        self.draws += 1
        return self._inner()

    @property
    def state(self) -> int:
        return self._inner.state

    def seed(self, value: int) -> None:
        self._inner.seed(value)


def _num(value: Any) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _normalize_stat_buffs(buffs: Optional[dict]) -> dict:
    buffs = buffs or {}
    return {k: buffs[k] for k in ("hp", "atk", "def", "speed", "special", "spdef") if buffs.get(k)}


def _normalize_mon(mon, slot: Optional[int] = None) -> Optional[dict]:
    if mon is None:
        return None
    base = getattr(mon, "base_stats", None)
    held = getattr(mon, "held_item", None)
    out = {
        "species_id": _num(getattr(mon, "species_id", None)),
        # Alternate-form identity. The port carries a distinct form only for
        # Origin Giratina today (species 487, distinct name/stats); anything
        # without one reports null on both sides.
        "form_id": getattr(mon, "form_id", None),
        "name": getattr(mon, "name", None) or None,
        "level": _num(getattr(mon, "level", None)),
        "max_hp": _num(getattr(mon, "max_hp", None)),
        "current_hp": _num(getattr(mon, "current_hp", None)),
        "types": list(getattr(mon, "types", ()) or ()),
        "move_tier": _num(getattr(mon, "move_tier", None)),
        "held_item": getattr(held, "id", None) if held is not None else None,
        "is_shiny": bool(getattr(mon, "is_shiny", False)),
        "status": getattr(mon, "status", None) or None,
        "burned": bool(getattr(mon, "burned", False)),
        "paralyzed": bool(getattr(mon, "paralyzed", False)),
        "poison_stacks": _num(getattr(mon, "poison_stacks", 0)) or 0,
        "base_stats": {
            "hp": _num(getattr(base, "hp", None)),
            "atk": _num(getattr(base, "atk", None)),
            # `data.BaseStats` spells it `defense` (`def` is a Python keyword);
            # the source field is `baseStats.def`.
            "def": _num(getattr(base, "defense", None)),
            "speed": _num(getattr(base, "speed", None)),
            "special": _num(getattr(base, "special", None)),
            "spdef": _num(getattr(base, "spdef", None)),
        }
        if base is not None
        else None,
        "stat_buffs": _normalize_stat_buffs(getattr(mon, "stat_buffs", None)),
    }
    if slot is not None:
        out["slot"] = slot
    return out


def _normalize_node(node: map_gen.MapNode) -> dict:
    out = {
        "id": node.id,
        "type": node.type,
        "layer": _num(node.layer),
        "col": _num(node.col),
        "visited": bool(node.visited),
        "accessible": bool(node.accessible),
        "revealed": bool(node.revealed),
    }
    extra = node.extra or {}
    # Same key set the JS side lifts off its node objects, minus
    # `trainerSprite` (presentation-only, and the source itself deletes it
    # when it retypes a node into SILVER/MAGMA/AQUA/UNDERGROUND/DISTORTION).
    if "mapIndex" in extra or "map_index" in extra:
        out["map_index"] = _num(extra.get("mapIndex", extra.get("map_index")))
    if "legendarySpeciesId" in extra or "legendary_species_id" in extra:
        out["legendary_species_id"] = _num(
            extra.get("legendarySpeciesId", extra.get("legendary_species_id"))
        )
    for js_key, py_key, out_key in (
        ("subKind", "sub_kind", "sub_kind"),
        ("rewardKind", "reward_kind", "reward_kind"),
        ("kind", "kind", "kind"),
        ("trainerKey", "trainer_key", "trainer_key"),
    ):
        if js_key in extra or py_key in extra:
            out[out_key] = extra.get(js_key, extra.get(py_key))
    if "wildBoss" in extra or "wild_boss" in extra:
        out["wild_boss"] = bool(extra.get("wildBoss", extra.get("wild_boss")))
    if "bossTeam" in extra or "boss_team" in extra:
        team = extra.get("bossTeam", extra.get("boss_team")) or []
        # The source's entries are `{id, level}`; `map_gen` spells the id
        # `species_id` for table-backed rosters and `id` for the string form
        # ids (e.g. "giratina-origin"), so accept either.
        out["boss_team"] = [
            {"id": b.get("id", b.get("species_id")), "level": _num(b.get("level"))} for b in team
        ]
    reward = extra.get("reward")
    if isinstance(reward, dict):
        out["reward"] = {"kind": reward.get("kind")}
    return out


def _normalize_map(gmap: Optional[map_gen.GeneratedMap]) -> Optional[dict]:
    if gmap is None:
        return None
    return {
        "index": _num(gmap.map_index),
        "is_sub_map": gmap.is_sub_map or None,
        "nodes": [_normalize_node(gmap.nodes[nid]) for nid in sorted(gmap.nodes)],
        "edges": [[src, dst] for src, dst in gmap.edges],
    }


def _normalize_sub_map_return(ret: Optional[dict]) -> Optional[dict]:
    if not ret:
        return None
    parent = ret.get("map")
    return {
        "kind": ret.get("kind"),
        "map_index": _num(ret.get("map_index")),
        "node_id": ret.get("node_id"),
        "has_map": bool(parent),
        "map_node_count": len(parent.nodes) if parent is not None else None,
        # The COMPLETE saved parent map, normalized exactly the way the live
        # map is (`_normalize_map`), not a flags-only summary. See the matching
        # comment in driver.js: the old `map_flags` list carried only
        # id/visited/accessible, which was too weak to let `exact_parent_return`
        # tell an exact `advanceFromNode` restore apart from a map whose types,
        # extras, `revealed` flags, map index or edge order had changed.
        "map_topology": _normalize_map(parent),
    }


def _normalize_item(item) -> Optional[str]:
    if isinstance(item, str):
        return item
    return getattr(item, "id", None)


def _normalize_battle(result: battle_loop.BattleResult, draws: int) -> dict:
    return {
        "player_won": bool(result.player_won),
        "rounds": _num(result.rounds),
        "rng_draws": draws,
        "player_team": [_normalize_mon(m) for m in result.player_team],
        "enemy_team": [_normalize_mon(m) for m in result.enemy_team],
        "player_participants": sorted(result.player_participants),
        "status_events": [dict(e) for e in result.status_events],
        # Diagnostic only, never compared: the Python port has no full
        # per-turn battle event log to count. See SCHEMA.md.
        "__diagnostic_event_count": None,
    }


class Runner:
    def __init__(self, scenario: dict) -> None:
        self.sc = scenario
        self.seq = 0
        self.checkpoints: list[dict] = []
        self.battles: list[dict] = []
        self.notes: list[str] = []
        self.error: Optional[str] = None

        self.engine = engine.Engine()
        # Per-instance, no global state: the counter wraps this Engine's own
        # private Mulberry32 stream.
        self.counter = CountingStream(self.engine._rng_stream)
        self.engine._rng_stream = self.counter  # type: ignore[assignment]

    def _install_battle_recorder(self):
        """Record every BattleResult as the engine produces it, without
        changing what the engine does with it.

        Installed for the duration of `run()` ONLY, never in `__init__`:
        `engine._run_battle` is a module global, so patching it at
        construction time meant that building two Runners before stepping
        either one left the second one's wrapper stranded (the first one's
        restore put the original back and silently dropped it). Constructing
        a Runner therefore has no global side effect at all.
        """
        real_run_battle = engine._run_battle

        def counting_run_battle(state, enemy_team):
            before = self.counter.draws
            result = real_run_battle(state, enemy_team)
            self.battles.append(_normalize_battle(result, self.counter.draws - before))
            return result

        engine._run_battle = counting_run_battle  # type: ignore[assignment]
        return real_run_battle

    # -- checkpoint ------------------------------------------------------
    def checkpoint(self, kind: str, event: Optional[dict] = None) -> dict:
        st = self.engine.state
        assert st is not None
        pending = st.pending
        cp = {
            "schema_version": SCHEMA_VERSION,
            "scenario": self.sc["scenario"],
            "seq": self.seq,
            "kind": kind,
            "event": event or {},
            "mode": {
                "nuzlocke": bool(st.nuzlocke_mode),
                "gen2": bool(st.gen2_mode),
                "gen3": bool(st.gen3_mode),
                "gen4": bool(st.gen4_mode),
            },
            "seed": _num(self.sc["seed"]),
            "rng": {"state": self.counter.state, "draws": self.counter.draws},
            "screen": _PHASE_TO_SCREEN.get(st.phase, f"<unmapped:{st.phase.value}>"),
            "map": _normalize_map(st.map),
            "current_map": _num(st.current_map),
            "current_node": st.current_node_id,
            "in_sub_map": st.in_sub_map or None,
            "sub_map_return": _normalize_sub_map_return(st.sub_map_return),
            "counters": {
                "badges": _num(st.badges) or 0,
                "elite_index": _num(st.elite_index) or 0,
                "starter_species_id": _num(st.starter_species_id),
                "max_team_size": _num(st.max_team_size) or 0,
                "silver_beaten": _num(st.silver_beaten) or 0,
                "fought_admin": bool(st.fought_admin),
                "used_pokecenter": bool(st.used_pokecenter),
                "picked_up_item": bool(st.picked_up_item),
                "used_tm": bool(st.used_tm),
                "used_ball_catch": bool(st.used_ball_catch),
                "got_via_question": bool(st.got_via_question),
                "any_fainted": bool(getattr(st, "any_fainted", False)),
                "escaped_via_rope": bool(st.escaped_via_rope),
                "distortion_worlds_entered": _num(st.distortion_worlds_entered) or 0,
                "distortion_legendary_claimed": bool(st.distortion_legendary_claimed),
                "entered_sub_map": bool(st.entered_sub_map),
            },
            "team": [_normalize_mon(m, i) for i, m in enumerate(st.team)],
            "items": [_normalize_item(i) for i in st.items],
            "game_over": bool(st.game_over) or st.phase == engine.Phase.GAME_OVER,
            # Pending-choice shape. Compared as cardinality + option identity
            # only: the JS side's option list is DOM cards, so the comparable
            # invariant is "how many choices, of what identity", not the
            # renderer's own dict layout. See SCHEMA.md.
            "pending": (
                {
                    "phase": pending.phase.value,
                    "optional": bool(pending.optional),
                    "option_count": len(pending.options),
                }
                if pending is not None
                else None
            ),
        }
        self.checkpoints.append(cp)
        self.seq += 1
        return cp

    # -- route -----------------------------------------------------------
    def run(self) -> dict:
        sc = self.sc
        real_run_battle = self._install_battle_recorder()
        try:
            self.engine.reset(
                nuzlocke_mode=bool(sc["mode"]["nuzlocke"]),
                gen2_mode=bool(sc["mode"]["gen2"]),
                gen3_mode=bool(sc["mode"]["gen3"]),
                gen4_mode=bool(sc["mode"]["gen4"]),
                seed=int(sc["seed"]),
            )
            self.checkpoint("run_init", {})

            st = self.engine.state
            assert st is not None and st.pending is not None
            self.checkpoint("starter_offered", {"screen": _PHASE_TO_SCREEN[st.phase]})

            # Optional RNG alignment instrument -- see SCHEMA.md and the
            # matching block in driver.js. Symmetric, source-API-only
            # (`seed_rng` is the port of the source's own `seedRng`), applied
            # at the same route point on both sides, and never a repair.
            align = sc.get("align_rng_after_starter_offer")
            if align is not None:
                previous = rng.set_active_stream(self.counter)  # type: ignore[arg-type]
                try:
                    rng.seed_rng(int(align) & 0xFFFFFFFF)
                finally:
                    rng.set_active_stream(previous)
                self.checkpoint("rng_aligned", {"to": int(align) & 0xFFFFFFFF})

            starter_index = int(sc["starter_index"])
            options = st.pending.options
            if not (0 <= starter_index < len(options)):
                raise IndexError(
                    f"starter_index {starter_index} out of range (offered {len(options)})"
                )
            self.engine.step(engine.ChooseStarter(int(options[starter_index]["species_id"])))
            self.checkpoint("starter_selected", {"starter_index": starter_index})

            for step, act in enumerate(sc["actions"]):
                kind = act["kind"]
                if kind == "visit":
                    node_id = act["node"]
                    st = self.engine.state
                    assert st is not None and st.map is not None
                    node = st.map.nodes.get(node_id)
                    if node is None:
                        raise KeyError(f"step {step}: no node {node_id}")
                    self.checkpoint(
                        "node_pre", {"node": node_id, "node_type": node.type, "step": step}
                    )
                    before = len(self.battles)
                    self.engine.step(engine.VisitNode(node_id))
                    for b in range(before, len(self.battles)):
                        self.checkpoint(
                            "battle",
                            {"node": node_id, "battle_index": b, "battle": self.battles[b]},
                        )
                    self.checkpoint("node_post", {"node": node_id, "step": step})
                elif kind == "choice":
                    st = self.engine.state
                    assert st is not None
                    index = act.get("index")
                    self.checkpoint(
                        "choice_pre",
                        {
                            "screen": _PHASE_TO_SCREEN.get(st.phase, f"<unmapped:{st.phase.value}>"),
                            "index": index,
                            "step": step,
                        },
                    )
                    before = len(self.battles)
                    self.engine.step(engine.SelectOption(index))
                    for b in range(before, len(self.battles)):
                        self.checkpoint(
                            "battle", {"node": None, "battle_index": b, "battle": self.battles[b]}
                        )
                    self.checkpoint("choice_post", {"index": index, "step": step})
                elif kind == "advance_map":
                    st = self.engine.state
                    assert st is not None
                    self.checkpoint(
                        "map_transition_pre", {"from_map": st.current_map, "step": step}
                    )
                    if st.phase != engine.Phase.NEXT_MAP_READY:
                        raise RuntimeError(
                            f"step {step}: advance_map but phase is {st.phase.value}"
                        )
                    self.engine.step(engine.AdvanceMap())
                    st = self.engine.state
                    assert st is not None
                    self.checkpoint(
                        "map_transition_post", {"to_map": st.current_map, "step": step}
                    )
                else:
                    raise ValueError(f"step {step}: unknown action kind {kind}")

            st = self.engine.state
            assert st is not None
            self.checkpoint(
                "terminal",
                {
                    "game_over": bool(st.game_over) or st.phase == engine.Phase.GAME_OVER,
                    "team_size": len(st.team),
                    "screen": _PHASE_TO_SCREEN.get(st.phase, f"<unmapped:{st.phase.value}>"),
                },
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced in the output, not swallowed
            import traceback

            self.error = "".join(traceback.format_exception(exc))
        finally:
            engine._run_battle = real_run_battle  # type: ignore[assignment]

        out: dict = {
            "checkpoints": self.checkpoints,
            "notes": self.notes,
            "rng_draws_total": self.counter.draws,
            "network_attempts": [],
            "screen_log": [c["screen"] for c in self.checkpoints],
        }
        if self.error:
            out["error"] = self.error
        return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python run_scenario.py <scenario.json>", file=sys.stderr)
        return 1
    with open(argv[1], encoding="utf-8") as handle:
        scenario = json.load(handle)
    if scenario.get("schema_version") != SCHEMA_VERSION:
        print(
            f"scenario {argv[1]} declares schema_version {scenario.get('schema_version')}, "
            f"runner speaks {SCHEMA_VERSION}",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(json.dumps(Runner(scenario).run()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
