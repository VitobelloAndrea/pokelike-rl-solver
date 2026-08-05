# Route-oracle checkpoint schema — version 2

Both runners (`run-scenario.js`, `run_scenario.py`) emit the same versioned
checkpoint stream. `compare.py` canonicalizes it, hashes it, and reports the
first field-level difference. A scenario, a runner, and the manifest must all
declare the same `schema_version`; a mismatch is a hard failure, never a
silent coercion.

**Version 2 (M4.2)** adds one compared top-level field, `resume_state` — see
"Live save/resume guards" below. A v1 stream and a v2 stream are not
comparable, so the version was bumped and every scenario, the manifest and
both runners were updated together. Nothing was removed, excluded or
loosened: v2 is v1 plus one field.

## Scenario file

```jsonc
{
  "schema_version": 1,
  "scenario": "story_gen1_map0_to_map1",   // stable id, appears in every checkpoint
  "description": "...",                     // human text, never compared
  "mode": { "nuzlocke": false, "gen2": false, "gen3": false, "gen4": false },
  "seed": 123456789,                        // Stream-B seed both sides start from
  "starter_index": 0,                       // index into the starter offer
  "align_rng_after_starter_offer": 88888888,// optional, see "RNG alignment"
  "actions": [
    { "kind": "visit",       "node": "n1_1" },
    { "kind": "choice",      "index": null },   // null = decline / skip
    { "kind": "advance_map" }
  ]
}
```

`mode` maps to `startNewRun(nuzlocke, gen2, gen3, gen4)` on the JavaScript
side and `Engine.reset(...)` on the Python side. At most one generation flag
may be set.

### Action vocabulary

| kind | JavaScript | Python |
|---|---|---|
| `visit` | `onNodeClick(state.map.nodes[node])` | `Engine.step(VisitNode(node))` |
| `choice` | invoke the handler the **source** attached to the corresponding card / skip button | `Engine.step(SelectOption(index))` |
| `advance_map` | click `#btn-next-map`, whose handler is `showBadgeScreen`'s own `startMap(currentMap + 1)` | `Engine.step(AdvanceMap())` |

`choice` is resolved against whichever screen the source is currently
showing, using bridges that are wired to the source's own elements:

| screen | `index: N` | `index: null` |
|---|---|---|
| `swap-screen` | team has room → the incoming card (`#swap-incoming .poke-card`); team full → the Nth card in `#swap-choices` | `#btn-cancel-swap` |
| `catch-screen` | Nth card in `#catch-choices` | `#btn-skip-catch` |
| `item-screen` | Nth card in `#item-choices` | `#btn-skip-item` |
| `battle-screen` | — | `#btn-continue-battle` |

The driver never invents a decision: it invokes the exact listener the source
registered, at the element the source registered it on. The single control it
presses on its own is `#btn-continue-battle`, which selects nothing — it only
dismisses a finished battle.

## Checkpoint

Every checkpoint carries the full normalized state plus a kind-specific
`event`. Kinds, in the order a run produces them:

| kind | emitted when |
|---|---|
| `run_init` | run created, before the starter is chosen |
| `starter_offered` | starter options exist, nothing chosen yet |
| `rng_aligned` | only when `align_rng_after_starter_offer` is set |
| `starter_selected` | starter accepted; map 0 generated |
| `node_pre` | immediately before a `visit` |
| `battle` | one per `runBattle` call that the action produced |
| `node_post` | after the node action settles or suspends |
| `choice_pre` / `choice_post` | around a `choice` |
| `map_transition_pre` / `map_transition_post` | around `advance_map` |
| `terminal` | end of the scenario |

Fields:

```jsonc
{
  "schema_version": 1,
  "scenario": "...", "seq": 0, "kind": "node_pre", "event": { ... },
  "mode": { "nuzlocke": …, "gen2": …, "gen3": …, "gen4": … },
  "seed": 123456789,
  "rng": { "state": 1323186932, "draws": 23 },   // raw Stream-B state + cumulative draws
  "screen": "map-screen",                         // see "Phase ↔ screen" below
  "current_map": 0,
  "current_node": "n0_0",
  "in_sub_map": null,                             // "underground" | "distortion" | null
  "map": {
    "index": 0,
    "is_sub_map": null,
    "nodes": [ { "id", "type", "layer", "col", "visited", "accessible", "revealed",
                 "map_index"?, "legendary_species_id"?, "sub_kind"?, "reward_kind"?,
                 "kind"?, "trainer_key"?, "wild_boss"?, "boss_team"?, "reward"? } ],
    "edges": [ ["n0_0", "n1_0"], … ]              // source order, never sorted
  },
  "sub_map_return": {                             // saved parent identity
    "kind", "map_index", "node_id", "has_map", "map_node_count",
    "map_topology": { … the COMPLETE saved parent map, normalized exactly
                      like the live `map` above: index, is_sub_map, every
                      node with its type/layer/col/flags/extras, and the
                      ordered edge list … }
  },
  "counters": { "badges", "elite_index", "starter_species_id", "max_team_size",
                "silver_beaten", "fought_admin", "used_pokecenter", "picked_up_item",
                "used_tm", "used_ball_catch", "got_via_question", "any_fainted",
                "escaped_via_rope", "distortion_worlds_entered",
                "distortion_legendary_claimed", "entered_sub_map" },
  "team": [ { "slot", "species_id", "form_id", "name", "level", "max_hp", "current_hp",
              "types", "move_tier", "held_item", "is_shiny", "status", "burned",
              "paralyzed", "poison_stacks", "base_stats", "stat_buffs" } ],
  "items": [ "escape_rope", … ],                  // bag order preserved
  "game_over": false,
  "pending": { … see "Pending-choice option identity" … } | null,
  "resume_state": { … see "Live save/resume guards" … }
}
```

`battle` checkpoints additionally carry `event.battle`:

```jsonc
{ "player_won", "rounds", "rng_draws",
  "player_team": [ …normalized mon… ], "enemy_team": [ … ],
  "player_participants": [0, 1],
  "status_events": [ { "type": "status_tick"|"poison_drain"|"faint", … } ],
  "turns": [ … see "Ordered per-turn battle events" … ] }
```

## Live save/resume guards (`resume_state`)

**Added in M4.2**, and the reason the schema version moved to 2.

The source keeps three node-scoped records so that a run saved while parked
on a choice screen resumes onto the *same* offer instead of re-rolling it:

| field | declared | written | read | key |
|---|---|---|---|---|
| `savedQuestionResolve` | 20030 | `onNodeClick` 77332 | 77328 | `"m<currentMap>:<nodeId>"` |
| `savedCatch` | 16827 | `doCatchNode` 78765 | 78441 | the **bare** `nodeId` |
| `savedShinyNode` | 22178 | `doShinyNode` 80919 | 80887 | `"m<currentMap>:<nodeId>"` |

They are compared because **which of them an exit clears is
branch-specific**, and getting that wrong changes no other compared field:

| exit | `savedCatch` | `savedQuestionResolve` | `savedShinyNode` |
|---|---|---|---|
| `doShinyNode` room accept (80962) | untouched | **retained** | cleared |
| `#btn-skip-shiny` (80986) | untouched | **retained** | cleared |
| `catchPokemon` room accept (79041-79042) | cleared | **cleared** | untouched |
| `#btn-skip-catch` (78956-78957) | cleared | **cleared** | untouched |
| `showSwapScreen` — all three exits (79182-79184 / 79227-79229 / 79252-79254) | cleared | cleared | cleared |

Shape — `null` per record when absent, so an absent record is a *compared*
value rather than a silent omission:

```jsonc
"resume_state": {
  "saved_question_resolve": { "key": "m1:n6_1", "resolved_type": "shiny" } | null,
  "saved_catch":            { "key": "n2_1",
                              "instances": [ …ordered option identities… ] } | null,
  "saved_shiny_node":       { "key": "m1:n6_1", "species_id": 126 } | null
}
```

Only deterministic semantic identity is projected. `savedCatch.instances`
reuses the ordinary option-identity shape (role `"saved_catch"`, `slot` = the
offer position) because the order is meaning: a resumed catch screen resolves
`action.index` against it. `savedCatch.rerollPool` / `.rerolled` / `.level`
are Endless-and-reroll bookkeeping the Python port does not model, and the
instances already carry every level and identity the comparison needs.
`savedShinyNode` carries only the species because that is all the source
stores — it rebuilds the card with `createInstance` on resume either way.

Both sides read real run state. `driver.js` reads `state.savedQuestionResolve`
/ `state.savedCatch` / `state.savedShinyNode` directly; `run_scenario.py`
reads `RunState.saved_question_resolve` / `.saved_catch` / `.saved_shiny_node`,
which `_resolve_question`, `_visit_catch`, `_visit_shiny`, `_try_add_to_team`,
`_resolve_catch_choice` and `_resolve_swap_choice` maintain at the source's
own write and clear points. Nothing is synthesized in either runner.

`saved_question_resolve` replaced the port's former `question_cache` dict in
M4.2. The source keeps **one** `{key, resolvedType}` slot, not a map: a
non-matching key is overwritten outright (77326-77332), so a second question
node evicts the first. The dict was observationally equivalent for every
route this harness can express — a question node that suspends on a pending
choice cannot be revisited until it advances — but it was a different shape
from the field now being compared. `_start_map`'s `question_cache.clear()`
went with it: the source has no clear there, and a stranded record is
harmless because the key is map-qualified.

### Live state is not persisted state

`resume_state` is the **live** `state` fields, nothing more. What `saveRun()`
last wrote is a different fact and is deliberately not projected:

- `saveRun` is called by `onNodeClick` before dispatch (77334), by
  `doCatchNode` right after pinning `savedCatch` (78771), and by
  `doShinyNode` right after pinning `savedShinyNode` (80923);
- **no accept or decline handler calls it at all** — so after a shiny room
  accept the most recent source snapshot still holds the *pre-accept* team
  and a *non-null* `savedShinyNode`, even though live state has neither;
- `driver.js` stubs `saveRun` to a no-op (it serialises the whole run to
  `localStorage` and has no gameplay effect), and the Python port has no
  persistence layer at all.

So this schema makes **no claim about save/reload parity**. Persistent
reload behaviour is outside the current port surface; only live-field parity
is asserted here.

## Pending-choice option identity

**Added in M3.3b (workstream 3).** Before it, `pending` carried only
`{phase, optional, option_count}`: the schema proved a choice screen existed
and how many affordances it had, but nothing about *which* objects were
offered or in what order, so a mutation that kept the count constant while
substituting or reordering an option was invisible.

```jsonc
"pending": {
  "phase": "choose_starter" | "catch_choice" | "item_choice" | "swap_choice",
  "optional": true,
  "option_count": 3,
  "options": [                        // ORDERED; index N is the `choice` index
    { "role": "starter" | "catch" | "item" | "swap_accept" | "swap_release"
              | "incoming" | "team",
      "kind": "mon" | "item",
      "species_id": 408, "form_id": null, "name": "Cranidos",
      "item_id": null,                // items only
      "slot": null,                   // team position, for options naming a
                                      // CURRENT team member
      "instance": { …normalized mon… } | null }
  ],
  "context": {                        // swap screens only, else null
    "incoming": { …option… },
    "team":     [ { …option…, "slot": 0 }, … ]
  }
}
```

Both runners read the offer from state the **source itself owns and orders**,
at build time and strictly before any listener can resolve it:

| screen | JavaScript | Python |
|---|---|---|
| starter | the per-card `renderPokemonCard(instance, …)` argument, cross-checked against the `#starter-choices` child count (76175-76194) | `PendingChoice.extra["instances"]` — the three real `Combatant`s `Engine.reset` builds from the same loop |
| catch | `state.savedCatch.instances` (78765; replaced in place on a reroll at 78896) | `PendingChoice.extra["candidates"]` |
| item | `state.itemOffer.ids` (79372), resolved through the source's own `ITEM_POOL`/`USABLE_ITEM_POOL` lookup (79348-79358) | `PendingChoice.extra["items"]` |
| swap | `showSwapScreen(incoming, node)`'s own arguments, captured by a delegating wrapper before it runs (79141), plus `state.team` | `extra["incoming"]` plus `RunState.team` |

Each side independently cross-checks its projection against the other
cardinality it can observe — the JavaScript side against the DOM cards the
source actually appended, the Python side against `PendingChoice.options` —
and a disagreement is a hard error, never a silently preferred side.

**`instance` is reported, not omitted, when a runtime built none.** Under M3
the source's starter screen materialised three `createInstance` objects before
the click while the port offered species ids and instantiated on click, so the
Python projection reported `null` — a *compared* difference rather than a
silent exclusion, frozen as blocker 1(b). **M4 repaired it**: the port now
builds the three instances at offer time, the projection reads them, and the
species-only helper that produced the `null` was deleted so the old shape
cannot return. The projection now raises if a starter offer carries no
instances.

Deliberately **not** in the projection: DOM text, css classes, element or
object identity, closure identity, and any opaque hash. `attackerName`-style
display strings are excluded for the same reason they are excluded everywhere
else in this schema.

### The swap screen's three branches, and one declared asymmetry

`showSwapScreen(incoming, node)` (bundle.deobfuscated.js:79141) computes two
flags at 79143-79145 and everything below follows from them:

```js
iu = state.team.length < 6                 // "has room"
ip = state.challengeNoReplace && !iu       // "Ride or Die", team already full
```

| state | source | `options` |
|---|---|---|
| `iu` (room) | one clickable incoming card, 79171-79201; the click **appends** | exactly one `swap_accept` |
| `!iu && !ip` (full) | one card per `state.team[i]`, 79202-79246; clicking *i* runs `state.team.splice(i, 1, incoming)` at 79230, so `team[i]` is **released** and the incoming takes its slot | one `swap_release` per team member, in `state.team` order, each carrying its `slot` |
| `!iu && ip` | the loop's guard `B2a < state.team.length && !(iu || ip)` is false immediately: **no cards at all**, and the prompt reads "Ride or Die — your team is full and can't be changed" | see below |

Cancel (`#btn-cancel-swap`, 79249-79258) is always present, which is why
`optional` is `true` in all three.

**Declared JS/Python asymmetry — `challengeNoReplace` (M3.4 Defect B).** The
two runners do *not* behave identically in the third row, and this is recorded
rather than repaired:

* **JavaScript** (`driver.js`) mirrors the source exactly. It reads
  `state.challengeNoReplace` and, with a full team, projects **zero** options —
  matching the zero cards the source builds. Its DOM cross-check
  (`#swap-choices` child count) then agrees at 0.
* **Python** (`run_scenario.py`) has **no counterpart** and always projects the
  full release list. The reason is not an oversight in the projection: the
  ported engine has no `challenge_no_replace` field *at all*. The flag is set
  in exactly one place in the source — `case "noreplace"` of the challenge
  setup switch at 82796 — and **Challenges mode is out of M3 scope**. Story and
  Nuzlocke, the only modes the route matrix runs, never set it.

So under `challengeNoReplace` with a full team the two projections would
disagree in cardinality (0 vs 6) for a reason that belongs to the *projection*,
not to the port. That configuration is **unreachable** in this matrix, so it is
latent, not active. To stop it becoming a silent trap if Challenges mode is
ever ported, `run_scenario.py` carries a tripwire: it raises if the engine ever
grows a truthy `challenge_no_replace` on a full-team swap, pointing here. The
correct repair at that point is to port the flag and give the Python
projection the same `ip` guard — not to relax the tripwire.

## Ordered per-turn battle events

**Added in M3.3b (workstream 5).** `event.battle.turns` partitions the battle
into its rounds and lists the ordered attack events inside each:

```jsonc
"turns": [
  { "turn": 1,
    "events": [
      { "type": "attack", "side": "player", "attacker_idx": 0,
        "target_side": "enemy", "target_idx": 0,
        "move_name": "Vine Whip", "move_type": "Grass",
        "damage": 9, "type_eff": 2, "crit": false, "is_special": false,
        "attacker_hp_after": 19, "target_hp_after": 4,
        "extra_attack": false } ] } ]
```

**Family.** `type === "attack"` and nothing else. Every source attack site
emits that one shape: the ordinary hit (55979-55995), the `noDamage` "nothing
happened" hit (55774-55801), and both extra-attack hits (56240-56255,
56322-56338, which additionally set `isExtraAttack`).

**Turn boundaries.** The source's `detailedLog` is one flat stream with no
per-round delimiter — its only `overtime_start` marker is pushed once, at the
overtime threshold (55418-55422). `run-scenario.js` therefore extends its
existing, assertion-guarded round-counter edit at `BI4++` (55415-55418, the
top of the round loop, before any of that round's events exist) to also record
`BcM.length` at that instant. That is a read of a length plus a push onto a
driver-owned array: no source state, no control flow and no RNG draw is
touched. The Python side emits `{"type": "turn_start", "round": n}` at the
matching point and both runners fold their flat stream into the shape above.
An event of the compared family occurring before the first turn boundary is a
hard error on both sides, not a silently dropped event.

**Why the Python side needed a behavior-file change.** Runner-only observation
was attempted first and is insufficient: `battle_loop.run_battle` is a single
function whose round counter and per-hit values are locals, `BattleResult`
exposed only `status_events`/`hook_trace` (neither carries an attack or a
round boundary), and the only module-level callables a runner could patch that
run once per attack are `battle.calc_damage` (pre-modifier-chain, so it cannot
report applied damage or post-hit HP) and the private `_handle_faint` (faints
only, no round boundary). `battle_loop.py` therefore gained one field,
`BattleResult.battle_events`, written only by `append` at three points. It was
proved behavior-neutral by stripping the M3.3b fields from both runners'
streams and confirming all sixteen stream hashes equal the pre-change
baselines byte for byte, plus 29/29 battle-oracle fixtures and full suite
discovery.

**Excluded from the events, and why.** `attackerName`/`targetName` —
presentation; `side` plus the index identifies the combatant exactly and the
teams themselves are compared field by field in the same checkpoint, and the
source's names go through `nickname || name`, which the port does not model.
`extra_attack` is always `false` on the Python side: the half_twice /
dragon_first_double follow-up hits live in helpers that are not instrumented
and require a passive the Story/Nuzlocke matrix never grants. If one ever
fired, the two projections would disagree in length rather than silently
agree.

**What this does NOT cover.** Status ticks, effects, faints, send-outs,
transforms and trait/ability triggers are still outside the projection.
`status_events` continues to carry the status-tick subset (untouched, and the
sleep-logging gap remains frozen as blocker 4). Producing a complete
cross-runtime event log remains renderer-track (R4) work.

`map_topology` inside `sub_map_return` is deliberately the **complete**
normalized parent map, not a count and not a flags-only summary. The parent's
lock state at save time is the whole point of the eager sibling lock, and the
parent's full topology is what `coverage.exact_parent_return` compares the
returned map against.

**M3.3 correction.** This field used to be `map_flags`, an
`{id, visited, accessible}` list. That was too weak for what the
`exact_parent_return` tag claims to prove: with only those three fields, a
restored parent whose node **type**, `revealed` flag, gameplay extras, map
index, edge **order**, or edge endpoint had changed still earned the tag — six
such mutations were demonstrated invisible before the change, and all six now
lose it.

## Ordering, hashing, and diffing

* Mappings are serialized with sorted keys; **sequences keep source order**.
  Ordering carries meaning here (team slots, bag contents, edge list, event
  streams) and is never sorted away.
* Node lists are ordered by node id on both sides, because the JavaScript
  `map.nodes` is an object (no defined order) while Python's is a dict. Node
  *identity* and every flag are still compared exactly; only the container's
  incidental iteration order is normalized.
* Integral floats are folded to integers. JavaScript has one number type, so
  `5` and `5.0` are the same value; the runners must not disagree over which
  one JSON happened to emit. Non-integral floats are compared as-is.
* `sha256` is computed per checkpoint and over the whole stream. The stream
  hash mixes each checkpoint's **index** with its hash, so an omitted,
  inserted, or reordered checkpoint changes it even when every individual
  checkpoint is unchanged.
* On mismatch `compare.py` prints the first differing checkpoint index, both
  hashes, and a `path = js / python` list for every differing field. An
  opaque hash mismatch alone is never the output.

## Phase ↔ screen

Python has `RunState.phase`; JavaScript has `showScreen(id)`. Neither exists
on the other side, so both are projected onto one `screen` string:

| Python phase | screen |
|---|---|
| `CHOOSE_STARTER` | `starter-screen` |
| `ON_MAP` | `map-screen` |
| `SWAP_CHOICE` | `swap-screen` |
| `CATCH_CHOICE` | `catch-screen`, or `shiny-screen` -- see below |
| `ITEM_CHOICE` | `item-screen` |
| `TRADE_CHOICE` | `trade-screen` |
| `NEXT_MAP_READY` | `badge-screen` |
| `GAME_OVER` | `gameover-screen` |
| `VICTORY` | `win-screen` |

Any phase without a mapping is reported verbatim as `<unmapped:NAME>` so a
surprise is visible rather than silently coerced onto a plausible screen.

**`CATCH_CHOICE` is not one screen.** `_visit_shiny` (engine.py:2340-2377)
reuses `Phase.CATCH_CHOICE` wholesale rather than a dedicated phase, but the
source's `doShinyNode` (bundle.deobfuscated.js:80872-80990) uses its own
`showScreen("shiny-screen")`, never `catch-screen`. `run_scenario.py`'s
`_screen_for`/`_is_shiny_origin` (M4 repair) tell the two apart by
`pending.extra["origin"]`: `"shiny_node"` for a real shiny offer,
`"catch"`/`"question"` for an ordinary (possibly question-resolved) catch --
**not** a shared `"question"` value, which was a real bug the M4 repair found
(a shiny catch was indistinguishable from an ordinary question-resolved catch
by origin alone).

**M4.1 correction.** This section previously went on to say that the distinct
origin also meant a shiny catch must *not* set `got_via_question`, "which the
source's `doShinyNode` never does -- it never calls
`catchPokemon`/`recordMonOrigin` at all". The second half of that is **false**
against the current source, and the port acted on it. `doShinyNode`'s
accept-with-room branch does not call `catchPokemon`, but it *inlines* an
equivalent body that includes a bare `recordMonOrigin(B)` at 80967 -- and `B`
is still the **QUESTION**-typed node, because `onNodeClick` dispatches on the
resolved type (`case "shiny"`, 77384) without rebinding `B`. So the source
does set `gotViaQuestion` on a shiny accept.

`"shiny_node"` is therefore a **projection discriminator only** -- it decides
`shiny-screen` vs `catch-screen`, not the `recordMonOrigin` outcome, and
`_try_add_to_team` now treats it exactly like `"question"` for the origin
flags. The error survived M4 because all five shiny resolutions in that
matrix were declines, and `#btn-skip-shiny` (80984-80989) genuinely does skip
`recordMonOrigin`; routing `story_gen1_shiny_accept` exposed it at once as
`counters.got_via_question` js=true / py=false.

The remaining unmapped phases are unmapped **on purpose**, because their
source counterparts are overlays that never call `showScreen` at all and so
have no screen id to project onto: `ITEM_EQUIP_CHOICE` →`openItemEquipModal`
(79442), which `doMoveTutorNode` (80464) also reuses the same
`#item-equip-modal` id/class for; `EVOLUTION_CHOICE` →
`showBranchingChoice`'s `#eevee-choice-overlay` (70560); `REWARD_TEAM_PICK` →
`showTeamPickerModal`'s `#submap-pick-modal` (76845) -- the `sacrifice`/
`stat10` submap rewards. **All four now have real `choice` bridges** (M4
repair item 1): `driver.js`'s `detectOverlay()` finds them directly from the
DOM the source built (mounted-in-`document.body` state, and — for the shared
`item-equip-modal` id — a literal content check distinguishing
`doMoveTutorNode`'s template from `openItemEquipModal`'s), never by inferring
from `currentScreen`, which never changes for any of them. `ESCAPE_ROPE_CHOICE`
has a screen but still has no `choice` bridge and no route through it --
genuinely out of this repair's scope. `ITEM_CHOICE` was added in M3.1 because
`doItemNode` does open with `showScreen("item-screen")` (79261) and two of the
new routes decline an item. `TRADE_CHOICE` was added in M4 (`doTradeNode`'s
ordinary, non-Endless2 path opens with `showScreen("trade-screen")`, 80587).

## RNG alignment (`align_rng_after_starter_offer`) — RETIRED in M4

**No fixture sets this key any more, and the primary parity matrix does not
depend on it.** A focused test (`test_no_scenario_uses_post_starter_rng_
alignment`) asserts that, and a mutation that reintroduces the key into a gate
scenario is killed by it.

It existed for exactly one reason: the starter offer itself diverged. The
source's Story starter screen calls `rollShiny()` once per offered starter
(bundle.deobfuscated.js:76175-76194; `rollShiny` always draws, 74921), for
three draws before the player clicks, while the port made zero and forced
`is_shiny = False`. Every later RNG-dependent decision was then a downstream
echo of that one difference, so the runners re-seeded symmetrically, through
each side's own primitive, at one route point after the offer.

M4 repaired the divergence instead: `Engine.reset` performs the same three
draws, in the same order, and keeps the three resulting instances. With
nothing left to isolate, keeping the instrument would only have provided a
place for that exact regression to hide, so it was removed and seven routes
were re-derived unaligned with `search_route.py --cross-runtime`.

The runner still honours the key if a scenario carries one, and the
`rng_aligned` checkpoint kind still exists, so an investigator can re-introduce
it deliberately for a one-off probe. It must not come back into the matrix.

## Excluded fields, and why

Only these are excluded. The list is deliberately tiny and must never grow
to hide a divergence.

| excluded | reason |
|---|---|
| DOM element identity, css classes, sprite/image urls, display strings | presentation |
| `trainerSprite` on nodes | presentation; the source itself deletes it when retyping a node to SILVER/MAGMA/AQUA/UNDERGROUND/DISTORTION |
| wall-clock timestamps, object addresses | not reproducible |
| `map.nodes` container iteration order | JS object vs Python dict; node identity and all flags are still compared |
| `__diagnostic_event_count` | see below |

Nothing else is excluded. Identity, ordering, flags, RNG state, and any
observed discrepancy are always compared.

## Known schema limitations (recorded, not papered over)

1. **No *complete* per-turn battle event stream.** **Narrowed in M3.3b.** The
   ordered, turn-delimited **attack** family is now compared in full — see
   "Ordered per-turn battle events" above — and it agrees exactly in all
   eight scenarios. What is still uncompared is the rest of the source's
   `detailedLog`: `effect`, `faint`, `send_out`, `transform`,
   `trait_trigger`, `ability_trigger` and `overtime_start`. The port has no
   counterpart for those, and fabricating one would have made the oracle
   assert its own invention. The status-tick subset continues to be compared
   separately as `status_events`, reusing
   `tools/battle-oracle/run-fixture.js`'s derivation verbatim. The JavaScript
   log length is still carried as `__diagnostic_event_count` and is **never
   compared**, because there is still no whole-log Python value to compare it
   with. Producing a complete one is renderer-track (R4) work.

   **M3.1 addendum, REPAIRED in M4.** The port's `_status_tick_round` logged
   burn and poison ticks only, while the source's pre-turn block (55647-55710)
   also logs `flinch`, `freeze_skip`, `sleep_wake` and `sleep_skip`. That
   showed as `event.battle.status_events[len]` js=3 / py=0 — with winner,
   round count, RNG draws and final state all agreeing exactly, because sleep
   itself *was* modelled correctly. `battle_loop`'s turn loop now emits the
   whole pre-turn `status_tick` family through a pure `_pre_turn_tick` helper,
   reading the status **before** `battle.resolve_pre_turn_status` folds freeze
   and sleep-not-woken into one boolean. `story_gen3_sleep_ticks` observes both
   sleep branches cross-runtime. `flinch` and `freeze_skip` are emitted but no
   current route inflicts either, so they are source-shaped rather than
   source-confirmed.

   **Also repaired in M4: the Mirror Coat counter-hit.** The `mirror_coat`
   ability's `beforeTurn` hook (58108-58142) pushes a real `type: "attack"`
   entry — a member of the family `event.battle.turns` already compares — and
   the port applied the damage without logging it. `damage` is the source's
   CLAMPED delta, not `stored * 2`; the two differ on an overkill.
   `story_gen3_mirror_coat` observes 9 such events cross-runtime.
2. **No XP field.** The game has no XP pool: `applyLevelGain`
   (bundle.deobfuscated.js:56791) awards levels directly. `level` is compared
   per team member and per battle; there is no XP quantity in either
   implementation to record.
3. **`moves` is represented by `move_tier`, not a move list.** Neither
   implementation stores a per-Pokemon move list: moves are derived at use
   time from `move_tier` plus the attacker/defender types
   (`getBestMove` / `battle.get_best_move`). `move_tier` — the identity input
   that fully determines selection — is compared per team member, and the
   moves actually chosen are compared through the battle events.
4. **`move-tutor`, `trade` and `shiny` screens, and the `openItemEquipModal`
   (79442) / `showBranchingChoice` (70560) / `showTeamPickerModal` (76845)
   overlays — REPAIRED in M4.** All six now have real `choice` bridges (see
   "Phase ↔ screen" above for how the three `showScreen`-less ones are
   detected). Ordinary `LEGENDARY` needed no new bridge at all: its win path
   always ends in the already-bridged `swap-screen`
   (`_visit_legendary`/`doLegendaryNode`). The submap `reward` flow's
   `fossil`/legendary cases were already bridged through `swap-screen`
   (`showSwapScreen` directly, 77076/77096); `sacrifice`/`stat10` now resolve
   through the newly-bridged team-picker modal instead of being routed
   around. `plan_route.py` (the older greedy walker, since replaced by
   `search_route.py` for anything needing to reach one of these) still stops
   rather than guess when only an unbridged screen is reachable;
   `ESCAPE_ROPE_CHOICE` remains genuinely unbridged and out of scope.
5. **`counters.any_fainted` — REPAIRED in M4.** `RunState.any_fainted` is now
   a real field, initialised false and set true only when a won Nuzlocke
   battle's cull actually removes at least one member (81371-81372, inside the
   win branch opened at 81278). The runner's defensive
   `getattr(st, "any_fainted", False)` is retained so that deleting the field
   again would surface as a compared difference rather than a crash.
