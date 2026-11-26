"""backup_cascade reads its frontier a wave at a time, and writes the same.

The visit loop of the cascade is unchanged; only where a visit reads its row
and its children from is (§ ingest, the block above _CascadeBulkReader).
What is defended here:

1. the per-key reads and the wave reads end in the same positions, closures,
   closure events and proof numbers, and the cascade changes the same keys
   in the same order: on landings drained through the queue (a
   transposition, a closure reaching another parent, a repetition cycle)
   and on direct cascades over mate distances (a stale parent, a long chain,
   a loop of closed positions);
2. a visit never sees what an earlier visit set on its objects without
   saving (the rewrite limit declines a save after setting the fields), and
   always sees what the cascade saved;
3. the reads cost a constant number of queries per wave, not two per key.
"""

from unittest import mock

from django.db import connections
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from . import ingest, logic, propagation_queue
from .database import atomic, current_alias
from .models import DBEvent, Edge, Position, PropagationSeed
from .test_deferred_propagation import (_Rollback, _child, _closure,
                                        _expanded, _land, _transposition)
from .test_proof_after_commit import _cycle, _state
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


def _events():
    return sorted((kind, sorted(payload.items()))
                  for kind, payload in DBEvent.objects
                  .exclude(kind='PROOF_GUARD')
                  .values_list('kind', 'payload'))


# ---------------- direct cascades over mate distances ----------------

def _stale_parent():
    """1.Nf3 d5: a parent closed at a stale 3 over its only winning child
    at 4 (test_mate_distance.ReportedShapeTests)."""
    parent = _position(_after('g1f3', 'd7d5'))
    ingest.expand(parent)
    Position.objects.filter(key=parent.key).update(
        status='WHITE_WIN', closure='MINIMAX', proof='ENGINE', mate_in=3,
        best_move='f3e5', clock_slack=90)
    child = Edge.objects.get(parent=parent, move_uci='f3e5').child_id
    Position.objects.filter(key=child).update(
        status='WHITE_WIN', closure='MATE_PV', proof='ENGINE',
        won_line='f7f6 e5d7 e7e6 d7f8', mate_in=4, best_move='f7f6',
        clock_slack=90)
    return [child]


_CHAIN = ('a2a3', 'a7a6', 'b2b3', 'b7b6', 'c2c3', 'c7c6', 'd2d3', 'd7d6',
          'e2e3', 'e7e6', 'f2f3', 'f7f6', 'h2h3', 'h7h6')


def _long_chain():
    """A line of real moves whose links are closed at a stale 100 over a
    position closed at 0 (the shape of test_mate_distance.DeepChainTests):
    the cascade climbs it all, rewriting every link."""
    fens = [logic.start_fen()]
    for uci in _CHAIN:
        fens.append(logic.apply_move(fens[-1], uci))
    nodes = [_position(fen, status='WHITE_WIN', proof='ENGINE', mate_in=100,
                       closure='MINIMAX', clock_slack=90, expanded=True,
                       best_move=uci)
             for fen, uci in zip(fens, _CHAIN)]
    terminal = _position(fens[-1], status='WHITE_WIN', closure='TERMINAL',
                         mate_in=0, clock_slack=90)
    for (parent, uci), child in zip(zip(nodes, _CHAIN),
                                    nodes[1:] + [terminal]):
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
    return [terminal.key]


def _closed_loop():
    """1.Nf3 Nf6 2.Ng1 Ng8 is the start position: four closed positions
    whose distances only hold each other up, plus one that is grounded."""
    cycle = [('g1f3', logic.start_fen())]
    for uci in ('g8f6', 'f3g1', 'f6g8'):
        cycle.append((uci, logic.apply_move(cycle[-1][1], cycle[-1][0])))
    nodes = []
    for index, (uci, fen) in enumerate(cycle):
        node = _position(fen, status='WHITE_WIN', closure='MINIMAX',
                         proof='ENGINE', mate_in=10 + index, best_move=uci,
                         expanded=True, clock_slack=90)
        nodes.append((node, uci))
    for node, uci in nodes:
        child_fen = logic.apply_move(node.fen, uci)
        Edge.objects.get_or_create(
            parent=node, move_uci=uci,
            defaults={'child': Position.objects.get(
                key=logic.key_of(child_fen))})
    return [node.key for node, _uci in nodes]


class EquivalenceTests(TestCase):
    """1 and 2.  The per-key reads and the wave reads end in one place."""

    def _drained(self, build, bulk):
        try:
            with atomic(), override_settings(
                    ATOMICDB_CASCADE_BULK_READS=bulk):
                for key, lines, budget, proofs in build():
                    _land(key, lines, budget, proofs, True)
                self.assertTrue(PropagationSeed.objects.exists())
                with mock.patch.object(
                        propagation_queue, 'proof_after_commit_enabled',
                        return_value=True):
                    with self.captureOnCommitCallbacks(
                            using=current_alias(), execute=True):
                        result = propagation_queue.drain()
                self.assertEqual(result['failed_batches'], 0)
                state = (_state(), _events())
                raise _Rollback()
        except _Rollback:
            return state

    def _cascaded(self, build, bulk):
        try:
            with atomic(), override_settings(
                    ATOMICDB_CASCADE_BULK_READS=bulk):
                seeds = build()
                report = {}
                changed = ingest.backup_cascade(seeds, report=report)
                state = (_state(), _events(), changed, report['changed'])
                raise _Rollback()
        except _Rollback:
            return state

    def _assert_same(self, run, build):
        per_key = run(build, False)
        bulk = run(build, True)
        positions, nodes, closed = per_key[0]
        self.assertEqual(positions.keys(), bulk[0][0].keys())
        for key in positions:
            self.assertEqual(positions[key], bulk[0][0][key],
                             f'position {key[:12]}')
        self.assertEqual(nodes, bulk[0][1])
        self.assertEqual(closed, bulk[0][2])
        self.assertEqual(per_key[1:], bulk[1:])
        return per_key

    def test_a_transposition(self):
        self._assert_same(self._drained, _transposition)

    def test_a_closure_that_reaches_another_parent(self):
        (_positions, _nodes, closed), _events = self._assert_same(
            self._drained, _closure)
        self.assertTrue(closed)

    def test_a_repetition_cycle(self):
        self._assert_same(self._drained, _cycle)

    def test_a_stale_mate_distance(self):
        state = self._assert_same(self._cascaded, _stale_parent)
        self.assertEqual(state[2], 1)

    def test_a_long_chain_of_mate_distances(self):
        state = self._assert_same(self._cascaded, _long_chain)
        self.assertEqual(state[2], len(_CHAIN))

    def test_a_loop_of_closed_positions(self):
        self._assert_same(self._cascaded, _closed_loop)

    def test_a_save_the_rewrite_limit_declines_is_never_read(self):
        """With the limit at 1 every second write of a key is declined
        after its fields were set in memory: the next visit must read the
        row as saved, not as left on the declined object."""
        with mock.patch.object(ingest, 'CASCADE_REWRITE_LIMIT', 1):
            self._assert_same(self._cascaded, _long_chain)
            self._assert_same(self._cascaded, _closed_loop)
            self._assert_same(self._drained, _closure)

    def test_waves_of_one_key_read_the_same(self):
        """The smallest wave is the per-key read with a cache: still the
        same values, and every revisit read through the cache."""
        with mock.patch.object(ingest, 'CASCADE_WAVE_KEYS', 1):
            self._assert_same(self._cascaded, _long_chain)
            self._assert_same(self._drained, _transposition)

    def test_forgetting_the_cache_reads_the_same(self):
        with mock.patch.object(ingest, 'CASCADE_CACHE_ROWS', 1):
            self._assert_same(self._cascaded, _long_chain)
            self._assert_same(self._drained, _closure)


class ReaderTests(TestCase):
    """2.  What a visit reads, piece by piece."""

    def setUp(self):
        self.root = _expanded(logic.start_fen())
        self.child = _child(self.root, 'g1f3')

    def test_a_visit_reads_what_the_per_key_query_reads(self):
        per_key = ingest._CascadePerKeyReader()
        bulk = ingest._CascadeBulkReader()
        pos, edges = per_key.visit(self.root.key, set())
        pos2, edges2 = bulk.visit(self.root.key, set())
        names = [f.attname for f in Position._meta.concrete_fields
                 if f.name not in ingest._CASCADE_DEFERRED_FIELDS]
        self.assertEqual([getattr(pos, n) for n in names],
                         [getattr(pos2, n) for n in names])
        self.assertEqual([(e.id, e.move_uci, e.child_id) for e in edges],
                         [(e.id, e.move_uci, e.child_id) for e in edges2])
        for a, b in zip(edges, edges2):
            self.assertEqual([getattr(a.child, n) for n in names],
                             [getattr(b.child, n) for n in names])
        # A deferred column still reads, from the database.
        self.assertEqual(pos2.last_analysis, pos.last_analysis)

    def test_an_unsaved_change_is_not_seen_and_a_saved_one_is(self):
        bulk = ingest._CascadeBulkReader()
        pos, _edges = bulk.visit(self.child.key, {self.root.key})
        pos.status = 'WHITE_WIN'             # set, never saved
        again, _ = bulk.visit(self.child.key, set())
        self.assertEqual(again.status, 'UNKNOWN')
        _root, edges = bulk.visit(self.root.key, set())
        seen = {e.child_id: e.child.status for e in edges}
        self.assertEqual(seen[self.child.key], 'UNKNOWN')

        pos.status, pos.closure, pos.mate_in = 'WHITE_WIN', 'MINIMAX', 7
        pos.save(update_fields=ingest._CASCADE_WRITTEN_FIELDS)
        bulk.wrote(pos)
        again, _ = bulk.visit(self.child.key, set())
        self.assertEqual((again.status, again.closure, again.mate_in),
                         ('WHITE_WIN', 'MINIMAX', 7))
        _root, edges = bulk.visit(self.root.key, set())
        child = next(e.child for e in edges if e.child_id == self.child.key)
        self.assertEqual((child.status, child.mate_in), ('WHITE_WIN', 7))

    def test_a_missing_key_raises_as_get_does(self):
        bulk = ingest._CascadeBulkReader()
        with self.assertRaises(Position.DoesNotExist):
            bulk.visit('0' * 64, set())

    def test_edges_come_in_move_order(self):
        bulk = ingest._CascadeBulkReader()
        _pos, edges = bulk.visit(self.root.key, set())
        moves = [e.move_uci for e in edges]
        self.assertEqual(moves, sorted(moves))


class QueryCountTests(TestCase):
    """3.  A wave costs the same few queries whatever its width."""

    def _star(self, width):
        """The start position and ``width`` of its replies, each expanded:
        a frontier of ``width`` open keys plus their parent, none of which
        the cascade can close."""
        root = _expanded(logic.start_fen())
        moves = sorted(Edge.objects.filter(parent=root)
                       .values_list('move_uci', flat=True))[:width]
        seeds = []
        for uci in moves:
            child = _child(root, uci)
            ingest.expand(child)
            seeds.append(child.key)
        return seeds

    def _queries(self, width, bulk):
        try:
            with atomic(), override_settings(
                    ATOMICDB_CASCADE_BULK_READS=bulk), \
                    mock.patch.object(ingest.proof, 'refresh_proof_numbers'):
                seeds = self._star(width)
                with CaptureQueriesContext(
                        connections[current_alias()]) as captured:
                    self.assertEqual(ingest.backup_cascade(seeds), 0)
                count = len(captured.captured_queries)
                raise _Rollback()
        except _Rollback:
            return count

    def test_the_wave_reads_do_not_grow_with_the_frontier(self):
        narrow, wide = self._queries(4, True), self._queries(16, True)
        self.assertEqual(narrow, wide)
        # One for the parents of the seeds, then a wave: edges, rows.
        self.assertLessEqual(wide, 3)

    def test_the_per_key_reads_did(self):
        narrow, wide = self._queries(4, False), self._queries(16, False)
        # Two per visit: the four or sixteen replies and their parent.
        self.assertEqual(wide - narrow, 2 * (16 - 4))
        self.assertGreater(wide, self._queries(16, True) * 5)
