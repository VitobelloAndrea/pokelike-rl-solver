# Route-oracle checkpoint schema — version 1

Both runners (`run-scenario.js`, `run_scenario.py`) emit the same versioned
checkpoint stream. `compare.py` canonicalizes it, hashes it, and reports the
first field-level difference. A scenario, a runner, and the manifest must all
declare the same `schema_version`; a mismatch is a hard failure, never a
silent coercion.

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
  "game_over": false
}
```

`battle` checkpoints additionally carry `event.battle`:

```jsonc
{ "player_won", "rounds", "rng_draws",
  "player_team": [ …normalized mon… ], "enemy_team": [ … ],
  "player_participants": [0, 1],
  "status_events": [ { "type": "status_tick"|"poison_drain"|"faint", … } ] }
```

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
| `CATCH_CHOICE` | `catch-screen` |
| `ITEM_CHOICE` | `item-screen` |
| `NEXT_MAP_READY` | `badge-screen` |
| `GAME_OVER` | `gameover-screen` |
| `VICTORY` | `win-screen` |

Any phase without a mapping is reported verbatim as `<unmapped:NAME>` so a
surprise is visible rather than silently coerced onto a plausible screen.

The unmapped phases are unmapped **on purpose**, because their source
counterparts are overlays that never call `showScreen` at all and so have no
screen id to project onto: `ITEM_EQUIP_CHOICE` →`openItemEquipModal` (79419),
`EVOLUTION_CHOICE` → `showBranchingChoice`'s `#eevee-choice-overlay` (70560),
`REWARD_TEAM_PICK` → `showTeamPickerModal`'s `#submap-pick-modal` (76845).
`MOVE_TUTOR_CHOICE`, `TRADE_CHOICE` and `ESCAPE_ROPE_CHOICE` have screens but
no `choice` bridge and no route through them. `ITEM_CHOICE` was added in M3.1
because `doItemNode` does open with `showScreen("item-screen")` (79261) and
two of the new routes decline an item.

## RNG alignment (`align_rng_after_starter_offer`)

An **oracle instrument, not a repair and not a behavior change.** When
present, both runners call the source's own seeding primitive (`seedRng` /
`rng.seed_rng`) with the same raw Stream-B state, at the same point in the
route: after the starter offer, before the starter is chosen.

It exists because the starter offer *itself* already diverges. The source's
Story starter screen calls `rollShiny()` once per offered starter
(bundle.deobfuscated.js:76175-76194; `rollShiny` always draws, line 74921),
so three draws happen before the player clicks. The Python port's
`ChooseStarter` handler makes zero and forces `is_shiny = False`
(`pokelike/engine.py:580-581`). Without re-alignment every later
RNG-dependent decision — starting with map generation — is only a downstream
echo of that one difference, and any *independent* divergence would be hidden
behind it.

The divergence is **still recorded and still compared**: the `run_init` and
`starter_offered` checkpoints carry the pre-alignment `rng.draws` (js `3`,
python `0`) and the pre-alignment `rng.state`, in **every** scenario, and both
are part of the frozen parity signature.

**Correction (M3.3).** This section previously claimed "the matrix includes at
least one scenario with no alignment at all". That was **false**: all eight
fixtures set `align_rng_after_starter_offer`. It was checked and corrected
rather than left standing.

What an unaligned run actually does was measured instead, by stripping the key
from a copy of `nuzlocke_gen1_permadeath.json` and re-running it: the Python
side's map generation immediately diverges from the source's, so the fixture's
own action list stops being valid — the run dies with
`expected SelectOption while resolving item_choice`, because Python is parked
on a screen the route never planned for. So an unaligned fixture is not simply
"the same route with more differences"; a route that is replayable on both
sides without alignment would have to be authored from scratch. That is
recorded as a residual limitation, not silently implied by omission.

What the alignment instrument does and does not hide, stated as measurements:

* the three pre-alignment draws are observable in every scenario at
  checkpoints 0-1 and are frozen there;
* re-seeding is symmetric and happens at exactly one boundary through each
  side's own primitive (`seedRng` / `rng.seed_rng`) — visible in the signature
  as `rng.state` differing at checkpoints **0-1 only** and agreeing from the
  `rng_aligned` checkpoint onward, in all eight scenarios;
* non-RNG differences survive it — `counters.any_fainted`, `current_node`,
  `map.nodes[i].accessible` and `event.battle.status_events[len]` are all found
  *through* the alignment point.

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

1. **No full per-turn battle event stream.** The source's `runBattle` returns
   a complete `detailedLog`; the Python port's `battle_loop.BattleResult`
   exposes only `status_events` and `hook_trace` — narrow oracle
   instrumentation added for the battle oracle — and has no counterpart to
   compare a full log against. This schema therefore compares winner, exact
   round count, RNG draws, final per-combatant state, participant set, and
   the ordered status-tick / poison-drain / status-faint stream, reusing
   `tools/battle-oracle/run-fixture.js`'s derivation verbatim. The JavaScript
   log length is carried as `__diagnostic_event_count` and is **never
   compared**, because there is no Python value to compare it with.
   Fabricating a Python event log to fill the gap would have made the oracle
   assert its own invention. Producing a real one is renderer-track (R4)
   work.

   **M3.1 addendum, now measured rather than predicted:** this limitation has
   a concrete cost. The port's `_status_tick_round`
   (`pokelike/battle_loop.py:1109-1160`) logs burn and poison ticks only,
   while the source (55687-55710) also logs `sleep_wake` / `sleep_skip`. The
   Gen3 Admin battle is the first route battle to inflict sleep, and it shows
   the gap as `event.battle.status_events[len]` js=3 / py=0 — with winner,
   round count, RNG draws and final state all agreeing exactly, because sleep
   itself *is* modelled. Frozen as finding 4 in
   `findings/M3-parity-blockers.md`; not repaired.
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
4. **`move-tutor`, `trade`, `question`, `legendary` and `shiny` screens have
   no `choice` bridge**, and neither do the three overlays that never call
   `showScreen`: `openItemEquipModal` (79419), `showBranchingChoice` (70560)
   and `showTeamPickerModal` (76845). The submap `reward` flow *is* bridged,
   through `swap-screen` — the `fossil` and legendary reward cases call
   `showSwapScreen` directly (77076 / 77096) — but the `sacrifice` and
   `stat10` rewards route to the unbridged team-picker modal instead. Routes
   are planned to avoid the unbridged ones; `plan_route.py` stops rather than
   guess when only an unbridged screen is reachable.
5. **`counters.any_fainted` reads through `getattr(st, "any_fainted", False)`.**
   That defensive read is deliberate: `RunState` has no such field, and
   reporting a constant `false` is what makes the absence show up as a
   comparable difference against the source's `state.anyFainted` (81372)
   rather than crashing the runner. Frozen as finding 5.
