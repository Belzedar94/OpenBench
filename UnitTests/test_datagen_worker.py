import bz2
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Client'))

import worker


def workload():
    return {
        'result': {'id': 9},
        'test': {
            'id': 7,
            'type': 'DATAGEN',
            'book': {
                'name': 'NONE',
                'sha': None,
                'raw_sha': None,
                'source': None,
            },
            'dev': {
                'engine': 'GenericEngine',
                'name': 'branch',
                'sha': 'a' * 40,
                'source': 'https://example.test/archive.zip',
                'network': '',
                'netname': '',
                'private': False,
                'bench': 1,
                'build': {'path': 'src'},
            },
            'base': {
                'engine': 'GenericEngine',
                'name': 'branch',
            },
            'datagen': {
                'command': (
                    'generate seed {SEED} count {COUNT} threads {THREADS} '
                    'book {BOOK} network {NETWORK} out {OUT}'
                ),
                'total_count': 200,
                'positions_per_chunk': 25,
                'chunk_idx': 3,
                'chunk_count': 25,
                'seed': 103,
                'attempt': 1,
            },
        },
    }


def config():
    return SimpleNamespace(
        workload=workload(),
        threads=2,
        blacklist=[],
        machine_id=11,
        secret_token='secret',
        server='http://localhost:8001',
        compilers={'GenericEngine': ['g++']},
        cpu_name='Generic CPU',
        cpu_flags=[],
    )


class DatagenWorkerTests(unittest.TestCase):

    def test_workload_log_identifies_datagen_chunk_instead_of_match(self):
        cfg = config()
        response = SimpleNamespace(json=lambda: {'workload': workload()})

        with mock.patch.object(worker.requests, 'post', return_value=response), \
             mock.patch('builtins.print') as output:
            worker.server_request_workload(cfg)

        output.assert_any_call(
            'Workload DATAGEN [GenericEngine] branch - chunk 4/8 (test #7)\n'
        )

    def test_template_substitution_is_engine_agnostic(self):
        rendered = worker.render_datagen_command(
            config(),
            os.path.join('Datagen', 'chunk.bin'),
            os.path.join('Networks', '12345678'),
        )
        self.assertEqual(
            rendered,
            'generate seed 103 count 25 threads 2 book NONE '
            'network Networks/12345678 '
            'out Datagen/chunk.bin',
        )

    def test_template_uses_none_when_the_workload_has_no_network(self):
        rendered = worker.render_datagen_command(
            config(), os.path.join('Datagen', 'chunk.bin')
        )
        self.assertIn('network NONE', rendered)

    def test_template_uses_the_exact_raw_book_identity(self):
        cfg = config()
        cfg.workload['test']['book'] = {
            'name': 'atomic.epd',
            'sha': '1' * 64,
            'raw_sha': 'abcdef0123456789' * 4,
            'source': 'https://example.test/atomic.zip',
        }
        cfg.workload['test']['datagen']['command'] = (
            'generate book {BOOK} book_sha256 {BOOK_SHA256} out {OUT} '
            'seed {SEED} count {COUNT} threads {THREADS}'
        )

        rendered = worker.render_datagen_command(
            cfg, os.path.join('Datagen', 'chunk.bin')
        )
        self.assertIn('book Books/atomic.epd', rendered)
        self.assertIn(
            'book_sha256 ' + ('ABCDEF0123456789' * 4), rendered
        )

    def test_opening_book_verifies_normalized_and_raw_identities(self):
        raw = b'fen-one\r\nfen-two\r\n'
        normalized = raw.replace(b'\r\n', b'\n')
        normalized_sha = hashlib.sha256(normalized).hexdigest()
        raw_sha = hashlib.sha256(raw).hexdigest()

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                Path('Books').mkdir()
                book = Path('Books', 'atomic.epd')
                book.write_bytes(raw)
                worker.download_opening_book(
                    normalized_sha, 'unused', book.name, raw_sha
                )
                self.assertEqual(book.read_bytes(), raw)

                with self.assertRaises(worker.OpenBenchCorruptedBookException):
                    worker.download_opening_book(
                        normalized_sha, 'unused', book.name, '0' * 64
                    )
                self.assertFalse(book.exists())
            finally:
                os.chdir(previous)

    def test_cleanup_removes_stale_datagen_sidecar_directories(self):
        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                for directory in ('PGNs', 'Engines', 'Networks', 'Datagen'):
                    Path(directory).mkdir()

                stale_file = Path('Datagen', 'old-chunk.bin')
                stale_file.write_bytes(b'old')
                stale_sidecar = Path('Datagen', 'old-chunk.parts')
                stale_sidecar.mkdir()
                stale_sidecar.joinpath('shard-0').write_bytes(b'old shard')
                fresh_sidecar = Path('Datagen', 'current-chunk.parts')
                fresh_sidecar.mkdir()

                stale_time = time.time() - (2 * 24 * 60 * 60)
                os.utime(stale_file, (stale_time, stale_time))
                os.utime(stale_sidecar, (stale_time, stale_time))

                worker.cleanup_client()

                self.assertFalse(stale_file.exists())
                self.assertFalse(stale_sidecar.exists())
                self.assertTrue(fresh_sidecar.exists())
            finally:
                os.chdir(previous)

    def test_locked_datagen_sidecar_does_not_block_other_cleanup(self):
        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                for directory in ('PGNs', 'Engines', 'Networks', 'Datagen'):
                    Path(directory).mkdir()

                locked_sidecar = Path('Datagen', 'locked.parts')
                locked_sidecar.mkdir()
                removable_file = Path('Datagen', 'other-old-chunk.bin')
                removable_file.write_bytes(b'old')
                stale_time = time.time() - (2 * 24 * 60 * 60)
                os.utime(locked_sidecar, (stale_time, stale_time))
                os.utime(removable_file, (stale_time, stale_time))

                real_rmtree = worker.shutil.rmtree

                def locked_rmtree(path, *args, **kwargs):
                    if os.path.normcase(os.fspath(path)) == os.path.normcase(
                        os.fspath(locked_sidecar)
                    ):
                        raise PermissionError('temporarily locked')
                    return real_rmtree(path, *args, **kwargs)

                with mock.patch.object(
                    worker.shutil, 'rmtree', side_effect=locked_rmtree
                ), mock.patch('builtins.print') as output:
                    worker.cleanup_client()

                self.assertTrue(locked_sidecar.exists())
                self.assertFalse(removable_file.exists())
                self.assertTrue(any(
                    'Cleanup deferred' in str(call)
                    and 'temporarily locked' in str(call)
                    for call in output.call_args_list
                ))
            finally:
                os.chdir(previous)

    def test_complete_workload_builds_one_engine_benches_compresses_and_uploads(self):
        cfg = config()
        cfg.threads = 30
        captured = {}

        def generate(
            _config, engine, output_path, _log_path, _heartbeat, network_path
        ):
            captured['engine'] = engine
            captured['network'] = network_path
            with open(output_path, 'wb') as output:
                output.write(b'opaque training records')

        def upload(_config, path, sha256, byte_count):
            raw = Path(path).read_bytes()
            captured['payload'] = bz2.decompress(raw)
            captured['sha256'] = sha256
            captured['bytes'] = byte_count
            self.assertEqual(hashlib.sha256(raw).hexdigest(), sha256)
            self.assertEqual(len(raw), byte_count)
            return {'completed_chunks': 1, 'total_chunks': 8}

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            os.mkdir('Datagen')
            try:
                with mock.patch.object(worker, 'download_opening_book'), \
                     mock.patch.object(
                         worker,
                         'safe_download_network_weights',
                         return_value=os.path.join('Networks', '12345678'),
                     ), \
                     mock.patch.object(worker, 'safe_download_engine', return_value='engine.exe') as build, \
                     mock.patch.object(worker, 'safe_run_benchmarks', return_value=1000) as bench, \
                     mock.patch.object(worker, 'run_datagen_command', side_effect=generate), \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(worker.ServerReporter, 'report_datagen', side_effect=upload):
                    worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(captured['payload'], b'opaque training records')
        self.assertEqual(captured['engine'], os.path.join('Engines', 'engine.exe'))
        self.assertEqual(captured['network'], os.path.join('Networks', '12345678'))
        build.assert_called_once()
        self.assertEqual(build.call_args.args[1], 'dev')
        bench.assert_called_once()
        self.assertEqual(bench.call_args.args[1], 'dev')
        self.assertEqual(bench.call_args.kwargs, {'bench_threads': 1})

    def test_retry_cleans_orphaned_chunk_files_before_running(self):
        cfg = config()
        stem = 'test_7_chunk_3.bin'
        stale_names = (
            [stem]
            + ['%s.%d' % (stem, idx) for idx in range(24)]
            + [stem + '.meta', stem + '.meta.json', stem + '.debug', stem + '.debug.trace']
        )
        preserved_names = [
            stem + '-backup',
            'test_7_chunk_30.bin.0',
            'test_7_chunk_4.bin.meta',
        ]

        def generate(
            _config, _engine, output_path, _log_path, _heartbeat, _network_path
        ):
            for name in stale_names:
                self.assertFalse(Path('Datagen', name).exists(), name)
            for name in preserved_names:
                self.assertTrue(Path('Datagen', name).exists(), name)
            Path(output_path).write_bytes(b'fresh retry output')

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            os.mkdir('Datagen')
            try:
                for name in stale_names + preserved_names:
                    Path('Datagen', name).write_bytes(b'orphaned attempt data')

                with mock.patch.object(worker, 'download_opening_book'), \
                     mock.patch.object(worker, 'safe_download_network_weights', return_value=None), \
                     mock.patch.object(worker, 'safe_download_engine', return_value='engine.exe'), \
                     mock.patch.object(worker, 'safe_run_benchmarks', return_value=1000), \
                     mock.patch.object(worker, 'run_datagen_command', side_effect=generate), \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(
                         worker.ServerReporter,
                         'report_datagen',
                         return_value={'completed_chunks': 1, 'total_chunks': 8},
                     ) as upload:
                    worker.complete_workload(cfg)

                upload.assert_called_once()
                for name in preserved_names:
                    self.assertTrue(Path('Datagen', name).exists(), name)
            finally:
                os.chdir(previous)

    def test_missing_output_is_a_clean_chunk_failure(self):
        process = SimpleNamespace(
            stdin=SimpleNamespace(
                write=mock.Mock(), flush=mock.Mock(), close=mock.Mock()
            ),
            poll=mock.Mock(return_value=0),
            returncode=0,
        )
        heartbeat = SimpleNamespace(stop_requested=threading.Event())

        with tempfile.TemporaryDirectory() as cwd:
            output = os.path.join(cwd, 'missing.bin')
            log = os.path.join(cwd, 'engine.log')
            with mock.patch.object(worker, 'Popen', return_value=process):
                with self.assertRaisesRegex(RuntimeError, r'without creating \{OUT\}'):
                    worker.run_datagen_command(
                        config(), 'engine.exe', output, log, heartbeat
                    )

    def test_runtime_failure_is_reported_and_blacklists_only_this_workload(self):
        cfg = config()

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            os.mkdir('Datagen')
            try:
                with mock.patch.object(worker, 'download_opening_book'), \
                     mock.patch.object(worker, 'safe_download_network_weights', return_value=None), \
                     mock.patch.object(worker, 'safe_download_engine', return_value='engine.exe'), \
                     mock.patch.object(worker, 'safe_run_benchmarks', return_value=1000), \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(worker, 'run_datagen_command', side_effect=RuntimeError('unknown command')), \
                     mock.patch.object(worker.ServerReporter, 'report_engine_error') as report:
                    with self.assertRaisesRegex(RuntimeError, 'unknown command'):
                        worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(cfg.blacklist, [7])
        report.assert_called_once()
        self.assertIn('chunk 3 failed', report.call_args.args[1])

    def test_transient_setup_failures_requeue_without_blacklisting(self):
        cases = (
            (
                'download_opening_book',
                OSError('temporary book outage'),
            ),
            (
                'safe_download_network_weights',
                OSError('temporary network outage'),
            ),
            (
                'safe_download_engine',
                OSError('temporary source archive outage'),
            ),
            (
                'download_opening_book',
                worker.OpenBenchCorruptedBookException(
                    'truncated book download'
                ),
            ),
            (
                'safe_download_network_weights',
                worker.OpenBenchCorruptedNetworkException(
                    'truncated network download'
                ),
            ),
        )

        for target, error in cases:
            with self.subTest(target=target):
                cfg = config()
                with tempfile.TemporaryDirectory() as cwd:
                    previous = os.getcwd()
                    os.chdir(cwd)
                    os.mkdir('Datagen')
                    try:
                        with mock.patch.object(
                            worker, 'download_opening_book'
                        ) as book, mock.patch.object(
                            worker,
                            'safe_download_network_weights',
                            return_value=None,
                        ) as network, mock.patch.object(
                            worker,
                            'safe_download_engine',
                            return_value='engine.exe',
                        ) as engine, mock.patch.object(
                            worker,
                            'report_datagen_transient_failure',
                            return_value=True,
                        ) as report:
                            targets = {
                                'download_opening_book': book,
                                'safe_download_network_weights': network,
                                'safe_download_engine': engine,
                            }
                            targets[target].side_effect = error

                            with self.assertRaisesRegex(
                                worker.DatagenTransientError, str(error)
                            ):
                                worker.complete_workload(cfg)
                    finally:
                        os.chdir(previous)

                self.assertEqual(cfg.blacklist, [])
                report.assert_called_once()
                self.assertIn(
                    'setup failed before generator launch',
                    report.call_args.args[1],
                )

    def test_deterministic_setup_configuration_failure_stays_blacklisted(self):
        cfg = config()

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            os.mkdir('Datagen')
            try:
                with mock.patch.object(
                    worker, 'download_opening_book'
                ), mock.patch.object(
                    worker,
                    'safe_download_network_weights',
                    return_value=None,
                ), mock.patch.object(
                    worker,
                    'safe_download_engine',
                    side_effect=worker.DatagenConfigurationError(
                        'unsupported DATAGEN artifact configuration'
                    ),
                ), mock.patch.object(
                    worker.ServerReporter, 'report_engine_error'
                ) as report:
                    with self.assertRaisesRegex(
                        worker.DatagenConfigurationError,
                        'unsupported DATAGEN artifact configuration',
                    ):
                        worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(cfg.blacklist, [7])
        report.assert_called_once()

    def test_upload_and_failure_report_errors_remain_retryable(self):
        cfg = config()

        def generate(
            _config, _engine, output_path, _log_path, _heartbeat, _network_path
        ):
            Path(output_path).write_bytes(b'retryable transport payload')

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            os.mkdir('Datagen')
            try:
                with mock.patch.object(worker, 'DATAGEN_TRANSFER_RETRY_DELAY', 0), \
                     mock.patch.object(worker.traceback, 'print_exc'), \
                     mock.patch.object(worker, 'download_opening_book'), \
                     mock.patch.object(worker, 'safe_download_network_weights', return_value=None), \
                     mock.patch.object(worker, 'safe_download_engine', return_value='engine.exe'), \
                     mock.patch.object(worker, 'safe_run_benchmarks', return_value=1000), \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(worker, 'run_datagen_command', side_effect=generate), \
                     mock.patch.object(
                         worker.ServerReporter,
                         'report_datagen',
                         side_effect=RuntimeError('temporary upload outage'),
                     ) as upload, \
                     mock.patch.object(
                         worker.ServerReporter,
                         'report_engine_error',
                         side_effect=RuntimeError('temporary report outage'),
                     ) as report:
                    with self.assertRaisesRegex(
                        worker.DatagenTransientError,
                        'upload failed after 3 attempts',
                    ):
                        worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(upload.call_count, 3)
        self.assertEqual(report.call_count, 3)
        self.assertEqual(cfg.blacklist, [])

    def test_bzip2_failure_requeues_without_blacklisting_workload(self):
        cfg = config()

        def generate(
            _config, _engine, output_path, _log_path, _heartbeat, _network_path
        ):
            Path(output_path).write_bytes(b'retryable compression payload')

        accepted = SimpleNamespace(raise_for_status=lambda: None)
        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            os.mkdir('Datagen')
            try:
                with mock.patch.object(worker, 'DATAGEN_TRANSFER_RETRY_DELAY', 0), \
                     mock.patch.object(worker.traceback, 'print_exc'), \
                     mock.patch.object(worker, 'download_opening_book'), \
                     mock.patch.object(worker, 'safe_download_network_weights', return_value=None), \
                     mock.patch.object(worker, 'safe_download_engine', return_value='engine.exe'), \
                     mock.patch.object(worker, 'safe_run_benchmarks', return_value=1000), \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(worker, 'run_datagen_command', side_effect=generate), \
                     mock.patch.object(
                         worker.bz2, 'open', side_effect=OSError('temporary disk error')
                     ) as compress, \
                     mock.patch.object(
                         worker.ServerReporter,
                         'report_engine_error',
                         return_value=accepted,
                     ) as report:
                    with self.assertRaisesRegex(
                        worker.DatagenTransientError,
                        'bzip2 compression failed after 3 attempts',
                    ):
                        worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(compress.call_count, 3)
        report.assert_called_once()
        self.assertIn('transient failure', report.call_args.args[1])
        self.assertEqual(cfg.blacklist, [])

    def test_locked_partial_archive_remains_a_transient_failure(self):
        heartbeat = SimpleNamespace(stop_requested=threading.Event())

        with tempfile.TemporaryDirectory() as cwd:
            source = os.path.join(cwd, 'chunk.bin')
            target = source + '.bz2'
            Path(source).write_bytes(b'payload')
            Path(target).write_bytes(b'partial')

            real_remove = os.remove

            def locked_remove(path):
                if path == target:
                    raise PermissionError('temporarily locked')
                return real_remove(path)

            with mock.patch.object(worker, 'DATAGEN_TRANSFER_RETRY_DELAY', 0), \
                 mock.patch.object(worker.traceback, 'print_exc'), \
                 mock.patch.object(worker.os, 'remove', side_effect=locked_remove):
                with self.assertRaisesRegex(
                    worker.DatagenTransientError,
                    'partial archive cleanup failed',
                ):
                    worker.compress_datagen_output(source, target, heartbeat)

    def test_reports_automatically_carry_the_chunk_lease(self):
        cfg = config()
        response = SimpleNamespace(json=lambda: {})
        with mock.patch.object(worker.requests, 'post', return_value=response) as post:
            worker.ServerReporter.report(
                cfg, 'clientSubmitError', {'test_id': 7, 'error': 'failed'}
            )
        self.assertEqual(post.call_args.kwargs['data']['chunk_idx'], 3)

    def test_heartbeat_continues_until_server_requests_stop(self):
        calls = []

        def heartbeat(_config):
            calls.append(time.time())
            return SimpleNamespace(json=lambda: {'stop': True} if len(calls) >= 2 else {})

        with mock.patch.object(worker, 'REPORT_INTERVAL', 0.01), \
             mock.patch.object(worker.ServerReporter, 'report_heartbeat', side_effect=heartbeat):
            with worker.DatagenHeartbeat(config()) as monitor:
                self.assertTrue(monitor.stop_requested.wait(0.2))

        self.assertGreaterEqual(len(calls), 2)

    def test_build_parallelism_can_be_capped_without_changing_default(self):
        with mock.patch.dict(os.environ, {'OPENBENCH_BUILD_JOBS': '8'}):
            command = worker.makefile_command(None, '.', 'engine', 'g++')
        self.assertIn('-j8', command)

    def test_datagen_build_has_provenance_target_switch_and_separate_cache(self):
        sha = '0123456789abcdef' * 2 + '01234567'
        command = worker.makefile_command(
            os.path.join('Networks', '12345678'),
            '.',
            'engine',
            'g++',
            sha,
            'datagen',
        )
        self.assertIn('GIT_SHA_FULL=%s' % sha, command)
        self.assertIn('OPENBENCH_DATAGEN=1', command)

        play_command = worker.makefile_command(
            None, '.', 'engine', 'g++', sha
        )
        self.assertIn('GIT_SHA_FULL=%s' % sha, play_command)
        self.assertNotIn('OPENBENCH_DATAGEN=1', play_command)

        play = worker.engine_binary_name(
            'GenericEngine', sha, os.path.join('Networks', '12345678'), False
        )
        datagen = worker.engine_binary_name(
            'GenericEngine',
            sha,
            os.path.join('Networks', '12345678'),
            False,
            'datagen',
        )
        self.assertNotEqual(play, datagen)
        self.assertTrue(datagen.endswith('-DATAGEN'))

        private_play = worker.engine_binary_name(
            'PrivateEngine', sha, os.path.join('Networks', '12345678'), True
        )
        private_datagen = worker.engine_binary_name(
            'PrivateEngine',
            sha,
            os.path.join('Networks', '12345678'),
            True,
            'datagen',
        )
        self.assertNotEqual(private_play, private_datagen)
        self.assertTrue(private_datagen.endswith('-DATAGEN'))

    def test_safe_download_engine_passes_full_sha_and_datagen_role(self):
        cfg = config()
        with mock.patch.object(
            worker, 'download_public_engine', return_value='engine.exe'
        ) as download:
            result = worker.safe_download_engine(
                cfg, 'dev', os.path.join('Networks', '12345678')
            )

        self.assertEqual(result, 'engine.exe')
        self.assertTrue(download.call_args.args[5].endswith('-DATAGEN'))
        self.assertEqual(download.call_args.args[7], 'a' * 40)
        self.assertEqual(download.call_args.args[8], 'datagen')

    def test_datagen_benches_once_while_play_keeps_worker_concurrency(self):
        cfg = config()
        cfg.threads = 30
        with mock.patch.object(
            worker.bench, 'run_benchmark', return_value=(1234, 1)
        ) as run:
            worker.safe_run_benchmarks(
                cfg, 'dev', 'engine.exe', None, bench_threads=1
            )
            self.assertEqual(run.call_args.args[3], 1)

            worker.safe_run_benchmarks(cfg, 'dev', 'engine.exe', None)
            self.assertEqual(run.call_args.args[3], 30)

    def test_private_generic_datagen_is_rejected_before_artifact_selection(self):
        cfg = config()
        cfg.workload['test']['dev']['private'] = True
        with mock.patch.object(
            worker, 'download_private_engine', return_value='engine.exe'
        ) as download:
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError,
                'does not support private engine artifacts',
            ):
                worker.safe_download_engine(
                    cfg, 'dev', os.path.join('Networks', '12345678')
                )

        download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
