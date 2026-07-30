"""Tests for pokelike/webui/state_json.py.

Only the pure encode/decode functions are unit-tested here (fast,
deterministic, no server needed) -- `server.py`'s HTTP routing/static
file serving was validated separately by driving the real API end-to-end
with scripted requests (see docs/webui.md), not via this suite.

Run with: python -m unittest pokelike.tests.test_webui -v
"""

from __future__ import annotations

import unittest

from pokelike import engine
from pokelike.webui.state_json import ActionDecodeError, decode_action, encode_state


def _fresh_state() -> engine.RunState:
    eng = engine.Engine()
    state = eng.reset(seed=1)
    starter_id = state.pending.options[0]["species_id"]
    return eng.step(engine.ChooseStarter(species_id=starter_id))


class EncodeStateTests(unittest.TestCase):
    def test_encodes_team_map_and_phase(self):
        state = _fresh_state()
        encoded = encode_state(state)
        self.assertEqual(encoded["phase"], "on_map")
        self.assertEqual(len(encoded["team"]), 1)
        mon = encoded["team"][0]
        self.assertEqual(mon["species_id"], state.team[0].species_id)
        self.assertEqual(mon["hp_pct"], 100.0)
        self.assertIsNotNone(encoded["map"])
        self.assertEqual(len(encoded["map"]["nodes"]), 23)  # fixed topology, docs/phase3-gym-design.md section 1.1
        self.assertEqual(encoded["map"]["current_node_id"], "n0_0")

    def test_pending_choice_round_trips_options(self):
        eng = engine.Engine()
        state = eng.reset(seed=1)
        encoded = encode_state(state)
        self.assertEqual(encoded["phase"], "choose_starter")
        self.assertEqual(encoded["pending"]["optional"], False)
        self.assertEqual(len(encoded["pending"]["options"]), 3)

    def test_no_pending_choice_encodes_as_none(self):
        state = _fresh_state()
        encoded = encode_state(state)
        self.assertIsNone(encoded["pending"])

    def test_log_total_is_monotonic_and_untrimmed(self):
        state = _fresh_state()
        for _ in range(3):
            state.log.append({"type": "start_map", "map_index": 0})
        encoded = encode_state(state, recent_log=1)
        self.assertEqual(len(encoded["log"]), 1)
        self.assertEqual(encoded["log_total"], len(state.log))


class DecodeActionTests(unittest.TestCase):
    def test_decodes_every_action_type(self):
        self.assertEqual(decode_action({"type": "ChooseStarter", "species_id": 1}), engine.ChooseStarter(species_id=1))
        self.assertEqual(decode_action({"type": "VisitNode", "node_id": "n1_0"}), engine.VisitNode(node_id="n1_0"))
        self.assertEqual(decode_action({"type": "AdvanceMap"}), engine.AdvanceMap())
        self.assertEqual(decode_action({"type": "SelectOption", "index": 2}), engine.SelectOption(index=2))
        self.assertEqual(decode_action({"type": "SelectOption", "index": None}), engine.SelectOption(index=None))
        self.assertEqual(decode_action({"type": "SelectOption"}), engine.SelectOption(index=None))
        # CODEX.md issue 39: ReorderTeam/UseItem/EquipItem previously had no
        # decode support at all (ActionDecodeError for all three).
        self.assertEqual(decode_action({"type": "ReorderTeam", "order": [1, 0, 2]}), engine.ReorderTeam(order=(1, 0, 2)))
        self.assertEqual(
            decode_action({"type": "UseItem", "item_index": 0, "target_index": 1}),
            engine.UseItem(item_index=0, target_index=1),
        )
        self.assertEqual(
            decode_action({"type": "EquipItem", "bag_index": 2, "team_index": 0}),
            engine.EquipItem(bag_index=2, team_index=0),
        )

    def test_rejects_malformed_payloads(self):
        with self.assertRaises(ActionDecodeError):
            decode_action({})
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "NotARealAction"})
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "VisitNode"})  # missing node_id
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "ChooseStarter"})  # missing species_id
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "ReorderTeam"})  # missing order
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "ReorderTeam", "order": []})  # empty order
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "UseItem", "item_index": 0})  # missing target_index
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "EquipItem", "bag_index": 0})  # missing team_index

    def test_malformed_scalars_raise_action_decode_error_not_value_error(self):
        # CODEX.md issue 47: a raw `int(...)` on attacker-controlled JSON
        # (a non-numeric string, a list, ...) must become an
        # `ActionDecodeError` (-> HTTP 400), never an uncaught ValueError.
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "ChooseStarter", "species_id": "not-a-number"})
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "SelectOption", "index": "nope"})
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "UseItem", "item_index": [1], "target_index": 0})
        with self.assertRaises(ActionDecodeError):
            decode_action({"type": "ReorderTeam", "order": [0, "x", 2]})


class ServerScalarCoercionTests(unittest.TestCase):
    """CODEX.md issue 47: `/api/reset`'s boolean/int fields must reject a
    malformed value instead of silently coercing it (Python truthiness
    turns the JSON string "false" into `True`)."""

    def test_to_bool_accepts_only_real_booleans(self):
        from pokelike.webui.server import _BadRequest, _to_bool

        self.assertTrue(_to_bool(True, "x"))
        self.assertFalse(_to_bool(False, "x"))
        with self.assertRaises(_BadRequest):
            _to_bool("false", "x")
        with self.assertRaises(_BadRequest):
            _to_bool(1, "x")
        with self.assertRaises(_BadRequest):
            _to_bool(None, "x")

    def test_to_int_rejects_bools_and_non_numeric_strings(self):
        from pokelike.webui.server import _BadRequest, _to_int

        self.assertEqual(_to_int(42, "x"), 42)
        self.assertEqual(_to_int("42", "x"), 42)
        with self.assertRaises(_BadRequest):
            _to_int(True, "x")
        with self.assertRaises(_BadRequest):
            _to_int("not-a-number", "x")
        with self.assertRaises(_BadRequest):
            _to_int(None, "x")


if __name__ == "__main__":
    unittest.main()
