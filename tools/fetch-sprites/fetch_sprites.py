"""One-time asset-fetch script: downloads a public fan-sprite set (the
PokeAPI/sprites GitHub repo, raw.githubusercontent.com -- a well-known,
freely available sprite dataset commonly used by Pokemon fan projects, NOT
pokelike.xyz's own hosted images, which aren't in this local mirror beyond
one sample file) for every species id in `pokelike/data/pokedex.json`, and
caches them locally under `pokelike/webui/static/img/sprites/pokemon/`
(regular) and `.../pokemon/shiny/` (shiny) -- the exact relative path shape
`pokelike/webui/static/index.html`'s sprite URLs expect (see
`pokelike/webui/static/js/app.js`'s `spriteUrl()`).

Run once (needs internet access):
    python tools/fetch-sprites/fetch_sprites.py [--shiny] [--skip-existing]

Safe to re-run -- skips files already downloaded unless --force is passed.
Missing ids (a 404 from the source repo) are logged and skipped, not
treated as fatal -- the web UI's own <img onerror> handler hides a broken
sprite gracefully rather than crashing.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_POKEDEX_JSON = _REPO_ROOT / "pokelike" / "data" / "pokedex.json"
_DEST_ROOT = _REPO_ROOT / "pokelike" / "webui" / "static" / "img" / "sprites" / "pokemon"


def _species_ids() -> list[int]:
    with io.open(_POKEDEX_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    return sorted(int(sid) for sid in raw.keys())


def _fetch_one(url: str, dest: Path, *, force: bool) -> str:
    if dest.exists() and not force:
        return "skip"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "missing"
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shiny", action="store_true", help="also fetch shiny variants")
    parser.add_argument("--force", action="store_true", help="re-download even if the file already exists")
    args = parser.parse_args()

    ids = _species_ids()
    print(f"Fetching {len(ids)} regular sprite(s){' + shiny' if args.shiny else ''} into {_DEST_ROOT}")

    counts = {"ok": 0, "skip": 0, "missing": 0}
    for i, species_id in enumerate(ids):
        url = f"{_REPO_BASE}/{species_id}.png"
        dest = _DEST_ROOT / f"{species_id}.png"
        result = _fetch_one(url, dest, force=args.force)
        counts[result] = counts.get(result, 0) + 1
        if args.shiny:
            shiny_url = f"{_REPO_BASE}/shiny/{species_id}.png"
            shiny_dest = _DEST_ROOT / "shiny" / f"{species_id}.png"
            shiny_result = _fetch_one(shiny_url, shiny_dest, force=args.force)
            counts[shiny_result] = counts.get(shiny_result, 0) + 1
        if (i + 1) % 100 == 0:
            print(f"  ... {i + 1}/{len(ids)}")

    print(f"Done. ok={counts.get('ok', 0)} skip={counts.get('skip', 0)} missing={counts.get('missing', 0)}")
    if counts.get("missing", 0):
        print("(missing ids are logged only -- app.js's <img onerror> hides them gracefully)")


if __name__ == "__main__":
    main()
