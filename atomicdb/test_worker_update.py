import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from Client import atomicdb_worker

# The update protocol the worker speaks.  A worker installs only a file that
# declares its own protocol, so raising it leaves every older copy on its
# build until its owner downloads the new file by hand.  Pinned on purpose:
# a bump has to change this line too.
UPDATE_PROTOCOL = 2


def _source(build, body='pass', protocol=UPDATE_PROTOCOL):
    return (
        '#!/usr/bin/env python3\n'
        f'ATOMICDB_WORKER_UPDATE_PROTOCOL = {protocol}\n'
        f'ATOMICDB_WORKER_BUILD = {build}\n'
        'class Engine:\n    pass\n'
        'def _install_worker_update(*args):\n    pass\n'
        'def _submit_until_definitive(*args):\n    pass\n'
        f'def main():\n    {body}\n'
        "if __name__ == '__main__':\n    main()\n"
    ).encode()


class _Response:
    def __init__(self, content, status=200,
                 url='https://example.invalid/atomicdb/engines/atomicdb_worker.py'):
        self.content = content
        self.status_code = status
        self.url = url
        self.headers = {'Content-Length': str(len(content))}
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise atomicdb_worker.requests.HTTPError(str(self.status_code))

    def close(self):
        self.closed = True


class _OsAs:
    """The worker's ``os`` as another platform would show it.

    Only the module's own reference is replaced: pathlib, subprocess and every
    other thread keep the real ``os``, so a restart branch can be exercised on
    any host without faking the platform for the whole process.
    """

    def __init__(self, name, **overrides):
        self.name = name
        self.__dict__.update(overrides)

    def __getattr__(self, attr):
        return getattr(os, attr)


class WorkerAutoUpdateTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / 'atomicdb_worker.py'
        self.current = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD)
        self.script.write_bytes(self.current)
        self.session = mock.Mock()
        patcher = mock.patch('Client.atomicdb_worker._http_session',
                             return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_build_is_a_noop(self):
        self.session.get.return_value = _Response(self.current)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)
        self.assertFalse(self.script.with_name(
            self.script.name + '.previous').exists())

    def test_valid_new_build_is_installed_atomically_with_backup(self):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1,
                            "print('new')")
        self.session.get.return_value = _Response(candidate)
        old_mode = stat.S_IMODE(self.script.stat().st_mode)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertTrue(changed)
        self.assertEqual(self.script.read_bytes(), candidate)
        self.assertEqual(self.script.with_name(
            self.script.name + '.previous').read_bytes(), self.current)
        self.assertEqual(stat.S_IMODE(self.script.stat().st_mode), old_mode)

    def test_invalid_candidate_leaves_current_worker_untouched(self):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1,
                            'this is not python !!!')
        self.session.get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    def test_redirect_is_rejected(self):
        response = _Response(b'', status=302)
        response.headers = {'Location': 'https://evil.invalid/worker.py'}
        self.session.get.return_value = response

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)
        self.assertFalse(self.session.get.call_args.kwargs['allow_redirects'])

    def test_oversized_candidate_is_rejected_before_install(self):
        response = _Response(b'x')
        response.headers = {
            'Content-Length': str(atomicdb_worker.WORKER_UPDATE_MAX_BYTES + 1)}
        self.session.get.return_value = response

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    def test_encoded_response_is_rejected(self):
        response = _Response(_source(
            atomicdb_worker.ATOMICDB_WORKER_BUILD + 1))
        response.headers['Content-Encoding'] = 'gzip'
        self.session.get.return_value = response

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)
        self.assertEqual(
            self.session.get.call_args.kwargs['headers']['Accept-Encoding'],
                         'identity')

    def test_network_failure_is_fail_open(self):
        self.session.get.side_effect = atomicdb_worker.requests.ReadTimeout(
            'offline')

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker.time.monotonic')
    def test_trickling_response_has_a_total_deadline(self, monotonic):
        self.session.get.return_value = _Response(_source(
            atomicdb_worker.ATOMICDB_WORKER_BUILD + 1))
        monotonic.side_effect = [
            100,
            100 + atomicdb_worker.WORKER_UPDATE_TOTAL_TIMEOUT_SECONDS + 1,
        ]

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker._atomic_write')
    def test_write_failure_is_fail_open(self, atomic_write):
        self.session.get.return_value = _Response(_source(
            atomicdb_worker.ATOMICDB_WORKER_BUILD + 1))
        atomic_write.side_effect = OSError('read-only')

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    def test_second_process_restarts_from_update_already_on_disk(self):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1)
        self.script.write_bytes(candidate)
        self.session.get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertTrue(changed)
        self.assertEqual(self.script.read_bytes(), candidate)
        self.assertFalse(self.script.with_name(
            self.script.name + '.previous').exists())

    def test_http_origin_is_rejected_without_a_request(self):
        changed = atomicdb_worker._install_worker_update(
            'http://example.invalid', self.script)
        self.assertFalse(changed)
        self.session.get.assert_not_called()

    def test_restart_preserves_all_worker_arguments(self):
        # POSIX replaces the process image with os.execv.
        execv = mock.Mock(side_effect=OSError('test exec failure'))
        installed = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1)
        self.script.write_bytes(installed)

        with mock.patch.object(atomicdb_worker, 'os',
                               _OsAs('posix', execv=execv)), \
                mock.patch.object(atomicdb_worker,
                                  '_spawn_replacement') as spawn, \
                mock.patch.object(atomicdb_worker.sys, 'argv', [
                    str(self.script), '-U', 'alice', '-P', 'secret', '-T',
                    '8']):
            restarted = atomicdb_worker._restart_updated_worker(self.script)

        self.assertFalse(restarted)
        spawn.assert_not_called()
        execv.assert_called_once_with(atomicdb_worker.sys.executable, [
            atomicdb_worker.sys.executable, str(self.script.resolve()), '-U',
            'alice', '-P', 'secret', '-T', '8'])
        self.assertEqual(self.script.read_bytes(), installed)

    def test_windows_restart_starts_the_same_argument_list_itself(self):
        # The Windows C runtime emulates exec by joining the arguments without
        # quoting them, so a space anywhere broke the restart.  The list goes
        # to the replacement process instead, and ``--once`` with it.
        execv = mock.Mock()
        argv = [str(self.script), '-U', 'bob smith', '-P', 'pa ss"wd', '-T',
                '8', '--syzygy', 'C:\\TB dir\\']

        with mock.patch.object(atomicdb_worker, 'os',
                               _OsAs('nt', execv=execv)), \
                mock.patch.object(atomicdb_worker, '_spawn_replacement',
                                  return_value=False) as spawn, \
                mock.patch.object(atomicdb_worker.sys, 'argv', argv):
            restarted = atomicdb_worker._restart_updated_worker(self.script)
            atomicdb_worker._restart_updated_worker(self.script, once=True)

        self.assertFalse(restarted)
        execv.assert_not_called()
        expected = [atomicdb_worker.sys.executable, str(self.script.resolve()),
                    *argv[1:]]
        self.assertEqual(spawn.call_args_list, [
            mock.call(expected, once=False), mock.call(expected, once=True)])

    def test_worker_speaks_the_pinned_update_protocol(self):
        self.assertEqual(atomicdb_worker.ATOMICDB_WORKER_UPDATE_PROTOCOL,
                         UPDATE_PROTOCOL)

    def test_another_update_protocol_is_refused_even_with_a_higher_build(self):
        for protocol in (UPDATE_PROTOCOL - 1, UPDATE_PROTOCOL + 1):
            with self.subTest(protocol=protocol):
                self.session.get.return_value = _Response(_source(
                    atomicdb_worker.ATOMICDB_WORKER_BUILD + 1,
                    protocol=protocol))
                out = io.StringIO()

                with redirect_stdout(out):
                    changed = atomicdb_worker._install_worker_update(
                        'https://example.invalid', self.script)

                self.assertFalse(changed)
                self.assertEqual(self.script.read_bytes(), self.current)
                self.assertIn('unsupported or missing update protocol',
                              out.getvalue())

    def test_same_build_with_different_bytes_does_not_overwrite(self):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD,
                            "print('reused build')")
        self.session.get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    def test_truncated_but_valid_python_is_rejected(self):
        candidate = (
            '#!/usr/bin/env python3\n'
            f'ATOMICDB_WORKER_UPDATE_PROTOCOL = {UPDATE_PROTOCOL}\n'
            f'ATOMICDB_WORKER_BUILD = '
            f'{atomicdb_worker.ATOMICDB_WORKER_BUILD + 1}\n').encode()
        self.session.get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    def test_duplicate_build_assignment_is_rejected(self):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1)
        candidate += b'ATOMICDB_WORKER_BUILD = 1\n'
        self.session.get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    def test_heartbeat_publishes_token_build_and_live_nps(self):
        post = self.session.post
        class StopAfterOne:
            calls = 0

            def wait(self, _seconds):
                self.calls += 1
                return self.calls > 1

        post.return_value.raise_for_status.return_value = None
        now = [100.0]
        current = atomicdb_worker.CurrentTaskState(clock=lambda: now[0])
        current.start(3016, 'opaque-token-3016')
        current.progress(6_230_000, 1.0)

        atomicdb_worker._heartbeat_loop(
            'https://example.invalid', {'machine': 'm1'}, current,
            StopAfterOne())

        post.assert_called_once()
        self.assertEqual(post.call_args.args[0],
                         'https://example.invalid/atomicdb/api/heartbeat')
        self.assertEqual(post.call_args.kwargs['data'], {
            'machine': 'm1',
            'worker_build': atomicdb_worker.ATOMICDB_WORKER_BUILD,
            'task_id': 3016,
            'lease_token': 'opaque-token-3016',
            'nps': 6_230_000,
        })

    def test_current_task_snapshot_is_coherent_during_concurrent_updates(self):
        state = atomicdb_worker.CurrentTaskState(clock=lambda: 100.0)
        expected = {(3016, 'token-a'), (3017, 'token-b')}
        failures = []
        started = threading.Event()

        def writer():
            started.set()
            for _ in range(2_000):
                state.start(3016, 'token-a')
                state.start(3017, 'token-b')

        thread = threading.Thread(target=writer)
        thread.start()
        started.wait()
        while thread.is_alive():
            snapshot = state.snapshot()
            if snapshot and (snapshot['id'], snapshot['lease_token']) not in expected:
                failures.append(snapshot)
                break
        thread.join()

        self.assertEqual(failures, [])
        self.assertIn((state.snapshot()['id'], state.snapshot()['lease_token']),
                      expected)

    def test_stale_engine_progress_is_omitted_from_heartbeat(self):
        post = self.session.post
        class StopAfterOne:
            calls = 0

            def wait(self, _seconds):
                self.calls += 1
                return self.calls > 1

        post.return_value.raise_for_status.return_value = None
        now = [100.0]
        current = atomicdb_worker.CurrentTaskState(clock=lambda: now[0])
        current.start(3016, 'expired-token')
        current.progress(128_000_000, 100.0)
        now[0] += atomicdb_worker.HEARTBEAT_PROGRESS_GRACE_SECONDS + 1

        atomicdb_worker._heartbeat_loop(
            'https://example.invalid', {'machine': 'm1'}, current,
            StopAfterOne())

        payload = post.call_args.kwargs['data']
        self.assertNotIn('task_id', payload)
        self.assertNotIn('lease_token', payload)
        self.assertEqual(payload['nps'], 0)
        self.assertEqual(payload['worker_build'],
                         atomicdb_worker.ATOMICDB_WORKER_BUILD)

    def test_valid_lease_response_uses_the_logical_request_nonce(self):
        post = self.session.post
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'tasks': [{'id': 3016}]}

        tasks = atomicdb_worker._request_tasks(
            'https://example.invalid', {'machine': 'm1'}, 'request-nonce-a')

        self.assertEqual(tasks, [{'id': 3016}])
        self.assertEqual(post.call_args.kwargs['data']['lease_session'],
                         'request-nonce-a')
        self.assertEqual(post.call_args.kwargs['timeout'], (3, 60))

    def test_indeterminate_lease_responses_require_same_nonce_retry(self):
        post = self.session.post
        for status, payload in ((500, {'tasks': []}), (200, None)):
            post.reset_mock()
            post.return_value.status_code = status
            if payload is None:
                post.return_value.json.side_effect = ValueError('truncated')
            else:
                post.return_value.json.side_effect = None
                post.return_value.json.return_value = payload

            with self.assertRaises(atomicdb_worker.LeaseRequestError) as caught:
                atomicdb_worker._request_tasks(
                    'https://example.invalid', {'machine': 'm1'},
                    'request-nonce-a')

            self.assertTrue(caught.exception.ambiguous)
            self.assertEqual(post.call_args.kwargs['data']['lease_session'],
                             'request-nonce-a')

    def test_definitive_lease_rejection_allows_nonce_rotation(self):
        post = self.session.post
        post.return_value.status_code = 403

        with self.assertRaises(atomicdb_worker.LeaseRequestError) as caught:
            atomicdb_worker._request_tasks(
                'https://example.invalid', {'machine': 'm1'},
                'request-nonce-a')

        self.assertFalse(caught.exception.ambiguous)


def _wait_until(predicate, timeout, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class ReplacementProcessTests(SimpleTestCase):
    """``_spawn_replacement``, the Windows restart, with real processes.

    The old worker hands over only once the new one has stayed up for the
    grace period; a replacement that ends sooner leaves it running the build
    it has, as a failed exec does.  The children are plain ``python -c``
    processes, so these run on any host.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / 'folder with spaces'
        self.dir.mkdir()

    def spawn(self, argv, **kwargs):
        exits = []
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            handed = atomicdb_worker._spawn_replacement(
                argv, exit_process=exits.append, **kwargs)
        return handed, exits, out.getvalue()

    def test_hands_over_once_the_new_worker_stays_up(self):
        record = self.dir / 'argv.json'
        code = ('import json, sys, time; '
                'open(sys.argv[1], "w").write(json.dumps(sys.argv[1:])); '
                'time.sleep(3)')
        argv = [sys.executable, '-c', code, str(record), 'arg with spaces',
                'quote"inside', 'C:\\TB dir\\', '', '-P', 'p a s s']

        handed, exits, out = self.spawn(argv, grace=1)

        self.assertTrue(handed)
        self.assertEqual(exits, [0])
        self.assertIn('restarted as process', out)
        # Every argument arrived whole: spaces, quotes, a trailing backslash
        # and an empty one.
        self.assertTrue(_wait_until(record.exists, 10))
        self.assertTrue(_wait_until(lambda: record.read_text().endswith(']'),
                                    10))
        self.assertEqual(json.loads(record.read_text()), argv[3:])

    def test_keeps_this_worker_when_the_new_one_dies(self):
        handed, exits, out = self.spawn(
            [sys.executable, '-c', 'raise SystemExit(3)'], grace=30)

        self.assertFalse(handed)
        self.assertEqual(exits, [])
        self.assertIn('exited with code 3', out)

    def test_a_clean_early_exit_is_a_failed_restart(self):
        # A new build whose job threads all crash still exits 0: that is a
        # dead worker, not a hand-over.
        code = ('import threading\n'
                'def job():\n'
                '    raise NameError("broken build")\n'
                'threads = [threading.Thread(target=job) for _ in range(2)]\n'
                'for t in threads: t.start()\n'
                'for t in threads: t.join()\n')

        handed, exits, out = self.spawn([sys.executable, '-c', code],
                                        grace=30)

        self.assertFalse(handed)
        self.assertEqual(exits, [])
        self.assertIn('exited with code 0', out)

    def test_keeps_this_worker_when_nothing_can_start(self):
        handed, exits, out = self.spawn(
            [str(self.dir / 'no such python'), 'x'], grace=1)

        self.assertFalse(handed)
        self.assertEqual(exits, [])
        self.assertIn('restart failed', out)

    def test_once_a_pass_that_already_finished_cleanly_counts(self):
        handed, exits, _ = self.spawn([sys.executable, '-c', 'pass'],
                                      grace=30, once=True)

        self.assertTrue(handed)
        self.assertEqual(exits, [0])

    def test_once_waits_for_the_new_pass_and_ends_with_its_code(self):
        started = time.monotonic()

        handed, exits, _ = self.spawn(
            [sys.executable, '-c',
             'import time, sys; time.sleep(2.5); sys.exit(7)'],
            grace=1, once=True)

        self.assertTrue(handed)
        self.assertEqual(exits, [7])
        self.assertGreaterEqual(time.monotonic() - started, 2.4)

    def test_a_windows_status_is_passed_on_as_a_c_int(self):
        # 0xC000013A is what a Ctrl+C'd process returns on Windows; os._exit
        # takes a C int and would raise OverflowError on it as it is.
        child = mock.Mock(pid=4242)
        child.wait.side_effect = [
            subprocess.TimeoutExpired('worker', 1), 0xC000013A]

        with mock.patch.object(atomicdb_worker.subprocess, 'Popen',
                               return_value=child):
            handed, exits, _ = self.spawn(['worker'], grace=1, once=True)

        self.assertTrue(handed)
        self.assertEqual(exits, [0xC000013A - 2 ** 32])

    @unittest.skipUnless(os.name == 'nt', 'Windows exit statuses')
    def test_that_c_int_exits_with_the_same_windows_status(self):
        done = subprocess.run([sys.executable, '-c',
                               f'import os; os._exit({0xC000013A - 2 ** 32})'])

        self.assertEqual(done.returncode, 0xC000013A)

    def test_inheritable_stdio(self):
        self.assertEqual(atomicdb_worker._inheritable_stdio(None),
                         subprocess.DEVNULL)
        self.assertEqual(atomicdb_worker._inheritable_stdio(io.StringIO()),
                         subprocess.DEVNULL)
        with open(self.dir / 'log.txt', 'w') as handle:
            self.assertEqual(atomicdb_worker._inheritable_stdio(handle),
                             handle.fileno())
