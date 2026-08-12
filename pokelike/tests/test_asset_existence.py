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
import unittest.mock

from pokelike import data, map_gen
from pokelike.render import contract

_REPO = pathlib.Path(__file__).resolve().parents[2]
_STATIC = _REPO / "pokelike" / "webui" / "static"
_IMG = _STATIC / "img"


# ---------------------------------------------------------------------------
# R7.1 -- what is left of the R6/N42 absence allowlist.
#
# N42 assumed a whole class of artwork could not be sourced. R7.1's authorized
# one-time bounded fetch resolved essentially all of it: 168 verified files
# (`docs/audits/R7.1-scraped-assets.json`) covering the map backgrounds, region
# cards, node/trainer/boss sprites, move-category icons and Loaded Dice. Every
# corresponding entry has been DELETED from this allowlist rather than left to
# rot, and the files are now checked like any other reference.
#
# Two paths remain, and they are a different kind of thing: the live host
# answers them with `text/html`, not an image. They are genuine upstream 404s,
# not files this project declined to fetch. Saving an HTML error body under a
# `.png` name, or substituting some other artwork, would both be worse than the
# absence -- so they stay absent, and
# `test_the_two_live_404_paths_are_unreachable_in_gen1` proves separately that
# no reachable Story/Nuzlocke projection can ever emit them.
#
# The list is deliberately tiny and every entry must justify itself. An
# absence with no proved unreachability or no named fallback does not belong
# here; it belongs in the failure output.
# ---------------------------------------------------------------------------
_DOCUMENTED_ABSENT = {
    "img/sprites/g1/bird-catcher.png":
        "live pokelike.xyz returns text/html for this exact path (R7.1 manifest "
        "`rejected_non_image`). `birdCatcher` is in GEN2_ONLY_TRAINER_KEYS, so "
        "`_trainer_sprite_candidates` never offers it outside Gen2 and the Gen1 "
        "path is unreachable; the reachable Gen2 file exists",
    "img/sprites/g1/school-boy.png":
        "same live text/html response; `schoolBoy` is likewise GEN2_ONLY, so the "
        "Gen1 path is unreachable and the Gen2 file exists",
}

#: The families that must NEVER be excused, whatever they are named. This is
#: the guard that keeps a future one-line allowlist edit from silently hiding a
#: missing Pokemon sprite, item icon or map background.
_NEVER_EXCUSED_PREFIXES = (
    "img/sprites/pokemon/", "img/sprites/items/", "img/sprites/badges/", "img/sprites/showdown/",
    "img/maps/", "img/regions/", "img/items/",
)


def _is_documented_absent(rel: str) -> bool:
    if rel.startswith(_NEVER_EXCUSED_PREFIXES):
        return False
    return rel in _DOCUMENTED_ABSENT


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
    """`appendItemIcon` (app.js) sets

        img.src = opt.icon_url || ('/img/sprites/items/' + id-with-dashes + '.png')

    -- so the `img/sprites/items/` path is the fallback for items that have NO
    `icon_url`, and is simply never used by an item that has one.

    **R7.1/N48 fix.** The old version of this collector enumerated the fallback
    path for EVERY item, including `loaded_dice`. That was wrong in both
    directions. `loaded_dice` is the one item whose contract `icon_url` is a
    LOCAL path (`img/items/loaded-dice.png`, the source's own ITEM_POOL value),
    and R7.1 fetched that exact file -- so the item renders its real icon and
    the `||` right-hand side is dead code for it. Demanding
    `img/sprites/items/loaded-dice.png` therefore asserted the existence of a
    file that nothing reads, which is what forced the second N48 allowlist
    entry. Enumerating the fallback only where the fallback is actually
    reachable removes the need for both entries.
    """
    out = set()
    for item in _items_without_icon_url():
        out.add("img/sprites/items/%s.png" % str(item.id).replace("_", "-"))
    return out


def collect_item_icon_url_references() -> set[str]:
    """The other half of the same `||`: local `icon_url` values the contract
    supplies directly. These are the primary source, not a fallback, so they
    must exist -- this is what actually closes N48 for `loaded_dice`."""
    out = set()
    for item in _all_items():
        rel = _normalise(getattr(item, "icon_url", None) or "")
        if rel:
            out.add(rel)
    return out


def collect_badge_sprite_references() -> set[str]:
    """The map HUD can select all eight badges in each of four regions.

    `renderBadgeCount` maps Gen1..Gen4 to the source's contiguous local
    sprite families 1..8, 9..16, 17..24 and 25..32 respectively. Enumerate
    every path here rather than sampling a one-badge run: an untouched early
    run never exercises the seven later sprites, much less the other regions.
    """
    return {f"img/sprites/badges/{number}.png" for number in range(1, 33)}


def _all_items():
    """Both ported item tables. `get_usable_items` and `get_passive_items` are
    genuinely separate sources (data.py:326-349) and the bag renders from both,
    so an icon check that read only one would miss half the family."""
    return list(data.get_usable_items()) + list(data.get_passive_items())


def _items_without_icon_url():
    return [it for it in _all_items() if not getattr(it, "icon_url", None)]


# ---------------------------------------------------------------------------
# R7.1 -- the REACHABLE node sprite denominator.
#
# R7's version sampled ONE seed-3 Gen1 run and checked whatever node sprites it
# happened to emit. That is not a denominator: it could not see a single Gen2/3/4
# path, any boss past map 0, any trainer archetype the seed did not roll, or any
# submap at all. R7.1 replaces it with an enumeration of every (generation, map
# index) the Story/Nuzlocke surface can actually produce, and every node variant
# reachable within it.
#
# Reachability is taken from `generate_map`'s own placement gates
# (map_gen.py:471-495) rather than assumed, because the special node types are
# NOT available everywhere:
#
#   SILVER       gen2, map indices 1/3/5/7
#   MAGMA/AQUA   gen3, map indices 2/5/7
#   UNDERGROUND  gen4, map indices 1/3/6
#   DISTORTION   gen4, map indices 3/5/7
#
# Enumerating them under every generation would inflate the denominator with
# paths the game cannot reach and would demand artwork that legitimately does
# not exist -- which is exactly how an over-broad check turns into a new
# allowlist.
# ---------------------------------------------------------------------------

#: (generation, gen2_mode, gen3_mode, gen4_mode). Gen1 is the no-flags default.
_GENERATIONS = ((1, False, False, False), (2, True, False, False),
                (3, False, True, False), (4, False, False, True))

#: `generate_map` builds nine maps per run, indices 0-8.
_MAP_INDICES = range(9)


def _node(node_type, **extra):
    return map_gen.MapNode(id="probe", type=node_type, layer=0, col=0, extra=extra)


def collect_reachable_node_sprite_references() -> set[str]:
    """Every `sprite_url` `contract._node_sprite` can emit on the declared
    Story/Nuzlocke Gen1-4 surface."""
    refs: set[str] = set()

    def add(url):
        if url:
            refs.add(url)

    # Present on every map in every generation.
    universal = (
        map_gen.START, map_gen.BATTLE, map_gen.CATCH, map_gen.ITEM,
        map_gen.QUESTION, map_gen.POKECENTER, map_gen.LEGENDARY,
        map_gen.MOVE_TUTOR, map_gen.TRADE,
    )

    for _gen, g2, g3, g4 in _GENERATIONS:
        for map_index in _MAP_INDICES:
            ctx = contract.NodeContext(
                gen2_mode=g2, gen3_mode=g3, gen4_mode=g4, current_map=map_index,
            )
            for node_type in universal:
                add(contract._node_sprite(_node(node_type), ctx))

            # Trainers: the archetype list is itself map-index dependent
            # (`aceTrainer`/`policeman` cutoffs), so it is re-derived per map
            # rather than taken once per generation.
            for key in map_gen._trainer_sprite_candidates(map_index, g2, g3, g4):
                add(contract._node_sprite(_node(map_gen.TRAINER, trainerSprite=key), ctx))

            # The end-of-map boss, whose sprite is chosen from `mapIndex`.
            add(contract._node_sprite(_node(map_gen.BOSS, mapIndex=map_index), ctx))

            if g2 and map_index in (1, 3, 5, 7):
                add(contract._node_sprite(_node(map_gen.SILVER), ctx))
            if g3 and map_index in (2, 5, 7):
                add(contract._node_sprite(_node(map_gen.MAGMA), ctx))
                add(contract._node_sprite(_node(map_gen.AQUA), ctx))
            if g4 and map_index in (1, 3, 6):
                add(contract._node_sprite(_node(map_gen.UNDERGROUND), ctx))
                refs |= _submap_sprite_references(map_gen.UNDERGROUND, ctx)
            if g4 and map_index in (3, 5, 7):
                add(contract._node_sprite(_node(map_gen.DISTORTION), ctx))
                refs |= _submap_sprite_references(map_gen.DISTORTION, ctx)

    return refs


def _submap_sprite_references(kind, ctx) -> set[str]:
    """The sub-map interior: sub-boss, rewards and the exit.

    The two kinds differ in how their boss sprite is chosen, and the difference
    matters for which files are required:

    * UNDERGROUND rolls TRAINERS (`_roll_underground_trainers`), so its
      sub-boss carries a `trainerKey` drawn from `GEN4_TRAINER_KEYS` and
      resolves through `_trainer_sprite_path`. `gen4_mode` is always true for
      an underground node, so these are `img/sprites/g4/` paths.
    * DISTORTION carries a literal `bossSprite` -- Cyrus from
      `submap_bosses.json`, plus, on the legendary-eligible visit, one of the
      three `DISTORTION_LEGENDARY_POOL` Pokemon sprites (Dialga, Palkia and the
      Origin Forme Giratina at `img/sprites/pokemon/10007.png`, which R7.1
      fetched specifically because this branch reaches it).
    """
    refs: set[str] = set()

    def add(url):
        if url:
            refs.add(url)

    if kind == map_gen.UNDERGROUND:
        for key in data.get_gen4_trainer_keys():
            add(contract._node_sprite(
                _node(map_gen.BOSS, subBoss=kind, trainerKey=key), ctx))
    else:
        add(contract._node_sprite(_node(
            map_gen.BOSS, subBoss=kind,
            bossSprite=data.get_submap_bosses()["distortion"].sprite), ctx))
        for entry in data.get_distortion_legendary_pool():
            add(contract._node_sprite(_node(
                map_gen.BOSS, subBoss=kind, wildBoss=True,
                bossSprite=entry.sprite), ctx))

    # `bossSprite` absent entirely -> the source's own placeholder.
    add(contract._node_sprite(_node(map_gen.BOSS, subBoss=kind), ctx))

    for reward_id in data.get_submap_reward_by_id():
        add(contract._node_sprite(_node(map_gen.REWARD, reward=reward_id), ctx))
    # `pickSubMapRewards` can leave a reward id unset; the projection falls
    # back to the item icon, and that branch is reachable.
    add(contract._node_sprite(_node(map_gen.REWARD, reward=None), ctx))
    add(contract._node_sprite(_node(map_gen.SUBEXIT), ctx))
    return refs


def collect_map_background_references() -> set[str]:
    """The 38 in-run backgrounds `app.js:mapBackgroundUrl` can resolve, mirrored
    from the same source precedence (bundle.deobfuscated.js:77234-77244) so the
    files are checked on the Python side too. Nine numbered maps per generation,
    plus the two Gen4 submap overrides."""
    out = {"img/maps/g4/distortion_world.png", "img/maps/g4/underground.png"}
    for gen in (1, 2, 3, 4):
        for map_index in _MAP_INDICES:
            out.add("img/maps/g%d/%d.png" % (gen, map_index + 1))
    return out


def collect_region_background_references() -> set[str]:
    """`HISTORY_REGIONS[*].bg` -- the region-card artwork, read from the client
    table rather than hard-coded here."""
    text = (_STATIC / "js" / "app.js").read_text(encoding="utf-8")
    out = set()
    for m in re.finditer(r"""bg:\s*['"]([^'"]+)['"]""", text):
        rel = _normalise(m.group(1))
        if rel:
            out.add(rel)
    return out


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

    def test_every_local_item_icon_url_the_contract_supplies_exists(self):
        """R7.1/N48. `loaded_dice` is the only item whose contract `icon_url` is
        a local path, and it is the one this closes: the source's own
        `img/items/loaded-dice.png` was fetched, so the primary URL resolves and
        the emoji fallback is no longer what the player sees."""
        refs = collect_item_icon_url_references()
        self.assertIn(
            "img/items/loaded-dice.png", refs,
            "the loaded_dice local icon_url is no longer emitted -- this check "
            "would silently stop covering N48",
        )
        self._check(refs, "contract item icon_url")

    def test_every_badge_sprite_the_map_hud_can_emit_exists_and_is_tracked(self):
        """The earned-badge image branch is the normal visual, never a gold
        circle placeholder. All 32 source sprites must therefore ship locally
        and remain under the same tracked-asset guard as item sprites."""
        refs = collect_badge_sprite_references()
        self.assertEqual(32, len(refs), "the four-region badge family collapsed")
        self._check(refs, "map HUD badge sprite")

    def test_every_reachable_node_sprite_exists_and_is_tracked(self):
        """R7.1. The whole reachable node-sprite family, across all nine maps
        and all four supported generations. This replaces R7's single seed-3
        Gen1 sample, which could not see any Gen2/3/4 path, any boss past map 0,
        any unrolled trainer archetype, or any submap."""
        refs = collect_reachable_node_sprite_references()
        self.assertGreater(
            len(refs), 100,
            f"only {len(refs)} reachable node sprites enumerated -- the "
            f"denominator collapsed and this check would be near-vacuous",
        )
        self._check(refs, "reachable node sprite_url")

    def test_no_reachable_node_sprite_hot_links_a_remote_host(self):
        """R7.1. Ordinary rendering must not depend on raw.githubusercontent.com
        or play.pokemonshowdown.com being reachable. Both hosts are named
        explicitly so that re-introducing either -- by reverting the cache
        projection, or by adding a new hot-linked table -- fails here."""
        remote = sorted(
            url for url in collect_reachable_node_sprite_references()
            if url.startswith(("http://", "https://", "//"))
        )
        self.assertEqual(
            [], remote,
            "these reachable node sprites still point at a remote host instead "
            "of their R7.1 local cache:\n  " + "\n  ".join(remote),
        )

    def test_the_local_cache_projection_is_not_vacuous(self):
        """The previous test would also pass if `_node_sprite` had simply
        stopped emitting the hot-linked families. Assert the cache is doing real
        work: the known remote URLs map to local files, and an uncached URL on
        the same host is left alone rather than rewritten into a 404."""
        self.assertEqual(
            "img/sprites/items/explorer-kit.png",
            contract._pokeapi_item("explorer-kit"),
        )
        self.assertEqual(
            "img/sprites/showdown/ruinmaniac.png",
            contract._local_cache_url(
                "https://play.pokemonshowdown.com/sprites/trainers/ruinmaniac.png"),
        )
        # No cache file was fetched for `red.png` (Gen2 map index 17, which the
        # Story surface cannot generate), so it must survive unchanged.
        unreachable = "https://play.pokemonshowdown.com/sprites/trainers/red.png"
        self.assertEqual(unreachable, contract._local_cache_url(unreachable))
        self.assertFalse((_STATIC / "img/sprites/showdown/red.png").is_file())

    def test_every_map_background_exists_and_is_tracked(self):
        """R7.1. All 38 in-run backgrounds `mapBackgroundUrl` can resolve."""
        refs = collect_map_background_references()
        self.assertEqual(38, len(refs), f"expected 38 map backgrounds, got {len(refs)}")
        self._check(refs, "map background")

    def test_every_region_background_exists_and_is_tracked(self):
        """R7.1. `HISTORY_REGIONS[*].bg`, read from the client table."""
        refs = collect_region_background_references()
        self.assertEqual(
            {"img/regions/kanto.jpg", "img/regions/johto.jpg",
             "img/regions/hoenn.jpg", "img/regions/sinnoh.jpg"},
            refs,
            "the region-card background table changed shape; update this check "
            "deliberately rather than letting it drift",
        )
        self._check(refs, "region background")

    def test_the_move_category_icons_exist_and_are_tracked(self):
        """R7.1. `img/physical.png`/`img/special.png` now exist, so the source's
        image branch is the ordinary branch. The text-badge `onerror` fallback
        stays in place and is covered by its own DOM detector."""
        self._check({"img/physical.png", "img/special.png"}, "move category icon")

    def test_the_two_live_404_paths_are_unreachable_in_gen1(self):
        """R7.1. `bird-catcher`/`school-boy` are the only remaining allowlist
        entries, and the allowlist is only honest if the paths are genuinely
        unreachable. Prove it from the candidate filter rather than by
        assertion: both keys are GEN2_ONLY, so no Gen1 map can offer them, and
        their reachable Gen2 files exist."""
        gen2_only = data.get_gen2_only_trainer_keys()
        self.assertIn("birdCatcher", gen2_only)
        self.assertIn("schoolBoy", gen2_only)

        for map_index in _MAP_INDICES:
            gen1 = map_gen._trainer_sprite_candidates(map_index, False, False, False)
            self.assertNotIn("birdCatcher", gen1, f"map {map_index}")
            self.assertNotIn("schoolBoy", gen1, f"map {map_index}")

        # ... and no reachable projection anywhere emits the Gen1 paths.
        reachable = collect_reachable_node_sprite_references()
        self.assertNotIn("img/sprites/g1/bird-catcher.png", reachable)
        self.assertNotIn("img/sprites/g1/school-boy.png", reachable)

        # The reachable Gen2 files, by contrast, must exist.
        for rel in ("img/sprites/g2/bird-catcher.png", "img/sprites/g2/school-boy.png"):
            self.assertIn(rel, reachable, f"{rel} should be reachable in Gen2")
            self.assertTrue((_STATIC / rel).is_file(), f"{rel} is missing")

    def test_the_documented_absences_are_still_genuinely_absent(self):
        """The allowlist must not outlive the gap it documents. If one of these
        files is ever added, it should be CHECKED like everything else rather
        than permanently excused."""
        for rel, reason in sorted(_DOCUMENTED_ABSENT.items()):
            self.assertFalse(
                (_STATIC / rel).is_file(),
                f"{rel} now exists, so its excuse ({reason}) is stale -- "
                f"remove it from _DOCUMENTED_ABSENT so it gets checked",
            )

    def test_no_allowlisted_path_is_actually_reachable(self):
        """The general invariant behind the allowlist: it may only excuse paths
        the game CANNOT reach. An entry that is reachable is a real missing
        asset being hidden, whatever justification the entry carries.

        This is what makes the allowlist self-policing. The hard-coded checks
        below catch specific known paths; this catches any future one, because
        the reachable set is enumerated rather than listed."""
        reachable = collect_reachable_node_sprite_references()
        reachable |= collect_map_background_references()
        reachable |= collect_region_background_references()
        reachable |= collect_item_icon_url_references()
        reachable |= {"img/physical.png", "img/special.png"}

        overlap = sorted(set(_DOCUMENTED_ABSENT) & reachable)
        self.assertEqual(
            [], overlap,
            "these paths are excused as absent but are REACHABLE, so the "
            "allowlist is hiding a genuinely missing asset:\n  " + "\n  ".join(overlap),
        )

    def test_the_allowlist_cannot_hide_an_ordinary_missing_file(self):
        """Non-vacuity for the allowlist itself: an arbitrary path must NOT be
        treated as documented-absent, and the never-excused families must reject
        an entry even if someone adds one to `_DOCUMENTED_ABSENT` by hand."""
        self.assertFalse(_is_documented_absent("img/logo.png"))
        self.assertFalse(_is_documented_absent("img/menu/reset.png"))
        self.assertFalse(_is_documented_absent("img/sprites/pokemon/1.png"))
        self.assertFalse(_is_documented_absent("img/sprites/g1/grass.png"))

        # A hand-added entry in a never-excused family stays un-excused. This is
        # the guard the R7.1 brief asks for: "the test must fail if an arbitrary
        # future missing file is added to an allowlist".
        for rel in ("img/maps/g1/1.png", "img/regions/kanto.jpg",
                    "img/sprites/pokemon/999999.png", "img/items/loaded-dice.png"):
            with unittest.mock.patch.dict(_DOCUMENTED_ABSENT, {rel: "hand-added"}):
                self.assertFalse(
                    _is_documented_absent(rel),
                    f"{rel} was excused by a hand-added allowlist entry",
                )


if __name__ == "__main__":
    unittest.main()
