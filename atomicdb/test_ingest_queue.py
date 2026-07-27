"""Ingesta asincrona: el submit encola y responde, el procesador aplica.

Lo que se defiende aqui:
  * el contrato con el worker no se mueve (Client/atomicdb_worker.py exige un
    2xx con ``ok`` verdadero, y trata cualquier 4xx como definitivo);
  * el resultado por la cola es IDENTICO al del camino sincrono (A/B sobre el
    mismo fixture);
  * un fallo reintenta con backoff y acaba en FAILED con su payload intacto;
  * aplicar dos veces el mismo payload no duplica nada;
  * un lease no puede caducar mientras su payload espera en la cola.
"""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from . import ingest, ingest_queue, logic
from .models import AnalysisTask, Edge, IngestJob, Position, ProofCampaign
from .testing import TestCase


def _worker():
    User.objects.create_user(username='w', password='p')
    return {'username': 'w', 'password': 'p', 'machine': 'm1'}


def _lease(client, auth, **extra):
    payload = dict(auth, worker_build='2026072203',
                   lease_session='session-m1')
    payload.update(extra)
    return client.post('/atomicdb/api/lease', payload).json()


def _analysis_lines(position, value=-320):
    return [{'move': edge.move_uci, 'eval_cp': value, 'pv': [edge.move_uci]}
            for edge in Edge.objects.filter(parent=position)
                                    .order_by('move_uci')[:5]]


class SubmitEnqueueTests(TestCase):

    def setUp(self):
        self.auth = _worker()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        self.task = AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=1_000_000)

    def _submit(self, **extra):
        leased = _lease(self.client, self.auth)['tasks'][0]
        payload = dict(self.auth, task_id=leased['id'],
                       lines=json.dumps(_analysis_lines(self.target)),
                       nodes='900000', elapsed='12.5',
                       lease_token=leased['lease_token'])
        payload.update(extra)
        return self.client.post('/atomicdb/api/submit', payload), leased

    def test_a_submit_acknowledges_and_leaves_the_work_queued(self):
        response, leased = self._submit()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])                 # el contrato del worker
        self.assertTrue(body['summary']['queued'])
        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'PENDING')
        self.assertEqual(job.task_id, leased['id'])
        self.assertEqual(job.position_id, self.target.key)
        self.assertEqual(job.payload['nodes'], 900_000)
        self.assertEqual(job.payload['elapsed'], 12.5)
        self.assertEqual(job.payload['username'], 'w')

    def test_the_submit_itself_does_not_touch_the_tree(self):
        self._submit()

        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 0)
        self.assertIsNone(self.target.eval_cp)

    def test_the_claim_and_the_enqueue_are_one_commit(self):
        # Un lease no puede caducar con el payload esperando: la tarea ya esta
        # COMPLETED, asi que nadie la vuelve a arrendar.
        _response, leased = self._submit()

        task = AnalysisTask.objects.get(id=leased['id'])
        self.assertEqual(task.state, 'COMPLETED')
        self.assertEqual(task.nodes_searched, 900_000)
        self.assertEqual(task.elapsed_seconds, 12.5)
        self.assertTrue(IngestJob.objects.filter(task=task).exists())
        again = _lease(Client(), self.auth, machine='m2',
                       lease_session='session-m2')['tasks']
        self.assertNotIn(leased['id'], [row['id'] for row in again])

    def test_a_replayed_submit_is_still_a_duplicate(self):
        response, leased = self._submit()
        del response

        again = self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(self.target)),
            lease_token=leased['lease_token']))

        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()['dup'])
        self.assertEqual(IngestJob.objects.count(), 1)

    def test_the_definitive_rejections_are_unchanged(self):
        leased = _lease(self.client, self.auth)['tasks'][0]
        cases = (
            ({'machine': 'someone-else'}, 409, 'not-your-lease'),
            ({'lease_token': 'wrong'}, 409, 'stale-lease'),
            ({'lines': 'not json'}, 400, None),
        )
        for extra, status, error in cases:
            with self.subTest(extra=extra):
                payload = dict(self.auth, task_id=leased['id'],
                               lines=json.dumps([]),
                               lease_token=leased['lease_token'])
                payload.update(extra)
                response = self.client.post('/atomicdb/api/submit', payload)
                self.assertEqual(response.status_code, status)
                if error:
                    self.assertEqual(response.json()['error'], error)
        self.assertEqual(IngestJob.objects.count(), 0)


class ProcessorTests(TestCase):

    def setUp(self):
        self.auth = _worker()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        self.task = AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=1_000_000)

    def _submit(self):
        leased = _lease(self.client, self.auth)['tasks'][0]
        return self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(self.target)),
            nodes='900000', elapsed='12.5',
            lease_token=leased['lease_token']))

    def test_the_processor_applies_what_the_submit_queued(self):
        self._submit()

        result = ingest_queue.drain()

        self.assertEqual(result, {'done': 1, 'failed': 0})
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)
        self.assertEqual(self.target.eval_cp, -320)
        self.assertEqual(self.target.time_invested, 12.5)
        self.assertEqual(self.target.nodes_invested, 900_000)
        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'DONE')
        self.assertIn('backed_evals', job.summary)

    def test_processing_twice_changes_nothing(self):
        self._submit()
        ingest_queue.drain()
        self.target.refresh_from_db()
        before = (self.target.visits, self.target.nodes_invested,
                  self.target.time_invested, self.target.eval_cp)

        job = IngestJob.objects.get()
        ingest_queue.apply_job(job)         # replay directo
        ingest_queue.drain()                # y por la via normal

        self.target.refresh_from_db()
        self.assertEqual(
            (self.target.visits, self.target.nodes_invested,
             self.target.time_invested, self.target.eval_cp), before)
        self.assertEqual(IngestJob.objects.count(), 1)

    def test_a_failure_retries_with_backoff_and_then_parks(self):
        self._submit()
        boom = mock.patch.object(ingest, 'ingest_analysis',
                                 side_effect=RuntimeError('disk on fire'))
        with boom:
            for attempt in range(1, ingest_queue.MAX_ATTEMPTS + 1):
                job = IngestJob.objects.get()
                IngestJob.objects.filter(pk=job.pk).update(
                    next_attempt_at=timezone.now())
                claimed = ingest_queue.claim_next()
                self.assertIsNotNone(claimed, f'attempt {attempt}')
                ingest_queue.process_job(claimed)

        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'FAILED')
        self.assertEqual(job.attempts, ingest_queue.MAX_ATTEMPTS)
        self.assertIn('disk on fire', job.last_error)
        self.assertTrue(job.payload['lines'])       # el payload, intacto
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 0)     # y el arbol, sin tocar

    def test_a_failed_job_can_be_requeued_and_then_succeeds(self):
        self._submit()
        with mock.patch.object(ingest, 'ingest_analysis',
                               side_effect=RuntimeError('transient')):
            for _ in range(ingest_queue.MAX_ATTEMPTS):
                IngestJob.objects.update(next_attempt_at=timezone.now())
                ingest_queue.process_job(ingest_queue.claim_next())
        self.assertEqual(IngestJob.objects.get().state, 'FAILED')

        self.assertEqual(ingest_queue.retry_failed(), 1)
        self.assertEqual(ingest_queue.drain(), {'done': 1, 'failed': 0})

        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)

    def test_a_backoff_keeps_a_job_out_of_the_queue_until_it_is_due(self):
        self._submit()
        with mock.patch.object(ingest, 'ingest_analysis',
                               side_effect=RuntimeError('transient')):
            ingest_queue.process_job(ingest_queue.claim_next())

        self.assertIsNone(ingest_queue.claim_next())   # aun no toca
        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'PENDING')
        self.assertGreater(job.next_attempt_at, timezone.now())

    def test_a_stale_claim_is_recovered(self):
        self._submit()
        job = ingest_queue.claim_next()
        IngestJob.objects.filter(pk=job.pk).update(
            claimed_at=timezone.now() - timezone.timedelta(
                seconds=ingest_queue.CLAIM_STALE_SECONDS + 60))

        recovered = ingest_queue.claim_next()

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.pk, job.pk)

    def test_the_management_command_drains_and_reports(self):
        self._submit()
        from io import StringIO
        out = StringIO()

        call_command('process_ingest_queue', once=True, stdout=out)

        self.assertEqual(json.loads(out.getvalue().strip()),
                         {'done': 1, 'failed': 0})
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)

    def test_the_management_command_reports_the_queue_depth(self):
        self._submit()
        from io import StringIO
        out = StringIO()

        call_command('process_ingest_queue', status=True, stdout=out)

        self.assertEqual(json.loads(out.getvalue().strip()), {'PENDING': 1})


class SynchronousFallbackTests(TestCase):
    """El interruptor de despliegue usa el MISMO codigo de aplicacion."""

    def setUp(self):
        self.auth = _worker()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        AnalysisTask.objects.create(position=self.target, generation=0,
                                    budget_nodes=1_000_000)

    def _submit(self):
        leased = _lease(self.client, self.auth)['tasks'][0]
        return self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(self.target)),
            nodes='900000', elapsed='12.5',
            lease_token=leased['lease_token']))

    @override_settings(ATOMICDB_SYNCHRONOUS_INGEST=True)
    def test_the_switch_applies_before_answering(self):
        response = self._submit()

        self.assertTrue(response.json()['ok'])
        self.assertIn('backed_evals', response.json()['summary'])
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)
        self.assertEqual(IngestJob.objects.get().state, 'DONE')


class SynchronousVsQueuedTests(TestCase):
    """A/B: el mismo fixture por los dos caminos deja el mismo arbol."""

    LINES_VALUE = -450

    def _fixture(self, prefix):
        User.objects.create_user(username=prefix, password='p')
        auth = {'username': prefix, 'password': 'p', 'machine': prefix}
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        target = (Edge.objects.filter(parent=root)
                  .order_by('move_uci').first().child)
        ingest.expand(target)
        AnalysisTask.objects.create(position=target, generation=0,
                                    budget_nodes=1_000_000)
        client = Client()
        leased = _lease(client, auth)['tasks'][0]
        client.post('/atomicdb/api/submit', dict(
            auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(target, self.LINES_VALUE)),
            nodes='900000', elapsed='12.5',
            lease_token=leased['lease_token']))
        return target

    def _tree_state(self):
        return {
            'positions': sorted(
                Position.objects.values_list(
                    'key', 'eval_cp', 'status', 'expanded', 'visits',
                    'nodes_invested', 'time_invested', 'backed_eval',
                    'backed_move', 'backed_plies', 'backed_nodes',
                    'best_move')),
            'edges': sorted(
                Edge.objects.values_list('parent_id', 'move_uci',
                                         'child_id')),
        }

    def test_both_paths_leave_the_same_tree(self):
        with override_settings(ATOMICDB_SYNCHRONOUS_INGEST=True):
            self._fixture('sync')
        synchronous = self._tree_state()

        IngestJob.objects.all().delete()
        AnalysisTask.objects.all().delete()
        Edge.objects.all().delete()          # PROTECT sobre Edge.child
        ProofCampaign.objects.all().delete()  # PROTECT sobre Campaign.root
        Position.objects.all().delete()

        self._fixture('async')
        ingest_queue.drain()
        queued = self._tree_state()

        self.assertEqual(synchronous['edges'], queued['edges'])
        self.assertEqual(synchronous['positions'], queued['positions'])
