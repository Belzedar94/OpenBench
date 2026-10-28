import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase

import OpenBench.config
import OpenBench.views # Before get_workload, which on its own trips an import cycle
from OpenBench.datagen import claim_chunk, initialize_chunks
from OpenBench.models import (
    DatagenChunk, Engine, LogEvent, Machine, Profile, Test,
)
from OpenBench.workloads import get_workload


ERROR = '[Spell-Stockfish-1A2B3C4D-5E6F7A8B] Failed to Execute Benchmark'
NOTE  = ' (test kept running, machine excluded)'


class BenchErrorTests(TestCase):

    ## A worker that fails the bench of a test stops that test only while the
    ## test has no games. Once it has games (positions, for a generic DATAGEN),
    ## other workers have reproduced that bench, so the failure belongs to the
    ## worker: the test keeps running and that worker alone stops receiving
    ## it, until the worker registers again.

    def setUp(self):
        self.author = User.objects.create_user('bench-author', password='pass')
        self.engine = Engine.objects.create(
            name='bench-branch',
            source='https://example.test/archive.zip',
            sha='a' * 40,
            bench=1234,
        )

    def make_test(self, games=0, priority=0, **overrides):
        values = dict(
            author=self.author.username,
            book_name='spell_openings.epd',
            dev=self.engine,
            base=self.engine,
            dev_repo='https://github.com/example/engine',
            base_repo='https://github.com/example/engine',
            dev_engine='Spell-Stockfish',
            base_engine='Spell-Stockfish',
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
            games=games,
        )
        values.update(overrides)
        return Test.objects.create(**values)

    def make_datagen_test(self, total=4, per_chunk=2):
        test = self.make_test(
            book_name='NONE',
            dev_options='',
            base_options='',
            dev_time_control='',
            base_time_control='',
            test_mode='DATAGEN',
            datagen_command=(
                'datagen seed {SEED} count {COUNT} threads {THREADS} '
                'book {BOOK} out {OUT}'
            ),
            datagen_total_count=total,
            datagen_positions_per_chunk=per_chunk,
            datagen_base_seed=100,
            max_games=total,
        )
        initialize_chunks(test)
        return test

    def complete_first_chunk(self, test):
        # What client_submit_datagen leaves behind for one finished chunk
        chunk = test.datagen_chunks.get(idx=0)
        DatagenChunk.objects.filter(pk=chunk.pk).update(
            status=DatagenChunk.COMPLETED)
        Test.objects.filter(pk=test.pk).update(
            games=chunk.position_count, datagen_completed_chunks=1)

    def make_machine(self, username='bench-worker'):
        user = User.objects.create_user(username, password='pass')
        Profile.objects.create(user=user, enabled=True)
        info = {
            'mac_address': 'AABBCCDDEEFF',
            'supported': ['Spell-Stockfish'],
            'concurrency': 2,
            'physical_cores': 2,
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

    def assign(self, machine, test):
        Machine.objects.filter(id=machine.id).update(workload=test.id)

    def report(self, machine, test, error=ERROR, **extra):
        payload = {
            'machine_id': machine.id,
            'secret': Machine.objects.get(id=machine.id).secret,
            'test_id': test.id,
            'error': error,
        }
        payload.update(extra)
        return Client().post('/clientBenchError/', payload)

    def workload_request(self, machine, **extra):
        payload = {
            'machine_id': machine.id,
            'secret': Machine.objects.get(id=machine.id).secret,
            'blacklist': [],
        }
        payload.update(extra)
        return RequestFactory().post('/clientGetWorkload/', payload)

    def request_workload(self, machine, **extra):
        # Exactly what verify_worker hands over: the row as it is right now
        fresh = Machine.objects.get(id=machine.id)
        return get_workload.get_workload(
            self.workload_request(machine, **extra), fresh)

    def skipped(self, machine):
        info = Machine.objects.get(id=machine.id).info
        return get_workload.bench_failed_tests(info)

    # A test without games: the bench itself is the suspect

    def test_a_failure_before_any_game_stops_the_test(self):
        test = self.make_test(games=0)
        machine = self.make_machine()
        self.assign(machine, test)

        response = self.report(machine, test)

        self.assertEqual(response.status_code, 200)
        test.refresh_from_db()
        self.assertTrue(test.finished)
        event = LogEvent.objects.get(test_id=test.id)
        self.assertEqual(event.summary, ERROR)
        self.assertEqual(event.machine_id, machine.id)
        self.assertEqual(event.author, machine.user.username)
        self.assertEqual(self.skipped(machine), [])

    def test_a_generic_datagen_without_positions_stops(self):
        test = self.make_datagen_test()
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        self.assign(machine, test)

        self.report(machine, test, chunk_idx=chunk.idx, attempt=chunk.attempts)

        test.refresh_from_db()
        self.assertTrue(test.finished)
        self.assertEqual(LogEvent.objects.get(test_id=test.id).summary, ERROR)
        self.assertEqual(self.skipped(machine), [])

    # A test with games: the failure belongs to the worker

    def test_a_failure_after_games_keeps_the_test_running(self):
        test = self.make_test(games=40186)
        machine = self.make_machine()
        self.assign(machine, test)

        response = self.report(machine, test)

        self.assertEqual(response.status_code, 200)
        test.refresh_from_db()
        self.assertFalse(test.finished)
        self.assertEqual(test.games, 40186)
        self.assertEqual(self.skipped(machine), [test.id])
        self.assertEqual(Machine.objects.get(id=machine.id).workload, 0)

        event = LogEvent.objects.get(test_id=test.id)
        self.assertEqual(event.summary, ERROR + NOTE)
        self.assertEqual(event.machine_id, machine.id)
        self.assertEqual(event.author, machine.user.username)

    def test_the_failing_worker_is_not_served_the_test_but_others_are(self):
        test = self.make_test(games=500, priority=10)
        fallback = self.make_test(priority=0)
        failing = self.make_machine('failing-worker')
        healthy = self.make_machine('healthy-worker')

        self.report(failing, test)

        self.assertEqual(
            self.request_workload(failing)['workload']['test']['id'],
            fallback.id)
        self.assertEqual(
            self.request_workload(healthy)['workload']['test']['id'], test.id)

        Test.objects.filter(id=fallback.id).update(finished=True)
        self.assertEqual(self.request_workload(failing), {})

    def test_a_worker_focused_on_the_test_is_told_why(self):
        test = self.make_test(games=500)
        machine = self.make_machine()
        self.report(machine, test)

        response = self.request_workload(machine, focus_test=test.id)

        self.assertNotIn('workload', response)
        self.assertEqual(response['focus'], {
            'test_id': test.id,
            'served': False,
            'message': 'Test #%d failed its bench on this worker, and is '
                       'skipped until the worker restarts' % (test.id),
        })

    def test_registering_again_offers_the_test_once_more(self):
        test = self.make_test(games=500)
        machine = self.make_machine()
        self.report(machine, test)
        self.assertEqual(self.request_workload(machine), {})

        engine = OpenBench.config.OPENBENCH_CONFIG['engines']['Spell-Stockfish']
        system_info = dict(
            Machine.objects.get(id=machine.id).info,
            machine_id=str(machine.id),
            cpu_flags=list(engine['build']['cpuflags']),
            compilers={'Spell-Stockfish': ['g++', '13.2.0']},
            tokens={},
            os_name=engine['build']['systems'][0],
        )
        system_info.pop(get_workload.BENCH_FAILED_TESTS)
        registered = Client().post('/clientWorkerInfo/', {
            'username': machine.user.username,
            'password': 'pass',
            'system_info': json.dumps(system_info),
        }).json()

        self.assertEqual(registered['machine_id'], machine.id)
        self.assertEqual(self.skipped(machine), [])
        self.assertEqual(
            self.request_workload(machine)['workload']['test']['id'], test.id)

    def test_failures_accumulate_without_repeats(self):
        first = self.make_test(games=500)
        second = self.make_test(games=500)
        machine = self.make_machine()

        self.report(machine, first)
        self.report(machine, first)
        self.report(machine, second)

        self.assertEqual(self.skipped(machine), [first.id, second.id])
        self.assertFalse(Test.objects.filter(finished=True).exists())

    def test_a_long_error_is_cut_to_keep_the_note(self):
        test = self.make_test(games=500)
        machine = self.make_machine()

        self.report(machine, test, error='x' * 300)

        summary = LogEvent.objects.get(test_id=test.id).summary
        self.assertEqual(
            len(summary), LogEvent._meta.get_field('summary').max_length)
        self.assertTrue(summary.endswith(NOTE))

    def test_a_test_that_already_finished_is_only_logged(self):
        test = self.make_test(games=500, finished=True, passed=True)
        machine = self.make_machine()

        self.report(machine, test)

        test.refresh_from_db()
        self.assertTrue(test.finished and test.passed)
        self.assertEqual(LogEvent.objects.get(test_id=test.id).summary, ERROR)
        self.assertEqual(self.skipped(machine), [])

    def test_a_generic_datagen_with_positions_keeps_running(self):
        test = self.make_datagen_test(total=4, per_chunk=2)
        self.complete_first_chunk(test)
        failing = self.make_machine('failing-worker')
        chunk = claim_chunk(test, failing)
        self.assign(failing, test)

        self.report(
            failing, test, chunk_idx=chunk.idx, attempt=chunk.attempts)

        test.refresh_from_db()
        self.assertFalse(test.finished)
        self.assertEqual(self.skipped(failing), [test.id])
        self.assertEqual(Machine.objects.get(id=failing.id).workload, 0)
        self.assertEqual(
            LogEvent.objects.get(test_id=test.id).summary, ERROR + NOTE)

        # The chunk goes straight back to the queue, not when its lease expires
        chunk.refresh_from_db()
        self.assertEqual(chunk.status, DatagenChunk.PENDING)
        self.assertIsNone(chunk.machine_id)
        self.assertEqual(chunk.last_error, ERROR)

        self.assertEqual(self.request_workload(failing), {})
        healthy = self.make_machine('healthy-worker')
        served = self.request_workload(healthy)['workload']['test']
        self.assertEqual(served['id'], test.id)
        self.assertEqual(served['datagen']['chunk_idx'], chunk.idx)

    # Copies of the Machine read before the failure was recorded

    def test_assigning_work_from_an_older_copy_keeps_the_skip_list(self):
        failing = self.make_test(games=500, priority=0)
        other = self.make_test(priority=10)
        machine = self.make_machine()
        older = Machine.objects.get(id=machine.id)

        self.report(machine, failing)
        response = get_workload.get_workload(
            self.workload_request(machine), older)

        self.assertEqual(response['workload']['test']['id'], other.id)
        self.assertEqual(self.skipped(machine), [failing.id])
        self.assertEqual(Machine.objects.get(id=machine.id).workload, other.id)

    def test_a_heartbeat_from_an_older_copy_keeps_the_skip_list(self):
        # A DATAGEN worker keeps sending heartbeats while it runs the bench
        test = self.make_datagen_test()
        self.complete_first_chunk(test)
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        self.assign(machine, test)
        older = Machine.objects.get(id=machine.id)

        self.report(machine, test, chunk_idx=chunk.idx, attempt=chunk.attempts)
        with mock.patch.object(Machine.objects, 'get', return_value=older):
            response = Client().post('/clientHeartbeat/', {
                'machine_id': machine.id,
                'secret': older.secret,
                'test_id': test.id,
                'chunk_idx': chunk.idx,
                'attempt': chunk.attempts,
            })

        self.assertEqual(response.json(), {'stop': True})
        self.assertEqual(self.skipped(machine), [test.id])
        self.assertEqual(Machine.objects.get(id=machine.id).workload, 0)
