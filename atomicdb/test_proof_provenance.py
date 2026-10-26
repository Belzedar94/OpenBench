"""A proven page says where its proof came from.

A MATE_PV page printed its winning line and nothing about the search that
found it, and a closure by minimax looked the same whatever closed it, so a
position closed the moment a visitor stepped into an already proven child
read as a closure out of nowhere.  One short line under the verdict now says
it: the search whose stored line this is (checked, not assumed), the winning
line of the position above for a deeper link of the same chain, or the
replies minimax closed it from.
"""

from django.conf import settings

from . import ingest, logic, views
from .models import Edge, Position
from .testing import TestCase

# A legal line from the start position.  The ingest takes the proof it is
# handed, so the line only has to be legal for the chain to materialise.
LINE = ['e2e4', 'e7e5', 'g1f3', 'b8c6', 'f1c4', 'd7d6']


def _after(*moves):
    fen = logic.start_fen()
    for uci in moves:
        fen = logic.apply_move(fen, uci)
    return fen


def _key(*moves):
    return logic.key_of(_after(*moves))


class _Closed:
    """The start position searched, its PV a mate line through 1.e4."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.line = {'move': LINE[0], 'eval_cp': 9990, 'mate': 3,
                     'pv': list(LINE)}
        ingest.ingest_analysis(
            self.root.key, [self.line], 1_000_000,
            mate_proofs={0: (True, LINE[1:], 'INCONCLUSIVE', None)})
        self.root.refresh_from_db()
        self.top = Position.objects.get(key=_key(LINE[0]))
        self.link = Position.objects.get(key=_key(*LINE[:2]))

    def _page(self, pos, **params):
        response = self.client.get(f'/atomicdb/explore/{pos.key}/', params)
        self.assertEqual(response.status_code, 200)
        return response


class MatePvProvenanceTests(_Closed, TestCase):

    def test_the_closed_child_names_the_search_that_proved_it(self):
        response = self._page(self.top)

        provenance = response.context['provenance']
        self.assertEqual(provenance['text'], 'proven by the search of')
        self.assertEqual(provenance['link_text'], 'the position before e2e4')
        self.assertEqual(provenance['url'],
                         f'/atomicdb/explore/{self.root.key}/')
        self.assertContains(response, 'proven by the search of')
        self.assertContains(response, 'the position before e2e4')

    def test_the_link_keeps_the_visitors_route(self):
        """Arriving through 1.e4 e5, the link goes one ply back on it."""
        response = self._page(self.link, play=','.join(LINE[:2]))

        self.assertEqual(response.context['provenance']['url'],
                         f'/atomicdb/explore/{self.top.key}/?play=e2e4')

    def test_a_deeper_link_names_the_line_it_is_part_of(self):
        response = self._page(self.link)

        provenance = response.context['provenance']
        self.assertEqual(provenance['text'],
                         'proven as part of the winning line of')
        self.assertEqual(provenance['link_text'], 'the position before e7e5')
        self.assertTrue(provenance['url'].startswith(
            f'/atomicdb/explore/{self.top.key}/'))

    def test_a_search_that_no_longer_shows_the_line_names_nothing(self):
        Position.objects.filter(key=self.root.key).update(last_analysis=[
            {'move': 'd2d4', 'eval_cp': 30, 'mate': None,
             'pv': ['d2d4', 'd7d5']}])

        response = self._page(self.top)

        self.assertIsNone(response.context['provenance'])
        self.assertNotContains(response, 'proven by the search of')

    def test_an_earlier_wider_pass_does_not_count(self):
        Position.objects.filter(key=self.root.key).update(last_analysis=[
            {'move': 'd2d4', 'eval_cp': 30, 'mate': None,
             'pv': ['d2d4', 'd7d5']},
            dict(self.line, prior_pass=True)])

        self.assertIsNone(self._page(self.top).context['provenance'])

    def test_it_costs_one_bounded_query(self):
        top = Position.objects.get(key=self.top.key)

        with self.assertNumQueries(1, using=settings.ATOMICDB_DATABASE_ALIAS):
            provenance = views._mate_pv_provenance(top)

        self.assertEqual(provenance['text'], 'proven by the search of')


class MinimaxProvenanceTests(_Closed, TestCase):

    def test_the_analysed_position_names_the_winning_reply(self):
        self.assertEqual((self.root.status, self.root.closure),
                         ('WHITE_WIN', 'MINIMAX'))

        response = self._page(self.root)

        self.assertEqual(response.context['provenance'],
                         {'text': 'proven by minimax from its replies: '
                                  'e2e4 wins'})
        self.assertContains(response,
                            'proven by minimax from its replies: e2e4 wins')

    def test_it_reads_the_rows_and_costs_nothing(self):
        rows = views._child_moves(self.root)

        with self.assertNumQueries(0, using=settings.ATOMICDB_DATABASE_ALIAS):
            views._minimax_provenance(self.root, rows)


class MinimaxShapesTests(TestCase):
    """Every reply lost, a draw, a single reply, several winners."""

    def _all_children(self, status):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        Position.objects.filter(
            key__in=Edge.objects.filter(parent=root).values('child_id'),
        ).update(status=status, closure='MINIMAX', mate_in=None)
        return root

    def test_a_loss_names_every_reply(self):
        root = self._all_children('BLACK_WIN')
        Position.objects.filter(key=root.key).update(
            status='BLACK_WIN', closure='MINIMAX')
        root.refresh_from_db()

        provenance = views._provenance(root, views._child_moves(root))

        self.assertEqual(provenance['text'],
                         'proven by minimax from its replies: all 20 lose')

    def test_a_draw_names_the_drawing_replies(self):
        root = self._all_children('BLACK_WIN')
        Position.objects.filter(key=_key('e2e4')).update(status='DRAW')
        Position.objects.filter(key=root.key).update(
            status='DRAW', closure='MINIMAX')
        root.refresh_from_db()

        provenance = views._provenance(root, views._child_moves(root))

        self.assertEqual(provenance['text'],
                         'proven by minimax from its replies: e2e4 draws, '
                         'none wins')

    def test_several_winners_lead_with_the_witness(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        winners = ['a2a3', 'b2b3', 'c2c3', 'd2d3', 'e2e3']
        Position.objects.filter(
            key__in=[_key(uci) for uci in winners],
        ).update(status='WHITE_WIN', closure='MATE_PV', mate_in=3)
        Position.objects.filter(key=root.key).update(
            status='WHITE_WIN', closure='MINIMAX', mate_in=4,
            best_move='d2d3')
        root.refresh_from_db()

        provenance = views._provenance(root, views._child_moves(root))

        self.assertEqual(provenance['text'],
                         'proven by minimax from its replies: d2d3, a2a3, '
                         'b2b3 and 2 more win')

    def test_a_single_reply(self):
        rows = [{'uci': 'e1d1', 'status': 'BLACK_WIN'}]
        pos = Position(key='k' * 64, fen='4k3/8/8/8/8/8/8/4K3 w - - 0 1',
                       status='BLACK_WIN', closure='MINIMAX')

        self.assertEqual(views._minimax_provenance(pos, rows)['text'],
                         'proven by minimax from its replies: its only '
                         'reply, e1d1, loses')

    def test_an_open_position_says_nothing_and_asks_nothing(self):
        root = ingest.get_or_create_position(logic.start_fen())

        with self.assertNumQueries(0, using=settings.ATOMICDB_DATABASE_ALIAS):
            self.assertIsNone(views._provenance(root, []))

    def test_a_terminal_or_tablebase_closure_has_no_line(self):
        root = ingest.get_or_create_position(logic.start_fen())
        for closure in ('TERMINAL', 'TB', 'SOLVE'):
            root.status, root.closure = 'WHITE_WIN', closure
            self.assertIsNone(views._provenance(root, []))
