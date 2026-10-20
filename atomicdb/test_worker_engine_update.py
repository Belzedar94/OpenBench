"""Engine hot swap in the AtomicDB worker, from build 2026102001 on.

A running worker re-reads the engine manifest, installs a newly published
binary next to the one in use, runs it once (``_probe_engine``) and only then
offers it.  Each job switches its engine between tasks and the solver between
proofs, and every result stays signed with the sha256 of the binary that
produced it.  Only the downloaded reference engine follows the manifest.

No real engine runs here.  Published binaries are byte strings that only get
hashed, the updater's probe is injected, ``Engine`` is a recording fake, and
the probe's own protocol is checked against a small Python stand-in.  The
server is a fake ``requests`` session serving one engines folder.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from django.test import SimpleTestCase

from Client import atomicdb_worker as worker

SERVER = 'https://example.invalid'
PLATFORM = worker._engine_platform_key()
ENGINE_A = b'reference engine A\n' * 4096
ENGINE_B = b'reference engine B\n' * 4096
ENGINE_C = b'reference engine C\n' * 4096
NET = b'nnue weights\n' * 4096
# Bytes that no operating system can start.
GARBAGE = b'\x00\x01 not an executable \x02\x03' * 2048
FENS = [
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1',
    'rnbqkb1r/pppppppp/5n2/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 1 2',
    'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',
    'r1bqkbnr/pppppppp/2n5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 1 2',
    'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',
]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _local(name, data):
    """The name the worker gives a downloaded binary in ``Engines``."""
    return worker._engine_local_name({'file': name, 'sha256': _sha(data)})


def _plays(path, bench=None):
    """Probe stand-in for a binary that runs on this machine."""
    return 'Fake Atomic 1.0'


def _task(task_id):
    return {'id': task_id, 'fen': FENS[task_id % len(FENS)],
            'budget_nodes': 1000, 'multipv': 1,
            'lease_token': f'token-{task_id}'}


class _Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise worker.requests.HTTPError(f'HTTP {self.status_code}')

    def json(self):
        return json.loads(self.content.decode('utf-8'))

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        pass


class _EnginesFolder:
    """``/atomicdb/engines/`` of a fake server, answering as a session."""

    def __init__(self):
        self.files = {}
        self.manifest = None
        self.manifest_status = 200
        self.gets = []

    def publish(self, name, data, sha=None, net=None, net_sha=None, **extra):
        """Serve ``data`` as ``name`` and point this platform's entry at it.

        ``net`` is ``(file name, bytes)`` for a binary that loads its net
        from disk.
        """
        self.files[name] = data
        entry = {'file': name, 'sha256': sha or _sha(data), 'size_mb': 1,
                 **extra}
        self.manifest = {'binaries': {
            PLATFORM: entry,
            'other-os-x86_64': {'file': 'other-os-engine',
                                'sha256': '0' * 64, 'size_mb': 1}}}
        if net is not None:
            net_name, net_data = net
            self.files[net_name] = net_data
            entry['needs_net'] = True
            self.manifest['net'] = {'file': net_name,
                                    'sha256': net_sha or _sha(net_data),
                                    'size_mb': 1}
        return entry

    def get(self, url, **kwargs):
        path = urlparse(url).path
        self.gets.append(path)
        if path == '/atomicdb/engines/manifest.json':
            if self.manifest is None or self.manifest_status != 200:
                return _Response(b'{}', self.manifest_status
                                 if self.manifest_status != 200 else 404)
            return _Response(json.dumps(self.manifest).encode('utf-8'))
        name = path[len('/atomicdb/engines/'):]
        if name in self.files:
            return _Response(self.files[name])
        return _Response(b'not found', 404)

    def downloads(self, name):
        return self.gets.count('/atomicdb/engines/' + name)


class _EngineCase(SimpleTestCase):
    """A fresh worker folder as the cwd, and the fake engines folder."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)
        self.served = _EnginesFolder()
        patcher = mock.patch.object(worker, '_http_session',
                                    lambda: self.served)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = []

    def _log(self, *args, **kwargs):
        self.log.append(' '.join(str(arg) for arg in args))

    def log_text(self):
        return '\n'.join(self.log)

    def provision(self, name='A.exe', data=ENGINE_A):
        """Download the reference engine as main() does at startup."""
        self.served.publish(name, data)
        with redirect_stdout(io.StringIO()):
            info = worker._provision_engine(SERVER)
        return info['path'], {'engine_sha': _sha(data), 'net_sha': ''}

    def updater(self, path, provenance, probe=_plays, **kwargs):
        kwargs.setdefault('interval', 3600)
        return worker.EngineUpdater(SERVER, path, provenance, probe=probe,
                                    log=self._log, **kwargs)

    def check(self, updater):
        with redirect_stdout(io.StringIO()):
            return updater.check_once()


class ManifestEntryTests(SimpleTestCase):
    """What a manifest entry may say before any of it reaches the disk."""

    def test_unsafe_file_names_are_refused(self):
        for name in ('../evil.exe', '..\\evil.exe', 'C:evil.exe',
                     'sub/evil.exe', '.hidden.exe', '', None, 7):
            with self.subTest(name=name):
                with self.assertRaises(worker.EngineUpdateError) as caught:
                    worker._engine_entry({'file': name, 'sha256': 'a' * 64},
                                         'entry')
                self.assertIn('unsafe file name', str(caught.exception))

    def test_a_bad_sha256_is_refused(self):
        for digest in ('a' * 63, 'a' * 65, 'g' * 64, None, 123):
            with self.subTest(digest=digest):
                with self.assertRaises(worker.EngineUpdateError) as caught:
                    worker._engine_entry({'file': 'A.exe', 'sha256': digest},
                                         'entry')
                self.assertIn('bad sha256', str(caught.exception))

    def test_an_entry_must_be_an_object(self):
        with self.assertRaises(worker.EngineUpdateError):
            worker._engine_entry(['A.exe', 'a' * 64], 'entry')

    def test_the_sha256_is_compared_in_lowercase(self):
        entry = worker._engine_entry({'file': 'A.exe', 'sha256': 'AB' * 32},
                                     'entry')
        self.assertEqual(entry['sha256'], 'ab' * 32)

    def test_a_manifest_without_this_platform_is_refused(self):
        with self.assertRaises(worker.EngineUpdateError):
            worker._manifest_engine({}, PLATFORM)
        with self.assertRaises(worker.EngineUpdateError) as caught:
            worker._manifest_engine({'binaries': {}}, PLATFORM)
        self.assertIn('no prebuilt engine', str(caught.exception))

    def test_the_local_name_carries_the_first_12_hex_of_the_sha256(self):
        digest = '0123456789ab' + 'c' * 52
        self.assertEqual(
            worker._engine_local_name(
                {'file': 'Atomic-Stockfish-win-x86_64-1020.exe',
                 'sha256': digest}),
            'Atomic-Stockfish-win-x86_64-1020-0123456789ab.exe')
        self.assertEqual(
            worker._engine_local_name(
                {'file': 'Atomic-Stockfish-linux-x86_64-1020',
                 'sha256': digest}),
            'Atomic-Stockfish-linux-x86_64-1020-0123456789ab')

    def test_the_bench_signature_comes_from_the_entry_or_the_top_level(self):
        read = worker._manifest_bench
        self.assertEqual(read({}, {'bench': {'nnue': 438711}}), 438711)
        self.assertEqual(read({'bench': {'nnue': 438711}}, {}), 438711)
        self.assertEqual(read({'bench': {'nnue': 1}},
                              {'bench': {'nnue': 438711}}), 438711)
        self.assertEqual(read({}, {'bench': 438711}), 438711)
        # No signature means no bench gate at all.
        self.assertIsNone(read({}, {}))
        self.assertIsNone(read({}, {'bench': {'classical': 735506}}))
        self.assertIsNone(read({}, {'bench': None}))

    def test_an_unreadable_bench_signature_is_refused_not_ignored(self):
        for raw in ({'nnue': 'fast'}, {'nnue': 0}, {'nnue': -5},
                    {'nnue': True}, 'fast', 1.5, [438711]):
            with self.subTest(raw=raw):
                with self.assertRaises(worker.EngineUpdateError) as caught:
                    worker._manifest_bench({}, {'bench': raw})
                self.assertIn('unreadable bench signature',
                              str(caught.exception))


class DownloadTests(_EngineCase):
    """``_download_verified``: a file gets its name only once it matches."""

    def download(self, entry, dest):
        with redirect_stdout(io.StringIO()):
            return worker._download_verified(SERVER, entry, dest)

    def parts(self):
        return list(self.home.rglob('*.part'))

    def test_a_download_failing_its_sha256_leaves_nothing_behind(self):
        self.served.files['B.exe'] = ENGINE_B
        dest = self.home / 'B.exe'

        with self.assertRaises(worker.EngineUpdateError) as caught:
            self.download({'file': 'B.exe', 'sha256': _sha(ENGINE_A)}, dest)

        self.assertIn('failed the sha256 check', str(caught.exception))
        self.assertFalse(dest.exists())
        self.assertEqual(self.parts(), [])

    def test_a_matching_local_copy_is_reused_without_a_request(self):
        dest = self.home / 'B.exe'
        dest.write_bytes(ENGINE_B)

        changed = self.download({'file': 'B.exe', 'sha256': _sha(ENGINE_B)},
                                dest)

        self.assertFalse(changed)
        self.assertEqual(self.served.downloads('B.exe'), 0)

    def test_stale_partial_downloads_are_swept(self):
        self.served.files['B.exe'] = ENGINE_B
        dest = self.home / 'B.exe'
        old = Path(str(dest) + '.leftover.part')
        fresh = Path(str(dest) + '.inprogress.part')
        other = self.home / 'Other.exe.leftover.part'
        for part in (old, fresh, other):
            part.write_bytes(b'partial')
        two_hours_ago = time.time() - 7200
        os.utime(old, (two_hours_ago, two_hours_ago))
        os.utime(other, (two_hours_ago, two_hours_ago))
        entry = {'file': 'B.exe', 'sha256': _sha(ENGINE_B)}

        self.assertTrue(self.download(entry, dest))

        self.assertEqual(dest.read_bytes(), ENGINE_B)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())     # may be another worker's
        self.assertTrue(other.exists())     # another file's partial
        # The usual case is a file completed later and only ever reused from
        # then on: the sweep still runs.
        os.utime(fresh, (two_hours_ago, two_hours_ago))
        self.assertFalse(self.download(entry, dest))
        self.assertFalse(fresh.exists())


class ProvisioningTests(_EngineCase):
    """The download at startup, which now uses the same verified path."""

    def test_the_binary_is_stored_under_its_name_and_sha(self):
        self.served.publish('A.exe', ENGINE_A)

        with redirect_stdout(io.StringIO()):
            info = worker._provision_engine(SERVER)
            again = worker.provision_engine(SERVER)

        self.assertEqual(info['path'],
                         str(Path('Engines') / _local('A.exe', ENGINE_A)))
        self.assertIsNone(info['net'])
        self.assertEqual(again, info['path'])          # str contract kept
        self.assertEqual(Path(info['path']).read_bytes(), ENGINE_A)
        self.assertEqual(self.served.downloads('A.exe'), 1)

    def test_a_sha_mismatch_exits_without_leaving_a_file(self):
        self.served.publish('A.exe', ENGINE_A, sha='0' * 64)

        with redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            worker._provision_engine(SERVER)

        self.assertIn('failed the sha256 check', str(caught.exception))
        self.assertEqual(list(Path('Engines').iterdir()), [])

    def test_an_unsafe_name_exits_before_any_download(self):
        self.served.publish('../A.exe', ENGINE_A)

        with redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            worker._provision_engine(SERVER)

        self.assertIn('unsafe file name', str(caught.exception))
        self.assertEqual(self.served.downloads('../A.exe'), 0)

    def test_a_disk_net_engine_returns_the_net_it_loads(self):
        self.served.publish('A.exe', ENGINE_A, net=('nn-test.nnue', NET))

        with redirect_stdout(io.StringIO()):
            info = worker._provision_engine(SERVER)

        self.assertEqual(info['net']['sha256'], _sha(NET))
        self.assertEqual(Path('nn-test.nnue').read_bytes(), NET)


class EngineUpdaterTests(_EngineCase):
    """``EngineUpdater.check_once``: nothing is offered that was not proven."""

    def test_an_unchanged_manifest_is_a_noop(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        before = len(self.served.gets)

        self.assertFalse(self.check(updater))

        self.assertEqual(updater.current(), (0, path, provenance))
        self.assertEqual(self.served.gets[before:],
                         ['/atomicdb/engines/manifest.json'])

    def test_a_new_binary_is_installed_beside_the_running_one_and_offered(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.publish('B.exe', ENGINE_B)

        self.assertTrue(self.check(updater))

        generation, new_path, new_provenance = updater.current()
        self.assertEqual(generation, 1)
        self.assertEqual(new_path,
                         str(Path('Engines') / _local('B.exe', ENGINE_B)))
        # Path and signature travel together.
        self.assertEqual(new_provenance, {'engine_sha': _sha(ENGINE_B),
                                          'net_sha': ''})
        self.assertEqual(Path(new_path).read_bytes(), ENGINE_B)
        # The old binary stays: it is the fallback.
        self.assertEqual(Path(path).read_bytes(), ENGINE_A)
        self.assertIn('engine update:', self.log_text())
        self.assertEqual(list(self.home.rglob('*.part')), [])
        # Going back is just another change, and the verified local copy of
        # A is reused rather than downloaded again.
        self.served.publish('A.exe', ENGINE_A)
        self.assertTrue(self.check(updater))
        self.assertEqual(updater.current(), (2, path, provenance))
        self.assertEqual(self.served.downloads('A.exe'), 1)

    def test_a_sha_mismatch_keeps_the_running_engine_and_is_retried(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.publish('C.exe', ENGINE_C, sha=_sha(ENGINE_B))

        self.assertFalse(self.check(updater))

        self.assertEqual(updater.current(), (0, path, provenance))
        self.assertIn('failed the sha256 check', self.log_text())
        self.assertEqual(list(Path('Engines').glob('C-*')), [])
        # The publisher fixes the manifest: the next check takes it.
        self.served.publish('C.exe', ENGINE_C)
        self.assertTrue(self.check(updater))
        self.assertEqual(updater.current()[2]['engine_sha'], _sha(ENGINE_C))

    def test_a_binary_that_fails_its_probe_is_not_offered(self):
        def no_move(path, bench=None):
            raise worker.EngineUpdateError('no move from the start position')

        path, provenance = self.provision()
        updater = self.updater(path, provenance, probe=no_move)
        self.served.publish('B.exe', ENGINE_B)

        self.assertFalse(self.check(updater))

        self.assertEqual(updater.current(), (0, path, provenance))
        self.assertIn('no move from the start position', self.log_text())

    def test_a_binary_that_cannot_start_is_not_offered(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance, probe=worker._probe_engine)
        self.served.publish('Garbage.exe', GARBAGE)

        self.assertFalse(self.check(updater))

        self.assertEqual(updater.current(), (0, path, provenance))
        self.assertIn('cannot start', self.log_text())

    def test_a_failing_binary_is_retried_ever_less_often(self):
        def broken(path, bench=None):
            raise worker.EngineUpdateError('broken build')

        path, provenance = self.provision()
        updater = self.updater(path, provenance, probe=broken)
        entry = self.served.publish('Broken.exe', ENGINE_B)
        attempts = lambda: self.log_text().count('Broken.exe failed')

        self.assertFalse(self.check(updater))      # first failure
        self.assertFalse(self.check(updater))      # retried at once
        self.assertEqual(attempts(), 2)
        self.assertFalse(self.check(updater))      # now backing off
        self.assertFalse(self.check(updater))
        self.assertEqual(attempts(), 2)
        failures, next_try = updater._failures[entry['sha256']]
        self.assertEqual(failures, 2)
        self.assertGreaterEqual(next_try - time.monotonic(),
                                4 * updater.interval - 5)
        # The delay passes: tried again, and the next delay is longer.
        updater._failures[entry['sha256']] = (failures, 0.0)
        self.assertFalse(self.check(updater))
        self.assertEqual(attempts(), 3)
        self.assertGreaterEqual(
            updater._failures[entry['sha256']][1] - time.monotonic(),
            min(16 * updater.interval,
                worker.ENGINE_UPDATE_MAX_BACKOFF_SECONDS) - 5)
        # Downloaded once, then reused from disk: the backoff spares the
        # server as well.
        self.assertEqual(self.served.downloads('Broken.exe'), 1)
        # A newly published binary is tried at once.
        updater._probe = _plays
        self.served.publish('C.exe', ENGINE_C)
        self.assertTrue(self.check(updater))

    def test_a_long_failure_streak_does_not_overflow_the_backoff(self):
        def broken(path, bench=None):
            raise worker.EngineUpdateError('broken build')

        path, provenance = self.provision()
        updater = self.updater(path, provenance, probe=broken, interval=900.0)
        entry = self.served.publish('Broken.exe', ENGINE_B)
        updater._failures[entry['sha256']] = (600, 0.0)

        self.assertFalse(self.check(updater))

        failures, next_try = updater._failures[entry['sha256']]
        self.assertEqual(failures, 601)
        self.assertLessEqual(next_try - time.monotonic(),
                             worker.ENGINE_UPDATE_MAX_BACKOFF_SECONDS)

    def test_network_and_manifest_failures_keep_the_running_engine(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.manifest_status = 503
        self.assertFalse(self.check(updater))
        self.served.manifest_status = 200
        # The manifest names a file the server does not have.
        self.served.publish('Missing.exe', ENGINE_B)
        del self.served.files['Missing.exe']
        self.assertFalse(self.check(updater))
        # No engine for this platform any more.
        self.served.manifest['binaries'].pop(PLATFORM)
        self.assertFalse(self.check(updater))

        self.assertEqual(updater.current(), (0, path, provenance))
        self.assertEqual(self.log_text().count('check skipped'), 2)
        self.assertIn('HTTP 503', self.log_text())
        self.assertIn('HTTP 404', self.log_text())
        self.assertIn('no prebuilt engine', self.log_text())
        self.assertEqual(list(Path('Engines').glob('Missing*')), [])

    def test_unsafe_names_in_the_manifest_are_refused(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        names = ('../evil.exe', '..\\evil.exe', 'C:evil.exe', '.hidden.exe',
                 '')
        for name in names:
            self.served.publish(name, ENGINE_B)
            self.assertFalse(self.check(updater))

        self.assertEqual(self.log_text().count('unsafe file name'),
                         len(names))
        self.assertEqual(updater.current(), (0, path, provenance))
        self.assertFalse((self.home.parent / 'evil.exe').exists())
        self.assertEqual(self.served.downloads('../evil.exe'), 0)

    def test_an_unreadable_bench_signature_is_refused_before_downloading(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.publish('B.exe', ENGINE_B, bench={'nnue': 'fast'})

        self.assertFalse(self.check(updater))

        self.assertIn("unreadable bench signature in the manifest: 'fast'",
                      self.log_text())
        self.assertEqual(self.served.downloads('B.exe'), 0)

    def test_the_bench_signature_reaches_the_probe(self):
        cases = (
            ('platform entry', {'bench': {'nnue': 438711}}, None, 438711),
            ('top level', {}, {'nnue': 438711}, 438711),
            ('absent', {}, None, None),
            ('classical only', {'bench': {'classical': 735506}}, None, None),
        )
        for label, extra, top, expected in cases:
            with self.subTest(label):
                benches = []

                def probe(binary, bench=None):
                    benches.append(bench)
                    return 'Fake Atomic 1.0'

                path, provenance = self.provision()
                updater = self.updater(path, provenance, probe=probe)
                self.log.clear()
                self.served.publish('B.exe', ENGINE_B, **extra)
                if top is not None:
                    self.served.manifest['bench'] = top

                self.assertTrue(self.check(updater), self.log_text())

                self.assertEqual(benches, [expected])
                if expected is None:
                    self.assertNotIn(', bench ', self.log_text())
                else:
                    self.assertIn(f', bench {expected})', self.log_text())

    def test_a_reused_file_name_gets_its_own_local_file(self):
        # A release that keeps the published name but changes the bytes: the
        # running binary is never touched and the swap still happens.
        path, provenance = self.provision('A.exe', ENGINE_A)
        updater = self.updater(path, provenance)
        self.served.publish('A.exe', ENGINE_B)

        self.assertTrue(self.check(updater), self.log_text())

        new_path = updater.current()[1]
        self.assertEqual(Path(new_path).name, _local('A.exe', ENGINE_B))
        self.assertNotEqual(Path(new_path), Path(path))
        self.assertEqual(Path(path).read_bytes(), ENGINE_A)
        self.assertEqual(Path(new_path).read_bytes(), ENGINE_B)

    def test_the_running_file_is_never_written_over(self):
        # Only possible when the running file no longer matches its own name.
        Path('Engines').mkdir()
        running = Path('Engines') / _local('B.exe', ENGINE_B)
        running.write_bytes(ENGINE_A)
        updater = self.updater(str(running), {'engine_sha': _sha(ENGINE_A),
                                              'net_sha': ''})
        self.served.publish('B.exe', ENGINE_B)

        self.assertFalse(self.check(updater))

        self.assertIn('it is not overwritten in use', self.log_text())
        self.assertEqual(running.read_bytes(), ENGINE_A)
        self.assertEqual(updater.current()[0], 0)

    def test_a_disk_net_engine_brings_its_net_and_names_it(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.publish('B.exe', ENGINE_B, net=('nn-test.nnue', NET))

        self.assertTrue(self.check(updater), self.log_text())

        self.assertEqual(updater.current()[2], {'engine_sha': _sha(ENGINE_B),
                                                'net_sha': _sha(NET)})
        self.assertEqual(Path('nn-test.nnue').read_bytes(), NET)

    def test_a_net_with_a_bad_sha_is_not_installed(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.publish('B.exe', ENGINE_B, net=('nn-test.nnue', NET),
                            net_sha='2' * 64)

        self.assertFalse(self.check(updater))

        self.assertFalse(Path('nn-test.nnue').exists())
        self.assertEqual(updater.current(), (0, path, provenance))

    def test_a_net_of_the_same_name_with_other_content_is_left_alone(self):
        path, provenance = self.provision()
        Path('nn-test.nnue').write_bytes(b'a net some engine may be using')
        updater = self.updater(path, provenance)
        self.served.publish('B.exe', ENGINE_B, net=('nn-test.nnue', NET))

        self.assertFalse(self.check(updater))

        self.assertEqual(Path('nn-test.nnue').read_bytes(),
                         b'a net some engine may be using')
        self.assertIn('exists with other content', self.log_text())
        self.assertEqual(updater.current()[0], 0)

    def test_the_check_loop_runs_on_its_interval_and_stops(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance, interval=0.05)
        self.served.publish('B.exe', ENGINE_B)
        stop = threading.Event()
        thread = threading.Thread(target=updater.run, args=(stop,),
                                  daemon=True)

        with redirect_stdout(io.StringIO()):
            thread.start()
            deadline = time.monotonic() + 30
            while (updater.current()[0] != 1
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            stop.set()
            thread.join(timeout=10)

        self.assertEqual(updater.current()[0], 1)
        self.assertFalse(thread.is_alive())


class TakeBackTests(_EngineCase):
    """``reject``: a newly offered engine that keeps failing goes back."""

    def test_reject_takes_an_offer_back_once_and_blocks_its_sha(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.assertFalse(updater.reject(0, 'test'))     # the startup engine
        self.served.publish('B.exe', ENGINE_B)
        self.assertTrue(self.check(updater))

        self.assertFalse(updater.reject(0, 'test'))     # no longer current
        self.assertTrue(updater.reject(1, 'test'))
        self.assertEqual(updater.current(), (2, path, provenance))
        self.assertFalse(updater.reject(1, 'again'))    # already taken back
        self.assertFalse(updater.reject(2, 'test'))     # a return
        # Still in the manifest, but not offered again...
        self.assertFalse(self.check(updater))
        self.assertEqual(updater.current()[0], 2)
        # ...until another binary is published.
        self.served.publish('C.exe', ENGINE_C)
        self.assertTrue(self.check(updater))
        self.assertEqual(updater.current()[0], 3)
        self.assertTrue(updater.reject(3, 'test'))
        self.assertEqual(updater.current(), (4, path, provenance))

    def test_an_offer_racing_a_take_back_gets_its_own_generation(self):
        path, provenance = self.provision()
        holder = {}

        def probe(binary, bench=None):
            if Path(binary).name.startswith('C-'):
                # A job takes generation 1 back while C is being checked.
                self.assertTrue(holder['updater'].reject(1, 'race'))
            return 'Fake Atomic 1.0'

        updater = holder['updater'] = self.updater(path, provenance,
                                                   probe=probe)
        self.served.publish('B.exe', ENGINE_B)
        self.assertTrue(self.check(updater))            # 1 = B
        self.served.publish('C.exe', ENGINE_C)
        self.assertTrue(self.check(updater))            # 2 = A again, 3 = C

        number, current_path, _ = updater.current()
        self.assertEqual(number, 3)
        self.assertEqual(Path(current_path).name, _local('C.exe', ENGINE_C))
        self.assertEqual(updater._before[3][0], path)


class _Analyses:
    """A recording stand-in for ``Engine``, one instance per test."""

    def __init__(self, failing=(), unstartable=(), on_search=None):
        self.ran = {}                 # fen -> binary that analysed it
        self.started = []
        self.failing = set(failing)
        self.unstartable = set(unstartable)
        self.on_search = on_search

    def engine_class(self):
        analyses = self

        class FakeEngine:
            def __init__(self, path, threads=1, hash_mb=256, syzygy=''):
                analyses.started.append(path)
                if path in analyses.unstartable:
                    raise OSError(f'cannot start {path}')
                self.path = path
                self.ready = True

            def analyse(self, fen, nodes, multipv, searchmoves=None,
                        progress=None):
                if analyses.on_search is not None:
                    analyses.on_search(fen, self.path)
                if self.path in analyses.failing:
                    raise worker.EngineExited('the engine stopped in the '
                                              'middle of a search (exit '
                                              'code 3)')
                analyses.ran[fen] = self.path
                return [{'move': 'e2e4', 'pv': ['e2e4'], 'eval_cp': 10,
                         'mate': None,
                         'raw': 'info depth 1 seldepth 1 multipv 1 score cp '
                                f'10 nodes {nodes} pv e2e4'}]

            def close(self):
                pass

        return FakeEngine


class AnalysisSlotTests(_EngineCase):
    """``_analysis_slot`` with an updater: switches happen between batches,
    and each result names the binary that analysed it."""

    def run_slot(self, engine_path, provenance, engines, batches, analyses):
        stop = threading.Event()
        script = [list(batch) for batch in batches]
        submitted = []

        def request_tasks(server, auth, lease_session):
            if not script:
                stop.set()
                return []
            return script.pop(0)

        def submit(server, payload, task_id):
            submitted.append(payload)
            response = mock.Mock()
            response.json.return_value = {'summary': 'stored'}
            return response

        a = argparse.Namespace(engine=engine_path, hash=16, syzygy='',
                               S=SERVER, no_auto_update=True, once=False)
        out = io.StringIO()
        with mock.patch.object(worker, 'Engine', analyses.engine_class()), \
                mock.patch.object(worker, '_request_tasks', request_tasks), \
                mock.patch.object(worker, '_submit_until_definitive',
                                  submit), \
                mock.patch.object(worker, '_heartbeat_loop',
                                  lambda *args: None), \
                redirect_stdout(out):
            worker._analysis_slot(0, a, None, {'machine': 'slot-test'},
                                  provenance, 1, stop, False, engines)
        return submitted, out.getvalue()

    def assertSignedByWhatRan(self, submitted, analyses):
        for payload in submitted:
            binary = analyses.ran[_task(payload['task_id'])['fen']]
            self.assertEqual(payload['engine_sha'],
                             _sha(Path(binary).read_bytes()),
                             f"task {payload['task_id']} signed by another "
                             'binary')

    def test_a_job_switches_between_batches_and_never_mixes_signatures(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        offered = []

        def publish_during_the_first_search(fen, binary):
            if not offered:
                self.served.publish('B.exe', ENGINE_B)
                offered.append(self.check(updater))

        analyses = _Analyses(on_search=publish_during_the_first_search)
        submitted, out = self.run_slot(
            path, provenance, updater,
            [[_task(0)], [_task(1), _task(2)], [_task(3)]], analyses)

        self.assertEqual(offered, [True])
        self.assertEqual([p['task_id'] for p in submitted], [0, 1, 2, 3])
        self.assertSignedByWhatRan(submitted, analyses)
        # The offer landed during task 0, which finished on A; the rest ran
        # on B.
        self.assertEqual(submitted[0]['engine_sha'], _sha(ENGINE_A))
        for payload in submitted[1:]:
            self.assertEqual(payload['engine_sha'], _sha(ENGINE_B))
        self.assertEqual(out.count('[0] engine switched to'), 1)

    def test_a_failed_switch_stays_on_the_old_engine_until_the_retry(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.publish('B.exe', ENGINE_B)
        self.assertTrue(self.check(updater))
        new_path = updater.current()[1]
        analyses = _Analyses(unstartable={new_path})

        submitted, out = self.run_slot(
            path, provenance, updater,
            [[_task(0)], [_task(1)], [_task(2)]], analyses)

        self.assertEqual([p['task_id'] for p in submitted], [0, 1, 2])
        for payload in submitted:
            self.assertEqual(payload['engine_sha'], _sha(ENGINE_A))
        self.assertSignedByWhatRan(submitted, analyses)
        # Tried once, then left alone until ENGINE_SWITCH_RETRY_SECONDS.
        self.assertEqual(out.count('engine switch to'), 1)
        self.assertEqual(analyses.started.count(new_path), 1)

    def test_a_hot_swapped_engine_that_keeps_failing_is_taken_back(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        self.served.publish('B.exe', ENGINE_B)
        self.assertTrue(self.check(updater))
        analyses = _Analyses(failing={updater.current()[1]})

        submitted, out = self.run_slot(
            path, provenance, updater,
            [[_task(task_id)] for task_id in range(5)], analyses)

        # The three searches cut short were never submitted; after the third
        # the job was back on A.
        self.assertEqual([p['task_id'] for p in submitted], [3, 4])
        for payload in submitted:
            self.assertEqual(payload['engine_sha'], _sha(ENGINE_A))
        self.assertSignedByWhatRan(submitted, analyses)
        self.assertEqual(updater.current(), (2, path, provenance))
        self.assertIn(_sha(ENGINE_B), updater._rejected)
        self.assertIn('taken back after 3 failed searches in a row',
                      self.log_text())
        self.assertEqual(out.count('restarting the engine'), 2)

    def test_the_engine_the_worker_started_with_is_never_taken_back(self):
        path, provenance = self.provision()
        updater = self.updater(path, provenance)
        analyses = _Analyses(failing={path})

        submitted, out = self.run_slot(
            path, provenance, updater,
            [[_task(task_id)] for task_id in range(5)], analyses)

        self.assertEqual(submitted, [])
        self.assertEqual(updater.current(), (0, path, provenance))
        self.assertNotIn('taken back', self.log_text())
        self.assertEqual(out.count('restarting the engine'), 5)

    def test_a_search_cut_short_is_not_submitted(self):
        path, provenance = self.provision()

        def kill_the_first_search(fen, binary):
            if fen == _task(0)['fen']:
                raise worker.EngineExited('the engine stopped in the middle '
                                          'of a search (exit code 1)')

        analyses = _Analyses(on_search=kill_the_first_search)
        submitted, out = self.run_slot(
            path, provenance, None, [[_task(0)], [_task(1)]], analyses)

        self.assertEqual([p['task_id'] for p in submitted], [1])
        self.assertIn('engine failure: the engine stopped in the middle of '
                      'a search', out)
        self.assertIn('restarting the engine', out)


class EngineSwitchTests(SimpleTestCase):
    """``_switch_engine``: a job never ends up without an engine."""

    def test_a_new_engine_that_is_not_ready_falls_back_to_the_old_one(self):
        closed = []

        class Engine:
            def __init__(self, path, **kwargs):
                self.path = path
                self.ready = path != 'new'

            def close(self):
                closed.append(self.path)

        a = argparse.Namespace(hash=16, syzygy='')
        with mock.patch.object(worker, 'Engine', Engine), \
                redirect_stdout(io.StringIO()) as out:
            engine, switched = worker._switch_engine(0, Engine('old'), 'old',
                                                     'new', 1, a)

        self.assertFalse(switched)
        self.assertEqual(engine.path, 'old')
        # One hash table at a time: the old process goes first.
        self.assertEqual(closed, ['old', 'new'])
        self.assertIn('did not answer isready', out.getvalue())

    def test_the_old_binary_is_retried_when_it_does_not_come_back_at_once(self):
        started = []

        class FlakyEngine:
            def __init__(self, path, **kwargs):
                started.append(path)
                if path == 'new' or started.count('old') == 1:
                    raise OSError('busy for a moment')
                self.ready = True

            def close(self):
                pass

        a = argparse.Namespace(hash=16, syzygy='')
        with mock.patch.object(worker, 'Engine', FlakyEngine), \
                mock.patch.object(worker.time, 'sleep') as sleep, \
                redirect_stdout(io.StringIO()):
            engine, switched = worker._switch_engine(0, mock.Mock(), 'old',
                                                     'new', 1, a)

        self.assertFalse(switched)
        self.assertIsInstance(engine, FlakyEngine)
        self.assertEqual(started, ['new', 'old', 'old'])
        sleep.assert_called_once_with(5)


class EngineProcessTests(SimpleTestCase):
    """``Engine`` tells a dead process apart from a finished search."""

    def process(self, output, code=3):
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(output)
        process.wait.return_value = code
        return process

    def engine(self, output):
        engine = worker.Engine.__new__(worker.Engine)
        engine.p = self.process(output)
        return engine

    def test_ready_says_whether_the_engine_answered_isready(self):
        for output, ready in (('id name Fake\nuciok\nreadyok\n', True),
                              ('id name Fake\nuciok\n', False)):
            with self.subTest(ready=ready), \
                    mock.patch.object(worker.subprocess, 'Popen',
                                      return_value=self.process(output)):
                self.assertIs(worker.Engine('engine').ready, ready)

    def test_a_search_cut_short_raises_instead_of_returning_lines(self):
        engine = self.engine('info depth 1 seldepth 1 multipv 1 score cp 12 '
                             'nodes 100 pv e2e4\n')

        with self.assertRaises(worker.EngineExited) as caught:
            engine.analyse(FENS[0], 1000, 1)

        self.assertIn('exit code 3', str(caught.exception))

    def test_a_finished_search_still_returns_its_lines(self):
        engine = self.engine('info depth 1 seldepth 1 multipv 1 score cp 12 '
                             'nodes 100 pv e2e4\nbestmove e2e4\n')

        lines = engine.analyse(FENS[0], 1000, 1)

        self.assertEqual([(line['move'], line['eval_cp']) for line in lines],
                         [('e2e4', 12)])


# Answers like an engine would, according to BEHAVIOUR.
_STAND_IN = r'''
import sys

for line in sys.stdin:
    words = line.split()
    if not words:
        continue
    if words[0] == 'quit':
        break
    if BEHAVIOUR == 'silent':
        continue
    if words[0] == 'uci':
        if BEHAVIOUR == 'dies':
            sys.exit(3)
        print('id name Fake Atomic 1.0')
        print('uciok', flush=True)
    elif words[0] == 'isready':
        if BEHAVIOUR == 'error-line':
            print('info string ERROR: network nn-test.nnue was not loaded')
        print('readyok', flush=True)
    elif words[0] == 'go':
        none = BEHAVIOUR == 'no-move'
        print('bestmove (none)' if none else 'bestmove e2e4', flush=True)
    elif words[0] == 'bench':
        if BEHAVIOUR == 'no-bench':
            print("Unknown command: 'bench'", flush=True)
        else:
            print('Nodes searched  : 438711', flush=True)
'''


class EngineProbeTests(SimpleTestCase):
    """``_probe_engine``, run against a Python stand-in for a binary.

    The probe starts a binary by its path; the stand-in is started through
    this interpreter instead, so the dialogue is the real one on any host.
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            return real_popen([sys.executable, *argv], *args, **kwargs)

        patcher = mock.patch.object(worker.subprocess, 'Popen', popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stand_in(self, behaviour):
        script = self.dir / f'engine {behaviour}.py'
        script.write_text(f'BEHAVIOUR = {behaviour!r}\n' + _STAND_IN,
                          encoding='utf-8')
        return str(script)

    def refusal(self, behaviour, **kwargs):
        with self.assertRaises(worker.EngineUpdateError) as caught:
            worker._probe_engine(self.stand_in(behaviour), **kwargs)
        return str(caught.exception)

    def test_an_engine_that_plays_passes_and_names_itself(self):
        self.assertEqual(worker._probe_engine(self.stand_in('plays')),
                         'Fake Atomic 1.0')

    def test_no_move_from_the_start_position_is_refused(self):
        self.assertIn('no move from the start position',
                      self.refusal('no-move'))

    def test_an_engine_error_line_is_refused_even_if_it_then_plays(self):
        # A net it cannot find, for instance: the line alone is the refusal.
        self.assertTrue(self.refusal('error-line').startswith(
            'info string ERROR: network nn-test.nnue was not loaded'))

    def test_a_binary_that_exits_is_refused(self):
        self.assertIn("exited before 'uciok'", self.refusal('dies'))

    def test_a_silent_binary_is_given_up_after_the_timeout(self):
        started = time.monotonic()

        message = self.refusal('silent', timeout=1)

        self.assertIn("no 'uciok' within 1s", message)
        # quit plus end of input lets it go: no wait for the kill.
        self.assertLess(time.monotonic() - started, 8)

    def test_the_bench_signature_must_match_exactly(self):
        self.assertEqual(
            worker._probe_engine(self.stand_in('plays'), bench=438711),
            'Fake Atomic 1.0')
        self.assertIn('bench signature 438711 does not match the manifest '
                      '(438712)',
                      self.refusal('plays', bench=438712))

    def test_a_binary_without_bench_cannot_pass_a_gated_probe(self):
        self.assertIn('within 2s',
                      self.refusal('no-bench', timeout=2, bench=438711))


class _Offers:
    """The ``current()`` side of an updater, for the solver loop."""

    def __init__(self, path, sha):
        self.value = (0, path, {'engine_sha': sha, 'net_sha': ''})

    def current(self):
        return self.value

    def offer(self, path, sha):
        self.value = (self.value[0] + 1, path,
                      {'engine_sha': sha, 'net_sha': ''})


class _FastStop:
    """A stop event whose waits do not wait, for loops with 60 s pauses."""

    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        time.sleep(0.005)
        return self._set


class SolverLoopTests(SimpleTestCase):
    """The solver takes a new engine between proofs, signed as it ran."""

    def run_loop(self, solver, stop, solve_once, engines=None):
        out = io.StringIO()
        with mock.patch.object(worker, '_solve_once', solve_once), \
                mock.patch.object(worker, '_solve_heartbeat_loop',
                                  lambda *args: None), \
                redirect_stdout(out):
            worker._solve_loop(SERVER, {}, solver, stop, engines)
        return out.getvalue()

    def test_the_solver_switches_between_proofs_with_its_signature(self):
        offers = _Offers('A.exe', 'a' * 64)
        solver = worker.Solver('A.exe', build_sha='a' * 64)
        calls = []
        stop = threading.Event()

        def solve_once(server, auth, solver_, lease_session,
                       current_task=None):
            calls.append((solver_.path, solver_.build_sha))
            if len(calls) == 2:
                offers.offer('B.exe', 'b' * 64)
            if len(calls) == 4:
                stop.set()
            return True

        out = self.run_loop(solver, stop, solve_once, offers)

        self.assertEqual(calls, [('A.exe', 'a' * 64), ('A.exe', 'a' * 64),
                                 ('B.exe', 'b' * 64), ('B.exe', 'b' * 64)])
        self.assertIn('solve: engine switched to B.exe', out)

    def test_the_solver_pauses_on_a_binary_without_solve_and_resumes(self):
        offers = _Offers('A.exe', 'a' * 64)
        solver = worker.Solver('A.exe', build_sha='a' * 64)
        calls = []
        stop = _FastStop()

        def publish_soon():
            time.sleep(0.3)
            offers.offer('B.exe', 'b' * 64)

        def solve_once(server, auth, solver_, lease_session,
                       current_task=None):
            calls.append((solver_.path, solver_.build_sha))
            if solver_.path == 'A.exe':
                if len(calls) == worker.SOLVER_ENGINE_FAILURE_LIMIT:
                    threading.Thread(target=publish_soon, daemon=True).start()
                raise worker.SolverEngineFailure('Unknown command: solve')
            stop.set()
            return True

        out = self.run_loop(solver, stop, solve_once, offers)

        limit = worker.SOLVER_ENGINE_FAILURE_LIMIT
        self.assertEqual(calls, [('A.exe', 'a' * 64)] * limit
                         + [('B.exe', 'b' * 64)])
        self.assertIn('solve PAUSED', out)
        self.assertNotIn('DISABLED', out)

    def test_a_new_binary_that_cannot_solve_hands_the_proofs_back(self):
        # A proved; B (published next) has no solver; the proofs go back to
        # A, signed as A, until C is published.
        offers = _Offers('A.exe', 'a' * 64)
        solver = worker.Solver('A.exe', build_sha='a' * 64)
        calls = []
        stop = _FastStop()

        def solve_once(server, auth, solver_, lease_session,
                       current_task=None):
            calls.append((solver_.path, solver_.build_sha))
            if solver_.path == 'B.exe':
                raise worker.SolverEngineFailure('Unknown command: solve')
            if len(calls) == 1:
                offers.offer('B.exe', 'b' * 64)
            elif solver_.path == 'A.exe':
                offers.offer('C.exe', 'c' * 64)
            elif solver_.path == 'C.exe':
                stop.set()
            return True

        out = self.run_loop(solver, stop, solve_once, offers)

        limit = worker.SOLVER_ENGINE_FAILURE_LIMIT
        self.assertEqual(calls, [('A.exe', 'a' * 64)]
                         + [('B.exe', 'b' * 64)] * limit
                         + [('A.exe', 'a' * 64), ('C.exe', 'c' * 64)])
        self.assertIn('B.exe cannot solve; proofs go back to A.exe', out)
        self.assertNotIn('PAUSED', out)

    def test_without_engine_updates_the_solver_still_retires(self):
        solver = worker.Solver('A.exe', build_sha='a' * 64)
        calls = []

        def solve_once(*args, **kwargs):
            calls.append(1)
            raise worker.SolverEngineFailure('Unknown command: solve')

        out = self.run_loop(solver, _FastStop(), solve_once)

        self.assertEqual(len(calls), worker.SOLVER_ENGINE_FAILURE_LIMIT)
        self.assertIn('solve DISABLED', out)


class MainWiringTests(_EngineCase):
    """Only the downloaded reference engine follows the manifest."""

    def run_main(self, *flags, net=None):
        engine = self.home / 'reference-engine'
        engine.write_bytes(ENGINE_A)
        provisioned = {'path': str(engine), 'net': net}
        argv = ['atomicdb_worker.py', '-U', 'user', '-P', 'pass', '-S',
                SERVER, '--no-auto-update', '--no-fetch-syzygy', *flags]
        with mock.patch.object(worker.sys, 'argv', argv), \
                mock.patch.object(worker, '_provision_engine',
                                  return_value=provisioned) as provision, \
                mock.patch.object(worker, 'EngineUpdater') as updater, \
                mock.patch.object(worker, '_analysis_slot') as slot, \
                mock.patch.object(worker, '_solve_loop') as solve_loop, \
                redirect_stdout(io.StringIO()):
            updater.return_value.interval = 900.0
            worker.main()
        return engine, provision, updater, slot, solve_loop

    def test_the_downloaded_engine_gets_an_updater_shared_by_every_job(self):
        engine, provision, updater, slot, solve_loop = self.run_main(
            '--solve')

        provision.assert_called_once_with(SERVER)
        updater.assert_called_once_with(
            SERVER, str(engine),
            {'engine_sha': _sha(ENGINE_A), 'net_sha': ''}, interval=900.0)
        self.assertIs(slot.call_args.args[8], updater.return_value)
        self.assertIs(solve_loop.call_args.args[4], updater.return_value)

    def test_an_engine_given_with_engine_is_left_alone(self):
        own = self.home / 'my-own-build'
        own.write_bytes(ENGINE_B)

        _, provision, updater, slot, _ = self.run_main('--engine', str(own))

        provision.assert_not_called()
        updater.assert_not_called()
        self.assertIsNone(slot.call_args.args[8])

    def test_no_engine_update_turns_it_off(self):
        _, _, updater, slot, _ = self.run_main('--no-engine-update')

        updater.assert_not_called()
        self.assertIsNone(slot.call_args.args[8])

    def test_a_single_pass_does_not_follow_the_manifest(self):
        _, _, updater, slot, _ = self.run_main('--once')

        updater.assert_not_called()
        slot.assert_called_once()

    def test_the_check_interval_is_kept_between_a_minute_and_a_day(self):
        for minutes, interval in (('0.1', 60), ('30', 1800), ('100000',
                                                             86400)):
            with self.subTest(minutes=minutes):
                _, _, updater, _, _ = self.run_main(
                    '--engine-update-minutes', minutes)
                self.assertEqual(updater.call_args.kwargs['interval'],
                                 interval)

    def test_a_downloaded_disk_net_is_named_in_the_provenance(self):
        _, _, updater, slot, _ = self.run_main(
            net={'file': 'nn-test.nnue', 'sha256': 'AB' * 32})

        self.assertEqual(updater.call_args.args[2]['net_sha'], 'ab' * 32)
        self.assertEqual(slot.call_args.args[4]['net_sha'], 'ab' * 32)
