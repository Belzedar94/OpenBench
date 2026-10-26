"""An echo is no value (see the block above ingest._child_contribution).

On 26-Oct-2026, after #77 and #80 went live, an 8-pass recascade of
cz_eclipsia's rook ending (2,734 positions below 795ffd96) did not come to
rest.  63 positions still held the 1208 of the loop of 20-Oct, standing on
each other at distances that grew from about 2,500 to 3,365 plies, and no
loop of pointers held them at rest: each one, recomputed, took the 1208 of a
neighbour one ply further, and the neighbour did the same a level later.
The top, 795ffd96 (own search 1234), read its searched moves as lines coming
back to it and published a draw.

A distance counts the chain of pointers down to an analysis, which never
repeats a position; in a 10% page sample of production that day, the only
distances past 128 plies were this family's 1208.  A distance past
BACKED_ECHO_PLIES was counted round a cycle: the number is an echo, and it
is read as no backed value at all.
"""

import hashlib

from . import ingest
from .models import Edge, Position
from .testing import TestCase

ENGINE = 128_000_000
ECHO = ingest.BACKED_ECHO_PLIES
FEN = '4k3/8/8/8/8/8/8/4K3 {} - - 0 1'


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm, **kw):
    return Position.objects.create(key=_key(name), fen=FEN.format(stm), **kw)


def _edge(parent, child, move):
    Edge.objects.create(parent=parent, move_uci=move, child=child)


def _backed(row):
    row.refresh_from_db()
    return row.backed_eval, row.backed_move, row.backed_plies


def _contribution(backed_plies, eval_cp=1120, nodes=ENGINE):
    return ingest._child_contribution(
        'h6f6', 'UNKNOWN', eval_cp, 1208, 2_000_000_000, nodes, backed_plies,
        'child', 'f8d8')


class ContributionTests(TestCase):

    def test_an_echo_is_read_as_the_childs_own_measure(self):
        child = _contribution(ECHO + 1)
        self.assertEqual((child.value, child.quality, child.plies),
                         (1120, ENGINE, 0))
        # No borrowed number, so no chain for the repetition walk to follow.
        self.assertIsNone(child.spine)

    def test_an_echo_with_no_measure_of_its_own_is_no_value(self):
        child = _contribution(3365, eval_cp=None, nodes=0)
        self.assertIsNone(child.value)

    def test_a_chain_up_to_the_bound_is_read_as_before(self):
        for plies in (0, 47, ECHO):
            child = _contribution(plies)
            self.assertEqual((child.value, child.plies, child.spine),
                             (1208, plies, 'f8d8'))

    def test_a_proven_child_is_never_an_echo(self):
        child = ingest._child_contribution(
            'h6f6', 'WHITE_WIN', 9999, 10_000, 0, ENGINE, 5000, 'child',
            'f8d8', mate_in=7)
        self.assertEqual((child.value, child.exact, child.mate_in),
                         (10_000, True, 7))


RING = ingest.BACKED_CYCLE_MAX_PLIES + 2    # too long for every walk


def _ring(name):
    """A shuffle longer than any walk sees, still holding a number its source
    gave up long ago: each position stands on the next at 1208, at distances
    only a cycle can count.  White's ways out bring 1198, Black's 1250, so
    the ring is worth 1198 (White's best exit)."""
    ring = [_pos(f'{name}-R{i}', 'w' if i % 2 == 0 else 'b', expanded=True,
                 eval_cp=1180 if i % 2 == 0 else 1190, nodes_invested=ENGINE,
                 backed_eval=1208, backed_move=f'r{i}', backed_plies=3000 + i,
                 backed_nodes=2_000_000_000)
            for i in range(RING)]
    for i, row in enumerate(ring):
        _edge(row, ring[(i + 1) % RING], f'r{i}')
        white = i % 2 == 0
        _edge(row, _pos(f'{name}-E{i}', 'b' if white else 'w',
                        eval_cp=1198 if white else 1250,
                        nodes_invested=ENGINE), f'e{i}')
    return ring


def _until_rest(keys, passes=6):
    changed = []
    for _ in range(passes):
        changed.append(ingest.backup_backed_evals(keys))
        if not changed[-1]:
            break
    return changed


class EchoFamilyTests(TestCase):

    def test_the_echo_dies_and_the_ring_takes_its_best_exit(self):
        ring = _ring('ON')

        changed = _until_rest([row.key for row in ring])

        self.assertEqual(changed[-1], 0)
        for row in ring:
            value, _move, plies = _backed(row)
            self.assertEqual(value, 1198)
            self.assertLess(plies, ingest.BACKED_MAX_PLIES)

    def test_without_the_rule_the_echo_holds_and_never_rests(self):
        # The record of the defect: read at face value, every position keeps
        # 1208, one ply further at every recompute, and no pass is the last.
        ring = _ring('OFF')
        saved = ingest.BACKED_ECHO_PLIES
        ingest.BACKED_ECHO_PLIES = 10 ** 9
        try:
            changed = _until_rest([row.key for row in ring], passes=3)
        finally:
            ingest.BACKED_ECHO_PLIES = saved

        self.assertTrue(all(changed))
        for row in ring:
            value, _move, plies = _backed(row)
            self.assertEqual(value, 1208)
            self.assertGreater(plies, 3000 + RING)
