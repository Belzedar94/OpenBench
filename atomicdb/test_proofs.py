from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from . import ingest, logic
from .models import AnalysisTask, DBEvent, Edge, Position
from .testing import TestCase


# A genuine atomic mate in two (three plies).  White has exactly the forcing
# move 1.Qg6; Black's only reply is 1...Kh4 and 2.Qg4 ends the game.
FORCED_MATE_FEN = '4p3/8/8/7k/n7/Kp2n3/3p4/1Q6 w - - 0 1'
FORCED_MATE_PV = ['b1g6', 'h5h4', 'g6g4']

# The displayed line reaches a legal white win, but Black has other replies
# at the root.  It is therefore a cooperative witness, not a forced mate.
COOPERATIVE_FEN = '4p1K1/8/4Q1k1/2p5/8/8/n7/8 b - - 0 1'
COOPERATIVE_PV = ['g6f7', 'e6e8']


class ForcedMateProofTests(SimpleTestCase):

    def test_proven_real_mate_in_two_uses_hint_first(self):
        self.assertTrue(logic.verify_mate_pv(
            FORCED_MATE_FEN, FORCED_MATE_PV, True))
        # Four visited positions are enough only because the hint is searched
        # first; it remains an ordering hint, not trusted evidence.
        self.assertEqual(
            logic.prove_forced_mate(
                FORCED_MATE_FEN, True, 3, budget_positions=4,
                hint_pv=FORCED_MATE_PV),
            'PROVEN',
        )
        self.assertEqual(
            logic.prove_forced_mate(
                FORCED_MATE_FEN, True, 3, budget_positions=4),
            'INCONCLUSIVE',
        )

    def test_inconclusive_when_position_budget_is_exhausted(self):
        self.assertEqual(
            logic.prove_forced_mate(
                FORCED_MATE_FEN, True, 3, budget_positions=3,
                hint_pv=FORCED_MATE_PV),
            'INCONCLUSIVE',
        )

    def test_inconclusive_when_wall_deadline_has_expired(self):
        self.assertEqual(
            logic.prove_forced_mate(
                FORCED_MATE_FEN, True, 3, budget_positions=200_000,
                hint_pv=FORCED_MATE_PV, deadline=0),
            'INCONCLUSIVE',
        )

    @patch('atomicdb.ingest.logic.prove_forced_mate')
    @patch('atomicdb.ingest.logic.verify_mate_pv', return_value=True)
    def test_online_proof_deadline_is_shared_and_fail_safe(
            self, verify, prove):
        prepared = ingest.prepare_mate_proofs(FORCED_MATE_FEN, [{
            'move': FORCED_MATE_PV[0],
            'pv': FORCED_MATE_PV,
            'mate': 2,
        }], deadline_seconds=0)

        self.assertEqual(prepared[0][2], 'INCONCLUSIVE')
        prove.assert_not_called()

    def test_no_mate_for_cooperative_but_non_forced_line(self):
        self.assertTrue(logic.verify_mate_pv(
            COOPERATIVE_FEN, COOPERATIVE_PV, True))
        self.assertEqual(
            logic.prove_forced_mate(
                COOPERATIVE_FEN, True, 2, budget_positions=50,
                hint_pv=COOPERATIVE_PV),
            'NO_MATE',
        )


class VerifyMatesCommandTests(TestCase):

    def _mate_position(self, fen=FORCED_MATE_FEN,
                       pv=FORCED_MATE_PV):
        fen = logic.canonical_fen(fen)
        return Position.objects.create(
            key=logic.key_of(fen), fen=fen, status='WHITE_WIN',
            closure='MATE_PV', won_line=' '.join(pv),
        )

    def test_command_classifies_andor_and_is_resumable(self):
        pos = self._mate_position()
        out = StringIO()
        call_command('verify_mates', budget_positions=4, stdout=out)
        pos.refresh_from_db()
        self.assertEqual(pos.proof, 'ANDOR')
        self.assertIn('ANDOR=1', out.getvalue())

        second = StringIO()
        call_command('verify_mates', budget_positions=4, stdout=second)
        self.assertIn('ANDOR=0', second.getvalue())

    def test_command_maps_budget_exhaustion_to_engine(self):
        pos = self._mate_position()
        call_command('verify_mates', budget_positions=1,
                     stdout=StringIO())
        pos.refresh_from_db()
        self.assertEqual(pos.proof, 'ENGINE')

    def test_missing_historical_witness_is_left_unclassified(self):
        pos = self._mate_position()
        pos.won_line = None
        pos.save(update_fields=['won_line'])
        out, err = StringIO(), StringIO()

        call_command('verify_mates', stdout=out, stderr=err)

        pos.refresh_from_db()
        self.assertIsNone(pos.proof)
        self.assertIn('MISSING=1', out.getvalue())
        self.assertIn('UNCLASSIFIED=1', out.getvalue())
        self.assertIn(pos.key, err.getvalue())

    @patch('atomicdb.management.commands.verify_mates.logic.prove_forced_mate',
           return_value='INCONCLUSIVE')
    def test_command_processes_shortest_witness_first(self, prove):
        long = self._mate_position(FORCED_MATE_FEN, FORCED_MATE_PV)
        short = self._mate_position(COOPERATIVE_FEN, ['g6f7'])

        call_command('verify_mates', limit=1, batch_size=1,
                     stdout=StringIO())

        long.refresh_from_db()
        short.refresh_from_db()
        self.assertIsNone(long.proof)
        self.assertEqual(short.proof, 'ENGINE')
        self.assertEqual(prove.call_args.args[0], short.fen)

    @patch('atomicdb.management.commands.verify_mates.logic.prove_forced_mate')
    def test_command_error_does_not_block_later_witnesses(self, prove):
        short = self._mate_position(COOPERATIVE_FEN, ['g6f7'])
        long = self._mate_position(FORCED_MATE_FEN, FORCED_MATE_PV)
        prove.side_effect = [RuntimeError('synthetic failure'), 'INCONCLUSIVE']

        call_command('verify_mates', batch_size=1, stdout=StringIO(),
                     stderr=StringIO())

        short.refresh_from_db()
        long.refresh_from_db()
        self.assertIsNone(short.proof)
        self.assertEqual(long.proof, 'ENGINE')
        self.assertEqual(prove.call_count, 2)

    @patch('atomicdb.management.commands.verify_mates.logic.prove_forced_mate')
    def test_command_cas_skips_a_changed_witness(self, prove):
        pos = self._mate_position()

        def change_witness(*args, **kwargs):
            current = Position.objects.get(key=pos.key)
            current.won_line = current.won_line + ' a1a2'
            current.save(update_fields=['won_line'])
            return 'INCONCLUSIVE'

        prove.side_effect = change_witness
        out = StringIO()
        call_command('verify_mates', stdout=out)

        pos.refresh_from_db()
        self.assertIsNone(pos.proof)
        self.assertIn('SKIPPED=1', out.getvalue())

    @patch('atomicdb.management.commands.verify_mates.logic.prove_forced_mate',
           return_value='NO_MATE')
    def test_disputed_reopens_the_position_and_keeps_its_witness(self, prove):
        """P0c: a refuted witness no longer keeps its WIN.

        The historic behaviour was to record the dispute and leave the closure
        standing, which is exactly what let 637 uncertified mates poison their
        ancestors through MINIMAX.  ``atomicdb/test_revocation.py`` owns the
        cascade; this pins the command's own contract.
        """
        pos = self._mate_position(COOPERATIVE_FEN, COOPERATIVE_PV)
        out = StringIO()
        call_command('verify_mates', stdout=out)
        pos.refresh_from_db()

        self.assertEqual(pos.proof, 'DISPUTED')
        self.assertEqual(pos.status, 'UNKNOWN')
        self.assertIsNone(pos.closure)
        self.assertEqual(pos.won_line, ' '.join(COOPERATIVE_PV))
        self.assertIn(pos.key, out.getvalue())
        self.assertTrue(DBEvent.objects.filter(
            kind='MATE_PROOF_DISPUTED', payload__key=pos.key).exists())
        self.assertTrue(DBEvent.objects.filter(
            kind='CLOSURE_REVOKED', payload__key=pos.key).exists())
        prove.assert_called_once()


class MinimaxProofInheritanceTests(TestCase):

    def test_winning_witness_inherits_andor_then_engine(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.get(parent=root, move_uci='e2e4').child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ANDOR'
        child.save(update_fields=['status', 'closure', 'proof'])

        ingest.backup_cascade([child.key])
        root.refresh_from_db()
        self.assertEqual((root.status, root.closure, root.proof),
                         ('WHITE_WIN', 'MINIMAX', 'ANDOR'))

        child.proof = 'ENGINE'
        child.save(update_fields=['proof'])
        ingest.backup_cascade([child.key])
        root.refresh_from_db()
        self.assertEqual(root.proof, 'ENGINE')


class NewDisputedMateTests(TestCase):

    @patch('atomicdb.ingest.logic.prove_forced_mate',
           return_value=('NO_MATE', None))
    @patch('atomicdb.ingest.logic.verify_mate_pv', return_value=True)
    def test_new_dispute_does_not_close_and_queues_max_budget(self,
                                                              verify, prove):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.ingest_analysis(parent.key, [{
            'move': 'e2e4', 'eval_cp': 9999, 'mate': 1,
            'pv': ['e2e4'],
        }], nodes_budget=1_000)
        child = Edge.objects.get(parent=parent, move_uci='e2e4').child
        child.refresh_from_db()
        self.assertEqual(child.status, 'UNKNOWN')
        self.assertIsNone(child.closure)
        self.assertEqual(child.proof, 'DISPUTED')
        task = AnalysisTask.objects.get(position=child, state='PENDING')
        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[-1])
        self.assertTrue(DBEvent.objects.filter(
            kind='MATE_PROOF_DISPUTED', payload__key=child.key).exists())
