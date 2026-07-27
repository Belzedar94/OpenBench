"""Canonicalizer v2: a phantom en-passant right is not part of the identity.

The move generator emits an en-passant square for captures that are
pseudo-legal but illegal.  In atomic there are two ways for that to happen —
the capture would explode the capturing side's own king, and the capturing
pawn is pinned — and both are reproduced here against the real move generator
rather than described.

The square sits in the key, so a phantom split one position into two rows.
The expensive half of that is not the wasted row: it is the branch repetition
guard in ``prove_forced_mate``, which compares these keys.  ``RepetitionGuard``
below is the line that used to walk straight past it.
"""

from io import StringIO

import pyffish as pf
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from . import ingest, logic, proof
from .models import (AnalysisTask, Campaign, DBEvent, Edge, IngestJob,
                     OpeningNameSuggestion, Position, ProofCampaign,
                     ProofNode, RequestLog, SolveTask)
from .testing import TestCase

# Black pawn d4, White pushes e2e4: the en-passant capture d4xe3 is generated
# but illegal because the explosion at e3 takes Black's own king with it.
OWN_KING_BEFORE = '8/8/8/8/3p4/3k4/4P3/6K1 w - - 0 1'
# Same shape with the black king on f3 instead of d3.
OWN_KING_F3_BEFORE = '8/8/8/8/3p4/5k2/4P3/6K1 w - - 0 1'
# The capturing pawn is pinned against its king on the d-file by Rd1.
PINNED_BEFORE = '3k4/8/8/8/3p4/8/4P3/3RK3 w - - 0 1'
# Nothing wrong with this one: d4xe3 is legal and the right is real.
LEGITIMATE_BEFORE = '7k/8/8/8/3p4/8/4P3/4K3 w - - 0 1'
# White's side of the same bug: h5xg6 would explode White's king on h6.
WHITE_PHANTOM_BEFORE = '1k6/6p1/7K/7P/8/8/8/8 b - - 0 1'

QUIET_FEN = '8/8/8/8/8/8/1k6/K6Q w - - 0 1'


def raw_after(before, move):
    """What the move generator emits, before any canonicalisation."""
    return pf.get_fen(logic.VARIANT, before, [move])


class PhantomIsNotIdentityTests(SimpleTestCase):

    def test_a_capture_that_explodes_its_own_king_leaves_no_right(self):
        raw = raw_after(OWN_KING_BEFORE, 'e2e4')
        self.assertEqual(raw.split()[3], 'e3')       # the generator says e3
        self.assertEqual(logic.canonical_fen(raw).split()[3], '-')

    def test_the_same_shape_from_the_other_side_of_the_square(self):
        raw = raw_after(OWN_KING_F3_BEFORE, 'e2e4')
        self.assertEqual(raw.split()[3], 'e3')
        self.assertEqual(logic.canonical_fen(raw).split()[3], '-')

    def test_a_pinned_capturing_pawn_leaves_no_right(self):
        raw = raw_after(PINNED_BEFORE, 'e2e4')
        self.assertEqual(raw.split()[3], 'e3')
        self.assertEqual(logic.canonical_fen(raw).split()[3], '-')

    def test_a_real_right_is_kept(self):
        raw = raw_after(LEGITIMATE_BEFORE, 'e2e4')
        self.assertEqual(logic.canonical_fen(raw).split()[3], 'e3')

    def test_the_white_side_phantom_is_dropped_too(self):
        raw = raw_after(WHITE_PHANTOM_BEFORE, 'g7g5')
        self.assertEqual(raw.split()[3], 'g6')
        self.assertEqual(logic.canonical_fen(raw).split()[3], '-')

    def test_no_adjacent_enemy_pawn_is_unchanged(self):
        """The generator already dropped it; nothing new happens here."""
        raw = raw_after(logic.start_fen(), 'e2e4')
        self.assertEqual(raw.split()[3], '-')
        self.assertEqual(logic.canonical_fen(raw).split()[3], '-')

    def test_a_phantom_and_its_twin_are_now_one_key(self):
        raw = raw_after(OWN_KING_BEFORE, 'e2e4')
        twin = ' '.join(raw.split()[:3] + ['-', '0', '1'])
        self.assertEqual(logic.key_of(raw), logic.key_of(twin))

    def test_a_real_right_still_distinguishes_two_keys(self):
        raw = raw_after(LEGITIMATE_BEFORE, 'e2e4')
        twin = ' '.join(raw.split()[:3] + ['-', '0', '1'])
        self.assertNotEqual(logic.key_of(raw), logic.key_of(twin))


class IdentityOfEverythingElseTests(SimpleTestCase):
    """The 3,523 legitimate e.p. rows and every other key must not move."""

    def test_positions_without_a_right_keep_their_exact_key(self):
        import hashlib
        for fen in (logic.start_fen(), QUIET_FEN,
                    'r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1',
                    '4k3/8/8/8/8/8/8/4K3 b - - 12 34'):
            canonical = ' '.join(fen.split()[:4] + ['0', '1'])
            self.assertEqual(logic.canonical_fen(fen), canonical)
            self.assertEqual(
                logic.key_of(fen),
                hashlib.sha256(canonical.encode()).hexdigest())

    def test_a_legitimate_right_keeps_its_pre_v2_key(self):
        import hashlib
        raw = raw_after(LEGITIMATE_BEFORE, 'e2e4')
        legacy = ' '.join(raw.split()[:4] + ['0', '1'])
        self.assertEqual(logic.key_of(raw),
                         hashlib.sha256(legacy.encode()).hexdigest())

    def test_a_position_with_no_legal_move_drops_a_meaningless_right(self):
        """Stalemate: the right cannot be exercised, so it is not identity.

        The move generator agrees — ``get_fen`` strips it there by itself.
        """
        stalemate = 'k7/1R6/8/8/8/6K1/8/8 b - e3 0 1'
        self.assertEqual(pf.get_fen(logic.VARIANT, stalemate, []).split()[3],
                         '-')
        self.assertEqual(logic.canonical_fen(stalemate).split()[3], '-')

    def test_the_canonicalizer_version_is_declared(self):
        self.assertEqual(logic.CANONICAL_VERSION, 2)

    def test_a_phantom_position_becomes_tablebase_applicable(self):
        """A right that cannot be used never blocked a TB probe either."""
        raw = raw_after(OWN_KING_BEFORE, 'e2e4')
        self.assertFalse(logic.tb_applicable(raw))
        self.assertTrue(logic.tb_applicable(logic.canonical_fen(raw)))


class RepetitionGuardTests(SimpleTestCase):
    """The line that used to walk straight past the repetition guard.

    Board B is reached after 1...g5, which sets a phantom g6.  Four reversible
    king moves later the board and the side to move are identical, but the
    right is gone.  Under v1 those were two keys, so the guard saw no
    repetition; under v2 they are one key and it fires.
    """

    SHUFFLE = ['h6h7', 'b8a8', 'h7h6', 'a8b8']

    def _board_after_double_push(self):
        return raw_after(WHITE_PHANTOM_BEFORE, 'g7g5')

    def test_the_shuffle_really_returns_to_the_same_board(self):
        fen = self._board_after_double_push()
        walked = fen
        for move in self.SHUFFLE:
            self.assertIn(move, pf.legal_moves(logic.VARIANT, walked, []))
            walked = pf.get_fen(logic.VARIANT, walked, [move])
        self.assertEqual(walked.split()[:2], fen.split()[:2])
        self.assertEqual(fen.split()[3], 'g6')     # the phantom
        self.assertEqual(walked.split()[3], '-')   # and its twin

    def test_v1_would_have_missed_it_and_v2_does_not(self):
        fen = self._board_after_double_push()
        walked = fen
        for move in self.SHUFFLE:
            walked = pf.get_fen(logic.VARIANT, walked, [move])
        legacy_start = ' '.join(fen.split()[:4] + ['0', '1'])
        legacy_end = ' '.join(walked.split()[:4] + ['0', '1'])
        self.assertNotEqual(legacy_start, legacy_end)          # v1: two keys
        self.assertEqual(logic.canonical_fen(fen),
                         logic.canonical_fen(walked))          # v2: one

    def test_the_witness_verifier_now_rejects_the_repetition(self):
        """``verify_mate_pv`` refuses a line that repeats internally."""
        fen = self._board_after_double_push()
        # The shuffle returns to the start of the line, so whatever it claims
        # to end in, the witness is not a legal proof of anything.
        self.assertFalse(logic.verify_mate_pv(fen, self.SHUFFLE, True))

    def test_a_forced_mate_search_treats_it_as_a_dead_branch(self):
        fen = self._board_after_double_push()
        self.assertEqual(
            logic.prove_forced_mate(fen, True, len(self.SHUFFLE),
                                    budget_positions=50_000),
            'NO_MATE')


class RekeyCommandTests(TestCase):

    def _phantom_row(self, before=OWN_KING_BEFORE, move='e2e4', **fields):
        """A row stored the way canonicalizer v1 would have stored it."""
        raw = raw_after(before, move)
        legacy_fen = ' '.join(raw.split()[:4] + ['0', '1'])
        import hashlib
        legacy_key = hashlib.sha256(legacy_fen.encode()).hexdigest()
        return Position.objects.create(key=legacy_key, fen=legacy_fen,
                                       **fields)

    def _twin_row(self, before=OWN_KING_BEFORE, move='e2e4', **fields):
        raw = raw_after(before, move)
        fen = logic.canonical_fen(raw)
        return Position.objects.create(key=logic.key_of(fen), fen=fen,
                                       **fields)

    def test_a_lone_phantom_row_moves_to_its_new_key(self):
        old = self._phantom_row(eval_cp=120, visits=3, nodes_invested=500)
        out = StringIO()

        call_command('rekey_en_passant', json=True, stdout=out)

        self.assertFalse(Position.objects.filter(key=old.key).exists())
        moved = Position.objects.get(fen=logic.canonical_fen(old.fen))
        self.assertEqual(moved.eval_cp, 120)
        self.assertEqual(moved.visits, 3)
        self.assertEqual(moved.fen.split()[3], '-')
        self.assertIn('"moved": 1', out.getvalue())
        event = DBEvent.objects.get(kind='REKEYED')
        self.assertEqual(event.payload['old'], old.key)
        self.assertFalse(event.payload['merged'])

    def test_a_legitimate_row_is_left_exactly_where_it_is(self):
        keeper = self._phantom_row(before=LEGITIMATE_BEFORE)
        out = StringIO()

        call_command('rekey_en_passant', json=True, stdout=out)

        self.assertTrue(Position.objects.filter(key=keeper.key).exists())
        self.assertIn('"unchanged": 1', out.getvalue())
        self.assertFalse(DBEvent.objects.filter(kind='REKEYED').exists())

    def test_dry_run_writes_nothing(self):
        old = self._phantom_row()
        out = StringIO()

        call_command('rekey_en_passant', dry_run=True, stdout=out)

        self.assertTrue(Position.objects.filter(key=old.key).exists())
        self.assertIn('would move', out.getvalue())
        self.assertFalse(DBEvent.objects.filter(kind='REKEYED').exists())

    def test_a_second_run_is_a_no_op(self):
        self._phantom_row()
        call_command('rekey_en_passant', stdout=StringIO())
        out = StringIO()

        call_command('rekey_en_passant', json=True, stdout=out)

        self.assertIn('"moved": 0', out.getvalue())
        self.assertEqual(DBEvent.objects.filter(kind='REKEYED').count(), 1)

    def test_edges_tasks_and_every_other_reference_follow_the_row(self):
        old = self._phantom_row()
        above = ingest.get_or_create_position(QUIET_FEN)
        below = ingest.get_or_create_position(
            '8/8/8/8/8/8/2k5/K6Q w - - 0 1')
        Edge.objects.create(parent=above, move_uci='h1h2', child=old)
        Edge.objects.create(parent=old, move_uci='d4e3', child=below)
        task = AnalysisTask.objects.create(position=old, generation=0,
                                           budget_nodes=1_000)
        IngestJob.objects.create(task=task, position=old, payload={})
        RequestLog.objects.create(ip='127.0.0.1', position=old)
        OpeningNameSuggestion.objects.create(position=old, proposed_name='x',
                                             ip='127.0.0.1')
        SolveTask.objects.create(position=old, goal='WHITE_WIN',
                                 budget_nodes=1_000)
        Campaign.objects.create(name='c', root=old)
        ProofCampaign.objects.create(name='pc', root=old, goal='WHITE_WIN')
        ProofNode.objects.create(
            campaign=ProofCampaign.objects.get(name='pc'), position=old)

        call_command('rekey_en_passant', stdout=StringIO())

        new_key = logic.key_of(logic.canonical_fen(old.fen))
        self.assertFalse(Position.objects.filter(key=old.key).exists())
        self.assertEqual(Edge.objects.get(parent=above).child_id, new_key)
        self.assertEqual(Edge.objects.get(child=below).parent_id, new_key)
        self.assertEqual(AnalysisTask.objects.get().position_id, new_key)
        self.assertEqual(IngestJob.objects.get().position_id, new_key)
        self.assertEqual(RequestLog.objects.get().position_id, new_key)
        self.assertEqual(OpeningNameSuggestion.objects.get().position_id,
                         new_key)
        self.assertEqual(SolveTask.objects.get().position_id, new_key)
        self.assertEqual(Campaign.objects.get(name='c').root_id, new_key)
        self.assertEqual(ProofCampaign.objects.get(name='pc').root_id, new_key)
        self.assertTrue(ProofNode.objects.filter(
            campaign__name='pc', position_id=new_key).exists())
        # Nothing at all may still point at the retired key.
        self.assertFalse(ProofNode.objects.filter(
            position_id=old.key).exists())
        self.assertFalse(Edge.objects.filter(parent_id=old.key).exists())
        self.assertFalse(Edge.objects.filter(child_id=old.key).exists())

    def test_a_real_merge_keeps_the_union_without_breaking_a_constraint(self):
        old = self._phantom_row(eval_cp=40, visits=2, nodes_invested=100,
                                time_invested=5.0, expanded=False)
        twin = self._twin_row(eval_cp=-15, visits=1, nodes_invested=900,
                              time_invested=2.0, expanded=True,
                              status='WHITE_WIN', closure='MATE_PV',
                              proof='ANDOR', mate_in=3, clock_slack=90)
        above = ingest.get_or_create_position(QUIET_FEN)
        below = ingest.get_or_create_position(
            '8/8/8/8/8/8/2k5/K6Q w - - 0 1')
        # Both rows carry the SAME outgoing move: one of the two edges has to
        # go, and the unique constraint is what says so.
        Edge.objects.create(parent=old, move_uci='d4e3', child=below)
        Edge.objects.create(parent=twin, move_uci='d4e3', child=below)
        Edge.objects.create(parent=old, move_uci='d4d3', child=below)
        Edge.objects.create(parent=above, move_uci='h1h2', child=old)
        # And the same generation, which must be renumbered rather than lost.
        AnalysisTask.objects.create(position=old, generation=0,
                                    budget_nodes=1_000)
        AnalysisTask.objects.create(position=twin, generation=0,
                                    budget_nodes=2_000)
        out = StringIO()

        call_command('rekey_en_passant', json=True, stdout=out)

        self.assertIn('"merged": 1', out.getvalue())
        self.assertFalse(Position.objects.filter(key=old.key).exists())
        survivor = Position.objects.get(key=twin.key)
        # Effort is additive; the proved status and its evidence survive.
        self.assertEqual(survivor.visits, 3)
        self.assertEqual(survivor.nodes_invested, 1_000)
        self.assertEqual(survivor.time_invested, 7.0)
        self.assertEqual(survivor.status, 'WHITE_WIN')
        self.assertEqual(survivor.clock_slack, 90)
        self.assertTrue(survivor.expanded)
        # The better funded search owns the eval.
        self.assertEqual(survivor.eval_cp, -15)
        # One edge per (parent, move), no duplicates, nothing orphaned.
        self.assertEqual(Edge.objects.filter(parent=survivor).count(), 2)
        self.assertEqual(
            sorted(Edge.objects.filter(parent=survivor)
                   .values_list('move_uci', flat=True)),
            ['d4d3', 'd4e3'])
        self.assertEqual(Edge.objects.get(parent=above).child_id, twin.key)
        self.assertEqual(AnalysisTask.objects.filter(
            position=survivor).count(), 2)
        self.assertEqual(
            sorted(AnalysisTask.objects.values_list('generation', flat=True)),
            [0, 1])

    def test_a_merge_keeps_one_proof_node_per_campaign(self):
        old = self._phantom_row()
        twin = self._twin_row()
        campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
        ProofNode.objects.create(campaign=campaign, position=old, pn=5, dn=7)
        ProofNode.objects.create(campaign=campaign, position=twin, pn=1, dn=2)

        call_command('rekey_en_passant', stdout=StringIO())

        self.assertEqual(ProofNode.objects.filter(campaign=campaign,
                                                  position=twin).count(), 1)

    def test_contradictory_proved_statuses_are_refused_not_guessed(self):
        old = self._phantom_row(status='WHITE_WIN', closure='MATE_PV')
        twin = self._twin_row(status='BLACK_WIN', closure='MATE_PV')
        out = StringIO()

        call_command('rekey_en_passant', json=True, stdout=out)

        self.assertIn('"conflicts": 1', out.getvalue())
        self.assertTrue(Position.objects.filter(key=old.key).exists())
        self.assertTrue(Position.objects.filter(key=twin.key).exists())
        self.assertTrue(DBEvent.objects.filter(kind='REKEY_CONFLICT').exists())

    def test_the_audit_verifies_the_pass_afterwards(self):
        self._phantom_row()

        with self.assertRaises(CommandError):
            call_command('audit_en_passant', verify=True, stdout=StringIO())

        call_command('rekey_en_passant', stdout=StringIO())
        out = StringIO()
        call_command('audit_en_passant', verify=True, json=True, stdout=out)
        self.assertIn('"phantom": 0', out.getvalue())

    def test_the_self_test_shows_what_the_canonicalizer_does(self):
        out = StringIO()
        call_command('audit_en_passant', self_test=True, stdout=out)
        text = out.getvalue()
        self.assertIn('PHANTOM own-king-explodes-d3', text)
        self.assertIn('canonical=dropped', text)
        self.assertIn('ok      ordinary-legal-capture', text)
        self.assertIn('canonical=kept', text)


class IngestUsesTheNewIdentityTests(TestCase):

    def test_expanding_a_parent_stores_the_child_without_the_phantom(self):
        parent = ingest.get_or_create_position(OWN_KING_BEFORE)
        ingest.expand(parent)

        child = Edge.objects.get(parent=parent, move_uci='e2e4').child

        self.assertEqual(child.fen.split()[3], '-')
        self.assertEqual(child.key, logic.key_of(child.fen))
