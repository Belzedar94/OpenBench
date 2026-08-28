import bz2
import base64
import copy
import hashlib
import importlib
import json
import os
import tempfile
import threading
import traceback
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections, transaction
from django.test import (
    Client, RequestFactory, TestCase, TransactionTestCase, override_settings,
)
from django.utils import timezone

import OpenBench.datagen
import OpenBench.config
import OpenBench.datagen_publication
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
from OpenBench.models import (
    DatagenChunk, DatagenProducerArtifact, DatagenProducerBuild,
    DatagenProducerOwnerQuota, DatagenProducerQuota, Engine, LogEvent,
    Machine, Network, Profile, Test,
)
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
            'client_ver': OpenBench.config.OPENBENCH_CONFIG['client_version'],
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

    def test_protocol_v39_worker_is_told_to_upgrade_to_active_version(self):
        self.machine.info = dict(self.machine.info, client_ver=39)
        self.machine.save(update_fields=['info'])
        request = RequestFactory().post(
            '/clientGetWorkload/',
            {'machine_id': self.machine.id, 'secret': self.machine.secret},
        )

        response = OpenBench.views.client_get_workload(request)

        error = json.loads(response.content)['error']
        self.assertIn('Bad Client Version', error)
        self.assertIn(
            str(OpenBench.config.OPENBENCH_CONFIG['client_version']), error
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

    def make_test(self, total=4, per_chunk=2, initialize=True, producer=False):
        command = (
            'datagen seed {SEED} count {COUNT} threads {THREADS} '
            'book {BOOK} out {OUT}'
        )
        if producer:
            command += ' producer {PRODUCER_SHA256}'
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
            datagen_command=command,
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

    def make_machine(
        self, username='worker', machine_id=None, atomic=0, manifest=None
    ):
        user = User.objects.create_user(username, password='password')
        Profile.objects.create(user=user, enabled=True)
        info = {
            'supported': ['Spell-Stockfish'],
            'concurrency': 2,
            'physical_cores': 2,
            'sockets': 1,
            'focus': [],
            'client_ver': OpenBench.config.OPENBENCH_CONFIG['client_version'],
            'tablebases': {
                'standard': 0,
                'atomic': {
                    'max': atomic,
                    'manifest_sha256': manifest,
                },
            },
            'syzygy_max': 0,
        }
        return Machine.objects.create(
            id=machine_id,
            user=user,
            secret='secret',
            info=info,
        )

    def make_tablebase_test(
        self, teacher_mode='pure', total=2, maximum=6, initialize=True,
    ):
        manifest = OpenBench.config.OPENBENCH_CONFIG['engines'][
            'Atomic-Stockfish'
        ]['tablebase_manifest_sha256'].lower()
        test = Test.objects.create(
            author=self.user.username,
            book_name='NONE',
            dev=self.engine,
            base=self.engine,
            dev_repo='https://github.com/example/engine',
            base_repo='https://github.com/example/engine',
            dev_engine='Atomic-Stockfish',
            base_engine='Atomic-Stockfish',
            dev_options='',
            base_options='',
            dev_time_control='',
            base_time_control='',
            syzygy_wdl='%d-MAN' % maximum,
            syzygy_adj='DISABLED',
            test_mode='DATAGEN',
            datagen_command=(
                'datagen {SEED} {COUNT} {THREADS} {OUT} '
                'syzygy "{SYZYGY}" '
                'syzygy_manifest_sha256 {SYZYGY_MANIFEST_SHA256} '
                'syzygy_max {SYZYGY_MAX} teacher_mode {TEACHER_MODE}'
            ),
            datagen_total_count=total,
            datagen_positions_per_chunk=total,
            datagen_base_seed=100,
            max_games=total,
            throughput=1000,
            approved=True,
            datagen_tablebase_family='atomic',
            datagen_tablebase_max=maximum,
            datagen_tablebase_manifest_sha256=manifest,
            datagen_teacher_mode=teacher_mode,
        )
        test.freeze_datagen_environment_contract(
            'atomic', maximum, manifest, teacher_mode
        )
        test.save(update_fields=[
            'datagen_tablebase_required',
            'datagen_tablebase_family',
            'datagen_tablebase_max',
            'datagen_tablebase_manifest_sha256',
            'datagen_teacher_mode',
            'datagen_environment_contract_sha256',
        ])
        if initialize:
            initialize_chunks(test)
        return test

    def make_publication_test(
        self,
        campaign_id='atomic-campaign',
        workload_id='opening-train',
        role='train',
        cohort='opening',
        total=2,
        per_chunk=2,
        producer=False,
        tablebase=False,
        book_name='NONE',
    ):
        network_bytes = b'authenticated-network-v41'
        network_sha256 = hashlib.sha256(network_bytes).hexdigest()
        network_id = network_sha256[:8].upper()
        Path(self.media.name, network_id).write_bytes(network_bytes)
        Network.objects.get_or_create(
            engine='Atomic-Stockfish',
            sha256=network_id,
            defaults={
                'name': 'network.nnue',
                'author': self.user.username,
            },
        )
        command = (
            'datagen seed {SEED} count {COUNT} threads {THREADS} '
            'book {BOOK} book_sha256 {BOOK_SHA256} '
            'network {NETWORK} network_sha256 {NETWORK_SHA256}'
        )
        if producer:
            command += ' producer {PRODUCER_SHA256}'
        if tablebase:
            command += (
                ' syzygy {SYZYGY} '
                'syzygy_manifest_sha256 {SYZYGY_MANIFEST_SHA256} '
                'syzygy_max {SYZYGY_MAX} teacher_mode {TEACHER_MODE}'
            )
        command += ' out {OUT}'
        payload = {
            'dev_engine': 'Atomic-Stockfish',
            'dev_repo': 'https://github.com/example/atomic-engine',
            'dev_branch': 'publication-v41',
            'dev_network': network_id,
            'dev_options': 'Hash=512',
            'book_name': book_name,
            'datagen_command': command,
            'datagen_total_count': str(total),
            'datagen_positions_per_chunk': str(per_chunk),
            'datagen_base_seed': '9000',
            'priority': '0',
            'throughput': '1000',
            'datagen_publication_protocol': '41',
            'datagen_campaign_id': campaign_id,
            'datagen_external_workload_id': workload_id,
            'datagen_role': role,
            'datagen_cohort': cohort,
        }
        if tablebase:
            payload.update({
                'datagen_teacher_mode': 'pure',
                'syzygy_wdl': '6-MAN',
            })
        request = RequestFactory().post('/newDatagen/', payload)
        request.user = self.user
        engine_info = (
            (
                'https://example.test/publication-v41.zip',
                'publication-v41',
                'b' * 40,
                456,
            ),
            True,
        )
        with mock.patch.object(
            create_workload, 'verify_workload', return_value=([], engine_info)
        ):
            test, errors = create_workload.create_new_datagen(request)
        return test, errors, network_sha256

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
        self.assertFalse(test.datagen_tablebase_required)
        self.assertEqual(test.datagen_teacher_mode, '')
        self.assertEqual(test.syzygy_wdl, 'DISABLED')
        self.assertEqual(test.syzygy_adj, 'DISABLED')
        self.assertTrue(test.datagen_environment_contract_is_current())
        self.assertEqual(
            list(test.datagen_chunks.values_list('idx', 'position_count')),
            [(idx, 2000) for idx in range(8)],
        )

    def test_creation_freezes_exact_atomic_tablebase_environment(self):
        payload = {
            'dev_engine': 'Atomic-Stockfish',
            'dev_repo': 'https://github.com/example/atomic-engine',
            'dev_branch': 'nnue-v2',
            'dev_network': '',
            'dev_options': '',
            'book_name': 'NONE',
            'datagen_command': (
                'datagen {SEED} {COUNT} {THREADS} {OUT} '
                'syzygy "{SYZYGY}" '
                'syzygy_manifest_sha256 {SYZYGY_MANIFEST_SHA256} '
                'syzygy_max {SYZYGY_MAX} teacher_mode {TEACHER_MODE}'
            ),
            'datagen_total_count': '2000',
            'datagen_positions_per_chunk': '2000',
            'datagen_base_seed': '9000',
            'datagen_teacher_mode': 'pure',
            'syzygy_wdl': '6-MAN',
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

        expected_manifest = OpenBench.config.OPENBENCH_CONFIG['engines'][
            'Atomic-Stockfish'
        ]['tablebase_manifest_sha256'].lower()
        self.assertIsNone(errors)
        self.assertTrue(test.datagen_tablebase_required)
        self.assertEqual(test.datagen_tablebase_family, 'atomic')
        self.assertEqual(test.datagen_tablebase_max, 6)
        self.assertEqual(
            test.datagen_tablebase_manifest_sha256, expected_manifest
        )
        self.assertEqual(test.datagen_teacher_mode, 'pure')
        self.assertEqual(test.syzygy_wdl, '6-MAN')
        self.assertEqual(test.syzygy_adj, 'DISABLED')
        self.assertTrue(test.datagen_environment_contract_is_current())

    def test_v41_creation_freezes_complete_publication_contract(self):
        test, errors, network_sha256 = self.make_publication_test()

        self.assertIsNone(errors)
        contract = test.datagen_publication_contract
        self.assertEqual(
            contract['schema'], 'openbench-datagen-publication-contract-v41'
        )
        self.assertEqual(contract['protocol'], 41)
        self.assertEqual(contract['campaign_id'], 'atomic-campaign')
        self.assertEqual(contract['external_workload_id'], 'opening-train')
        self.assertEqual(contract['role'], 'train')
        self.assertEqual(contract['cohort'], 'opening')
        self.assertNotIn('test_id', contract)
        self.assertEqual(contract['engine']['repo'], test.dev_repo)
        self.assertEqual(contract['engine']['commit'], 'b' * 40)
        self.assertEqual(contract['engine']['bench'], 456)
        self.assertEqual(contract['network']['sha256'], network_sha256)
        self.assertEqual(contract['network']['bytes'], 25)
        self.assertEqual(contract['book']['kind'], 'builtin-startpos')
        self.assertIsNone(contract['book']['raw_sha256'])
        self.assertEqual(
            contract['generation']['command_sha256'],
            hashlib.sha256(test.datagen_command.encode('utf-8')).hexdigest(),
        )
        self.assertEqual(
            contract['generation']['seed_method'],
            'base-plus-chunk-index-v1',
        )
        self.assertFalse(contract['producer']['required'])
        self.assertFalse(contract['syzygy']['required'])
        self.assertEqual(
            test.datagen_publication_contract_sha256,
            OpenBench.datagen_publication.canonical_json_sha256(contract),
        )
        self.assertTrue(test.datagen_publication_contract_is_current())
        self.assertEqual(test.datagen_chunks.count(), 1)

    def test_v41_request_contract_is_explicit_complete_and_slugged(self):
        payload = {
            'datagen_publication_protocol': '41',
            'datagen_campaign_id': 'atomic-20260719',
            'datagen_external_workload_id': 'launch2-opening-train',
            'datagen_role': 'selfplay-train',
            'datagen_cohort': 'launch2-opening',
            'dev_network': 'DEADBEEF',
            'datagen_command': (
                'datagen {BOOK} {BOOK_SHA256} {NETWORK} {NETWORK_SHA256}'
            ),
        }
        self.assertEqual(
            OpenBench.datagen_publication.validate_publication_request(payload),
            [],
        )

        for field in (
            'datagen_campaign_id',
            'datagen_external_workload_id',
            'datagen_role',
            'datagen_cohort',
            'dev_network',
        ):
            malformed = dict(payload, **{field: ''})
            self.assertTrue(
                OpenBench.datagen_publication.validate_publication_request(
                    malformed
                ),
                field,
            )
        malformed = dict(payload, datagen_role='Uppercase')
        self.assertTrue(
            OpenBench.datagen_publication.validate_publication_request(malformed)
        )
        malformed = dict(
            payload,
            datagen_command='datagen {BOOK} {BOOK_SHA256} {NETWORK}',
        )
        self.assertTrue(
            OpenBench.datagen_publication.validate_publication_request(malformed)
        )
        malformed = dict(payload, datagen_publication_protocol='0')
        self.assertTrue(
            OpenBench.datagen_publication.validate_publication_request(malformed)
        )

    def test_v41_creation_rejects_missing_network_bytes_without_rows(self):
        network_bytes = b'missing-after-registration'
        network_id = hashlib.sha256(network_bytes).hexdigest()[:8].upper()
        Network.objects.create(
            engine='Atomic-Stockfish',
            sha256=network_id,
            name='missing.nnue',
            author=self.user.username,
        )
        payload = {
            'dev_engine': 'Atomic-Stockfish',
            'dev_repo': 'https://github.com/example/atomic-engine',
            'dev_branch': 'publication-v41',
            'dev_network': network_id,
            'book_name': 'NONE',
            'datagen_command': (
                'datagen {SEED} {COUNT} {THREADS} {OUT} {BOOK} '
                '{BOOK_SHA256} {NETWORK} {NETWORK_SHA256}'
            ),
            'datagen_total_count': '2',
            'datagen_positions_per_chunk': '2',
            'datagen_base_seed': '1',
            'priority': '0',
            'throughput': '1',
            'datagen_publication_protocol': '41',
            'datagen_campaign_id': 'atomic-campaign',
            'datagen_external_workload_id': 'opening-train',
            'datagen_role': 'train',
            'datagen_cohort': 'opening',
        }
        request = RequestFactory().post('/newDatagen/', payload)
        request.user = self.user
        engine_info = (
            ('https://example.test/v41.zip', 'v41', 'b' * 40, 456), True,
        )

        with mock.patch.object(
            create_workload, 'verify_workload', return_value=([], engine_info)
        ):
            test, errors = create_workload.create_new_datagen(request)

        self.assertIsNone(test)
        self.assertIn('network bytes are unavailable', errors[0])
        self.assertEqual(Test.objects.count(), 0)
        self.assertEqual(DatagenChunk.objects.count(), 0)

    def test_v41_freezes_both_book_identities_and_rejects_missing_raw_sha(self):
        complete = {
            'sha': '1' * 64,
            'raw_sha': '2' * 64,
            'source': 'https://example.test/publication.epd.zip',
        }
        with mock.patch.dict(
            OpenBench.config.OPENBENCH_CONFIG['books'],
            {'publication.epd': complete},
        ):
            test, errors, _ = self.make_publication_test(
                book_name='publication.epd'
            )
        self.assertIsNone(errors)
        self.assertEqual(
            test.datagen_publication_contract['book'],
            {
                'kind': 'file',
                'name': 'publication.epd',
                'source': complete['source'],
                'text_sha256': complete['sha'],
                'raw_sha256': complete['raw_sha'],
            },
        )

        Test.objects.all().delete()
        incomplete = {
            'sha': '1' * 64,
            'source': 'https://example.test/incomplete.epd.zip',
        }
        with mock.patch.dict(
            OpenBench.config.OPENBENCH_CONFIG['books'],
            {'incomplete.epd': incomplete},
        ):
            rejected, rejected_errors, _ = self.make_publication_test(
                book_name='incomplete.epd',
                workload_id='opening-validation',
                role='validation',
            )
        self.assertIsNone(rejected)
        self.assertIn('raw SHA-256', rejected_errors[0])

    def test_v41_campaign_slots_are_unique_and_legacy_rows_are_exempt(self):
        test, errors, _ = self.make_publication_test()
        self.assertIsNone(errors)

        duplicate_external = copy.copy(test)
        duplicate_external.pk = None
        duplicate_external.id = None
        duplicate_external._state.adding = True
        duplicate_external.datagen_role = 'validation'
        duplicate_external.datagen_cohort = 'midgame'
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate_external.save(force_insert=True)

        duplicate_slot = copy.copy(test)
        duplicate_slot.pk = None
        duplicate_slot.id = None
        duplicate_slot._state.adding = True
        duplicate_slot.datagen_external_workload_id = 'different-workload'
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate_slot.save(force_insert=True)

        first = self.make_test()
        second = self.make_test()
        self.assertEqual(first.datagen_publication_protocol, 0)
        self.assertEqual(second.datagen_publication_protocol, 0)

    def test_v41_semantic_mutation_fails_closed_but_tuning_does_not(self):
        test, errors, _ = self.make_publication_test()
        self.assertIsNone(errors)
        test.priority += 1
        test.throughput += 1
        self.assertTrue(test.datagen_publication_contract_is_current())

        original = test.datagen_command
        test.datagen_command += ' drift'
        self.assertFalse(test.datagen_publication_contract_is_current())
        self.assertIsNone(claim_chunk(test, self.make_machine()))
        test.datagen_command = original
        self.assertTrue(test.datagen_publication_contract_is_current())

        contract = copy.deepcopy(test.datagen_publication_contract)
        contract['network']['bytes'] += 1
        test.datagen_publication_contract = contract
        test.datagen_publication_contract_sha256 = (
            OpenBench.datagen_publication.canonical_json_sha256(contract)
        )
        self.assertFalse(test.datagen_publication_contract_is_current())

        test.datagen_publication_contract = copy.deepcopy(
            Test.objects.get(pk=test.pk).datagen_publication_contract
        )
        test.datagen_publication_contract_sha256 = (
            OpenBench.datagen_publication.canonical_json_sha256(
                test.datagen_publication_contract
            )
        )
        test.datagen_book_source = 'unexpected-source'
        self.assertFalse(test.datagen_publication_contract_is_current())

    def test_v41_canonical_json_has_a_fixed_order_independent_vector(self):
        document = {
            'external_workload_id': 'opening-train',
            'schema': 'openbench-datagen-publication-contract-v41',
            'protocol': 41,
            'campaign_id': 'atomic-20260719',
        }
        expected = '26d0aff3562181c97d0de8ee3fac81bc9ea842792fd5055ed1d1876e481541fc'
        self.assertEqual(
            OpenBench.datagen_publication.canonical_json_sha256(document),
            expected,
        )
        self.assertEqual(
            OpenBench.datagen_publication.canonical_json_sha256(
                dict(reversed(list(document.items())))
            ),
            expected,
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

    def test_private_engine_requires_explicit_datagen_artifact_role(self):
        request = SimpleNamespace(POST={'dev_engine': 'PrivateEngine'})
        errors = []
        private = {
            'PrivateEngine': {
                'private': True,
                'build': {'artifact_roles': ['play']},
            }
        }
        with mock.patch.dict(
            OpenBench.config.OPENBENCH_CONFIG['engines'], private, clear=False
        ):
            verify_workload.verify_datagen_engine_role(
                errors, request, 'dev_engine'
            )

        self.assertEqual(len(errors), 1)
        self.assertIn('explicit datagen artifact role', errors[0])

        private['PrivateEngine']['build']['artifact_roles'].append('datagen')
        errors = []
        with mock.patch.dict(
            OpenBench.config.OPENBENCH_CONFIG['engines'], private, clear=False
        ):
            verify_workload.verify_datagen_engine_role(
                errors, request, 'dev_engine'
            )
        self.assertEqual(errors, [])

    def test_private_artifact_role_discovery_is_fail_closed(self):
        legacy = [{'name': 'horde-linux-avx2-pext'}]
        tagged = [
            {'name': 'horde-linux-avx2-pext-play'},
            {'name': 'horde-linux-avx2-pext-datagen'},
        ]

        self.assertTrue(
            verify_workload.artifacts_support_role(legacy, 'play')
        )
        self.assertFalse(
            verify_workload.artifacts_support_role(legacy, 'datagen')
        )
        self.assertTrue(
            verify_workload.artifacts_support_role(tagged, 'play')
        )
        self.assertTrue(
            verify_workload.artifacts_support_role(tagged, 'datagen')
        )

    def test_datagen_rejects_a_gameplay_only_book_before_scheduling(self):
        request = SimpleNamespace(POST={'book_name': 'gameplay-only.epd'})
        books = {
            'gameplay-only.epd': {'datagen_enabled': False},
            'training.epd': {},
        }

        with mock.patch.dict(
            OpenBench.config.OPENBENCH_CONFIG['books'], books, clear=True
        ):
            errors = []
            verify_workload.verify_datagen_book(
                errors, request, 'book_name', 'Book', 'books'
            )
            self.assertEqual(errors, ['Book is not enabled for DATAGEN'])

            request.POST['book_name'] = 'training.epd'
            errors = []
            verify_workload.verify_datagen_book(
                errors, request, 'book_name', 'Book', 'books'
            )
            self.assertEqual(errors, [])

    def test_cross_engine_variant_contract_must_match(self):
        request = SimpleNamespace(POST={
            'dev_engine': 'Horde-Stockfish',
            'base_engine': 'Horde-Baseline',
            'book_name': 'HORDE_openings.epd',
        })
        configured = {
            'Horde-Stockfish': {'variant_contract': 'LICHESS_HORDE_V1'},
            'Horde-Baseline': {'variant_contract': 'LICHESS_HORDE_V1'},
        }
        books = {
            'HORDE_openings.epd': {'variant_contract': 'LICHESS_HORDE_V1'},
        }
        errors = []
        with mock.patch.dict(OpenBench.config.OPENBENCH_CONFIG['engines'], configured, clear=False), \
             mock.patch.dict(OpenBench.config.OPENBENCH_CONFIG['books'], books, clear=False):
            verify_workload.verify_matching_variant_contracts(
                errors, request, 'dev_engine', 'base_engine', 'book_name'
            )
        self.assertEqual(errors, [])

        configured['Horde-Baseline']['variant_contract'] = 'ATOMIC_V1'
        with mock.patch.dict(OpenBench.config.OPENBENCH_CONFIG['engines'], configured, clear=False), \
             mock.patch.dict(OpenBench.config.OPENBENCH_CONFIG['books'], books, clear=False):
            verify_workload.verify_matching_variant_contracts(
                errors, request, 'dev_engine', 'base_engine', 'book_name'
            )
        self.assertIn(
            'variant contracts disagree', errors[-1]
        )

    def test_workload_variant_contract_is_propagated_fail_closed(self):
        test = SimpleNamespace(
            dev_engine='Horde-Stockfish',
            base_engine='Horde-Baseline',
            book_name='HORDE_openings.epd',
            variant_contract='LICHESS_HORDE_V1',
        )
        configured = {
            'engines': {
                'Horde-Stockfish': {'variant_contract': 'LICHESS_HORDE_V1'},
                'Horde-Baseline': {'variant_contract': 'LICHESS_HORDE_V1'},
            },
            'books': {
                'HORDE_openings.epd': {'variant_contract': 'LICHESS_HORDE_V1'},
            },
        }
        with mock.patch.object(get_workload, 'OPENBENCH_CONFIG', configured):
            self.assertEqual(
                get_workload.workload_variant_contract(test), 'LICHESS_HORDE_V1'
            )
            configured['engines']['Horde-Baseline'][
                'variant_contract'
            ] = 'ATOMIC_V1'
            with self.assertRaisesRegex(ValueError, 'contracts disagree'):
                get_workload.workload_variant_contract(test)

            configured['engines']['Horde-Baseline'][
                'variant_contract'
            ] = 'LICHESS_HORDE_V1'
            test.variant_contract = ''
            with self.assertRaisesRegex(ValueError, 'not persisted'):
                get_workload.workload_variant_contract(test)

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

        self.assertFalse(
            renew_chunk(test.id, reclaimed.idx, first, original.attempts)
        )
        self.assertFalse(
            requeue_chunk(
                test.id, reclaimed.idx, first, original.attempts,
                'late stale error',
            )
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
                'attempt': original.attempts,
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
                    'attempt': chunk.attempts,
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
                    'attempt': chunk.attempts,
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
                    'attempt': chunk.attempts,
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
                'attempt': chunk.attempts,
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

    def test_tablebase_template_requires_complete_group_explicit_limit_and_teacher(self):
        base = {
            'dev_engine': 'Atomic-Stockfish',
            'syzygy_wdl': '6-MAN',
            'datagen_teacher_mode': 'pure',
        }
        full_command = (
            'datagen {SEED} {COUNT} {THREADS} {OUT} '
            '{SYZYGY} {SYZYGY_MANIFEST_SHA256} {SYZYGY_MAX} '
            '{TEACHER_MODE}'
        )
        request = SimpleNamespace(POST=dict(
            base, datagen_command=full_command
        ))
        errors = []
        verify_workload.verify_datagen_template(
            errors, request, 'datagen_command'
        )
        verify_workload.verify_datagen_tablebase_contract(errors, request)
        self.assertEqual(errors, [])

        partial = SimpleNamespace(POST=dict(
            base,
            datagen_command=(
                'datagen {SEED} {COUNT} {THREADS} {OUT} {SYZYGY}'
            ),
        ))
        errors = []
        verify_workload.verify_datagen_tablebase_contract(errors, partial)
        self.assertIn('use {SYZYGY}', errors[0])

        missing_teacher_placeholder = SimpleNamespace(POST=dict(
            base,
            datagen_command=(
                'datagen {SEED} {COUNT} {THREADS} {OUT} '
                '{SYZYGY} {SYZYGY_MANIFEST_SHA256} {SYZYGY_MAX}'
            ),
        ))
        errors = []
        verify_workload.verify_datagen_tablebase_contract(
            errors, missing_teacher_placeholder
        )
        self.assertTrue(any('{TEACHER_MODE}' in error for error in errors))

        teacher_without_tablebases = SimpleNamespace(POST=dict(
            base,
            datagen_command=(
                'datagen {SEED} {COUNT} {THREADS} {OUT} {TEACHER_MODE}'
            ),
        ))
        errors = []
        verify_workload.verify_datagen_tablebase_contract(
            errors, teacher_without_tablebases
        )
        self.assertTrue(any('{SYZYGY}' in error for error in errors))

        optional = SimpleNamespace(POST=dict(
            base, datagen_command=full_command, syzygy_wdl='OPTIONAL'
        ))
        errors = []
        verify_workload.verify_datagen_tablebase_contract(errors, optional)
        self.assertIn('explicit 3-MAN through 6-MAN', errors[0])

        seven_man = SimpleNamespace(POST=dict(
            base, datagen_command=full_command, syzygy_wdl='7-MAN'
        ))
        errors = []
        verify_workload.verify_datagen_tablebase_contract(errors, seven_man)
        self.assertIn('3-MAN through 6-MAN', errors[0])

        ambiguous_teacher = SimpleNamespace(POST=dict(
            base, datagen_command=full_command, datagen_teacher_mode=''
        ))
        errors = []
        verify_workload.verify_datagen_tablebase_contract(
            errors, ambiguous_teacher
        )
        self.assertTrue(any('pure or true' in error for error in errors))

        semantic_default = SimpleNamespace(POST={
            'dev_engine': 'Atomic-Stockfish',
            'datagen_command': (
                'datagen {SEED} {COUNT} {THREADS} {OUT}'
            ),
            'datagen_teacher_mode': 'none',
        })
        errors = []
        verify_workload.verify_datagen_tablebase_contract(
            errors, semantic_default
        )
        self.assertTrue(any('{TEACHER_MODE}' in error for error in errors))

    def test_atomic_v40_rejects_seven_man_at_creation_schedule_and_lease(self):
        with self.assertRaisesRegex(ValueError, '3-MAN through 6-MAN'):
            self.make_tablebase_test(maximum=7)

        test = self.make_tablebase_test()
        test.syzygy_wdl = '7-MAN'
        test.freeze_datagen_environment_contract(
            'atomic', 7, test.datagen_tablebase_manifest_sha256, 'pure'
        )
        test.save(update_fields=[
            'syzygy_wdl', 'datagen_tablebase_required',
            'datagen_tablebase_family', 'datagen_tablebase_max',
            'datagen_tablebase_manifest_sha256', 'datagen_teacher_mode',
            'datagen_environment_contract_sha256',
        ])
        machine = self.make_machine(
            atomic=7, manifest=test.datagen_tablebase_manifest_sha256
        )
        self.assertFalse(get_workload.valid_tablebase_assignment(test, machine))
        with self.assertRaisesRegex(ValueError, 'capability does not match'):
            OpenBench.datagen.tablebase_lease(test, machine, 0, 1)

    def test_tablebase_chunk_freezes_lease_and_completed_manifest_receipt(self):
        test = self.make_tablebase_test(teacher_mode='true')
        manifest_sha = test.datagen_tablebase_manifest_sha256
        machine = self.make_machine(
            atomic=6, manifest=manifest_sha
        )
        chunk = claim_chunk(test, machine)
        self.assertIsNotNone(chunk)
        self.assertEqual(
            chunk.environment_lease['tablebase']['manifest_sha256'],
            manifest_sha,
        )

        # Machine.info is mutable operational state. The upload is authorized
        # against the immutable lease captured above, not this later mutation.
        machine.info['tablebases']['atomic'] = {
            'max': 5,
            'manifest_sha256': 'd' * 64,
        }
        machine.save(update_fields=['info'])
        response = self.submit_chunk(
            test, chunk, machine, tablebase=True
        )
        self.assertEqual(response.status_code, 200, response.content)
        chunk.refresh_from_db()
        self.assertRegex(chunk.environment_receipt_sha256, r'^[0-9a-f]{64}$')
        self.assertEqual(
            chunk.environment_receipt['environment_lease_sha256'],
            chunk.environment_lease_sha256,
        )
        self.assertNotIn('path', json.dumps(chunk.environment_receipt).lower())

        receipt_sha = chunk.environment_receipt_sha256
        retry = self.submit_chunk(test, chunk, machine, tablebase=True)
        self.assertEqual(retry.status_code, 200, retry.content)
        test.refresh_from_db()
        chunk.refresh_from_db()
        self.assertEqual(test.datagen_completed_chunks, 1)
        self.assertEqual(chunk.environment_receipt_sha256, receipt_sha)

        authorization = 'Basic ' + base64.b64encode(
            b'datagen:password'
        ).decode('ascii')
        document = Client(HTTP_AUTHORIZATION=authorization).get(
            '/api/datagen/%d/' % test.id, secure=True
        ).json()
        self.assertTrue(document['environment']['tablebase_required'])
        self.assertEqual(
            document['chunks'][0]['environment_receipt_sha256'],
            chunk.environment_receipt_sha256,
        )
        self.assertNotIn('path', json.dumps(document).lower())
        self.assertNotIn('combined', json.dumps(document).lower())

        chunk.environment_receipt['artifact']['sha256'] = '0' * 64
        chunk.save(update_fields=['environment_receipt'])
        rejected = Client(HTTP_AUTHORIZATION=authorization).get(
            '/api/datagen/%d/' % test.id, secure=True
        ).json()
        self.assertIn('inconsistent tablebase receipt', rejected['error'])

    def test_v41_chunk_receipt_and_manifest_bind_the_publication_contract(self):
        test, errors, _ = self.make_publication_test()
        self.assertIsNone(errors)
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)

        self.assertEqual(
            chunk.environment_lease['schema'],
            'openbench-datagen-publication-lease-v41',
        )
        self.assertEqual(chunk.environment_lease['protocol'], 41)
        self.assertEqual(
            chunk.environment_lease['publication_contract_sha256'],
            test.datagen_publication_contract_sha256,
        )
        self.assertFalse(chunk.environment_lease['tablebase']['required'])
        self.assertEqual(
            chunk.environment_lease_sha256,
            OpenBench.datagen_publication.canonical_json_sha256(
                chunk.environment_lease
            ),
        )

        response = self.submit_chunk(test, chunk, machine)
        self.assertEqual(response.status_code, 200, response.content)
        test.refresh_from_db()
        chunk.refresh_from_db()
        receipt = chunk.environment_receipt
        self.assertEqual(
            receipt['schema'], 'openbench-datagen-publication-receipt-v41'
        )
        self.assertEqual(
            receipt['publication_contract_sha256'],
            test.datagen_publication_contract_sha256,
        )
        self.assertEqual(
            receipt['environment_lease_sha256'],
            chunk.environment_lease_sha256,
        )
        self.assertEqual(receipt['artifact']['sha256'], chunk.sha256)
        self.assertNotIn('path', json.dumps(receipt).lower())

        authorization = 'Basic ' + base64.b64encode(
            b'datagen:password'
        ).decode('ascii')
        client = Client(HTTP_AUTHORIZATION=authorization)
        document = client.get(
            '/api/datagen/%d/' % test.id, secure=True
        ).json()
        self.assertEqual(
            document['schema'],
            'openbench-datagen-publication-manifest-v41',
        )
        self.assertEqual(document['version'], 1)
        self.assertEqual(document['protocol'], 41)
        self.assertEqual(
            document['publication_contract'],
            test.datagen_publication_contract,
        )
        manifest_sha256 = document.pop('manifest_sha256')
        self.assertEqual(
            manifest_sha256,
            OpenBench.datagen_publication.canonical_json_sha256(document),
        )

        receipt['publication_contract_sha256'] = 'd' * 64
        chunk.environment_receipt = receipt
        chunk.environment_receipt_sha256 = (
            OpenBench.datagen_publication.canonical_json_sha256(receipt)
        )
        chunk.save(update_fields=[
            'environment_receipt', 'environment_receipt_sha256',
        ])
        rejected = client.get(
            '/api/datagen/%d/' % test.id, secure=True
        ).json()
        self.assertIn('inconsistent publication receipt', rejected['error'])

    def test_v41_upload_rejects_wrong_publication_binding_before_hashing(self):
        test, errors, _ = self.make_publication_test()
        self.assertIsNone(errors)
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)

        with mock.patch.object(
            OpenBench.views,
            '_datagen_uploaded_digest',
            side_effect=AssertionError('upload body must not be hashed'),
        ) as digest:
            response = self.submit_chunk(
                test,
                chunk,
                machine,
                tablebase_overrides={
                    'publication_contract_sha256': 'd' * 64,
                },
            )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn('publication attestation', response.json()['error'])
        digest.assert_not_called()

    def test_v41_tablebase_receipt_binds_teacher_syzygy_and_producer(self):
        test, errors, _ = self.make_publication_test(
            producer=True, tablebase=True
        )
        self.assertIsNone(errors)
        contract = test.datagen_publication_contract
        self.assertTrue(contract['producer']['required'])
        self.assertEqual(contract['teacher']['mode'], 'pure')
        self.assertEqual(contract['syzygy']['family'], 'atomic')
        self.assertEqual(contract['syzygy']['max'], 6)
        self.assertRegex(
            contract['syzygy']['manifest_sha256'], r'^[0-9a-f]{64}$'
        )

        machine = self.make_machine(
            atomic=6,
            manifest=test.datagen_tablebase_manifest_sha256,
        )
        chunk = claim_chunk(test, machine)
        registered = self.register_producer(test, chunk, machine).json()
        self.assertRegex(registered['sha256'], r'^[0-9a-f]{64}$')
        response = self.submit_chunk(
            test, chunk, machine, registered, tablebase=True
        )
        self.assertEqual(response.status_code, 200, response.content)

        chunk.refresh_from_db()
        receipt = chunk.environment_receipt
        self.assertEqual(receipt['teacher_mode'], 'pure')
        self.assertTrue(receipt['tablebase']['required'])
        self.assertEqual(
            receipt['tablebase']['manifest_sha256'],
            test.datagen_tablebase_manifest_sha256,
        )
        self.assertEqual(receipt['producer']['sha256'], registered['sha256'])
        self.assertEqual(
            receipt['publication_contract_sha256'],
            test.datagen_publication_contract_sha256,
        )

    def test_manifest_rejects_every_drifted_lease_and_receipt_binding(self):
        test = self.make_tablebase_test(teacher_mode='true')
        machine = self.make_machine(
            atomic=6, manifest=test.datagen_tablebase_manifest_sha256
        )
        chunk = claim_chunk(test, machine)
        self.assertEqual(
            self.submit_chunk(test, chunk, machine, tablebase=True).status_code,
            200,
        )
        chunk.refresh_from_db()
        original_lease = copy.deepcopy(chunk.environment_lease)
        original_lease_sha = chunk.environment_lease_sha256
        original_receipt = copy.deepcopy(chunk.environment_receipt)
        original_receipt_sha = chunk.environment_receipt_sha256
        client = Client()
        client.force_login(self.user)

        lease_mutations = [
            (('schema',), 'openbench-datagen-tablebase-lease-v41'),
            (('protocol',), 41),
            (('test_id',), test.id + 1),
            (('chunk_idx',), chunk.idx + 1),
            (('attempt',), chunk.attempts + 1),
            (('machine_id',), machine.id + 1),
            (('environment_contract_sha256',), 'd' * 64),
            (('tablebase', 'family'), 'standard'),
            (('tablebase', 'required_max'), 5),
            (('tablebase', 'worker_max'), 5),
            (('tablebase', 'manifest_sha256'), 'd' * 64),
            (('teacher_mode',), 'pure'),
        ]
        for path, value in lease_mutations:
            with self.subTest(evidence='lease', field='.'.join(path)):
                lease = copy.deepcopy(original_lease)
                target = lease
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                lease_sha = OpenBench.views._canonical_json_sha256(lease)
                receipt = copy.deepcopy(original_receipt)
                receipt['environment_lease_sha256'] = lease_sha
                receipt_sha = OpenBench.views._canonical_json_sha256(receipt)
                DatagenChunk.objects.filter(pk=chunk.pk).update(
                    environment_lease=lease,
                    environment_lease_sha256=lease_sha,
                    environment_receipt=receipt,
                    environment_receipt_sha256=receipt_sha,
                )
                rejected = client.get('/api/datagen/%d/' % test.id).json()
                self.assertIn(
                    'inconsistent tablebase lease', rejected['error']
                )

        DatagenChunk.objects.filter(pk=chunk.pk).update(
            environment_lease=original_lease,
            environment_lease_sha256=original_lease_sha,
        )
        receipt_mutations = [
            (('schema',), 'openbench-datagen-tablebase-receipt-v41'),
            (('protocol',), 41),
            (('test_id',), test.id + 1),
            (('chunk_idx',), chunk.idx + 1),
            (('attempt',), chunk.attempts + 1),
            (('machine_id',), machine.id + 1),
            (('environment_contract_sha256',), 'd' * 64),
            (('environment_lease_sha256',), 'd' * 64),
            (('tablebase', 'family'), 'standard'),
            (('tablebase', 'required_max'), 5),
            (('tablebase', 'worker_max'), 5),
            (('tablebase', 'manifest_sha256'), 'd' * 64),
            (('teacher_mode',), 'pure'),
            (('artifact', 'sha256'), 'd' * 64),
            (('artifact', 'bytes'), original_receipt['artifact']['bytes'] + 1),
            (('producer',), {'sha256': 'd' * 64, 'bytes': 1, 'commit': 'e' * 40}),
        ]
        for path, value in receipt_mutations:
            with self.subTest(evidence='receipt', field='.'.join(path)):
                receipt = copy.deepcopy(original_receipt)
                target = receipt
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                DatagenChunk.objects.filter(pk=chunk.pk).update(
                    environment_receipt=receipt,
                    environment_receipt_sha256=(
                        OpenBench.views._canonical_json_sha256(receipt)
                    ),
                )
                rejected = client.get('/api/datagen/%d/' % test.id).json()
                self.assertIn(
                    'inconsistent tablebase receipt', rejected['error']
                )

        DatagenChunk.objects.filter(pk=chunk.pk).update(
            environment_receipt=original_receipt,
            environment_receipt_sha256=original_receipt_sha,
        )

        for malformed_sha in (None, 42):
            with self.subTest(evidence='lease-sha-type', value=malformed_sha):
                chunk.environment_lease = copy.deepcopy(original_lease)
                chunk.environment_lease_sha256 = malformed_sha
                with self.assertRaisesRegex(
                    PermissionError,
                    'lease does not match campaign or worker',
                ):
                    OpenBench.views._frozen_datagen_tablebase_attestation(
                        test, chunk, machine
                    )

    def test_tablebase_scheduler_requires_exact_family_limit_and_pin(self):
        test = self.make_tablebase_test()
        manifest = test.datagen_tablebase_manifest_sha256

        self.assertTrue(get_workload.valid_tablebase_assignment(
            test, self.make_machine(atomic=6, manifest=manifest)
        ))
        self.assertFalse(get_workload.valid_tablebase_assignment(
            test, self.make_machine(
                username='too-small', atomic=5, manifest=manifest
            )
        ))
        self.assertFalse(get_workload.valid_tablebase_assignment(
            test, self.make_machine(
                username='wrong-pin', atomic=6, manifest='d' * 64
            )
        ))
        orthodox = self.make_machine(
            username='orthodox-only', atomic=0, manifest=None
        )
        orthodox.info['tablebases']['standard'] = 7
        orthodox.info['syzygy_max'] = 7
        orthodox.save(update_fields=['info'])
        self.assertFalse(
            get_workload.valid_tablebase_assignment(test, orthodox)
        )

    def test_tablebase_contract_mutation_stops_future_claims(self):
        test = self.make_tablebase_test()
        machine = self.make_machine(
            atomic=6, manifest=test.datagen_tablebase_manifest_sha256
        )
        self.assertTrue(test.datagen_environment_contract_is_current())

        test.datagen_teacher_mode = 'true'
        test.save(update_fields=['datagen_teacher_mode'])
        self.assertFalse(test.datagen_environment_contract_is_current())
        self.assertIsNone(claim_chunk(test, machine))

    def test_tablebase_upload_fails_before_hashing_when_attestation_is_missing(self):
        test = self.make_tablebase_test()
        machine = self.make_machine(
            atomic=6, manifest=test.datagen_tablebase_manifest_sha256
        )
        chunk = claim_chunk(test, machine)
        with mock.patch.object(
            OpenBench.views,
            '_datagen_uploaded_digest',
            side_effect=AssertionError('must reject before hashing'),
        ):
            response = self.submit_chunk(test, chunk, machine)
        self.assertEqual(response.status_code, 400)
        self.assertIn('omitted tablebase', response.json()['error'])

        request_mutations = {
            'environment_contract_sha256': 'd' * 64,
            'environment_lease_sha256': 'd' * 64,
            'tablebase_family': 'standard',
            'tablebase_max': 5,
            'tablebase_worker_max': 7,
            'tablebase_manifest_sha256': 'd' * 64,
            'teacher_mode': 'true',
        }
        for field, value in request_mutations.items():
            with self.subTest(field=field), mock.patch.object(
                OpenBench.views,
                '_datagen_uploaded_digest',
                side_effect=AssertionError('must reject before hashing'),
            ):
                response = self.submit_chunk(
                    test,
                    chunk,
                    machine,
                    tablebase=True,
                    tablebase_overrides={field: value},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn('does not match', response.json()['error'])

    def test_requeue_clears_frozen_tablebase_attempt_evidence(self):
        test = self.make_tablebase_test()
        machine = self.make_machine(
            atomic=6, manifest=test.datagen_tablebase_manifest_sha256
        )
        chunk = claim_chunk(test, machine)
        first_lease_sha = chunk.environment_lease_sha256
        self.assertTrue(first_lease_sha)
        self.assertTrue(
            requeue_chunk(
                test.id, chunk.idx, machine, chunk.attempts, 'retry'
            )
        )
        chunk.refresh_from_db()
        self.assertEqual(chunk.environment_lease, {})
        self.assertEqual(chunk.environment_lease_sha256, '')
        self.assertEqual(chunk.environment_receipt, {})

        second = claim_chunk(test, machine)
        self.assertNotEqual(second.environment_lease_sha256, first_lease_sha)
        self.assertEqual(second.environment_lease['attempt'], 2)

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

    def producer_test(self, total=2, per_chunk=2):
        return self.make_test(
            total=total, per_chunk=per_chunk, producer=True
        )

    def register_producer(
        self, test, chunk, machine, payload=b'producer-binary', commit=None,
        sha256=None, byte_count=None, attempt=None, metadata_only=False,
    ):
        fields = {
            'machine_id': machine.id,
            'secret': machine.secret,
            'test_id': test.id,
            'chunk_idx': chunk.idx,
            'attempt': chunk.attempts if attempt is None else attempt,
            'sha256': sha256 or hashlib.sha256(payload).hexdigest(),
            'bytes': len(payload) if byte_count is None else byte_count,
            'commit': commit or test.dev.sha,
            'metadata_only': int(metadata_only),
        }
        if not metadata_only:
            fields['file'] = SimpleUploadedFile('producer.bin', payload)
        return Client().post(
            '/clientSubmitDatagenProducer/',
            fields,
        )

    def submit_chunk(
        self, test, chunk, machine, producer=None, attempt=None,
        tablebase=False, tablebase_overrides=None,
    ):
        payload = bz2.compress(b'opaque-chunk')
        fields = {
            'machine_id': machine.id,
            'secret': machine.secret,
            'test_id': test.id,
            'chunk_idx': chunk.idx,
            'attempt': chunk.attempts if attempt is None else attempt,
            'sha256': hashlib.sha256(payload).hexdigest(),
            'bytes': len(payload),
            'file': SimpleUploadedFile('chunk.bz2', payload),
        }
        if producer is not None:
            fields.update({
                'producer_sha256': producer['sha256'],
                'producer_bytes': producer['bytes'],
                'producer_commit': producer['commit'],
            })
        if tablebase or test.is_publication_datagen():
            lease = chunk.environment_lease
            tablebase_lease = lease['tablebase']
            fields.update({
                'environment_contract_sha256': (
                    lease['environment_contract_sha256']
                ),
                'environment_lease_sha256': (
                    chunk.environment_lease_sha256
                ),
                'tablebase_family': tablebase_lease['family'] or '',
                'tablebase_max': tablebase_lease['required_max'],
                'tablebase_worker_max': tablebase_lease['worker_max'],
                'tablebase_manifest_sha256': (
                    tablebase_lease['manifest_sha256'] or ''
                ),
                'teacher_mode': lease['teacher_mode'] or '',
            })
            if test.is_publication_datagen():
                fields['publication_contract_sha256'] = lease[
                    'publication_contract_sha256'
                ]
            fields.update(tablebase_overrides or {})
        return Client().post('/clientSubmitDatagen/', fields)

    def test_template_and_workload_opt_in_to_producer_evidence(self):
        request = SimpleNamespace(POST={'datagen_command': (
            'datagen {SEED} {COUNT} {THREADS} {OUT} '
            'producer {PRODUCER_SHA256}'
        )})
        errors = []
        verify_workload.verify_datagen_template(
            errors, request, 'datagen_command'
        )
        self.assertEqual(errors, [])

        test = self.producer_test()
        machine = self.make_machine()
        chunk = test.datagen_chunks.get(idx=0)
        workload = get_workload.workload_to_dictionary(
            test, SimpleNamespace(id=1), machine, chunk
        )
        self.assertTrue(
            workload['test']['datagen']['producer_artifact_required']
        )
        self.assertEqual(
            workload['test']['datagen']['producer_contract_sha256'],
            test.datagen_producer_contract_sha256,
        )
        frozen_contract = test.datagen_producer_contract_sha256

        test.datagen_command = (
            'datagen {SEED} {COUNT} {THREADS} {OUT} '
            'literal {{PRODUCER_SHA256}}'
        )
        test.save(update_fields=['datagen_command'])
        self.assertTrue(test.datagen_requires_producer_artifact())
        test.refresh_from_db()
        self.assertEqual(
            test.datagen_producer_contract_sha256, frozen_contract
        )
        self.assertFalse(test.datagen_producer_contract_is_current())
        self.assertFalse(OpenBench.datagen.has_assignable_chunk(test))

    def test_producer_is_rehashed_stored_and_bound_before_generation(self):
        test = self.producer_test()
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        payload = b'producer-binary'
        sha256 = hashlib.sha256(payload).hexdigest()

        response = self.register_producer(test, chunk, machine, payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['sha256'], sha256)
        chunk.refresh_from_db()
        self.assertEqual(chunk.producer_sha256, sha256)
        self.assertEqual(chunk.producer_bytes, len(payload))
        self.assertEqual(chunk.producer_commit, self.engine.sha)
        artifact = DatagenProducerArtifact.objects.get(sha256=sha256)
        self.assertEqual(artifact.bytes, len(payload))
        self.assertEqual(
            Path(self.media.name, artifact.filename()).read_bytes(), payload
        )

    def test_metadata_probe_requests_upload_without_binding_missing_cas(self):
        test = self.producer_test()
        machine = self.make_machine('metadata-miss')
        chunk = claim_chunk(test, machine)

        response = self.register_producer(
            test, chunk, machine, metadata_only=True
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['upload_required'])
        chunk.refresh_from_db()
        self.assertEqual(chunk.producer_sha256, '')
        self.assertEqual(DatagenProducerArtifact.objects.count(), 0)

    def test_producer_content_length_is_rejected_before_multipart_parsing(self):
        request = RequestFactory().generic(
            'POST',
            '/clientSubmitDatagenProducer/',
            b'',
            content_type='multipart/form-data; boundary=early-limit',
            CONTENT_LENGTH=str(
                OpenBench.datagen.MAX_DATAGEN_PRODUCER_REQUEST_BYTES + 1
            ),
        )
        response = OpenBench.views.client_submit_datagen_producer(request)
        self.assertEqual(response.status_code, 413)
        self.assertIn('size limit', response.content.decode('utf-8'))

    def test_producer_storage_failure_has_no_database_side_effects(self):
        test = self.producer_test()
        machine = self.make_machine('producer-storage-failure')
        chunk = claim_chunk(test, machine)

        with mock.patch.object(
            OpenBench.views.FileSystemStorage,
            'save',
            side_effect=OSError('storage unavailable'),
        ):
            response = self.register_producer(test, chunk, machine)

        self.assertEqual(response.status_code, 500, response.content)
        chunk.refresh_from_db()
        self.assertEqual(chunk.producer_sha256, '')
        self.assertEqual(DatagenProducerArtifact.objects.count(), 0)

    def test_failed_promotion_is_repaired_by_authenticated_retry(self):
        test = self.producer_test()
        machine = self.make_machine('producer-promotion-retry')
        chunk = claim_chunk(test, machine)
        real_replace = OpenBench.views.os.replace

        with mock.patch.object(
            OpenBench.views.os,
            'replace',
            side_effect=OSError('rename unavailable'),
        ):
            failed = self.register_producer(test, chunk, machine)

        self.assertEqual(failed.status_code, 500, failed.content)
        chunk.refresh_from_db()
        self.assertNotEqual(chunk.producer_sha256, '')
        artifact = DatagenProducerArtifact.objects.get(
            sha256=chunk.producer_sha256
        )
        self.assertFalse(Path(self.media.name, artifact.filename()).exists())

        with mock.patch.object(
            OpenBench.views.os, 'replace', side_effect=real_replace
        ):
            repaired = self.register_producer(test, chunk, machine)
        self.assertEqual(repaired.status_code, 200, repaired.content)
        self.assertFalse(repaired.json()['already_registered'])
        self.assertEqual(
            Path(self.media.name, artifact.filename()).read_bytes(),
            b'producer-binary',
        )

    def test_reclaim_after_database_bind_cannot_authorize_generation(self):
        test = self.producer_test()
        machine = self.make_machine('producer-post-bind-race')
        first = claim_chunk(test, machine)
        real_replace = OpenBench.views.os.replace
        reclaimed = []

        def replace_after_reclaim(source, destination):
            self.assertTrue(requeue_chunk(
                test.id, first.idx, machine, first.attempts, 'post-bind reclaim'
            ))
            reclaimed.append(claim_chunk(test, machine))
            return real_replace(source, destination)

        with mock.patch.object(
            OpenBench.views.os, 'replace', side_effect=replace_after_reclaim
        ):
            response = self.register_producer(test, first, machine)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(reclaimed[0].attempts, 2)
        current = DatagenChunk.objects.get(pk=first.pk)
        self.assertEqual(current.status, DatagenChunk.RUNNING)
        self.assertEqual(current.producer_sha256, '')
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)

    def test_producer_rejects_bad_bytes_commit_and_stale_lease(self):
        test = self.producer_test()
        owner = self.make_machine('producer-owner')
        stale = self.make_machine('producer-stale')
        chunk = claim_chunk(test, owner)

        bad_hash = self.register_producer(
            test, chunk, owner, sha256='0' * 64
        )
        self.assertEqual(bad_hash.status_code, 400)
        bad_commit = self.register_producer(
            test, chunk, owner, commit='b' * 40
        )
        self.assertEqual(bad_commit.status_code, 409)
        stale_lease = self.register_producer(test, chunk, stale)
        self.assertEqual(stale_lease.status_code, 409)
        self.assertEqual(DatagenProducerArtifact.objects.count(), 0)

    def test_producer_registration_is_idempotent_and_cas_is_reused(self):
        test = self.producer_test(total=2, per_chunk=1)
        first_machine = self.make_machine('producer-first')
        second_machine = self.make_machine('producer-second')
        first = claim_chunk(test, first_machine)
        second = claim_chunk(test, second_machine)

        first_response = self.register_producer(test, first, first_machine)
        retry_response = self.register_producer(test, first, first_machine)
        second_response = self.register_producer(test, second, second_machine)

        self.assertEqual(first_response.status_code, 200)
        self.assertFalse(first_response.json()['already_registered'])
        self.assertEqual(retry_response.status_code, 200)
        self.assertTrue(retry_response.json()['already_registered'])
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)
        self.assertEqual(
            test.datagen_chunks.values('producer_sha256').distinct().count(), 1
        )

    def test_metadata_only_bind_reuses_campaign_producer_without_file(self):
        test = self.producer_test(total=2, per_chunk=1)
        first_machine = self.make_machine('metadata-first')
        second_machine = self.make_machine('metadata-second')
        first = claim_chunk(test, first_machine)
        second = claim_chunk(test, second_machine)

        uploaded = self.register_producer(test, first, first_machine)
        rebound = self.register_producer(
            test, second, second_machine, metadata_only=True
        )

        self.assertEqual(uploaded.status_code, 200, uploaded.content)
        self.assertEqual(rebound.status_code, 200, rebound.content)
        self.assertTrue(rebound.json()['already_registered'])
        self.assertFalse(rebound.json()['upload_required'])
        second.refresh_from_db()
        self.assertEqual(
            second.producer_sha256, uploaded.json()['sha256']
        )

    def test_metadata_probe_detects_same_size_producer_bitrot(self):
        test = self.producer_test(total=2, per_chunk=1)
        first_machine = self.make_machine('bitrot-probe-first')
        second_machine = self.make_machine('bitrot-probe-second')
        first = claim_chunk(test, first_machine)
        second = claim_chunk(test, second_machine)
        uploaded = self.register_producer(test, first, first_machine)
        self.assertEqual(uploaded.status_code, 200, uploaded.content)
        artifact = DatagenProducerArtifact.objects.get(
            sha256=uploaded.json()['sha256']
        )
        path = Path(self.media.name, artifact.filename())
        path.write_bytes(b'x' * artifact.bytes)

        response = self.register_producer(
            test, second, second_machine, metadata_only=True
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['upload_required'])
        second.refresh_from_db()
        self.assertEqual(second.producer_sha256, '')
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)

    def test_campaign_accepts_a_bounded_set_of_producer_builds(self):
        test = self.producer_test(total=2, per_chunk=1)
        first_machine = self.make_machine('identity-first')
        second_machine = self.make_machine('identity-second')
        first = claim_chunk(test, first_machine)
        second = claim_chunk(test, second_machine)

        accepted = self.register_producer(
            test, first, first_machine, payload=b'producer-a'
        )
        second_build = self.register_producer(
            test, second, second_machine, payload=b'producer-b'
        )

        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(second_build.status_code, 200, second_build.content)
        second.refresh_from_db()
        self.assertEqual(second.producer_sha256, second_build.json()['sha256'])
        self.assertEqual(DatagenProducerArtifact.objects.count(), 2)

    def test_campaign_rejects_producer_build_count_above_quota(self):
        test = self.producer_test(total=2, per_chunk=1)
        first_machine = self.make_machine('quota-first')
        second_machine = self.make_machine('quota-second')
        first = claim_chunk(test, first_machine)
        second = claim_chunk(test, second_machine)

        with mock.patch.object(
            OpenBench.datagen, 'MAX_DATAGEN_PRODUCERS_PER_CAMPAIGN', 1
        ):
            accepted = self.register_producer(
                test, first, first_machine, payload=b'producer-a'
            )
            rejected = self.register_producer(
                test, second, second_machine, payload=b'producer-b'
            )

        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(rejected.status_code, 409, rejected.content)
        self.assertIn('quota', rejected.json()['error'])
        second.refresh_from_db()
        self.assertEqual(second.producer_sha256, '')
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)

    def test_campaign_rejects_aggregate_producer_bytes_above_quota(self):
        test = self.producer_test(total=2, per_chunk=1)
        first_machine = self.make_machine('bytes-first')
        second_machine = self.make_machine('bytes-second')
        first = claim_chunk(test, first_machine)
        second = claim_chunk(test, second_machine)
        first_payload = b'producer-a'

        with mock.patch.object(
            OpenBench.datagen,
            'MAX_DATAGEN_PRODUCER_BYTES_PER_CAMPAIGN',
            len(first_payload),
        ):
            accepted = self.register_producer(
                test, first, first_machine, payload=first_payload
            )
            rejected = self.register_producer(
                test, second, second_machine, payload=b'producer-b'
            )

        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(rejected.status_code, 409, rejected.content)
        second.refresh_from_db()
        self.assertEqual(second.producer_sha256, '')
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)

    def test_same_machine_reclaim_rejects_stale_attempt_everywhere(self):
        test = self.producer_test()
        machine = self.make_machine('aba-machine')
        first = claim_chunk(test, machine)
        self.assertTrue(
            requeue_chunk(
                test.id, first.idx, machine, first.attempts, 'retry'
            )
        )
        current = claim_chunk(test, machine)
        self.assertEqual(current.attempts, first.attempts + 1)
        machine.workload = test.id
        machine.save(update_fields=['workload'])

        producer = self.register_producer(test, first, machine)
        stale_chunk = self.submit_chunk(test, first, machine)
        heartbeat = Client().post('/clientHeartbeat/', {
            'machine_id': machine.id,
            'secret': machine.secret,
            'test_id': test.id,
            'chunk_idx': first.idx,
            'attempt': first.attempts,
        })
        error = Client().post('/clientSubmitError/', {
            'machine_id': machine.id,
            'secret': machine.secret,
            'test_id': test.id,
            'chunk_idx': first.idx,
            'attempt': first.attempts,
            'error': 'late attempt-one failure',
            'logs': 'stale',
        })

        self.assertEqual(producer.status_code, 409, producer.content)
        self.assertEqual(stale_chunk.status_code, 409, stale_chunk.content)
        self.assertTrue(heartbeat.json()['stop'])
        self.assertEqual(error.status_code, 409)
        self.assertEqual(LogEvent.objects.count(), 0)
        self.assertFalse(any(
            path.name.startswith('event')
            for path in Path(self.media.name).rglob('*') if path.is_file()
        ))
        current.refresh_from_db()
        machine.refresh_from_db()
        self.assertEqual(current.status, DatagenChunk.RUNNING)
        self.assertEqual(current.attempts, 2)
        self.assertEqual(current.producer_sha256, '')
        self.assertEqual(machine.workload, test.id)
        self.assertEqual(DatagenProducerArtifact.objects.count(), 0)

    def test_lost_lease_after_producer_hash_leaves_no_canonical_blob(self):
        test = self.producer_test()
        machine = self.make_machine('producer-race')
        first = claim_chunk(test, machine)
        original_hash = OpenBench.views._hash_regular_file
        reclaimed = []

        def lose_lease(path):
            identity = original_hash(path)
            if '.staging' in os.fspath(path) and not reclaimed:
                self.assertTrue(requeue_chunk(
                    test.id, first.idx, machine, first.attempts, 'lease lost'
                ))
                reclaimed.append(claim_chunk(test, machine))
            return identity

        with mock.patch.object(
            OpenBench.views, '_hash_regular_file', side_effect=lose_lease
        ):
            response = self.register_producer(test, first, machine)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(reclaimed[0].attempts, 2)
        self.assertEqual(DatagenProducerArtifact.objects.count(), 0)
        files = [path for path in Path(self.media.name).rglob('*') if path.is_file()]
        self.assertEqual(files, [])

    def test_completion_cas_rejects_reclaim_after_body_authentication(self):
        test = self.producer_test()
        machine = self.make_machine('completion-race')
        first = claim_chunk(test, machine)
        producer = self.register_producer(test, first, machine).json()
        original_digest = OpenBench.views._datagen_uploaded_digest
        reclaimed = []

        def lose_lease(upload, *args, **kwargs):
            identity = original_digest(upload, *args, **kwargs)
            self.assertTrue(requeue_chunk(
                test.id, first.idx, machine, first.attempts, 'lease lost'
            ))
            reclaimed.append(claim_chunk(test, machine))
            return identity

        with mock.patch.object(
            OpenBench.views, '_datagen_uploaded_digest', side_effect=lose_lease
        ):
            response = self.submit_chunk(test, first, machine, producer)

        self.assertEqual(response.status_code, 409, response.content)
        current = DatagenChunk.objects.get(pk=first.pk)
        self.assertEqual(current.status, DatagenChunk.RUNNING)
        self.assertEqual(current.attempts, 2)
        self.assertEqual(current.producer_sha256, '')
        self.assertFalse(Path(self.media.name, current.filename()).exists())
        self.assertFalse(any(
            path.is_file() and '.staging-' in path.name
            for path in Path(self.media.name).rglob('*')
        ))

    def test_chunk_staging_is_rehashed_before_receipt_and_completion_cas(self):
        test = self.make_tablebase_test(total=2)
        machine = self.make_machine(
            'staging-corruption',
            atomic=6,
            manifest=test.datagen_tablebase_manifest_sha256,
        )
        chunk = claim_chunk(test, machine)

        with mock.patch.object(
            OpenBench.views,
            '_hash_regular_file',
            return_value=('0' * 64, 1),
        ), mock.patch.object(OpenBench.views, '_fsync_promoted_file') as fsync:
            response = self.submit_chunk(
                test, chunk, machine, tablebase=True
            )

        self.assertEqual(response.status_code, 500, response.content)
        fsync.assert_not_called()
        current = DatagenChunk.objects.get(pk=chunk.pk)
        self.assertEqual(current.status, DatagenChunk.RUNNING)
        self.assertEqual(current.sha256, '')
        self.assertEqual(current.environment_receipt, {})
        self.assertFalse(Path(self.media.name, current.filename()).exists())
        self.assertFalse(any(
            path.is_file() and '.staging-' in path.name
            for path in Path(self.media.name).rglob('*')
        ))

    def test_required_chunk_cannot_publish_without_exact_producer_binding(self):
        test = self.producer_test()
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)

        missing = self.submit_chunk(test, chunk, machine)
        self.assertEqual(missing.status_code, 400)

        registered = self.register_producer(test, chunk, machine).json()
        mismatched = dict(registered)
        mismatched['sha256'] = 'f' * 64
        conflict = self.submit_chunk(test, chunk, machine, mismatched)
        self.assertEqual(conflict.status_code, 409)

        accepted = self.submit_chunk(test, chunk, machine, registered)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(
            accepted.json()['producer_sha256'], registered['sha256']
        )
        chunk.refresh_from_db()
        self.assertEqual(chunk.status, DatagenChunk.COMPLETED)

    def test_chunk_submission_rejects_same_size_producer_bitrot(self):
        test = self.producer_test()
        machine = self.make_machine('producer-bitrot-submit')
        chunk = claim_chunk(test, machine)
        registered = self.register_producer(test, chunk, machine).json()
        artifact = DatagenProducerArtifact.objects.get(
            sha256=registered['sha256']
        )
        Path(self.media.name, artifact.filename()).write_bytes(
            b'x' * artifact.bytes
        )

        response = self.submit_chunk(test, chunk, machine, registered)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn('unavailable or corrupt', response.json()['error'])
        chunk.refresh_from_db()
        self.assertEqual(chunk.status, DatagenChunk.RUNNING)

    def test_missing_cas_blob_blocks_chunk_and_authenticated_retry_repairs_it(self):
        test = self.producer_test()
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        registered = self.register_producer(test, chunk, machine).json()
        artifact = DatagenProducerArtifact.objects.get(
            sha256=registered['sha256']
        )
        Path(self.media.name, artifact.filename()).unlink()

        blocked = self.submit_chunk(test, chunk, machine, registered)
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('unavailable or corrupt', blocked.json()['error'])

        repaired = self.register_producer(test, chunk, machine)
        self.assertEqual(repaired.status_code, 200)
        self.assertFalse(repaired.json()['already_registered'])
        self.assertEqual(
            self.submit_chunk(test, chunk, machine, registered).status_code,
            200,
        )

    def test_requeue_clears_attempt_local_producer_binding(self):
        test = self.producer_test()
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        self.assertEqual(
            self.register_producer(test, chunk, machine).status_code, 200
        )

        self.assertTrue(
            requeue_chunk(
                test.id, chunk.idx, machine, chunk.attempts, 'retry'
            )
        )
        chunk.refresh_from_db()
        self.assertEqual(chunk.producer_sha256, '')
        self.assertEqual(chunk.producer_bytes, 0)
        self.assertEqual(chunk.producer_commit, '')
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)

    def test_requeue_cannot_recycle_campaign_quota_for_unique_uploads(self):
        test = self.producer_test()
        machine = self.make_machine('quota-requeue')
        first = claim_chunk(test, machine)

        with mock.patch.object(
            OpenBench.datagen, 'MAX_DATAGEN_PRODUCERS_PER_CAMPAIGN', 1
        ):
            accepted = self.register_producer(
                test, first, machine, payload=b'producer-a'
            )
            self.assertEqual(accepted.status_code, 200, accepted.content)
            self.assertTrue(requeue_chunk(
                test.id, first.idx, machine, first.attempts, 'retry'
            ))
            second = claim_chunk(test, machine)
            rejected = self.register_producer(
                test, second, machine, payload=b'producer-b'
            )

        self.assertEqual(rejected.status_code, 409, rejected.content)
        test.refresh_from_db()
        self.assertEqual(test.datagen_producer_build_count, 1)
        self.assertEqual(DatagenProducerBuild.objects.filter(test=test).count(), 1)
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)

    def test_owner_and_global_quotas_rollback_rejected_artifact(self):
        first = self.producer_test()
        second = self.producer_test()
        first_machine = self.make_machine('owner-quota-first')
        second_machine = self.make_machine('owner-quota-second')
        first_chunk = claim_chunk(first, first_machine)
        second_chunk = claim_chunk(second, second_machine)

        with mock.patch.object(
            OpenBench.datagen, 'MAX_DATAGEN_PRODUCERS_PER_OWNER', 1
        ):
            accepted = self.register_producer(
                first, first_chunk, first_machine, payload=b'producer-a'
            )
            rejected = self.register_producer(
                second, second_chunk, second_machine, payload=b'producer-b'
            )
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(rejected.status_code, 409, rejected.content)
        self.assertIn('owner', rejected.json()['error'])
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)
        quota = DatagenProducerOwnerQuota.objects.get(owner=self.user)
        self.assertEqual((quota.build_count, quota.reserved_bytes), (1, 10))

        # A different campaign owner is still bounded by the physical CAS cap.
        other_owner = User.objects.create_user('campaign-two-owner')
        Profile.objects.create(user=other_owner, enabled=True)
        second.author = other_owner.username
        second.save(update_fields=['author'])
        with mock.patch.object(
            OpenBench.datagen, 'MAX_DATAGEN_PRODUCERS_GLOBAL', 1
        ):
            globally_rejected = self.register_producer(
                second, second_chunk, second_machine, payload=b'producer-c'
            )
        self.assertEqual(globally_rejected.status_code, 409)
        self.assertIn('global', globally_rejected.json()['error'])
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)

    def test_staging_crash_is_repaired_by_idempotent_reconciler(self):
        test = self.producer_test()
        machine = self.make_machine('reconcile-staging')
        chunk = claim_chunk(test, machine)
        with mock.patch.object(
            OpenBench.views.os, 'replace', side_effect=OSError('crash window')
        ):
            failed = self.register_producer(test, chunk, machine)
        self.assertEqual(failed.status_code, 500, failed.content)
        artifact = DatagenProducerArtifact.objects.get()
        self.assertEqual(artifact.state, DatagenProducerArtifact.STAGING)
        staging = Path(self.media.name, artifact.staging_name)
        self.assertTrue(staging.exists())
        old = timezone.now() - timedelta(hours=2)
        DatagenProducerArtifact.objects.filter(pk=artifact.pk).update(updated=old)
        os.utime(staging, (old.timestamp(), old.timestamp()))

        call_command(
            'reconcile_datagen_producers', '--scrub',
            '--staging-max-age-hours=1',
        )
        call_command(
            'reconcile_datagen_producers', '--scrub',
            '--staging-max-age-hours=1',
        )

        artifact.refresh_from_db()
        self.assertEqual(artifact.state, DatagenProducerArtifact.AVAILABLE)
        self.assertEqual(artifact.staging_name, '')
        self.assertEqual(
            Path(self.media.name, artifact.filename()).read_bytes(),
            b'producer-binary',
        )

    def test_reconciler_retains_active_and_retires_old_finished_campaign(self):
        test = self.producer_test()
        machine = self.make_machine('reconcile-retention')
        chunk = claim_chunk(test, machine)
        self.assertEqual(
            self.register_producer(test, chunk, machine).status_code, 200
        )
        artifact = DatagenProducerArtifact.objects.get()
        old = timezone.now() - timedelta(days=3)
        Test.objects.filter(pk=test.pk).update(updated=old)
        DatagenProducerArtifact.objects.filter(pk=artifact.pk).update(updated=old)

        call_command('reconcile_datagen_producers', '--retention-days=1')
        self.assertTrue(DatagenProducerBuild.objects.filter(test=test).exists())
        self.assertTrue(DatagenProducerArtifact.objects.filter(pk=artifact.pk).exists())

        Test.objects.filter(pk=test.pk).update(finished=True, updated=old)
        call_command('reconcile_datagen_producers', '--retention-days=1')

        self.assertFalse(DatagenProducerBuild.objects.filter(test=test).exists())
        self.assertFalse(DatagenProducerArtifact.objects.filter(pk=artifact.pk).exists())
        self.assertFalse(Path(self.media.name, artifact.filename()).exists())

    def test_reconciler_repairs_stale_refcount_without_deleting_live_build(self):
        test = self.producer_test()
        machine = self.make_machine('reconcile-stale-refcount')
        chunk = claim_chunk(test, machine)
        self.assertEqual(
            self.register_producer(test, chunk, machine).status_code, 200
        )
        artifact = DatagenProducerArtifact.objects.get()
        old = timezone.now() - timedelta(days=3)
        DatagenProducerArtifact.objects.filter(pk=artifact.pk).update(
            reference_count=0,
            updated=old,
        )

        call_command('reconcile_datagen_producers', '--retention-days=1')

        artifact.refresh_from_db()
        self.assertEqual(artifact.reference_count, 1)
        self.assertTrue(
            DatagenProducerBuild.objects.filter(
                test=test, artifact=artifact,
            ).exists()
        )
        self.assertTrue(Path(self.media.name, artifact.filename()).exists())

    def test_reconciler_collects_only_expired_staging_and_canonical_orphans(self):
        staging = Path(self.media.name, 'datagen-producers', '.staging')
        staging.mkdir(parents=True)
        old_staging = staging / 'old-upload'
        fresh_staging = staging / 'fresh-upload'
        old_staging.write_bytes(b'old')
        fresh_staging.write_bytes(b'fresh')
        orphan_sha = 'd' * 64
        orphan = Path(
            self.media.name, 'datagen-producers', 'sha256', 'dd', orphan_sha
        )
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b'orphan')
        old = timezone.now() - timedelta(days=3)
        os.utime(old_staging, (old.timestamp(), old.timestamp()))
        os.utime(orphan, (old.timestamp(), old.timestamp()))

        call_command(
            'reconcile_datagen_producers',
            '--staging-max-age-hours=1',
            '--retention-days=1',
        )

        self.assertFalse(old_staging.exists())
        self.assertTrue(fresh_staging.exists())
        self.assertFalse(orphan.exists())

    def test_migration_backfill_and_counter_rebuild_are_idempotent(self):
        test = self.producer_test()
        machine = self.make_machine('backfill-idempotent')
        chunk = claim_chunk(test, machine)
        self.assertEqual(
            self.register_producer(test, chunk, machine).status_code, 200
        )
        migration = importlib.import_module(
            'OpenBench.migrations.0006_producer_reservations_v39'
        )
        migration.backfill_producer_reservations(django_apps, None)
        first = (
            DatagenProducerArtifact.objects.count(),
            DatagenProducerBuild.objects.count(),
            DatagenProducerQuota.objects.get(key='global').reserved_bytes,
        )
        migration.backfill_producer_reservations(django_apps, None)
        OpenBench.datagen.rebuild_producer_quota_counters()
        second = (
            DatagenProducerArtifact.objects.count(),
            DatagenProducerBuild.objects.count(),
            DatagenProducerQuota.objects.get(key='global').reserved_bytes,
        )
        self.assertEqual(first, second)
        artifact = DatagenProducerArtifact.objects.get()
        self.assertEqual(
            artifact.state, DatagenProducerArtifact.UNVERIFIED
        )
        call_command('reconcile_datagen_producers', '--scrub')
        artifact.refresh_from_db()
        self.assertEqual(
            artifact.state, DatagenProducerArtifact.AVAILABLE
        )

    def test_completed_manifest_and_producer_download_expose_exact_evidence(self):
        test = self.producer_test()
        machine = self.make_machine()
        chunk = claim_chunk(test, machine)
        registered = self.register_producer(test, chunk, machine).json()
        self.assertEqual(
            self.submit_chunk(test, chunk, machine, registered).status_code,
            200,
        )

        anonymous = Client().get('/api/datagen/%d/' % test.id)
        self.assertIn('requires authentication', anonymous.json()['error'])
        authorization = 'Basic ' + base64.b64encode(
            b'datagen:password'
        ).decode('ascii')
        client = Client(HTTP_AUTHORIZATION=authorization)
        insecure = client.get('/api/datagen/%d/' % test.id)
        self.assertIn('requires authentication', insecure.json()['error'])
        spoofed_proxy = client.get(
            '/api/datagen/%d/' % test.id,
            HTTP_X_FORWARDED_PROTO='https',
        )
        self.assertIn(
            'requires authentication', spoofed_proxy.json()['error']
        )
        verifier = OpenBench.views._open_verified_producer_descriptor
        with mock.patch.object(
            OpenBench.views,
            '_open_verified_producer_descriptor',
            wraps=verifier,
        ) as verified:
            manifest = client.get('/api/datagen/%d/' % test.id, secure=True)
        self.assertEqual(verified.call_count, 1)
        self.assertEqual(manifest.status_code, 200)
        document = manifest.json()
        self.assertEqual(document['producer_commit'], self.engine.sha)
        self.assertEqual(document['producer_builds'], [{
            'sha256': registered['sha256'],
            'bytes': registered['bytes'],
            'commit': self.engine.sha,
        }])
        self.assertTrue(document['producer_artifact_required'])
        self.assertEqual(
            document['producer_contract_sha256'],
            test.datagen_producer_contract_sha256,
        )
        self.assertEqual(document['chunks'][0]['producer_sha256'], registered['sha256'])

        self.assertIn(
            'requires authentication',
            Client().get(
                '/api/datagen-producer/%s/' % registered['sha256']
            ).json()['error'],
        )
        with mock.patch.object(
            OpenBench.views,
            '_open_verified_producer_descriptor',
            wraps=verifier,
        ) as verified:
            download = client.get(
                '/api/datagen-producer/%s/' % registered['sha256'],
                secure=True,
            )
        self.assertEqual(verified.call_count, 1)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b''.join(download.streaming_content), b'producer-binary')
        self.assertEqual(
            download['ETag'], '"sha256:%s"' % registered['sha256']
        )

    def test_manifest_and_download_reject_same_size_producer_bitrot(self):
        test = self.producer_test()
        machine = self.make_machine('producer-bitrot-publication')
        chunk = claim_chunk(test, machine)
        registered = self.register_producer(test, chunk, machine).json()
        self.assertEqual(
            self.submit_chunk(test, chunk, machine, registered).status_code,
            200,
        )
        artifact = DatagenProducerArtifact.objects.get(
            sha256=registered['sha256']
        )
        Path(self.media.name, artifact.filename()).write_bytes(
            b'x' * artifact.bytes
        )
        client = Client()
        client.force_login(self.user)

        manifest = client.get('/api/datagen/%d/' % test.id).json()
        download = client.get(
            '/api/datagen-producer/%s/' % registered['sha256']
        ).json()

        self.assertIn('unavailable producer evidence', manifest['error'])
        self.assertIn('CAS is invalid', download['error'])

    def test_manifest_rejects_drift_from_campaign_producer_identity(self):
        test = self.producer_test()
        machine = self.make_machine('manifest-drift')
        chunk = claim_chunk(test, machine)
        producer = self.register_producer(test, chunk, machine).json()
        self.assertEqual(
            self.submit_chunk(test, chunk, machine, producer).status_code, 200
        )

        DatagenChunk.objects.filter(pk=chunk.pk).update(
            producer_commit='f' * 40
        )
        client = Client()
        client.force_login(self.user)
        response = client.get('/api/datagen/%d/' % test.id)
        self.assertIn('incomplete producer evidence', response.json()['error'])

    def test_manifest_exposes_sorted_build_set_and_exact_chunk_mapping(self):
        test = self.producer_test(total=2, per_chunk=1)
        machines = [
            self.make_machine('manifest-build-a'),
            self.make_machine('manifest-build-b'),
        ]
        chunks = [claim_chunk(test, machine) for machine in machines]
        producers = [
            self.register_producer(
                test, chunks[0], machines[0], payload=b'producer-a'
            ).json(),
            self.register_producer(
                test, chunks[1], machines[1], payload=b'producer-b'
            ).json(),
        ]
        for chunk, machine, producer in zip(chunks, machines, producers):
            self.assertEqual(
                self.submit_chunk(test, chunk, machine, producer).status_code,
                200,
            )

        client = Client()
        client.force_login(self.user)
        document = client.get('/api/datagen/%d/' % test.id).json()
        expected = sorted(
            (
                {
                    'sha256': producer['sha256'],
                    'bytes': producer['bytes'],
                    'commit': self.engine.sha,
                }
                for producer in producers
            ),
            key=lambda build: build['sha256'],
        )
        self.assertEqual(document['producer_builds'], expected)
        self.assertEqual(
            [
                entry['producer_sha256'] for entry in document['chunks']
            ],
            [producer['sha256'] for producer in producers],
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
                info={
                    'concurrency': 1,
                    'client_ver': OpenBench.config.OPENBENCH_CONFIG[
                        'client_version'
                    ],
                },
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
                        'attempt': chunks[index].attempts,
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

    def test_simultaneous_chunks_reuse_one_content_addressed_producer(self):
        self.test.datagen_command += ' producer {PRODUCER_SHA256}'
        self.test.freeze_datagen_producer_contract()
        self.test.save(update_fields=[
            'datagen_command', 'datagen_producer_required',
            'datagen_producer_contract_sha256',
        ])
        chunks = [
            claim_chunk(self.test, self.machines[index]) for index in range(2)
        ]
        payload = b'shared-producer-binary'
        sha256 = hashlib.sha256(payload).hexdigest()
        barrier = threading.Barrier(2)
        responses = [None, None]
        failures = []

        def submit(index):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                response = Client().post(
                    '/clientSubmitDatagenProducer/',
                    {
                        'machine_id': self.machines[index].id,
                        'secret': self.machines[index].secret,
                        'test_id': self.test.id,
                        'chunk_idx': chunks[index].idx,
                        'attempt': chunks[index].attempts,
                        'sha256': sha256,
                        'bytes': len(payload),
                        'commit': self.engine.sha,
                        'file': SimpleUploadedFile('producer.bin', payload),
                    },
                )
                responses[index] = (response.status_code, response.json())
            except Exception as error:
                failures.append((error, traceback.format_exc()))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=submit, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual([status for status, _ in responses], [200, 200])
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)
        self.assertEqual(
            self.test.datagen_chunks.filter(
                producer_sha256=sha256,
                producer_bytes=len(payload),
                producer_commit=self.engine.sha,
            ).count(),
            2,
        )

    def test_concurrent_distinct_uploads_serialize_campaign_quota(self):
        self.test.datagen_command += ' producer {PRODUCER_SHA256}'
        self.test.freeze_datagen_producer_contract()
        self.test.save(update_fields=[
            'datagen_command', 'datagen_producer_required',
            'datagen_producer_contract_sha256',
        ])
        chunks = [
            claim_chunk(self.test, self.machines[index]) for index in range(2)
        ]
        payloads = [b'producer-a', b'producer-b']
        barrier = threading.Barrier(2)
        responses = [None, None]
        failures = []

        def submit(index):
            close_old_connections()
            try:
                payload = payloads[index]
                barrier.wait(timeout=5)
                response = Client().post(
                    '/clientSubmitDatagenProducer/',
                    {
                        'machine_id': self.machines[index].id,
                        'secret': self.machines[index].secret,
                        'test_id': self.test.id,
                        'chunk_idx': chunks[index].idx,
                        'attempt': chunks[index].attempts,
                        'sha256': hashlib.sha256(payload).hexdigest(),
                        'bytes': len(payload),
                        'commit': self.engine.sha,
                        'file': SimpleUploadedFile('producer.bin', payload),
                    },
                )
                responses[index] = (response.status_code, response.json())
            except Exception as error:
                failures.append((error, traceback.format_exc()))
            finally:
                close_old_connections()

        with mock.patch.object(
            OpenBench.datagen, 'MAX_DATAGEN_PRODUCERS_PER_CAMPAIGN', 1
        ):
            threads = [
                threading.Thread(target=submit, args=(index,))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(sorted(status for status, _ in responses), [200, 409])
        self.test.refresh_from_db()
        self.assertEqual(self.test.datagen_producer_build_count, 1)
        self.assertEqual(DatagenProducerBuild.objects.count(), 1)
        self.assertEqual(DatagenProducerArtifact.objects.count(), 1)
