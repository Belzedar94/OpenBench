import io
import importlib.util
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

    def test_branch_with_slash_uses_the_archive_root(self):
        archive_root = 'OpenBench-agent-atomic-syzygy-datagen-v1'
        payload = client_archive(
            ('%s/Client/client.py' % archive_root, b'new bootstrap'),
            ('%s/Client/worker.py' % archive_root, b'new worker'),
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

    def test_archive_layout_fails_closed(self):
        payload = client_archive(
            ('OpenBench-good/Client/client.py', b'bootstrap'),
            ('unexpected/Client/worker.py', b'worker'),
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            with self.assertRaisesRegex(ValueError, 'unexpected layout'):
                CLIENT.archive_client_root(archive)
