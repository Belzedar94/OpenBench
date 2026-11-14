"""The rules of Alice Atomic (Fun side quests), and their two copies agreeing.

The server checks every move of a game between two people with
``funquests.alice_atomic``; the browser plays with
``static/atomicdb/funquests/alice_atomic.js``.  The vectors file is written
by the Python copy and read by both, so these tests fail when the file is
stale and, with Node installed, when the browser's copy disagrees.
"""

import os
import shutil
import subprocess
import unittest

from django.test import SimpleTestCase

from .funquests import alice_atomic as rules
from .funquests import vectors


def legal(afen):
    return set(rules.legal_moves(rules.parse(afen)))


class PerftTests(SimpleTestCase):

    def test_counts_from_the_start(self):
        start = rules.start_state()
        self.assertEqual(rules.perft(start, 1), 20)
        self.assertEqual(rules.perft(start, 2), 400)
        self.assertEqual(rules.perft(start, 3), 9388)

    def test_count_at_depth_four(self):
        # Depths 1 to 3 do not tell the capture rules apart (no capture can
        # be answered yet); depth 4 does.
        self.assertEqual(rules.perft(rules.start_state(), 4), 219784)

    def test_the_start_position_round_trips(self):
        self.assertEqual(rules.to_afen(rules.start_state()), rules.START_AFEN)


class AliceMovementTests(SimpleTestCase):

    def test_a_quiet_move_passes_through_to_the_other_board(self):
        after = rules.play(rules.start_state(), 'Ae2e4')
        self.assertEqual(after.boards[0][rules.parse_square('e2')], '')
        self.assertEqual(after.boards[0][rules.parse_square('e4')], '')
        self.assertEqual(after.boards[1][rules.parse_square('e4')], 'P')

    def test_a_quiet_move_needs_the_square_empty_on_the_other_board(self):
        moves = legal('4k3/8/8/8/8/8/4P3/4K3|8/8/8/8/8/4n3/8/8 w - - 0 1')
        self.assertNotIn('Ae2e3', moves)
        # The double step only needs its landing square empty over there.
        self.assertIn('Ae2e4', moves)

    def test_only_pieces_on_the_same_board_block(self):
        # A knight on B a4 does not stop the rook on A a1.
        moves = legal('4k3/8/8/8/8/8/8/R3K3|8/8/8/8/n7/8/8/8 w - - 0 1')
        self.assertIn('Aa1a8', moves)
        self.assertNotIn('Aa1a4', moves)

    def test_promotion_without_capture_passes_through(self):
        state = rules.parse('2r1k3/3P4/8/8/8/8/8/4K3|8/8/8/8/8/8/8/8 w - - 0 1')
        after = rules.play(state, 'Ad7d8n')
        self.assertEqual(after.boards[1][rules.parse_square('d8')], 'N')
        self.assertEqual(after.boards[0][rules.parse_square('d8')], '')


class AtomicCaptureTests(SimpleTestCase):

    def test_a_capture_explodes_on_the_other_board(self):
        state = rules.parse(
            '4k3/8/8/2n5/2b1p3/8/3N4/4K3|8/8/8/2r5/8/1p1q4/8/8 w - - 0 1')
        after = rules.play(state, 'Ad2c4')
        a, b = after.boards
        sq = rules.parse_square
        self.assertEqual(a[sq('c4')], '')    # the victim
        self.assertEqual(a[sq('d2')], '')    # the capturer left
        # Nothing explodes on A.
        self.assertEqual(a[sq('c5')], 'n')
        self.assertEqual(a[sq('e4')], 'p')
        # The capturer passed through to B c4 and exploded there.
        self.assertEqual(b[sq('c4')], '')
        self.assertEqual(b[sq('c5')], '')    # blown up
        self.assertEqual(b[sq('d3')], '')    # blown up
        self.assertEqual(b[sq('b3')], 'p')   # pawns survive

    def test_a_capture_needs_the_square_empty_on_the_other_board(self):
        moves = legal('4k3/8/8/8/2b5/8/3N4/4K3|8/8/8/8/2r5/8/8/8 w - - 0 1')
        self.assertNotIn('Ad2c4', moves)
        moves = legal('4k3/8/8/8/2b5/8/3N4/4K3|8/8/8/8/8/8/8/8 w - - 0 1')
        self.assertIn('Ad2c4', moves)

    def test_pawn_captures_need_it_too(self):
        # The pawn on A d4 may take on c5 (B c5 empty) but not on e5.
        moves = legal(
            '4k3/8/8/2p1p3/3P4/8/8/4K3|8/8/8/4n3/8/8/8/8 w - - 0 1')
        self.assertIn('Ad4c5', moves)
        self.assertNotIn('Ad4e5', moves)

    def test_blowing_up_the_enemy_king_wins(self):
        state = rules.parse(
            '8/5p2/8/7Q/8/8/8/4K3|4k3/8/8/8/8/8/8/8 w - - 0 1')
        after = rules.play(state, 'Ah5f7')
        self.assertIsNone(rules.king_location(after, 'b'))
        self.assertEqual(rules.outcome(after), ('1-0', 'explosion'))

    def test_blowing_up_your_own_king_is_illegal(self):
        # The white king stands on B next to c2, where the rook would explode.
        moves = legal('k7/8/8/8/8/8/2p5/2R5|8/8/8/8/8/8/3K4/8 w - - 0 1')
        self.assertNotIn('Ac1c2', moves)
        # Next to the capture square on A, the king is safe.
        moves = legal('k7/8/8/8/8/8/2pK4/2R5|8/8/8/8/8/8/8/8 w - - 0 1')
        self.assertIn('Ac1c2', moves)

    def test_kings_cannot_capture(self):
        moves = legal('4k3/8/8/8/8/8/3p4/4K3|8/8/8/8/8/8/8/8 w - - 0 1')
        self.assertNotIn('Ae1d2', moves)

    def test_an_explosion_win_beats_a_check_on_your_own_king(self):
        # White is in check on B, yet blowing up the black king is legal.
        afen = '8/5p2/8/7Q/8/8/8/8|4k3/8/8/8/8/8/8/r3K3 w - - 0 1'
        self.assertTrue(rules.in_check(rules.parse(afen), 'w'))
        self.assertIn('Ah5f7', legal(afen))


class CheckTests(SimpleTestCase):

    def test_a_piece_on_the_other_board_gives_no_check(self):
        state = rules.parse('4k3/8/8/8/8/8/8/4K3|4r3/8/8/8/8/8/8/8 w - - 0 1')
        self.assertFalse(rules.in_check(state, 'w'))

    def test_kings_side_by_side_still_check(self):
        state = rules.parse('8/8/8/8/3kK3/8/8/4r3|8/8/8/8/8/8/8/8 w - - 0 1')
        self.assertTrue(rules.in_check(state, 'w'))

    def test_no_check_when_the_capture_could_not_be_made(self):
        base = '8/8/8/8/8/8/8/r3K3|{} w - - 0 1'
        # Check: the rook could take on e1 and explode on B e1.
        self.assertTrue(rules.in_check(
            rules.parse(base.format('7k/8/8/8/8/8/8/8')), 'w'))
        # B e1 is taken: the rook cannot pass through.
        self.assertFalse(rules.in_check(
            rules.parse(base.format('7k/8/8/8/8/8/8/4N3')), 'w'))
        # The black king on B f2 would be blown up.
        self.assertFalse(rules.in_check(
            rules.parse(base.format('8/8/8/8/8/8/5k2/8')), 'w'))

    def test_checkmate_and_stalemate(self):
        mate = rules.parse('k7/1Q6/2K5/8/8/8/8/8|1N6/N7/8/8/8/8/8/8 b - - 0 1')
        self.assertEqual(rules.outcome(mate), ('1-0', 'checkmate'))
        stale = rules.parse('k7/2Q5/1K6/8/8/8/8/8|1N6/NN6/8/8/8/8/8/8 b - - 0 1')
        self.assertEqual(rules.outcome(stale), ('1/2-1/2', 'stalemate'))

    def test_a_king_in_check_can_escape_through_the_glass(self):
        state = rules.parse('k7/1Q6/2K5/8/8/8/8/8|8/8/8/8/8/8/8/8 b - - 0 1')
        self.assertTrue(rules.in_check(state, 'b'))
        self.assertIsNone(rules.outcome(state))


class EnPassantTests(SimpleTestCase):

    def test_en_passant_on_the_board_the_pawn_passed_to(self):
        state = rules.parse('4k3/8/8/8/8/8/4P3/4K3|8/8/8/8/3p4/8/8/8 w - - 0 1')
        after = rules.play(state, 'Ae2e4')
        self.assertEqual(rules.to_afen(after).split()[3], 'Be3')
        self.assertIn('Bd4e3', rules.legal_moves(after))
        taken = rules.play(after, 'Bd4e3')
        self.assertEqual(taken.boards[1][rules.parse_square('e4')], '')
        self.assertEqual(taken.boards[1][rules.parse_square('d4')], '')
        # The capturing pawn exploded on A e3, without touching the kings.
        self.assertEqual(taken.boards[0][rules.parse_square('e3')], '')
        self.assertEqual(rules.king_location(taken, 'w'),
                         (0, rules.parse_square('e1')))

    def test_no_en_passant_when_the_square_passed_over_is_taken(self):
        state = rules.parse(
            '4k3/8/8/8/8/8/4P3/4K3|8/8/8/8/3p4/4n3/8/8 w - - 0 1')
        after = rules.play(state, 'Ae2e4')
        self.assertIsNone(after.ep)

    def test_no_en_passant_from_the_other_board(self):
        state = rules.parse('4k3/8/8/8/3p4/8/4P3/4K3|8/8/8/8/8/8/8/8 w - - 0 1')
        after = rules.play(state, 'Ae2e4')
        self.assertFalse([m for m in rules.legal_moves(after)
                          if m.startswith('Ad4e3')])


class CastlingTests(SimpleTestCase):

    def test_both_pieces_pass_through(self):
        state = rules.parse(
            'r3k2r/8/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/8 w KQkq - 0 1')
        after = rules.play(state, 'Ae1g1')
        self.assertEqual(after.boards[1][rules.parse_square('g1')], 'K')
        self.assertEqual(after.boards[1][rules.parse_square('f1')], 'R')
        self.assertEqual(after.castling, 'kq')

    def test_the_landing_squares_must_be_empty_on_the_other_board(self):
        moves = legal('r3k2r/8/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/6n1 w KQkq - 0 1')
        self.assertNotIn('Ae1g1', moves)
        self.assertIn('Ae1c1', moves)

    def test_not_through_an_attacked_square(self):
        moves = legal(
            'r3k2r/5r2/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/8 w KQkq - 0 1')
        self.assertNotIn('Ae1g1', moves)
        self.assertIn('Ae1c1', moves)

    def test_castling_past_a_rook_that_cannot_really_take(self):
        # The rook on A e7 attacks e1, but B e1 is taken: no check there.
        moves = legal(
            'r3k2r/4r3/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/4N3 w KQkq - 0 1')
        self.assertIn('Ae1g1', moves)
        # The rook on A f7 attacks f1, but its own king stands on B f2.
        moves = legal('r6r/5r2/8/8/8/8/8/R3K2R|8/8/8/8/8/8/5k2/8 w KQ - 0 1')
        self.assertIn('Ae1g1', moves)

    def test_a_rook_that_left_loses_the_right(self):
        state = rules.parse(
            'r3k2r/8/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/8 w KQkq - 0 1')
        after = rules.play(state, 'Ah1h2')
        self.assertEqual(after.castling, 'Qkq')


class DrawTests(SimpleTestCase):

    def test_threefold_repetition(self):
        shuffle = ['Ab1c3', 'Ag8f6', 'Bc3b1', 'Bf6g8']
        _state, found = rules.replay(shuffle)
        self.assertIsNone(found)
        _state, found = rules.replay(shuffle * 2)
        self.assertEqual(found, ('1/2-1/2', 'repetition'))

    def test_fifty_moves(self):
        state = rules.parse('4k3/8/8/8/8/8/8/R3K3|8/8/8/8/8/8/8/8 w - - 100 80')
        self.assertEqual(rules.outcome(state), ('1/2-1/2', 'fifty-move'))

    def test_no_move_after_the_end(self):
        with self.assertRaises(rules.IllegalMove):
            rules.replay(['Ab1c3', 'Ag8f6', 'Bc3b1', 'Bf6g8'] * 2 + ['Ab1c3'])


class SharedVectorTests(SimpleTestCase):

    def test_the_vectors_file_matches_the_rules(self):
        import json
        with open(vectors.VECTORS_PATH, encoding='utf-8') as handle:
            stored = json.load(handle)
        self.assertEqual(stored, vectors.build(),
                         'run python -m atomicdb.funquests.vectors')

    def test_the_browser_rules_agree(self):
        node = shutil.which('node')
        if not node:
            raise unittest.SkipTest('Node is not installed')
        script = os.path.join(os.path.dirname(__file__),
                              'test_alice_atomic_rules.cjs')
        run = subprocess.run([node, script], capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
