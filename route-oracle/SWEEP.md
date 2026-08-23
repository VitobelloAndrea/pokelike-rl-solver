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
python route-oracle/sweep.py coverage --external corpus.json random.json guided.json
```

`run` exits non-zero if any episode diverged; `coverage` exits non-zero if any
required target is unearned; `validate-targets` exits non-zero if the manifest
is inconsistent with the code it claims to derive from.

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
The breadth difference is **reported as finding F1**, not silently intersected
away. Enumerating all `n!` permutations was rejected explicitly: it would
satisfy an API shape while proving nothing the transpositions do not.

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
| traits/passives/abilities | **NOT compared** — see finding **F2**. The `traits` protocol op exposes them read-only for diagnosis; they are not in the projection, so a trait divergence is only visible once it changes a compared field. |

Nothing is called equal by dropping it from both sides without a line above.

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
honest.

`validate-targets` rejects a manifest with duplicate ids, an unknown evidence
source, a `phase`/`action_kind`/`route_tag` the runtime does not have, an
excluded target with no reason, or any `Phase` or action kind that no target
names.

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
