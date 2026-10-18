"""Server-picked work is searched at 128M, never with the tiny probes.

Community poll, closed 5-0 in October 2026: when nobody is requesting
analysis, the workers search the open positions the server picks at 128M
nodes instead of the 8M searches behind the write floods of 8 and 11 October
(the night before, ~40 results a minute, almost all AUTO 8M and FILL 8M, with
the five ingest writers saturated).

What these tests pin, and why each one:

* the rule itself (``ingest.server_picked_budget``): the floor is the first
  request rung, a rung above it is untouched, and a clamp that IS the purchase
  keeps its budget, with the same test ``multipv_for`` uses to give it a single
  line;
* every place the server mints a task on its own goes through it: the two
  selectors (and so the pool top-up and the inline refill of an empty lease),
  every arm of the value walker, the cascade, the quality arm, coverage
  completion, dn repair, the witness-refuted revisit and the uncertainty
  expansion;
* the two documented exceptions stay cheap: a short-mate verification and the
  look at a sibling the proof no longer needs;
* a visitor's own request is not this rule's business;
* and setting the floor to 0 gives back exactly the old prices, which is the
  rollback.
"""

import hashlib
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import ingest, logic, proof
from .models import AnalysisTask, Edge, Position, ProofCampaign
from .testing import TestCase, worker_account

FLOOR = ingest.REQUEST_BUDGET_LADDER[0]
PN = {'ATOMICDB_SELECTOR': 'pn'}
VALUE = {'ATOMICDB_SELECTOR': 'pn', 'ATOMICDB_DESCENT': 'value'}


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **fields):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **fields)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _mate_eval(moves):
    """``eval_cp`` as the worker writes it for ``score mate N``."""
    return 10_000 - moves


class RuleTests(SimpleTestCase):
    """The rule, on its own."""

    def test_the_floor_is_the_first_request_rung(self):
        self.assertEqual(ingest.SERVER_PICKED_MIN_NODES, FLOOR)
        self.assertEqual(FLOOR, 128_000_000)

    def test_the_tiny_probes_are_lifted_to_the_floor(self):
        for budget in (ingest.BUDGET_LADDER[0], ingest.BUDGET_LADDER[1],
                       ingest.COVERAGE_SEED_NODES, 1):
            self.assertEqual(ingest.server_picked_budget(budget), FLOOR,
                             msg=budget)

    def test_a_rung_above_the_floor_is_untouched(self):
        for budget in ingest.REQUEST_BUDGET_LADDER:
            self.assertEqual(ingest.server_picked_budget(budget), budget)

    def test_a_clamp_that_is_the_purchase_keeps_its_budget(self):
        mate = (12_000_000, ingest.MATE_CLAMP_MULTIPV)
        self.assertEqual(ingest.server_picked_budget(12_000_000, mate),
                         12_000_000)
        sibling = (proof.OR_CLAMP_NODES, proof.OR_CLAMP_MULTIPV)
        self.assertEqual(
            ingest.server_picked_budget(proof.OR_CLAMP_NODES, sibling),
            proof.OR_CLAMP_NODES)

    def test_a_ladder_asking_more_than_the_clamp_is_an_ordinary_look(self):
        """The test of ``multipv_for``: above the clamp it is not the clamp."""
        mate = (12_000_000, ingest.MATE_CLAMP_MULTIPV)
        self.assertEqual(ingest.server_picked_budget(32_000_000, mate), FLOOR)
        self.assertEqual(ingest.multipv_for(1, 32_000_000, clamp=mate), 5)

    def test_zero_is_the_rollback(self):
        with patch.object(ingest, 'SERVER_PICKED_MIN_NODES', 0):
            self.assertEqual(
                ingest.server_picked_budget(ingest.BUDGET_LADDER[0]),
                ingest.BUDGET_LADDER[0])


class RegretSelectorTests(TestCase):
    """``next_tasks`` on the historic selector, and the pool it feeds."""

    def setUp(self):
        ingest._priority_refresh_cache['at'] = 0.0

    def _single(self, **fields):
        pos = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(pk=pos.pk).update(**fields)
        ingest.refresh_priorities()
        tasks = ingest.next_tasks(1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].position_id, pos.key)
        return tasks[0]

    def test_a_first_look_is_minted_at_the_floor(self):
        task = self._single(eval_cp=40)

        self.assertEqual(task.source, AnalysisTask.Source.AUTO)
        self.assertEqual(task.budget_nodes, FLOOR)
        self.assertEqual(task.multipv, 5)

    def test_the_visit_ladder_above_the_floor_still_climbs(self):
        task = self._single(eval_cp=40, visits=3)

        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[3])

    def test_a_short_mate_check_stays_a_cheap_single_line(self):
        task = self._single(eval_cp=_mate_eval(2))

        self.assertEqual(task.budget_nodes, 3 * ingest.MATE_CLAMP_PER_PLY)
        self.assertEqual(task.multipv, ingest.MATE_CLAMP_MULTIPV)

    def test_once_the_ladder_outbids_the_clamp_it_is_floored(self):
        task = self._single(eval_cp=_mate_eval(2), visits=1)

        self.assertEqual(task.budget_nodes, FLOOR)
        self.assertEqual(task.multipv, 5)

    def test_the_pool_top_up_mints_at_the_floor_and_counts_tasks(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        for edge in Edge.objects.filter(parent=root)[:6]:
            Position.objects.filter(pk=edge.child_id).update(
                eval_cp=100, expanded=True)
        ingest.refresh_priorities()

        self.assertEqual(ingest.top_up_analysis_pool(target=5), 5)
        self.assertEqual(ingest.top_up_analysis_pool(target=5), 0)

        budgets = set(AnalysisTask.objects.filter(
            state='PENDING', source=AnalysisTask.Source.AUTO)
            .values_list('budget_nodes', flat=True))
        self.assertEqual(budgets, {FLOOR})

    def test_zero_gives_back_the_old_price(self):
        with patch.object(ingest, 'SERVER_PICKED_MIN_NODES', 0):
            task = self._single(eval_cp=40)

        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[0])


@override_settings(**PN)
class ProofSelectorTests(TestCase):
    """``_next_tasks_by_proof``, the selector production runs."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        edges = list(Edge.objects.filter(parent=self.root).order_by('id'))
        self.first, self.second = edges[0].child, edges[1].child

    def _mint(self, pos):
        with patch.object(proof, 'descend', return_value=(pos, 1)):
            tasks = ingest.next_tasks(1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].position_id, pos.key)
        return tasks[0]

    def test_the_df_pn_descent_mints_at_the_floor(self):
        task = self._mint(self.second)

        self.assertEqual(task.source, AnalysisTask.Source.AUTO)
        self.assertEqual(task.budget_nodes, FLOOR)
        self.assertEqual(task.multipv, 5)

    def test_a_pool_of_several_is_all_at_the_floor(self):
        """Real descents this time, no patch: the pool as it is minted."""
        proof.refresh_proof_numbers([self.root.key])

        self.assertEqual(ingest.top_up_analysis_pool(target=3), 3)

        tasks = AnalysisTask.objects.filter(state='PENDING')
        self.assertEqual(tasks.count(), 3)
        self.assertEqual({task.budget_nodes for task in tasks}, {FLOOR})

    def test_the_proved_or_clamp_still_buys_its_cheap_look(self):
        Position.objects.filter(key=self.first.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        task = self._mint(self.second)

        self.assertEqual(task.budget_nodes, proof.OR_CLAMP_NODES)
        self.assertEqual(task.multipv, proof.OR_CLAMP_MULTIPV)

    def test_a_short_mate_check_stays_a_cheap_single_line(self):
        Position.objects.filter(key=self.second.key).update(mate_in=3)
        self.second.refresh_from_db()

        task = self._mint(self.second)

        self.assertEqual(task.budget_nodes, 3 * ingest.MATE_CLAMP_PER_PLY)
        self.assertEqual(task.multipv, ingest.MATE_CLAMP_MULTIPV)

    def test_once_the_ladder_outbids_the_clamp_it_is_floored(self):
        Position.objects.filter(key=self.second.key).update(mate_in=3,
                                                            visits=1)
        self.second.refresh_from_db()

        task = self._mint(self.second)

        self.assertEqual(task.budget_nodes, FLOOR)
        self.assertEqual(task.multipv, 5)

    def test_zero_gives_back_the_old_price(self):
        with patch.object(ingest, 'SERVER_PICKED_MIN_NODES', 0):
            task = self._mint(self.second)

        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[0])


@override_settings(**PN)
class EmptyQueueLeaseTests(TestCase):
    """The inline refill of ``api_lease`` when the queue is empty."""

    def test_the_task_a_worker_gets_from_an_empty_queue_is_128m(self):
        worker_account('w', 'pw')
        self.assertFalse(AnalysisTask.objects.exists())

        response = self.client.post('/atomicdb/api/lease', {
            'username': 'w', 'password': 'pw', 'machine': 'idle-box',
            'worker_build': '2026072203', 'lease_session': 'idle-session'})

        served = response.json()['tasks']
        self.assertEqual(len(served), 1)
        self.assertEqual(served[0]['budget_nodes'], FLOOR)
        task = AnalysisTask.objects.get(id=served[0]['id'])
        self.assertEqual(task.source, AnalysisTask.Source.AUTO)
        self.assertEqual(task.state, AnalysisTask.TState.LEASED)


@override_settings(**VALUE)
class ValueWalkerTests(TestCase):
    """Every arm of the value walker, cheap ones included."""

    def _mint(self, arm, floor_nodes=None):
        pos = _pos(f'VW-{arm}', 'b', eval_cp=40)
        target = proof.ValueTarget(pos, 1, arm, floor_nodes)
        with patch.object(proof, 'descend_value', return_value=target):
            tasks = ingest._next_tasks_by_proof(1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].arm, ingest.VALUE_DESCEND_ARM)
        return tasks[0]

    def test_the_explore_and_backup_arms_are_floored(self):
        self.assertEqual(self._mint(proof.VALUE_ARM_EXPLORE).budget_nodes,
                         FLOOR)
        self.assertEqual(self._mint(proof.VALUE_ARM_BACKUP).budget_nodes,
                         FLOOR)

    def test_the_cycle_arm_is_floored(self):
        task = self._mint(proof.VALUE_ARM_CYCLE,
                          floor_nodes=ingest.BUDGET_LADDER[0])
        self.assertEqual(task.budget_nodes, FLOOR)

    def test_the_spine_was_already_there(self):
        self.assertEqual(self._mint(proof.VALUE_ARM_SPINE).budget_nodes,
                         FLOOR)

    def test_the_next_rung_above_the_floor_is_untouched(self):
        task = self._mint(proof.VALUE_ARM_RUNG, floor_nodes=FLOOR)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[1])

    def test_the_cascade_is_floored(self):
        node = _pos('VW-node', 'w', eval_cp=300, expanded=True)
        for index in range(3):
            _edge(node, _pos(f'VW-reply-{index}', 'b'), f'c{index}')

        self.assertEqual(ingest._queue_value_cascade(node), 3)

        tasks = AnalysisTask.objects.filter(arm=ingest.VALUE_CASCADE_ARM)
        self.assertEqual(tasks.count(), 3)
        self.assertEqual({task.budget_nodes for task in tasks}, {FLOOR})
        self.assertEqual({task.source for task in tasks},
                         {AnalysisTask.Source.AUTO})


class QualityArmTests(TestCase):
    """The FILL arm that buys a child the support of its parent."""

    def setUp(self):
        self.parent = _pos('QA-parent', 'w', eval_cp=100)
        self.child = _pos('QA-child', 'b')
        _edge(self.parent, self.child, 'q1q2')

    def test_the_cycle_disambiguation_is_floored(self):
        made = ingest._queue_cycle_disambiguation(
            [('q1q2', self.parent.key, ingest.BUDGET_LADDER[0])])

        self.assertEqual(made, 1)
        task = AnalysisTask.objects.get(position=self.child)
        self.assertEqual((task.source, task.arm),
                         (AnalysisTask.Source.FILL, ingest.QUALITY_ARM))
        self.assertEqual(task.budget_nodes, FLOOR)

    def test_the_quality_convergence_is_floored(self):
        made = ingest._queue_quality_convergence(
            [('q1q2', self.parent.key, ingest.BUDGET_LADDER[0])])

        self.assertEqual(made, 1)
        task = AnalysisTask.objects.get(position=self.child)
        self.assertEqual(task.budget_nodes, FLOOR)

    def test_the_floor_never_adds_a_purchase_the_arm_skipped(self):
        """A child already searched at the parent's support needs nothing."""
        Position.objects.filter(key=self.child.key).update(
            nodes_invested=ingest.BUDGET_LADDER[0])

        made = ingest._queue_quality_convergence(
            [('q1q2', self.parent.key, ingest.BUDGET_LADDER[0])])

        self.assertEqual(made, 0)
        self.assertFalse(
            AnalysisTask.objects.filter(position=self.child).exists())


class CoverageAndDnTests(TestCase):
    """The two FILL arms of the selector service."""

    def test_coverage_completion_is_floored(self):
        fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1'
        parent = ingest.get_or_create_position(fen)
        ingest.expand(parent)
        edges = list(Edge.objects.filter(parent=parent).order_by('id'))
        for edge in edges[1:]:
            Position.objects.filter(key=edge.child_id).update(
                status='WHITE_WIN', closure='MATE_PV', proof='ANDOR')

        self.assertEqual(ingest.enqueue_coverage_completion(), 1)

        task = AnalysisTask.objects.get(position=edges[0].child)
        self.assertEqual((task.source, task.arm),
                         (AnalysisTask.Source.FILL, ingest.COVERAGE_ARM))
        self.assertEqual(task.budget_nodes, FLOOR)
        self.assertEqual(task.multipv, ingest.DEPTH_MULTIPV)

    def test_dn_repair_is_floored(self):
        fen = logic.apply_move(logic.start_fen(),
                               logic.legal_moves(logic.start_fen())[0])
        node = ingest.get_or_create_position(fen)
        ingest.expand(node)
        proof.refresh_proof_numbers([node.key])
        self.assertTrue(ProofCampaign.objects.filter(active=True).exists())

        made = ingest.enqueue_dn_repair()

        self.assertEqual(made, ingest.DN_REPAIR_REPLIES)
        tasks = AnalysisTask.objects.filter(arm=ingest.DN_ARM)
        self.assertEqual({task.source for task in tasks},
                         {AnalysisTask.Source.FILL})
        self.assertEqual({task.budget_nodes for task in tasks}, {FLOOR})


@override_settings(ATOMICDB_BREADTH_SWAP=True)
class BreadthSwapArmTests(TestCase):
    """The two AUTO arms that a landing result can trigger."""

    def test_the_witness_refuted_revisit_is_floored(self):
        parent = _pos('WR-parent', 'w', eval_cp=100, last_analysis=[
            {'move': 'w1w2', 'eval_cp': 100, 'pv': ['w1w2']}])
        child = _pos('WR-child', 'b', eval_cp=-400)
        _edge(parent, child, 'w1w2')

        self.assertEqual(
            ingest._witness_refuted_revisit(child, [parent.key]), 1)

        task = AnalysisTask.objects.get(position=parent)
        self.assertEqual(task.source, AnalysisTask.Source.AUTO)
        self.assertEqual(task.budget_nodes, FLOOR)
        self.assertEqual(task.multipv, ingest.DEPTH_MULTIPV)

    def _uncertain_parent(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        Position.objects.filter(pk=parent.pk).update(eval_cp=100,
                                                     backed_eval=400)
        first = Edge.objects.filter(parent=parent).order_by('id').first()
        return parent, first.child

    def test_the_uncertainty_expansion_is_floored_past_a_clamp(self):
        parent, child = self._uncertain_parent()
        Position.objects.filter(key=child.key).update(mate_in=3, visits=1)

        self.assertEqual(ingest._uncertainty_expand([parent.key]), 2)

        tasks = AnalysisTask.objects.all()
        self.assertEqual({task.source for task in tasks},
                         {AnalysisTask.Source.AUTO})
        self.assertEqual({task.budget_nodes for task in tasks}, {FLOOR})

    def test_the_uncertainty_expansion_still_verifies_a_short_mate(self):
        parent, child = self._uncertain_parent()
        Position.objects.filter(key=child.key).update(mate_in=3)

        ingest._uncertainty_expand([parent.key])

        task = AnalysisTask.objects.get(position=child)
        self.assertEqual(task.budget_nodes, 3 * ingest.MATE_CLAMP_PER_PLY)
        self.assertEqual(task.multipv, ingest.MATE_CLAMP_MULTIPV)


class VisitorRequestTests(TestCase):
    """A request somebody made is priced by the request ladder, not by this."""

    def test_the_same_child_keeps_its_visitor_price(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        child = Edge.objects.filter(parent=parent).order_by('id').first().child
        Position.objects.filter(key=child.key).update(mate_in=3, visits=1)

        ingest.enqueue_unexplored_children(parent, cap=1)

        task = AnalysisTask.objects.get(position=child)
        self.assertEqual(task.source, AnalysisTask.Source.USER)
        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[1])
