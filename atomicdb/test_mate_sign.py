from unittest.mock import patch

from django.test import TestCase

from . import ingest, logic
from .models import Edge


BLACK_MATE_FEN = 'qk6/8/8/8/8/8/6PP/6RK b - - 0 1'


class MateWinnerPerspectiveTests(TestCase):
    """The worker's mate sign is already normalized to White's perspective."""

    @patch('atomicdb.ingest.logic.prove_forced_mate',
           return_value='INCONCLUSIVE')
    @patch('atomicdb.ingest.logic.verify_mate_pv', return_value=True)
    def test_all_parent_turn_and_winner_quadrants(self, verify, prove):
        cases = [
            (logic.start_fen(), 'e2e4', 1, 'WHITE_WIN'),
            (logic.apply_move(
                logic.apply_move(logic.start_fen(), 'g1f3'), 'd7d5'),
             'd2d4', -1, 'BLACK_WIN'),
            (logic.apply_move(logic.start_fen(), 'g1f3'),
             'd7d5', 1, 'WHITE_WIN'),
            (logic.apply_move(logic.start_fen(), 'b1c3'),
             'e7e5', -1, 'BLACK_WIN'),
        ]
        for fen, move, mate, expected in cases:
            with self.subTest(stm=fen.split()[1], mate=mate):
                parent = ingest.get_or_create_position(fen)
                ingest.ingest_analysis(parent.key, [{
                    'move': move, 'eval_cp': 9999 if mate > 0 else -9999,
                    'mate': mate, 'pv': [move],
                }], nodes_budget=1_000)
                child = Edge.objects.get(parent=parent, move_uci=move).child
                self.assertEqual(child.status, expected)
                self.assertEqual(child.closure, 'MATE_PV')
                self.assertEqual(child.proof, 'ENGINE')

        self.assertEqual(verify.call_count, 4)
        self.assertEqual(prove.call_count, 4)

    def test_real_black_mate_fixture_is_verified_from_black_to_move(self):
        fen = logic.canonical_fen(BLACK_MATE_FEN)
        self.assertIn('a8g2', logic.legal_moves(fen))
        self.assertEqual(
            logic.terminal_status(logic.apply_move(fen, 'a8g2'))[0],
            'BLACK_WIN')
        self.assertTrue(logic.verify_mate_pv(fen, ['a8g2'], False))
