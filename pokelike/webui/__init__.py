"""A local web UI over `pokelike.engine` -- a browser-based, higher-fidelity
sibling to `pokelike.render` (the plain-text console viewer). Reuses the
real site's actual `index.html`/`style/main.css` (from `pokelike_forked/`,
trimmed/adapted -- see `pokelike/webui/static/`) for visual fidelity, with a
brand-new `app.js` (NOT the obfuscated `bundle.js`) that talks to a small
local JSON API (`server.py`) wrapping `engine.Engine`, instead of the
site's own game logic.

Scope, matching what the user asked for this pass: the CORE LOOP only
(title -> starter select -> map -> battle -> catch/item/pokecenter/
move_tutor/trade/swap/evolution -> badge -> game over/win) -- the same
`Phase`s `engine.py` actually implements. Meta screens the real site has
(Pokedex gallery, achievements, settings, Hall of Fame, cloud sync, ads)
are NOT modeled here since there's no corresponding data/logic in
`engine.py` to back them -- building UI-only mockups for those would be
misleading, not "start simple."

Known, deliberate gaps (flag rather than silently fake):
- `battle_loop.run_battle` resolves an entire multi-round battle
  synchronously and doesn't expose a per-turn event feed (see that
  module's and `engine.py`'s own docstrings) -- the battle screen here
  shows the pre-battle matchup, then the final result, NOT an animated
  turn-by-turn exchange like the real site's battle screen. Adding a
  real per-turn callback to `battle_loop.run_battle` would be the
  prerequisite for that, and is flagged as follow-up work, not attempted
  here.
- The map's node LAYOUT (pixel positions) is this module's own simple
  per-layer grid, not a port of the source's actual positioning algorithm
  (untraced, lives in the obfuscated `map.js`-equivalent bundle code) --
  it reuses the real CSS classes/SVG structure (`g.map-node`,
  `--node-tx`/`--node-ty`) but computes its own coordinates from
  `map_gen`'s layer/col indices.
- Pokemon sprites are a locally-cached copy of a public fan-sprite set
  (NOT pokelike.xyz's own hosted images, which aren't in this local
  mirror beyond one sample file) -- see `tools/fetch-sprites/` for the
  one-time download script.
"""
