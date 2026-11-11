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
        # ``opening`` is the query API's own last key, not part of the root.
        query.pop('opening')
        query.pop('opening_named_by')
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
