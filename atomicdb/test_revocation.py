"""Revocation of exact closures that lost their evidence (P0c).

The system could always CLOSE a position; until now it could not un-close one.
``verify_mates`` marked a refuted witness ``DISPUTED`` and left its ``WIN``
standing, so every ancestor that inherited it through MINIMAX stayed poisoned.
These tests pin the withdrawal: what falls, what survives, what stops the
cascade, and what goes back into the queue.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from . import ingest, logic
from .management.commands import recertify_mates as recertify
from .models import AnalysisTask, DBEvent, Edge, Position
from .testing import TestCase

FORCED_MATE_FEN = '4p3/8/8/7k/n7/Kp2n3/3p4/1Q6 w - - 0 1'
FORCED_MATE_PV = ['b1g6', 'h5h4', 'g6g4']
COOPERATIVE_FEN = '4p1K1/8/4Q1k1/2p5/8/8/n7/8 b - - 0 1'
COOPERATIVE_PV = ['g6f7', 'e6e8']


def _position(fen, **fields):
    fen = logic.canonical_fen(fen)
    return Position.objects.create(key=logic.key_of(fen), fen=fen, **fields)


class RevokeClosureTests(TestCase):
    """A three-level chain: MINIMAX over MINIMAX over a false MATE_PV."""

    def _chain(self):
        """grand(w) -> parent(b) -> leaf(w), all closed WHITE_WIN.

        Synthetic FENs, because what is under test is the graph algebra, not
        the rules: ``expanded`` is set by hand so ``backup_status`` treats the
        stored edges as the complete legal list.
        """
        leaf = _position('8/8/8/8/8/8/1k6/K6Q w - - 0 1',
                         status='WHITE_WIN', closure='MATE_PV',
                         proof='ENGINE', won_line='h1h8', mate_in=1)
        parent = _position('8/8/8/8/8/8/1k6/K6Q b - - 0 1',
                           status='WHITE_WIN', closure='MINIMAX',
                           proof='ENGINE', mate_in=2, expanded=True,
                           best_move='b2b3')
        grand = _position('8/8/8/8/8/8/1k6/K5Q1 w - - 0 1',
                          status='WHITE_WIN', closure='MINIMAX',
                          proof='ENGINE', mate_in=3, expanded=True,
                          best_move='g1h1')
        Edge.objects.create(parent=parent, move_uci='b2b3', child=leaf)
        Edge.objects.create(parent=grand, move_uci='g1h1', child=parent)
        return grand, parent, leaf

    def test_three_level_chain_collapses_entirely(self):
        grand, parent, leaf = self._chain()

        outcome = ingest.revoke_closure(leaf.key, reason='test')

        self.assertEqual(set(outcome['revoked']),
                         {leaf.key, parent.key, grand.key})
        for node in (grand, parent, leaf):
            node.refresh_from_db()
            self.assertEqual(node.status, 'UNKNOWN')
            self.assertIsNone(node.closure)
            self.assertIsNone(node.mate_in)
        # The seed keeps no refuted-witness trace unless the caller asks.
        self.assertIsNone(leaf.proof)
        self.assertIsNone(leaf.won_line)

    def test_seed_can_keep_its_disputed_trace(self):
        _, _, leaf = self._chain()

        ingest.revoke_closure(leaf.key, reason='test', mark_disputed=True)

        leaf.refresh_from_db()
        self.assertEqual(leaf.status, 'UNKNOWN')
        self.assertEqual(leaf.proof, 'DISPUTED')
        self.assertEqual(leaf.won_line, 'h1h8')

    def test_surviving_ancestor_is_not_revoked_and_recomputes_its_dtm(self):
        """A second winning child keeps the parent closed; only DTM moves."""
        grand, parent, leaf = self._chain()
        spare = _position('8/8/8/8/8/8/2k5/K6Q w - - 0 1',
                          status='WHITE_WIN', closure='MATE_PV',
                          proof='ANDOR', won_line='h1h7 x', mate_in=5)
        Edge.objects.create(parent=parent, move_uci='b2c2', child=spare)

        outcome = ingest.revoke_closure(leaf.key, reason='test')

        # ``parent`` is a black-to-move AND node: it is a WHITE win only if
        # EVERY reply loses, so losing one exact child does open it.  What the
        # test pins is that ``spare`` itself is untouched.
        self.assertIn(leaf.key, outcome['revoked'])
        spare.refresh_from_db()
        self.assertEqual((spare.status, spare.closure, spare.proof),
                         ('WHITE_WIN', 'MATE_PV', 'ANDOR'))

    def test_or_node_survives_when_another_winning_edge_remains(self):
        """White to move needs ONE winning reply; a spare edge holds it up."""
        good = _position('8/8/8/8/8/8/1k6/K6Q w - - 0 1',
                         status='WHITE_WIN', closure='MATE_PV',
                         proof='ANDOR', won_line='h1h8', mate_in=1)
        doubtful = _position('8/8/8/8/8/8/1k6/K5Q1 w - - 0 1',
                             status='WHITE_WIN', closure='MATE_PV',
                             proof='ENGINE', won_line='g1g8', mate_in=7)
        parent = _position('8/8/8/8/8/8/1k6/K4Q2 w - - 0 1',
                           status='WHITE_WIN', closure='MINIMAX',
                           proof='ENGINE', mate_in=8, expanded=True,
                           best_move='f1g1')
        Edge.objects.create(parent=parent, move_uci='f1h1', child=good)
        Edge.objects.create(parent=parent, move_uci='f1g1', child=doubtful)

        outcome = ingest.revoke_closure(doubtful.key, reason='test')

        self.assertEqual(outcome['revoked'], [doubtful.key])
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'WHITE_WIN')
        # The surviving witness is the verified one, and the DTM follows it.
        self.assertEqual(parent.best_move, 'f1h1')
        self.assertEqual(parent.mate_in, 2)
        self.assertEqual(parent.proof, 'ANDOR')

    def test_intermediate_andor_cuts_the_cascade(self):
        leaf = _position('8/8/8/8/8/8/1k6/K6Q w - - 0 1',
                         status='WHITE_WIN', closure='MATE_PV',
                         proof='ENGINE', won_line='h1h8', mate_in=1)
        proven = _position('8/8/8/8/8/8/1k6/K6Q b - - 0 1',
                           status='WHITE_WIN', closure='MATE_PV',
                           proof='ANDOR', won_line='b2b3 h1h8', mate_in=2,
                           expanded=True)
        above = _position('8/8/8/8/8/8/1k6/K5Q1 w - - 0 1',
                          status='WHITE_WIN', closure='MINIMAX',
                          proof='ANDOR', mate_in=3, expanded=True)
        Edge.objects.create(parent=proven, move_uci='b2b3', child=leaf)
        Edge.objects.create(parent=above, move_uci='g1h1', child=proven)

        outcome = ingest.revoke_closure(leaf.key, reason='test')

        self.assertEqual(outcome['revoked'], [leaf.key])
        proven.refresh_from_db()
        above.refresh_from_db()
        self.assertEqual(proven.status, 'WHITE_WIN')
        self.assertEqual(proven.proof, 'ANDOR')
        self.assertEqual(above.status, 'WHITE_WIN')

    def test_terminal_closure_is_never_revoked(self):
        terminal = _position('8/8/8/8/8/8/1k6/K6Q w - - 0 1',
                             status='DRAW', closure='TERMINAL', mate_in=0)

        outcome = ingest.revoke_closure(terminal.key, reason='test')

        self.assertEqual(outcome['revoked'], [])
        terminal.refresh_from_db()
        self.assertEqual((terminal.status, terminal.closure),
                         ('DRAW', 'TERMINAL'))

    def test_tombstones_are_revived_for_reopened_nodes_and_children(self):
        _, parent, leaf = self._chain()
        below = _position('8/8/8/8/8/8/3k4/K6Q w - - 0 1',
                          priority=ingest.DEAD)
        Edge.objects.create(parent=leaf, move_uci='h1h2', child=below)
        Position.objects.filter(key__in=[leaf.key, parent.key]).update(
            priority=ingest.DEAD)

        outcome = ingest.revoke_closure(leaf.key, reason='test')

        self.assertGreaterEqual(outcome['revived'], 3)
        for node in (leaf, parent, below):
            node.refresh_from_db()
            self.assertEqual(node.priority, 0.0)

    def test_reopened_seed_is_requeued_at_the_maximum_budget(self):
        _, _, leaf = self._chain()

        outcome = ingest.revoke_closure(leaf.key, reason='test')

        task = AnalysisTask.objects.get(position=leaf, state='PENDING')
        self.assertEqual(task.id, outcome['requeued'])
        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[-1])

    def test_event_records_the_affected_chain(self):
        grand, parent, leaf = self._chain()

        ingest.revoke_closure(leaf.key, reason='mate-witness-refuted')

        event = DBEvent.objects.get(kind='CLOSURE_REVOKED')
        self.assertEqual(event.payload['key'], leaf.key)
        self.assertEqual(event.payload['reason'], 'mate-witness-refuted')
        self.assertEqual(event.payload['revoked'], 3)
        self.assertEqual(set(event.payload['chain']),
                         {leaf.key, parent.key, grand.key})

    def test_revoking_an_open_position_is_a_no_op(self):
        leaf = _position('8/8/8/8/8/8/1k6/K6Q w - - 0 1')

        outcome = ingest.revoke_closure(leaf.key, reason='test')

        self.assertEqual(outcome, {'revoked': [], 'requeued': None,
                                   'revived': 0})
        self.assertFalse(DBEvent.objects.filter(
            kind='CLOSURE_REVOKED').exists())


class VerifyMatesRevokesTests(TestCase):

    @patch('atomicdb.management.commands.verify_mates.logic.prove_forced_mate',
           return_value='NO_MATE')
    def test_disputed_now_reopens_the_position(self, prove):
        pos = _position(COOPERATIVE_FEN, status='WHITE_WIN',
                        closure='MATE_PV', won_line=' '.join(COOPERATIVE_PV))
        out = StringIO()

        call_command('verify_mates', stdout=out)

        pos.refresh_from_db()
        self.assertEqual(pos.status, 'UNKNOWN')
        self.assertIsNone(pos.closure)
        self.assertEqual(pos.proof, 'DISPUTED')
        self.assertEqual(pos.won_line, ' '.join(COOPERATIVE_PV))
        self.assertIn('closure revoked', out.getvalue())
        self.assertIn('revoked_nodes=1', out.getvalue())
        self.assertTrue(DBEvent.objects.filter(
            kind='CLOSURE_REVOKED', payload__key=pos.key).exists())
        prove.assert_called_once()


class RecertifyMatesTests(TestCase):

    def _engine_mate(self, fen=FORCED_MATE_FEN, pv=FORCED_MATE_PV):
        return _position(fen, status='WHITE_WIN', closure='MATE_PV',
                         proof='ENGINE', won_line=' '.join(pv),
                         mate_in=len(pv))

    def test_engine_closure_is_upgraded_to_andor(self):
        pos = self._engine_mate()
        out = StringIO()

        call_command('recertify_mates', stdout=out)

        pos.refresh_from_db()
        self.assertEqual(pos.proof, 'ANDOR')
        self.assertIn('ANDOR=1', out.getvalue())
        self.assertTrue(DBEvent.objects.filter(
            kind='MATE_RECERTIFIED', payload__key=pos.key).exists())

    def test_second_run_skips_what_the_first_already_spent(self):
        self._engine_mate()
        call_command('recertify_mates', stdout=StringIO())
        # An ANDOR row is no longer selected at all; make it ENGINE again to
        # prove the event-based resume, not the query filter.
        Position.objects.update(proof='ENGINE')
        out = StringIO()

        call_command('recertify_mates', stdout=out)

        self.assertIn('SKIPPED=1', out.getvalue())
        self.assertIn('ANDOR=0', out.getvalue())

    def test_force_ignores_the_resume_history(self):
        self._engine_mate()
        call_command('recertify_mates', stdout=StringIO())
        Position.objects.update(proof='ENGINE')
        out = StringIO()

        call_command('recertify_mates', force=True, stdout=out)

        self.assertIn('ANDOR=1', out.getvalue())

    def test_budget_tiers_follow_the_witness_length(self):
        self.assertEqual(recertify.budget_for_witness(20),
                         recertify.RECERTIFY_SHORT_BUDGET)
        self.assertEqual(recertify.budget_for_witness(21),
                         recertify.RECERTIFY_LONG_BUDGET)
        self.assertEqual(recertify.budget_for_witness(21, override=7), 7)

    @patch('atomicdb.management.commands.recertify_mates.logic'
           '.prove_forced_mate', return_value='NO_MATE')
    def test_refuted_witness_is_revoked_with_its_ancestors(self, prove):
        leaf = self._engine_mate(COOPERATIVE_FEN, COOPERATIVE_PV)
        parent = _position('8/8/8/8/8/8/1k6/K5Q1 w - - 0 1',
                           status='WHITE_WIN', closure='MINIMAX',
                           proof='ENGINE', mate_in=3, expanded=True)
        Edge.objects.create(parent=parent, move_uci='g1h1', child=leaf)
        out = StringIO()

        call_command('recertify_mates', stdout=out)

        leaf.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(leaf.status, 'UNKNOWN')
        self.assertEqual(leaf.proof, 'DISPUTED')
        self.assertEqual(parent.status, 'UNKNOWN')
        self.assertIn('REVOKED=1', out.getvalue())
        self.assertIn('revoked_nodes=2', out.getvalue())

    def test_unwitnessed_row_is_recovered_from_its_own_analysis(self):
        pos = _position(FORCED_MATE_FEN, status='WHITE_WIN',
                        closure='MATE_PV',
                        last_analysis=[{'move': FORCED_MATE_PV[0],
                                        'mate': 2, 'pv': FORCED_MATE_PV}])
        out = StringIO()

        call_command('recertify_mates', include_missing=True, stdout=out)

        pos.refresh_from_db()
        self.assertEqual(pos.proof, 'ANDOR')
        self.assertEqual(pos.won_line, ' '.join(FORCED_MATE_PV))
        self.assertIn('RECOVERED=1', out.getvalue())

    def test_unwitnessed_row_without_evidence_is_only_reported(self):
        pos = _position(FORCED_MATE_FEN, status='WHITE_WIN',
                        closure='MATE_PV', last_analysis=None)
        out, err = StringIO(), StringIO()

        call_command('recertify_mates', include_missing=True, stdout=out,
                     stderr=err)

        pos.refresh_from_db()
        self.assertEqual(pos.status, 'WHITE_WIN')
        self.assertIn('UNWITNESSED=1', out.getvalue())
        self.assertIn(pos.key, err.getvalue())

    def test_unwitnessed_row_is_revoked_on_request(self):
        pos = _position(FORCED_MATE_FEN, status='WHITE_WIN',
                        closure='MATE_PV', last_analysis=None)

        call_command('recertify_mates', revoke_unwitnessed=True,
                     stdout=StringIO(), stderr=StringIO())

        pos.refresh_from_db()
        self.assertEqual(pos.status, 'UNKNOWN')
        self.assertIsNone(pos.closure)

    def test_recovery_rejects_a_pv_that_no_longer_verifies(self):
        self.assertIsNone(recertify.recover_witness(
            logic.canonical_fen(FORCED_MATE_FEN), 'WHITE_WIN',
            [{'move': 'b1b2', 'mate': 2, 'pv': ['b1b2', 'h5h4']}]))
        self.assertIsNone(recertify.recover_witness(
            logic.canonical_fen(FORCED_MATE_FEN), 'BLACK_WIN',
            [{'move': FORCED_MATE_PV[0], 'mate': 2, 'pv': FORCED_MATE_PV}]))


class OnlineDisputeRevokesTests(TestCase):

    def test_refuted_witness_on_an_already_closed_child_is_withdrawn(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        child = Edge.objects.get(parent=parent, move_uci='e2e4').child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ENGINE'
        child.won_line = 'a1a2'
        child.mate_in = 1
        child.save()

        with patch('atomicdb.ingest.logic.verify_mate_pv', return_value=True), \
                patch('atomicdb.ingest.logic.prove_forced_mate',
                      return_value=('NO_MATE', None)):
            summary = ingest.ingest_analysis(parent.key, [{
                'move': 'e2e4', 'eval_cp': 9999, 'mate': 1,
                'pv': ['e2e4', 'a1a2'],
            }], nodes_budget=1_000)

        child.refresh_from_db()
        self.assertEqual(summary.get('revoked'), 1)
        self.assertEqual(child.status, 'UNKNOWN')
        self.assertEqual(child.proof, 'DISPUTED')
        self.assertTrue(DBEvent.objects.filter(
            kind='CLOSURE_REVOKED', payload__key=child.key).exists())

    def test_a_deeper_claim_than_the_search_is_left_alone(self):
        """NO_MATE within N plies says nothing about a mate in N+5."""
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        child = Edge.objects.get(parent=parent, move_uci='e2e4').child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ENGINE'
        child.mate_in = 40
        child.save()

        with patch('atomicdb.ingest.logic.verify_mate_pv', return_value=True), \
                patch('atomicdb.ingest.logic.prove_forced_mate',
                      return_value=('NO_MATE', None)):
            ingest.ingest_analysis(parent.key, [{
                'move': 'e2e4', 'eval_cp': 9999, 'mate': 1,
                'pv': ['e2e4', 'a1a2'],
            }], nodes_budget=1_000)

        child.refresh_from_db()
        self.assertEqual(child.status, 'WHITE_WIN')


class MinimaxWitnessPreferenceTests(TestCase):

    def test_verified_winning_child_is_preferred_as_the_witness(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        edges = list(Edge.objects.filter(parent=parent).order_by('id'))
        engine_edge, verified_edge = edges[0], edges[1]
        for edge, proof in ((engine_edge, 'ENGINE'), (verified_edge, 'ANDOR')):
            child = edge.child
            child.status = 'WHITE_WIN'
            child.closure = 'MATE_PV'
            child.proof = proof
            child.mate_in = 3
            child.save()

        ingest.backup_cascade([engine_edge.child.key,
                               verified_edge.child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'WHITE_WIN')
        self.assertEqual(parent.proof, 'ANDOR')
        self.assertEqual(parent.best_move, verified_edge.move_uci)
