import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Client'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')

import worker

import OpenBench.config
import OpenBench.views
from OpenBench.models import Engine, Machine, Profile, Result, Test
from OpenBench.workloads import get_workload


class WorkerFocusServerTests(TestCase):

    ## A worker may name a single test on its workload request. The Server
    ## serves that test or nothing, and it says why. Requests without the
    ## field keep the historical assignment behaviour, byte for byte.

    def setUp(self):
        self.author = User.objects.create_user('focus-author', password='pass')
        self.engine = Engine.objects.create(
            name='focus-branch',
            source='https://example.test/archive.zip',
            sha='a' * 40,
            bench=1234,
        )

    def make_test(self, priority=0, engine='Spell-Stockfish', **overrides):
        values = dict(
            author=self.author.username,
            book_name='spell_openings.epd',
            dev=self.engine,
            base=self.engine,
            dev_repo='https://github.com/example/engine',
            base_repo='https://github.com/example/engine',
            dev_engine=engine,
            base_engine=engine,
            dev_options='Threads=1 Hash=32',
            base_options='Threads=1 Hash=32',
            dev_time_control='8.0+0.08',
            base_time_control='8.0+0.08',
            syzygy_wdl='DISABLED',
            syzygy_adj='DISABLED',
            test_mode='SPRT',
            workload_size=8,
            priority=priority,
            throughput=1000,
            approved=True,
        )
        values.update(overrides)
        return Test.objects.create(**values)

    def make_machine(self, username='focus-worker', supported=None, threads=2):
        user = User.objects.create_user(username, password='pass')
        Profile.objects.create(user=user, enabled=True)
        info = {
            'supported': ['Spell-Stockfish'] if supported is None else supported,
            'concurrency': threads,
            'physical_cores': threads,
            'sockets': 1,
            'focus': [],
            'client_ver': OpenBench.config.OPENBENCH_CONFIG['client_version'],
            'tablebases': {
                'standard': 0,
                'atomic': {'max': 0, 'manifest_sha256': None},
            },
            'syzygy_max': 0,
        }
        return Machine.objects.create(user=user, secret='secret', info=info)

    def post(self, machine, **extra):
        payload = {
            'machine_id': machine.id,
            'secret': machine.secret,
            'blacklist': [],
        }
        payload.update(extra)
        return RequestFactory().post('/clientGetWorkload/', payload)

    def refusal(self, machine, focus_test, **extra):
        response = get_workload.get_workload(
            self.post(machine, focus_test=focus_test, **extra), machine
        )
        self.assertNotIn('workload', response)
        self.assertFalse(response['focus']['served'])
        self.assert_nothing_was_bound(machine)
        return response['focus']['message']

    def assert_nothing_was_bound(self, machine):
        machine.refresh_from_db()
        self.assertEqual(machine.workload, 0)
        self.assertFalse(Result.objects.filter(machine=machine).exists())

    # A focus that can be served

    def test_focused_worker_receives_the_test_it_asked_for(self):
        wanted = self.make_test(priority=0)
        self.make_test(priority=10)
        machine = self.make_machine()

        response = get_workload.get_workload(
            self.post(machine, focus_test=wanted.id), machine
        )

        self.assertEqual(response['workload']['test']['id'], wanted.id)
        self.assertEqual(response['focus'], {
            'test_id': wanted.id,
            'served': True,
            'message': 'Serving focused test #%d' % (wanted.id),
        })
        machine.refresh_from_db()
        self.assertEqual(machine.workload, wanted.id)

    def test_focus_beats_the_workload_the_worker_was_already_on(self):
        wanted = self.make_test(priority=0)
        top = self.make_test(priority=10)
        machine = self.make_machine()
        machine.workload = top.id
        machine.save(update_fields=['workload'])

        response = get_workload.get_workload(
            self.post(machine, focus_test=wanted.id), machine
        )

        self.assertEqual(response['workload']['test']['id'], wanted.id)
        machine.refresh_from_db()
        self.assertEqual(machine.workload, wanted.id)

    def test_focus_leaves_the_global_priorities_alone(self):
        wanted = self.make_test(priority=0)
        top = self.make_test(priority=10)

        focused = self.make_machine('focused-worker')
        get_workload.get_workload(
            self.post(focused, focus_test=wanted.id), focused
        )

        ordinary = self.make_machine('ordinary-worker')
        response = get_workload.get_workload(self.post(ordinary), ordinary)

        self.assertEqual(response['workload']['test']['id'], top.id)
        self.assertNotIn('focus', response)

    # A request with no focus at all

    def test_a_worker_without_the_field_is_served_as_before(self):
        self.make_test(priority=0)
        top = self.make_test(priority=10)
        machine = self.make_machine()

        response = get_workload.get_workload(self.post(machine), machine)

        self.assertEqual(response['workload']['test']['id'], top.id)
        self.assertNotIn('focus', response)

    def test_an_empty_field_is_treated_as_no_focus(self):
        top = self.make_test(priority=10)
        machine = self.make_machine()

        response = get_workload.get_workload(
            self.post(machine, focus_test=''), machine
        )

        self.assertEqual(response['workload']['test']['id'], top.id)
        self.assertNotIn('focus', response)

    def test_no_work_at_all_still_answers_with_an_empty_body(self):
        machine = self.make_machine()

        self.assertEqual(
            get_workload.get_workload(self.post(machine), machine), {}
        )

    # A focus that cannot be served

    def test_a_finished_test_is_refused(self):
        finished = self.make_test(finished=True)
        machine = self.make_machine()

        self.assertEqual(
            self.refusal(machine, finished.id),
            'Test #%d has already finished' % (finished.id),
        )

    def test_a_test_awaiting_approval_is_refused(self):
        pending = self.make_test(approved=False, awaiting=True)
        machine = self.make_machine()

        self.assertEqual(
            self.refusal(machine, pending.id),
            'Test #%d has not been approved yet' % (pending.id),
        )

    def test_a_deleted_test_is_refused_as_nonexistent(self):
        removed = self.make_test(deleted=True)
        machine = self.make_machine()

        self.assertEqual(
            self.refusal(machine, removed.id),
            'Test #%d does not exist' % (removed.id),
        )

    def test_an_unknown_test_id_is_refused(self):
        machine = self.make_machine()

        self.assertEqual(
            self.refusal(machine, 987654), 'Test #987654 does not exist'
        )

    def test_a_test_for_an_engine_the_worker_cannot_run_is_refused(self):
        foreign = self.make_test(engine='Atomic-Stockfish')
        machine = self.make_machine()

        self.assertEqual(
            self.refusal(machine, foreign.id),
            'Test #%d needs the Atomic-Stockfish engine, which this worker '
            'cannot run' % (foreign.id),
        )

    def test_a_test_the_worker_lacks_threads_for_is_refused(self):
        heavy = self.make_test(
            dev_options='Threads=8 Hash=32', base_options='Threads=8 Hash=32'
        )
        machine = self.make_machine(threads=2)

        self.assertEqual(
            self.refusal(machine, heavy.id),
            'Test #%d needs more threads than this worker offers' % (heavy.id),
        )

    def test_a_test_needing_tablebases_the_worker_lacks_is_refused(self):
        probing = self.make_test(syzygy_wdl='6-MAN')
        machine = self.make_machine()

        self.assertEqual(
            self.refusal(machine, probing.id),
            'Test #%d needs tablebases this worker does not provide'
            % (probing.id),
        )

    def test_a_blacklisted_test_is_refused(self):
        banned = self.make_test()
        machine = self.make_machine()

        self.assertEqual(
            self.refusal(machine, banned.id, blacklist=[banned.id]),
            'Test #%d is blacklisted on this worker' % (banned.id),
        )

    def test_a_focus_that_is_not_a_number_is_refused(self):
        top = self.make_test(priority=10)
        machine = self.make_machine()

        response = get_workload.get_workload(
            self.post(machine, focus_test='latest'), machine
        )

        self.assertNotIn('workload', response)
        self.assertEqual(response['focus'], {
            'test_id': None,
            'served': False,
            'message': 'Focus test id must be a whole number, not "latest"',
        })
        self.assert_nothing_was_bound(machine)
        self.assertTrue(Test.objects.filter(id=top.id, games=0).exists())

    # The endpoint itself

    def test_the_endpoint_serves_a_focus_and_reports_a_refusal(self):
        wanted = self.make_test(priority=0)
        self.make_test(priority=10)
        machine = self.make_machine()

        served = OpenBench.views.client_get_workload(
            self.post(machine, focus_test=wanted.id)
        )
        self.assertEqual(served.status_code, 200)
        body = json.loads(served.content)
        self.assertEqual(body['workload']['test']['id'], wanted.id)
        self.assertTrue(body['focus']['served'])

        refused = OpenBench.views.client_get_workload(
            self.post(machine, focus_test=987654)
        )
        self.assertEqual(refused.status_code, 200)
        body = json.loads(refused.content)
        self.assertNotIn('workload', body)
        self.assertEqual(body['focus'], {
            'test_id': 987654,
            'served': False,
            'message': 'Test #987654 does not exist',
        })

    def test_the_endpoint_is_unchanged_for_a_worker_that_omits_the_field(self):
        top = self.make_test(priority=10)
        machine = self.make_machine()

        response = OpenBench.views.client_get_workload(self.post(machine))

        body = json.loads(response.content)
        self.assertEqual(body['workload']['test']['id'], top.id)
        self.assertEqual(list(body.keys()), ['workload'])


class WorkerFocusClientTests(TestCase):

    ## The Client only sends the field when the volunteer asked for it, and it
    ## always says out loud what came back.

    @staticmethod
    def configuration(focus_test):
        config = worker.Configuration.__new__(worker.Configuration)
        config.server = 'http://localhost:8000'
        config.machine_id = 12
        config.secret_token = 'secret'
        config.blacklist = []
        config.workload = None
        config.focus_test = focus_test
        return config

    @staticmethod
    def request_workload(config, payload):
        captured = {}

        def post(_target, data, timeout):
            captured.update(data)
            return SimpleNamespace(json=lambda: payload)

        with mock.patch.object(
            worker.requests, 'post', side_effect=post
        ), mock.patch('builtins.print') as printed:
            worker.server_request_workload(config)

        logged = ' '.join(str(call.args[0]) for call in printed.call_args_list)
        return captured, logged

    def test_an_unfocused_worker_sends_exactly_what_it_always_sent(self):
        config = self.configuration(None)

        captured, logged = self.request_workload(config, {})

        self.assertNotIn('focus_test', captured)
        self.assertNotIn('focus', logged)
        self.assertIsNone(config.workload)

    def test_a_focused_worker_names_its_test_on_every_request(self):
        config = self.configuration(4321)

        captured, logged = self.request_workload(config, {
            'focus': {'test_id': 4321, 'served': True, 'message': 'ok'},
            'workload': {'test': {
                'id': 4321,
                'type': 'SPRT',
                'dev': {'engine': 'Spell-Stockfish', 'name': 'focus-branch'},
                'base': {'engine': 'Spell-Stockfish', 'name': 'master'},
            }},
        })

        self.assertEqual(captured['focus_test'], 4321)
        self.assertIn('Test #4321 only', logged)
        self.assertEqual(config.workload['test']['id'], 4321)

    def test_a_refusal_is_printed_with_the_reason_from_the_server(self):
        config = self.configuration(4321)

        _, logged = self.request_workload(config, {'focus': {
            'test_id': 4321,
            'served': False,
            'message': 'Test #4321 has already finished',
        }})

        self.assertIn('Test #4321 has already finished', logged)
        self.assertIn('Waiting for test #4321', logged)
        self.assertIsNone(config.workload)

    def test_a_server_without_the_feature_is_called_out(self):
        config = self.configuration(4321)

        _, logged = self.request_workload(config, {})

        self.assertIn('does not support --focus-test', logged)

    def test_the_flag_only_accepts_a_test_id(self):
        self.assertIsNone(worker.Configuration.parse_focus_test(None))
        self.assertIsNone(worker.Configuration.parse_focus_test(''))
        self.assertEqual(worker.Configuration.parse_focus_test('4321'), 4321)

        with self.assertRaisesRegex(ValueError, 'expects a test id'):
            worker.Configuration.parse_focus_test('latest')

    def test_the_flag_is_optional_and_absent_by_default(self):
        with mock.patch.object(sys, 'argv', ['worker.py', '-T', '2', '-N', '1']):
            args = worker.parse_arguments(SimpleNamespace())
        self.assertIsNone(args.focus_test)

        with mock.patch.object(
            sys, 'argv',
            ['worker.py', '-T', '2', '-N', '1', '--focus-test', '4321'],
        ):
            args = worker.parse_arguments(SimpleNamespace())
        self.assertEqual(args.focus_test, '4321')
