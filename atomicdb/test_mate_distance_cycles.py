"""A repetition cannot lengthen a proven mate (see the block above
``ingest.MATE_SUPPORT_HOPS``).

THE CASE (25-Nov-2026).  Four closed positions stood on each other: a
Black-to-move MATE_PV position with replies still open took its longest
closed reply, which led through a White position, a Black position with
every reply closed and another White position back to itself.  The side
that is mated takes its longest reply, so each lap added four plies, and
CASCADE_REWRITE_LIMIT only capped how many laps one cascade made: the live
rows read 284-289 plies over a won line of 30.

The loop here is the same shape on the knight shuffle 1.Nf3 Nf6 2.Ng1 Ng8,
which IS the start position again: A (Black) -Nf6-> B (White) -Ng1-> C
(Black, every reply closed) -Ng8-> S (White) -Nf3-> A.
"""

import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from . import ingest, logic
from .models import Edge, Position
from .testing import TestCase


def _position(fen, **fields):
    fen = logic.canonical_fen(fen)
    pos, _ = Position.objects.update_or_create(
        key=logic.key_of(fen), defaults={'fen': fen, **fields})
    return pos


def _after(*moves):
    fen = logic.start_fen()
    for uci in moves:
        fen = logic.apply_move(fen, uci)
    return fen


def _close(pos, mate_in, closure='MINIMAX', **fields):
    Position.objects.filter(key=pos.key).update(
        status='WHITE_WIN', closure=closure, proof='ENGINE', mate_in=mate_in,
        clock_slack=90, **fields)


OWN_LINE = 'e7e5 f3e5 d7d6 e5f7 e8f7 d1e2 f7g6 e2e4 g6h6'     # 9 plies


class RepetitionLoopTests(TestCase):

    def _loop(self, anchor='partial', a=None, b=None, c=None, s=None,
              a_best='e7e5'):
        """The four positions of the loop, closed, with these distances.

        ``anchor='partial'``: A is MATE_PV on OWN_LINE and two replies are
        closed, at 5 and 3 plies.  ``anchor='full'``: A is MINIMAX with no
        line of its own and every reply closed, the longest outside the
        loop at 7 plies.  ``anchor='alone'``: A's only edge is the loop.
        C's replies outside the loop are closed at 5 plies and less.
        """
        start = _position(logic.start_fen())
        pa = _position(_after('g1f3'))
        pb = _position(_after('g1f3', 'g8f6'))
        pc = _position(_after('g1f3', 'g8f6', 'f3g1'))
        self.assertEqual(
            logic.key_of(logic.apply_move(pc.fen, 'f6g8')), start.key)
        for pos in (start, pb, pc):
            ingest.expand(pos)
        if anchor == 'alone':
            Edge.objects.get_or_create(parent=pa, move_uci='g8f6',
                                       defaults={'child': pb})
        else:
            ingest.expand(pa)
        a_edges = list(Edge.objects.filter(parent=pa).select_related('child'))
        if anchor == 'partial':
            outside = {'d7d5': 5, 'b8c6': 3}
        elif anchor == 'full':
            outside = {e.move_uci: (7 if e.move_uci == 'd7d5' else 2)
                       for e in a_edges if e.move_uci != 'g8f6'}
        else:
            outside = {}
        for edge in a_edges:
            if edge.move_uci in outside:
                _close(edge.child, outside[edge.move_uci], closure='MATE_PV')
        for edge in Edge.objects.filter(parent=pc).select_related('child'):
            if edge.move_uci != 'f6g8':
                _close(edge.child, 5 if edge.move_uci == 'e7e5' else 1,
                       closure='MATE_PV')
        if anchor == 'partial':
            _close(pa, a, closure='MATE_PV', won_line=OWN_LINE,
                   best_move=a_best)
        else:
            _close(pa, a, best_move=a_best)
        _close(pb, b, best_move='f3g1')
        _close(pc, c, best_move='f6g8')
        _close(start, s, best_move='g1f3')
        return start, pa, pb, pc

    def _mates(self, *positions):
        return [Position.objects.get(key=p.key).mate_in for p in positions]

    # ---- the partial-information anchor, the live shape ----

    def test_the_loop_converges_to_the_acyclic_distances(self):
        # Acyclic: A = its own 9; S = 1 + A; C = 1 + max(S, 5) (repeating
        # into S and mating from there IS C's longest defence: the loop does
        # not pass through C); B = 1 + C.  A would take 1 + B = 13: that
        # reply comes back to A, so it is set aside.
        start, pa, pb, pc = self._loop(a=9, b=None, c=None, s=None)
        Position.objects.filter(key__in=[pb.key, pc.key, start.key]).update(
            mate_in=3)

        ingest.backup_cascade([pc.key, start.key, pb.key, pa.key])

        self.assertEqual(self._mates(pa, start, pc, pb), [9, 10, 11, 12])
        for seeds in ([pa.key], [pb.key], [pc.key], [start.key],
                      [pa.key, pb.key, pc.key, start.key]):
            ingest.backup_cascade(seeds)
            self.assertEqual(self._mates(pa, start, pc, pb), [9, 10, 11, 12])

    def test_an_inflated_loop_at_rest_is_repaired_by_the_listed_backfill(self):
        """The live rows: inflated, consistent round the loop, at rest."""
        start, pa, pb, pc = self._loop(a=289, b=288, c=287, s=286)
        handle, path = tempfile.mkstemp(suffix='.txt')
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as keys:
                keys.write('\n'.join(p.key for p in (pa, pb, pc, start)))
            out = StringIO()
            call_command('backfill_mate_distance', '--keys-file', path,
                         stdout=out, stderr=out)
        finally:
            os.remove(path)

        report = out.getvalue()
        self.assertIn('fixed_point=True', report)
        self.assertIn('frozen=0', report)
        self.assertIn('lowered=4', report)
        self.assertEqual(self._mates(pa, start, pc, pb), [9, 10, 11, 12])
        # And what the cascade does from there changes nothing.
        ingest.backup_cascade([pa.key, pb.key, pc.key, start.key])
        self.assertEqual(self._mates(pa, start, pc, pb), [9, 10, 11, 12])

    def test_an_inflated_loop_that_moves_comes_down_in_the_cascade(self):
        start, pa, pb, pc = self._loop(a=289, b=288, c=287, s=286)

        ingest.backup_cascade([pa.key])

        self.assertEqual(self._mates(pa, start, pc, pb), [9, 10, 11, 12])

    def test_the_exported_defence_does_not_point_into_the_loop(self):
        start, pa, pb, pc = self._loop(a=9, b=None, c=None, s=None,
                                       a_best='g8f6')

        ingest.backup_cascade([pc.key, start.key, pb.key, pa.key])

        row = Position.objects.get(key=pa.key)
        self.assertEqual(row.mate_in, 9)
        self.assertEqual(row.best_move, 'e7e5')       # its own line again
        self.assertEqual(row.status, 'WHITE_WIN')
        self.assertEqual(row.closure, 'MATE_PV')
        # A closed row backs up its best move (§ _backed_for).
        row.backed_move = None
        self.assertEqual(ingest._backed_for(row, [])[1], 'e7e5')

    # ---- a full-information anchor ----

    def test_a_full_information_anchor_keeps_its_longest_outside_reply(self):
        start, pa, pb, pc = self._loop(anchor='full', a=8, b=None, c=None,
                                       s=None)

        ingest.backup_cascade([pc.key, start.key, pb.key, pa.key])

        # A = 1 + 7 (d7d5); S = 9; C = 1 + max(9, 5); B = 11.
        self.assertEqual(self._mates(pa, start, pc, pb), [8, 9, 10, 11])
        ingest.backup_cascade([pa.key, pb.key, pc.key, start.key])
        self.assertEqual(self._mates(pa, start, pc, pb), [8, 9, 10, 11])

    def test_an_anchor_with_nothing_but_the_loop_loses_its_number(self):
        """No witness and no reply outside the loop: the distance becomes
        unknown, the status stays."""
        start, pa, pb, pc = self._loop(anchor='alone', a=9, b=12, c=11,
                                       s=10, a_best='g8f6')
        row = Position.objects.get(key=pa.key)
        edges = list(Edge.objects.filter(parent=row).select_related('child'))

        self.assertEqual(ingest.mate_distance_refresh(row, edges),
                         (None, None))

    # ---- what does not change ----

    def test_a_longer_defence_that_does_not_come_back_is_still_taken(self):
        """quasa's M20/M22 (#284): a closed reply longer than the own line
        raises it when its chain does not return."""
        start, pa, pb, pc = self._loop(a=9, b=None, c=None, s=None)
        d7d5 = Edge.objects.select_related('child').get(parent=pa,
                                                        move_uci='d7d5')
        Position.objects.filter(key=d7d5.child.key).update(mate_in=21)

        ingest.backup_cascade([d7d5.child.key])

        self.assertEqual(self._mates(pa)[0], 22)

    def test_an_acyclic_raise_is_unchanged(self):
        parent = _position(_after('g1f3'))
        ingest.expand(parent)
        edges = list(Edge.objects.filter(parent=parent).select_related(
            'child'))
        for index, edge in enumerate(edges):
            _close(edge.child, 8 if index == 0 else 2, closure='MATE_PV')
        _close(parent, 3)

        ingest.backup_cascade([edges[0].child.key])

        self.assertEqual(self._mates(parent), [9])    # 1 + max, as before

    # ---- the hop limit ----

    def test_the_walk_stops_at_the_hop_limit(self):
        start, pa, pb, pc = self._loop(a=289, b=288, c=287, s=286)
        # B -> C -> S -> A: A is the fourth position the walk reads.
        self.assertTrue(ingest.support_reaches(pb.key, pa.key, hops=4))
        self.assertFalse(ingest.support_reaches(pb.key, pa.key, hops=3))
        # Inflated, A stands on B; at its own 9 it stands on its witness,
        # and a chain that ends there reaches nothing.
        self.assertTrue(ingest.support_reaches(pa.key, pb.key))
        Position.objects.filter(key=pa.key).update(mate_in=9)
        self.assertFalse(ingest.support_reaches(pa.key, pb.key))

    def test_a_loop_longer_than_the_hop_limit_is_not_set_aside(self):
        """The rule itself, read on one row, so no cascade order is involved.

        A cascade over this loop with a short hop limit is no good to test
        on: backup_cascade pops its frontier from a set, so the order it
        rewrites the four rows in follows the interpreter's string hashing,
        and how far a loop it cannot see climbs before CASCADE_REWRITE_LIMIT
        cuts it differs from run to run (that backstop has its own test in
        test_performance_guards).
        """
        start, pa, pb, pc = self._loop(a=9, b=12, c=11, s=10)
        row = Position.objects.get(key=pa.key)
        edges = list(Edge.objects.filter(parent=row).select_related('child'))

        # B -> C -> S -> A: four positions, so three hops do not see it.
        with patch('atomicdb.ingest.MATE_SUPPORT_HOPS', 3):
            self.assertEqual(ingest.mate_distance_refresh(row, edges)[0], 13)
        self.assertEqual(ingest.mate_distance_refresh(row, edges)[0], 9)
