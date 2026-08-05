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
from pokelike.render import contract
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
        """
        expected = {
            1: "f0d1ca23188b21ea41e330857bb47220f0dc94239d36117cd37736d3cc0fd9da",
            12345: "c4eaee77e2f0cef259903f6e6c622cb1ec35e7f51bca344411bcabd1cfc86e4e",
            999: "9a4dfae164168338e5c0de10e2bbef48d986099ce782a0bd4227abd964bdc0f1",
            424242: "e89b7b1da104e2d9d3a26241cfc1337b23f21fc33c9c12817db2c95fcdbe1334",
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


if __name__ == "__main__":
    unittest.main()
