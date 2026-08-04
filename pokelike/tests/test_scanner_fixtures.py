"""Executable verdicts for the load-time scanner's fixture corpus.

M3.4 **Defect D**: `route-oracle/fixtures/scanner/` was advertised as
"adversarial fixtures the scanner must fail, and legitimate ones it must pass",
but nothing ran them -- no test module referenced the directory and
`compare.py` never invokes the scanner -- so a regression in the guard analysis
would have been caught by no suite at all. Worse, the three `MUST PASS`
fixtures *could not* exit 0: the allow-list is pinned to the real prefix's
sha256, so the scanner rejected every other file before it ever reported a
verdict.

This module closes both halves. Every fixture is run through
`scan-toplevel-danger.js --no-allowlist` -- the fixture mode, in which a file
with no unaccounted load-reachable reference exits 0 without consulting the
pinned allow-list -- and its exit code is asserted against the verdict the
fixture itself declares on its first line. The pinned behaviour remains the
default and is what the real prefix is still audited under; `--no-allowlist`
never applies to it.

The expectations are held in TWO places on purpose: the `EXPECTED` table below
and the `MUST PASS` / `MUST FAIL` marker in each fixture's own header comment.
The test asserts they agree, so neither a silently retagged fixture nor a
silently edited table can move a verdict on its own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ORACLE = os.path.join(_REPO, "route-oracle")
_SCANNER = os.path.join(_ORACLE, "scan-toplevel-danger.js")
_FIXTURES = os.path.join(_ORACLE, "fixtures", "scanner")

PASS, FAIL = "MUST PASS", "MUST FAIL"

# Every fixture, with the verdict the scanner must reach. The three M3.4
# adversarial probes that exposed the detector blind spots are marked; the four
# it already handled are tracked here too, so the repair cannot regress them.
EXPECTED: dict[str, str] = {
    # -- legitimate: must exit 0 ------------------------------------------
    "guarded_typeof_positive.js": PASS,   # `typeof X !== "undefined"` consequent
    "guarded_typeof_return.js": PASS,     # early-`return` guard domination
    "uncalled_function.js": PASS,         # declared, never invoked at load
    # -- M3.6 defect: names and bindings are not references, fixed in M3.7 ---
    "object_method_key.js": PASS,         # `{ addEventListener() {} }`
    "object_property_key.js": PASS,       # `{ addEventListener: local }`
    "local_binding_shadow.js": PASS,      # `const setTimeout = …`, params, patterns
    "class_method_key.js": PASS,          # `class E { addEventListener() {} }`
    # -- adversarial: must exit nonzero -----------------------------------
    "plain_toplevel.js": FAIL,
    "unguarded_fetch.js": FAIL,
    "unguarded_document.js": FAIL,
    "unguarded_localstorage.js": FAIL,
    "nested_iife.js": FAIL,
    "call_apply_iife.js": FAIL,           # both spellings together
    "call_iife.js": FAIL,                 # `.call` alone
    "apply_iife.js": FAIL,                # `.apply` alone
    "dotted_global_member.js": FAIL,      # `globalThis.localStorage`
    # -- M3.4 probes the scanner MISSED (Defect C), fixed in M3.5 ---------
    "bind_invocation.js": FAIL,           # probe 1: `.bind(…)()`
    "bind_chain_call.js": FAIL,           # probe 1: `.bind(…).bind(…).call(…)`
    "toplevel_timers.js": FAIL,           # probe 2: setTimeout/setInterval
    "toplevel_microtask.js": FAIL,        # probe 2: queueMicrotask/rAF
    "computed_member.js": FAIL,           # probe 3: `window["fetch"]`
    # -- M3.4 probes the scanner already handled; pinned against regression
    "guard_wrong_global.js": FAIL,        # guard names a different global
    "arrow_iife.js": FAIL,                # `(()=>{…})()`
    "escape_after_guard.js": FAIL,        # unguarded access AFTER a good guard
    "try_catch_unguarded.js": FAIL,       # swallowed, but not inert
    # -- M3.7: the reference positions a key/binding exclusion must NOT hide --
    "shorthand_risky_reference.js": FAIL,  # `{ setTimeout }` reads it
    "computed_key_reference.js": FAIL,     # `{ [setTimeout]: h }` evaluates it
    "global_root_scheduler.js": FAIL,      # `window.addEventListener(…)`
    # -- M3.8 defect 1 / M3.9: invoked parameter defaults --------------------
    "iife_default_omitted.js": FAIL,             # argument missing -> default runs
    "iife_default_supplied.js": PASS,            # literal argument -> default dead
    "iife_default_null_argument.js": PASS,       # `null` is not `undefined`
    "iife_default_explicit_undefined.js": FAIL,  # `(…)(undefined)` triggers it
    "iife_default_unprovable.js": FAIL,          # POLICY: unproved -> assume it runs
    "iife_default_spread_argument.js": FAIL,     # a spread abandons the mapping
    "arrow_iife_default.js": FAIL,               # arrow parameters, not the body
    "uncalled_default.js": PASS,                 # never invoked -> never evaluated
    "iife_destructured_default.js": FAIL,        # a default nested in a pattern
    "call_default_omitted.js": FAIL,             # `.call(receiver)` supplies none
    "apply_default_supplied.js": PASS,           # `.apply(r, [1])` maps positions
    "apply_default_unknown_array.js": FAIL,      # `.apply(r, args)` is unprovable
    "bind_default_supplied.js": PASS,            # `.bind(r, 1)` fixes argument 0
    "bind_default_omitted.js": FAIL,             # `.bind(r)` fixes only the receiver
    "bind_argument_expression.js": FAIL,         # bind arguments evaluate at load
    # -- M3.8 defect 2 / M3.9: class evaluation vs construction --------------
    "class_instance_field.js": PASS,             # deferred to `new`
    "class_static_field.js": FAIL,               # runs at class evaluation
    "class_static_block.js": FAIL,               # runs at class evaluation
    "class_computed_field_key.js": FAIL,         # the KEY evaluates when defining
    "class_risky_field_name.js": PASS,           # a field name is a name
    "class_direct_construction.js": FAIL,        # `new (class { … })()`
    "class_constructor_deferred.js": PASS,       # a constructor nobody calls
    "class_direct_construction_ctor.js": FAIL,   # `new` runs the constructor
    "class_method_body_deferred.js": PASS,       # method/accessor bodies defer
    "class_construction_ctor_default.js": FAIL,  # constructor default, omitted
    "class_construction_ctor_supplied.js": PASS,  # constructor default, supplied
    # -- M3.8 defect 3 / M3.9: directly evaluated literal accessors ----------
    "accessor_defined_only.js": PASS,            # defined, never touched
    "accessor_getter_read.js": FAIL,             # `({ get v() {…} }).v`
    "accessor_setter_write.js": FAIL,            # `({ set v(x) {…} }).v = …`
    "accessor_read_skips_setter.js": PASS,       # a read is not a write
    "accessor_write_skips_getter.js": PASS,      # a write is not a read
    "accessor_compound_assignment.js": FAIL,     # `+=` reads AND writes
    "accessor_string_key.js": FAIL,              # the string-literal spelling
    "accessor_data_property.js": PASS,           # a value has no body to run
    "accessor_risky_name.js": PASS,              # a risky accessor NAME, inert body
    "accessor_other_property.js": PASS,          # a different property is touched
    # -- M3.10 defect 1 / M3.11: the source-order descriptor fold -------------
    "accessor_data_shadows_getter.js": PASS,      # later data clears the getter
    "accessor_data_shadows_setter.js": PASS,      # later data clears the setter
    "accessor_getter_shadows_data.js": FAIL,      # later getter replaces data
    "accessor_setter_shadows_data.js": FAIL,      # later setter replaces data
    "accessor_method_shadows_getter.js": PASS,    # a method is a data descriptor
    "accessor_pair_read_runs_getter_only.js": FAIL,   # both halves live, read
    "accessor_pair_write_runs_setter_only.js": FAIL,  # both halves live, write
    "accessor_pair_compound_runs_both.js": FAIL,      # both halves live, `+=`
    "accessor_data_between_accessors_read.js": PASS,  # data cleared the getter
    "accessor_data_between_accessors_write.js": FAIL,  # the later setter is live
    "accessor_spread_after_accessor.js": FAIL,    # POLICY: unresolvable -> assume live
    "accessor_spread_before_accessor.js": FAIL,   # defined after the spread: exact
    "accessor_spread_after_data_property.js": PASS,  # a spread defines only data
    # -- M3.10 defect 2 / M3.11: `delete` invokes no accessor ----------------
    "delete_accessor_getter.js": PASS,            # `delete` is not a read
    "delete_accessor_setter.js": PASS,            # `delete` is not a write
    "delete_computed_key_expression.js": FAIL,    # the KEY still evaluates
    "delete_global_member.js": FAIL,              # `delete window.fetch` still names it
    # -- M3.10 defect 3 / M3.11: numeric/string key canonicalisation ---------
    "accessor_numeric_key_numeric_access.js": FAIL,  # `get 0()` via `[0]`
    "accessor_numeric_key_string_access.js": FAIL,   # `get 0()` via `["0"]`
    "accessor_string_key_numeric_access.js": FAIL,   # `get "0"()` via `[0]`
    "accessor_numeric_setter_write.js": FAIL,        # the write path canonicalises too
    "accessor_numeric_key_unmatched.js": PASS,       # `[1]` is not `"0"`
    "accessor_dynamic_key_still_evaluated.js": FAIL,  # dynamic key, still evaluated
    # -- M3.11 mutation survivors: forms no existing fixture could isolate ----
    "accessor_setter_then_getter_write.js": FAIL,    # a later getter keeps the setter
    "accessor_setter_then_getter_read.js": FAIL,     # the later getter is the read half
    "computed_object_key_reference.js": FAIL,        # `{ [setTimeout]: h }` alone
    "computed_pattern_key_reference.js": FAIL,       # `const { [fetch]: p } = r` alone
    # -- M3.12 repair: source-side spread Get and remaining static keys --------
    "accessor_spread_source_getter.js": FAIL,         # CopyDataProperties invokes it
    "accessor_spread_source_shadowed_getter.js": PASS,  # final source descriptor is data
    "accessor_spread_source_setter.js": PASS,         # a spread Get invokes no setter
    "accessor_nonfinite_key.js": FAIL,                # `1e999` -> "Infinity"
    "accessor_bigint_key.js": FAIL,                   # `1n` -> "1"
    "accessor_negative_zero_key.js": FAIL,            # `-0` -> "0"
    "accessor_template_key.js": FAIL,                 # no-substitution template -> string
    # -- M3.12 mutation survivors: later same-kind definitions ----------------
    "accessor_repeated_getter_later_wins.js": PASS,
    "accessor_repeated_setter_later_wins.js": PASS,
}

# The M3.7 pairs, each a legitimate name/binding position beside the adversarial
# reference it is easiest to confuse it with. Asserted as pairs (below) as well
# as individually, because the failure mode being defended against is a repair
# that moves BOTH verdicts at once -- excluding property keys wholesale fixes the
# left column and breaks the right.
REFERENCE_POSITION_PAIRS = (
    ("object_method_key.js", "shorthand_risky_reference.js"),
    ("object_property_key.js", "computed_key_reference.js"),
    ("class_method_key.js", "computed_key_reference.js"),
    ("local_binding_shadow.js", "shorthand_risky_reference.js"),
    ("object_method_key.js", "global_root_scheduler.js"),
    # -- M3.11: the two computed-key forms, isolated from each other ---------
    # `computed_key_reference.js` bundles both, and they run through DIFFERENT
    # walk paths (the Property branch and `walkPattern`), so a regression in
    # one was hidden by the other still failing the bundled fixture.
    ("object_method_key.js", "computed_object_key_reference.js"),
    ("object_property_key.js", "computed_pattern_key_reference.js"),
)

# The M3.9 terminal truth table: the execution-semantics distinctions the three
# M3.8 defects turned on. Each row is (inert form, executing form) over the SAME
# syntax, so the only difference between the two files is the one decision under
# test. Asserted as pairs as well as individually, because both directions are
# failures: missing the right-hand file is a blind gate, and reporting the
# left-hand file is the false-positive repair the brief rules out.
EXECUTION_SEMANTICS_PAIRS = (
    # a default whose argument is supplied vs one whose argument is missing
    ("iife_default_supplied.js", "iife_default_omitted.js"),
    # `null` is a value; `undefined` is not
    ("iife_default_null_argument.js", "iife_default_explicit_undefined.js"),
    # the same default in a function nobody calls vs in a direct IIFE
    ("uncalled_default.js", "iife_default_omitted.js"),
    # the receiver of `.call`/`.bind` is not an argument
    ("bind_default_supplied.js", "bind_default_omitted.js"),
    ("apply_default_supplied.js", "call_default_omitted.js"),
    # an instance field vs a static field of the same class shape
    ("class_instance_field.js", "class_static_field.js"),
    # an instance field vs the same field under direct construction
    ("class_instance_field.js", "class_direct_construction.js"),
    # a field NAME that spells a global vs a computed field KEY that reads one
    ("class_risky_field_name.js", "class_computed_field_key.js"),
    # a constructor nobody calls vs the same constructor under direct `new`
    ("class_constructor_deferred.js", "class_direct_construction_ctor.js"),
    ("class_construction_ctor_supplied.js", "class_construction_ctor_default.js"),
    # an accessor merely defined vs one directly read
    ("accessor_defined_only.js", "accessor_getter_read.js"),
    # a read does not invoke a setter; a write does
    ("accessor_read_skips_setter.js", "accessor_setter_write.js"),
    # a write does not invoke a getter; a read does
    ("accessor_write_skips_getter.js", "accessor_getter_read.js"),
    # an ordinary data property access vs an accessor invocation
    ("accessor_data_property.js", "accessor_getter_read.js"),
    # a risky accessor NAME with an inert body vs a real accessor invocation
    ("accessor_risky_name.js", "accessor_string_key.js"),
    # touching a different property vs touching the accessor's own
    ("accessor_other_property.js", "accessor_getter_read.js"),
    # -- M3.10 defects / M3.11 repair ---------------------------------------
    # the SAME two definitions in the opposite source order
    ("accessor_data_shadows_getter.js", "accessor_getter_shadows_data.js"),
    ("accessor_data_shadows_setter.js", "accessor_setter_shadows_data.js"),
    # a method establishes a data descriptor just as a value does
    ("accessor_method_shadows_getter.js", "accessor_getter_shadows_data.js"),
    # the intervening data property cleared the getter; the later setter is live
    ("accessor_data_between_accessors_read.js",
     "accessor_data_between_accessors_write.js"),
    # a spread cannot resurrect a cleared accessor, but it cannot be proved to
    # have replaced a live one either
    ("accessor_spread_after_data_property.js", "accessor_spread_after_accessor.js"),
    ("accessor_spread_after_data_property.js", "accessor_spread_before_accessor.js"),
    # `delete` invokes no accessor; an ordinary read of the same literal does
    ("delete_accessor_getter.js", "accessor_getter_read.js"),
    ("delete_accessor_setter.js", "accessor_setter_write.js"),
    # `delete` skips the accessor but NOT the computed key expression
    ("delete_accessor_getter.js", "delete_computed_key_expression.js"),
    # `delete` still names a risky host global
    ("delete_accessor_getter.js", "delete_global_member.js"),
    # a numeric key that matches vs one that does not
    ("accessor_numeric_key_unmatched.js", "accessor_numeric_key_numeric_access.js"),
    ("accessor_numeric_key_unmatched.js", "accessor_numeric_setter_write.js"),
    # a dynamic key resolves no accessor, but is still evaluated
    ("accessor_other_property.js", "accessor_dynamic_key_still_evaluated.js"),
    # a direct spread source invokes its live getter, not a shadowed getter or setter
    ("accessor_spread_source_shadowed_getter.js", "accessor_spread_source_getter.js"),
    ("accessor_spread_source_setter.js", "accessor_spread_source_getter.js"),
    # repeated definitions use the later body, within one accessor kind
    ("accessor_repeated_getter_later_wins.js", "accessor_getter_read.js"),
    ("accessor_repeated_setter_later_wins.js", "accessor_setter_write.js"),
)

# The four spellings of ONE property: `ToPropertyKey(0)` is `"0"`, so a numeric
# and a string key are the same key on both the definition and the access side.
# Every cell must FAIL; a single surviving cell is a bypass. Held separately
# from EXECUTION_SEMANTICS_PAIRS because these do not move against each other --
# they must all move together, which is the point.
NUMERIC_KEY_GRID = (
    "accessor_numeric_key_numeric_access.js",   # `get 0()`   reached by `[0]`
    "accessor_numeric_key_string_access.js",    # `get 0()`   reached by `["0"]`
    "accessor_string_key_numeric_access.js",    # `get "0"()` reached by `[0]`
    "accessor_string_key.js",                   # `get "0"()` reached by `["0"]`
)

# Static ToPropertyKey spellings that M3.11 deliberately declined but M3.12
# proved were not conservative: each direct accessor ran in node while the
# scanner silently resolved no descriptor.
STATIC_KEY_EDGE_CASES = (
    "accessor_nonfinite_key.js",
    "accessor_bigint_key.js",
    "accessor_negative_zero_key.js",
    "accessor_template_key.js",
)

# Which accessor half a given touch invokes, proved by IDENTITY rather than by
# exit code. Each fixture defines a getter that reads `fetch` and a setter that
# reads `document`, over one literal whose descriptor keeps BOTH halves live, so
# the reported name says exactly which body ran. Exit code alone cannot
# distinguish these: all three are MUST FAIL.
DESCRIPTOR_HALF_IDENTITY = {
    "accessor_pair_read_runs_getter_only.js": ["fetch"],
    "accessor_pair_write_runs_setter_only.js": ["document"],
    "accessor_pair_compound_runs_both.js": ["fetch", "document"],
    # The same two halves defined in the OPPOSITE order. Only this order can
    # detect a fold that clears the setter when a getter joins the descriptor;
    # in the other order the setter is written last and survives such a bug by
    # accident. An M3.11 mutation survived the corpus until these existed.
    "accessor_setter_then_getter_write.js": ["document"],
    "accessor_setter_then_getter_read.js": ["fetch"],
}

_HAVE_NODE = shutil.which("node") is not None


def _run_scanner(path: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", _SCANNER, path, *extra],
        capture_output=True, text=True, cwd=_REPO,
    )


def _declared_verdict(path: str) -> str | None:
    """The `MUST PASS` / `MUST FAIL` marker in the fixture's own header."""
    with open(path, encoding="utf-8") as handle:
        for line in handle.read().splitlines()[:6]:
            if PASS in line:
                return PASS
            if FAIL in line:
                return FAIL
    return None


@unittest.skipUnless(_HAVE_NODE, "node is not on PATH")
class ScannerFixtureTests(unittest.TestCase):
    """One assertion per fixture, plus the corpus-level invariants."""

    def test_every_fixture_is_tracked(self):
        """No fixture may exist without a declared expectation, and no
        expectation may name a fixture that is gone. Either would let the
        corpus drift away from what is actually asserted."""
        on_disk = {n for n in os.listdir(_FIXTURES) if n.endswith(".js")}
        self.assertEqual(
            on_disk, set(EXPECTED),
            "fixtures/scanner and EXPECTED disagree; untracked="
            f"{sorted(on_disk - set(EXPECTED))} missing={sorted(set(EXPECTED) - on_disk)}",
        )

    def test_declared_and_expected_verdicts_agree(self):
        for name, expected in sorted(EXPECTED.items()):
            with self.subTest(fixture=name):
                declared = _declared_verdict(os.path.join(_FIXTURES, name))
                self.assertEqual(
                    declared, expected,
                    f"{name} declares {declared!r} in its header but EXPECTED says "
                    f"{expected!r}",
                )

    def test_fixture_verdicts(self):
        for name, expected in sorted(EXPECTED.items()):
            with self.subTest(fixture=name):
                proc = _run_scanner(os.path.join(_FIXTURES, name), "--no-allowlist")
                self.assertNotEqual(
                    proc.returncode, 2,
                    f"{name}: scanner usage/parse error:\n{proc.stderr}",
                )
                if expected == PASS:
                    self.assertEqual(
                        proc.returncode, 0,
                        f"{name} is declared {PASS} but the scanner rejected it:\n"
                        f"{proc.stdout}\n{proc.stderr}",
                    )
                else:
                    self.assertEqual(
                        proc.returncode, 1,
                        f"{name} is declared {FAIL} but the scanner accepted it. A "
                        f"load-reachable risky reference went undetected:\n{proc.stdout}",
                    )

    def test_adversarial_fixtures_actually_report_references(self):
        """A `MUST FAIL` fixture has to fail for the RIGHT reason.

        Exit 1 alone is too weak: a scanner that reported zero references and
        failed on some unrelated complaint would satisfy the verdict test while
        detecting nothing. Every adversarial fixture must show at least one
        load-reachable risky reference.
        """
        for name, expected in sorted(EXPECTED.items()):
            if expected != FAIL:
                continue
            with self.subTest(fixture=name):
                proc = _run_scanner(os.path.join(_FIXTURES, name), "--no-allowlist")
                marker = "load-reachable risky references: "
                line = next(
                    (l for l in proc.stdout.splitlines() if l.startswith(marker)), None
                )
                self.assertIsNotNone(line, f"{name}: no reference count in output")
                self.assertGreater(
                    int(line[len(marker):].strip()), 0,
                    f"{name}: scanner found no risky references at all",
                )
                self.assertIn("UNGUARDED", proc.stdout + proc.stderr)

    def test_legitimate_fixtures_are_proved_not_merely_unseen(self):
        """`guarded_*` must exit 0 because the guard analysis PROVED the
        references inert -- not because it failed to see them. This is the
        assertion that dies if a guard-analysis branch is deleted or weakened:
        without the analysis the references are still found, but they come back
        UNGUARDED instead of proved-guarded."""
        for name in ("guarded_typeof_positive.js", "guarded_typeof_return.js"):
            with self.subTest(fixture=name):
                proc = _run_scanner(os.path.join(_FIXTURES, name), "--no-allowlist")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("proved-guarded", proc.stdout)
                self.assertIn("proved guarded by", proc.stdout)

    # -- M3.6 defect / M3.7 repair: references vs names ---------------------

    _NAME_POSITION_FIXTURES = (
        "object_method_key.js",
        "object_property_key.js",
        "local_binding_shadow.js",
        "class_method_key.js",
    )

    def test_name_positions_report_zero_references(self):
        """The legitimate reference-position fixtures must pass because the
        scanner found NOTHING to report -- not because a hit was excused.

        Exit 0 on its own is too weak here: a hit that the allow-list or the
        guard analysis waved through would also exit 0 while the false positive
        remained. The assertion is on the count, and on the absence of the name
        from the report entirely.
        """
        for name in self._NAME_POSITION_FIXTURES:
            with self.subTest(fixture=name):
                proc = _run_scanner(os.path.join(_FIXTURES, name), "--no-allowlist")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(
                    "load-reachable risky references: 0", proc.stdout,
                    f"{name}: a name/binding position was counted as a reference:\n"
                    f"{proc.stdout}",
                )
                self.assertNotIn("UNGUARDED", proc.stdout + proc.stderr)

    def test_shorthand_and_computed_keys_are_still_references(self):
        """The two positions an over-broad key exclusion would swallow.

        `{ setTimeout }` reads the identifier through the property's VALUE;
        `{ [setTimeout]: h }` reads it through a computed KEY. Both must be
        reported by name, so a repair cannot satisfy the legitimate fixtures by
        skipping every property key.
        """
        expected = {
            "shorthand_risky_reference.js": ("setTimeout", "addEventListener"),
            "computed_key_reference.js": ("setTimeout", "fetch"),
        }
        for name, names in expected.items():
            with self.subTest(fixture=name):
                proc = _run_scanner(os.path.join(_FIXTURES, name), "--no-allowlist", "--json")
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                report = json.loads(proc.stdout)
                reported = {hit["name"] for hit in report["hits"]}
                self.assertEqual(
                    reported, set(names),
                    f"{name}: expected {sorted(names)} to be reported, got "
                    f"{sorted(reported)}",
                )
                for hit in report["hits"]:
                    self.assertEqual(hit["verdict"], "UNGUARDED", hit)
                    self.assertEqual(hit["reach"], "top", hit)

    def test_reference_position_pairs_do_not_move_together(self):
        """Each legitimate name position, checked beside the reference it is
        easiest to confuse it with. The defect being pinned is a repair that
        moves both verdicts at once: exclude property keys without testing
        `computed`/`shorthand` and the left column passes while the right one
        silently starts passing too."""
        for legit, adversarial in REFERENCE_POSITION_PAIRS:
            with self.subTest(legitimate=legit, adversarial=adversarial):
                good = _run_scanner(os.path.join(_FIXTURES, legit), "--no-allowlist")
                bad = _run_scanner(os.path.join(_FIXTURES, adversarial), "--no-allowlist")
                self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
                self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)

    # -- M3.8 defects / M3.9 repair: execution semantics --------------------

    _INERT_EXECUTION_FIXTURES = (
        "iife_default_supplied.js",
        "iife_default_null_argument.js",
        "uncalled_default.js",
        "apply_default_supplied.js",
        "bind_default_supplied.js",
        "class_instance_field.js",
        "class_risky_field_name.js",
        "class_constructor_deferred.js",
        "class_method_body_deferred.js",
        "class_construction_ctor_supplied.js",
        "accessor_defined_only.js",
        "accessor_read_skips_setter.js",
        "accessor_write_skips_getter.js",
        "accessor_data_property.js",
        "accessor_risky_name.js",
        "accessor_other_property.js",
        # -- M3.11: descriptor fold, `delete`, key canonicalisation ----------
        "accessor_data_shadows_getter.js",
        "accessor_data_shadows_setter.js",
        "accessor_method_shadows_getter.js",
        "accessor_data_between_accessors_read.js",
        "accessor_spread_after_data_property.js",
        "delete_accessor_getter.js",
        "delete_accessor_setter.js",
        "accessor_numeric_key_unmatched.js",
        # -- M3.12: CopyDataProperties and repeated same-kind definitions ----
        "accessor_spread_source_shadowed_getter.js",
        "accessor_spread_source_setter.js",
        "accessor_repeated_getter_later_wins.js",
        "accessor_repeated_setter_later_wins.js",
    )

    def test_inert_execution_forms_report_zero_references(self):
        """The MUST PASS half of the truth table must pass because the scanner
        found NOTHING to report.

        Exit 0 alone is too weak: a hit excused by the guard analysis or by an
        allow-list entry would also exit 0 while the false positive survived.
        Each of these files contains a risky NAME in a position that does not
        execute, so the correct count is exactly zero.
        """
        for name in self._INERT_EXECUTION_FIXTURES:
            with self.subTest(fixture=name):
                proc = _run_scanner(os.path.join(_FIXTURES, name), "--no-allowlist")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(
                    "load-reachable risky references: 0", proc.stdout,
                    f"{name}: a form that does not execute at load was counted:\n"
                    f"{proc.stdout}",
                )
                self.assertNotIn("UNGUARDED", proc.stdout + proc.stderr)

    def test_execution_semantics_pairs_do_not_move_together(self):
        """Each inert form beside the executing form it is easiest to confuse it
        with. The failure mode being pinned is a repair that moves both verdicts
        at once: clearing reach for every class body fixes the instance-field
        false positive and silences static fields; walking every parameter
        default fixes the missing-argument miss and starts flagging dead ones."""
        for inert, executing in EXECUTION_SEMANTICS_PAIRS:
            with self.subTest(inert=inert, executing=executing):
                good = _run_scanner(os.path.join(_FIXTURES, inert), "--no-allowlist")
                bad = _run_scanner(os.path.join(_FIXTURES, executing), "--no-allowlist")
                self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
                self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)

    def test_executed_forms_report_the_expected_reference(self):
        """Every executing form must fail naming the RIGHT global, at the right
        reach. Exit 1 is not enough on its own -- a scanner that failed for an
        unrelated reason, or that reported the accessor's *name* rather than the
        call inside its body, would satisfy the verdict test while detecting
        nothing real.

        `bind_argument_expression.js` is the one case whose reach is `top`: a
        bind argument is evaluated in the caller, not inside the invoked body.
        """
        expected = {
            "iife_default_omitted.js": ("fetch", "iife"),
            "iife_default_explicit_undefined.js": ("fetch", "iife"),
            "iife_default_unprovable.js": ("fetch", "iife"),
            "iife_default_spread_argument.js": ("fetch", "iife"),
            "arrow_iife_default.js": ("document", "iife"),
            "iife_destructured_default.js": ("fetch", "iife"),
            "call_default_omitted.js": ("fetch", "iife"),
            "apply_default_unknown_array.js": ("fetch", "iife"),
            "bind_default_omitted.js": ("fetch", "iife"),
            "bind_argument_expression.js": ("fetch", "top"),
            "class_static_field.js": ("fetch", "top"),
            "class_static_block.js": ("setTimeout", "top"),
            "class_computed_field_key.js": ("fetch", "top"),
            "class_direct_construction.js": ("fetch", "top"),
            "class_direct_construction_ctor.js": ("fetch", "top"),
            "class_construction_ctor_default.js": ("fetch", "top"),
            "accessor_getter_read.js": ("fetch", "top"),
            "accessor_setter_write.js": ("fetch", "top"),
            "accessor_compound_assignment.js": ("fetch", "top"),
            "accessor_string_key.js": ("fetch", "top"),
            # -- M3.11 -------------------------------------------------------
            "accessor_getter_shadows_data.js": ("fetch", "top"),
            "accessor_setter_shadows_data.js": ("fetch", "top"),
            "accessor_data_between_accessors_write.js": ("document", "top"),
            "accessor_spread_after_accessor.js": ("fetch", "top"),
            "accessor_spread_before_accessor.js": ("fetch", "top"),
            "accessor_numeric_key_numeric_access.js": ("fetch", "top"),
            "accessor_numeric_key_string_access.js": ("fetch", "top"),
            "accessor_string_key_numeric_access.js": ("fetch", "top"),
            "accessor_numeric_setter_write.js": ("fetch", "top"),
            "accessor_dynamic_key_still_evaluated.js": ("fetch", "top"),
            "delete_computed_key_expression.js": ("fetch", "top"),
            "delete_global_member.js": ("fetch", "top"),
            # -- M3.12 -------------------------------------------------------
            "accessor_spread_source_getter.js": ("fetch", "top"),
            "accessor_nonfinite_key.js": ("fetch", "top"),
            "accessor_bigint_key.js": ("fetch", "top"),
            "accessor_negative_zero_key.js": ("fetch", "top"),
            "accessor_template_key.js": ("fetch", "top"),
        }
        for name, (identifier, reach) in sorted(expected.items()):
            with self.subTest(fixture=name):
                proc = _run_scanner(
                    os.path.join(_FIXTURES, name), "--no-allowlist", "--json"
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual(
                    [(h["name"], h["reach"], h["verdict"]) for h in report["hits"]],
                    [(identifier, reach, "UNGUARDED")],
                    f"{name}: unexpected hit set {report['hits']}",
                )

    # -- M3.10 defects / M3.11 repair: descriptor, delete, key -------------

    def test_descriptor_halves_are_invoked_independently(self):
        """Which accessor half ran, proved by the NAME reported.

        All three fixtures are MUST FAIL over the same literal, so the verdict
        test cannot tell them apart. The getter reads `fetch` and the setter
        reads `document`, so the reported identity is the evidence: a read must
        invoke the getter alone, a write the setter alone, and a compound
        assignment both, getter first.

        This is the assertion that dies if the descriptor fold stops preserving
        the untouched half -- e.g. if adding a setter cleared a live getter, the
        compound case would report only `document`.
        """
        for name, expected in sorted(DESCRIPTOR_HALF_IDENTITY.items()):
            with self.subTest(fixture=name):
                proc = _run_scanner(
                    os.path.join(_FIXTURES, name), "--no-allowlist", "--json"
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual(
                    [h["name"] for h in report["hits"]], expected,
                    f"{name}: the wrong accessor half ran -- hits {report['hits']}",
                )
                for hit in report["hits"]:
                    self.assertEqual(hit["verdict"], "UNGUARDED", hit)
                    self.assertEqual(hit["reach"], "top", hit)

    def test_static_keys_resolve_their_direct_accessor(self):
        """Static ToPropertyKey spellings must resolve one direct accessor.

        `ToPropertyKey(0)` is `"0"`, so all four definition/access spellings
        are the same property and every one must be detected.

        M3.10 defect 3 was an asymmetry between the two key helpers: the
        definition side canonicalised numbers and the access side did not, so
        two of these four cells silently resolved to no accessor at all. A
        surviving cell is a bypass, not a rounding error, so the grid is
        asserted as a whole rather than one representative case.

        M3.12 then proved that `1e999`, BigInt, unary `-0` and a static template
        also fell to silence. They use the same shared helper and are asserted
        here as direct static-key cases, not general dynamic evaluation.
        """
        for name in NUMERIC_KEY_GRID + STATIC_KEY_EDGE_CASES:
            with self.subTest(fixture=name):
                proc = _run_scanner(
                    os.path.join(_FIXTURES, name), "--no-allowlist", "--json"
                )
                self.assertEqual(
                    proc.returncode, 1,
                    f"{name}: a numeric/string key spelling resolved to no "
                    f"accessor -- the gate is blind to it:\n{proc.stdout}",
                )
                report = json.loads(proc.stdout)
                self.assertEqual(
                    [(h["name"], h["reach"]) for h in report["hits"]],
                    [("fetch", "top")], f"{name}: unexpected hits {report['hits']}",
                )

    def test_delete_invokes_no_accessor_but_still_evaluates_the_key(self):
        """`delete` is a fourth property-touch mode, not a read.

        Deleting an accessor property removes a descriptor; it invokes neither
        half. But it still evaluates the base object and a computed key
        expression, and it still names a risky host global. Asserted together
        because the failure mode of an over-broad repair is to suppress the
        whole member walk under `delete` and blind the last two.
        """
        for name in ("delete_accessor_getter.js", "delete_accessor_setter.js"):
            with self.subTest(fixture=name, expect="no accessor invoked"):
                proc = _run_scanner(os.path.join(_FIXTURES, name), "--no-allowlist")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(
                    "load-reachable risky references: 0", proc.stdout,
                    f"{name}: `delete` invoked an accessor:\n{proc.stdout}",
                )
        for name in ("delete_computed_key_expression.js", "delete_global_member.js"):
            with self.subTest(fixture=name, expect="still visible"):
                proc = _run_scanner(
                    os.path.join(_FIXTURES, name), "--no-allowlist", "--json"
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual(
                    [(h["name"], h["reach"], h["verdict"]) for h in report["hits"]],
                    [("fetch", "top", "UNGUARDED")],
                    f"{name}: `delete` suppressed something it must not "
                    f"-- hits {report['hits']}",
                )

    def test_allowlist_pin_is_still_the_default(self):
        """The fixture mode must not have relaxed the real gate: WITHOUT
        `--no-allowlist`, a file that is not the audited prefix is still
        rejected on the pin, exactly as before."""
        proc = _run_scanner(os.path.join(_FIXTURES, "uncalled_function.js"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("allow-list was audited against prefix sha256", proc.stderr)


if __name__ == "__main__":
    unittest.main()
