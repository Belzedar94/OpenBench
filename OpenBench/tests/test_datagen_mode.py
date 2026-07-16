import bz2
import hashlib
import os
import tempfile
import threading
import traceback
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import (
    Client, RequestFactory, TestCase, TransactionTestCase, override_settings,
)
from django.utils import timezone

import OpenBench.datagen
import OpenBench.config
import OpenBench.utils
import OpenBench.views
from OpenBench.templatetags import mytags
from OpenBench.datagen import (
    DATAGEN_CHUNK_CREATE_BATCH,
    DATAGEN_LEASE,
    MAX_LEGACY_DATAGEN_GAMES,
    MAX_DATAGEN_CHUNKS,
    claim_chunk,
    initialize_chunks,
    renew_chunk,
    requeue_chunk,
)
from OpenBench.models import DatagenChunk, Engine, Machine, Profile, Test
from OpenBench.workloads import (
    create_workload,
    get_workload,
    verify_workload,
    view_workload,
)


class MachineIdentityTests(TestCase):

    def setUp(self):
        self.owner = User.objects.create_user('machine-owner', password='password')
        self.other = User.objects.create_user('other-worker', password='password')
        self.info = {
            'mac_address': 'AABBCCDDEEFF',
            'client_ver': 38,
            'concurrency': 2,
        }
        self.machine = Machine.objects.create(
            user=self.owner,
            secret='owner-secret',
            info=self.info,
        )
        self.profile = Profile.objects.create(user=self.owner, enabled=True)

    def test_persisted_machine_id_is_reusable_by_its_owner(self):
        resolved = OpenBench.utils.get_machine(
            str(self.machine.id), self.owner, dict(self.info)
        )

        self.assertEqual(resolved, self.machine)

    def test_persisted_machine_id_is_rejected_for_another_user(self):
        resolved = OpenBench.utils.get_machine(
            str(self.machine.id), self.other, dict(self.info)
        )

        self.assertIsNone(resolved)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.user, self.owner)
        self.assertEqual(self.machine.secret, 'owner-secret')

    def test_disabling_profile_revokes_an_existing_worker_session(self):
        self.profile.enabled = False
        self.profile.save(update_fields=['enabled'])
        request = RequestFactory().post(
            '/clientGetWorkload/',
            {'machine_id': self.machine.id, 'secret': self.machine.secret},
        )

        response = OpenBench.views.client_get_workload(request)

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {'error': 'Worker Account Disabled', 'stop': True},
        )


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

    def make_test(self, total=4, per_chunk=2, initialize=True):
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
            max_games=min(total, MAX_LEGACY_DATAGEN_GAMES),
            throughput=1000,
            approved=True,
        )
        if initialize:
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
            'client_ver': 38,
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

    def test_creation_caps_legacy_summary_and_preserves_64_bit_total(self):
        total = MAX_LEGACY_DATAGEN_GAMES + 123
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
            'datagen_total_count': str(total),
            'datagen_positions_per_chunk': str(total),
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
        test.refresh_from_db()
        self.assertEqual(test.datagen_total_count, total)
        self.assertEqual(test.max_games, MAX_LEGACY_DATAGEN_GAMES)
        self.assertEqual(test.games, 0)
        self.assertEqual(
            Test._meta.get_field('games').get_internal_type(), 'BigIntegerField'
        )
        self.assertEqual(test.datagen_chunks.get().position_count, total)

    def test_chunk_count_is_capped_before_initialization(self):
        boundary = SimpleNamespace(POST={
            'total': str(MAX_DATAGEN_CHUNKS),
            'per_chunk': '1',
        })
        errors = []
        verify_workload.verify_datagen_counts(
            errors, boundary, 'total', 'per_chunk'
        )
        self.assertEqual(errors, [])

        over_limit = SimpleNamespace(POST={
            'total': str(MAX_DATAGEN_CHUNKS + 1),
            'per_chunk': '1',
        })
        errors = []
        verify_workload.verify_datagen_counts(
            errors, over_limit, 'total', 'per_chunk'
        )
        self.assertEqual(len(errors), 1)
        self.assertIn(str(MAX_DATAGEN_CHUNKS), errors[0])

        test = self.make_test(
            total=MAX_DATAGEN_CHUNKS + 1,
            per_chunk=1,
            initialize=False,
        )
        with mock.patch.object(DatagenChunk.objects, 'bulk_create') as create:
            with self.assertRaisesRegex(ValueError, str(MAX_DATAGEN_CHUNKS)):
                initialize_chunks(test)
        create.assert_not_called()

    def test_chunk_initialization_uses_bounded_bulk_create_batches(self):
        total = DATAGEN_CHUNK_CREATE_BATCH * 2 + 1
        test = self.make_test(total=total, per_chunk=1, initialize=False)
        batches = []

        def capture(objects, **_kwargs):
            batches.append(list(objects))

        with mock.patch.object(
            DatagenChunk.objects, 'bulk_create', side_effect=capture
        ):
            initialize_chunks(test)

        self.assertEqual(
            [len(batch) for batch in batches],
            [DATAGEN_CHUNK_CREATE_BATCH, DATAGEN_CHUNK_CREATE_BATCH, 1],
        )
        self.assertEqual(
            [chunk.idx for batch in batches for chunk in batch],
            list(range(total)),
        )

    def test_engine_change_uses_current_datagen_mode_for_presets(self):
        source = (
            Path(__file__).resolve().parents[2]
            / 'OpenBench' / 'static' / 'create_workload.js'
        ).read_text(encoding='utf-8')
        self.assertIn("mode.value == 'DATAGEN'", source)
        self.assertIn(
            'workload_type = current_workload_type(workload_type);', source
        )

    def test_private_engine_is_rejected_for_generic_datagen(self):
        request = SimpleNamespace(POST={'dev_engine': 'PrivateEngine'})
        errors = []
        private = {'PrivateEngine': {'private': True}}
        with mock.patch.dict(
            OpenBench.config.OPENBENCH_CONFIG['engines'], private, clear=False
        ):
            verify_workload.verify_public_datagen_engine(
                errors, request, 'dev_engine'
            )

        self.assertEqual(len(errors), 1)
        self.assertIn('only public engines', errors[0])

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

    def test_expired_owner_cannot_renew_or_requeue_reclaimed_chunk(self):
        test = self.make_test(total=1, per_chunk=1)
        first = self.make_machine('expired-owner')
        second = self.make_machine('new-owner')

        original = claim_chunk(test, first)
        original.assigned = timezone.now() - DATAGEN_LEASE - timedelta(seconds=1)
        original.save(update_fields=['assigned'])
        reclaimed = claim_chunk(test, second)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.machine_id, second.id)

        self.assertFalse(renew_chunk(test.id, reclaimed.idx, first))
        self.assertFalse(
            requeue_chunk(test.id, reclaimed.idx, first, 'late stale error')
        )

        reclaimed.refresh_from_db()
        self.assertEqual(reclaimed.status, DatagenChunk.RUNNING)
        self.assertEqual(reclaimed.machine_id, second.id)
        self.assertEqual(reclaimed.attempts, 2)
        self.assertEqual(reclaimed.last_error, '')

    def test_expired_owner_cannot_upload_over_reclaimed_chunk(self):
        test = self.make_test(total=1, per_chunk=1)
        first = self.make_machine('expired-uploader')
        second = self.make_machine('current-uploader')

        original = claim_chunk(test, first)
        original.assigned = timezone.now() - DATAGEN_LEASE - timedelta(seconds=1)
        original.save(update_fields=['assigned'])
        reclaimed = claim_chunk(test, second)
        self.assertEqual(reclaimed.machine_id, second.id)

        payload = bz2.compress(b'stale-opaque-payload')
        response = Client().post(
            '/clientSubmitDatagen/',
            {
                'machine_id': first.id,
                'secret': first.secret,
                'test_id': test.id,
                'chunk_idx': reclaimed.idx,
                'sha256': hashlib.sha256(payload).hexdigest(),
                'bytes': len(payload),
                'file': SimpleUploadedFile('stale.bz2', payload),
            },
        )

        self.assertEqual(response.status_code, 409, response.content)
        reclaimed.refresh_from_db()
        self.assertEqual(reclaimed.status, DatagenChunk.RUNNING)
        self.assertEqual(reclaimed.machine_id, second.id)
        self.assertEqual(reclaimed.attempts, 2)
        self.assertFalse(
            os.path.exists(os.path.join(self.media.name, reclaimed.filename()))
        )

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

    def test_upload_progress_crosses_signed_32_bit_boundary(self):
        total = MAX_LEGACY_DATAGEN_GAMES + 2
        test = self.make_test(
            total=total,
            per_chunk=MAX_LEGACY_DATAGEN_GAMES,
        )
        machine = self.make_machine()
        client = Client()

        expected_positions = [MAX_LEGACY_DATAGEN_GAMES, total]
        for idx, expected in enumerate(expected_positions):
            chunk = claim_chunk(test, machine)
            payload = bz2.compress(('large-counter-%d' % idx).encode())
            response = client.post(
                '/clientSubmitDatagen/',
                {
                    'machine_id': machine.id,
                    'secret': machine.secret,
                    'test_id': test.id,
                    'chunk_idx': chunk.idx,
                    'sha256': hashlib.sha256(payload).hexdigest(),
                    'bytes': len(payload),
                    'file': SimpleUploadedFile('chunk.bz2', payload),
                },
            )
            self.assertEqual(response.status_code, 200, response.content)
            test.refresh_from_db()
            self.assertEqual(test.games, expected)
            self.assertEqual(response.json()['positions'], expected)

        self.assertEqual(test.max_games, MAX_LEGACY_DATAGEN_GAMES)
        self.assertEqual(test.datagen_completed_chunks, 2)
        self.assertTrue(test.finished)
        self.assertTrue(test.passed)

        self.assertEqual(
            mytags.shortStatBlock(test),
            'Chunks: 2/2\nPositions: %d/%d' % (total, total),
        )
        self.assertEqual(
            mytags.longStatBlock(test),
            'Chunks    | 2 / 2\nPositions | %d / %d' % (total, total),
        )

    def test_storage_failure_rolls_back_completed_transition(self):
        test = self.make_test(total=2, per_chunk=2)
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        payload = bz2.compress(b'opaque-storage-failure')

        with mock.patch.object(
            OpenBench.views.FileSystemStorage,
            'save',
            side_effect=OSError('temporary storage failure'),
        ):
            response = Client().post(
                '/clientSubmitDatagen/',
                {
                    'machine_id': machine.id,
                    'secret': machine.secret,
                    'test_id': test.id,
                    'chunk_idx': chunk.idx,
                    'sha256': hashlib.sha256(payload).hexdigest(),
                    'bytes': len(payload),
                    'file': SimpleUploadedFile('chunk.bz2', payload),
                },
            )

        self.assertEqual(response.status_code, 500, response.content)
        chunk.refresh_from_db()
        test.refresh_from_db()
        self.assertEqual(chunk.status, DatagenChunk.RUNNING)
        self.assertEqual(chunk.machine_id, machine.id)
        self.assertEqual(chunk.sha256, '')
        self.assertEqual(chunk.bytes, 0)
        self.assertFalse(test.finished)
        self.assertEqual(test.games, 0)

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

    def test_search_handles_generic_datagen_with_empty_engine_options(self):
        test = self.make_test(total=2, per_chunk=2)
        response = Client().post('/search/', {
            'author': '',
            'engine': '',
            'opening-book': '',
            'test-mode': 'DATAGEN',
            'syzygy-wdl': '',
            'keywords': '',
            'tc-type': '',
            'tc-value-input': '',
            'tc-value-select': '=',
            'threads-select': '>=',
            'threads-input': '1',
            'show-greens': '',
            'show-yellows': '',
            'show-reds': '',
            'show-blues': '',
            'show-stopped': '',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/datagen/%d/' % test.id)

    def test_datagen_chunk_view_renders_only_one_bounded_page(self):
        page_size = view_workload.DATAGEN_CHUNKS_PER_PAGE
        test = self.make_test(total=page_size + 5, per_chunk=1)
        test.datagen_chunks.filter(idx=page_size).update(
            status=DatagenChunk.COMPLETED
        )
        test.datagen_completed_chunks = 1
        test.games = 1
        test.save(update_fields=['datagen_completed_chunks', 'games'])
        client = Client()
        client.force_login(self.user)

        response = client.get('/datagen/%d/' % test.id)
        self.assertEqual(response.status_code, 200)
        page = response.context['datagen_chunk_page']
        self.assertEqual(page.number, 1)
        self.assertEqual(len(response.context['datagen_chunks']), page_size)
        self.assertEqual(
            [chunk.idx for chunk in response.context['datagen_chunks']],
            list(range(page_size)),
        )
        self.assertContains(response, 'Showing rows 1-%d of %d chunks' % (
            page_size, page_size + 5
        ))
        self.assertContains(response, 'Chunks 1 / %d' % (page_size + 5))
        self.assertContains(response, '?chunks_page=2#datagen-chunks')

        response = client.get(
            '/datagen/%d/' % test.id, {'chunks_page': 2}
        )
        self.assertEqual(response.status_code, 200)
        page = response.context['datagen_chunk_page']
        self.assertEqual(page.number, 2)
        self.assertEqual(
            [chunk.idx for chunk in response.context['datagen_chunks']],
            list(range(page_size, page_size + 5)),
        )
        self.assertContains(response, 'Showing rows %d-%d of %d chunks' % (
            page_size + 1, page_size + 5, page_size + 5
        ))

    def test_datagen_chunk_view_handles_invalid_page_safely(self):
        page_size = view_workload.DATAGEN_CHUNKS_PER_PAGE
        test = self.make_test(total=page_size + 1, per_chunk=1)
        client = Client()
        client.force_login(self.user)

        response = client.get(
            '/datagen/%d/' % test.id, {'chunks_page': 'invalid'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['datagen_chunk_page'].number, 1)

        response = client.get(
            '/datagen/%d/' % test.id, {'chunks_page': '999999'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['datagen_chunk_page'].number, 2)

    def test_template_validation_rejects_unknown_or_missing_placeholders(self):
        valid = SimpleNamespace(POST={'datagen_command': (
            'datagen seed {SEED} count {COUNT} threads {THREADS} out {OUT}'
            ' network {NETWORK} book-sha {BOOK_SHA256}'
        )})
        errors = []
        verify_workload.verify_datagen_template(errors, valid, 'datagen_command')
        self.assertEqual(errors, [])

        invalid = SimpleNamespace(POST={'datagen_command': 'datagen {SPELL} {OUT}'})
        errors = []
        verify_workload.verify_datagen_template(errors, invalid, 'datagen_command')
        self.assertEqual(len(errors), 1)

    def test_workload_carries_separate_normalized_and_raw_book_hashes(self):
        test = self.make_test(total=2, per_chunk=2)
        test.book_name = 'ATOMIC_openings.epd'
        test.save(update_fields=['book_name'])
        machine = self.make_machine()
        chunk = test.datagen_chunks.get(idx=0)

        payload = get_workload.workload_to_dictionary(
            test, SimpleNamespace(id=1), machine, chunk
        )
        book = payload['test']['book']
        self.assertEqual(
            book['sha'],
            'ec3752727cd732a966fd6cb7b3340fb68a726f0b3426d198a3da7b891faa2e91',
        )
        self.assertEqual(
            book['raw_sha'],
            '28ed51c2f42e723d5e127d2d3f21c0bfa4a9b318615afdb299b93ea62dea2b1e',
        )


class DatagenClaimConcurrencyTests(TransactionTestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.user = User.objects.create_user('datagen-concurrent', password='password')
        Profile.objects.create(user=self.user, enabled=True, approver=True)
        self.engine = Engine.objects.create(
            name='concurrent-engine',
            source='https://example.test/archive.zip',
            sha='c' * 40,
            bench=123,
        )
        self.test = Test.objects.create(
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
            datagen_command='datagen {SEED} {COUNT} {THREADS} {OUT}',
            datagen_total_count=2,
            datagen_positions_per_chunk=1,
            datagen_base_seed=100,
            max_games=2,
            throughput=1000,
            approved=True,
        )
        initialize_chunks(self.test)
        self.machines = []
        for idx in range(2):
            worker = User.objects.create_user(
                'concurrent-worker-%d' % idx, password='password'
            )
            Profile.objects.create(user=worker, enabled=True)
            self.machines.append(Machine.objects.create(
                user=worker,
                secret='secret-%d' % idx,
                info={'concurrency': 1, 'client_ver': 38},
            ))

    def tearDown(self):
        self.media_override.disable()
        self.media.cleanup()
        super().tearDown()

    def test_simultaneous_claims_use_distinct_chunks_without_lock_errors(self):
        barrier = threading.Barrier(2)
        local = threading.local()
        original = OpenBench.datagen._next_claim_candidate
        results = [None, None]
        failures = []

        def synchronize_first_read(test, now):
            candidate = original(test, now)
            if not getattr(local, 'synchronized', False):
                local.synchronized = True
                barrier.wait(timeout=5)
            return candidate

        def claim(index):
            close_old_connections()
            try:
                test = Test.objects.get(pk=self.test.pk)
                machine = Machine.objects.get(pk=self.machines[index].pk)
                results[index] = claim_chunk(test, machine)
            except Exception as error:
                failures.append((error, traceback.format_exc()))
            finally:
                close_old_connections()

        with mock.patch.object(
            OpenBench.datagen,
            '_next_claim_candidate',
            side_effect=synchronize_first_read,
        ):
            threads = [threading.Thread(target=claim, args=(idx,)) for idx in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertTrue(all(chunk is not None for chunk in results))
        self.assertEqual(sorted(chunk.idx for chunk in results), [0, 1])
        self.assertEqual(
            DatagenChunk.objects.filter(
                test=self.test, status=DatagenChunk.RUNNING
            ).count(),
            2,
        )
        self.assertEqual(
            sorted(self.test.datagen_chunks.values_list('attempts', flat=True)),
            [1, 1],
        )

    def test_simultaneous_uploads_finish_once_without_losing_progress(self):
        chunks = [
            claim_chunk(self.test, self.machines[index]) for index in range(2)
        ]
        self.assertEqual(sorted(chunk.idx for chunk in chunks), [0, 1])

        barrier = threading.Barrier(2)
        responses = [None, None]
        failures = []

        def submit(index):
            close_old_connections()
            try:
                payload = bz2.compress(('opaque-%d' % index).encode())
                barrier.wait(timeout=5)
                response = Client().post(
                    '/clientSubmitDatagen/',
                    {
                        'machine_id': self.machines[index].id,
                        'secret': self.machines[index].secret,
                        'test_id': self.test.id,
                        'chunk_idx': chunks[index].idx,
                        'sha256': hashlib.sha256(payload).hexdigest(),
                        'bytes': len(payload),
                        'file': SimpleUploadedFile('chunk.bz2', payload),
                    },
                )
                responses[index] = (response.status_code, response.json())
            except Exception as error:
                failures.append((error, traceback.format_exc()))
            finally:
                close_old_connections()

        threads = [threading.Thread(target=submit, args=(idx,)) for idx in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual([status for status, _body in responses], [200, 200])

        self.test.refresh_from_db()
        self.assertEqual(self.test.games, 2)
        self.assertEqual(self.test.datagen_completed_chunks, 2)
        self.assertTrue(self.test.finished)
        self.assertTrue(self.test.passed)
        self.assertEqual(
            self.test.datagen_chunks.filter(
                status=DatagenChunk.COMPLETED
            ).count(),
            2,
        )
        for chunk in chunks:
            self.assertTrue(
                os.path.isfile(os.path.join(self.media.name, chunk.filename()))
            )
