"""Tests for the special submap system (`map_gen.generate_sub_map` and
`engine`'s `_enter_sub_map`/`_visit_underground`/`_visit_distortion`/
`_visit_sub_map_boss`/`_visit_reward`/`_visit_subexit`/`_return_from_sub_map`)
-- the port of `generateSubMap`/`enterSubMap`/`doSubMapBoss`/
`doSubMapReward`/`returnFromSubMap` (bundle.deobfuscated.js:53508-53632,
76687-77107). Gen4/Sinnoh-only: SILVER (Gen2) and MAGMA/AQUA (Gen3) do NOT
use this system at all (see `map_gen.py`'s and `engine.py`'s module
docstrings) -- `TrainerSpriteAssignmentTests`/`SilverNodeTests`/
`AdminNodeTests` in `test_trainer_silver_admin.py` remain the regression
suite for those, untouched here.

**Validation approach, stated plainly**: `map_gen.generate_sub_map` and its
helpers (`_roll_underground_trainers`/`_roll_sub_map_boss`/
`_pick_sub_map_rewards`/`_distortion_legendary`) were cross-checked bit-for-
bit against the REAL `generateSubMap`/`rollUndergroundTrainers`/
`rollSubMapBoss`/`pickSubMapRewards`/`distortionLegendary` run through Node
(`tools/battle-oracle/oracle-submap-check.js`, reusing the already-audited
`tools/battle-oracle/out/battle-prefix.js`, lines 1-81051 -- the same safety
reasoning `test_trainer_silver_admin.py`'s own docstring cites). Three
scenarios (ordinary underground, a 2nd-visit distortion legendary
encounter, and a 1st-visit distortion/small-team encounter) were run through
both sides and found byte-for-byte identical -- `OracleRegressionTests`
below hardcodes those exact JS-verified outputs as permanent regressions,
not just plausible-looking Python. `engine.py`'s own new handlers
(`_visit_sub_map_boss`/`_visit_reward`/`_return_from_sub_map`) are new
DESIGN/wiring on top of already-oracle-verified generation, validated here
against the source's own traced control flow (bundle.deobfuscated.js
citations throughout) the same way the rest of `test_engine.py` validates
`engine.py`'s non-formula wiring.

Run with: python -m unittest pokelike.tests.test_submaps -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pokelike import battle, data, engine, map_gen, rng
from pokelike.battle_loop import BattleResult
from pokelike.tests.test_engine import _loss, _mon, _start, _win
from pokelike.tests.test_trainer_silver_admin import _distinct_loss, _distinct_win


# ---------------------------------------------------------------------------
# map_gen.generate_sub_map -- topology, boundaries, and real-JS regressions
# ---------------------------------------------------------------------------


class GenerateSubMapTopologyTests(unittest.TestCase):
    def test_underground_has_two_bosses_and_full_bipartite_edges(self):
        rng.seed_rng(12345)
        result = map_gen.generate_sub_map("underground", 1, parent_node_level=20, team_size=3)
        m = result.map
        self.assertEqual(m.is_sub_map, "underground")
        boss_ids = [n.id for n in m.nodes.values() if n.type == map_gen.BOSS]
        reward_ids = [n.id for n in m.nodes.values() if n.type == map_gen.REWARD]
        self.assertEqual(boss_ids, ["n1_0", "n1_1"])
        self.assertEqual(reward_ids, ["n2_0", "n2_1", "n2_2"])
        self.assertEqual(m.nodes["n2_2"].extra["reward"], "skip")
        self.assertEqual(set(m.nodes.keys()), {"n0_0", "n1_0", "n1_1", "n2_0", "n2_1", "n2_2", "n3_0"})
        # Full bipartite: start -> every boss, every boss -> every reward,
        # every reward -> the single subexit -- NOT `_connect_layers`'s
        # proportional distribution (a mutation to reuse that helper here
        # would produce a DIFFERENT, sparser edge set than this).
        expected_edges = (
            [("n0_0", b) for b in boss_ids]
            + [(b, r) for b in boss_ids for r in reward_ids]
            + [(r, "n3_0") for r in reward_ids]
        )
        self.assertEqual(m.edges, expected_edges)
        self.assertTrue(m.nodes["n0_0"].visited)
        for b in boss_ids:
            self.assertTrue(m.nodes[b].accessible)
        for r in reward_ids:
            self.assertFalse(m.nodes[r].accessible)
        self.assertFalse(m.nodes["n3_0"].accessible)

    def test_underground_boss_extras(self):
        rng.seed_rng(12345)
        result = map_gen.generate_sub_map("underground", 1, parent_node_level=20, team_size=3)
        for node_id in ("n1_0", "n1_1"):
            node = result.map.nodes[node_id]
            self.assertEqual(node.extra["subBoss"], "underground")
            self.assertEqual(len(node.extra["bossTeam"]), 3)
            self.assertNotIn("wildBoss", node.extra)

    def test_distortion_non_legendary_has_one_boss(self):
        rng.seed_rng(1)
        result = map_gen.generate_sub_map(
            "distortion", 3, parent_node_level=30, team_size=3,
            distortion_worlds_entered=0, distortion_legendary_claimed=False,
        )
        m = result.map
        self.assertEqual(result.distortion_worlds_entered, 1)
        boss_ids = [n.id for n in m.nodes.values() if n.type == map_gen.BOSS]
        self.assertEqual(boss_ids, ["n1_0"])
        self.assertNotIn("wildBoss", m.nodes["n1_0"].extra)

    def test_distortion_second_visit_rolls_legendary_two_bosses(self):
        rng.seed_rng(999)
        result = map_gen.generate_sub_map(
            "distortion", 3, parent_node_level=30, team_size=2,
            distortion_worlds_entered=1, distortion_legendary_claimed=False,
        )
        m = result.map
        self.assertEqual(result.distortion_worlds_entered, 2)
        boss_ids = [n.id for n in m.nodes.values() if n.type == map_gen.BOSS]
        self.assertEqual(boss_ids, ["n1_0", "n1_1"])
        self.assertFalse(m.nodes["n1_0"].extra.get("wildBoss"))
        self.assertTrue(m.nodes["n1_1"].extra["wildBoss"])
        # n2_0 is ALWAYS the legendary's own guaranteed reward
        # (bundle.deobfuscated.js:53585-53591) -- not a random pick.
        legend_reward = m.nodes["n1_1"].extra["bossName"]
        self.assertIn(legend_reward, ("Dialga", "Palkia", "Giratina (Origin)"))
        expected_id = {"Dialga": "dialga", "Palkia": "palkia", "Giratina (Origin)": "giratina"}[legend_reward]
        self.assertEqual(m.nodes["n2_0"].extra["reward"], expected_id)

    def test_third_distortion_visit_does_not_reroll_legendary(self):
        # bundle.deobfuscated.js:76399-76409: `distortionWorldsEntered !== 2`
        # -- the exact-equality boundary means a SKIPPED 2nd claim is gone
        # forever, not retried on the 3rd+ visit.
        rng.seed_rng(999)
        result = map_gen.generate_sub_map(
            "distortion", 3, parent_node_level=30, team_size=2,
            distortion_worlds_entered=2, distortion_legendary_claimed=False,
        )
        self.assertEqual(result.distortion_worlds_entered, 3)
        boss_ids = [n.id for n in result.map.nodes.values() if n.type == map_gen.BOSS]
        self.assertEqual(boss_ids, ["n1_0"])

    def test_claimed_legendary_suppresses_second_visit_reroll(self):
        rng.seed_rng(999)
        result = map_gen.generate_sub_map(
            "distortion", 3, parent_node_level=30, team_size=2,
            distortion_worlds_entered=1, distortion_legendary_claimed=True,
        )
        self.assertEqual(result.distortion_worlds_entered, 2)
        boss_ids = [n.id for n in result.map.nodes.values() if n.type == map_gen.BOSS]
        self.assertEqual(boss_ids, ["n1_0"])

    def test_zero_worlds_entered_before_never_reaches_two_after_first_visit(self):
        # First-ever distortion visit: entered_after == 1, never eligible.
        rng.seed_rng(1)
        result = map_gen.generate_sub_map(
            "distortion", 0, parent_node_level=5, team_size=1,
            distortion_worlds_entered=0, distortion_legendary_claimed=False,
        )
        self.assertEqual(result.distortion_worlds_entered, 1)
        boss_ids = [n.id for n in result.map.nodes.values() if n.type == map_gen.BOSS]
        self.assertEqual(boss_ids, ["n1_0"])


class GenerateSubMapOracleRegressionTests(unittest.TestCase):
    """Exact byte-for-byte outputs from real `generateSubMap` execution via
    `tools/battle-oracle/oracle-submap-check.js` (see this file's module
    docstring) -- these three scenarios are the actual JS-vs-Python proof,
    not just plausible assertions."""

    def test_underground_seed12345_map1_matches_js(self):
        rng.seed_rng(12345)
        result = map_gen.generate_sub_map("underground", 1, parent_node_level=20, team_size=3)
        n = result.map.nodes
        self.assertEqual(n["n1_0"].extra["trainerKey"], "psychic")
        self.assertEqual(
            [(m["species_id"], m["level"]) for m in n["n1_0"].extra["bossTeam"]],
            [(425, 19), (355, 20), (281, 21)],
        )
        self.assertEqual(n["n1_1"].extra["trainerKey"], "youngster")
        self.assertEqual(
            [(m["species_id"], m["level"]) for m in n["n1_1"].extra["bossTeam"]],
            [(397, 19), (400, 20), (396, 21)],
        )
        self.assertEqual(n["n2_0"].extra["reward"], "stat10")
        self.assertEqual(n["n2_1"].extra["reward"], "rare_candy")

    def test_distortion_seed999_map3_legendary_matches_js(self):
        rng.seed_rng(999)
        result = map_gen.generate_sub_map(
            "distortion", 3, parent_node_level=30, team_size=2,
            distortion_worlds_entered=1, distortion_legendary_claimed=False,
        )
        n = result.map.nodes
        self.assertEqual(
            [(m["species_id"], m["level"]) for m in n["n1_0"].extra["bossTeam"]],
            [(461, 28), (442, 29), (130, 31)],
        )
        self.assertEqual(n["n1_1"].extra["bossName"], "Giratina (Origin)")
        self.assertEqual(n["n1_1"].extra["bossTeam"], [{"species_id": "giratina-origin", "level": 36}])
        self.assertEqual(n["n2_0"].extra["reward"], "giratina")
        self.assertEqual(n["n2_1"].extra["reward"], "transform")

    def test_distortion_seed555_map5_smallteam_nonlegendary_matches_js(self):
        rng.seed_rng(555)
        result = map_gen.generate_sub_map(
            "distortion", 5, parent_node_level=40, team_size=1,
            distortion_worlds_entered=0, distortion_legendary_claimed=False,
        )
        n = result.map.nodes
        boss_ids = [nid for nid, node in n.items() if node.type == map_gen.BOSS]
        self.assertEqual(boss_ids, ["n1_0"])  # non-legendary: exactly 1 boss
        self.assertEqual(
            [(m["species_id"], m["level"]) for m in n["n1_0"].extra["bossTeam"]],
            [(461, 38), (442, 39), (130, 41)],
        )
        # team_size=1 excludes "sacrifice" (minTeam=2) from the random pool.
        self.assertEqual(n["n2_0"].extra["reward"], "three_items")
        self.assertEqual(n["n2_1"].extra["reward"], "attack_up")
        self.assertNotIn("sacrifice", [n["n2_0"].extra["reward"], n["n2_1"].extra["reward"]])


class PickSubMapRewardsBoundaryTests(unittest.TestCase):
    def test_sacrifice_excluded_below_min_team(self):
        rng.seed_rng(1)
        pool_team1 = map_gen._pick_sub_map_rewards("underground", team_size=1, count=20)
        self.assertNotIn("sacrifice", pool_team1)
        rng.seed_rng(1)
        pool_team2 = map_gen._pick_sub_map_rewards("underground", team_size=2, count=20)
        self.assertIn("sacrifice", pool_team2)

    def test_skip_and_legend_rewards_never_in_random_pool(self):
        rng.seed_rng(2)
        pool = map_gen._pick_sub_map_rewards("distortion", team_size=6, count=20)
        self.assertNotIn("skip", pool)
        self.assertFalse(set(pool) & data.get_distortion_legend_rewards())

    def test_fossil_only_offered_for_underground(self):
        rng.seed_rng(3)
        underground_pool = map_gen._pick_sub_map_rewards("underground", team_size=6, count=20)
        distortion_pool = map_gen._pick_sub_map_rewards("distortion", team_size=6, count=20)
        self.assertIn("fossil", underground_pool)
        self.assertNotIn("fossil", distortion_pool)

    def test_shuffle_draw_count_independent_of_requested_count(self):
        # The Fisher-Yates shuffle runs over the FULL filtered pool
        # regardless of `count` (bundle.deobfuscated.js:76671-76686) -- a
        # mutation that shuffled only the first `count` slots would consume
        # FEWER rng() draws and desync every later draw in generate_sub_map.
        pool_size = len(
            [r for r in data.get_submap_rewards() if "underground" in r.kinds and r.id != "skip" and r.id not in data.get_distortion_legend_rewards()]
        )
        draws = {"n": 0}
        source_rng = rng.rng

        def counted():
            draws["n"] += 1
            return source_rng()

        rng.seed_rng(4)
        with patch.object(rng, "rng", side_effect=counted):
            map_gen._pick_sub_map_rewards("underground", team_size=6, count=1)
        self.assertEqual(draws["n"], pool_size - 1)


# ---------------------------------------------------------------------------
# engine.py -- submap entry/exit
# ---------------------------------------------------------------------------


def _underground_node(state):
    node = next(n for n in state.map.nodes.values() if n.accessible)
    node.type = map_gen.UNDERGROUND
    return node


class SubMapEntryExitTests(unittest.TestCase):
    def test_entering_underground_replaces_map_and_saves_return(self):
        eng, state = _start(seed=10)
        state.gen4_mode = True
        state.current_map = 1
        parent_map = state.map
        node = _underground_node(state)
        node.layer = 4
        original_other_node = next(n for n in parent_map.nodes.values() if n.id != node.id)
        original_visited = original_other_node.visited

        eng.step(engine.VisitNode(node_id=node.id))

        self.assertEqual(state.in_sub_map, "underground")
        self.assertIsNotNone(state.map)
        self.assertEqual(state.map.is_sub_map, "underground")
        self.assertIsNot(state.map, parent_map)
        self.assertEqual(state.current_node_id, "n0_0")
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        # Parent map is SAVED, not lost.
        self.assertIs(state.sub_map_return["map"], parent_map)
        self.assertEqual(state.sub_map_return["node_id"], node.id)
        self.assertEqual(state.sub_map_return["map_index"], 1)
        self.assertEqual(original_other_node.visited, original_visited)

    def test_subexit_restores_parent_map_and_heals_team_and_advances_original_node(self):
        eng, state = _start(seed=11)
        state.gen4_mode = True
        state.current_map = 1
        parent_map = state.map
        node = _underground_node(state)
        node.layer = 4
        state.team[0].current_hp = 1  # damaged before entering
        eng.step(engine.VisitNode(node_id=node.id))
        self.assertFalse(node.visited)  # not yet advanced -- only on return

        engine._visit_subexit(state, state.map.nodes["n3_0"])

        self.assertIsNone(state.in_sub_map)
        self.assertIsNone(state.sub_map_return)
        self.assertIs(state.map, parent_map)
        self.assertEqual(state.current_map, 1)
        self.assertEqual(state.current_node_id, node.id)
        self.assertTrue(node.visited)
        self.assertFalse(node.accessible)
        self.assertEqual(state.team[0].current_hp, state.team[0].max_hp)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_return_from_sub_map_with_no_saved_state_is_a_safe_noop(self):
        state = engine.RunState(gen4_mode=True)
        state.map = map_gen.generate_map(0, gen4_mode=True)
        state.sub_map_return = None
        state.in_sub_map = None
        engine._return_from_sub_map(state)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertIsNone(state.sub_map_return)

    def test_second_distortion_entry_increments_persisted_counter(self):
        eng, state = _start(seed=12)
        state.gen4_mode = True
        state.current_map = 3
        state.distortion_worlds_entered = 1
        node = _underground_node(state)
        node.type = map_gen.DISTORTION
        node.layer = 4
        eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.distortion_worlds_entered, 2)


# ---------------------------------------------------------------------------
# engine.py -- submap boss battles
# ---------------------------------------------------------------------------


def _sub_boss_node(node_id="n1_0", *, kind="underground", team=None, wild=False):
    team = team if team is not None else [{"species_id": 1, "level": 20}, {"species_id": 4, "level": 21}, {"species_id": 7, "level": 22}]
    extra = {"subBoss": kind, "bossName": "Test Boss", "bossTeam": team}
    if wild:
        extra["wildBoss"] = True
    return map_gen.MapNode(id=node_id, type=map_gen.BOSS, layer=1, col=0, accessible=True, extra=extra)


def _map_with_node(node, kind="underground"):
    return map_gen.GeneratedMap(nodes={node.id: node}, edges=[], layers=[[node]], map_index=1, is_sub_map=kind)


class SubMapBossTests(unittest.TestCase):
    def test_boss_dispatch_routes_through_visit_boss(self):
        # `_visit_boss` is the shared BOSS-type dispatch entry (map_gen.BOSS
        # -> engine._visit_boss in _NODE_HANDLERS) -- a mutation removing the
        # `subBoss` check there would send this into the ordinary gym-leader/
        # Elite-Four path instead.
        state = engine.RunState(gen4_mode=True, current_map=1)
        node = _sub_boss_node()
        with patch.object(engine, "_visit_sub_map_boss") as mock:
            engine._visit_boss(state, node)
        mock.assert_called_once_with(state, node)

    def test_boss_team_built_from_baked_extra_with_move_tier_2(self):
        state = engine.RunState(gen4_mode=True, current_map=6)  # map index would give move_tier 1 via get_move_tier_for_map
        node = _sub_boss_node()
        captured = {}

        def fake_run_battle(player_team, enemy_team, **kwargs):
            captured["enemy"] = list(enemy_team)
            return _distinct_win(player_team, enemy_team)

        state.team = [_mon(1, level=30)]
        state.map = _map_with_node(node)
        with patch.object(engine.battle_loop, "run_battle", side_effect=fake_run_battle):
            engine._visit_sub_map_boss(state, node)
        self.assertEqual(len(captured["enemy"]), 3)
        for mon in captured["enemy"]:
            self.assertEqual(mon.move_tier, 2)
        self.assertEqual([m.species_id for m in captured["enemy"]], [1, 4, 7])
        self.assertEqual([m.level for m in captured["enemy"]], [20, 21, 22])
        self.assertIsNone(captured["enemy"][0].held_item)

    def test_no_escape_rope_offer_on_loss_even_with_rope_in_bag(self):
        state = engine.RunState(gen4_mode=True, current_map=1)
        state.team = [_mon(1, level=5)]
        state.items = ["escape_rope"]
        node = _sub_boss_node()
        with patch.object(engine.battle_loop, "run_battle", side_effect=lambda p, e, **kw: _distinct_loss(p, e)):
            engine._visit_sub_map_boss(state, node)
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.items, ["escape_rope"])  # untouched

    def test_win_grants_level_2_to_participants_only_not_all_team_xp(self):
        # `_apply_level_gain` grants to every ALIVE member regardless of
        # `participants` (that set only matters for a FAINTED member) --
        # the real "participants-only, not all_team_xp" distinction this
        # test proves is that a FAINTED non-participant is excluded, unlike
        # `_visit_silver`/`_visit_admin`'s `all_team_xp=True` convention
        # (which would force it in via the full-team-range participants set
        # `_after_battle` builds in that mode).
        state = engine.RunState(gen4_mode=True, current_map=1)
        mon0 = _mon(1, level=10)
        mon1 = _mon(4, level=10)
        mon1.current_hp = 0  # fainted, did not participate
        state.team = [mon0, mon1]
        node = _sub_boss_node()
        state.map = _map_with_node(node)

        def fake_run_battle(player_team, enemy_team, **kwargs):
            result = _distinct_win(player_team, enemy_team, participants=[0])
            for mon in result.player_team:
                if mon.species_id == 4:
                    mon.current_hp = 0
            return result

        with patch.object(engine.battle_loop, "run_battle", side_effect=fake_run_battle):
            engine._visit_sub_map_boss(state, node)
        self.assertEqual(state.team[0].level, 12)
        self.assertEqual(state.team[1].level, 10)  # fainted non-participant untouched

    def test_win_advances_boss_node_reward_nodes_become_accessible_not_subexit(self):
        rng.seed_rng(500)
        result = map_gen.generate_sub_map("underground", 1, parent_node_level=20, team_size=3)
        state = engine.RunState(gen4_mode=True, current_map=1)
        state.map = result.map
        state.team = [_mon(1, level=90)]  # overpowered
        node = state.map.nodes["n1_0"]

        with patch.object(engine.battle_loop, "run_battle", side_effect=lambda p, e, **kw: _distinct_win(p, e)):
            engine._visit_sub_map_boss(state, node)

        self.assertTrue(node.visited)
        self.assertFalse(node.accessible)
        accessible = {n.id for n in engine.accessible_nodes(state)}
        reward_ids = {n.id for n in state.map.nodes.values() if n.type == map_gen.REWARD}
        # Premature-progression check: only the reward layer opens up, NOT
        # the subexit (which only becomes reachable after a reward node is
        # itself visited).
        self.assertEqual(accessible, reward_ids)
        self.assertFalse(state.map.nodes["n3_0"].accessible)
        self.assertFalse(state.map.nodes["n3_0"].visited)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_empty_boss_team_is_a_safe_advance(self):
        state = engine.RunState(gen4_mode=True, current_map=1)
        state.map = map_gen.GeneratedMap(nodes={}, edges=[], layers=[], map_index=1, is_sub_map="underground")
        node = _sub_boss_node(team=[])
        state.map.nodes[node.id] = node
        engine._visit_sub_map_boss(state, node)
        self.assertTrue(node.visited)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_giratina_origin_wild_boss_falls_back_to_base_giratina_species(self):
        state = engine.RunState(gen4_mode=True, current_map=3)
        state.team = [_mon(1, level=50)]
        node = _sub_boss_node(kind="distortion", wild=True, team=[{"species_id": "giratina-origin", "level": 40}])
        state.map = _map_with_node(node, kind="distortion")
        captured = {}

        def fake_run_battle(player_team, enemy_team, **kwargs):
            captured["enemy"] = list(enemy_team)
            return _distinct_win(player_team, enemy_team)

        with patch.object(engine.battle_loop, "run_battle", side_effect=fake_run_battle):
            engine._visit_sub_map_boss(state, node)
        self.assertEqual(captured["enemy"][0].species_id, 0x1E7)


# ---------------------------------------------------------------------------
# engine.py -- reward nodes
# ---------------------------------------------------------------------------


def _reward_node(reward_id, node_id="n2_0"):
    return map_gen.MapNode(id=node_id, type=map_gen.REWARD, layer=2, col=0, accessible=True, extra={"reward": reward_id})


def _reward_state(team=None, map_index=1):
    state = engine.RunState(gen4_mode=True, current_map=map_index)
    state.team = team if team is not None else [_mon(1, level=20)]
    state.map = map_gen.GeneratedMap(nodes={}, edges=[], layers=[], map_index=map_index, is_sub_map="underground")
    state.sub_map_return = {"kind": "underground", "map": None, "map_index": map_index, "node_id": "origin", "no_advance": False}
    return state


class RewardNodeTests(unittest.TestCase):
    def test_missing_reward_is_a_free_advance(self):
        state = _reward_state()
        node = _reward_node(None)
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertTrue(node.visited)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_skip_reward_is_a_free_advance(self):
        state = _reward_state()
        node = _reward_node("skip")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertTrue(node.visited)

    def test_team_lvl2_flat_unscaled_gain(self):
        state = _reward_state(team=[_mon(1, level=20)])
        node = _reward_node("team_lvl2")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.team[0].level, 22)  # +2, NOT halved (unlike stat buffs)
        self.assertEqual(state.team[0].current_hp, state.team[0].max_hp)  # full heal

    def test_rare_candy_appended_to_bag(self):
        state = _reward_state()
        node = _reward_node("rare_candy")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.items, ["rare_candy"])

    def test_three_items_draws_up_to_three_without_replacement(self):
        rng.seed_rng(7)
        state = _reward_state()
        node = _reward_node("three_items")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(len(state.items), 3)
        self.assertEqual(len(set(state.items)), 3)  # no duplicates
        self.assertTrue(set(state.items) <= {i.id for i in data.get_passive_items()})

    def test_three_items_stops_early_when_pool_exhausted_by_bag_dedup(self):
        rng.seed_rng(8)
        all_ids = [i.id for i in data.get_passive_items()]
        state = _reward_state()
        state.items = list(all_ids[:-1])  # only 1 item not already owned
        node = _reward_node("three_items")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(len(state.items), len(all_ids))  # exactly the 1 missing one added
        self.assertEqual(set(state.items), set(all_ids))

    def test_attack_up_applies_halved_rounded_gain_to_atk_and_special(self):
        state = _reward_state(team=[_mon(1, level=20), _mon(4, level=20)])
        node = _reward_node("attack_up")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        for mon in state.team:
            self.assertEqual(mon.stat_buffs.get("atk"), 1)  # raw +2 * 0.5 = 1
            self.assertEqual(mon.stat_buffs.get("special"), 1)
            self.assertNotIn("def", mon.stat_buffs)

    def test_fossil_offers_cranidos_or_shieldon_catch_auto_added_room_on_team(self):
        # team_size 1 < TEAM_CAP -- `_try_add_to_team` auto-adds directly
        # (no SWAP_CHOICE/CATCH_CHOICE prompt needed, matching
        # `_visit_legendary`'s own established convention for this port).
        rng.seed_rng(20)
        state = _reward_state()
        node = _reward_node("fossil")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 2)
        added = state.team[1]
        self.assertIn(added.species_id, (0x198, 0x19A))
        self.assertEqual(added.move_tier, 2)

    def test_fossil_offers_swap_choice_when_team_full(self):
        rng.seed_rng(20)
        state = _reward_state(team=[_mon(1, level=20 + i) for i in range(6)])
        node = _reward_node("fossil")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.phase, engine.Phase.SWAP_CHOICE)
        offered = state.pending.extra["incoming"]
        self.assertIn(offered.species_id, (0x198, 0x19A))

    def test_giratina_reward_uses_base_form_not_origin_and_sets_claimed(self):
        rng.seed_rng(21)
        state = _reward_state()
        node = _reward_node("giratina")
        state.map.nodes[node.id] = node
        self.assertFalse(state.distortion_legendary_claimed)
        engine._visit_reward(state, node)
        self.assertTrue(state.distortion_legendary_claimed)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        added = state.team[1]
        self.assertEqual(added.species_id, 0x1E7)  # base Giratina, NOT "giratina-origin"

    def test_dialga_and_palkia_reward_species(self):
        for reward_id, expected in (("dialga", 0x1E3), ("palkia", 0x1E4)):
            with self.subTest(reward_id=reward_id):
                rng.seed_rng(22)
                state = _reward_state()
                node = _reward_node(reward_id)
                state.map.nodes[node.id] = node
                engine._visit_reward(state, node)
                added = state.team[1]
                self.assertEqual(added.species_id, expected)

    def test_transform_rerolls_every_member_and_adds_four_levels(self):
        rng.seed_rng(30)
        mon0 = _mon(1, level=20)
        mon1 = _mon(4, level=25)
        original_species = {mon0.species_id, mon1.species_id}
        state = _reward_state(team=[mon0, mon1])
        node = _reward_node("transform")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.team[0].level, 24)
        self.assertEqual(state.team[1].level, 29)
        for mon in state.team:
            self.assertTrue(map_gen.is_gen4_line_eligible(mon.species_id))
            self.assertNotIn(mon.species_id, data.get_legendary_ids())
            self.assertEqual(mon.current_hp, mon.max_hp)

    def test_sacrifice_below_min_team_is_a_free_advance(self):
        state = _reward_state(team=[_mon(1, level=20)])  # only 1 member
        node = _reward_node("sacrifice")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertTrue(node.visited)
        self.assertEqual(len(state.team), 1)

    def test_sacrifice_opens_team_pick_phase(self):
        state = _reward_state(team=[_mon(1, level=20), _mon(4, level=20)])
        node = _reward_node("sacrifice")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.phase, engine.Phase.REWARD_TEAM_PICK)
        self.assertFalse(state.pending.optional)
        self.assertEqual(len(state.pending.options), 2)

    def test_stat10_opens_team_pick_phase(self):
        state = _reward_state(team=[_mon(1, level=20)])
        node = _reward_node("stat10")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        self.assertEqual(state.phase, engine.Phase.REWARD_TEAM_PICK)
        self.assertEqual(state.pending.extra["kind"], "stat10")


class RewardTeamPickResolutionTests(unittest.TestCase):
    def test_sacrifice_releases_chosen_member_and_grants_flat_four_levels(self):
        state = _reward_state(team=[_mon(1, level=20), _mon(4, level=20), _mon(7, level=20)])
        node = _reward_node("sacrifice")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        engine._resolve_reward_team_pick(state, engine.SelectOption(index=1))
        self.assertEqual(len(state.team), 2)
        self.assertEqual([m.species_id for m in state.team], [1, 7])
        for mon in state.team:
            self.assertEqual(mon.level, 24)  # +4, unscaled
        self.assertTrue(node.visited)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_sacrifice_rechecked_at_resolution_time_below_min_team(self):
        # bundle.deobfuscated.js:77027 -- if the team shrank to <2 between
        # the reward being OFFERED and the pick being resolved (e.g. a
        # Nuzlocke fainted-cull after the boss fight), the pick is silently
        # ignored (no release, no level gain) rather than crashing.
        state = _reward_state(team=[_mon(1, level=20), _mon(4, level=20)])
        node = _reward_node("sacrifice")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        state.team.pop()  # simulate the team shrinking before the pick resolves
        engine._resolve_reward_team_pick(state, engine.SelectOption(index=0))
        self.assertEqual(len(state.team), 1)
        self.assertEqual(state.team[0].level, 20)  # untouched

    def test_stat10_applies_plus_one_to_all_six_stats_and_partial_recompute(self):
        mon = _mon(1, level=20)
        mon.current_hp = 1  # damaged -- partial recompute must NOT full-heal
        state = _reward_state(team=[mon])
        node = _reward_node("stat10")
        state.map.nodes[node.id] = node
        engine._visit_reward(state, node)
        engine._resolve_reward_team_pick(state, engine.SelectOption(index=0))
        for stat in ("hp", "atk", "def", "speed", "special", "spdef"):
            self.assertEqual(mon.stat_buffs.get(stat), 1)
        self.assertEqual(mon.current_hp, 1)  # NOT healed


# ---------------------------------------------------------------------------
# Full public-flow round trip (real battles, not mocked)
# ---------------------------------------------------------------------------


class SubMapFullRoundTripTests(unittest.TestCase):
    def test_enter_defeat_bosses_pick_reward_exit_real_battles(self):
        eng, state = _start(seed=42)
        state.gen4_mode = True
        state.current_map = 1
        state.team = [_mon(1, level=95)]  # overpowered -- guaranteed wins
        parent_node = _underground_node(state)
        parent_node.layer = 4

        state = eng.step(engine.VisitNode(node_id=parent_node.id))
        self.assertEqual(state.in_sub_map, "underground")
        submap = state.map

        # `_advance`'s same-layer exclusivity (bundle.deobfuscated.js's
        # `advanceFromNode`) means fighting ONE boss node locks out any
        # other boss node in the same layer -- exactly like the parent
        # map's own single-choice-per-layer navigation, not "clear every
        # boss."
        boss_ids = [n.id for n in submap.nodes.values() if n.type == map_gen.BOSS]
        state = eng.step(engine.VisitNode(node_id=boss_ids[0]))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        for other_boss_id in boss_ids[1:]:
            self.assertFalse(submap.nodes[other_boss_id].accessible)

        reward_node_id = next(n.id for n in engine.accessible_nodes(state))
        state = eng.step(engine.VisitNode(node_id=reward_node_id))
        if state.phase == engine.Phase.REWARD_TEAM_PICK:
            state = eng.step(engine.SelectOption(index=0))
        elif state.phase == engine.Phase.SWAP_CHOICE:
            state = eng.step(engine.SelectOption(index=None))

        subexit_id = next(n.id for n in engine.accessible_nodes(state))
        self.assertEqual(submap.nodes[subexit_id].type, map_gen.SUBEXIT)
        state = eng.step(engine.VisitNode(node_id=subexit_id))

        self.assertIsNone(state.in_sub_map)
        self.assertIsNone(state.sub_map_return)
        self.assertIsNot(state.map, submap)
        self.assertEqual(state.current_map, 1)
        self.assertTrue(parent_node.visited)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)


if __name__ == "__main__":
    unittest.main()
