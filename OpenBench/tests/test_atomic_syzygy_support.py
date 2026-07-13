import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Client'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')

import django

django.setup()

import worker
import OpenBench.views
from OpenBench import config as openbench_config
from OpenBench.workloads import get_workload
from OpenBench.workloads import verify_workload


class AtomicSyzygyWorkerTests(unittest.TestCase):

    @staticmethod
    def args(**overrides):
        values = {
            'username': 'worker',
            'password': 'password',
            'server': 'http://localhost:8000',
            'threads': '1',
            'nsockets': '1',
            'identity': None,
            'syzygy': None,
            'atomic_syzygy': 'combined',
            'atomic_syzygy_manifest': 'inventory.json',
            'fleet': False,
            'focus': None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_atomic_scanner_requires_atomic_wdl_and_dtz_files(self):
        checked = []

        def exists(path):
            checked.append(path)
            return True

        with mock.patch.object(worker.os.path, 'isfile', side_effect=exists):
            self.assertTrue(
                worker.validate_syzygy_exists(
                    'tables', 3, ('.atbw', '.atbz')
                )
            )

        self.assertTrue(any(path.endswith('.atbw') for path in checked))
        self.assertTrue(any(path.endswith('.atbz') for path in checked))
        self.assertTrue(
            all(path.endswith(('.atbw', '.atbz')) for path in checked)
        )

    def test_atomic_path_and_manifest_are_required_together(self):
        config = worker.Configuration.__new__(worker.Configuration)
        config.process_args(self.args())
        self.assertEqual(config.atomic_syzygy_path, 'combined')
        self.assertEqual(config.atomic_syzygy_manifest, 'inventory.json')

        with self.assertRaisesRegex(ValueError, 'manifest requires'):
            config.process_args(
                self.args(atomic_syzygy=None, atomic_syzygy_manifest='x.json')
            )
        with self.assertRaisesRegex(ValueError, 'requires.*manifest'):
            config.process_args(
                self.args(atomic_syzygy='combined', atomic_syzygy_manifest=None)
            )

    def test_explicit_probe_limits_are_not_overridden(self):
        config = SimpleNamespace(
            syzygy_path=None,
            syzygy_max=0,
            atomic_syzygy_path=r'F:\atomic-tables',
            atomic_syzygy_max=6,
            workload={
                'test': {
                    'type': 'TEST',
                    'syzygy_wdl': '6-MAN',
                    'dev': {
                        'options': 'Threads=1 Hash=32 SyzygyProbeLimit=6',
                        'network': 'None',
                        'private': False,
                        'engine': 'Atomic-Stockfish',
                        'nps': 1000,
                        'time_control': '8.0+0.08',
                        'tablebase_family': 'atomic',
                    },
                    'base': {
                        'options': 'Threads=1 Hash=32 SyzygyProbeLimit=0',
                        'network': 'None',
                        'private': False,
                        'engine': 'Atomic-Stockfish',
                        'nps': 1000,
                        'time_control': '8.0+0.08',
                        'tablebase_family': 'atomic',
                    },
                }
            },
        )

        dev = worker.Cutechess.engine_settings(
            config, 'atomic.exe', 'dev', 1.0, 0
        )
        base = worker.Cutechess.engine_settings(
            config, 'atomic.exe', 'base', 1.0, 0
        )

        self.assertEqual(dev.count('option.SyzygyProbeLimit=6'), 1)
        self.assertNotIn('option.SyzygyProbeLimit=0', dev)
        self.assertEqual(base.count('option.SyzygyProbeLimit=0'), 1)
        self.assertNotIn('option.SyzygyProbeLimit=6', base)
        self.assertIn('option.SyzygyPath=F:\\\\atomic-tables', dev)
        self.assertIn('option.SyzygyPath=F:\\\\atomic-tables', base)

        config.atomic_syzygy_max = 5
        with self.assertRaisesRegex(RuntimeError, 'requires 6-man atomic'):
            worker.Cutechess.engine_settings(
                config, 'atomic.exe', 'dev', 1.0, 0
            )

    def test_implicit_probe_limit_remains_backward_compatible(self):
        self.assertFalse(
            worker.has_uci_option('Threads=1 Hash=32', 'SyzygyProbeLimit')
        )
        self.assertTrue(
            worker.has_uci_option(
                'Threads=1 syzygyprobelimit="5" Hash=32',
                'SyzygyProbeLimit',
            )
        )

    @staticmethod
    def authenticated_fixture(root):
        source = Path(root) / '6-wdl'
        runtime = Path(root) / 'combined'
        source.mkdir()
        runtime.mkdir()
        inventory = []
        for name, payload in (
            ('KPPPPvK.atbw', b'wdl'),
            ('KPPPPvK.atbz', b'dtz'),
        ):
            source_file = source / name
            source_file.write_bytes(payload)
            os.link(source_file, runtime / name)
            inventory.append(
                {'directory': '6-wdl', 'name': name, 'bytes': len(payload)}
            )

        raw = json.dumps(inventory).encode()
        manifest = Path(root) / 'inventory.json'
        manifest.write_bytes(raw)
        marker = {
            'schema': 'atomic-syzygy-acquisition-v1',
            'directory': '6-wdl',
            'files': 2,
            'bytes': 6,
            'source_inventory_sha256': hashlib.sha256(raw).hexdigest(),
            'official_md5_verification': 'pass',
        }
        (source / '.acquisition-complete.json').write_text(json.dumps(marker))
        return source, runtime, manifest, inventory, raw

    def test_inventory_binds_names_sizes_hardlinks_md5_marker_and_sha(self):
        with tempfile.TemporaryDirectory() as root:
            _, runtime, manifest, inventory, raw = self.authenticated_fixture(
                root
            )
            self.assertEqual(
                worker.validate_tablebase_inventory(
                    str(runtime), str(manifest), ('.atbw', '.atbz')
                ),
                hashlib.sha256(raw).hexdigest(),
            )

            inventory[0]['bytes'] = 4
            manifest.write_text(json.dumps(inventory))
            with self.assertRaisesRegex(ValueError, 'byte count mismatch'):
                worker.validate_tablebase_inventory(
                    str(runtime), str(manifest), ('.atbw', '.atbz')
                )

    def test_runtime_copy_cannot_impersonate_authenticated_hardlink(self):
        with tempfile.TemporaryDirectory() as root:
            _, runtime, manifest, _, _ = self.authenticated_fixture(root)
            table = runtime / 'KPPPPvK.atbw'
            table.unlink()
            table.write_bytes(b'wdl')
            with self.assertRaisesRegex(ValueError, 'authenticated hardlink'):
                worker.validate_tablebase_inventory(
                    str(runtime), str(manifest), ('.atbw', '.atbz')
                )

    def test_inventory_rejects_extra_files_and_failed_md5_marker(self):
        with tempfile.TemporaryDirectory() as root:
            source, runtime, manifest, _, _ = self.authenticated_fixture(root)
            (runtime / 'unexpected.atbw').write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
                worker.validate_tablebase_inventory(
                    str(runtime), str(manifest), ('.atbw', '.atbz')
                )

            (runtime / 'unexpected.atbw').unlink()
            marker_path = source / '.acquisition-complete.json'
            marker = json.loads(marker_path.read_text())
            marker['official_md5_verification'] = 'fail'
            marker_path.write_text(json.dumps(marker))
            with self.assertRaisesRegex(ValueError, 'Invalid or stale'):
                worker.validate_tablebase_inventory(
                    str(runtime), str(manifest), ('.atbw', '.atbz')
                )

    def test_worker_advertises_atomic_family_maximum_and_manifest(self):
        captured = {}

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def post(_target, data, timeout):
            captured.update(json.loads(data['system_info']))
            return Response({'machine_id': 12, 'secret': 'secret'})

        config = SimpleNamespace(
            server='http://localhost:8000',
            username='worker',
            password='password',
            compilers={},
            git_tokens={},
            cpu_flags=[],
            cpu_name='cpu',
            os_name='Windows',
            os_ver='10',
            python_ver='3.11',
            mac_address='AABBCC',
            logical_cores=2,
            physical_cores=1,
            ram_total_mb=1024,
            machine_id='None',
            identity='atomic-worker',
            threads=1,
            sockets=1,
            syzygy_max=0,
            atomic_syzygy_max=6,
            atomic_syzygy_manifest_sha256='a' * 64,
            focus=[],
            scan_for_compilers=mock.Mock(),
            scan_for_private_tokens=mock.Mock(),
            scan_for_cpu_flags=mock.Mock(),
            scan_for_machine_id=mock.Mock(),
        )

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch.object(
                    worker.requests, 'get', return_value=Response({})
                ), mock.patch.object(worker.requests, 'post', side_effect=post):
                    worker.server_configure_worker(config)
            finally:
                os.chdir(previous)

        self.assertEqual(captured['tablebases']['standard'], 0)
        self.assertEqual(captured['tablebases']['atomic']['max'], 6)
        self.assertEqual(
            captured['tablebases']['atomic']['manifest_sha256'], 'a' * 64
        )
        self.assertEqual(captured['client_ver'], worker.CLIENT_VERSION)


class AtomicSyzygySchedulingTests(unittest.TestCase):

    def setUp(self):
        self.config = mock.patch.object(
            get_workload,
            'OPENBENCH_CONFIG',
            {
                'engines': {
                    'Atomic-Stockfish': {'tablebase_family': 'atomic'},
                    'Stockfish': {'tablebase_family': 'standard'},
                }
            },
        )
        self.config.start()

    def tearDown(self):
        self.config.stop()

    @staticmethod
    def workload(
            dev='Atomic-Stockfish', base='Atomic-Stockfish',
            wdl='6-MAN', adj='DISABLED'):
        return SimpleNamespace(
            dev_engine=dev,
            base_engine=base,
            syzygy_wdl=wdl,
            syzygy_adj=adj,
        )

    @staticmethod
    def machine(standard=0, atomic=0, legacy=None, manifest=None):
        info = {
            'tablebases': {
                'standard': standard,
                'atomic': {
                    'max': atomic,
                    'manifest_sha256': manifest,
                },
            }
        }
        if legacy is not None:
            info['syzygy_max'] = legacy
        return SimpleNamespace(info=info)

    def test_atomic_jobs_only_reach_atomic_capable_workers(self):
        job = self.workload()
        self.assertTrue(
            get_workload.valid_tablebase_assignment(
                job, self.machine(atomic=6)
            )
        )
        self.assertFalse(
            get_workload.valid_tablebase_assignment(
                job, self.machine(standard=7)
            )
        )

    def test_orthodox_adjudication_is_rejected_for_atomic(self):
        job = self.workload(wdl='6-MAN', adj='5-MAN')
        self.assertFalse(
            get_workload.valid_tablebase_assignment(
                job, self.machine(atomic=6)
            )
        )
        self.assertFalse(
            get_workload.valid_tablebase_assignment(
                job, self.machine(standard=5, atomic=6)
            )
        )

    def test_pinned_inventory_rejects_a_different_atomic_corpus(self):
        get_workload.OPENBENCH_CONFIG['engines']['Atomic-Stockfish'][
            'tablebase_manifest_sha256'
        ] = 'a' * 64
        job = self.workload()
        self.assertFalse(
            get_workload.valid_tablebase_assignment(
                job, self.machine(atomic=6, manifest='b' * 64)
            )
        )
        self.assertTrue(
            get_workload.valid_tablebase_assignment(
                job, self.machine(atomic=6, manifest='A' * 64)
            )
        )

    def test_optional_probing_enforces_pin_only_when_tables_are_present(self):
        get_workload.OPENBENCH_CONFIG['engines']['Atomic-Stockfish'][
            'tablebase_manifest_sha256'
        ] = 'a' * 64
        job = self.workload(wdl='OPTIONAL')
        self.assertFalse(
            get_workload.valid_tablebase_assignment(
                job, self.machine(atomic=6, manifest='b' * 64)
            )
        )
        self.assertTrue(
            get_workload.valid_tablebase_assignment(job, self.machine())
        )

        job.syzygy_wdl = 'DISABLED'
        self.assertTrue(
            get_workload.valid_tablebase_assignment(
                job, self.machine(atomic=6, manifest='b' * 64)
            )
        )

    def test_legacy_standard_capability_is_preserved(self):
        machine = SimpleNamespace(info={'syzygy_max': 6})
        job = self.workload(dev='Stockfish', base='Stockfish')
        self.assertTrue(get_workload.valid_tablebase_assignment(job, machine))


class AtomicSyzygyConfigurationTests(unittest.TestCase):

    def test_client_version_matches_server(self):
        server = json.loads((ROOT / 'Config' / 'config.json').read_text())
        self.assertEqual(worker.CLIENT_VERSION, server['client_version'])

    def test_engine_metadata_accepts_only_known_family_and_sha256_pin(self):
        base = {
            'private': False,
            'nps': 1000,
            'source': 'https://example.test/engine',
            'build': {},
        }
        openbench_config.verify_engine_basics(
            dict(
                base,
                tablebase_family='atomic',
                tablebase_manifest_sha256='a' * 64,
            )
        )
        with self.assertRaises(AssertionError):
            openbench_config.verify_engine_basics(
                dict(base, tablebase_family='unknown')
            )
        with self.assertRaises(AssertionError):
            openbench_config.verify_engine_basics(
                dict(base, tablebase_manifest_sha256='not-a-sha')
            )

    def test_atomic_cutechess_adjudication_is_rejected_at_creation(self):
        request = SimpleNamespace(
            POST={
                'dev_engine': 'Atomic-Stockfish',
                'base_engine': 'Atomic-Stockfish',
                'syzygy_adj': 'OPTIONAL',
            }
        )
        configured = {
            'engines': {
                'Atomic-Stockfish': {'tablebase_family': 'atomic'},
            }
        }
        errors = []
        with mock.patch.object(
            verify_workload.OpenBench.config,
            'OPENBENCH_CONFIG',
            configured,
        ):
            verify_workload.verify_syzygy_field(
                errors, request, 'syzygy_adj', 'Syzygy Adjudication'
            )
        self.assertEqual(len(errors), 1)
        self.assertIn('DISABLED', errors[0])


if __name__ == '__main__':
    unittest.main()
