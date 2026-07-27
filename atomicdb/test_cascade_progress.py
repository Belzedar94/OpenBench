"""Cascade progress: what is happening BELOW a position, for the explorer.

Past the top rung a visitor request no longer buys work on the position that
was clicked, it buys it underneath.  Watching the clicked row alone then says
nothing at all for six minutes, which is exactly how long the explorer used
to sit on "Still queued, check back in a while".  The endpoint here reports
the level below instead, in one aggregate, because a position can have sixty
children and the page asks every ten seconds.
"""

from django.conf import settings
from django.test import RequestFactory

from . import ingest, logic, views
from .models import AnalysisTask, Edge
from .testing import TestCase


def _task(pos, state, source=AnalysisTask.Source.USER, generation=0):
    return AnalysisTask.objects.create(
        position=pos, generation=generation, budget_nodes=128_000_000,
        state=state, source=source)


def _spend_ladder(pos):
    return AnalysisTask.objects.create(
        position=pos, generation=pos.visits,
        budget_nodes=ingest.REQUEST_BUDGET_LADDER[-1],
        state=AnalysisTask.TState.COMPLETED,
        source=AnalysisTask.Source.USER)


class FrontierProgressTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.children = {edge.move_uci: edge.child for edge in
                         Edge.objects.filter(parent=self.root)
                         .select_related('child')}

    def _body(self, key=None):
        response = self.client.get(
            f'/atomicdb/api/frontier/{key or self.root.key}/')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_visitor_tasks_are_counted_by_state(self):
        _task(self.children['e2e4'], AnalysisTask.TState.LEASED)
        _task(self.children['d2d4'], AnalysisTask.TState.PENDING)
        _task(self.children['g1f3'], AnalysisTask.TState.PENDING)
        _task(self.children['b1c3'], AnalysisTask.TState.COMPLETED)

        body = self._body()

        self.assertEqual((body['running'], body['queued'], body['done']),
                         (1, 2, 1))

    def test_autonomous_work_is_not_reported_as_the_visitor_s(self):
        _task(self.children['e2e4'], AnalysisTask.TState.PENDING,
              source=AnalysisTask.Source.AUTO)

        self.assertEqual(self._body()['queued'], 0)

    def test_closed_children_are_counted(self):
        for uci in ('e2e4', 'd2d4'):
            child = self.children[uci]
            child.status, child.closure = 'DRAW', 'TERMINAL'
            child.save(update_fields=['status', 'closure'])

        body = self._body()

        self.assertEqual(body['children_solved'], 2)
        self.assertEqual(body['children_total'], len(self.children))

    def test_several_tasks_on_one_child_do_not_inflate_the_level(self):
        child = self.children['e2e4']
        _task(child, AnalysisTask.TState.COMPLETED, generation=0)
        _task(child, AnalysisTask.TState.PENDING, generation=1)

        body = self._body()

        # The task join multiplies rows; the child counters must not follow.
        self.assertEqual(body['children_total'], len(self.children))
        self.assertEqual((body['queued'], body['done']), (1, 1))

    def test_work_further_down_is_not_claimed_as_this_level_s(self):
        child = self.children['e2e4']
        ingest.expand(child)
        grandchild = Edge.objects.filter(parent=child).first().child
        _task(grandchild, AnalysisTask.TState.PENDING)

        self.assertEqual(self._body()['queued'], 0)
        self.assertEqual(self._body(child.key)['queued'], 1)

    def test_a_bare_level_answers_zeros_rather_than_nothing(self):
        lonely = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))

        body = self._body(lonely.key)

        self.assertEqual(
            (body['running'], body['queued'], body['done'],
             body['children_total'], body['children_solved']),
            (0, 0, 0, 0, 0))

    def test_an_unknown_position_is_not_invented(self):
        response = self.client.get(f'/atomicdb/api/frontier/{"f" * 64}/')

        self.assertEqual(response.status_code, 404)

    def test_the_cost_does_not_grow_with_the_width_of_the_level(self):
        for child in self.children.values():
            _task(child, AnalysisTask.TState.PENDING)
        lonely = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        request = RequestFactory().get('/atomicdb/api/frontier/')
        alias = settings.ATOMICDB_DATABASE_ALIAS

        # One row for the position, one aggregate for the whole level: the
        # poller must not pay a statement per child.
        self.assertEqual(len(self.children), 20)
        with self.assertNumQueries(2, using=alias):
            views.api_frontier(request, lonely.key)
        with self.assertNumQueries(2, using=alias):
            views.api_frontier(request, self.root.key)


class ExplorerCascadeTests(TestCase):
    """The explorer only watches below when there is a below to watch."""

    def test_a_spent_ladder_arms_the_cascade_line_on_load(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _spend_ladder(pos)

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertContains(response, 'let below = key;')
        self.assertContains(response, 'Analyzing beneath this position')
        self.assertContains(response, '/atomicdb/api/frontier/')
        self.assertContains(response, 'children solved')

    def test_an_ordinary_position_does_not_poll_below_itself(self):
        pos = ingest.get_or_create_position(logic.start_fen())

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertContains(response, 'let below = null;')

    def test_a_solved_position_offers_no_request_machinery_at_all(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _spend_ladder(pos)
        pos.status, pos.closure = 'DRAW', 'MINIMAX'
        pos.save(update_fields=['status', 'closure'])

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertNotContains(response, '/atomicdb/api/frontier/')
