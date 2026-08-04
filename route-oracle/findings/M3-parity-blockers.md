# M3 parity blockers — frozen source/Python differences

> **STATUS (2026-08-03): ALL FIVE BLOCKERS BELOW ARE REPAIRED IN M4.**
> This document is retained unchanged below the line as the historical record
> of what M3 found and deliberately froze. Nothing in it has been rewritten to
> pretend the differences never existed, and the M3 signature it describes
> (`bde87cbbedfd5abf459ef6aa3b2a8c6d62eeddf30b6cb0d77c75567176a93ef`, 40
> difference records over 9 scenarios) is preserved verbatim.
>
> Current state after M4:
>
> | | M3 (this document) | M4 |
> |---|---|---|
> | `python route-oracle/compare.py --all` | exit **1** | exit **0** |
> | scenarios | 9 | 11 |
> | difference records | 40 | **0** |
> | frozen signature | `bde87cbb…76a93ef` | `777c1316bac57f8e2e49e1727ed170a30fdbb6334a5258917de1a127eed381bd` |
> | `align_rng_after_starter_offer` | set by all 9 fixtures | **retired; set by none** |
>
> | blocker | repaired by |
> |---|---|
> | 1 + 1(b) starter offer draws / instances | `Engine.reset` materialises three real starters, three `rollShiny` draws, and `ChooseStarter` installs the offered object |
> | 2 eager same-layer sibling locking | `_visit_node` locks at `onNodeClick`'s own point, for every node type |
> | 3 `showSwapScreen` clears `currentNode` | `_resolve_swap_choice` clears it on all three exits |
> | 4 `status_events` omits sleep ticks | `battle_loop` emits the whole pre-turn `status_tick` family |
> | 5 `state.anyFainted` unported | `RunState.any_fainted`, set only by a real Nuzlocke cull |
>
> Two claims in the text below are **wrong** and are corrected in
> `docs/audits/M4-implementation.md` §2.8: the port function is
> `battle.resolve_pre_turn_status`, not `battle.is_incapacitated`; and no
> battle-oracle fixture inflicts sleep at all (the oracle compares live JS to
> live Python, not stored expectations).
>
> Full M4 evidence: `docs/audits/M4-implementation.md`.

---

Produced 2026-08-01 by the M3 route oracle at repository revision
`964985b1724f86d8ba675ff645f5dd0330d3e412`. Findings 1-2 were recorded by the
original M3 session over a 4-scenario matrix; findings 3-5 were surfaced by
the **M3.1 coverage completion**, which added the Magma/Aqua Admin route, the
complete submap reward/subexit/parent-return route, a Silver *win* route and a
Nuzlocke *permadeath* route. Those paths simply were not executed before.

**Nothing here was repaired.** These are bounded M4 inputs, recorded exactly
as the oracle reported them.

| | |
|---|---|
| bundle sha256 | `dc29e7444b2883becbc336fc4cdc4dc4e823058b6cdd9b3a3dadbda400f99d82` |
| route-prefix sha256 | `92c82e6cdf4d36201d1143fa2802c3400af61e0e55268b45f236ba11465d102d` |
| battle-oracle prefix sha256 | `32cb2d0c736c53db4cb7de0fdd6b62179d07e3cc59cb824a97692384bd44778c` |
| schema version | 1 |
| parity command | `python route-oracle/compare.py --all` (exits 1) |
| audit command | `python route-oracle/compare.py --all --audit-frozen` (exits 0 — **still BLOCKED**) |

`--audit-frozen` succeeds only when the complete observed diff set equals the
five findings below. It is **not** a parity mode and never reports parity
PASS; it exists so a reviewer can assert that nothing *new* has appeared. The
default mode remains strict and exits nonzero on any difference at all.

## Regenerating the full normalized streams

The complete per-checkpoint streams for both sides are **deterministically
regenerable** from tracked inputs (the bundle, the scenario fixtures, and both
runners — all hash-pinned and verified on every run), so the multi-megabyte
JSON is not checked in. Reproduce it with:

```sh
python route-oracle/compare.py --all --dump route-oracle/findings/streams
```

`route-oracle/findings/streams/` is git-ignored for that reason. The exact
field-level differences it contains are reproduced in full below — the
divergence set is small enough to state exhaustively.

## Per-scenario result

| scenario | checkpoints | RNG draws (js / py) | js stream sha256 | py stream sha256 |
|---|---:|---:|---|---|
| `story_gen1_map0_to_map1` | 56 / 56 | 443 / 440 | `4e574aa453a99da00a4f3ac9d8ede6fb62497ba46beffa3428a9fe40d7f5bccf` | `dadb8a8f651266681cddc764259b7162339b65f91658ba04f63c14a423d8221a` |
| `story_gen2_silver` | 42 / 42 | 299 / 296 | `d10fe4f5858275eed9513cb669b14e2dfd71e0faecd50ba70a31fa86c2890f65` | `03f0509f1951012d6a75b8bc23820e9bb6a90351fabc72aa14d8ba66ab90bb8c` |
| `story_gen2_silver_win` | 54 / 54 | 390 / 387 | `8254bb928cdd374e7600886e5f925cd8bc1b6bb4f8d9937ba63b643177e3e4b9` | `598a15753869d0a8ae7b75692fac79fe086a71ee16cd01f3a653052862d87ee2` |
| `nuzlocke_gen1_loss` | 15 / 15 | 162 / 159 | `d5b9b1894e55f03531dd96d291d6d2d78dae037e187180f677bfcc4ac444d347` | `5f3aa71f2c26c972bd39a26104629e17a0a0d23c738f557eac2ccfc78f1c59fc` |
| `nuzlocke_gen1_permadeath` | 22 / 22 | 189 / 186 | `121789d9963f8a00022b62e601dc174f04cb0f2a732efe86745d89908886e203` | `46c55ef99c10f10d052f6ef44e5fa4cb85fa3835df0b459b896972fb89809897` |
| `story_gen4_underground` | 44 / 44 | 280 / 277 | `b6b38e7879110735182d6e788f908ede014ab0f64f4db7f7790415c3a122b8fe` | `d42988c937019acd1b1c78f030627367f68f85b78d8b4a452250e939180780cc` |
| `story_gen4_submap_full` | 59 / 59 | 386 / 383 | `1460fee5c4272f4dbe444125fa949761e272b8fac543812ec4251a030cac2b75` | `0af395bb3f327b443e95ce2278267776a45a3218b6791db1dc6a45637ee0cee9` |
| `story_gen3_admin` | 84 / 84 | 611 / 608 | `e997aca35105ddf30fb9664db8dad7aab4387666de8c3d2f375b06a550da29ef` | `14246ca094d824da3d6a0d3ac2ef5fa1db9835aec76aa289221652db12270220` |

Checkpoint counts and event order agree **exactly** in all nine scenarios.
Every scenario's RNG draw count differs by exactly **3**, and by nothing else.

## Complete set of differing field paths

This is the whole divergence surface across all nine scenarios — there is
nothing else. The ninth was added by M3.5; see the re-freeze note below.

| scenario | path | count | first checkpoints |
|---|---|---:|---|
| `story_gen1_map0_to_map1` | `rng.draws` | 56 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 2 | 40, 41 |
| `story_gen2_silver` | `rng.draws` | 42 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 3 | 39, 40, 41 |
| `story_gen2_silver_win` | `rng.draws` | 54 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 2 | 5, 6 |
| `nuzlocke_gen1_loss` | `rng.draws` | 15 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 5 | 5, 6, 12, 13, 14 |
| `nuzlocke_gen1_permadeath` | `rng.draws` | 22 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 7 | 5, 6, 12, 13, 19, 20 |
| | **`counters.any_fainted`** | 13 | 9 onward |
| `story_gen4_underground` | `rng.draws` | 44 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 3 | 41, 42, 43 |
| `story_gen4_submap_full` | `rng.draws` | 59 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 10 | 5, 6, 31, 32, 38, 39 |
| | **`current_node`** | 2 | 49, 50 |
| `story_gen3_admin` | `rng.draws` | 84 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 8 | 5, 6, 33, 34, 59, 60 |
| | **`event.battle.status_events[len]`** | 1 | 70 |
| `story_gen1_swap_release` | `rng.draws` | 84 | all |
| | `rng.state` | 2 | 0, 1 |
| | `map.nodes[i].accessible` | 20 | 5, 6, 33, 34, 59, 60, 63, 64 |
| | **`current_node`** | 2 | 81, 82 |

### M3.5 re-freeze — a ninth scenario, no new blocker

`story_gen1_swap_release.json` was added by M3.5 to close M3.4 **Defect A**:
`showSwapScreen`'s full-team *replace* branch
(bundle.deobfuscated.js:79202-79246) was implemented on both runtimes but no
scenario ever reached a six-member team, so the release affordance was never
built and never clicked. The route was derived by
`route-oracle/search_route.py --target swap_release --choice-order accept-first
--cross-runtime`, not hand-authored, and it verifies on **both** runtimes
(`swap_release EARNED at [82]` on each).

The signature was re-frozen for this and **only** this reason. Diffed record by
record against the previous signature:

| | before | after |
|---|---:|---:|
| scenarios | 8 | 9 |
| difference records | 35 | 40 |
| total occurrences | 520 | 637 |

* **added: 5** — every one of them belongs to `story_gen1_swap_release.json`,
  and every one is an already-approved blocker class: `rng.draws` (84×,
  blocker 1), `rng.state` (2×, blocker 1), `map.nodes[i].accessible` (20×,
  blocker 2), `current_node` (2×, blocker 3), `pending.options[i].instance`
  (9×, blocker 1(b));
* **removed: 0**;
* **changed: 0** — every pre-existing record kept its count, its checkpoint
  indices, its kinds and its canonical value hash.

No new difference *class* appeared: the search's `_cross_runtime_gate` rejects
any candidate route whose JS-vs-Python difference set strays outside the frozen
paths, so a fresh parity finding cannot enter the matrix disguised as tooling
work. The five frozen blockers, blocker 1(b), the ordinary-map `LEGENDARY`
lifecycle and Origin Giratina's stats are untouched and remain M4/M5 inputs.

| | |
|---|---|
| signature before | `d5cc8b7d2822cc718c6dd993c4251b79fba4984bb38ba063d82ccb33d307a25d` |
| signature after | `bde87cdbbedfd5abf459ef6aa3b2a8c6d62eeddf30b6cb0d77c75567176a93ef` |

**M3.3b addition.** Every scenario additionally carries
`pending.options[i].instance` 9× at checkpoints 0-2 — see blocker 1(b). That
is the only path M3.3b added; the per-scenario rows above are otherwise
unchanged,
and the re-freeze removed no record. The per-scenario stream hashes in the
table above predate M3.3b's observation fields and are superseded; regenerate
them with `--dump` if needed.

The ordered per-turn battle-event projection M3.3b added
(`event.battle.turns`, workstream 5) produced **no** difference in any
scenario: every attack, in every turn, on both runtimes, matches in acting
side, attacker/target index, move name and type, damage, type effectiveness,
crit, special flag and both post-hit HP values.

Everything else agrees: map topology, node identity and every other node
flag, edge lists, battle winners, exact round counts, per-battle RNG draws,
final combatant state, participant sets, team composition/level/HP/form
identity/held items, bag contents, every other Story and Nuzlocke counter and
flag, saved parent/submap identity and its locked parent flags, submap
generation, submap boss wins, reward resolution, subexit and exact parent
restoration, the Admin win, the Silver win, the Nuzlocke cull itself,
map transitions, pending-choice kind and cardinality, and terminal state.

---

## Blocker 1 — starter selection consumes 3 RNG draws in the source and 0 in the port

**Classification: direct source evidence.**

Observed at checkpoint 0 (`run_init`) of every scenario:

```
rng.draws    js = 3            python = 0
rng.state    js = 1323186932   python = 123456789      (story_gen1)
```

`python` still holds the raw seed (nothing has drawn yet); `js` has already
advanced three steps.

**Source.** The Story/Nuzlocke branch of `showStarterSelect`
(bundle.deobfuscated.js:76175-76194) loops over the fetched starter entries
and, for each one, calls:

```js
const BIV = rollShiny(),
      BIv = createInstance(BIj, B2l, BIV, 0x0),
```

`rollShiny` (bundle.deobfuscated.js:74912-74923) ends in `rng() < O` — an
unconditional draw on every call. With three starters offered that is
**three draws before the player clicks anything**, and the instance the
player picks carries whatever shininess its own roll produced.

**Port.** `engine._dispatch_action`'s `CHOOSE_STARTER` branch
(`pokelike/engine.py:580-581`) builds the chosen species directly and then
forces `mon.is_shiny = False`. It consumes **zero** draws and the starter can
never be shiny.

**Consequences.**
1. Every subsequent Stream-B draw in the port is offset by three, so map
   generation and everything downstream of it diverge from the source for
   the same seed.
2. A shiny starter is unreachable in the port.

**Not repaired here.** M3 is an oracle-building iteration.

### 1(b) — the same cause, seen without the RNG counters (new in M3.3b)

M3.3b's pending-choice option-identity projection (workstream 3) exposed a
second, non-RNG face of exactly this difference:

```
pending.options[i].instance   js = {…full normalized instance…}   python = null
```

9 occurrences per scenario, in all eight scenarios, at the three checkpoints
where the starter screen is up (`run_init`, `starter_offered`, `rng_aligned`)
× the three offered starters.

**Source.** The loop at bundle.deobfuscated.js:76175-76194 builds a real
`createInstance(BIj, B2l, BIV, 0x0)` per card *before* the player clicks, and
the card's own click listener closes over that instance
(`addEventListener("click", () => selectStarter(BIv))`, 76186). So at offer
time the source holds three fully-materialised Pokemon, each with its own
rolled shininess, level, HP and base stats.

**Port.** `Engine.reset` (`pokelike/engine.py:536-543`) offers
`{"species_id", "name"}` pairs from `data.get_starter_ids(generation)` and
builds nothing; the chosen species is instantiated at click time by
`_dispatch_action`'s `CHOOSE_STARTER` branch.

The runners therefore report the instance where one exists and `null` where
none does, rather than omitting the field — the same discipline
`counters.any_fainted` follows (blocker 5). This is **not a sixth finding**:
it is blocker 1's mechanism observed in a field that does not depend on draw
counting, and it is what makes the difference survive the `align_rng_after_
starter_offer` instrument. It is frozen as part of the M3.3b re-freeze; the
re-freeze added these 8 aggregate records and removed none.

Catch, item and swap option identities — species/form identity, full
instances, item ids, and the incoming-versus-team roles — **agree exactly**
on both runtimes in all eight scenarios. That agreement is new information:
before M3.3b only `{phase, optional, option_count}` was compared.

**Note on the oracle's own handling.** Because this offset would otherwise
mask every independent difference, the scenarios use
`align_rng_after_starter_offer` (see `SCHEMA.md`) — a symmetric re-seed
through each side's own primitive, applied at the same route point, after
the offer. The divergence itself is still measured and reported at
checkpoints 0-1; alignment only stops it from swamping the rest. The M3.1
routes are direct proof that alignment does not hide anything: three
independent differences (3, 4, 5 below) surfaced *through* it.

---

## Blocker 2 — `onNodeClick` locks same-layer siblings eagerly; the port locks them only on resolution

**Classification: direct source evidence.**

Observed wherever a node **suspends on a choice screen or ends the run**.
Concretely, from `nuzlocke_gen1_loss`:

```
checkpoint 5 (node_post, n1_0, screen=catch-screen)
  map.nodes[n1_1].accessible   js = false   python = true
checkpoint 12-14 (battle / node_post / terminal, after the wipe)
  map.nodes[n3_2].accessible   js = false   python = true
```

**Source.** `onNodeClick` (bundle.deobfuscated.js:77305-77396) locks the
siblings **before** it dispatches — lines 77311-77316, ahead of the type
`switch` at 77334:

```js
state["currentNode"] = B;
for (const ip of Object["values"](state["map"]["nodes"]))
  ip["layer"] === B["layer"] && ip["id"] !== B["id"] && ip["accessible"] &&
    (ip["accessible"] = !0x1);
```

So the lock has already happened while the catch screen is up, and it
survives a loss that never advances the node.

**Port.** `engine._visit_node` (`pokelike/engine.py:1761-1774`) sets
`state.current_node_id` and dispatches immediately, with no sibling lock. The
lock lives only in `_advance` (`pokelike/engine.py:696-713`), which runs when
the node *resolves*. `_advance`'s docstring asserts the two are "provably
idempotent"; the oracle shows that holds **only for nodes that resolve
immediately**. For a node that suspends, or one whose battle ends the run,
the port leaves the sibling accessible where the source has locked it.

`engine._lock_same_layer_siblings` already exists (added by the M2.1-M2.3
repair) but is called only from `_enter_sub_map`, not from the general
node-click path.

**Consequences.** After a Nuzlocke wipe, or while any choice screen is
pending, the port reports a same-layer sibling as still reachable when the
source does not. This is observable run state, not presentation.

**M3.1 addition.** The new routes broadened the evidence considerably: the
count rises to 10 occurrences in `story_gen4_submap_full` and 8 in
`story_gen3_admin`, and the *first* occurrence in every route that accepts a
catch is now checkpoint 5/6 (the catch screen), not a late-run wipe. Every
occurrence is still the same single cause.

**Not repaired here.**

---

## Blocker 3 (new in M3.1) — `showSwapScreen` clears `state.currentNode`; the port leaves it set

**Classification: direct source evidence.**

Observed in `story_gen4_submap_full`, at the two checkpoints between the
submap reward being resolved and the next node being clicked:

```
checkpoint 49 (choice_post, after accepting the fossil Cranidos)
  current_node   js = null      python = "n2_1"
checkpoint 50 (node_pre, n3_0 subexit)
  current_node   js = null      python = "n2_1"
```

By checkpoint 51 both sides report `n4_0` again (the restored parent node), so
the divergence is bounded to that window.

**Source.** `showSwapScreen` clears the current node on **all three** of its
exits — the accept-with-room handler (bundle.deobfuscated.js:79186), the
release-and-swap handler (79231), and the cancel button (79256). Each reads:

```js
advanceFromNode(state["map"], O["id"]),
(state["currentNode"] = null),
```

Note the asymmetry, which is source behaviour and not a typo: `catchPokemon`'s
own room path (79036-79045) calls `advanceFromNode` and `showMapScreen`
without clearing `currentNode`.

**Port.** `engine._resolve_swap_choice` (`pokelike/engine.py:2907-2930`) calls
`_advance(state, node_id)` and sets `state.phase = Phase.ON_MAP`, but never
clears `state.current_node_id`. Nothing in the port clears it at all — the
only writers are `_visit_node`, map start, and submap return
(`pokelike/engine.py:644`, `1768`, `2543`, `2579`).

**Consequences.** Between resolving any swap screen and clicking the next
node, the port reports a stale current node where the source reports none.
Any consumer that treats `current_node` as "the node the player is standing
on" — the renderer's selected-node highlight is the obvious R2 case — reads a
node that the source considers already left.

**Why it took until M3.1 to see.** The original four scenarios never resolved
a swap screen: three of them never reach one, and the fourth loses first.

**Not repaired here.**

---

## Blocker 4 (new in M3.1) — the port's `status_events` omits sleep ticks

**Classification: direct source evidence for the source side; port
instrumentation gap. Gameplay agreement is intact.**

Observed once, in `story_gen3_admin` at checkpoint 70 — the Team Magma admin
battle at `n4_0`:

```
event.battle.status_events[len]   js = 3   python = 0

js status_events:
  {type: status_tick, side: enemy, idx: 1, status: sleep_wake, hp_change: 0, hp_after: 20}
  {type: status_tick, side: enemy, idx: 2, status: sleep_skip, hp_change: 0, hp_after: 48}
  {type: status_tick, side: enemy, idx: 2, status: sleep_wake, hp_change: 0, hp_after: 16}
python status_events: []
```

Everything else about that battle agrees exactly: `player_won` true on both
sides, `rounds` 10 on both sides, `rng_draws` **42** on both sides,
participants `[0, 1, 2]` on both sides, and every final combatant's HP,
status and stat buffs.

**Source.** The status-tick round (bundle.deobfuscated.js:55687-55710) pushes
a `status_tick` event for a sleeping combatant on both branches of its wake
roll — `sleep_wake` when `rng() < 0.5` clears the status, `sleep_skip`
otherwise.

**Port.** `battle_loop._status_tick_round` (`pokelike/battle_loop.py:1109-
1160`) appends `status_tick` events for **burn and poison only**. Sleep is
modelled, and modelled correctly — `battle.is_incapacitated`
(`pokelike/battle.py:767-774`) performs the same 50% wake roll and consumes
the same draw, which is exactly why the RNG counts match to the draw — but it
is never logged.

**Classification detail.** This is a *logging* gap, not a behaviour gap: no
simulated outcome differs. It is nevertheless a real difference in a compared
field and is recorded as such rather than normalised away, because the
schema's stated contract is that `status_events` is the ordered status stream
for the battle. Closing it is a small addition to the port's oracle-facing
instrumentation and belongs with the existing declared limitation (the port
has no full per-turn battle event log — see `SCHEMA.md`).

**Why it took until M3.1 to see.** No battle in the original four scenarios
happened to inflict sleep. The 29 battle-oracle fixtures do exercise sleep,
but they compare `runBattle` directly against `battle_loop` with fixtures
whose expected `status_events` were derived from the port, so the omission was
invisible there.

**Not repaired here.**

---

## Blocker 5 (new in M3.1) — `state.anyFainted` is unported

**Classification: direct source evidence.**

Observed in `nuzlocke_gen1_permadeath`, from the checkpoint of the won battle
in which a party member faints and is culled, and on every checkpoint after:

```
checkpoint 9 (battle, n2_2, player_won=true, team 2 -> 1)
  counters.any_fainted   js = true   python = false
... identical through checkpoint 21 (terminal)
```

**Source.** `runBattleScreen`'s Nuzlocke cull (bundle.deobfuscated.js:81358-
81380), inside the win branch opened at 81278, sets the flag when at least one
member was actually culled:

```js
BI1["length"] > 0x0 &&
  ((state["anyFainted"] = !0x0), renderTeamBar(...), renderItemBadges(...)),
```

It is initialised to `false` in `startNewRun` (75478) and its only consumer is
the `_no_tombstone` achievement check at 81971.

**Port.** `RunState` has **no `any_fainted` field at all** (`grep -n
any_fainted pokelike/*.py` returns nothing). `run_scenario.py` reads it as
`getattr(st, "any_fainted", False)`, which is why the port reports a constant
`false` rather than crashing — the defensive read is deliberate and is what
makes the absence visible as a comparable field.

**Consequences.** This is the same shape as the `enteredSubMap` gap the
M2.1-M2.3 repair closed: a persisted run flag whose only source consumer is an
achievement the port has not built. It costs nothing today and is a latent gap
for any future achievement/progression surface.

**Why it took until M3.1 to see.** The cull that sets it runs on the **win**
branch only, so it needs a Nuzlocke run that wins a battle in which a member
faints. `nuzlocke_gen1_loss` wipes with a one-Pokemon party and never culls.

**Not repaired here.**

---

## Related, previously known, and deliberately not routed through

The ordinary-map `doLegendaryNode` room-auto-add discrepancy recorded by the
M2.4 audit (bundle.deobfuscated.js:80454 vs `engine._visit_legendary`) is a
pre-existing M4 blocker. No scenario in this matrix routes through a
`legendary` node, so it is neither re-proved nor disturbed here.

## Harness defects found and fixed in M3.1 (NOT parity findings)

Two of these were mistaken for port discrepancies before being traced, and are
recorded so a future session does not re-derive them:

1. **`recordMonOrigin` was stubbed as persistence but mutates run state.** Its
   body (bundle.deobfuscated.js:79047-79063) sets `state.usedBallCatch` and
   `state.gotViaQuestion` before calling `incrementStoryCounter`. The stub
   *suppressed* gameplay state — the mirror of the "no stub may generate
   gameplay state" rule. It presented as a 52-checkpoint
   `counters.used_ball_catch` divergence on the first route that accepts a
   catch. The stub was removed; the four original stream hashes are unchanged.
2. **The DOM shim never cleared children on `innerHTML = ...`.** A rebuilt
   screen therefore kept the previous generation's click listeners, so a route
   that accepts a card would have fired a stale closure from an earlier screen
   (`showSwapScreen` at 79152 and `doCatchNode` at 78435 both rewrite their
   container before repopulating it). Fixed with a real `innerHTML` setter plus
   `#id descendant` selector resolution; the four original stream hashes are
   unchanged.

## Not evidence of parity

Remaining declared coverage gaps (see `route-oracle/README.md`): Distortion
submaps, and the unbridged `move-tutor` / `trade` / `question` / `legendary` /
`shiny` screens and the `openItemEquipModal` / `showBranchingChoice` /
`showTeamPickerModal` overlays. These are **untested**, not agreeing. No
parity claim is made about them.
