"""A repetition is a draw where the line RETURNS, not for the whole child.

The 3-Aug rule scored a child at 0 as soon as its best line came back to the
parent that was evaluating it.  That is right about the line and wrong about
the child: on the way back somebody may have a better move than returning,
and that somebody is exactly the side that does not want the draw.

The fixture is moky's position of 15-Sep with the numbers the database held:
after Rf5+ Kg6 White can repeat with Rf6+ or play Rg5+ and keep +1163.
"""

import hashlib

from . import ingest
from .models import DBEvent, Edge, Position
from .testing import TestCase

ENGINE = 128_000_000


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _leaf(name, stm, value):
    return _pos(name, stm, eval_cp=value, nodes_invested=ENGINE)


def _state(*nodes):
    out = []
    for node in nodes:
        node.refresh_from_db()
        out.append((node.backed_eval, node.backed_move))
    return out


class ReturningLineTests(TestCase):

    def _loop(self, name, leave_p2=1163, leave_b=916, loop=1246):
        """P2(w) -f5f6-> A(b) -g6g5-> B(w) -f6f5-> C(b) -g5g6-> P2.

        Every reply is on the table at the four of them.  White may leave the
        loop at P2 (f5g5) or at B (g2g3); Black's ways out lose more than the
        loop does.  The stored state is the one an ascent finds half way: all
        four still quote the number the loop lends itself, each pointing at
        the next.
        """
        def link(tag, stm, move):
            return _pos(f'{name}-{tag}', stm, expanded=True, backed_eval=loop,
                        backed_move=move, backed_plies=4, backed_nodes=ENGINE)

        p2 = link('P2', 'w', 'f5f6')
        a = link('A', 'b', 'g6g5')
        b = link('B', 'w', 'f6f5')
        c = link('C', 'b', 'g5g6')
        _edge(p2, a, 'f5f6')
        _edge(a, b, 'g6g5')
        _edge(b, c, 'f6f5')
        _edge(c, p2, 'g5g6')
        _edge(p2, _leaf(f'{name}-leaveP2', 'b', leave_p2), 'f5g5')
        _edge(b, _leaf(f'{name}-leaveB', 'b', leave_b), 'g2g3')
        _edge(a, _leaf(f'{name}-A-other', 'w', 1235), 'g6h5')
        _edge(c, _leaf(f'{name}-C-other', 'w', 1300), 'g5h6')
        return p2, a, b, c

    def test_the_winning_side_keeps_the_move_that_can_leave_the_loop(self):
        # "It thinks it is a bad line because of the lowered eval but it is
        # actually a good line."  f6f5 does lead back to B if everybody keeps
        # repeating; White does not have to, and two plies later plays f5g5.
        _p2, _a, b, _c = self._loop('WIN')

        ingest.backup_backed_evals([b.key], max_plies=1)

        self.assertEqual(_state(b), [(1163, 'f6f5')])

    def test_the_losing_side_is_not_handed_a_draw_the_other_can_refuse(self):
        # "Parent shows +-0cp but it is actually +11 ... it thinks there is a
        # forced draw under there which there isn't."  Black to move and every
        # reply known: going back to P2 is a draw only if White then agrees to
        # repeat, and White is the one who is eleven pawns up.
        _p2, _a, _b, c = self._loop('LOSE')

        ingest.backup_backed_evals([c.key], max_plies=1)

        self.assertEqual(_state(c), [(1163, 'g5g6')])

    def test_a_loop_nobody_can_leave_is_still_a_draw(self):
        # The rule of 3-Aug, untouched: when the side that would like more has
        # nothing better than 0 anywhere on the way back, repeating is the
        # verdict.  It carries no search weight and says it is a repetition.
        _p2, _a, b, _c = self._loop('DRAW', leave_p2=-200, leave_b=-300)

        ingest.backup_backed_evals([b.key], max_plies=1)

        b.refresh_from_db()
        self.assertEqual((b.backed_eval, b.backed_move, b.backed_nodes),
                         (0, 'f6f5', 0))
        self.assertEqual(ingest.repetition_moves(b), {'f6f5'})

    def test_one_touch_settles_the_whole_loop(self):
        # No lap of corrections any more: wherever the ascent starts, the four
        # positions end on the value of the move that leaves the loop.
        for name, pick in (('LAP-B', 2), ('LAP-C', 3), ('LAP-P2', 0)):
            nodes = self._loop(name)

            ingest.backup_backed_evals([nodes[pick].key])

            self.assertEqual([value for value, _move in _state(*nodes)],
                             [1163] * 4, name)
            self.assertEqual(_state(nodes[0]), [(1163, 'f5g5')], name)

    def test_out_of_budget_the_walk_keeps_its_old_verdict(self):
        # The valuation re-reads a handful of positions.  With no budget left
        # it claims nothing new: the child is worth the draw, as before.
        _p2, _a, b, _c = self._loop('BUDGET')
        saved = ingest.BACKED_CYCLE_MAX_NODES
        ingest.BACKED_CYCLE_MAX_NODES = 0
        try:
            ingest.backup_backed_evals([b.key], max_plies=1)
        finally:
            ingest.BACKED_CYCLE_MAX_NODES = saved

        self.assertEqual(_state(b), [(916, 'g2g3')])


class NestedLoopTests(TestCase):
    """Two loops that share two positions, with the numbers production held.

    moky's position again, with the second loop the king has: after Rf5+ Black
    may go Kg6 (P2) or Kh6 (Q), and from both White can check on f6 and be
    back at B after ...Kg5.  White leaves either loop for the same +1163.

    That is the shape the flat zero could not settle.  The value going round a
    loop IS the value of its exit, so the exit and the loop tie; the tie goes
    to the heavier support, which is the loop (it inherits the 2.6B nodes of
    P2); the loop is then scored 0 from the position that closes it, the zero
    lowers everybody, the exit wins again, and the tie is back.  The revisit
    cap used to freeze that wheel wherever it happened to be: 916 under a
    1163 one day, a 0 on a position eleven pawns up the next.
    """

    HEAVY = 2_640_000_000

    def _nest(self, name, deep_exit=0):
        def node(tag, stm, value, move, **kw):
            return _pos(f'{name}-{tag}', stm, expanded=True, backed_eval=value,
                        backed_move=move, backed_plies=3, backed_nodes=ENGINE,
                        **kw)

        p2 = node('P2', 'w', 1163, 'f5g5', eval_cp=1265,
                  nodes_invested=self.HEAVY)
        q = node('Q', 'w', 1163, 'f5h5', eval_cp=1092, nodes_invested=ENGINE)
        c = node('C', 'b', 1163, 'g5g6', eval_cp=1079, nodes_invested=ENGINE)
        b = node('B', 'w', 1163, 'f6f5', eval_cp=1083, nodes_invested=ENGINE)
        # The two positions production had left behind, on the value B held
        # while the wheel was in its low phase.
        a = node('A', 'b', 916, 'g6g5', eval_cp=962, nodes_invested=ENGINE)
        e = node('E', 'b', 916, 'h6g5', eval_cp=1185, nodes_invested=ENGINE)
        _edge(p2, a, 'f5f6')
        _edge(a, b, 'g6g5')
        _edge(b, c, 'f6f5')
        _edge(c, p2, 'g5g6')
        _edge(c, q, 'g5h6')
        _edge(q, e, 'f5f6')
        _edge(e, b, 'h6g5')
        _edge(p2, _leaf(f'{name}-leaveP2', 'b', 1163), 'f5g5')
        # Q's way out may be a long line of forced replies: the same value,
        # many plies further away than the road through the loop.
        upper, colour = q, 'w'
        for ply in range(deep_exit):
            colour = 'b' if colour == 'w' else 'w'
            lower = _pos(f'{name}-deep{ply}', colour, expanded=True)
            _edge(upper, lower, 'f5h5' if upper is q else 'a1a1')
            upper = lower
        _edge(upper, _leaf(f'{name}-leaveQ', 'b', 1163),
              'f5h5' if upper is q else 'a1a1')
        _edge(b, _leaf(f'{name}-leaveB', 'b', 916), 'g2g3')
        _edge(a, _leaf(f'{name}-A-other', 'w', 1235), 'g6h5')
        _edge(e, _leaf(f'{name}-E-other', 'w', 1300), 'h6h7')
        return p2, q, c, b, a, e

    def test_an_ascent_through_a_nest_of_loops_comes_to_rest(self):
        nodes = self._nest('NEST')
        everybody = [node.key for node in nodes]

        ingest.backup_backed_evals([nodes[4].key, nodes[5].key])

        # Nothing had to stop the ascent: it ran out of changes by itself.
        self.assertFalse(DBEvent.objects.filter(kind='BACKED_GUARD').exists())
        # Every position is worth what leaving the loop is worth, nobody is
        # on White's second move and nobody was handed a draw.
        self.assertEqual([value for value, _move in _state(*nodes)],
                         [1163] * 6)
        self.assertEqual(_state(nodes[3]), [(1163, 'f6f5')])
        # And it IS at rest, which is the stop condition ``recascade_backed``
        # could never meet on this family.  The first look only settles the
        # bookkeeping this fixture left loose (plies and support of the rows
        # the ascent had no reason to touch); after it, nothing moves.
        ingest.backup_backed_evals(everybody)
        for _ in range(3):
            self.assertEqual(ingest.backup_backed_evals(everybody), 0)
        self.assertEqual([value for value, _move in _state(*nodes)],
                         [1163] * 6)
        self.assertFalse(DBEvent.objects.filter(kind='BACKED_GUARD').exists())


    def test_the_rest_does_not_depend_on_how_ties_are_broken(self):
        # Breaking the tie towards the shallower chain would stop the wheel
        # above, where both ways out are one ply away.  It only moves the
        # problem: here Q's own way out is eight plies deep, the road through
        # the loop to P2's way out is the shallower one, and it is chosen
        # again.  What ends the wheel is that the loop is worth 916 to Q on
        # the line that returns, so there is no tie to break.
        nodes = self._nest('DEEP', deep_exit=8)
        everybody = list(Position.objects.values_list('key', flat=True))

        ingest.backup_backed_evals(everybody)     # the long line fills in
        ingest.backup_backed_evals(everybody)

        for _ in range(3):
            self.assertEqual(ingest.backup_backed_evals(everybody), 0)
        self.assertEqual([value for value, _move in _state(*nodes)],
                         [1163] * 6)
        self.assertFalse(DBEvent.objects.filter(kind='BACKED_GUARD').exists())


class MoverDrawTests(TestCase):
    """The draw belongs to whoever can play the repeating move.

    opabinia's position of 8-Sep with the numbers the database held: White's
    best line shuffles the bishop (a3b4, then b4a3), Black shuffles the queen
    (b6d8, then d8b6) and the four positions quote +86 to each other.  "The
    propagation doesn't take advantage of the fact that repetitions lead to
    draws for the side that wants a draw."
    """

    def _shuffle(self, name):
        def link(tag, stm, move, **kw):
            return _pos(f'{name}-{tag}', stm, backed_eval=86, backed_move=move,
                        backed_plies=2, backed_nodes=ENGINE, **kw)

        x0 = link('X0', 'w', 'a3b4', expanded=True)
        x1 = link('X1', 'b', 'b6d8', expanded=True)
        x2 = link('X2', 'w', 'b4a3', expanded=True)
        # The position that closes the loop was searched (86, through a6b5)
        # and still has replies nobody opened.
        x3 = _pos(f'{name}-X3', 'b', eval_cp=86, nodes_invested=ENGINE,
                  backed_eval=86, backed_nodes=ENGINE)
        _edge(x0, x1, 'a3b4')
        _edge(x1, x2, 'b6d8')
        _edge(x2, x3, 'b4a3')
        _edge(x3, x0, 'd8b6')
        _edge(x3, _pos(f'{name}-a6b5', 'w', eval_cp=86), 'a6b5')
        # What White has when the shuffle is not there to be repeated.
        _edge(x0, _leaf(f'{name}-f1g2', 'b', 68), 'f1g2')
        _edge(x2, _leaf(f'{name}-b5b6', 'b', 22), 'b5b6')
        return x0, x1, x2, x3

    def test_the_side_that_wants_the_draw_makes_the_other_leave_the_loop(self):
        nodes = self._shuffle('OPAB')
        x0, _x1, _x2, x3 = nodes

        ingest.backup_backed_evals([x3.key])

        # Black goes back with the queen; White cannot shuffle again without
        # repeating, so the whole family is worth White's best OTHER move.
        self.assertEqual([value for value, _move in _state(*nodes)], [68] * 4)
        self.assertEqual(_state(x0), [(68, 'f1g2')])
        self.assertEqual(_state(x3), [(68, 'd8b6')])
        # One number per edge: the header of X0 is what its best row says.
        self.assertEqual(ingest.backup_backed_evals(
            [node.key for node in nodes]), 0)


class OpenRingOrderTests(TestCase):
    """What a ring publishes cannot depend on where the ascent entered it."""

    def _ring(self, name):
        """A(w) -e6c5-> C(b) -x1x1-> D(w) -y1y1-> A, nobody fully opened.

        C carries the only search of the three (898 at 128M) and a backed 903
        that it borrows from the ring.
        """
        a = _pos(f'{name}-A', 'w')
        c = _pos(f'{name}-C', 'b', eval_cp=898, nodes_invested=ENGINE,
                 backed_eval=903, backed_move='x1x1', backed_plies=2,
                 backed_nodes=ENGINE)
        d = _pos(f'{name}-D', 'w', backed_eval=903, backed_move='y1y1',
                 backed_plies=1)
        _edge(a, c, 'e6c5')
        _edge(c, d, 'x1x1')
        _edge(d, a, 'y1y1')
        _edge(a, _pos(f'{name}-L', 'b', status='BLACK_WIN',
                      closure='MINIMAX'), 'a2a3')
        return a, c, d

    def _published(self, nodes):
        out = []
        for node in nodes:
            node.refresh_from_db()
            out.append(ingest.best_known_eval(node))
        return out

    def test_entering_from_the_top_or_from_inside_publishes_the_same(self):
        top = self._ring('TOP')
        inside = self._ring('INSIDE')

        ingest.backup_backed_evals([top[0].key])        # seeded at A
        ingest.backup_backed_evals([inside[1].key])     # seeded at C

        # The one number anybody measured, by the edge it came through.
        self.assertEqual(self._published(inside), [898, 898, 898])
        self.assertEqual(self._published(top), [898, 898, 898])
        self.assertEqual(top[0].backed_move, 'e6c5')
        self.assertEqual(inside[0].backed_move, 'e6c5')
