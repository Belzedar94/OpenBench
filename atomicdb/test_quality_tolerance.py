"""The fourth mechanism of the Wolfram symptom: the quality guard's strictness.

Node after 1.Nf3 f6 2.Nc3 Nh6 3.g4 c6.  Its d2d4 row showed 416 [BACKED]; its
own header stayed at 369.  Forensics: the node had 128,111,808 nodes of its
own and the child's backing carried 128,007,926 — a 0.08% difference, the same
search for all practical purposes — and the quality guard blocked a
mover-favourable displacement over it.

That guard exists so an 8M search cannot overwrite what a 10B one backed.  It
was never meant to arbitrate between two runs of the same size.
"""

from . import ingest, logic
from .models import AnalysisTask, DBEvent, Edge, Position
from .testing import TestCase


def _pos(key, stm, **fields):
    fen = ('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR '
           f'{stm} KQkq - 0 1')
    return Position.objects.create(key=key.ljust(64, '0'), fen=fen, **fields)


class QualityToleranceTests(TestCase):

    def _wolfram(self, child_quality, child_value=416, own=369,
                 own_quality=128_111_808):
        """The reported shape: White to move, partial coverage, one informed
        child claiming a better value."""
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        parent.eval_cp = own
        parent.nodes_invested = own_quality
        parent.save()
        edge = Edge.objects.get(parent=parent, move_uci='d2d4')
        child = edge.child
        child.backed_eval = child_value
        child.backed_nodes = child_quality
        child.backed_plies = 2
        child.save()
        return parent, child

    def test_the_reported_case_now_displaces(self):
        parent, _child = self._wolfram(child_quality=128_007_926)

        ingest.backup_backed_evals([parent.key])

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 416)
        self.assertEqual(parent.backed_move, 'd2d4')

    def test_a_genuinely_shallow_child_still_cannot_overwrite(self):
        """8M against 10B: the case the guard was built for."""
        parent, _child = self._wolfram(child_quality=8_000_000,
                                       own_quality=10_000_000_000)

        ingest.backup_backed_evals([parent.key])

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 369)
        self.assertIsNone(parent.backed_move)

    def test_exactly_at_the_tolerance_qualifies(self):
        parent, _child = self._wolfram(child_quality=50_000_000,
                                       own_quality=100_000_000)
        ingest.backup_backed_evals([parent.key])
        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 416)

    def test_just_under_the_tolerance_does_not(self):
        parent, _child = self._wolfram(child_quality=49_000_000,
                                       own_quality=100_000_000)
        ingest.backup_backed_evals([parent.key])
        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 369)

    def test_the_directional_guard_is_untouched_at_any_quality(self):
        """A worse-for-the-mover value never displaces, however well funded."""
        parent, _child = self._wolfram(child_value=120,   # worse for White
                                       child_quality=10_000_000_000,
                                       own_quality=1_000)

        ingest.backup_backed_evals([parent.key])

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 369)
        self.assertIsNone(parent.backed_move)

    def test_the_tolerance_is_a_named_documented_constant(self):
        self.assertEqual(ingest.BACKED_QUALITY_TOLERANCE, 0.5)


class QualityConvergenceTests(TestCase):

    def test_a_legitimate_block_buys_the_child_the_missing_depth(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        parent.eval_cp = 369
        parent.nodes_invested = 2_000_000_000
        parent.save()
        child = Edge.objects.get(parent=parent, move_uci='d2d4').child
        child.backed_eval = 416
        child.backed_nodes = 8_000_000
        child.nodes_invested = 8_000_000
        child.save()

        ingest.backup_backed_evals([parent.key])

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 369)     # correctly blocked
        task = AnalysisTask.objects.get(position=child)
        self.assertEqual(task.source, AnalysisTask.Source.FILL)
        self.assertGreaterEqual(task.budget_nodes, 2_000_000_000)
        self.assertTrue(DBEvent.objects.filter(
            kind='QUALITY_CONVERGENCE').exists())

    def test_a_displacement_buys_nothing(self):
        """No discrepancy left to resolve: the value simply moved."""
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        parent.eval_cp = 369
        parent.nodes_invested = 128_111_808
        parent.save()
        child = Edge.objects.get(parent=parent, move_uci='d2d4').child
        child.backed_eval = 416
        child.backed_nodes = 128_007_926
        child.save()

        ingest.backup_backed_evals([parent.key])

        self.assertFalse(AnalysisTask.objects.filter(
            source=AnalysisTask.Source.FILL).exists())

    def test_a_child_that_already_has_the_depth_is_not_re_bought(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        parent.eval_cp = 369
        parent.nodes_invested = 2_000_000_000
        parent.save()
        child = Edge.objects.get(parent=parent, move_uci='d2d4').child
        child.backed_eval = 416
        child.backed_nodes = 8_000_000
        child.nodes_invested = 2_000_000_000
        child.save()

        ingest.backup_backed_evals([parent.key])

        self.assertFalse(AnalysisTask.objects.filter(position=child).exists())

    def test_bought_depth_closes_the_loop_instead_of_going_silent(self):
        """The other half of every purchase: it has to WORK.

        The child's contribution used to carry only ``backed_nodes`` — the
        support of the leaf that backed the value.  A convergence purchase
        raised the child's ``nodes_invested``, which the contribution never
        looked at, so the parent stayed outweighed, nothing re-bought the
        child ("already has the depth"), and the discrepancy went silent
        forever.  Wolfram's spines, own-searched at 2.6B, blocked every
        128M-supported chain under them permanently.  The child's best
        knowledge is sustained by EVERYTHING invested in it.
        """
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        parent.eval_cp = 369
        parent.nodes_invested = 2_000_000_000
        parent.save()
        child = Edge.objects.get(parent=parent, move_uci='d2d4').child
        child.backed_eval = 416
        child.backed_nodes = 8_000_000            # the leaf that backed it
        child.nodes_invested = 2_000_000_000      # the purchase, landed
        child.save()

        ingest.backup_backed_evals([parent.key])

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 416)
        self.assertEqual(parent.backed_move, 'd2d4')

    def test_the_rung_matches_the_support_it_has_to_equal(self):
        self.assertEqual(ingest._rung_at_least(1), ingest.BUDGET_LADDER[0])
        self.assertEqual(ingest._rung_at_least(200_000_000), 512_000_000)
        self.assertEqual(ingest._rung_at_least(10 ** 12),
                         ingest.BUDGET_LADDER[-1])

    def _blocked_parent_and_child(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        parent.eval_cp = 369
        parent.nodes_invested = 2_000_000_000
        parent.save()
        child = Edge.objects.get(parent=parent, move_uci='d2d4').child
        child.backed_eval = 416
        child.backed_nodes = 8_000_000
        child.save()
        return parent, child

    def _fill_queue(self, rows, arm):
        filler = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'a2a3'))
        for index in range(rows):
            AnalysisTask.objects.create(
                position=filler, generation=index, budget_nodes=1,
                source=AnalysisTask.Source.FILL, arm=arm)

    def test_the_fill_cap_still_bounds_it(self):
        parent, child = self._blocked_parent_and_child()
        self._fill_queue(ingest.QUALITY_QUEUE_CAP, ingest.QUALITY_ARM)

        ingest.backup_backed_evals([parent.key])

        self.assertFalse(AnalysisTask.objects.filter(position=child).exists())

    def test_another_arms_backlog_does_not_spend_this_arms_quota(self):
        """El cupo se cuenta por BRAZO, no sobre todo lo que sea FILL.

        Los tres productores de FILL viven en dos procesos distintos y ninguno
        puede decidir por los otros: contar la cola ajena como propia hacia que
        el que llegaba segundo se quedara sin encolar nada.
        """
        parent, child = self._blocked_parent_and_child()
        self._fill_queue(ingest.COVERAGE_QUEUE_CAP + ingest.QUALITY_QUEUE_CAP,
                         ingest.COVERAGE_ARM)

        ingest.backup_backed_evals([parent.key])

        queued = AnalysisTask.objects.filter(position=child).first()
        self.assertIsNotNone(queued)
        self.assertEqual(queued.arm, ingest.QUALITY_ARM)
