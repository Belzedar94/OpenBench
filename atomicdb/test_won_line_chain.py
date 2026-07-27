"""A mate witness is a tree, not a string.

A MATE_PV closure used to live only on its own node: ``expand`` skips anything
that is not UNKNOWN, so the chain of the winning line was never materialised.
The explorer showed the closure in the header and "unexplored" on every row
underneath, the winning move included, and clicking it landed on a position
the database did not have.
"""

from io import StringIO

from django.core.management import call_command

from . import ingest, logic
from .models import DBEvent, Edge, Position
from .testing import TestCase
from .views import _child_moves

# A genuine atomic mate in three plies: 1.Qg6 Kh4 2.Qg4 explodes the king.
FORCED_MATE_FEN = '7k/6p1/8/8/8/8/8/3QK3 w - - 0 1'


def _witness(fen):
    """The engine's line, taken from the prover rather than made up."""
    verdict, _ = logic.prove_forced_mate(fen, True, 6, budget_positions=200_000,
                                         return_run=True)
    assert verdict == 'PROVEN', verdict


class MaterialiseWonLineTests(TestCase):

    def _closed(self, line, proof='ANDOR', slack=90):
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        pos.status = 'WHITE_WIN'
        pos.closure = 'MATE_PV'
        pos.proof = proof
        pos.won_line = ' '.join(line)
        pos.mate_in = len(line)
        pos.best_move = line[0]
        pos.clock_slack = slack
        pos.save()
        return pos

    def _line(self):
        """A real, verifiable winning line from the fixture position."""
        fen = logic.canonical_fen(FORCED_MATE_FEN)
        for first in logic.legal_moves(fen):
            after = logic.apply_move(fen, first)
            terminal = logic.terminal_status(after)
            if terminal and terminal[0] == 'WHITE_WIN':
                return [first]
            for second in logic.legal_moves(after):
                mid = logic.apply_move(after, second)
                for third in logic.legal_moves(mid):
                    end = logic.apply_move(mid, third)
                    t = logic.terminal_status(end)
                    if t and t[0] == 'WHITE_WIN' and logic.verify_mate_pv(
                            fen, [first, second, third], True):
                        return [first, second, third]
        raise AssertionError('no winning line in the fixture')

    def test_the_whole_chain_becomes_navigable(self):
        line = self._line()
        pos = self._closed(line)

        result = ingest.materialise_won_line(pos)

        self.assertEqual(result['plies'], len(line))
        node = pos
        for index, uci in enumerate(line):
            edge = Edge.objects.get(parent=node, move_uci=uci)
            child = edge.child
            suffix = line[index + 1:]
            if suffix:
                self.assertEqual(child.status, 'WHITE_WIN')
                self.assertEqual(child.closure, 'MATE_PV')
                self.assertEqual(child.mate_in, len(suffix))
                self.assertEqual(child.won_line, ' '.join(suffix))
                self.assertEqual(child.best_move, suffix[0])
            else:
                # The end of the line is terminal on its own merits.
                self.assertEqual(child.status, 'WHITE_WIN')
                self.assertEqual(child.closure, 'TERMINAL')
            node = child

    def test_the_winning_row_now_paints_the_win(self):
        """Exactly the owner's complaint: the winning move said 'unexplored'."""
        line = self._line()
        pos = self._closed(line)
        self.assertEqual(_child_moves(pos), [])   # no edges at all, before

        ingest.materialise_won_line(pos)

        row = next(m for m in _child_moves(pos) if m['uci'] == line[0])
        self.assertEqual(row['status'], 'WHITE_WIN')
        self.assertEqual(row['mate_str'], f'≤M{(len(line) + 1) // 2}')

    def test_the_distance_shrinks_one_ply_at_a_time(self):
        line = self._line()
        pos = self._closed(line)
        ingest.materialise_won_line(pos)

        distances = []
        node = pos
        for uci in line:
            node = Edge.objects.get(parent=node, move_uci=uci).child
            distances.append(node.mate_in)
        self.assertEqual(distances, list(range(len(line) - 1, -1, -1)))

    def test_andor_keeps_its_grade_down_the_chain(self):
        line = self._line()
        pos = self._closed(line, proof='ANDOR')
        ingest.materialise_won_line(pos)
        child = Edge.objects.get(parent=pos, move_uci=line[0]).child
        self.assertEqual(child.proof, 'ANDOR')

    def test_engine_stays_engine_down_the_chain(self):
        """An uncertified witness does not become certified by being walked."""
        line = self._line()
        pos = self._closed(line, proof='ENGINE')
        ingest.materialise_won_line(pos)
        child = Edge.objects.get(parent=pos, move_uci=line[0]).child
        self.assertEqual(child.proof, 'ENGINE')

    def test_a_disputed_witness_materialises_nothing(self):
        line = self._line()
        pos = self._closed(line, proof='DISPUTED')
        result = ingest.materialise_won_line(pos)
        self.assertEqual(result['created_edges'], 0)
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_the_chain_is_not_expanded(self):
        """Only the line's edge: the selector must keep ignoring these."""
        line = self._line()
        pos = self._closed(line)
        ingest.materialise_won_line(pos)
        child = Edge.objects.get(parent=pos, move_uci=line[0]).child
        self.assertFalse(child.expanded)
        self.assertEqual(Edge.objects.filter(parent=child).count(), 1)

    def test_slack_is_inherited_conservatively(self):
        line = self._line()
        pos = self._closed(line, slack=88)
        ingest.materialise_won_line(pos)
        child = Edge.objects.get(parent=pos, move_uci=line[0]).child
        self.assertEqual(child.clock_slack, 88)

    def test_a_transposition_into_the_line_inherits_the_closure(self):
        line = self._line()
        pos = self._closed(line)
        ingest.materialise_won_line(pos)
        middle_fen = logic.apply_move(pos.fen, line[0])

        landed = ingest.get_or_create_position(middle_fen)

        self.assertEqual(landed.status, 'WHITE_WIN')
        self.assertEqual(landed.mate_in, len(line) - 1)

    def test_an_existing_better_closure_is_not_overwritten(self):
        line = self._line()
        pos = self._closed(line)
        middle_fen = logic.apply_move(pos.fen, line[0])
        middle = ingest.get_or_create_position(middle_fen)
        middle.status = 'WHITE_WIN'
        middle.closure = 'SOLVE'
        middle.proof = 'ANDOR'
        middle.mate_in = 1
        middle.save()

        ingest.materialise_won_line(pos)

        middle.refresh_from_db()
        self.assertEqual(middle.closure, 'SOLVE')
        self.assertEqual(middle.mate_in, 1)

    def test_a_witness_that_no_longer_verifies_is_refused(self):
        pos = self._closed(['d1d2', 'h8h7'])
        result = ingest.materialise_won_line(pos, verify=True)
        self.assertEqual(result['rejected'], 'witness-does-not-verify')
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_the_cascade_does_not_re_derive_a_partly_materialised_node(self):
        """A CLOSED node with one edge must not be treated as a backup."""
        line = self._line()
        pos = self._closed(line)
        ingest.materialise_won_line(pos)
        child = Edge.objects.get(parent=pos, move_uci=line[0]).child

        ingest.backup_cascade([child.key])

        child.refresh_from_db()
        self.assertEqual(child.closure, 'MATE_PV')   # not rewritten to MINIMAX
        self.assertEqual(child.status, 'WHITE_WIN')


class ChainRevocationTests(TestCase):

    def test_revoking_a_link_takes_the_whole_chain_above_it(self):
        base = MaterialiseWonLineTests()
        line = base._line.__func__(base)
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', 'ENGINE'
        pos.won_line = ' '.join(line)
        pos.mate_in = len(line)
        pos.best_move = line[0]
        pos.save()
        ingest.materialise_won_line(pos)
        middle = Edge.objects.get(parent=pos, move_uci=line[0]).child

        outcome = ingest.revoke_closure(middle.key, reason='test',
                                        mark_disputed=True)

        self.assertIn(middle.key, outcome['revoked'])
        self.assertIn(pos.key, outcome['revoked'])
        pos.refresh_from_db()
        self.assertEqual(pos.status, 'UNKNOWN')
        self.assertTrue(DBEvent.objects.filter(kind='CLOSURE_REVOKED').exists())

    def test_a_witness_that_does_not_run_through_the_node_survives(self):
        line = ['d1g4']
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', 'ENGINE'
        pos.won_line = ' '.join(line)
        pos.best_move = line[0]
        pos.mate_in = 1
        pos.save()
        # An unrelated closed child, reached by a different move.
        other = ingest.get_or_create_position(
            logic.apply_move(pos.fen, 'd1d2'))
        other.status, other.closure, other.proof = 'WHITE_WIN', 'MATE_PV', 'ENGINE'
        other.mate_in = 5
        other.save()
        Edge.objects.create(parent=pos, move_uci='d1d2', child=other)

        ingest.revoke_closure(other.key, reason='test')

        pos.refresh_from_db()
        self.assertEqual(pos.status, 'WHITE_WIN')


class BackfillCommandTests(TestCase):

    def _closed(self, line):
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', 'ANDOR'
        pos.won_line = ' '.join(line)
        pos.mate_in = len(line)
        pos.best_move = line[0]
        pos.clock_slack = 95
        pos.save()
        return pos

    def test_backfill_materialises_and_is_idempotent(self):
        base = MaterialiseWonLineTests()
        line = base._line.__func__(base)
        pos = self._closed(line)
        out = StringIO()

        call_command('materialise_mate_lines', json=True, stdout=out)
        first = __import__('json').loads(out.getvalue())

        self.assertGreaterEqual(first["witnesses"], 1)
        self.assertGreater(first['edges_created'], 0)
        before = Position.objects.count(), Edge.objects.count()

        second_out = StringIO()
        call_command('materialise_mate_lines', json=True, stdout=second_out)
        second = __import__('json').loads(second_out.getvalue())

        self.assertEqual(second['edges_created'], 0)
        self.assertEqual(second['nodes_closed'], 0)
        self.assertEqual((Position.objects.count(), Edge.objects.count()),
                         before)

    def test_dry_run_writes_nothing(self):
        base = MaterialiseWonLineTests()
        pos = self._closed(base._line.__func__(base))

        call_command('materialise_mate_lines', dry_run=True, stdout=StringIO())

        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_disputed_rows_are_not_selected(self):
        base = MaterialiseWonLineTests()
        pos = self._closed(base._line.__func__(base))
        pos.proof = 'DISPUTED'
        pos.save(update_fields=['proof'])
        out = StringIO()

        call_command('materialise_mate_lines', json=True, stdout=out)

        self.assertEqual(__import__('json').loads(out.getvalue())['witnesses'],
                         0)
