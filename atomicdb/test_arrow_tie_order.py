"""On a tie of value, the table's top row is the arrow's move.

Live on 26-Oct, Black to move in
``rnb1k1nr/2p2p1p/4p1p1/3p4/p3B3/N1P2P2/1P1P1bPP/R1BQ1K1R``: four moves at
0.00, and the API answered ``best_move=c8a6`` with ``moves[0]=h7h5``.  The
arrow names the move the header's number comes from; the table sorted by
value alone, so on a tie its order was the order the database returned the
edges in.  The tie now goes to the arrow's move and then to the move itself,
on the page and in the API alike.
"""

from django.test import Client

from . import ingest, logic, views
from .models import Edge, Position
from .testing import TestCase

REPORTED_FEN = 'rnb1k1nr/2p2p1p/4p1p1/3p4/p3B3/N1P2P2/1P1P1bPP/R1BQ1K1R b kq - 0 1'
TIED = ['h7h5', 'a8a7', 'c8a6', 'e8e7']


def _tied_position(insert_order, value=0, **fields):
    """The reported position with four replies, all searched, all at
    ``value`` (White's point of view, as stored), inserted in that order."""
    parent = ingest.get_or_create_position(logic.canonical_fen(REPORTED_FEN))
    Position.objects.filter(key=parent.key).update(
        **{'eval_cp': value, 'backed_eval': value, 'best_move': 'c8a6',
           'nodes_invested': 512_000_000, 'visits': 1, **fields})
    parent.refresh_from_db()
    for uci in insert_order:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Position.objects.filter(key=child.key).update(
            eval_cp=value, nodes_invested=128_000_000, visits=1)
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
    return parent


def _order(parent):
    return [row['uci'] for row in views._child_moves(parent)]


class TieOrderTests(TestCase):

    def test_the_arrows_move_leads_a_tie(self):
        parent = _tied_position(TIED)

        rows = views._child_moves(parent)

        self.assertEqual(rows[0]['uci'], 'c8a6')
        self.assertEqual(views._arrow_move(rows, parent), 'c8a6')
        # The rest of the tie in a fixed order: the move itself.
        self.assertEqual([row['uci'] for row in rows],
                         ['c8a6', 'a8a7', 'e8e7', 'h7h5'])

    def test_the_order_does_not_depend_on_how_the_edges_were_stored(self):
        first = _order(_tied_position(TIED))
        Edge.objects.all().delete()

        second = _order(_tied_position(list(reversed(TIED))))

        self.assertEqual(first, second)

    def test_a_backed_move_leads_the_tie_it_is_part_of(self):
        # The header quotes the backup through h7h5, not the own search.
        parent = _tied_position(TIED, value=-30, eval_cp=-10,
                                backed_move='h7h5')

        rows = views._child_moves(parent)

        self.assertEqual(views._header_move(parent), 'h7h5')
        self.assertEqual(rows[0]['uci'], 'h7h5')
        self.assertEqual(views._arrow_move(rows, parent), 'h7h5')

    def test_value_still_comes_first(self):
        # A better move outside the tie stays on top: only ties are broken.
        parent = _tied_position(TIED)
        better = Edge.objects.get(parent=parent, move_uci='e8e7').child
        Position.objects.filter(key=better.key).update(eval_cp=-50)

        self.assertEqual(_order(parent)[:2], ['e8e7', 'c8a6'])

    def test_a_proven_position_puts_its_best_move_first_among_equal_wins(self):
        # Its header quotes the verdict, so no backed move: the tie goes to
        # the witness, the last edge stored here.
        parent = _tied_position(TIED, status='BLACK_WIN', closure='MINIMAX',
                                mate_in=3, best_move='e8e7')
        for uci in TIED:
            child = Edge.objects.get(parent=parent, move_uci=uci).child
            Position.objects.filter(key=child.key).update(
                status='BLACK_WIN', closure='MINIMAX', mate_in=2)

        rows = views._child_moves(parent)

        self.assertEqual(rows[0]['uci'], 'e8e7')
        self.assertEqual(views._arrow_move(rows, parent), 'e8e7')


class FloorTieTests(TestCase):
    """A floor the backup refuses goes after the rows it can stand on."""

    def test_a_refused_floor_does_not_lead_a_tie(self):
        parent = ingest.get_or_create_position(
            logic.apply_move(logic.apply_move(logic.start_fen(), 'e2e4'),
                             'e7e5'))
        # Two current lines: nothing outside them beats 30 for White.
        Position.objects.filter(key=parent.key).update(
            eval_cp=50, best_move='g1f3', nodes_invested=512_000_000,
            last_analysis=[
                {'move': 'g1f3', 'eval_cp': 50, 'mate': None,
                 'pv': ['g1f3']},
                {'move': 'f1c4', 'eval_cp': 30, 'mate': None,
                 'pv': ['f1c4']}])
        parent.refresh_from_db()
        floor = ingest.get_or_create_position(
            logic.apply_move(parent.fen, 'a2a3'))
        Position.objects.filter(key=floor.key).update(backed_eval=40)
        searched = ingest.get_or_create_position(
            logic.apply_move(parent.fen, 'b1c3'))
        Position.objects.filter(key=searched.key).update(
            eval_cp=40, nodes_invested=128_000_000)
        Edge.objects.create(parent=parent, move_uci='a2a3', child=floor)
        Edge.objects.create(parent=parent, move_uci='b1c3', child=searched)

        rows = views._child_moves(parent)

        self.assertEqual([(row['uci'], row['floor']) for row in rows],
                         [('b1c3', False), ('a2a3', True)])
        self.assertEqual(views._arrow_move(rows, parent), 'b1c3')


class PageAndApiTests(TestCase):
    """The page and the API read the same list in the same order."""

    def test_the_api_top_row_is_its_best_move(self):
        parent = _tied_position(TIED)

        payload = Client().get('/atomicdb/api/query',
                               {'fen': parent.fen}).json()

        self.assertEqual(payload['best_move'], 'c8a6')
        self.assertEqual(payload['moves'][0]['uci'], payload['best_move'])

    def test_the_page_and_the_api_agree(self):
        parent = _tied_position(TIED)

        page = Client().get(f'/atomicdb/explore/{parent.key}/')
        payload = Client().get('/atomicdb/api/query',
                               {'fen': parent.fen}).json()

        self.assertEqual(page.status_code, 200)
        self.assertEqual([row['uci'] for row in page.context['moves']],
                         [row['uci'] for row in payload['moves']])
        self.assertEqual(page.context['best_move'], payload['best_move'])
        self.assertEqual(page.context['moves'][0]['uci'],
                         page.context['best_move'])
