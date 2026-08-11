"""One-time asset-fetch script for ITEM icons, the sibling of
`fetch_sprites.py` and built on exactly the same precedent: it downloads from
the PokeAPI/sprites GitHub repo (raw.githubusercontent.com -- a well-known,
freely available fan-sprite dataset commonly used by Pokemon fan projects),
and it does NOT pull pokelike.xyz's own hosted images.

Why this exists (R6 §4). The source's `itemIconHtml`
(`bundle.deobfuscated.js:52113-52141`) resolves an item icon to
`item.iconUrl || raw.githubusercontent.com/PokeAPI/sprites/master/sprites/
items/<id>.png` -- so the site's OWN fallback for an item icon is this exact
public dataset, addressed by the item id with `_` replaced by `-` (52116).
Item icons are therefore the one asset class in R6 §4 that can be sourced the
same way the species sprites were, rather than being pokelike's own artwork.

This client is local-only and offline, so it caches them under
`pokelike/webui/static/img/sprites/items/` -- the path shape
`app.js`'s `appendItemIcon()` expects, alongside the existing
`.../sprites/pokemon/` cache.

Running this is OPTIONAL. Every ported item carries an emoji `icon`
(`contract.item_view`), and `appendItemIcon` reproduces the source's own
`onerror` handler, which replaces a failed image with that emoji. A fresh
checkout that never runs this script shows emoji item icons, which is the
source's own documented behaviour -- not a broken screen.

Run once (needs internet access):
    python tools/fetch-sprites/fetch_item_sprites.py [--force]

Safe to re-run -- skips files already downloaded unless --force is passed.
A 404 (an id this dataset does not carry) is logged and skipped, not fatal.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEST_ROOT = _REPO_ROOT / "pokelike" / "webui" / "static" / "img" / "sprites" / "items"


def _item_ids() -> list[str]:
    """Every id in the two ported item tables -- the same pair
    `contract._item_table()` builds, so the fetch set and the set the renderer
    can actually ask for cannot drift apart."""
    sys.path.insert(0, str(_REPO_ROOT))
    from pokelike import data  # noqa: E402  (path set above on purpose)

    ids = {item.id for item in data.get_passive_items()}
    ids |= {item.id for item in data.get_usable_items()}
    return sorted(ids)


def _fetch_one(url: str, dest: Path, *, force: bool) -> str:
    if dest.exists() and not force:
        return "skip"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "missing"
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    return "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="re-download even if the file already exists"
    )
    args = parser.parse_args()

    ids = _item_ids()
    print(f"Fetching {len(ids)} item icon(s) into {_DEST_ROOT}")

    counts: dict[str, int] = {"ok": 0, "skip": 0, "missing": 0}
    missing: list[str] = []
    for item_id in ids:
        # 52116: the source's own id transform for this dataset.
        slug = item_id.replace("_", "-")
        result = _fetch_one(f"{_REPO_BASE}/{slug}.png", _DEST_ROOT / f"{slug}.png", force=args.force)
        counts[result] = counts.get(result, 0) + 1
        if result == "missing":
            missing.append(slug)

    print(
        f"Done. ok={counts.get('ok', 0)} skip={counts.get('skip', 0)} "
        f"missing={counts.get('missing', 0)}"
    )
    if missing:
        print("not in the dataset (the emoji fallback covers these): " + ", ".join(missing))


if __name__ == "__main__":
    main()
