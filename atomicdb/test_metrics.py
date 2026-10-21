from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import ingest, logic
from .metrics import reset_metrics_cache, worker_metrics
from .models import AnalysisTask, WorkerPing
from .testing import TestCase


class WorkerMetricTests(TestCase):

    def tearDown(self):
        reset_metrics_cache()

    def test_active_capacity_reported_nps_and_rolling_position_rate(self):
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
            position=pos, generation=0, budget_nodes=1_200_000,
            nodes_searched=1_200_000, state='COMPLETED',
            completed=now - timedelta(minutes=2))

        metrics = worker_metrics(now=now)

        self.assertEqual(metrics['workers'], 1)
        self.assertEqual(metrics['cores'], 8)
        self.assertEqual(metrics['nps_reported'], 1_500_000)
        self.assertEqual(metrics['nps'], 2_000)
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
        self.assertEqual(metrics['nps_reported'], 0)

    def test_live_worker_with_stale_nps_is_capacity_not_reported_speed(self):
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
        self.assertEqual(metrics['nps_reported'], 0)

    def test_pending_and_old_completions_do_not_inflate_rate(self):
        now = timezone.now()
        first = ingest.get_or_create_position(logic.start_fen())
        second = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        AnalysisTask.objects.create(
            position=first, generation=0, budget_nodes=9_000_000_000,
            nodes_searched=9_000_000_000, state='COMPLETED',
            completed=now - timedelta(minutes=11))
        AnalysisTask.objects.create(
            position=second, generation=0, budget_nodes=9_000_000_000,
            nodes_searched=9_000_000_000, state='PENDING')

        metrics = worker_metrics(now=now)

        self.assertEqual(metrics['positions_per_minute'], 0)
        self.assertEqual(metrics['nps'], 0)

    def test_nps_is_the_nodes_delivered_in_the_window_per_second(self):
        """Nodes of the analyses COMPLETED in the last 10 minutes, over 600 s.

        Only completed analyses inside the window count.  One that completed
        eleven minutes ago, one still leased and one still pending add
        nothing, whatever their node counts say.
        """
        now = timezone.now()
        start = logic.start_fen()
        positions = [
            ingest.get_or_create_position(logic.apply_move(start, uci))
            for uci in logic.legal_moves(start)[:5]]
        rows = (
            (3_000_000_000, 'COMPLETED', now - timedelta(minutes=1)),
            (600_000_000, 'COMPLETED', now - timedelta(minutes=9)),
            (9_000_000_000, 'COMPLETED', now - timedelta(minutes=11)),
            (7_000_000_000, 'LEASED', None),
            (5_000_000_000, 'PENDING', None),
        )
        for pos, (nodes, state, completed) in zip(positions, rows):
            AnalysisTask.objects.create(
                position=pos, generation=0, budget_nodes=nodes,
                nodes_searched=nodes, state=state, completed=completed)

        metrics = worker_metrics(now=now)

        self.assertEqual(metrics['nps'], (3_000_000_000 + 600_000_000) / 600)
        self.assertEqual(metrics['positions_per_minute'], 0.2)

    def test_nps_does_not_fall_to_zero_when_no_worker_is_mid_search(self):
        """What the home page used to show: a heartbeat sum of zero.

        One live worker sits between two tasks, the other runs a build that
        never sends ``nps`` with its heartbeat.  The heartbeat sum is zero,
        as it always was; the public number is what they delivered.
        """
        now = timezone.now()
        between = WorkerPing.objects.create(
            machine='between-tasks', user='u', threads=32,
            current_task_id=None)
        silent = WorkerPing.objects.create(
            machine='no-nps-build', user='u', threads=32,
            current_task_id=202)
        WorkerPing.objects.filter(pk__in=(between.pk, silent.pk)).update(
            last_seen=now)
        pos = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(
            position=pos, generation=0, budget_nodes=33_600_000_000,
            nodes_searched=33_600_000_000, state='COMPLETED',
            completed=now - timedelta(minutes=3))

        metrics = worker_metrics(now=now)

        self.assertEqual(metrics['workers'], 2)
        self.assertEqual(metrics['nps_reported'], 0)
        self.assertEqual(metrics['nps'], 56_000_000)

    def test_the_delivered_rate_costs_no_extra_query(self):
        """The live pings and ONE aggregate, as before the delivered rate."""
        now = timezone.now()
        pos = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(
            position=pos, generation=0, budget_nodes=600_000,
            nodes_searched=600_000, state='COMPLETED', completed=now)

        with self.assertNumQueries(2, using=settings.ATOMICDB_DATABASE_ALIAS):
            metrics = worker_metrics(now=now)

        self.assertEqual(metrics['nps'], 1_000)
        self.assertEqual(metrics['positions_per_minute'], 0.1)


class HomeSpeedTests(TestCase):
    """What the home page prints under "nodes / sec", and what it says it is."""

    def setUp(self):
        # The view reads worker_metrics() through its 30-second process
        # cache: another test's home page must not answer for this one.
        reset_metrics_cache()

    def tearDown(self):
        reset_metrics_cache()

    def test_the_home_page_shows_the_delivered_rate_and_says_so(self):
        now = timezone.now()
        ping = WorkerPing.objects.create(
            machine='between-tasks', user='u', threads=32,
            current_task_id=None)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=now)
        pos = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(
            position=pos, generation=0, budget_nodes=33_600_000_000,
            nodes_searched=33_600_000_000, state='COMPLETED',
            completed=now - timedelta(minutes=3))

        response = self.client.get('/atomicdb/')

        self.assertEqual(response.context['compute_nps_h'], '56.0M')
        self.assertContains(
            response, 'Nodes searched in the analyses completed over the '
                      'last 10 minutes, per second')
        self.assertNotContains(response, 'latest reported engine speed')
