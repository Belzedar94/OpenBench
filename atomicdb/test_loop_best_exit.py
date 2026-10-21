"""A loop of pointers is worth its best exit (owner, 21-Oct-2026).

After #69 and #70 went live, three loops of ``backed_move`` pointers in the
rook ending below 795ffd96 (cz_eclipsia's family: 16 positions on the loops,
34 more draining into them) held 1208, more than any move that leaves them
brings.  With no tie to break, the rule of #69/#70 left them standing, and
every recascade added plies, each position counting its distance through the
next one, round and round (about 2,110 by then).

The rule: when a level is about to write a loop of pointers at a value other
than 0 and no position of the level can open it for the same value, the moves
that keep play inside the loop bring no value of their own.  Every position
chooses between its best way out and going on round, a full lap is the
repetition (0), and the distance is counted from the way out.

Most fixtures store what a family keeps when its ways out were searched again
lower and nothing has recomputed the loop since: 1208 everywhere, far away,
on moves that the level then turns into a loop.  ``max_plies=1`` looks at
what one level writes, which is where the rule lives.
"""

import hashlib
import os
import tempfile
import time
import types
from io import StringIO
from unittest import mock

from django.core.management import call_command

from . import ingest
from .models import DBEvent, Edge, Position
from .testing import TestCase

ENGINE = 128_000_000
HEAVY = 768_000_000
STALE = 1208
FAR = 2110
FEN = '4k3/8/8/8/8/8/8/4K3 {} - - 0 1'


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _keys(*names):
    return [_key(name) for name in names]


def _pos(name, stm, **kw):
    return Position.objects.create(key=_key(name), fen=FEN.format(stm), **kw)


def _leaf(name, stm, value, nodes=ENGINE, **kw):
    return _pos(name, stm, eval_cp=value, nodes_invested=nodes, **kw)


def _edge(parent, child, move):
    Edge.objects.create(parent=parent, move_uci=move, child=child)


def _node(name, stm, move, value=STALE, plies=FAR):
    """A searched position with every reply on the table, as stored."""
    return _pos(name, stm, expanded=True, eval_cp=value,
                nodes_invested=ENGINE, backed_eval=value, backed_move=move,
                backed_plies=plies, backed_nodes=HEAVY)


def _state(*names):
    out = []
    for name in names:
        row = Position.objects.get(key=_key(name))
        out.append((row.backed_eval, row.backed_move, row.backed_plies,
                    row.backed_nodes))
    return out


def _moves(*names):
    """``(value, move, plies)`` of each position."""
    return [state[:3] for state in _state(*names)]


def _values(*names):
    return [state[0] for state in _state(*names)]


def _loops():
    """Positions whose chain of pointers comes back to them."""
    kid = {(p, m): c for p, m, c in Edge.objects.values_list(
        'parent_id', 'move_uci', 'child_id')}
    moves = dict(Position.objects.values_list('key', 'backed_move'))
    looping = set()
    for start in moves:
        path, node = [], start
        while node is not None and node not in path:
            path.append(node)
            move = moves.get(node)
            node = kid.get((node, move)) if move else None
        if node is not None:
            looping.update(path[path.index(node):])
    return looping


def _without_the_rule():
    """The backup as it was before 21-Oct-2026."""
    return mock.patch.object(ingest, '_loop_at_best_exit', return_value=None)


def _the_rule_must_not_run():
    return mock.patch.object(ingest, '_loop_at_best_exit',
                             side_effect=AssertionError('the rule ran'))


def _two_cycle(name, white_out=1100, black_out=1300, on_loop=False):
    """W (White) and B (Black) answer each other; each has one way out.

    Stored at 1208, either on their ways out (the default) or already round
    the loop (``on_loop``), as production kept it.
    """
    w = _node(f'{name}-W', 'w', 'wb' if on_loop else 'wx')
    b = _node(f'{name}-B', 'b', 'bw' if on_loop else 'bx')
    _edge(w, b, 'wb')
    _edge(b, w, 'bw')
    _edge(w, _leaf(f'{name}-WX', 'b', white_out), 'wx')
    _edge(b, _leaf(f'{name}-BX', 'w', black_out), 'bx')
    return w, b


def _four_cycle(name, value=1199, outs=(1150, 1400, 1199, 1450)):
    """W1 -> B1 -> W2 -> B2 -> W1, stored round the loop, far away.

    cz_eclipsia's shape: White leaves at W2 for what the loop is worth.
    """
    ring = [_node(f'{name}-{tag}', stm, f'k{index + 1}', value=value)
            for index, (tag, stm) in enumerate((('W1', 'w'), ('B1', 'b'),
                                                ('W2', 'w'), ('B2', 'b')))]
    for index, node in enumerate(ring):
        _edge(node, ring[(index + 1) % 4], f'k{index + 1}')
        _edge(node, _leaf(f'{name}-O{index + 1}', 'b' if index % 2 == 0
                          else 'w', outs[index]), f'o{index + 1}')
    return ring


class AboveEveryExitTests(TestCase):
    """The reported shape: a loop that holds more than any way out brings."""

    def test_a_two_cycle_becomes_its_best_exit(self):
        _two_cycle('TWO')

        ingest.backup_backed_evals(_keys('TWO-W', 'TWO-B'), max_plies=1)

        # White leaves at once for what its way out brings; Black, whose own
        # way out is worse for Black, goes back to White.  The distance is
        # counted from the way out, and the support is the way out's.
        self.assertEqual(_state('TWO-W', 'TWO-B'),
                         [(1100, 'wx', 1, ENGINE), (1100, 'bw', 2, ENGINE)])
        self.assertFalse(_loops())

    def test_before_the_rule_the_level_wrote_the_loop_at_its_own_number(self):
        _two_cycle('OLD')

        with _without_the_rule():
            ingest.backup_backed_evals(_keys('OLD-W', 'OLD-B'), max_plies=1)

        # The defect: each position read the other's stale 1208 and stood on
        # it, every way out was lower, no tie was there to break, and each
        # one counted its distance through the other's.
        self.assertEqual(_moves('OLD-W', 'OLD-B'),
                         [(STALE, 'wb', FAR + 1), (STALE, 'bw', FAR + 1)])
        self.assertEqual(_loops(), set(_keys('OLD-W', 'OLD-B')))

    def test_a_three_cycle_takes_the_best_of_its_exits(self):
        # P1 (White) -> P2 (Black) -> P3 (White) -> P1.  White can leave at
        # P1 for 1050 or at P3 for 1150; Black's way out is worse for Black.
        p1 = _node('THREE-P1', 'w', 'x1')
        p2 = _node('THREE-P2', 'b', 'x2')
        p3 = _node('THREE-P3', 'w', 'x3')
        _edge(p1, p2, 'a1')
        _edge(p2, p3, 'a2')
        _edge(p3, p1, 'a3')
        _edge(p1, _leaf('THREE-X1', 'b', 1050), 'x1')
        _edge(p2, _leaf('THREE-X2', 'w', 1400), 'x2')
        _edge(p3, _leaf('THREE-X3', 'b', 1150), 'x3')
        names = ('THREE-P1', 'THREE-P2', 'THREE-P3')

        ingest.backup_backed_evals(_keys(*names), max_plies=1)

        # The best exit, and a line that goes round to it.
        self.assertEqual(_moves(*names),
                         [(1150, 'a1', 3), (1150, 'a2', 2), (1150, 'x3', 1)])
        self.assertFalse(_loops())

    def test_the_family_rests_at_the_best_exit(self):
        _two_cycle('REST', on_loop=True)
        keys = _keys('REST-W', 'REST-B')

        ingest.backup_backed_evals(keys)

        self.assertEqual(_values('REST-W', 'REST-B'), [1100, 1100])
        for _ in range(3):
            self.assertEqual(ingest.backup_backed_evals(keys), 0)


class SideToMoveTests(TestCase):
    """Values stay White's; each position's own FEN says who chooses."""

    def test_the_mirror_case_is_black_leaving(self):
        # The loop holds -1208, Black's way out brings -1100, and White's way
        # out is worse for White than that.
        _two_cycle('MIRROR', white_out=-1300, black_out=-1100)
        Position.objects.filter(key__in=_keys('MIRROR-W', 'MIRROR-B')).update(
            backed_eval=-STALE, eval_cp=-STALE)

        ingest.backup_backed_evals(_keys('MIRROR-W', 'MIRROR-B'),
                                   max_plies=1)

        self.assertEqual(_moves('MIRROR-W', 'MIRROR-B'),
                         [(-1100, 'wb', 2), (-1100, 'bx', 1)])

    def test_sides_need_not_alternate_round_the_loop(self):
        # Two White positions in a row and one Black one, each choosing for
        # its own side.  White's best way out (1150 at W2) wins; Black's
        # (1400) is worse for Black than letting White leave.
        w1 = _node('ODD-W1', 'w', 'o1')
        w2 = _node('ODD-W2', 'w', 'o2')
        b = _node('ODD-B', 'b', 'o3')
        _edge(w1, w2, 'w1w2')
        _edge(w2, b, 'w2b')
        _edge(b, w1, 'bw1')
        _edge(w1, _leaf('ODD-O1', 'w', 1000), 'o1')
        _edge(w2, _leaf('ODD-O2', 'b', 1150), 'o2')
        _edge(b, _leaf('ODD-O3', 'w', 1400), 'o3')

        ingest.backup_backed_evals(_keys('ODD-W1', 'ODD-W2', 'ODD-B'),
                                   max_plies=1)

        self.assertEqual(_moves('ODD-W1', 'ODD-W2', 'ODD-B'),
                         [(1150, 'w1w2', 2), (1150, 'o2', 1),
                          (1150, 'bw1', 3)])


class NoWayOutTests(TestCase):

    def test_a_loop_nobody_wants_to_leave_is_a_draw(self):
        # White's way out loses, Black's loses more: going round is the best
        # either side has, so the loop is the repetition, worth 0 with no
        # search weight, it shows as one, and the question is bought.
        _two_cycle('DRAW', white_out=-150, black_out=1500)

        ingest.backup_backed_evals(_keys('DRAW-W', 'DRAW-B'), max_plies=1)

        self.assertEqual(_state('DRAW-W', 'DRAW-B'),
                         [(0, 'wb', 1, 0), (0, 'bw', 1, 0)])
        w = Position.objects.get(key=_key('DRAW-W'))
        self.assertEqual(ingest.repetition_moves(w), {'wb'})
        self.assertTrue(DBEvent.objects.filter(
            kind='CYCLE_DISAMBIGUATION').exists())

    def test_a_loop_with_no_move_out_at_all_is_a_draw(self):
        ring = [_node(f'SHUT-{n}', stm, None)
                for n, stm in enumerate(('w', 'b', 'w', 'b'))]
        for index, node in enumerate(ring):
            _edge(node, ring[(index + 1) % 4], f'm{index}')
        members = [node.key for node in ring]
        cache = ingest._SpineCache()
        for index, key in enumerate(members):
            cache.wrote(key, f'm{index}')

        worth = ingest._loop_at_best_exit(members, {}, {}, cache,
                                          time.monotonic() + 5)

        self.assertEqual([worth[key] for key in members],
                         [(0, f'm{index}', 1, 0) for index in range(4)])


class DrainTests(TestCase):

    @staticmethod
    def _sink(name):
        w, _b = _two_cycle(name)
        d1 = _node(f'{name}-D1', 'b', 'd1')
        d2 = _node(f'{name}-D2', 'w', 'd2')
        _edge(d1, w, 'd1')
        _edge(d2, d1, 'd2')

    def test_the_rule_writes_the_loop_and_nothing_that_drains_into_it(self):
        self._sink('SINK1')

        ingest.backup_backed_evals(_keys('SINK1-W', 'SINK1-B'), max_plies=1)

        self.assertEqual(_values('SINK1-W', 'SINK1-B'), [1100, 1100])
        self.assertEqual(_values('SINK1-D1', 'SINK1-D2'), [STALE, STALE])

    def test_what_drains_into_the_loop_follows_through_the_backup(self):
        self._sink('SINK2')

        ingest.backup_backed_evals(_keys('SINK2-W', 'SINK2-B'))

        self.assertEqual(_moves('SINK2-D1', 'SINK2-D2'),
                         [(1100, 'd1', 2), (1100, 'd2', 3)])


class OverlappingLoopTests(TestCase):

    def test_a_loop_sharing_a_position_with_another_is_valued_through_it(self):
        # A (White) and B (Black) answer each other, and A also goes round a
        # second loop through C (Black) and D (White).  White's best way out
        # is D's, on the second loop; the loop of pointers is A-B.
        a = _node('EIGHT-A', 'w', 'ab', plies=4)
        b = _node('EIGHT-B', 'b', 'ba', plies=4)
        c = _node('EIGHT-C', 'b', 'cd', plies=4)
        d = _node('EIGHT-D', 'w', 'da', plies=4)
        _edge(a, b, 'ab')
        _edge(b, a, 'ba')
        _edge(a, c, 'ac')
        _edge(c, d, 'cd')
        _edge(d, a, 'da')
        _edge(a, _leaf('EIGHT-AX', 'b', 1050), 'ax')
        _edge(b, _leaf('EIGHT-BX', 'w', 1400), 'bx')
        _edge(c, _leaf('EIGHT-CX', 'w', 1300), 'cx')
        _edge(d, _leaf('EIGHT-DX', 'b', 1150), 'dx')
        names = ('EIGHT-A', 'EIGHT-B', 'EIGHT-C', 'EIGHT-D')

        for _ in range(3):
            ingest.backup_backed_evals(_keys(*names))

        self.assertEqual(ingest.backup_backed_evals(_keys(*names)), 0)
        self.assertEqual(_moves(*names),
                         [(1150, 'ac', 3), (1150, 'ba', 4),
                          (1150, 'cd', 2), (1150, 'dx', 1)])
        self.assertFalse(_loops())

    def test_two_loops_that_feed_each_other_rest_at_the_best_exit_of_both(self):
        # Each loop has a way out into the other, at the number the other
        # stores.  White's best real way out is 1150, on the second loop.
        w1, b1 = _two_cycle('PAIR1', white_out=1100, black_out=1300,
                            on_loop=True)
        w2, b2 = _two_cycle('PAIR2', white_out=1150, black_out=1350,
                            on_loop=True)
        _edge(w1, b2, 'w1b2')
        _edge(w2, b1, 'w2b1')
        names = ('PAIR1-W', 'PAIR1-B', 'PAIR2-W', 'PAIR2-B')

        ingest.backup_backed_evals(_keys(*names), max_plies=1)

        # One level is enough: the loop the level closes runs through both.
        self.assertEqual(_values(*names), [1150] * 4)
        for _ in range(3):
            ingest.backup_backed_evals(_keys(*names))
        self.assertEqual(ingest.backup_backed_evals(_keys(*names)), 0)
        self.assertEqual(_values(*names), [1150] * 4)
        self.assertFalse(_loops())

    def test_a_way_back_is_no_way_out_and_its_own_exit_comes_through(self):
        # W's only move out, X, leads to Y, which can leave for 1100 or go
        # back into the loop, and stands on going back at the stale 1208.
        # Its number is the loop's own: it keeps play inside, so the loop by
        # itself has no way out White wants, and is a draw.
        w = _node('ECHO-W', 'w', 'wb')
        b = _node('ECHO-B', 'b', 'bw')
        x = _node('ECHO-X', 'b', 'xy')
        y = _node('ECHO-Y', 'w', 'yb')
        _edge(w, b, 'wb')
        _edge(b, w, 'bw')
        _edge(w, x, 'wx')
        _edge(x, y, 'xy')
        _edge(y, b, 'yb')
        _edge(y, _leaf('ECHO-S', 'b', 1100), 'ys')
        _edge(b, _leaf('ECHO-BX', 'w', 1400), 'bx')
        cache = ingest._SpineCache()
        cache.wrote(w.key, 'wb')
        cache.wrote(b.key, 'bw')

        worth = ingest._loop_at_best_exit([w.key, b.key], {}, {}, cache,
                                          time.monotonic() + 5)

        self.assertEqual(worth[w.key], (0, 'wb', 1, 0))
        self.assertEqual(worth[b.key], (0, 'bw', 1, 0))
        # The backup takes it from there: Y stands on the loop, finds its
        # own exit, and the line through X is White's way out at 1100.
        for _ in range(3):
            ingest.backup_backed_evals(_keys('ECHO-W', 'ECHO-B', 'ECHO-X',
                                             'ECHO-Y'))
        self.assertEqual(ingest.backup_backed_evals(
            _keys('ECHO-W', 'ECHO-B', 'ECHO-X', 'ECHO-Y')), 0)
        self.assertEqual(_moves('ECHO-W', 'ECHO-B', 'ECHO-X', 'ECHO-Y'),
                         [(1100, 'wx', 3), (1100, 'bw', 4), (1100, 'xy', 2),
                          (1100, 'ys', 1)])
        self.assertFalse(_loops())


class PliesTests(TestCase):

    def test_the_distance_is_counted_from_the_exit_and_does_not_grow(self):
        _two_cycle('FAR', on_loop=True)
        d = _node('FAR-D', 'b', 'dw')
        _edge(d, Position.objects.get(key=_key('FAR-W')), 'dw')
        names = ('FAR-W', 'FAR-B', 'FAR-D')
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'family.txt')
            with open(path, 'w') as handle:
                handle.write('\n'.join(_keys(*names)))
            for _ in range(3):
                out = StringIO()
                call_command('recascade_backed', keys_file=path,
                             until_rest=True, stdout=out)
                self.assertIn('punto fijo', out.getvalue())
                self.assertEqual(_moves(*names),
                                 [(1100, 'wx', 1), (1100, 'bw', 2),
                                  (1100, 'dw', 2)])

    def test_a_loop_kept_at_its_value_counts_from_the_exit(self):
        # The loop is worth what White gets at W2, which is not in the
        # level: B1 goes on round, and counts from W2's way out.
        _four_cycle('KEEP')

        ingest.backup_backed_evals([_key('KEEP-B1')], max_plies=1)

        self.assertEqual(_state('KEEP-B1'), [(1199, 'k2', 2, ENGINE)])


class UntouchedTests(TestCase):
    """What the rule must leave exactly as it was."""

    def test_positions_off_a_loop_back_up_as_before(self):
        # A DAG with a transposition, a partly covered position with a
        # showcase bound, and a proven reply: no loop anywhere, so the rule
        # never runs and the backup is what it always was.
        root = _pos('DAG-R', 'w', expanded=True, eval_cp=40,
                    nodes_invested=ENGINE)
        left = _pos('DAG-L', 'b', expanded=True, eval_cp=35,
                    nodes_invested=ENGINE)
        right = _pos('DAG-RR', 'b', eval_cp=60, nodes_invested=ENGINE,
                     last_analysis=[{'move': 'q1', 'eval_cp': 60},
                                    {'move': 'q2', 'eval_cp': 20}])
        meet = _pos('DAG-M', 'w', eval_cp=55, nodes_invested=ENGINE)
        _edge(root, left, 'r1')
        _edge(root, right, 'r2')
        _edge(left, meet, 'l1')
        _edge(left, _pos('DAG-WIN', 'w', status='WHITE_WIN',
                         closure='MINIMAX'), 'l2')
        _edge(right, meet, 'q1')
        _edge(right, _leaf('DAG-Q2', 'w', 20), 'q2')
        everybody = list(Position.objects.values_list('key', flat=True))

        with _the_rule_must_not_run():
            ingest.backup_backed_evals(everybody)

        self.assertEqual(_state('DAG-L', 'DAG-RR', 'DAG-R'),
                         [(55, 'l1', 1, ENGINE), (20, 'q2', 1, ENGINE),
                          (55, 'r1', 2, ENGINE)])

    def test_a_loop_opened_for_the_same_value_is_left_to_part_2(self):
        # Every position of this loop has a way out worth what the loop is
        # worth: #69 opens it, and the rule has nothing to do.
        ring = [_node(f'TIE-{n}', stm, f't{n}', value=1199)
                for n, stm in enumerate(('w', 'b', 'w', 'b'))]
        for index, node in enumerate(ring):
            _edge(node, ring[(index + 1) % 4], f't{index}')
            _edge(node, _leaf(f'TIE-O{index}', 'b' if index % 2 == 0 else 'w',
                              1199, nodes=32_000_000), f'o{index}')
        keys = [node.key for node in ring]

        with _the_rule_must_not_run():
            for _ in range(3):
                ingest.backup_backed_evals(keys)

        self.assertFalse(_loops())
        self.assertEqual(_values(*(f'TIE-{n}' for n in range(4))),
                         [1199] * 4)

    def test_exact_closures_are_never_touched(self):
        # Black's way out is a proven win for White: it counts at its exact
        # value, Black never takes it, and neither it nor a proven position
        # standing above the loop changes in any field.
        w, b = _two_cycle('EXACT')
        won = _pos('EXACT-WIN', 'w', status='WHITE_WIN', closure='MATE_PV',
                   proof='ANDOR', best_move='e1e2', mate_in=7,
                   backed_eval=10_000, backed_move='e1e2',
                   backed_nodes=ingest.PROVEN_QUALITY)
        _edge(b, won, 'bwin')
        above = _pos('EXACT-UP', 'b', status='BLACK_WIN', closure='MINIMAX',
                     best_move='uw', backed_eval=-10_000, backed_move='uw',
                     backed_nodes=ingest.PROVEN_QUALITY)
        _edge(above, w, 'uw')
        fields = ('status', 'closure', 'proof', 'best_move', 'mate_in',
                  'backed_eval', 'backed_move', 'backed_plies',
                  'backed_nodes')
        before = list(Position.objects.filter(
            key__in=[won.key, above.key]).order_by('key').values(*fields))

        ingest.backup_backed_evals(_keys('EXACT-W', 'EXACT-B', 'EXACT-UP'),
                                   max_plies=1)

        self.assertEqual(_moves('EXACT-W', 'EXACT-B'),
                         [(1100, 'wx', 1), (1100, 'bw', 2)])
        after = list(Position.objects.filter(
            key__in=[won.key, above.key]).order_by('key').values(*fields))
        self.assertEqual(after, before)


class BoundTests(TestCase):
    """Past its bounds the rule leaves the loop as it is, as before."""

    def test_no_time_left_leaves_the_loop(self):
        _two_cycle('CLOCK')
        with mock.patch.object(ingest, 'BACKED_LOOP_MAX_SECONDS', 0):
            ingest.backup_backed_evals(_keys('CLOCK-W', 'CLOCK-B'),
                                       max_plies=1)
        self.assertEqual(_values('CLOCK-W', 'CLOCK-B'), [STALE, STALE])

    def test_a_deadline_already_past_reads_nothing(self):
        _two_cycle('LATE', on_loop=True)
        cache = ingest._SpineCache()
        cache.wrote(_key('LATE-W'), 'wb')
        cache.wrote(_key('LATE-B'), 'bw')

        with self.assertNumQueries(0):
            worth = ingest._loop_at_best_exit(
                _keys('LATE-W', 'LATE-B'), {}, {}, cache,
                time.monotonic() - 1)

        self.assertIsNone(worth)

    def test_only_rows_of_the_level_are_written(self):
        # Seeded at B1 alone, the level holds B1: the other positions of the
        # loop are valued, not written, and woken for a look.
        _four_cycle('LEVEL')
        seen = []
        real = ingest._loop_at_best_exit

        def spy(members, by_key, *args, **kwargs):
            seen.append(sorted(by_key))
            return real(members, by_key, *args, **kwargs)

        with mock.patch.object(ingest, '_loop_at_best_exit', spy):
            ingest.backup_backed_evals([_key('LEVEL-B1')], max_plies=1)

        self.assertEqual(seen, [[_key('LEVEL-B1')]])
        self.assertEqual(_moves('LEVEL-W1', 'LEVEL-W2', 'LEVEL-B2'),
                         [(1199, 'k1', FAR), (1199, 'k3', FAR),
                          (1199, 'k4', FAR)])
        ingest.backup_backed_evals([_key('LEVEL-B1')])
        self.assertEqual(_moves('LEVEL-W1', 'LEVEL-B1', 'LEVEL-W2',
                                'LEVEL-B2'),
                         [(1199, 'k1', 3), (1199, 'k2', 2), (1199, 'o3', 1),
                          (1199, 'k4', 4)])
        self.assertFalse(_loops())


def _wheel():
    """Four positions, sides alternating, as a replay of random loop
    families drew them (family 402 of seed 32): N0-N1-N2-N3 round, Black's
    ways out at N1 (-150 and 50) and N3 (-150), and the numbers an old pass
    left behind.  Every position is worth -150.
    """
    stm = {'N0': 'w', 'N1': 'b', 'N2': 'w', 'N3': 'b'}
    kids = {'N0': ['N1'], 'N1': ['N2', 'N1L0', 'N1L1'], 'N2': ['N3'],
            'N3': ['N0', 'N3L0']}
    leaves = {'N1L0': -150, 'N1L1': 50, 'N3L0': -150}
    stored = {'N0': (900, 'N1', 4), 'N1': (1208, 'N1L0', 4),
              'N2': (-1208, 'N3', 4), 'N3': (0, 'N3L0', FAR)}
    for leaf, value in leaves.items():
        _leaf(f'X-{leaf}', 'w', value)
    for node, (value, move, plies) in stored.items():
        _pos(f'X-{node}', stm[node], expanded=True, nodes_invested=ENGINE,
             backed_eval=value, backed_move=f'{node}>{move}',
             backed_plies=plies, backed_nodes=HEAVY)
    for node, children in kids.items():
        for child in children:
            Edge.objects.create(parent_id=_key(f'X-{node}'),
                                move_uci=f'{node}>{child}',
                                child_id=_key(f'X-{child}'))
    return [_key(f'X-{node}') for node in stm]


def _row(key, value, move, plies=1, nodes=ENGINE):
    return types.SimpleNamespace(key=key, backed_eval=value,
                                 backed_move=move, backed_plies=plies,
                                 backed_nodes=nodes)


def _kid(move, key, returns=False):
    child = ingest._ChildValue(move, 0, ENGINE, 0, key)
    child.returns = child.cycles = returns
    return child


class WaitingTests(TestCase):
    """A level never writes a number it is rewriting."""

    def test_the_wheel_of_the_report_in_miniature(self):
        # Before: an old number goes round for ever, one position ahead of
        # its correction, and its distance grows by one at every level, 64
        # plies a pass.  That is the recascade that never came to rest.
        keys = _wheel()
        with _without_the_rule(), mock.patch.object(
                ingest, '_rows_that_wait', return_value=set()):
            plies = []
            for _ in range(3):
                self.assertGreater(ingest.backup_backed_evals(keys), 0)
                plies.append(_state('X-N2')[0][2])
            self.assertEqual(_state('X-N2')[0][0], -1208)
            self.assertEqual([b - a for a, b in zip(plies, plies[1:])],
                             [64, 64])

    def test_waiting_alone_brings_the_miniature_to_rest(self):
        # It is the waiting that ends this wheel, not the loop rule: no loop
        # of pointers is ever written here, the old number just moves on.
        keys = _wheel()

        with _without_the_rule():
            ingest.backup_backed_evals(keys)
            self.assertEqual(ingest.backup_backed_evals(keys), 0)

        self.assertEqual(_moves('X-N0', 'X-N1', 'X-N2', 'X-N3'),
                         [(-150, 'N0>N1', 2), (-150, 'N1>N1L0', 1),
                          (-150, 'N2>N3', 2), (-150, 'N3>N3L0', 1)])
        self.assertFalse(_loops())

    def test_the_miniature_comes_to_rest_at_its_value(self):
        keys = _wheel()

        ingest.backup_backed_evals(keys)

        self.assertEqual(ingest.backup_backed_evals(keys), 0)
        self.assertEqual(_values('X-N0', 'X-N1', 'X-N2', 'X-N3'),
                         [-150] * 4)
        self.assertFalse(_loops())

    def test_a_chain_alternates_from_the_bottom(self):
        # A stands on B, B on C, C on a child outside the level; all three
        # get a new number.  C writes, B read C's old number and waits, A
        # read B's number, which stays, and writes.
        rows = [_row('A', 10, 'ab'), _row('B', 10, 'bc'), _row('C', 10, 'cl')]
        children = {'A': [_kid('ab', 'B')], 'B': [_kid('bc', 'C')],
                    'C': [_kid('cl', 'L')]}
        decided = {'A': (20, 'ab', 1, ENGINE), 'B': (20, 'bc', 1, ENGINE),
                   'C': (20, 'cl', 1, ENGINE)}

        self.assertEqual(ingest._rows_that_wait(rows, children, decided),
                         {'B'})
        # A row the loop rule decided is written whatever it stands on.
        self.assertEqual(
            ingest._rows_that_wait(rows, children, decided, ruled={'B'}),
            {'A'})

    def test_what_does_not_wait(self):
        rows = [_row('A', 10, 'ab'), _row('B', 10, 'ba'), _row('C', 10, 'cd'),
                _row('D', 10, 'dx'), _row('E', 10, 'ef'), _row('F', 10, 'f1')]
        children = {'A': [_kid('ab', 'B')], 'B': [_kid('ba', 'A')],
                    'C': [_kid('cd', 'D', returns=True)],
                    'D': [_kid('dx', 'X')], 'E': [_kid('ef', 'F')],
                    'F': [_kid('f1', 'G'), _kid('f2', 'H')]}
        decided = {
            # A ring of rewrites is written as before.
            'A': (20, 'ab', 1, ENGINE), 'B': (20, 'ba', 1, ENGINE),
            # A child valued on a line that returns is not its stored number.
            'C': (20, 'cd', 1, ENGINE), 'D': (20, 'dx', 1, ENGINE),
            # A child whose number stays (only its move changes).
            'E': (20, 'ef', 1, ENGINE), 'F': (10, 'f2', 1, ENGINE)}

        self.assertEqual(ingest._rows_that_wait(rows, children, decided),
                         set())
