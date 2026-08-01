# route-oracle

The M3 **short full-run** source/Python oracle. It executes the same
deterministic Story/Nuzlocke route through the real JavaScript in
`pokelike_forked/js/bundle.deobfuscated.js` and through the real
`pokelike.engine`, then compares versioned, canonicalized checkpoint streams
field by field.

This is the run-level counterpart to `tools/battle-oracle`, which compares a
single `runBattle` call. Neither side is a reimplementation: the JavaScript
runner calls the source's own `startNewRun` / `showStarterSelect` /
`selectStarter` / `startMap` / `generateMap` / `onNodeClick` / `doTrainerNode`
/ `doSilverNode` / `enterSubMap` / `runBattleScreen` / `showBadgeScreen`, and
drives choices by invoking the exact click listeners the source attached.

> **Why this lives at the repository root rather than under `tools/`.**
> `.gitignore` ignores `tools/` wholesale, and M3 requires the harness,
> runners, fixtures and schema docs to be tracked, reviewable artifacts.
> Un-ignoring a subdirectory of an ignored directory needs surgery on the
> existing `tools/` rule; a sibling top-level directory needs none.

## Dependencies

* `node` (developed against v20.19.2).
* `python` 3.11+ (developed against 3.13.13) — standard library only.

`extract-prefix.js`, `compare.py` and both runners are dependency-free on
purpose, so **the oracle itself runs from a fresh checkout with nothing
installed**.

`scan-toplevel-danger.js` is the one exception: it needs `acorn` for its AST
walk. It is an **audit** step only and `compare.py` never calls it, but it is a
required part of the M3 tooling gate, so its dependency is now pinned in
tracked metadata rather than borrowed:

```sh
npm --prefix route-oracle ci      # exact version from package-lock.json
```

`route-oracle/package.json` and `route-oracle/package-lock.json` are **tracked**;
`route-oracle/node_modules/` is ignored. M3.3 removed the previous fallback onto
`tools/extract-data/node_modules`: `tools/` is git-ignored wholesale, so relying
on it made this audit step unreproducible from a fresh checkout while looking
like it worked.

## Fresh-checkout command

```sh
git clone <repo> && cd pokelike

# 1. build the audited JS prefix (writes route-oracle/out/, git-ignored)
node route-oracle/extract-prefix.js \
     pokelike_forked/js/bundle.deobfuscated.js \
     route-oracle/out/route-prefix.js

# 2. run the oracle
python route-oracle/compare.py --all
```

Step 1 is optional in practice — `compare.py` re-extracts the prefix to a
temp file on every run and refuses to continue unless the freshly extracted
bytes match both `out/route-prefix.js` and the checked-in expected hash in
`prefix.sha256` — but running it once is what creates `out/` the first time.

## Commands

```sh
# every scenario in scenarios/manifest.json
python route-oracle/compare.py --all

# one scenario
python route-oracle/compare.py route-oracle/scenarios/nuzlocke_gen1_loss.json

# determinism / order-independence
python route-oracle/compare.py --all --order reverse
python route-oracle/compare.py --all --order sorted

# machine-readable summary (hashes, RNG counts, divergent indices)
python route-oracle/compare.py --all --json

# write both normalized streams per scenario for inspection
python route-oracle/compare.py --all --dump /tmp/streams

# one side alone, for debugging
node   route-oracle/run-scenario.js route-oracle/scenarios/<name>.json
python route-oracle/run_scenario.py route-oracle/scenarios/<name>.json

# regenerate the prefix, then re-audit it for top-level side effects
node route-oracle/extract-prefix.js pokelike_forked/js/bundle.deobfuscated.js \
     route-oracle/out/route-prefix.js
node route-oracle/scan-toplevel-danger.js route-oracle/out/route-prefix.js   # needs acorn

# author or extend a route fixture from the real generated maps
python route-oracle/plan_route.py --seed 123456789 --align 88888888 --maps 1
```

Harness-level unit tests (canonicalization, hashing, diffing, and mutation
sensitivity) live in `pokelike/tests/test_route_oracle.py` and run inside the
ordinary suite:

```sh
python -m unittest pokelike.tests.test_route_oracle -v
```

## Self-checks

Every `compare.py` invocation hard-fails on:

* a stale prefix (fresh extraction ≠ `out/route-prefix.js`), or a prefix that
  does not match `prefix.sha256`;
* a scenario, manifest, or runner whose `schema_version` disagrees;
* a manifest whose `required_coverage` disagrees with `coverage.REQUIRED_TAGS`,
  or a scenario entry with no `expected_coverage` block, or a scenario listed
  more than once;
* a fixture named by the manifest that does not exist;
* either runner reporting an error;
* any network URL outside the documented allow-list (see below);
* **incomplete route coverage on either runtime**, per-scenario coverage
  evidence that has drifted from the manifest, or the two runtimes earning
  different evidence (`--all` only — see "Machine-enforced coverage");
* any checkpoint divergence.

`scan-toplevel-danger.js` is a separate audit step with its own gate: it fails
on any load-reachable risky global that is neither proved guarded by `typeof`
analysis nor covered by an allow-list entry pinned to the current prefix hash,
and also on a stale allow-list entry that no longer matches anything.

## Offline behavior

The sandbox defines no `localStorage`, `fetch`, `XMLHttpRequest`,
`navigator`, or `Audio` at load time. `driver.js` installs an in-memory
storage stub and a *recording* `fetch` rejector only **after** the prefix has
loaded — the ordering matters and is explained at length in `driver.js`'s
header (defining `localStorage` before load makes the bundle hang, because
two top-level IIFEs gated on `typeof localStorage == "undefined"` otherwise
run).

Two URLs may be attempted and are allow-listed:

| URL | what happens |
|---|---|
| `data/pokedex.json` | never actually requested — the bundle sets `window.__POKEDEX__` itself (bundle.deobfuscated.js:38772) and `loadStaticPokedex` prefers that offline path |
| `https://pokeapi.co/api/v2/pokemon?limit=…` | `fetchSpeciesList` (47955-47968) fails, warns, returns null, and callers take the `FALLBACK_SPECIES_POOL` branch — which is exactly the branch the Python port implements |

Anything else fails the run.

## Route matrix and coverage

| scenario | mode | cps | covers |
|---|---|---:|---|
| `story_gen1_map0_to_map1` | Story / Gen1 | 56 | init, starter selection, map generation + node identity, ordinary trainer, wild battles, catch decline, **evolution** (Bulbasaur→Ivysaur), **boss win**, **map transition**, winning progression |
| `story_gen2_silver` | Story / Gen2 | 42 | Silver **placement and encounter**, boss win, map transition, evolution (Cyndaquil→Quilava), pokecenter, run termination on a non-Nuzlocke loss. Reaches Silver and **loses** — it does not earn the `silver` tag |
| `story_gen2_silver_win` | Story / Gen2 | 54 | **beats Silver** at n4_1 (`silverBeaten` incremented), catch accept, boss win, map transition, terminal loss |
| `nuzlocke_gen1_loss` | **Nuzlocke** / Gen1 | 15 | Nuzlocke init, the Nuzlocke-only layer-1 map shape, catch decline, **game over / run termination**. Wipes with a one-Pokemon party, so nothing is ever culled |
| `nuzlocke_gen1_permadeath` | **Nuzlocke** / Gen1 | 22 | catch accept, **a real permadeath cull** (a member faints in a *won* battle — the only branch that culls), item decline, game over |
| `story_gen4_underground` | Story / Gen4 | 44 | **special-submap entry**, submap generation/topology, saved parent identity + locked parent flags, submap boss **loss**, boss win + map transition on the parent map |
| `story_gen4_submap_full` | Story / Gen4 | 59 | the **complete submap lifecycle**: entry → **boss win** → **pending fossil reward** → **resolved reward** (accept, via the source's own `#swap-incoming .poke-card` listener) → **subexit** → **exact parent restore** → continue on the parent map |
| `story_gen3_admin` | Story / Gen3 | 84 | **Team Magma Admin resolved** at n4_0 on map 2 (Courtney, levels 26/28/27) after surviving two full maps; catch accept/decline, item decline, two boss wins, two map transitions, terminal loss |

Terminal losses are separate scenarios with their own deterministic prefix,
never a fabricated continuation past game over.

### Machine-enforced coverage

`compare.py --all` **fails** unless the observed checkpoints earn all fifteen
required tags in `coverage.REQUIRED_TAGS`, and unless each scenario earns
exactly the evidence indices pinned in `scenarios/manifest.json`. Coverage is
derived from what actually happened (`coverage.derive`), never from a
scenario's own `covers` list — so a source citation, a planned route, a
synthetic fixture and a *losing* attempt all count for nothing. Pinning the
indices is what makes a removed, inserted or reordered checkpoint fail rather
than silently still counting.

**M3.3: `compare.py --all` now derives and enforces coverage independently over
BOTH runtimes**, and additionally requires the two to agree with each other.
Previously only the JavaScript stream was gated here and the Python stream was
checked later, by the ordinary unittest suite — so `compare.py --all` could
pass while the port reached a different set of paths. `RouteCoverageTests` in
`pokelike/tests/test_route_oracle.py` still enforces the Python side
in-process and without node, as a second, independent check.

Required tags: `starter_selection`, `ordinary_trainer`, `silver`, `admin`,
`submap_entry`, `submap_boss_win`, `pending_submap_reward`,
`resolved_submap_reward`, `subexit`, `exact_parent_return`,
`evolution_or_reward_transition`, `map_transition`, `winning_progression`,
`nuzlocke_permadeath`, `terminal_loss`.

### Declared coverage gaps

Stated plainly rather than implied by omission:

* **Distortion submaps are not covered** (maps 3/5/7). M3 requires one
  successful special-submap family, and Underground provides the complete
  lifecycle; Distortion remains untested.
* **`move-tutor`, `trade`, `question`, `legendary` and `shiny` screens have
  no `choice` bridge**, and neither do the three *overlays* that never call
  `showScreen`: `openItemEquipModal` (79419, which is why items are only ever
  declined), `showBranchingChoice`'s `#eevee-choice-overlay` (70560) and
  `showTeamPickerModal`'s `#submap-pick-modal` (76845, the `sacrifice` and
  `stat10` submap rewards). Routes are planned around all of them.
* Consequently, `RunState` phases `MOVE_TUTOR_CHOICE`, `ITEM_EQUIP_CHOICE`,
  `TRADE_CHOICE`, `ESCAPE_ROPE_CHOICE`, `EVOLUTION_CHOICE` and
  `REWARD_TEAM_PICK` are deliberately left out of the phase↔screen projection
  in `run_scenario.py`, so an unexpected one surfaces as `<unmapped:...>`
  rather than being folded onto a plausible-looking screen id.

* **Pending-choice OPTION IDENTITY is not compared.** Both runners report only
  `{phase, optional, option_count}` for a pending choice. Which starter, which
  catch candidate, which item or which incoming/release Pokemon was offered —
  and in what order — is **not** in the schema, so a mutation that keeps the
  option count constant while changing an option's identity or order would not
  be detected. Open M3.3 item; do not read the current agreement on `pending`
  as evidence that the two sides offer the same options.
* **There is no ordered per-turn battle-event projection.** See "Known schema
  limitations" 1 in `SCHEMA.md`: the comparison is winner, round count, RNG
  draws, final per-combatant state, participants and a narrow status-tick
  subset. Acting side/combatant, selected move identity and category, target,
  per-hit damage, crit/faint/switch events and round boundaries are **not**
  compared. Open M3.3 item.
* **The Admin route's provenance is not reproducible from tracked artifacts.**
  `story_gen3_admin.json` was selected by a bounded search that lived in a
  session scratchpad; `plan_route.py` is a **greedy authoring helper** that
  takes a seed as input and cannot search seeds, starters or paths, so it
  cannot re-derive the fixture. Open M3.3 item.
* **All eight fixtures set `align_rng_after_starter_offer`.** An unaligned
  route is not currently replayable at all (see `SCHEMA.md`'s RNG-alignment
  section for the measurement). The pre-alignment divergence is still compared,
  and frozen, at checkpoints 0-1 of every scenario.

These are harness-coverage gaps, not parity claims. Nothing about them is
asserted either way.

## Current result

**Route coverage: 15/15 required tags earned. Parity gate: BLOCKED** on five
bounded, source-backed differences, all frozen as M4 inputs in
`findings/M3-parity-blockers.md` and none repaired:

1. `rng.draws` / `rng.state` — the source's Story starter screen calls
   `rollShiny()` once per offered starter (bundle.deobfuscated.js:76175-76194;
   `rollShiny` always draws, line 74921) for **3 draws** before the player
   clicks; `engine._dispatch_action`'s `CHOOSE_STARTER` branch makes **0**.
2. `map.nodes[N].accessible` — `onNodeClick` locks same-layer siblings
   **eagerly, before** dispatching (77312-77316, ahead of the switch at
   77334); the port locks them only when a node *resolves*.
3. `current_node` — `showSwapScreen` clears `state.currentNode` on all three
   of its exits (79186 / 79231 / 79256); `engine._resolve_swap_choice` leaves
   it set.
4. `event.battle.status_events[len]` — the source logs `sleep_wake` /
   `sleep_skip` status ticks (55687-55710); the port's `_status_tick_round`
   logs burn and poison only. Sleep *is* modelled (same rng draw), so winner,
   rounds, draw counts and final state all agree — this is a logging gap.
5. `counters.any_fainted` — `state.anyFainted` (81372) has no counterpart in
   `RunState` at all.

Findings 3-5 were surfaced by the M3.1 coverage completion; the paths that
reach them did not exist before. Everything else agrees: map topology and node
identity, battle outcomes, round counts, RNG state after the alignment point,
team/level/HP/evolution, items, every other counter and flag, submap entry and
generation, boss wins, reward resolution, subexit and exact parent
restoration, map transitions, and terminal state.

`align_rng_after_starter_offer` is an oracle instrument that isolates the
others from (1); it is symmetric, uses each side's own seeding primitive, and
is documented in `SCHEMA.md`. The M3.1 routes are direct evidence that it
hides nothing: three independent differences surfaced *through* it.

### Audit mode — the frozen parity signature

```sh
python route-oracle/compare.py --all --audit-frozen
```

Exits 0 only when the **complete observed parity signature** equals the tracked
`frozen_signature.json` **and** coverage is complete on both runtimes. It is
**not** a parity mode and never reports parity PASS — a clean audit run still
means PARITY BLOCKED. The default mode remains strict and exits nonzero on any
difference at all.

The signature is a canonical, hashed structure binding the exact manifest
scenario set and each scenario's identity (order-independent), and for every
difference: the scenario filename, the checkpoint index and kind, the
normalized field path, the occurrence count, and a collision-resistant hash of
the ordered JS/Python values — plus one `signature_sha256` over the whole
thing.

**M3.3 replaced a field-name allow-list with this.** The old `--audit-frozen`
compared only the *set of differing field names*, which exited 0 in three
reproduced cases that all should have failed:

| hole | old behaviour | now |
|---|---|---|
| audit a **subset** (one scenario, 3 of 6 paths) | exit 0, and printed "the observed diff set is exactly the frozen M4 finding set" | `--audit-frozen` **requires `--all`** |
| **hide** a redundant scenario from `manifest.json` | exit 0 | `scenario(s) HIDDEN from the run: [...]`, exit 1 |
| change the **count or values** under an already-known path | exit 0 | `COUNT changed` / `VALUES changed`, exit 1 |

A duplicated manifest entry, a difference that *moves* to another checkpoint,
and a difference that appears in a *different scenario* all fail too.

Re-freezing is a separate, deliberate act and never part of a gate:

```sh
python route-oracle/compare.py --all --write-frozen-signature
```

Every changed difference must be traced to source and recorded in
`findings/M3-parity-blockers.md` before re-freezing.

## Files

| file | role |
|---|---|
| `extract-prefix.js` | cuts the audited JS prefix out of the bundle (dependency-free) |
| `scan-toplevel-danger.js` | AST audit of the prefix's load-time side effects: guard analysis + pinned allow-list (needs `acorn`) |
| `toplevel-allowlist.json` | the audited load-time exceptions, pinned to the prefix sha256 |
| `fixtures/scanner/` | adversarial fixtures the scanner must fail, and legitimate ones it must pass |
| `package.json` / `package-lock.json` | pinned `acorn` for the scanner; `npm --prefix route-oracle ci` |
| `frozen_signature.py` / `frozen_signature.json` | the exact frozen parity signature and its comparator |
| `run-scenario.js` | JS sandbox + DOM bridge; loads `driver.js` |
| `driver.js` | the in-sandbox driver: stubs, RNG counter, checkpoint builder, route loop |
| `run_scenario.py` | the Python runner |
| `checkpoints.py` | canonical JSON, hashing, field-level diff (shared with the tests) |
| `coverage.py` | derives the M3 coverage tags from observed checkpoints |
| `compare.py` | the harness entry point, all self-checks, the coverage gate and `--audit-frozen` |
| `plan_route.py` | fixture-authoring helper; not part of either gate |
| `scenarios/` | the route matrix, its `manifest.json` and the pinned coverage evidence |
| `prefix.sha256` | expected prefix hash, checked in for freshness verification |
| `SCHEMA.md` | the versioned checkpoint schema and every exclusion |
