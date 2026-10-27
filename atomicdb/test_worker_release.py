"""The AtomicDB worker hands its tasks back when it is stopped.

Ctrl+C, SIGTERM, Ctrl+Break and a closed console window all end the same way:
the engines are ended, every lease the worker still holds goes back to the
server in one ``api/release`` call, and the process exits.  It exits promptly
whatever the server answers -- a server without the endpoint says 404 -- and
makes no call at all when it holds nothing.

The last class runs the published file itself, as a process, against a
stand-in server and a stand-in engine, and stops it with a real signal.
"""

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
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs

from django.test import SimpleTestCase

from Client import atomicdb_worker as worker

WORKER_FILE = Path(worker.__file__).resolve()
START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
AUTH = {'username': 'u', 'password': 'p', 'machine': 'box-atomicdb#0'}


def _wait_until(predicate, timeout, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class _Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError('no JSON')
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise worker.requests.HTTPError(str(self.status_code))


class HeldLeasesTests(SimpleTestCase):

    def test_a_new_lease_replaces_what_the_slot_held(self):
        held = worker.HeldLeases()
        held.hold('analysis', 'm#0', [{'id': 1, 'lease_token': 'a'}])
        held.hold('analysis', 'm#1', [{'id': 2, 'lease_token': 'b'}])
        held.hold('analysis', 'm#0', [{'id': 3, 'lease_token': 'c'}])

        self.assertEqual(held.snapshot(), [
            {'kind': 'analysis', 'machine': 'm#0', 'task_id': 3,
             'lease_token': 'c'},
            {'kind': 'analysis', 'machine': 'm#1', 'task_id': 2,
             'lease_token': 'b'}])

    def test_a_task_whose_submit_was_answered_is_not_handed_back(self):
        held = worker.HeldLeases()
        held.hold('analysis', 'm', [{'id': 1, 'lease_token': 'a'}])
        held.drop('analysis', 'm', 1)
        held.drop('analysis', 'm', 99)          # unknown: nothing happens
        self.assertEqual(held.snapshot(), [])

    def test_a_lease_without_a_token_is_left_to_its_expiry(self):
        held = worker.HeldLeases()
        held.hold('analysis', 'm', [{'id': 1}, {'id': 2, 'lease_token': ''}])
        self.assertEqual(held.snapshot(), [])

    def test_a_proof_is_held_until_its_submit_is_answered(self):
        held = worker.HeldLeases()
        state = worker.SolveLeaseState(held, 'm#0')
        state.start(41, 'token-41')
        state.clear()               # an engine failure ends the heartbeat...
        self.assertIsNone(state.snapshot())
        self.assertEqual(held.snapshot(), [         # ...not the lease
            {'kind': 'solve', 'machine': 'm#0', 'task_id': 41,
             'lease_token': 'token-41'}])
        state.settled(41)
        self.assertEqual(held.snapshot(), [])

    def test_the_solver_drops_its_lease_once_the_server_answers(self):
        held = worker.HeldLeases()
        state = worker.SolveLeaseState(held, 'm#0')
        solver = mock.Mock(build_sha='')
        solver.solve.return_value = ('UNKNOWN', None, None, 10, 1.0, '', {})
        session = mock.Mock()
        session.post.side_effect = [
            _Response(200, {'tasks': [{'id': 7, 'fen': START_FEN,
                                       'budget_nodes': 10,
                                       'lease_token': 't7'}]}),
            _Response(200, {'ok': True, 'summary': 'stored'})]

        with mock.patch.object(worker, '_http_session', return_value=session), \
                redirect_stdout(io.StringIO()):
            self.assertTrue(worker._solve_once('http://server', AUTH, solver,
                                               'session', state))

        self.assertEqual(held.snapshot(), [])

    def test_a_proof_whose_engine_failed_stays_held(self):
        held = worker.HeldLeases()
        state = worker.SolveLeaseState(held, 'm#0')
        solver = mock.Mock(build_sha='')
        solver.solve.side_effect = worker.SolverEngineFailure('no solve')
        session = mock.Mock()
        session.post.return_value = _Response(200, {'tasks': [
            {'id': 7, 'fen': START_FEN, 'budget_nodes': 10,
             'lease_token': 't7'}]})

        with mock.patch.object(worker, '_http_session', return_value=session), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(worker.SolverEngineFailure):
                worker._solve_once('http://server', AUTH, solver, 'session',
                                   state)

        self.assertEqual([lease['task_id'] for lease in held.snapshot()], [7])


class ReleaseCallTests(SimpleTestCase):
    """``_release_leases``: one call, and never a wait past its deadline."""

    LEASES = [{'kind': 'analysis', 'machine': 'box-atomicdb#0',
               'task_id': 4242, 'lease_token': 'tok'}]

    def test_one_call_names_every_lease(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(200, {'ok': True, 'released': 1})

        released = worker._release_leases('https://server/', AUTH,
                                          self.LEASES, post=post)

        self.assertEqual(released, (1, ''))
        self.assertEqual(len(calls), 1)
        url, kwargs = calls[0]
        self.assertEqual(url, 'https://server/atomicdb/api/release')
        self.assertEqual(json.loads(kwargs['data']['leases']), self.LEASES)
        self.assertEqual(kwargs['data']['machine'], 'box-atomicdb#0')
        self.assertEqual(kwargs['data']['password'], 'p')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'],
                         (worker.RELEASE_TIMEOUT_SECONDS,) * 2)

    def test_a_server_without_the_endpoint_is_no_error(self):
        released = worker._release_leases(
            'https://server', AUTH, self.LEASES,
            post=lambda url, **kwargs: _Response(404))
        self.assertEqual(released, (None, 'HTTP 404'))

    def test_a_server_that_does_not_answer_is_given_up_on(self):
        def post(url, **kwargs):
            time.sleep(5)
            return _Response(200, {'ok': True, 'released': 1})

        started = time.monotonic()
        released = worker._release_leases('https://server', AUTH,
                                          self.LEASES, timeout=0.3, post=post)

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(released, (None, 'no answer within 0.3s'))

    def test_a_network_error_is_reported_not_raised(self):
        def post(url, **kwargs):
            raise worker.requests.ConnectionError('connection refused')

        released, problem = worker._release_leases('https://server', AUTH,
                                                   self.LEASES, post=post)
        self.assertIsNone(released)
        self.assertIn('connection refused', problem)


class _Process:
    """A stand-in engine process for ``_stop_engines``."""

    def __init__(self, log, obeys=True):
        self.log, self.obeys, self.code = log, obeys, None
        self.stdin = self

    def poll(self):
        return self.code

    def write(self, text):
        self.log.append(('stdin', text))
        if self.obeys and text == 'quit\n':
            self.code = 0

    def flush(self):
        pass

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired('engine', timeout)
        return self.code

    def kill(self):
        self.log.append(('kill', None))
        self.code = -9


class GracefulStopTests(SimpleTestCase):

    def setUp(self):
        self.log = []
        self.processes = worker._EngineProcesses()
        self.held = worker.HeldLeases()
        self.stop = threading.Event()

    def stopper(self, threads=()):
        return worker.GracefulStop('https://server', AUTH, self.held,
                                   self.stop, threads=threads,
                                   processes=self.processes)

    def run_stop(self, stopper, answer=(2, '')):
        def release(server, auth, leases):
            self.log.append(('release', leases))
            return answer

        out = io.StringIO()
        with mock.patch.object(worker, '_release_leases', release), \
                redirect_stdout(out):
            stopper.run()
        return out.getvalue()

    def test_a_stop_hands_everything_back_in_one_call(self):
        self.held.hold('analysis', 'box-atomicdb#0',
                       [{'id': 1, 'lease_token': 'a'}])
        self.held.hold('analysis', 'box-atomicdb#1',
                       [{'id': 2, 'lease_token': 'b'}])
        self.held.hold('solve', 'box-atomicdb#0',
                       [{'id': 3, 'lease_token': 'c'}])
        stopper = self.stopper()

        out = self.run_stop(stopper, answer=(3, ''))

        self.assertTrue(self.stop.is_set())
        self.assertTrue(stopper.done.is_set())
        releases = [entry for entry in self.log if entry[0] == 'release']
        self.assertEqual(len(releases), 1)
        self.assertEqual(sorted(lease['task_id'] for lease in releases[0][1]),
                         [1, 2, 3])
        self.assertIn('handing 3 task(s) back', out)
        self.assertIn('took back 3', out)

    def test_nothing_held_means_no_call(self):
        out = self.run_stop(self.stopper())

        self.assertEqual(self.log, [])
        self.assertTrue(self.stop.is_set())
        self.assertEqual(out, '')

    def test_the_engines_are_ended_before_the_leases_go_back(self):
        quits, ignores = _Process(self.log), _Process(self.log, obeys=False)
        self.processes.add(quits)
        self.processes.add(ignores)
        self.held.hold('analysis', 'm', [{'id': 1, 'lease_token': 'a'}])

        self.run_stop(self.stopper())

        kinds = [entry[0] for entry in self.log]
        self.assertEqual(kinds.count('release'), 1)
        self.assertLess(kinds.index('stdin'), kinds.index('release'))
        self.assertLess(kinds.index('kill'), kinds.index('release'))
        self.assertEqual(quits.code, 0)
        self.assertEqual(ignores.code, -9)

    def test_a_server_that_refuses_still_ends_the_stop(self):
        self.held.hold('analysis', 'm', [{'id': 1, 'lease_token': 'a'}])
        stopper = self.stopper()

        out = self.run_stop(stopper, answer=(None, 'HTTP 404'))

        self.assertTrue(stopper.done.is_set())
        self.assertIn('did not take them back (HTTP 404)', out)

    def test_two_callers_make_one_stop(self):
        self.held.hold('analysis', 'm', [{'id': 1, 'lease_token': 'a'}])
        stopper = self.stopper()
        entered = threading.Event()
        release_calls = []

        def slow_release(server, auth, leases):
            release_calls.append(leases)
            entered.set()
            time.sleep(0.3)
            return 1, ''

        with mock.patch.object(worker, '_release_leases', slow_release), \
                redirect_stdout(io.StringIO()):
            first = threading.Thread(target=stopper.run)
            first.start()
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            stopper.run(wait=5)            # the console handler, say
            waited = time.monotonic() - started
            first.join(5)

        self.assertEqual(len(release_calls), 1)
        self.assertGreater(waited, 0.1, 'the second caller did not wait')

    def test_a_result_already_on_its_way_is_not_handed_back(self):
        self.held.hold('analysis', 'm', [{'id': 1, 'lease_token': 'a'}])

        def job():
            # A submit that the server answers just after the stop began.
            self.stop.wait(5)
            time.sleep(0.05)
            self.held.drop('analysis', 'm', 1)

        thread = threading.Thread(target=job)
        thread.start()
        self.run_stop(self.stopper(threads=[thread]))
        thread.join(5)

        self.assertEqual([entry for entry in self.log
                          if entry[0] == 'release'], [])


class EngineStopTests(SimpleTestCase):
    """``_stop_engines`` with real processes."""

    def start(self, code):
        process = subprocess.Popen(
            [sys.executable, '-c', code], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def test_an_engine_that_obeys_quit_is_not_killed(self):
        process = self.start('import sys\n'
                             'for line in sys.stdin:\n'
                             '    if line.strip() == "quit":\n'
                             '        sys.exit(0)\n')

        self.assertEqual(worker._stop_engines([process], timeout=10), 0)
        self.assertEqual(process.returncode, 0)

    def test_engines_that_ignore_quit_are_killed_within_the_deadline(self):
        # One deadline for all of them, not one each: --jobs 8 must not
        # make a closing console wait eight times as long.
        processes = [self.start('import time\ntime.sleep(60)\n')
                     for _ in range(4)]
        started = time.monotonic()

        self.assertEqual(worker._stop_engines(processes, timeout=0.5), 4)

        self.assertLess(time.monotonic() - started, 0.5 + 0.25 + 1.5)
        for process in processes:
            self.assertIsNotNone(process.poll())

    def test_an_engine_already_gone_is_only_reaped(self):
        process = self.start('pass')
        process.wait(10)
        self.assertEqual(worker._stop_engines([process], timeout=0.5), 0)

    def test_a_proof_in_progress_is_ended_by_a_stop(self):
        real_popen = subprocess.Popen
        registry = worker._EngineProcesses()
        script = Path(tempfile.mkdtemp()) / 'slow solver.py'
        self.addCleanup(lambda: script.unlink() or script.parent.rmdir())
        script.write_text('import sys, time\nsys.stdin.readline()\n'
                          'time.sleep(60)\n', encoding='utf-8')
        outcome = {}

        def popen(argv, *args, **kwargs):
            return real_popen([sys.executable, *argv], *args, **kwargs)

        def prove():
            try:
                worker.Solver(str(script)).solve(START_FEN, 'WHITE_WIN', 1000)
            except Exception as exc:
                outcome['error'] = exc

        with mock.patch.object(worker.subprocess, 'Popen', popen), \
                mock.patch.object(worker, '_ENGINE_PROCESSES', registry):
            thread = threading.Thread(target=prove)
            thread.start()
            self.assertTrue(_wait_until(lambda: registry.snapshot(), 10))
            worker._stop_engines(timeout=0.5)
            thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get('error'),
                              worker.SolverEngineFailure)
        self.assertEqual(registry.snapshot(), [])


class _Requested:
    """``GracefulStop.requested`` whose first wait is a Ctrl+C."""

    def __init__(self):
        self.calls = 0

    def is_set(self):
        return False

    def wait(self, timeout):
        self.calls += 1
        raise KeyboardInterrupt


class SupervisorTests(SimpleTestCase):

    def graceful(self, held=None):
        graceful = mock.Mock()
        graceful.held = held or worker.HeldLeases()
        graceful.requested = threading.Event()
        return graceful

    def jobs(self, stop, count=2):
        return [threading.Thread(target=stop.wait, args=(10,), daemon=True)
                for _ in range(count)]

    def test_ctrl_c_runs_the_stop_once_and_asks_for_an_exit(self):
        stop = threading.Event()
        graceful = self.graceful()
        graceful.requested = _Requested()
        graceful.run.side_effect = lambda: stop.set()

        self.assertTrue(worker._supervise(self.jobs(stop), graceful))
        graceful.run.assert_called_once_with()

    def test_a_second_ctrl_c_during_the_stop_still_exits(self):
        stop = threading.Event()
        graceful = self.graceful()
        graceful.requested = _Requested()

        def interrupted_stop():
            stop.set()
            raise KeyboardInterrupt

        graceful.run.side_effect = interrupted_stop
        self.assertTrue(worker._supervise(self.jobs(stop), graceful))

    def test_a_stop_begun_by_another_thread_is_waited_for(self):
        stop = threading.Event()
        graceful = self.graceful()
        threading.Timer(0.1, graceful.requested.set).start()
        graceful.run.side_effect = lambda: stop.set()

        self.assertTrue(worker._supervise(self.jobs(stop), graceful,
                                          poll=0.05))
        graceful.run.assert_called_once_with()

    def test_jobs_that_end_holding_nothing_end_the_worker_quietly(self):
        graceful = self.graceful()
        done = [threading.Thread(target=lambda: None) for _ in range(2)]

        self.assertFalse(worker._supervise(done, graceful, poll=0.05))
        graceful.run.assert_not_called()

    def test_jobs_that_end_leaving_a_lease_hand_it_back(self):
        held = worker.HeldLeases()
        held.hold('analysis', 'm', [{'id': 1, 'lease_token': 'a'}])
        graceful = self.graceful(held)
        done = [threading.Thread(target=lambda: None)]

        self.assertFalse(worker._supervise(done, graceful, poll=0.05))
        graceful.run.assert_called_once_with()

    def test_exit_now_flushes_and_exits_with_the_code(self):
        codes = []
        worker._exit_now(0, exit_process=codes.append)
        self.assertEqual(codes, [0])


class SignalRoutingTests(SimpleTestCase):
    """Every way of stopping the worker reaches the same stop."""

    def graceful(self):
        graceful = mock.Mock()
        graceful.requested = threading.Event()
        return graceful

    def at_default(self, signum):
        """``signum`` at its default for this test, as in a fresh worker."""
        previous = signal.signal(signum, signal.SIG_DFL)
        self.addCleanup(signal.signal, signum, previous)

    def test_sigterm_stops_the_worker_as_ctrl_c_does(self):
        self.at_default(signal.SIGTERM)

        with worker._stop_signals(self.graceful()):
            self.assertIs(signal.getsignal(signal.SIGTERM), worker._interrupt)
            with self.assertRaises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGTERM)

        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    @unittest.skipUnless(hasattr(signal, 'SIGBREAK'), 'Windows Ctrl+Break')
    def test_ctrl_break_stops_the_worker_as_ctrl_c_does(self):
        self.at_default(signal.SIGBREAK)

        with worker._stop_signals(self.graceful()):
            with self.assertRaises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGBREAK)

        self.assertIs(signal.getsignal(signal.SIGBREAK), signal.SIG_DFL)

    @unittest.skipUnless(hasattr(signal, 'SIGHUP'), 'POSIX terminals')
    def test_a_terminal_that_goes_away_stops_the_worker_too(self):
        self.at_default(signal.SIGHUP)

        with worker._stop_signals(self.graceful()):
            self.assertIs(signal.getsignal(signal.SIGHUP), worker._interrupt)

        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)

    def test_a_signal_set_to_be_ignored_stays_ignored(self):
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)

        with worker._stop_signals(self.graceful()):
            self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)

        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)

    def test_closing_the_console_runs_the_stop_before_returning(self):
        graceful = self.graceful()
        for event in worker._CONSOLE_CLOSE_EVENTS:
            with self.subTest(event=event):
                graceful.run.reset_mock()
                self.assertTrue(worker._on_console_event(graceful, event))
                graceful.run.assert_called_once_with(
                    wait=worker.CONSOLE_CLOSE_WAIT_SECONDS)

    def test_ctrl_c_and_ctrl_break_are_left_to_the_signal_handlers(self):
        graceful = self.graceful()
        for event in (0, 1):     # CTRL_C_EVENT, CTRL_BREAK_EVENT
            self.assertFalse(worker._on_console_event(graceful, event))
        graceful.run.assert_not_called()

    def test_the_whole_stop_fits_in_what_a_closing_window_allows(self):
        # Windows ends the process about five seconds after the window is
        # closed; the reaping of killed engines adds at most 0.25 s.
        self.assertLessEqual(
            worker.ENGINE_QUIT_SECONDS + 0.25 + worker.STOP_JOIN_SECONDS
            + worker.RELEASE_TIMEOUT_SECONDS, 4.75)
        self.assertLess(worker.CONSOLE_CLOSE_WAIT_SECONDS, 5)

    @unittest.skipUnless(os.name == 'nt', 'Windows console handler')
    def test_the_console_handler_is_registered_and_removed(self):
        handler = worker._ConsoleCloseHandler(self.graceful())
        handler.remove()


class _SlotEngine:
    """An ``Engine`` for one task, whose search ``behaviour`` decides."""

    started = 0

    def __init__(self, path, threads=1, hash_mb=256, syzygy=''):
        type(self).started += 1
        self.ready = True

    def analyse(self, fen, nodes, multipv, searchmoves=None, progress=None):
        return self.behaviour()

    def close(self):
        pass


class SlotStopTests(SimpleTestCase):
    """``_analysis_slot`` once ``stop`` is set."""

    def run_slot(self, behaviour, tasks=None):
        stop = threading.Event()
        held = worker.HeldLeases()
        submitted = []
        script = [tasks or [{'id': 9, 'fen': START_FEN, 'budget_nodes': 100,
                             'multipv': 1, 'lease_token': 'tok-9'}]]

        def request_tasks(server, auth, lease_session):
            if not script:
                stop.set()
                return []
            return script.pop(0)

        def submit(server, payload, task_id):
            submitted.append(payload)
            return _Response(200, {'ok': True, 'summary': 'stored'})

        engine = type('Engine', (_SlotEngine,), {
            'started': 0, 'behaviour': staticmethod(
                lambda: behaviour(stop))})
        a = SimpleNamespace(engine='engine', hash=16, syzygy='',
                            S='https://server', no_auto_update=True,
                            once=False)
        with mock.patch.object(worker, 'Engine', engine), \
                mock.patch.object(worker, '_request_tasks', request_tasks), \
                mock.patch.object(worker, '_submit_until_definitive', submit), \
                mock.patch.object(worker, '_heartbeat_loop',
                                  lambda *args: None), \
                redirect_stdout(io.StringIO()) as out:
            worker._analysis_slot(0, a, None, {'machine': 'box-atomicdb'},
                                  {}, 1, stop, False, held=held)
        return submitted, held, engine, out.getvalue()

    def test_a_search_cut_short_by_the_stop_is_not_submitted(self):
        def quit_mid_search(stop):
            # The stop told the engine to quit, and it printed bestmove.
            stop.set()
            return [{'move': 'e2e4', 'pv': ['e2e4'], 'eval_cp': 10,
                     'mate': None, 'raw': 'info depth 1 nodes 50 pv e2e4'}]

        submitted, held, _, out = self.run_slot(quit_mid_search)

        self.assertEqual(submitted, [])
        self.assertEqual([lease['task_id'] for lease in held.snapshot()], [9])
        self.assertIn('task 9 interrupted by the stop', out)

    def test_an_engine_ended_by_the_console_waits_for_the_stop(self):
        def killed_by_ctrl_c(stop):
            threading.Timer(0.2, stop.set).start()
            raise worker.EngineExited('the engine stopped in the middle of '
                                      'a search (exit code 3221225786)',
                                      0xC000013A)

        submitted, held, engine, out = self.run_slot(killed_by_ctrl_c)

        self.assertEqual(engine.started, 1, 'a new engine was started')
        self.assertEqual(submitted, [])
        self.assertEqual([lease['task_id'] for lease in held.snapshot()], [9])
        self.assertNotIn('restarting the engine', out)

    def test_an_answered_submit_leaves_nothing_to_hand_back(self):
        def finishes(stop):
            return [{'move': 'e2e4', 'pv': ['e2e4'], 'eval_cp': 10,
                     'mate': None, 'raw': 'info depth 1 nodes 100 pv e2e4'}]

        submitted, held, _, _ = self.run_slot(finishes)

        self.assertEqual([payload['task_id'] for payload in submitted], [9])
        self.assertEqual(held.snapshot(), [])


# ---------------------------------------------------------------------------
# The published file, as a process
# ---------------------------------------------------------------------------

_ENGINE = '''#!{python}
import os, sys

marker = {marker!r}
with open(marker + '.pid', 'w') as handle:
    handle.write(str(os.getpid()))
searching = False
for line in sys.stdin:
    words = line.split()
    if not words:
        continue
    if words[0] == 'uci':
        print('id name Stand-in', flush=True)
        print('uciok', flush=True)
    elif words[0] == 'isready':
        print('readyok', flush=True)
    elif words[0] == 'go':
        print('info depth 1 seldepth 1 multipv 1 score cp 12 nodes 1000 '
              'nps 1000 pv e2e4', flush=True)
        open(marker + '.go', 'w').close()
        searching = True
    elif words[0] == 'quit':
        if searching:
            # As Stockfish does when quit cuts a search short.
            print('bestmove e2e4', flush=True)
        break
'''


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The worker that a test stopped has gone before a slow answer is
        # written: a broken pipe here is the point of the test, not news.
        pass


class _StandInServer:
    """Answers the worker protocol and records what it is sent."""

    def __init__(self, release='ok', tasks=1):
        self.release_mode, self.tasks_left = release, tasks
        self.requests = []
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                form = {key: values[0] for key, values in parse_qs(
                    self.rfile.read(length).decode(),
                    keep_blank_values=True).items()}
                with server.lock:
                    server.requests.append((self.path, form))
                status, payload = server.answer(self.path, form)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = _QuietServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def answer(self, path, form):
        if path == '/atomicdb/api/lease':
            with self.lock:
                give = self.tasks_left > 0
                self.tasks_left -= give
            return 200, {'tasks': [{
                'id': 4242, 'fen': START_FEN, 'budget_nodes': 10 ** 9,
                'multipv': 1, 'searchmoves': [],
                'lease_token': 'tok-4242'}] if give else []}
        if path == '/atomicdb/api/release':
            if self.release_mode == '404':
                return 404, {'error': 'not found'}
            if self.release_mode == 'hang':
                time.sleep(8)
            return 200, {'ok': True,
                         'released': len(json.loads(form['leases']))}
        if path in ('/atomicdb/api/heartbeat', '/atomicdb/api/submit'):
            return 200, {'ok': True, 'summary': 'stored'}
        return 404, {'error': 'not found'}

    def sent(self, path):
        with self.lock:
            return [form for seen, form in self.requests if seen == path]


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:                                    # a zombie is gone too
        stat = Path(f'/proc/{pid}/stat').read_text()
        return stat.rsplit(')', 1)[1].split()[0] == 'Z'
    except OSError:
        return False


@unittest.skipUnless(os.name == 'posix', 'a real signal to a real process; '
                     'the Windows paths are covered above')
class WorkerProcessStopTests(SimpleTestCase):
    """The worker file run as a process, stopped by a real signal."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.marker = str(self.dir / 'engine')
        engine = self.dir / 'stand-in-engine'
        engine.write_text(_ENGINE.format(python=sys.executable,
                                         marker=self.marker),
                          encoding='utf-8')
        engine.chmod(0o755)
        self.engine = engine

    def start(self, server, *flags):
        process = subprocess.Popen(
            [sys.executable, str(WORKER_FILE), '-U', 'u', '-P', 'p',
             '-S', server.url, '--engine', str(self.engine), '-T', '1',
             '--hash', '16', '--no-auto-update', '--no-fetch-syzygy',
             *flags],
            cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def stop(self, process, signum=signal.SIGTERM, within=10):
        os.kill(process.pid, signum)
        started = time.monotonic()
        out, _ = process.communicate(timeout=within + 20)
        return process.returncode, time.monotonic() - started, out

    def searching(self):
        return _wait_until(lambda: os.path.exists(self.marker + '.go'), 60)

    def engine_pid(self):
        return int(Path(self.marker + '.pid').read_text())

    def test_sigterm_hands_the_task_back_and_exits(self):
        server = _StandInServer()
        self.addCleanup(server.close)
        process = self.start(server)
        self.assertTrue(self.searching(), 'the worker never started a search')

        code, took, out = self.stop(process)

        self.assertEqual(code, 0, out)
        self.assertLess(took, 10, out)
        released = server.sent('/atomicdb/api/release')
        self.assertEqual(len(released), 1, out)
        lease, = json.loads(released[0]['leases'])
        self.assertEqual((lease['kind'], lease['task_id'],
                          lease['lease_token']),
                         ('analysis', 4242, 'tok-4242'))
        self.assertEqual(lease['machine'], released[0]['machine'])
        self.assertEqual(released[0]['username'], 'u')
        # The engine printed bestmove on its way out: still not a result.
        self.assertEqual(server.sent('/atomicdb/api/submit'), [], out)
        self.assertTrue(_wait_until(lambda: _gone(self.engine_pid()), 10))

    def test_ctrl_c_hands_the_task_back_and_exits(self):
        server = _StandInServer()
        self.addCleanup(server.close)
        process = self.start(server)
        self.assertTrue(self.searching(), 'the worker never started a search')

        code, took, out = self.stop(process, signal.SIGINT)

        self.assertEqual(code, 0, out)
        self.assertEqual(len(server.sent('/atomicdb/api/release')), 1, out)
        self.assertEqual(server.sent('/atomicdb/api/submit'), [], out)
        self.assertTrue(_wait_until(lambda: _gone(self.engine_pid()), 10))

    def test_a_server_without_the_endpoint_still_lets_it_exit(self):
        server = _StandInServer(release='404')
        self.addCleanup(server.close)
        process = self.start(server)
        self.assertTrue(self.searching(), 'the worker never started a search')

        code, took, out = self.stop(process)

        self.assertEqual(code, 0, out)
        self.assertLess(took, 10, out)
        self.assertIn('did not take them back (HTTP 404)', out)

    def test_a_server_that_does_not_answer_still_lets_it_exit(self):
        server = _StandInServer(release='hang')
        self.addCleanup(server.close)
        process = self.start(server)
        self.assertTrue(self.searching(), 'the worker never started a search')

        code, took, out = self.stop(process)

        self.assertEqual(code, 0, out)
        self.assertLess(took, worker.RELEASE_TIMEOUT_SECONDS
                        + worker.ENGINE_QUIT_SECONDS
                        + worker.STOP_JOIN_SECONDS + 3, out)
        self.assertIn('no answer within', out)

    def test_nothing_held_means_no_call(self):
        server = _StandInServer(tasks=0)
        self.addCleanup(server.close)
        process = self.start(server)
        self.assertTrue(_wait_until(
            lambda: server.sent('/atomicdb/api/lease'), 60))

        code, took, out = self.stop(process)

        self.assertEqual(code, 0, out)
        self.assertLess(took, 10, out)
        self.assertEqual(server.sent('/atomicdb/api/release'), [], out)

    def test_a_single_pass_hands_its_task_back_too(self):
        # --once runs its pass in the main thread and leaves as it always
        # did, through the KeyboardInterrupt -- after the hand-back.
        server = _StandInServer()
        self.addCleanup(server.close)
        process = self.start(server, '--once')
        self.assertTrue(self.searching(), 'the worker never started a search')

        code, took, out = self.stop(process)

        self.assertIn(code, (-signal.SIGINT, 128 + signal.SIGINT), out)
        self.assertLess(took, 10, out)
        self.assertEqual(len(server.sent('/atomicdb/api/release')), 1, out)
        self.assertEqual(server.sent('/atomicdb/api/submit'), [], out)
