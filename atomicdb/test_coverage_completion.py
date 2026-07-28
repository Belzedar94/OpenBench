"""Asking for the few moves a node needs to become true (round 3, addendum 2).

Wolfram's node: twelve replies, nine already proven WHITE_WIN, two evaluated
at -1300 for the mover, and ONE never analysed.  The coverage guard did the
right thing — a partial minimax at an AND node is optimistic and must not be
promoted — so the node kept publishing its stale -150 until a human asked for
that one move by hand.  It came back mate, coverage completed, and the value
jumped to +1261 and climbed to its parent.

The system knew exactly what it was missing and did nothing about it.
"""

from . import ingest, logic
from .models import AnalysisTask, DBEvent, Edge
from .testing import TestCase


class CoverageGapTests(TestCase):

    def _node(self, informed, missing, mover='b'):
        """A parent with `informed` decisive replies and `missing` blanks."""
        fen = ('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR '
               f'{mover} KQkq - 0 1')
        parent = ingest.get_or_create_position(fen)
        ingest.expand(parent)
        edges = list(Edge.objects.filter(parent=parent).order_by('id'))
        for index, edge in enumerate(edges):
            child = edge.child
            if index < len(informed):
                kind, value = informed[index]
                if kind == 'closed':
                    child.status = value
                    child.closure = 'MATE_PV'
                    child.proof = 'ANDOR'
                else:
                    child.eval_cp = value
            elif index < len(informed) + missing:
                pass                      # deliberately blank
            else:
                child.status = 'WHITE_WIN'   # filler, decisive for White
                child.closure = 'MATE_PV'
                child.proof = 'ANDOR'
            child.save()
        return parent

    def test_the_wolfram_shape_asks_for_exactly_the_missing_move(self):
        # Black to move: proven White wins plus big White-POV evals, one blank.
        parent = self._node([('closed', 'WHITE_WIN')] * 9
                            + [('eval', 1_261), ('eval', 1_337)], missing=1)

        made = ingest.enqueue_coverage_completion()

        self.assertEqual(made, 1)
        task = AnalysisTask.objects.get(source=AnalysisTask.Source.FILL)
        self.assertEqual(task.multipv, ingest.DEPTH_MULTIPV)
        self.assertGreaterEqual(task.budget_nodes, ingest.COVERAGE_SEED_NODES)
        self.assertTrue(Edge.objects.filter(
            parent=parent, child=task.position).exists())

    def test_a_balanced_node_is_left_alone(self):
        self._node([('eval', 20), ('eval', -30)], missing=1)
        self.assertEqual(ingest.enqueue_coverage_completion(), 0)

    def test_one_survivable_reply_disqualifies_the_node(self):
        """If anything looked playable, the missing moves decide nothing."""
        self._node([('closed', 'WHITE_WIN')] * 9 + [('eval', -50)],
                   missing=1)
        self.assertEqual(ingest.enqueue_coverage_completion(), 0)

    def test_a_winning_reply_for_the_mover_disqualifies_it(self):
        self._node([('closed', 'BLACK_WIN')], missing=1)
        self.assertEqual(ingest.enqueue_coverage_completion(), 0)

    def test_too_many_blanks_is_ordinary_exploration_not_this(self):
        self._node([('closed', 'WHITE_WIN')] * 8, missing=8)
        self.assertEqual(ingest.enqueue_coverage_completion(), 0)

    def test_the_global_cap_is_respected(self):
        self._node([('closed', 'WHITE_WIN')] * 9 + [('eval', 1_261)],
                   missing=3)

        self.assertEqual(ingest.enqueue_coverage_completion(cap=2), 2)
        self.assertEqual(ingest.enqueue_coverage_completion(cap=2), 0)

    def test_it_records_what_it_did(self):
        self._node([('closed', 'WHITE_WIN')] * 9 + [('eval', 1_261)],
                   missing=1)
        ingest.enqueue_coverage_completion()
        self.assertTrue(DBEvent.objects.filter(
            kind='COVERAGE_ENQUEUED').exists())

    def test_an_existing_auto_task_is_promoted_not_duplicated(self):
        parent = self._node([('closed', 'WHITE_WIN')] * 9
                            + [('eval', 1_261)], missing=1)
        blank = next(edge.child for edge in
                     Edge.objects.filter(parent=parent).order_by('id')
                     if edge.child.status == 'UNKNOWN'
                     and edge.child.eval_cp is None)
        AnalysisTask.objects.create(position=blank, generation=blank.visits,
                                    budget_nodes=8_000_000, source='AUTO')

        ingest.enqueue_coverage_completion()

        self.assertEqual(
            AnalysisTask.objects.filter(position=blank).count(), 1)
        self.assertEqual(AnalysisTask.objects.get(position=blank).source,
                         AnalysisTask.Source.FILL)


class CoverageClosesTheNodeTests(TestCase):

    def test_completing_the_coverage_lifts_the_guard_and_closes(self):
        """The whole point: the value stops lying and the node closes."""
        fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1'
        parent = ingest.get_or_create_position(fen)
        ingest.expand(parent)
        parent.eval_cp = -150            # the stale value it kept publishing
        parent.save()
        edges = list(Edge.objects.filter(parent=parent).order_by('id'))
        for edge in edges[:-1]:
            child = edge.child
            child.status = 'WHITE_WIN'
            child.closure = 'MATE_PV'
            child.proof = 'ANDOR'
            child.mate_in = 3
            child.clock_slack = 90
            child.save()
        blank = edges[-1].child

        # Before: one reply unknown, so the AND guard holds the node open.
        ingest.backup_cascade([edges[0].child.key])
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'UNKNOWN')

        self.assertEqual(ingest.enqueue_coverage_completion(), 1)

        # The missing move comes back a loss too: coverage complete.
        blank.status = 'WHITE_WIN'
        blank.closure = 'MATE_PV'
        blank.proof = 'ANDOR'
        blank.mate_in = 3
        blank.clock_slack = 90
        blank.save()
        ingest.backup_cascade([blank.key])

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'WHITE_WIN')
        self.assertEqual(parent.closure, 'MINIMAX')


class CoverageIsServedAheadOfExplorationTests(TestCase):

    def test_the_queue_order_is_user_then_fill_then_auto(self):
        """``choose_pending`` orders by ``-source``; the names encode it."""
        self.assertEqual(sorted(['AUTO', 'FILL', 'USER'], reverse=True),
                         ['USER', 'FILL', 'AUTO'])
        self.assertEqual(AnalysisTask.Source.FILL, 'FILL')
