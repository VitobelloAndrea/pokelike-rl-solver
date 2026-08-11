# battle-oracle

A fixed-seed JavaScript-vs-Python battle equivalence harness. It executes the
real `runBattle`, `buildGen3AbilityConfig`, `buildTraitsConfig`, and
`mergeBattleConfigs` from an audited prefix of the local deobfuscated bundle;
it does not reimplement those functions. The same fixture is run through
`pokelike.battle_loop.run_battle`, and normalized results are compared at the
first divergence.

The oracle currently demonstrates the P0.1 merged-config semantics, the
P0.2 status-tick/faint-dispatch semantics, the P0.3 `half_twice`/
`dragon_first_double` extra-attack semantics, and the P0.3-adjacent
`elec_lead`/`fairy_opening_volley`/`rock_explode` hook-re-entry semantics
recorded in `CODEX.md`. It is a battle oracle, not yet a complete-run
oracle.

## Commands

```text
# run every fixture (also verifies that the extracted prefix is current)
python compare.py --all

# run one fixture
python compare.py fixtures/gen1_baseline.json

# run the JavaScript side alone for fixture debugging
node run-fixture.js fixtures/gen1_baseline.json

# explicitly regenerate the audited prefix after a bundle change
node extract-prefix.js ../../pokelike_forked/js/bundle.deobfuscated.js ./out/battle-prefix.js

# re-audit a regenerated prefix for top-level side effects
node scan-toplevel-danger.js out/battle-prefix.js
```

Every `compare.py` invocation freshly extracts the source prefix to a temporary
file and compares its SHA-256 hash with `out/battle-prefix.js`. A stale prefix,
fixture error, or comparison divergence is a hard failure; nothing is silently
skipped.

## What is compared

Both sides use the same fixture, seed, teams, statuses, generation, and
passives. `compare.py` uses `Engine._battle_configs` for Python branch
selection rather than maintaining a second copy of that rule.

The normalized comparison includes:

- winner, final HP/status/stat stages, and other final combatant state
- exact completed round count
- RNG draw count and final RNG state
- ordered status-tick, poison-drain, and status-faint events
- the P0.2-relevant `onKO`, `onFaint`, `afterStatusTick`, and `sweepKOs`
  hook trace

The JavaScript event count is also reported as a diagnostic. The status and
hook fields are deliberately narrow oracle instrumentation, not a claim that
Python now exposes the source's complete UI event stream.

## Audited JavaScript execution

The relevant functions end near line 81051 of
`pokelike_forked/js/bundle.deobfuscated.js`, beyond the smaller prefix used by
`tools/extract-data`. `extract-prefix.js` finds the end with Acorn and extracts
the whole dependency-bearing prefix. `scan-toplevel-danger.js` performs an AST
walk rather than unreliable textual grep.

The scan found six unconditional top-level `document[...]` UI-wiring calls
around lines 63649-63818. `run-fixture.js` supplies an inert `document` Proxy;
the oracle never invokes UI, network, storage, or audio behavior. The driver is
concatenated with the prefix into one `vm.runInContext` execution because the
bundle's top-level `let` bindings are not reflected onto the sandbox object
across separate VM calls.

Round counting and hook tracing are added by exact, assertion-guarded source
instrumentation. If the expected source needle no longer occurs exactly once,
the JavaScript runner fails instead of producing a plausible but invalid
comparison.

## Battle-config selection

For ordinary non-Endless Story/Nuzlocke battles, matching
`runBattleScreen` around bundle lines 81075-81085:

- Gen1/Gen2 use no battle config.
- Gen3/Gen4 always build the ability config.
- Traits are built from passives. With empty tier maps and no passives,
  `buildTraitsConfig` returns `null` (lines 60733-60738).
- `mergeBattleConfigs(ability, null)` returns the ability config unchanged
  (line 80994). Therefore empty-passive Gen3/Gen4 is ability-only, not a
  merged config.

The oracle found and drove the correction of Python's former behavior of
always constructing a real traits config in Gen3/Gen4.

## Fixture inventory and demonstrated repairs

The original seven fixtures cover the harness baseline and P0.1:

| Fixture | Behavior isolated | Pre-fix | Current |
|---|---|---:|---:|
| `gen1_baseline.json` | no-config baseline | AGREE | AGREE |
| `truant.json` | merged `beforeTurn` return discard | DIVERGE | AGREE |
| `mirror_coat.json` | merged counter side effect plus discarded skip | DIVERGE | AGREE |
| `own_tempo.json` | merged `onBeforeAttack` return discard | DIVERGE | AGREE |
| `trick_room.json` | merged `isTrickRoom` return discard | DIVERGE | AGREE |
| `trait_before_turn_skip.json` | trait-side generic return discard | DIVERGE | AGREE |
| `combined_ability_trait.json` | ability-then-trait order and return discard | DIVERGE | AGREE |

Ten status fixtures isolate P0.2. All ten diverged against the pre-P0.2
Python code and now agree, including round count and RNG consumption:

| Fixture | Behavior isolated | Pre-fix | Current | RNG |
|---|---|---:|---:|---:|
| `burn_nonfatal.json` | surviving burn tick | DIVERGE | AGREE | AGREE |
| `burn_fatal_player.json` | fatal player burn tick | DIVERGE | AGREE | AGREE |
| `burn_fatal_enemy.json` | fatal enemy burn tick | DIVERGE | AGREE | AGREE |
| `poison_nonfatal.json` | surviving poison tick | DIVERGE | AGREE | AGREE |
| `poison_fatal_player.json` | fatal player poison tick | DIVERGE | AGREE | AGREE |
| `poison_fatal_enemy_merged_ko.json` | immediate merged killer-aware `onKO` | DIVERGE | AGREE | AGREE |
| `poison_drain.json` | poison-drain healing | DIVERGE | AGREE | AGREE |
| `poison_drain_blocked.json` | `no_heal_revive` drain gate | DIVERGE | AGREE | AGREE |
| `poison_drain_aspear.json` | `aspearOnHeal` after poison drain | DIVERGE | AGREE | AGREE |
| `poison_multi_dedup.json` | immediate `onKO` plus sweep deduplication | DIVERGE | AGREE | AGREE |

Eight P0.3 fixtures isolate `half_twice`/`dragon_first_double`'s extra
attacks. The original six diverged against the pre-P0.3 Python code (which only
applied `half_twice`'s main-hit damage halving, never the actual extra hit,
and had no `dragon_first_double` extra-hit code at all) and now agree. Two
additional independent-verification fixtures make an extra-hit critical and
the two nested hook calls observable through final state; targeted in-memory
omission mutations made each of those fixtures diverge:

| Fixture | Behavior isolated | Pre-fix / mutation | Current | Rounds agree | RNG agrees | Hook/event evidence |
|---|---|---:|---:|---|---|---|
| `half_twice_basic.json` | extra hit fires every round it connects (`_halfTwiceUsed` is a PER-ROUND flag, reset for that round's active attacker, not a battle-lifetime one) | DIVERGE | AGREE | yes | yes | n/a (no hooks, Gen1) |
| `half_twice_fatal.json` | the extra hit itself is the killing blow; natural post-chain faint dispatch fires off its HP change | DIVERGE | AGREE | yes | yes | n/a (no hooks, Gen1) |
| `half_twice_extra_crit.json` | main hit is noncritical; the fresh extra hit crits and `BIO` applies `crit_boost` once | DIVERGE (BIO omission mutation) | AGREE | yes | yes | final stages |
| `dragon_first_double_basic.json` | extra hit fires once for the WHOLE BATTLE (`battle_config.fired_flags["dragon_first_double"]`), gated on a Dragon-type attacker | DIVERGE | AGREE | yes | yes | n/a (no hooks, Gen1) |
| `dragon_first_double_fatal.json` | the extra hit itself is the killing blow | DIVERGE | AGREE | yes | yes | n/a (no hooks, Gen1) |
| `dragon_first_double_merged.json` | Gen3 merged ability+traits config: the extra hit re-invokes the threaded `attackerDamageMod` hook (`power_bracer`'s 1.2x lands on it too), unlike `half_twice` which skips that hook entirely | DIVERGE | AGREE | yes | yes | final HP |
| `dragon_first_double_hook_reentry.json` | merged nonfatal extra hit re-enters defender `whenAttacked` (Rough Skin) and attacker `afterAttack(isExtraAttack=true)` (Venom Strike) exactly once each | DIVERGE (second-hook omission mutation) | AGREE | yes | yes | final HP/poison stacks |
| `half_twice_dragon_double_combo.json` | both traits on the same Dragon-type attacker: sequential (not nested/recursive) ordering -- `half_twice`'s hit mutates state first, `dragon_first_double`'s own guard re-checks the mutated state | DIVERGE | AGREE | yes | yes | n/a (no hooks, Gen1) |

The generic `hook_trace` field remains intentionally status-focused; P0.3
hook re-entry is demonstrated by source-derived final HP/stacks/stages and
mutation sensitivity, not by a newly duplicated oracle hook model.

Four more fixtures isolate the P0.3-adjacent `elec_lead`/
`fairy_opening_volley`/`rock_explode` hook-re-entry gap. All four diverged
against the pre-fix Python code (which called `calc_damage` for these three
traits but never re-invoked `when_attacked`/`after_attack`/`on_ko`, and, for
`rock_explode`, computed one shared splash value against the first alive
enemy instead of a fresh per-target roll) and now agree:

| Fixture | Behavior isolated | Pre-fix | Current | Rounds agree | RNG agrees |
|---|---|---:|---:|---|---|
| `elec_lead_nonfatal.json` | nonfatal extra hit re-enters `after_attack` (`elec_chain` grants the attacker +1 speed) | DIVERGE | AGREE | yes | yes |
| `elec_lead_fatal.json` | a fatal extra hit still calls `after_attack` (`elec_chain`) AND `on_ko` (`poison_pass`) -- both are independent statements in the source, not an if/else | DIVERGE | AGREE | yes | yes |
| `fairy_opening_volley_multi.json` | genuinely multi-attacker (every alive player Fairy-type, not just the lead) with per-attacker target re-lookup; a fatal hit's ternary SKIPS `after_attack` entirely, a nonfatal hit fires it | DIVERGE | AGREE | yes | yes |
| `rock_explode_fanout.json` | `rock_explode`'s splash recomputes a fresh `calc_damage` PER alive enemy target, not one shared roll applied flat to everyone | DIVERGE | AGREE | yes | yes |

Current result: **29/29 fixtures agree**. The Python test suite passes
**336/336 tests**.

## P0.3 source behavior established

The extra-attack blocks are at bundle.deobfuscated.js:56220-56284
(`half_twice`) and 56285-56364 (`dragon_first_double`), both gated on the
MAIN hit's actual dealt damage (fixed before either block runs) and both
alive. Each recalculates a fresh `calcDamage` (its own crit/variance RNG
draws, same move/items/side/battle_config as the main hit), applies the HP
change directly, and re-enters only a 5-effect post-hit subset
(`crit_boost`/`crit_lifesteal`/`crit_flinch`/`speed_diff`/`bug_critlvl`)
via the source's own separate `BIO` helper (lines 55300-55404) -- textually
distinct from the main hit's own inline 10-effect chain, not a shared call
the main hit also makes. Then, only if the extra hit itself dealt damage,
`whenAttacked`/`afterAttack(isExtraAttack=true)` fire.

The two traits differ in exactly two ways:

- **Flag scope.** `half_twice`'s `_halfTwiceUsed` is reset every round for
  that round's current active attacker on both sides (lines 55430-55431) --
  despite the name, it is a per-round flag, not a battle-lifetime one, so a
  Pokemon holding the lead across many rounds gets an extra hit every round
  it connects. `dragon_first_double`'s flag (`BI9` in the source, `battle_
  config.fired_flags["dragon_first_double"]` in Python) is set once and
  never reset -- true battle-wide, matching the existing `dragon_first_crit`
  convention (`fired_flags["dragon_first_crit"]`).
- **Damage pipeline.** `half_twice` halves the fresh roll and applies the
  overtime multiplier only -- no `attackerDamageMod`, `beforeDamage`,
  life_orb, or defender-side modifiers. `dragon_first_double` DOES re-run
  the threaded `attackerDamageMod` hook (both `ability_config` and
  `traits_config`, ability-then-traits order, matching P0.1's threaded-hook
  semantics) plus `all_more`/`all_half`, then the overtime multiplier --
  still no `beforeDamage`/life_orb/defender-side modifiers.

Python: `battle_loop._apply_half_twice_extra_attack`, `battle_loop.
_apply_dragon_first_double_extra_attack`, and the shared `battle_loop.
_bio_post_hit`, called from `run_battle` right after `_apply_post_hit_
traits` returns (which remains untouched -- it still separately implements
the main hit's own inline chain, unmodified).

## `elec_lead`/`fairy_opening_volley`/`rock_explode` source behavior established

`elec_lead` and `fairy_opening_volley` (bundle.deobfuscated.js:61187-61292
and 61336-61419) run inside `TraitsConfig.onStartFight`. `Gen3AbilityConfig`
never defines `onStartFight`, so `mergeBattleConfigs`'s generic-hook wrapper
(the same wrapper documented under P0.1) only ever calls
`traitsConfig["onStartFight"](...)` -- a real JS method call on the traits
object, which binds `this` to that object for the rest of the call. Every
`this["whenAttacked"]`/`this["afterAttack"]`/`this["onKO"]` reference inside
`onStartFight` is therefore `TraitsConfig`'s OWN method, never
`Gen3AbilityConfig`'s, in or out of a merged Gen3/4 battle -- confirmed by
reading `mergeBattleConfigs`'s per-hook dispatch (`typeof B2n=="function" &&
O[hook](...), typeof B2l=="function" && iu[hook](...)`, each an independent
method call whose own `this` is `O`/`iu`, not the merged wrapper). Both
blocks call `calcDamage` with a literal `null` battle_config-like final
argument (lines 61212, 61363), not the real `battleConfig` -- no
weather/`darkCritFloor` bonus on either extra hit.

The two traits differ in hook-dispatch shape:

- **`elec_lead`** picks the first ALIVE player Electric-type (not
  necessarily the team lead) and the first alive enemy, once per battle
  (`elecLeadFired`). All four consequences of the hit are independent comma
  expressions, not an if/else (lines 61245-61290): `when_attacked` and
  `after_attack(is_extra_attack=True)` both fire whenever `actual_damage >
  0`, and `on_ko` fires whenever the target's HP reaches 0 -- a fatal hit
  gets BOTH `after_attack` (for effects not gated on target-alive, see
  below) and `on_ko`, not one or the other.
- **`fairy_opening_volley`** loops over EVERY alive player Fairy-type
  member, re-finding the first alive enemy before each hit, gated by a
  single battle-wide `fairyVolleyFired` flag (not per-attacker) -- so an
  earlier Fairy KO'ing its target changes who the next Fairy hits. Its
  fatal case is a ternary (lines 61398-61405): a kill pushes only a `faint`
  log entry and SKIPS `after_attack` entirely; `on_ko` is never referenced
  in this block at all, fatal or not.

Neither block calls `on_faint` -- `TraitsConfig` has no `onFaint` method in
the source (only `Gen3AbilityConfig` does), so `this["onFaint"]` would be
`undefined` even if referenced, and the source simply never references it
here.

A related, previously-unknown bug surfaced while building `elec_lead_fatal.
json`: `after_attack`'s own internal `poison_onhit`/`ground_slow_onhit`/
`elec_chain`/`elec_paralyze` cluster is gated behind `target.currentHp > 0`
in the source (line 61444: `BIY==="player" && BIH!==BIY &&
BIz["currentHp"]>0x0 && (...)`), and the Ghost-execute splash independently
requires the same (line 61486). Python's `_after_attack_once` was missing
this gate entirely -- any connecting hit, fatal or not, wrongly ran these
effects. This is not specific to `elec_lead` (it affects the ordinary
main-hit `after_attack` dispatch identically) but was only caught because
`elec_lead_fatal.json`'s exact-final-state comparison flagged a doubled
speed stage. Fixed in the same `_after_attack_once` both `elec_lead` and
ordinary attacks share; see `battle_traits.py`'s inline comments at both
gate sites.

`rock_explode` (lines 62622-62684) lives inside `TraitsConfig.on_ko` itself
-- already correctly reached through the existing `battle_loop._handle_
faint`/`_status_tick_round` dispatch, so no new call-site wiring was needed.
The only defect was the splash damage: the source recomputes a fresh
`calcDamage` (its own crit/variance RNG draws) PER alive enemy target,
using THAT target's own defense stats and held item, not one shared value
computed against the first alive enemy and applied flat to everyone.

Python: `TraitsConfig._apply_elec_lead`, `TraitsConfig.
_apply_fairy_opening_volley`, and the `rock_explode` block inside
`TraitsConfig.on_ko`, all in `battle_traits.py` -- see each site's own
docstring/inline comments for the full citation. 13 new Python unit tests in
`pokelike/tests/test_battle_traits.py`'s `SecondaryAttackHookReentryTests`
cover the `calc_damage(..., None)` argument, the fatal/nonfatal hook-call
gates, the multi-attacker re-lookup, the per-target fan-out, and the
`after_attack` target-alive gate directly -- the oracle fixtures remain the
cross-language proof of exact behavior.

## P0.2 source behavior established

The status loop is around bundle lines 56590-56712:

- Input burn, paralysis, and poison stacks survive battle initialization.
- Burn death logs the faint at the tick site but does not directly dispatch
  `onKO`/`onFaint`; the later traits sweep can observe it.
- Poison drain only runs when `no_heal_revive` is false and damage is
  positive.
- Successful poison-drain healing runs `aspearOnHeal`.
- Enemy poison death immediately calls the final merged config's `onKO`,
  attributing the kill to the first living player Pokemon, then calls
  `onFaint`.
- Player poison death has no immediate killer `onKO`.
- The traits config's handled-KO set prevents `sweepKOs` from processing the
  same poison death twice.

Python now reproduces those mechanics. `BattleResult.status_events` and
`hook_trace` expose only the diagnostics needed to prove their order.

## Known divergent / not yet covered

No current fixture diverges.

Still not covered:

- most of the roughly 95 traits and 85 abilities, beyond the named fixtures
- broad multi-Pokemon switching, recoil/faint combinations, stalemate
  breakers, and overtime behavior
- `elec_lead`/`fairy_opening_volley`/`half_twice`/`dragon_first_double`'s own
  interaction when several of these secondary-attack traits are active on
  the same team in the same battle (each is individually oracle-verified in
  isolation; their combined interaction is not)
- a short full-run comparison containing map, catch, trade, item, evolution,
  battle, and post-battle transitions
- Battle Tower, Endless/Endless2, Challenges, procedural rosters, and submaps

Existing unit tests for uncovered mechanics are useful but are not
cross-language equivalence proof.

## Adding a fixture

1. Use real species and base stats from `pokelike/data/pokedex.json`.
2. Express passives as ID strings, for example
   `"passives": ["sword_charm"]`; both runners normalize these.
3. Run the JavaScript side alone to inspect the source output.
4. Run the full comparison and preserve a demonstrated pre-fix divergence
   before implementing a repair.
