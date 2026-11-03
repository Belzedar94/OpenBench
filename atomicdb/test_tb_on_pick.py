"""A position the server's own tables cover is closed from them, never served.

Before this, a worker without the tables (or with only the 3-4-5 set) was
served a covered position, searched it in full, and only the ingest closed
it from the server's tables (§ ingest.prepare_server_tb_closure).  Now the
lease skips it and the ingest process closes it with no search at all.
"""

import json
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from . import ingest, ingest_queue, logic
from .models import AnalysisTask, RequestNotification
from .testing import TestCase, worker_account


SIX_MEN = '4k3/8/8/8/8/8/PPP5/KQ6 w - - 0 1'


@override_settings(ATOMICDB_TB_SERVER_MAX_MEN=6)
class TablesSettleQueuedPositionsTests(TestCase):

    def setUp(self):
        worker_account('u', 'p')
        self.covered = ingest.get_or_create_position(SIX_MEN)
        self.normal = ingest.get_or_create_position(logic.start_fen())

    def _lease(self, tb='0'):
        response = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'tb': tb, 'machine': 'm1'})
        return [task['fen'] for task in json.loads(response.content)['tasks']]

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=2)
    def test_a_covered_position_is_served_to_no_worker(self, probe):
        AnalysisTask.objects.create(position=self.covered, budget_nodes=1000)
        # An empty queue refills itself from the selector, so the lease may
        # still hand out something; never the covered position.
        self.assertNotIn(SIX_MEN, self._lease(tb='1'))
        probe.assert_any_call(SIX_MEN, max_pieces=6)

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=2)
    def test_the_lease_goes_on_to_the_next_task(self, probe):
        del probe
        AnalysisTask.objects.create(position=self.covered, budget_nodes=1000,
                                    source=AnalysisTask.Source.USER,
                                    requested_by='u')
        AnalysisTask.objects.create(position=self.normal, budget_nodes=1000)
        self.assertEqual(self._lease(), [self.normal.fen])

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=None)
    def test_a_probe_that_fails_leaves_the_task_served(self, probe):
        del probe
        AnalysisTask.objects.create(position=self.covered, budget_nodes=1000)
        self.assertEqual(self._lease(tb='1'), [SIX_MEN])

    @override_settings(ATOMICDB_TB_SERVER_MAX_MEN=5)
    @mock.patch('atomicdb.ingest.tb.probe_wdl')
    def test_a_position_beyond_the_tables_never_reaches_the_disk(self, probe):
        AnalysisTask.objects.create(position=self.covered, budget_nodes=1000)
        self.assertEqual(self._lease(tb='1'), [SIX_MEN])
        probe.assert_not_called()

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=2)
    def test_the_sweep_closes_it_and_tells_the_requester(self, probe):
        del probe
        task = AnalysisTask.objects.create(
            position=self.covered, budget_nodes=1000,
            source=AnalysisTask.Source.USER, requested_by='u')

        self.assertEqual(ingest_queue.close_queued_by_tables(), 1)

        self.covered.refresh_from_db()
        self.assertEqual((self.covered.status, self.covered.closure),
                         ('WHITE_WIN', 'TB'))
        task.refresh_from_db()
        self.assertEqual((task.state, task.machine), ('COMPLETED', ''))
        self.assertTrue(RequestNotification.objects.filter(
            task_id=task.pk, username='u').exists())

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=2)
    def test_the_sweep_leaves_other_positions_alone(self, probe):
        AnalysisTask.objects.create(position=self.normal, budget_nodes=1000)
        self.assertEqual(ingest_queue.close_queued_by_tables(), 0)
        probe.assert_not_called()

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=None)
    def test_the_sweep_closes_nothing_the_tables_cannot_answer(self, probe):
        del probe
        AnalysisTask.objects.create(position=self.covered, budget_nodes=1000)
        self.assertEqual(ingest_queue.close_queued_by_tables(), 0)
        self.covered.refresh_from_db()
        self.assertEqual(self.covered.status, 'UNKNOWN')

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=0)
    def test_the_processor_runs_the_sweep(self, probe):
        del probe
        AnalysisTask.objects.create(position=self.covered, budget_nodes=1000)
        from io import StringIO
        out = StringIO()

        call_command('process_ingest_queue', once=True, stdout=out)

        self.assertEqual(json.loads(out.getvalue().strip()),
                         {'done': 0, 'failed': 0, 'tb_closed': 1})
        self.covered.refresh_from_db()
        self.assertEqual(self.covered.status, 'DRAW')
