"""The first Ctrl+C drains the AtomicDB worker; the second stops it at once.

Requested by Eclipsia (24-Nov-2026).  After the first Ctrl+C no job takes
another task: a running search finishes and is submitted, the job prints
"[N] has finished working", and the worker exits once every job has.  When
--drain-timeout runs out it stops as a second Ctrl+C does -- engines ended,
every lease still held handed back in one ``api/release`` call.  A second
Ctrl+C, SIGTERM, SIGHUP, Ctrl+Break or a closing window stop at once.

The last class runs the published file as a process and sends it a real
SIGINT through its process group, as a terminal does: the engines must not
receive it.
"""

import gc
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from Client import atomicdb_worker as worker
from atomicdb.test_worker_release import (
    START_FEN, WORKER_FILE, _Response, _StandInServer, _gone, _wait_until)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ParseDurationTests(SimpleTestCase):

    def test_plain_numbers_are_seconds_and_suffixes_scale(self):
        self.assertEqual(worker.parse_duration('0'), 0)
        self.assertEqual(worker.parse_duration('90'), 90)
        self.assertEqual(worker.parse_duration('90s'), 90)
        self.assertEqual(worker.parse_duration('10m'), 600)
        self.assertEqual(worker.parse_duration('1.5h'), 5400)
        self.assertEqual(worker.parse_duration(' 2M '), 120)

    def test_anything_else_is_refused(self):
        for text in ('', '-5', 'ten', '10d', '5 minutes'):
            with self.subTest(text=text), \
                    self.assertRaises(worker.argparse.ArgumentTypeError):
                worker.parse_duration(text)

    def test_the_default_is_ten_minutes(self):
        self.assertEqual(worker.DRAIN_TIMEOUT_DEFAULT_SECONDS, 600)


class DrainTests(SimpleTestCase):

    def test_begin_announces_the_drain_once(self):
        drain = worker.Drain(600, clock=_Clock())
        with redirect_stdout(io.StringIO()) as out:
            self.assertTrue(drain.begin(3))
            self.assertFalse(drain.begin(3))       # the second Ctrl+C
        self.assertTrue(drain.is_set())
        self.assertEqual(out.getvalue().splitlines(), [
            'Termination signal received: no new tasks; waiting for 3 '
            'running jobs to finish, hard stop in 10m00s (Ctrl+C again to '
            'stop now)'])

    def test_a_zero_timeout_never_drains(self):
        drain = worker.Drain(0)
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(drain.begin(1))
        self.assertFalse(drain.enabled)
        self.assertFalse(drain.is_set())
        self.assertEqual(out.getvalue(), '')

    def test_it_expires_when_the_timeout_has_passed(self):
        clock = _Clock()
        drain = worker.Drain(90, clock=clock)
        self.assertFalse(drain.expired())          # not even begun
        with redirect_stdout(io.StringIO()):
            drain.begin(1)
        clock.now += 89
        self.assertFalse(drain.expired())
        self.assertAlmostEqual(drain.remaining(), 1)
        clock.now += 1
        self.assertTrue(drain.expired())
        self.assertEqual(drain.remaining(), 0)

    def test_the_countdown_is_printed_once_a_minute(self):
        clock = _Clock()
        drain = worker.Drain(600, clock=clock)
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(drain.report(2))      # not draining yet
            drain.begin(2)
            clock.now += 30
            self.assertFalse(drain.report(2))
            clock.now += 30
            self.assertTrue(drain.report(2))
            self.assertFalse(drain.report(2))
        self.assertEqual(out.getvalue().splitlines()[1:], [
            'Draining: 2 jobs still working, hard stop in 9m00s (Ctrl+C '
            'again to stop now)'])

    def test_each_job_says_once_that_it_has_finished(self):
        drain = worker.Drain(600)
        with redirect_stdout(io.StringIO()) as out:
            drain.finished(0)
            drain.finished(0)
            drain.finished('solve')
        self.assertEqual(out.getvalue().splitlines(), [
            '[0] has finished working', '[solve] has finished working'])


class IdleWaitTests(SimpleTestCase):

    def test_a_pause_ends_as_soon_as_a_drain_begins(self):
        stop, drain = threading.Event(), worker.Drain(600)
        with redirect_stdout(io.StringIO()):
            threading.Timer(0.2, drain.begin).start()
            started = time.monotonic()
            self.assertTrue(worker._idle_wait(stop, drain, 30))
        self.assertLess(time.monotonic() - started, 5)

    def test_a_pause_ends_on_a_stop(self):
        stop, drain = threading.Event(), worker.Drain(600)
        threading.Timer(0.2, stop.set).start()
        self.assertTrue(worker._idle_wait(stop, drain, 30))

    def test_a_full_pause_answers_false(self):
        self.assertFalse(worker._idle_wait(threading.Event(),
                                           worker.Drain(600), 0.3))
        self.assertFalse(worker._idle_wait(threading.Event(), None, 0.05))


# ---------------------------------------------------------------------------
# The analysis slot
# ---------------------------------------------------------------------------

class _Search:
    """A job's ``CurrentTaskState`` on the test's clock."""

    def __init__(self, clock, budget, nodes, seconds):
        self.state = worker.CurrentTaskState(clock=clock)
        self.state.start(7, 'tok-7', budget)
        if nodes:
            self.state.progress(nodes, seconds)


class AdaptiveDrainTests(SimpleTestCase):
    """No --drain-timeout given: the hard stop waits for the longest
    running search, at least ten minutes and at most two hours."""

    def drain(self, *searches, adaptive=True):
        clock = searches[0].state._clock if searches else _Clock()
        drain = worker.Drain(600, clock=clock, adaptive=adaptive)
        for search in searches:
            drain.watch(search.state)
        return drain

    def begin(self, drain):
        with redirect_stdout(io.StringIO()) as out:
            self.assertTrue(drain.begin(1))
        return out.getvalue()

    def test_time_left_is_what_the_budget_needs_at_the_speed_so_far(self):
        clock = _Clock()
        search = _Search(clock, 2_000_000_000, 200_000_000, 200.0)
        self.assertEqual(search.state.seconds_left(), 1800)
        clock.now += 60                 # silent since: a minute less left
        self.assertEqual(search.state.seconds_left(), 1740)
        self.assertIsNone(_Search(clock, 2_000_000_000, 0, 0)
                          .state.seconds_left())      # no speed yet
        self.assertIsNone(_Search(clock, 0, 100, 1.0).state.seconds_left())
        search.state.clear()
        self.assertIsNone(search.state.seconds_left())

    def test_a_big_search_moves_the_hard_stop_later(self):
        clock = _Clock()
        drain = self.drain(_Search(clock, 2_000_000_000, 200_000_000, 200.0),
                           _Search(clock, 1_000_000, 500_000, 1.0))

        out = self.begin(drain)

        self.assertIn('hard stop in 37m30s', out)  # 1800 s x 1.25
        clock.now += 2249
        self.assertFalse(drain.expired())
        clock.now += 1
        self.assertTrue(drain.expired())

    def test_short_searches_keep_the_ten_minutes(self):
        clock = _Clock()
        drain = self.drain(_Search(clock, 1_000_000, 500_000, 1.0))
        self.assertIn('hard stop in 10m00s', self.begin(drain))
        clock.now += 600
        self.assertTrue(drain.expired())

    def test_the_wait_is_capped_at_two_hours(self):
        clock = _Clock()
        drain = self.drain(_Search(clock, 10 ** 12, 1_000_000, 1.0))
        self.assertIn('hard stop in 2h00m', self.begin(drain))
        clock.now += 2 * 3600
        self.assertTrue(drain.expired())

    def test_a_search_that_reports_its_speed_later_still_counts(self):
        clock = _Clock()
        search = _Search(clock, 2_000_000_000, 0, 0)
        drain = self.drain(search)
        self.assertIn('hard stop in 10m00s', self.begin(drain))
        clock.now += 100
        search.state.progress(100_000_000, 100.0)    # 1900 s left
        self.assertAlmostEqual(drain.remaining(), 1900 * 1.25)

    def test_the_deadline_never_moves_earlier(self):
        clock = _Clock()
        search = _Search(clock, 2_000_000_000, 200_000_000, 200.0)
        drain = self.drain(search)
        self.begin(drain)
        deadline = drain.deadline
        search.state.progress(1_900_000_000, 210.0)  # a burst of speed
        drain.expired()
        self.assertEqual(drain.deadline, deadline)

    def test_an_explicit_timeout_keeps_its_exact_meaning(self):
        clock = _Clock()
        drain = self.drain(_Search(clock, 2_000_000_000, 200_000_000, 200.0),
                           adaptive=False)
        self.assertIn('hard stop in 10m00s', self.begin(drain))
        clock.now += 600
        self.assertTrue(drain.expired())


class DrainHeartbeatTests(SimpleTestCase):
    """The heartbeats keep the leases alive for the whole drain: they run
    until the stop, so a lease cannot lapse on the server (4 minutes
    without one) while its search is still finishing."""

    def test_a_drain_does_not_stop_the_heartbeat(self):
        drain, stop = worker.Drain(600), threading.Event()
        current = worker.CurrentTaskState()
        current.start(5275448, 'tok', 2_000_000_000)
        current.progress(1000, 1.0)
        named = []

        def post(url, data, timeout):
            named.append(data.get('task_id'))
            return _Response(200, {'ok': True})

        session = mock.Mock(post=post)
        with redirect_stdout(io.StringIO()):
            drain.begin(1)
            with mock.patch.object(worker, 'HEARTBEAT_INTERVAL_SECONDS',
                                   0.01), \
                    mock.patch.object(worker, '_http_session',
                                      return_value=session):
                beat = threading.Thread(target=worker._heartbeat_loop,
                                        args=('https://server', {}, current,
                                              stop))
                beat.start()
                self.assertTrue(_wait_until(lambda: len(named) >= 3, 10))
                stop.set()
                beat.join(10)

        self.assertFalse(beat.is_alive())
        self.assertEqual(set(named), {5275448})


LINES = [{'move': 'e2e4', 'pv': ['e2e4'], 'eval_cp': 10, 'mate': None,
          'raw': 'info depth 1 nodes 100 pv e2e4'}]


def _task(task_id=9):
    return {'id': task_id, 'fen': START_FEN, 'budget_nodes': 100,
            'multipv': 1, 'lease_token': f'tok-{task_id}'}


class SlotDrainTests(SimpleTestCase):
    """``_analysis_slot`` with a drain."""

    def run_slot(self, leases, search=lambda drain: LINES, begun=False,
                 adaptive=False):
        """``leases``: what each api/lease answers, as functions of the drain
        (the slot then stops after the script runs out)."""
        stop = threading.Event()
        drain = worker.Drain(600, adaptive=adaptive)
        held = worker.HeldLeases()
        log = []
        script = list(leases)

        def request_tasks(server, auth, lease_session):
            log.append('lease')
            if not script:
                stop.set()
                return []
            return script.pop(0)(drain)

        def submit(server, payload, task_id):
            log.append(('submit', task_id))
            return _Response(200, {'ok': True, 'summary': 'stored'})

        def release(server, auth, leases):
            log.append(('release', [lease['task_id'] for lease in leases]))
            return len(leases), ''

        class Engine:
            def __init__(self, *args, **kwargs):
                self.ready = True

            def analyse(self, fen, nodes, multipv, searchmoves=None,
                        progress=None):
                log.append('search')
                drain.progress = progress     # for a search to report
                return search(drain)

            def close(self):
                log.append('close')

        a = SimpleNamespace(engine='engine', hash=16, syzygy='',
                            S='https://server', no_auto_update=True,
                            once=False)
        with redirect_stdout(io.StringIO()) as out:
            if begun:
                drain.begin()
            with mock.patch.object(worker, 'Engine', Engine), \
                    mock.patch.object(worker, '_request_tasks',
                                      request_tasks), \
                    mock.patch.object(worker, '_submit_until_definitive',
                                      submit), \
                    mock.patch.object(worker, '_release_leases', release), \
                    mock.patch.object(worker, '_heartbeat_loop',
                                      lambda *args: None):
                worker._analysis_slot(0, a, None, {'machine': 'box#0'}, {},
                                      1, stop, False, held=held, jobs=2,
                                      drain=drain)
        return log, held, stop, out.getvalue()

    def test_a_drained_slot_takes_no_task(self):
        log, held, stop, out = self.run_slot([lambda drain: [_task()]],
                                             begun=True)

        self.assertEqual(log, ['close'])
        self.assertFalse(stop.is_set())
        self.assertIn('[0] has finished working', out)

    def test_the_running_search_finishes_and_is_submitted(self):
        def search(drain):
            drain.begin()             # Ctrl+C in the middle of the search
            return LINES

        log, held, stop, out = self.run_slot(
            [lambda drain: [_task()], lambda drain: [_task(10)]],
            search=search)

        self.assertEqual(log, ['lease', 'search', ('submit', 9), 'close'])
        self.assertEqual(held.snapshot(), [])
        self.assertIn('[0] has finished working', out)

    def test_a_task_leased_as_the_drain_began_goes_back_unsearched(self):
        def lease(drain):
            drain.begin()
            return [_task(), _task(10)]

        log, held, stop, out = self.run_slot([lease])

        self.assertEqual(log, ['lease', ('release', [9]), ('release', [10]),
                               'close'])
        self.assertEqual(held.snapshot(), [])
        self.assertIn('task 9 handed back to the server', out)
        self.assertIn('[0] has finished working', out)

    def test_a_slot_waiting_for_work_leaves_at_once(self):
        def nothing(drain):
            threading.Timer(0.2, drain.begin).start()
            return []

        started = time.monotonic()
        log, held, stop, out = self.run_slot([nothing])

        self.assertLess(time.monotonic() - started, 10, 'waited the 60 s')
        self.assertEqual(log, ['lease', 'close'])
        self.assertIn('[0] has finished working', out)

    def test_the_hard_stop_is_sized_to_the_running_search(self):
        def lease(drain):
            return [dict(_task(), budget_nodes=2_000_000_000)]

        def search(drain):
            # A 2G-node task, 200M nodes in at 1M nodes per second: about
            # 30 minutes left, so the hard stop comes in about 37m30s.
            drain.progress(200_000_000, 200.0)
            drain.begin(1)
            return LINES

        log, held, stop, out = self.run_slot([lease], search=search,
                                             adaptive=True)

        self.assertRegex(out, r'waiting for 1 running job to finish, hard '
                              r'stop in 37m(29|30)s ')
        self.assertIn(('submit', 9), log)


class SolveLoopDrainTests(SimpleTestCase):

    def test_the_solver_ends_between_proofs(self):
        drain = worker.Drain(600)
        with redirect_stdout(io.StringIO()) as out:
            drain.begin()
            with mock.patch.object(worker, '_solve_heartbeat_loop',
                                   lambda *args: None), \
                    mock.patch.object(worker, '_solve_once',
                                      side_effect=AssertionError('proved')):
                worker._solve_loop('https://server', {'machine': 'box#0'},
                                   mock.Mock(), threading.Event(),
                                   drain=drain)
        self.assertIn('[solve] has finished working', out.getvalue())


# ---------------------------------------------------------------------------
# The supervisor: what each Ctrl+C does
# ---------------------------------------------------------------------------

class _Requested:
    """``GracefulStop.requested`` whose waits replay ``script``: 'int' is a
    Ctrl+C, 'term' a SIGTERM, anything else a short pause."""

    def __init__(self, *script):
        self.script = list(script)

    def is_set(self):
        return False

    def wait(self, timeout):
        step = self.script.pop(0) if self.script else None
        if step == 'int':
            raise KeyboardInterrupt
        if step == 'term':
            raise worker._HardStop
        time.sleep(0.02)
        return False


class SupervisorDrainTests(SimpleTestCase):

    def setUp(self):
        self.stop = threading.Event()
        self.graceful = mock.Mock()
        self.graceful.held = worker.HeldLeases()
        self.alive_at_stop = []

        def run():
            self.alive_at_stop.append(
                [thread.is_alive() for thread in self.jobs])
            self.stop.set()

        self.graceful.run.side_effect = run

    def supervise(self, drain, requested, jobs, helpers=()):
        self.jobs = list(jobs) + list(helpers)
        self.graceful.requested = requested
        with redirect_stdout(io.StringIO()) as out:
            stopped = worker._supervise(jobs, self.graceful, poll=0.02,
                                        drain=drain, helpers=helpers)
        for thread in self.jobs:
            thread.join(5)
        return stopped, out.getvalue()

    def finishing(self, drain, after=0.1):
        """A job that ends ``after`` seconds into the drain, or at a stop."""
        def job():
            while not self.stop.is_set():
                if drain.is_set():
                    time.sleep(after)
                    return
                time.sleep(0.01)
        return threading.Thread(target=job, daemon=True)

    def busy(self):
        """A job whose search only a stop ends."""
        return threading.Thread(target=self.stop.wait, args=(10,),
                                daemon=True)

    def test_the_first_ctrl_c_waits_for_the_jobs_then_stops(self):
        drain = worker.Drain(600)

        stopped, out = self.supervise(
            drain, _Requested('int'),
            [self.finishing(drain), self.finishing(drain, 0.3)])

        self.assertTrue(stopped)
        self.assertTrue(drain.is_set())
        self.assertEqual(self.alive_at_stop, [[False, False]],
                         'stopped before the jobs had finished')
        self.assertIn('Termination signal received', out)
        self.assertIn('All jobs have finished working; exiting', out)
        self.assertNotIn('Timeout expired', out)

    def test_the_second_ctrl_c_stops_at_once(self):
        drain = worker.Drain(600)

        stopped, out = self.supervise(
            drain, _Requested('int', None, None, 'int'), [self.busy()])

        self.assertTrue(stopped)
        self.assertEqual(self.alive_at_stop, [[True]])
        self.assertIn('Second interrupt: stopping now', out)

    def test_the_timeout_stops_whatever_is_still_running(self):
        drain = worker.Drain(0.2)

        started = time.monotonic()
        stopped, out = self.supervise(drain, _Requested('int'),
                                      [self.busy(), self.finishing(drain)])

        self.assertTrue(stopped)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(self.alive_at_stop, [[True, False]])
        self.assertIn('Timeout expired, returning remaining tasks back to '
                      'server', out)
        self.graceful.run.assert_called_once_with()

    def test_sigterm_never_drains(self):
        drain = worker.Drain(600)

        stopped, out = self.supervise(drain, _Requested('term'),
                                      [self.busy()])

        self.assertTrue(stopped)
        self.assertFalse(drain.is_set())
        self.assertEqual(self.alive_at_stop, [[True]])
        self.assertNotIn('Termination signal received', out)

    def test_a_zero_timeout_stops_at_the_first_ctrl_c(self):
        drain = worker.Drain(0)

        stopped, out = self.supervise(drain, _Requested('int'),
                                      [self.busy()])

        self.assertTrue(stopped)
        self.assertEqual(self.alive_at_stop, [[True]])
        self.assertEqual(out, '')

    def test_the_drain_waits_for_the_solver_too(self):
        drain = worker.Drain(600)
        solver = self.finishing(drain, 0.4)

        stopped, out = self.supervise(drain, _Requested('int'),
                                      [self.finishing(drain)],
                                      helpers=[solver])

        self.assertTrue(stopped)
        self.assertEqual(self.alive_at_stop, [[False, False]])

    def test_outside_a_drain_the_solver_does_not_keep_the_worker(self):
        # The jobs ending by themselves end the worker, as before.
        stopped, out = self.supervise(
            worker.Drain(600), _Requested(),
            [threading.Thread(target=lambda: None)], helpers=[self.busy()])

        self.assertFalse(stopped)
        self.graceful.run.assert_not_called()
        self.stop.set()


_HANGING_ENGINE = (
    'import sys, time\n'
    'for line in sys.stdin:\n'
    '    command = line.strip()\n'
    '    if command == "uci":\n'
    '        print("uciok", flush=True)\n'
    '    elif command == "isready":\n'
    '        print("readyok", flush=True)\n'
    '    elif command == "quit":\n'
    '        time.sleep(60)          # ignores it: the stop kills it\n')


class DeadEnginePipeTests(SimpleTestCase):
    """The hard stop writes to engines it has just ended.

    Reported by Torom (Linux, Python 3.15, --jobs 32) at the end of a drain:
    "Exception ignored while finalizing file <_io.TextIOWrapper name=41>:
    BrokenPipeError".  The stop killed the engine, the slot's
    ``Engine.close`` then wrote ``quit`` into the dead pipe, and the line
    left in the buffer was flushed again when the slot's thread ended and
    the pipe was garbage-collected.
    """

    def setUp(self):
        self.unraisable, self.uncaught = [], []
        hook = mock.patch.object(
            sys, 'unraisablehook',
            lambda info: self.unraisable.append(info.exc_value))
        thread_hook = mock.patch.object(
            threading, 'excepthook',
            lambda args: self.uncaught.append(args.exc_value))
        hook.start()
        thread_hook.start()
        self.addCleanup(hook.stop)
        self.addCleanup(thread_hook.stop)

    def engine(self, registry):
        script = Path(tempfile.mkdtemp()) / 'hanging engine.py'
        script.write_text(_HANGING_ENGINE, encoding='utf-8')
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            return real_popen([sys.executable, *argv], *args, **kwargs)

        with mock.patch.object(worker.subprocess, 'Popen', popen), \
                mock.patch.object(worker, '_ENGINE_PROCESSES', registry):
            engine = worker.Engine(str(script))
        # Weakly: a cleanup holding the process would keep its pipes alive
        # past the test's own ``gc.collect``.
        ref = weakref.ref(engine.p)
        self.addCleanup(lambda: ref() is not None and ref().poll() is None
                        and ref().kill())
        self.addCleanup(lambda: script.unlink() or script.parent.rmdir())
        self.assertTrue(engine.ready)
        return engine

    def test_the_hard_stop_on_a_dead_engine_raises_nothing(self):
        registry = worker._EngineProcesses()
        held = worker.HeldLeases()
        held.hold('analysis', 'box-atomicdb#13',
                  [{'id': 5275448, 'lease_token': 'tok'}])
        stop = threading.Event()
        engine = self.engine(registry)
        engine._searching = True        # a search was under way
        process = engine.p
        stopper = worker.GracefulStop('https://server', {'machine': 'box'},
                                      held, stop, processes=registry)
        out = io.StringIO()

        def slot():
            # What the slot and its heartbeat do once the stop has ended
            # the engine: a late ``stop`` from the heartbeat, then close.
            engine.stop_search()
            engine.close()

        with mock.patch.object(worker, '_release_leases',
                               lambda *args: (0, '')), \
                redirect_stdout(out):
            stopper.run()
            self.assertIsNotNone(process.poll())     # the stop killed it
            thread = threading.Thread(target=slot)
            thread.start()
            thread.join(10)
        del engine, process, thread
        gc.collect()

        self.assertEqual(self.uncaught, [])
        self.assertEqual(self.unraisable, [])
        self.assertIn('the server took back 0; 1 task was already back in '
                      'the queue (the lease had lapsed)', out.getvalue())

    def test_a_write_that_fails_in_the_stop_closes_the_pipe(self):
        engine = self.engine(worker._EngineProcesses())
        process = engine.p
        process.kill()
        process.wait(10)
        engine.p = SimpleNamespace(       # dead, but not reaped yet
            poll=lambda: None, stdin=process.stdin,
            wait=process.wait, kill=process.kill)

        self.assertEqual(worker._stop_engines([engine.p], timeout=0.5), 0)

        self.assertTrue(process.stdin.closed)
        process.stdout.close()
        del engine, process
        gc.collect()
        self.assertEqual(self.unraisable, [])


class ReleaseMessageTests(SimpleTestCase):

    def test_every_lease_taken_back(self):
        self.assertEqual(worker._describe_release(3, 3),
                         'the server took back 3')

    def test_leases_that_had_lapsed_are_said_to_be_back_in_the_queue(self):
        self.assertEqual(
            worker._describe_release(1, 3),
            'the server took back 1; 2 tasks were already back in the queue '
            '(the lease had lapsed)')


class SignalRoutingTests(SimpleTestCase):

    def test_sigterm_raises_a_stop_that_is_never_drained(self):
        previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)

        with worker._stop_signals(mock.Mock()):
            with self.assertRaises(worker._HardStop):
                signal.raise_signal(signal.SIGTERM)
        self.assertTrue(issubclass(worker._HardStop, KeyboardInterrupt))


class EngineProcessGroupTests(SimpleTestCase):
    """Every engine starts where a console's Ctrl+C does not reach it."""

    def test_the_options_put_the_engine_in_a_group_of_its_own(self):
        options = worker._engine_process_options()
        if os.name == 'nt':
            self.assertEqual(options, {
                'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP})
        else:
            self.assertEqual(options, {'start_new_session': True})

    def test_every_engine_process_is_started_with_them(self):
        seen = []

        class Started(OSError):
            pass

        def popen(*args, **kwargs):
            seen.append(kwargs)
            raise Started

        expected = worker._engine_process_options()
        with mock.patch.object(worker.subprocess, 'Popen', popen):
            with self.assertRaises(Started):
                worker.Engine('engine')
            with self.assertRaises(Started):
                worker.Solver('engine').solve(START_FEN, 'WHITE_WIN', 10)
            worker.check_engine_identity('engine')     # reports, not raises
            with self.assertRaises(worker.EngineUpdateError):
                worker._probe_engine('engine')
        self.assertEqual(len(seen), 4)
        for kwargs in seen:
            for key, value in expected.items():
                self.assertEqual(kwargs.get(key), value)


# ---------------------------------------------------------------------------
# The published file, as a process
# ---------------------------------------------------------------------------

# A stand-in engine whose search ends by itself ``finish`` seconds after
# ``go`` (never when None), or at ``stop``/``quit``.  It is a Python script,
# so a SIGINT that reached it would end it with a KeyboardInterrupt.
_ENGINE = '''#!{python}
import os, sys, threading

marker, finish = {marker!r}, {finish!r}
with open(marker + '.pid', 'w') as handle:
    handle.write(str(os.getpid()))
lock = threading.Lock()
state = {{'searching': False}}


def bestmove():
    with lock:
        if state['searching']:
            state['searching'] = False
            print('bestmove e2e4', flush=True)


for line in sys.stdin:
    words = line.split()
    if not words:
        continue
    if words[0] == 'uci':
        print('id name Atomic-Stockfish stand-in', flush=True)
        print('uciok', flush=True)
    elif words[0] == 'isready':
        print('readyok', flush=True)
    elif words[0] == 'go':
        with lock:
            state['searching'] = True
            print('info depth 1 seldepth 1 multipv 1 score cp 12 nodes 1000 '
                  'nps 1000 pv e2e4', flush=True)
        open(marker + '.go', 'w').close()
        if finish is not None:
            threading.Timer(finish, bestmove).start()
    elif words[0] == 'stop':
        bestmove()
    elif words[0] == 'quit':
        bestmove()
        break
'''


@unittest.skipUnless(os.name == 'posix', 'a real SIGINT to a process group; '
                     'the Windows paths are covered above')
class WorkerProcessDrainTests(SimpleTestCase):
    """The worker file run as a process, interrupted as a terminal does."""

    def engine(self, finish):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.marker = str(self.dir / 'engine')
        engine = self.dir / 'stand-in-engine'
        engine.write_text(_ENGINE.format(python=sys.executable,
                                         marker=self.marker, finish=finish),
                          encoding='utf-8')
        engine.chmod(0o755)
        return engine

    def start(self, server, engine, *flags):
        # In a session of its own, so that a SIGINT to its process group --
        # what a terminal's Ctrl+C is -- reaches the worker and whatever
        # shares its group, and not this test.
        process = subprocess.Popen(
            [sys.executable, str(WORKER_FILE), '-U', 'u', '-P', 'p',
             '-S', server.url, '--engine', str(engine), '-T', '1',
             '--hash', '16', '--no-auto-update', '--no-fetch-syzygy',
             *flags],
            cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.output = []
        reader = threading.Thread(
            target=lambda: self.output.extend(iter(process.stdout.readline,
                                                   '')), daemon=True)
        reader.start()
        self.reader = reader
        self.assertTrue(
            _wait_until(lambda: os.path.exists(self.marker + '.go'), 60),
            'the worker never started a search')
        return process

    def ctrl_c(self, process):
        os.killpg(process.pid, signal.SIGINT)

    def said(self, text):
        return any(text in line for line in self.output)

    def finish(self, process, within):
        started = time.monotonic()
        process.wait(timeout=within + 20)
        self.reader.join(5)
        return process.returncode, time.monotonic() - started, \
            ''.join(self.output)

    def engine_pid(self):
        return int(Path(self.marker + '.pid').read_text())

    def test_the_first_ctrl_c_lets_the_search_finish(self):
        server = _StandInServer(tasks=5)
        self.addCleanup(server.close)
        process = self.start(server, self.engine(finish=3))

        self.ctrl_c(process)
        code, took, out = self.finish(process, within=15)

        self.assertEqual(code, 0, out)
        # The engine was not interrupted with the worker: its search ended
        # by itself and was submitted.
        self.assertEqual([form['task_id'] for form in
                          server.sent('/atomicdb/api/submit')], ['4242'], out)
        self.assertEqual(server.sent('/atomicdb/api/release'), [], out)
        self.assertEqual(len(server.sent('/atomicdb/api/lease')), 1, out)
        self.assertIn('Termination signal received', out)
        self.assertIn('[0] has finished working', out)
        self.assertIn('All jobs have finished working; exiting', out)
        self.assertTrue(_wait_until(lambda: _gone(self.engine_pid()), 10))

    def test_the_drain_timeout_hands_the_task_back(self):
        server = _StandInServer()
        self.addCleanup(server.close)
        process = self.start(server, self.engine(finish=None),
                             '--drain-timeout', '2s')

        self.ctrl_c(process)
        code, took, out = self.finish(process, within=10)

        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(took, 1.5, out)
        self.assertIn('Timeout expired, returning remaining tasks back to '
                      'server', out)
        released = server.sent('/atomicdb/api/release')
        self.assertEqual(len(released), 1, out)
        self.assertEqual([lease['task_id'] for lease in
                          json.loads(released[0]['leases'])], [4242])
        self.assertEqual(server.sent('/atomicdb/api/submit'), [], out)
        self.assertTrue(_wait_until(lambda: _gone(self.engine_pid()), 10))

    def test_the_second_ctrl_c_stops_at_once(self):
        server = _StandInServer()
        self.addCleanup(server.close)
        process = self.start(server, self.engine(finish=None))

        self.ctrl_c(process)
        self.assertTrue(_wait_until(
            lambda: self.said('Termination signal received'), 10))
        self.ctrl_c(process)
        code, took, out = self.finish(process, within=10)

        self.assertEqual(code, 0, out)
        self.assertLess(took, 10, out)
        self.assertIn('Second interrupt: stopping now', out)
        self.assertEqual(len(server.sent('/atomicdb/api/release')), 1, out)
        self.assertEqual(server.sent('/atomicdb/api/submit'), [], out)
        self.assertTrue(_wait_until(lambda: _gone(self.engine_pid()), 10))
