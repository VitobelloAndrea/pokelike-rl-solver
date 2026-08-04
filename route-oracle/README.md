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

# verify a checked-in fixture's coverage claim on BOTH runtimes
python route-oracle/search_route.py verify \
       route-oracle/scenarios/story_gen3_admin.json --target admin

# derive a route that earns a coverage tag, by bounded deterministic search
python route-oracle/search_route.py search --target admin --gen3 \
       --seeds 240 --align 26457513 --max-maps 2 --max-expansions 200000
```

### Deterministic route search (`search_route.py`)

`plan_route.py` is a greedy walker: it takes a seed as input, follows one path
by a fixed type preference, spawns a `node` process per step, and cannot
search seeds, starters or branch points. It cannot re-derive a fragile
multi-map fixture. `search_route.py` is the tracked replacement.

* **`verify <scenario> --target <tag>`** re-runs the scenario through the real
  Python runner *and* the real JavaScript runner and requires
  `coverage.derive` to earn the tag on both, with identical evidence indices.
  This is what makes a checked-in fixture's provenance checkable rather than
  asserted. It is the command that reproduces `story_gen3_admin.json`'s claim.
* **`search --target <tag> …`** runs a depth-first, deterministic, bounded
  search over `(seed, starter, node choice, screen choice)` on the real Python
  engine, then **verifies** the winner against an observed checkpoint stream
  before printing it. It never trusts its own generated action list.

Contract:

| property | how |
|---|---|
| deterministic | no RNG, clock or filesystem scan feeds a decision; nodes are ordered by `(layer, col, id)`, not by dict order |
| order-independent | seeds are canonicalized with `sorted(set(...))`, so `--seeds 242,240,241`, `--seeds 240,241,242` and `--seeds 240-242` search identically |
| bounded | `--max-expansions`, `--max-depth`, `--max-maps`, `--max-choice-options`, `--max-candidates`, `--max-candidates-per-root` and the seed/starter lists are all explicit |
| bounded failure | exhausting the space or a bound prints the bounds and the counters and exits **2** — never a hang, never a silent "no" |
| verified | exit **3** if the emitted route does not earn the tag on a real observed stream |
| source-gated (M3.5) | `--cross-runtime` rejects any candidate the JavaScript source resolves differently and **resumes the walk**, instead of the search having to be right first time. A candidate is admissible only when the JS runner replays it without error, both runtimes derive identical coverage evidence, and every JS-vs-Python difference path is already a frozen blocker path — so a fresh parity finding cannot enter the matrix disguised as tooling work |
| branch order (M3.5) | `--choice-order accept-first` mirrors the default `decline-first`. Targets that need the team to **grow** are unreachable under the default, because depth-first search exhausts the entire decline-everything subtree before it ever accepts a catch. Both orders are total and content-derived, so each is deterministic on its own |
| non-destructive | writing into `scenarios/` is refused without `--allow-fixture-overwrite` |
| cacheable | `--cache PATH` stores the result under a sha256 of *every* search input; a changed seed, target or bound invalidates it, and a tampered digest is not served |
| offline | no sockets, and the only file written is the one named on the command line |

**Measured cost.** Finding the Admin route from the fixture's own seed 240
(gen3, `--max-maps 2`, three starters) takes **0.42 s / 437 expansions**,
including verification. Fully *exhausting* a seed is the expensive direction:
ten seeds at `--max-maps 0` take **9.3 s** (~0.93 s per single-map seed), and
the tree grows quickly with `--max-maps`. `RouteSearchTests` in
`pokelike/tests/test_route_oracle.py` covers the whole contract with one
bounded search (~20 s) and stays in ordinary discovery; a wide sweep such as

```sh
python route-oracle/search_route.py search --target admin --gen3 \
       --seeds 0-999 --align 26457513 --max-maps 2 --max-expansions 100000000
```

is **not** part of discovery — run it deliberately and expect minutes to
hours depending on how early a solution appears.

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

**What counts as load-reachable** — widened by M3.5 to close M3.4 Defect C,
which demonstrated three blind spots with fresh adversarial inputs:

| form | before M3.5 | now |
|---|---|---|
| `(function(){…})()`, `(()=>{…})()`, `.call(…)`, `.apply(…)` | flagged | flagged |
| `(function(){…}).bind(this)()`, and `.call`/`.apply` on a bound function | **missed — reported 0 references** | flagged (`unwrapBound`) |
| `setTimeout` / `setInterval` / `queueMicrotask` / `requestAnimationFrame` / `addEventListener` … at load | **missed — not in the risky set at all** | flagged (`RISKY_SCHEDULERS`) |
| `window["fetch"]`, `g["localStorage"]` | **missed — while dotted access on the same object was caught** | flagged (`staticPropertyName`) |

Schedulers are kept separate from the data/IO globals and count only as a bare
identifier or off an explicit global root (`window.setTimeout`), because
`document.addEventListener(…)` is already fully accounted for by its
`document` reference — flagging the method name too would double-count it and
would wrongly report `typeof document !== "undefined" && document.addEventListener(…)`
as unguarded.

The wider net surfaced **two real references the previous scanner never
reported**: `setTimeout(O, 5000)` and `setInterval(O, 30000)` at prefix lines
38810-38811, which re-arm the bundle's anti-clone hostname check. They execute
on every oracle run — the enclosing `typeof window != "undefined"` guard names
`window`, and the sandbox *does* define `window` — and are inert only because
`run-scenario.js` binds both timers to `() => 0`. That is recorded as an
allow-list entry with its reason, not proved away. The real prefix now reports
**21** references (6 proved guarded, 15 allow-listed) and still exits 0.

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
| `story_gen1_map0_to_map1` | Story / Gen1 | 38 | the ordinary-progression spine: starter selection, map generation + node identity, wild/trainer battles, catch decline, boss win, map transition, winning progression |
| `story_gen2_silver` | Story / Gen2 | 44 | Silver **placement and encounter**, then a **loss** there — it proves the Gen2 rival placement without the win branch, and earns `terminal_loss`, not `silver` |
| `story_gen2_silver_win` | Story / Gen2 | 43 | **beats Silver** (`silverBeaten` incremented) — the scenario that earns the `silver` tag |
| `nuzlocke_gen1_loss` | **Nuzlocke** / Gen1 | 13 | Nuzlocke init, the Nuzlocke-only layer-1 map shape, catch decline, game over. Wipes with a one-Pokemon party, so nothing is ever culled |
| `nuzlocke_gen1_permadeath` | **Nuzlocke** / Gen1 | 11 | a real permadeath **cull** (a member faints in a *won* battle — the only branch that culls) and therefore the primary observation of `any_fainted` |
| `story_gen4_underground` | Story / Gen4 | 45 | special-submap **entry**, submap generation/topology and the saved locked parent, observed on a route that **loses** the submap boss and never returns |
| `story_gen4_submap_full` | Story / Gen4 | 53 | the **complete submap lifecycle**: entry → boss win → pending reward on the source's own `showSwapScreen` → resolved reward → subexit → **exact parent restore** → continue on the parent |
| `story_gen3_admin` | Story / Gen3 | 73 | **Team Magma Admin resolved** through `doAdminNode` with the run still alive |
| `story_gen1_swap_release` | Story / Gen1 | 73 | grows the team to **six**, then really clicks a release card, so `showSwapScreen`'s full-team **replace** branch (79202-79246) is exercised |
| `story_gen3_sleep_ticks` | Story / Gen3 | 18 | the matrix's **sleep** observation — both `sleep_wake` and `sleep_skip`, so a mutation omitting either is killed on its own |
| `story_gen3_mirror_coat` | Story / Gen3 | 76 | the matrix's **Mirror Coat counter-hit** observation — 9 counter-hit attack events |

Every fixture is **unaligned** and every one was derived or verified with
`search_route.py --cross-runtime`, so the JavaScript source replays it, both
runtimes derive identical coverage evidence, and the difference set is empty.

Terminal losses are separate scenarios with their own deterministic prefix,
never a fabricated continuation past game over.

### Machine-enforced coverage

`compare.py --all` **fails** unless the observed checkpoints earn all sixteen
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

Stated plainly rather than implied by omission.

**Closed since M3:**

* **`question`** — 4 cross-runtime resolutions; `resolveQuestionMark` runs on
  both runtimes and the resolved types agree at zero difference.
* **Underground submaps** — the complete lifecycle plus a boss-loss route.
* **The swap-screen submap reward branch** (`fossil` / Distortion legendaries)
  — pending and resolved, through the source's own listener.

**Closed by the M4 repair** (`docs/prompts/M4-repair.md`, driven by
`docs/audits/M4-independent-closure-audit.md`'s FAIL finding): every family
that audit found unbridged or unrouted now has both a real `choice` bridge
*and* cross-runtime route evidence, not just source-traced Python:

* **`legendary` and `shiny` nodes** — the ordinary-legendary lifecycle (win +
  room accept/decline, win + full-team replace/decline) and shiny-node
  accept/decline all have real routes, verified zero-difference on both
  runtimes.
* **`move-tutor` and `trade`** have real `choice` bridges (`driver.js`'s
  `shiny-screen`/`trade-screen` branches, `run_scenario.py`'s matching
  `_pending_projection` cases) and routed accept/decline/tier-boundary
  scenarios.
* **Distortion submaps** — entry, boss win, boss loss, subexit, exact parent
  restoration, continued parent progress, and the guaranteed-legendary reward
  branch (`distortion_reward_resolved` — only reachable on a run's
  **second-ever** Distortion visit, see `_distortion_legendary`,
  `pokelike/map_gen.py:912-927`) are all routed and verified.
* **The `sacrifice` and `stat10` reward branches** now route through the real
  team-picker bridge (`showTeamPickerModal`'s `#submap-pick-modal`, detected
  directly via DOM presence, never inferred from `currentScreen`).
* **The three overlays that never call `showScreen`** — `openItemEquipModal`
  (79419, shared `#item-equip-modal` id with `doMoveTutorNode` at 80464,
  disambiguated by button family, not by which node type opened it),
  `showBranchingChoice`'s `#eevee-choice-overlay` (70560), and
  `showTeamPickerModal`'s `#submap-pick-modal` (76845) — all three now have
  real bridges and routed scenarios.
* Consequently `RunState` phases `MOVE_TUTOR_CHOICE`, `ITEM_EQUIP_CHOICE`,
  `TRADE_CHOICE`, `EVOLUTION_CHOICE` and `REWARD_TEAM_PICK` are folded into
  the phase↔screen projection via `run_scenario.py`'s `_screen_for` (see
  SCHEMA.md's Phase↔screen table for the exact source citation behind each).

**Still open — no parity claim is made in either direction:**

* **`ESCAPE_ROPE_CHOICE`** still has no `choice` bridge and no route through
  it — explicitly out of the M4 repair's scope, unchanged from M3.

These are harness-coverage gaps, not parity claims.

## Current result

**Route coverage: 32/32 required tags earned, independently on both runtimes.
Parity gate: PASS — `python route-oracle/compare.py --all` exits 0 with 24/24
scenarios agreeing checkpoint-for-checkpoint, including RNG state and draw
counts, in manifest, reverse and sorted execution order.**

M4 repaired the five bounded differences M3 had frozen, plus two more:

1. **starter offer** — `Engine.reset` now materialises three real starter
   instances and consumes the same three `rollShiny()` draws the source's
   `showStarterSelect` does (bundle.deobfuscated.js:76175-76194), and
   `ChooseStarter` installs the offered object rather than rebuilding it, so a
   shiny starter is reachable and the Stream-B offset is gone;
2. **eager sibling locking** — `_visit_node` locks already-accessible
   same-layer siblings at `onNodeClick`'s own point (77312-77316), before
   dispatch, so a suspended choice screen or a run-ending loss observes it;
3. **`current_node`** — `_resolve_swap_choice` clears it on all three
   `showSwapScreen` exits (79186 / 79231 / 79256), while `catchPokemon`'s room
   path deliberately still does not;
4. **sleep ticks** — the whole pre-turn `status_tick` family
   (`flinch`, `freeze_skip`, `sleep_wake`, `sleep_skip`, 55647-55710) is now
   emitted; behaviour was already correct, only the log was missing;
5. **`any_fainted`** — a real `RunState` field, set only when a won Nuzlocke
   battle actually culls (81371-81372);
6. **ordinary-map `LEGENDARY`** — `doLegendaryNode`'s win callback ends in an
   unconditional `showSwapScreen` (80457), so the incoming legendary is now
   pending until an explicit accept or decline even with room. Ordinary catch
   (79036) and shiny (80962) keep their room-based auto-add;
7. **Mirror Coat counter-hit** — the source pushes it as a real `type:
   "attack"` entry (58108-58142); the port applied the damage silently. Found
   because the tightened cross-runtime search gate rejected nine candidate
   routes on it.

**`align_rng_after_starter_offer` is retired.** It was an instrument that
re-seeded past repair 1's divergence; with that repaired it isolates nothing,
and no fixture sets it. Seven routes were re-derived unaligned with
`search_route.py --cross-runtime`; a focused test asserts no fixture carries
the key.

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
| `fixtures/scanner/` | adversarial fixtures the scanner must fail, and legitimate ones it must pass. Every one is run and asserted by `pokelike/tests/test_scanner_fixtures.py` in ordinary discovery |
| `package.json` / `package-lock.json` | pinned `acorn` for the scanner; `npm --prefix route-oracle ci` |
| `frozen_signature.py` / `frozen_signature.json` | the exact frozen parity signature and its comparator |
| `run-scenario.js` | JS sandbox + DOM bridge; loads `driver.js` |
| `driver.js` | the in-sandbox driver: stubs, RNG counter, checkpoint builder, route loop |
| `run_scenario.py` | the Python runner |
| `checkpoints.py` | canonical JSON, hashing, field-level diff (shared with the tests) |
| `coverage.py` | derives the M3 coverage tags from observed checkpoints |
| `compare.py` | the harness entry point, all self-checks, the coverage gate and `--audit-frozen` |
| `plan_route.py` | greedy fixture-authoring helper; not part of either gate |
| `search_route.py` | deterministic bounded route search + both-runtime fixture verification; not part of either gate |
| `scenarios/` | the route matrix, its `manifest.json` and the pinned coverage evidence |
| `prefix.sha256` | expected prefix hash, checked in for freshness verification |
| `SCHEMA.md` | the versioned checkpoint schema and every exclusion |
