import bz2
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory, TestCase, override_settings

import OpenBench.config
import OpenBench.relabel
import OpenBench.views  # noqa: F401  (import order: views before workloads)
from OpenBench.datagen import claim_chunk
from OpenBench.models import DatagenChunk, Engine, LogEvent, Machine, Network, Profile, Test
from OpenBench.workloads import create_workload, get_workload, verify_workload

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Client'))
import relabel as client_relabel  # noqa: E402

FORMAT = 'alice-v2-record-v1'
FMT = OpenBench.relabel.RELABEL_FORMATS[FORMAT]


def alice_record(index, score=0):
    """A well-formed ALICEV2_RECORD_V1 record: two kings plus a numbered pawn."""

    cells = bytearray(64)
    cells[4] = 6            # white king e1
    cells[60] = 14          # black king e8
    cells[8 + index % 48] = 1 | (0x10 if index % 2 else 0)
    body = struct.pack('<64sIhHbBBBHH', bytes(cells), 12 | (20 << 6), score,
                       10 + index, 0, index % 2, 0, 1, 0, 1 + index)
    return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)


def relabelled(data, offset=7):
    return b''.join(
        client_relabel.rewrite_score(
            data[i:i + 84], FMT, client_relabel.record_score(data[i:i + 84], FMT) + offset
        )
        for i in range(0, len(data), 84)
    )


def install_input_set(media_root, set_name='t349-mini', per_chunk=3, total=7):
    directory = Path(media_root, 'relabel', 'inputs', set_name)
    directory.mkdir(parents=True)
    data = b''.join(alice_record(i, score=i * 10 - 30) for i in range(total))
    chunks = []
    for idx, start in enumerate(range(0, total, per_chunk)):
        raw = data[start * 84:min(total, start + per_chunk) * 84]
        compressed = bz2.compress(raw)
        (directory / ('chunk_%d.bz2' % idx)).write_bytes(compressed)
        chunks.append({
            'idx': idx,
            'file': 'chunk_%d.bz2' % idx,
            'records': len(raw) // 84,
            'sha256': hashlib.sha256(compressed).hexdigest(),
            'bytes': len(compressed),
            'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'keys_sha256': hashlib.sha256(
                OpenBench.relabel.key_projection(raw, FMT)).hexdigest(),
        })
    manifest = {
        'schema': OpenBench.relabel.RELABEL_MANIFEST_SCHEMA,
        'set': set_name,
        'format': FORMAT,
        'variant': 'alice',
        'record_bytes': 84,
        'records_per_chunk': per_chunk,
        'total_records': total,
        'chunks': chunks,
    }
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    return manifest, data


class RelabelModeTests(TestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.user = User.objects.create_user('relabeler', password='password')
        Profile.objects.create(user=self.user, enabled=True, approver=True)
        Network.objects.create(engine='Alice-Stockfish', sha256='9F9E5570',
                               name='alice_run2rl_e40_l09.nnue', author='relabeler')
        self.manifest, self.data = install_input_set(self.media.name)

    def tearDown(self):
        self.media_override.disable()
        self.media.cleanup()

    def payload(self, **changes):
        payload = {
            'dev_engine': 'Alice-Stockfish',
            'dev_repo': 'https://github.com/Belzedar94/Alice-Stockfish',
            'dev_branch': 'main',
            'dev_network': '9F9E5570',
            'dev_options': 'Threads=1 Hash=64',
            'book_name': 'NONE',
            'datagen_command': '',
            'datagen_total_count': '16000',
            'datagen_positions_per_chunk': '2000',
            'datagen_base_seed': '1',
            'priority': '300',
            'throughput': '1000',
            'relabel_input_set': 't349-mini',
            'relabel_depth': '10',
            'relabel_nodes': '0',
        }
        payload.update(changes)
        return payload

    def create(self, **changes):
        request = RequestFactory().post('/newDatagen/', self.payload(**changes))
        request.user = self.user
        engine_info = (
            ('https://example.test/alice.zip', 'main', 'c' * 40, 202963), True,
        )
        with mock.patch.object(verify_workload, 'collect_github_info',
                               return_value=engine_info):
            return create_workload.create_new_datagen(request)

    def make_test(self):
        test, errors = self.create()
        self.assertIsNone(errors)
        test.approved = True
        test.save(update_fields=['approved'])
        return test

    def make_machine(self, username='worker', capable=True):
        user = User.objects.create_user(username, password='password')
        Profile.objects.create(user=user, enabled=True)
        info = {
            'supported': list(OpenBench.config.OPENBENCH_CONFIG['engines']),
            'concurrency': 8,
            'physical_cores': 8,
            'sockets': 1,
            'focus': [],
            'client_ver': OpenBench.config.OPENBENCH_CONFIG['client_version'],
            'tablebases': {'standard': 0, 'atomic': {'max': 0, 'manifest_sha256': None}},
            'syzygy_max': 0,
        }
        if capable:
            info['relabel_protocols'] = [OpenBench.relabel.RELABEL_PROTOCOL]
            info['relabel_formats'] = [FORMAT]
        return Machine.objects.create(user=user, secret='secret', info=info)

    def upload(self, test, chunk, machine, payload):
        return Client().post('/clientSubmitDatagen/', {
            'machine_id': machine.id,
            'secret': machine.secret,
            'test_id': test.id,
            'chunk_idx': chunk.idx,
            'attempt': chunk.attempts,
            'sha256': hashlib.sha256(payload).hexdigest(),
            'bytes': len(payload),
            'file': SimpleUploadedFile('chunk.bz2', payload),
        })

    def chunk_input(self, idx):
        lo = idx * 3
        return self.data[lo * 84:min(7, lo + 3) * 84]

    # Creation -----------------------------------------------------------

    def test_creation_freezes_the_input_set_into_the_chunk_map(self):
        test = self.make_test()

        self.assertTrue(test.is_generic_datagen())
        self.assertTrue(test.is_relabel())
        self.assertEqual(test.relabel_format, FORMAT)
        self.assertEqual((test.relabel_depth, test.relabel_nodes), (10, 0))
        self.assertEqual(test.datagen_command,
                         'RELABEL set=t349-mini format=alice-v2-record-v1 depth=10 nodes=0')
        self.assertNotIn('{', test.datagen_command)
        self.assertEqual(test.datagen_total_count, 7)
        self.assertEqual(test.datagen_total_chunks(), 3)
        self.assertEqual(test.book_name, 'NONE')
        self.assertEqual(test.dev_netname, 'alice_run2rl_e40_l09.nnue')
        self.assertEqual(test.relabel_manifest_sha256,
                         OpenBench.relabel.manifest_sha256(self.manifest))
        self.assertFalse(test.datagen_producer_required)
        self.assertEqual(test.datagen_publication_protocol, 0)
        chunks = list(test.datagen_chunks.order_by('idx'))
        self.assertEqual([c.position_count for c in chunks], [3, 3, 1])
        for chunk, entry in zip(chunks, self.manifest['chunks']):
            self.assertEqual(chunk.relabel_input['sha256'], entry['sha256'])
            self.assertEqual(chunk.relabel_input['keys_sha256'], entry['keys_sha256'])
            self.assertEqual(chunk.filename(), 'relabel/%d/out/chunk_%d.bz2' % (test.id, chunk.idx))

    def test_creation_validates_limits_network_engine_and_manifest(self):
        cases = [
            (dict(relabel_depth='0', relabel_nodes='0'), 'exactly one of depth'),
            (dict(relabel_depth='10', relabel_nodes='5000'), 'exactly one of depth'),
            (dict(relabel_depth='999'), 'exactly one of depth'),
            (dict(dev_network=''), 'requires the network'),
            (dict(dev_engine='Spell-Stockfish', dev_network=''), 'requires the network'),
            (dict(relabel_input_set='missing-set'), 'has no manifest.json'),
            (dict(relabel_input_set='Bad Name'), 'lowercase slug'),
            (dict(datagen_publication_protocol='41'), 'publication contract'),
        ]
        for changes, message in cases:
            test, errors = self.create(**changes)
            self.assertIsNone(test)
            self.assertTrue(any(message in error for error in errors), (changes, errors))
        self.assertFalse(Test.objects.exists())

    def test_engine_without_the_format_is_refused(self):
        Network.objects.create(engine='Spell-Stockfish', sha256='ABCDEF12', name='n', author='x')
        test, errors = self.create(dev_engine='Spell-Stockfish', dev_network='ABCDEF12')
        self.assertIsNone(test)
        self.assertTrue(any('does not declare' in e for e in errors), errors)

    def test_manifest_inconsistencies_are_refused(self):
        def broken(edit):
            manifest = json.loads(json.dumps(self.manifest))
            edit(manifest)
            return manifest

        cases = [
            broken(lambda m: m.update(format='psv')),
            broken(lambda m: m.update(variant='chess')),
            broken(lambda m: m.update(total_records=8)),
            broken(lambda m: m['chunks'][1].update(records=2)),
            broken(lambda m: m['chunks'][2].update(file='../escape.bz2')),
            broken(lambda m: m['chunks'][0].update(keys_sha256='x')),
            broken(lambda m: m['chunks'].reverse()),
            broken(lambda m: m.update(set='other')),
        ]
        for manifest in cases:
            with self.assertRaises(OpenBench.relabel.RelabelError):
                OpenBench.relabel.validate_manifest(manifest, 't349-mini')

        Path(self.media.name, 'relabel', 'inputs', 't349-mini', 'chunk_1.bz2').write_bytes(b'x')
        with self.assertRaisesRegex(OpenBench.relabel.RelabelError, 'wrong size'):
            OpenBench.relabel.load_manifest(self.media.name, 't349-mini')

    def test_check_input_command_verifies_every_file(self):
        out = io.StringIO()
        call_command('relabel_check_input', 't349-mini', stdout=out)
        self.assertIn('OK: 3 chunks, 7 records', out.getvalue())

        path = Path(self.media.name, 'relabel', 'inputs', 't349-mini', 'chunk_0.bz2')
        size = path.stat().st_size
        path.write_bytes(bz2.compress(self.chunk_input(0)[:-1] + b'\x00')[:size].ljust(size, b'\x00'))
        with self.assertRaises(CommandError):
            call_command('relabel_check_input', 't349-mini', stdout=io.StringIO())

    # Scheduling ---------------------------------------------------------

    def test_only_capable_workers_are_offered_relabel_chunks(self):
        test = self.make_test()
        request = RequestFactory().post('/clientGetWorkload/', {'blacklist': []})

        old = self.make_machine('old-worker', capable=False)
        self.assertEqual(get_workload.get_workload(request, old), {})
        focus = RequestFactory().post('/clientGetWorkload/', {'focus_test': str(test.id)})
        self.assertIn('RELABEL', get_workload.get_workload(focus, old)['focus']['message'])

        new = self.make_machine('new-worker')
        workload = get_workload.get_workload(request, new)['workload']
        spec = workload['test']['datagen']['relabel']
        self.assertEqual(workload['test']['datagen']['chunk_idx'], 0)
        self.assertEqual(spec['format'], FORMAT)
        self.assertEqual(spec['depth'], 10)
        self.assertEqual(spec['record_bytes'], 84)
        self.assertEqual(spec['max_engines'], 8)
        expected = dict(self.manifest['chunks'][0])
        del expected['idx']
        self.assertEqual(spec['input'], expected)
        self.assertEqual(workload['test']['dev']['network'], '9F9E5570')

    def test_input_is_served_only_to_the_lease_owner(self):
        test = self.make_test()
        owner = self.make_machine('owner')
        other = self.make_machine('other')
        chunk = claim_chunk(test, owner)

        def fetch(machine, attempt):
            return Client().post('/clientRelabelInput/', {
                'machine_id': machine.id, 'secret': machine.secret,
                'test_id': test.id, 'chunk_idx': chunk.idx, 'attempt': attempt,
            })

        response = fetch(owner, chunk.attempts)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(bz2.decompress(b''.join(response.streaming_content)),
                         self.chunk_input(0))
        self.assertEqual(fetch(other, chunk.attempts).status_code, 409)
        self.assertEqual(fetch(owner, chunk.attempts + 1).status_code, 409)

    # Upload validation --------------------------------------------------

    def test_pure_relabel_is_stored_and_completes_the_workload(self):
        test = self.make_test()
        machine = self.make_machine()
        for idx in range(3):
            chunk = claim_chunk(test, machine)
            payload = bz2.compress(relabelled(self.chunk_input(idx)))
            response = self.upload(test, chunk, machine, payload)
            self.assertEqual(response.status_code, 200, response.content)
            stored = Path(self.media.name, 'relabel', str(test.id), 'out', 'chunk_%d.bz2' % idx)
            self.assertEqual(stored.read_bytes(), payload)
        test.refresh_from_db()
        self.assertTrue(test.finished)
        self.assertTrue(test.passed)
        self.assertEqual(test.games, 7)

        manifest = Client().get('/api/datagen/%d/' % test.id)
        self.assertEqual(manifest.status_code, 200)

    def test_garbage_shifted_or_tampered_output_is_rejected_and_requeued(self):
        test = self.make_test()
        machine = self.make_machine()
        good = relabelled(self.chunk_input(0))
        moved = bytearray(good)
        moved[66] ^= 1          # stored move belongs to the position key
        bad_score = client_relabel.rewrite_score(good[:84], FMT, 0)
        bad_score = bytearray(bad_score)
        struct.pack_into('<h', bad_score, 68, -32768)
        struct.pack_into('<I', bad_score, 80, zlib.crc32(bytes(bad_score[:80])) & 0xFFFFFFFF)
        bad_crc = bytearray(good)
        bad_crc[68] ^= 1
        cases = {
            'missing record': bz2.compress(good[:-84]),
            'extra record': bz2.compress(good + good[:84]),
            'shifted records': bz2.compress(good[84:] + good[:84]),
            'changed key byte': bz2.compress(client_relabel.rewrite_score(bytes(moved[:84]), FMT, 1) + good[84:]),
            'out of range score': bz2.compress(bytes(bad_score) + good[84:]),
            'bad checksum': bz2.compress(bytes(bad_crc)),
            'other chunk': bz2.compress(relabelled(self.chunk_input(1))),
            'not bzip2': good,
        }
        for label, payload in cases.items():
            chunk = claim_chunk(test, machine)
            self.assertEqual(chunk.idx, 0, label)
            response = self.upload(test, chunk, machine, payload)
            self.assertEqual(response.status_code, 422, (label, response.content))
            self.assertIn('RELABEL chunk 0 rejected', response.json()['error'])
            chunk.refresh_from_db()
            self.assertEqual(chunk.status, DatagenChunk.PENDING, label)
            self.assertIn('rejected', chunk.last_error)
            self.assertFalse(Path(self.media.name, chunk.filename()).exists(), label)
        self.assertEqual(
            LogEvent.objects.filter(test_id=test.id, summary__startswith='RELABEL').count(),
            len(cases),
        )
        test.refresh_from_db()
        self.assertEqual((test.games, test.datagen_completed_chunks), (0, 0))

    def test_tampered_input_on_the_server_blocks_completion(self):
        test = self.make_test()
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        path = Path(self.media.name, 'relabel', 'inputs', 't349-mini', 'chunk_0.bz2')
        path.write_bytes(bz2.compress(relabelled(self.chunk_input(0), offset=1)))
        response = self.upload(test, chunk, machine, bz2.compress(relabelled(self.chunk_input(0))))
        self.assertEqual(response.status_code, 422, response.content)


class RelabelFormatTableTests(TestCase):

    def test_server_and_client_agree_on_every_format(self):
        self.assertEqual(OpenBench.relabel.RELABEL_PROTOCOL, client_relabel.RELABEL_PROTOCOL)
        self.assertEqual(set(OpenBench.relabel.RELABEL_FORMATS), set(client_relabel.FORMATS))
        for name, fmt in OpenBench.relabel.RELABEL_FORMATS.items():
            self.assertEqual(fmt, client_relabel.FORMATS[name])
        data = b''.join(alice_record(i) for i in range(5))
        self.assertEqual(OpenBench.relabel.key_projection(data, FMT),
                         client_relabel.key_projection(data, FMT))

    def test_alice_engine_declares_its_relabel_format(self):
        engines = OpenBench.config.OPENBENCH_CONFIG['engines']
        self.assertEqual(engines['Alice-Stockfish']['relabel_formats'], [FORMAT])

    def test_synthetic_records_are_valid_alice_positions(self):
        fen, _material = client_relabel.alice_decode(alice_record(3))
        self.assertEqual(fen.split()[1], 'b')
