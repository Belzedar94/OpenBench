"""The explorer's win / draw / loss bars, its "play a line" buttons and its
keyboard shortcuts (inspired by chessdb's explorer, 26-Nov-2026).

All three are display only: the bars are read off what each row already
shows, the player walks the page's own links, and nothing here writes a
stored value, a closure or a backup.
"""

import json
import re

from . import logic, views, wdl
from .models import Edge, Position
from .testing import TestCase

ENGINE = 128_000_000


class EstimateTests(TestCase):

    def test_shares_always_add_up_to_a_hundred(self):
        for cp in range(-3000, 3001, 37):
            self.assertEqual(sum(wdl.estimate(cp)), 100, cp)

    def test_it_is_symmetric_between_the_two_sides(self):
        for cp in (0, 50, 254, 400, 1029, 9000):
            win, draw, loss = wdl.estimate(cp)
            self.assertEqual(wdl.estimate(-cp), (loss, draw, win))

    def test_the_win_share_grows_with_the_score(self):
        wins = [wdl.estimate(cp)[0] for cp in range(-2000, 2001, 50)]
        self.assertEqual(wins, sorted(wins))

    def test_the_documented_anchors(self):
        # The numbers the module docstring and the docs page quote.
        self.assertEqual(wdl.estimate(0), (6, 88, 6))
        self.assertEqual(wdl.estimate(100), (12, 85, 3))
        self.assertEqual(wdl.estimate(400), (50, 50, 0))
        self.assertEqual(wdl.estimate(1000), (98, 2, 0))

    def test_huge_scores_do_not_overflow(self):
        self.assertEqual(wdl.estimate(10_000_000), (100, 0, 0))
        self.assertEqual(wdl.estimate(-10_000_000), (0, 0, 100))


class RowBarTests(TestCase):

    WIN, LOSS = 'WHITE_WIN', 'BLACK_WIN'

    def bar(self, **row):
        row.setdefault('status', 'UNKNOWN')
        row.setdefault('closure', None)
        row.setdefault('score', None)
        return wdl.row_wdl(row, self.WIN, self.LOSS)

    def test_a_proven_win_fills_the_bar(self):
        bar = self.bar(status=self.WIN, closure='MATE_PV', score=10_000)
        self.assertEqual((bar['w'], bar['d'], bar['l']), (100, 0, 0))
        self.assertTrue(bar['proven'])
        self.assertIn('proven win (MATE_PV)', bar['title'])

    def test_a_proven_loss_and_a_proven_draw(self):
        loss = self.bar(status=self.LOSS, closure='TB', score=-10_000)
        draw = self.bar(status='DRAW', closure='TB', score=0)
        self.assertEqual((loss['w'], loss['d'], loss['l']), (0, 0, 100))
        self.assertEqual((draw['w'], draw['d'], draw['l']), (0, 100, 0))
        self.assertTrue(loss['proven'] and draw['proven'])

    def test_a_repetition_is_a_proven_draw_whatever_the_status(self):
        bar = self.bar(repetition=True, score=0)
        self.assertEqual((bar['w'], bar['d'], bar['l']), (0, 100, 0))
        self.assertTrue(bar['proven'])
        self.assertIn('repetition draw', bar['title'])

    def test_an_open_number_is_an_estimate_and_says_so(self):
        bar = self.bar(score=400)
        self.assertEqual((bar['w'], bar['d'], bar['l']), (50, 50, 0))
        self.assertFalse(bar['proven'])
        self.assertIn('estimate from score', bar['title'])

    def test_a_row_with_no_number_has_no_bar(self):
        self.assertIsNone(self.bar())


class PlaylineTests(TestCase):

    def test_blocked_rows_and_rows_without_a_link_are_left_out(self):
        pos = Position(key='k' * 16, fen=logic.start_fen(), status='UNKNOWN')
        moves = [
            {'uci': 'g1f3', 'key': 'a', 'url': '/a/', 'score': 50,
             'blocked': False},
            {'uci': 'e2e4', 'key': 'b', 'url': None, 'score': 0,
             'blocked': True},
            {'uci': 'd2d4', 'key': 'c', 'url': '/c/', 'score': None,
             'blocked': False},
        ]
        data = views._playline(pos, moves, 'g1f3')
        self.assertTrue(data['open'])
        self.assertEqual(data['best'], 'g1f3')
        self.assertEqual([r['uci'] for r in data['rows']], ['g1f3', 'd2d4'])
        self.assertEqual(data['rows'][0],
                         {'uci': 'g1f3', 'key': 'a', 'url': '/a/',
                          'score': 50})

    def test_a_closed_position_is_not_open(self):
        pos = Position(key='k' * 16, fen=logic.start_fen(),
                       status='WHITE_WIN', closure='MINIMAX')
        self.assertFalse(views._playline(pos, [], None)['open'])


def _position(fen, **kw):
    # The start position is seeded by a migration: take it over.
    pos, _ = Position.objects.update_or_create(
        key=logic.key_of(fen), defaults={'fen': fen, **kw})
    return pos


class ExplorePageTests(TestCase):

    def setUp(self):
        self.root = _position(logic.start_fen(), expanded=True,
                              eval_cp=300, nodes_invested=ENGINE)
        # Searched: +420 for White, the side to move.
        self.searched = self._child('g1f3', eval_cp=420,
                                    nodes_invested=ENGINE)
        # Proven for White.
        self.proven = self._child('e2e3', status='WHITE_WIN',
                                  closure='MINIMAX')
        # In the tree, no value at all.
        self.blank = self._child('d2d4')

    def _child(self, uci, **kw):
        child = _position(logic.apply_move(self.root.fen, uci), **kw)
        Edge.objects.create(parent=self.root, move_uci=uci, child=child)
        return child

    def page(self, pos=None):
        pos = pos or self.root
        response = self.client.get(f'/atomicdb/explore/{pos.key}/')
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def test_each_row_with_a_number_carries_its_bar(self):
        response, body = self.page()
        rows = {m['uci']: m for m in response.context['moves']}
        self.assertEqual(
            (rows['e2e3']['wdl']['w'], rows['e2e3']['wdl']['proven']),
            (100, True))
        self.assertEqual(rows['g1f3']['wdl']['w'],
                         wdl.estimate(420)[0])
        self.assertFalse(rows['g1f3']['wdl']['proven'])
        self.assertIsNone(rows['d2d4']['wdl'])
        # Drawn: the proven one solid, the estimate faint and labelled.
        self.assertIn('<span class="wdl-w" style="width:100%">', body)
        self.assertRegex(body, r'class="wdl wdl-est" data-stm="w"')
        self.assertIn('estimate from score', body)
        self.assertEqual(body.count('role="img" title='),
                         2)

    def test_the_bar_is_from_the_side_to_move(self):
        # Black to move after 1. Nf3: the row values are Black's.
        fen = self.searched.fen
        reply = _position(logic.apply_move(fen, 'g8f6'), eval_cp=-900,
                          nodes_invested=ENGINE)
        Edge.objects.create(parent=self.searched, move_uci='g8f6',
                            child=reply)
        self.searched.expanded = True
        self.searched.save()
        response, body = self.page(self.searched)
        row = next(m for m in response.context['moves']
                   if m['uci'] == 'g8f6')
        self.assertEqual(row['score'], 900)
        self.assertEqual((row['wdl']['w'], row['wdl']['d'], row['wdl']['l']),
                         wdl.estimate(900))
        self.assertIn('data-stm="b"', body)

    def test_nothing_stored_changes(self):
        before = list(Position.objects.order_by('key').values())
        self.page()
        self.assertEqual(list(Position.objects.order_by('key').values()),
                         before)

    def test_the_player_reads_the_stored_rows_and_the_arrow(self):
        response, body = self.page()
        found = re.search(
            r'<script id="atomicdb-playline" type="application/json">'
            r'(.*?)</script>', body, re.S)
        self.assertIsNotNone(found)
        data = json.loads(found.group(1))
        self.assertEqual(data, response.context['playline'])
        self.assertTrue(data['open'])
        self.assertEqual(data['key'], self.root.key)
        self.assertEqual(data['best'], response.context['best_move'])
        self.assertEqual(data['best'], 'e2e3')
        self.assertEqual({r['uci'] for r in data['rows']},
                         {'g1f3', 'e2e3', 'd2d4'})
        for r in data['rows']:
            self.assertTrue(r['url'].startswith(
                '/atomicdb/explore/%s/' % r['key']))
        self.assertIn('id="play-best"', body)
        self.assertIn('id="play-random"', body)

    def test_the_keys_hint_offers_q_only_where_the_request_button_is(self):
        _, body = self.page()
        self.assertIn('id="reqbtn"', body)
        self.assertIn('<kbd>Q</kbd>', body)
        self.assertIn('<kbd>F</kbd>', body)
        _, closed = self.page(self.proven)
        self.assertNotIn('id="reqbtn"', closed)
        self.assertNotIn('<kbd>Q</kbd>', closed)
        self.assertIn('<kbd>F</kbd>', closed)

    def test_a_closed_position_tells_the_player_to_stop(self):
        response, _ = self.page(self.proven)
        self.assertFalse(response.context['playline']['open'])

    def test_the_docs_explain_the_bar_the_player_and_the_keys(self):
        body = self.client.get('/atomicdb/docs/reading/').content.decode()
        self.assertIn('id="col-wdl"', body)
        self.assertIn('estimate from the score', body)
        self.assertIn('id="play"', body)
        self.assertIn('Play random line', body)
