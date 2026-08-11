# extract-data

Pulls the static data tables (Pokedex, moves, type chart, items, evolutions,
fixed trainers, map BST/level ranges, fallback species pool, wild-encounter
eligibility sets, trait unlock requirements, alternate-form maps) out of
`pokelike_forked/js/bundle.deobfuscated.js` as JSON, instead of hand-
transcribing ~721 species entries and everything else by eye.

## How it works

The relevant tables all live in the first ~57% of the bundle and are built
through pure data-literal construction (confirmed: zero `document.`/
`localStorage.`/`fetch(`/`navigator.`/`Audio(` calls execute at the top
level in that range — the only browser global touched is a couple of
`window["..."] = ...` property assignments). So the script:

1. Parses the bundle with `acorn` to find the exact end of the
   `BRANCHING_EVOLUTIONS` statement (the last table we need, in source
   order), and slices the file there — a real AST boundary, not a manual
   line-count guess, so the slice is always syntactically complete.
2. Stubs `k`/`K` (the string-array decoder from `tools/deobfuscate/` — dead
   weight at this point since the bundle we read from already has every
   string decoded), `window`/`location` (the bundle has a dev/anti-clone
   hostname check; `location.hostname = "localhost"` is itself on the
   game's own allowlist, so this isn't a bypass), and `setTimeout`/
   `setInterval` (the same check re-arms itself with a timer).
3. Runs that prefix in a `vm` sandbox, then reads the tables straight off
   their real top-level names — `MOVE_POOL`, `TYPE_CHART`, `EVOLUTIONS`,
   `GYM_LEADERS`, etc. are literal, un-mangled identifiers in this bundle;
   only function-local variables got obfuscator-renamed. (One exception:
   the offline wild-encounter fallback pool's variable, `Sl`, IS a mangled
   name — flag this if the bundle is ever regenerated with a different
   obfuscator seed, since that identifier could change.)

## Usage

```
npm install    # installs acorn
node extract-tables.js ../../pokelike_forked/js/bundle.deobfuscated.js ./out
```

Then copy whichever `out/*.json` files changed into `pokelike/data/` (see
that directory's layout, and `pokelike/data.py`'s docstring, for where each
file goes and what reads it).

## After re-running

`pokelike/tests/test_data.py` has enough spot-checks (species counts, known
stat lines, a couple of "shape surprises" like Elite Four members having no
`badge`/`moveTier` — only gym leaders do — and Arceus's signature move
having no `type`) to catch most regenerations that come out structurally
different. Run it: `python -m unittest pokelike.tests.test_data -v`.

## `extract-trainer-tables.js` (added 2026-07-31)

A second script, same directory, same technique -- pulls
`TRAINER_BATTLE_CONFIG` (34 mid-map trainer archetypes),
`TRAINER_SPRITE_KEYS`/`GEN2_ONLY_TRAINER_KEYS`/`GEN1_ONLY_TRAINER_KEYS`/
`GEN3_TRAINER_KEYS`/`GEN4_TRAINER_KEYS`, `SILVER_ENCOUNTERS`/
`SILVER_STARTER_LINES`, and `MAGMA_ENCOUNTERS`/`AQUA_ENCOUNTERS` --
transcribed by CODEX.md's P0.9 addendum. These tables live much further
into the bundle than `extract-tables.js`'s own cutoff (past
`BRANCHING_EVOLUTIONS`), so this script reuses the SAME already-audited,
further-extended cutoff `tools/battle-oracle/extract-prefix.js` established
(bundle lines 1-81051, cut right after `mergeBattleConfigs`'s declaration,
including that tool's inert `document` stub for the real top-level
`document[...]` calls `scan-toplevel-danger.js` found) rather than
re-deriving a fresh safety argument for a third, slightly-different cutoff
point.

```
node extract-trainer-tables.js ../../pokelike_forked/js/bundle.deobfuscated.js ./out
```

Then copy the new `out/*.json` files into `pokelike/data/trainers/`
(`trainer_battle_config.json`, `silver_encounters.json`,
`silver_starter_lines.json`, `magma_encounters.json`,
`aqua_encounters.json`) and `pokelike/data/map_config/`
(`trainer_sprite_keys.json`, `gen1_only_trainer_keys.json`,
`gen2_only_trainer_keys.json`, `gen3_trainer_keys.json`,
`gen4_trainer_keys.json`) -- see `pokelike/data.py`'s "Procedural mid-map
trainer archetypes" section for the loaders. Covered by
`pokelike/tests/test_trainer_silver_admin.py`, which cross-checks the
consuming logic (not just this extraction) against the real JS run through
Node.
