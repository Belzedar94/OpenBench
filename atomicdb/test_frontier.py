"""Frontier expansion: what a visitor request buys once the ladder is spent.

The depth ladder (128M -> 512M -> 2B -> 10B) stops being useful the moment
10B is COMPLETED on a position; buying it again would repeat a search we
already have.  From there the request is spent one ply deeper instead, the
way proof-number search grows a frontier: an OR node (White, the attacker of
the conjecture, to move) only needs one good try; an AND node (Black to move)
has to answer every reply.
"""

from unittest import mock

from . import ingest, logic
from .models import AnalysisTask, Edge, Position, RequestLog
from .testing import TestCase


def _exhaust_ladder(pos, budget=None):
    """Record the top request rung as already COMPLETED on ``pos``."""
    budget = ingest.REQUEST_BUDGET_LADDER[-1] if budget is None else budget
    generation = pos.visits
    while AnalysisTask.objects.filter(position=pos,
                                      generation=generation).exists():
        generation += 1
    return AnalysisTask.objects.create(
        position=pos, generation=generation, budget_nodes=budget,
        state=AnalysisTask.TState.COMPLETED,
        source=AnalysisTask.Source.USER)


def _queued_moves(parent):
    """Moves of ``parent`` whose child now has work waiting or running."""
    keys = set(AnalysisTask.objects.filter(
        position__edges_in__parent=parent,
        state__in=(AnalysisTask.TState.PENDING, AnalysisTask.TState.LEASED),
    ).values_list('position_id', flat=True))
    return sorted(Edge.objects.filter(parent=parent, child_id__in=keys)
                  .values_list('move_uci', flat=True))


def _multipv(*moves):
    return [{'move': uci, 'eval_cp': 40 - 10 * i, 'mate': None, 'pv': [uci]}
            for i, uci in enumerate(moves)]


class LadderExhaustionTests(TestCase):

    def test_exhausted_ladder_no_longer_requeues_the_parent(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        spent = _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(
            list(AnalysisTask.objects.filter(position=pos)
                 .values_list('id', flat=True)), [spent.id])

    def test_ladder_exhausted_reports_only_the_spent_top_rung(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        self.assertFalse(ingest.ladder_exhausted(pos))
        _exhaust_ladder(pos, budget=ingest.REQUEST_BUDGET_LADDER[-2])
        self.assertFalse(ingest.ladder_exhausted(pos))
        _exhaust_ladder(pos)
        self.assertTrue(ingest.ladder_exhausted(pos))

    def test_solved_position_never_expands(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos)
        Position.objects.filter(pk=pos.pk).update(status='DRAW',
                                                  closure='MINIMAX')

        self.assertEqual(ingest.request_analysis(pos), 'already-solved')
        self.assertFalse(Edge.objects.filter(parent=pos).exists())


class UnchangedLadderTests(TestCase):
    """The ordinary path must not notice this feature at all."""

    def test_fresh_request_keeps_the_classic_single_task(self):
        pos = ingest.get_or_create_position(logic.start_fen())

        self.assertEqual(ingest.request_analysis(pos), 'queued')

        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(
            (task.source, task.state, task.budget_nodes),
            (AnalysisTask.Source.USER, AnalysisTask.TState.PENDING,
             ingest.REQUEST_BUDGET_LADDER[0]))
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_middle_rung_still_escalates_on_the_position_itself(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos, budget=ingest.REQUEST_BUDGET_LADDER[0])
        Position.objects.filter(pk=pos.pk).update(visits=1)
        pos.refresh_from_db()

        self.assertEqual(ingest.request_analysis(pos), 'queued')

        follow_up = AnalysisTask.objects.get(position=pos, generation=1)
        self.assertEqual(follow_up.budget_nodes,
                         ingest.REQUEST_BUDGET_LADDER[1])
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_ordinary_status_keeps_its_single_key_body(self):
        pos = ingest.get_or_create_position(logic.start_fen())

        response = self.client.post(f'/atomicdb/request/{pos.key}/')

        self.assertEqual(response.json(), {'status': 'queued'})


class OrNodeExpansionTests(TestCase):
    """White to move: one good try is enough, so only the top-k are bought."""

    def _root(self, *moves):
        pos = ingest.get_or_create_position(logic.start_fen())
        self.assertEqual(pos.fen.split()[1], 'w')
        if moves:
            pos.last_analysis = _multipv(*moves)
            pos.save(update_fields=['last_analysis'])
        return pos

    def test_multipv_order_picks_the_top_k(self):
        pos = self._root('e2e4', 'd2d4', 'g1f3', 'b1c3')
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(outcome.detail['children_queued'],
                         ingest.FRONTIER_OR_WIDTH)
        self.assertEqual(_queued_moves(pos), ['d2d4', 'e2e4', 'g1f3'])

    def test_children_enter_the_ladder_at_their_own_floor(self):
        pos = self._root('e2e4', 'd2d4', 'g1f3')
        _exhaust_ladder(pos)

        ingest.request_analysis(pos)

        tasks = AnalysisTask.objects.filter(position__edges_in__parent=pos)
        self.assertEqual(tasks.count(), 3)
        for task in tasks:
            self.assertEqual(
                (task.state, task.source, task.budget_nodes, task.generation),
                (AnalysisTask.TState.PENDING, AnalysisTask.Source.USER,
                 ingest.REQUEST_BUDGET_LADDER[0], 0))

    def test_solved_children_never_take_a_slot(self):
        pos = self._root('e2e4', 'd2d4', 'g1f3', 'b1c3')
        ingest.expand(pos)
        solved = Edge.objects.get(parent=pos, move_uci='e2e4').child
        Position.objects.filter(pk=solved.pk).update(status='DRAW',
                                                     closure='TERMINAL')
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertFalse(AnalysisTask.objects.filter(position=solved).exists())
        self.assertEqual(_queued_moves(pos), ['b1c3', 'd2d4', 'g1f3'])
        self.assertEqual(outcome.detail['children_queued'],
                         ingest.FRONTIER_OR_WIDTH)

    def test_child_evals_order_the_expansion_without_multipv(self):
        pos = self._root()
        ingest.expand(pos)
        for uci, eval_cp in (('e2e4', 40), ('d2d4', 60), ('g1f3', 10)):
            Position.objects.filter(
                pk=Edge.objects.get(parent=pos, move_uci=uci).child_id
            ).update(eval_cp=eval_cp)
        _exhaust_ladder(pos)

        ingest.request_analysis(pos)

        self.assertEqual(_queued_moves(pos), ['d2d4', 'e2e4', 'g1f3'])

    def test_no_ordering_at_all_falls_back_to_the_first_legal_moves(self):
        pos = self._root()
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'],
                         ingest.FRONTIER_BLIND_WIDTH)
        self.assertEqual(
            _queued_moves(pos),
            sorted(logic.legal_moves(pos.fen)[:ingest.FRONTIER_BLIND_WIDTH]))

    def test_multipv_entries_the_movegen_rejects_are_ignored(self):
        pos = self._root('e2e5', 'd2d4', 'g1f3', 'b1c3')
        _exhaust_ladder(pos)

        ingest.request_analysis(pos)

        self.assertEqual(_queued_moves(pos), ['b1c3', 'd2d4', 'g1f3'])


class AndNodeExpansionTests(TestCase):
    """Black to move: every reply has to be answered, so all are bought."""

    def _after_e4(self):
        pos = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        self.assertEqual(pos.fen.split()[1], 'b')
        return pos

    def test_every_legal_reply_is_queued(self):
        pos = self._after_e4()
        legal = logic.legal_moves(pos.fen)
        self.assertLess(len(legal), ingest.FRONTIER_AND_CAP)
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(outcome.detail['children_queued'], len(legal))
        self.assertEqual(_queued_moves(pos), sorted(legal))

    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 5)
    def test_cap_bounds_a_wide_and_node(self):
        pos = self._after_e4()
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'], 5)
        self.assertEqual(len(_queued_moves(pos)), 5)

    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1_000)
    @mock.patch('atomicdb.ingest.FRONTIER_CLICK_CAP', 4)
    def test_no_click_can_exceed_the_hard_task_cap(self):
        pos = self._after_e4()
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'], 4)

    def test_declared_caps_match_the_approved_design(self):
        self.assertEqual(ingest.FRONTIER_OR_WIDTH, 3)
        self.assertEqual(ingest.FRONTIER_AND_CAP, 64)
        self.assertEqual(ingest.FRONTIER_CLICK_CAP, 64)


class SingleLevelRecursionTests(TestCase):

    def test_child_with_a_spent_ladder_is_counted_not_recursed(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.last_analysis = _multipv('e2e4', 'd2d4', 'g1f3')
        pos.save(update_fields=['last_analysis'])
        ingest.expand(pos)
        deep_child = Edge.objects.get(parent=pos, move_uci='e2e4').child
        spent = _exhaust_ladder(deep_child)
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_exhausted'], 1)
        self.assertEqual(outcome.detail['children_queued'], 2)
        self.assertEqual(_queued_moves(pos), ['d2d4', 'g1f3'])
        # v1 stops one ply down: no grandchildren, no task on the spent child.
        self.assertFalse(Edge.objects.filter(parent=deep_child).exists())
        self.assertEqual(
            list(AnalysisTask.objects.filter(position=deep_child)
                 .values_list('id', flat=True)), [spent.id])

    def test_a_child_already_queued_is_reported_as_covered(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.last_analysis = _multipv('e2e4', 'd2d4', 'g1f3')
        pos.save(update_fields=['last_analysis'])
        ingest.expand(pos)
        child = Edge.objects.get(parent=pos, move_uci='e2e4').child
        AnalysisTask.objects.create(
            position=child, generation=0,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            source=AnalysisTask.Source.AUTO)
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'], 3)
        self.assertEqual(AnalysisTask.objects.get(position=child).source,
                         AnalysisTask.Source.USER)


class ExpansionRequestApiTests(TestCase):

    def _exhausted_root(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.last_analysis = _multipv('e2e4', 'd2d4', 'g1f3')
        pos.save(update_fields=['last_analysis'])
        _exhaust_ladder(pos)
        return pos

    def test_expansion_reports_its_counters(self):
        pos = self._exhausted_root()

        body = self.client.post(f'/atomicdb/request/{pos.key}/').json()

        self.assertEqual(body['status'], 'expanded')
        self.assertEqual(body['children_queued'], 3)
        self.assertEqual(body['children_considered'], 3)
        self.assertEqual(body['children_solved'], 0)
        self.assertEqual(body['children_exhausted'], 0)

    def test_one_click_is_one_expansion_event(self):
        pos = self._exhausted_root()

        first = self.client.post(f'/atomicdb/request/{pos.key}/')
        second = self.client.post(f'/atomicdb/request/{pos.key}/')

        self.assertEqual(first.json()['status'], 'expanded')
        self.assertEqual(second.json(), {'status': 'already-requested'})
        self.assertEqual(RequestLog.objects.filter(position=pos).count(), 1)
        self.assertEqual(
            AnalysisTask.objects.filter(
                position__edges_in__parent=pos,
                state=AnalysisTask.TState.PENDING).count(), 3)

    def test_expansion_consumes_the_hourly_ip_allowance(self):
        pos = self._exhausted_root()
        other = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        for _ in range(30):
            RequestLog.objects.create(ip='127.0.0.1', position=other)

        response = self.client.post(f'/atomicdb/request/{pos.key}/')

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {'status': 'rate-limited'})
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_a_different_visitor_is_not_deduplicated(self):
        pos = self._exhausted_root()
        self.client.post(f'/atomicdb/request/{pos.key}/')

        response = self.client.post(f'/atomicdb/request/{pos.key}/',
                                    HTTP_X_FORWARDED_FOR='203.0.113.7')

        self.assertEqual(response.json()['status'], 'expanded')
        self.assertEqual(RequestLog.objects.filter(position=pos).count(), 2)

    def test_an_expanded_child_is_immediately_leasable(self):
        from django.contrib.auth.models import User

        User.objects.create_user('u', password='p')
        pos = self._exhausted_root()
        self.client.post(f'/atomicdb/request/{pos.key}/')

        lease = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'm1', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'session-m1',
        })

        served = lease.json()['tasks'][0]
        self.assertEqual(served['budget_nodes'],
                         ingest.REQUEST_BUDGET_LADDER[0])
        self.assertIn(
            AnalysisTask.objects.get(id=served['id']).position_id,
            set(Edge.objects.filter(parent=pos)
                .values_list('child_id', flat=True)))

    def test_explorer_renders_the_expansion_status(self):
        pos = self._exhausted_root()

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertContains(response, 'Expanding beneath this position')
        self.assertContains(response, 'children_queued')
