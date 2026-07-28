"""The worker fetching its own tablebases: manifest, verification, and the
several ways a mirror can let you down without being allowed to stop the fleet.

WHY EVERY WORKER AND NOT JUST THE CLOSURES.  Analysis blind to a tablebase
re-searches endgames that were solved decades ago, and it does it on every
visit.  The 3-4-5 atomic set is 1.3 GB: worth asking a contributor to store
once, never worth asking them to fetch by hand.

The manifests in here are built by LOOKING AT REAL FILES -- written to a temp
directory, then measured and hashed -- rather than typed out as dicts.  That
is the lesson from the F0 classifier, which was written against invented
telemetry keys, passed every test, and would never have fired in production:
a fixture you invent tests your invention.  The one thing spelled out by hand
is the published vocabulary itself, which is pinned deliberately, because
getting `name`/`bytes`/`sha256` wrong is exactly the failure this suite
exists to catch -- the engine manifest next door uses `file`/`size_mb`.
"""

import hashlib
import importlib.util
import pathlib
import tempfile

from django.test import SimpleTestCase

_SPEC = importlib.util.spec_from_file_location(
    'atomicdb_worker_syzygy_under_test',
    pathlib.Path(__file__).resolve().parent.parent / 'Client'
    / 'atomicdb_worker.py')
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)

SERVER = 'https://mirror.example'


def manifest_from_directory(directory, set_name=None):
    """Describe what is actually on disk, in the published format."""
    files = []
    for path in sorted(pathlib.Path(directory).iterdir()):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        files.append({'name': path.name, 'bytes': len(blob),
                      'sha256': hashlib.sha256(blob).hexdigest()})
    return {'set': set_name or worker.SYZYGY_SET, 'files': files}


class _Response:
    def __init__(self, body=None, payload=None, status=200):
        self._body = body or b''
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload

    def iter_content(self, chunk_size=1):
        for at in range(0, len(self._body), chunk_size):
            yield self._body[at:at + chunk_size]


class Mirror:
    """The distribution point, stood in for at the requests.get seam."""

    def __init__(self, source, manifest=None, fail=(), manifest_status=200,
                 overlong=()):
        self.source = pathlib.Path(source)
        self.manifest = manifest if manifest is not None else \
            manifest_from_directory(source)
        self.fail = set(fail)
        self.overlong = set(overlong)
        self.manifest_status = manifest_status
        self.hits = []

    def __call__(self, url, **kwargs):
        self.hits.append(url)
        name = url.rsplit('/', 1)[-1]
        if name == 'manifest.json':
            if self.manifest_status >= 400:
                return _Response(status=self.manifest_status)
            return _Response(payload=self.manifest)
        if name in self.fail:
            raise RuntimeError('connection reset')
        body = (self.source / name).read_bytes()
        if name in self.overlong:
            body += b'surprise'
        return _Response(body=body)

    def file_hits(self):
        return [u for u in self.hits if not u.endswith('manifest.json')]


class _Args:
    def __init__(self, syzygy='', no_fetch_syzygy=False):
        self.syzygy = syzygy
        self.no_fetch_syzygy = no_fetch_syzygy


def _quiet(*args, **kwargs):
    pass


class ManifestVocabularyTests(SimpleTestCase):
    """The published format, pinned. Getting this wrong is silent and total."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.source = pathlib.Path(self._temp.name)
        (self.source / 'KQvK.atbw').write_bytes(b'tablebase-one' * 100)
        (self.source / 'KRvK.atbz').write_bytes(b'tablebase-two' * 50)
        self.addCleanup(self._temp.cleanup)

    def test_a_manifest_describing_real_files_parses(self):
        entries = worker.parse_syzygy_manifest(
            manifest_from_directory(self.source))
        self.assertEqual([e['name'] for e in entries],
                         ['KQvK.atbw', 'KRvK.atbz'])
        for entry in entries:
            actual = (self.source / entry['name']).read_bytes()
            self.assertEqual(entry['bytes'], len(actual))
            self.assertEqual(entry['sha256'],
                             hashlib.sha256(actual).hexdigest())

    def test_the_engine_manifest_vocabulary_is_not_accepted(self):
        """`file`/`size_mb` is the OTHER manifest in this worker."""
        wrong = {'set': worker.SYZYGY_SET,
                 'files': [{'file': 'KQvK.atbw', 'size_mb': 1,
                            'sha256': '0' * 64}]}
        with self.assertRaises(worker.SyzygyManifestError):
            worker.parse_syzygy_manifest(wrong)

    def test_a_manifest_for_another_set_is_refused(self):
        payload = manifest_from_directory(self.source, set_name='atomic-6men')
        with self.assertRaises(worker.SyzygyManifestError):
            worker.parse_syzygy_manifest(payload)

    def test_a_name_that_is_a_path_is_refused(self):
        """A manifest is a remote instruction to write to a stranger's disk."""
        for name in ('../evil', 'a/b', 'a\\b', '..', '.', '/etc/passwd',
                     'C:evil', '.hidden'):
            payload = {'set': worker.SYZYGY_SET,
                       'files': [{'name': name, 'bytes': 1,
                                  'sha256': '0' * 64}]}
            with self.assertRaises(worker.SyzygyManifestError,
                                   msg=f'{name!r} was accepted'):
                worker.parse_syzygy_manifest(payload)

    def test_malformed_fields_are_refused(self):
        base = {'name': 'KQvK.atbw', 'bytes': 10, 'sha256': '0' * 64}
        for override in ({'bytes': -1}, {'bytes': '10'}, {'bytes': True},
                         {'sha256': 'xyz'}, {'sha256': '0' * 63},
                         {'name': ''}, {'bytes': worker.SYZYGY_MAX_FILE_BYTES + 1}):
            payload = {'set': worker.SYZYGY_SET, 'files': [dict(base, **override)]}
            with self.assertRaises(worker.SyzygyManifestError,
                                   msg=f'{override} was accepted'):
                worker.parse_syzygy_manifest(payload)

    def test_an_empty_or_duplicated_file_list_is_refused(self):
        with self.assertRaises(worker.SyzygyManifestError):
            worker.parse_syzygy_manifest({'set': worker.SYZYGY_SET, 'files': []})
        entry = {'name': 'KQvK.atbw', 'bytes': 1, 'sha256': '0' * 64}
        with self.assertRaises(worker.SyzygyManifestError):
            worker.parse_syzygy_manifest(
                {'set': worker.SYZYGY_SET, 'files': [entry, dict(entry)]})


class FetchTests(SimpleTestCase):

    def setUp(self):
        self._source = tempfile.TemporaryDirectory()
        self._dest = tempfile.TemporaryDirectory()
        self.source = pathlib.Path(self._source.name)
        self.dest = pathlib.Path(self._dest.name) / 'syzygy-345'
        (self.source / 'KQvK.atbw').write_bytes(b'queen' * 1000)
        (self.source / 'KRvK.atbw').write_bytes(b'rook' * 800)
        (self.source / 'KPvK.atbz').write_bytes(b'pawn' * 1200)
        self.addCleanup(self._source.cleanup)
        self.addCleanup(self._dest.cleanup)

    def test_a_cold_fetch_brings_the_whole_set_down_verified(self):
        mirror = Mirror(self.source)
        got = worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet, get=mirror)

        self.assertEqual(got, self.dest)
        self.assertEqual(len(mirror.file_hits()), 3)
        for entry in manifest_from_directory(self.source)['files']:
            self.assertEqual(worker.syzygy_file_state(self.dest, entry), 'ok')

    def test_a_second_run_downloads_nothing(self):
        """It runs on every start, including after a self-update restart."""
        mirror = Mirror(self.source)
        worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet, get=mirror)
        again = Mirror(self.source)
        got = worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet, get=again)

        self.assertEqual(got, self.dest)
        self.assertEqual(again.file_hits(), [],
                         'a verified set must not be fetched again')

    def test_a_corrupted_file_is_the_only_one_refetched(self):
        mirror = Mirror(self.source)
        worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet, get=mirror)
        victim = self.dest / 'KRvK.atbw'
        blob = bytearray(victim.read_bytes())
        blob[0] ^= 0xFF                       # same length, different content
        victim.write_bytes(bytes(blob))

        repair = Mirror(self.source)
        got = worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet, get=repair)

        self.assertEqual(got, self.dest)
        self.assertEqual([u.rsplit('/', 1)[-1] for u in repair.file_hits()],
                         ['KRvK.atbw'])
        self.assertEqual(victim.read_bytes(),
                         (self.source / 'KRvK.atbw').read_bytes())

    def test_a_truncated_file_is_caught_on_size_before_hashing(self):
        mirror = Mirror(self.source)
        worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet, get=mirror)
        victim = self.dest / 'KPvK.atbz'
        victim.write_bytes(victim.read_bytes()[:-10])
        entry = next(e for e in manifest_from_directory(self.source)['files']
                     if e['name'] == 'KPvK.atbz')

        self.assertEqual(worker.syzygy_file_state(self.dest, entry),
                         'wrong-size')
        worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet,
                                get=Mirror(self.source))
        self.assertEqual(worker.syzygy_file_state(self.dest, entry), 'ok')

    def test_a_missing_file_is_reported_as_missing(self):
        entry = manifest_from_directory(self.source)['files'][0]
        self.assertEqual(worker.syzygy_file_state(self.dest, entry), 'missing')

    def test_a_dead_mirror_yields_none_rather_than_an_exception(self):
        mirror = Mirror(self.source, manifest_status=503)
        got = worker.fetch_syzygy_set(SERVER, self.dest, attempts=2,
                                      log=_quiet, get=mirror)
        self.assertIsNone(got)

    def test_one_bad_file_fails_the_set_without_losing_the_good_ones(self):
        mirror = Mirror(self.source, fail={'KRvK.atbw'})
        got = worker.fetch_syzygy_set(SERVER, self.dest, attempts=2,
                                      log=_quiet, get=mirror)

        self.assertIsNone(got)
        # The two that arrived are intact and a later run will keep them.
        self.assertTrue((self.dest / 'KQvK.atbw').exists())
        self.assertFalse((self.dest / 'KRvK.atbw').exists(),
                         'a file that failed must not exist half-written')
        recovered = Mirror(self.source)
        self.assertEqual(worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet,
                                                 get=recovered), self.dest)
        self.assertEqual([u.rsplit('/', 1)[-1] for u in recovered.file_hits()],
                         ['KRvK.atbw'], 'only the missing file should refetch')

    def test_a_server_sending_more_than_declared_is_refused(self):
        mirror = Mirror(self.source, overlong={'KQvK.atbw'})
        got = worker.fetch_syzygy_set(SERVER, self.dest, attempts=1,
                                      log=_quiet, get=mirror)
        self.assertIsNone(got)
        self.assertFalse((self.dest / 'KQvK.atbw').exists())

    def test_no_part_files_survive_a_failure(self):
        worker.fetch_syzygy_set(SERVER, self.dest, attempts=1, log=_quiet,
                                get=Mirror(self.source, fail={'KPvK.atbz'}))
        leftovers = [p.name for p in self.dest.iterdir()
                     if p.name.endswith('.part')]
        self.assertEqual(leftovers, [])

    def test_the_manifest_is_requested_from_the_published_path(self):
        mirror = Mirror(self.source)
        worker.fetch_syzygy_set(SERVER, self.dest, log=_quiet, get=mirror)
        self.assertEqual(
            mirror.hits[0],
            SERVER + '/atomicdb/engines/syzygy-345/manifest.json')
        self.assertTrue(all(
            u.startswith(SERVER + '/atomicdb/engines/syzygy-345/')
            for u in mirror.file_hits()))


class ResolveTests(SimpleTestCase):

    def setUp(self):
        self._source = tempfile.TemporaryDirectory()
        self.source = pathlib.Path(self._source.name)
        (self.source / 'KQvK.atbw').write_bytes(b'queen' * 10)
        self.addCleanup(self._source.cleanup)

    def test_an_explicit_syzygy_wins_and_touches_the_network_never(self):
        """Our T2 box points at a full six-man set; do not 'help'."""
        def explode(*args, **kwargs):
            raise AssertionError('the network must not be touched')

        args = _Args(syzygy='/srv/tb/6men;/srv/tb/5men')
        self.assertEqual(
            worker.resolve_syzygy(args, SERVER, log=_quiet, get=explode),
            '/srv/tb/6men;/srv/tb/5men')

    def test_the_opt_out_is_clean(self):
        def explode(*args, **kwargs):
            raise AssertionError('the network must not be touched')

        args = _Args(no_fetch_syzygy=True)
        self.assertEqual(
            worker.resolve_syzygy(args, SERVER, log=_quiet, get=explode), '')

    def test_a_dead_mirror_still_yields_a_startable_worker(self):
        """No tablebases is slower. Not starting is worse."""
        mirror = Mirror(self.source, manifest_status=500)
        with tempfile.TemporaryDirectory() as home:
            script = pathlib.Path(home) / 'atomicdb_worker.py'
            script.write_text('', encoding='utf-8')
            path = worker.resolve_syzygy(_Args(), SERVER, log=_quiet,
                                         script_path=str(script), get=mirror)
        self.assertEqual(path, '')

    def test_the_set_lands_beside_the_script_not_the_cwd(self):
        mirror = Mirror(self.source)
        with tempfile.TemporaryDirectory() as home:
            script = pathlib.Path(home) / 'atomicdb_worker.py'
            script.write_text('', encoding='utf-8')
            path = worker.resolve_syzygy(_Args(), SERVER, log=_quiet,
                                         script_path=str(script), get=mirror)
            self.assertEqual(pathlib.Path(path),
                             pathlib.Path(home) / worker.SYZYGY_DIR_NAME)
            self.assertTrue((pathlib.Path(path) / 'KQvK.atbw').exists())

    def test_every_slot_shares_one_copy(self):
        """--jobs K must not mean K downloads of 1.3 GB."""
        with tempfile.TemporaryDirectory() as home:
            script = str(pathlib.Path(home) / 'atomicdb_worker.py')
            self.assertEqual(worker.syzygy_dir(script),
                             worker.syzygy_dir(script))


class StartupOrderTests(SimpleTestCase):
    """The fetch has to survive the way workers actually get upgraded.

    The fleet self-updates between batches and re-execs, so a fetch that only
    ran on a cold start would never run for an already-deployed worker -- which
    is every worker that matters.  Pinned by reading main() rather than by
    hoping, since the ordering is the whole property.
    """

    def test_the_fetch_runs_after_the_self_update_restart(self):
        text = (pathlib.Path(__file__).resolve().parent.parent / 'Client'
                / 'atomicdb_worker.py').read_text(encoding='utf-8')
        body = text.split('def main():', 1)[1]
        restart = body.index('_restart_updated_worker()')
        resolve = body.index('resolve_syzygy(')
        self.assertLess(restart, resolve,
                        'the set must be resolved after a self-update restart, '
                        'so a restarted worker re-verifies it')

    def test_the_capability_flag_follows_the_path_not_the_probe(self):
        """python-chess missing must not hide the tablebases from the server."""
        text = (pathlib.Path(__file__).resolve().parent.parent / 'Client'
                / 'atomicdb_worker.py').read_text(encoding='utf-8')
        self.assertIn("'tb': '1' if a.syzygy else '0'", text)
