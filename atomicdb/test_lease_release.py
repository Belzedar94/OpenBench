"""``api/release``: a stopping worker hands its leases back.

Until this endpoint a stopped worker's tasks stayed LEASED until the recycler
in ``api_lease`` noticed the silence -- ``LEASE_MINUTES`` for an analysis,
``SOLVE_LEASE_MINUTES`` for a proof -- and only then could anybody else take
them.  A worker that is told to stop now hands them back on its way out.

What these tests hold the endpoint to: it releases only what the caller
proves it holds (machine and token, as heartbeat and submit check), it leaves
a row exactly as the recycler leaves an expired one, and every other case is
a no-op, so calling it twice, late or wrongly changes nothing.
"""

import json
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from . import ingest, logic, views
from .models import AnalysisTask, IngestJob, Position, SolveTask, WorkerPing
from .testing import TestCase, worker_account

MACHINE = 'box-atomicdb'
RELEASED_FIELDS = ('state', 'machine', 'lease_heartbeat_at', 'lease_token',
                   'lease_session', 'attempts')


class _ReleaseCase(TestCase):

    def setUp(self):
        worker_account('worker', 'secret')
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.children = list(Position.objects.exclude(key=self.root.key)
                             .order_by('key'))

    def leased(self, machine=MACHINE, token='token-a', position=None,
               **fields):
        now = timezone.now()
        values = dict(state='LEASED', machine=machine, lease_token=token,
                      lease_session=f'session-{token}', leased_at=now,
                      lease_heartbeat_at=now, attempts=1)
        values.update(fields)
        return AnalysisTask.objects.create(
            position=position or self.children[AnalysisTask.objects.count()],
            generation=0, budget_nodes=128_000_000, **values)

    def solve_leased(self, machine=MACHINE, token='solve-token', **fields):
        now = timezone.now()
        values = dict(state='LEASED', machine=machine, lease_token=token,
                      lease_session='solve-session', leased_at=now,
                      lease_heartbeat_at=now, attempts=1)
        values.update(fields)
        return SolveTask.objects.create(
            position=self.root, goal='WHITE_WIN', budget_nodes=2_000_000,
            **values)

    @staticmethod
    def lease_of(task, kind='analysis', **overrides):
        entry = {'kind': kind, 'task_id': task.id,
                 'lease_token': task.lease_token, 'machine': task.machine}
        entry.update(overrides)
        return entry

    def release(self, leases, machine=MACHINE, username='worker',
                password='secret', client=None, raw=None):
        return (client or self.client).post('/atomicdb/api/release', {
            'username': username, 'password': password, 'machine': machine,
            'worker_build': '2026102701',
            'leases': raw if raw is not None else json.dumps(leases)})

    def released(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(set(body), {'ok', 'released'})
        self.assertIs(body['ok'], True)
        return body['released']

    @staticmethod
    def fields(task):
        task.refresh_from_db()
        return {name: getattr(task, name) for name in RELEASED_FIELDS}


class ReleaseOwnLeasesTests(_ReleaseCase):

    def test_a_released_lease_is_back_in_the_queue_at_once(self):
        task = self.leased()
        leased_at = task.leased_at

        self.assertEqual(self.released(self.release([self.lease_of(task)])), 1)

        self.assertEqual(self.fields(task), {
            'state': 'PENDING', 'machine': '', 'lease_heartbeat_at': None,
            'lease_token': '', 'lease_session': '', 'attempts': 1})
        self.assertEqual(task.leased_at, leased_at)

    def test_the_row_is_left_exactly_as_the_recycler_leaves_an_expired_one(self):
        released = self.leased(token='released')
        silent = timezone.now() - timedelta(minutes=views.LEASE_MINUTES + 1)
        expired = self.leased(machine='gone-atomicdb', token='expired',
                              leased_at=silent, lease_heartbeat_at=silent)

        self.release([self.lease_of(released)])
        # The recycler runs inside api_lease.  A build without lease tokens
        # may not take a task that already had a lease, so nothing leases
        # the recycled row again before it can be compared.
        self.client.post('/atomicdb/api/lease', {
            'username': 'worker', 'password': 'secret', 'machine': 'legacy'})

        self.assertEqual(self.fields(released), self.fields(expired))
        self.assertEqual(self.fields(expired)['state'], 'PENDING')
        self.assertIsNotNone(released.leased_at)

    def test_a_task_on_a_closed_position_is_absorbed_as_the_recycler_does(self):
        closed = self.children[0]
        task = self.leased(position=closed)
        Position.objects.filter(key=closed.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        self.assertEqual(self.released(self.release([self.lease_of(task)])), 1)

        task.refresh_from_db()
        self.assertEqual(task.state, 'COMPLETED')
        self.assertTrue(ingest.is_absorbed(task))
        self.assertEqual((task.nodes_searched, task.machine), (0, ''))
        self.assertNotEqual(task.state, AnalysisTask.TState.CANCELLED)

    def test_every_slot_and_the_solver_go_back_in_one_call(self):
        slots = [self.leased(machine=f'{MACHINE}#{slot}', token=f't{slot}')
                 for slot in range(3)]
        proof = self.solve_leased(machine=f'{MACHINE}#0')

        response = self.release(
            [self.lease_of(task) for task in slots]
            + [self.lease_of(proof, kind='solve')], machine=f'{MACHINE}#0')

        self.assertEqual(self.released(response), 4)
        for task in slots:
            self.assertEqual(self.fields(task)['state'], 'PENDING')
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.machine, proof.lease_token),
                         ('PENDING', '', ''))

    def test_releasing_twice_changes_nothing_the_second_time(self):
        task = self.leased()
        leases = [self.lease_of(task)]

        self.assertEqual(self.released(self.release(leases)), 1)
        after_first = self.fields(task)
        self.assertEqual(self.released(self.release(leases)), 0)

        self.assertEqual(self.fields(task), after_first)

    def test_a_released_task_goes_to_the_next_worker_that_asks(self):
        task = self.leased()
        self.release([self.lease_of(task)])

        leased = self.client.post('/atomicdb/api/lease', {
            'username': 'worker', 'password': 'secret',
            'machine': 'next-atomicdb', 'tb': '1',
            'worker_build': views.LEASE_TOKEN_BUILD,
            'lease_session': 'next'}).json()['tasks']

        self.assertEqual([row['id'] for row in leased], [task.id])
        task.refresh_from_db()
        self.assertEqual((task.machine, task.attempts), ('next-atomicdb', 2))

    def test_a_late_submit_of_a_released_task_is_refused(self):
        task = self.leased()
        self.release([self.lease_of(task)])

        response = self.client.post('/atomicdb/api/submit', {
            'username': 'worker', 'password': 'secret', 'machine': MACHINE,
            'task_id': task.id, 'lease_token': 'token-a', 'lines': '[]'})

        self.assertEqual((response.status_code, response.json()['error']),
                         (400, 'not-leased'))
        self.assertEqual(self.fields(task)['state'], 'PENDING')
        self.assertFalse(IngestJob.objects.exists())

    def test_the_machine_stops_showing_the_task_it_gave_back(self):
        task = self.leased()
        seen = timezone.now() - timedelta(seconds=20)
        ping = WorkerPing.objects.create(
            machine=MACHINE, user='worker', current_task_id=task.id,
            last_nps=5_000_000, nps_updated=seen)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=seen)

        self.release([self.lease_of(task)])

        ping = WorkerPing.objects.get(machine=MACHINE, user='worker')
        self.assertEqual((ping.current_task_id, ping.last_nps,
                          ping.nps_updated), (None, 0, None))
        # Neither a heartbeat nor a hello: the machine is not made to look
        # alive by the call it makes on its way out.
        self.assertEqual(ping.last_seen, seen)

    def test_nothing_named_is_an_answer_too(self):
        self.assertEqual(self.released(self.release([])), 0)
        self.assertEqual(self.released(self.release(None, raw='')), 0)

    def test_the_worker_protocol_needs_no_csrf_token(self):
        task = self.leased()
        strict = Client(enforce_csrf_checks=True)

        response = self.release([self.lease_of(task)], client=strict)

        self.assertEqual(self.released(response), 1)


class ReleaseRefusalTests(_ReleaseCase):
    """Everything that is not the caller's live lease is left alone."""

    def test_another_machines_lease_is_left_alone(self):
        theirs = self.leased(machine='other-atomicdb', token='their-token')
        before = self.fields(theirs)

        # Their machine and their token, named by somebody else's machine.
        self.assertEqual(self.released(self.release(
            [self.lease_of(theirs)])), 0)
        # The right token, but naming the caller's own machine.
        self.assertEqual(self.released(self.release(
            [self.lease_of(theirs, machine=MACHINE)])), 0)

        self.assertEqual(self.fields(theirs), before)

    def test_a_lookalike_machine_is_another_machine(self):
        theirs = self.leased(machine=f'{MACHINE}2', token='lookalike')

        self.assertEqual(self.released(self.release(
            [self.lease_of(theirs)])), 0)
        self.assertEqual(self.fields(theirs)['state'], 'LEASED')

    def test_a_wrong_or_stale_token_releases_nothing(self):
        task = self.leased(token='current')

        self.assertEqual(self.released(self.release(
            [self.lease_of(task, lease_token='not-it')])), 0)
        # The token of an earlier lease of the same task, same machine.
        self.assertEqual(self.released(self.release(
            [self.lease_of(task, lease_token='earlier')])), 0)
        self.assertEqual(self.fields(task)['lease_token'], 'current')
        self.assertEqual(self.fields(task)['state'], 'LEASED')

    def test_a_tokenless_lease_is_never_released(self):
        legacy = self.leased(token='', lease_session='')

        self.assertEqual(self.released(self.release(
            [self.lease_of(legacy)])), 0)
        self.assertEqual(self.fields(legacy)['state'], 'LEASED')

    def test_finished_and_waiting_tasks_are_left_alone(self):
        completed = self.leased(token='done')
        AnalysisTask.objects.filter(pk=completed.pk).update(state='COMPLETED')
        cancelled = self.leased(token='withdrawn')
        AnalysisTask.objects.filter(pk=cancelled.pk).update(state='CANCELLED')
        waiting = self.leased(token='requeued')
        AnalysisTask.objects.filter(pk=waiting.pk).update(state='PENDING')
        before = {task.pk: self.fields(task)
                  for task in (completed, cancelled, waiting)}

        response = self.release([self.lease_of(task) for task in
                                 (completed, cancelled, waiting)])

        self.assertEqual(self.released(response), 0)
        for task in (completed, cancelled, waiting):
            self.assertEqual(self.fields(task), before[task.pk])

    def test_a_solve_lease_of_another_machine_or_token_stays(self):
        theirs = self.solve_leased(machine='other-atomicdb', token='theirs')
        mine = self.solve_leased(token='mine')

        response = self.release([
            self.lease_of(theirs, kind='solve'),
            self.lease_of(mine, kind='solve', lease_token='stale')])

        self.assertEqual(self.released(response), 0)
        for task in (theirs, mine):
            task.refresh_from_db()
            self.assertEqual(task.state, 'LEASED')

    def test_credentials_are_required(self):
        task = self.leased()
        worker_account('expelled', 'pw', enabled=False)
        for username, password in (('', ''), ('worker', 'wrong'),
                                   ('nobody', 'secret'), ('expelled', 'pw')):
            with self.subTest(username=username):
                response = self.release([self.lease_of(task)],
                                        username=username, password=password)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {'error': 'bad credentials'})
        self.assertEqual(self.fields(task)['state'], 'LEASED')

    def test_the_refusal_without_credentials_is_the_heartbeats(self):
        for url in ('/atomicdb/api/release', '/atomicdb/api/heartbeat'):
            with self.subTest(url=url):
                response = self.client.post(url, {})
                self.assertEqual((response.status_code, response.json()),
                                 (403, {'error': 'bad credentials'}))

    def test_only_post_is_accepted(self):
        response = self.client.get('/atomicdb/api/release')
        self.assertEqual((response.status_code, response.json()),
                         (405, {'error': 'POST required'}))

    def test_a_malformed_list_is_refused_whole(self):
        task = self.leased()
        good = self.lease_of(task)
        for label, raw in (
                ('not json', '[{'),
                ('not a list', json.dumps(good)),
                ('not an object', json.dumps([good, 7])),
                ('bool id', json.dumps([good, dict(good, task_id=True)])),
                ('text id', json.dumps([good, dict(good, task_id='7')])),
                ('huge id', json.dumps([good, dict(good, task_id=2 ** 64)])),
                ('kind', json.dumps([good, dict(good, kind='proof')])),
                ('token', json.dumps([good, dict(good, lease_token=7)])),
                ('too many', json.dumps(
                    [good] * (views.RELEASE_MAX_LEASES + 1)))):
            with self.subTest(label):
                response = self.release(None, raw=raw)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()['error'].startswith(
                    'malformed: '))
        self.assertEqual(self.fields(task)['state'], 'LEASED')

    def test_one_call_can_name_every_lease_a_machine_can_hold(self):
        self.assertEqual(views.RELEASE_MAX_LEASES,
                         views.LEASES_PER_MACHINE + 1)
        tasks = [self.leased(machine=f'{MACHINE}#{slot}', token=f's{slot}')
                 for slot in range(3)]
        named = [self.lease_of(task) for task in tasks]
        # Padded to the limit with harmless names: the limit is accepted.
        named += [dict(named[0], lease_token='nothing')] * (
            views.RELEASE_MAX_LEASES - len(named))

        self.assertEqual(self.released(self.release(named,
                                                    machine=f'{MACHINE}#0')),
                         3)


class SharedRecyclerTests(_ReleaseCase):
    """The recyclers still recycle, through the helpers they now share."""

    def test_the_solve_recycler_and_a_release_leave_the_same_row(self):
        silent = timezone.now() - timedelta(
            minutes=ingest.SOLVE_LEASE_MINUTES + 5)
        expired = self.solve_leased(machine='gone-atomicdb', token='old',
                                    leased_at=silent, lease_heartbeat_at=silent)
        released = self.solve_leased(token='mine')

        self.release([self.lease_of(released, kind='solve')])
        self.assertEqual(ingest.recycle_stale_solve_leases(), 1)

        fields = ('state', 'machine', 'lease_heartbeat_at', 'lease_token',
                  'lease_session', 'attempts')
        expired.refresh_from_db()
        released.refresh_from_db()
        self.assertEqual({name: getattr(released, name) for name in fields},
                         {name: getattr(expired, name) for name in fields})
        self.assertEqual(released.state, 'PENDING')

    def test_the_analysis_recycler_leaves_live_leases_alone(self):
        live = self.leased(token='live')
        self.client.post('/atomicdb/api/lease', {
            'username': 'worker', 'password': 'secret', 'machine': 'legacy'})
        self.assertEqual(self.fields(live)['state'], 'LEASED')
