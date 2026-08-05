"""Canonicalization, hashing and field-level diffing for the M3 route oracle.

Shared by ``compare.py`` and by ``pokelike/tests/test_route_oracle.py`` so
there is exactly one definition of "what does it mean for two checkpoint
streams to agree".
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Optional

# Collapses `team[0].level` -> `team[i].level` so per-element noise groups.
# `map.edges[len]` (the length marker diff_values emits) is left alone.
_INDEX_RE = re.compile(r"\[\d+\]")

SCHEMA_VERSION = 2

# Fields recorded for human/diagnostic value but deliberately NOT compared.
# Every entry here needs a reason in SCHEMA.md; the list is intentionally
# tiny, and it must never grow to hide a real divergence.
DIAGNOSTIC_KEYS = frozenset({"__diagnostic_event_count"})


def canonical(value: Any) -> Any:
    """Recursively drop diagnostic-only keys and put mappings into a stable
    key order. Sequences keep their order -- ordering is meaning here, never
    noise."""
    if isinstance(value, dict):
        return {k: canonical(value[k]) for k in sorted(value) if k not in DIAGNOSTIC_KEYS}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        # JS has one number type: 5 and 5.0 are the same value, and the two
        # runners must not disagree merely over which one JSON happened to
        # emit. Non-integral floats are left exactly as they are.
        return int(value)
    return value


def dumps(value: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace, UTF-8 safe."""
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def checkpoint_hashes(checkpoints: Iterable[dict]) -> list[str]:
    return [sha256_of(cp) for cp in checkpoints]


def stream_hash(checkpoints: Iterable[dict]) -> str:
    """Hash of the whole ordered stream. Built from the per-checkpoint
    hashes so that an omitted, inserted or REORDERED checkpoint changes it
    even when the individual checkpoints are individually unchanged."""
    digest = hashlib.sha256()
    for index, cp_hash in enumerate(checkpoint_hashes(checkpoints)):
        digest.update(f"{index}:{cp_hash}\n".encode("utf-8"))
    return digest.hexdigest()


def _fmt(value: Any, limit: int = 160) -> str:
    text = dumps(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def diff_values(left: Any, right: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Field-level differences between two canonicalized values, as
    ``(path, left, right)`` triples. Recurses into dicts and lists so the
    report names the exact field, never just "the checkpoints differ"."""
    left, right = canonical(left), canonical(right)
    if isinstance(left, dict) and isinstance(right, dict):
        out: list[tuple[str, Any, Any]] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else key
            if key not in left:
                out.append((child, "<missing>", right[key]))
            elif key not in right:
                out.append((child, left[key], "<missing>"))
            else:
                out.extend(diff_values(left[key], right[key], child))
        return out
    if isinstance(left, list) and isinstance(right, list):
        out = []
        if len(left) != len(right):
            out.append((f"{path}[len]", len(left), len(right)))
        for index in range(min(len(left), len(right))):
            out.extend(diff_values(left[index], right[index], f"{path}[{index}]"))
        return out
    if left != right:
        return [(path or "<root>", left, right)]
    return []


def compare_streams(
    js_checkpoints: list[dict],
    py_checkpoints: list[dict],
    *,
    max_fields: int = 40,
) -> Optional[dict]:
    """Compare two checkpoint streams. Returns ``None`` when they agree, or
    a report describing the FIRST differing checkpoint plus its field-level
    diff. A length difference is itself reported at the first index that
    exists on only one side, so an omitted or extra checkpoint is caught."""
    js_hashes = checkpoint_hashes(js_checkpoints)
    py_hashes = checkpoint_hashes(py_checkpoints)

    for index in range(max(len(js_hashes), len(py_hashes))):
        if index >= len(js_hashes):
            return {
                "index": index,
                "reason": "python stream has extra checkpoint(s)",
                "js": None,
                "py_kind": py_checkpoints[index].get("kind"),
                "fields": [],
                "js_len": len(js_hashes),
                "py_len": len(py_hashes),
            }
        if index >= len(py_hashes):
            return {
                "index": index,
                "reason": "javascript stream has extra checkpoint(s)",
                "js_kind": js_checkpoints[index].get("kind"),
                "py": None,
                "fields": [],
                "js_len": len(js_hashes),
                "py_len": len(py_hashes),
            }
        if js_hashes[index] != py_hashes[index]:
            fields = diff_values(js_checkpoints[index], py_checkpoints[index])
            return {
                "index": index,
                "reason": "checkpoint hash mismatch",
                "js_kind": js_checkpoints[index].get("kind"),
                "py_kind": py_checkpoints[index].get("kind"),
                "js_hash": js_hashes[index],
                "py_hash": py_hashes[index],
                "fields": fields[:max_fields],
                "field_total": len(fields),
                "js_len": len(js_hashes),
                "py_len": len(py_hashes),
            }
    return None


def divergent_indices(js_checkpoints: list[dict], py_checkpoints: list[dict]) -> list[int]:
    """Every checkpoint index whose two sides disagree (or exists on one side
    only). Reported alongside the first divergence so a single upstream
    difference and a genuinely broken stream can be told apart."""
    js_hashes = checkpoint_hashes(js_checkpoints)
    py_hashes = checkpoint_hashes(py_checkpoints)
    return [
        i
        for i in range(max(len(js_hashes), len(py_hashes)))
        if i >= len(js_hashes) or i >= len(py_hashes) or js_hashes[i] != py_hashes[i]
    ]


def field_path_summary(
    js_checkpoints: list[dict], py_checkpoints: list[dict]
) -> list[tuple[str, int, list[int]]]:
    """Across EVERY diverging checkpoint, which field paths actually differ
    and how often, as ``(path, count, first_few_indices)`` sorted by count.

    This exists because a single upstream difference in a CUMULATIVE field
    (``rng.draws`` is the live example: the source is permanently +3 ahead
    after the starter offer) makes every later checkpoint hash differ even
    when nothing else in them does. Reporting only the first divergence
    would then say nothing about whether the rest of the route agrees. List
    indices are collapsed to ``[i]`` so per-element noise groups together.
    """
    counts: dict[str, list[int]] = {}
    for index in range(max(len(js_checkpoints), len(py_checkpoints))):
        if index >= len(js_checkpoints) or index >= len(py_checkpoints):
            counts.setdefault("<stream length>", []).append(index)
            continue
        for path, _, _ in diff_values(js_checkpoints[index], py_checkpoints[index]):
            counts.setdefault(_INDEX_RE.sub("[i]", path), []).append(index)
    return sorted(
        ((path, len(idx), idx[:8]) for path, idx in counts.items()),
        key=lambda row: (-row[1], row[0]),
    )


def signature_records(
    js_checkpoints: list[dict], py_checkpoints: list[dict]
) -> list[dict]:
    """Every individual difference in the stream, as a flat ordered list.

    This is the raw material of the FROZEN PARITY SIGNATURE. Unlike
    ``field_path_summary`` (which aggregates to a field-name histogram for
    humans), nothing is collapsed away here: each record pins the checkpoint
    index, that checkpoint's kind on both sides, the raw field path, the
    index-collapsed path, and both values. A difference that moves to another
    checkpoint, changes its value, or appears in a different scenario produces
    a different record set.

    A stream-length difference is emitted as a record of its own so a
    hidden/extra checkpoint cannot pass as "no differing fields".
    """
    records: list[dict] = []
    for index in range(max(len(js_checkpoints), len(py_checkpoints))):
        js_cp = js_checkpoints[index] if index < len(js_checkpoints) else None
        py_cp = py_checkpoints[index] if index < len(py_checkpoints) else None
        if js_cp is None or py_cp is None:
            records.append({
                "index": index,
                "js_kind": js_cp.get("kind") if js_cp else None,
                "py_kind": py_cp.get("kind") if py_cp else None,
                "raw_path": "<stream length>",
                "path": "<stream length>",
                "js": len(js_checkpoints),
                "py": len(py_checkpoints),
            })
            continue
        for raw_path, left, right in diff_values(js_cp, py_cp):
            records.append({
                "index": index,
                "js_kind": js_cp.get("kind"),
                "py_kind": py_cp.get("kind"),
                "raw_path": raw_path,
                "path": _INDEX_RE.sub("[i]", raw_path),
                "js": left,
                "py": right,
            })
    return records


def compress_ranges(values: list[int]) -> str:
    """`[0,1,2,5,7,8]` -> `"0-2, 5, 7-8"`."""
    if not values:
        return "(none)"
    parts: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(parts)


def format_report(report: dict) -> str:
    lines = [
        f"  first divergence at checkpoint index {report['index']} ({report['reason']})",
        f"  stream lengths: js={report.get('js_len')} python={report.get('py_len')}",
    ]
    if report.get("js_kind") or report.get("py_kind"):
        lines.append(f"  kind: js={report.get('js_kind')!r} python={report.get('py_kind')!r}")
    if report.get("js_hash"):
        lines.append(f"  js  sha256 {report['js_hash']}")
        lines.append(f"  py  sha256 {report['py_hash']}")
    total = report.get("field_total", 0)
    shown = len(report.get("fields", []))
    if shown:
        lines.append(f"  differing fields ({shown} of {total} shown):")
        for path, left, right in report["fields"]:
            lines.append(f"    {path}")
            lines.append(f"        js     = {_fmt(left)}")
            lines.append(f"        python = {_fmt(right)}")
    return "\n".join(lines)
