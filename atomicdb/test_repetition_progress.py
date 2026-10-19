"""A move that comes back is not progress: a loop opens at its way out.

cz_eclipsia, 18-Oct, at ``909727fb`` (White to move): "starting from this
node, theres no way to avoid an infinite loop while keeping the eval the
same".  Rd6-f6 Rf8-d8 Rf6-d6 Rd8-f8 is back where it started, and the four
positions of that loop published the same 1200, each one on the move to the
next, so following the best move went round for ever.

The fixture is that loop, with its real positions, keys and moves, and one
way out at each position.  White can leave at ``909727fb`` (Rh6) and at
``e5866b4e`` (Rf7) for the value the family holds, 1199; those two ways out
were searched less deeply than the positions of the loop, which is what let
the loop win the tie.  Black's ways out (Re8 from both Black positions) are
worse for Black than going round.  The stored state is the one production
held: all four at 1200, each on the move to the next.
"""

import types

from django.test import Client

from . import ingest, logic, views
from .models import Edge, Position
from .testing import TestCase

ENGINE = 128_000_000
SHALLOW = 32_000_000
START = '5r2/1p5k/3R4/8/4P3/2P5/PP2Kb1P/8 w - - 0 1'
LOOP = ('d6f6', 'f8d8', 'f6d6', 'd8f8')
# The search each position of the loop had in production: (eval, nodes).
OWN = ((1118, ENGINE), (1105, ENGINE), (1124, ENGINE), (1188, 6 * ENGINE))


def _after(fen, uci):
    return logic.canonical_fen(logic.apply_move(fen, uci))


def _row(fen, **kw):
    return Position.objects.create(key=logic.key_of(fen), fen=fen, **kw)


def _loop(white_out=1199, black_out=(1365, 1444), stored=1200):
    """The four positions of the loop, as production stored them."""
    fens = [START]
    for uci in LOOP[:-1]:
        fens.append(_after(fens[-1], uci))
    assert _after(fens[-1], LOOP[-1]) == START, 'the loop must close'
    ring = [_row(fen, expanded=True, eval_cp=value, nodes_invested=nodes,
                 backed_eval=stored, backed_move=uci, backed_plies=4,
                 backed_nodes=ENGINE)
            for fen, (value, nodes), uci in zip(fens, OWN, LOOP)]
    for index, node in enumerate(ring):
        Edge.objects.create(parent=node, move_uci=LOOP[index],
                            child=ring[(index + 1) % 4])
    ways_out = (('d6h6', white_out, SHALLOW), ('f8e8', black_out[0], ENGINE),
                ('f6f7', white_out, SHALLOW), ('d8e8', black_out[1], ENGINE))
    for node, (uci, value, nodes) in zip(ring, ways_out):
        leaf = _row(_after(node.fen, uci), eval_cp=value,
                    nodes_invested=nodes)
        Edge.objects.create(parent=node, move_uci=uci, child=leaf)
    return ring


def _published(nodes):
    out = []
    for node in nodes:
        node.refresh_from_db()
        out.append((node.backed_eval, node.backed_move))
    return out


def _line(start, plies=12):
    """The keys the published line goes through, ``backed_move`` by move."""
    keys, node = [start.key], start
    for _ in range(plies):
        node.refresh_from_db()
        if not node.backed_move:
            break
        edge = (Edge.objects.select_related('child')
                .filter(parent=node, move_uci=node.backed_move).first())
        if edge is None:
            break
        node = edge.child
        keys.append(node.key)
    return keys


class ReportedLoopTests(TestCase):

    def test_the_loop_opens_at_the_way_out(self):
        ring = _loop()

        ingest.backup_backed_evals([ring[0].key])

        # The value is the same as before for everybody: what White gets by
        # leaving.  Nobody keeps the number the loop lent itself, and Black,
        # twelve pawns down, is not handed a draw.
        self.assertEqual([value for value, _move in _published(ring)],
                         [1199] * 4)
        # What changes is the move White stands on at the position the line
        # returns to: of two moves worth the same, the one that does not
        # come back.  Black still goes round, which is Black's best.
        published = _published(ring)
        self.assertEqual(published[0], (1199, 'd6h6'))
        self.assertEqual((published[1], published[3]),
                         ((1199, 'f8d8'), (1199, 'd8f8')))

    def test_the_published_line_never_repeats(self):
        ring = _loop()

        ingest.backup_backed_evals([ring[0].key])

        loop = {node.key for node in ring}
        for node in ring:
            keys = _line(node)
            self.assertEqual(len(keys), len(set(keys)), keys)
            self.assertNotIn(keys[-1], loop)

    def test_following_the_best_move_leaves_the_loop(self):
        # What the report describes, through what a client reads: play the
        # best move of the public API until it stops.  It used to go round.
        _loop()
        ingest.backup_backed_evals([logic.key_of(START)])
        client = Client()

        fen, seen = START, [START]
        for _ in range(12):
            reply = client.get('/atomicdb/api/query', {'fen': fen})
            if reply.status_code != 200 or not reply.json()['best_move']:
                break
            fen = _after(fen, reply.json()['best_move'])
            seen.append(fen)

        self.assertEqual(len(seen), len(set(seen)), seen)
        self.assertEqual(seen[:2], [START, _after(START, 'd6h6')])

    def test_the_backed_chip_reaches_an_analysis(self):
        # "Every backed value terminates in a real engine analysis, never in
        # a loop" (docs): the chip of the four positions jumped to a
        # repetition instead.
        ring = _loop()

        ingest.backup_backed_evals([ring[0].key])

        for node in ring:
            node.refresh_from_db()
            origin, _walked, outcome = views._walk_backed_spine(node)
            self.assertEqual(outcome, '', node.key[:8])
            self.assertNotIn(origin.key, {other.key for other in ring})

    def test_wherever_the_ascent_starts_the_loop_comes_to_rest_open(self):
        # Where White leaves (Rh6 at once, or Rf7 two plies later) may
        # depend on where the ascent came in; both are worth the same.  That
        # the line leaves, the value, and that it rests do not.
        for start in range(4):
            if start:
                # A fresh copy of the loop: the keys are the real ones.
                keys = set(Edge.objects.filter(parent__in=ring)
                           .values_list('child_id', flat=True))
                keys.update(node.key for node in ring)
                Edge.objects.filter(parent_id__in=keys).delete()
                Position.objects.filter(key__in=keys).delete()
            ring = _loop()
            everybody = [node.key for node in ring]

            ingest.backup_backed_evals([ring[start].key])
            ingest.backup_backed_evals(everybody)

            self.assertEqual([value for value, _move in _published(ring)],
                             [1199] * 4, start)
            for node in ring:
                keys = _line(node)
                self.assertEqual(len(keys), len(set(keys)), (start, keys))
            # At rest: one more look at everybody changes nothing.
            self.assertEqual(ingest.backup_backed_evals(everybody), 0, start)


MATE_START = '1R3b2/2k2P2/r3p1p1/4P1P1/1p6/1P6/P3K3/8 w - - 0 1'
MATE_LOOP = ('b8c8', 'c7b7', 'c8b8', 'b7c7')
MATE_OUT = ('b8d8', 'c7d7', 'c8c7', 'b7c6')


def _mate_loop():
    """``fcb828cc``'s loop, Rb8-c8 Kc7-b7 Rc8-b8 Kb7-c7, as sampled on 19-Oct.

    Everything in it is at 9500, the band where the engine sees a mate it
    has not proven: the four positions searched at 128M, and beside each
    one a move that the position's own lines claim at 9500 too, with no
    search of its own.  Production stored the four on the loop.
    """
    fens = [MATE_START]
    for uci in MATE_LOOP[:-1]:
        fens.append(_after(fens[-1], uci))
    assert _after(fens[-1], MATE_LOOP[-1]) == MATE_START
    ring = [_row(fen, expanded=True, eval_cp=9500, nodes_invested=ENGINE,
                 backed_eval=9500, backed_move=uci, backed_plies=4,
                 backed_nodes=ENGINE)
            for fen, uci in zip(fens, MATE_LOOP)]
    for index, node in enumerate(ring):
        Edge.objects.create(parent=node, move_uci=MATE_LOOP[index],
                            child=ring[(index + 1) % 4])
        Edge.objects.create(parent=node, move_uci=MATE_OUT[index],
                            child=_row(_after(node.fen, MATE_OUT[index]),
                                       eval_cp=9500))
    return ring


class LoopRecomputedAtOnceTests(TestCase):
    """Every position of a loop in the same level: a sweep, a settling look.

    A move that returns losing its ties is not enough there: all four see
    the loop and all four leave; one level later nobody returns any more,
    the loop's searched positions win the ties against the claims again and
    all four come back.  The sampled family rewrote 256 rows on every sweep.
    """

    def test_the_loop_opens_at_one_position_and_rests(self):
        ring = _mate_loop()
        everybody = [node.key for node in ring]

        for _ in range(3):
            ingest.backup_backed_evals(everybody)

        self.assertEqual(ingest.backup_backed_evals(everybody), 0)
        published = _published(ring)
        self.assertEqual([value for value, _move in published], [9500] * 4)
        # Opened once: three positions still go round, towards the one
        # that leaves, and that is the line to the way out.
        self.assertEqual(sum(move == loop for (_v, move), loop
                             in zip(published, MATE_LOOP)), 3)
        for node in ring:
            keys = _line(node)
            self.assertEqual(len(keys), len(set(keys)), keys)


class DrawStillDrawsTests(TestCase):

    def test_a_loop_nobody_can_leave_is_still_a_draw(self):
        # White's ways out lose, Black's lose more: going round is the best
        # either side has, and the repetition is the verdict, as before.
        ring = _loop(white_out=-150)

        ingest.backup_backed_evals([ring[0].key])

        self.assertEqual([value for value, _move in _published(ring)],
                         [0] * 4)
        self.assertEqual(_published(ring)[0], (0, 'd6f6'))
        self.assertEqual(ingest.repetition_moves(ring[0]), {'d6f6'})


class TieBreakTests(TestCase):
    """The rule on its own: a returning move loses ties, and only ties."""

    @staticmethod
    def _child(move, value, quality, returns=False):
        child = ingest._ChildValue(move, value, quality, 1)
        child.returns = returns
        return child

    def test_a_returning_move_loses_a_tie_whatever_its_weight(self):
        for stm_white in (True, False):
            loop = self._child('loop', 1199, 6 * ENGINE, returns=True)
            out = self._child('out', 1199, SHALLOW)

            self.assertIs(ingest._best_child([loop, out], stm_white), out)
            self.assertIs(ingest._best_child([out, loop], stm_white), out)

    def test_a_returning_move_still_wins_when_it_is_strictly_better(self):
        # moky's Rf5+: the move into the loop is the only way to +1163, the
        # way out on the spot is worth +916.
        loop = self._child('loop', 1163, ENGINE, returns=True)
        out = self._child('out', 916, 6 * ENGINE)
        self.assertIs(ingest._best_child([loop, out], True), loop)
        # And for the side that wants the draw, going back in is its move.
        draw = self._child('back', 0, 0, returns=True)
        loss = self._child('out', 1444, ENGINE)
        self.assertIs(ingest._best_child([loss, draw], False), draw)

    def test_the_negamax_stands_on_the_way_out(self):
        row = types.SimpleNamespace(
            key='x', fen=START, status='UNKNOWN', expanded=True,
            eval_cp=1118, nodes_invested=ENGINE, best_move='d6h6')
        loop = self._child('d6f6', 1199, ENGINE, returns=True)
        out = self._child('d6h6', 1199, SHALLOW)

        value, move, _plies, quality = ingest._backed_for(row, [loop, out])

        self.assertEqual((value, move, quality), (1199, 'd6h6', SHALLOW))
