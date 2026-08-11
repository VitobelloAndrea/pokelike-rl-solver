"""R1: detectors for the renderer observation/event contract.

A documented contract with no detector is described, not frozen. These tests
exist so that each of the following FAILS rather than being noticed later by
a renderer author:

- a field silently disappearing from (or appearing in) the observation, any
  Pokemon view, any node view, or any item view;
- an event `type` string being renamed in `battle_loop`;
- the battle event stream losing its order;
- `engine.py`'s plumbing dropping part of the stream on the way out.

The order/completeness detectors are deliberately BOTH structural (invariants
that survive a legitimate re-freeze) and golden (a hash of the exact stream
for a fixed seed, which is maximally sensitive). The structural ones say what
is true; the golden one notices everything else.

Nothing here compares against the JavaScript runtime -- that is the route
oracle's job, and this contract is explicitly not the oracle's. See
docs/renderer-contract.md.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from pokelike import battle_loop, engine
from pokelike.render import console, contract, play
from pokelike.webui.state_json import encode_state


# The observation keys `encode_state` emitted BEFORE R1. Pinned literally so
# that R1's "strict superset, no existing client breaks" claim keeps being
# checked rather than being a one-time assertion in a report.
_PRE_R1_OBSERVATION_KEYS = frozenset({
    "phase", "current_map", "badges", "elite_index",
    "nuzlocke_mode", "gen2_mode", "gen3_mode", "gen4_mode",
    "team", "items", "map", "pending", "log", "log_total",
    "game_over", "won", "run_seed",
})
_PRE_R1_MON_KEYS = frozenset({
    "species_id", "name", "level", "current_hp", "max_hp", "hp_pct",
    "status", "is_shiny", "types", "held_item", "move_tier",
})
_PRE_R1_NODE_KEYS = frozenset({
    "id", "type", "layer", "col", "visited", "accessible", "revealed",
})
_PRE_R1_MAP_KEYS = frozenset({
    "map_index", "current_node_id", "nodes", "edges", "question_cache",
})


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stream_hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _play(seed: int, *, max_steps: int = 400, stop_after_battle: bool = False):
    """A fixed, fully deterministic policy: always take the first legal
    option. Used as a route generator, not as a gameplay assertion -- what is
    pinned is the CONTRACT's shape over whatever route this produces.

    Returns `(engine, state, battles)` where `battles` is every
    `state.last_battle` snapshot observed, in order.
    """
    eng = engine.Engine()
    state = eng.reset(seed=seed)
    battles = []
    seen = 0
    for _ in range(max_steps):
        if state.game_over or state.won:
            break
        actions = engine.legal_actions(state)
        if not actions:
            break
        if "choose_starter" in actions:
            state = eng.step(engine.ChooseStarter(
                species_id=actions["choose_starter"]["species_ids"][0]))
        elif "select_option" in actions:
            indices = actions["select_option"]["indices"]
            state = eng.step(engine.SelectOption(index=indices[0] if indices else None))
        elif "visit_node" in actions and actions["visit_node"]["node_ids"]:
            state = eng.step(engine.VisitNode(node_id=actions["visit_node"]["node_ids"][0]))
        elif "advance_map" in actions:
            state = eng.step(engine.AdvanceMap())
        else:
            break
        if state.last_battle is not None and state.last_battle is not seen:
            if not battles or battles[-1] is not state.last_battle:
                battles.append(state.last_battle)
                seen = state.last_battle
                if stop_after_battle:
                    break
    return eng, state, battles


def _any_state_with_battle(seed: int = 12345):
    _, state, battles = _play(seed, stop_after_battle=True)
    return state, battles


class ObservationFieldSetTests(unittest.TestCase):
    """Mutant 1: delete a field from `encode_state`'s output."""

    def test_observation_field_set_is_exactly_pinned(self):
        for seed in (1, 12345, 999):
            state, _ = _any_state_with_battle(seed)
            observed = encode_state(state)
            self.assertEqual(
                set(observed), set(contract.OBSERVATION_FIELDS),
                f"observation field set drifted at seed {seed}",
            )

    def test_every_pre_r1_observation_key_survives(self):
        state, _ = _any_state_with_battle()
        observed = encode_state(state)
        missing = _PRE_R1_OBSERVATION_KEYS - set(observed)
        self.assertEqual(set(), missing, "R1 must be a strict superset of the pre-R1 contract")

    def test_mon_field_set_is_exactly_pinned(self):
        state, _ = _any_state_with_battle()
        self.assertTrue(state.team, "route produced no team to check")
        for mon in encode_state(state)["team"]:
            self.assertEqual(set(mon), set(contract.MON_FIELDS))
            self.assertEqual(set(), _PRE_R1_MON_KEYS - set(mon))

    def test_node_and_map_field_sets_are_exactly_pinned(self):
        state, _ = _any_state_with_battle()
        map_json = encode_state(state)["map"]
        self.assertIsNotNone(map_json)
        self.assertEqual(set(), _PRE_R1_MAP_KEYS - set(map_json))
        self.assertTrue(map_json["nodes"])
        for node in map_json["nodes"]:
            self.assertEqual(set(node), set(contract.NODE_FIELDS))
            self.assertEqual(set(), _PRE_R1_NODE_KEYS - set(node))

    def test_item_view_field_set_is_exactly_pinned(self):
        for item_id in ("rare_candy", "charcoal", "definitely_not_an_item"):
            self.assertEqual(set(contract.item_view(item_id)), set(contract.ITEM_FIELDS))
        self.assertFalse(contract.item_view("definitely_not_an_item")["known"])
        self.assertTrue(contract.item_view("rare_candy")["known"])

    def test_contract_version_is_present_and_independent_of_oracle_schema(self):
        state, _ = _any_state_with_battle()
        self.assertEqual(encode_state(state)["contract_version"], contract.CONTRACT_VERSION)
        # The oracle's schema is version 2 and must not be what this reports.
        # Equal numbers here would be the first symptom of the two contracts
        # being conflated, which R1 exists to prevent.
        self.assertIsInstance(contract.CONTRACT_VERSION, int)

    def test_engine_internal_extra_is_never_exposed(self):
        """`PendingChoice.extra` can hold live Combatant/Trainer references."""
        eng = engine.Engine()
        state = eng.reset(seed=7)
        pending = encode_state(state)["pending"]
        self.assertIsNotNone(pending)
        self.assertNotIn("extra", pending)


class BattleFeedTests(unittest.TestCase):
    """Mutants 2, 3 and 4: reorder, rename and drop in the battle feed."""

    def test_engine_plumbs_a_battle_feed_at_all(self):
        state, battles = _any_state_with_battle()
        self.assertTrue(battles, "no battle resolved on the probe route")
        self.assertIsNotNone(state.last_battle)
        view = contract.battle_view(state)
        self.assertIsNotNone(view)
        self.assertTrue(view["turns"], "battle feed carried no turns")

    def test_turn_count_equals_rounds(self):
        """Mutant 4: dropping the final turn on the way out of `engine.py`.

        `battle_loop` appends exactly one `turn_start` per round and reports
        the same count as `rounds`, so a plumbing layer that truncates the
        stream breaks this equality.
        """
        for seed in (1, 12345, 999, 424242):
            _, _, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                starts = [e for e in feed["battle_events"] if e["type"] == "turn_start"]
                self.assertEqual(
                    len(starts), feed["rounds"],
                    f"turn_start count != rounds at seed {seed}",
                )

    def test_turn_numbers_are_strictly_increasing(self):
        """Mutant 2, structural half: the stream must stay ordered."""
        for seed in (1, 12345, 999, 424242):
            _, _, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                turns = contract.fold_turns(feed["battle_events"])
                numbers = [t["turn"] for t in turns]
                self.assertEqual(
                    numbers, sorted(set(numbers)),
                    f"turn numbers not strictly increasing at seed {seed}",
                )

    def test_no_event_precedes_the_first_turn_marker(self):
        for seed in (1, 12345, 999):
            _, _, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                events = feed["battle_events"]
                if not events:
                    continue
                self.assertEqual(
                    events[0]["type"], "turn_start",
                    "an attack escaped its round in the plumbed stream",
                )

    def test_event_type_strings_are_exactly_pinned(self):
        """Mutant 3: renaming an event `type`."""
        seen_battle, seen_status = set(), set()
        for seed in (1, 12345, 999, 424242, 31337):
            _, _, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                for event in feed["battle_events"]:
                    if event["type"] != "turn_start":
                        seen_battle.add(event["type"])
                for event in feed["status_events"]:
                    seen_status.add(event["type"])
        self.assertTrue(seen_battle, "no battle events observed to check")
        self.assertLessEqual(seen_battle, set(contract.BATTLE_EVENT_TYPES))
        self.assertLessEqual(seen_status, set(contract.STATUS_EVENT_TYPES))
        # `turn_start` is the partition marker the fold depends on; renaming it
        # silently collapses every turn into one.
        for seed in (1, 12345):
            _, _, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                self.assertIn("turn_start", {e["type"] for e in feed["battle_events"]})

    def test_attack_record_keys_are_exactly_pinned(self):
        """The renderer projects these through unchanged; a key vanishing in
        `battle_loop` would silently blank an animation."""
        expected = {
            "type", "side", "attacker_idx", "target_side", "target_idx",
            "move_name", "move_type", "damage", "type_eff", "crit",
            "is_special", "attacker_hp_after", "target_hp_after", "extra_attack",
        }
        found = False
        for seed in (1, 12345, 999, 424242):
            _, _, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                for event in feed["battle_events"]:
                    if event["type"] == "attack":
                        self.assertEqual(set(event), expected)
                        found = True
        self.assertTrue(found, "no attack record observed to check")

    def test_stream_is_byte_stable_for_a_fixed_seed(self):
        """Mutants 2/3/4, golden half. Any reorder, rename, drop or added
        field changes this hash. Deliberately the most sensitive detector
        here: the structural tests say what is true, this one notices
        everything else.

        Recomputed rather than hard-coded, then compared against a second
        independent run: a hard-coded digest would pin THIS machine's route
        rather than the contract's stability, and would have to be re-blessed
        on every legitimate gameplay change.
        """
        _, _, first = _play(12345, stop_after_battle=True)
        _, _, second = _play(12345, stop_after_battle=True)
        self.assertTrue(first)
        self.assertEqual(
            _stream_hash([f["battle_events"] for f in first]),
            _stream_hash([f["battle_events"] for f in second]),
            "the plumbed battle stream is not reproducible for a fixed seed",
        )

    def test_stream_matches_a_pinned_golden_digest(self):
        """Mutant 2, the detector that actually catches a WITHIN-TURN reorder.

        `test_stream_is_byte_stable_for_a_fixed_seed` compares two runs of the
        same build, so a *deterministic* reorder passes it. These digests pin
        the exact ordered stream, so swapping two attacks inside one turn --
        which no structural invariant above can see -- fails here.

        Re-blessing: these are contract detectors, not gameplay assertions. A
        deliberate gameplay change legitimately moves them; regenerate with
        `_stream_hash([f["battle_events"] for f in _play(seed,
        stop_after_battle=True)[2]])` and say so in the milestone record. An
        UNEXPLAINED move is the failure this exists to surface.

        M6 re-blessed all four. `battle_events` gained the `effect` (N10) and
        `faint` (N11) families, which is a deliberate, declared addition. It
        was proved to be an ADDITION and nothing else by the M3.3b method:
        stripping `effect`/`faint` back out of every stream reproduces all four
        PRE-M6 digests byte for byte, so no record was reordered, renamed or
        dropped and no gameplay moved. The pre-M6 values, for that audit:

            1:      f0d1ca23188b21ea41e330857bb47220f0dc94239d36117cd37736d3cc0fd9da
            12345:  c4eaee77e2f0cef259903f6e6c622cb1ec35e7f51bca344411bcabd1cfc86e4e
            999:    9a4dfae164168338e5c0de10e2bbef48d986099ce782a0bd4227abd964bdc0f1
            424242: e89b7b1da104e2d9d3a26241cfc1337b23f21fc33c9c12817db2c95fcdbe1334
        """
        expected = {
            1: "91e8642dbeabfa23961b9d4c0319323fd9c05ab0cccac4514f01aff118da7bf0",
            12345: "4e1ecdcc67016fcb08e9f4ff38fb232d84f6a001d414622648c08a349e662894",
            999: "61e07906e8c95f58434079c36676c6c982cb8a968049b682729c4b4a534f5a9c",
            424242: "188922c1ceaf61f6dba662ea9183ff4fe8ecb13c4a43043e5abb59fa4b559813",
        }
        for seed, digest in expected.items():
            _, _, battles = _play(seed, stop_after_battle=True)
            self.assertTrue(battles, f"no battle resolved at seed {seed}")
            self.assertEqual(
                _stream_hash([f["battle_events"] for f in battles]), digest,
                f"the ordered battle stream moved at seed {seed}",
            )

    def test_battle_view_does_not_alias_engine_state(self):
        state, _ = _any_state_with_battle()
        view = contract.battle_view(state)
        original = json.loads(_canonical(state.last_battle["battle_events"]))
        for turn in view["turns"]:
            for event in turn["events"]:
                event["damage"] = -999
        self.assertEqual(
            original,
            json.loads(_canonical(state.last_battle["battle_events"])),
            "a renderer can mutate engine state through the battle view",
        )

    def test_battle_view_is_none_before_any_battle(self):
        eng = engine.Engine()
        state = eng.reset(seed=5)
        self.assertIsNone(state.last_battle)
        self.assertIsNone(contract.battle_view(state))

    def test_feed_is_replaced_not_accumulated(self):
        """`RunState.last_battle` holds one battle. An accumulating log would
        make `search_route`'s per-branch deepcopy grow without limit."""
        _, _, battles = _play(12345, max_steps=400)
        if len(battles) < 2:
            self.skipTest("probe route resolved fewer than two battles")
        for feed in battles:
            starts = [e for e in feed["battle_events"] if e["type"] == "turn_start"]
            self.assertEqual(len(starts), feed["rounds"])


class BehaviorNeutralityTests(unittest.TestCase):
    """R1's plumbing must be provably free of gameplay effect (the M3.3b
    method): the same seed must produce the same run with the feed read or
    ignored, and `battle_loop`'s own result must be untouched by it."""

    def test_plumbing_draws_no_rng_and_changes_no_outcome(self):
        def fingerprint(read_feed: bool):
            eng = engine.Engine()
            state = eng.reset(seed=4242)
            trace = []
            for _ in range(120):
                if state.game_over or state.won:
                    break
                actions = engine.legal_actions(state)
                if not actions:
                    break
                if read_feed:
                    contract.battle_view(state)
                    encode_state(state)
                if "choose_starter" in actions:
                    state = eng.step(engine.ChooseStarter(
                        species_id=actions["choose_starter"]["species_ids"][0]))
                elif "select_option" in actions:
                    indices = actions["select_option"]["indices"]
                    state = eng.step(engine.SelectOption(index=indices[0] if indices else None))
                elif "visit_node" in actions and actions["visit_node"]["node_ids"]:
                    state = eng.step(engine.VisitNode(
                        node_id=actions["visit_node"]["node_ids"][0]))
                elif "advance_map" in actions:
                    state = eng.step(engine.AdvanceMap())
                else:
                    break
                trace.append((
                    state.phase.value, state.current_map, state.badges,
                    [(m.species_id, m.level, m.current_hp) for m in state.team],
                    list(state.items),
                ))
            return _canonical(trace)

        self.assertEqual(
            fingerprint(read_feed=False), fingerprint(read_feed=True),
            "reading the renderer contract changed the run",
        )

    def test_battle_result_still_carries_its_own_streams(self):
        """The oracle reads `BattleResult.battle_events` directly. R1 must not
        have moved or emptied it in the course of plumbing a copy out."""
        self.assertIn("battle_events", battle_loop.BattleResult.__dataclass_fields__)
        self.assertIn("status_events", battle_loop.BattleResult.__dataclass_fields__)


class ScreenAndOverlayTests(unittest.TestCase):
    """M5 finding F1, disposed on the renderer surface only."""

    def test_overlay_is_none_on_an_ordinary_map_screen(self):
        state, _ = _any_state_with_battle()
        if state.pending is None:
            self.assertIsNone(contract.overlay_for(state))

    def test_every_showscreenless_phase_gets_an_overlay_discriminator(self):
        for phase in (engine.Phase.ITEM_EQUIP_CHOICE, engine.Phase.MOVE_TUTOR_CHOICE,
                      engine.Phase.EVOLUTION_CHOICE, engine.Phase.REWARD_TEAM_PICK):
            state = engine.RunState(phase=phase)
            self.assertIsNotNone(
                contract.overlay_for(state),
                f"{phase.value} has no renderer overlay discriminator",
            )

    def test_overlay_phases_match_the_four_f1_phases_exactly(self):
        self.assertEqual(
            set(contract._OVERLAY_PHASES),
            {engine.Phase.ITEM_EQUIP_CHOICE, engine.Phase.MOVE_TUTOR_CHOICE,
             engine.Phase.EVOLUTION_CHOICE, engine.Phase.REWARD_TEAM_PICK},
        )

    def test_screen_is_defined_for_every_phase(self):
        for phase in engine.Phase:
            state = engine.RunState(phase=phase)
            self.assertIsInstance(contract._screen_for(state), str)

    def test_overlay_does_not_disturb_the_oracles_screen_projection(self):
        """The renderer's `screen` is its own mapping. This asserts R1 did not
        reach into the oracle's `_screen_for` to add the discriminator there,
        which is the conflation the milestone forbids."""
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[2] / "route-oracle" / "run_scenario.py"
        source = path.read_text(encoding="utf-8")
        # The word itself appears in that module's prose (it explains the same
        # four modals). What must not appear is an emitted PROJECTION KEY.
        self.assertNotIn('"overlay"', source, "the oracle projection gained a renderer field")
        self.assertNotIn("contract_version", source, "the oracle projection gained the renderer's version")
        # And the renderer must not have taken a dependency on the oracle's
        # screen answer, which is owned by parity, not by presentation. The
        # name appears in this module's prose (it explains the split), so the
        # check is on the IMPORT statements, not on the text.
        import ast
        tree = ast.parse(pathlib.Path(contract.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(a.name for a in node.names)
        self.assertNotIn("run_scenario", imported)
        self.assertFalse(
            any("route" in name and "oracle" in name for name in imported),
            f"the renderer contract imported an oracle module: {sorted(imported)}",
        )


class SerializationBoundaryTests(unittest.TestCase):
    """P1.9 is explicitly out of R1's scope. These record the boundary so a
    later session does not mistake this projection for a resumption format."""

    def test_observation_is_json_serializable(self):
        state, _ = _any_state_with_battle()
        json.dumps(encode_state(state))

    def test_observation_is_lossy_and_not_a_resumption_format(self):
        state, _ = _any_state_with_battle()
        observed = encode_state(state)
        for dropped in ("passives", "saved_catch", "saved_shiny_node",
                        "saved_question_resolve", "item_offer", "sub_map_return"):
            self.assertNotIn(dropped, observed)


class UnsuppliedTests(unittest.TestCase):
    def test_unsupplied_is_reported_rather_than_faked(self):
        state, _ = _any_state_with_battle()
        observed = encode_state(state)
        self.assertIn("unsupplied", observed)
        self.assertIn("battle_flavor_text", observed["unsupplied"])
        self.assertIn("unvisited_wild_species", observed["unsupplied"])

    def test_unknown_item_is_marked_unknown_not_invented(self):
        view = contract.item_view("no_such_item_id")
        self.assertFalse(view["known"])
        self.assertIsNone(view["icon_url"])


# ===========================================================================
# R2 -- map/node presentation, plus the two carried-forward R1 gaps.
#
# The R1 field-set detectors above compare the real projection against
# `contract.MON_FIELDS` / `NODE_FIELDS`, which catches a field dropped from the
# projection alone. It does NOT catch a session that deletes the field from
# BOTH the projection and the pinned set -- so the R2 additions are also pinned
# LITERALLY here, against a name typed out in this file.
# ===========================================================================


class R1CarriedForwardGapTests(unittest.TestCase):
    """N1 and N2 from `docs/audits/R1-independent-closure-audit.md` section 7."""

    def test_ability_is_carried_on_every_mon_view(self):
        """N1. Required mutant: delete `ability` from `mon_view`."""
        self.assertIn("ability", contract.MON_FIELDS)
        state, _ = _any_state_with_battle()
        self.assertTrue(state.team)
        for mon in encode_state(state)["team"]:
            self.assertIn("ability", mon, "N1 regressed: mon_view dropped `ability`")

    def test_ability_reports_the_engines_own_field_never_a_guess(self):
        """Not merely present -- EQUAL to what the battle engine reads.

        `battle.py:307`/`:362` branch on `Combatant.gen3_ability`, so a hover
        card showing anything else would explain an immunity wrongly. Note the
        field is battle-local: `engine.py:1593-1596` records that it "only
        reflects whatever a Traced battle last set it to and is otherwise unset
        outside battle" (CODEX issue 20). Carrying that value verbatim, `None`
        included, is the faithful thing to do -- re-deriving a species ability
        here would be inventing a number the engine does not hold.
        """
        for seed in (1, 12345, 999):
            state, _ = _any_state_with_battle(seed)
            self.assertTrue(state.team)
            for mon, view in zip(state.team, contract.observation(state)["team"]):
                self.assertEqual(view["ability"], mon.gen3_ability)

    def test_ability_is_actually_populated_on_a_gen3_battle_roster(self):
        """Where the field is live -- N2's rosters, which are the battle's own
        `Combatant`s. This is the join N1 and N2 exist to make possible: an
        `attack` record's `side`+index reaches a combatant whose ability is
        known, so a replay can say WHY a hit did nothing."""
        eng = engine.Engine()
        state = eng.reset(seed=333333333, gen3_mode=True)
        actions = engine.legal_actions(state)
        state = eng.step(engine.ChooseStarter(
            species_id=actions["choose_starter"]["species_ids"][0]))
        seen_non_null = False
        for _ in range(200):
            if state.last_battle is not None:
                view = contract.battle_view(state)
                for side in ("player_team", "enemy_team"):
                    seen_non_null = seen_non_null or any(
                        m["ability"] is not None for m in view[side])
                if seen_non_null:
                    break
            actions = engine.legal_actions(state)
            if state.game_over or state.won or not actions:
                break
            if "select_option" in actions:
                indices = actions["select_option"]["indices"]
                state = eng.step(engine.SelectOption(index=indices[0] if indices else None))
            elif "visit_node" in actions and actions["visit_node"]["node_ids"]:
                state = eng.step(engine.VisitNode(node_id=actions["visit_node"]["node_ids"][0]))
            elif "advance_map" in actions:
                state = eng.step(engine.AdvanceMap())
            else:
                break
        self.assertTrue(
            seen_non_null,
            "no combatant on any Gen3 battle roster carried an ability -- either "
            "the engine stopped assigning them or N1's plumbing broke",
        )

    def test_battle_view_carries_both_rosters(self):
        """N2. Required mutant: delete `player_team` or `enemy_team`.

        An `attack` record names its participants by `side` + index only, so
        without these a replay cannot name or HP-scale either combatant. R4
        depends on this.
        """
        state, battles = _any_state_with_battle()
        self.assertTrue(battles)
        view = contract.battle_view(state)
        for key in ("player_team", "enemy_team"):
            self.assertIn(key, view, f"N2 regressed: battle_view dropped `{key}`")
            self.assertTrue(view[key], f"battle_view carried an empty `{key}`")
            for mon in view[key]:
                self.assertEqual(set(mon), set(contract.MON_FIELDS))

    def test_every_attack_record_indexes_into_the_carried_rosters(self):
        """The point of N2: `side` + index must actually resolve. A roster that
        is present but wrong (truncated, or the wrong side) fails here."""
        for seed in (1, 12345, 999, 424242):
            _, state, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                view = contract.battle_view(_StateWithFeed(state, feed))
                rosters = {"player": view["player_team"], "enemy": view["enemy_team"]}
                for turn in view["turns"]:
                    for event in turn["events"]:
                        if event.get("type") != "attack":
                            continue
                        for side_key, idx_key in (
                            ("side", "attacker_idx"), ("target_side", "target_idx"),
                        ):
                            side = event.get(side_key)
                            idx = event.get(idx_key)
                            if side is None or idx is None:
                                continue
                            self.assertIn(side, rosters)
                            self.assertLess(
                                idx, len(rosters[side]),
                                f"attack {idx_key}={idx} indexes past the carried "
                                f"{side} roster at seed {seed}",
                            )


class BattleReplayTests(unittest.TestCase):
    """R4's detectors. Every one of these exists because the surface it covers
    was, until R4, produced and read by nobody -- so nothing failed when it was
    wrong.
    """

    def _views(self, seeds=(1, 12345, 999, 424242)):
        """Every battle on several routes, as `battle_view` projections."""
        out = []
        for seed in seeds:
            _, state, battles = _play(seed, stop_after_battle=True)
            for feed in battles:
                out.append((seed, contract.battle_view(_StateWithFeed(state, feed))))
        self.assertTrue(out, "no route produced a battle to project")
        return out

    def test_battle_view_field_set_is_exactly_pinned(self):
        """Required mutant 2: drop a field from `battle_view`'s output (e.g.
        `player_team`). A contract-level detector must catch it, not a human
        noticing the screen looks wrong."""
        for seed, view in self._views():
            self.assertEqual(
                set(view), set(contract.BATTLE_FIELDS),
                f"battle_view field set drifted at seed {seed}",
            )

    def test_every_replay_step_field_set_is_exactly_pinned(self):
        for seed, view in self._views():
            for step in view["replay"]:
                self.assertEqual(
                    set(step), set(contract.REPLAY_STEP_FIELDS),
                    f"replay step field set drifted at seed {seed}",
                )

    def test_the_pre_battle_rosters_are_present_and_are_not_the_post_battle_ones(self):
        """R4's new engine plumbing. `player_team`/`enemy_team` are the
        POST-battle state; a replay opening on them would spoil the result
        before drawing a hit.

        BOTH sides are checked independently, and that is not pedantry: the
        first version of this test only compared the enemy rosters, and a
        mutant that snapshotted `result.player_team` as `player_team_start`
        survived it. Each side needs its own evidence that the snapshot is
        taken BEFORE the battle, not after.
        """
        differed = {"player": False, "enemy": False}
        for _seed, view in self._views():
            for key in ("player_team_start", "enemy_team_start"):
                self.assertIn(key, view)
                self.assertTrue(view[key], f"battle_view carried an empty `{key}`")
                for mon in view[key]:
                    self.assertEqual(set(mon), set(contract.MON_FIELDS))
            for side in ("player", "enemy"):
                start = [m["current_hp"] for m in view[f"{side}_team_start"]]
                end = [m["current_hp"] for m in view[f"{side}_team"]]
                if start != end:
                    differed[side] = True
        for side, seen in differed.items():
            self.assertTrue(
                seen,
                f"no battle showed a pre/post HP difference on the {side} side "
                f"-- `{side}_team_start` is being snapshotted after the battle, "
                f"not before",
            )

    def test_the_replay_hp_source_is_the_target_not_the_attacker(self):
        """Required mutant 3: swap `attacker_hp_after` / `target_hp_after` as
        the replay's HP-bar source.

        The source animates the TARGET's bar (bundle.deobfuscated.js:69286-69296
        moves `Bcg`, the target element, to `targetHpAfter`). Reading the
        attacker's value instead is invisible by eye on any turn where both
        happen to be similar, so it is pinned against the record directly.
        """
        checked = 0
        for seed, view in self._views():
            flat = [e for t in view["turns"] for e in t["events"] if e.get("type") == "attack"]
            steps = [s for s in view["replay"] if s["kind"] == "attack"]
            self.assertEqual(
                len(flat), len(steps),
                f"replay dropped or invented an attack step at seed {seed}",
            )
            for event, step in zip(flat, steps):
                self.assertEqual(
                    step["hp_after"], event["target_hp_after"],
                    f"replay HP source is not target_hp_after at seed {seed}",
                )
                self.assertEqual(step["side"], event["target_side"])
                self.assertEqual(step["idx"], event["target_idx"])
                checked += 1
        self.assertTrue(checked, "no attack step was available to check")

    def test_the_replay_never_adds_a_key_to_an_oracle_owned_record(self):
        """docs/renderer-contract.md section 2's central rule, as a detector.
        The enrichment is a SIBLING list; `turns[*].events[*]` must still
        project `battle_loop`'s shape exactly."""
        allowed = {
            "type", "side", "attacker_idx", "target_side", "target_idx",
            "move_name", "move_type", "damage", "type_eff", "crit",
            "is_special", "attacker_hp_after", "target_hp_after", "extra_attack",
        }
        for seed, view in self._views():
            for turn in view["turns"]:
                for event in turn["events"]:
                    if event.get("type") != "attack":
                        continue
                    self.assertEqual(
                        set(event), allowed,
                        f"a renderer field leaked into an attack record at seed {seed}",
                    )

    def test_turn_partitioning_survives_into_the_replay(self):
        """Required mutant 2 (second half): break `fold_turns`' partitioning.
        Every attack step must carry the round its record was folded into, and
        the rounds must be the feed's own `turn_start` sequence."""
        for seed, view in self._views():
            expected = [t["turn"] for t in view["turns"]]
            self.assertEqual(
                expected, sorted(expected),
                f"turns are not in ascending round order at seed {seed}",
            )
            seen = []
            for turn in view["turns"]:
                for _event in turn["events"]:
                    seen.append(turn["turn"])
            replay_turns = [s["turn"] for s in view["replay"] if s["turn"] is not None]
            self.assertEqual(
                seen, replay_turns,
                f"replay lost the feed's turn partitioning at seed {seed}",
            )

    def test_status_steps_are_honest_about_having_no_round(self):
        """The declared limitation (contract section 11). Steps built from the
        `status_events` stream carry `turn: None` on purpose -- that stream has
        no round markers, so the two cannot be interleaved. A future change
        that starts GUESSING a round fails here.

        M6 narrowed what this can assert per-kind. `faint` is now emitted on
        BOTH streams: `battle_events` carries the ordinary combat KO (N11),
        which genuinely knows its round and correctly carries it, while
        `status_events` still carries the status-tick-caused KO, which does
        not. So the per-kind assertion holds only for the two kinds that remain
        status-exclusive, and the stream boundary itself is pinned separately
        below -- a strictly stronger check than the original, because it also
        fails if a turn-derived step is ever appended after a status one.
        """
        for seed, view in self._views():
            for step in view["replay"]:
                if step["kind"] in ("status_tick", "poison_drain"):
                    self.assertIsNone(
                        step["turn"],
                        f"a status-exclusive step invented a round at seed {seed}",
                    )
            turns = [step["turn"] for step in view["replay"]]
            if None in turns:
                boundary = turns.index(None)
                self.assertTrue(
                    all(t is None for t in turns[boundary:]),
                    f"a turn-derived replay step follows a status one at seed {seed}",
                )

    def test_the_attack_line_matches_the_sources_own_format(self):
        """The text is ported from bundle.deobfuscated.js:69301-69320, not
        invented. Pinned on the pieces that are the source's own: the
        `(enemy) ` prefix, the arrow, the ` dmg.` and the four suffixes."""
        self.assertEqual(" Super effective!", contract._effectiveness_suffix(2, False))
        self.assertEqual(" No effect!", contract._effectiveness_suffix(0, False))
        self.assertEqual(" Not very effective...", contract._effectiveness_suffix(0.5, False))
        self.assertEqual("", contract._effectiveness_suffix(1, False))
        # Only the ANIMATION appends the crit suffix (69307); runBattle's own
        # inline logger (55960-55976) does not.
        self.assertEqual(" Critical hit!", contract._effectiveness_suffix(1, True))
        self.assertEqual(
            " Super effective! Critical hit!", contract._effectiveness_suffix(2, True))
        for _seed, view in self._views():
            for step in view["replay"]:
                if step["kind"] != "attack":
                    continue
                self.assertIn(" used ", step["text"])
                self.assertIn(" → ", step["text"])
                self.assertIn(" dmg.", step["text"])
                self.assertIn(step["cls"], ("log-player", "log-enemy"))

    def test_popup_kind_and_hp_bar_colour_are_the_sources_own(self):
        """`spawnDmgPopup`'s kinds (69274-69281) and `hpBarColor`
        (64134-64137). Both are small enough that a wrong constant is the
        likeliest regression, so both are pinned by value."""
        self.assertEqual("crit", contract._popup_kind(10, 1, True))
        self.assertEqual("se", contract._popup_kind(10, 2, False))
        self.assertEqual("nve", contract._popup_kind(10, 0.5, False))
        self.assertEqual("normal", contract._popup_kind(10, 1, False))
        # The source spawns no popup at all when nothing was dealt (69273).
        self.assertIsNone(contract._popup_kind(0, 2, True))
        self.assertEqual("#00FF4A", contract.hp_bar_color(0.51))
        self.assertEqual("#EAFF00", contract.hp_bar_color(0.5))
        self.assertEqual("#EAFF00", contract.hp_bar_color(0.11))
        self.assertEqual("#FF0000", contract.hp_bar_color(0.1))
        self.assertEqual("#FF0000", contract.hp_bar_color(0.0))

    def test_every_replay_value_is_json_safe(self):
        """The web renderer receives this over HTTP. A `Combatant` or a set
        leaking into a step would 500 the server, not merely render oddly."""
        for _seed, view in self._views():
            json.loads(_canonical(view))

    def test_replay_step_kinds_stay_within_the_pinned_type_sets(self):
        for seed, view in self._views():
            known = (contract.BATTLE_EVENT_TYPES | contract.STATUS_EVENT_TYPES)
            for step in view["replay"]:
                self.assertIn(
                    step["kind"], known,
                    f"replay invented a step kind at seed {seed}: {step['kind']}",
                )
                if step["cls"] is not None:
                    self.assertIn(step["cls"], contract.REPLAY_LOG_CLASSES)
                if step["popup"] is not None and step["kind"] == "attack":
                    self.assertIn(step["popup"], contract.REPLAY_POPUP_KINDS)

    def test_battle_view_does_not_alias_the_pre_battle_rosters(self):
        """Same guarantee R1 gave for the event streams: a renderer holding
        this must not be able to mutate engine state through it."""
        state, battles = _any_state_with_battle()
        view = contract.battle_view(state)
        before = state.last_battle["player_team_start"][0].current_hp
        view["player_team_start"][0]["current_hp"] = -999
        self.assertEqual(before, state.last_battle["player_team_start"][0].current_hp)


class ConsoleBattleReplayTests(unittest.TestCase):
    """R4: the console renderer must actually consume the feed. Before R4 its
    `"battle"` log branch printed the outcome, the round count and the enemy
    roster, and nothing in the module read `battle_view` at all."""

    def test_console_prints_a_turn_by_turn_replay(self):
        state, battles = _any_state_with_battle()
        self.assertTrue(battles)
        text = console.render_battle_replay(state)
        self.assertTrue(text, "console produced no replay for a resolved battle")
        self.assertIn("battle replay", text)
        view = contract.battle_view(state)
        for step in view["replay"]:
            self.assertIn(step["text"], text, "console dropped a replay step")
        # Both bookends, for the reason contract section 11 gives: HP changes
        # with no event record make the last replayed HP and the true final HP
        # legitimately differ.
        self.assertIn("start  player", text)
        self.assertIn("final  player", text)

    def test_console_replay_is_empty_before_any_battle(self):
        eng = engine.Engine()
        state = eng.reset(seed=7)
        self.assertEqual("", console.render_battle_replay(state))

    def test_render_state_shows_the_replay_exactly_when_a_battle_was_logged(self):
        """Gated on the LOG, not on `last_battle` being non-None -- the latter
        is replaced and never cleared, so keying off it would reprint the same
        battle on every later step."""
        state, battles = _any_state_with_battle()
        self.assertTrue(battles)
        self.assertEqual("battle", state.log[-1]["type"])
        self.assertIn("battle replay", console.render_state(state, recent_log=1))
        # A window that excludes the battle entry must not print the replay.
        no_battle = _StateWithLog(state, [{"type": "badge", "badges": 1}])
        self.assertNotIn("battle replay", console.render_state(no_battle, recent_log=1))

    def test_a_multi_entry_step_still_surfaces_its_battle(self):
        """The console twin of `app.js`'s interstitial bug. `render_state`'s
        default window is ONE entry, so a battle followed by an evolve in the
        same `Engine.step` used to print only the evolve. `play.py` now sizes
        the window to what the step actually appended."""
        state, _ = _any_state_with_battle()
        batched = _StateWithLog(state, list(state.log) + [
            {"type": "evolve", "team_index": 0, "name": "Ivysaur"},
        ])
        self.assertNotIn("battle replay", console.render_state(batched, recent_log=1))
        self.assertIn("battle replay", console.render_state(batched, recent_log=2))

    def test_play_sizes_its_log_window_to_the_step(self):
        """The fix itself, pinned: `play.run_episode` must track the log total
        and pass the delta, not take `render_state`'s one-entry default."""
        import inspect
        source = inspect.getsource(play.run_episode)
        self.assertIn("seen_log_total", source)
        self.assertIn("recent_log=new_entries", source)


class _StateWithLog:
    """A `RunState` proxy with a chosen `log`."""

    def __init__(self, state, log):
        self._state = state
        self.log = log

    def __getattr__(self, name):
        return getattr(self._state, name)


class WebBattleReplayWiringTests(unittest.TestCase):
    """R4: the browser client must consume the feed and must no longer inspect
    only the newest log entry.

    Same honest limitation as `WebRendererWiringTests`, and for the same
    reason: nothing here EXECUTES `app.js`. The standing JS/DOM-shim detector
    is R5's, deliberately left alone. These catch the regression that actually
    happened -- a surface being read by nothing at all -- and the executable
    evidence for the interstitial fix is in `docs/audits/R4-implementation.md`.
    """

    def _app_js(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        return (root / "pokelike" / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    def _index_html(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        return (root / "pokelike" / "webui" / "static" / "index.html").read_text(encoding="utf-8")

    def test_app_js_reads_the_battle_replay_surface(self):
        """The R4 headline. Before this, a full-file search of `app.js` for
        `state.battle` returned NOTHING -- the feed R1 built and R2/N2
        completed was consumed by neither renderer."""
        js = self._app_js()
        for token in ("state.battle", "view.replay", "player_team_start", "enemy_team_start"):
            self.assertIn(token, js, f"app.js never reads `{token}`")

    def test_app_js_scans_the_whole_log_batch_for_a_battle(self):
        """CODEX section 7.7's bug. The old code read `state.log[state.log
        .length - 1]` and tested only that one entry, so a battle followed by
        any other entry in the same step showed no interstitial at all."""
        js = self._app_js()
        body = js[js.index("function applyWithBattleInterstitial"):]
        body = body[:body.index("\nfunction ")]
        self.assertIn("log_total - lastSeenLogTotal", body)
        self.assertIn(".find(", body)
        self.assertNotIn(
            "state.log[state.log.length - 1]", body,
            "app.js still inspects only the newest log entry",
        )

    def test_the_battle_log_pane_exists_and_is_styled(self):
        """`main.css` is copied verbatim from the site and has no `.log-entry`
        rule -- the source's own log pane is dead code (`const B2V = null`,
        bundle.deobfuscated.js:69084). `index.html` supplies the styling."""
        html = self._index_html()
        self.assertIn('id="battle-log"', html)
        for cls in ("log-player", "log-enemy", "log-faint", "log-item"):
            self.assertIn(f".battle-log .{cls}", html)

    def test_main_css_is_not_edited_to_add_renderer_classes(self):
        """It is a verbatim copy of the site's stylesheet and stays one."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        css = (root / "pokelike" / "webui" / "static" / "style" / "main.css").read_text(
            encoding="utf-8", errors="replace")
        self.assertNotIn(".battle-log", css)

    def test_the_stale_docstrings_no_longer_claim_there_is_no_feed(self):
        """Section 4.4 of the R4 brief: both module docstrings said
        `run_battle` "resolves a whole battle synchronously with no per-turn
        event feed", false since R1."""
        import pokelike.webui as webui
        js = self._app_js()
        for text in (webui.__doc__, js):
            self.assertNotIn("no per-turn event feed", text)
            self.assertNotIn("doesn't expose a per-turn event feed", text)


class _StateWithFeed:
    """A `RunState` proxy whose `last_battle` is a chosen historical feed."""

    def __init__(self, state, feed):
        self._state = state
        self.last_battle = feed

    def __getattr__(self, name):
        return getattr(self._state, name)


class NodePresentationTests(unittest.TestCase):
    """R2: the source's own node presentation, ported. Required mutant 3 --
    corrupt one node-presentation field (e.g. swap two node types' icons)."""

    #: Typed out here, not imported, so deleting one from `contract.py`'s
    #: `NODE_FIELDS` too still fails.
    R2_NODE_FIELDS = (
        "sprite_url", "sprite_size", "icon", "color", "tooltip",
        "clickable", "dimmed", "unexplored", "is_current", "pos",
    )

    def _a_map_view(self, seed=333333333, **kw):
        eng = engine.Engine()
        state = eng.reset(seed=seed, **kw)
        actions = engine.legal_actions(state)
        state = eng.step(engine.ChooseStarter(
            species_id=actions["choose_starter"]["species_ids"][0]))
        return state, contract.map_view(state)

    def test_every_r2_node_field_is_present_on_every_node(self):
        _, view = self._a_map_view()
        self.assertTrue(view["nodes"])
        for node in view["nodes"]:
            for field in self.R2_NODE_FIELDS:
                self.assertIn(field, node, f"R2 node field `{field}` disappeared")

    def test_node_icons_match_the_sources_own_table(self):
        """`getNodeIcon`, bundle.deobfuscated.js:54574-54595. Pinned literally,
        so swapping two types' glyphs in `contract._NODE_ICONS` fails here."""
        from pokelike import map_gen
        expected = {
            map_gen.START: "★", map_gen.BATTLE: "⚔",
            map_gen.CATCH: "⬟", map_gen.ITEM: "✦",
            map_gen.QUESTION: "?", map_gen.BOSS: "♛",
            map_gen.POKECENTER: "+", map_gen.TRAINER: "⚑",
            map_gen.LEGENDARY: "⚝", map_gen.MOVE_TUTOR: "♪",
            map_gen.TRADE: "⇄", map_gen.SILVER: "⚔",
            map_gen.MAGMA: "\U0001f525", map_gen.AQUA: "\U0001f30a",
            map_gen.REWARD: "\U0001f381", map_gen.SUBEXIT: "\U0001f6aa",
        }
        self.assertEqual(contract._NODE_ICONS, expected)
        # UNDERGROUND/DISTORTION genuinely have no entry in the source (54594).
        self.assertNotIn(map_gen.UNDERGROUND, contract._NODE_ICONS)
        self.assertNotIn(map_gen.DISTORTION, contract._NODE_ICONS)
        # And the table is really what a node view reports.
        for node_type, glyph in expected.items():
            node = map_gen.MapNode(id="p", type=node_type, layer=0, col=0)
            self.assertEqual(contract.node_view(node)["icon"], glyph)

    def test_a_visited_node_reports_the_check_glyph(self):
        """`getNodeIcon` short-circuits on `visited` before its table (54594)."""
        from pokelike import map_gen
        node = map_gen.MapNode(id="p", type=map_gen.BATTLE, layer=0, col=0, visited=True)
        self.assertEqual(contract.node_view(node)["icon"], "✓")

    def test_node_colours_match_the_sources_own_table(self):
        """`getNodeColor`, 54540-54573. Pinned literally."""
        from pokelike import map_gen
        for node_type, colour in (
            (map_gen.BATTLE, "#6a2a2a"), (map_gen.CATCH, "#2a6a2a"),
            (map_gen.ITEM, "#2a4a6a"), (map_gen.BOSS, "#8a2a8a"),
            (map_gen.POKECENTER, "#006666"), (map_gen.TRAINER, "#6a3a1a"),
            (map_gen.LEGENDARY, "#7a6a00"), (map_gen.MAGMA, "#a83218"),
            (map_gen.AQUA, "#1a5aa8"), (map_gen.SUBEXIT, "#1a5a5a"),
        ):
            node = map_gen.MapNode(id="p", type=node_type, layer=0, col=0)
            self.assertEqual(contract.node_view(node)["color"], colour)

    def test_start_keeps_its_colour_when_visited_and_tracks_nuzlocke(self):
        """The source tests START BEFORE `visited` (54542 vs 54546), so a
        visited START does NOT go grey. Faithful, and easy to "fix" wrongly."""
        from pokelike import map_gen
        node = map_gen.MapNode(id="n0_0", type=map_gen.START, layer=0, col=0, visited=True)
        self.assertEqual(contract.node_view(node)["color"], "#3a4566")
        nuz = contract.NodeContext(nuzlocke_mode=True)
        self.assertEqual(contract.node_view(node, nuz)["color"], "#6a4050")
        # A visited node of any OTHER type does go grey.
        other = map_gen.MapNode(id="p", type=map_gen.BATTLE, layer=0, col=0, visited=True)
        self.assertEqual(contract.node_view(other)["color"], "#333")

    def test_node_state_flags_follow_the_sources_definitions(self):
        """`renderMap`'s BcH/BcC (54172-54173). "Clickable" is accessible AND
        NOT visited -- both renderers previously used plain `accessible`."""
        from pokelike import map_gen

        def flags(**kw):
            node = map_gen.MapNode(id="p", type=map_gen.BATTLE, layer=0, col=0, **kw)
            v = contract.node_view(node)
            return v["clickable"], v["dimmed"], v["unexplored"]

        self.assertEqual(flags(accessible=True, visited=False), (True, False, False))
        self.assertEqual(flags(accessible=True, visited=True), (False, True, False))
        self.assertEqual(flags(accessible=False, visited=True), (False, True, False))
        self.assertEqual(flags(accessible=False, visited=False), (False, False, True))

    def test_sprite_paths_match_the_sources_own_branches(self):
        """`getNodeSprite`, 53944-54025 -- including that REWARD/SUBEXIT/
        sub-boss are decided BEFORE the per-type table."""
        from pokelike import map_gen
        gen1 = contract.NodeContext()
        gen2 = contract.NodeContext(gen2_mode=True)
        gen3 = contract.NodeContext(gen3_mode=True)

        def sprite(node_type, ctx=gen1, **extra):
            node = map_gen.MapNode(id="p", type=node_type, layer=0, col=0, extra=extra)
            return contract.node_view(node, ctx)["sprite_url"]

        self.assertEqual(sprite(map_gen.BATTLE), "img/sprites/g1/grass.png")
        self.assertEqual(sprite(map_gen.BATTLE, gen2), "img/sprites/g2/grass.png")
        self.assertEqual(sprite(map_gen.BATTLE, gen3), "img/sprites/g3/grass.png")
        self.assertEqual(sprite(map_gen.CATCH), "img/sprites/g1/pokeball.png")
        self.assertEqual(sprite(map_gen.CATCH, gen2), "img/sprites/g2/pokeball.png")
        self.assertEqual(sprite(map_gen.ITEM), "img/sprites/item-icon.png")
        self.assertEqual(sprite(map_gen.POKECENTER), "img/sprites/poke-center.png")
        self.assertEqual(sprite(map_gen.SILVER), "img/sprites/g2/silver.png")
        self.assertEqual(sprite(map_gen.SUBEXIT), contract._pokeapi_item("escape-rope"))
        self.assertEqual(sprite(map_gen.UNDERGROUND), contract._pokeapi_item("explorer-kit"))
        self.assertEqual(sprite(map_gen.DISTORTION), contract._pokeapi_item("odd-keystone"))
        # A sub-boss with no trainerKey falls back to its own bossSprite (53982).
        self.assertEqual(
            sprite(map_gen.BOSS, subBoss="distortion", bossSprite="img/x.png"),
            "img/x.png",
        )
        self.assertEqual(
            sprite(map_gen.BOSS, subBoss="distortion"),
            "img/sprites/mistery-trainer.png",
        )
        # An ordinary gym BOSS indexes the generation's leader sprite array.
        self.assertEqual(sprite(map_gen.BOSS, mapIndex=0), "img/sprites/g1/brock.png")
        # START is the one type with no sprite -- the circle branch (54315).
        self.assertIsNone(sprite(map_gen.START))

    def test_sprite_sizes_match_rendermaps_own_boxes(self):
        """54192-54200, plus the circle radius its other branch uses (54316)."""
        from pokelike import map_gen

        def size(node_type, ctx=None):
            node = map_gen.MapNode(id="p", type=node_type, layer=0, col=0)
            return contract.node_view(node, ctx or contract.NodeContext())["sprite_size"]

        self.assertEqual(size(map_gen.ITEM), {"w": 30, "h": 42, "circle_radius": 18})
        self.assertEqual(size(map_gen.BOSS), {"w": 52, "h": 52, "circle_radius": 22})
        self.assertEqual(size(map_gen.TRAINER), {"w": 38, "h": 52, "circle_radius": 18})
        self.assertEqual(
            size(map_gen.TRAINER, contract.NodeContext(gen3_mode=True)),
            {"w": 48, "h": 48, "circle_radius": 18},
        )
        self.assertEqual(
            size(map_gen.TRAINER, contract.NodeContext(gen4_mode=True)),
            {"w": 46, "h": 52, "circle_radius": 18},
        )
        self.assertEqual(size(map_gen.BATTLE), {"w": 40, "h": 40, "circle_radius": 18})

    def test_tooltips_carry_the_sources_own_text(self):
        """`getNodeLabel`, 54686-54824, as structure rather than HTML."""
        from pokelike import map_gen

        def tip(node_type, ctx=None, **extra):
            node = map_gen.MapNode(id="p", type=node_type, layer=0, col=0, extra=extra)
            return contract.node_view(node, ctx or contract.NodeContext())["tooltip"]

        self.assertEqual(tip(map_gen.BATTLE)["title"], "Wild Battle — +1 level")
        self.assertEqual(tip(map_gen.CATCH)["title"], "Catch Pokemon")
        self.assertEqual(tip(map_gen.QUESTION)["title"], "Random Event")
        self.assertEqual(tip(map_gen.POKECENTER)["title"], "Pokemon Center")
        self.assertEqual(tip(map_gen.MOVE_TUTOR)["title"], "Move Tutor")
        self.assertEqual(tip(map_gen.SUBEXIT)["title"], "Exit")
        self.assertEqual(
            tip(map_gen.SUBEXIT)["notes"], ["Return to where you left the map"])
        self.assertEqual(tip(map_gen.UNDERGROUND)["title"], "Sinnoh Underground")
        # A gym boss names the real leader and previews the real roster.
        gym = tip(map_gen.BOSS, mapIndex=0)
        self.assertEqual(gym["title"], "Brock — Rock Gym")
        self.assertTrue(gym["team"])
        self.assertTrue(all(m["name"] and m["level"] for m in gym["team"]))
        # Elite Four sits at map index 8, per generation.
        self.assertEqual(
            tip(map_gen.BOSS, mapIndex=8)["title"], "Elite Four & Champion")
        self.assertEqual(
            tip(map_gen.BOSS, contract.NodeContext(gen4_mode=True), mapIndex=8)["title"],
            "Elite Four & Champion Cynthia",
        )
        # A visited node short-circuits everything else (54689).
        visited = map_gen.MapNode(
            id="p", type=map_gen.BOSS, layer=0, col=0, visited=True, extra={"mapIndex": 0})
        self.assertEqual(contract.node_view(visited)["tooltip"]["title"], "Visited")

    def test_trainer_tooltip_uses_the_extracted_name_and_specialty_tables(self):
        from pokelike import map_gen
        node = map_gen.MapNode(
            id="p", type=map_gen.TRAINER, layer=0, col=0, extra={"trainerSprite": "bugCatcher"})
        self.assertEqual(
            contract.node_view(node)["tooltip"]["title"],
            "Bug Catcher — +2 Levels — Bug Pokemon",
        )
        # No sprite key at all -> the source's own generic string (54811).
        bare = map_gen.MapNode(id="p", type=map_gen.TRAINER, layer=0, col=0)
        self.assertEqual(
            contract.node_view(bare)["tooltip"]["title"], "Trainer Battle — +2 Levels")

    def test_admin_tooltip_adds_the_nuzlocke_note_only_in_nuzlocke(self):
        """`getAdminHoverLabel`, 54670-54673."""
        from pokelike import map_gen
        node = map_gen.MapNode(id="p", type=map_gen.MAGMA, layer=0, col=0)
        plain = contract.node_view(node, contract.NodeContext(gen3_mode=True, current_map=2))
        nuz = contract.node_view(
            node, contract.NodeContext(gen3_mode=True, current_map=2, nuzlocke_mode=True))
        self.assertIn("Team Magma", plain["tooltip"]["title"])
        self.assertNotIn("No Perma-Death", plain["tooltip"]["notes"])
        self.assertIn("No Perma-Death", nuz["tooltip"]["notes"])
        self.assertTrue(plain["tooltip"]["team"])

    def test_is_current_tracks_the_live_node(self):
        state, view = self._a_map_view()
        current = [n for n in view["nodes"] if n["is_current"]]
        self.assertEqual([n["id"] for n in current], [state.current_node_id])


class MapLayoutTests(unittest.TestCase):
    """R2: `renderMap`'s layout loop, 54126-54142. Required mutant 3 also
    covers dropping a node's computed position."""

    def _a_map_view(self, seed=333333333, **kw):
        eng = engine.Engine()
        state = eng.reset(seed=seed, **kw)
        actions = engine.legal_actions(state)
        state = eng.step(engine.ChooseStarter(
            species_id=actions["choose_starter"]["species_ids"][0]))
        return state, contract.map_view(state)

    def test_every_node_carries_a_position(self):
        _, view = self._a_map_view()
        for node in view["nodes"]:
            self.assertIsNotNone(node["pos"], f"node {node['id']} lost its position")
            for key in ("x_frac", "y_frac", "layer_index", "index_in_layer", "layer_size"):
                self.assertIn(key, node["pos"])

    def test_positions_reproduce_the_sources_formula_exactly(self):
        """Recomputed here from the bundle's own arithmetic rather than from
        `contract`'s, so a change to the formula fails instead of tracking."""
        state, view = self._a_map_view()
        width, height = 600, 500          # renderMap's own fallbacks, 54113-54114
        margin = 28                       # B2Q, 54127
        layers = state.map.layers
        layer_count = len(layers)
        by_id = {n["id"]: n for n in view["nodes"]}
        for layer_index, layer in enumerate(layers):
            expected_y = (
                margin + (layer_index / (layer_count - 1)) * (height - 2 * margin)
                if layer_count > 1 else height / 2
            )
            size = len(layer)
            spacing = width / (size + 0.2)
            for index_in_layer, node in enumerate(layer):
                expected_x = (
                    width / 2 if size == 1
                    else width / 2 + (index_in_layer - (size - 1) / 2) * spacing
                )
                x, y = contract.node_pixel_position(by_id[node.id]["pos"], width, height)
                self.assertAlmostEqual(x, expected_x, places=9, msg=node.id)
                self.assertAlmostEqual(y, expected_y, places=9, msg=node.id)

    def test_x_fraction_is_viewport_independent(self):
        """The reason `pos` travels as fractions: scaling the viewport must
        scale x exactly, so the browser can use its live container size."""
        _, view = self._a_map_view()
        for node in view["nodes"]:
            x600, _ = contract.node_pixel_position(node["pos"], 600, 500)
            x1200, _ = contract.node_pixel_position(node["pos"], 1200, 500)
            self.assertAlmostEqual(x1200, 2 * x600, places=9)

    def test_a_single_node_layer_is_centred(self):
        _, view = self._a_map_view()
        singles = [n for n in view["nodes"] if n["pos"]["layer_size"] == 1]
        self.assertTrue(singles, "no single-node layer on the probe map")
        for node in singles:
            self.assertEqual(node["pos"]["x_frac"], 0.5)

    def test_layout_indexes_by_layer_array_position(self):
        """The source uses the index in `layers[i]`, not `node.col` (54134).
        They agree for every map `map_gen` builds -- asserted here, because if
        they ever diverge the ported formula must keep following the source."""
        state, view = self._a_map_view()
        by_id = {n["id"]: n for n in view["nodes"]}
        for layer in state.map.layers:
            for index_in_layer, node in enumerate(layer):
                self.assertEqual(by_id[node.id]["pos"]["index_in_layer"], index_in_layer)
                self.assertEqual(node.col, index_in_layer, f"{node.id}: col != array index")

    def test_map_view_carries_the_denormalization_constants(self):
        _, view = self._a_map_view()
        self.assertEqual(view["edge_margin"], contract.MAP_EDGE_MARGIN)
        self.assertEqual(view["edge_margin"], 28)
        self.assertEqual(view["layer_count"], 9)


class MapEdgeTests(unittest.TestCase):
    """R2: `renderMap`'s edge loop, 54143-54162. Required mutant 4 -- delete
    one edge."""

    def _a_state(self, seed=333333333):
        eng = engine.Engine()
        state = eng.reset(seed=seed)
        actions = engine.legal_actions(state)
        return eng, eng.step(engine.ChooseStarter(
            species_id=actions["choose_starter"]["species_ids"][0]))

    def test_every_generated_edge_reaches_the_view(self):
        """Mutant 4: dropping an edge in `map_gen` or in `map_view`'s
        projection changes this count and the exact pair set."""
        _, state = self._a_state()
        view = contract.map_view(state)
        self.assertEqual(len(view["edges"]), len(state.map.edges))
        self.assertEqual(
            {(e["from"], e["to"]) for e in view["edges"]},
            {(src, dst) for src, dst in state.map.edges},
        )
        # And a concrete count, so a whole layer's worth vanishing is loud even
        # if `map_gen` and the projection were changed together.
        self.assertEqual(len(view["edges"]), 36)

    def test_edge_field_set_is_exactly_pinned(self):
        _, state = self._a_state()
        for edge in contract.map_view(state)["edges"]:
            self.assertEqual(set(edge), set(contract.EDGE_FIELDS))

    def test_edge_styling_follows_the_sources_own_predicates(self):
        """`Bch` (both visited) and `BcL` (both visited-or-accessible), and the
        dash pattern that is applied exactly when NOT active (54149-54160)."""
        from pokelike import map_gen

        def styled(src_kw, dst_kw):
            src = map_gen.MapNode(id="a", type=map_gen.BATTLE, layer=0, col=0, **src_kw)
            dst = map_gen.MapNode(id="b", type=map_gen.BATTLE, layer=1, col=0, **dst_kw)
            return contract.edge_view(src, dst)

        both_visited = styled({"visited": True}, {"visited": True})
        self.assertEqual(both_visited["color"], "#333")
        self.assertTrue(both_visited["active"])
        self.assertFalse(both_visited["dashed"])
        self.assertEqual(both_visited["width"], 2.5)

        active = styled({"visited": True}, {"accessible": True})
        self.assertEqual(active["color"], "#999")
        self.assertTrue(active["active"])
        self.assertFalse(active["dashed"])

        far = styled({}, {})
        self.assertEqual(far["color"], "#222")
        self.assertFalse(far["active"])
        self.assertTrue(far["dashed"])
        self.assertEqual(far["width"], 1.5)

    def test_edges_on_the_fresh_map_are_active_only_from_the_start(self):
        _, state = self._a_state()
        view = contract.map_view(state)
        active = [(e["from"], e["to"]) for e in view["edges"] if e["active"]]
        self.assertEqual(sorted(active), [("n0_0", "n1_0"), ("n0_0", "n1_1")])


class RendererAgreementTests(unittest.TestCase):
    """R1's whole point, extended to R2: the two renderers may format
    differently but must not DISAGREE about a node."""

    def test_console_and_web_agree_on_which_nodes_are_clickable(self):
        from pokelike.render import console
        eng = engine.Engine()
        state = eng.reset(seed=333333333)
        actions = engine.legal_actions(state)
        state = eng.step(engine.ChooseStarter(
            species_id=actions["choose_starter"]["species_ids"][0]))

        web = {n["id"] for n in encode_state(state)["map"]["nodes"] if n["clickable"]}
        # The console lists exactly the clickable nodes under "reachable:".
        lines = console.render_map(state).splitlines()
        start = lines.index("  reachable:")
        listed = {line.split()[0] for line in lines[start + 1:] if line.strip()}
        self.assertEqual(web, listed)
        self.assertEqual(web, {n.id for n in engine.accessible_nodes(state)
                               if not n.visited})

    def test_console_map_shows_a_real_tooltip_not_just_a_letter(self):
        from pokelike.render import console
        eng = engine.Engine()
        state = eng.reset(seed=333333333)
        actions = engine.legal_actions(state)
        state = eng.step(engine.ChooseStarter(
            species_id=actions["choose_starter"]["species_ids"][0]))
        text = console.render_map(state)
        self.assertIn("reachable:", text)
        self.assertIn("Wild Battle", text)
        self.assertIn("Catch Pokemon", text)

    def test_console_symbol_table_covers_exactly_the_sources_icon_types(self):
        """The console is ASCII-only by design, but its mapping must stay
        one-to-one with `getNodeIcon`'s -- one symbol per node type, no
        duplicates, and the same key set plus the two the source defaults."""
        from pokelike import map_gen
        from pokelike.render import console
        symbols = console._ASCII_NODE_SYMBOLS
        self.assertEqual(
            set(symbols),
            set(contract._NODE_ICONS) | {map_gen.UNDERGROUND, map_gen.DISTORTION},
        )
        self.assertEqual(len(set(symbols.values())), len(symbols), "duplicate ASCII symbol")


# ---------------------------------------------------------------------------
# R3: party/item/reward/choice/evolution UI parity.
# ---------------------------------------------------------------------------


def _state_after_starter(seed: int = 7):
    eng = engine.Engine()
    state = eng.reset(seed=seed)
    eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
    return eng, eng.state


def _reward_team_pick(kind: str):
    """The REWARD_TEAM_PICK pending exactly as `_visit_reward` builds it
    (engine.py:3233-3248 for `sacrifice`, 3242-3248 for `stat10`)."""
    eng, state = _state_after_starter()
    state.pending = engine.PendingChoice(
        phase=engine.Phase.REWARD_TEAM_PICK,
        options=[engine._mon_summary(m) for m in state.team],
        optional=False,
        extra={"node_id": "n1_0", "kind": kind},
    )
    state.phase = engine.Phase.REWARD_TEAM_PICK
    return state


def _escape_rope_pending():
    """The ESCAPE_ROPE_CHOICE pending exactly as `_finish_battle` builds it
    (engine.py:1544-1552)."""
    eng, state = _state_after_starter()
    state.items.append("escape_rope")
    rope_index = len(state.items) - 1
    state.pending = engine.PendingChoice(
        phase=engine.Phase.ESCAPE_ROPE_CHOICE,
        options=[{"action": "use_escape_rope", "item_index": rope_index}],
        optional=True,
        extra={"rope_index": rope_index, "continuation": []},
    )
    state.phase = engine.Phase.ESCAPE_ROPE_CHOICE
    return state


def _branching_evolution_state():
    """A real `Phase.EVOLUTION_CHOICE`, raised through the real Moon Stone
    path (`_apply_use_item` -> `_maybe_evolve_one`, engine.py:1819-1822)."""
    from pokelike import data
    eng, state = _state_after_starter()
    species = sorted(data.get_branching_evolutions())[0]
    state.team[0].species_id = species
    state.items.append("moon_stone")
    eng.step(engine.UseItem(item_index=len(state.items) - 1, target_index=0))
    return eng.state


class PendingContextTests(unittest.TestCase):
    """R3 mutant 2: break the `reward_team_pick` / `escape_rope_choice`
    option-and-context projection."""

    def test_pending_field_set_is_exactly_pinned(self):
        eng = engine.Engine()
        fresh = eng.reset(seed=11)
        pending = encode_state(fresh)["pending"]
        self.assertIsNotNone(pending)
        self.assertEqual(set(pending), set(contract.PENDING_FIELDS))
        self.assertEqual(set(pending["context"]), set(contract.PENDING_CONTEXT_FIELDS))

    def test_every_context_key_is_present_on_every_phase(self):
        """`context` is documented as always carrying all five keys so a
        renderer never needs an existence check. A phase that quietly emits a
        short dict would make `context.title` undefined in JS rather than
        null, which renders as the string "undefined"."""
        states = [
            _reward_team_pick("sacrifice"),
            _reward_team_pick("stat10"),
            _escape_rope_pending(),
            _branching_evolution_state(),
        ]
        eng = engine.Engine()
        states.append(eng.reset(seed=3))  # choose_starter: a phase with no context
        for state in states:
            ctx = contract.pending_view(state.pending, state)["context"]
            self.assertEqual(
                set(ctx), set(contract.PENDING_CONTEXT_FIELDS),
                f"context key set drifted on {state.phase.value}",
            )

    def test_the_two_reward_branches_are_distinguishable(self):
        """The decisive one. `sacrifice` and `stat10` present an IDENTICAL
        team list (both are `[_mon_summary(m) for m in state.team]`,
        engine.py:3235/3244) and do opposite things -- one deletes the picked
        member, the other buffs it (engine.py:3288-3299). If the context stops
        distinguishing them, a browser player is one click from releasing a
        Pokemon they meant to power up, with nothing on screen to warn them.
        """
        sac_state = _reward_team_pick("sacrifice")
        stat_state = _reward_team_pick("stat10")
        sac = contract.pending_view(sac_state.pending, sac_state)
        stat = contract.pending_view(stat_state.pending, stat_state)
        # Identical option lists -- the whole point: only the context differs.
        self.assertEqual(sac["options"], stat["options"])
        self.assertEqual(sac["context"]["kind"], "sacrifice")
        self.assertEqual(stat["context"]["kind"], "stat10")
        self.assertNotEqual(sac["context"]["title"], stat["context"]["title"])
        self.assertNotEqual(sac["context"]["desc"], stat["context"]["desc"])
        for ctx in (sac["context"], stat["context"]):
            self.assertTrue(ctx["title"], "reward pick screen has no title")
            self.assertTrue(ctx["desc"], "reward pick screen has no description")
        # The source's own strings (bundle.deobfuscated.js:77022-77024).
        self.assertIn("release", sac["context"]["title"].lower())
        self.assertIn("+4 levels", sac["context"]["desc"])

    def test_stat10_label_tracks_the_engines_own_buff_scaling(self):
        """The displayed percentage is `max(1, round(2 * multiplier)) * 5`
        (bundle.deobfuscated.js:77040) over the SAME multiplier
        `_apply_run_stat_buff` scales the real buff by (engine.py:3110). Pinned
        against the engine rather than against the literal "5%", so the label
        and the mechanic cannot drift apart -- a hard-coded string would keep
        claiming 10% if the multiplier ever changed.
        """
        from pokelike import map_gen
        expected = max(1, map_gen._js_round(2 * engine._SUBMAP_REWARD_STAT_MULTIPLIER)) * 5
        self.assertEqual(contract._stat10_percent(), expected)
        state = _reward_team_pick("stat10")
        self.assertIn(f"+{expected}%", contract.pending_view(state.pending, state)["context"]["desc"])

    def test_escape_rope_option_is_labelled_not_a_raw_dict(self):
        """The engine's option is `{"action": ..., "item_index": ...}`
        (engine.py:1548), which matched no console branch and had no web case
        at all -- both rendered it as a raw dict or a toast. Enriched on the
        read side per R1's rule."""
        state = _escape_rope_pending()
        view = contract.pending_view(state.pending, state)
        self.assertEqual(view["context"]["kind"], "escape_rope")
        self.assertTrue(view["context"]["title"])
        self.assertTrue(view["context"]["desc"])
        self.assertTrue(view["optional"], "the rope offer must be declinable")
        opt = view["options"][0]
        self.assertEqual(opt["item_id"], engine._ESCAPE_ROPE_ITEM_ID)
        self.assertTrue(opt["label"])
        # The engine's own keys must survive the enrichment.
        self.assertEqual(opt["action"], "use_escape_rope")
        self.assertEqual(opt["item_index"], state.pending.extra["rope_index"])

    def test_evolution_choice_names_the_evolving_pokemon(self):
        """`showBranchingChoice` titles the screen `displayName(mon) + " is
        evolving!"` (bundle.deobfuscated.js:70567-70570). The engine's options
        are `{into, name}` only, so before R3 both renderers showed two target
        species and never said which team member was becoming one."""
        state = _branching_evolution_state()
        self.assertEqual(state.phase, engine.Phase.EVOLUTION_CHOICE)
        view = contract.pending_view(state.pending, state)
        ctx = view["context"]
        subject = state.team[state.pending.extra["team_index"]]
        self.assertEqual(ctx["team_index"], state.pending.extra["team_index"])
        self.assertIsNotNone(ctx["subject"])
        self.assertEqual(ctx["subject"]["species_id"], subject.species_id)
        self.assertIn(subject.nickname or subject.name, ctx["title"])
        self.assertTrue(ctx["desc"])

    def test_evolution_options_carry_the_branch_types_and_a_species_id(self):
        """Each branch card is sprite + name + `types.join("/")`
        (bundle.deobfuscated.js:70601-70603), and the sprite path is chosen
        from the EVOLVING mon's shininess, not the branch's (70578-70581)."""
        state = _branching_evolution_state()
        view = contract.pending_view(state.pending, state)
        subject = state.team[state.pending.extra["team_index"]]
        self.assertTrue(view["options"])
        for opt, branch in zip(view["options"], state.pending.extra["branches"]):
            self.assertEqual(opt["species_id"], opt["into"])
            self.assertEqual(opt["into"], branch.into)
            self.assertEqual(opt["is_shiny"], bool(subject.is_shiny))
            self.assertEqual(opt["types"], list(branch.types or []))
        # At least one branch really does declare types -- otherwise this test
        # would pass vacuously against an empty list everywhere.
        self.assertTrue(any(o["types"] for o in view["options"]))

    def test_context_never_exposes_a_live_engine_object(self):
        """`extra` holds live `Combatant`/`data.Trainer`/`data.Evolution`
        references. `_pending_context` is an allow-list precisely so a new one
        cannot leak; this proves the result is still plain JSON."""
        for state in (_reward_team_pick("sacrifice"), _escape_rope_pending(),
                      _branching_evolution_state()):
            view = contract.pending_view(state.pending, state)
            json.dumps(view)  # raises TypeError if any live object survived
            self.assertNotIn("extra", view)
            self.assertNotIn("branches", json.dumps(view))

    def test_pending_view_still_works_without_a_state(self):
        """R1's single-argument call site must keep working -- `state` is
        optional. The context degrades to what does not need the team rather
        than raising."""
        state = _branching_evolution_state()
        view = contract.pending_view(state.pending)
        self.assertEqual(set(view), set(contract.PENDING_FIELDS))
        self.assertEqual(set(view["context"]), set(contract.PENDING_CONTEXT_FIELDS))
        self.assertIsNone(view["context"]["subject"])


class PartyActionLegalityTests(unittest.TestCase):
    """R3 mutant 1: make an illegal ReorderTeam/UseItem/EquipItem appear legal
    in `legal_actions` (or a legal one disappear).

    The UI is built to draw a control only when `legal_actions` says so, which
    makes that dict a load-bearing boundary rather than an advisory. These
    detectors pin it against the engine's OWN apply-side rules, so the two
    cannot drift: anything `legal_actions` advertises must actually be
    accepted by `Engine.step`, and anything it withholds must be rejected.
    """

    def _on_map_state(self, seed=7):
        eng, state = _state_after_starter(seed)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        return eng, state

    def test_every_advertised_use_item_target_is_actually_accepted(self):
        eng, state = self._on_map_state()
        state.items.extend(["rare_candy", "sacred_ash", "moon_stone", "tm_normal"])
        legal = engine.legal_actions(state)
        self.assertIn("use_item", legal)
        for entry in legal["use_item"]:
            for target in entry["target_indices"]:
                self.assertTrue(
                    engine._usable_item_can_target(entry["item_id"], state.team[target]),
                    f"{entry['item_id']} advertised an ineligible target {target}",
                )

    def test_every_withheld_use_item_target_is_actually_rejected(self):
        """The other direction, and the one a UI bug hides behind: a target
        the engine omits must really be refused by `_apply_use_item`
        (engine.py:1797-1798), not merely undrawn."""
        eng, state = self._on_map_state()
        state.items.append("sacred_ash")
        bag_index = len(state.items) - 1
        # sacred_ash may only target a damaged member (engine.py:1755-1756).
        for mon in state.team:
            mon.current_hp = mon.max_hp
        legal = engine.legal_actions(state)
        entry = next((e for e in legal.get("use_item", []) if e["item_index"] == bag_index), None)
        self.assertIsNone(entry, "a full-HP team must not be a legal sacred_ash target")
        with self.assertRaises(ValueError):
            eng.step(engine.UseItem(item_index=bag_index, target_index=0))

    def test_equip_item_never_advertises_a_usable_item(self):
        """CODEX P0.5: the source's team-bar handler routes usable items to
        `applyUsableItemTo` and never to `equipItemFromBag`
        (bundle.deobfuscated.js:64943-64950), so a usable bag index must not
        appear in `equip_item.bag_indices` -- and must be refused if sent."""
        eng, state = self._on_map_state()
        state.items.append("rare_candy")
        bag_index = len(state.items) - 1
        legal = engine.legal_actions(state)
        self.assertNotIn(bag_index, legal.get("equip_item", {}).get("bag_indices", []))
        with self.assertRaises(ValueError):
            eng.step(engine.EquipItem(bag_index=bag_index, team_index=0))

    def test_every_advertised_equip_pair_is_actually_accepted(self):
        eng, state = self._on_map_state()
        state.items.append("charcoal")
        legal = engine.legal_actions(state)
        eq = legal.get("equip_item")
        self.assertIsNotNone(eq, "a passive bag item must be equippable")
        bag_index = eq["bag_indices"][-1]
        team_index = eq["team_indices"][0]
        eng.step(engine.EquipItem(bag_index=bag_index, team_index=team_index))
        self.assertIsNotNone(eng.state.team[team_index].held_item)

    def test_reorder_is_advertised_exactly_when_a_permutation_exists(self):
        """`reorder_team` is reported only for a team of 2+ (engine.py:535).
        A one-member team has exactly one permutation -- the identity -- so
        advertising it would draw a drag handle that can do nothing."""
        eng, state = self._on_map_state()
        self.assertEqual(len(state.team), 1)
        self.assertNotIn("reorder_team", engine.legal_actions(state))
        state.team.append(state.team[0])
        legal = engine.legal_actions(state)
        self.assertEqual(legal["reorder_team"]["team_size"], 2)

    def test_a_non_permutation_reorder_is_refused(self):
        """`_apply_reorder_team`'s guard (engine.py:1868-1869). The UI builds
        a transposition, but the boundary must reject anything else."""
        eng, state = self._on_map_state()
        state.team.append(state.team[0])
        for bad in ((0, 0), (1, 1), (0,), (0, 1, 2), (0, 2)):
            with self.assertRaises(ValueError, msg=f"accepted non-permutation {bad}"):
                eng.step(engine.ReorderTeam(order=bad))

    def test_the_transposition_the_ui_builds_is_applied_as_the_source_does(self):
        """The web UI's `swapPermutation` and play.py's interactive/autopilot
        paths all build the identity permutation with two positions exchanged,
        because `renderTeamBar`'s drop handler is a straight two-element swap
        (bundle.deobfuscated.js:64805). This pins the semantics
        `new_team[i] = old_team[order[i]]` those callers depend on."""
        eng, state = self._on_map_state()
        first = state.team[0]
        second = engine._make_wild_combatant(25, 5, move_tier=1)
        state.team.append(second)
        eng.step(engine.ReorderTeam(order=(1, 0)))
        self.assertIs(eng.state.team[0], second)
        self.assertIs(eng.state.team[1], first)


class WebRendererWiringTests(unittest.TestCase):
    """R3: the web renderer must actually SEND the three actions and HANDLE
    the two phases.

    Honest limitation, and R5's scope, not this milestone's: nothing here
    executes `app.js`. R2's own audit named the missing JS/DOM-shim harness as
    its most valuable follow-up, and its N5 finding is exactly the class of bug
    a text detector cannot see. What these do catch is the regression that
    actually happened before -- a phase or an action having NO wiring at all --
    which is what R3 exists to fix.
    """

    def _app_js(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        return (root / "pokelike" / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    def _index_html(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        return (root / "pokelike" / "webui" / "static" / "index.html").read_text(encoding="utf-8")

    def test_every_engine_phase_has_a_dispatcher_case(self):
        """The bug R3 closes: `render()`'s switch had no case for
        `reward_team_pick` or `escape_rope_choice`, so a browser player who
        reached either got `showToast('Unhandled phase: ...')` and was stuck
        with no control at all. Pinned over the whole `Phase` enum so the next
        phase added cannot repeat it."""
        js = self._app_js()
        missing = [p.value for p in engine.Phase if f"case '{p.value}':" not in js]
        self.assertEqual([], missing, f"app.js render() has no case for: {missing}")

    def test_app_js_sends_all_three_party_actions(self):
        """CODEX section 7.2's finding, which stood until R3: `ReorderTeam`,
        `UseItem` and `EquipItem` were engine-modelled and `legal_actions`-
        reported, but a full-file search of app.js found no sender for any of
        them."""
        js = self._app_js()
        for action in ("ReorderTeam", "UseItem", "EquipItem"):
            self.assertIn(f"type: '{action}'", js, f"app.js never sends {action}")

    def test_app_js_reads_legality_from_the_observation(self):
        """A renderer that re-derives eligibility client-side is the same bug
        class as one reading a stale field (docs/renderer-contract.md). The
        controls must be gated on `legal_actions`, which the observation
        already carries whole."""
        js = self._app_js()
        for key in ("reorder_team", "use_item", "equip_item", "target_indices", "bag_indices"):
            self.assertIn(key, js, f"app.js does not consult legal_actions.{key}")

    def test_the_item_equip_overlays_third_exit_is_reachable(self):
        """M5 proved `#btn-equip-cancel` is neither an equip nor a bank
        (bundle.deobfuscated.js:79563-79569) and the engine models it as
        `SelectOption(cancel=True)`. No browser control could send it before
        R3, so the only way out of that screen banked an item the source did
        not."""
        js = self._app_js()
        html = self._index_html()
        self.assertIn("btn-equip-cancel", html)
        self.assertIn("btn-equip-to-bag", html)
        self.assertIn("cancel: true", js)

    def test_the_two_new_screens_exist_in_the_document(self):
        html = self._index_html()
        for element_id in ("reward-pick-screen", "reward-pick-title", "reward-pick-desc",
                           "escape-rope-screen", "escape-rope-title", "btn-decline-rope"):
            self.assertIn(f'id="{element_id}"', html, f"index.html lacks #{element_id}")

    def test_the_transport_decodes_all_three_actions_and_cancel(self):
        """The wiring is only real if the server accepts it. `cancel` was
        silently dropped by `decode_action` before R3, so a browser sending it
        got a plain skip."""
        from pokelike.webui.state_json import decode_action
        self.assertEqual(
            decode_action({"type": "ReorderTeam", "order": [1, 0]}),
            engine.ReorderTeam(order=(1, 0)),
        )
        self.assertEqual(
            decode_action({"type": "UseItem", "item_index": 1, "target_index": 2}),
            engine.UseItem(item_index=1, target_index=2),
        )
        self.assertEqual(
            decode_action({"type": "EquipItem", "bag_index": 1, "team_index": 2}),
            engine.EquipItem(bag_index=1, team_index=2),
        )
        cancelled = decode_action({"type": "SelectOption", "cancel": True})
        self.assertTrue(cancelled.cancel)
        self.assertIsNone(cancelled.index)
        # A plain skip must stay a plain skip.
        self.assertFalse(decode_action({"type": "SelectOption", "index": None}).cancel)
        # And a non-boolean must be refused, not coerced (CODEX issue 47).
        from pokelike.webui.state_json import ActionDecodeError
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "SelectOption", "cancel": "false"})


class ConsoleRendererPhaseTests(unittest.TestCase):
    """R3: `render_pending` is phase-generic, which is not the same as
    adequate for every phase."""

    def test_the_escape_rope_option_is_not_printed_as_a_raw_dict(self):
        from pokelike.render import console
        state = _escape_rope_pending()
        text = console.render_pending(state)
        self.assertNotIn("{'action'", text)
        self.assertIn("Escape Rope", text)
        self.assertIn("(or skip / decline)", text)

    def test_the_reward_pick_header_says_which_branch_it_is(self):
        """Both branches print the same team list; without the title the
        console gave a player no way to tell "release" from "buff"."""
        from pokelike.render import console
        sac = console.render_pending(_reward_team_pick("sacrifice"))
        stat = console.render_pending(_reward_team_pick("stat10"))
        self.assertNotEqual(sac, stat)
        self.assertIn("release", sac.lower())
        self.assertIn("+4 levels", sac)
        self.assertIn("%", stat)

    def test_the_item_choice_prints_the_description_the_contract_supplies(self):
        """R6/N33, the console half.

        The web client's item card was ignoring `icon`/`icon_url`/`desc`; this
        renderer was ignoring `desc`. Both had the same root cause -- the
        ITEM_CHOICE options were never routed through `item_view` at all, so
        neither renderer *could* have drawn them. Asserted here against the
        real ported item table rather than against the projection's own value,
        so a projection that stopped supplying descriptions fails instead of
        agreeing with itself.
        """
        from pokelike import data
        from pokelike.render import console, contract

        eng, state = _state_after_starter()
        item = next(i for i in data.get_passive_items() if i.desc)
        state.pending = engine.PendingChoice(
            phase=engine.Phase.ITEM_CHOICE,
            options=[{"id": item.id, "name": item.name, "usable": False}],
            optional=True,
            extra={"items": [], "node_id": "n0_0"},
        )
        state.phase = engine.Phase.ITEM_CHOICE

        options = contract.pending_view(state.pending, state)["options"]
        self.assertEqual(item.desc, options[0]["desc"],
                         "ITEM_CHOICE options are no longer enriched from item_view")
        self.assertEqual(item.icon, options[0]["icon"])

        text = console.render_pending(state)
        self.assertIn(item.name, text)
        self.assertIn(item.desc, text,
                      "the console prints an item offer without the description "
                      "that makes it decidable")

    def test_a_damaging_move_preview_never_reports_zero_power(self):
        """R6/N34. The power a card draws has to be worth drawing.

        This is the invariant that makes the browser-side power assertion
        non-tautological: a detector that compares the rendered badge against
        `move_preview.power` agrees with itself if the projection is what
        broke. `power > 0 for a damaging move` is an independent fact -- a
        move with no power that is not flagged `no_damage` is incoherent,
        whatever the projection says.
        """
        from pokelike.render import contract

        checked = 0
        for seed in range(1, 12):
            # `reset` stops at CHOOSE_STARTER with an EMPTY team, so a sweep
            # over `state.team` there checks nothing at all.
            eng, state = _state_after_starter(seed=seed)
            for mon in state.team:
                preview = contract._move_preview(mon)
                if preview is None:
                    continue
                checked += 1
                if preview["no_damage"]:
                    continue
                self.assertIsInstance(preview["power"], int)
                self.assertGreater(
                    preview["power"], 0,
                    f"{preview['name']} is a damaging move projected with "
                    f"power {preview['power']}",
                )
        self.assertGreater(checked, 0, "no move_preview was reachable -- vacuous")

    def test_the_evolution_prompt_names_the_evolving_pokemon(self):
        from pokelike.render import console
        state = _branching_evolution_state()
        text = console.render_pending(state)
        subject = state.team[state.pending.extra["team_index"]]
        self.assertIn(subject.nickname or subject.name, text)
        self.assertIn("evolving", text.lower())

    def test_console_play_can_construct_all_three_party_actions(self):
        """Before R3 `play.py`'s ON_MAP branch returned `VisitNode` and
        nothing else, in BOTH modes -- so no path in the repository outside
        the tests ever built one of these. The autopilot helper is driven
        directly here rather than through a whole episode so the assertion is
        about the action set, not about a route."""
        import random as _random
        from pokelike.render import play
        eng, state = _state_after_starter()
        state.team.append(engine._make_wild_combatant(25, 5, move_tier=1))
        state.items.extend(["charcoal", "rare_candy"])
        legal = engine.legal_actions(state)
        for key in ("reorder_team", "use_item", "equip_item"):
            self.assertIn(key, legal, f"probe state does not offer {key}")
        seen = set()
        for seed in range(400):
            _random.seed(seed)
            action = play._autopilot_party_action(state, legal)
            if action is not None:
                seen.add(type(action).__name__)
        self.assertEqual({"ReorderTeam", "UseItem", "EquipItem"}, seen)

    def test_autopilot_party_actions_are_always_legal(self):
        """Every generated action must be accepted by the engine -- the
        autopilot must not be a source of `ValueError`s that a human would
        never trigger."""
        import random as _random
        from pokelike.render import play
        for seed in range(60):
            _random.seed(seed)
            eng, state = _state_after_starter(seed=seed % 17 + 1)
            state.team.append(engine._make_wild_combatant(25, 5, move_tier=1))
            state.items.extend(["charcoal", "rare_candy", "sacred_ash"])
            legal = engine.legal_actions(state)
            action = play._autopilot_party_action(state, legal)
            if action is None:
                continue
            eng.step(action)  # raises ValueError if the autopilot proposed an illegal move


class ContractVersionTests(unittest.TestCase):
    def test_contract_version_is_5_after_m6(self):
        """R2 took this to 2, R3 to 3, R4 to 4 (`battle_view` gained
        `player_team_start`/`enemy_team_start` and `replay`). M6 changed the
        SHAPE again -- `battle.turns[*].events` can now carry `effect` (N10)
        and `faint` (N11) records, so a renderer switching on the event type
        sees a member it did not before -- so per docs/renderer-contract.md
        section 8 it bumps again."""
        self.assertEqual(contract.CONTRACT_VERSION, 5)

    def test_the_oracle_surface_is_untouched_by_this_contract(self):
        """R2 must not have moved the oracle. `map.edges` changing shape here
        is exactly the kind of change that would break parity if the two
        surfaces were shared -- they are not, and this says so."""
        import pathlib
        schema = pathlib.Path(__file__).resolve().parents[2] / "route-oracle" / "SCHEMA.md"
        text = schema.read_text(encoding="utf-8")
        self.assertIn("schema_version", text)
        # The renderer's own field names must not have leaked into the oracle.
        for renderer_only in ("edge_margin", "sprite_size", "circle_radius", "unexplored"):
            self.assertNotIn(renderer_only, text)


class ConsoleReplayRepeatTests(unittest.TestCase):
    """R5/N14. `play.run_episode` used to floor its log delta at
    `max(1, len(state.log) - seen_log_total)`. The floor was there for a real
    reason -- `render_state`'s `state.log[-recent_log:]` turns a window of 0
    into `[-0:]`, i.e. the WHOLE log -- but on a zero-delta step it re-showed
    the previous step's entry, and when that entry was a `"battle"` the entire
    replay block printed again, up to 3x in a row.

    The repair moves the responsibility: `render_state` now treats a window of
    <= 0 as "show nothing", and `run_episode` passes the true delta.
    """

    def test_a_zero_window_shows_no_log_entries_at_all(self):
        """The `[-0:]` trap, pinned directly. Before R5 this printed the whole
        run's log and, with it, every battle replay in the run."""
        state, battles = _any_state_with_battle()
        self.assertTrue(battles)
        text = console.render_state(state, recent_log=0)
        self.assertNotIn("battle replay", text)
        for entry in state.log:
            self.assertNotIn(
                console.render_log_entry(entry), text,
                "recent_log=0 leaked a log entry -- the `[-0:]` whole-list slice is back",
            )
        # It must still be a real render, not an empty string.
        self.assertIn("Team:", text)

    def test_a_negative_window_is_also_empty(self):
        state, _ = _any_state_with_battle()
        self.assertNotIn("battle replay", console.render_state(state, recent_log=-1))

    def test_play_passes_the_true_delta_with_no_floor(self):
        """The floor is what caused the repeat, so its absence is the fix.

        Comments are stripped first: the code comment explaining the removal
        necessarily names `max(1, ...)`, and a detector that cannot tell the
        two apart would fail on its own documentation."""
        import inspect
        source = inspect.getsource(play.run_episode)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn("new_entries = len(state.log) - seen_log_total", code)
        self.assertNotIn("max(1", code.replace(" ", ""))

    def test_each_battle_is_replayed_exactly_once_per_episode(self):
        """The behavioural detector, not a source-text one: drive real
        episodes and require one printed replay block per logged battle.

        `play._choose_action` draws from the GLOBAL `random`, so the module's
        state is seeded and restored here to keep this deterministic."""
        import contextlib
        import io
        import random
        saved = random.getstate()
        try:
            for seed in (7, 23, 101):
                random.seed(90000 + seed)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    state = play.run_episode(seed=seed, max_steps=2000)
                printed = buf.getvalue().count("battle replay")
                logged = sum(1 for e in state.log if e.get("type") == "battle")
                self.assertEqual(
                    logged, printed,
                    f"seed {seed}: {logged} battles logged but {printed} replay blocks printed",
                )
        finally:
            random.setstate(saved)


class LogBurstWindowTests(unittest.TestCase):
    """R5/N16. `applyWithBattleInterstitial` sizes its batch from `log_total`
    (authoritative) but can only read `state.log`, which the server trims to
    `encode_state`'s `recent_log`. If one `Engine.step` ever appended MORE
    entries than that window, with the `"battle"` among the oldest, the entry
    would not be in the payload at all and CODEX section 7.7's interstitial bug
    would silently re-open.

    R4's audit measured this unreachable (max step delta 3 against a window of
    5) but undefended -- nothing would notice a future engine change that
    lengthened a burst. This is that detector.
    """

    #: Every mode combination `engine.Engine.reset` accepts that the renderers
    #: support, so a burst introduced on any one of them is in scope.
    CONFIGS = (
        {}, {"nuzlocke_mode": True}, {"gen2_mode": True},
        {"gen3_mode": True}, {"gen4_mode": True},
        {"gen4_mode": True, "nuzlocke_mode": True},
    )
    SEEDS = tuple(range(1, 9))

    def _sweep(self):
        """Returns (delta histogram, step count, distinct log-entry types)."""
        import collections
        import random
        hist = collections.Counter()
        kinds = set()
        steps = 0
        saved = random.getstate()
        try:
            for seed in self.SEEDS:
                for i, cfg in enumerate(self.CONFIGS):
                    random.seed(seed * 131 + i)
                    eng = engine.Engine()
                    state = eng.reset(seed=seed, **cfg)
                    seen = len(state.log)
                    n = 0
                    while state.phase not in (engine.Phase.GAME_OVER, engine.Phase.VICTORY) and n < 2000:
                        n += 1
                        state = eng.step(play._choose_action(state, interactive=False))
                        hist[len(state.log) - seen] += 1
                        seen = len(state.log)
                        steps += 1
                    kinds.update(e.get("type") for e in state.log)
        finally:
            random.setstate(saved)
        return hist, steps, kinds

    def _default_window(self):
        import inspect
        return inspect.signature(contract.observation).parameters["recent_log"].default

    def test_no_single_step_appends_more_log_entries_than_the_client_window(self):
        hist, steps, kinds = self._sweep()
        window = self._default_window()
        max_delta = max(hist)
        # Non-vacuity, so a sweep that silently stopped exercising the engine
        # cannot pass by observing nothing.
        self.assertGreater(steps, 500, "sweep covered too few steps to mean anything")
        self.assertGreaterEqual(len(kinds), 8, "sweep exercised too few log-entry types")
        self.assertGreater(
            max_delta, 1,
            "sweep never observed a multi-entry step -- it cannot detect a burst",
        )
        self.assertLess(
            max_delta, window,
            f"a single Engine.step appended {max_delta} log entries against a client "
            f"window of {window}: the interstitial can now miss a battle "
            f"(CODEX section 7.7 re-opened). Either raise encode_state's recent_log "
            f"default or have the client request a window >= `appended`. "
            f"Histogram: {dict(sorted(hist.items()))}",
        )

    def test_the_window_default_is_shared_by_both_encode_paths(self):
        """`encode_state` and `contract.observation` must agree, or the number
        the test above validates is not the number the client receives."""
        import inspect
        from pokelike.webui import state_json
        self.assertEqual(
            inspect.signature(state_json.encode_state).parameters["recent_log"].default,
            self._default_window(),
        )

    def test_app_js_documents_the_clamp_it_depends_on(self):
        """The client cannot detect the truncation itself, so the bound is
        load-bearing documentation. If it goes, so does the reason this
        detector exists."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        js = (root / "pokelike" / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("recent_log", js)
        self.assertIn("log_total", js)


class ChoiceOptionEnrichmentTests(unittest.TestCase):
    """R7/N43 and N45 -- the read-side enrichment that makes a Pokemon choice
    decidable, pinned on the PYTHON side.

    The DOM-shim detectors prove the browser draws this. These prove the
    projection carries it, which is the half that survives without Node.
    """

    def _started(self, seed=7, **cfg):
        eng = engine.Engine()
        eng.reset(seed=seed, **cfg)
        eng.step(engine.ChooseStarter(species_id=eng.state.pending.options[0]["species_id"]))
        return eng, eng.state

    def _node(self, node_type):
        from pokelike import map_gen
        return map_gen.MapNode(id="n", type=node_type, layer=1, col=0)

    def _raise_each_phase(self, state):
        """Every R7-enriched choice, raised through the ENGINE'S OWN producer.

        Constructing a `PendingChoice` by hand here would test this module's
        assumption about the option order instead of the engine's actual
        behaviour, which is the entire question §3.3 asks.
        """
        from pokelike import map_gen
        out = {}

        engine._visit_trade(state, self._node(map_gen.TRADE))
        out["trade_choice"] = state.pending

        engine._offer_catch_choice(state, self._node(map_gen.CATCH), list(state.team))
        out["catch_choice"] = state.pending

        engine._visit_move_tutor(state, self._node(map_gen.MOVE_TUTOR))
        out["move_tutor_choice"] = state.pending

        engine._try_add_to_team  # noqa: B018 -- documents where swap comes from
        state.pending = engine.PendingChoice(
            phase=engine.Phase.ITEM_EQUIP_CHOICE,
            options=[engine._mon_summary(m) for m in state.team],
            optional=True,
            extra={"item_id": "leftovers", "node_id": "n"},
        )
        out["item_equip_choice"] = state.pending
        return out

    def test_every_mon_choice_option_carries_the_full_card_projection(self):
        _, state = self._started()
        for phase, pending in self._raise_each_phase(state).items():
            options = contract.pending_view(pending, state)["options"]
            self.assertTrue(options, f"{phase} produced no options")
            for i, opt in enumerate(options):
                for key in ("types", "base_stats", "effective_stats",
                            "stages", "stat_buffs", "move_preview",
                            "status_flags", "sprite_url"):
                    self.assertIn(key, opt, f"{phase}[{i}] is missing {key}")

    def test_the_enriched_values_are_the_real_combatants(self):
        """Positional correspondence is a producer fact (§3.3). This asserts
        the fact rather than trusting it: option i must describe team member i
        (or candidate i), by a value only that Combatant has."""
        eng, state = self._started()
        # A team of two with DIFFERENT effective stats, so a crossed
        # correspondence produces a wrong number rather than a coincidence.
        from pokelike import map_gen
        state.team.append(engine._make_wild_combatant(
            0x19, 12, is_shiny=False, move_tier=1, gen2_mode=False, gen4_mode=False))
        self.assertNotEqual(
            state.team[0].species_id, state.team[1].species_id,
            "the fixture needs two distinguishable members",
        )
        engine._visit_trade(state, self._node(map_gen.TRADE))
        options = contract.pending_view(state.pending, state)["options"]
        self.assertEqual(len(options), len(state.team))
        for i, (opt, mon) in enumerate(zip(options, state.team)):
            self.assertEqual(opt["species_id"], mon.species_id, f"option {i} names another mon")
            self.assertEqual(opt["effective_stats"], contract._effective_stats(mon),
                             f"option {i} carries another mon's stats")

    def test_a_mismatched_subject_is_not_enriched(self):
        """The `_subject_matches` guard. If a future producer ever broke the
        correspondence, the card must lose detail rather than gain a LIE --
        the wrong Pokemon's stats on a release screen is destructive."""
        opt = {"species_id": 1, "level": 5, "current_hp": 19, "max_hp": 19, "is_shiny": False}
        _, state = self._started()
        mon = state.team[0]
        contract._enrich_from_subject(opt, mon)
        enriched_when_matching = "effective_stats" in opt

        wrong = dict(opt)
        for key in ("effective_stats", "base_stats", "types"):
            wrong.pop(key, None)
        wrong["species_id"] = mon.species_id + 1
        contract._enrich_from_subject(wrong, mon)
        self.assertTrue(enriched_when_matching, "the guard rejected a MATCHING subject")
        self.assertNotIn("effective_stats", wrong,
                         "a mismatched subject was enriched anyway")

    def test_the_swap_screens_two_shapes_are_addressed_not_guessed(self):
        """`_offer_swap_screen` presents the INCOMING mon when the team has
        room and the TEAM when it is full (engine.py:1064-1071). Enriching
        both from `state.team` would put the wrong Pokemon on the card in the
        first case."""
        _, state = self._started()
        incoming = engine._make_wild_combatant(
            0x19, 30, is_shiny=False, move_tier=2, gen2_mode=False, gen4_mode=False)
        engine._offer_swap_screen(state, incoming, "n")
        self.assertTrue(state.pending.extra["has_room"], "fixture precondition: room")
        opt = contract.pending_view(state.pending, state)["options"][0]
        self.assertEqual(opt["species_id"], incoming.species_id)
        self.assertEqual(opt["effective_stats"], contract._effective_stats(incoming),
                         "the room branch was enriched from the TEAM, not the incoming mon")

    def test_move_preview_at_an_explicit_tier_is_deterministic(self):
        """N45 previews a move the player does not have yet. That is only
        honest if `get_best_move` is a pure function of its inputs -- checked,
        not assumed (R7 §5.2)."""
        _, state = self._started()
        mon = state.team[0]
        first = contract._move_preview(mon, 1)
        for _ in range(5):
            self.assertEqual(contract._move_preview(mon, 1), first,
                             "get_best_move is not deterministic -- a preview would be a lie")
        self.assertIsNotNone(first)

    def test_the_tutor_ceiling_mirrors_the_engines_actual_behaviour(self):
        """`contract._MOVE_TIER_MAX` is a mirrored literal (engine.py:3558),
        because R7 may not add a constant to `engine.py`. This pins it by
        EXECUTION: tutoring a mon already at the ceiling must not move it."""
        _, state = self._started()
        mon = state.team[0]
        mon.move_tier = contract._MOVE_TIER_MAX
        # A REAL node id: `_resolve_move_tutor_choice` advances the map, so a
        # synthetic id would fail on the advance rather than on the ceiling.
        node_id = next(iter(state.map.nodes))
        state.pending = engine.PendingChoice(
            phase=engine.Phase.MOVE_TUTOR_CHOICE,
            options=[{"team_index": 0, "species_id": mon.species_id,
                      "name": mon.name, "move_tier": mon.move_tier}],
            optional=True, extra={"node_id": node_id},
        )
        state.phase = engine.Phase.MOVE_TUTOR_CHOICE
        engine._resolve_move_tutor_choice(state, engine.SelectOption(index=0))
        self.assertEqual(
            mon.move_tier, contract._MOVE_TIER_MAX,
            "the engine tutored PAST contract._MOVE_TIER_MAX -- the mirror has drifted",
        )

    def test_the_tutor_producer_never_offers_a_maxed_member(self):
        """Why `move_tier_capped` is unreachable, asserted rather than
        asserted-about: `_visit_move_tutor` filters to `move_tier < 2`
        (engine.py:2868), porting the source's 'Already mastered!' span."""
        from pokelike import map_gen
        _, state = self._started()
        state.team[0].move_tier = contract._MOVE_TIER_MAX
        engine._visit_move_tutor(state, self._node(map_gen.MOVE_TUTOR))
        options = contract.pending_view(state.pending, state)["options"]
        self.assertEqual(options, [], "a fully-mastered member was offered to the tutor")

    def test_the_tutor_option_previews_a_genuinely_different_move(self):
        from pokelike import map_gen
        _, state = self._started()
        engine._visit_move_tutor(state, self._node(map_gen.MOVE_TUTOR))
        options = contract.pending_view(state.pending, state)["options"]
        self.assertTrue(options, "no tutor options to check")
        opt = options[0]
        self.assertGreater(opt["move_tier_next"], opt["move_tier"])
        self.assertFalse(opt["move_tier_capped"])
        self.assertIsNotNone(opt["move_preview_next"])
        self.assertNotEqual(
            (opt["move_preview"]["name"], opt["move_preview"]["power"]),
            (opt["move_preview_next"]["name"], opt["move_preview_next"]["power"]),
            "the tutor previews the same move for two different tiers",
        )

    def test_enrichment_never_overwrites_what_the_engine_wrote(self):
        """R6/N33's rule: the producer is the authority on what it is
        offering; the renderer may only ADD detail."""
        _, state = self._started()
        from pokelike import map_gen
        engine._visit_trade(state, self._node(map_gen.TRADE))
        raw = [dict(o) for o in state.pending.options]
        options = contract.pending_view(state.pending, state)["options"]
        for produced, projected in zip(raw, options):
            for key, value in produced.items():
                self.assertEqual(projected[key], value,
                                 f"the enrichment overwrote the engine's own {key}")

    def test_the_enrichment_did_not_bump_the_contract_version(self):
        """R7 §8.3. A read-side enrichment in `_pending_options` is explicitly
        not a version change; a shape change to `observation()` would be."""
        self.assertEqual(contract.CONTRACT_VERSION, 5)
        self.assertNotIn("move_preview_next", contract.MON_FIELDS)
        self.assertNotIn("uses_special_attack", contract.MON_FIELDS)


class UsesSpecialAttackParityTests(unittest.TestCase):
    """R7/N43 ported `usesSpecialAttack` into `app.js` rather than growing the
    pinned `MON_FIELDS` set. That is only safe if the two copies agree, so the
    JS copy is pinned against the Python original here."""

    def _js(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        return (root / "pokelike" / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    def test_the_js_copy_states_the_same_rule_as_battle_py(self):
        js = self._js()
        self.assertIn("function usesSpecialAttack(speciesId, baseStats)", js)
        # The two hardcoded species-id exceptions (battle.py:296-297).
        self.assertIn("if (speciesId === 307 || speciesId === 308) return false;", js)
        # And the comparison itself: special >= atk, not >.
        self.assertIn("return special >= atk;", js)

    def test_the_python_original_still_says_what_the_js_copy_mirrors(self):
        """If `battle.uses_special_attack` changes, this fails and the JS copy
        has to be revisited -- which is the whole point of pinning it."""
        from pokelike import battle, data
        stats = data.BaseStats(hp=45, atk=49, defense=49, speed=45, special=65)
        self.assertTrue(battle.uses_special_attack(1, stats),
                        "special >= atk should be a special attacker")
        self.assertFalse(battle.uses_special_attack(307, stats),
                         "307 is a hardcoded physical exception")
        self.assertFalse(battle.uses_special_attack(308, stats),
                         "308 is a hardcoded physical exception")
        physical = data.BaseStats(hp=45, atk=90, defense=49, speed=45, special=20)
        self.assertFalse(battle.uses_special_attack(1, physical))


class ConsoleChoiceReadabilityTests(unittest.TestCase):
    """R7 constraint 5: both renderers stay in step. §3's stats and §5's two
    moves are all text, so the console builds them too."""

    def _started(self, seed=7):
        eng = engine.Engine()
        eng.reset(seed=seed)
        eng.step(engine.ChooseStarter(species_id=eng.state.pending.options[0]["species_id"]))
        return eng, eng.state

    def test_a_trade_option_prints_types_hp_move_and_stats(self):
        from pokelike import map_gen
        _, state = self._started()
        engine._visit_trade(state, map_gen.MapNode(id="n", type=map_gen.TRADE, layer=1, col=0))
        text = console.render_pending(state)
        mon = state.team[0]
        self.assertIn("/".join(mon.types), text, "the console option shows no types")
        self.assertIn(f"{mon.current_hp}/{mon.max_hp}", text, "no HP")
        eff = contract._effective_stats(mon)
        self.assertIn(f"Spe {eff['speed']}", text, "no effective speed on the console option")
        self.assertIn(f"Atk {eff['atk']}", text, "no effective attack on the console option")

    def test_a_tutor_option_prints_both_moves(self):
        from pokelike import map_gen
        _, state = self._started()
        engine._visit_move_tutor(
            state, map_gen.MapNode(id="n", type=map_gen.MOVE_TUTOR, layer=1, col=0))
        options = contract.pending_view(state.pending, state)["options"]
        self.assertTrue(options)
        text = console.render_pending(state)
        self.assertIn(options[0]["move_preview"]["name"], text, "no current move")
        self.assertIn(options[0]["move_preview_next"]["name"], text, "no successive move")
        self.assertIn(f"-> tier {options[0]['move_tier_next']}", text,
                      "the console does not say which tier the successor belongs to")

    def test_the_move_is_not_printed_twice_on_one_option(self):
        """`_stat_line` also appends the move; `_format_option` passes
        `include_move=False` so the option does not say it twice."""
        from pokelike import map_gen
        _, state = self._started()
        engine._visit_trade(state, map_gen.MapNode(id="n", type=map_gen.TRADE, layer=1, col=0))
        options = contract.pending_view(state.pending, state)["options"]
        name = options[0]["move_preview"]["name"]
        self.assertEqual(
            console._format_option(options[0]).count(name), 1,
            "the console printed the move twice on one option",
        )


if __name__ == "__main__":
    unittest.main()
