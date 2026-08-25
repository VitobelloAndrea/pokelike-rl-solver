"""M7.0 — focused CROSS-RUNTIME fixtures for the M7 sweep's adapter surfaces.

The M7-A audit could validate the source-side adapter only by reading it and
by watching whole random episodes: there was no checked-in test that drives a
named affordance and asserts both what each runtime OFFERS and what each
runtime DOES. That is what this file is (`docs/audits/
M7-A-independent-tool-audit.md` §10 item 4).

Six affordances, one checked-in fixture each
(`route-oracle/fixtures/sweep/<goal>.json`, derived by
`route-oracle/find-sweep-fixtures.py`):

    six_member_team         a full six-member team, all 15 transpositions
    nonusable_bag_item      a non-usable bag item -> equip, never use
    usable_item_targets     a usable item whose targets come from the source
                            predicate `usableItemCanTarget`
    held_item_nonzero_slot  a held-item overlay opened from a NONZERO slot
                            while another member also holds
    move_tutor_gap          a mastered member filtered out, so `data-tutor`
                            team identity != normalized option position (T2)
    item_equip_exits        the three item-equip exits: target, bank, cancel

Every assertion goes through the tool's own lockstep loop
(`sweep.run_episode` with a forced action list), so:

  * the SOURCE side keeps using `sweep-adapter.js` — the source's own state,
    screens, `data-*` values and real click handlers;
  * the PYTHON side keeps using `engine.legal_actions` and real
    `Engine.step`;
  * neither side ever sees the other's answer, and nothing here builds a JS
    expectation out of Python output or a source action list out of the
    coverage manifest.

Unlike the rest of `pokelike/tests`, this file NEEDS `node` and the source
bundle. It skips (loudly) if either is unavailable rather than passing
vacuously.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROUTE_ORACLE = os.path.join(_ROOT, "route-oracle")
for _p in (_ROOT, _ROUTE_ORACLE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sweep  # noqa: E402

FIXTURE_DIR = os.path.join(_ROUTE_ORACLE, "fixtures", "sweep")
GOALS = ("six_member_team", "nonusable_bag_item", "usable_item_targets",
         "held_item_nonzero_slot", "move_tutor_gap", "item_equip_exits",
         # M7-COMBINED (A3): the three action families that had no focused
         # witness of their own. The six above are RETAINED unchanged.
         "starter_select", "node_visit_fanout", "map_advance")

# `starter_select`'s goal state IS the initial state, so its action list is
# legitimately empty -- see `find-sweep-fixtures.g_starter_select`.
EMPTY_ACTION_GOALS = frozenset({"starter_select"})


def _load(goal: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, goal + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _reason_to_skip() -> str | None:
    if shutil.which("node") is None:
        return "node is not on PATH"
    if not os.path.isfile(os.path.join(_ROOT, "pokelike_forked", "js",
                                       "bundle.deobfuscated.js")):
        return "the source bundle is absent"
    for goal in GOALS:
        if not os.path.isfile(os.path.join(FIXTURE_DIR, goal + ".json")):
            return (f"fixture {goal}.json is missing -- rederive with "
                    "python route-oracle/find-sweep-fixtures.py")
    return None


_SKIP = _reason_to_skip()


@unittest.skipIf(_SKIP is not None, str(_SKIP))
class _CrossRuntimeFixtureTest(unittest.TestCase):
    """Shared lockstep plumbing. One `node` host for the whole class; each
    replay resets it, which builds a brand-new VM context (see sweep-host.js),
    so episodes never leak into each other."""

    js: sweep.JsRuntime
    py: sweep.PyRuntime

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = sweep.JsRuntime()
        cls.py = sweep.PyRuntime()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.js.close()
        cls.py.close()

    # -- the one primitive every test below is built from -------------------

    def replay(self, fixture: dict, extra: tuple[dict, ...] = ()) -> dict:
        """Force `fixture["actions"] + extra` through the real lockstep loop.

        `run_episode` compares the two legal sets BEFORE each step and the two
        state projections AFTER each step, so a clean return is the claim that
        both runtimes agreed at every point — enumeration and execution alike.
        """
        actions = list(fixture["actions"]) + list(extra)
        spec = dict(fixture["config"])
        spec["episode_id"] = fixture["goal"]
        return sweep.run_episode(self.js, self.py, spec, None, len(actions) + 1,
                                 forced_actions=actions)

    def replay_clean(self, fixture: dict, extra: tuple[dict, ...] = ()) -> dict:
        ep = self.replay(fixture, extra)
        self.assertIsNone(ep["divergence"],
                          f"{fixture['goal']}: {json.dumps(ep['divergence'])[:1500]}")
        return ep

    def at_goal(self, goal: str) -> tuple[dict, list[dict], list[dict]]:
        """Replay a fixture to its goal state and hand back BOTH legal sets.

        The runtimes are left standing on the goal state: `run_episode` stops
        with `replay_exhausted` after enumerating and comparing, before it
        would have chosen anything.
        """
        fixture = _load(goal)
        ep = self.replay_clean(fixture)
        self.assertEqual(ep["outcome"], "replay_exhausted")
        js_legal = self.js.legal()["actions"]
        py_legal = self.py.legal()
        # Belt and braces: the loop already compared these, assert it here too
        # so a future change to `run_episode` cannot quietly weaken this file.
        self.assertIsNone(sweep.compare_legal(js_legal, py_legal))
        return fixture, js_legal, py_legal

    # -- small readers ------------------------------------------------------

    @staticmethod
    def of_kind(actions: list[dict], kind: str) -> list[dict]:
        return [a for a in actions if a["kind"] == kind]

    def assert_prov(self, actions: list[dict], needle: str) -> None:
        """The SOURCE side records why each action is legal, in source terms.
        Asserting on it is how this file checks the derivation rather than
        just the answer."""
        self.assertTrue(
            any(needle in str(a.get(sweep.PROV_KEY, "")) for a in actions),
            f"no source provenance mentioning {needle!r} in "
            f"{[a.get(sweep.PROV_KEY) for a in actions]}")


# ===========================================================================
# 1. A full six-member team, including every transposition
# ===========================================================================


class SixMemberTeamTests(_CrossRuntimeFixtureTest):

    def test_a_full_team_enumerates_all_fifteen_transpositions(self):
        fixture, js_legal, py_legal = self.at_goal("six_member_team")
        self.assertEqual(len(self.py.state.team), 6)
        expected = {(i, j) for i in range(6) for j in range(i + 1, 6)}
        for side, actions in (("js", js_legal), ("py", py_legal)):
            with self.subTest(side=side):
                pairs = {(a["i"], a["j"]) for a in self.of_kind(actions, "reorder_team")}
                self.assertEqual(pairs, expected)
                self.assertEqual(len(pairs), 15)
        # ...and the source's reason for each is the team-bar drag SWAP, which
        # is why the compared domain is transpositions (finding F1).
        self.assert_prov(self.of_kind(js_legal, "reorder_team"), "64798-64806")

    def test_every_transposition_executes_identically_on_both_runtimes(self):
        fixture = _load("six_member_team")
        for i in range(6):
            for j in range(i + 1, 6):
                with self.subTest(swap=(i, j)):
                    self.replay_clean(
                        fixture, ({"kind": "reorder_team", "i": i, "j": j},))

    def test_a_transposition_really_swaps_those_two_members(self):
        fixture, _, _ = self.at_goal("six_member_team")
        before = [m.species_id for m in self.py.state.team]
        self.replay_clean(fixture, ({"kind": "reorder_team", "i": 1, "j": 4},))
        after = [m.species_id for m in self.py.state.team]
        expected = list(before)
        expected[1], expected[4] = expected[4], expected[1]
        self.assertEqual(after, expected)
        # The source agreed: `checkpoint.team` is ordered and compared, so the
        # clean replay above already proves the JS team ended in this order.


# ===========================================================================
# 2. Bag items: the source's own usable/non-usable discriminator
# ===========================================================================


class BagItemTests(_CrossRuntimeFixtureTest):

    def test_a_non_usable_bag_item_routes_to_equip_and_never_to_use(self):
        fixture, js_legal, py_legal = self.at_goal("nonusable_bag_item")
        equips = self.of_kind(js_legal, "equip_item")
        self.assertTrue(equips)
        equip_bags = {a["bag_index"] for a in equips}
        use_bags = {a["bag_index"] for a in self.of_kind(js_legal, "use_item")}
        self.assertTrue(equip_bags - use_bags,
                        "the fixture no longer holds a non-usable bag item")
        # Both runtimes classified the same bag slots the same way.
        self.assertEqual(equip_bags,
                         {a["bag_index"] for a in self.of_kind(py_legal, "equip_item")})
        self.assertEqual(use_bags,
                         {a["bag_index"] for a in self.of_kind(py_legal, "use_item")})
        # And the source's reason is `equipItemFromBag`, not a table in the
        # adapter: `it.usable` is read off the object the source itself stored.
        self.assert_prov(equips, "equipItemFromBag")
        # Every team member is an equip target for a non-usable item (64950).
        bag = sorted(equip_bags - use_bags)[0]
        self.assertEqual({a["team_index"] for a in equips if a["bag_index"] == bag},
                         set(range(len(self.py.state.team))))

    def test_equipping_a_non_usable_item_executes_identically(self):
        fixture, js_legal, _ = self.at_goal("nonusable_bag_item")
        equips = self.of_kind(js_legal, "equip_item")
        use_bags = {a["bag_index"] for a in self.of_kind(js_legal, "use_item")}
        chosen = sorted((a for a in equips if a["bag_index"] not in use_bags),
                        key=sweep.canon_action)[-1]  # the LAST member, not slot 0
        bare = {k: v for k, v in chosen.items() if k != sweep.PROV_KEY}
        item_id = bare["item_id"]
        self.replay_clean(fixture, (bare,))
        held = self.py.state.team[bare["team_index"]].held_item
        self.assertEqual(getattr(held, "id", held), item_id)

    def test_a_usable_items_targets_come_from_the_source_predicate(self):
        fixture, js_legal, py_legal = self.at_goal("usable_item_targets")
        uses = self.of_kind(js_legal, "use_item")
        self.assertTrue(uses)
        by_item: dict[int, set[int]] = {}
        for a in uses:
            by_item.setdefault(a["bag_index"], set()).add(a["target_index"])
        team_size = len(self.py.state.team)
        self.assertTrue(
            any(len(t) < team_size for t in by_item.values()),
            "the fixture no longer has a usable item with a RESTRICTED target "
            "set, so this test would no longer distinguish the source "
            "predicate from 'every member'")
        # The gate is the source's own `usableItemCanTarget`, called by the
        # adapter rather than reimplemented in it.
        self.assert_prov(uses, "usableItemCanTarget")
        # Python reached the same restricted set from its own port of it.
        py_by_item: dict[int, set[int]] = {}
        for a in self.of_kind(py_legal, "use_item"):
            py_by_item.setdefault(a["bag_index"], set()).add(a["target_index"])
        self.assertEqual(by_item, py_by_item)

    def test_using_a_usable_item_executes_identically(self):
        fixture, js_legal, _ = self.at_goal("usable_item_targets")
        chosen = sorted(self.of_kind(js_legal, "use_item"), key=sweep.canon_action)[0]
        self.replay_clean(fixture,
                          ({k: v for k, v in chosen.items() if k != sweep.PROV_KEY},))


# ===========================================================================
# 3. The held-item overlay, opened from a nonzero slot
# ===========================================================================


class HeldItemOverlayTests(_CrossRuntimeFixtureTest):
    """`openItemEquipModal(mon.heldItem, {fromPokemonIdx: h})` renders one row
    per member, but the OPENING member's row carries `data-unequip` instead of
    `data-idx` (79521-79531). The `[data-idx]` list therefore has a hole in it:
    target `t` sits at position `t` when `t < h` and at `t - 1` when `t > h`.

    Addressing it by POSITION lands on the wrong member — and it is invisible
    on the two-member probe the M7 record's first M6 mutant used, where the
    only target is already at position 0. This fixture is chosen to make the
    two differ."""

    def test_the_overlay_opens_from_a_nonzero_slot_with_another_holder(self):
        fixture, js_legal, py_legal = self.at_goal("held_item_nonzero_slot")
        team = self.py.state.team
        holders = [i for i, m in enumerate(team) if m.held_item]
        self.assertGreaterEqual(len(team), 4)
        self.assertGreaterEqual(len(holders), 2)
        self.assertGreater(max(holders), 0)
        # Both runtimes offer unequip for exactly the members holding something.
        for side, actions in (("js", js_legal), ("py", py_legal)):
            with self.subTest(side=side):
                self.assertEqual(
                    {a["team_index"] for a in self.of_kind(actions, "unequip_item")},
                    set(holders))
        self.assert_prov(self.of_kind(js_legal, "unequip_item"), "data-unequip")

    def test_a_hand_off_whose_data_idx_value_differs_from_its_position(self):
        fixture, js_legal, py_legal = self.at_goal("held_item_nonzero_slot")
        team = self.py.state.team
        holders = [i for i, m in enumerate(team) if m.held_item]

        def position(from_index: int, to_index: int) -> int:
            return to_index if to_index < from_index else to_index - 1

        # `to_index <= len(team) - 2` makes the POSITION `to_index` a real
        # button belonging to somebody else, so a positional mis-map lands on
        # a valid WRONG member and shows up as a state divergence -- not
        # merely as an execution error on a button that does not exist.
        candidates = [a for a in self.of_kind(js_legal, "hand_off_item")
                      if a["from_index"] in holders and a["from_index"] > 0
                      and position(a["from_index"], a["to_index"]) != a["to_index"]
                      and a["to_index"] <= len(team) - 2]
        self.assertTrue(candidates,
                        "the fixture no longer distinguishes [data-idx] VALUE "
                        "from position, so a positional mis-map would be "
                        "behaviourally identical and this test vacuous")
        chosen = sorted(candidates, key=sweep.canon_action)[0]
        bare = {k: v for k, v in chosen.items() if k != sweep.PROV_KEY}
        src, dst = bare["from_index"], bare["to_index"]
        held_before = [getattr(m.held_item, "id", m.held_item) for m in team]

        self.replay_clean(fixture, (bare,))

        after = [getattr(m.held_item, "id", m.held_item) for m in self.py.state.team]
        expected = list(held_before)
        expected[src], expected[dst] = expected[dst], expected[src]
        self.assertEqual(after, expected)
        # The clean replay is the source's agreement: `checkpoint.team[*]
        # .held_item` is a compared field, so JS put the item on the same
        # member. Naming the member a positional mis-map WOULD have hit makes
        # that concrete: `ov.buttons[dst]` is the row of team member
        # `value_at`, which exists, is not `dst`, and would have received the
        # item instead.
        value_at = dst if dst < src else dst + 1
        self.assertNotEqual(value_at, dst)
        self.assertLess(value_at, len(team))
        self.assertEqual(after[value_at], held_before[value_at])

    def test_unequip_from_a_nonzero_holder_executes_identically(self):
        fixture, js_legal, _ = self.at_goal("held_item_nonzero_slot")
        holder = max(a["team_index"]
                     for a in self.of_kind(js_legal, "unequip_item"))
        self.assertGreater(holder, 0)
        bag_before = list(self.py.state.items)
        item = self.py.state.team[holder].held_item
        item_id = getattr(item, "id", item)
        self.replay_clean(fixture, ({"kind": "unequip_item", "team_index": holder},))
        self.assertIsNone(self.py.state.team[holder].held_item)
        self.assertEqual([getattr(i, "id", i) for i in self.py.state.items],
                         [getattr(i, "id", i) for i in bag_before] + [item_id])


# ===========================================================================
# 4. The move tutor — finding T2
# ===========================================================================


class MoveTutorTests(_CrossRuntimeFixtureTest):
    """`doMoveTutorNode` (80464-80563) builds one `[data-tutor]` button per
    NON-mastered member. The attribute's value is the member's TEAM index; the
    normalized `select_option.index` is the POSITION in the agreed option
    list. The two differ the moment a mastered member is filtered out."""

    def test_a_mastered_member_creates_a_gap_between_identity_and_position(self):
        fixture, js_legal, py_legal = self.at_goal("move_tutor_gap")
        pending = self.py.state.pending
        self.assertIsNotNone(pending)
        team_indices = [o["team_index"] for o in pending.options]
        self.assertTrue(any(t != i for i, t in enumerate(team_indices)),
                        f"the fixture no longer has a gap: {team_indices}")
        # Both runtimes enumerate POSITIONS 0..n-1 plus the skip button, and
        # neither leaks the team index into `index`.
        expected = {(i, False) for i in range(len(pending.options))} | {(None, False)}
        for side, actions in (("js", js_legal), ("py", py_legal)):
            with self.subTest(side=side):
                self.assertEqual(
                    {(a["index"], bool(a.get("cancel")))
                     for a in self.of_kind(actions, "select_option")},
                    expected)
        # The source provenance still records the real `data-tutor` value, so
        # the derivation stays auditable even though it is not the index.
        self.assert_prov(js_legal, f'data-tutor="{team_indices[0]}"')

    def test_choosing_position_zero_tutors_the_member_it_names(self):
        fixture, _, _ = self.at_goal("move_tutor_gap")
        pending = self.py.state.pending
        target = pending.options[0]["team_index"]
        tiers = [m.move_tier for m in self.py.state.team]
        self.replay_clean(fixture,
                          ({"kind": "select_option", "index": 0, "cancel": False},))
        after = [m.move_tier for m in self.py.state.team]
        expected = list(tiers)
        expected[target] = min(2, expected[target] + 1)
        self.assertEqual(after, expected)
        # Every member NOT named by option 0 is untouched — this is the
        # assertion a `byDataValue(..., 'tutor', index)` mis-map fails.
        for i, (was, now) in enumerate(zip(tiers, after)):
            if i != target:
                self.assertEqual(was, now)

    def test_skipping_the_tutor_executes_identically(self):
        fixture, _, _ = self.at_goal("move_tutor_gap")
        tiers = [m.move_tier for m in self.py.state.team]
        self.replay_clean(
            fixture, ({"kind": "select_option", "index": None, "cancel": False},))
        self.assertEqual([m.move_tier for m in self.py.state.team], tiers)


# ===========================================================================
# 5. The three item-equip exits
# ===========================================================================


class ItemEquipExitTests(_CrossRuntimeFixtureTest):
    """`openItemEquipModal` has three genuinely different exits (79535-79569):

        [data-idx]         equip/swap onto that member
        #btn-equip-to-bag  bank / return the item
        #btn-equip-cancel  neither — the whole handler body is `B2O.remove()`

    The third is the one M7's single `engine.py` declaration change is about.
    """

    def test_all_three_exits_are_offered_by_both_runtimes(self):
        fixture, js_legal, py_legal = self.at_goal("item_equip_exits")
        for side, actions in (("js", js_legal), ("py", py_legal)):
            with self.subTest(side=side):
                opts = self.of_kind(actions, "select_option")
                picks = [a for a in opts if a["index"] is not None]
                banks = [a for a in opts
                         if a["index"] is None and not a.get("cancel")]
                cancels = [a for a in opts if a.get("cancel")]
                self.assertGreaterEqual(len(picks), 2)
                self.assertEqual(len(banks), 1)
                self.assertEqual(len(cancels), 1)
        self.assert_prov(js_legal, "btn-equip-to-bag")
        self.assert_prov(js_legal, "btn-equip-cancel")
        self.assert_prov(js_legal, "data-idx")

    def test_the_target_exit_executes_identically(self):
        fixture, _, _ = self.at_goal("item_equip_exits")
        self.replay_clean(fixture,
                          ({"kind": "select_option", "index": 1, "cancel": False},))

    def test_the_bank_exit_executes_identically(self):
        fixture, _, _ = self.at_goal("item_equip_exits")
        self.replay_clean(
            fixture, ({"kind": "select_option", "index": None, "cancel": False},))

    def test_the_cancel_exit_executes_identically(self):
        """M7-COMBINED (F-A): the cancel exit is now CLEAN cross-runtime.

        Under M7.0 this test asserted the opposite — that executing the cancel
        exit still reproduced finding F-A with the signature
        `["state", "checkpoint.pending", "checkpoint.screen"]` — because the
        defect was real and out of scope then. It has since been repaired in
        the runtime (`engine._resolve_item_equip_choice`), not in this adapter:
        `#btn-equip-cancel`'s whole handler body is `B2O.remove()`
        (bundle.deobfuscated.js:79563-79569), so the source stays on
        `item-screen` with `doItemNode`'s offer still live, and the port now
        restores that same offer instead of dropping to `ON_MAP`.

        The inversion is the point of the test, so the two properties it
        would be easiest to lose are asserted explicitly below rather than
        left implicit in `replay_clean`.
        """
        fixture = _load("item_equip_exits")
        ep = self.replay_clean(
            fixture, ({"kind": "select_option", "index": None, "cancel": True},))
        self.assertEqual(ep["outcome"], "replay_exhausted")

        # Both runtimes are standing on the RESTORED item offer, not the map.
        js_state = self.js.state(0, {})
        py_state = self.py.projection(0, {})
        for side, projection in (("js", js_state), ("py", py_state)):
            with self.subTest(side=side):
                cp = projection["checkpoint"]
                self.assertEqual(cp["screen"], "item-screen")
                self.assertIsNotNone(cp["pending"])
                self.assertEqual(cp["pending"]["phase"], "item_choice")

        # And the offer is live: both sides re-offer the same picks.
        self.assertIsNone(sweep.compare_legal(self.js.legal()["actions"],
                                              self.py.legal()))

    def test_taking_the_cancel_exit_earns_the_cancel_coverage_target(self):
        """`exit.cancel` was unearnable while F-A stood: `observe_coverage`
        only credits a COMPLETED step, and the cancel step never completed.
        This is the behavioural half of closing that coverage gap."""
        fixture = _load("item_equip_exits")
        actions = list(fixture["actions"]) + [
            {"kind": "select_option", "index": None, "cancel": True}]
        spec = dict(fixture["config"])
        spec["episode_id"] = "item_equip_exits_cancel_coverage"

        # The ledger is filled by `run_episode` itself, from the OBSERVED
        # agreed step -- the same path the real sweep uses. Nothing is
        # credited here by hand.
        ledger = sweep.CoverageLedger(sweep.load_targets())
        ep = sweep.run_episode(self.js, self.py, spec, None, len(actions) + 1,
                               ledger=ledger, forced_actions=actions)
        self.assertIsNone(ep["divergence"],
                          f"{json.dumps(ep['divergence'])[:1500]}")
        self.assertIn("exit.cancel", ledger.earned)
        self.assertEqual(ledger.earned["exit.cancel"]["source"], "sweep")


# ===========================================================================
# 6. The three families that had no witness of their own (M7-COMBINED A3)
# ===========================================================================


class StarterSelectTests(_CrossRuntimeFixtureTest):
    """`choose_starter` was exercised by every other fixture's first action and
    witnessed by none of them. Its goal state is the initial state, so this
    fixture's action list is empty by construction."""

    def test_both_runtimes_offer_the_same_three_starters(self):
        fixture, js_legal, py_legal = self.at_goal("starter_select")
        for side, actions in (("js", js_legal), ("py", py_legal)):
            with self.subTest(side=side):
                starters = self.of_kind(actions, "choose_starter")
                self.assertEqual(len(starters), 3)
                self.assertEqual(len(actions), 3,
                                 "nothing else is offered on the starter screen")
        js_ids = sorted(a["species_id"] for a in self.of_kind(js_legal, "choose_starter"))
        py_ids = sorted(a["species_id"] for a in self.of_kind(py_legal, "choose_starter"))
        self.assertEqual(js_ids, py_ids)
        self.assert_prov(js_legal, "selectStarter")

    def test_choosing_each_offered_starter_executes_identically(self):
        fixture, _, py_legal = self.at_goal("starter_select")
        for a in self.of_kind(py_legal, "choose_starter"):
            with self.subTest(species_id=a["species_id"]):
                self.replay_clean(fixture, ({"kind": "choose_starter",
                                             "species_id": a["species_id"]},))


class NodeVisitTests(_CrossRuntimeFixtureTest):
    """`visit_node` likewise. A fan-out of at least two is what makes the
    enumeration non-degenerate -- with one accessible node, "enumerated the
    accessible set" and "enumerated everything" cannot be told apart."""

    def test_the_accessible_set_is_a_strict_subset_of_the_map(self):
        fixture, js_legal, py_legal = self.at_goal("node_visit_fanout")
        offered = {a["node_id"] for a in self.of_kind(py_legal, "visit_node")}
        self.assertGreaterEqual(len(offered), 2)
        all_nodes = set(self.py.state.map.nodes)
        self.assertTrue(offered < all_nodes,
                        "a whole-map enumeration would prove nothing")
        js_offered = {a["node_id"] for a in self.of_kind(js_legal, "visit_node")}
        self.assertEqual(js_offered, offered)
        self.assert_prov(js_legal, "onNodeClick")

    def test_every_offered_node_is_accessible_and_unvisited(self):
        fixture, _, py_legal = self.at_goal("node_visit_fanout")
        for a in self.of_kind(py_legal, "visit_node"):
            node = self.py.state.map.nodes[a["node_id"]]
            with self.subTest(node=a["node_id"]):
                self.assertTrue(node.accessible)
                self.assertFalse(node.visited)

    def test_each_offered_node_executes_identically(self):
        fixture, _, py_legal = self.at_goal("node_visit_fanout")
        for a in self.of_kind(py_legal, "visit_node"):
            with self.subTest(node=a["node_id"]):
                self.replay_clean(fixture, ({"kind": "visit_node",
                                             "node_id": a["node_id"]},))


class MapAdvanceTests(_CrossRuntimeFixtureTest):
    """`advance_map` reached NONE of the six M7.0 fixtures, because getting to
    the badge screen means actually beating the map's gym leader."""

    def test_the_badge_screen_offers_exactly_one_affordance(self):
        fixture, js_legal, py_legal = self.at_goal("map_advance")
        for side, actions in (("js", js_legal), ("py", py_legal)):
            with self.subTest(side=side):
                self.assertEqual([a["kind"] for a in actions], ["advance_map"])
        self.assert_prov(js_legal, "btn-next-map")

    def test_the_run_really_earned_a_badge_to_get_here(self):
        """Guards against the fixture drifting onto some other single-action
        screen that happens to normalize the same way."""
        self.at_goal("map_advance")
        self.assertEqual(self.py.state.phase.value, "next_map_ready")
        self.assertGreaterEqual(self.py.state.badges, 1)

    def test_advancing_executes_identically_and_moves_to_the_next_map(self):
        fixture, _, _ = self.at_goal("map_advance")
        before = self.py.state.current_map
        self.replay_clean(fixture, ({"kind": "advance_map"},))
        self.assertEqual(self.py.state.current_map, before + 1)
        self.assertEqual(self.py.state.phase.value, "on_map")


class ActionFamilyWitnessTests(unittest.TestCase):
    """M7-COMBINED (A3): the denominator side of the fixture evidence.

    The requirement is not "there are nine fixtures", it is that EVERY action
    family in the normalized vocabulary has a checked-in, source-derived
    witness. This test derives the family list from the tool itself and the
    coverage manifest -- both directions -- so a family added to the
    vocabulary and not to the fixtures fails here rather than quietly losing
    its witness.
    """

    #: goal -> the families that goal witnesses (enumeration AND execution).
    WITNESSED = {
        "starter_select": {"choose_starter"},
        "node_visit_fanout": {"visit_node"},
        "map_advance": {"advance_map"},
        "six_member_team": {"reorder_team"},
        "nonusable_bag_item": {"equip_item"},
        "usable_item_targets": {"use_item"},
        "held_item_nonzero_slot": {"unequip_item", "hand_off_item"},
        "move_tutor_gap": {"select_option"},
        "item_equip_exits": {"select_option"},
    }

    def test_every_action_family_has_a_fixture_witness(self):
        manifest_path = os.path.join(_ROUTE_ORACLE, "sweep-targets.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        families = {t["action_kind"] for t in manifest["targets"]
                    if t.get("action_kind")}
        self.assertTrue(families, "the manifest must name action families")
        witnessed = set().union(*self.WITNESSED.values())
        self.assertEqual(families - witnessed, set(),
                         "an action family with no checked-in fixture witness")

    def test_the_witness_map_names_only_real_goals(self):
        self.assertEqual(set(self.WITNESSED), set(GOALS))

    @unittest.skipIf(_SKIP is not None, str(_SKIP))
    def test_each_claimed_witness_really_appears_in_that_fixture(self):
        """The claim above is checked against the fixtures themselves: the
        witnessing family must show up in the goal state's own recorded
        `legal_kinds`, or in the action list that reached it."""
        for goal, families in self.WITNESSED.items():
            fx = _load(goal)
            seen = set(fx["witness"]["legal_kinds"])
            seen |= {a["kind"] for a in fx["actions"]}
            for family in families:
                with self.subTest(goal=goal, family=family):
                    self.assertIn(family, seen)


# ===========================================================================
# 7. Fixture hygiene
# ===========================================================================


@unittest.skipIf(_SKIP is not None, str(_SKIP))
class FixtureManifestTests(unittest.TestCase):
    """The fixtures are DERIVED evidence, not hand-written routes; these are
    the properties that keep them readable and re-derivable."""

    def test_every_goal_has_a_fixture_with_the_declared_shape(self):
        for goal in GOALS:
            with self.subTest(goal=goal):
                fx = _load(goal)
                self.assertEqual(fx["goal"], goal)
                self.assertEqual(fx["derived_by"],
                                 "route-oracle/find-sweep-fixtures.py")
                if goal in EMPTY_ACTION_GOALS:
                    self.assertEqual(fx["actions"], [])
                else:
                    self.assertTrue(fx["actions"])
                self.assertTrue(fx["description"])
                self.assertIn("seed", fx["config"])
                self.assertIn("mode", fx["config"])
                self.assertTrue(fx["verification"]["clean"])
                self.assertTrue(fx["verification"]["goal_reached"])
                for path, digest in fx["protected_hashes"].items():
                    self.assertRegex(digest, r"^[0-9a-f]{64}$", path)

    def test_every_action_in_every_fixture_is_in_the_normalized_vocabulary(self):
        kinds = {"choose_starter", "advance_map", "visit_node", "select_option",
                 "use_item", "equip_item", "unequip_item", "hand_off_item",
                 "reorder_team"}
        for goal in GOALS:
            fx = _load(goal)
            for i, action in enumerate(fx["actions"]):
                with self.subTest(goal=goal, step=i):
                    self.assertIn(action["kind"], kinds)
                    self.assertNotIn(sweep.PROV_KEY, action)

    def test_the_fixtures_cover_more_than_one_mode_and_generation(self):
        modes = {json.dumps(_load(g)["config"]["mode"], sort_keys=True) for g in GOALS}
        self.assertGreater(len(modes), 1)


# ===========================================================================
# 10. F-F -- the Elite Four gauntlet, and the virtual clock it needs
# ===========================================================================
#
# F-F was reported as `apply_error_asymmetry` / "pump did not quiesce after
# 5000 rounds" on the `visit_node` that enters map 8. It was NOT a port defect
# and NOT an adapter-boundary error: the gauntlet ran to completion,
# `showWinScreen` (bundle.deobfuscated.js:81631) came up, and the harness then
# refused to settle ON the win screen.
#
# The cause is a stopped clock. `showWinScreen` awards pokedollars (81649) ->
# `onPokedollarsGained` (75236) -> `animatePokedollarGain` (75242) ->
# `_spawnPokedollarBurst` (75281), whose coin-fly loop re-arms
# `requestAnimationFrame(B2Q)` (75399) for exactly as long as any coin has
# `(_pdNow() - start - delay) / dur < 1` (75343-75360). `_pdNow` (75189)
# prefers `performance.now()`, which `sandbox.js:265` pins at the constant 0,
# and `driver.js`'s old `setTimeout` shim discarded the requested delay
# outright. So the ratio was never 1, no coin ever completed, and a finite
# wall-clock-bounded presentation animation became non-terminating -- in the
# harness only. `showWinScreen` is the ONLY `addPokedollars` call site the
# declared Story/Nuzlocke surface reaches (81544 is Endless-gated; 87540,
# 87725, 87868 and 89108 are egg/Endless/Challenges/pokechain), which is
# exactly why no other route ever tripped it.
#
# The repair is in `driver.js`: the virtual timer queue now carries a virtual
# clock, `performance.now()` reads it, and `requestAnimationFrame` schedules a
# real frame interval and passes the timestamp its contract requires. FIFO
# order and the pinned `Date.now` are deliberately unchanged, and so is the
# 5000-round safety bound.


@unittest.skipIf(shutil.which("node") is None, "node is not on PATH")
class DriverVirtualClockTests(unittest.TestCase):
    """The timer model, tested against the REAL `driver.js` text.

    The block under test is sliced out of `route-oracle/driver.js` itself
    rather than restated here, so this cannot pass against a driver that no
    longer contains it.
    """

    def _run(self, body: str) -> dict:
        import subprocess
        import tempfile

        with open(os.path.join(_ROUTE_ORACLE, "driver.js"),
                  encoding="utf-8", newline="") as fh:
            driver = fh.read()
        start = driver.index("  var timerQueue = [];")
        end_needle = "throw new Error('pump did not quiesce after 5000 rounds');"
        end = driver.index("}", driver.index(end_needle) + len(end_needle)) + 1
        block = driver[start:end]
        # The slice must really be the shim PLUS the pump, not some prefix of
        # it that happens to cut cleanly.
        for needle in ("setTimeout = function", "requestAnimationFrame = function",
                       "async function pump()", "for (var t of due)"):
            self.assertIn(needle, block, "driver.js's timer block moved")

        script = (
            "'use strict';\n"
            "var OUT = { notes: [] };\n"
            "var currentScreen = 'none';\n"
            "var performance = { now: function () { return 0; } };\n"
            "var setTimeout, setInterval, clearTimeout, clearInterval;\n"
            "var requestAnimationFrame, cancelAnimationFrame;\n"
            "(async function () {\n"
            + block + "\n"
            "  var RESULT = {};\n"
            + body + "\n"
            "  process.stdout.write(JSON.stringify(RESULT));\n"
            "})();\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clock.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(script)
            proc = subprocess.run(["node", path], capture_output=True, text=True,
                                  encoding="utf-8", timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[:2000])
        return json.loads(proc.stdout)

    def test_the_clock_starts_at_zero_and_advances_by_the_requested_delay(self):
        """The whole defect in one assertion: a callback must observe its own
        delay as elapsed. Under the old shim the delay argument was discarded
        and `performance.now()` stayed the `sandbox.js` constant 0."""
        out = self._run(
            "  RESULT.before = performance.now();\n"
            "  var seen = null;\n"
            "  setTimeout(function () { seen = performance.now(); }, 2000);\n"
            "  await pump();\n"
            "  RESULT.seen = seen;\n"
            "  RESULT.after = performance.now();\n"
        )
        self.assertEqual(out["before"], 0)
        self.assertEqual(out["seen"], 2000)
        self.assertEqual(out["after"], 2000)

    def test_the_clock_is_monotonic_and_never_rewinds(self):
        out = self._run(
            "  var stamps = [];\n"
            "  setTimeout(function () { stamps.push(performance.now()); }, 5000);\n"
            "  setTimeout(function () { stamps.push(performance.now()); }, 10);\n"
            "  await pump();\n"
            "  RESULT.stamps = stamps;\n"
        )
        self.assertEqual(out["stamps"], [5000, 5000])

    def test_callbacks_still_run_in_fifo_scheduling_order_not_due_order(self):
        """Deliberately unchanged, and load-bearing: the frozen 29-scenario
        signature was produced under FIFO. A delay affects the clock a
        callback observes, never when it runs."""
        out = self._run(
            "  var order = [];\n"
            "  setTimeout(function () { order.push('slow'); }, 9000);\n"
            "  setTimeout(function () { order.push('fast'); }, 0);\n"
            "  await pump();\n"
            "  RESULT.order = order;\n"
        )
        self.assertEqual(out["order"], ["slow", "fast"])

    def test_raf_advances_one_frame_and_passes_the_timestamp(self):
        """The rAF contract the old shim silently omitted: it invoked the
        callback with no arguments at all."""
        out = self._run(
            "  var ts = [];\n"
            "  requestAnimationFrame(function (t) { ts.push(t); });\n"
            "  await pump();\n"
            "  requestAnimationFrame(function (t) { ts.push(t); });\n"
            "  await pump();\n"
            "  RESULT.ts = ts;\n"
        )
        self.assertEqual(len(out["ts"]), 2)
        self.assertAlmostEqual(out["ts"][0], 1000 / 60, places=6)
        self.assertAlmostEqual(out["ts"][1], 2 * (1000 / 60), places=6)

    def test_a_clock_driven_animation_loop_retires(self):
        """THE F-F REGRESSION, in the shape `_spawnPokedollarBurst` actually
        has (75343-75360): re-arm rAF while any item's
        `(now - start - delay) / dur` is still below 1. Under the old
        frozen-clock shim this never terminated and `pump()` threw.

        The numbers are the source's own worst case for a Story Gen1 win:
        13 coins `B2y * 0x2d` ms apart (75327) with `dur` up to
        `0x26c + 0x118` (75328).
        """
        out = self._run(
            "  var items = [];\n"
            "  for (var i = 0; i < 13; i++) items.push({ delay: i * 45, dur: 900, done: false });\n"
            "  var start = performance.now();\n"
            "  var frames = 0;\n"
            "  function frame() {\n"
            "    frames++;\n"
            "    var pending = false;\n"
            "    for (var k = 0; k < items.length; k++) {\n"
            "      var it = items[k];\n"
            "      if (it.done) continue;\n"
            "      var el = performance.now() - start - it.delay;\n"
            "      if (el < 0) { pending = true; continue; }\n"
            "      if (el / it.dur >= 1) { it.done = true; continue; }\n"
            "      pending = true;\n"
            "    }\n"
            "    if (pending) requestAnimationFrame(frame);\n"
            "  }\n"
            "  requestAnimationFrame(frame);\n"
            "  await pump();\n"
            "  RESULT.frames = frames;\n"
            "  RESULT.allDone = items.every(function (it) { return it.done; });\n"
        )
        self.assertTrue(out["allDone"])
        # It retires on its own frame count, comfortably inside the bound --
        # the bound is not what stops it.
        self.assertGreater(out["frames"], 1)
        self.assertLess(out["frames"], 200)

    def test_the_bound_still_fails_loudly_on_a_genuinely_endless_loop(self):
        """The safety bound is preserved, not raised, removed or swallowed. A
        loop that re-arms with NO termination condition -- which is what F-F
        was mistaken for -- must still trip it.
        """
        out = self._run(
            "  function forever() { requestAnimationFrame(forever); }\n"
            "  requestAnimationFrame(forever);\n"
            "  try { await pump(); RESULT.threw = null; }\n"
            "  catch (e) { RESULT.threw = e.message; }\n"
        )
        self.assertEqual(out["threw"], "pump did not quiesce after 5000 rounds")

    def test_the_shim_does_not_touch_date_now(self):
        """`driver.js` pins `Date.now` to `SC.seed` so `startNewRun`'s own seed
        expression (75455) computes the episode's seed. The clock repair must
        leave it alone, or every run's seed would move."""
        out = self._run(
            "  Date.now = function () { return 4242; };\n"
            "  var seen = null;\n"
            "  setTimeout(function () { seen = Date.now(); }, 7000);\n"
            "  await pump();\n"
            "  RESULT.seen = seen;\n"
        )
        self.assertEqual(out["seen"], 4242)


class EliteFourGauntletTests(_CrossRuntimeFixtureTest):
    """The two retained F-F reproducers, replayed through the real lockstep
    loop.

    This is the cross-runtime half of the F-F regression. Both records fail
    under the pre-repair driver with `apply_error_asymmetry` at `visit_node
    n8_0`, and neither can be made to pass by anything short of the gauntlet
    genuinely completing, and agreeing, on both runtimes.
    """

    RECORDS = ("M7-divergence-hunt_story_gen1_0626.json",
               "M7-divergence-hunt_story_gen1_0698.json")

    def _record(self, name: str) -> dict:
        path = os.path.join(_ROUTE_ORACLE, "findings", name)
        if not os.path.isfile(path):
            self.skipTest("retained reproducer " + name + " is absent")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _replay(self, name: str, ledger=None) -> dict:
        record = self._record(name)
        # The premise: this record really is the Elite Four one, and its
        # recorded divergence really is the pump deadlock it was filed for.
        self.assertEqual(record["divergence"]["kind"], "apply_error_asymmetry")
        self.assertEqual(record["divergence"]["detail"]["action"],
                         {"kind": "visit_node", "node_id": "n8_0"})
        self.assertIn("pump did not quiesce", record["divergence"]["detail"]["js"])
        self.assertIsNone(record["divergence"]["detail"]["py"],
                          "the Python side never errored -- this was the harness")

        spec = dict(record["config"])
        spec["episode_id"] = record["episode_id"]
        return sweep.run_episode(self.js, self.py, spec, None,
                                 record["max_steps"], ledger=ledger,
                                 forced_actions=record["actions"])

    def test_both_reproducers_replay_with_no_divergence(self):
        for name in self.RECORDS:
            with self.subTest(record=name):
                ep = self._replay(name)
                self.assertIsNone(ep["divergence"],
                                  json.dumps(ep["divergence"])[:2000])

    def test_both_reproducers_still_reach_the_elite_four_node(self):
        """A repair that made the records stop REACHING `n8_0` would replay
        clean and prove nothing."""
        for name in self.RECORDS:
            with self.subTest(record=name):
                ep = self._replay(name)
                visited = [s["action"]["node_id"] for s in ep["steps"]
                           if s["action"]["kind"] == "visit_node"]
                self.assertIn("n8_0", visited)

    def test_both_reproducers_run_the_gauntlet_through_to_the_win_screen(self):
        """The step that used to deadlock now completes, and both runtimes
        agree on the terminal screen. `run_episode` compares the checkpoint,
        the battle evidence and the RNG totals after every action, so a clean
        step ending on `win-screen` is BOTH runtimes' answer, not one side's.
        """
        for name in self.RECORDS:
            with self.subTest(record=name):
                ep = self._replay(name)
                self.assertEqual(ep["outcome"], "win")
                self.assertEqual(ep["steps"][-1]["screen"], "win-screen")
                self.assertEqual(ep["steps"][-1]["action"],
                                 {"kind": "visit_node", "node_id": "n8_0"})

    def test_the_gauntlet_step_compares_five_agreeing_battles(self):
        """The Elite Four is five sequential fights inside ONE `visit_node`.
        Comparing that step at all is the point: while it deadlocked, its
        battle evidence and RNG position had never been compared even once.

        `run_episode` compares `battles[*]` -- winner, rounds, per-battle RNG
        draws, rosters, final HP/status, participants and the per-turn attack
        projection -- after every action, so reading the count off the
        SOURCE's own post-episode state and finding the roster length there is
        a statement about a comparison that already succeeded.
        """
        expected = len(sweep.data.get_elite_four(1))
        for name in self.RECORDS:
            with self.subTest(record=name):
                ep = self._replay(name)
                self.assertIsNone(ep["divergence"])
                # Both runtimes, asked independently for every battle they
                # have ever run, must report the same final one.
                js_all = self.js.state(0, {"phase": "final"})
                py_all = self.py.projection(0, {"phase": "final"})
                self.assertEqual(js_all["battles_total"], py_all["battles_total"])
                self.assertGreaterEqual(len(js_all["battles"]), expected)
                # The last `expected` battles are the gauntlet, and the source
                # won all of them -- otherwise `doElite4` would have called
                # `showGameOver` instead of `showWinScreen`.
                for battle in js_all["battles"][-expected:]:
                    self.assertTrue(battle["player_won"])
                self.assertGreater(ep["steps"][-1]["rng"]["draws"], 0)

    def test_the_win_screen_earns_its_two_required_targets(self):
        """`outcome.win` and `phase.win-screen` are credited by
        `observe_coverage` from the OBSERVED agreed step, never from the
        episode's intent. They are the two required targets F-F blocked.
        """
        for name in self.RECORDS:
            with self.subTest(record=name):
                ledger = sweep.CoverageLedger(sweep.load_targets())
                ep = self._replay(name, ledger=ledger)
                self.assertIsNone(ep["divergence"])
                earned = set(ledger.earned)
                self.assertIn("outcome.win", earned)
                self.assertIn("phase.win-screen", earned)



# ===========================================================================
# 11. `replay-set` -- accounting for what a reproducer replay actually observes
# ===========================================================================


class ReplaySetAccountingTests(_CrossRuntimeFixtureTest):
    """`replay` re-ran one record and reported only whether it still diverged,
    discarding everything the replay observed. `replay-set` merges those
    observations into a coverage number.

    The whole risk in that addition is that a record file could become
    evidence for its own contents. These tests exist to show it cannot: credit
    still comes from `observe_coverage` on a COMPLETED, agreed step, so a
    record whose action list stops short of the state earns nothing for it.
    """

    RECORDS = ("M7-divergence-hunt_story_gen1_0626.json",
               "M7-divergence-hunt_story_gen1_0698.json")

    def _paths(self) -> list[str]:
        paths = [os.path.join(_ROUTE_ORACLE, "findings", n) for n in self.RECORDS]
        for path in paths:
            if not os.path.isfile(path):
                self.skipTest("retained reproducers are absent")
        return paths

    def test_the_two_f_f_records_earn_the_win_targets_from_observed_steps(self):
        ledger = sweep.CoverageLedger(sweep.load_targets())
        result = sweep.replay_records(self._paths(), ledger)

        # Every record replayed clean and ran to a win on BOTH runtimes.
        for row in result["replay_set"]:
            self.assertFalse(row["reproduced"], row["record"])
            self.assertEqual(row["outcome"], "win", row["record"])

        earned = result["coverage"]["earned"]
        for tid in ("outcome.win", "phase.win-screen"):
            self.assertIn(tid, earned)
            # ...and cited to a real episode/step, not to the record file.
            self.assertRegex(earned[tid]["first"], r"^hunt_story_gen1_\d+#\d+$")
            self.assertEqual(earned[tid]["source"], "sweep")

    def test_a_record_truncated_before_the_gauntlet_earns_neither(self):
        """THE ANTI-FAKE TEST. Same record file, same config, same everything
        except that the action list stops one action short of the `visit_node
        n8_0` that reaches the win screen. If credit came from the record's
        existence rather than from the observed step, this would still earn
        both targets.
        """
        path = self._paths()[0]
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        actions = record["actions"]
        self.assertEqual(actions[-1], {"kind": "visit_node", "node_id": "n8_0"},
                         "the record's last action is the gauntlet entry")

        spec = dict(record["config"])
        spec["episode_id"] = record["episode_id"]
        ledger = sweep.CoverageLedger(sweep.load_targets())
        ep = sweep.run_episode(self.js, self.py, spec, None, record["max_steps"],
                               ledger=ledger, forced_actions=actions[:-1])
        self.assertIsNone(ep["divergence"])
        self.assertNotEqual(ep["outcome"], "win")
        for tid in ("outcome.win", "phase.win-screen"):
            self.assertNotIn(tid, ledger.earned)

    def test_the_replay_set_is_deterministic(self):
        """Two invocations of the same record set must agree on every episode
        digest and on the whole earned set -- no clock, no batch position, no
        path-order sensitivity."""
        paths = self._paths()
        first = sweep.replay_records(paths, sweep.CoverageLedger(sweep.load_targets()))
        second = sweep.replay_records(list(reversed(paths)),
                                      sweep.CoverageLedger(sweep.load_targets()))
        self.assertEqual([r["episode_digest"] for r in first["replay_set"]],
                         [r["episode_digest"] for r in second["replay_set"]])
        self.assertEqual(first["plan_digest"], second["plan_digest"])
        self.assertEqual(set(first["coverage"]["earned"]),
                         set(second["coverage"]["earned"]))

    def test_the_result_shape_is_what_coverage_merges(self):
        """`coverage` reads `res["coverage"]["earned"]` off every result file
        it is given. A replay-set result must be mergeable exactly like a run
        or hunt result, with no special case anywhere."""
        result = sweep.replay_records(self._paths(),
                                      sweep.CoverageLedger(sweep.load_targets()))
        self.assertIn("coverage", result)
        self.assertIn("earned", result["coverage"])
        self.assertIn("protected_hashes", result)
        for rec in result["coverage"]["earned"].values():
            self.assertIn("source", rec)
            self.assertIn("first", rec)
            self.assertIn("count", rec)


# ===========================================================================
# 12. Accepted harness-boundary replay accounting
# ===========================================================================


class HarnessBoundaryDispositionTests(_CrossRuntimeFixtureTest):
    """The offline Giratina lookup boundary is accepted narrowly, not hidden."""

    RECORDS = (
        "M7-divergence-distortion_wildboss_giratina_2779800549.json",
        "M7-divergence-distortion_wildboss_giratina_3187443927.json",
    )

    def _paths(self) -> list[str]:
        paths = [os.path.join(_ROUTE_ORACLE, "findings", n) for n in self.RECORDS]
        for path in paths:
            if not os.path.isfile(path):
                self.skipTest("retained Giratina boundary records are absent")
        return paths

    def test_boundary_records_remain_visible_but_are_accepted(self):
        result = sweep.replay_records(
            self._paths(), sweep.CoverageLedger(sweep.load_targets()))

        self.assertEqual(result["summary"]["diverged"], 2)
        self.assertEqual(result["summary"]["accepted_harness_boundary"], 2)
        self.assertEqual(result["summary"]["unexpected_divergence"], 0)
        self.assertTrue(all(row["reproduced"] for row in result["replay_set"]))
        self.assertTrue(all(row["disposition"] == "accepted-harness-boundary"
                            for row in result["replay_set"]))

    def test_a_changed_boundary_shape_is_not_accepted(self):
        path = self._paths()[0]
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)

        episode = {"divergence": json.loads(json.dumps(record["divergence"]))}
        episode["divergence"]["action"]["node_id"] = "n1_0"
        self.assertEqual(sweep.replay_disposition(record, episode),
                         "unexpected-divergence")

        episode["divergence"]["action"]["node_id"] = "n1_1"
        record["classification"]["verdict"] = "wrong classification"
        self.assertEqual(sweep.replay_disposition(record,
                                                   {"divergence": episode["divergence"]}),
                         "unexpected-divergence")



if __name__ == "__main__":
    unittest.main()
