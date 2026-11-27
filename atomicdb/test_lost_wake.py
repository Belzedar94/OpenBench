"""Two climbs that meet above both landings leave no stale row behind.

THE CASE (27-Nov-2026, 0db83faa after ...Qa7).  Black to move, it stored
1294 via ...e6e5 while ...e6e5 read 1324 and ...h6g8 1316.  The inline climb
of a search under ...e6e5 and the drainer's climb of a search under ...e6d5
ran in the same second and both read 0db83faa: each decided it from a
sibling the other was changing (see the block above
ingest.CLIMB_RECHECK_MAX_KEYS).

The tests replay the two transactions one after the other on one
connection: the climb that ran beside the other one is given the number the
other had not committed yet, and the commit of each one is the moment its
on-commit callbacks run.

    P (Black to move)  -- e7e5 -> A  30
                       -- d7d5 -> B  20, then 50 after a search of D below it
                       -- c7c5 -> C  15, then 40 after a search of E below it

Whatever the order, P ends at 30 via e7e5.
"""

from . import ingest, logic, propagation_queue
from .database import current_alias
from .models import Edge, Position
from .testing import TestCase

_BACKED = ('backed_eval', 'backed_move', 'backed_plies', 'backed_nodes')


def _after(pos, uci):
    child = Edge.objects.get(parent=pos, move_uci=uci).child
    ingest.expand(child)
    return child


def _lines(values):
    return [{'move': uci, 'eval_cp': value, 'pv': [uci]}
            for uci, value in values]


def _backed(pos):
    return Position.objects.filter(key=pos.key).values(*_BACKED).get()


def _put(pos, values):
    Position.objects.filter(key=pos.key).update(**values)


class MeetingClimbTests(TestCase):

    def setUp(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        self.p = _after(root, 'g1f3')
        self._land(self.p, [('d7d5', 20), ('e7e5', 30), ('c7c5', 15)])
        self.b = _after(self.p, 'd7d5')
        self._land(self.b, [('e2e3', 20), ('b1c3', 10)])
        self.c = _after(self.p, 'c7c5')
        self._land(self.c, [('e2e3', 15), ('b1c3', 5)])
        self.d = _after(self.b, 'e2e3')
        self.e = _after(self.c, 'e2e3')
        self.assertEqual(self._p(), (15, 'c7c5'))

    def _land(self, pos, values, defer=False):
        ingest.ingest_analysis(pos.key, _lines(values), 128_000_000,
                               defer_propagation={} if defer else None)
        if defer:
            propagation_queue.drain()

    def _p(self):
        row = _backed(self.p)
        return row['backed_eval'], row['backed_move']

    def _search_d(self, defer):
        """The search under B: B rises to 50."""
        self._land(self.d, [('g8f6', 50), ('b8c6', 60)], defer)

    def _search_e(self, defer):
        """The search under C: C rises to 40."""
        self._land(self.e, [('g8f6', 40), ('b8c6', 45)], defer)

    def _commits_last_after_writing(self, defer):
        # B's climb commits first.  C's climb, beside it, read B before that
        # commit and rewrote P from B's old 20; it commits last.
        old_b = _backed(self.b)
        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True):
            self._search_d(defer)
        new_b = _backed(self.b)
        self.assertEqual(new_b['backed_eval'], 50)
        _put(self.b, old_b)
        with self.captureOnCommitCallbacks(using=current_alias()) as late:
            self._search_e(defer)
        self.assertEqual(self._p(), (20, 'd7d5'))
        _put(self.b, new_b)             # what C's climb had not seen
        for callback in late:
            callback()
        self.assertEqual(self._p(), (30, 'e7e5'))

    def _commits_last_after_resting(self, defer):
        # The case of 27-Nov: B's climb read C before C's climb committed,
        # found P standing on C at 15 as stored, and commits last.  C's climb
        # read B before that commit and wrote P from B's old 20.
        old_b = _backed(self.b)
        with self.captureOnCommitCallbacks(using=current_alias()) as late:
            self._search_d(defer)
        self.assertEqual(self._p(), (15, 'c7c5'))
        new_b = _backed(self.b)
        _put(self.b, old_b)
        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True):
            self._search_e(defer)
        self.assertEqual(self._p(), (20, 'd7d5'))
        _put(self.b, new_b)
        for callback in late:
            callback()
        self.assertEqual(self._p(), (30, 'e7e5'))

    def test_the_climb_that_wrote_last_reads_the_row_again(self):
        self._commits_last_after_writing(defer=False)

    def test_the_climb_that_rested_last_reads_the_row_again(self):
        self._commits_last_after_resting(defer=False)

    def test_deferred_climb_that_wrote_last_reads_the_row_again(self):
        self._commits_last_after_writing(defer=True)

    def test_deferred_climb_that_rested_last_reads_the_row_again(self):
        self._commits_last_after_resting(defer=True)

    def test_one_climb_after_the_other_needs_no_repair(self):
        for defer in (False, True):
            with self.subTest(defer=defer):
                self._search_d(defer)
                self._search_e(defer)
                self.assertEqual(self._p(), (30, 'e7e5'))
