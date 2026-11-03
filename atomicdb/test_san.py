"""Moves in SAN on the pages, UCI everywhere a machine reads them (§ san)."""

import json
import random
import re
from unittest import mock

import pyffish as pf
from django.core.cache import cache
from django.test import Client, override_settings

from . import logic, san
from .models import Edge, Position
from .test_explore_performance import CACHE_FOR_TESTS
from .testing import TestCase


START = logic.start_fen()

# Positions where SAN has something to say beyond the piece and the square:
# castling both ways, en passant, promotions, and pieces that need a file, a
# rank or both to be told apart.
TRICKY = (
    'r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1',
    'rnbqkbnr/ppp1p1pp/5p2/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 1',
    '4k3/1P6/8/8/8/8/6p1/4K2R b K - 0 1',
    'n1n1k3/8/8/1N1N4/8/1N1N4/8/4K3 w - - 0 1',
    'R6R/8/8/R6R/8/8/8/1k2K3 w - - 0 1',
    'q3q3/8/8/q3q3/8/8/8/k5K1 b - - 0 1',
    '4k3/8/8/8/8/8/3q4/4K3 b - - 0 1',
)


def _movegen_sans(fen):
    return {uci: pf.get_san(logic.VARIANT, fen, uci)
            for uci in logic.legal_moves(fen)}


class SanAgreesWithTheMovegenTests(TestCase):
    """Every name is the one pyffish gives, with and without the legal moves.

    Measured once over 34,618 moves of random games with no difference; this
    keeps a smaller sample so the suite stays fast.
    """

    def _agree(self, fen):
        expected = _movegen_sans(fen)
        legal = list(expected)
        self.assertEqual(san.sans(fen, legal, legal=legal), expected, fen)
        self.assertEqual(san.sans(fen, legal), expected, fen)

    def test_the_tricky_positions(self):
        for fen in TRICKY:
            self._agree(fen)

    def test_a_random_game(self):
        rng = random.Random(20261103)
        fen = START
        for _ply in range(60):
            legal = logic.legal_moves(fen)
            if not legal:
                break
            self._agree(fen)
            fen = pf.get_fen(logic.VARIANT, fen, [rng.choice(legal)])

    def test_the_names_that_matter(self):
        names = san.sans(TRICKY[0], ['e1g1', 'e1c1', 'a1a8'])
        self.assertEqual(names, {'e1g1': 'O-O', 'e1c1': 'O-O-O',
                                 'a1a8': 'Rxa8'})
        self.assertEqual(san.san(TRICKY[1], 'e5d6'), 'exd6')
        self.assertEqual(san.san(TRICKY[2], 'g2h1q'), 'gxh1=Q')
        self.assertEqual(san.san(TRICKY[3], 'b3d4'), 'N3d4')
        self.assertEqual(san.san(TRICKY[4], 'a5a6'), 'R5a6')
        self.assertEqual(san.san(TRICKY[4], 'a5e5'), 'Rae5')

    def test_the_common_case_asks_the_movegen_nothing(self):
        legal = logic.legal_moves(START)
        with mock.patch('pyffish.get_san') as named, \
                mock.patch('atomicdb.logic.legal_moves') as generated:
            names = san.sans(START, legal)
        self.assertEqual(names['g1f3'], 'Nf3')
        self.assertEqual(named.call_count, 0)
        self.assertEqual(generated.call_count, 0)

    def test_two_pieces_on_one_square_ask_for_the_legal_moves_once(self):
        fen = TRICKY[3]
        legal = logic.legal_moves(fen)
        with mock.patch('atomicdb.logic.legal_moves',
                        wraps=logic.legal_moves) as generated:
            san.sans(fen, legal)
        self.assertEqual(generated.call_count, 1)

    def test_what_cannot_be_named_keeps_its_uci(self):
        self.assertEqual(san.san(START, 'e4e5'), 'e4e5')
        self.assertEqual(san.san('', 'g1f3'), 'g1f3')
        # Not among the legal moves it was given: no guess either.
        self.assertEqual(san.san(START, 'g1f3', legal=['b1c3']), 'g1f3')


def _row_of(body, uci):
    for row in re.findall(r'<tr><td><span class="chip.*?</tr>', body, re.S):
        if f'title="{uci}"' in row:
            return row
    raise AssertionError(f'no row for {uci}')


@override_settings(CACHES=CACHE_FOR_TESTS)
class ExplorerSanTests(TestCase):

    def setUp(self):
        cache.clear()
        self.root = Position.objects.get_or_create(
            key=logic.key_of(START), defaults={'fen': START})[0]
        fen = logic.apply_move(START, 'g1f3')
        self.child = Position.objects.create(key=logic.key_of(fen), fen=fen,
                                             eval_cp=20, visits=1)
        Edge.objects.create(parent=self.root, child=self.child,
                            move_uci='g1f3')
        self.url = f'/atomicdb/explore/{self.root.key}/'

    def test_the_table_names_moves_in_san_with_the_uci_beside(self):
        body = Client().get(self.url).content.decode()

        row = _row_of(body, 'g1f3')
        self.assertIn('<strong data-uci="g1f3" title="g1f3">Nf3</strong>', row)
        self.assertIn(f'href="/atomicdb/explore/{self.child.key}/', row)
        # An off-tree reply links by its UCI and reads in SAN.
        offtree = _row_of(body, 'e2e4')
        self.assertIn('<strong data-uci="e2e4" title="e2e4">e4</strong>', offtree)
        self.assertIn('/atomicdb/goto/', offtree)
        self.assertIn('e2e4/', offtree)
        self.assertNotIn('<strong>g1f3</strong>', body)

    def test_the_keyboard_list_reads_in_san_and_links_in_uci(self):
        # The list lives on the home page's board; the explorer's table is
        # its list there.
        body = Client().get('/atomicdb/').content.decode()

        nav = re.search(r'<nav aria-label="Legal moves">(.*?)</nav>', body,
                        re.S).group(1)
        self.assertRegex(nav, r'href="[^"]*/b1c3/[^"]*" title="b1c3">Nc3</a>')
        # The board script still reads UCI.
        moves = json.loads(re.search(
            r'<script id="atomicdb-legal-moves" type="application/json">'
            r'(.*?)</script>', body, re.S).group(1))
        self.assertIn('g1f3', moves)

    def test_the_api_keeps_uci(self):
        data = Client().get('/atomicdb/api/query', {'fen': START}).json()

        self.assertEqual([move['uci'] for move in data['moves']], ['g1f3'])
        self.assertNotIn('san', data['moves'][0])
        self.assertEqual(data['best_move'], 'g1f3')

    def test_the_transpositions_name_their_moves(self):
        # 1.Nc3 Nc6 2.Nf3 and 1.Nf3 Nc6 2.Nc3 meet in one position.
        def walk(ucis):
            fen = START
            for uci in ucis:
                fen = logic.apply_move(fen, uci)
            return fen
        meet_fen = walk(['b1c3', 'b8c6', 'g1f3'])
        meet = Position.objects.create(key=logic.key_of(meet_fen),
                                       fen=meet_fen)
        for line, last in ((['b1c3', 'b8c6'], 'g1f3'),
                           (['g1f3', 'b8c6'], 'b1c3')):
            fen = walk(line)
            parent = Position.objects.get_or_create(
                key=logic.key_of(fen), defaults={'fen': fen})[0]
            Edge.objects.create(parent=parent, child=meet, move_uci=last)

        body = Client().get(f'/atomicdb/explore/{meet.key}/').content.decode()

        # A folded toggle with the count, the links inside it.
        crumb = re.search(
            r'<details class="suggest-name transpositions">\s*<summary[^>]*>'
            r'transpositions \(2\)</summary>(.*?)</details>', body,
            re.S).group(1)
        self.assertIn('title="g1f3">Nf3</a>', crumb)
        self.assertIn('title="b1c3">Nc3</a>', crumb)

    def test_a_proven_line_reads_in_san(self):
        Position.objects.filter(pk=self.root.pk).update(
            status='WHITE_WIN', closure='MATE_PV', won_line='g1f3 f7f6')

        body = Client().get(self.url).content.decode()

        self.assertIn('<strong data-uci="g1f3 f7f6" title="g1f3 f7f6">Nf3 f6</strong>', body)

    def test_a_witness_move_reads_in_san(self):
        Position.objects.filter(pk=self.child.pk).update(status='WHITE_WIN',
                                                         closure='MINIMAX')
        Position.objects.filter(pk=self.root.pk).update(
            status='WHITE_WIN', closure='MINIMAX', best_move='g1f3')

        body = Client().get(self.url).content.decode()

        self.assertIn('<strong data-uci="g1f3" title="g1f3">Nf3</strong> <span class="dim">'
                      '(follow it below)', body)
        self.assertIn('proven by minimax from its replies: Nf3 wins', body)

    def test_the_page_asks_pyffish_for_no_name(self):
        with mock.patch('pyffish.get_san') as named, \
                mock.patch('atomicdb.logic.legal_moves',
                           wraps=logic.legal_moves) as generated:
            response = Client().get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(named.call_count, 0)
        self.assertEqual(generated.call_count, 1)


@override_settings(CACHES=CACHE_FOR_TESTS)
class HomeSanTests(TestCase):

    def setUp(self):
        cache.clear()
        root = Position.objects.get_or_create(
            key=logic.key_of(START), defaults={'fen': START})[0]
        fen = logic.apply_move(START, 'g1f3')
        child = Position.objects.create(key=logic.key_of(fen), fen=fen,
                                        eval_cp=20)
        Edge.objects.create(parent=root, child=child, move_uci='g1f3')
        self.child = child

    def test_the_first_moves_read_in_san(self):
        body = Client().get('/atomicdb/').content.decode()

        self.assertIn(f'<a href="/atomicdb/explore/{self.child.key}/">'
                      '<strong data-uci="g1f3" title="g1f3">Nf3</strong></a>', body)
        self.assertNotIn('<strong>g1f3</strong>', body)
