# Renderer observation/event contract — version 4

**Status: R1 deliverable, extended by R2, R3 and R4 (all closed, independently
audited). Frozen and test-pinned. R5 is OPEN pending an independent audit and
made NO contract change — the version stays 4; see §12.**

Version 2 (R2) is strictly additive except for one shape change, and is
described in §9. In summary: `ability` on every Pokemon view (N1), both battle
rosters on `battle_view` (N2), ten ported presentation fields on every node,
and `map.edges` changing from `[from, to]` pairs to objects.

Version 3 (R3) adds `pending.context` and a read-side enrichment layer over
`pending.options`, described in §10. No engine change was required for it.

Version 4 (R4) is strictly additive: `battle_view` gains
`player_team_start`/`enemy_team_start` (the pre-battle rosters a replay's first
frame needs) and `replay` (the ordered presentation steps both renderers
drain), described in §11. It is the first version to require an engine change
since R1 — two pre-battle roster snapshots on `RunState.last_battle` — and no
`battle_events`/`status_events` record gained a key.

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
| Version | `schema_version: 2` | `CONTRACT_VERSION = 4` |
| Consumer | cross-runtime parity | a UI running only against Python |
| Requirement | both runtimes produce it and it must agree **byte for byte** | presentation completeness |
| Cost of a new field | schema bump + re-freeze of `frozen_signature.json` | none |

The versions are independent. **Adding a field here must never require
touching `SCHEMA.md`, the oracle's compared fields, or the frozen signature.**
If a change to this contract seems to require bumping schema version 2, that
is the signal the two have been conflated — stop and re-read this section.

R1's acceptance depended on this and it held; R2's does too. After both, the
frozen parity signature is byte-identical
(`b7ab9749ba5c8b3912698ef853a81b7648d1690cb8e751fabd9d49326342f5e4`) in
manifest, reverse and sorted order, and comparison-surface drift is 0. R2 is
the sharper test of the two: it changed the *shape* of `map.edges` on this
surface, which would be a re-freeze if the two contracts were shared. Nothing
under `route-oracle/` was touched.

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
`ITEM_FIELDS`, `EDGE_FIELDS`, `PENDING_FIELDS` and `PENDING_CONTEXT_FIELDS`
and asserted for exact equality by the detectors, so a field appearing *or*
disappearing fails a test.

### Stability classes

- **Stable** — will not change without a `CONTRACT_VERSION` bump: every key in
  `OBSERVATION_FIELDS`, `MON_FIELDS`, `NODE_FIELDS`, `ITEM_FIELDS`; the
  `attack` record's keys; the `type` strings in `BATTLE_EVENT_TYPES` /
  `STATUS_EVENT_TYPES`.
- **Provisional** — shape may change within a version: `pending.options`
  (per-phase and driven by engine internals; R3 adds a read-side enrichment
  layer over it, see §10), `log` entries (the engine's own coarse log, never
  designed as a contract).
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
| `pending` | object \| null | `RunState.pending` — `phase`, `optional`, `options`, `context` (v3). **`extra` is never exposed**; it can hold live `Combatant`/`Trainer` references. See §10 |
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
`effective_stats`, `stages`, `stat_buffs`, `sprite_url`, **`ability`** (v2).

`effective_stats` runs the same `battle.get_effective_stat` the damage formula
calls, with the mon's own held item and stages folded in — it is the number a
hover card should show; `base_stats` alone is misleading mid-battle.
`base_stats.spdef` may be `null`: some fixed-trainer rosters genuinely omit it
(see `data.BaseStats`), and it is reported as absent rather than backfilled.

### Per-node

`id`, `type`, `layer`, `col`, `visited`, `accessible`, `revealed`,
`encounter`, plus the ten R2 fields documented in §9: `sprite_url`,
`sprite_size`, `icon`, `color`, `tooltip`, `clickable`, `dimmed`,
`unexplored`, `is_current`, `pos`.

`encounter` is `null` unless map generation already fixed something, and only
carries what it fixed: `trainer_sprite`, `legendary_species_id`, `sub_boss`,
`reward`.

### Per-edge (v2)

`from`, `to`, `active`, `both_visited`, `color`, `width`, `dashed` —
`EDGE_FIELDS`. See §9.

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

R2 adds two more:

- **`node_sprite_assets`** — the node sprite *paths* are computed faithfully
  (`getNodeSprite`, `bundle.deobfuscated.js:53944-54025`), but the files
  themselves are **not in this mirror**: `pokelike_forked/img/sprites/`
  contains only `pokemon/`. The paths resolve if the assets are ever added; the
  web renderer falls back to the source's own circle+icon presentation on image
  load error, which is a deliberate approximation and is marked as such in
  `app.js`. This is a missing-asset gap, not an unported algorithm.
- **`silver_hover_starter_line`** — `getSilverHoverLabel` (54596-54643) swaps
  Silver's **last** previewed team slot for the player's counter-starter's
  evolution line, resolved live via `resolveEvoForLevel`. The engine already
  performs that swap at battle time; re-implementing the resolver inside a
  tooltip would be a second implementation of a rule the port already owns, so
  the previewed roster is the fixed table's. The other three previews (gym
  leader, Magma/Aqua, sub-boss) are complete.

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

---

## 9. Version 2 — R2's map and node parity

Before R2 both renderers positioned and drew map nodes by rules they had
invented, and both said so: `app.js` carried the comment *"a fixed per-layer
grid layout, NOT a port of the source's own (untraced) node-positioning
algorithm"*, and `console.py` mapped each node type to an arbitrary letter.
They also disagreed with each other about what "clickable" meant.

The source decides all of it in one loop and four functions, all now ported
into `render/contract.py` with line citations:

| Source | Lines | Ported as |
|---|---|---|
| `renderMap` layout loop | 54126-54142 | `contract._node_positions` + `node_pixel_position` |
| `renderMap` edge loop | 54143-54162 | `contract.edge_view` |
| `renderMap` node flags | 54172-54181 | `clickable` / `dimmed` / `unexplored` |
| `renderMap` sprite boxes | 54192-54200, 54316 | `contract._node_sprite_size` |
| `getNodeSprite` | 53944-54025 | `contract._node_sprite` |
| `getNodeColor` | 54540-54573 | `contract._node_color` |
| `getNodeIcon` | 54574-54595 | `contract._node_icon` |
| `getNodeLabel` (+ Silver/Admin) | 54596-54824 | `contract._node_tooltip` |
| `_mapTooltip` | 54026-54051 | `app.js`'s `mapTooltip` |

### Layout travels as fractions, not pixels

The source's `x` is `W/2 + (i − (m−1)/2) · W/(m + 0.2)` — a pure multiple of
the container width — while its `y` applies an absolute 28px margin. So
`pos` carries `x_frac` (exact and viewport-free), `y_frac` (the source's own
`layer_index / (layer_count − 1)`, or `null` for the single-layer "centre it"
case), and the three inputs `layer_index` / `index_in_layer` / `layer_size`.
`contract.node_pixel_position(pos, w, h)` is the single de-normalizer;
`app.js` mirrors those two lines against the live container size, exactly as
the source reads `clientWidth`/`clientHeight`. `map.edge_margin` carries the
28 so it is not hard-coded twice.

The source indexes by **position in the layer array**, not by `node.col`. They
agree for every map `map_gen` builds, and a detector asserts that — but the
array index is what is ported, because it is what the source reads.

### The one shape change

`map.edges` was `[[from, to], …]`; it is now a list of `edge_view` objects.
Both endpoints are still there as `from`/`to`, but a client that unpacked the
pair positionally must now read them by name. This is what makes v2 a shape
change rather than a pure addition, and it is why `CONTRACT_VERSION` moved.
The stroke decisions (`active`, `both_visited`, `color`, `width`, `dashed`)
are the source's own predicates `BcL` and `Bch`, taken once rather than
re-derived differently in each renderer.

### Tooltips are structure, not HTML

`getNodeLabel` returns an inline-styled HTML string. A Python contract
emitting HTML would be dictating markup to the console renderer, so
`tooltip` carries the same content as `{title, notes, team}` and each renderer
formats it — `app.js` back into the source's own markup, `console.py` into one
line. Every string inside is the source's.

### N1 and N2 — the R1 audit's carried-forward gaps

Both are closed here, as `docs/audits/R1-independent-closure-audit.md` §7
recommended.

- **N1 `ability`** — now on every `mon_view`. It is the engine's own
  `Combatant.gen3_ability`, carried verbatim. That field is battle-local
  (`engine.py:1593-1596`, CODEX issue 20), so it is usually `null` on a
  `state.team` member and populated on N2's battle rosters — which is where a
  replay needs it. Re-deriving a species ability here would invent a value the
  engine does not hold.
- **N2 battle rosters** — `battle_view` now carries `player_team` and
  `enemy_team`, projected through `mon_view`. An `attack` record identifies its
  participants by `side` + index only, so this is the join target that makes
  those indices resolvable. This is the enrichment layer §2's rule points at:
  no `battle_events` record gained a key. **R4 depends on this and it is now
  unblocked** — but R2 did not build the replay UI itself.

### N3

Recorded by the R1 audit as a process risk, not a defect: within-turn ordering
is caught only by the pinned golden digest. No action was taken, and R2 did
not move any of the four pinned per-seed digests.

---

## 10. Version 3 — R3's party, item, reward, choice and evolution parity

R3 wired the three actions the engine has modelled since before R1 —
`ReorderTeam`, `UseItem`, `EquipItem` — into both renderers, and gave `app.js`
dispatcher cases for the two phases it had none for. **No engine change was
needed: `engine.py` is byte-identical across R3.** Everything below is
renderer-side.

### The one shape change: `pending.context`

`pending` gained a fourth key. Three phases were not renderable from
`options` alone:

- **`reward_team_pick`** — `sacrifice` and `stat10` build an *identical*
  option list (`[_mon_summary(m) for m in state.team]`, `engine.py:3235` and
  `3244`) and do opposite things: one deletes the picked member, the other
  buffs it (`engine.py:3288-3299`). The discriminator lived only in
  `extra["kind"]`, which is never exposed. A renderer had no way to tell the
  player which one they were looking at.
- **`evolution_choice`** — options are `{into, name}` only, so neither
  renderer said *who* was evolving. The source's screen does:
  `displayName(mon) + " is evolving!"` (`bundle.deobfuscated.js:70567-70570`).
- **`escape_rope_choice`** — the single option is `{action, item_index}`,
  which the console printed as a raw Python dict and `app.js` never reached at
  all.

`context` always carries **all five** of `PENDING_CONTEXT_FIELDS` —
`title`, `desc`, `kind`, `subject`, `team_index` — with `null` where the phase
has nothing to say, so a renderer never needs an existence check and a phase
losing its context fails a detector rather than rendering an empty screen.

It is a per-phase **allow-list**, not a filtered copy of `extra`. `extra` holds
live `Combatant`/`data.Trainer`/`data.Evolution` references; a
copy-then-drop approach would leak the next one someone adds. `subject` is a
`mon_view`, itself already a projection.

### Read-side option enrichment

Per §2's rule the renderer may enrich on the way out, never require a new key
inside a producer's record. Two phases use it, and no engine structure changed:

- `evolution_choice` options gain `types` (from `data.Evolution.types`,
  populated exactly for branching evolutions), `is_shiny` (from the *evolving*
  mon, not the branch — `bundle.deobfuscated.js:70578-70581`) and a
  `species_id` alias for `into`, so one card builder covers this screen too.
  The source renders each branch as sprite + name + `types.join("/")`
  (`70601-70603`).
- `escape_rope_choice`'s option gains `item_id` and a `label`.

### Derived, not hard-coded

`stat10`'s description interpolates a percentage. The source computes it as
`max(1, round(2 * multiplier)) * 5` (`bundle.deobfuscated.js:77040`) over the
same multiplier `engine._apply_run_stat_buff` scales the real buff by, so
`contract._stat10_percent()` recomputes it from the engine's own constant
rather than embedding a literal. On the only branch that reaches it the answer
is **5%**, not the 10% the reward's `stat10` id suggests — a hard-coded string
would have been wrong, and would have stayed wrong if the constant moved.

### What the source's interaction actually is

Traced rather than invented, and the shape matters:

- **Reorder is a transposition.** `renderTeamBar`'s drop handler swaps exactly
  two slots — `[team[dragIdx], team[dropIdx]] = [team[dropIdx], team[dragIdx]]`
  (`bundle.deobfuscated.js:64805`) — then re-renders. `ReorderTeam` accepts any
  permutation; the source only ever produces the identity with two positions
  exchanged, which is what both renderers build.
- **Tap versus drag** is the source's own 6px movement threshold
  (`64757-64759`, `64883-64885`), not two separate listeners. Installing a
  `click` handler *alongside* the pointer gesture double-fires, because the
  browser synthesises `click` after `pointerup` — one gesture handler only.
- **The bag is index-addressed.** `renderItemBadges` renders one badge per bag
  index (`64834`); an aggregated-by-count bar cannot address an item, and
  `UseItem`/`EquipItem` are both index-addressed.
- **Eligibility is the engine's answer**, surfaced via `legal_actions` —
  `use_item[*].target_indices` *is* `usableItemCanTarget`
  (`79571-79583`). Neither renderer re-derives it.

### Known gap, not closed by R3

The source's equip overlay offers "Unequip (return to bag)" (`79530`) and a
direct hand-off from one member to another (`79544-79545`). **Neither is
expressible**: `EquipItem` moves bag → member only (`engine.py:301-321`), and
the port has no unequip or hand-off action at all. Adding one is new engine
surface, which R3 was explicitly not scoped to add. What is reachable is
exposed instead — equipping a different bag item onto that member displaces the
held one back into the bag — and the held-item modal says so rather than
offering a control that cannot work. See `docs/audits/R3-implementation.md`.

---

## 11. Version 4 — R4's battle replay

Before R4 the turn-by-turn battle surface was **fully built and read by
nothing**. `battle_view` had carried `turns`, `status_events` and (since
R2/N2) both rosters since R1; a full-file search of `app.js` for
`state.battle` returned nothing, and `console.py`'s `render_log_entry`
`"battle"` branch printed only the outcome, the round count and the enemy
roster. Both renderers showed the coarse post-battle log entry
(`engine.py:1508-1515`) and nothing else. R4 is the milestone that consumes
the feed.

### The replay model is the source's own

`runBattleScreen` resolves the **entire** battle synchronously
(`runBattle`, `bundle.deobfuscated.js:81208-81222`) and only then replays the
finished `detailedLog` through `animateBattleVisually` (`81272`). The
animation never feeds back into resolution.

This matters for two reasons. It retires the claim both module docstrings used
to make — that synchronous resolution was what blocked an animated battle
screen — and it means pacing is purely a frontend concern: the sequence is
already fixed, so a renderer drains it on whatever clock it likes without
touching `Engine.step`.

### Two new roster fields

`player_team`/`enemy_team` are the **post**-battle state — the replay's last
frame. A replay also needs its first, so version 4 adds
`player_team_start`/`enemy_team_start`, projected through `mon_view` like every
other Pokemon view. The source seeds `animateBattleVisually`'s own HP trackers
from exactly these pre-battle clones (`69084-69092`).

This is the one engine change: `engine._run_battle` snapshots
`player_clone` and `enemy_team` onto `RunState.last_battle`. It is safe by
construction — `battle_loop.run_battle` clones both arguments before touching
anything (`battle_loop.py:289-290`), so those objects still hold pre-battle HP
after it returns. A pure read; no RNG draw, no control flow, exactly like the
two lines R1 added beside it.

### `replay` — the enrichment layer, not a record change

`replay` is a flat, ordered list of presentation steps, each with exactly
`REPLAY_STEP_FIELDS`. It obeys §2's rule literally: it **joins** each record
against the rosters in the same observation and adds **no key to any record**.
`turns[*].events[*]` still projects `battle_loop`'s shape byte for byte —
there is a detector asserting exactly that. `replay` is a sibling key, the way
R2/N2's rosters are siblings.

What each step carries is traced, not invented:

| Field | Source |
| --- | --- |
| `text` for an attack | the line built at `69309-69322`, with `_effectiveness_suffix` reproducing the suffix chain at `69301-69307` |
| `cls` | the `log-player`/`log-enemy`/`log-faint`/`log-item` classes the source's appender is called with |
| `popup` | `spawnDmgPopup`'s kind, computed as at `69274-69281` and gated by the same `damage > 0` test (`69273`) |
| `delay_ms` | the source's own per-kind pause, passed to `BcF(ms)` = `setTimeout(ms / battleSpeedMultiplier)` (`69109-69111`) |
| faint text | `69389-69402` |
| status-tick labels | `69552-69676`, one branch per `status` string |

`hp_bar_color` ports `hpBarColor` (`64134-64137`) verbatim.

### Approximated deliberately (browser-native, no algorithm to port)

- **Per-move attack animation.** `playAttackAnimation` (`66698+`) is a canvas
  particle system with ~30 per-move special cases and type-keyed durations. The
  web client substitutes the step's own `delay_ms`; `console.py` has no
  analogue at all.
- **HP-bar tween.** `animateHpBarFull` (`65035-65064`) interpolates per frame
  under `requestAnimationFrame`. The web client uses a CSS transition over the
  same 250 ms base duration and the same speed divisor.
- **Speed control.** `battleSpeedMultiplier` starts at 1, the Skip button sets
  it to `SKIP_SPEED` = 3 (`63640-63641`, `81251-81260`), and a 30 s wall-clock
  timeout raises it to `OVERTIME_SPEED` = 5 (`81267-81270`). **R5 ports all
  three.** The overtime bump is armed as the replay starts and cleared when it
  finishes (`81273`), and it only ever RAISES the multiplier — the source's
  guard is `battleSpeedMultiplier < OVERTIME_SPEED && (…)`, so a viewer who
  already pressed Skip is never slowed to 5 from a higher speed.

  Not ported, deliberately: the `overtime-banner` the source removes on the
  next line (`81274-81275`) belongs to a **different mechanic that shares the
  name** — an `overtime_start` battle-log record worth 3× damage, built at
  `69377-69382`. That is gameplay, it is Endless-only, and producing the record
  would mean changing `battle_loop.py`, which is the oracle's compared surface.

### Three source behaviors reported rather than reproduced

1. **The site's in-battle text log is dead code in this mirror.**
   `animateBattleVisually`'s log container is `const B2V = null` (`69084`) and
   its appender opens with `if (!B2V) return;` (`69102`), so all ~12 of its log
   calls are no-ops. The strings are still built. Both renderers here DO show
   them — they are the only per-attack presentation a plain-ASCII renderer can
   give at all, and the strings are the source's own — but the live site shows
   HP bars, popups and CSS classes, not a scrolling log. `main.css` (copied
   verbatim) accordingly has no `.log-entry` rule; `index.html` supplies one.
2. **Damage numbers are Endless-mode-only.** `spawnDmgPopup` returns early
   unless `state.isEndlessMode` (`68511-68517`), so a Story/Nuzlocke battle on
   the real site shows no damage number. `popup` therefore carries the computed
   *kind* and leaves the decision to the renderer. `console.py` prints the
   number because text is its only channel; that is a declared deviation.
3. **`runBattle`'s inline logger omits the crit suffix.** The string built
   during resolution (`55960-55976`) has no `" Critical hit!"`; only the
   animation appends it (`69307`). The animation's version is what a replay
   should show, and is what `_effectiveness_suffix` ports.

### Three declared limitations of the feed itself

None is fixable inside R4: each would require changing `battle_loop.py`'s
record shape, which is the oracle's compared surface and forbidden by §2.

1. **Status events cannot be attributed to a round.** `battle_events` and
   `status_events` are separate streams and only the first carries
   `turn_start` markers. The source has no such problem — it animates one
   interleaved `detailedLog`. So status steps are appended after the turn steps
   in stream order with `turn: None`, and both renderers label them as
   post-turn effects rather than implying a round they cannot know.
2. **Some HP changes have no record at all.** Held-item recoil and healing
   (`battle_loop.py:726-739`: `rocky_helmet`, `enemy_recoil`, `life_orb`,
   `shell_bell`) mutate HP after the `attack` event is appended and emit
   nothing. The source covers this with an `effect` record family
   (`animateBattleVisually` handles `type === "effect"` at `69352-69375`) that
   this port does not produce. **Consequence: the last replayed HP and the true
   final HP can legitimately differ.** Both renderers therefore end on the real
   post-battle rosters, which is what the source does too —
   `renderBattleField(Bch, BcL)` at `81278`, immediately after the replay.
3. **A combat KO produces no `faint` record.** `status_events` gains
   `{"type": "faint", ...}` only inside `_status_tick_round`
   (`battle_loop.py:1293` and `1330`) — that is, only when a burn or poison
   tick is what killed the combatant. The ordinary case, a Pokemon knocked out
   by an attack, goes through `_handle_faint` (`battle_loop.py:747-750`),
   which emits nothing. Observed directly in the console transcript in
   `docs/audits/R4-implementation.md`: a member's HP bar reaches `0/17` mid-
   replay with no "fainted!" line under it. `STATUS_EVENT_TYPES` still pins
   `faint` because the status-tick path genuinely produces it; what is missing
   is coverage of the commoner path, and adding it means a new record in the
   oracle's compared stream.

## 12. Version 4 (unchanged) — R5's interaction ports

**No contract change.** `CONTRACT_VERSION` stays **4**: R5 added no observation
field, renamed nothing and changed no shape. Everything below is drawn from
data the contract already carried, which is what §8 asks a milestone to
establish before reaching for a new field.

Three interaction behaviours deferred since R2/R3/R4 are now ported, each
re-traced against the current bundle rather than taken from the prior reports:

### 12.1 Touch long-press node tooltips (`54400-54470`)

The gesture is the source's own, constant for constant:

| Behaviour | Source | Value |
| --- | --- | --- |
| long-press delay | `Bcb`, `54400` | `0x190` = **400 ms** |
| move-cancel radius | `BcR`, `54401` | `0xc` = **12 px** |
| cancel test | `54442` | `dx*dx + dy*dy > 12*12` — a true radius, not a per-axis box |
| tooltip follows the finger once open | `54436-54439` | move only, no re-arm |
| `touchend` | `54447-54457` | dismiss **or** act, never both |
| `touchcancel` | `54458-54461` | clears timer, flag and tooltip |
| synthetic-click suppression | `54462-54470` | a handled `touchend` swallows the click that follows |

Read from the contract: `node.clickable` and `node.tooltip`. Nothing new.

### 12.2 Keyboard shortcuts and `data-shortcut` badges

Two badge systems, one handler (`87896-88156`):

- **Map nodes** (`54474-54510`): the first **two** `accessible && !visited`
  nodes, ordered by `(layer, col)`, get a numbered SVG badge; unmodified
  `Digit1`/`Digit2` on the map screen visit exactly those two (`88130-88144`).
  The port derives the badge list and the key target from **one** function, so
  they cannot disagree about which node is "1".
- **Team bar** (`64629-64632`): while a reorder is legal, slots **1..5** carry
  `data-shortcut = "⇧" + (idx + 1)`; `Shift+Digit2..6` then runs
  `swapPartyLeadWith(1..5)` (`88145-88154`), which is a straight two-element
  swap of `team[0]` and `team[i]` (`88163-88166`). Slot 0 gets no badge: it is
  already the lead. The port expresses this as the engine's existing
  `ReorderTeam` permutation — the same action R3's drag produces.

Also ported, from the same handler: `Escape` → the equip overlay's own Cancel
(`87921-87928`, which M5 proved is *not* a skip), `Enter` → the screen's
continue button (`87998-88006`), `Space` → the screen's skip/cancel button
(`88056-88078`), and `DigitN` → the Nth card on a choice screen
(`88079-88129`). Modifier and text-field guards are the source's
(`87899-87904`, `87982-87985`).

The badges are hidden unless `body.show-shortcuts` is set, which is the
**mirrored, unmodified** `main.css:8464-8474` and `8482-8483`.
`applyShortcutsClass` (`70823-70828`) gates that class on a desktop pointer AND
a "show keyboard shortcuts" setting; this port has no settings system, so only
the pointer half is ported — stated here rather than silently dropped.

Not ported, because the corresponding screens do not exist in this client:
Endless, challenges, the Pokedex/achievements/settings/credits modals, and the
league/mart quick-nav.

### 12.3 The 30-second overtime speed bump

See §11's "Speed control" entry above, which R5 rewrote.
