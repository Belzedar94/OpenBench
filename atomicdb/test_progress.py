import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command

from .management.commands.capture_atomicdb_progress import (
    capture_progress,
    utc_hour,
)
from .models import AnalysisTask, DBEvent, Position, ProgressSnapshot, WorkerPing
from .testing import TestCase


UTC = datetime_timezone.utc


class ProgressCaptureTests(TestCase):

    def setUp(self):
        self.now = datetime(2026, 7, 23, 10, 17, 41, tzinfo=UTC)
        unknown = Position.objects.create(
            key='unknown', fen='unknown w - - 0 1', expanded=True)
        unknown_two = Position.objects.create(
            key='unknown-two', fen='unknown-two b - - 0 1')
        terminal = Position.objects.create(
            key='terminal', fen='terminal w - - 0 1', status='DRAW',
            closure='TERMINAL', expanded=True)
        Position.objects.create(
            key='tb', fen='tb w - - 0 1', status='WHITE_WIN', closure='TB')
        Position.objects.create(
            key='andor', fen='andor w - - 0 1', status='WHITE_WIN',
            closure='MATE_PV', proof='ANDOR')
        Position.objects.create(
            key='engine', fen='engine b - - 0 1', status='BLACK_WIN',
            closure='MINIMAX', proof='ENGINE')
        Position.objects.create(
            key='disputed', fen='disputed w - - 0 1', status='DRAW',
            closure='MATE_PV', proof='DISPUTED')
        Position.objects.create(
            key='unclassified', fen='unclassified w - - 0 1',
            status='DRAW')

        AnalysisTask.objects.create(
            position=unknown, generation=0, budget_nodes=200,
            nodes_searched=123, elapsed_seconds=1.5, state='COMPLETED',
            attempts=3, completed=self.now - timedelta(minutes=5))
        AnalysisTask.objects.create(
            position=unknown_two, generation=0, budget_nodes=200,
            state='PENDING')
        AnalysisTask.objects.create(
            position=terminal, generation=0, budget_nodes=200,
            state='LEASED', attempts=2, leased_at=self.now)

        DBEvent.objects.create(kind='TB_REJECTED', payload={'reason': 'one'})
        DBEvent.objects.create(kind='TB_REJECTED', payload={'reason': 'two'})
        DBEvent.objects.create(kind='CASCADE_GUARD', payload={})

        busy = WorkerPing.objects.create(
            machine='busy', user='u', threads=8, current_task_id=1,
            last_nps=1_000_000, nps_updated=self.now - timedelta(seconds=10))
        idle = WorkerPing.objects.create(
            machine='idle', user='u', threads=4, current_task_id=None,
            last_nps=900_000, nps_updated=self.now - timedelta(seconds=10))
        stale = WorkerPing.objects.create(
            machine='stale', user='u', threads=32, current_task_id=1,
            last_nps=5_000_000, nps_updated=self.now - timedelta(minutes=5))
        WorkerPing.objects.filter(pk__in=(busy.pk, idle.pk)).update(
            last_seen=self.now)
        WorkerPing.objects.filter(pk=stale.pk).update(
            last_seen=self.now - timedelta(minutes=5))

    def test_capture_records_only_observable_cumulative_counters(self):
        snapshot, created = capture_progress(now=self.now)

        self.assertTrue(created)
        self.assertEqual(snapshot.bucket_start,
                         datetime(2026, 7, 23, 10, 0, tzinfo=UTC))
        # Nine, not eight: migration 0018 seeds the root proof campaign, and
        # a campaign needs its root position to exist.  That row is UNKNOWN
        # and unexpanded, so it lands in total and unknown only.
        self.assertEqual(
            (snapshot.positions_total, snapshot.positions_unknown,
             snapshot.positions_closed, snapshot.positions_expanded),
            (9, 3, 6, 2))
        self.assertEqual(
            (snapshot.engine_nodes_total, snapshot.engine_seconds_total,
             snapshot.analyses_completed),
            (123, 1.5, 1))
        self.assertEqual(
            (snapshot.tasks_pending, snapshot.tasks_leased,
             snapshot.tasks_retried, snapshot.lease_retries_total,
             snapshot.recorded_rejections_total),
            (1, 1, 2, 3, 2))
        self.assertEqual(
            (snapshot.active_workers, snapshot.active_threads,
             snapshot.active_nps),
            (2, 12, 1_000_000))
        self.assertEqual(
            (snapshot.closure_terminal, snapshot.closure_tb,
             snapshot.closure_mate_pv, snapshot.closure_minimax,
             snapshot.closure_unclassified),
            (1, 1, 2, 1, 1))
        self.assertEqual(
            (snapshot.trust_verified, snapshot.trust_andor,
             snapshot.trust_engine, snapshot.trust_disputed,
             snapshot.trust_unclassified),
            (2, 1, 1, 1, 1))

    def test_same_hour_is_idempotent_and_never_rewrites_history(self):
        first, created = capture_progress(now=self.now)
        self.assertTrue(created)
        Position.objects.create(key='later', fen='later w - - 0 1')

        second, created = capture_progress(
            now=self.now + timedelta(minutes=40))

        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.positions_total, 9)
        self.assertEqual(ProgressSnapshot.objects.count(), 1)

    def test_snapshot_instance_cannot_be_updated_or_deleted(self):
        snapshot, _ = capture_progress(now=self.now)
        snapshot.positions_total = 999

        with self.assertRaisesMessage(ValidationError, 'append-only'):
            snapshot.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            snapshot.delete()

    def test_next_hour_captures_a_new_observation(self):
        first, _ = capture_progress(now=self.now)
        Position.objects.create(key='later', fen='later w - - 0 1')

        second, created = capture_progress(now=self.now + timedelta(hours=1))

        self.assertTrue(created)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.positions_total, 10)
        self.assertEqual(ProgressSnapshot.objects.count(), 2)

    def test_naive_capture_time_is_rejected(self):
        with self.assertRaisesMessage(
                ValueError, 'requires an aware datetime'):
            utc_hour(datetime(2026, 7, 23, 10, 17))

    def test_bucket_is_normalized_to_utc_before_truncation(self):
        madrid = datetime_timezone(timedelta(hours=2))

        bucket = utc_hour(datetime(
            2026, 7, 23, 10, 59, 59, tzinfo=madrid))

        self.assertEqual(
            bucket, datetime(2026, 7, 23, 8, 0, tzinfo=UTC))

    @mock.patch(
        'atomicdb.management.commands.capture_atomicdb_progress.timezone.now')
    def test_command_emits_machine_readable_idempotent_receipt(self, now):
        now.return_value = self.now
        output = StringIO()

        call_command('capture_atomicdb_progress', '--json', stdout=output)
        first = json.loads(output.getvalue())
        output = StringIO()
        call_command('capture_atomicdb_progress', '--json', stdout=output)
        second = json.loads(output.getvalue())

        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(
            first['snapshot']['bucket_start'], '2026-07-23T10:00:00+00:00')
        self.assertEqual(first['snapshot'], second['snapshot'])
