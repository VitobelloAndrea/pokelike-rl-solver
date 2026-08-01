"""The M3 **frozen parity signature**: a complete, canonical, tracked
description of every difference the route oracle is currently expected to see.

Why this replaced the old allow-list
------------------------------------

`compare.py --audit-frozen` used to compare a set of differing FIELD NAMES
against a hard-coded `FROZEN_DIFF_PATHS` set. M3.3 reproduced three concrete
ways that let a real regression through, all of them exiting 0:

* running a SUBSET of the matrix (one scenario, three of the six paths) still
  printed "the observed diff set is exactly the frozen M4 finding set";
* HIDING a redundant scenario from `manifest.json` still passed `--all`;
* changing the COUNT or the VALUES under an already-known path -- the live
  demonstration inverted `accessible` on every `n1_0` node -- still passed,
  because only the field NAME was ever compared.

The signature below binds all of that. It is an ordered, canonically hashed
structure covering:

* the exact manifest scenario set and each scenario's identity (order-
  independent: the list is sorted, so `--order reverse` produces the same
  signature, but a hidden, added or duplicated scenario does not);
* the scenario filename each difference belongs to;
* the checkpoint index and the checkpoint kind on both sides;
* the normalized field path;
* the occurrence count;
* a collision-resistant canonical hash of the ordered JS/Python values;
* one `signature_sha256` over the whole structure.

`--audit-frozen` exits 0 only when the complete observed signature equals the
tracked one. It is still not a parity mode and never reports a parity PASS.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import checkpoints as cp_mod

SIGNATURE_VERSION = 1

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNATURE_FILE = os.path.join(HERE, "frozen_signature.json")


def _values_hash(records: list[dict]) -> str:
    """Collision-resistant hash of the ordered (index, raw path, js, py)
    tuples behind one grouped difference. Grouping is for readability; this
    hash is what makes a changed VALUE -- not merely a changed field name --
    change the signature."""
    return cp_mod.sha256_of(
        [[r["index"], r["raw_path"], r["js"], r["py"]] for r in records]
    )


def build(results: list[dict]) -> dict:
    """Build the complete observed signature from `compare.py`'s per-scenario
    results. `results` must carry `signature_records` (see `compare.compare_one`).

    Scenario order is normalized away (sorted by filename) so `--order reverse`
    and `--order sorted` produce identical signatures, while the scenario SET
    is bound exactly.
    """
    scenarios = sorted(
        ({"file": r["file"], "scenario": r["scenario"]} for r in results),
        key=lambda s: (s["file"], s["scenario"]),
    )

    differences: list[dict] = []
    for result in sorted(results, key=lambda r: r["file"]):
        grouped: dict[str, list[dict]] = {}
        for record in result.get("signature_records", []):
            grouped.setdefault(record["path"], []).append(record)
        for path in sorted(grouped):
            records = grouped[path]
            differences.append({
                "file": result["file"],
                "scenario": result["scenario"],
                "path": path,
                "count": len(records),
                "checkpoints": [
                    [r["index"], r["js_kind"], r["py_kind"]] for r in records
                ],
                "values_sha256": _values_hash(records),
            })

    body = {
        "signature_version": SIGNATURE_VERSION,
        "schema_version": cp_mod.SCHEMA_VERSION,
        "scenarios": scenarios,
        "differences": differences,
    }
    body["signature_sha256"] = cp_mod.sha256_of(body)
    return body


def load(path: str = SIGNATURE_FILE) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(signature: dict, path: str = SIGNATURE_FILE) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(signature, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _key(difference: dict) -> tuple:
    return (difference["file"], difference["path"])


def compare(observed: dict, tracked: dict) -> list[str]:
    """Return a list of human-readable reasons the observed signature differs
    from the tracked one. Empty list == exact match."""
    problems: list[str] = []

    if observed.get("signature_version") != tracked.get("signature_version"):
        problems.append(
            f"signature_version {observed.get('signature_version')} != tracked "
            f"{tracked.get('signature_version')}"
        )
    if observed.get("schema_version") != tracked.get("schema_version"):
        problems.append(
            f"schema_version {observed.get('schema_version')} != tracked "
            f"{tracked.get('schema_version')}"
        )

    # -- scenario set --------------------------------------------------------
    obs_files = [s["file"] for s in observed.get("scenarios", [])]
    trk_files = [s["file"] for s in tracked.get("scenarios", [])]
    duplicates = sorted({f for f in obs_files if obs_files.count(f) > 1})
    if duplicates:
        problems.append(f"scenario(s) appear more than once in the run: {duplicates}")
    missing = sorted(set(trk_files) - set(obs_files))
    added = sorted(set(obs_files) - set(trk_files))
    if missing:
        problems.append(f"scenario(s) HIDDEN from the run: {missing}")
    if added:
        problems.append(f"scenario(s) ADDED to the run: {added}")
    obs_ident = {s["file"]: s["scenario"] for s in observed.get("scenarios", [])}
    trk_ident = {s["file"]: s["scenario"] for s in tracked.get("scenarios", [])}
    for name in sorted(set(obs_ident) & set(trk_ident)):
        if obs_ident[name] != trk_ident[name]:
            problems.append(
                f"{name}: scenario identity changed {trk_ident[name]!r} -> {obs_ident[name]!r}"
            )

    # -- differences ---------------------------------------------------------
    obs_diff = {_key(d): d for d in observed.get("differences", [])}
    trk_diff = {_key(d): d for d in tracked.get("differences", [])}

    for key in sorted(set(obs_diff) - set(trk_diff)):
        difference = obs_diff[key]
        problems.append(
            f"NEW difference: {difference['file']} :: {difference['path']} "
            f"({difference['count']}x, first at checkpoint "
            f"{difference['checkpoints'][0][0] if difference['checkpoints'] else '?'})"
        )
    for key in sorted(set(trk_diff) - set(obs_diff)):
        difference = trk_diff[key]
        problems.append(
            f"MISSING difference (frozen but not observed): {difference['file']} :: "
            f"{difference['path']} ({difference['count']}x)"
        )
    for key in sorted(set(obs_diff) & set(trk_diff)):
        obs, trk = obs_diff[key], trk_diff[key]
        if obs["count"] != trk["count"]:
            problems.append(
                f"COUNT changed: {obs['file']} :: {obs['path']} "
                f"{trk['count']} -> {obs['count']}"
            )
        if obs["checkpoints"] != trk["checkpoints"]:
            obs_ix = [c[0] for c in obs["checkpoints"]]
            trk_ix = [c[0] for c in trk["checkpoints"]]
            if obs_ix != trk_ix:
                problems.append(
                    f"CHECKPOINTS moved: {obs['file']} :: {obs['path']} "
                    f"{cp_mod.compress_ranges(trk_ix)} -> {cp_mod.compress_ranges(obs_ix)}"
                )
            else:
                problems.append(
                    f"CHECKPOINT KIND changed: {obs['file']} :: {obs['path']}"
                )
        if obs["values_sha256"] != trk["values_sha256"]:
            problems.append(
                f"VALUES changed: {obs['file']} :: {obs['path']} "
                f"(canonical value hash {trk['values_sha256'][:16]}... -> "
                f"{obs['values_sha256'][:16]}...)"
            )

    if not problems and observed.get("signature_sha256") != tracked.get("signature_sha256"):
        problems.append(
            f"signature_sha256 {observed.get('signature_sha256')} != tracked "
            f"{tracked.get('signature_sha256')} although no field-level cause was "
            "identified -- treat as a signature-format change and re-audit."
        )
    return problems


def summarize(signature: dict) -> str:
    """A readable per-scenario summary printed alongside the canonical hash."""
    lines = [
        f"frozen parity signature {signature['signature_sha256']}",
        f"  scenarios ({len(signature['scenarios'])}): "
        + ", ".join(s["file"] for s in signature["scenarios"]),
    ]
    by_file: dict[str, list[dict]] = {}
    for difference in signature["differences"]:
        by_file.setdefault(difference["file"], []).append(difference)
    for name in sorted(by_file):
        lines.append(f"  {name}:")
        for difference in by_file[name]:
            indices = [c[0] for c in difference["checkpoints"]]
            lines.append(
                f"      {difference['count']:4d}x  {difference['path']:38s} "
                f"checkpoints {cp_mod.compress_ranges(indices)}"
            )
    return "\n".join(lines)
