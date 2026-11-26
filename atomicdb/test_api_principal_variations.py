"""One call for a position and its best lines (seaside_cliff, for Atomic
Puzzles, 4-Nov-2026): the root as ``/api/query`` returns it, and each of the
top moves followed along the board arrow for a few plies."""

from django.test import Client

from . import ingest, logic
from .models import Edge, Position
from .testing import TestCase

URL = '/atomicdb/api/principal-variations'


def _node(fen, value):
    """A searched position at ``value`` (White's point of view, as stored)."""
    pos = ingest.get_or_create_position(logic.canonical_fen(fen))
    Position.objects.filter(key=pos.key).update(
        eval_cp=value, nodes_invested=128_000_000, visits=1)
    pos.refresh_from_db()
    return pos


def _child(parent, uci, value):
    child = _node(logic.apply_move(parent.fen, uci), value)
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


class LineWalkBudgetTests(TestCase):
    """Up to 26 move tables per call share ONE repetition-walk budget.

    Each table may walk the repetition for up to the backup's own 1 s
    (see views._repetition_moves); a call that reaches many positions must
    not pay that per table.  The clock is patched: every walk costs a full
    second, and once views.LINE_WALK_REQUEST_SECONDS are spent the tables
    that remain show stored values without walking.
    """

    def setUp(self):
        self.root = _node(logic.start_fen(), 40)
        for index, uci in enumerate(('e2e4', 'd2d4', 'g1f3', 'c2c4', 'e2e3')):
            first = _child(self.root, uci, 50 - index)
            reply = _child(first, 'a7a6', 40 - index)
            _child(reply, 'a2a3', 30 - index)

    def test_many_tables_stay_within_one_budget(self):
        from unittest import mock
        import types
        from . import views
        clock = [0.0]
        calls = []

        def walk(pos, line=frozenset(), deadline=None, detail=None):
            calls.append((clock[0], deadline))
            clock[0] += 1.0       # every walk spends a whole second
            return {}

        tables = []
        real = views._repetition_moves

        def counted(pos, route_keys=None, detail=None):
            tables.append(pos.key)
            return real(pos, route_keys, detail)

        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0])
        with mock.patch.object(views, 'time', fake_time), \
                mock.patch.object(ingest, 'line_moves', side_effect=walk), \
                mock.patch.object(views, '_repetition_moves', counted):
            response = Client().get(URL, {'fen': self.root.fen})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['lines']), 5)
        self.assertEqual(body['lines']['e2e4'], ['e2e4', 'a7a6', 'a2a3'])
        # Root plus fifteen positions on the lines, and only the walks that
        # fit in the budget ran.
        self.assertGreaterEqual(len(tables), 16)
        budget = views.LINE_WALK_REQUEST_SECONDS
        self.assertEqual(len(calls), int(-(-budget // 1.0)))
        # Every walk got the request's deadline, which caps its own 1 s.
        self.assertTrue(all(deadline == budget for _t, deadline in calls))
        self.assertLessEqual(max(t for t, _d in calls), budget)

    def test_without_a_request_budget_a_table_walks_as_before(self):
        from . import views
        self.assertIsNone(views._line_walk_deadline.get())
        from unittest import mock
        with mock.patch.object(ingest, 'line_moves',
                               return_value={}) as walk:
            views._child_moves(self.root)
        walk.assert_called_once()
        self.assertIsNone(walk.call_args.kwargs['deadline'])


class PrincipalVariationsTests(TestCase):

    def setUp(self):
        # White to move: e4 (+60) beats d4 (+20).  After e4, Black's best
        # is e5 (+30 for White, the least bad), and after e5 White's is Nf3.
        self.root = _node(logic.start_fen(), 40)
        e4 = _child(self.root, 'e2e4', 60)
        _child(self.root, 'd2d4', 20)
        e5 = _child(e4, 'e7e5', 30)
        _child(e4, 'd7d5', 80)
        _child(e5, 'g1f3', 35)
        _child(e5, 'a2a3', 0)
        self.client = Client()

    def _get(self, **params):
        params.setdefault('fen', self.root.fen)
        return self.client.get(URL, params)

    def test_the_root_is_what_a_query_returns(self):
        body = self._get().json()
        query = self.client.get('/atomicdb/api/query',
                                {'fen': self.root.fen}).json()
        self.assertEqual(body['position'], query)

    def test_each_top_move_follows_the_arrow(self):
        body = self._get().json()
        self.assertEqual(body['lines']['e2e4'], ['e2e4', 'e7e5', 'g1f3'])
        # d4 has no searched reply, so its line stops after one ply.
        self.assertEqual(body['lines']['d2d4'], ['d2d4'])
        self.assertEqual(list(body['lines']), ['e2e4', 'd2d4'])

    def test_positions_cover_every_position_on_the_lines(self):
        body = self._get().json()
        after_e4 = logic.apply_move(self.root.fen, 'e2e4')
        record = body['positions'][Position.objects.get(
            key=logic.key_of(after_e4)).fen]
        self.assertEqual(record['best_move'], 'e7e5')
        # Black to move: the score is from Black's side.
        self.assertEqual(record['score'], -60)
        self.assertEqual(len(body['positions']), 4)

    def test_counts_are_capped_and_honoured(self):
        body = self._get(line_count=1, ply_count=2).json()
        self.assertEqual(body['lines'], {'e2e4': ['e2e4', 'e7e5']})
        body = self._get(line_count=99, ply_count=99).json()
        self.assertEqual((body['line_count'], body['ply_count']), (5, 5))
        body = self._get(line_count='x', ply_count='0').json()
        self.assertEqual((body['line_count'], body['ply_count']), (5, 1))

    def test_bad_and_unknown_positions_answer_like_query(self):
        self.assertEqual(self._get(fen='rubbish').status_code, 400)
        response = self._get(fen='4k3/8/8/8/8/8/8/4K2R w K - 0 1')
        self.assertEqual(response.status_code, 404)


class NodeBackedPliesTests(TestCase):
    """seaside_cliff, 5-Nov-2026: each move carries its own position's
    backing depth beside the chip's, unknown stays null, and both endpoints
    agree move by move."""

    def setUp(self):
        self.root = _node(logic.start_fen(), 40)
        self.deep = _child(self.root, 'e2e4', 60)
        self.same = _child(self.root, 'd2d4', 20)
        self.none = _child(self.root, 'g1f3', 10)
        # e4: backed by its subtree, 7 plies down, to a value of its own.
        Position.objects.filter(key=self.deep.key).update(
            backed_eval=75, backed_plies=7)
        # d4: backed to exactly its own search, 3 plies: no chip.
        Position.objects.filter(key=self.same.key).update(
            backed_eval=20, backed_plies=3)
        # Nf3: no backing at all.

    def _moves(self, url):
        body = Client().get(url, {'fen': self.root.fen}).json()
        moves = body['position']['moves'] if 'position' in body else body['moves']
        return {m['uci']: m for m in moves}, [m['uci'] for m in moves]

    def test_each_move_keeps_its_own_value(self):
        moves, _order = self._moves(URL)
        self.assertEqual(moves['e2e4']['node_backed_plies'], 7)
        self.assertEqual(moves['e2e4']['backed_plies'], 7)
        # The chip says nothing where the backing is the move's own search;
        # the node's depth is still there.
        self.assertEqual(moves['d2d4']['backed_plies'], 0)
        self.assertEqual(moves['d2d4']['node_backed_plies'], 3)
        # Unknown stays unknown, not a zero.
        self.assertIsNone(moves['g1f3']['node_backed_plies'])

    def test_both_endpoints_agree_move_by_move_and_keep_the_order(self):
        pv, pv_order = self._moves(URL)
        q, q_order = self._moves('/atomicdb/api/query')
        self.assertEqual(pv_order, q_order)
        for uci in pv:
            self.assertEqual(pv[uci]['node_backed_plies'], q[uci]['node_backed_plies'])
            self.assertEqual(pv[uci]['backed_plies'], q[uci]['backed_plies'])
