"""JavaScript-vs-Python battle equivalence oracle.

Runs the same fixture through the real JS `runBattle` (via `run-fixture.js`,
executing an audited slice of `pokelike_forked/js/bundle.deobfuscated.js` --
see `extract-prefix.js`'s header for the safety reasoning) and through the
Python port (`pokelike.battle_loop.run_battle`), using the SAME fixed RNG
seed and the SAME battle-config branch-selection rule
(`runBattleScreen`, bundle.deobfuscated.js:81075-81085 -- ordinary,
non-Endless Story/Nuzlocke only, per this session's locked scope), then
diffs the two normalized results and reports the first divergence.

Usage:
    python compare.py <fixture.json> [<fixture.json> ...]
    python compare.py --all          # every fixtures/*.json

Exit code is nonzero if any fixture diverges (or errors).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from pokelike import battle_loop, engine, rng  # noqa: E402
from pokelike.battle import BattleConfig, Combatant, HeldItem, Trait  # noqa: E402
from pokelike.data import BaseStats  # noqa: E402


def _build_base_stats(raw: dict) -> BaseStats:
    """Fixture JSON uses the JS field name `def` (`baseStats.def`); Python's
    `BaseStats` calls it `defense` since `def` is a reserved word."""
    fields = dict(raw)
    if "def" in fields:
        fields["defense"] = fields.pop("def")
    return BaseStats(**fields)


def _build_combatant(spec: dict) -> Combatant:
    base_stats = _build_base_stats(spec["base_stats"])
    level = spec["level"]
    max_hp = spec.get("max_hp")
    if max_hp is None:
        from pokelike.map_gen import calc_hp

        max_hp = calc_hp(base_stats.hp, level)
    held_item = HeldItem(id=spec["held_item"]) if spec.get("held_item") else None
    return Combatant(
        species_id=spec["species_id"],
        level=level,
        base_stats=base_stats,
        types=tuple(spec["types"]),
        max_hp=max_hp,
        current_hp=spec.get("current_hp", max_hp),
        name=spec.get("name", f"Species{spec['species_id']}"),
        nickname=spec.get("name"),
        held_item=held_item,
        move_tier=spec.get("move_tier", 1),
        is_shiny=bool(spec.get("is_shiny", False)),
        burned=bool(spec.get("burned", False)),
        paralyzed=bool(spec.get("paralyzed", False)),
        poison_stacks=int(spec.get("poison_stacks", 0)),
    )


def _battle_configs_for_fixture(fixture: dict):
    """Use the engine's actual ordinary Story/Nuzlocke config selector.

    This deliberately avoids maintaining a second copy of the Gen1-4/null-
    traits branching rule inside the oracle.
    """
    passives = [Trait(id=p) if isinstance(p, str) else Trait(**p) for p in fixture.get("passives", [])]
    state = engine.RunState(
        gen2_mode=fixture["gen"] == 2,
        gen3_mode=fixture["gen"] == 3,
        gen4_mode=fixture["gen"] == 4,
        passives=passives,
    )
    ability_config, traits_config = engine._battle_configs(state, [])

    # M6.2: `buildTraitsConfig`'s first two arguments are the player/enemy
    # trait-tier maps. `runBattleScreen`'s ordinary Story/Nuzlocke branch
    # passes `{}, {}` (bundle.deobfuscated.js:81084), which `_battle_configs`
    # faithfully mirrors (engine.py:1235-1236) -- so tier-gated traits are
    # unreachable in this mode on BOTH sides. Real source call sites at 76812,
    # 81069, 86059 and 90734 do pass computed maps, so a fixture may declare
    # them to exercise those already-ported traits cross-runtime. This mirrors
    # `_battle_configs`'s own non-null rule (tiers OR passives) rather than
    # inventing a second branch rule, and is a no-op when both keys are absent.
    player_tiers = fixture.get("player_tiers") or {}
    enemy_tiers = fixture.get("enemy_tiers") or {}
    if (player_tiers or enemy_tiers) and (state.gen3_mode or state.gen4_mode):
        from pokelike import battle_traits

        traits_config = battle_traits.TraitsConfig(
            player_tiers=dict(player_tiers),
            enemy_tiers=dict(enemy_tiers),
            traits=passives,
        )
    return ability_config, traits_config, passives


def run_python(fixture: dict) -> dict:
    rng.seed_rng(fixture["seed"])
    ability_config, traits_config, passives = _battle_configs_for_fixture(fixture)
    player_team = [_build_combatant(s) for s in fixture["player_team"]]
    enemy_team = [_build_combatant(s) for s in fixture["enemy_team"]]
    battle_config = BattleConfig()
    if traits_config is not None:
        battle_config.dark_crit_floor = traits_config.dark_crit_floor
    original_rng = rng.rng
    rng_draws = 0

    def counted_rng():
        nonlocal rng_draws
        rng_draws += 1
        return original_rng()

    rng.rng = counted_rng
    try:
        result = battle_loop.run_battle(
            player_team,
            enemy_team,
            traits=passives,
            ability_config=ability_config,
            traits_config=traits_config,
            battle_config=battle_config,
        )
    finally:
        rng.rng = original_rng

    def normalize_mon(m: Combatant) -> dict:
        return {
            "species_id": m.species_id,
            "level": m.level,
            "current_hp": m.current_hp,
            "max_hp": m.max_hp,
            "status": m.status,
            "burned": bool(m.burned),
            "paralyzed": bool(m.paralyzed),
            "poison_stacks": m.poison_stacks or 0,
            "stages": {k: m.stages.get(k, 0) for k in ("atk", "def", "speed", "special", "spdef")},
        }

    return {
        "player_won": result.player_won,
        "player_team": [normalize_mon(m) for m in result.player_team],
        "enemy_team": [normalize_mon(m) for m in result.enemy_team],
        "player_participants": sorted(result.player_participants),
        "rounds": result.rounds,
        "rng_draws": rng_draws,
        "final_rng_seed": rng.get_rng_seed(),
        "status_events": result.status_events,
        "hook_trace": result.hook_trace,
    }


def run_js(fixture_path: Path) -> tuple[dict, dict]:
    proc = subprocess.run(
        ["node", str(_HERE / "run-fixture.js"), str(fixture_path.resolve())],
        capture_output=True,
        text=True,
        cwd=str(_HERE),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"JS oracle failed for {fixture_path}:\n{proc.stderr}")
    data = json.loads(proc.stdout)
    diagnostics = {"js_event_count": data.pop("event_count", None)}
    return data, diagnostics


def _verify_prefix_freshness() -> None:
    """Regenerate the executable slice when absent, then fail if it is stale.

    M6/N17. `out/battle-prefix.js` is a 3.2 MB derived slice of the tracked
    bundle and is deliberately NOT in the repository -- but it used to be
    ignored by a blanket `tools/` rule that also hid this file, every fixture
    and this script, so a clean clone could not run the gate at all. It merely
    raised "missing oracle prefix". Since this comparison has been a standing
    acceptance gate in every milestone since M3, that made the gate
    unreproducible for anyone but the machine that first generated it.

    The pattern is `route-oracle/`'s, which had it right: track the tooling,
    ignore only the derived artifact, and pin its hash in a tracked file so
    nothing required is invisible. `prefix.sha256` is that pin.
    """
    prefix = _HERE / "out" / "battle-prefix.js"
    bundle = _REPO_ROOT / "pokelike_forked" / "js" / "bundle.deobfuscated.js"
    pinned_path = _HERE / "prefix.sha256"

    with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as tmp:
        fresh_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["node", str(_HERE / "extract-prefix.js"), str(bundle), str(fresh_path)],
            capture_output=True,
            text=True,
            cwd=str(_HERE),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"could not verify oracle prefix freshness:\n{proc.stderr}")
        fresh_hash = hashlib.sha256(fresh_path.read_bytes()).hexdigest()

        if not prefix.is_file():
            # A clean clone. Materialise it from the tracked bundle rather than
            # refusing to run; the pin below still has to agree.
            prefix.parent.mkdir(parents=True, exist_ok=True)
            prefix.write_bytes(fresh_path.read_bytes())
            print(f"PREFIX  regenerated from the tracked bundle ({prefix})")

        current_hash = hashlib.sha256(prefix.read_bytes()).hexdigest()
        if current_hash != fresh_hash:
            raise RuntimeError(
                "oracle prefix is stale; regenerate it with extract-prefix.js "
                f"(on-disk sha256={current_hash}, fresh sha256={fresh_hash})"
            )

        if pinned_path.is_file():
            pinned = pinned_path.read_text(encoding="utf-8").strip()
            if pinned != fresh_hash:
                raise RuntimeError(
                    "oracle prefix does not match the tracked pin -- either the "
                    "bundle or extract-prefix.js changed, and that has to be a "
                    "deliberate, recorded decision.\n"
                    f"  pinned  ({pinned_path.name}) sha256={pinned}\n"
                    f"  fresh   (from the bundle)    sha256={fresh_hash}"
                )
        else:
            raise RuntimeError(
                f"missing tracked prefix pin: {pinned_path}. It is what makes "
                f"the derived slice safe to leave untracked."
            )

        print(f"PREFIX  fresh sha256={current_hash} (== {pinned_path.name})")
    finally:
        fresh_path.unlink(missing_ok=True)


def _first_divergence(js_result: dict, py_result: dict) -> str | None:
    def walk(a, b, path_str):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in a.keys() | b.keys():
                sub = walk(a.get(key), b.get(key), f"{path_str}.{key}")
                if sub:
                    return sub
            return None
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return f"{path_str}: length JS={len(a)} Python={len(b)}"
            for i, (x, y) in enumerate(zip(a, b)):
                sub = walk(x, y, f"{path_str}[{i}]")
                if sub:
                    return sub
            return None
        if a != b:
            return f"{path_str}: JS={a!r} Python={b!r}"
        return None

    return walk(js_result, py_result, "$")


def run_fixture(fixture_path: Path) -> bool:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    js_result, diagnostics = run_js(fixture_path)
    py_result = run_python(fixture)
    divergence = _first_divergence(js_result, py_result)
    name = fixture.get("description", fixture_path.name)
    if divergence is None:
        print(f"AGREE   {fixture_path.name}: {name}")
        print(
            "        "
            f"rounds={py_result['rounds']} rng_draws={py_result['rng_draws']} "
            f"js_events={diagnostics['js_event_count']}"
        )
        return True
    print(f"DIVERGE {fixture_path.name}: {name}")
    print(f"        first divergence: {divergence}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", nargs="*", help="fixture JSON paths")
    parser.add_argument("--all", action="store_true", help="run every fixtures/*.json")
    args = parser.parse_args()

    if args.all:
        paths = sorted((_HERE / "fixtures").glob("*.json"))
    else:
        paths = [Path(p) for p in args.fixtures]
    if not paths:
        parser.error("no fixtures given (pass paths, or --all)")

    _verify_prefix_freshness()
    results = [run_fixture(p) for p in paths]
    agree = sum(results)
    total = len(results)
    print(f"\n{agree}/{total} fixtures agree.")
    return 0 if agree == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
