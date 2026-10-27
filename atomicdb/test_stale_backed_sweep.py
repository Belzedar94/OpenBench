"""Stale backed values: what leaves them, and the sweep that repairs them.

Three community reports on 26-Oct-2026, one symptom: a header that its own
table contradicts.  ``edc90c6b`` stored 1224 via f5f6 over a child at 1183;
the top of the main line, ``7e11bbf6``, stored 1183 over a child at 1177.
What left them (see sweep_stale_backed for the evidence):

* an ascent cut by BACKED_MAX_PLIES with its frontier dropped.  The main line
  is deeper than 64 plies of pointers, so a change at its bottom never reached
  its top.  The BACKED_GUARD event now carries the frontier, and the sweep
  continues it;
* two ingests in flight at once, each computing a shared ancestor without the
  other's uncommitted change (a lost update or write skew).  Nothing records
  it, so the sweep looks where overlapping ingests meet, and everywhere else
  on a rotation;
* a retry after a lock conflict that seeded the original keys again: the
  levels before the conflict were committed, so the retry found nothing to
  change and the rest of the climb was lost.  It now starts from the level
  that was rolled back.
"""

import hashlib
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone

from . import ingest, ingest_queue
from .management.commands import sweep_stale_backed as sweep
from .models import AnalysisTask, DBEvent, Edge, IngestJob, Position
from .testing import TestCase, TransactionTestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _line(name, plies, value):
    """``plies`` moves from a top position down to a leaf searched at
    ``value``.  Every position on the way has that one move and stands on
    it, so the top's distance is ``plies``."""
    nodes, colour = [], 'w'
    for index in range(plies):
        nodes.append(_pos(f'{name}-{index}', colour, expanded=True))
        colour = 'b' if colour == 'w' else 'w'
    leaf = _pos(f'{name}-leaf', colour, eval_cp=value,
                nodes_invested=1_000_000)
    for upper, lower in zip(nodes, nodes[1:] + [leaf]):
        _edge(upper, lower, 'a1a2')
    ingest.backup_backed_evals([leaf.key, nodes[-1].key])
    return nodes, leaf


def _analyse(leaf, parent, value, **kwargs):
    """What an ingest does to the backed values: the analysed position and
    its parents seed the ascent (see ingest_analysis)."""
    Position.objects.filter(key=leaf.key).update(eval_cp=value)
    return ingest.backup_backed_evals([leaf.key, parent.key], **kwargs)


def _stored(position):
    position.refresh_from_db()
    return (position.backed_eval, position.backed_move,
            position.backed_plies, position.backed_nodes)


def _lost_update(name):
    """The shape of 5fc31111 (19-Oct): Black to move, two searched replies,
    and a backed value computed while b7b5 was still an unsearched seed at
    1216, written by an ingest that committed after b7b5's own."""
    parent = _pos(f'{name}-P', 'b', expanded=True, eval_cp=1177,
                  nodes_invested=128_000_000, backed_eval=1216,
                  backed_move='b7b5', backed_plies=1, backed_nodes=0)
    b7b5 = _pos(f'{name}-b7b5', 'w', eval_cp=1139,
                nodes_invested=128_000_000)
    b7b6 = _pos(f'{name}-b7b6', 'w', eval_cp=1246,
                nodes_invested=128_000_000)
    _edge(parent, b7b5, 'b7b5')
    _edge(parent, b7b6, 'b7b6')
    return parent


def _deadlock():
    cause = Exception('deadlock detected')
    cause.sqlstate = '40P01'
    error = OperationalError('deadlock detected')
    error.__cause__ = cause
    return error


def _sweep(*args):
    out = StringIO()
    call_command('sweep_stale_backed', '--settle-seconds', '0', *args,
                 stdout=out, stderr=StringIO())
    return DBEvent.objects.filter(kind='BACKED_SWEEP').order_by('-id').first()


class CutAscentTests(TestCase):
    """The top of the main line: an ascent too short for the line."""

    def test_a_cut_ascent_leaves_its_frontier_on_record(self):
        nodes, leaf = _line('CUT', 8, 100)

        _analyse(leaf, nodes[-1], 250, max_plies=3)

        guard = DBEvent.objects.get(kind='BACKED_GUARD')
        self.assertEqual(guard.payload['reason'], 'ply-guard')
        self.assertEqual(guard.payload['generation'], 0)
        # Three levels rewrote the three positions above the leaf; the
        # fourth is the one the next level would have read.
        self.assertEqual(guard.payload['keys'], [nodes[4].key])
        self.assertEqual(guard.payload['left'], 1)
        self.assertEqual(_stored(nodes[5])[0], 250)
        self.assertEqual(_stored(nodes[4])[0], 100)
        self.assertIn(nodes[4].key, ingest.stale_backed_rows([nodes[4].key]))

    def test_the_sweep_carries_a_cut_ascent_to_the_top(self):
        nodes, leaf = _line('CARRY', 8, 100)
        _analyse(leaf, nodes[-1], 250, max_plies=3)

        event = _sweep('--no-rotation', '--no-recent')

        self.assertEqual(_stored(nodes[0])[:3], (250, 'a1a2', 8))
        self.assertEqual(event.payload['guards']['keys'], {'0': 1})
        self.assertEqual(event.payload['ascents'], 1)
        self.assertFalse(ingest.stale_backed_rows(
            [node.key for node in nodes]))
        # The next run does not carry the same frontier again.
        again = _sweep('--no-rotation', '--no-recent')
        self.assertEqual(again.payload['guards']['events'], 0)
        self.assertEqual(again.payload['ascents'], 0)

    def test_a_frontier_is_carried_on_a_bounded_number_of_times(self):
        nodes, leaf = _line('BOUND', 8, 100)
        _analyse(leaf, nodes[-1], 250, max_plies=3)
        DBEvent.objects.filter(kind='BACKED_GUARD').update(
            payload={'reason': 'ply-guard', 'generation': 2,
                     'keys': [nodes[4].key], 'left': 1})

        event = _sweep('--no-rotation', '--no-recent')

        self.assertEqual(event.payload['guards']['held_back'], 1)
        self.assertEqual(event.payload['ascents'], 0)
        self.assertEqual(_stored(nodes[0])[0], 100)


def _ingest_job(leaf, parent, value):
    """A queued job whose application ends in a cut ascent: the analysis is
    stood in for, the rest of apply_job runs as it does in production."""
    task = AnalysisTask.objects.create(position=leaf, generation=0,
                                       budget_nodes=128_000_000)
    job = IngestJob.objects.create(task=task, position=leaf, payload={})

    def analysis(*_args, **_kwargs):
        _analyse(leaf, parent, value, max_plies=3)
        return {}

    return job, mock.patch.object(ingest, 'ingest_analysis',
                                  side_effect=analysis)


class IngestCarryTests(TestCase):
    """An ingest carries its own cut ascent on once it has committed
    (27-Oct-2026: the top of the main line waited 10 to 25 minutes for the
    sweep)."""

    def test_the_frontiers_of_a_block_are_collected(self):
        nodes, leaf = _line('COLLECT', 8, 100)

        with ingest.carried_cuts() as cut:
            _analyse(leaf, nodes[-1], 250, max_plies=3)

        self.assertEqual(cut, [nodes[4].key])

    def test_an_ascent_outside_an_ingest_collects_nothing(self):
        nodes, leaf = _line('OUTSIDE', 8, 100)

        with ingest.carried_cuts() as cut:
            pass
        _analyse(leaf, nodes[-1], 250, max_plies=3)

        self.assertEqual(cut, [])
        self.assertEqual(_stored(nodes[0])[0], 100)

    def test_an_ingest_carries_its_cut_to_the_top(self):
        nodes, leaf = _line('INGEST', 8, 100)
        job, analysis = _ingest_job(leaf, nodes[-1], 250)

        with analysis:
            summary = ingest_queue.apply_job(job)

        self.assertEqual(_stored(nodes[0])[:3], (250, 'a1a2', 8))
        # The ingest's three levels stopped at nodes[5]; the carry rewrote
        # the five above it.
        self.assertEqual(summary['carried'], 5)
        self.assertFalse(ingest.stale_backed_rows(
            [node.key for node in nodes]))
        job.refresh_from_db()
        self.assertEqual(job.state, IngestJob.JState.DONE)
        self.assertEqual(job.summary['carried'], 5)
        # The cut stays on record: the sweep may still carry it, and finds
        # nothing left to change.
        guard = DBEvent.objects.get(kind='BACKED_GUARD')
        self.assertEqual(guard.payload['keys'], [nodes[4].key])
        event = _sweep('--no-rotation', '--no-recent')
        self.assertEqual(event.payload['changed'], 0)

    def test_a_wide_frontier_is_left_to_the_sweep(self):
        nodes, leaf = _line('WIDE', 8, 100)
        job, analysis = _ingest_job(leaf, nodes[-1], 250)

        with analysis, mock.patch.object(ingest, 'CARRY_MAX_KEYS', 0):
            summary = ingest_queue.apply_job(job)

        self.assertNotIn('carried', summary)
        job.refresh_from_db()
        self.assertNotIn('carried', job.summary)
        self.assertEqual(_stored(nodes[0])[0], 100)
        _sweep('--no-rotation', '--no-recent')
        self.assertEqual(_stored(nodes[0])[:3], (250, 'a1a2', 8))

    def test_a_carry_that_fails_leaves_the_job_applied(self):
        nodes, leaf = _line('FAILED', 8, 100)
        job, analysis = _ingest_job(leaf, nodes[-1], 250)

        with analysis, mock.patch.object(ingest, 'carry_cut_ascents',
                                         side_effect=_deadlock()), \
                self.assertLogs('atomicdb.ingest_queue', 'ERROR'):
            summary, error = ingest_queue.process_job(job)

        self.assertIsNone(error)
        self.assertNotIn('carried', summary)
        job.refresh_from_db()
        self.assertEqual(job.state, IngestJob.JState.DONE)
        self.assertEqual(_stored(nodes[0])[0], 100)
        _sweep('--no-rotation', '--no-recent')
        self.assertEqual(_stored(nodes[0])[:3], (250, 'a1a2', 8))


class LostUpdateTests(TestCase):
    """What two ingests in flight at once leave, and where to look for it."""

    def test_the_check_finds_what_one_level_of_the_backup_would_rewrite(self):
        parent = _lost_update('CHECK')

        stale = ingest.stale_backed_rows([parent.key])

        self.assertEqual(stale[parent.key],
                         ((1216, 'b7b5', 1, 0),
                          (1139, 'b7b5', 1, 128_000_000)))
        # Nothing was written.
        self.assertEqual(_stored(parent), (1216, 'b7b5', 1, 0))

    def test_the_rotation_repairs_it(self):
        parent = _lost_update('ROTATE')

        event = _sweep('--no-guards', '--no-recent', '--units', '1000000')

        self.assertEqual(_stored(parent), (1139, 'b7b5', 1, 128_000_000))
        self.assertEqual(event.payload['stale'], 1)
        self.assertEqual(event.payload['confirmed'], 1)
        self.assertEqual(event.payload['kinds'], {'value': 1})

    def test_the_merge_points_of_overlapping_ingests(self):
        # P has children A and B; A has a child A1.  Ingests of A1 and of B
        # in flight at once both climb through P.
        top = _pos('MERGE-P', 'b', expanded=True)
        a = _pos('MERGE-A', 'w', expanded=True)
        b = _pos('MERGE-B', 'w')
        a1 = _pos('MERGE-A1', 'b')
        _edge(top, a, 'a1a2')
        _edge(top, b, 'b1b2')
        _edge(a, a1, 'c1c2')
        now = timezone.now()
        overlapping = [(a1.key, now, now + timedelta(seconds=5)),
                       (b.key, now + timedelta(seconds=1),
                        now + timedelta(seconds=3))]
        apart = [(a1.key, now, now + timedelta(seconds=1)),
                 (b.key, now + timedelta(seconds=2),
                  now + timedelta(seconds=3))]

        self.assertEqual(sweep._merge_points(overlapping, 2), [top.key])
        self.assertEqual(sweep._merge_points(apart, 2), [])
        self.assertEqual(sweep._merge_points(overlapping, 1), [])

    def test_the_recent_feed_repairs_a_merge_point(self):
        parent = _lost_update('RECENT')
        b7b5, b7b6 = (Edge.objects.get(parent=parent, move_uci=move).child
                      for move in ('b7b5', 'b7b6'))
        start = timezone.now() - timedelta(minutes=5)
        for child, offset in ((b7b5, 0), (b7b6, 1)):
            task = AnalysisTask.objects.create(
                position=child, generation=0, budget_nodes=128_000_000)
            job = IngestJob.objects.create(
                task=task, position=child, payload={},
                state=IngestJob.JState.DONE)
            IngestJob.objects.filter(pk=job.pk).update(
                created=start + timedelta(seconds=offset),
                updated=start + timedelta(seconds=offset + 4))

        event = _sweep('--no-guards', '--no-rotation')

        self.assertEqual(event.payload['recent'],
                         {'jobs': 2, 'merge_points': 1})
        self.assertEqual(_stored(parent), (1139, 'b7b5', 1, 128_000_000))


class SettleTests(TestCase):
    """Only what stays stale is repaired."""

    def test_a_position_rewritten_in_between_is_left_alone(self):
        parent = _lost_update('MOVED')

        def ingest_lands(_seconds):
            Position.objects.filter(key=parent.key).update(backed_eval=1200)

        with mock.patch.object(sweep.time, 'sleep', side_effect=ingest_lands):
            event = _sweep('--no-guards', '--no-recent', '--units', '1000000',
                           '--settle-seconds', '1')

        self.assertEqual(event.payload['moved'], 1)
        self.assertEqual(event.payload['confirmed'], 0)
        self.assertEqual(_stored(parent)[0], 1200)

    def test_a_position_fixed_in_between_is_not_touched(self):
        parent = _lost_update('RESOLVED')

        def ingest_lands(_seconds):
            ingest.backup_backed_evals([parent.key])

        with mock.patch.object(sweep.time, 'sleep', side_effect=ingest_lands):
            event = _sweep('--no-guards', '--no-recent', '--units', '1000000',
                           '--settle-seconds', '1')

        self.assertEqual(event.payload['resolved'], 1)
        self.assertEqual(event.payload['ascents'], 0)

    def test_a_position_with_its_own_result_landing_waits_for_it(self):
        parent = _lost_update('FLIGHT')
        task = AnalysisTask.objects.create(
            position=parent, generation=0, budget_nodes=128_000_000)
        IngestJob.objects.create(task=task, position=parent, payload={})

        event = _sweep('--no-guards', '--no-recent', '--units', '1000000')

        self.assertEqual(event.payload['in_flight'], 1)
        self.assertEqual(_stored(parent), (1216, 'b7b5', 1, 0))


class SafetyTests(TestCase):

    def test_a_proven_position_is_never_written(self):
        parent = _lost_update('PROVEN')
        grand = _pos('PROVEN-G', 'w', status='WHITE_WIN', closure='MINIMAX',
                     best_move='h1h2', expanded=True, backed_eval=300,
                     backed_move='h1h2', backed_plies=3)
        _edge(grand, parent, 'h1h2')

        _sweep('--no-guards', '--no-recent', '--units', '1000000')

        self.assertEqual(_stored(parent)[0], 1139)
        self.assertEqual(_stored(grand), (300, 'h1h2', 3, 0))
        self.assertNotIn(grand.key, ingest.stale_backed_rows([grand.key]))

    def test_a_dry_run_writes_nothing(self):
        parent = _lost_update('DRY')
        out = StringIO()

        call_command('sweep_stale_backed', '--dry-run', '--settle-seconds',
                     '0', '--no-guards', '--no-recent', '--units', '1000000',
                     stdout=out)

        self.assertEqual(_stored(parent), (1216, 'b7b5', 1, 0))
        self.assertFalse(DBEvent.objects.filter(kind='BACKED_SWEEP').exists())
        self.assertIn('stale=1', out.getvalue())

    def test_the_rewrite_cap_hands_the_rest_to_the_next_run(self):
        first, second = _lost_update('CAP1'), _lost_update('CAP2')

        event = _sweep('--no-guards', '--no-recent', '--units', '1000000',
                       '--repair-batch', '1', '--max-changes', '1')

        self.assertEqual(event.payload['valve'], 'max-changes')
        self.assertEqual(event.payload['ascents'], 1)
        self.assertEqual(len(event.payload['pending']), 1)
        repaired = [_stored(p)[0] for p in (first, second)].count(1139)
        self.assertEqual(repaired, 1)

        _sweep('--no-guards', '--no-recent', '--no-rotation')
        self.assertEqual([_stored(p)[0] for p in (first, second)],
                         [1139, 1139])

    def test_a_change_of_rule_is_reported_not_swept(self):
        parents = [_lost_update(f'RULE{i}') for i in range(3)]

        with mock.patch.object(sweep, 'ROTATION_MIN_SAMPLE', 2):
            event = _sweep('--no-guards', '--no-recent', '--units', '1000000',
                           '--max-stale-fraction', '0.2')

        self.assertEqual(event.payload['valve'], 'stale-fraction')
        self.assertEqual(event.payload['held'], 3)
        self.assertEqual(event.payload['ascents'], 0)
        for parent in parents:
            self.assertEqual(_stored(parent)[0], 1216)

    def test_the_rotation_resumes_where_the_last_run_stopped(self):
        for index in range(4):
            _lost_update(f'ROT{index}')

        first = _sweep('--no-guards', '--no-recent', '--units', '1')
        second = _sweep('--no-guards', '--no-recent', '--units', '1')

        cursor = first.payload['cursors']['rotation']
        self.assertEqual(second.payload['rotation']['from'], cursor)
        rotation = first.payload['rotation']
        self.assertEqual(cursor, 0 if rotation['to'] >= rotation['total']
                         else rotation['to'])

    def test_its_event_stays_off_the_public_feed(self):
        from .views import FEED_HIDDEN_KINDS

        self.assertIn('BACKED_SWEEP', FEED_HIDDEN_KINDS)


class AtRestTests(TestCase):
    """Nothing stale is exactly an ascent with nothing to write."""

    def test_no_stale_rows_once_the_backup_is_at_rest(self):
        parent = _lost_update('REST')
        nodes, leaf = _line('REST-LINE', 5, 40)
        _edge(nodes[-1], parent, 'g1g2')
        everything = list(Position.objects.values_list('key', flat=True))
        self.assertTrue(ingest.stale_backed_rows(everything))

        while ingest.backup_backed_evals(everything):
            pass

        self.assertEqual(ingest.stale_backed_rows(everything), {})


class LockConflictTests(TransactionTestCase):
    """A retry after a deadlock starts from the level that rolled back."""

    def test_a_retry_climbs_on_from_the_aborted_level(self):
        nodes, leaf = _line('RETRY', 6, 100)
        Position.objects.filter(key=leaf.key).update(eval_cp=250)
        original = Position.objects.bulk_update
        calls = []

        def flaky(objs, fields, batch_size=None):
            calls.append(len(objs))
            if len(calls) == 2:
                raise _deadlock()
            return original(objs, fields, batch_size=batch_size)

        report = {}
        with mock.patch.object(Position.objects, 'bulk_update',
                               side_effect=flaky), \
                mock.patch.object(ingest.time, 'sleep'):
            ingest.backup_backed_evals_retrying(
                [leaf.key, nodes[-1].key], report=report)

        self.assertEqual(report['retries'], 1)
        # Seeding the original keys again would have found them written and
        # stopped there, with everything above them still at 100.
        self.assertEqual(_stored(nodes[0])[:3], (250, 'a1a2', 6))
        aborted = DBEvent.objects.get(kind='BACKED_GUARD')
        self.assertEqual(aborted.payload['reason'], 'aborted')
        self.assertEqual(aborted.payload['keys'], [nodes[-2].key])

    def test_a_conflict_that_does_not_go_away_is_raised(self):
        nodes, leaf = _line('STUCK', 3, 100)
        Position.objects.filter(key=leaf.key).update(eval_cp=250)

        with mock.patch.object(Position.objects, 'bulk_update',
                               side_effect=_deadlock()), \
                mock.patch.object(ingest.time, 'sleep') as slept:
            with self.assertRaises(OperationalError):
                ingest.backup_backed_evals_retrying(
                    [leaf.key, nodes[-1].key])

        self.assertEqual(slept.call_count, len(ingest.BACKED_RETRY_SLEEPS))


class RecascadeConflictTests(TestCase):

    def test_recascade_reports_a_conflict_and_goes_on(self):
        nodes, leaf = _line('RECASCADE', 3, 100)
        out = StringIO()

        with mock.patch.object(ingest, 'backup_backed_evals',
                               side_effect=_deadlock()):
            call_command('recascade_backed', '--keys',
                         f'{nodes[0].key},{nodes[1].key}', '--chunk', '1',
                         stdout=out)

        self.assertEqual(out.getvalue().count('conflicto de bloqueo'), 2)
        self.assertIn('2 trozos saltados', out.getvalue())
