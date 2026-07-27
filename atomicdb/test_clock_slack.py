"""Clock context v1: ``clock_slack``, the fresh-context rule and the e.p.
audit (P1b).

``canonical_fen`` zeroes the counters, so every node in the DAG means "this
position with the fifty-move counter at zero".  A decisive closure proved from
zero does not automatically survive being reached with the counter already
running.  These tests pin the five recurrences, the rule that decides when a
child may carry a closure upward, and the audit that measures how much of the
identity is not what it claims to be.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from . import ingest, logic
from .models import DBEvent, Edge, Position
from .testing import TestCase

# White pawn e2, black pawn d4: 1.e2e4 offers an en-passant capture.
EP_ORDINARY = '7k/8/8/8/3p4/8/4P3/4K3 w - - 0 1'
# Same, but capturing would explode Black's own king.
EP_OWN_KING = '8/8/8/8/3p4/3k4/4P3/6K1 w - - 0 1'
# Same, but the capturing pawn is pinned on the d-file.
EP_PINNED = '3k4/8/8/8/3p4/8/4P3/3RK3 w - - 0 1'

QUIET_FEN = '8/8/8/8/8/8/1k6/K6Q w - - 0 1'
CAPTURE_FEN = '4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1'


class ZeroingTests(TestCase):

    def test_pawn_moves_and_captures_reset_the_counter(self):
        self.assertTrue(logic.is_zeroing(logic.start_fen(), 'e2e4'))
        self.assertTrue(logic.is_zeroing(CAPTURE_FEN, 'e4d5'))

    def test_quiet_moves_do_not(self):
        self.assertFalse(logic.is_zeroing(logic.start_fen(), 'g1f3'))
        self.assertFalse(logic.is_zeroing(QUIET_FEN, 'h1h2'))


class SlackRecurrenceTests(SimpleTestCase):
    """The five cases, one test each."""

    def test_terminal_takes_the_maximum(self):
        self.assertEqual(logic.CLOCK_SLACK_MAX, 100)

    def test_exhaustive_proof_uses_its_worst_reversible_run(self):
        self.assertEqual(logic.slack_from_run(0), 100)
        self.assertEqual(logic.slack_from_run(7), 93)
        self.assertEqual(logic.slack_from_run(250), 0)
        self.assertIsNone(logic.slack_from_run(None))

    def test_uncertified_witness_uses_the_crude_length_bound(self):
        self.assertEqual(logic.slack_from_witness_length(0), 100)
        self.assertEqual(logic.slack_from_witness_length(9), 91)
        self.assertEqual(logic.slack_from_witness_length(500), 0)

    def test_tablebase_needs_a_dtz_or_it_is_zero(self):
        self.assertEqual(logic.slack_from_dtz(None), 0)
        self.assertEqual(logic.slack_from_dtz(0), 99)
        self.assertEqual(logic.slack_from_dtz(10), 89)
        self.assertEqual(logic.slack_from_dtz(-10), 89)   # sign-agnostic
        self.assertEqual(logic.slack_from_dtz(500), 0)

    def test_minimax_win_takes_the_best_usable_winning_edge(self):
        # h1h2 is quiet, so it costs a ply; a zeroing edge would be free.
        slack = logic.minimax_slack(
            QUIET_FEN, [('h1h2', 10), ('h1h8', 40)], [], mover_wins=True)
        self.assertEqual(slack, 39)

    def test_minimax_loss_takes_the_worst_of_every_edge(self):
        slack = logic.minimax_slack(
            QUIET_FEN, [], [('h1h2', 40), ('h1h8', 10)], mover_wins=False)
        self.assertEqual(slack, 9)

    def test_a_zeroing_edge_restores_the_full_range(self):
        self.assertEqual(logic.edge_slack(logic.start_fen(), 'e2e4', 3), 100)
        self.assertEqual(logic.edge_slack(logic.start_fen(), 'g1f3', 3), 2)

    def test_an_unmeasured_child_is_treated_as_zero(self):
        self.assertEqual(logic.edge_slack(QUIET_FEN, 'h1h2', None), 0)


class ProofRunTests(SimpleTestCase):
    """The run comes from the proof TREE, not from the engine's PV."""

    FORCED_MATE_FEN = '4p3/8/8/7k/n7/Kp2n3/3p4/1Q6 w - - 0 1'
    FORCED_MATE_PV = ['b1g6', 'h5h4', 'g6g4']

    def test_a_proof_reports_its_worst_reversible_run(self):
        verdict, run = logic.prove_forced_mate(
            self.FORCED_MATE_FEN, True, 3, budget_positions=200_000,
            hint_pv=self.FORCED_MATE_PV, return_run=True)
        self.assertEqual(verdict, 'PROVEN')
        # Three quiet plies: queen, king, queen. Nothing zeroes.
        self.assertEqual(run, 3)
        self.assertEqual(logic.slack_from_run(run), 97)

    def test_the_old_signature_is_untouched(self):
        self.assertEqual(
            logic.prove_forced_mate(self.FORCED_MATE_FEN, True, 3,
                                    hint_pv=self.FORCED_MATE_PV),
            'PROVEN')

    def test_a_failed_proof_reports_no_run(self):
        verdict, run = logic.prove_forced_mate(
            self.FORCED_MATE_FEN, True, 1, budget_positions=200_000,
            return_run=True)
        self.assertEqual(verdict, 'NO_MATE')
        self.assertIsNone(run)


class FreshContextTests(TestCase):
    """A decisive child needs clock margin to carry a closure up a QUIET edge."""

    def _parent_with(self, move_uci, child_slack):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        child = Edge.objects.get(parent=parent, move_uci=move_uci).child
        child.status = 'BLACK_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ANDOR'
        child.mate_in = 1
        child.clock_slack = child_slack
        child.save()
        return parent, child

    def test_a_quiet_edge_with_no_margin_cannot_close_the_parent(self):
        """Every reply losing would close the root — but not through this."""
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        for edge in Edge.objects.filter(parent=parent).select_related('child'):
            child = edge.child
            child.status = 'BLACK_WIN'
            child.closure = 'MATE_PV'
            child.proof = 'ANDOR'
            child.mate_in = 1
            # Quiet knight moves have no margin; pawn moves zero the counter
            # and are exempt, so give everything zero and rely on the quiet
            # ones to block.
            child.clock_slack = 0
            child.save()

        ingest.backup_cascade(list(Edge.objects.filter(parent=parent)
                                   .values_list('child_id', flat=True)))

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'UNKNOWN')

    def test_the_same_shape_closes_once_the_children_have_margin(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        for edge in Edge.objects.filter(parent=parent).select_related('child'):
            child = edge.child
            child.status = 'BLACK_WIN'
            child.closure = 'MATE_PV'
            child.proof = 'ANDOR'
            child.mate_in = 1
            child.clock_slack = 50
            child.save()

        ingest.backup_cascade(list(Edge.objects.filter(parent=parent)
                                   .values_list('child_id', flat=True)))

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'BLACK_WIN')
        self.assertEqual(parent.closure, 'MINIMAX')
        # Every edge is worth min(50-1, 100) except the pawn moves, which
        # zero and are worth the maximum; an AND node takes the worst.
        self.assertEqual(parent.clock_slack, 49)

    def test_a_zeroing_edge_carries_a_closure_with_no_margin_at_all(self):
        """e2e4 is a pawn move: after it the counter is zero by definition."""
        parent, child = self._parent_with('e2e4', 0)
        # Give every other reply a comfortable margin so only the e2e4 edge is
        # under test.
        for edge in Edge.objects.filter(parent=parent).select_related('child'):
            if edge.move_uci == 'e2e4':
                continue
            other = edge.child
            other.status = 'BLACK_WIN'
            other.closure = 'MATE_PV'
            other.proof = 'ANDOR'
            other.mate_in = 1
            other.clock_slack = 90
            other.save()

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'BLACK_WIN')

    @override_settings(ATOMICDB_FRESH_CONTEXT=False)
    def test_the_rule_can_be_switched_off_for_the_deploy_window(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        for edge in Edge.objects.filter(parent=parent).select_related('child'):
            child = edge.child
            child.status = 'BLACK_WIN'
            child.closure = 'MATE_PV'
            child.proof = 'ANDOR'
            child.mate_in = 1
            child.save()

        ingest.backup_cascade(list(Edge.objects.filter(parent=parent)
                                   .values_list('child_id', flat=True)))

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'BLACK_WIN')


class ClosureSlackTests(TestCase):

    def test_a_terminal_closure_is_born_with_the_maximum(self):
        mated = ingest.get_or_create_position(
            'rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 1')
        self.assertEqual(mated.status, 'BLACK_WIN')
        self.assertEqual(mated.closure, 'TERMINAL')
        self.assertEqual(mated.clock_slack, logic.CLOCK_SLACK_MAX)

    def test_a_terminal_draw_carries_no_slack(self):
        # Atomic stalemate: Rb7 covers a7 and b8, and a king may not capture.
        stalemate = ingest.get_or_create_position(
            'k7/1R6/8/8/8/6K1/8/8 b - - 0 1')
        self.assertEqual(stalemate.status, 'DRAW')
        self.assertIsNone(stalemate.clock_slack)

    def test_a_proven_mate_uses_the_run_and_an_engine_one_uses_the_bound(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        with patch('atomicdb.ingest.logic.verify_mate_pv', return_value=True), \
                patch('atomicdb.ingest.logic.prove_forced_mate',
                      return_value=('PROVEN', 4)):
            ingest.ingest_analysis(parent.key, [{
                'move': 'e2e4', 'eval_cp': 9999, 'mate': 3,
                'pv': ['e2e4', 'a7a6', 'd1h5'],
            }], nodes_budget=1_000)
        child = Edge.objects.get(parent=parent, move_uci='e2e4').child
        self.assertEqual(child.clock_slack, 96)

        other = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'd2d4'))
        with patch('atomicdb.ingest.logic.verify_mate_pv', return_value=True), \
                patch('atomicdb.ingest.logic.prove_forced_mate',
                      return_value=('INCONCLUSIVE', None)):
            ingest.ingest_analysis(other.key, [{
                'move': 'a7a6', 'eval_cp': 9999, 'mate': 2,
                'pv': ['a7a6', 'd1d3'],
            }], nodes_budget=1_000)
        grand = Edge.objects.get(parent=other, move_uci='a7a6').child
        self.assertEqual(grand.clock_slack, 99)   # 100 - 1 ply of witness

    def test_revoking_a_closure_also_withdraws_its_slack(self):
        pos = Position.objects.create(
            key='k' * 64, fen=QUIET_FEN, status='WHITE_WIN',
            closure='MATE_PV', proof='ENGINE', won_line='h1h8', mate_in=1,
            clock_slack=90)

        ingest.revoke_closure(pos.key, reason='test')

        pos.refresh_from_db()
        self.assertIsNone(pos.clock_slack)


class TablebaseDtzTests(TestCase):

    def test_a_wdl_without_dtz_closes_with_zero_slack(self):
        pos = ingest.get_or_create_position('4k3/8/8/8/8/8/8/R3K3 w - - 0 1')
        with patch('atomicdb.ingest.tb.probe_wdl', return_value=2):
            self.assertTrue(ingest.close_by_tb(pos.key, 2))
        pos.refresh_from_db()
        self.assertEqual(pos.status, 'WHITE_WIN')
        self.assertEqual(pos.clock_slack, 0)

    def test_a_reported_dtz_buys_a_real_range(self):
        pos = ingest.get_or_create_position('4k3/8/8/8/8/8/8/R3K3 w - - 0 1')
        with patch('atomicdb.ingest.tb.probe_wdl', return_value=2):
            self.assertTrue(ingest.close_by_tb(pos.key, 2, dtz=12))
        pos.refresh_from_db()
        self.assertEqual(pos.clock_slack, 87)      # 100 - (12 + 1)
        event = DBEvent.objects.filter(kind='NODE_CLOSED',
                                       payload__closure='TB').first()
        self.assertEqual(event.payload['dtz'], 12)
        self.assertFalse(event.payload['dtz_verified'])

    def test_a_tablebase_draw_carries_no_slack(self):
        pos = ingest.get_or_create_position('4k3/8/8/8/8/8/8/4K3 w - - 0 1')
        with patch('atomicdb.ingest.tb.probe_wdl', return_value=0):
            self.assertTrue(ingest.close_by_tb(pos.key, 0, dtz=0))
        pos.refresh_from_db()
        self.assertEqual(pos.status, 'DRAW')
        self.assertIsNone(pos.clock_slack)


class BackfillCommandTests(TestCase):

    def test_backfill_fills_each_kind_and_is_idempotent(self):
        Position.objects.create(
            key='t' * 64, fen=QUIET_FEN, status='WHITE_WIN',
            closure='TERMINAL', mate_in=0)
        Position.objects.create(
            key='m' * 64, fen=QUIET_FEN, status='WHITE_WIN',
            closure='MATE_PV', proof='ENGINE', won_line='h1h2 b2b3 h2h8',
            mate_in=3)
        Position.objects.create(
            key='b' * 64, fen=QUIET_FEN, status='WHITE_WIN', closure='TB')
        out = StringIO()

        call_command('backfill_clock_slack', stdout=out)

        self.assertEqual(Position.objects.get(key='t' * 64).clock_slack, 100)
        self.assertEqual(Position.objects.get(key='m' * 64).clock_slack, 97)
        self.assertEqual(Position.objects.get(key='b' * 64).clock_slack, 0)
        self.assertIn('TERMINAL=1', out.getvalue())

        second = StringIO()
        call_command('backfill_clock_slack', stdout=second)
        self.assertIn('TERMINAL=0', second.getvalue())
        self.assertIn('UNCHANGED=3', second.getvalue())

    def test_dry_run_writes_nothing(self):
        Position.objects.create(
            key='t' * 64, fen=QUIET_FEN, status='WHITE_WIN',
            closure='TERMINAL', mate_in=0)

        call_command('backfill_clock_slack', dry_run=True, stdout=StringIO())

        self.assertIsNone(Position.objects.get(key='t' * 64).clock_slack)
        self.assertFalse(DBEvent.objects.filter(
            kind='CLOCK_SLACK_BACKFILL').exists())

    def test_backfill_uses_a_recorded_dtz_when_there_is_one(self):
        Position.objects.create(
            key='b' * 64, fen=QUIET_FEN, status='WHITE_WIN', closure='TB')
        DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': 'b' * 64, 'closure': 'TB', 'dtz': 30})

        call_command('backfill_clock_slack', stdout=StringIO())

        self.assertEqual(Position.objects.get(key='b' * 64).clock_slack, 69)


class EnPassantAuditTests(TestCase):
    """The identity audit. It reports; it must never re-key anything."""

    def test_a_legal_capture_is_not_a_phantom(self):
        after = logic.apply_move(EP_ORDINARY, 'e2e4')
        verdict = logic.audit_en_passant(after)
        self.assertEqual(verdict['square'], 'e3')
        self.assertEqual(verdict['legal_moves'], ['d4e3'])
        self.assertFalse(verdict['phantom'])

    def test_an_atomic_self_explosion_leaves_a_phantom_square(self):
        """CONFIRMED against the pinned move generator, not hypothetical."""
        after = logic.apply_move(EP_OWN_KING, 'e2e4')
        verdict = logic.audit_en_passant(after)
        self.assertEqual(verdict['square'], 'e3')
        self.assertEqual(verdict['legal_moves'], [])
        self.assertTrue(verdict['phantom'])

    def test_a_pinned_capturing_pawn_leaves_a_phantom_square(self):
        after = logic.apply_move(EP_PINNED, 'e2e4')
        self.assertTrue(logic.audit_en_passant(after)['phantom'])

    def test_no_adjacent_enemy_pawn_drops_the_square_correctly(self):
        after = logic.apply_move(logic.start_fen(), 'e2e4')
        self.assertIsNone(logic.audit_en_passant(after))

    def test_two_identical_positions_get_two_keys(self):
        """The concrete cost: one board, two rows, and a repetition guard
        that compares those keys."""
        phantom = logic.apply_move(EP_OWN_KING, 'e2e4')
        without = ' '.join(phantom.split()[:3] + ['-', '0', '1'])
        self.assertNotEqual(logic.key_of(phantom), logic.key_of(without))
        self.assertEqual(phantom.split()[0], without.split()[0])

    def test_self_test_reports_the_known_shapes(self):
        out = StringIO()
        call_command('audit_en_passant', self_test=True, stdout=out)
        text = out.getvalue()
        self.assertIn('PHANTOM own-king-explodes-d3', text)
        self.assertIn('PHANTOM pinned-capturing-pawn', text)
        self.assertIn('ok      ordinary-legal-capture', text)

    def test_scan_counts_stored_positions_without_touching_them(self):
        phantom = logic.apply_move(EP_OWN_KING, 'e2e4')
        stored = Position.objects.create(key=logic.key_of(phantom),
                                         fen=phantom)
        out = StringIO()

        call_command('audit_en_passant', json=True, stdout=out)

        import json as jsonlib
        report = jsonlib.loads(out.getvalue())
        self.assertEqual(report['phantom'], 1)
        self.assertEqual(report['with_ep'], 1)
        stored.refresh_from_db()
        self.assertEqual(stored.fen, phantom)
