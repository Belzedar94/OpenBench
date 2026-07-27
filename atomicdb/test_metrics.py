from datetime import timedelta

from django.utils import timezone

from . import ingest, logic
from .metrics import reset_metrics_cache, worker_metrics
from .models import AnalysisTask, WorkerPing
from .testing import TestCase


class WorkerMetricTests(TestCase):

    def tearDown(self):
        reset_metrics_cache()

    def test_active_capacity_nps_and_rolling_position_rate(self):
        now = timezone.now()
        live = WorkerPing.objects.create(
            machine='live', user='u', threads=8, last_nps=1_500_000,
            nps_updated=now - timedelta(seconds=10), current_task_id=101)
        stale = WorkerPing.objects.create(
            machine='stale', user='u', threads=32, last_nps=9_000_000,
            nps_updated=now - timedelta(minutes=5))
        WorkerPing.objects.filter(pk=live.pk).update(last_seen=now)
        WorkerPing.objects.filter(pk=stale.pk).update(
            last_seen=now - timedelta(minutes=5))
        pos = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(
            position=pos, generation=0, budget_nodes=1000,
            state='COMPLETED', completed=now - timedelta(minutes=2))

        metrics = worker_metrics(now=now)

        self.assertEqual(metrics['workers'], 1)
        self.assertEqual(metrics['cores'], 8)
        self.assertEqual(metrics['nps'], 1_500_000)
        self.assertEqual(metrics['positions_per_minute'], 0.1)

    def test_idle_live_worker_does_not_report_historical_nps(self):
        now = timezone.now()
        ping = WorkerPing.objects.create(
            machine='idle', user='u', threads=8, last_nps=1_500_000,
            nps_updated=now - timedelta(seconds=10), current_task_id=None)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=now)

        metrics = worker_metrics(now=now)

        self.assertEqual(metrics['workers'], 1)
        self.assertEqual(metrics['cores'], 8)
        self.assertEqual(metrics['nps'], 0)

    def test_live_worker_with_stale_nps_is_capacity_but_not_throughput(self):
        now = timezone.now()
        ping = WorkerPing.objects.create(
            machine='stalled-engine', user='u', threads=8,
            last_nps=1_500_000,
            nps_updated=now - timedelta(seconds=181),
            current_task_id=101)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=now)

        metrics = worker_metrics(now=now)

        self.assertEqual(metrics['workers'], 1)
        self.assertEqual(metrics['cores'], 8)
        self.assertEqual(metrics['nps'], 0)

    def test_pending_and_old_completions_do_not_inflate_rate(self):
        now = timezone.now()
        first = ingest.get_or_create_position(logic.start_fen())
        second = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        AnalysisTask.objects.create(
            position=first, generation=0, budget_nodes=1000,
            state='COMPLETED', completed=now - timedelta(minutes=11))
        AnalysisTask.objects.create(
            position=second, generation=0, budget_nodes=1000,
            state='PENDING')

        self.assertEqual(worker_metrics(now=now)['positions_per_minute'], 0)
