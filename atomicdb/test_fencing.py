import json
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from . import ingest, logic
from .database import connection
from .models import AnalysisTask, Position
from .testing import TestCase, TransactionTestCase


class SubmitFencingTests(TestCase):

    def setUp(self):
        User.objects.create_user('worker', password='secret')
        self.position = ingest.get_or_create_position(logic.start_fen())
        self.base = {
            'username': 'worker',
            'password': 'secret',
            'machine': 'machine-a',
            'lines': '[]',
        }

    def _task(self, state='PENDING', machine='', position=None):
        return AnalysisTask.objects.create(
            position=position or self.position,
            generation=AnalysisTask.objects.count(),
            budget_nodes=1_000,
            state=state,
            machine=machine,
            leased_at=timezone.now() if state == 'LEASED' else None,
        )

    def _submit(self, task, **extra):
        return self.client.post(
            '/atomicdb/api/submit',
            dict(self.base, task_id=task.id, **extra),
        )

    def test_pending_task_is_rejected(self):
        task = self._task()
        response = self._submit(task)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'not-leased')
        task.refresh_from_db()
        self.assertEqual(task.state, 'PENDING')

    def test_other_machine_is_rejected(self):
        task = self._task('LEASED', 'machine-a')
        response = self._submit(task, machine='machine-b')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'not-your-lease')
        task.refresh_from_db()
        self.assertEqual(task.state, 'LEASED')

    def test_duplicate_submit_is_idempotent(self):
        task = self._task('LEASED', 'machine-a')
        first = self._submit(task)
        self.assertEqual(first.status_code, 200)
        with mock.patch('atomicdb.ingest.prepare_mate_proofs',
                        side_effect=AssertionError('must not recompute')):
            second = self._submit(task)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {'ok': True, 'dup': True})

    def test_oversized_pv_is_rejected_before_claiming_the_task(self):
        task = self._task('LEASED', 'machine-a')
        response = self._submit(task, lines=json.dumps([{
            'move': 'e2e4', 'pv': ['e2e4'] * 513,
            'eval_cp': 12, 'mate': None,
        }]))

        self.assertEqual(response.status_code, 400)
        self.assertIn('excessively long PV', response.json()['error'])
        task.refresh_from_db()
        self.assertEqual(task.state, 'LEASED')

    def test_reported_nodes_are_clamped_to_twice_budget(self):
        task = self._task('LEASED', 'machine-a')
        response = self._submit(task, nodes='999999')
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.nodes_searched, 2_000)
        position = Position.objects.get(key=self.position.key)
        self.assertEqual(position.nodes_invested, 2_000)

    def test_zero_reported_nodes_remain_zero(self):
        task = self._task('LEASED', 'machine-a')
        response = self._submit(task, nodes='0')
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.position.refresh_from_db()
        self.assertEqual(task.nodes_searched, 0)
        self.assertEqual(self.position.nodes_invested, 0)

    def test_same_machine_release_during_proof_is_stale(self):
        task = self._task('LEASED', 'machine-a')

        def re_lease(fen, lines):
            AnalysisTask.objects.filter(id=task.id).update(
                attempts=task.attempts + 1, leased_at=timezone.now())
            return {}

        with mock.patch('atomicdb.ingest.prepare_mate_proofs',
                        side_effect=re_lease):
            response = self._submit(task, nodes='123')

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'stale-lease')
        task.refresh_from_db()
        self.position.refresh_from_db()
        self.assertEqual(task.state, 'LEASED')
        self.assertEqual(task.attempts, 1)
        self.assertEqual(self.position.visits, 0)
        self.assertEqual(self.position.nodes_invested, 0)

    def test_nodes_use_authoritative_budget_after_claim(self):
        task = self._task('LEASED', 'machine-a')

        def resize_budget(fen, lines):
            AnalysisTask.objects.filter(id=task.id).update(budget_nodes=100)
            return {}

        with mock.patch('atomicdb.ingest.prepare_mate_proofs',
                        side_effect=resize_budget):
            response = self._submit(task, nodes='999')

        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.position.refresh_from_db()
        self.assertEqual(task.nodes_searched, 200)
        self.assertEqual(self.position.nodes_invested, 200)

    def test_machine_name_has_no_reserved_submit_prefix(self):
        task = self._task('LEASED', '@submit:client')
        response = self._submit(task, machine='@submit:client')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('dup', response.json())

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=-2)
    def test_rejected_tb_does_not_account_elapsed_time(self, probe):
        tb_position = ingest.get_or_create_position(
            '7k/8/8/8/8/8/8/K6R w - - 0 1')
        task = self._task('LEASED', 'machine-a', position=tb_position)

        response = self._submit(
            task, tb_wdl='2', elapsed='86400', nodes='1000')

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'tb-rejected')
        task.refresh_from_db()
        tb_position.refresh_from_db()
        self.assertEqual(task.state, 'LEASED')
        self.assertEqual(task.machine, 'machine-a')
        self.assertEqual(task.nodes_searched, 0)
        self.assertEqual(tb_position.time_invested, 0)
        probe.assert_called_once()


class SubmitPreparationBoundaryTests(TransactionTestCase):
    """Expensive proof and TB I/O must finish before the write transaction."""

    reset_sequences = True

    def setUp(self):
        User.objects.create_user('worker', password='secret')

    def _leased_task(self, fen):
        position = ingest.get_or_create_position(fen)
        task = AnalysisTask.objects.create(
            position=position, generation=0, budget_nodes=1_000,
            state='LEASED', machine='machine-a', leased_at=timezone.now())
        return position, task

    def _submit(self, task, **extra):
        payload = {
            'username': 'worker', 'password': 'secret',
            'machine': 'machine-a', 'task_id': task.id,
            'lines': '[]',
        }
        payload.update(extra)
        return self.client.post('/atomicdb/api/submit', payload)

    def test_andor_preparation_runs_outside_atomic_block(self):
        _, task = self._leased_task(logic.start_fen())

        def prepare(fen, lines):
            self.assertFalse(connection.in_atomic_block)
            return {}

        with mock.patch('atomicdb.ingest.prepare_mate_proofs',
                        side_effect=prepare) as proof:
            response = self._submit(task)

        self.assertEqual(response.status_code, 200)
        proof.assert_called_once()

    def test_tb_probe_runs_outside_atomic_block(self):
        _, task = self._leased_task(
            '7k/8/8/8/8/8/8/K6R w - - 0 1')

        def probe(fen, max_pieces):
            self.assertFalse(connection.in_atomic_block)
            return 0

        with mock.patch('atomicdb.ingest.tb.probe_wdl',
                        side_effect=probe) as tb_probe:
            response = self._submit(task, tb_wdl='0')

        self.assertEqual(response.status_code, 200)
        tb_probe.assert_called_once()
