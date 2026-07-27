"""The global selector pass no longer lives inside a worker request (P0d).

``refresh_priorities`` is O(whole graph): a Dijkstra plus two dictionaries
holding every position and every edge.  It used to run inline in
``/atomicdb/api/lease``, once per gunicorn process.  These tests pin the three
things that matter: the lease path does not trigger it, the service does, and
the emergency flag brings the old behaviour back through the same code.
"""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import override_settings

from . import ingest, logic
from .models import AnalysisTask, Position
from .testing import TestCase


class SelectorOutOfRequestTests(TestCase):

    def setUp(self):
        User.objects.create_user('worker', password='pw')
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)

    def _lease(self):
        return self.client.post('/atomicdb/api/lease', {
            'username': 'worker', 'password': 'pw', 'machine': 'm1',
            'worker_build': 2026072203, 'threads': 1, 'hash': 64})

    def test_next_tasks_does_not_run_the_global_pass(self):
        with patch('atomicdb.ingest.refresh_priorities') as refresh:
            ingest.next_tasks(2)
        refresh.assert_not_called()

    def test_lease_does_not_run_the_global_pass(self):
        with patch('atomicdb.ingest._regret_from_root') as dijkstra:
            response = self._lease()
        self.assertEqual(response.status_code, 200)
        dijkstra.assert_not_called()
        # It still hands out work: the queue is refilled from the stored
        # priority column, exactly as before.
        self.assertEqual(len(response.json()['tasks']), 1)

    @override_settings(ATOMICDB_INLINE_SELECTOR=True)
    def test_emergency_flag_restores_the_inline_pass(self):
        ingest._priority_refresh_cache['at'] = 0.0
        with patch('atomicdb.ingest._regret_from_root',
                   return_value={}) as dijkstra:
            ingest.next_tasks(2)
        dijkstra.assert_called_once()

    def test_flag_defaults_to_off(self):
        self.assertFalse(ingest.inline_selector_enabled())


class RefreshSelectorCommandTests(TestCase):

    def setUp(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)

    def test_command_runs_the_pass_once(self):
        ingest._priority_refresh_cache['at'] = 0.0
        out = StringIO()

        with patch('atomicdb.ingest._regret_from_root',
                   return_value={}) as dijkstra:
            call_command('refresh_selector', stdout=out)

        dijkstra.assert_called_once()
        self.assertIn('"pass": 1', out.getvalue())

    def test_command_ignores_the_process_local_cache(self):
        """The service's own interval is the only clock it obeys."""
        out = StringIO()
        with patch('atomicdb.ingest._regret_from_root', return_value={}):
            call_command('refresh_selector', stdout=out)
            with patch('atomicdb.ingest._regret_from_root',
                       return_value={}) as second:
                call_command('refresh_selector', stdout=out)
        second.assert_called_once()

    def test_command_writes_real_priorities(self):
        ingest._priority_refresh_cache['at'] = 0.0
        child = Position.objects.filter(status='UNKNOWN').exclude(
            key=logic.key_of(logic.start_fen())).first()
        child.eval_cp = 1200
        child.save(update_fields=['eval_cp'])

        call_command('refresh_selector', stdout=StringIO())

        child.refresh_from_db()
        self.assertGreater(child.priority, 0.0)

    def test_status_reports_the_live_queue(self):
        out = StringIO()
        call_command('refresh_selector', status=True, stdout=out)
        self.assertIn('"live"', out.getvalue())
        self.assertIn('"tombstoned"', out.getvalue())

    def test_stale_priorities_still_produce_tasks(self):
        """Correctness never depended on the refresh; relevance does."""
        Position.objects.filter(status='UNKNOWN').update(priority=0.0)
        with patch('atomicdb.ingest.refresh_priorities') as refresh:
            tasks = ingest.next_tasks(3)
        refresh.assert_not_called()
        self.assertEqual(len(tasks), 3)
        self.assertEqual(AnalysisTask.objects.filter(
            state='PENDING').count(), 3)
