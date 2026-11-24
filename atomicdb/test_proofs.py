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


class MovegenMemoTests(SimpleTestCase):
    """The proof asks pyffish each question once (18-nov ingest profile).

    Every pyffish call pays for building the variant's attack tables, so the
    number of calls IS the cost of a proof.  The memo must change that number
    and nothing else: same verdict, same reversible run, same nodes counted
    against the budget.
    """

    WRAPPED = ('get_fen', 'legal_moves', 'is_immediate_game_end',
               'game_result')

    def _recorded(self, run):
        calls = []
        patches = []
        for name in self.WRAPPED:
            real = getattr(logic.pf, name)

            def spy(*args, _name=name, _real=real):
                calls.append((_name, repr(args)))
                return _real(*args)
            patches.append(patch.object(logic.pf, name, side_effect=spy))
        for p in patches:
            p.start()
        try:
            result = run()
        finally:
            for p in patches:
                p.stop()
        return result, calls

    def test_no_question_is_asked_twice_in_a_proof(self):
        logic.clear_caches()
        # No hint and a spare ply: the search wanders through move orders
        # that transpose, and still asks every question only once.  The
        # expected value is what the code before the memo answered.
        result, calls = self._recorded(lambda: logic.prove_forced_mate(
            FORCED_MATE_FEN, True, 4, budget_positions=3_000,
            return_run=True))

        self.assertEqual(result, ('PROVEN', 3))
        self.assertGreater(len(calls), 0)
        self.assertEqual(len(calls), len(set(calls)))

    def test_a_warm_proof_asks_nothing_and_answers_the_same(self):
        logic.clear_caches()
        cold = logic.prove_forced_mate(
            FORCED_MATE_FEN, True, 3, budget_positions=3_000,
            return_run=True)

        warm, calls = self._recorded(lambda: logic.prove_forced_mate(
            FORCED_MATE_FEN, True, 3, budget_positions=3_000,
            return_run=True))

        self.assertEqual(warm, cold)
        self.assertEqual(calls, [])

    def test_the_budget_still_counts_every_visited_node(self):
        # Warm caches must not make a node free: the exact budget at which
        # the hinted proof stops fitting is the same cold and warm.
        for warm in (False, True):
            if not warm:
                logic.clear_caches()
            self.assertEqual(logic.prove_forced_mate(
                FORCED_MATE_FEN, True, 3, budget_positions=4,
                hint_pv=FORCED_MATE_PV), 'PROVEN')
            self.assertEqual(logic.prove_forced_mate(
                FORCED_MATE_FEN, True, 3, budget_positions=3,
                hint_pv=FORCED_MATE_PV), 'INCONCLUSIVE')

    # Real mate lines from the production ingest (18-nov), with the result
    # and the EXACT node budget the code before the memo needed: PROVEN at
    # that budget and INCONCLUSIVE one node below.  The fixed budget pins the
    # node count, so a memo that skipped, reordered or double-counted a node
    # would move the boundary.
    REAL_LINES = (
        # (fen, winner_is_white, max_plies, hint, budget, result)
        ('5rk1/3b4/p4n2/1p3Ppp/1PN1p1PP/3P4/PBr5/R3KB1R b KQ - 0 1', False,
         7, ['c2e2', 'e1d1', 'e2e1', 'd1c2', 'e4d3'], 84, ('PROVEN', 4)),
        ('1r2k3/1P6/3p4/p2P2Nq/P1p1p3/B1P1PP2/8/1R4K1 b - - 0 1', False,
         5, ['h5h1', 'g1f2', 'e4f3'], 4, ('PROVEN', 2)),
        ('r1bqr2k/pp1pB3/n3p3/4Pp1p/3P4/2P2P2/P2Q4/R3KB1R w KQ - 0 1', True,
         5, ['d2h6', 'h8g8', 'h6g7'], 4, ('PROVEN', 3)),
    )

    def test_real_lines_keep_their_verdict_run_and_node_count(self):
        for fen, white, plies, hint, budget, result in self.REAL_LINES:
            with self.subTest(fen=fen):
                logic.clear_caches()
                self.assertEqual(logic.prove_forced_mate(
                    fen, white, plies, budget_positions=budget,
                    hint_pv=hint, return_run=True), result)
                self.assertEqual(logic.prove_forced_mate(
                    fen, white, plies, budget_positions=budget - 1,
                    hint_pv=hint, return_run=True), ('INCONCLUSIVE', None))

    def test_is_zeroing_reuses_the_position_apply_move_built(self):
        logic.clear_caches()
        fen = logic.canonical_fen(FORCED_MATE_FEN)
        logic.apply_move(fen, 'b1g6')

        zeroing, calls = self._recorded(
            lambda: logic.is_zeroing(fen, 'b1g6'))

        self.assertFalse(zeroing)
        self.assertEqual(calls, [])

    def test_legal_moves_hands_out_a_fresh_list(self):
        fen = logic.canonical_fen(FORCED_MATE_FEN)
        moves = logic.legal_moves(fen)
        moves.reverse()
        moves.append('junk')

        self.assertEqual(logic.legal_moves(fen),
                         list(logic.pf.legal_moves(logic.VARIANT, fen, [])))


class MovegenShortcutTests(SimpleTestCase):
    """The immediate-end flag is answered from the board when both kings are
    on it (23-nov ingest profile), and the e.p. probe shares the position
    memo.  Both must give exactly the movegen's answer and only save calls.
    """

    # Real positions plus a few edge cases: a king already blown up on either
    # side, a real e.p. right, a phantom one, castling rights.
    FENS = (
        FORCED_MATE_FEN,
        COOPERATIVE_FEN,
        *(fen for fen, *_ in MovegenMemoTests.REAL_LINES),
        'rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 1',
        'rnbqkbnr/pppp1ppp/8/8/3Pp3/8/PPP1PPPP/RNBQKBNR b KQkq d3 0 1',
        'r3k2r/pppq1ppp/2n1bn2/3pp3/3PP3/2N1BN2/PPPQ1PPP/R3K2R w KQkq - 0 1',
        '8/8/8/8/8/8/8/K7 b - - 0 1',
        'k7/8/8/8/8/8/8/8 w - - 0 1',
        'k7/1p6/8/8/8/8/8/1R5K w - - 0 1',
        'rnbq1bnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQ - 0 1',
    )

    def _positions(self):
        """Every FEN above, canonical, and every child of each."""
        seen = []
        for fen in self.FENS:
            fen = logic.canonical_fen(fen)
            seen.append(fen)
            for uci in logic.pf.legal_moves(logic.VARIANT, fen, []):
                seen.append(logic.canonical_fen(
                    logic.pf.get_fen(logic.VARIANT, fen, [uci])))
        return seen

    def test_immediate_end_matches_the_movegen_everywhere(self):
        positions = self._positions()
        missing_king = 0
        for fen in positions:
            with self.subTest(fen=fen):
                truth = bool(logic.pf.is_immediate_game_end(
                    logic.VARIANT, fen, [])[0])
                self.assertEqual(logic._immediate_end(fen), truth)
                if logic._both_kings_on_board(fen):
                    self.assertFalse(truth)
                else:
                    missing_king += 1
                    self.assertTrue(truth)
        # The sample really contains blown-up kings, not just the easy case.
        self.assertGreaterEqual(missing_king, 3)

    def test_terminal_status_matches_the_movegen_everywhere(self):
        def reference(fen):
            moves = logic.pf.legal_moves(logic.VARIANT, fen, [])
            immediate = logic.pf.is_immediate_game_end(
                logic.VARIANT, fen, [])[0]
            if moves and not immediate:
                return None
            res = logic.pf.game_result(logic.VARIANT, fen, [])
            white = fen.split()[1] == 'w'
            if res > 0:
                return ('WHITE_WIN' if white else 'BLACK_WIN', 'terminal')
            if res < 0:
                return ('BLACK_WIN' if white else 'WHITE_WIN', 'terminal')
            return ('DRAW', 'terminal')

        logic.clear_caches()
        for fen in self._positions():
            with self.subTest(fen=fen):
                self.assertEqual(logic.terminal_status(fen), reference(fen))

    def test_both_kings_ask_pyffish_nothing(self):
        logic.clear_caches()
        fen = logic.canonical_fen(FORCED_MATE_FEN)
        result, calls = MovegenMemoTests._recorded(
            MovegenMemoTests(), lambda: logic._immediate_end(fen))
        self.assertFalse(result)
        self.assertEqual(calls, [])

    def test_a_proof_never_asks_the_flag_with_both_kings(self):
        logic.clear_caches()
        result, calls = MovegenMemoTests._recorded(
            MovegenMemoTests(), lambda: logic.prove_forced_mate(
                FORCED_MATE_FEN, True, 4, budget_positions=3_000,
                return_run=True))
        self.assertEqual(result, ('PROVEN', 3))
        for name, args in calls:
            if name == 'is_immediate_game_end':
                fen = eval(args)[1]
                self.assertFalse(logic._both_kings_on_board(fen), fen)

    def test_en_passant_probe_answers_the_next_question(self):
        for fen, kept in (
                ('rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6'
                 ' 0 1', True),
                ('rnbqkbnr/pppp1ppp/8/8/3Pp3/8/PPP1PPPP/RNBQKBNR b KQkq d3'
                 ' 0 1', None)):
            with self.subTest(fen=fen):
                logic.clear_caches()
                canonical = logic.canonical_fen(fen)
                if kept is not None:
                    self.assertEqual(canonical.split()[3] != '-', kept)
                moves, calls = MovegenMemoTests._recorded(
                    MovegenMemoTests(), lambda: logic.legal_moves(canonical))
                self.assertEqual(calls, [])
                self.assertEqual(moves, list(logic.pf.legal_moves(
                    logic.VARIANT, canonical, [])))


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
