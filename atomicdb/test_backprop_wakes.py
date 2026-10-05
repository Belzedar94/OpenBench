"""No backed value is left standing on something its children no longer hold.

Three community reports, one invariant (docs/value-semantics.md, invariant 5).

* "A repetition related backprop bug" (moky, 15-Sep) and "peak backprop"
  (Eclipsia, 4-Oct): a node kept ``490 via d8a5`` while the position after
  d8a5 had moved on to 522.  The ascent reached that node three times in one
  cascade, through transpositions of different lengths, and the third wake was
  dropped without a trace: ``BACKED_MAX_REVISITS`` was 2.  Shuffling lines are
  where a position is reachable by many move orders of different lengths, which
  is why it read as a repetition bug.

* "best move doesnt match eval" (Eclipsia, 13-Sep): a child that two parents
  share was re-seeded by the analysis of ONE of them, and the other parent kept
  quoting the number the child had before.
"""

import hashlib

from . import ingest, logic
from .models import DBEvent, Edge, Position
from .testing import TestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _leaf(name, stm, value):
    return _pos(name, stm, eval_cp=value, nodes_invested=1_000)


def _chain(name, top, bottom, plies):
    """``plies`` edges from ``top`` down to ``bottom`` through fresh nodes."""
    upper, colour = top, top.fen.split()[1]
    for i in range(plies - 1):
        colour = 'b' if colour == 'w' else 'w'
        lower = _pos(f'{name}-{i}', colour, expanded=True)
        _edge(upper, lower, 'a1a1')
        upper = lower
    _edge(upper, bottom, 'a1a1')


class LateWakeTests(TestCase):
    """A node reached three times by one ascent is recomputed three times."""

    def _three_roads(self, name):
        """P(white) reaches S by 2, 4 and 6 plies.

        The two short roads pass through a black node with a cheaper way
        out (150), so only the LONGEST road carries the full value of S up.
        That is the shape of the reports: the wave that matters arrives last.
        """
        p = _pos(f'{name}-P', 'w', expanded=True)
        s = _leaf(f'{name}-S', 'w', 100)
        # 2 plies: P -> c1(b) -> S, and c1 may also take the 150 exit.
        c1 = _pos(f'{name}-c1', 'b', expanded=True)
        _edge(p, c1, 'm1m1')
        _edge(c1, s, 'a1a1')
        _edge(c1, _leaf(f'{name}-cap1', 'w', 150), 'h1h1')
        # 4 plies: P -> c2(b) -> x2(w) -> y2(b) -> S, same exit at y2.
        c2 = _pos(f'{name}-c2', 'b', expanded=True)
        x2 = _pos(f'{name}-x2', 'w', expanded=True)
        y2 = _pos(f'{name}-y2', 'b', expanded=True)
        _edge(p, c2, 'm2m2')
        _edge(c2, x2, 'a1a1')
        _edge(x2, y2, 'a1a1')
        _edge(y2, s, 'a1a1')
        _edge(y2, _leaf(f'{name}-cap2', 'w', 150), 'h1h1')
        # 6 plies, no exit: P -> c3(b) -> ... -> S.
        c3 = _pos(f'{name}-c3', 'b', expanded=True)
        _edge(p, c3, 'm3m3')
        _chain(f'{name}-long', c3, s, 5)
        return p, s, c3

    def _analyse(self, pos, value):
        """What an ingest does to the backed values: the analysed position and
        its parents seed the ascent (see ingest_analysis)."""
        Position.objects.filter(key=pos.key).update(eval_cp=value)
        parents = list(Edge.objects.filter(child_id=pos.key)
                       .values_list('parent_id', flat=True))
        return ingest.backup_backed_evals([pos.key, *parents])

    def test_the_last_wave_of_an_ascent_still_reaches_the_node(self):
        p, s, c3 = self._three_roads('LATE')
        self._analyse(s, 100)
        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 100)

        self._analyse(s, 200)

        c3.refresh_from_db()
        p.refresh_from_db()
        self.assertEqual(c3.backed_eval, 200)
        # The header of P is the max of what its three rows say.  Before the
        # fix it stayed on 150 with a row at 200 right under it.
        self.assertEqual((p.backed_eval, p.backed_move), (200, 'm3m3'))

    def test_an_ascent_leaves_nothing_for_the_next_one_to_fix(self):
        # The stop condition of ``recascade_backed`` is a pass that changes no
        # row.  A cascade that leaves its own stale node behind makes that
        # condition unreachable: the next pass fixes it, and breaks another.
        p, s, _c3 = self._three_roads('FIXPOINT')
        self._analyse(s, 100)
        self._analyse(s, 200)

        every = list(Position.objects.values_list('key', flat=True))
        self.assertEqual(ingest.backup_backed_evals(every), 0)

    def test_a_wake_is_never_dropped_in_silence(self):
        # Whatever bound stops an ascent says so.  A node the ascent could not
        # recompute is a node standing on a value nobody backs, and that is
        # worth an event, not a shrug.
        p, s, _c3 = self._three_roads('LOUD')
        self._analyse(s, 100)
        saved = ingest.BACKED_MAX_REVISITS
        ingest.BACKED_MAX_REVISITS = 2
        try:
            self._analyse(s, 200)
        finally:
            ingest.BACKED_MAX_REVISITS = saved

        guard = DBEvent.objects.filter(kind='BACKED_GUARD').last()
        self.assertIsNotNone(guard)
        self.assertEqual(guard.payload['reason'], 'revisit-cap')
        self.assertIn(p.key, guard.payload['keys'])


class SharedChildSeedTests(TestCase):
    """A re-seeded child wakes EVERY parent that quotes it."""

    def _lines(self, values):
        return [{'move': uci, 'eval_cp': value, 'pv': [uci]}
                for uci, value in values]

    def _transposition(self):
        """1.Nf3 Nf6 2.Nc3 and 1.Nc3 Nf6 2.Nf3 are one position."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)

        def after(pos, uci):
            child = Edge.objects.get(parent=pos, move_uci=uci).child
            ingest.expand(child)
            return child

        first = after(after(root, 'g1f3'), 'g8f6')
        second = after(after(root, 'b1c3'), 'g8f6')
        shared = Edge.objects.get(parent=first, move_uci='b1c3').child
        self.assertEqual(
            shared.key,
            Edge.objects.get(parent=second, move_uci='g1f3').child_id)
        return first, second, shared

    def test_the_other_parent_follows_a_seed_it_did_not_write(self):
        first, second, shared = self._transposition()
        ingest.ingest_analysis(first.key, self._lines(
            [('b1c3', 500), ('e2e4', 400)]), 128_000_000)
        first.refresh_from_db()
        self.assertEqual((first.backed_eval, first.backed_move),
                         (500, 'b1c3'))

        # The second parent's search values the SAME child at 100, and the
        # newer claim replaces the older one on the child (invariant 2).
        ingest.ingest_analysis(second.key, self._lines(
            [('g1f3', 100), ('e2e4', 50)]), 128_000_000)

        shared.refresh_from_db()
        self.assertEqual(shared.eval_cp, 100)
        first.refresh_from_db()
        # 500 via b1c3 is a number no row of this table says any more: b1c3
        # reads 100 and the best row is e2e4 at 400.
        self.assertEqual((first.backed_eval, first.backed_move),
                         (400, 'e2e4'))
