"""A loop of pointers can protect itself through the positions around it.

After #69 went live, a `recascade_backed --keys` over cz_eclipsia's loop
(909727fb: Rd6-f6 Rf8-d8 Rf6-d6 Rd8-f8) left the four positions at 1208,
each still standing on the next.  The positions AROUND the loop were free to
stand on it: for them the loop's move is no repetition, it only ties with a
move to a real source and carries more search.  Once they drained into the
loop, every move of equal value at the loop's own positions ran back into it
through them, so no position of the loop had a way out that does not come
back and the loop rested.

The fixture is that shape, small.  The loop W1-B1-W2-B2 is worth +12, and so
is the way out each White position has: through a Black position (X1, X2)
and a White one (Y1, Y2) that can either take a source worth +12 (S1, S2)
or go back into the loop, which is the heavier of the two.  The stored state
is the one production rested in: everybody on the loop, Y1 and Y2 included.
"""

import hashlib
import os
import tempfile
from io import StringIO

from django.core.management import call_command

from . import ingest
from .models import Edge, Position
from .testing import TestCase

LOOP_NODES = 768_000_000
NODES = 128_000_000
LIGHT = 32_000_000
VALUE = 1208
FEN = '4k3/8/8/8/8/8/8/4K3 {} - - 0 1'


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm, **kw):
    return Position.objects.create(key=_key(name), fen=FEN.format(stm), **kw)


def _edge(parent, child, move):
    Edge.objects.create(parent=parent, move_uci=move, child=child)


def _basin(name):
    """The loop, the two drains around it and the sources they ignore."""
    def node(tag, stm, move, nodes=NODES, value=VALUE):
        return _pos(f'{name}-{tag}', stm, expanded=True, eval_cp=value,
                    nodes_invested=nodes, backed_eval=value,
                    backed_move=move, backed_plies=4, backed_nodes=nodes)

    w1 = node('W1', 'w', 'l1a1', LOOP_NODES)
    b1 = node('B1', 'b', 'l2a2', LOOP_NODES)
    w2 = node('W2', 'w', 'l3a3', LOOP_NODES)
    b2 = node('B2', 'b', 'l4a4', LOOP_NODES)
    _edge(w1, b1, 'l1a1')
    _edge(b1, w2, 'l2a2')
    _edge(w2, b2, 'l3a3')
    _edge(b2, w1, 'l4a4')
    # Black's own ways out are worse for Black than going round.
    _edge(b1, _pos(f'{name}-E1', 'w', eval_cp=VALUE + 200,
                   nodes_invested=NODES), 'e1e1')
    _edge(b2, _pos(f'{name}-E2', 'w', eval_cp=VALUE + 200,
                   nodes_invested=NODES), 'e2e2')
    for index, (white, black) in enumerate(((w1, b1), (w2, b2)), 1):
        x = node(f'X{index}', 'b', f'y{index}y{index}')
        y = node(f'Y{index}', 'w', f'd{index}d{index}')
        source = _pos(f'{name}-S{index}', 'b', eval_cp=VALUE,
                      nodes_invested=LIGHT)
        _edge(white, x, f'x{index}x{index}')
        _edge(x, y, f'y{index}y{index}')
        _edge(y, black, f'd{index}d{index}')       # the drain
        _edge(y, source, f's{index}s{index}')      # the source
    return [w1, b1, w2, b2]


def _loops(keys):
    """Positions of ``keys`` whose chain of pointers comes back to itself."""
    kid = {(p, m): c for p, m, c in Edge.objects.values_list(
        'parent_id', 'move_uci', 'child_id')}
    moves = dict(Position.objects.values_list('key', 'backed_move'))
    looping = set()
    for start in keys:
        path, node = [], start
        while node is not None and node not in path:
            path.append(node)
            move = moves.get(node)
            node = kid.get((node, move)) if move else None
        if node is not None:
            looping.update(path[path.index(node):])
    return looping


class SelfProtectingLoopTests(TestCase):

    def test_the_loop_opens_once_the_positions_around_it_drain_to_a_source(self):
        ring = _basin('BASIN')
        everybody = list(Position.objects.values_list('key', flat=True))

        for _ in range(3):
            ingest.backup_backed_evals(everybody)

        self.assertEqual(ingest.backup_backed_evals(everybody), 0)
        self.assertFalse(_loops([node.key for node in ring]))
        # Values do not move: every position is still worth the +12 the
        # sources back.
        self.assertEqual(
            sorted(set(Position.objects.filter(key__in=[n.key for n in ring])
                       .values_list('backed_eval', flat=True))), [VALUE])

    def test_a_drain_prefers_the_source_to_the_loop_at_the_same_value(self):
        _basin('DRAIN')

        ingest.backup_backed_evals([_key('DRAIN-Y1'), _key('DRAIN-Y2')])

        for tag in ('Y1', 'Y2'):
            row = Position.objects.get(key=_key(f'DRAIN-{tag}'))
            self.assertEqual((row.backed_eval, row.backed_move),
                             (VALUE, f's{tag[1]}s{tag[1]}'), tag)


class FamilyRepairTests(TestCase):
    """`recascade_backed --keys-file --until-rest`: a family, to its rest."""

    def test_the_family_comes_to_rest_from_a_file_of_keys(self):
        ring = _basin('FILE')
        keys = sorted(Position.objects.filter(key__in=[
            _key(f'FILE-{tag}') for tag in ('W1', 'B1', 'W2', 'B2', 'X1',
                                            'Y1', 'X2', 'Y2')])
            .values_list('key', flat=True))
        out = StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'family.txt')
            with open(path, 'w') as handle:
                handle.write('\n'.join(keys[:5]) + '\n' + ','.join(keys[5:]))
            call_command('recascade_backed', keys_file=path, until_rest=True,
                         stdout=out)

        self.assertIn('desde 8 claves', out.getvalue())
        self.assertIn('punto fijo', out.getvalue())
        self.assertFalse(_loops([node.key for node in ring]))

    def test_without_until_rest_it_is_still_one_pass(self):
        _basin('ONCE')
        out = StringIO()

        call_command('recascade_backed', keys=_key('ONCE-Y1'), stdout=out)

        self.assertEqual(out.getvalue().count('filas cambiadas'), 1)
        self.assertNotIn('punto fijo', out.getvalue())


class GroundingTests(TestCase):
    """The tie-break on its own: source, then loop elsewhere, then return."""

    @staticmethod
    def _child(move, quality, returns=False, circular=False):
        child = ingest._ChildValue(move, VALUE, quality, 1)
        child.returns, child.circular = returns, circular
        return child

    def test_a_source_beats_a_loop_elsewhere_beats_a_return(self):
        for stm_white in (True, False):
            back = self._child('back', 6 * LOOP_NODES, returns=True)
            loop = self._child('loop', LOOP_NODES, circular=True)
            source = self._child('source', LIGHT)

            self.assertIs(ingest._best_child([back, loop, source], stm_white),
                          source)
            self.assertIs(ingest._best_child([back, loop], stm_white), loop)

    def test_a_better_value_still_wins_whatever_its_chain(self):
        loop = ingest._ChildValue('loop', VALUE + 1, LOOP_NODES, 1)
        loop.circular = True
        source = ingest._ChildValue('source', VALUE, LIGHT, 1)
        self.assertIs(ingest._best_child([loop, source], True), loop)
