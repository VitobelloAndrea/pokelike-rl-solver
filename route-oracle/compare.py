"""M3 short-full-run route oracle: run every scenario through the real
JavaScript source and the real Python port, compare the normalized
checkpoint streams, and fail nonzero on any divergence.

    python route-oracle/compare.py --all
    python route-oracle/compare.py route-oracle/scenarios/story_gen1_map0.json
    python route-oracle/compare.py --all --order reverse
    python route-oracle/compare.py --all --json      # machine-readable summary

Self-checks performed on every invocation (each one a hard failure, nothing
is silently skipped):

* the route prefix is freshly re-extracted from the bundle to a temp file and
  its SHA-256 compared against ``out/route-prefix.js`` AND against the
  expected hash checked in at ``prefix.sha256`` -- a stale prefix, a changed
  bundle, or a prefix built from a different bundle all fail here;
* every scenario declares the schema version both runners speak;
* every scenario file named by ``manifest.json`` exists;
* neither runner reported an error, and the JavaScript runner reports no
  attempted network access.

Exit codes: 0 all scenarios agree; 1 a divergence or a self-check failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import checkpoints as cp_mod  # noqa: E402
import coverage as cov_mod  # noqa: E402
import frozen_signature as sig_mod  # noqa: E402

BUNDLE = os.path.join(REPO, "pokelike_forked", "js", "bundle.deobfuscated.js")
PREFIX = os.path.join(HERE, "out", "route-prefix.js")
PREFIX_HASH_FILE = os.path.join(HERE, "prefix.sha256")
MANIFEST = os.path.join(HERE, "scenarios", "manifest.json")

# The only network URLs a Story/Nuzlocke route may attempt. Both are handled
# by the source's own offline fallbacks, and those fallback branches are what
# the Python port models -- see driver.js's network guard for the full
# reasoning. Any other URL fails the run.
ALLOWED_NETWORK_PREFIXES = (
    "data/pokedex.json",
    "https://pokeapi.co/api/v2/pokemon?limit=",
)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_prefix_freshness(quiet: bool = False) -> str:
    """Regenerate the prefix from the bundle into a temp file and require it
    to match both the on-disk prefix and the checked-in expected hash."""
    if not os.path.exists(BUNDLE):
        _fail(f"missing bundle {BUNDLE}")
    if not os.path.exists(PREFIX):
        _fail(
            f"missing {PREFIX} -- run: node route-oracle/extract-prefix.js "
            f"{os.path.relpath(BUNDLE, REPO)} route-oracle/out/route-prefix.js"
        )
    with tempfile.TemporaryDirectory() as tmp:
        fresh = os.path.join(tmp, "route-prefix.js")
        proc = subprocess.run(
            ["node", os.path.join(HERE, "extract-prefix.js"), BUNDLE, fresh],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            _fail(f"extract-prefix.js failed:\n{proc.stdout}\n{proc.stderr}")
        fresh_hash = _sha256_file(fresh)
    on_disk = _sha256_file(PREFIX)
    if fresh_hash != on_disk:
        _fail(
            "route prefix is STALE: freshly extracted "
            f"{fresh_hash} != on-disk {on_disk}. Re-run extract-prefix.js."
        )
    if os.path.exists(PREFIX_HASH_FILE):
        with open(PREFIX_HASH_FILE, encoding="utf-8") as handle:
            expected = handle.read().split()[0].strip()
        if expected != fresh_hash:
            _fail(
                f"route prefix hash {fresh_hash} does not match the checked-in "
                f"expected hash {expected} in prefix.sha256 -- the bundle or the "
                "cut point changed; re-audit with scan-toplevel-danger.js before "
                "updating prefix.sha256."
            )
    else:
        _fail(f"missing {PREFIX_HASH_FILE}; the expected prefix hash must be checked in")
    if not quiet:
        print(f"prefix sha256 {fresh_hash} (fresh == on-disk == prefix.sha256)")
    return fresh_hash


def run_js(scenario_path: str) -> dict:
    proc = subprocess.run(
        ["node", os.path.join(HERE, "run-scenario.js"), scenario_path],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if proc.returncode != 0:
        _fail(f"javascript runner failed for {scenario_path}:\n{proc.stdout}\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        _fail(f"javascript runner produced non-JSON for {scenario_path}:\n{proc.stdout[:2000]}")
        raise  # unreachable


def run_py(scenario_path: str) -> dict:
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "run_scenario.py"), scenario_path],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if proc.returncode != 0:
        _fail(f"python runner failed for {scenario_path}:\n{proc.stdout}\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        _fail(f"python runner produced non-JSON for {scenario_path}:\n{proc.stdout[:2000]}")
        raise  # unreachable


def load_manifest() -> list[dict]:
    if not os.path.exists(MANIFEST):
        _fail(f"missing {MANIFEST}")
    with open(MANIFEST, encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != cp_mod.SCHEMA_VERSION:
        _fail(
            f"manifest declares schema_version {manifest.get('schema_version')}, "
            f"harness speaks {cp_mod.SCHEMA_VERSION}"
        )
    required = manifest.get("required_coverage")
    if required is None:
        _fail("manifest has no `required_coverage` list -- the coverage gate cannot run")
    if list(required) != list(cov_mod.REQUIRED_TAGS):
        _fail(
            "manifest `required_coverage` disagrees with coverage.REQUIRED_TAGS:\n"
            f"  manifest: {list(required)}\n"
            f"  harness : {list(cov_mod.REQUIRED_TAGS)}"
        )
    entries = manifest["scenarios"]
    seen: set[str] = set()
    for entry in entries:
        if entry["file"] in seen:
            _fail(f"manifest lists {entry['file']} more than once")
        seen.add(entry["file"])
        path = os.path.join(HERE, "scenarios", entry["file"])
        if not os.path.exists(path):
            _fail(f"manifest names missing fixture {entry['file']}")
        if "expected_coverage" not in entry:
            _fail(f"manifest entry {entry['file']} has no `expected_coverage` block")
        entry["path"] = path
    return entries


def check_coverage(results: list[dict]) -> None:
    """Fail unless the OBSERVED checkpoints earn every required tag, and unless
    each scenario earns exactly the evidence its manifest entry pins.

    Coverage is derived and enforced INDEPENDENTLY OVER BOTH RUNTIMES. The
    JavaScript stream is the authority on whether a route was really reached,
    but the Python stream has to demonstrate the same path on its own: before
    M3.3 the Python side was only checked later, by the ordinary unittest
    suite, so `compare.py --all` could pass while the port reached a different
    set of paths. Pinning the evidence INDICES is what makes a removed,
    inserted or reordered checkpoint fail here rather than silently still
    counting.
    """
    for runtime in ("js", "py"):
        key = "coverage" if runtime == "js" else "py_coverage"
        observed = {r["file"]: r[key] for r in results}
        for r in results:
            expected = r.get("expected_coverage")
            if expected is None:
                continue
            got = r[key]
            if {k: list(v) for k, v in got.items()} != {
                k: list(v) for k, v in expected.items()
            }:
                lines = [f"COVERAGE DRIFT in {r['file']} ({runtime.upper()} stream):"]
                for tag in sorted(set(got) | set(expected)):
                    if list(got.get(tag, [])) != list(expected.get(tag, [])):
                        lines.append(
                            f"  {tag}: observed {got.get(tag)} != manifest {expected.get(tag)}"
                        )
                lines.append(
                    "  A checkpoint was removed, inserted or reordered, or the route "
                    "changed. Re-derive with coverage.derive() and update manifest.json "
                    "only if the new route is genuinely intended."
                )
                _fail("\n".join(lines))
        gaps = cov_mod.missing(observed)
        if gaps:
            _fail(
                f"ROUTE COVERAGE INCOMPLETE on the {runtime.upper()} stream -- the M3 "
                "tooling gate cannot pass.\n"
                f"  required but never observed: {gaps}\n"
                "  (a scenario's own `covers` list, a source citation, a planned route "
                "and a losing attempt all count for nothing here)"
            )

    # The two runtimes must also agree with EACH OTHER, so a path that the
    # source reaches and the port does not (or vice versa) cannot hide behind
    # a manifest that happens to match both.
    for r in results:
        if r["coverage"] != r["py_coverage"]:
            lines = [f"COVERAGE DISAGREEMENT between runtimes in {r['file']}:"]
            for tag in sorted(set(r["coverage"]) | set(r["py_coverage"])):
                if r["coverage"].get(tag) != r["py_coverage"].get(tag):
                    lines.append(
                        f"  {tag}: js {r['coverage'].get(tag)} != python "
                        f"{r['py_coverage'].get(tag)}"
                    )
            _fail("\n".join(lines))


def compare_one(entry: dict, *, verbose: bool, dump_dir: str | None, quiet: bool = False) -> dict:
    path = entry["path"]
    with open(path, encoding="utf-8") as handle:
        scenario = json.load(handle)
    if scenario.get("schema_version") != cp_mod.SCHEMA_VERSION:
        _fail(
            f"{entry['file']} declares schema_version {scenario.get('schema_version')}, "
            f"harness speaks {cp_mod.SCHEMA_VERSION}"
        )

    js = run_js(path)
    py = run_py(path)

    if js.get("error"):
        _fail(f"{entry['file']}: javascript runner reported an error:\n{js['error']}")
    if py.get("error"):
        _fail(f"{entry['file']}: python runner reported an error:\n{py['error']}")
    unexpected = [
        url for url in js.get("network_attempts", [])
        if not any(url.startswith(prefix) for prefix in ALLOWED_NETWORK_PREFIXES)
    ]
    if unexpected:
        _fail(
            f"{entry['file']}: javascript run attempted UNEXPECTED network access for "
            f"{unexpected} -- the oracle must be offline (see driver.js's network guard)"
        )

    js_cps, py_cps = js["checkpoints"], py["checkpoints"]
    result = {
        "file": entry["file"],
        "scenario": scenario["scenario"],
        "covers": entry.get("covers", []),
        "js_checkpoints": len(js_cps),
        "py_checkpoints": len(py_cps),
        "js_stream_sha256": cp_mod.stream_hash(js_cps),
        "py_stream_sha256": cp_mod.stream_hash(py_cps),
        "js_rng_draws": js.get("rng_draws_total"),
        "py_rng_draws": py.get("rng_draws_total"),
        # Derived from the OBSERVED source stream, never from `covers`.
        "coverage": cov_mod.derive(js_cps),
        "py_coverage": cov_mod.derive(py_cps),
        "expected_coverage": entry.get("expected_coverage"),
    }

    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
        stem = os.path.splitext(entry["file"])[0]
        for side, payload in (("js", js), ("py", py)):
            with open(os.path.join(dump_dir, f"{stem}.{side}.json"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps(cp_mod.canonical(payload), sort_keys=True, indent=2))

    report = cp_mod.compare_streams(js_cps, py_cps)
    diverging = cp_mod.divergent_indices(js_cps, py_cps)
    result["agree"] = report is None
    result["divergent_checkpoints"] = diverging
    result["agreeing_checkpoints"] = max(len(js_cps), len(py_cps)) - len(diverging)
    if report is not None:
        result["report"] = report
    summary = cp_mod.field_path_summary(js_cps, py_cps) if report is not None else []
    result["field_path_summary"] = [
        {"path": p, "count": c, "first_indices": i} for p, c, i in summary
    ]
    # The raw, uncollapsed difference records the frozen signature is built
    # from. Always computed -- an unexpectedly EMPTY record set is exactly as
    # much of a signature change as an unexpectedly full one.
    result["signature_records"] = cp_mod.signature_records(js_cps, py_cps)
    if verbose or (report is not None and not quiet):
        status = "AGREE  " if report is None else "DIVERGE"
        print(f"{status} {entry['file']}: {scenario.get('description', '')}")
        print(
            f"        checkpoints js={len(js_cps)} py={len(py_cps)}  "
            f"rng draws js={js.get('rng_draws_total')} py={py.get('rng_draws_total')}"
        )
        print(f"        js stream sha256 {result['js_stream_sha256']}")
        print(f"        py stream sha256 {result['py_stream_sha256']}")
        if report is not None:
            print(
                f"        {result['agreeing_checkpoints']}/{max(len(js_cps), len(py_cps))} "
                f"checkpoints agree; diverging: {cp_mod.compress_ranges(diverging)}"
            )
            print(cp_mod.format_report(report))
            print("  every differing field path across the whole stream:")
            for path, count, first in summary:
                print(f"    {count:4d}x  {path}   first at checkpoint(s) {first}")
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", help="a single scenario json to run")
    parser.add_argument("--all", action="store_true", help="run every scenario in the manifest")
    parser.add_argument(
        "--order",
        choices=("manifest", "reverse", "sorted"),
        default="manifest",
        help="scenario execution order; used to prove order-independence",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    parser.add_argument(
        "--dump", metavar="DIR", help="write both normalized streams per scenario into DIR"
    )
    parser.add_argument(
        "--audit-frozen",
        action="store_true",
        help=(
            "AUDIT MODE, not a parity mode. Requires --all. Exits 0 only when the "
            "COMPLETE observed parity signature (scenario set, per-difference "
            "checkpoint indices, kinds, counts and canonical value hashes) equals the "
            "tracked frozen_signature.json AND route coverage is complete on both "
            "runtimes. It never reports parity PASS -- a clean audit run still means "
            "PARITY BLOCKED. Use the default mode for the parity gate."
        ),
    )
    parser.add_argument(
        "--write-frozen-signature",
        action="store_true",
        help=(
            "Regenerate route-oracle/frozen_signature.json from this run. A DELIBERATE "
            "re-freeze, never part of a gate: every changed difference must be traced "
            "to source and recorded in findings/M3-parity-blockers.md first. Requires "
            "--all."
        ),
    )
    args = parser.parse_args(argv[1:])

    if not args.all and not args.scenario:
        parser.error("pass a scenario path or --all")
    # The frozen signature describes the COMPLETE matrix. Auditing a subset of
    # it was the first M3.2 defect reproduced in M3.3: a single scenario showed
    # three of the six known differences and `--audit-frozen` exited 0 while
    # printing that the observed set was "exactly the frozen M4 finding set".
    if args.audit_frozen and not args.all:
        parser.error(
            "--audit-frozen requires --all: the frozen signature covers the complete "
            "manifest, and auditing a subset of it proves nothing."
        )
    if args.write_frozen_signature and not args.all:
        parser.error("--write-frozen-signature requires --all")
    if args.audit_frozen and args.write_frozen_signature:
        parser.error("--audit-frozen and --write-frozen-signature are mutually exclusive")

    check_prefix_freshness(quiet=args.json)

    if args.all:
        entries = load_manifest()
        if args.order == "reverse":
            entries = list(reversed(entries))
        elif args.order == "sorted":
            entries = sorted(entries, key=lambda e: e["file"])
    else:
        path = os.path.abspath(args.scenario)
        entries = [{"file": os.path.basename(path), "path": path}]

    results = [
        compare_one(e, verbose=args.verbose, dump_dir=args.dump, quiet=args.json)
        for e in entries
    ]
    agreed = sum(1 for r in results if r["agree"])

    # The coverage gate is part of TOOLING, not parity: it runs whenever the
    # whole manifest was executed, and it is a hard failure of its own.
    if args.all:
        check_coverage(results)

    observed_paths = {
        item["path"] for r in results for item in r.get("field_path_summary", [])
    }
    observed_signature = sig_mod.build(results) if args.all else None

    if args.json:
        print(json.dumps({
            "results": results,
            "agreed": agreed,
            "total": len(results),
            "observed_diff_paths": sorted(observed_paths),
            "observed_signature": observed_signature,
        }, indent=2))
    else:
        if args.all:
            earned = cov_mod.merge({r["file"]: r["coverage"] for r in results})
            print(
                f"\nroute coverage: {len(earned & set(cov_mod.REQUIRED_TAGS))}"
                f"/{len(cov_mod.REQUIRED_TAGS)} required tags earned from observed checkpoints."
            )
        print(f"{agreed}/{len(results)} scenarios agree.")
        if agreed != len(results):
            print("Diverging scenarios:")
            for r in results:
                if not r["agree"]:
                    print(f"  - {r['file']} (checkpoint {r['report']['index']})")

    if args.write_frozen_signature:
        assert observed_signature is not None
        sig_mod.save(observed_signature)
        print(f"\nWROTE {sig_mod.SIGNATURE_FILE}")
        print(sig_mod.summarize(observed_signature))
        print(
            "\nThis is a deliberate RE-FREEZE, not a gate result. Every changed "
            "difference must already be traced to source in "
            "findings/M3-parity-blockers.md."
        )
        return 0

    if args.audit_frozen:
        assert observed_signature is not None
        tracked = sig_mod.load()
        if tracked is None:
            _fail(
                f"AUDIT: no frozen signature at {sig_mod.SIGNATURE_FILE}. Generate it "
                "with `--all --write-frozen-signature` only after every difference is "
                "traced to source and recorded in findings/M3-parity-blockers.md."
            )
        problems = sig_mod.compare(observed_signature, tracked)
        if problems:
            _fail(
                "AUDIT: the observed parity signature does NOT equal the frozen "
                "signature.\n"
                + "\n".join(f"  - {p}" for p in problems)
                + "\n  observed signature_sha256 "
                + f"{observed_signature['signature_sha256']}\n"
                + f"  frozen   signature_sha256 {tracked.get('signature_sha256')}\n"
                "  Trace every change to source and record it in "
                "findings/M3-parity-blockers.md before re-freezing. Do not repair it "
                "in an M3 session."
            )
        print()
        print(sig_mod.summarize(observed_signature))
        print(
            "\nAUDIT: the COMPLETE observed parity signature equals the frozen "
            f"signature ({observed_signature['signature_sha256']})."
        )
        # The wording has to follow the signature's own content. Under M3 the
        # frozen signature carried 40 difference records, so a clean audit
        # genuinely still meant PARITY BLOCKED and saying so was the whole
        # point. Since M4 it carries zero, and repeating the M3 sentence would
        # be a false statement printed by a passing gate.
        if observed_signature.get("differences"):
            print(
                "PARITY REMAINS BLOCKED -- this is NOT a parity PASS. The default mode "
                "(no --audit-frozen) is the parity gate and still exits nonzero."
            )
        else:
            print(
                "The frozen signature contains ZERO difference records, so this also "
                "asserts that no difference has REAPPEARED. It is still not the parity "
                "gate: the default mode (no --audit-frozen) is, and it must exit 0."
            )
        return 0

    return 0 if agreed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
