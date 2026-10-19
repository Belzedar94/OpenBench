import hashlib
import io
import json
import os
import tempfile

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from . import engine_policy, ingest, logic
from .models import AnalysisTask
from .testing import TestCase, worker_account


class EngineGateTests(TestCase):

    def setUp(self):
        worker_account('worker', 'secret')
        self.position = ingest.get_or_create_position(logic.start_fen())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engines = os.path.join(tmp.name, 'engines')
        os.mkdir(self.engines)
        self.reference = self._binary('Atomic-Stockfish-win-x86_64-pgo.exe',
                                      b'reference build')
        self.previous = self._binary('Atomic-Stockfish-win-x86_64.exe',
                                     b'previous build')
        self.worker_script = self._binary('atomicdb_worker.py',
                                          b'not an engine')
        self.foreign = hashlib.sha256(b'some other engine').hexdigest()
        self.policy = os.path.join(tmp.name, 'policy.json')
        overrides = override_settings(
            ATOMICDB_ENGINES_DIR=self.engines,
            ATOMICDB_ENGINE_POLICY_FILE=self.policy)
        overrides.enable()
        self.addCleanup(overrides.disable)
        engine_policy._digests.clear()
        engine_policy._policies.clear()

    def _binary(self, name, content):
        with open(os.path.join(self.engines, name), 'wb') as handle:
            handle.write(content)
        return hashlib.sha256(content).hexdigest()

    def _write_policy(self, text):
        with open(self.policy, 'w', encoding='utf-8') as handle:
            handle.write(text)
        engine_policy._policies.clear()

    def _enforce(self, **extra):
        self._write_policy(json.dumps(dict(mode='enforce', **extra)))

    def _task(self, machine='machine-a'):
        return AnalysisTask.objects.create(
            position=self.position,
            generation=AnalysisTask.objects.count(),
            budget_nodes=1_000,
            state='LEASED',
            machine=machine,
            leased_at=timezone.now(),
        )

    def _submit(self, task, machine='machine-a', **extra):
        return self.client.post('/atomicdb/api/submit', dict(
            username='worker', password='secret', machine=machine,
            lines='[]', task_id=task.id, **extra))

    def test_the_gate_is_off_without_a_policy_file(self):
        task = self._task()
        response = self._submit(task, engine_sha=self.foreign)
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.state, 'COMPLETED')
        self.assertEqual(task.engine_sha, self.foreign)

    def test_a_broken_or_unknown_policy_reads_as_off(self):
        for text in ('{not json', '"enforce"', '{"mode": "Enforce"}',
                     '{"mode": "observe"}'):
            self._write_policy(text)
            self.assertEqual(engine_policy.load_policy()['mode'], 'off', text)
            self.assertFalse(engine_policy.rejects(self.foreign), text)

    def test_current_and_previous_reference_builds_are_accepted(self):
        self._enforce()
        for sha in (self.reference, self.previous):
            task = self._task()
            response = self._submit(task, engine_sha=sha)
            self.assertEqual(response.status_code, 200, sha)
            task.refresh_from_db()
            self.assertEqual(task.state, 'COMPLETED')

    def test_other_files_in_the_engines_directory_are_not_engines(self):
        self._enforce()
        self.assertTrue(engine_policy.rejects(self.worker_script))
        self.assertEqual(set(engine_policy.reference_engines()),
                         {self.reference, self.previous})

    def test_a_foreign_engine_is_refused_and_the_task_goes_back(self):
        self._enforce()
        task = self._task()
        response = self._submit(task, engine_sha=self.foreign)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body['error'], 'engine-not-accepted')
        self.assertIn('reference engine', body['detail'])
        task.refresh_from_db()
        self.assertEqual(task.state, 'PENDING')
        self.assertEqual(task.machine, '')
        self.assertEqual(task.lease_token, '')
        self.assertEqual(task.engine_sha, '')

    def test_a_missing_sha_is_refused_while_enforcing(self):
        self._enforce()
        task = self._task()
        response = self._submit(task)
        self.assertEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.state, 'PENDING')

    def test_an_extra_engine_listed_in_the_policy_is_accepted(self):
        self._enforce(extra_engines={self.foreign.upper(): 'checked build',
                                     'not-a-sha': 'ignored'})
        self.assertEqual(engine_policy.load_policy()['extra_engines'],
                         {self.foreign: 'checked build'})
        task = self._task()
        response = self._submit(task, engine_sha=self.foreign)
        self.assertEqual(response.status_code, 200)

    def test_tablebase_results_are_never_gated(self):
        self._enforce()
        task = self._task()
        response = self._submit(task, engine_sha=self.foreign, tb_wdl='0')
        self.assertNotEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.state, 'COMPLETED')

    def test_another_machine_cannot_push_a_lease_back_with_a_bad_sha(self):
        self._enforce()
        task = self._task('machine-a')
        response = self._submit(task, machine='machine-b',
                                engine_sha=self.foreign)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'not-your-lease')
        task.refresh_from_db()
        self.assertEqual(task.state, 'LEASED')
        self.assertEqual(task.machine, 'machine-a')

    def test_no_reference_binary_lets_everything_through(self):
        self._enforce()
        for name in os.listdir(self.engines):
            os.remove(os.path.join(self.engines, name))
        self.assertFalse(engine_policy.rejects(self.foreign))
        task = self._task()
        response = self._submit(task, engine_sha=self.foreign)
        self.assertEqual(response.status_code, 200)

    def test_a_replaced_binary_is_hashed_again(self):
        self._enforce()
        self.assertFalse(engine_policy.rejects(self.reference))
        replaced = self._binary('Atomic-Stockfish-win-x86_64-pgo.exe',
                                b'rebuilt reference, longer than before')
        self.assertTrue(engine_policy.rejects(self.reference))
        self.assertFalse(engine_policy.rejects(replaced))

    def test_the_report_marks_what_the_gate_would_refuse(self):
        for sha, account in ((self.reference, 'alice'),
                             (self.foreign, 'bob')):
            AnalysisTask.objects.create(
                position=self.position,
                generation=AnalysisTask.objects.count(),
                budget_nodes=1_000, state='COMPLETED',
                completed=timezone.now(), nodes_searched=1_000,
                delivered_by=account, engine_sha=sha)
        out = io.StringIO()
        call_command('engine_gate_report', '--days', '1', stdout=out)
        text = out.getvalue()
        self.assertIn('gate mode: off', text)
        self.assertRegex(text, r'ok\s+alice')
        self.assertRegex(text, r'REFUSED\s+bob')
        self.assertIn('would refuse 1 of 2 tasks', text)
