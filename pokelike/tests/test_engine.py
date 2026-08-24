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

    def test_starter_offer_materialises_three_real_instances_and_draws_three(self):
        # `showStarterSelect`'s Story branch (bundle.deobfuscated.js:76175-76194)
        # runs `rollShiny()` then `createInstance(entry, 5, shiny, 0)` once per
        # offered starter, BEFORE the click. Three offers, three draws.
        eng = engine.Engine()
        eng.reset(seed=1)
        drawn = eng._rng_stream  # the run's own stream
        instances = eng.state.pending.extra["instances"]
        self.assertEqual(len(instances), 3)
        self.assertEqual(
            [m.species_id for m in instances],
            list(data.get_starter_ids(1)),
            "offers must be in the source's own STARTER_IDS order",
        )
        for mon in instances:
            self.assertEqual(mon.level, 5)          # `B2l = 0x5` at 75648
            self.assertEqual(mon.move_tier, 0)      # `createInstance(..., 0x0)`
            self.assertEqual(mon.current_hp, mon.max_hp)
        self.assertEqual(
            [o["species_id"] for o in eng.state.pending.options],
            [m.species_id for m in instances],
            "the public option list must stay in the offered order",
        )
        self.assertIsNotNone(drawn)

    def test_starter_offer_consumes_exactly_three_draws(self):
        from pokelike import rng as rng_mod

        eng = engine.Engine()
        eng.reset(seed=1)
        previous = rng_mod.set_active_stream(eng._rng_stream)
        try:
            after_offer = rng_mod.get_rng_seed()
        finally:
            rng_mod.set_active_stream(previous)
        # Reproduce the same three draws independently, on a separate stream,
        # straight from the raw seed: exactly three and no more.
        previous = rng_mod.set_active_stream(engine.Engine()._rng_stream)
        try:
            rng_mod.seed_rng(1)
            for _ in range(3):
                rng_mod.rng()
            expected = rng_mod.get_rng_seed()
        finally:
            rng_mod.set_active_stream(previous)
        self.assertEqual(after_offer, expected, "the offer must consume exactly three draws")

    def test_selected_starter_is_the_offered_object_not_a_rebuild(self):
        # `selectStarter(BIv)` installs the very instance the clicked card
        # closed over: `state["team"] = [B]` at 76206. Identity, not equality.
        eng = engine.Engine()
        eng.reset(seed=1)
        offered = list(eng.state.pending.extra["instances"])
        chosen = offered[1]
        state = eng.step(engine.ChooseStarter(species_id=chosen.species_id))
        self.assertIs(state.team[0], chosen)

    def test_selected_starter_keeps_its_offer_time_shininess(self):
        # The port used to force `is_shiny = False` on the chosen starter,
        # making a shiny starter unreachable (frozen blocker 1). The offered
        # instance's own roll is what counts.
        eng = engine.Engine()
        eng.reset(seed=1)
        offered = eng.state.pending.extra["instances"]
        offered[2].is_shiny = True
        state = eng.step(engine.ChooseStarter(species_id=offered[2].species_id))
        self.assertTrue(state.team[0].is_shiny)

    def test_unselected_starter_offers_do_not_reach_the_team(self):
        eng = engine.Engine()
        eng.reset(seed=1)
        offered = list(eng.state.pending.extra["instances"])
        state = eng.step(engine.ChooseStarter(species_id=offered[0].species_id))
        self.assertEqual(len(state.team), 1)
        self.assertIs(state.team[0], offered[0])
        for other in offered[1:]:
            self.assertNotIn(other, state.team)

    def test_two_engines_offer_independently_from_the_same_seed(self):
        a, b = engine.Engine(), engine.Engine()
        a.reset(seed=7)
        b.reset(seed=7)
        self.assertEqual(
            [(m.species_id, m.is_shiny, m.max_hp) for m in a.state.pending.extra["instances"]],
            [(m.species_id, m.is_shiny, m.max_hp) for m in b.state.pending.extra["instances"]],
        )
        self.assertIsNot(a.state.pending.extra["instances"][0],
                         b.state.pending.extra["instances"][0])

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
        # bundle.deobfuscated.js:81358-81380: the fainted-cull only runs in
        # `runBattleScreen`'s WIN branch -- an ordinary LOSS (this test)
        # never touches `state["team"]` at all, so the fainted member stays
        # in the roster (HP 0) rather than being filtered out. GAME_OVER
        # still fires because `not result.player_won` alone triggers it.
        eng, state = _start(seed=6)
        state.nuzlocke_mode = True
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.BATTLE
        result = _loss(state.team)  # sets currentHp=0 on the only team member
        with patch.object(engine.battle_loop, "run_battle", return_value=result):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.GAME_OVER)
        self.assertEqual(len(state.team), 1)  # NOT culled on loss -- fainted member preserved
        self.assertEqual(state.team[0].current_hp, 0)

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
        # `cancel` is M7's corrected declaration of `_resolve_pending`'s THIRD
        # exit (`#btn-equip-cancel`, 79563-79569). It mirrors that resolver's
        # own gate, so it is FALSE here: the escape-rope offer has a skip
        # (`optional`) but no cancel affordance.
        self.assertEqual(
            actions,
            {"select_option": {"indices": [0], "optional": True, "cancel": False}},
        )
        self.assertFalse(actions["select_option"]["cancel"])

    def test_cancel_is_declared_only_for_the_item_equip_phase(self):
        """`legal_actions` may declare `cancel: True` on exactly the one
        phase `_resolve_pending` accepts it on. The source has a single
        `#btn-equip-cancel` (79563-79569), on `openItemEquipModal`'s
        overlay, so every other pending phase must declare it False."""
        eng, state = _start(seed=20)
        for phase in engine.Phase:
            if phase not in engine._PENDING_RESOLVERS:
                continue
            state.phase = phase
            state.pending = engine.PendingChoice(
                phase=phase, options=[{"slot": 0}], optional=True, extra={})
            declared = engine.legal_actions(state)["select_option"]["cancel"]
            self.assertEqual(
                declared, phase == engine.Phase.ITEM_EQUIP_CHOICE,
                f"{phase.value} declared cancel={declared}")
            # ...and the declaration matches what the resolver accepts.
            if not declared:
                with self.assertRaises(ValueError):
                    eng.step(engine.SelectOption(index=None, cancel=True))

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
        # The rope continuation is the SAME callback a win would have run, and
        # since M4 repair 6 that callback is `offer_swap` -- `showSwapScreen`
        # unconditionally (80457), not `catchPokemon`'s room-based auto-add. So
        # the legendary is pending, not yet on the team.
        self.assertEqual(state.phase, engine.Phase.SWAP_CHOICE)
        self.assertEqual(len(state.team), 1)
        self.assertEqual(state.pending.extra["incoming"].species_id, 144)
        state = eng.step(engine.SelectOption(index=0))
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
        engine._apply_evolution(state, mon, 5, data.get_evolutions()[4].name, force=True)  # Charmeleon
        self.assertEqual(mon.species_id, 5)
        self.assertEqual(mon.current_hp, 1)

    def test_augment_pct_scales_force_true_evolution_maxhp(self):
        eng, state = _start(seed=18)
        mon = _mon(4, level=16)
        mon.augment_pct = 50.0
        state.team = [mon]
        expected_without_augment = map_gen.calc_hp(data.get_pokedex()[5].base_stats.hp, 16)
        engine._apply_evolution(state, mon, 5, data.get_evolutions()[4].name, force=True)
        self.assertEqual(mon.max_hp, math.floor(expected_without_augment * 1.5))

    def test_non_forced_evolution_keeps_a_fainted_mon_fainted(self):
        # checkAndEvolveTeam's own formula (force=False) -- the mirror image
        # of the quirk above: a mon already at 0 HP when evolution is
        # checked stays at 0 HP afterward.
        eng, state = _start(seed=19)
        mon = _mon(4, level=16)
        mon.current_hp = 0
        state.team = [mon]
        engine._apply_evolution(state, mon, 5, data.get_evolutions()[4].name, force=False)
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

        # Visiting again with the WHOLE team at tier 2: doMoveTutorNode has no
        # early-bail (bundle.deobfuscated.js:80464-80563) -- the modal still
        # opens, with zero [data-tutor] buttons and only the skip control, so
        # this raises MOVE_TUTOR_CHOICE with an empty (decline-only) options
        # list rather than skipping straight to ON_MAP.
        for mon in state.team:
            mon.move_tier = 2
        node2 = next(n for n in state.map.nodes.values() if n.accessible and n.id != node.id)
        node2.type = map_gen.MOVE_TUTOR
        state = eng.step(engine.VisitNode(node_id=node2.id))
        self.assertEqual(state.phase, engine.Phase.MOVE_TUTOR_CHOICE)
        self.assertEqual(state.pending.options, [])
        self.assertTrue(state.pending.optional)
        state = eng.step(engine.SelectOption(index=None))
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
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.team[0].held_item.id, item_id)
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_item_equip_decline_keeps_item_in_bag(self):
        """`openItemEquipModal`'s `#btn-equip-to-bag` (79552-79562), the
        source's real decline exit for the reachable `fromBagIdx=-1,
        fromPokemonIdx=-1` configuration `doItemNode` always uses
        (79423-79429): banks the item instead of losing it. Found while
        tracing the exact source for the M4 route-oracle item-equip bridge --
        a prior version of this resolver had no decline branch at all and
        `PendingChoice.optional` was `False`, which was a real gap, not a
        simplification."""
        eng, state = _start(seed=12)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.ITEM
        state = eng.step(engine.VisitNode(node_id=node.id))
        passive_idx = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        item_id = state.pending.options[passive_idx]["id"]
        state = eng.step(engine.SelectOption(index=passive_idx))
        self.assertEqual(state.phase, engine.Phase.ITEM_EQUIP_CHOICE)
        self.assertTrue(state.pending.optional)
        state = eng.step(engine.SelectOption(index=None))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertIn(item_id, state.items)
        self.assertIsNone(state.team[0].held_item)

    # -- M5: `state.itemOffer`, the fourth resume guard --------------------
    #
    # Source facts these pin (bundle.deobfuscated.js):
    #   79371-79375  the rolled offer is pinned as {nodeId, ids}
    #   79360-79364  a pinned offer for THIS node id is rebuilt from the saved
    #                ids through the COMBINED pools, drawing no RNG; ids that
    #                do not resolve are dropped, and only an empty result
    #                falls through to a fresh roll
    #   79419-79422  the usable pick nulls it
    #   79424-79428  the equip modal's onComplete nulls it
    #   79433-79437  #btn-skip-item nulls it
    #   79563-79569  #btn-equip-cancel does NONE of that -- its whole body is
    #                `B2O.remove()`
    #   76228-76245  startMap clears none of the resume guards
    #
    # Directly observed on the real source (probe over the audited prefix,
    # seeds 333333333 and 222222222): first visit 20 draws, cancel 0 draws,
    # second visit 0 draws with a byte-identical id list.

    def _item_node_offer(self, seed=12):
        eng, state = _start(seed=seed)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.ITEM
        state = eng.step(engine.VisitNode(node_id=node.id))
        return eng, state, node

    def test_item_offer_is_pinned_at_the_roll(self):
        _, state, node = self._item_node_offer()
        self.assertEqual(state.item_offer["node_id"], node.id)
        self.assertEqual(
            state.item_offer["item_ids"],
            [o["id"] for o in state.pending.options],
        )

    def test_item_offer_cleared_by_skip(self):
        eng, state, _ = self._item_node_offer()
        state = eng.step(engine.SelectOption(index=None))
        self.assertIsNone(state.item_offer)

    def test_item_offer_cleared_by_usable_pick(self):
        eng, state = _start(seed=12)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.ITEM
        with patch.object(engine.data, "get_passive_items", return_value=()):
            state = eng.step(engine.VisitNode(node_id=node.id))
        idx = next(i for i, o in enumerate(state.pending.options) if o["usable"])
        state = eng.step(engine.SelectOption(index=idx))
        self.assertIsNone(state.item_offer)

    def test_item_offer_cleared_by_equip_and_by_keep_in_bag(self):
        for pick in (0, None):
            eng, state, _ = self._item_node_offer()
            passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
            state = eng.step(engine.SelectOption(index=passive))
            self.assertEqual(state.phase, engine.Phase.ITEM_EQUIP_CHOICE)
            state = eng.step(engine.SelectOption(index=pick))
            self.assertIsNone(state.item_offer, f"equip exit index={pick}")

    def test_equip_cancel_consumes_nothing_and_keeps_the_offer(self):
        """`#btn-equip-cancel` (79563-79569) is `B2O.remove()` and nothing
        else: no equip, no bank, no onComplete, so no advance and no clear.

        M7-COMBINED (F-A) updated the two SCREEN assertions here, and only
        those. `B2O` is the equip OVERLAY; removing it uncovers the
        `item-screen` that `doItemNode` put up at 79263 and never took down,
        with its three `.item-card` listeners and `#btn-skip-item` still
        attached. So the run does not go back to the map -- it goes back to
        the very same item offer, which can be picked again. The earlier
        expectation (`ON_MAP` / `pending is None`) is what the M7 sweep
        reported as finding F-A. Everything this test says about what cancel
        CONSUMES is unchanged, because that half was always right.
        """
        eng, state, node = self._item_node_offer()
        pinned = dict(state.item_offer)
        bag_before = list(state.items)
        offered = [o["id"] for o in state.pending.options]
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        state = eng.step(engine.SelectOption(index=passive))
        state = eng.step(engine.SelectOption(cancel=True))

        # Back on the still-live item offer, not on the map.
        self.assertEqual(state.phase, engine.Phase.ITEM_CHOICE)
        self.assertIsNotNone(state.pending)
        self.assertEqual(state.pending.phase, engine.Phase.ITEM_CHOICE)
        self.assertEqual([o["id"] for o in state.pending.options], offered)
        self.assertTrue(state.pending.optional)             # #btn-skip-item
        self.assertEqual(state.items, bag_before)           # nothing banked
        self.assertIsNone(state.team[0].held_item)          # nothing equipped
        self.assertFalse(state.map.nodes[node.id].visited)  # not advanced
        self.assertTrue(state.map.nodes[node.id].accessible)
        self.assertEqual(state.item_offer, pinned)          # still pinned

    def test_the_offer_restored_by_cancel_can_be_picked_again(self):
        """M7-COMBINED (F-A). The point of staying on `item-screen` is that
        the cards still work: the same offer can be re-entered, and the second
        pick behaves exactly like the first. Cancel is a loop, not an exit.
        """
        eng, state, node = self._item_node_offer()
        offered = [o["id"] for o in state.pending.options]
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        eng.step(engine.SelectOption(index=passive))
        state = eng.step(engine.SelectOption(cancel=True))

        # Re-enter the SAME card and complete the equip this time.
        before = eng._rng_stream.state
        state = eng.step(engine.SelectOption(index=passive))
        self.assertEqual(state.phase, engine.Phase.ITEM_EQUIP_CHOICE)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(eng._rng_stream.state, before, "re-entry must draw no RNG")
        self.assertEqual(state.team[0].held_item.id, offered[passive])
        self.assertIsNone(state.item_offer)                 # onComplete cleared it
        self.assertTrue(state.map.nodes[node.id].visited)   # and advanced
        self.assertEqual(state.phase, engine.Phase.ON_MAP)

    def test_cancel_then_skip_leaves_the_map_exactly_as_a_plain_skip_would(self):
        """M7-COMBINED (F-A). The other exit from the restored offer is
        `#btn-skip-item` (79433-79437), and taking it after a cancel must be
        indistinguishable from taking it without one -- the cancel consumed
        nothing, so it cannot have moved the run."""
        eng, state, node = self._item_node_offer()
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        eng.step(engine.SelectOption(index=passive))
        eng.step(engine.SelectOption(cancel=True))
        state = eng.step(engine.SelectOption(index=None))

        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertIsNone(state.pending)
        self.assertIsNone(state.item_offer)
        self.assertTrue(state.map.nodes[node.id].visited)

    def test_a_pinned_offer_restores_with_no_rng_at_the_same_node_id(self):
        """The decisive transition: the restore branch draws NO RNG and
        reproduces the offer exactly (79360-79364).

        M7-COMBINED (F-A) re-based this test's SETUP. It used to reach the map
        by cancelling the equip modal, on the (wrong) premise that cancel
        returns to `map-screen`; it does not, so that route to a revisit does
        not exist. The restore branch is still exactly as reachable as it ever
        was -- `startMap` (76228-76245) clears no offer and the pin's key is
        the bare node id -- so the map transition is what this now uses, which
        is the same real path `test_restore_keeps_an_item_a_fresh_roll_would_
        now_reject` already exercises. The CONTRACT asserted is unchanged.
        """
        eng, state, node = self._item_node_offer()
        first = [o["id"] for o in state.pending.options]
        pinned = dict(state.item_offer)

        # Cancel really does keep the pin alive across the screen it stays on.
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        eng.step(engine.SelectOption(index=passive))
        state = eng.step(engine.SelectOption(cancel=True))
        self.assertEqual(state.item_offer, pinned)
        self.assertFalse(state.map.nodes[node.id].visited)

        engine._start_map(state, 1)
        same = state.map.nodes[node.id]
        self.assertTrue(same.accessible)
        self.assertFalse(same.visited)
        same.type = map_gen.ITEM

        before = eng._rng_stream.state
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(eng._rng_stream.state, before, "restore must not draw RNG")
        self.assertEqual([o["id"] for o in state.pending.options], first)

    def test_restore_ignores_eligibility_and_drops_unresolvable_ids(self):
        """`B2P` resolves ids through the COMBINED pools with no eligibility
        re-test, and `.filter(Boolean)` drops what does not resolve."""
        eng, state, node = self._item_node_offer()
        real = state.item_offer["item_ids"][0]
        eng.step(engine.SelectOption(index=None))  # clear, then re-pin by hand
        state.item_offer = {"node_id": node.id, "item_ids": [real, "no_such_item_id"]}
        state.map.nodes[node.id].visited = False
        state.map.nodes[node.id].accessible = True
        state.phase = engine.Phase.ON_MAP

        before = eng._rng_stream.state
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(eng._rng_stream.state, before)
        self.assertEqual([o["id"] for o in state.pending.options], [real])

    def test_pinned_offer_for_another_node_is_not_restored(self):
        """The guard is `state.itemOffer.nodeId === B.id` (79361), so an
        offer pinned for a DIFFERENT node must not be reused -- it must roll
        fresh. Without this control, dropping the node-id test entirely is
        invisible: every other test pins the offer on the node it visits."""
        eng, state, node = self._item_node_offer()
        eng.step(engine.SelectOption(index=None))  # clears the real offer
        state.item_offer = {"node_id": "n99_9", "item_ids": ["rare_candy"]}

        other = next(
            n for n in state.map.nodes.values()
            if n.accessible and n.id != node.id
        )
        other.type = map_gen.ITEM
        before = eng._rng_stream.state
        state = eng.step(engine.VisitNode(node_id=other.id))
        self.assertNotEqual(before, eng._rng_stream.state, "must roll fresh")
        self.assertEqual(state.item_offer["node_id"], other.id)
        self.assertNotEqual([o["id"] for o in state.pending.options], ["rare_candy"])

    def test_restore_falls_through_to_a_fresh_roll_when_nothing_resolves(self):
        eng, state, node = self._item_node_offer()
        eng.step(engine.SelectOption(index=None))
        state.item_offer = {"node_id": node.id, "item_ids": ["nope_a", "nope_b"]}
        state.map.nodes[node.id].visited = False
        state.map.nodes[node.id].accessible = True
        state.phase = engine.Phase.ON_MAP

        before = eng._rng_stream.state
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertNotEqual(eng._rng_stream.state, before, "empty restore must re-roll")
        self.assertTrue(state.pending.options)

    def test_pinned_offer_is_keyed_by_bare_node_id_not_by_map(self):
        """`state.itemOffer.nodeId === B.id` (79361) is the BARE id, exactly
        like `savedCatch` (78441), and `startMap` clears none of the guards --
        so a cancelled offer really can be restored at the same node id on a
        LATER map. Mirrored, not tidied away."""
        eng, state, node = self._item_node_offer()
        pinned = dict(state.item_offer)
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        eng.step(engine.SelectOption(index=passive))
        state = eng.step(engine.SelectOption(cancel=True))

        engine._start_map(state, 1)
        self.assertEqual(state.current_map, 1)
        self.assertEqual(state.item_offer, pinned)

    # --- M5.1: the "restore does not re-test eligibility" contract -----------
    #
    # The M5 independent closure audit (docs/audits/M5-independent-closure-
    # audit.md, blocker B1) found this contract had NO detector: a
    # non-equivalent mutant that re-applies `passive_eligible`/
    # `usable_eligible` while resolving a non-empty pinned offer survived the
    # whole discovery suite, the focused route-oracle suite and the strict
    # route gate.
    #
    # The reason is structural, not accidental: every restore test above pins
    # and restores against UNCHANGED eligibility -- same map, immediately
    # after the roll -- so the filter is a no-op there and re-applying it
    # cannot change the result. `test_restore_ignores_eligibility_and_drops_
    # unresolvable_ids` proves only the second half of its name. The tests
    # below change eligibility BETWEEN the pin and the restore, which is the
    # only state in which the two behaviours differ at all.
    #
    # Source facts (bundle.deobfuscated.js):
    #   79279-79289  the fresh roll's passive pool IS filtered
    #   79306-79317  the fresh roll's usable pool IS filtered; in a non-Endless
    #                run `moon_stone` is eligible only while
    #                `state.currentMap <= 2` (79309-79315)
    #   79348-79358  `B2P` is a BARE id lookup over the combined pools plus a
    #                mega-stone fallback -- it applies no eligibility test
    #   79360-79364  the restore maps the pinned ids through `B2P` and, if
    #                anything survives, skips the roll (and its RNG) entirely
    #   79371-79375  the pin's key is the BARE node id, so it survives startMap

    BOUNDARY_ITEM = "moon_stone"

    def _fresh_roll_ids_at_map(self, seed, map_index):
        """Roll a REAL item node through the production path with the pool
        narrowed to the boundary item plus one always-eligible usable
        (`rare_candy`), and return the offered ids. This measures the
        fresh-roll eligibility predicate itself rather than asserting it."""
        eng, state = _start(seed=seed)
        if map_index != 0:
            engine._start_map(state, map_index)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.ITEM
        pool = tuple(i for i in data.get_usable_items() if i.id in (self.BOUNDARY_ITEM, "rare_candy"))
        self.assertEqual(len(pool), 2, "both control items must exist in the usable pool")
        with patch.object(engine.data, "get_passive_items", return_value=()), \
             patch.object(engine.data, "get_usable_items", return_value=pool):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertIsNotNone(state.pending, "the control roll must produce a real offer")
        return [o["id"] for o in state.pending.options]

    @staticmethod
    def _counting_draws():
        """Patch the one draw path (`_shuffle` -> `rng.rng()`, engine.py:819)
        with a counter, so "no RNG" is measured as a draw COUNT rather than
        inferred from the generator state alone."""
        real = engine.rng.rng
        counter = {"draws": 0}

        def counting():
            counter["draws"] += 1
            return real()

        return counter, patch.object(engine.rng, "rng", counting)

    def _boundary_offer(self):
        """Derive -- never hard-code -- a seed whose REAL rolled offer contains
        the boundary item and at least one passive, AND whose node id is still
        a reachable, unvisited node on the later map. Every precondition the
        detector depends on is asserted here, on real engine state."""
        for seed in range(1, 64):
            eng, state, node = self._item_node_offer(seed=seed)
            offered = state.pending.options
            if self.BOUNDARY_ITEM not in [o["id"] for o in offered]:
                continue
            if not any(not o["usable"] for o in offered):
                continue
            probe_eng, probe_state = _start(seed=seed)
            engine._start_map(probe_state, 3)
            later = probe_state.map.nodes.get(node.id)
            if later is None or later.visited or not later.accessible:
                continue
            return seed, eng, state, node
        self.fail(
            f"no seed in 1..63 rolled a {self.BOUNDARY_ITEM} offer with a passive "
            f"on a node id that is still reachable on map 3"
        )

    def test_fresh_roll_rejects_the_boundary_item_after_the_map_boundary(self):
        """Precondition control for the detector below, measured on the real
        fresh-roll path: `moon_stone` is offered while `currentMap <= 2` and
        filtered out from map 3 on (79309-79315)."""
        seed, _, _, _ = self._boundary_offer()
        for map_index in (0, 1, 2):
            self.assertIn(self.BOUNDARY_ITEM, self._fresh_roll_ids_at_map(seed, map_index))
        for map_index in (3, 4):
            self.assertNotIn(self.BOUNDARY_ITEM, self._fresh_roll_ids_at_map(seed, map_index))

    def test_restore_keeps_an_item_a_fresh_roll_would_now_reject(self):
        """The M5.1 detector. Eligibility genuinely changes between the pin and
        the restore, and the restore still reproduces the pinned list byte for
        byte for zero draws -- because `B2P` (79348-79358) applies no
        eligibility predicate at all."""
        seed, eng, state, node = self._boundary_offer()
        pinned = dict(state.item_offer)
        self.assertIn(self.BOUNDARY_ITEM, pinned["item_ids"])
        self.assertLessEqual(state.current_map, 2, "the boundary item must be eligible AT THE PIN")

        # The real cancel path: `#btn-equip-cancel` (79563-79569) is
        # `B2O.remove()` and nothing else, so the offer survives.
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        eng.step(engine.SelectOption(index=passive))
        state = eng.step(engine.SelectOption(cancel=True))
        self.assertEqual(state.item_offer, pinned)
        self.assertFalse(state.map.nodes[node.id].visited)

        # The real map transition. `startMap` (76228-76245) clears no offer,
        # and the pin's key is the bare node id, so the offer follows.
        engine._start_map(state, 3)
        self.assertEqual(state.current_map, 3)
        self.assertEqual(state.item_offer, pinned)

        # The eligibility predicate really has flipped by now.
        self.assertNotIn(self.BOUNDARY_ITEM, self._fresh_roll_ids_at_map(seed, 3))

        # Same BARE node id, a real reachable and unvisited node on this map.
        same = state.map.nodes[node.id]
        self.assertTrue(same.accessible)
        self.assertFalse(same.visited)
        same.type = map_gen.ITEM

        before = eng._rng_stream.state
        counter, counting = self._counting_draws()
        with counting:
            state = eng.step(engine.VisitNode(node_id=node.id))

        self.assertEqual(counter["draws"], 0, "the restore path must draw no RNG")
        self.assertEqual(eng._rng_stream.state, before)
        restored = [o["id"] for o in state.pending.options]
        self.assertEqual(restored, pinned["item_ids"], "the pinned list must be reproduced exactly")
        self.assertIn(
            self.BOUNDARY_ITEM,
            restored,
            "the restore must NOT re-apply the eligibility filter (79348-79358)",
        )
        self.assertEqual(state.item_offer, pinned)

    def test_restore_still_falls_through_when_nothing_resolves_after_the_boundary(self):
        """Control for the detector: the contract is not "always restore". In
        the SAME changed-eligibility state, an offer whose ids all fail to
        resolve falls through to a fresh roll and really does draw RNG
        (`!B2a || !B2a.length`, 79364)."""
        _, eng, state, node = self._boundary_offer()
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        eng.step(engine.SelectOption(index=passive))
        state = eng.step(engine.SelectOption(cancel=True))
        engine._start_map(state, 3)

        state.item_offer = {"node_id": node.id, "item_ids": ["nope_a", "nope_b"]}
        same = state.map.nodes[node.id]
        same.type = map_gen.ITEM

        before = eng._rng_stream.state
        counter, counting = self._counting_draws()
        with counting:
            state = eng.step(engine.VisitNode(node_id=node.id))

        self.assertGreater(counter["draws"], 0, "an empty restore must fall through and roll")
        self.assertNotEqual(eng._rng_stream.state, before)
        self.assertTrue(state.pending.options)
        self.assertEqual(state.item_offer["node_id"], node.id)
        self.assertNotIn(
            self.BOUNDARY_ITEM,
            [o["id"] for o in state.pending.options],
            "a FRESH roll at map 3 must not offer the boundary item",
        )

    def test_cancel_is_rejected_on_every_other_pending_phase(self):
        eng, state, _ = self._item_node_offer()
        self.assertEqual(state.phase, engine.Phase.ITEM_CHOICE)
        with self.assertRaises(ValueError):
            eng.step(engine.SelectOption(cancel=True))

    def test_cancel_with_an_index_is_rejected(self):
        eng, state, _ = self._item_node_offer()
        passive = next(i for i, o in enumerate(state.pending.options) if not o["usable"])
        eng.step(engine.SelectOption(index=passive))
        with self.assertRaises(ValueError):
            eng.step(engine.SelectOption(index=0, cancel=True))

    def test_mega_question_resolution_reuses_item_handler(self):
        eng, state = _start(seed=12)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.QUESTION
        # The record's key is map-qualified (`_resolve_question`, CODEX.md
        # issue 9). Seeding the bare node id never hit it, so this test used
        # to fall through to a live `rng()` roll and merely assert that
        # whatever came back was one of two phases -- it was passing on the
        # roll, not on the "mega" dispatch it names. Seed the real key so the
        # pinned type is actually used and no RNG is consumed.
        #
        # M4.2: this used to be a `question_cache` dict entry. The source
        # keeps ONE `savedQuestionResolve` record (bundle.deobfuscated.js:
        # 77326-77332), so the port does too; the key is unchanged.
        state.saved_question_resolve = {
            "key": f"m{state.current_map}:{node.id}",
            "resolved_type": "mega",
        }
        state = eng.step(engine.VisitNode(node_id=node.id))
        # `case "mega": doItemNode(B)` (77385-77386) -- the item handler
        # verbatim, so either an item offer or, with nothing offerable, a
        # straight advance.
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


class UnequipAndHandOffTests(unittest.TestCase):
    """M6 / the item-equip overlay's two remaining exits, which R3 disclosed
    as unbuilt because the engine had no action for either.

    Sources: the `[data-unequip]` rows (bundle.deobfuscated.js:79521-79531)
    and `#btn-equip-to-bag` with `fromPokemonIdx >= 0` (79549-79553) for the
    unequip; the `[data-idx]` row's `iu >= 0` branch (79541-79545) for the
    hand-off.
    """

    def test_unequip_moves_the_held_item_to_the_end_of_the_bag(self):
        eng, state = _start(seed=20)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        state.items = ["eviolite"]
        state = eng.step(engine.UnequipItem(team_index=0))
        self.assertIsNone(state.team[0].held_item)
        # `items.push`, not an insert: bag order is index-addressable.
        self.assertEqual(state.items, ["eviolite", "leftovers"])

    def test_unequip_is_offered_only_for_members_actually_holding(self):
        eng, state = _start(seed=20)
        self.assertNotIn("unequip_item", engine.legal_actions(state))
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        self.assertEqual(
            engine.legal_actions(state)["unequip_item"]["team_indices"], [0])

    def test_unequipping_an_empty_slot_is_rejected(self):
        eng, state = _start(seed=20)
        state.items = []
        with self.assertRaises(ValueError):
            eng.step(engine.UnequipItem(team_index=0))
        self.assertEqual(state.items, [])

    def test_hand_off_swaps_two_members_items_and_never_touches_the_bag(self):
        """The discriminating case, and the reason this is its own action
        rather than a composition: when the TARGET is also holding something,
        the source gives that item to the SOURCE member (79544-79545). An
        unequip-then-equip would route it to the bag instead."""
        eng, state = _start(seed=20)
        state.team.append(_mon(4, level=5))
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        state.team[1].held_item = battle.HeldItem(id="eviolite")
        state.items = ["moon_stone"]
        state = eng.step(engine.HandOffItem(from_index=0, to_index=1))
        self.assertEqual(state.team[0].held_item.id, "eviolite")
        self.assertEqual(state.team[1].held_item.id, "leftovers")
        self.assertEqual(state.items, ["moon_stone"])

    def test_hand_off_to_an_empty_member_leaves_the_source_empty(self):
        eng, state = _start(seed=20)
        state.team.append(_mon(4, level=5))
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        state.items = []
        state = eng.step(engine.HandOffItem(from_index=0, to_index=1))
        self.assertIsNone(state.team[0].held_item)
        self.assertEqual(state.team[1].held_item.id, "leftovers")
        self.assertEqual(state.items, [])

    def test_hand_off_from_an_empty_member_is_rejected(self):
        eng, state = _start(seed=20)
        state.team.append(_mon(4, level=5))
        with self.assertRaises(ValueError):
            eng.step(engine.HandOffItem(from_index=0, to_index=1))

    def test_hand_off_to_self_is_rejected(self):
        """The source labels the opening member's own row "Holding" (79470)
        and gives it a `[data-unequip]` button instead of a `[data-idx]` one
        (79490-79495), so there is no reachable hand-off to yourself."""
        eng, state = _start(seed=20)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        with self.assertRaises(ValueError):
            eng.step(engine.HandOffItem(from_index=0, to_index=0))

    def test_hand_off_needs_more_than_one_member_to_be_offered(self):
        eng, state = _start(seed=20)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        self.assertNotIn("hand_off_item", engine.legal_actions(state))
        state.team.append(_mon(4, level=5))
        legal = engine.legal_actions(state)
        self.assertEqual(legal["hand_off_item"]["from_indices"], [0])
        self.assertEqual(legal["hand_off_item"]["team_size"], 2)

    def test_out_of_range_indices_are_rejected_before_any_mutation(self):
        eng, state = _start(seed=20)
        state.team[0].held_item = battle.HeldItem(id="leftovers")
        state.items = []
        for action in (
            engine.UnequipItem(team_index=9),
            engine.HandOffItem(from_index=0, to_index=9),
            engine.HandOffItem(from_index=9, to_index=0),
        ):
            with self.assertRaises(ValueError):
                eng.step(action)
        self.assertEqual(state.team[0].held_item.id, "leftovers")
        self.assertEqual(state.items, [])

    def test_both_actions_decode_from_the_web_transport(self):
        from pokelike.webui import state_json

        self.assertEqual(
            state_json.decode_action({"type": "UnequipItem", "team_index": 1}),
            engine.UnequipItem(team_index=1),
        )
        self.assertEqual(
            state_json.decode_action(
                {"type": "HandOffItem", "from_index": 0, "to_index": 2}),
            engine.HandOffItem(from_index=0, to_index=2),
        )
        for bad in ({"type": "UnequipItem"}, {"type": "HandOffItem", "from_index": 0}):
            with self.assertRaises(state_json.ActionDecodeError):
                state_json.decode_action(bad)


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

    def test_legendary_node_uses_preassigned_species_and_offers_swap_with_room(self):
        # M4 repair 6: `doLegendaryNode`'s win callback ends in a bare
        # `showSwapScreen(B2P, B)` (bundle.deobfuscated.js:80457) with NO
        # `team.length < 6` test, so with room the legendary stays PENDING
        # until the player clicks its card. It does NOT auto-add the way
        # `catchPokemon` (79036) and `doShinyNode` (80962) do.
        eng, state = _start(seed=14)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.LEGENDARY
        node.extra["legendarySpeciesId"] = 144  # Articuno
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.SWAP_CHOICE)
        self.assertEqual(len(state.team), 1)  # still pending, nothing added yet
        self.assertTrue(state.pending.optional)
        self.assertEqual(len(state.pending.options), 1)  # the incoming card only
        self.assertTrue(state.pending.extra["has_room"])
        self.assertEqual(state.pending.extra["incoming"].species_id, 144)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 2)
        self.assertEqual(state.team[1].species_id, 144)
        self.assertEqual(state.team[1].current_hp, state.team[1].max_hp)  # caught at full HP, not the fainted battle instance
        # All three `showSwapScreen` exits clear `currentNode` (79186/79231/79256).
        self.assertIsNone(state.current_node_id)

    def test_legendary_node_room_decline_advances_without_changing_team(self):
        # `#btn-cancel-swap` (79249-79258): advance the node, change nothing.
        eng, state = _start(seed=14)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.LEGENDARY
        node.extra["legendarySpeciesId"] = 144
        before = [m.species_id for m in state.team]
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.SWAP_CHOICE)
        state = eng.step(engine.SelectOption(index=None))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual([m.species_id for m in state.team], before)
        self.assertTrue(state.map.nodes[node.id].visited)
        self.assertIsNone(state.current_node_id)

    def test_legendary_node_full_team_offers_ordered_release_choices(self):
        eng, state = _start(seed=14)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.LEGENDARY
        node.extra["legendarySpeciesId"] = 144
        # Species chosen so none of them evolves at or below level 10 -- the
        # post-battle `evolve` todo step runs over the whole team before the
        # swap offer, and an evolving member would change the ids under test
        # for a reason unrelated to the swap lifecycle.
        roster = (1, 4, 7, 16, 19, 23)
        state.team = [engine._make_wild_combatant(sid, 10) for sid in roster]
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.SWAP_CHOICE)
        self.assertFalse(state.pending.extra["has_room"])
        # One card per team member, in `state.team` order (79202-79246).
        self.assertEqual([o["species_id"] for o in state.pending.options], list(roster))
        state = eng.step(engine.SelectOption(index=2))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        # `state.team.splice(B2j, 1, incoming)` at 79230: released in place.
        self.assertEqual([m.species_id for m in state.team], [1, 4, 144, 16, 19, 23])
        self.assertIsNone(state.current_node_id)

    def test_legendary_node_full_team_decline_keeps_team(self):
        eng, state = _start(seed=14)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.LEGENDARY
        node.extra["legendarySpeciesId"] = 144
        roster = (1, 4, 7, 16, 19, 23)  # see the replace test: none evolves by level 10
        state.team = [engine._make_wild_combatant(sid, 10) for sid in roster]
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=node.id))
        state = eng.step(engine.SelectOption(index=None))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual([m.species_id for m in state.team], list(roster))
        self.assertIsNone(state.current_node_id)

    def test_ordinary_catch_still_auto_adds_with_room(self):
        # Regression control for repair 6: `catchPokemon`'s room branch
        # (79036-79044) really does auto-add, and unlike `showSwapScreen` it
        # leaves `currentNode` SET. Legendary's lifecycle must not be copied
        # onto it.
        eng, state = _start(seed=14)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.CATCH
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.CATCH_CHOICE)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 2)
        self.assertEqual(state.current_node_id, node.id)

    def test_shiny_node_still_auto_adds_with_room(self):
        # Regression control: `doShinyNode` inlines the same
        # `team.length < 6 ? push : showSwapScreen` test (80962-80970).
        eng, state = _start(seed=15)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        # `"shiny"` is a resolved QUESTION type, not a generated node type --
        # `onNodeClick`'s `case "shiny": doShinyNode(B)` (77383-77384), which
        # `_NODE_HANDLERS` mirrors with the same bare string key.
        node.type = "shiny"
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.CATCH_CHOICE)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertEqual(len(state.team), 2)
        self.assertTrue(state.team[1].is_shiny)
        self.assertEqual(state.current_node_id, node.id)

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
        # M4.1 INVERTED this assertion, against the real source.
        #
        # It previously read `assertFalse`, justified by "`doShinyNode`'s
        # accept handler never calls `catchPokemon`/`recordMonOrigin` -- it
        # inlines its own team push". Only the first half of that is true.
        # The handler does not call `catchPokemon`, but the body it inlines
        # contains a bare `recordMonOrigin(B)` at bundle.deobfuscated.js:80967,
        # in the room branch's comma sequence between the `maxTeamSize` update
        # and `advanceFromNode`.
        #
        # `B` is still the QUESTION-typed node: `onNodeClick` sets
        # `iu = B.type` (77317), replaces `iu` with `resolveQuestionMark()`
        # for a QUESTION node (77318-77332), and dispatches on `iu` via
        # `case "shiny"` (77384) WITHOUT rebinding `B`. There is no
        # `NODE_TYPES.SHINY` -- "shiny" exists only as a resolved type -- so
        # `recordMonOrigin` always takes its QUESTION branch here and sets
        # `state.gotViaQuestion = true`.
        #
        # This test is Python-only, so it could assert the port's belief
        # rather than the source's behavior for as long as no route executed
        # the branch. Every shiny resolution in the M4 route matrix was a
        # DECLINE, and `#btn-skip-shiny` (80984-80989) really does skip
        # `recordMonOrigin`. `route-oracle/scenarios/story_gen1_shiny_accept.json`
        # executes the accept branch cross-runtime and the JavaScript source
        # reports `counters.got_via_question = true`.
        self.assertTrue(state.got_via_question)
        # ...and the shiny path must still NOT look like a ball catch:
        # `recordMonOrigin`'s CATCH branch is a different arm of the same
        # ternary (79051-79052).
        self.assertFalse(state.used_ball_catch)

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


class M4BoundaryCaseTests(unittest.TestCase):
    """M4 repair item 5: source-to-Python boundary cases for the newly
    bridged lifecycle families (legendary/shiny/move-tutor/trade/item-equip/
    submap-reward team-picker). Cases already covered elsewhere are not
    duplicated here -- see `RewardTeamPickResolutionTests` (test_submaps.py)
    for empty/partial-party sacrifice gating, and
    `MoveTutorAndItemTests.test_move_tutor_bumps_tier_capped_at_two` for the
    move-tutor repeated-transition/mastered-tier case."""

    def test_js_round_rounds_exact_half_up_not_to_even(self):
        # map_gen._js_round's own docstring cites this as "load-bearing" for
        # `_trainer_fight_level` (58.5 -> 59) but had no direct test anywhere
        # -- Python's builtin round() would give 58 (banker's rounding to
        # even), diverging from the source's Math.round (half away from
        # zero). Also covers the plain 0.5 boundary the M4 stat10 reward
        # path (_apply_run_stat_buff) shares the same helper with.
        self.assertEqual(map_gen._js_round(58.5), 59)
        self.assertEqual(round(58.5), 58)  # documents the Python builtin's divergence
        self.assertEqual(map_gen._js_round(0.5), 1)
        self.assertEqual(map_gen._js_round(1.5), 2)
        self.assertEqual(map_gen._js_round(2.5), 3)

    def test_stat10_reward_rounds_odd_buff_amount_half_up(self):
        # The one real call site (`_resolve_reward_team_pick`'s "stat10"
        # branch) always passes amount=2 -> 2*0.5=1.0, never landing on the
        # exact .5 boundary through ordinary play. `_apply_run_stat_buff`
        # itself is exercised directly here with an odd amount to prove the
        # JS-round semantics are actually wired through this M4 code path,
        # not just decoratively imported.
        mon = _mon(1, level=20)
        engine._apply_run_stat_buff(mon, "atk", 1)  # 1*0.5=0.5 -> js_round rounds UP to 1
        self.assertEqual(mon.stat_buffs.get("atk"), 1)

    def test_move_tutor_and_trade_options_include_fainted_bench_members(self):
        # Neither doMoveTutorNode nor doTradeNode gates its member list on
        # HP -- that's a battle-only concept. A fainted bench member (0 HP,
        # not yet culled -- e.g. non-Nuzlocke, or Nuzlocke pre-cull) must
        # still be offered.
        eng, state = _start(seed=11)
        state.team.append(_mon(4, level=20))
        state.team[1].current_hp = 0
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.MOVE_TUTOR
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(state.phase, engine.Phase.MOVE_TUTOR_CHOICE)
        self.assertEqual({o["team_index"] for o in state.pending.options}, {0, 1})

        eng2, state2 = _start(seed=13)
        state2.team.append(_mon(4, level=20))
        state2.team[1].current_hp = 0
        node2 = next(n for n in state2.map.nodes.values() if n.accessible)
        node2.type = map_gen.TRADE
        state2 = eng2.step(engine.VisitNode(node_id=node2.id))
        self.assertEqual(state2.phase, engine.Phase.TRADE_CHOICE)
        self.assertEqual(len(state2.pending.options), 2)

    def test_trade_resolves_by_index_despite_duplicate_species_in_team(self):
        # Two identically-speciesed team members must not be confused with
        # each other -- resolution is strictly by list index, matching the
        # source's own array-index click handlers.
        eng, state = _start(seed=13)
        state.team.append(_mon(state.team[0].species_id, level=state.team[0].level))
        self.assertEqual(state.team[0].species_id, state.team[1].species_id)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        self.assertEqual(len(state.pending.options), 2)
        kept_id = id(state.team[0])
        state = eng.step(engine.SelectOption(index=1))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        # Slot 0 (identity-checked, not species-checked) is untouched; only
        # slot 1 -- the index actually selected -- was replaced.
        self.assertEqual(id(state.team[0]), kept_id)

    def test_repeated_trade_visits_across_maps_leave_no_stale_pending_state(self):
        # A second TRADE node visited on a later map must not inherit any
        # stale `pending.extra`/`got_via_question` bookkeeping from the
        # first -- proving the M4 trade bridge's state is per-visit, not
        # leaking across the repeated transition.
        eng, state = _start(seed=13)
        node = next(n for n in state.map.nodes.values() if n.accessible)
        node.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node.id))
        first_node_id = state.pending.extra["node_id"]
        state = eng.step(engine.SelectOption(index=None))  # decline
        self.assertIsNone(state.pending)
        self.assertFalse(state.got_via_question)

        node2 = next(n for n in state.map.nodes.values() if n.accessible and n.id != node.id)
        node2.type = map_gen.TRADE
        state = eng.step(engine.VisitNode(node_id=node2.id))
        self.assertEqual(state.phase, engine.Phase.TRADE_CHOICE)
        self.assertNotEqual(state.pending.extra["node_id"], first_node_id)
        state = eng.step(engine.SelectOption(index=0))
        self.assertEqual(state.phase, engine.Phase.ON_MAP)
        self.assertTrue(state.got_via_question)


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
        # M7 (F-I): `doElite4` does NOT reset `eliteIndex`. Its tail is
        # `unlockAchievement("elite_four")` + the gen3/gen4 achievement checks
        # + `showWinScreen()` (bundle.deobfuscated.js:77887-77893), so the
        # value its loop header last wrote (`state["eliteIndex"] = iu` at
        # 77855) survives. Only `doGen2Elite4` clears it (78394) -- see
        # `test_the_gen2_gauntlet_alone_resets_the_elite_index`. This
        # assertion used to read `0`; the cross-runtime sweep observed the
        # source reporting 4 against the port's 0 at the end of a Gen1
        # gauntlet (reproducers `M7-divergence-hunt_story_gen1_0626` /
        # `_0698`).
        self.assertEqual(state.elite_index, len(roster) - 1)
        # And neither gauntlet tail calls `advanceFromNode` (53639), so the
        # Elite Four node ends the run un-visited and still accessible.
        # `onNodeClick` (77312-77316) only locks its SAME-LAYER SIBLINGS.
        self.assertFalse(boss.visited)
        self.assertTrue(boss.accessible)

    def test_the_gen2_gauntlet_alone_resets_the_elite_index(self):
        """The control for the assertion above. `doGen2Elite4`'s tail is
        `state["eliteIndex"] = 0x0, showWinScreen()` (78390-78396), so Gen2 --
        and only Gen2 -- ends the gauntlet back at 0. Without this test,
        "never reset" would pass just as well as the source's actual rule.
        """
        eng = engine.Engine()
        state = eng.reset(seed=16, gen2_mode=True)
        state = eng.step(engine.ChooseStarter(
            species_id=state.pending.options[0]["species_id"]))
        state.current_map = 8
        boss = state.map.layers[-1][0]
        boss.accessible = True
        with patch.object(engine.battle_loop, "run_battle", return_value=_win(state.team)):
            state = eng.step(engine.VisitNode(node_id=boss.id))
        self.assertEqual(state.phase, engine.Phase.VICTORY)
        self.assertTrue(state.won)
        self.assertEqual(state.elite_index, 0)
        self.assertFalse(boss.visited)
        self.assertTrue(boss.accessible)

    def test_the_elite_four_roster_fights_at_move_tier_two(self):
        """M7 (F-H). Both gauntlet loops spread `createInstance(it, it.level,
        false, 0x2)` -- `doElite4` at bundle.deobfuscated.js:77859-77862 and
        `doGen2Elite4` at 78361-78366 -- with the move tier HARDCODED, unlike
        `doBossNode`'s gym branches, which read `it["moveTier"] ?? 1`
        (77758-77763, 77812-77817). No Elite Four table entry carries a
        `moveTier` field, so the port's shared `?? 1` fallback fought the whole
        gauntlet a tier low.
        """
        # The premise: the tables really do omit the field, so `?? 1` really
        # would apply and the two rules really do differ.
        for gen in (1, 2, 3, 4):
            for trainer in data.get_elite_four(gen):
                self.assertIsNone(trainer.move_tier,
                                  "an Elite Four entry gained a moveTier field")

        eng, state = _start(seed=16)
        state.current_map = 8
        boss = state.map.layers[-1][0]
        boss.accessible = True

        tiers = []

        def capture(player_team, enemy_team, **kwargs):
            tiers.append(sorted({m.move_tier for m in enemy_team}))
            return _win(player_team)

        with patch.object(engine.battle_loop, "run_battle", side_effect=capture):
            eng.step(engine.VisitNode(node_id=boss.id))

        self.assertEqual(len(tiers), len(data.get_elite_four(1)))
        for observed in tiers:
            self.assertEqual(observed, [2])

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
