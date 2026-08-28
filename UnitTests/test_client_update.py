import io
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'openbench_bootstrap_client', ROOT / 'Client' / 'client.py'
)
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


def client_archive(*members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data in members:
            archive.writestr(name, data)
    return output.getvalue()


class ClientUpdateTests(unittest.TestCase):

    def test_production_ref_is_compatible_with_legacy_bootstrap(self):
        config = json.loads((ROOT / 'Config' / 'config.json').read_text())
        repo_ref = config['client_repo_ref']
        self.assertRegex(repo_ref, r'^[0-9a-f]{40}$')
        archive_root = 'OpenBench-%s' % repo_ref
        payload = client_archive(
            ('%s/Client/client.py' % archive_root, b'bootstrap'),
            ('%s/Client/worker.py' % archive_root, b'worker'),
        )
        with tempfile.TemporaryDirectory() as target:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                archive.extractall(target)
            legacy_client_dir = Path(target, 'OpenBench-%s' % repo_ref, 'Client')
            self.assertTrue(legacy_client_dir.is_dir())
            self.assertTrue(Path(legacy_client_dir, 'worker.py').is_file())

    def test_branch_with_slash_uses_the_archive_root(self):
        archive_root = 'OpenBench-agent-atomic-syzygy-datagen-v1'
        payload = client_archive(
            ('%s/Client/client.py' % archive_root, b'new bootstrap'),
            ('%s/Client/worker.py' % archive_root, b'new worker'),
            (
                '%s/Client/referees/contract/windows/referee.exe'
                % archive_root,
                b'nested referee',
            ),
        )
        version = {
            'client_repo_url': 'https://github.com/Belzedar94/OpenBench',
            'client_repo_ref': 'agent/atomic-syzygy-datagen-v1',
        }
        args = SimpleNamespace(
            username='worker', password='secret', server='https://example.test'
        )
        with tempfile.TemporaryDirectory() as target:
            bootstrap = Path(target, 'client.py')
            bootstrap.write_bytes(b'old bootstrap')
            with (
                mock.patch.object(
                    CLIENT.requests, 'post', return_value=mock.Mock(
                        json=mock.Mock(return_value=version)
                    )
                ),
                mock.patch.object(
                    CLIENT.requests, 'get', return_value=mock.Mock(
                        status_code=200, content=payload
                    )
                ),
                mock.patch.object(CLIENT.os, 'getcwd', return_value=target),
            ):
                CLIENT.download_client_files(args)

            self.assertEqual(bootstrap.read_bytes(), b'old bootstrap')
            self.assertEqual(Path(target, 'worker.py').read_bytes(), b'new worker')
            self.assertEqual(
                Path(
                    target,
                    'referees',
                    'contract',
                    'windows',
                    'referee.exe',
                ).read_bytes(),
                b'nested referee',
            )
            self.assertFalse(Path(target, 'referee.exe').exists())

    def test_archive_layout_fails_closed(self):
        payload = client_archive(
            ('OpenBench-good/Client/client.py', b'bootstrap'),
            ('unexpected/Client/worker.py', b'worker'),
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            with self.assertRaisesRegex(ValueError, 'unexpected layout'):
                CLIENT.archive_client_root(archive)
