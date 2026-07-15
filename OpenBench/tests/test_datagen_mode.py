import bz2
import hashlib
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

import OpenBench.views
from OpenBench.datagen import DATAGEN_LEASE, claim_chunk, initialize_chunks
from OpenBench.models import DatagenChunk, Engine, Machine, Profile, Test
from OpenBench.workloads import create_workload, get_workload, verify_workload


class DatagenModeTests(TestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()

        self.user = User.objects.create_user('datagen', password='password')
        Profile.objects.create(
            user=self.user, enabled=True, approver=True, engine='Spell-Stockfish'
        )
        self.engine = Engine.objects.create(
            name='nnue-v2',
            source='https://example.test/archive.zip',
            sha='a' * 40,
            bench=123,
        )

    def tearDown(self):
        self.media_override.disable()
        self.media.cleanup()

    def make_test(self, total=4, per_chunk=2):
        test = Test.objects.create(
            author=self.user.username,
            book_name='NONE',
            dev=self.engine,
            base=self.engine,
            dev_repo='https://github.com/example/engine',
            base_repo='https://github.com/example/engine',
            dev_engine='Spell-Stockfish',
            base_engine='Spell-Stockfish',
            dev_options='',
            base_options='',
            dev_time_control='',
            base_time_control='',
            syzygy_wdl='DISABLED',
            syzygy_adj='DISABLED',
            test_mode='DATAGEN',
            datagen_command=(
                'datagen seed {SEED} count {COUNT} threads {THREADS} '
                'book {BOOK} out {OUT}'
            ),
            datagen_total_count=total,
            datagen_positions_per_chunk=per_chunk,
            datagen_base_seed=100,
            max_games=total,
            throughput=1000,
            approved=True,
        )
        initialize_chunks(test)
        return test

    def make_machine(self, username='worker', machine_id=None):
        user = User.objects.create_user(username, password='password')
        Profile.objects.create(user=user, enabled=True)
        info = {
            'supported': ['Spell-Stockfish'],
            'concurrency': 2,
            'physical_cores': 2,
            'sockets': 1,
            'focus': [],
            'client_ver': 37,
            'tablebases': {'standard': 0},
            'syzygy_max': 0,
        }
        return Machine.objects.create(
            id=machine_id,
            user=user,
            secret='secret',
            info=info,
        )

    def test_creation_builds_exact_numbered_chunk_map(self):
        payload = {
            'dev_engine': 'Spell-Stockfish',
            'dev_repo': 'https://github.com/example/engine',
            'dev_branch': 'nnue-v2',
            'dev_network': '',
            'dev_options': '',
            'book_name': 'NONE',
            'datagen_command': (
                'datagen seed {SEED} count {COUNT} threads {THREADS} '
                'book {BOOK} out {OUT}'
            ),
            'datagen_total_count': '16000',
            'datagen_positions_per_chunk': '2000',
            'datagen_base_seed': '9000',
            'priority': '0',
            'throughput': '1000',
        }
        request = RequestFactory().post('/newDatagen/', payload)
        request.user = self.user
        engine_info = (
            ('https://example.test/archive.zip', 'nnue-v2', 'b' * 40, 456),
            True,
        )

        with mock.patch.object(
            create_workload, 'verify_workload', return_value=([], engine_info)
        ):
            test, errors = create_workload.create_new_datagen(request)

        self.assertIsNone(errors)
        self.assertEqual(test.dev_id, test.base_id)
        self.assertEqual(test.datagen_total_chunks(), 8)
        self.assertEqual(
            list(test.datagen_chunks.values_list('idx', 'position_count')),
            [(idx, 2000) for idx in range(8)],
        )

    def test_scheduler_assigns_seed_count_and_renews_stale_work(self):
        test = self.make_test(total=5, per_chunk=2)
        first = self.make_machine('first')
        request = RequestFactory().post('/clientGetWorkload/', {'blacklist': []})

        response = get_workload.get_workload(request, first)['workload']
        self.assertEqual(response['test']['datagen']['chunk_idx'], 0)
        self.assertEqual(response['test']['datagen']['chunk_count'], 2)
        self.assertEqual(response['test']['datagen']['seed'], 100)
        self.assertEqual(response['test']['datagen']['command'], test.datagen_command)

        chunk = test.datagen_chunks.get(idx=0)
        chunk.assigned = timezone.now() - DATAGEN_LEASE - timedelta(seconds=1)
        chunk.save(update_fields=['assigned'])

        second = self.make_machine('second')
        reclaimed = claim_chunk(test, second)
        self.assertEqual(reclaimed.idx, 0)
        self.assertEqual(reclaimed.machine_id, second.id)
        self.assertEqual(reclaimed.attempts, 2)

    def test_upload_verifies_opaque_bz2_and_finishes_on_all_chunks(self):
        test = self.make_test(total=4, per_chunk=2)
        machine = self.make_machine()
        client = Client()

        for idx in range(2):
            chunk = claim_chunk(test, machine)
            self.assertEqual(chunk.idx, idx)
            payload = bz2.compress(('opaque-%d' % idx).encode())
            sha256 = hashlib.sha256(payload).hexdigest()
            response = client.post(
                '/clientSubmitDatagen/',
                {
                    'machine_id': machine.id,
                    'secret': machine.secret,
                    'test_id': test.id,
                    'chunk_idx': idx,
                    'sha256': sha256,
                    'bytes': len(payload),
                    'file': SimpleUploadedFile('chunk.bz2', payload),
                },
            )
            self.assertEqual(response.status_code, 200, response.content)
            row = DatagenChunk.objects.get(test=test, idx=idx)
            self.assertEqual(row.sha256, sha256)
            self.assertEqual(row.bytes, len(payload))
            self.assertTrue(os.path.isfile(os.path.join(self.media.name, row.filename())))

        test.refresh_from_db()
        self.assertTrue(test.finished)
        self.assertTrue(test.passed)
        self.assertEqual(test.games, 4)
        self.assertEqual(response.json()['completed_chunks'], 2)

    def test_runtime_error_uses_event_flow_and_requeues_chunk(self):
        test = self.make_test(total=2, per_chunk=2)
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        machine.workload = test.id
        machine.save()

        response = Client().post(
            '/clientSubmitError/',
            {
                'machine_id': machine.id,
                'secret': machine.secret,
                'test_id': test.id,
                'chunk_idx': chunk.idx,
                'error': 'DATAGEN command completed without creating {OUT}',
                'logs': 'Unknown command: datagen',
            },
        )
        self.assertEqual(response.status_code, 200)
        chunk.refresh_from_db()
        machine.refresh_from_db()
        self.assertEqual(chunk.status, DatagenChunk.PENDING)
        self.assertIsNone(chunk.machine)
        self.assertEqual(machine.workload, 0)
        self.assertIn('without creating', chunk.last_error)

    def test_template_validation_rejects_unknown_or_missing_placeholders(self):
        valid = SimpleNamespace(POST={'datagen_command': (
            'datagen seed {SEED} count {COUNT} threads {THREADS} out {OUT}'
            ' network {NETWORK}'
        )})
        errors = []
        verify_workload.verify_datagen_template(errors, valid, 'datagen_command')
        self.assertEqual(errors, [])

        invalid = SimpleNamespace(POST={'datagen_command': 'datagen {SPELL} {OUT}'})
        errors = []
        verify_workload.verify_datagen_template(errors, invalid, 'datagen_command')
        self.assertEqual(len(errors), 1)
