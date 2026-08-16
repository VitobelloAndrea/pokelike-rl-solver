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

    def _corpus(self, entries: dict[str, bytes], manifest: list[tuple[str, bytes]]):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        for name, payload in entries.items():
            (root / name).write_bytes(payload)
        lines = [f"{self._digest(payload)}  {name}" for name, payload in manifest]
        (root / "manifest.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
