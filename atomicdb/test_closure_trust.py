"""The closure trust gate, closure provenance and lines into tablebase wins.

What is defended here:
  * a mate line the server could not prove closes nothing when it comes
    from an account that is not trusted; it is kept as a pending claim;
  * the same line from a trusted account closes, as before;
  * the cooperative line 1.Nf3 a6 2.Ng5 a5 3.Nxf7 from an untrusted account
    no longer closes the start position;
  * proven closures are untouched by the gate;
  * every closure records the task, account and engine build behind it;
  * a line that ends in a position the server's tables score as won for the
    claimed side closes like a mate line, and a wrong-sign claim does not.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from . import ingest, ingest_queue, logic
from .models import (AnalysisTask, ClosureProvenance, DBEvent, Edge,
                     PendingClaim, Position, TrustedAccount)
from .testing import TestCase

COOPERATIVE = ['g1f3', 'a7a6', 'f3g5', 'a6a5', 'g5f7']
# A real forced mate (the one of test_ingest_queue): the exhaustive check
# proves it, so it closes for anybody.
FORCED_MATE_FEN = '4p3/8/8/7k/n7/Kp2n3/3p4/1Q6 w - - 0 1'
FORCED_MATE_PV = ['b1g6', 'h5h4', 'g6g4']
# Rxa5 ends the line in a four-piece ending with a capture.
TB_PARENT = '4k3/8/8/n6b/8/8/8/R2QK3 w - - 0 1'
TB_LINE = ['d1d2', 'e8f7', 'a1a5']
# The same kind of line, but its last move is quiet.
QUIET_PARENT = '4k3/8/8/7b/8/8/8/3QK3 w - - 0 1'
QUIET_LINE = ['d1d2', 'e8f7', 'd2d3']
SHA = 'ab' * 32


def _after(fen, moves):
    for uci in moves:
        fen = logic.apply_move(fen, uci)
    return fen


def _inconclusive(*args, **kwargs):
    return ('INCONCLUSIVE', None) if kwargs.get('return_run') \
        else 'INCONCLUSIVE'


class _Jobs:
    """Results delivered through the ingest queue, as a worker's are."""

    def _deliver(self, position, lines, account, generation=0):
        task = AnalysisTask.objects.create(
            position=position, generation=generation,
            budget_nodes=1_000_000, state='COMPLETED', machine='m1',
            delivered_by=account, engine_sha=SHA, source='AUTO')
        job, _ = ingest_queue.enqueue(task, {
            'lines': lines, 'nodes': 1_000_000, 'elapsed': 1.0,
            'machine': 'm1', 'username': account})
        return task, ingest_queue.apply_job(job)


@mock.patch('atomicdb.logic.prove_forced_mate', side_effect=_inconclusive)
class CooperativeMateTests(_Jobs, TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.line = {'move': COOPERATIVE[0], 'eval_cp': 9995, 'mate': 3,
                     'pv': list(COOPERATIVE)}
        self.child_key = logic.key_of(
            logic.apply_move(logic.start_fen(), COOPERATIVE[0]))

    def test_an_untrusted_account_does_not_close_the_start_position(self, _p):
        task, summary = self._deliver(self.root, [self.line], 'stranger')

        self.root.refresh_from_db()
        self.assertEqual(self.root.status, 'UNKNOWN')
        child = Position.objects.get(key=self.child_key)
        self.assertEqual(child.status, 'UNKNOWN')
        self.assertIsNone(child.closure)
        self.assertEqual(summary['closed_children'], 0)
        self.assertFalse(Position.objects.exclude(status='UNKNOWN')
                         .exclude(closure='TERMINAL').exists())
        claim = PendingClaim.objects.get()
        self.assertEqual(claim.position_key, self.child_key)
        self.assertEqual(claim.parent_key, self.root.key)
        self.assertEqual(claim.account, 'stranger')
        self.assertEqual(claim.task_id, task.pk)
        self.assertEqual(claim.engine_sha, SHA)
        self.assertEqual(claim.kind, 'MATE')
        self.assertEqual(claim.winner, 'WHITE')
        self.assertEqual(claim.pv, ' '.join(COOPERATIVE[1:]))
        self.assertIsNone(claim.resolved)
        # The search itself still counts: the position keeps its numbers.
        self.assertEqual(self.root.visits, 1)

    def test_a_trusted_account_closes_as_before(self, _p):
        TrustedAccount.objects.create(username='veteran')

        self._deliver(self.root, [self.line], 'veteran')

        child = Position.objects.get(key=self.child_key)
        self.assertEqual((child.status, child.closure, child.proof),
                         ('WHITE_WIN', 'MATE_PV', 'ENGINE'))
        self.root.refresh_from_db()
        self.assertEqual(self.root.status, 'WHITE_WIN')
        self.assertFalse(PendingClaim.objects.exists())

    def test_every_closure_records_task_account_and_engine(self, _p):
        TrustedAccount.objects.create(username='veteran')

        task, _summary = self._deliver(self.root, [self.line], 'veteran')

        closed = set(Position.objects.exclude(status='UNKNOWN')
                     .exclude(closure='TERMINAL')
                     .values_list('key', flat=True))
        # The child, the links of its materialised line and the start
        # position closed by minimax.
        self.assertIn(self.child_key, closed)
        self.assertIn(self.root.key, closed)
        rows = ClosureProvenance.objects.in_bulk(list(closed))
        self.assertEqual(set(rows), closed)
        for row in rows.values():
            self.assertEqual((row.task_id, row.account, row.engine_sha,
                              row.source), (task.pk, 'veteran', SHA, 'AUTO'))
        self.assertEqual(rows[self.root.key].closure, 'MINIMAX')
        self.assertEqual(rows[self.child_key].proof, 'ENGINE')

    def test_a_pending_claim_resolves_when_the_position_closes(self, _p):
        self._deliver(self.root, [self.line], 'stranger')
        TrustedAccount.objects.create(username='veteran')

        self._deliver(self.root, [self.line], 'veteran', generation=1)

        claim = PendingClaim.objects.get()
        self.assertIsNotNone(claim.resolved)
        self.assertEqual(claim.resolution, 'CONFIRMED')

    def test_the_same_delivery_twice_is_one_claim(self, _p):
        self._deliver(self.root, [self.line], 'stranger')
        self._deliver(self.root, [self.line], 'stranger', generation=1)

        self.assertEqual(PendingClaim.objects.count(), 1)

    @override_settings(ATOMICDB_CLOSURE_TRUST_GATE=False)
    def test_the_gate_can_be_switched_off(self, _p):
        self._deliver(self.root, [self.line], 'stranger')

        self.root.refresh_from_db()
        self.assertEqual(self.root.status, 'WHITE_WIN')

    def test_a_contradicted_claim_says_so(self, _p):
        self._deliver(self.root, [self.line], 'stranger')
        child = Position.objects.get(key=self.child_key)
        child.status, child.closure = 'DRAW', 'TB'
        child.save()

        ingest._emit_closure_events(child)

        self.assertEqual(PendingClaim.objects.get().resolution,
                         'CONTRADICTED')


class ProvenClosuresTests(_Jobs, TestCase):

    def test_a_proven_mate_closes_for_any_account(self):
        parent = ingest.get_or_create_position(FORCED_MATE_FEN)
        line = {'move': FORCED_MATE_PV[0], 'eval_cp': 9997, 'mate': 2,
                'pv': list(FORCED_MATE_PV)}

        self._deliver(parent, [line], 'stranger')

        child = Edge.objects.get(parent=parent,
                                 move_uci=FORCED_MATE_PV[0]).child
        self.assertEqual((child.status, child.closure, child.proof),
                         ('WHITE_WIN', 'MATE_PV', 'ANDOR'))
        self.assertFalse(PendingClaim.objects.exists())
        self.assertEqual(ClosureProvenance.objects.get(key=child.key).account,
                         'stranger')

    def test_a_refuted_line_resolves_its_claim(self):
        claim = PendingClaim.objects.create(
            position_key='k' * 64, parent_key='p' * 64, account='stranger',
            kind='MATE', winner='WHITE', pv='a b')

        ingest._refute_pending_claims('k' * 64, ['a', 'b'])

        claim.refresh_from_db()
        self.assertEqual(claim.resolution, 'REFUTED')


class _Tables:
    """The server's tables, answering for one position only."""

    def _tables(self, fen, wdl):
        wanted = logic.canonical_fen(fen)

        def probe(asked):
            return wdl if logic.canonical_fen(asked) == wanted else None
        return mock.patch('atomicdb.ingest.server_tables_wdl',
                          side_effect=probe)


class TablebaseLineTests(_Tables, TestCase):

    def setUp(self):
        self.parent = ingest.get_or_create_position(TB_PARENT)
        self.end_fen = _after(TB_PARENT, TB_LINE)
        self.line = {'move': TB_LINE[0], 'eval_cp': 9500, 'mate': None,
                     'pv': list(TB_LINE)}
        self.child_key = logic.key_of(_after(TB_PARENT, TB_LINE[:1]))

    def test_a_line_into_a_won_ending_closes_like_a_mate_line(self):
        # The end has Black to move, so White's win is a loss for the mover.
        with self._tables(self.end_fen, -2):
            ingest.ingest_analysis(self.parent.key, [self.line], 1_000_000)

        child = Position.objects.get(key=self.child_key)
        self.assertEqual((child.status, child.closure, child.proof),
                         ('WHITE_WIN', 'MATE_PV', 'TB_LINE'))
        self.assertIsNone(child.mate_in)
        self.assertEqual(child.won_line, ' '.join(TB_LINE[1:]))
        # Rxa5 resets the clock, Kf7 spends one ply.
        self.assertEqual(child.clock_slack, 99)
        end = Position.objects.get(key=logic.key_of(self.end_fen))
        self.assertEqual((end.status, end.closure, end.clock_slack),
                         ('WHITE_WIN', 'TB', 0))
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.status, 'WHITE_WIN')

    def test_a_wrong_sign_claim_is_refused(self):
        with self._tables(self.end_fen, 2):     # Black, to move, wins
            summary = ingest.ingest_analysis(self.parent.key, [self.line],
                                             1_000_000)

        self.assertEqual(summary['closed_children'], 0)
        child = Position.objects.get(key=self.child_key)
        self.assertEqual(child.status, 'UNKNOWN')
        event = DBEvent.objects.get(kind='TB_LINE_REJECTED')
        self.assertEqual(event.payload['reason'], 'wdl-contradicts-claim')

    def test_a_drawn_ending_is_refused(self):
        with self._tables(self.end_fen, 0):
            ingest.ingest_analysis(self.parent.key, [self.line], 1_000_000)

        self.assertEqual(Position.objects.get(key=self.child_key).status,
                         'UNKNOWN')

    def test_an_untrusted_account_keeps_it_as_a_pending_claim(self):
        with self._tables(self.end_fen, -2):
            ingest.ingest_analysis(self.parent.key, [self.line], 1_000_000,
                                   trusted=False)

        self.assertEqual(Position.objects.get(key=self.child_key).status,
                         'UNKNOWN')
        claim = PendingClaim.objects.get()
        self.assertEqual((claim.kind, claim.winner), ('TB', 'WHITE'))

    def test_a_score_short_of_a_decided_result_claims_nothing(self):
        line = dict(self.line, eval_cp=350)
        with self._tables(self.end_fen, -2):
            ingest.ingest_analysis(self.parent.key, [line], 1_000_000)

        self.assertEqual(Position.objects.get(key=self.child_key).status,
                         'UNKNOWN')
        self.assertFalse(DBEvent.objects.filter(
            kind='TB_LINE_REJECTED').exists())

    def test_a_quiet_move_into_the_ending_is_refused(self):
        parent = ingest.get_or_create_position(QUIET_PARENT)
        end_fen = _after(QUIET_PARENT, QUIET_LINE)
        line = {'move': QUIET_LINE[0], 'eval_cp': 9500, 'mate': None,
                'pv': list(QUIET_LINE)}

        with self._tables(end_fen, -2):
            ingest.ingest_analysis(parent.key, [line], 1_000_000)

        child_key = logic.key_of(_after(QUIET_PARENT, QUIET_LINE[:1]))
        self.assertEqual(Position.objects.get(key=child_key).status,
                         'UNKNOWN')
        event = DBEvent.objects.get(kind='TB_LINE_REJECTED')
        self.assertEqual(event.payload['reason'], 'fifty-move-margin')

    def test_the_line_names_no_mate_distance_upwards(self):
        with self._tables(self.end_fen, -2):
            ingest.ingest_analysis(self.parent.key, [self.line], 1_000_000)

        self.parent.refresh_from_db()
        self.assertEqual(self.parent.closure, 'MINIMAX')
        self.assertIsNone(self.parent.mate_in)


class TrustCommandTests(TestCase):

    def test_add_list_remove(self):
        out = StringIO()
        call_command('atomicdb_trust', 'add', 'Torom', 'BManBJ',
                     '--note', 'history', stdout=out)
        self.assertEqual(set(TrustedAccount.objects.values_list(
            'username', flat=True)), {'Torom', 'BManBJ'})
        self.assertTrue(ingest.account_trusted('Torom'))
        self.assertFalse(ingest.account_trusted('torom'))
        self.assertFalse(ingest.account_trusted(''))

        PendingClaim.objects.create(position_key='k' * 64,
                                    parent_key='p' * 64, account='x',
                                    kind='MATE', winner='WHITE', pv='a')
        out = StringIO()
        call_command('atomicdb_trust', 'list', stdout=out)
        self.assertIn('trusted accounts: 2', out.getvalue())
        self.assertIn('pending claims: 1 open', out.getvalue())
        out = StringIO()
        call_command('atomicdb_trust', 'pending', stdout=out)
        self.assertIn('x', out.getvalue())

        call_command('atomicdb_trust', 'remove', 'Torom', stdout=StringIO())
        self.assertFalse(ingest.account_trusted('Torom'))
