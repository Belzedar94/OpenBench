import stat
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from Client import atomicdb_worker


def _source(build, body='pass'):
    return (
        '#!/usr/bin/env python3\n'
        'ATOMICDB_WORKER_UPDATE_PROTOCOL = 1\n'
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


class WorkerAutoUpdateTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / 'atomicdb_worker.py'
        self.current = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD)
        self.script.write_bytes(self.current)

    def tearDown(self):
        self.temp.cleanup()

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_current_build_is_a_noop(self, get):
        get.return_value = _Response(self.current)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)
        self.assertFalse(self.script.with_name(
            self.script.name + '.previous').exists())

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_valid_new_build_is_installed_atomically_with_backup(self, get):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1,
                            "print('new')")
        get.return_value = _Response(candidate)
        old_mode = stat.S_IMODE(self.script.stat().st_mode)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertTrue(changed)
        self.assertEqual(self.script.read_bytes(), candidate)
        self.assertEqual(self.script.with_name(
            self.script.name + '.previous').read_bytes(), self.current)
        self.assertEqual(stat.S_IMODE(self.script.stat().st_mode), old_mode)

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_invalid_candidate_leaves_current_worker_untouched(self, get):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1,
                            'this is not python !!!')
        get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_redirect_is_rejected(self, get):
        response = _Response(b'', status=302)
        response.headers = {'Location': 'https://evil.invalid/worker.py'}
        get.return_value = response

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_oversized_candidate_is_rejected_before_install(self, get):
        response = _Response(b'x')
        response.headers = {
            'Content-Length': str(atomicdb_worker.WORKER_UPDATE_MAX_BYTES + 1)}
        get.return_value = response

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_encoded_response_is_rejected(self, get):
        response = _Response(_source(
            atomicdb_worker.ATOMICDB_WORKER_BUILD + 1))
        response.headers['Content-Encoding'] = 'gzip'
        get.return_value = response

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)
        self.assertEqual(get.call_args.kwargs['headers']['Accept-Encoding'],
                         'identity')

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_network_failure_is_fail_open(self, get):
        get.side_effect = atomicdb_worker.requests.ReadTimeout('offline')

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker._atomic_write')
    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_write_failure_is_fail_open(self, get, atomic_write):
        get.return_value = _Response(_source(
            atomicdb_worker.ATOMICDB_WORKER_BUILD + 1))
        atomic_write.side_effect = OSError('read-only')

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_second_process_restarts_from_update_already_on_disk(self, get):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1)
        self.script.write_bytes(candidate)
        get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertTrue(changed)
        self.assertEqual(self.script.read_bytes(), candidate)
        self.assertFalse(self.script.with_name(
            self.script.name + '.previous').exists())

    def test_http_origin_is_rejected_without_a_request(self):
        with mock.patch('Client.atomicdb_worker.requests.get') as get:
            changed = atomicdb_worker._install_worker_update(
                'http://example.invalid', self.script)
        self.assertFalse(changed)
        get.assert_not_called()

    @mock.patch('Client.atomicdb_worker.os.execv')
    def test_restart_preserves_all_worker_arguments(self, execv):
        execv.side_effect = OSError('test exec failure')
        installed = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1)
        self.script.write_bytes(installed)

        with mock.patch.object(atomicdb_worker.sys, 'argv', [
                str(self.script), '-U', 'alice', '-P', 'secret', '-T', '8']):
            restarted = atomicdb_worker._restart_updated_worker(self.script)

        self.assertFalse(restarted)
        execv.assert_called_once_with(atomicdb_worker.sys.executable, [
            atomicdb_worker.sys.executable, str(self.script), '-U', 'alice',
            '-P', 'secret', '-T', '8'])
        self.assertEqual(self.script.read_bytes(), installed)

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_same_build_with_different_bytes_does_not_overwrite(self, get):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD,
                            "print('reused build')")
        get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_truncated_but_valid_python_is_rejected(self, get):
        candidate = (
            '#!/usr/bin/env python3\n'
            'ATOMICDB_WORKER_UPDATE_PROTOCOL = 1\n'
            f'ATOMICDB_WORKER_BUILD = '
            f'{atomicdb_worker.ATOMICDB_WORKER_BUILD + 1}\n').encode()
        get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)

    @mock.patch('Client.atomicdb_worker.requests.get')
    def test_duplicate_build_assignment_is_rejected(self, get):
        candidate = _source(atomicdb_worker.ATOMICDB_WORKER_BUILD + 1)
        candidate += b'ATOMICDB_WORKER_BUILD = 1\n'
        get.return_value = _Response(candidate)

        changed = atomicdb_worker._install_worker_update(
            'https://example.invalid', self.script)

        self.assertFalse(changed)
        self.assertEqual(self.script.read_bytes(), self.current)
