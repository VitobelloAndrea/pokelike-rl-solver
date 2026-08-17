"""N57: the battle-oracle corpus must not pass from a truncated checkout."""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_COMPARE = _ROOT / "tools" / "battle-oracle" / "compare.py"
_SPEC = importlib.util.spec_from_file_location("battle_oracle_compare", _COMPARE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class BattleFixtureManifestTests(unittest.TestCase):
    @staticmethod
    def _digest(payload: bytes) -> str:
        path_data = __import__("json").loads(payload.decode("utf-8"))
        canonical = __import__("json").dumps(
            path_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _corpus(
        self,
        entries: dict[str, bytes],
        manifest: list[tuple[str, bytes]],
        raw_manifest: str | None = None,
    ):
        """`raw_manifest` writes the manifest bytes verbatim instead of building
        them from `manifest`. Needed for the two structural invariants below: a
        duplicate line cannot be expressed as a `{name: payload}` mapping, and an
        empty manifest must be zero bytes rather than the single `"\\n"` the
        normal join would emit (which is a malformed LINE, a different error)."""
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        for name, payload in entries.items():
            (root / name).write_bytes(payload)
        if raw_manifest is None:
            lines = [f"{self._digest(payload)}  {name}" for name, payload in manifest]
            raw_manifest = "\n".join(lines) + "\n"
        (root / "manifest.sha256").write_text(raw_manifest, encoding="utf-8")
        return temporary, root

    def test_repository_manifest_pins_all_44_fixtures(self):
        paths = _MODULE._manifest_fixture_paths(_COMPARE.parent / "fixtures")
        self.assertEqual(len(paths), 44)
        self.assertEqual([path.name for path in paths], sorted(path.name for path in paths))

    def test_missing_fixture_fails(self):
        temporary, root = self._corpus({}, [("missing.json", b"{}")])
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(RuntimeError, r"missing=.*missing\.json"):
            _MODULE._manifest_fixture_paths(root)

    def test_unlisted_fixture_fails(self):
        temporary, root = self._corpus(
            {"listed.json": b"{}", "extra.json": b"{}"},
            [("listed.json", b"{}")],
        )
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(RuntimeError, r"unexpected=.*extra\.json"):
            _MODULE._manifest_fixture_paths(root)

    def test_hash_drift_fails(self):
        temporary, root = self._corpus(
            {"changed.json": b'{"changed": true}'},
            [("changed.json", b'{"changed": false}')],
        )
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(RuntimeError, r"hash_mismatch=.*changed\.json"):
            _MODULE._manifest_fixture_paths(root)

    def test_duplicate_manifest_entry_fails(self):
        """`compare.py:79`. Duplicate names collapse to a single dict key, so
        neither the 44-count assertion above nor the missing/unexpected/changed
        comparison can see them -- deleting this rejection leaves every other
        check green. That is the N57.1 mutant-4 gap.

        Both shapes are asserted. The verbatim repeat is the harmless-looking
        one; the same name with a CONFLICTING digest is the dangerous one, since
        without the rejection the later line silently overwrites the earlier and
        the corpus ends up pinned to whichever order the manifest happens to be
        written in."""
        payload = b'{"listed": true}'
        digest = self._digest(payload)
        conflicting = self._digest(b'{"listed": false}')

        for label, second in (("verbatim repeat", digest),
                              ("conflicting digest", conflicting)):
            with self.subTest(duplicate=label):
                temporary, root = self._corpus(
                    {"listed.json": payload},
                    [],
                    raw_manifest=f"{digest}  listed.json\n{second}  listed.json\n",
                )
                self.addCleanup(temporary.cleanup)
                with self.assertRaisesRegex(
                    RuntimeError, r"2: duplicate fixture listed\.json"
                ):
                    _MODULE._manifest_fixture_paths(root)

    def test_empty_manifest_fails(self):
        """`compare.py:101`. A manifest that pins nothing would let `--all`
        report success having compared zero fixtures.

        Reachable only when the fixtures directory is empty as well: with any
        fixture present the unexpected-file branch raises first. So the empty
        corpus is precisely the case that exercises this raise, and it is the
        N57.1 mutant-5 detector."""
        temporary, root = self._corpus({}, [], raw_manifest="")
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(RuntimeError, r"battle-fixture manifest is empty"):
            _MODULE._manifest_fixture_paths(root)


if __name__ == "__main__":
    unittest.main()
