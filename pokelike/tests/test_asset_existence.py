"""R7/N27 + N47: every local image this renderer references must actually
exist -- and must exist IN THE INDEX, not merely on this workstation.

Why a whole module for this. Two separate failures motivated it:

* **N27.** The renderer emits and references image URLs from four different
  places -- `render/contract.py` (Pokemon sprites, node sprites, item icons),
  `index.html` (`src` attributes), `main.css` (`url(...)` backgrounds) and
  `app.js` (template-built sprite and item-icon paths). A broken path in any of
  them degrades silently in a browser: a missing PNG is an alt-text stub, not
  an error, so nothing fails and nobody notices.
* **N47.** `pokelike/data/`, `tools/` and 54 images were un-ignored by M6 and
  then never added, so three audits' worth of "survives a clean clone" claims
  were unproven. A file that exists here but is absent from the index is
  exactly as broken, for anyone else, as a file that does not exist.

So each referenced asset is checked TWICE: present on disk, and tracked by git.

**A detector that only scanned hard-coded HTML `src` attributes would be
insufficient** (R7 §7.4 says so explicitly, and it is right): the two largest
reference families -- 722 Pokemon sprites and 39 item icons -- are built by
string construction at runtime and appear in no markup at all. Both are
enumerated here from the same data the renderer builds them from.

**Documented absences are explicit, not swept up.** R6/N42 established that a
class of artwork cannot be sourced from this repository and that pokelike.xyz
must not be scraped; those paths have working, source-derived fallbacks. They
are listed one by one below WITH the fallback that covers each, so the
allowlist is a statement about known gaps rather than a way to make the test
pass. Anything referenced and absent that is NOT on that list fails.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

from pokelike import data
from pokelike.render import contract

_REPO = pathlib.Path(__file__).resolve().parents[2]
_STATIC = _REPO / "pokelike" / "webui" / "static"
_IMG = _STATIC / "img"


# ---------------------------------------------------------------------------
# R6/N42 -- the artwork this mirror genuinely does not contain.
#
# Each entry names the fallback that makes its absence survivable. These are
# NOT excused failures: they are references whose absence is already handled by
# ported behaviour, and the fallback is what this test is really asserting
# exists. Adding to this list without a fallback would be defeating the test.
# ---------------------------------------------------------------------------
_DOCUMENTED_ABSENT = {
    "img/physical.png":
        "move category icon; `appendMoveBlock`'s onerror swaps in the source's "
        "own `.move-cat-physical` text badge (main.css:1287)",
    "img/special.png":
        "move category icon; same onerror fallback via `.move-cat-special`",
    "img/regions/kanto.jpg": "region background; the region card renders without one",
    "img/regions/johto.jpg": "region background; the region card renders without one",
    "img/regions/hoenn.jpg": "region background; the region card renders without one",
    "img/regions/sinnoh.jpg": "region background; the region card renders without one",
    # R7/N48 -- found BY this detector on its first run, which is the point of
    # building it. 40 item ids, 39 icons on disk. `loaded_dice` is the one
    # miss, and it is a different shape from the other 39: the source's own
    # ITEM_POOL gives it `iconUrl: "img/items/loaded-dice.png"`, a LOCAL path
    # into an `img/items/` directory this mirror does not contain at all --
    # whereas every other item resolves through `_POKEAPI_ITEM_BASE`. So it is
    # the same N42 class (artwork absent from this mirror), not a fetch that
    # was skipped. The fallback is ported and already detector-covered:
    # "N33: a missing item sprite falls back to the emoji the contract
    # carries". Recorded as N48 rather than silently allowlisted.
    "img/items/loaded-dice.png":
        "the source's own local iconUrl for loaded_dice; `img/items/` is absent "
        "from this mirror. Falls back to the contract's emoji (R6/N33)",
    "img/sprites/items/loaded-dice.png":
        "app.js's local fallback path for the same item; same absence, same "
        "emoji fallback",
}

#: Node/trainer sprite prefixes. `pokelike_forked/img/sprites/` contains only
#: `pokemon/` (contract.py:503-504), so every `g1/`-`g4/` path and the two
#: node icons are absent by the same N42 decision. The renderer's fallback is
#: the source's own circle-branch node, which `contract.node_view` already
#: reports via a null `sprite_url` -- and which a standing detector covers
#: ("N5: a visited circle-branch node ...").
_ABSENT_SPRITE_PREFIXES = (
    "img/sprites/g1/", "img/sprites/g2/", "img/sprites/g3/", "img/sprites/g4/",
)

#: The node icons that live directly under `img/sprites/`, enumerated from the
#: projection rather than guessed (`item-icon`, `move-tutor`, `poke-center`,
#: `question-mark`, `trade-icon`). Same N42 absence, same fallback: a null/
#: missing `sprite_url` renders the source's own circle-branch node, which the
#: standing "N5" DOM detector already covers.
_ABSENT_NODE_ICONS = {
    "img/sprites/item-icon.png",
    "img/sprites/move-tutor.png",
    "img/sprites/poke-center.png",
    "img/sprites/question-mark.png",
    "img/sprites/trade-icon.png",
}

#: The two families under `img/sprites/` that MUST exist and must never be
#: excused by the node-icon rule above. Node icons are one path segment deep
#: (`img/sprites/x.png`); these are two (`img/sprites/pokemon/1.png`), which is
#: what keeps the two rules from overlapping.
_REQUIRED_SPRITE_PREFIXES = ("img/sprites/pokemon/", "img/sprites/items/")


def _is_documented_absent(rel: str) -> bool:
    if rel.startswith(_REQUIRED_SPRITE_PREFIXES):
        # Explicitly first: a Pokemon or item sprite is never excused by the
        # node-icon rule, whatever it is named.
        return rel in _DOCUMENTED_ABSENT
    return (
        rel in _DOCUMENTED_ABSENT
        or rel in _ABSENT_NODE_ICONS
        or rel.startswith(_ABSENT_SPRITE_PREFIXES)
    )


def _normalise(url: str) -> str | None:
    """A local, repo-relative `img/...` path, or None for anything remote."""
    if not url or url.startswith(("http://", "https://", "data:", "//")):
        return None
    url = url.split("?", 1)[0].split("#", 1)[0]
    url = url.lstrip("/")
    while url.startswith("../"):
        url = url[3:]
    return url if url.startswith("img/") else None


def _tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", "pokelike/webui/static"],
        cwd=str(_REPO), capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise unittest.SkipTest("git ls-files failed: %s" % out.stderr.strip())
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


class AssetReferenceCollectionTests(unittest.TestCase):
    """The collectors themselves, checked before anything is concluded from
    them. A collector that silently returned nothing would turn every test
    below green while checking nothing -- the exact vacuity R6 hit twice."""

    def test_html_references_are_found(self):
        refs = collect_html_references()
        self.assertGreaterEqual(len(refs), 4, f"only found {len(refs)} HTML image refs")
        self.assertIn("img/logo.png", refs)

    def test_css_references_are_found(self):
        refs = collect_css_references()
        self.assertGreaterEqual(len(refs), 3, f"only found {len(refs)} CSS image refs")
        self.assertIn("img/background.png", refs)

    def test_js_references_are_found(self):
        refs = collect_js_references()
        self.assertGreaterEqual(len(refs), 2, f"only found {len(refs)} JS image refs")
        self.assertIn("img/physical.png", refs)

    def test_dynamic_pokemon_sprites_are_enumerated(self):
        refs = collect_pokemon_sprite_references()
        self.assertGreater(len(refs), 100,
                           "the pokedex sprite family was not enumerated -- this is the "
                           "largest reference family and appears in no markup")

    def test_dynamic_item_icons_are_enumerated(self):
        refs = collect_item_icon_references()
        self.assertGreater(len(refs), 10,
                           "the item-icon family was not enumerated")


def collect_html_references() -> set[str]:
    text = (_STATIC / "index.html").read_text(encoding="utf-8")
    out = set()
    for m in re.finditer(r'(?:src|href)\s*=\s*"([^"]+)"', text):
        rel = _normalise(m.group(1))
        if rel:
            out.add(rel)
    return out


def collect_css_references() -> set[str]:
    text = (_STATIC / "style" / "main.css").read_text(encoding="utf-8")
    out = set()
    for m in re.finditer(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", text):
        rel = _normalise(m.group(1))
        if rel:
            out.add(rel)
    return out


def collect_js_references() -> set[str]:
    """Literal `/img/...` paths in `app.js`, INCLUDING the ones assembled by
    concatenation. `'/img/' + catKey + '.png'` cannot be read as a literal, so
    the two values `catKey` can take are expanded from the same
    `MOVE_CAT_LABEL` table the client keys on."""
    text = (_STATIC / "js" / "app.js").read_text(encoding="utf-8")
    out = set()
    # Plain literals, in quotes or template strings, skipping comment prose by
    # requiring the path to start a quoted run.
    for m in re.finditer(r"""['"`](/img/[^'"`$]*\.(?:png|jpg|jpeg|gif|svg|webp))['"`]""", text):
        rel = _normalise(m.group(1))
        if rel:
            out.add(rel)
    # `'/img/' + catKey + '.png'` -- the move-category icons.
    if "'/img/' + catKey + '.png'" in text:
        out.update({"img/physical.png", "img/special.png"})
    return out


def collect_pokemon_sprite_references() -> set[str]:
    """`spriteUrl(speciesId, shiny)` (app.js:363) builds
    `/img/sprites/pokemon/<id>.png` and `/img/sprites/pokemon/shiny/<id>.png`
    for every species the pokedex can project -- and `contract._sprite_url`
    reads the same pokedex entries server-side. Enumerated from the pokedex
    itself so the check tracks the real data rather than a sampled guess."""
    out = set()
    for entry in data.get_pokedex().values():
        for url in (entry.sprite_url, entry.shiny_sprite_url):
            rel = _normalise(url or "")
            if rel:
                out.add(rel)
    return out


def collect_item_icon_references() -> set[str]:
    """app.js:993 falls back to `/img/sprites/items/<id with _ as ->.png`
    whenever the contract's remote `icon_url` fails. That fallback is the
    reason the 39 files in `img/sprites/items/` were fetched at all, so it is
    the local reference that has to exist."""
    out = set()
    for item_id in _known_item_ids():
        out.add("img/sprites/items/%s.png" % str(item_id).replace("_", "-"))
    return out


def _known_item_ids():
    """Both ported item tables. `get_usable_items` and `get_passive_items` are
    genuinely separate sources (data.py:326-349) and the bag renders from both,
    so an icon check that read only one would miss half the family."""
    ids = []
    for item in data.get_usable_items() + data.get_passive_items():
        ids.append(item.id)
    return ids


class EmittedAssetExistenceTests(unittest.TestCase):
    """The detector proper."""

    def _check(self, refs, label):
        """Every reference must be on disk AND tracked, or documented-absent."""
        tracked = _tracked_files()
        missing, untracked, excused = [], [], []
        for rel in sorted(refs):
            if _is_documented_absent(rel):
                excused.append(rel)
                continue
            path = _STATIC / rel
            if not path.is_file():
                missing.append(rel)
                continue
            repo_rel = path.relative_to(_REPO).as_posix()
            if repo_rel not in tracked:
                untracked.append(repo_rel)
        self.assertEqual(
            [], missing,
            f"{label}: referenced image files do not exist:\n  " + "\n  ".join(missing),
        )
        self.assertEqual(
            [], untracked,
            f"{label}: referenced image files exist here but are NOT IN THE INDEX, so a "
            f"clean clone would 404 on them (this is N47):\n  " + "\n  ".join(untracked),
        )
        return excused

    def test_html_referenced_images_exist_and_are_tracked(self):
        self._check(collect_html_references(), "index.html")

    def test_css_referenced_images_exist_and_are_tracked(self):
        self._check(collect_css_references(), "main.css")

    def test_js_referenced_images_exist_and_are_tracked(self):
        self._check(collect_js_references(), "app.js")

    def test_every_pokemon_sprite_the_contract_emits_exists_and_is_tracked(self):
        self._check(collect_pokemon_sprite_references(), "pokedex sprite_url")

    def test_every_item_icon_the_client_falls_back_to_exists_and_is_tracked(self):
        self._check(collect_item_icon_references(), "item icon fallback")

    def test_the_node_sprites_the_contract_emits_are_the_documented_absences(self):
        """`contract.node_view`'s `sprite_url` family. These are N42 absences
        WITH a fallback, and this asserts they are exactly that -- so a NEW
        node sprite path appearing here fails rather than being absorbed."""
        # Collected from the EMITTED projection rather than from a helper, so
        # this follows whatever the renderer really sends to a client.
        refs = set()
        from pokelike import engine
        eng = engine.Engine()
        state = eng.reset(seed=3)
        eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        obs = contract.observation(eng.state)
        for node in (obs.get("map") or {}).get("nodes", []):
            rel = _normalise(node.get("sprite_url") or "")
            if rel:
                refs.add(rel)
        self.assertTrue(refs, "no node sprite_url was emitted -- this check would be vacuous")
        for rel in sorted(refs):
            self.assertTrue(
                _is_documented_absent(rel) or (_STATIC / rel).is_file(),
                f"node sprite {rel} is neither present nor a documented N42 absence",
            )

    def test_the_documented_absences_are_still_genuinely_absent(self):
        """The allowlist must not outlive the gap it documents. If one of these
        files is ever added, it should be CHECKED like everything else rather
        than permanently excused."""
        for rel, reason in sorted(_DOCUMENTED_ABSENT.items()):
            self.assertFalse(
                (_STATIC / rel).is_file(),
                f"{rel} now exists, so its N42 excuse ({reason}) is stale -- "
                f"remove it from _DOCUMENTED_ABSENT so it gets checked",
            )

    def test_the_allowlist_cannot_hide_an_ordinary_missing_file(self):
        """Non-vacuity for the allowlist itself: an arbitrary path must NOT be
        treated as documented-absent."""
        self.assertFalse(_is_documented_absent("img/logo.png"))
        self.assertFalse(_is_documented_absent("img/menu/reset.png"))
        self.assertFalse(_is_documented_absent("img/sprites/pokemon/1.png"))


if __name__ == "__main__":
    unittest.main()
