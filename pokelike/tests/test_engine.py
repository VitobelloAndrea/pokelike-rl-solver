"""Tests for pokelike/engine.py.

**Validation approach, stated plainly**: unlike `battle.py`/`map_gen.py`,
much of `engine.py` is an explicit DESIGN DECISION (the `Phase`/
`PendingChoice` state machine reifying the source's suspended-continuation
UI flows), not a byte-for-byte port -- there is no single JS function to
diff a Python function against here. Tests below validate:
- the parts that ARE a direct port of traced formulas (level-gain-per-
  encounter-type table, the question-node resolution cutoffs, the
  Wonder-Guard-HP-1 rule, Nuzlocke permadeath, the badge-advance map-8
  clamp) against `docs/logic-notes-runlifecycle.md`/`docs/logic-notes-
  nodes.md`'s citations, and
- the state-machine wiring itself (phase transitions, resumability across
  a branching-evolution interruption, invalid-action rejection) as
  structural/behavioral invariants.

Most tests replace `battle_loop.run_battle` with a stub returning a fixed
`BattleResult` (via `unittest.mock.patch`) rather than relying on real
combat outcomes -- `battle_loop.py` already has its own extensive test
suite for damage/turn-order/hook correctness; these tests are about
`engine.py`'s OWN bookkeeping (level gain, evolution, permadeath, map
advancement), not re-validating combat math. A handful of tests do run a
real battle (via a heavily overpowered player team) specifically to smoke-
test that `engine.py` wires `battle_loop.run_battle` correctly end-to-end.

Run with: python -m unittest pokelike.tests.test_engine -v
"""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from pokelike import battle, data, engine, map_gen, rng
from pokelike.battle_loop import BattleResult


def _mon(species_id, level=50, **overrides):
    mon = data.get_pokedex()[species_id]
    bs = overrides.pop("base_stats", mon.base_stats)
    hp = overrides.pop("max_hp", map_gen.calc_hp(bs.hp, level))
    return battle.Combatant(
        species_id=species_id, level=level, base_stats=bs, types=mon.types,
        max_hp=hp, current_hp=hp, name=mon.name, **overrides,
    )


def _win(player_team, enemy_team=None, participants=None, rounds=3):
    return BattleResult(
        player_won=True,
        player_team=list(player_team),
        enemy_team=list(enemy_team) if enemy_team is not None else [_mon(1, level=5)],
        player_participants=participants if participants is not None else {0},
        rounds=rounds,
    )


def _loss(player_team, enemy_team=None, rounds=3):
    for m in player_team:
        m.current_hp = 0
    return BattleResult(
        player_won=False,
        player_team=list(player_team),
        enemy_team=list(enemy_team) if enemy_team is not None else [_mon(1, level=50)],
        player_participants={0},
        rounds=rounds,
    )


class ResetAndStarterTests(unittest.TestCase):
    def test_reset_starts_at_choose_starter(self):
        eng = engine.Engine()
        state = eng.reset(seed=1)
        self.assertEqual(state.phase, engine.Phase.CHOOSE_STARTER)
        offered = {o["species_id"] for o in state.pending.options}
        self.assertEqual(offered, set(data.get_starter_ids(1)))

    def test_choose_starter_builds_team_and_starts_map0(self):
        eng = engine.Engine()
        state = eng.reset(seed=1)
        starter_id = state.pending.options[0]["species_id"]
        state = eng.step(engine.ChooseStarter(species_id=starter_id))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 1)
        self.assertEqual(state.team[0].species_id, starter_id)
        self.assertEqual(state.team[0].level, 5)
        self.assertEqual(state.starter_species_id, starter_id)
        self.assertEqual(state.current_map, 0)
        self.assertEqual(state.current_node_id, "n0_0")
        self.assertIsNotNone(state.map)

    def test_choose_invalid_starter_raises(self):
        eng = engine.Engine()
        eng.reset(seed=1)
        with self.assertRaises(ValueError):
            eng.step(engine.ChooseStarter(species_id=999999))

    def test_wrong_action_for_phase_raises(self):
        eng = engine.Engine()
        eng.reset(seed=1)
        with self.assertRaises(ValueError):
            eng.step(engine.VisitNode(node_id="n0_0"))

    def test_step_before_reset_raises(self):
        eng = engine.Engine()
        with self.assertRaises(RuntimeError):
            eng.step(engine.AdvanceMap())


def _start(seed=1) -> tuple[engine.Engine, engine.RunState]:
    eng = engine.Engine()
    state = eng.reset(seed=seed)
    starter_id = state.pending.options[0]["species_id"]
    state = eng.step(engine.ChooseStarter(species_id=starter_id))
    return eng, state


class NodeAccessTests(unittest.TestCase):
    def test_visit_inaccessible_node_raises(self):
        eng, state = _start()
        inaccessible = next(n for n in state.map.nodes.values() if not n.accessible and n.id != "n0_0")
        with self.assertRaises(ValueError):
            eng.step(engine.VisitNode(node_id=inaccessible.id))

    def test_visit_unknown_node_raises(self):
        eng, state = _start()
        with self.assertRaises(ValueError):
            eng.step(engine.VisitNode(node_id="does-not-exist"))

    def test_accessible_nodes_helper(self):
        eng, state = _start()
        accessible = engine.accessible_nodes(state)
        self.assertTrue(accessible)
        self.assertTrue(all(n.accessible for n in accessible))


class LegalActionsTests(unittest.TestCase):
    """CODEX.md issue 32: `legal_actions` is the single authoritative
    legality source spanning every phase, not just map choices."""

    def test_choose_starter_phase(self):
        eng = engine.Engine()
        state = eng.reset(seed=1)
        actions = engine.legal_actions(state)
        self.assertIn("choose_starter", actions)
        self.assertEqual(set(actions["choose_starter"]["species_ids"]), set(data.get_starter_ids(1)))
        self.assertNotIn("visit_node", actions)

    def test_on_map_phase_lists_accessible_nodes(self):
        eng, state = _start()
        actions = engine.legal_actions(state)
        self.assertIn("visit_node", actions)
        node_ids = set(actions["visit_node"]["node_ids"])
        self.assertEqual(node_ids, {n.id for n in engine.accessible_nodes(state)})

    def test_on_map_reorder_team_only_offered_with_multiple_members(self):
        eng, state = _start()
        actions = engine.legal_actions(state)
        self.assertNotIn("reorder_team", actions)  # single starter, nothing to reorder
        state.team.append(_mon(4, level=5))
        actions = engine.legal_actions(state)
        self.assertEqual(actions["reorder_team"]["team_size"], 2)

    def test_use_item_lists_only_usable_items_with_eligible_targets(self):
        eng, state = _start()
        state.items = ["rare_candy", "moon_stone", "eviolite"]  # eviolite is passive, not usable
        state.team[0].current_hp = 0  # moon_stone requires current_hp > 0
        actions = engine.legal_actions(state)
        use_item = {e["item_id"]: e for e in actions["use_item"]}
        self.assertIn("rare_candy", use_item)  # rare_candy is eligible even fainted
        self.assertNotIn("moon_stone", use_item)  # blocked: fainted target
        self.assertNotIn("eviolite", use_item)  # not a usable item at all

    def test_equip_item_offered_for_passive_bag_item(self):
        eng, state = _start()
        state.items = ["eviolite"]
        actions = engine.legal_actions(state)
        self.assertEqual(actions["equip_item"]["bag_indices"], [0])
        self.assertEqual(actions["equip_item"]["team_indices"], [0])

    def test_pending_choice_phase_lists_select_option_indices(self):
        eng, state = _start(seed=10)
        state.team = [_mon(133, level=25)]  # Eevee, branching evolutions
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.EVOLUTION_CHOICE)
        actions = engine.legal_actions(state)
        self.assertIn("select_option", actions)
        self.assertEqual(actions["select_option"]["indices"], list(range(len(state.pending.options))))
        self.assertFalse(actions["select_option"]["optional"])  # a branching evolution is not skippable

    def test_next_map_ready_phase(self):
        eng, state = _start(seed=2)
        boss = state.map.layers[-1][0]
        boss.accessible = True
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.phase, engine.Phase.NEXT_MAP_READY)
        self.assertEqual(engine.legal_actions(state), {"advance_map": True})

    def test_game_over_phase_has_no_legal_actions(self):
        eng, state = _start(seed=2)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_loss(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(engine.legal_actions(state), {})


class BattleWinLossTests(unittest.TestCase):
    def test_wild_battle_win_advances_and_grants_one_level(self):
        eng, state = _start(seed=2)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        before_level = state.team[0].level
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertTrue(node.visited)
        self.assertFalse(node.accessible)
        self.assertEqual(state.team[0].level, before_level + 1)  # wild = +1, docs/logic-notes-runlifecycle.md section 5

    def test_wild_battle_loss_ends_run(self):
        eng, state = _start(seed=2)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_loss(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertTrue(state.game_over)

    def test_step_after_game_over_raises(self):
        eng, state = _start(seed=2)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_loss(state.team)):
            eng.step(engine.VisitNode(node_id=node.id))
        with self.assertRaises(ValueError):
            eng.step(engine.AdvanceMap())

    def test_boss_win_grants_badge_and_awaits_advance_map(self):
        eng, state = _start(seed=2)
        boss = state.map.layers[-1][0]
        boss.accessible = True
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.badges, 1)
        self.assertEqual(state.phase, engine.Phase.NEXT_MAP_READY)
        with self.assertRaises(ValueError):
            eng.step(engine.VisitNode(node_id="n0_0"))  # can't visit nodes mid-badge-screen
        state = eng.step(engine.AdvanceMap())
        self.assertEqual(state.current_map, 1)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_trainer_and_gym_level_gain_amounts(self):
        # Port of applyLevelGain's per-encounter table (docs/logic-notes-
        # runlifecycle.md section 5): trainer=2, gym leader (non-nuzlocke)=2.
        eng, state = _start(seed=3)
        trainer_node = next(n for n in state.map.nodes.values() if n.accessible)
        trainer_node.type = map_gen.TRAINER
        before = state.team[0].level
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=trainer_node.id))
        self.assertEqual(state.team[0].level, before + 2)

    def test_gym_leader_nuzlocke_level_gain_is_one(self):
        eng, state = _start(seed=4)
        state.nuzlocke_mode = True
        boss = state.map.layers[-1][0]
        boss.accessible = True
        before = state.team[0].level
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.team[0].level, before + 1)


class NuzlockePermadeathTests(unittest.TestCase):
    def test_fainted_member_removed_and_held_item_dropped_to_bag(self):
        eng, state = _start(seed=5)
        state.nuzlocke_mode = True
        second = _mon(4, level=10, held_item=battle.HeldItem(id="leftovers"))
        second.current_hp = 0
        state.team.append(second)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team, participants={0, 1})):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(len(state.team), 1)
        self.assertIn("leftovers", state.items)

    def test_total_wipe_ends_run(self):
        eng, state = _start(seed=6)
        state.nuzlocke_mode = True
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)  # sets currentHp=0 on the only team member
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.team, [])

    def test_nuzlocke_loss_cannot_recover_even_with_rope(self):
        # P0.6: runBattleScreen's eligibility check is `!isBoss &&
        # !isEndlessMode && !nuzlockeMode` (bundle.deobfuscated.js:81399-
        # 81402) -- Nuzlocke disqualifies the rope offer regardless of a
        # wild (non-boss) encounter and a rope being present.
        eng, state = _start(seed=6)
        state.nuzlocke_mode = True
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.items, ["escape_rope"])  # never consumed


class EscapeRopeRecoveryTests(unittest.TestCase):
    """P0.6: `runBattleScreen`'s eligible-loss branch (bundle.deobfuscated.
    js:81388-81429) -- traced per encounter call site's `isBoss` argument,
    not inferred from "non-boss" prose. Eligible: `doBattleNode`/wild
    (bundle.deobfuscated.js:77724, isBoss=false), `doTrainerNode`
    (bundle.deobfuscated.js:80327, isBoss=false), `doLegendaryNode`
    (bundle.deobfuscated.js:80439, isBoss=false). Ineligible: gym leader
    (`doBossNode`, isBoss=true), Elite Four (`doElite4`, isBoss=true),
    Silver (`doSilverNode`, isBoss=true), Magma/Aqua (`doAdminNode`,
    isBoss=true) -- regardless of a rope in the bag.
    """

    def test_eligible_wild_loss_with_rope_enters_nonterminal_choice(self):
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.ESCAPE_ROPE_CHOICE)
        self.assertFalse(state.game_over)
        actions = engine.legal_actions(state)
        self.assertEqual(actions, {"select_option": {"indices": [0], "optional": True}})

    def test_accepting_consumes_one_rope_and_sets_only_last_member_to_1hp(self):
        eng, state = _start(seed=20)
        state.team.append(_mon(4, level=5))
        state.items = ["oran_berry", "escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.items, ["oran_berry"])  # exactly the rope removed
        self.assertEqual(state.team[0].current_hp, 0)
        self.assertEqual(state.team[-1].current_hp, 1)  # only the FINAL member, per source
        self.assertTrue(state.escaped_via_rope)

    def test_accepting_wild_battle_advances_node_without_xp_or_evolution(self):
        eng, state = _start(seed=20)
        state.team[0] = _mon(4, level=15)  # Charmander, one level short of evolving at 16
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertTrue(node.visited)
        self.assertEqual(state.team[0].level, 15)  # no XP from a recovered loss
        self.assertEqual(state.team[0].species_id, 4)  # no evolution check ran either

    def test_accepting_trainer_battle_advances_node(self):
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRAINER
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertTrue(node.visited)

    def test_accepting_legendary_battle_still_offers_the_catch(self):
        # Traced: doLegendaryNode's runBattleScreen call is isBoss=false
        # (bundle.deobfuscated.js:80439), so its loss branch offers Escape
        # Rope same as a wild battle; accepting re-enters the SAME success
        # callback a win would (mark-caught/show-swap-screen), per source.
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.LEGENDARY
        node.extra["legendarySpeciesId"] = 144  # Articuno
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.ESCAPE_ROPE_CHOICE)
        state = eng.step(engine.SelectOption(index=0))
        # `offer_catch` ran (not GAME_OVER) -- team had room, so `_try_add_to_team`
        # auto-adds the legendary and returns straight to ON_MAP, matching an
        # actual win's same continuation (`test_legendary_node_uses_preassigned_
        # species_and_autocatches` in TradeAndLegendaryAndShinyTests).
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 2)
        self.assertEqual(state.team[1].species_id, 144)

    def test_declining_reaches_game_over_without_consuming_rope(self):
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=None))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertTrue(state.game_over)
        self.assertEqual(state.items, ["escape_rope"])
        self.assertFalse(state.escaped_via_rope)

    def test_no_rope_reaches_game_over_immediately(self):
        eng, state = _start(seed=20)
        state.items = []
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)

    def test_boss_loss_with_rope_cannot_recover(self):
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        boss = state.map.layers[-1][0]
        boss.accessible = True
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.items, ["escape_rope"])

    def test_elite_four_loss_with_rope_cannot_recover(self):
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        state.current_map = 8
        boss = state.map.layers[-1][0]
        boss.accessible = True
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.items, ["escape_rope"])

    def test_silver_loss_with_rope_cannot_recover(self):
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.SILVER
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.items, ["escape_rope"])

    def test_magma_admin_loss_with_rope_cannot_recover(self):
        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.MAGMA
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.items, ["escape_rope"])

    def test_multiple_ropes_consume_only_the_first_matching_entry(self):
        eng, state = _start(seed=20)
        state.items = ["leftovers", "escape_rope", "escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.items, ["leftovers", "escape_rope"])  # index 1 removed, not index 2

    def test_state_serialization_describes_pending_escape_rope_choice(self):
        from pokelike.webui import state_json

        eng, state = _start(seed=20)
        state.items = ["escape_rope"]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        payload = state_json.encode_state(state)
        self.assertEqual(payload["phase"], "escape_rope_choice")
        self.assertEqual(payload["pending"]["phase"], "escape_rope_choice")
        self.assertTrue(payload["pending"]["optional"])


class CatchAndSwapTests(unittest.TestCase):
    def test_catch_node_presents_choices_and_adds_to_team(self):
        eng, state = _start(seed=7)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.CATCH_CHOICE)
        self.assertGreater(len(state.pending.options), 0)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 2)
        self.assertTrue(state.used_ball_catch)

    def test_catch_choice_decline_advances_without_change(self):
        eng, state = _start(seed=7)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=None))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 1)
        self.assertTrue(node.visited)

    def test_full_team_prompts_swap_choice(self):
        eng, state = _start(seed=8)
        # fill the roster to TEAM_CAP
        while len(state.team) < engine.TEAM_CAP:
            state.team.append(_mon(4, level=5, held_item=battle.HeldItem(id="oran_berry")))
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.SWAP_CHOICE)
        self.assertEqual(len(state.pending.options), engine.TEAM_CAP)
        released_species = state.team[2].species_id
        state = eng.step(engine.SelectOption(index=2))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), engine.TEAM_CAP)
        self.assertNotEqual(state.team[2].species_id, released_species)
        self.assertIn("oran_berry", state.items)

    def test_swap_choice_cancel_keeps_team_unchanged(self):
        eng, state = _start(seed=8)
        while len(state.team) < engine.TEAM_CAP:
            state.team.append(_mon(4, level=5))
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        species_before = [m.species_id for m in state.team]
        state = eng.step(engine.SelectOption(index=0))  # -> SWAP_CHOICE
        state = eng.step(engine.SelectOption(index=None))  # decline
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual([m.species_id for m in state.team], species_before)


class QuestionNodeTests(unittest.TestCase):
    def test_cutoffs_match_resolveQuestionMark(self):
        eng, state = _start(seed=9)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION

        cases = [
            (0.10, map_gen.BATTLE),
            (0.30, map_gen.TRAINER),
            (0.45, map_gen.CATCH),   # non-nuzlocke secondary branch
            (0.60, map_gen.ITEM),
            (0.70, "shiny"),
            (0.90, "mega"),
        ]
        for roll, expected in cases:
            with self.subTest(roll=roll):
                fresh_node_id = f"probe_{roll}"
                state.map.nodes[fresh_node_id] = map_gen.MapNode(
                    id=fresh_node_id, type=map_gen.QUESTION, layer=node.layer, col=0, accessible=True,
                )
                with patch.object(engine.rng, "rng", return_value=roll):
                    resolved = engine._resolve_question(state, state.map.nodes[fresh_node_id])
                self.assertEqual(resolved, expected)

    def test_nuzlocke_secondary_branch_is_battle_not_catch(self):
        eng, state = _start(seed=9)
        state.nuzlocke_mode = True
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        with patch.object(engine.rng, "rng", return_value=0.45):
            resolved = engine._resolve_question(state, node)
        self.assertEqual(resolved, map_gen.BATTLE)

    def test_resolution_is_cached_per_node(self):
        eng, state = _start(seed=9)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        with patch.object(engine.rng, "rng", return_value=0.10):
            first = engine._resolve_question(state, node)
        with patch.object(engine.rng, "rng", return_value=0.90):
            second = engine._resolve_question(state, node)
        self.assertEqual(first, second)


class QuestionNodeShinyBonusTests(unittest.TestCase):
    """P0.4: resolveQuestionMark's additive shiny-node bonus
    (bundle.deobfuscated.js:77397-77430) -- +0.07 for `hasShinyCharm()`
    (`state.shiny_charm`) and +0.07 for an enabled `shiny_rate` passive,
    added together (not the multiplicative doubling `_shiny_chance` uses)."""

    def _probe(self, state, roll):
        node = map_gen.MapNode(
            id=f"probe_{roll}_{id(state)}", type=map_gen.QUESTION, layer=0, col=0, accessible=True,
        )
        state.map.nodes[node.id] = node
        with patch.object(engine.rng, "rng", return_value=roll):
            return engine._resolve_question(state, node)

    def test_no_bonus_upper_bound_is_072(self):
        eng, state = _start(seed=9)
        self.assertEqual(self._probe(state, 0.71999), "shiny")
        self.assertEqual(self._probe(state, 0.72), "mega")

    def test_shiny_charm_only_shifts_cutoff_to_079(self):
        eng = engine.Engine()
        state = eng.reset(seed=9, shiny_charm=True)
        starter_id = state.pending.options[0]["species_id"]
        state = eng.step(engine.ChooseStarter(species_id=starter_id))
        self.assertEqual(self._probe(state, 0.75), "shiny")
        self.assertEqual(self._probe(state, 0.78999), "shiny")
        self.assertEqual(self._probe(state, 0.79), "mega")

    def test_shiny_rate_passive_only_shifts_cutoff_to_079(self):
        eng = engine.Engine()
        state = eng.reset(seed=9, passives=[battle.Trait("shiny_rate")])
        starter_id = state.pending.options[0]["species_id"]
        state = eng.step(engine.ChooseStarter(species_id=starter_id))
        self.assertEqual(self._probe(state, 0.75), "shiny")
        self.assertEqual(self._probe(state, 0.78999), "shiny")
        self.assertEqual(self._probe(state, 0.79), "mega")

    def test_shiny_rate_passive_disabled_gives_no_bonus(self):
        eng = engine.Engine()
        state = eng.reset(seed=9, passives=[battle.Trait("shiny_rate", enabled=False)])
        starter_id = state.pending.options[0]["species_id"]
        state = eng.step(engine.ChooseStarter(species_id=starter_id))
        self.assertEqual(self._probe(state, 0.75), "mega")

    def test_both_bonuses_stack_additively_to_086(self):
        eng = engine.Engine()
        state = eng.reset(seed=9, shiny_charm=True, passives=[battle.Trait("shiny_rate")])
        starter_id = state.pending.options[0]["species_id"]
        state = eng.step(engine.ChooseStarter(species_id=starter_id))
        self.assertEqual(self._probe(state, 0.85999), "shiny")
        self.assertEqual(self._probe(state, 0.86), "mega")

    def test_roll_075_reproduction_with_shiny_charm(self):
        # Concrete repro: pre-fix Python resolved this as "mega" because
        # hasShinyCharm() was never threaded into the cutoff at all.
        eng = engine.Engine()
        state = eng.reset(seed=9, shiny_charm=True)
        starter_id = state.pending.options[0]["species_id"]
        state = eng.step(engine.ChooseStarter(species_id=starter_id))
        self.assertEqual(self._probe(state, 0.75), "shiny")

    def test_cached_resolution_consumes_no_further_rng_draw(self):
        eng = engine.Engine()
        state = eng.reset(seed=9, shiny_charm=True)
        starter_id = state.pending.options[0]["species_id"]
        state = eng.step(engine.ChooseStarter(species_id=starter_id))
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        with patch.object(engine.rng, "rng", return_value=0.75) as mock_rng:
            first = engine._resolve_question(state, node)
            self.assertEqual(mock_rng.call_count, 1)
            second = engine._resolve_question(state, node)
            self.assertEqual(mock_rng.call_count, 1)
        self.assertEqual(first, second)


class EvolutionTests(unittest.TestCase):
    def test_non_branching_evolution_is_automatic(self):
        eng, state = _start(seed=10)
        # Charmander (4) -> Charmeleon (5) at level 16
        state.team = [_mon(4, level=16)]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.team[0].species_id, 5)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_eviolite_blocks_evolution(self):
        eng, state = _start(seed=10)
        state.team = [_mon(4, level=16, held_item=battle.HeldItem(id="eviolite"))]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.team[0].species_id, 4)

    def test_branching_evolution_pauses_for_a_choice(self):
        eng, state = _start(seed=10)
        state.team = [_mon(133, level=25)]  # Eevee, branching evolutions
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.EVOLUTION_CHOICE)
        self.assertGreater(len(state.pending.options), 1)
        chosen_into = state.pending.options[0]["into"]
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.team[0].species_id, chosen_into)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)  # resumed straight through to the node's own finish step
        self.assertTrue(node.visited)

    def test_evolution_choice_is_not_optional(self):
        eng, state = _start(seed=10)
        state.team = [_mon(133, level=25)]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            eng.step(engine.VisitNode(node_id=node.id))
        with self.assertRaises(ValueError):
            eng.step(engine.SelectOption(index=None))

    def test_nincada_evolving_into_ninjask_spawns_shedinja(self):
        # CODEX.md issue 17: `spawnShedinjaIfNinjask` (bundle.deobfuscated.js:
        # 79848-79882) -- evolving Nincada (290) into Ninjask (291) must add
        # a fresh Shedinja (292) to the team if there's an open slot.
        eng, state = _start(seed=10)
        state.team = [_mon(290, level=20)]  # Nincada, evolves into Ninjask at 20
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.team[0].species_id, 291)
        self.assertEqual(len(state.team), 2)
        self.assertEqual(state.team[1].species_id, 292)
        self.assertEqual(state.team[1].level, state.team[0].level)
        self.assertEqual(state.team[1].current_hp, state.team[1].max_hp)

    def test_shedinja_not_spawned_when_team_is_full(self):
        eng, state = _start(seed=10)
        state.team = [_mon(290, level=20)] + [_mon(1, level=5) for _ in range(5)]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.team[0].species_id, 291)
        self.assertEqual(len(state.team), 6)
        self.assertFalse(any(m.species_id == 292 for m in state.team))

    def test_force_true_evolution_revives_a_fainted_mon_to_one_hp(self):
        # CODEX.md issue 18: `applyEvolution` (force=True, Moon Stone's own
        # path) has no was-fainted branch -- current HP is unconditionally
        # max(1, floor(fraction*newMaxHp)), so a fainted mon comes back at
        # 1 HP as a side effect of the HP-curve recompute. This is a real,
        # source-confirmed quirk, distinct from `checkAndEvolveTeam`'s own
        # fainted-stays-fainted formula (see the next test). Exercised by
        # calling `_apply_evolution` directly rather than through the public
        # `UseItem` action -- `usableItemCanTarget`'s own moon_stone gate
        # (bundle.deobfuscated.js:79571-79583) already blocks fainted
        # targets from reaching Moon Stone at all, so this formula-level
        # quirk is otherwise unreachable through that specific item, but is
        # still the correct port of `applyEvolution` itself.
        eng, state = _start(seed=17)
        mon = _mon(4, level=16)  # Charmander
        mon.current_hp = 0
        state.team = [mon]
        engine._apply_evolution(state, mon, 5, force=True)  # Charmeleon
        self.assertEqual(mon.species_id, 5)
        self.assertEqual(mon.current_hp, 1)

    def test_augment_pct_scales_force_true_evolution_maxhp(self):
        eng, state = _start(seed=18)
        mon = _mon(4, level=16)
        mon.augment_pct = 50.0
        state.team = [mon]
        expected_without_augment = map_gen.calc_hp(data.get_pokedex()[5].base_stats.hp, 16)
        engine._apply_evolution(state, mon, 5, force=True)
        self.assertEqual(mon.max_hp, math.floor(expected_without_augment * 1.5))

    def test_non_forced_evolution_keeps_a_fainted_mon_fainted(self):
        # checkAndEvolveTeam's own formula (force=False) -- the mirror image
        # of the quirk above: a mon already at 0 HP when evolution is
        # checked stays at 0 HP afterward.
        eng, state = _start(seed=19)
        mon = _mon(4, level=16)
        mon.current_hp = 0
        state.team = [mon]
        engine._apply_evolution(state, mon, 5, force=False)
        self.assertEqual(mon.species_id, 5)
        self.assertEqual(mon.current_hp, 0)

    def test_rare_candy_evolution_checks_the_whole_team_not_just_the_target(self):
        # CODEX.md issue 19: Rare Candy calls the FULL `checkAndEvolveTeam()`
        # afterward, not a target-only check -- a second team member that
        # independently became evolution-eligible must also evolve in the
        # same `UseItem` call.
        eng, state = _start(seed=20)
        candy_target = _mon(1, level=50)  # does not evolve further
        other = _mon(4, level=16)  # Charmander, already evolution-eligible
        state.team = [candy_target, other]
        state.items = ["rare_candy"]
        state = eng.step(engine.UseItem(item_index=0, target_index=0))
        self.assertEqual(state.team[1].species_id, 5)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)


class BattleConfigByModeTests(unittest.TestCase):
    """CODEX.md issue 1 / P0 item 1: `runBattleScreen`'s battle-config
    construction (bundle.deobfuscated.js:81067-81085) is generation-gated,
    not "always build both". Ordinary (non-Endless) Gen1/Gen2 gets no
    battle config at all; ordinary Gen3/Gen4 gets `buildGen3AbilityConfig()`
    merged with `buildTraitsConfig({}, {}, passives)` -- EMPTY tier maps,
    never `compute_trait_tiers(state.team)`.
    """

    def test_gen1_story_gets_no_battle_config(self):
        eng, state = _start(seed=20)
        ability_cfg, traits_cfg = engine._battle_configs(state, [])
        self.assertIsNone(ability_cfg)
        self.assertIsNone(traits_cfg)

    def test_gen2_story_gets_no_battle_config(self):
        eng = engine.Engine()
        state = eng.reset(gen2_mode=True, seed=21)
        state = eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        ability_cfg, traits_cfg = engine._battle_configs(state, [])
        self.assertIsNone(ability_cfg)
        self.assertIsNone(traits_cfg)

    def test_gen3_story_gets_ability_config_and_no_traits_config_when_no_passives(self):
        """`buildTraitsConfig` itself returns `null` when both tier maps AND
        the passives list are empty (bundle.deobfuscated.js:60733-60738) --
        confirmed via the JS-vs-Python oracle's `truant`/`mirror_coat`/etc
        fixtures (tools/battle-oracle/fixtures/), which only reproduce the
        source's `mergeBattleConfigs` quirks when `traits_config` is
        genuinely `None` for an empty passive list, not a real-but-inert
        object. An earlier version of this test asserted the opposite
        (`traits_cfg` always non-`None`) -- that was the bug, not this."""
        eng = engine.Engine()
        state = eng.reset(gen3_mode=True, seed=22)
        state = eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        # give the team enough same-typed members that compute_trait_tiers
        # WOULD produce a non-empty tier map, to prove it's not being used.
        state.team = [_mon(4, level=50), _mon(5, level=50), _mon(6, level=50)]  # all Fire
        ability_cfg, traits_cfg = engine._battle_configs(state, [])
        self.assertIsInstance(ability_cfg, engine.battle_abilities.Gen3AbilityConfig)
        self.assertIsNone(traits_cfg)

    def test_gen3_story_gets_traits_config_when_passives_present(self):
        eng = engine.Engine()
        state = eng.reset(gen3_mode=True, seed=22)
        state = eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        state.passives = [battle.Trait(id="sword_charm")]
        ability_cfg, traits_cfg = engine._battle_configs(state, [])
        self.assertIsInstance(ability_cfg, engine.battle_abilities.Gen3AbilityConfig)
        self.assertIsInstance(traits_cfg, engine.battle_traits.TraitsConfig)
        self.assertEqual(traits_cfg.player_tiers, {})
        self.assertEqual(traits_cfg.enemy_tiers, {})

    def test_gen4_story_ability_config_uses_gen4_table(self):
        eng = engine.Engine()
        state = eng.reset(gen4_mode=True, seed=23)
        state = eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        ability_cfg, traits_cfg = engine._battle_configs(state, [])
        self.assertIsInstance(ability_cfg, engine.battle_abilities.Gen3AbilityConfig)
        self.assertTrue(ability_cfg.gen4_mode)
        self.assertIsNone(traits_cfg)


class CopyBackBoundaryTests(unittest.TestCase):
    """CODEX.md issues 3-4 / P0 item 2: battle-local mutations (Ditto's
    transform, a Traced ability, ...) must never leak onto the persistent
    `state.team` objects -- only the narrow win/loss copy-back contract
    (`bundle.deobfuscated.js:81283-81318`/`81389-81391`) should reach them.
    Runs the REAL `battle_loop.run_battle` (not mocked) to exercise the
    actual clone boundary end-to-end.
    """

    def test_ditto_type_change_does_not_leak_after_a_real_win(self):
        eng, state = _start(seed=30)
        ditto = _mon(132, level=100)  # Ditto, overleveled so it always wins
        original_types = ditto.types
        original_base_stats = ditto.base_stats
        state.team = [ditto]
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        rng.seed_rng(99)
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.team[0].species_id, 132)
        self.assertEqual(state.team[0].types, original_types)
        self.assertEqual(state.team[0].base_stats, original_base_stats)

    def test_win_copies_level_maxhp_and_clamped_currenthp_only(self):
        # Unit-level: call the copy-back function directly (bypassing
        # `_after_battle`'s own separate level-gain step, a different
        # source stage) to isolate exactly what `runBattleScreen`'s win
        # branch itself copies (bundle.deobfuscated.js:81283-81318).
        eng, state = _start(seed=31)
        mon = _mon(1, level=50)
        state.team = [mon]
        clone = _mon(1, level=51, max_hp=200)
        clone.current_hp = 150
        clone.types = ("Poison",)  # should NOT leak -- not part of the copy-back contract
        clone.flags["_runSpeedStage"] = 2
        engine._copy_back_battle_result(state, [clone], player_won=True)
        self.assertEqual(state.team[0].level, 51)
        self.assertEqual(state.team[0].max_hp, 200)
        self.assertEqual(state.team[0].current_hp, 150)
        self.assertEqual(state.team[0].types, mon.types)  # unchanged, real species types
        self.assertEqual(state.team[0].flags.get("_runSpeedStage"), 2)

    def test_win_with_run_max_hp_recomputes_maxhp_instead_of_copying_clone_maxhp(self):
        # bundle.deobfuscated.js:81294-81313 -- when `_runMaxHp` is set,
        # maxHp is NOT just copied from the clone: it's recomputed from the
        # (possibly new) level/base-stats curve plus the `_runMaxHp` bonus.
        eng, state = _start(seed=31)
        mon = _mon(1, level=50)
        state.team = [mon]
        clone = _mon(1, level=50, max_hp=999)  # deliberately wrong/stale clone maxHp
        clone.current_hp = 900
        clone.flags["_runMaxHp"] = 5
        engine._copy_back_battle_result(state, [clone], player_won=True)
        expected = map_gen.calc_hp(mon.base_stats.hp, 50) + 5
        self.assertEqual(state.team[0].max_hp, expected)
        self.assertEqual(state.team[0].flags.get("_runMaxHp"), 5)
        self.assertEqual(state.team[0].current_hp, min(900, expected))

    def test_loss_copies_only_currenthp(self):
        eng, state = _start(seed=32)
        mon = _mon(1, level=50)
        state.team = [mon]
        clone = _mon(1, level=99, max_hp=999)
        clone.current_hp = 42
        clone.types = ("Poison",)
        engine._copy_back_battle_result(state, [clone], player_won=False)
        self.assertEqual(state.team[0].current_hp, 42)
        self.assertEqual(state.team[0].level, 50)  # untouched on a loss
        self.assertEqual(state.team[0].max_hp, mon.max_hp)  # untouched on a loss
        self.assertEqual(state.team[0].types, mon.types)


class WonderGuardHpTests(unittest.TestCase):
    def test_wild_wonder_guard_mon_has_1_hp(self):
        shedinja_ability = engine.battle_abilities.get_gen3_ability(292)
        mon = engine._make_wild_combatant(292, level=50)  # Shedinja
        if shedinja_ability == "wonder_guard":
            self.assertEqual(mon.max_hp, 1)
            self.assertEqual(mon.current_hp, 1)


class MoveTutorAndItemTests(unittest.TestCase):
    def test_move_tutor_bumps_tier_capped_at_two(self):
        eng, state = _start(seed=11)
        state.team[0].move_tier = 1
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.MOVE_TUTOR
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.MOVE_TUTOR_CHOICE)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.team[0].move_tier, 2)
        self.assertTrue(state.used_tm)

        # Visiting again with everyone already at tier 2 should skip straight through.
        node2 = next(n for n in state.map.nodes.values() if n.accessible and n.id != node.id)
        node2.type = map_gen.MOVE_TUTOR
        state = eng.step(engine.VisitNode(node_id=node2.id))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_item_choice_usable_goes_straight_to_bag(self):
        eng, state = _start(seed=12)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.ITEM
        # rare_candy is unconditionally eligible (docs/logic-notes-nodes.md
        # section 5) but the 3-item offer is shuffled from a larger pool --
        # force an empty passive-item pool so the usable items (which always
        # include rare_candy) are guaranteed to be offered, keeping this
        # test deterministic rather than seed-dependent.
        with patch.object(engine.data, "get_passive_items", return_value=()):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.ITEM_CHOICE)
        rare_candy_idx = next(i for i, o in enumerate(state.pending.options) if o["id"] == "rare_candy")
        state = eng.step(engine.SelectOption(index=rare_candy_idx))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertIn("rare_candy", state.items)
        self.assertTrue(state.picked_up_item)

    def test_item_choice_passive_requires_equip_target(self):
        eng, state = _start(seed=12)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.ITEM
        state = eng.step(engine.VisitNode(node_id=node.id))
        passive_idx = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        item_id = state.pending.options[passive_idx]["id"]
        state = eng.step(engine.SelectOption(index=passive_idx))
        self.assertEqual(state.phase, engine.Phase.ITEM_EQUIP_CHOICE)
        with self.assertRaises(ValueError):
            eng.step(engine.SelectOption(index=None))  # equip target is not optional
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.team[0].held_item.id, item_id)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_mega_question_resolution_reuses_item_handler(self):
        eng, state = _start(seed=12)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        state.question_cache[node.id] = "mega"
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertIn(state.phase, (engine.Phase.ITEM_CHOICE, engine.Phase.ON_MAP))


class EquipItemLegalityTests(unittest.TestCase):
    """P0.5: the source's team-bar click handler
    (bundle.deobfuscated.js:64943-64950) routes `item.usable` items only to
    `applyUsableItemTo`/`UseItem`, never `equipItemFromBag`/`EquipItem` --
    the low-level helper itself has no such check, but the public action
    surface must preserve the dispatch distinction."""

    def test_mixed_bag_exposes_only_passive_equip_indices(self):
        eng, state = _start(seed=20)
        state.items = ["rare_candy", "eviolite", "moon_stone", "leftovers", "tm_normal"]
        actions = engine.legal_actions(state)
        self.assertEqual(actions["equip_item"]["bag_indices"], [1, 3])

    def test_usable_items_remain_exposed_through_use_item(self):
        eng, state = _start(seed=20)
        state.items = ["rare_candy", "eviolite"]
        actions = engine.legal_actions(state)
        self.assertIn("rare_candy", {e["item_id"] for e in actions["use_item"]})

    def test_direct_rare_candy_equip_attempt_is_rejected(self):
        eng, state = _start(seed=20)
        state.items = ["rare_candy"]
        with self.assertRaises(ValueError):
            eng.step(engine.EquipItem(bag_index=0, team_index=0))

    def test_direct_sacred_ash_equip_attempt_is_rejected(self):
        eng, state = _start(seed=20)
        state.items = ["sacred_ash"]
        with self.assertRaises(ValueError):
            eng.step(engine.EquipItem(bag_index=0, team_index=0))

    def test_direct_moon_stone_equip_attempt_is_rejected(self):
        eng, state = _start(seed=20)
        state.items = ["moon_stone"]
        with self.assertRaises(ValueError):
            eng.step(engine.EquipItem(bag_index=0, team_index=0))

    def test_direct_tm_equip_attempt_is_rejected(self):
        eng, state = _start(seed=20)
        state.items = ["tm_normal"]
        with self.assertRaises(ValueError):
            eng.step(engine.EquipItem(bag_index=0, team_index=0))

    def test_rejected_equip_leaves_bag_and_held_item_unchanged(self):
        eng, state = _start(seed=20)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        state.items = ["moon_stone", "eviolite"]
        with self.assertRaises(ValueError):
            eng.step(engine.EquipItem(bag_index=0, team_index=0))
        self.assertEqual(state.items, ["moon_stone", "eviolite"])
        self.assertEqual(state.team[0].held_item.id, "leftovers")

    def test_unknown_item_id_is_rejected_not_silently_equipped(self):
        eng, state = _start(seed=20)
        state.items = ["totally_bogus_item"]
        with self.assertRaises(ValueError):
            eng.step(engine.EquipItem(bag_index=0, team_index=0))
        self.assertEqual(state.items, ["totally_bogus_item"])
        self.assertIsNone(state.team[0].held_item)

    def test_ordinary_held_item_equip_and_swap_still_works(self):
        eng, state = _start(seed=20)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        state.items = ["eviolite"]
        state = eng.step(engine.EquipItem(bag_index=0, team_index=0))
        self.assertEqual(state.team[0].held_item.id, "eviolite")
        self.assertEqual(state.items, ["leftovers"])  # old held item pushed back to bag

    def test_api_action_path_inherits_engine_rejection(self):
        from pokelike.webui import state_json

        eng, state = _start(seed=20)
        state.items = ["rare_candy"]
        action = state_json.decode_action({"type": "EquipItem", "bag_index": 0, "team_index": 0})
        with self.assertRaises(ValueError):
            eng.step(action)


class TradeAndLegendaryAndShinyTests(unittest.TestCase):
    def test_trade_decline_is_a_no_op(self):
        eng, state = _start(seed=13)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.TRADE_CHOICE)
        species_before = state.team[0].species_id
        state = eng.step(engine.SelectOption(index=None))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(state.team[0].species_id, species_before)

    def test_trade_accept_swaps_team_member(self):
        eng, state = _start(seed=13)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertTrue(state.got_via_question)

    def test_legendary_node_uses_preassigned_species_and_autocatches(self):
        eng, state = _start(seed=14)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.LEGENDARY
        node.extra["legendarySpeciesId"] = 144  # Articuno
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 2)
        self.assertEqual(state.team[1].species_id, 144)
        self.assertEqual(state.team[1].current_hp, state.team[1].max_hp)  # caught at full HP, not the fainted battle instance

    def test_legendary_node_without_species_is_a_safe_noop(self):
        eng, state = _start(seed=14)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.LEGENDARY
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 1)

    def test_shiny_node_is_always_shiny_and_first_candidate(self):
        eng, state = _start(seed=15)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = "shiny"
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.CATCH_CHOICE)
        self.assertTrue(state.pending.options[0]["is_shiny"])
        state = eng.step(engine.SelectOption(index=0))
        self.assertTrue(state.team[-1].is_shiny)
        self.assertTrue(state.got_via_question)

    def test_shiny_node_uses_move_tier_for_map_and_does_not_evolve(self):
        # CODEX.md issues 9-10: `doShinyNode` passes `getMoveTierForMap`
        # and NEVER calls `resolveEvoForLevel` -- Charmander (4) offered at
        # a level that would normally evolve it via the catch/battle path
        # must stay Charmander here.
        eng, state = _start(seed=15)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = "shiny"
        with patch.object(engine.map_gen, "get_catch_choices", return_value=[4]):
            with patch.object(engine.map_gen, "get_level_for_node", return_value=50):
                state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.pending.extra["candidates"][0].species_id, 4)
        expected_tier = map_gen.get_move_tier_for_map(state.current_map)
        self.assertEqual(state.pending.extra["candidates"][0].move_tier, expected_tier)

    def test_shiny_node_requests_three_candidates(self):
        eng, state = _start(seed=15)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = "shiny"
        with patch.object(engine.map_gen, "get_catch_choices", return_value=[4]) as mocked:
            eng.step(engine.VisitNode(node_id=node.id))
        args, kwargs = mocked.call_args
        self.assertEqual(args[1], 3)
        self.assertTrue(kwargs.get("exclude_starters"))

    def test_trade_replacement_requests_18_candidates_excluding_starters(self):
        eng, state = _start(seed=13)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        with patch.object(engine.map_gen, "get_catch_choices", wraps=engine.map_gen.get_catch_choices) as mocked:
            eng.step(engine.SelectOption(index=0))
        args, kwargs = mocked.call_args
        self.assertEqual(args[1], 18)
        self.assertTrue(kwargs.get("exclude_starters"))

    def test_trade_does_not_transfer_held_item_when_no_replacement_exists(self):
        eng, state = _start(seed=13)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        with patch.object(engine.map_gen, "get_catch_choices", return_value=[]):
            state = eng.step(engine.SelectOption(index=0))
        self.assertNotIn("leftovers", state.items)
        self.assertEqual(state.team[0].held_item.id, "leftovers")

    def test_trade_transfers_held_item_only_after_successful_replacement(self):
        eng, state = _start(seed=13)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=0))
        self.assertIn("leftovers", state.items)


class ShinyFormulaTests(unittest.TestCase):
    """CODEX.md issues 5-6: `rollShiny`/`legendaryShinyChanceFlat`
    (bundle.deobfuscated.js:74912-74957) are 1% base / 2% with Shiny Charm,
    doubled again by the `shiny_rate` trait -- not the old 1/128 placeholder.
    """

    def test_base_rate_is_one_percent(self):
        eng, state = _start(seed=1)
        self.assertEqual(engine._shiny_chance(state), 0.01)

    def test_shiny_charm_doubles_to_two_percent(self):
        eng = engine.Engine()
        state = eng.reset(shiny_charm=True, seed=2)
        state = eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        self.assertEqual(engine._shiny_chance(state), 0.02)

    def test_shiny_rate_trait_doubles_again(self):
        eng = engine.Engine()
        state = eng.reset(shiny_charm=True, seed=3, passives=[battle.Trait("shiny_rate")])
        state = eng.step(engine.ChooseStarter(species_id=state.pending.options[0]["species_id"]))
        self.assertEqual(engine._shiny_chance(state), 0.04)

    def test_roll_shiny_consumes_exactly_one_rng_draw(self):
        eng, state = _start(seed=4)
        rng.seed_rng(123)
        before = rng.get_rng_seed()
        engine.roll_shiny(state)
        after_one_call = rng.get_rng_seed()
        self.assertNotEqual(before, after_one_call)
        # Reseed and manually advance once -- must match exactly one call's effect.
        rng.seed_rng(123)
        rng.rng()
        self.assertEqual(rng.get_rng_seed(), after_one_call)


class EliteFourAndVictoryTests(unittest.TestCase):
    def test_elite_four_gauntlet_wins_through_to_victory(self):
        eng, state = _start(seed=16)
        state.current_map = 8
        boss = state.map.layers[-1][0]
        boss.accessible = True
        roster = data.get_elite_four(1)
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.phase, engine.Phase.VICTORY)
        self.assertTrue(state.won)
        self.assertEqual(state.elite_index, 0)  # reset after a clean sweep

    def test_elite_four_loss_partway_ends_the_run_and_keeps_index(self):
        eng, state = _start(seed=16)
        state.current_map = 8
        boss = state.map.layers[-1][0]
        boss.accessible = True
        roster = data.get_elite_four(1)
        call_count = {"n": 0}

        def fake_run_battle(player_team, enemy_team, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return _loss(player_team)
            return _win(player_team)

        with patch.object(engine.battle_loop, "run_battle", side_effect=fake_run_battle):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(state.elite_index, 1)  # resume checkpoint left where the loss happened

    def test_evolution_choice_interrupts_and_resumes_elite_four_gauntlet(self):
        eng, state = _start(seed=16)
        state.team = [_mon(133, level=25)]  # Eevee -- will hit a branching choice after the first win
        state.current_map = 8
        boss = state.map.layers[-1][0]
        boss.accessible = True
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.phase, engine.Phase.EVOLUTION_CHOICE)
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.SelectOption(index=0))
        # gauntlet should have continued (and finished, since every fight is stubbed as a win)
        self.assertEqual(state.phase, engine.Phase.VICTORY)


class RealBattleSmokeTest(unittest.TestCase):
    """One end-to-end test using the REAL battle_loop.run_battle (no stub),
    to catch wiring mistakes the mocked tests above can't see."""

    def test_overpowered_team_beats_a_wild_encounter_for_real(self):
        rng.seed_rng(123)
        eng, state = _start(seed=17)
        state.team[0].level = 90
        state.team[0].max_hp = map_gen.calc_hp(state.team[0].base_stats.hp, 90)
        state.team[0].current_hp = state.team[0].max_hp
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertGreater(state.team[0].level, 90)
        self.assertTrue(state.log)
        self.assertEqual(state.log[-2]["type"], "battle")
        self.assertTrue(state.log[-2]["won"])


if __name__ == "__main__":
    unittest.main()
