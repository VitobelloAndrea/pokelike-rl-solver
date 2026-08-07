"""R5: the standing JS/DOM-shim detector.

R2, R3 and R4 each built a throwaway Node DOM shim in scratch space, used it to
execute the real `app.js`, found a real bug with it (N5's own discovery method;
R3's double-dispatch listener bug), and then discarded it. Every one of those
reports named the same follow-up: a CHECKED-IN detector that executes `app.js`
as part of the ordinary test run, so a regression in the web renderer -- as
opposed to the Python contract layer, which the 110+ detectors in
`test_renderer_contract.py` already pin hard -- does not require a human to
think to build a shim before it is found.

This is that detector. The division of labour:

* `pokelike/tests/dom_shim/shim.js` -- a minimal hand-rolled DOM over Node's built-in
  `vm`. No `jsdom`/`puppeteer`/`playwright` dependency: three sessions running
  proved the minimal approach sufficient.
* `pokelike/tests/dom_shim/detectors.js` -- the assertions. Executes the REAL `app.js`
  against the REAL `index.html` element set.
* this module -- generates the fixtures from the REAL
  `pokelike.render.contract.observation()`, so the payloads `app.js` is driven
  with cannot drift from the payloads the server actually sends, and runs the
  suite as an ordinary `unittest` so `unittest discover -s .` picks it up with
  no separate command to remember.

If `node` is not on PATH the suite SKIPS rather than fails -- but
`test_the_detector_files_are_present_and_non_trivial` still runs, so the shim
being deleted is caught even on a machine with no Node.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pokelike import engine
from pokelike.render import contract, play

_REPO = pathlib.Path(__file__).resolve().parents[2]
# Deliberately NOT under `tools/`: the whole `tools/` tree is gitignored
# (.gitignore:11), so a "standing" detector placed there would vanish on a
# clean clone while this tracked test kept requiring it. See R5's record.
_SHIM_DIR = pathlib.Path(__file__).resolve().parent / "dom_shim"
_DETECTORS = _SHIM_DIR / "detectors.js"
_SHIM = _SHIM_DIR / "shim.js"


def _node() -> str | None:
    return shutil.which("node")


def _observation_fixtures() -> dict[str, dict]:
    """One real `contract.observation()` per distinct phase we can reach, plus
    a couple of extra `on_map` states so the map detectors see more than one
    map shape.

    Deterministic: the engine is seeded, and `play._choose_action` draws from
    the global `random`, which is seeded and restored around the sweep.
    """
    import random

    fixtures: dict[str, dict] = {}
    maps_captured = 0
    teams_captured = 0
    saved = random.getstate()
    try:
        for seed in range(1, 25):
            for i, cfg in enumerate(({}, {"gen4_mode": True}, {"nuzlocke_mode": True})):
                random.seed(seed * 977 + i)
                eng = engine.Engine()
                state = eng.reset(seed=seed, **cfg)
                steps = 0
                while state.phase not in (engine.Phase.GAME_OVER, engine.Phase.VICTORY) and steps < 400:
                    phase = state.phase.value
                    obs = None
                    if phase not in fixtures:
                        obs = contract.observation(state)
                        fixtures[phase] = obs
                    # The map detectors need a state with at least one VISITED
                    # node (N5 is about visited nodes) -- the first `on_map`
                    # observation of a run has none, so keep collecting.
                    if phase == "on_map" and maps_captured < 3:
                        obs = obs if obs is not None else contract.observation(state)
                        if obs.get("map") and any(n["dimmed"] for n in obs["map"]["nodes"]):
                            if any(not n["sprite_url"] for n in obs["map"]["nodes"] if n["dimmed"]):
                                maps_captured += 1
                                fixtures[f"on_map_visited_{maps_captured}"] = obs
                    # The team-bar drag handlers only attach when a reorder is
                    # legal, which needs at least two members -- so a run's
                    # opening one-member states cannot exercise them.
                    if phase == "on_map" and teams_captured < 2 and len(state.team) >= 2:
                        obs = obs if obs is not None else contract.observation(state)
                        teams_captured += 1
                        fixtures[f"on_map_team{len(state.team)}_{teams_captured}"] = obs
                    steps += 1
                    state = eng.step(play._choose_action(state, interactive=False))
                phase = state.phase.value
                fixtures.setdefault(phase, contract.observation(state))
    finally:
        random.setstate(saved)
    return fixtures


class DomShimDetectorTests(unittest.TestCase):
    """The suite runs as ONE test: `detectors.js` reports every detector's
    result, and its stdout is attached to the failure so a break names itself.
    """

    def test_the_detector_files_are_present_and_non_trivial(self):
        """Runs even without Node -- deleting the shim must not go unnoticed."""
        for path in (_SHIM, _DETECTORS):
            self.assertTrue(path.is_file(), f"{path} is missing")
            text = path.read_text(encoding="utf-8")
            self.assertGreater(len(text), 2000, f"{path} is too small to be the real thing")
        detectors = _DETECTORS.read_text(encoding="utf-8")
        # It must execute the real app.js, not a copy or a mock.
        self.assertIn("'app.js'", detectors)
        self.assertIn("vm.runInContext", detectors)
        self.assertIn("index.html", detectors)
        # And it must still be checking the things it was built to check.
        for token in ("N5:", "listenerCount", "Unhandled phase"):
            self.assertIn(token, detectors, f"detectors.js no longer mentions {token}")

    def test_fixture_generation_reaches_a_visited_circle_branch_node(self):
        """Non-vacuity for the N5 detector, asserted on the PYTHON side: if the
        sweep stops producing a state with a visited node that has no sprite
        (the circle branch N5 was reported against), the JS detector would pass
        by checking nothing. `detectors.js` asserts this too; this makes the
        failure legible without Node."""
        fixtures = _observation_fixtures()
        maps = [s for s in fixtures.values() if s["phase"] == "on_map" and s.get("map")]
        self.assertTrue(maps, "no on_map fixture generated")
        visited_circle = [
            n for s in maps for n in s["map"]["nodes"] if n["dimmed"] and not n["sprite_url"]
        ]
        self.assertTrue(
            visited_circle,
            "no fixture reached a VISITED circle-branch node -- the N5 detector would be vacuous",
        )

    @unittest.skipIf(_node() is None, "node is not on PATH")
    def test_app_js_passes_every_dom_shim_detector(self):
        fixtures = _observation_fixtures()
        self.assertGreaterEqual(len(fixtures), 3, "too few phases reached to drive the shim")
        with tempfile.TemporaryDirectory(prefix="pokelike-domshim-") as tmp:
            for name, payload in fixtures.items():
                (pathlib.Path(tmp) / f"{name}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
            proc = subprocess.run(
                [_node(), str(_DETECTORS), tmp],
                capture_output=True, text=True, cwd=str(_REPO), timeout=300,
            )
        self.assertEqual(
            0, proc.returncode,
            "DOM-shim detectors failed:\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        )
        # Non-vacuity: a harness that ran nothing must not read as a pass.
        self.assertIn("DOM-shim detectors passed.", proc.stdout)
        passed, _, total = proc.stdout.rsplit("\n", 2)[-2].split()[0].partition("/")
        self.assertGreaterEqual(int(total), 8, "suspiciously few detectors ran")
        self.assertEqual(passed, total)


if __name__ == "__main__":
    unittest.main()
