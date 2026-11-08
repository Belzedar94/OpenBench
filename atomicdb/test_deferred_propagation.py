"""The climb through the ancestors, deferred out of the landing.

A landing commits its result on its own position at once (stage a) and,
with ``ATOMICDB_DEFER_PROPAGATION`` on, leaves the climb through its
ancestors (stage b) as a PropagationSeed row in the same commit; one
drainer merges many of them into one climb (§ propagation_queue).  What is
defended here:

1. after stage (a) alone, the position shows its new search and nothing
   above it has moved;
2. once the queue is drained, every value equals what the inline climb, one
   landing after the other, writes: on a line from the start position, on a
   transposition, and on a closure that reaches a second parent;
3. a process that dies between (a) and (b), or in the middle of (b), leaves
   rows that a later drain climbs;
4. seeds that several landings share are climbed once.
"""

import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from . import ingest, ingest_queue, logic, propagation_queue
from .database import atomic
from .models import (AnalysisTask, DBEvent, Edge, IngestJob, Position,
                     ProofNode, PropagationSeed)
from .test_closure_reaches_all_parents import (FORK_FEN, FORK_OTHER_MOVE,
                                               MATE_LINES, TRANSPOSED_FEN,
                                               TRANSPOSED_MOVE)
from .test_proofs import FORCED_MATE_FEN, FORCED_MATE_PV
from .testing import TestCase

_FIELDS = ('status', 'closure', 'proof', 'won_line', 'mate_in',
           'clock_slack', 'best_move', 'eval_cp', 'backed_eval',
           'backed_move', 'backed_plies', 'backed_nodes', 'visits',
           'nodes_invested', 'expanded')
DEFER = {}          # what apply_job passes, minus the job: a deferred climb


class _Rollback(Exception):
    pass


def _expanded(fen):
    pos = ingest.get_or_create_position(fen)
    ingest.expand(pos)
    return pos


def _child(parent, uci):
    return Edge.objects.get(parent=parent, move_uci=uci).child


def _lines(pos, values):
    return [{'move': edge.move_uci, 'eval_cp': value, 'pv': [edge.move_uci]}
            for edge, value in zip(
                Edge.objects.filter(parent=pos).order_by('move_uci'), values)]


def _state():
    """Every value the climb decides, for every position and proof node."""
    positions = {row[0]: row[1:] for row in Position.objects.order_by('key')
                 .values_list('key', *_FIELDS)}
    nodes = {(row[0], row[1]): row[2:] for row in ProofNode.objects
             .values_list('campaign_id', 'position_id', 'pn', 'dn')}
    closed = sorted(DBEvent.objects.filter(kind='NODE_CLOSED')
                    .values_list('payload__key', flat=True))
    return positions, nodes, closed


def _land(key, lines, budget, proofs, defer):
    return ingest.ingest_analysis(
        key, lines, budget, mate_proofs=proofs,
        defer_propagation=DEFER if defer else None)


# ---------------- the three trees ----------------

def _start_line():
    """The start position, its first reply and two of that reply's replies;
    four searches land from the bottom up and then at the root."""
    root = _expanded(logic.start_fen())
    child = _child(root, Edge.objects.filter(parent=root)
                   .order_by('move_uci').first().move_uci)
    ingest.expand(child)
    grands = [edge.child for edge in Edge.objects.filter(parent=child)
              .order_by('move_uci')[:2]]
    for grand in grands:
        ingest.expand(grand)
    return [
        (grands[0].key, _lines(grands[0], [40, 25, 10, -5, -30]),
         4_000_000, {}),
        (grands[1].key, _lines(grands[1], [180, 120, 60, 0, -60]),
         8_000_000, {}),
        (child.key, _lines(child, [-150, -90, -40, 20, 70]), 2_000_000, {}),
        (root.key, _lines(root, [60, 45, 30, 15, 0]), 1_000_000, {}),
    ]


def _transposition():
    """1.Nf3 Nf6 2.Nc3 and 1.Nc3 Nf6 2.Nf3 reach one position."""
    root = _expanded(logic.start_fen())
    left = _child(root, 'g1f3')
    right = _child(root, 'b1c3')
    ingest.expand(left)
    ingest.expand(right)
    left2 = _child(left, 'g8f6')
    right2 = _child(right, 'g8f6')
    ingest.expand(left2)
    ingest.expand(right2)
    meet = _child(left2, 'b1c3')
    assert meet.key == _child(right2, 'g1f3').key
    ingest.expand(meet)
    return [
        (meet.key, _lines(meet, [-210, -160, -120, -80, -40]),
         16_000_000, {}),
        (left2.key, _lines(left2, [300, 250, 200, 150, 100]), 2_000_000, {}),
        (right2.key, _lines(right2, [90, 70, 50, 30, 10]), 2_000_000, {}),
        (left.key, _lines(left, [-20, -10, 0, 10, 20]), 1_000_000, {}),
    ]


_MATE_PROOFS = {}


def _closure():
    """A mate PV closes the child of the analysed position (stage a); the
    analysed position, the child's other parent (a transposition) and the
    AND fork above a link of the won line close in the climb (stage b)."""
    other = _expanded(TRANSPOSED_FEN)
    fork = _expanded(FORK_FEN)
    Position.objects.filter(key=_child(fork, FORK_OTHER_MOVE).key).update(
        status='WHITE_WIN', closure='MATE_PV', proof='ENGINE', mate_in=3,
        clock_slack=90)
    analysed = _expanded(FORCED_MATE_FEN)
    if 'proofs' not in _MATE_PROOFS:
        _MATE_PROOFS['proofs'] = ingest.prepare_mate_proofs(
            analysed.fen, MATE_LINES, deadline_seconds=None)
    return [
        (other.key, _lines(other, [900, 700, 500]), 4_000_000, {}),
        (analysed.key, MATE_LINES, 8_000_000, _MATE_PROOFS['proofs']),
    ]


class EquivalenceTests(TestCase):
    """2. Drained, the deferred climb writes what the inline one writes."""

    def _run(self, build, deferred):
        try:
            with atomic():
                for key, lines, budget, proofs in build():
                    _land(key, lines, budget, proofs, deferred)
                if deferred:
                    self.assertTrue(PropagationSeed.objects.exists())
                    result = propagation_queue.drain()
                    self.assertEqual(result['failed_batches'], 0)
                    self.assertFalse(PropagationSeed.objects.exists())
                state = _state()
                raise _Rollback()
        except _Rollback:
            return state

    def _assert_same(self, build):
        inline = self._run(build, deferred=False)
        deferred = self._run(build, deferred=True)
        self.assertEqual(inline[0].keys(), deferred[0].keys())
        for key in inline[0]:
            self.assertEqual(inline[0][key], deferred[0][key],
                             f'position {key[:12]}')
        self.assertEqual(inline[1], deferred[1])
        self.assertEqual(inline[2], deferred[2])
        return inline

    def test_a_line_from_the_start_position(self):
        positions, _nodes, _closed = self._assert_same(_start_line)
        root = logic.key_of(logic.start_fen())
        self.assertIsNotNone(positions[root][_FIELDS.index('backed_eval')])

    def test_a_transposition(self):
        self._assert_same(_transposition)

    def test_a_closure_that_reaches_another_parent(self):
        positions, _nodes, closed = self._assert_same(_closure)
        other = logic.key_of(TRANSPOSED_FEN)
        self.assertEqual(positions[other][:2], ('WHITE_WIN', 'MINIMAX'))
        self.assertIn(other, closed)
        self.assertIn(logic.key_of(FORK_FEN), closed)

    def test_one_batch_per_landing_writes_the_same_as_one_merged_batch(self):
        """Batch size is a knob, not a semantic: one landing per climb and
        all of them in one climb end in the same place."""
        def drained(limit):
            try:
                with atomic():
                    for key, lines, budget, proofs in _transposition():
                        _land(key, lines, budget, proofs, True)
                    propagation_queue.drain(limit=limit)
                    state = _state()
                    raise _Rollback()
            except _Rollback:
                return state

        self.assertEqual(drained(1), drained(propagation_queue.BATCH_SIZE))


class StageATests(TestCase):
    """1. Stage (a) alone: the result is there, the ancestors have not
    moved, and the climb they are owed is on record."""

    def test_the_search_is_stored_and_nothing_above_it_moves(self):
        root = _expanded(logic.start_fen())
        child = _child(root, 'g1f3')
        ingest.expand(child)
        ingest.ingest_analysis(child.key, _lines(child, [-40] * 5),
                               1_000_000, mate_proofs={})
        before = {key: row for key, row in _state()[0].items()
                  if key != child.key}

        summary = ingest.ingest_analysis(
            child.key, _lines(child, [-400, -350, -300, -250, -200]),
            64_000_000, mate_proofs={}, defer_propagation=DEFER)

        self.assertEqual(summary, {'closed_children': 0,
                                   'propagation': 'deferred'})
        child.refresh_from_db()
        self.assertEqual((child.visits, child.nodes_invested, child.eval_cp),
                         (2, 65_000_000, -400))
        self.assertEqual(child.last_analysis[0]['eval_cp'], -400)
        after = _state()[0]
        root_row = after[root.key]
        self.assertEqual(root_row, before[root.key])
        # The seeds of the children re-seeded by this pass are children of
        # ``child``: their own rows took the new seed (stage a), nothing
        # else did.
        reseeded = set(Edge.objects.filter(parent=child)
                       .values_list('child_id', flat=True))
        for key, row in before.items():
            if key not in reseeded:
                self.assertEqual(after[key], row, key[:12])
        seed = PropagationSeed.objects.get()
        self.assertEqual(seed.position_id, child.key)
        self.assertEqual(seed.payload['parents'], [root.key])
        self.assertEqual(seed.payload['cascade'][0], child.key)
        self.assertEqual(seed.payload['backed'][:2], [child.key, root.key])

        propagation_queue.drain()

        root.refresh_from_db()
        self.assertEqual(root.backed_move, 'g1f3')
        self.assertEqual(root.backed_eval, -400)

    def test_a_closure_of_its_own_child_is_stage_a_and_its_parents_are_b(self):
        other = _expanded(TRANSPOSED_FEN)
        analysed = _expanded(FORCED_MATE_FEN)
        proofs = ingest.prepare_mate_proofs(analysed.fen, MATE_LINES,
                                            deadline_seconds=None)

        ingest.ingest_analysis(analysed.key, MATE_LINES, 1_000,
                               mate_proofs=proofs, defer_propagation=DEFER)

        child = _child(analysed, FORCED_MATE_PV[0])
        self.assertEqual((child.status, child.closure),
                         ('WHITE_WIN', 'MATE_PV'))
        analysed.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(analysed.status, 'UNKNOWN')
        self.assertEqual(other.status, 'UNKNOWN')
        payload = PropagationSeed.objects.get().payload
        self.assertIn(child.key, payload['closed'])
        self.assertEqual(payload['others'], [other.key])

        propagation_queue.drain()

        analysed.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(analysed.status, 'WHITE_WIN')
        self.assertEqual((other.status, other.closure, other.best_move),
                         ('WHITE_WIN', 'MINIMAX', TRANSPOSED_MOVE))


def _job_on(target, budget=1_000_000, **task_fields):
    task = AnalysisTask.objects.create(
        position=target, generation=target.visits, budget_nodes=budget,
        state=AnalysisTask.TState.COMPLETED, **task_fields)
    return IngestJob.objects.create(
        task=task, position=target, next_attempt_at=timezone.now(),
        payload={'lines': _lines(target, [-320] * 5), 'nodes': budget,
                 'elapsed': 3.0, 'machine': 'm1'})


@override_settings(ATOMICDB_DEFER_PROPAGATION=True)
class DurableDeferralTests(TestCase):
    """3. Nothing between (a) and (b) is held in memory."""

    def setUp(self):
        self.root = _expanded(logic.start_fen())
        self.target = _child(self.root, 'e2e3')
        ingest.expand(self.target)

    def test_the_job_commits_its_result_and_leaves_its_climb(self):
        job = _job_on(self.target)

        summary = ingest_queue.apply_job(job)

        self.assertEqual(summary['propagation'], 'deferred')
        self.assertNotIn('backed_evals', summary)
        job.refresh_from_db()
        self.assertEqual(job.state, 'DONE')
        self.target.refresh_from_db()
        self.assertEqual((self.target.visits, self.target.eval_cp), (1, -320))
        self.root.refresh_from_db()
        self.assertIsNone(self.root.backed_eval)
        seed = PropagationSeed.objects.get()
        self.assertEqual(seed.payload['job'], job.pk)

        # The process dies here: nothing else runs.  Whoever drains next
        # finds the climb on the table.
        result = propagation_queue.drain()

        self.assertEqual(result['settled'], 1)
        self.root.refresh_from_db()
        self.assertEqual(self.root.backed_move, 'e2e3')
        job.refresh_from_db()
        self.assertEqual(job.summary['propagated']['batch'], 1)
        self.assertIn('backed_evals', job.summary['propagated'])

    def test_a_climb_that_fails_keeps_its_rows_and_retries(self):
        ingest_queue.apply_job(_job_on(self.target))
        boom = mock.patch.object(ingest, 'propagate_landings',
                                 side_effect=RuntimeError('killed mid-climb'))
        with boom:
            self.assertEqual(propagation_queue.drain_batch(), -1)

        seed = PropagationSeed.objects.get()
        self.assertEqual(seed.attempts, 1)
        self.assertIn('killed mid-climb', seed.last_error)
        self.assertGreater(seed.next_attempt_at, timezone.now())
        self.root.refresh_from_db()
        self.assertIsNone(self.root.backed_eval)
        # Not ready before its backoff...
        self.assertEqual(propagation_queue.drain()['batches'], 0)
        PropagationSeed.objects.update(next_attempt_at=timezone.now())

        self.assertEqual(propagation_queue.drain()['settled'], 1)

        self.assertFalse(PropagationSeed.objects.exists())
        self.root.refresh_from_db()
        self.assertEqual(self.root.backed_move, 'e2e3')

    def test_a_row_that_failed_climbs_alone(self):
        ingest_queue.apply_job(_job_on(self.target))
        PropagationSeed.objects.update(attempts=1)
        other = _child(self.root, 'd2d3')
        ingest.expand(other)
        ingest_queue.apply_job(_job_on(other))
        calls = []
        real = ingest.propagate_landings

        def spy(landings):
            calls.append([landing.key for landing in landings])
            return real(landings)

        with mock.patch.object(ingest, 'propagate_landings', spy):
            propagation_queue.drain()

        self.assertEqual(calls, [[self.target.key], [other.key]])

    def test_the_requested_verification_waits_for_the_climb(self):
        job = _job_on(self.target, requested_by='alice', route='e2e3',
                      then_verify=True)
        with mock.patch.object(ingest_queue, '_verify_after',
                               return_value=2) as verify:
            ingest_queue.apply_job(job)
            self.assertFalse(verify.called)
            propagation_queue.drain()

        self.assertEqual(verify.call_count, 1)
        self.assertEqual(verify.call_args[0][1],
                         {'requested_by': 'alice', 'route': 'e2e3'})
        job.refresh_from_db()
        self.assertEqual(job.summary['then_verify'], 2)

    def test_the_synchronous_ingest_still_climbs_inline(self):
        with override_settings(ATOMICDB_SYNCHRONOUS_INGEST=True):
            summary = ingest_queue.apply_job(_job_on(self.target))

        self.assertIn('backed_evals', summary)
        self.assertFalse(PropagationSeed.objects.exists())

    def test_the_command_drains_and_reports_the_backlog(self):
        ingest_queue.apply_job(_job_on(self.target))
        out = StringIO()
        call_command('process_propagation', '--status', stdout=out)
        status = json.loads(out.getvalue())
        self.assertEqual(status['waiting'], 1)
        self.assertIsNotNone(status['oldest_seconds'])

        out = StringIO()
        call_command('process_propagation', '--once', stdout=out)

        self.assertEqual(json.loads(out.getvalue())['settled'], 1)
        self.assertEqual(propagation_queue.backlog()['waiting'], 0)

    def test_the_writers_take_what_is_left_once_deferral_is_off(self):
        ingest_queue.apply_job(_job_on(self.target))
        with override_settings(ATOMICDB_DEFER_PROPAGATION=False):
            out = StringIO()
            call_command('process_ingest_queue', '--once', stdout=out)

        self.assertEqual(json.loads(out.getvalue())['propagation_settled'], 1)
        self.assertFalse(PropagationSeed.objects.exists())


class DeduplicationTests(TestCase):
    """4. A seed several landings share is climbed once."""

    def setUp(self):
        self.root = _expanded(logic.start_fen())
        self.kids = [_child(self.root, uci) for uci in ('e2e3', 'd2d3')]
        for kid in self.kids:
            ingest.expand(kid)

    def _spy_backed(self):
        seeds = []
        real = ingest.backup_backed_evals

        def spy(keys, *args, **kwargs):
            seeds.append(list(keys))
            return real(keys, *args, **kwargs)

        return seeds, mock.patch.object(ingest, 'backup_backed_evals', spy)

    def test_siblings_share_their_parent_in_one_climb(self):
        for kid, value in zip(self.kids, (-100, -300)):
            ingest.ingest_analysis(kid.key, _lines(kid, [value] * 5),
                                   1_000_000, mate_proofs={},
                                   defer_propagation=DEFER)
        self.assertEqual(PropagationSeed.objects.count(), 2)
        seeds, spy = self._spy_backed()

        with spy:
            propagation_queue.drain()

        # One ascent, the root in it once, the keys in key order.
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0].count(self.root.key), 1)
        self.assertEqual(seeds[0], sorted(set(seeds[0])))
        self.root.refresh_from_db()
        self.assertEqual(self.root.backed_eval, -100)

    def test_two_landings_on_one_position_are_one_climb(self):
        kid = self.kids[0]
        for value in (-100, -200):
            ingest.ingest_analysis(kid.key, _lines(kid, [value] * 5),
                                   1_000_000, mate_proofs={},
                                   defer_propagation=DEFER)
        rows = list(PropagationSeed.objects.order_by('id'))
        self.assertEqual(len(rows), 2)
        landings, jobs = propagation_queue._merge(rows)
        self.assertEqual([landing.key for landing in landings], [kid.key])
        self.assertEqual(len(jobs[kid.key]), 2)
        self.assertEqual(landings[0].parents, [self.root.key])
        arms = mock.patch.object(ingest, '_queue_value_cascade',
                                 return_value=0)

        with arms as cascade:
            propagation_queue.drain()

        self.assertEqual(cascade.call_count, 1)
        self.assertFalse(PropagationSeed.objects.exists())
        self.root.refresh_from_db()
        self.assertEqual(self.root.backed_eval, -200)
