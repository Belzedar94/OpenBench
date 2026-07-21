import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import ingest, logic
from .models import AnalysisTask, Position


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

    def _task(self, state='PENDING', machine=''):
        return AnalysisTask.objects.create(
            position=self.position,
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
        second = self._submit(task)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {'ok': True, 'dup': True})

    def test_reported_nodes_are_clamped_to_twice_budget(self):
        task = self._task('LEASED', 'machine-a')
        response = self._submit(task, nodes='999999')
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.nodes_searched, 2_000)
        position = Position.objects.get(key=self.position.key)
        self.assertEqual(position.nodes_invested, 2_000)
