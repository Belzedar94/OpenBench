from . import ingest, logic
from .models import Edge
from .testing import TestCase


WHITE_FORCED_FEN = '4p3/8/8/7k/n7/Kp2n3/3p4/1Q6 w - - 0 1'
WHITE_FORCED_PV = ['b1g6', 'h5h4', 'g6g4']
BLACK_FORCED_FEN = '6q1/4P3/3N2Pk/7N/K7/8/8/3P4 b - - 0 1'
BLACK_FORCED_PV = ['g8b3', 'a4a5', 'b3b5']


class MateWinnerPerspectiveTests(TestCase):
    """Exercise all parent-turn/winner quadrants without mocked proof code."""

    def _assert_closes(self, parent_fen, pv, mate, expected):
        parent = ingest.get_or_create_position(parent_fen)
        ingest.ingest_analysis(parent.key, [{
            'move': pv[0],
            'eval_cp': 9_999 if mate > 0 else -9_999,
            'mate': mate,
            'pv': pv,
        }], nodes_budget=1_000)
        child = Edge.objects.get(parent=parent, move_uci=pv[0]).child
        self.assertEqual(child.status, expected)
        self.assertEqual(child.closure, 'MATE_PV')
        self.assertEqual(child.proof, 'ANDOR')
        self.assertEqual(child.won_line, ' '.join(pv[1:]))

    def test_white_parent_white_winner(self):
        self._assert_closes(
            WHITE_FORCED_FEN, WHITE_FORCED_PV, 2, 'WHITE_WIN')

    def test_black_parent_black_winner(self):
        self._assert_closes(
            BLACK_FORCED_FEN, BLACK_FORCED_PV, -2, 'BLACK_WIN')

    def test_white_parent_black_winner(self):
        predecessor = '6q1/4P3/3N2Pk/7N/8/K7/8/3P4 w - - 0 1'
        pv = ['a3a4', *BLACK_FORCED_PV]
        self.assertEqual(
            logic.canonical_fen(logic.apply_move(predecessor, pv[0])),
            logic.canonical_fen(BLACK_FORCED_FEN),
        )
        self._assert_closes(predecessor, pv, -2, 'BLACK_WIN')

    def test_black_parent_white_winner(self):
        predecessor = '4p3/8/7k/8/n7/Kp2n3/3p4/1Q6 b - - 0 1'
        pv = ['h6h5', *WHITE_FORCED_PV]
        self.assertEqual(
            logic.canonical_fen(logic.apply_move(predecessor, pv[0])),
            logic.canonical_fen(WHITE_FORCED_FEN),
        )
        self._assert_closes(predecessor, pv, 2, 'WHITE_WIN')
