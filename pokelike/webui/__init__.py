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

Battle presentation (R4 -- this was previously listed here as a known gap,
and the description had been false since R1):
- The battle screen replays the fight turn by turn, off
  `render.contract.battle_view`'s `replay`. The old note claimed
  `battle_loop.run_battle` exposed no turn-level event stream, and named
  adding a per-turn callback as the prerequisite follow-up. Both
  were out of date: the feed has existed since R1 (`battle_events` /
  `status_events`, carried onto `RunState.last_battle` by
  `engine._run_battle`) and gained its rosters in R2/N2. Nothing was ever
  missing from the engine side; the renderers simply did not read it.
- Synchronous resolution was not the obstacle either -- it is the source's
  own model. `runBattleScreen` resolves the whole battle first
  (bundle.deobfuscated.js:81208-81222), then replays the finished log
  through `animateBattleVisually` (81272). The replay here follows that
  shape: a client-side drain of an already-fixed sequence, touching no
  engine state and no `Engine.step` timing.
- What IS genuinely approximated, and is documented as such in
  docs/renderer-contract.md section 11: the browser-native half. The
  source's per-move particle canvases (`playAttackAnimation`, 66698+) and
  its `requestAnimationFrame` HP tween (65035-65064) are per-frame
  rendering, not portable algorithms.
- The map's node LAYOUT (pixel positions) IS a port, as of R2 -- this
  bullet said the opposite until R5 and had been false since R2. The
  source's positioning is not CSS the browser computes and not
  untraceable: it is plain JS grid arithmetic inside `renderMap`
  (bundle.deobfuscated.js:54126-54142) written into an SVG `transform`,
  and `pokelike/render/contract.py` now computes exactly that. What stays
  browser-side is only turning the resulting viewport-free fractions into
  pixels against the live container, the way the source does from its own
  `clientWidth`/`clientHeight` (54113-54114). The real CSS classes/SVG
  structure (`g.map-node`, `--node-tx`/`--node-ty`) are reused as before.
- Pokemon sprites are a locally-cached copy of a public fan-sprite set
  (NOT pokelike.xyz's own hosted images, which aren't in this local
  mirror beyond one sample file) -- see `tools/fetch-sprites/` for the
  one-time download script.
"""
