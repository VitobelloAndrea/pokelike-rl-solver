# SWEEP.md — the M7 cross-runtime convergence sweep

The frozen route oracle (`compare.py`, `SCHEMA.md`) replays **29 pinned action
lists** and compares the resulting checkpoint streams. That is strong evidence
about those 29 routes and says nothing about any other. The M7 sweep holds both
runtimes at the *same step* and, at every step:

1. enumerates each runtime's normalized **legal action set**;
2. compares those sets **before anything chooses**;
3. executes the same chosen action through **each runtime's real path**;
4. compares the normalized state and battle evidence **after every action**;
5. records coverage against a checked-in, code-derived **target manifest**;
6. saves a deterministic replay at the first divergence and **minimizes** it.

Neither side is a reimplementation. Python reads legality off
`engine.legal_actions` and steps the real `pokelike.engine`; the source side
enumerates from the source's own run state, active screen/overlay and real
click handlers, and executes by invoking those same handlers.

## Files

| file | role |
|---|---|
| `sweep.py` | the CLI, the Python legal-action adapter, comparison, coverage, replay, minimization |
| `sweep-host.js` | node process; line-delimited JSON protocol; one fresh VM context per episode |
| `sweep-adapter.js` | the **source-side** legal-action adapter and action executor, in-sandbox |
| `sweep-targets.json` | the checked-in coverage **target** manifest |
| `sandbox.js` | the VM sandbox, extracted verbatim from `run-scenario.js` so both hosts build it from one definition |
| `findings/M7-divergence-*.json` | durable reproducers, one per diverging episode |
| `find-sweep-fixtures.py` | **M7.0** — derives the checked-in adapter fixtures by lockstep search |
| `fixtures/sweep/*.json` | **M7.0** (six) + **M7-combined A3** (three) — one checked-in fixture per focused adapter surface |

The tool's own evidence gate is itself tested, by two checked-in files:

| test file | what it holds the tool to |
|---|---|
| `pokelike/tests/test_sweep_tool.py` | the comparison projection, the episode digest, the coverage denominator and the coverage accounting — dependency-free |
| `pokelike/tests/test_sweep_adapter.py` | nine focused CROSS-RUNTIME adapter fixtures, one per action family; needs `node` and the bundle |
| `pokelike/tests/test_m7_repairs.py` | the focused regressions for the runtime repairs F-B, F-C, F-D and F-E — dependency-free |

`driver.js` gained exactly one additive branch (`if (SC.sweep)`) plus its
closing brace; the pre-M7 fixed-scenario route below it is unchanged, and the
frozen signature and 29/29 strict result are the proof.

## Commands

```sh
python route-oracle/sweep.py validate-targets
python route-oracle/sweep.py plan --episodes 200 --max-steps 120 --out plan.json
python route-oracle/sweep.py run  --plan plan.json --out random.json
python route-oracle/sweep.py run  --corpus [--guided] --out corpus.json
python route-oracle/sweep.py run  --corpus --order reverse|sorted   # order-independence
python route-oracle/sweep.py replay --record findings/<name>.json [--minimize]
python route-oracle/sweep.py replay-set --records findings/*.json --out replays.json
python route-oracle/sweep.py coverage --external corpus.json random.json guided.json
python route-oracle/sweep.py hunt --from corpus.json random.json --external --episodes 400
python route-oracle/sweep.py search --goal reward:fossil --buckets story_gen4 \
       --episodes 30 --base-seed 20260824 --max-steps 45 --max-expansions 800 \
       --verify --out search.json
python route-oracle/sweep.py search ... --no-steer     # shuffled order instead
```

`run` exits non-zero if any episode diverged; `coverage` exits non-zero if any
required target is unearned; `validate-targets` exits non-zero if the manifest
is inconsistent with the code it claims to derive from.

## Bounded search, and why its snapshot has to be honest

`hunt` spends its budget on whole random episodes. That stops working once the
remaining targets are deep and conjunctive: the distortion legendary rewards
need a gen4 run to reach its *second* distortion world and then take one
specific node, which a per-episode random policy essentially never assembles.

`search` explores the action tree instead — it steps the Python engine forward
and, on a dead end, **backtracks** to an ancestor and tries a different action
from there.

### The snapshot defect this section exists to prevent

Backtracking is only sound if restoring a snapshot restores everything the next
`step()` reads — above all the RNG.

`engine.Engine` owns a **private** `rng.Mulberry32` (`_rng_stream`,
`engine.py:677`) and installs it as `pokelike.rng`'s *active* stream only for
the duration of each `reset()`/`step()` call, restoring the previous active
stream on the way out (`engine.py:718, 789`). So **outside** a step — exactly
when a search snapshots — `rng.get_rng_seed()` does not read the engine's
stream at all. It reads the module-level `_stream_b` default, which the engine
never touches:

```python
e = engine.Engine(); e.reset(seed=12345)
e._rng_stream.state   # 1199742488  -- the engine's real position
rng.get_rng_seed()    # 0           -- the untouched module default
```

A snapshotter written as `(deepcopy(engine.state), rng.get_rng_seed())`
therefore captures a constant and restores it into the wrong object, leaving
`engine._rng_stream` wherever the abandoned branch advanced it. Re-running the
same action from the "same" restored state then draws from a different RNG
position and produces a different outcome — the search gets **free re-rolls**,
and the routes it emits do not reproduce when replayed.

`sweep.engine_snapshot` / `sweep.engine_restore` are the honest form: they
snapshot and restore the engine's own stream by value. `Mulberry32` holds a
single 32-bit word and `seed()` sets it raw (`rng.py:92-115`), so
`stream.seed(stream.state)` is an exact identity round-trip and the pair is a
complete checkpoint. `pokelike/tests/test_sweep_search.py` pins this
executably and keeps the broken form checked in so the detector is
*demonstrated firing*, not merely asserted.

### A candidate is not evidence

`search` never touches the source runtime, so it is fast but **proves
nothing**. Its output is a *candidate*: a `(seed, mode, actions)` plan, the
same shape a retained reproducer has. Nothing is credited until that plan is
replayed through `run_episode` — the real lockstep loop — where both runtimes
are compared step by step and `observe_coverage` runs only on steps that
already agreed. `--verify` performs that replay; without it the emitted
candidates are inert and the command says so.

Because the search proposes only actions the *port* reports legal, a candidate
can still turn out to diverge on replay. When it does, that replay is a
**finding**, not a failure of the search.

### Steering: exploration order, and nothing else

A shuffled DFS is enough for a shallow goal and useless for a deep one.
`reward.fossil` is the worst case in the manifest: `SUBMAP_REWARDS` gives
`fossil` `kinds = ("underground",)` alone, and `map_gen` places UNDERGROUND
only on gen4 **layer 4 of maps 1/3/6** (`map_gen.py:465-495`). The goal
therefore sits behind a gym leader, a map advance, four more layers and a
submap boss. Measured on the current tree: 4 runs x 4 000 expansions, **zero
candidates**.

`search` now takes a **priority** (order) and a **prune** (budget) per goal:

* the **priority** sorts each frame's already-enumerated legal set. It cannot
  add an action and cannot drop one -- the tree being searched is the tree the
  unsteered search explores, only visited in a different order. The ladder is
  `hunt_policy`'s (*survival before ambition*, for the reason recorded there)
  restated over the port's own run state, since a Python-only search has no
  compared checkpoint to read. Its one measured rung: ranking CATCH above
  TRAINER while the roster is short took 80 greedy gen4 rollouts from 18 to
  27 reaching map 1;
* the **prune** declares a reached state's subtree not worth the budget, and
  is handled exactly like `game_over` -- the frame is not pushed and the
  search backtracks. For a `reward:<id>` goal it fires when the run is
  standing inside a submap whose generated reward nodes do not carry `<id>`.
  `generate_sub_map` bakes those ids at entry (`_pick_sub_map_rewards`,
  `map_gen.py:1005-1024`) and nothing rewrites them afterwards, so re-rolling
  from a different RNG position is the only way to get a different draw --
  `fossil` occupies one of the two random slots roughly **27%** of the time
  (8-id pool, 2 kept). Without the prune the search sinks its whole budget
  into the first submap it happens to enter.

The prune makes the search deliberately **incomplete**: a run really could
leave a fossil-less underground and reach a later one on map 3 or 6, and this
gives that route up. That is a trade a *proposer* is allowed to make, and only
a proposer -- `search` still never touches the source runtime and still
credits nothing until `--verify` replays the candidate through `run_episode`.

`--no-steer` restores the shuffled order. `SearchSteeringIsOrderOnlyTests`
(`pokelike/tests/test_sweep_search.py`) pins that the priority is a
*permutation* of `py_legal_actions`, that the submap kinds come from
`SUBMAP_REWARDS` rather than from a list restated in the search, that the
pruner fires only inside a submap actually missing the reward, and that a
steered candidate still replays identically in a fresh engine.

**`reward.fossil` was earned this way.** Twenty-five searched runs in,
`search_story_gen4_3678945229` found a 20-action route at depth 20, and the
`--verify` replay compared 20 action steps with **zero** divergence,
crediting `reward.fossil` at `search_story_gen4_3678945229#19`: a gen4 Story
run straight through map 0, one `advance_map`, map 1 to the layer-4
UNDERGROUND node, the submap boss `n1_1`, then `n2_1` -- a REWARD node
carrying `fossil`, landing on the `swap-screen` that `showSwapScreen` puts up
(bundle.deobfuscated.js:77065-77078). Retained as
`findings/M7-target-reward_fossil.json`.

## The distortion wild-legendary node, and the offline harness boundary

Two retained records —
`findings/M7-divergence-distortion_wildboss_giratina_*.json` — diverge at a
`visit_node n1_1` inside a gen4 distortion sub-map, with `battles[len]` js 0
against py 1. They are **not port defects**. The trace:

| step | source | line |
|---|---|---|
| a distortion sub-map with a legendary places `n1_1` as the **wild** boss | `generateSubMap` | 53553-53560 |
| its `bossTeam` is `[{id: B6M.bossId, …}]` | | 53559 |
| `DISTORTION_LEGENDARY_POOL` — dialga `0x1e3`, palkia `0x1e4`, **giratina the string `"giratina-origin"`** | | 76382-76396 |
| `fetchPokemonById` serves *numeric* ids from the static pokedex … | | 48623-48639 |
| … and sends every *string* id to the live PokeAPI | | 48651-48654 |
| on failure it catches and returns `null` | | 48713-48718 |
| `doSubMapBoss` filters the nulls, and an empty team takes the early return — `advanceFromNode`, `showMapScreen`, **no battle** | | 76780-76786 |

`route-oracle` guards the network in two places: `sandbox.js:267-269`
deliberately leaves `fetch` **undefined**, and `driver.js:191-193` -- which
`sweep-host.js` loads unchanged into that sandbox (`sweep-host.js:69, 116`) --
then defines it as a stub that records the URL and **rejects**. The observed
error string is the driver stub's, and the source runtime prints it on exactly
the diverging action:

```text
Failed to fetch pokemon giratina-origin
Error: route-oracle: network disabled (https://pokeapi.co/api/v2/pokemon/giratina-origin)
```

**The control is decisive.** The same `n1_1` wild-boss node with a *numeric*
`bossId` compares clean cross-runtime in the retained target findings:
`legend_3581912727` (Dialga, 483) visits `n1_1` at action 60 and
`legend_1743692343` (Palkia, 484) at action 64, both with zero divergence.
Only the string-id form diverges, which isolates the cause to the
network-guarded lookup rather than to the port's boss or battle path.

The port resolves the same form locally from `data.get_giratina_origin_form()`
(a deliberate, cited decision — see `data.DistortionLegendaryEntry`) and fights
the battle, which is what the **live** game does. Making the port skip it would
match the offline harness rather than the game, so **no port change was made**.
This is the same shape as F-F — a harness limitation modelled as game
behaviour — except that here the harness cannot be repaired faithfully without
inventing a PokeAPI response, which would put non-extracted data inside the
*source* runtime and destroy the oracle's independence.

Retained and classified `harness-boundary-divergence`; the accepted boundary
policy is recorded below and does not erase the observed differences.

## M7 scope disposition: Story/Nuzlocke only

M7's declared surface is Story/Nuzlocke Gen1-4. Endless and Challenge modes are
future scope, not hidden gaps in this gate. Accordingly, the target manifest
explicitly excludes these two targets for M7:

| target | M7 disposition | reason |
|---|---|---|
| `battle.immune` | excluded for M7 | Story/Nuzlocke replaces an immune main move with typeless Struggle before a compared `typeEff == 0` attack event; the remaining zero-producing sites require passives unavailable on this surface. |
| `battle.status_poison_drain` | excluded for M7 | the source requires the player's `poison_drain` passive, and Story/Nuzlocke has no live passive-acquisition path. |

This is a **scope disposition**, not a claim that the game can never produce
these events. Endless/Challenge work must restore both targets to the required
denominator when those modes enter scope. The M7 denominator is now **163 total
/ 154 required / 9 excluded**.

The final 154/154 coverage snapshot is retained at
`evidence/M7-story-nuzlocke-coverage.json` rather than only under the
gitignored `out/` directory. `SweepDenominatorTests` pins that snapshot's
earned and excluded ids to the live manifest, so either side changing makes
the ordinary test suite fail.

## Accepted harness boundary: Giratina wild boss

The two retained Giratina `n1_1` records remain visible and reproducible. They
are accepted as an oracle-contract boundary, not erased as zero differences:
the offline source runtime rejects the PokeAPI lookup for the string
`giratina-origin`, while the port uses its deliberate local canonical data. The
numeric Dialga/Palkia controls are clean. The final replay report must separate
these two accepted `harness-boundary-divergence` records from any unexpected
port divergence; any new unexpected divergence remains a failure.

## The normalized action vocabulary

One dict shape per action, identical on both runtimes. The canonical comparable
form drops `__prov` (provenance), which each side derives from its own code
path and which is therefore **reported, never compared**.

| kind | fields | source affordance |
|---|---|---|
| `choose_starter` | `species_id` | `showStarterSelect` card → `selectStarter` (76176-76186) |
| `visit_node` | `node_id` | `onNodeClick(node)` (77312+) |
| `advance_map` | — | `showBadgeScreen` `#btn-next-map` |
| `select_option` | `index`, `cancel` | every choice screen/overlay; see the exit table below |
| `reorder_team` | `i`, `j` | team-bar drag **swap** (64798-64806) |
| `use_item` | `item_id`, `bag_index`, `target_index` | `applyUsableItemTo`, gated by the source's own `usableItemCanTarget` (64946-64948) |
| `equip_item` | `item_id`, `bag_index`, `team_index` | `equipItemFromBag` (64950 → 79653-79671) |
| `unequip_item` | `team_index` | `openItemEquipModal(fromPokemonIdx)` `[data-unequip]` (79521-79531) |
| `hand_off_item` | `from_index`, `to_index` | the same overlay's `[data-idx]` hand-off (79541-79545) |

### `select_option` has three distinct exits

| normalized | source | meaning |
|---|---|---|
| `index=N, cancel=false` | the Nth built card/button | pick option N |
| `index=null, cancel=false` | `#btn-skip-*`, or `#btn-equip-to-bag` | skip / decline / **bank** |
| `index=null, cancel=true` | `#btn-equip-cancel` (79563-79569) | **neither** — the whole handler body is `B2O.remove()` |

`cancel` is legal only for `Phase.ITEM_EQUIP_CHOICE`, on both sides.
`engine.legal_actions` did not declare it before M7 even though
`_resolve_pending` had accepted it since M5; correcting that declaration is
M7's single permitted `engine.py` change, and it alters no state and no RNG.

### `index` means POSITION, not a `data-*` value

`select_option.index` is the **position in the agreed pending option list**.
The source's `data-tutor` / `data-idx` attributes carry the **team** index,
and the two differ whenever a member is filtered out of the offer — a move
tutor with one already-mastered member builds a single button `data-tutor="1"`
while the port's `PendingChoice.options` holds a single option at position 0.
The member's identity travels in the compared option's `slot`, never in the
index. (Finding **T2**; the enumeration and the executor must use the same
rule, and both now use position.)

### `reorder_team` is a transposition, not a permutation

`engine.legal_actions` reports `{"team_size": n}`, i.e. "any permutation".
The source's only reorder affordance is the team-bar drag handler, whose entire
mutation is `[team[a], team[b]] = [team[b], team[a]]`. The compared domain is
therefore the transpositions `(i, j)`, `i < j` — the source's atomic action,
expressible on both sides, and a strict subset of what `ReorderTeam` accepts.
Enumerating all `n!` permutations was rejected explicitly: it would satisfy an
API shape while proving nothing the transpositions do not — and, since only the
port could ever offer them, every extra element would be a guaranteed
legal-set divergence describing the Python API rather than the game.

**Finding F1 is RESOLVED as of M7-combined (A2)**, in the source's favour, and
the resolution is checked rather than merely written down. `sweep.py` declares
the reduction as `REORDER_DOMAIN` and performs it in `reorder_transpositions`,
which **asserts** that `legal_actions` still declares the wider permutation
form it is reducing from — so a redeclaration fails loudly instead of quietly
enumerating something else. `SweepReorderDomainTests` pins both halves of the
breadth difference: that the engine really does execute a non-transposition
permutation (a 3-cycle), and that no source affordance can produce one. The
wider capability is unreachable here by **construction**, not by filtering:
`py_reorder_action` only ever builds the identity order with exactly two
positions exchanged, so there is no permutation to intersect away. The
cross-runtime half is the checked-in `six_member_team` fixture, which exercises
all 15 transpositions of a full team on both runtimes.

## Presentation-only dismissals

Two source screens park the run on a button that carries no decision. They are
auto-pressed and never offered as legal actions, because the port has no
counterpart screen and comparing one would report a divergence where the
runtimes agree:

* `#btn-continue-battle` — resolves `runBattleScreen`'s promise
  (81384-81387 / 81427-81429). `driver.js`'s `drive()` already pressed it.
* `#btn-trade-continue` — `completeTrade`'s receipt (80818-80860). The trade is
  already finished when it appears: the team splice (80825),
  `state.savedTrade = null` (80826), `recordMonOrigin` (80827) and
  `advanceFromNode` (80841) all run **before** `showScreen("shiny-screen")` at
  80846, and the button's whole handler is `() => showMapScreen()` (80859).
  (Finding **T1**.) The receipt shares the `shiny-screen` id with
  `doShinyNode`'s real choice (80937); the two are told apart by template text,
  and those are the only two `showScreen("shiny-screen")` sites in the bundle.

### A real overlay is a decision, never a dismissal

The auto-press loop quiesces the moment `detectOverlay()` reports one of the
three `showScreen`-less overlays. This is not a nicety — it was a live deadlock
found by the A4 scheduler the first time it reached a **branching evolution**.
`checkAndEvolveTeam` runs inside `runBattleScreen`'s win branch (81381), so
`#eevee-choice-overlay` goes up while `currentScreen` still says
`battle-screen`; the loop therefore kept re-pressing `#btn-continue-battle`,
kept getting a truthy click back, and span until its bound tripped. Every
branching evolution reached that way was reported as an
`apply_error_asymmetry` instead of the choice it actually is.
`#btn-continue-battle` has already done its job by then — resolving the battle
promise is what let the evolve step run at all — so pressing it again dismisses
nothing.

## The virtual clock (M7-F-F)

The harness has no real time in it. `driver.js` replaces `setTimeout` /
`setInterval` / `requestAnimationFrame` with a **virtual timer queue** so that
repeated runs are byte-identical, and `sandbox.js:265` supplies
`performance = { now: () => 0 }`.

Until F-F that queue discarded the requested delay outright and left the clock
stopped at 0. The header claimed "nothing in a Story/Nuzlocke route depends on
real elapsed time". **That claim was false**, and finding F-F is the
counterexample.

### What F-F actually was

Reported as `apply_error_asymmetry`, `pump did not quiesce after 5000 rounds`,
on the `visit_node` that enters map 8. It was neither a port defect nor an
adapter-boundary error. The Elite Four gauntlet **completed**: all five fights
resolved, `doElite4`'s tail ran and `showWinScreen` (81631) came up. The
harness then refused to settle *on the win screen*:

| step | source | line |
|---|---|---|
| 1 | `showWinScreen` awards pokedollars | 81649 |
| 2 | `addPokedollars` -> `onPokedollarsGained` | 48921, 75236 |
| 3 | `animatePokedollarGain` arms a `setTimeout(..., 0x46)` | 75242-75258 |
| 4 | `_spawnPokedollarBurst` builds 5-14 coins, each with its own `delay` and `dur` | 75281-75330 |
| 5 | its frame callback re-arms `requestAnimationFrame(B2Q)` while ANY coin has `(_pdNow() - start - delay) / dur < 1` | 75343-75400 |
| 6 | `_pdNow` prefers `performance.now()` | 75189-75194 |

With `performance.now()` pinned at 0 the ratio is never 1, no coin ever
completes, `B2Q` re-arms forever and `pump()` cannot quiesce. A finite,
wall-clock-bounded presentation animation that terminates in ~1.5 s in a real
browser was modelled as non-terminating. Raising the pump bound could never
have helped, and the previous cycle's 200 000-round experiment confirmed that
empirically.

`showWinScreen` is the **only** `addPokedollars` call site the declared
Story/Nuzlocke surface reaches — 81544 is `state.isEndlessMode`-gated, and
87540 / 87725 / 87868 / 89108 are egg-hatch, Endless, Challenges and pokechain
— which is exactly why no other route in 858 episodes ever tripped it.

### What the queue does now

* `setTimeout(fn, delay)` **records** the delay as a due time instead of
  dropping it;
* `pump()` advances `virtualNow` **monotonically** to each callback's own due
  time before invoking it — the real `setTimeout` guarantee that at least
  `delay` has elapsed;
* `performance.now()` reads that clock;
* `requestAnimationFrame(fn)` schedules a real frame interval (1000/60 ms) and
  passes the `DOMHighResTimeStamp` its contract requires. The old shim was
  `setTimeout(fn, 0)` and invoked `fn` with **no arguments at all**, so every
  source loop that measured progress from its rAF argument saw `undefined`,
  computed `NaN`, and retired after exactly one frame by accident.

Three things are deliberately **unchanged**, and the frozen 29-scenario
signature is why:

* **FIFO order.** Timers still run in scheduling order, never due-time order.
  A delay affects the clock a callback observes, never when it runs.
* **`Date.now`**, still pinned to `SC.seed` for `startNewRun`'s own seed
  expression (75455). `_pdNow` only falls back to it when `performance` is
  absent, and `sandbox.js` always defines `performance`.
* **The 5000-round pump bound.** It is not the fix and was not touched. A
  clock-driven animation now retires on its own frame count (the pokedollar
  burst's worst case is `(12*45 + 900) / (1000/60)` ~= 87 frames), while a
  loop that re-arms with no termination condition still trips the bound and
  still fails loudly.

`DriverVirtualClockTests` (`pokelike/tests/test_sweep_adapter.py`) pins all of
it against the **real `driver.js` text**, sliced out of the file rather than
restated, so it cannot pass against a driver that no longer contains the shim.
Four of its seven tests fail against the pre-repair driver; the three that
pass under both are precisely the invariants listed above as preserved.

### Two port defects F-F had been hiding

The deadlock meant the gauntlet step's state had never been compared even
once. With the clock repaired, the same two reproducers immediately reported
real divergences, both repaired in `engine.py`:

* **F-H — the Elite Four fights at move tier 2.** Both gauntlet loops spread
  `createInstance(it, it.level, false, 0x2)` with the tier **hardcoded** —
  `doElite4` at 77859-77862 and `doGen2Elite4` at 78361-78366 — unlike
  `doBossNode`'s gym branches, which read `it["moveTier"] ?? 1` (77758-77763,
  77812-77817). No Elite Four table entry carries a `moveTier` field, so the
  port's shared `?? 1` fallback fought the whole gauntlet a tier low: js
  `Hydro Pump` for 46 against py `Surf` for 37, on turn 0 of battle 0.
* **F-I — the gauntlet tail.** `doElite4` (77887-77893) never writes
  `eliteIndex` again, so the value its loop header last wrote (77855) survives
  into the win screen; only `doGen2Elite4` clears it (78394). And **neither**
  tail calls `advanceFromNode` (53639) — `doBossNode`'s map-8 branches are a
  bare `await doElite4(); return;`, and `onNodeClick` only locks the clicked
  node's same-layer siblings (77312-77316). So on the source the Elite Four
  node ends the run still `accessible` and still un-`visited`. The port
  reported `eliteIndex` 0 against the source's 4, and `visited=true,
  accessible=false` against `visited=false, accessible=true`.

### Two more the deeper sweep then found

Repairing F-F let the hunt run far deeper, and 1 500 fresh episodes reported
two further real port defects. Both are repaired, with their own retained
reproducers:

* **F-J — `afterAttack` is gated on the hit having connected.** The source
  computes `BEs = BEq - BEf["currentHp"]` (the HP the target actually lost,
  after the sturdy / Focus-Sash / Fighting-Spirit clamps) and calls
  `afterAttack` only `if (BEs > 0x0 && ...)` (55998-56001). `whenAttacked`
  immediately above it is deliberately ungated. Both extra-attack sites
  already carried the gate on their own actual damage (56264-56282,
  56343-56362) and so did the port; the MAIN hit was the one place the port
  called the hook unconditionally. It matters whenever a hit computes damage
  but removes no HP — the observed case is `wonder_guard`, which `calcDamage`
  zeroes for a non-super-effective, non-critical hit (55063). A Seviper's Acid
  on a Shedinja deals 0, so the source never runs `afterAttack` and Seviper's
  `venom_strike` never poisons it. The port ran the hook, put two poison
  stacks on a one-max-HP Shedinja and lost the battle to the next status tick:
  js 13 rounds / 50 draws / player won against py 6 rounds / 22 draws / game
  over. Reproducer: `findings/M7-divergence-hunt_story_gen3_0624.json`.
* **F-K — a third "was sent out" site.** `playerParticipants` has three `add`
  sites in the source (55729, 56456, 56493) and the port carried two, both in
  `_handle_faint`. The missing one fires when `onBeforeAttack` aborts the turn
  *and* the hook killed the mover: the source promotes the first alive member
  on that side (55723-55742). The hook that reaches it is `own_tempo`, whose
  20% roll deals `max(1, floor(maxHp * 0.1))` self-damage and returns truthy,
  so a mover already under 10% HP faints with no combat faint ever occurring.
  Observed as `battles[0].player_participants[len]` js 2 / py 1 with winner,
  rounds and RNG draws agreeing exactly. The source's accompanying `send_out`
  log entry has no port counterpart and was deliberately not invented — the
  compared per-turn projection carries `type === "attack"` events only.
  Reproducer: `findings/M7-divergence-hunt_story_gen4_1490.json`.

## The M7 comparison projection

The base is the existing route checkpoint — the same normalization the frozen
gate already trusts — plus two documented additions.

| field | disposition |
|---|---|
| `checkpoint.mode`, `seed` | **compared** — episode configuration and generation flags |
| `checkpoint.rng.state` / `.draws` | **compared** — seeded RNG position and draw count |
| `checkpoint.screen` | **compared** — the phase/screen/overlay both sides agree on |
| normalized legal actions | **compared, before each action** (as sets; see below) |
| `checkpoint.map`, `current_map`, `current_node`, `in_sub_map`, `sub_map_return` | **compared** — node identity/state/accessibility, submap identity, full saved-parent topology |
| `checkpoint.team[*]` | **compared** — order plus every projected member field |
| `checkpoint.items` | **compared** — bag contents and order |
| `checkpoint.team[*].held_item` | **compared** — held-item identity |
| `checkpoint.counters` | **compared** — every counter/flag the route schema carries |
| `checkpoint.pending` | **compared** — type, optionality, ordered option identity |
| `checkpoint.resume_state` | **compared** — the three live resume guards |
| `checkpoint.game_over`, terminal screen | **compared** — terminal/truncation outcome |
| `battles[*]` | **compared** — winner, rounds, per-battle RNG draws, rosters, final HP/status, participants, status-event family, per-turn attack projection |
| **`battle_stages[*]`** | **ADDED BY M7** — per-combatant final stat stages. Required by the brief; carried by neither pre-M7 gate (`driver.js` defines `normalizeStages` but `normalizeMon` never calls it). See below. |
| `checkpoint.seq` | **excluded** — each side counts its own checkpoints and the sweep emits them in lockstep; bookkeeping, not behaviour |
| `checkpoint.scenario` | **excluded** — the episode label, identical by construction |
| `__prov` on actions | **excluded** — adapter provenance, not runtime state |
| `__diagnostic_event_count` | **excluded** — already diagnostic-only in the frozen schema |
| renderer layout/presentation | **excluded** — explicitly out of M7 scope |
| **`battle_abilities[*]`** | **ADDED BY M7-COMBINED (A1)** — the ability each combatant actually resolved during the battle, per roster. Observed, not recomputed: read off the `_gen3Ability` the source's own `onSwitchIn` wrote onto the clone `runBattle` returns (57696-57702), and off the `Combatant.gen3_ability` the port's `on_switch_in` wrote. A combatant that never switched in normalizes to `null` on both sides. |
| **`run_passives`** | **ADDED BY M7-COMBINED (A1)** — the run-level passive ids, from each runtime's own `state.passives`. |
| trait TIER maps | **not compared — a documented limitation, not an omission.** They are the constant `{}` across the entire declared Story/Nuzlocke surface *on both sides*, so there is no varying state to compare. `runBattleScreen`'s non-Endless branch is literally `buildTraitsConfig({}, {}, state.passives \|\| [])` (81076-81085) — `computeTraitTiers` is called only in the `isEndlessMode` branch — and `engine._battle_configs` mirrors that exactly (engine.py:1247-1253). `state.passives` is therefore the whole varying trait/passive input to a battle, and it IS compared, one row above. |
| per-battle trait COUNTERS | **not compared — a documented limitation.** The source keeps them in two private closures that never share state (`buildGen3AbilityConfig`'s and `buildTraitsConfig`'s `_fightState`), merged only at the hook level; the port deliberately consolidates both into one `battle.BattleConfig`, which its own docstring calls "a design decision, not a literal port". There is therefore no source-faithful 1:1 shape to normalize both sides into, and two JS fields (`leafPlayerActiveSeen`, `broughtTeamSize`) have no port counterpart at all. Comparing an invented merged shape would be comparing the adapters. |

Nothing is called equal by dropping it from both sides without a line above.

**And nothing above is taken on trust.** `SweepProjectionTests` asserts that
`project()` carries every checkpoint field but the two excluded ones, perturbs
each of those fields in turn and requires the diff to name it, and reproduces
the F-B stage swap as a fixture. The M7-A audit's first mutant — deleting
`battle_stages` from `project()` on both sides — is checked in beside that
test (`test_removing_battle_stages_from_the_projection_hides_F_B`), which
shows the same fixture comparing *equal* under the mutant. A disposition line
in the table above that the projection does not actually implement is
therefore a test failure, not a documentation drift.

### Why `battle_stages` had to be added

Stat stages are gameplay-relevant state the brief names explicitly, and no
pre-M7 gate carried them. Their absence was not theoretical: the first sweep
reported an unexplained 1-point damage gap **two events after** the real cause,
which was a single stat stage applied to a different stat on each side. With
stages compared, the same episode fails at the origin instead. The capture
wraps `runBattle` a second time, outside `driver.js`'s own wrapper, so it
observes the same result object at the same index — no extra call, no RNG draw,
and the legacy schema untouched.

## Legal-action comparison rules

* Sets are compared **before** an action is chosen. A missing action, an extra
  action, or a different target set is itself a divergence.
* The **intersection is never taken**, and neither side ever picks
  independently — that would mask exactly the legality bugs this exists to find.
* A duplicate normalized action is an adapter bug, not a state fact, and fails
  its own check (`action_multiset_error`).
* Order is not compared; identity is. Both sides enumerate deterministically
  (the source orders nodes by the source's own `(layer, col, id)`).

## Determinism and reproducibility

An episode is fully determined by `(seed, mode, policy_seed)`. No clock, no
filesystem scan and no dict-iteration order feeds a decision. Every episode
record carries the tool/schema version, the protected hashes, the seeds, the
ordered normalized actions, per-step legal-set and state digests, and — on
failure — the first mismatch with a field-level diff and both raw excerpts.

`episode_digest` covers config + ordered actions + ordered post-action state
digests + outcome. It contains nothing about batch position, so running the
same corpus in `manifest`, `reverse` and `sorted` order must reproduce every
per-episode digest exactly.

That gate is only as good as the digest itself. `SweepDigestTests` requires
each of the four identity inputs (and the ORDER of the two ordered ones) to
change the digest, requires sixteen distinct episodes to produce sixteen
distinct digests, and requires the same episode to keep its digest across
batch positions and wall-clock times. The M7-A audit's second mutant —
`digest()` returning a constant, which made the three-order comparison agree
no matter what the runtimes did — is checked in there as
`test_a_constant_digest_collapses_them_all`.

A fresh VM context per episode is what makes that true on the source side: a
run cannot be reset in place, and a reused context would let one episode's
`localStorage`, Pokedex cache and RNG binding reach the next.

### Minimization is signature-preserving

`--minimize` binary-searches the shortest still-diverging **prefix**, then
greedily deletes earlier actions. A reduction counts only when it reproduces
**the same divergence signature** (kind, plus the index-normalized set of diff
paths), never merely *a* divergence. Accepting any divergence is unsound and
was observed reducing a 7-action item-equip-cancel finding to a 1-action list
whose only "divergence" was that its first action is illegal at step 0.

## Coverage

`sweep-targets.json` is derived from `engine.Phase`, the action vocabulary,
`map_gen`'s node-type constants, `coverage.REQUIRED_TAGS` and the post-M6.4
battle-oracle corpus — **not** from whichever paths a first run happened to
hit. Each target names which evidence source may earn it:

| `evidence` | earned by |
|---|---|
| `sweep` | an observed, agreed step in a compared episode |
| `route-corpus` | a real exit-0 `compare.py --all` run |
| `battle-oracle` | a real exit-0 `tools/battle-oracle/compare.py --all` run with the named fixture present |
| `excluded` | never earned; an explicitly recorded project gap with a reason |

Coverage is derived from what actually happened — the state both runtimes
agreed on, or the legal set both offered — never from an episode's intent, the
scheduler's target, or the manifest itself. Excluded targets are **not** faked
as hits; unbuilt modes are recorded as project gaps so the denominator stays
honest. The episode's mode stratum is read off the COMPARED `checkpoint.mode`
rather than the plan entry that asked for the episode: the plan is intent, the
checkpoint is what both runtimes reported.

`validate-targets` rejects a manifest with duplicate ids, an unknown evidence
source, a `phase`/`action_kind`/`route_tag`/`node_type`/`reward_kind` the
runtime does not have, an excluded target with no reason, a `node.*`/`reward.*`
target that declares no derived key, or any `Phase`, action kind, `map_gen`
node type or `SUBMAP_REWARDS` reward id that no target names.

### The denominator is checked in BOTH directions (M7.0)

Before M7.0 the validator only rejected manifest values the runtime lacked. It
never asked whether the manifest still covered everything the runtime HAS, so
the M7-A audit deleted the required `node.start` target and `validate-targets`
reported no problems at all: a shrinking denominator was indistinguishable
from a complete one. `runtime_node_types()` (`map_gen`'s uppercase `str`
constants) and `runtime_reward_kinds()` (`data.get_submap_rewards()`) must now
both be fully named by the manifest, and `SweepDenominatorTests` deletes each
node type and each reward kind in turn and requires the rejection.

Adding the two derived keys surfaced eight reward kinds the manifest had never
named (`team_lvl2`, `rare_candy`, `transform`, `three_items`, `attack_up`,
`giratina`, `dialga`, `palkia`). The manifest is therefore v2: **163 targets,
156 required, the same 7 exclusions**.

### `node.start` is earned from OCCUPANCY, not from a visit (M7.0)

The layer-0 entry node is where `startMap` puts the run. It is `visited` from
that moment and never `accessible`, so no `visit_node` action can reach it and
`observe_coverage`'s `visit_node` branch could never earn it — a required
target that no legal action could satisfy. It is now credited from the
COMPARED checkpoint's own `current_node` and `map.nodes`: state both runtimes
produced and that the step had already proved identical. The same rule credits
whichever node the run is standing on, which is observation either way. It is
not an exclusion, and it is not a manifest freebie: blanking `current_node`
leaves it unearned, and an action naming a node the observed map does not
contain earns nothing.

### `submap.*` is earned from the OBSERVED submap, not from a node field (M7-combined)

`submap.underground` / `submap.distortion` used to be credited from a visited
node's `sub_kind`, and were **unearnable by construction**: both normalizers
carry that field only if the runtime sets it (`driver.js:427`,
`run_scenario.py:156`), and *neither runtime ever sets it* — `subKind` appears
nowhere in the bundle or in `pokelike/`. The symptom was unmistakable once the
A4 scheduler got deep enough: runs that demonstrably entered a submap and
earned `node.reward`, `node.subexit`, `lifecycle.submap_enter`/`_exit`,
`pending.reward_team_pick` and four `reward.*` kinds still left these two
unearned.

The kind IS observed, on the compared checkpoint: `enterSubMap` sets
`state.inSubMap` to it (`engine.py:3198`) and `exitSubMap` clears it (3226),
and `checkpoint.in_sub_map` is compared on both sides (`driver.js:827`). It is
now credited from there — the same rule `node.start` uses, and for the same
reason.

### A reproducer replay is evidence too (M7-F-F)

`replay` re-runs one saved record, reports whether it still diverges, and then
discards everything the replay observed. That was an accounting gap, not a
safeguard. A checked-in reproducer is a deterministic plan — a pinned
`(seed, mode)`, an ordered action list and a `max_steps` bound — and replaying
it drives both real runtimes through `run_episode`, the same lockstep loop
every other result file is produced by.

`replay-set` replays a set of records and emits a normal result file, so
`coverage` merges it exactly like a run or a hunt result, with no special case
anywhere.

**Nothing about how coverage is credited changes.** `run_episode` calls
`observe_coverage` itself, per step, and only after that step's legal sets and
state projections have already compared equal. A record that still diverges
contributes exactly the steps it completed *before* diverging; a record whose
action list stops short of a state earns nothing for it. The record file is
never itself evidence — it only says which episode to run.

`ReplaySetAccountingTests` pins that, and its middle test is the point: the
same F-F record with its action list truncated by ONE action — the
`visit_node n8_0` that enters the gauntlet — replays clean, does not reach the
win screen, and earns **neither** `outcome.win` nor `phase.win-screen`. If
credit came from the record's existence rather than from the observed step, it
would still earn both.

This is how `outcome.win` and `phase.win-screen` are earned. They are cited to
`hunt_story_gen1_0626#88` — a real compared step in a real episode, on which
both runtimes reported `screen == "win-screen"` after the same
`visit_node n8_0`.

### The goal-directed bounded scheduler (M7-combined A4)

`random_policy` and `guided_policy` are samplers, and after 258 episodes they
left 30 required targets unearned — every one behind a map advance or a submap
entry. That is a **depth** shortfall, and more uniform episodes do not fix it.

The source says exactly where the missing content is, deterministically rather
than by a roll (`map_gen.py:465-495`):

| content | generation | layer | map indices |
|---|---|---|---|
| SILVER | gen2 | 4 | 1, 3, 5, 7 |
| MAGMA + AQUA | gen3 | 4 | 2, 5, 7 |
| UNDERGROUND | gen4 | 4 | 1, 3, 6 |
| DISTORTION | gen4 | 4 | 3, 5, 7 |

So `node.magma` is not rare, it is behind **two map advances in one
generation**. `sweep.py hunt` therefore spends its budget on the generations
that can produce what is missing (`HUNT_MODE_HINTS`) and steers with
`hunt_policy`, whose ladder puts **survival before ambition**: an earlier
version chased target nodes first and lost all 300 episodes without reaching
map 1 in Gen4, because a run that walks into every MAGMA/AQUA/LEGENDARY node it
sees arrives at the gym leader under-levelled and dead.

What the scheduler may **not** do is unchanged, and is the point:

* it only ever re-orders the legal set `run_episode` has **already enumerated
  on both runtimes and compared as equal** (step 2 runs before step 3, which is
  where the policy is called). It cannot add to that set and never sees the
  source's answer;
* it steers from the **compared checkpoint projection**, never from the Python
  engine directly;
* coverage is still credited only by `observe_coverage`, from an observed
  agreed step. A target the policy steered toward but did not reach earns
  nothing — pinned by
  `SweepAccountingTests.test_the_guided_policy_wants_targets_but_never_credits_them`;
* every episode stays a pure function of its own `(seed, policy_seed, mode)`,
  the plan carries its own digest, and `--stop-when-covered` only shortens a
  run — it cannot change any episode, so per-episode digests are unaffected by
  batch position.

## Protocol

`sweep-host.js` speaks line-delimited JSON: `hello`, `reset`, `legal`, `state`,
`apply`, `traits` (read-only diagnostics), `quit`.

Two implementation notes that are easy to rediscover the hard way:

* the in-sandbox service loop must yield with a real **macrotask**
  (`__SWEEP_YIELD__`, injected by the host). The sandbox and host share one
  event loop, so a microtask spin starves the host's pump and deadlocks the
  protocol on the very first request;
* the host's **stderr must be drained**. The source prints an offline PokeAPI
  warning on every reset; left unread, the pipe buffer fills after roughly 25
  episodes and both processes block forever — which presents as a product hang.
