# Renderer observation/event contract — version 1

**Status: R1 deliverable. Frozen and test-pinned; R1 itself is OPEN pending an
independent audit.**

This is the single, versioned description of what `pokelike`'s engine exposes
to a renderer. It exists so R2–R5 (map sprites, hover cards, battle
animations, browser controls) can be built without forcing a second breaking
change to the surface underneath them.

Implementation: [`pokelike/render/contract.py`](../pokelike/render/contract.py).
Detectors: [`pokelike/tests/test_renderer_contract.py`](../pokelike/tests/test_renderer_contract.py).

---

## 1. This is not the route oracle's contract

The repository has **two** contracts and conflating them is the single easiest
way to break the M5 baseline.

| | Route-oracle schema | Renderer contract |
|---|---|---|
| Document | `route-oracle/SCHEMA.md` | this file |
| Version | `schema_version: 2` | `CONTRACT_VERSION = 1` |
| Consumer | cross-runtime parity | a UI running only against Python |
| Requirement | both runtimes produce it and it must agree **byte for byte** | presentation completeness |
| Cost of a new field | schema bump + re-freeze of `frozen_signature.json` | none |

The versions are independent. **Adding a field here must never require
touching `SCHEMA.md`, the oracle's compared fields, or the frozen signature.**
If a change to this contract seems to require bumping schema version 2, that
is the signal the two have been conflated — stop and re-read this section.

R1's acceptance depended on this and it held: after R1 the frozen parity
signature is byte-identical
(`b7ab9749ba5c8b3912698ef853a81b7648d1690cb8e751fabd9d49326342f5e4`) in
manifest, reverse and sorted order, and comparison-surface drift is 0.

---

## 2. Battle feed ownership — the central R1 decision

**Decision: one producer, two consumers, each with its own projection. The
*oracle* owns the shape of the records; the renderer owns only its read side.**

### Why

`battle_loop.run_battle` already builds `battle_events` (a flat
`turn_start`/`attack` stream), `status_events` and `hook_trace`, added for the
oracle in M3.3b. Two facts decided the design:

1. **The records are already compared.**
   `route-oracle/run_scenario.py::_fold_turns` folds `battle_events` into the
   checkpoint's `turns` field, and SCHEMA.md records that the ordered,
   turn-delimited attack family "is now compared in full". So the *shape of a
   record* is owned by parity. A renderer-driven field addition inside a
   record would move the frozen signature — exactly what R1 forbids.
2. **A second producer would drift.** Emitting a parallel renderer-only stream
   from the same call sites is the classic duplicate-source-of-truth bug, and
   it is the risk the milestone brief explicitly names.

Reusing the producer while separating the projections gets both: no second
stream to drift, and no renderer need that can reach into the oracle's
compared surface.

### The rule this implies

> The renderer's projection may **drop** fields, **regroup** them, or
> **enrich** a view from `RunState`. It may **never** require a new key inside
> a `battle_events` record.

Wanting a new key is the signal to add it to the enrichment layer in
`render/contract.py` instead. For example, if R4 needs a sprite for the
attacker, it joins `attack.side` + `attack.attacker_idx` against the team in
the same observation — it does not add `attacker_sprite` to the record.

### The plumbing

`engine._run_battle` now copies both streams onto `RunState.last_battle` after
`run_battle` has returned. It is written once, read by nobody inside
`engine.py`, and no control flow or RNG draw depends on it.

It is **replaced, never appended to** — one battle, not a run-long log.
`route-oracle/search_route.py` deep-copies `RunState` per explored branch, and
an accumulating log would make that cost grow without limit.

Behaviour-neutrality was proved by the M3.3b method: the frozen signature is
unmoved, 27/27 scenarios still agree, the battle oracle is still 29/29, and
`test_plumbing_draws_no_rng_and_changes_no_outcome` drives the same seed with
the feed read and ignored and requires an identical run fingerprint.

### Turn-by-turn replay

R4 is expressible. Each `attack` record carries `damage`, `type_eff`, `crit`,
`is_special`, `move_name`, `move_type`, both participants' side+index, and
**both `attacker_hp_after` and `target_hp_after`** — enough to animate HP bars
without re-simulating. `contract.fold_turns` partitions the flat stream into
`[{turn, events}]`.

`fold_turns` is deliberately a *separate function* from the oracle's
`_fold_turns`. The oracle raises when a compared event precedes the first turn
marker, because for parity a dropped event must be loud. A renderer must not
crash on a malformed feed, so it opens a synthetic turn 0 and degrades to
"shows the hits without a round number".

---

## 3. Consumer inventory — the gaps R1 closed at contract level

Each row was confirmed against current code, not taken from the audit table.
The CODEX references are to
`docs/reference/CODEX-phase1-2-audit-through-2026-07-31.md` section 3.

| CODEX | What R2–R5 must render | Carrying field | Before R1 | After R1 |
|---|---|---|---|---|
| 3 | Battle animation | `battle.turns[*].events` | **absent** — `battle_events` existed but `engine.py` never read it; a battle resolved atomically and the renderer saw one before/after snapshot | present, turn-partitioned |
| 5 | Team reorder control | `legal_actions.reorder_team` | existed on `engine.legal_actions` but was not in the observation | in the observation |
| 6 | Item icons | `items_info[*]`, `team[*].held_item_info` | **absent** — items were bare string ids | full `Item` metadata (`name`/`desc`/`icon`/`icon_url`/`usable`) |
| 7 | Map encounter icons | `map.nodes[*].encounter` | **absent** — only `type` | generation-time hints: trainer sprite, legendary species, sub-boss, reward |
| 8 | Hover stats | `team[*].base_stats`, `effective_stats`, `stages`, `stat_buffs`, `status_flags`, `sprite_url` | **absent** — `_mon_json` emitted 11 flat fields | present |
| 10 | Move-tutor move preview | `team[*].move_preview` | **absent** | the actual `get_best_move` result for the current tier |

### One correctness bug found while inventorying

`Combatant.status` only ever holds `"freeze"`, `"sleep"` or `None`. Burn,
paralysis and poison live in three *separate* fields (`burned`, `paralyzed`,
`poison_stacks`). Both renderers emitted `status` alone, so **a burned or
paralysed or poisoned Pokemon rendered as perfectly healthy** in the browser
and in the console.

`status_flags` carries all four. `status` is kept unchanged beside it, so no
existing client breaks. This was a renderer-contract defect, not a gameplay
one — the engine's own battle math always read the real fields.

---

## 4. The surface

`contract.observation(state, recent_log=5)` returns the whole view.
`webui/state_json.encode_state` is now a one-line adapter over it, and
`render/console.py` projects team rendering through `contract.mon_view`.

Field sets are exported as `OBSERVATION_FIELDS`, `MON_FIELDS`, `NODE_FIELDS`,
`ITEM_FIELDS` and asserted for exact equality by the detectors, so a field
appearing *or* disappearing fails a test.

### Stability classes

- **Stable** — will not change without a `CONTRACT_VERSION` bump: every key in
  `OBSERVATION_FIELDS`, `MON_FIELDS`, `NODE_FIELDS`, `ITEM_FIELDS`; the
  `attack` record's keys; the `type` strings in `BATTLE_EVENT_TYPES` /
  `STATUS_EVENT_TYPES`.
- **Provisional** — shape may change within version 1: `pending.options`
  (per-phase and driven by engine internals), `log` entries (the engine's own
  coarse log, never designed as a contract).
- **Passthrough** — owned elsewhere, mirrored here: `legal_actions` (owned by
  `engine.legal_actions`), the interior of every `battle` event record (owned
  by the oracle, per §2).

### Top level

| Field | Type | Source |
|---|---|---|
| `contract_version` | int | this module |
| `phase` | str | `RunState.phase` |
| `screen` | str | renderer-owned mapping, see §5 |
| `overlay` | str \| null | see §5 |
| `current_map`, `badges`, `elite_index` | int | `RunState` |
| `nuzlocke_mode`, `gen2_mode`, `gen3_mode`, `gen4_mode` | bool | `RunState` |
| `in_sub_map` | str \| null | `RunState.in_sub_map` |
| `team` | list of mon views | `RunState.team` |
| `items` | list[str] | `RunState.items` (unchanged, for back-compat) |
| `items_info` | list of item views | `data.get_*_items()` joined on id |
| `map` | object \| null | `RunState.map` |
| `pending` | object \| null | `RunState.pending` — **`extra` is never exposed**; it can hold live `Combatant`/`Trainer` references |
| `legal_actions` | object | `engine.legal_actions(state)` |
| `battle` | object \| null | `RunState.last_battle`, see §2 |
| `log`, `log_total` | list, int | `RunState.log` |
| `game_over`, `won`, `run_seed` | bool, bool, int | `RunState` |
| `unsupplied` | list[str] | see §6 |

`legal_actions` is included so the observation and action sides travel
together: a renderer drawing a button for an illegal action is the same bug
class as one reading a stale field. `engine.legal_actions` remains the single
authority — this carries its answer, it does not re-derive it.

### Per-Pokemon

`species_id`, `name`, `nickname`, `level`, `current_hp`, `max_hp`, `hp_pct`,
`fainted`, `status`, `status_flags`, `types`, `is_shiny`, `held_item`,
`held_item_info`, `move_tier`, `move_preview`, `base_stats`,
`effective_stats`, `stages`, `stat_buffs`, `sprite_url`.

`effective_stats` runs the same `battle.get_effective_stat` the damage formula
calls, with the mon's own held item and stages folded in — it is the number a
hover card should show; `base_stats` alone is misleading mid-battle.
`base_stats.spdef` may be `null`: some fixed-trainer rosters genuinely omit it
(see `data.BaseStats`), and it is reported as absent rather than backfilled.

### Per-node

`id`, `type`, `layer`, `col`, `visited`, `accessible`, `revealed`,
`encounter`.

`encounter` is `null` unless map generation already fixed something, and only
carries what it fixed: `trainer_sprite`, `legendary_species_id`, `sub_boss`,
`reward`.

---

## 5. M5 finding F1 — disposition

`docs/audits/M5-implementation.md:262` assigned F1 to R1.

**F1 is not a gameplay defect and was not repaired as one.** The four
`showScreen`-less overlays (item-equip, move-tutor, branching-evolution,
team-picker) leave the oracle's projected `screen` at the screen underneath.
That is correct: in the source the modal itself is the guard —
`.item-equip-overlay` is `position:fixed; inset:0; z-index:500` with no
`pointer-events:none` (`style/main.css:2125-2139`), so it intercepts every map
click. The port's phase guard is equivalent for any player-reachable route,
and the oracle's compared `pending` field already distinguishes the states.

**Nothing on the oracle surface changed.** `run_scenario._screen_for` is
untouched, and a detector asserts the oracle projection has gained neither an
`"overlay"` key nor `contract_version`.

What a *renderer* lacked was an explicit answer to "is a modal up, and which
one" without inferring both from `phase`. That is added here, additively, as
`overlay`:

| Phase | `screen` (underneath) | `overlay` |
|---|---|---|
| `item_equip_choice` | `item-screen` | `item-equip-overlay` |
| `move_tutor_choice` | `map-screen` | `move-tutor-overlay` |
| `evolution_choice` | `map-screen` | `branching-evolution-overlay` |
| `reward_team_pick` | `map-screen` | `team-picker-overlay` |

`contract._screen_for` is a small renderer-owned mapping, deliberately *not*
an import of `run_scenario._screen_for`. That function answers a parity
question (what would the JS's `currentScreen` variable hold) and is owned by
the oracle; coupling to it would let a renderer need drag the oracle's
projection around. A detector asserts `contract.py` imports no oracle module.

---

## 6. What the port cannot supply

Named in `contract.UNSUPPLIED` and echoed on every observation, rather than
filled with plausible-looking values (CLAUDE.md: don't guess at game logic).

- **`move_pp`** — the engine has no PP model. Note the site shows one move per
  Pokemon because `getBestMove` returns one, not because three were dropped;
  `move_preview` is real.
- **`battle_flavor_text`** — the source's per-turn `log`/`detailedLog` prose.
  The mechanical events are carried; the prose is not ported (`js/ui.js` is
  reference-only).
- **`unvisited_wild_species`** — an ordinary wild encounter's species is rolled
  from the live RNG stream **at visit time**. Previewing it would mean drawing,
  which would change the run. `node.encounter` therefore carries only what map
  generation already fixed.

Additionally, `item_view` returns `known: false` for an id in neither ported
item table (Mega Stones are built by `makeMegaStoneItem` from a separate source
table with a different shape). A renderer must treat that as "unknown", not as
a real item named after its own id.

---

## 7. Relationship to run-state serialization (P1.9)

P1.9 — `RunState` → dict → `RunState` for episode resumption — is **out of
R1's scope** and is a *different consumer* (RL resumption, not rendering).

This observation is a **lossy, presentation-oriented projection and must never
be used to reconstruct a `RunState`.** It deliberately drops `pending.extra`,
`passives`, `_todo`, and all four resume guards (`saved_catch`,
`saved_shiny_node`, `saved_question_resolve`, `item_offer`) — every one of
which a resumption format needs. A detector asserts those keys stay absent, so
the boundary cannot erode by accident.

The two surfaces are independent by design: adding a field here neither helps
nor blocks adding one there. A future P1.9 should be its own module with its
own version, not an extension of this one.

---

## 8. Changing this contract

1. Change `render/contract.py` and the matching entry in the pinned field set.
2. Bump `CONTRACT_VERSION` for any *shape* change (field added, removed,
   renamed, retyped). Not for a value changing.
3. Update this document.
4. Re-run the detectors. If a golden digest in
   `test_stream_matches_a_pinned_golden_digest` moves, say why in the
   milestone record — an unexplained move is the failure it exists to surface.
5. Confirm the frozen parity signature has **not** moved. If it has, the
   change touched the oracle's surface and belongs in a schema bump, not here.
