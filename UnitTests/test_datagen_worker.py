import bz2
import hashlib
import json
import os
from pathlib import Path
import struct
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


def tablebase_config(teacher_mode='pure'):
    cfg = config()
    cfg.atomic_syzygy_path = r'C:\Atomic Tables\combined'
    cfg.atomic_syzygy_max = 6
    cfg.atomic_syzygy_manifest_sha256 = 'b' * 64
    test = cfg.workload['test']
    test['syzygy_wdl'] = '6-MAN'
    test['syzygy_adj'] = 'DISABLED'
    test['dev']['tablebase_family'] = 'atomic'
    data = test['datagen']
    data.update({
        'command': (
            'generate seed {SEED} count {COUNT} threads {THREADS} '
            'out {OUT} syzygy "{SYZYGY}" '
            'syzygy_manifest_sha256 {SYZYGY_MANIFEST_SHA256} '
            'syzygy_max {SYZYGY_MAX} teacher_mode {TEACHER_MODE}'
        ),
        'tablebase_required': True,
        'tablebase_family': 'atomic',
        'tablebase_max': 6,
        'tablebase_manifest_sha256': 'b' * 64,
        'teacher_mode': teacher_mode,
        'environment_contract_sha256': 'c' * 64,
    })
    lease = {
        'schema': 'openbench-datagen-tablebase-lease-v40',
        'protocol': 40,
        'test_id': test['id'],
        'chunk_idx': data['chunk_idx'],
        'attempt': data['attempt'],
        'machine_id': cfg.machine_id,
        'environment_contract_sha256': 'c' * 64,
        'tablebase': {
            'family': 'atomic',
            'required_max': 6,
            'worker_max': 6,
            'manifest_sha256': 'b' * 64,
        },
        'teacher_mode': teacher_mode,
    }
    data['environment_lease'] = lease
    data['environment_lease_sha256'] = hashlib.sha256(
        json.dumps(lease, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    return cfg


def publication_config(tablebase=False):
    cfg = tablebase_config() if tablebase else config()
    test = cfg.workload['test']
    data = test['datagen']
    network_bytes = b'v41-worker-network'
    network_sha256 = hashlib.sha256(network_bytes).hexdigest()
    network_id = network_sha256[:8].upper()
    test['dev'].update({
        'repo': 'https://github.com/example/engine',
        'requested_ref': 'branch',
        'options': '',
        'network': network_id,
        'netname': 'network.nnue',
    })
    test['book'] = {
        'name': 'NONE',
        'sha': None,
        'raw_sha': None,
        'source': None,
    }
    command = (
        'generate seed {SEED} count {COUNT} threads {THREADS} '
        'book {BOOK} book_sha256 {BOOK_SHA256} '
        'network {NETWORK} network_sha256 {NETWORK_SHA256}'
    )
    if tablebase:
        command += (
            ' syzygy {SYZYGY} '
            'syzygy_manifest_sha256 {SYZYGY_MANIFEST_SHA256} '
            'syzygy_max {SYZYGY_MAX} teacher_mode {TEACHER_MODE}'
        )
    command += ' out {OUT}'
    data.update({
        'command': command,
        'base_seed': 100,
        'producer_artifact_required': False,
        'producer_contract_sha256': 'd' * 64,
        'publication_protocol': 41,
    })
    if not tablebase:
        test.update({'syzygy_wdl': 'DISABLED', 'syzygy_adj': 'DISABLED'})
        test['dev']['tablebase_family'] = 'atomic'
        data.update({
            'tablebase_required': False,
            'tablebase_family': '',
            'tablebase_max': 0,
            'tablebase_manifest_sha256': '',
            'teacher_mode': '',
            'environment_contract_sha256': 'c' * 64,
        })
    contract = {
        'schema': 'openbench-datagen-publication-contract-v41',
        'protocol': 41,
        'campaign_id': 'atomic-campaign',
        'external_workload_id': 'opening-train',
        'role': 'train',
        'cohort': 'opening',
        'engine': {
            'name': test['dev']['engine'],
            'repo': test['dev']['repo'],
            'source': test['dev']['source'],
            'requested_ref': test['dev']['requested_ref'],
            'commit': test['dev']['sha'],
            'bench': test['dev']['bench'],
            'options': test['dev']['options'],
        },
        'network': {
            'name': test['dev']['netname'],
            'openbench_id': network_id,
            'sha256': network_sha256,
            'bytes': len(network_bytes),
        },
        'book': {
            'kind': 'builtin-startpos',
            'name': 'NONE',
            'source': None,
            'text_sha256': None,
            'raw_sha256': None,
        },
        'generation': {
            'command': command,
            'command_sha256': hashlib.sha256(command.encode()).hexdigest(),
            'total_count': data['total_count'],
            'positions_per_chunk': data['positions_per_chunk'],
            'base_seed': data['base_seed'],
            'seed_method': 'base-plus-chunk-index-v1',
        },
        'producer': {
            'required': False,
            'contract_sha256': data['producer_contract_sha256'],
        },
        'teacher': {'mode': data['teacher_mode'] or None},
        'syzygy': {
            'required': data['tablebase_required'],
            'family': data['tablebase_family'] or None,
            'max': data['tablebase_max'],
            'manifest_sha256': data['tablebase_manifest_sha256'] or None,
            'environment_contract_sha256': data[
                'environment_contract_sha256'
            ],
        },
    }
    contract_sha256 = hashlib.sha256(json.dumps(
        contract,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()).hexdigest()
    data['publication_contract'] = contract
    data['publication_contract_sha256'] = contract_sha256
    tablebase_lease = {
        'required': data['tablebase_required'],
        'family': data['tablebase_family'] or None,
        'required_max': data['tablebase_max'],
        'worker_max': data['tablebase_max'],
        'manifest_sha256': data['tablebase_manifest_sha256'] or None,
    }
    lease = {
        'schema': 'openbench-datagen-publication-lease-v41',
        'protocol': 41,
        'test_id': test['id'],
        'chunk_idx': data['chunk_idx'],
        'attempt': data['attempt'],
        'machine_id': cfg.machine_id,
        'publication_contract_sha256': contract_sha256,
        'environment_contract_sha256': data['environment_contract_sha256'],
        'tablebase': tablebase_lease,
        'teacher_mode': data['teacher_mode'] or None,
    }
    data['environment_lease'] = lease
    data['environment_lease_sha256'] = hashlib.sha256(json.dumps(
        lease, sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()
    return cfg, network_bytes


def horde_publication_config(
    book_name='HORDE_openings.epd',
    book_sha256=None,
    registered_generation=None,
):
    cfg, _ = publication_config()
    test = cfg.workload['test']
    data = test['datagen']
    publication = data['publication_contract']
    book_sha256 = (
        book_sha256 or worker.HORDE_OPENING_BOOK_SHA256
    ).lower()
    registered_generation = dict(
        registered_generation or worker.HORDE_BIN_V1_GENERATION
    )
    command = (
        'horde_generate_training_data threads {THREADS} hash %d '
        'network {NETWORK} network_sha256 {NETWORK_SHA256} '
        'producer_sha256 {PRODUCER_SHA256} count {COUNT} seed {SEED} '
        'book {BOOK} book_sha256 {BOOK_SHA256} out {OUT} depth %d '
        'nodes %d random_move_min_ply %d random_move_max_ply %d '
        'random_move_count %d random_multi_pv %d random_multi_pv_diff %d '
        'write_min_ply %d write_max_ply %d max_game_ply %d '
        'set_recommended_uci_options'
        % (
            registered_generation['hash_mb'],
            registered_generation['depth'],
            registered_generation['nodes'],
            registered_generation['random_move_min_ply'],
            registered_generation['random_move_max_ply'],
            registered_generation['random_move_count'],
            registered_generation['random_multi_pv'],
            registered_generation['random_multi_pv_diff'],
            registered_generation['write_min_ply'],
            registered_generation['write_max_ply'],
            registered_generation['max_game_ply'],
        )
    )
    book_source = 'https://example.test/%s.zip' % book_name

    test['variant_contract'] = worker.HORDE_DATAGEN_VARIANT_CONTRACT
    test['dev'].update({
        'engine': 'Horde-Stockfish',
        'network': worker.HORDE_RUN6B_SHA256[:8],
        'netname': 'hordetest_run6b_e37_l06.nnue',
    })
    test['book'] = {
        'name': book_name,
        'sha': book_sha256,
        'raw_sha': book_sha256,
        'source': book_source,
    }
    data.update({
        'command': command,
        'total_count': 1,
        'positions_per_chunk': 1,
        'base_seed': 103,
        'chunk_idx': 0,
        'chunk_count': 1,
        'seed': 103,
        'producer_artifact_required': True,
    })
    publication.update({
        'campaign_id': 'horde-v1-run6b-canary-20260806',
        'external_workload_id': 'horde-v1-run6b-g0-canary',
        'role': 'g0-canary',
        'cohort': 'run6b-d6',
    })
    publication['engine']['name'] = test['dev']['engine']
    publication['network'] = {
        'name': test['dev']['netname'],
        'openbench_id': test['dev']['network'],
        'sha256': worker.HORDE_RUN6B_SHA256.lower(),
        'bytes': 1088416,
    }
    publication['book'] = {
        'kind': 'file',
        'name': test['book']['name'],
        'source': book_source,
        'text_sha256': book_sha256,
        'raw_sha256': book_sha256,
    }
    publication['generation'].update({
        'command': command,
        'command_sha256': hashlib.sha256(command.encode()).hexdigest(),
        'total_count': data['total_count'],
        'positions_per_chunk': data['positions_per_chunk'],
        'base_seed': data['base_seed'],
    })
    publication['producer']['required'] = True
    data['publication_contract_sha256'] = hashlib.sha256(json.dumps(
        publication,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()).hexdigest()
    data['environment_lease']['publication_contract_sha256'] = data[
        'publication_contract_sha256'
    ]
    data['environment_lease_sha256'] = hashlib.sha256(json.dumps(
        data['environment_lease'], sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()
    producer = {
        'sha256': 'c' * 64,
        'bytes': 123,
        'commit': test['dev']['sha'],
    }
    return cfg, producer


def horde_record():
    record = bytearray(worker.HORDE_BIN_V1_RECORD_SIZE)
    record[0] = 0x5B  # Black king a1, White queen b1.
    record[32] = 1
    record[34] = 64
    record[35] = 1
    struct.pack_into('<HHhHHbB', record, 36, 0, 1, 23, 1, 1, 1, 2)
    return bytes(record)


def horde_manifest(cfg, producer, payload):
    test = cfg.workload['test']
    data = test['datagen']
    generation = {
        'requested_records': data['chunk_count'],
        'seed': str(data['seed']),
        'threads': cfg.threads,
    }
    book_sha256 = data['publication_contract']['book'][
        'raw_sha256'
    ].upper()
    generation.update(
        worker.HORDE_BIN_V1_REGISTERED_GENERATIONS[book_sha256]
    )
    return {
        'schema': 'HORDE_BIN_V1',
        'schema_sha256': worker.HORDE_BIN_V1_SCHEMA_SHA256,
        'format_version': worker.HORDE_BIN_V1_VERSION,
        'header_bytes': worker.HORDE_BIN_V1_HEADER_SIZE,
        'record_bytes': worker.HORDE_BIN_V1_RECORD_SIZE,
        'record_count': data['chunk_count'],
        'byte_order': 'little',
        'source_commit': test['dev']['sha'],
        'source_dirty': False,
        'network': {
            'schema': 'HORDETEST_HP_LEGACY_V1',
            'sha256': worker.HORDE_RUN6B_SHA256,
        },
        'book_sha256': book_sha256,
        'producer_sha256': producer['sha256'].upper(),
        'payload_sha256': hashlib.sha256(payload).hexdigest().upper(),
        'label_contract': {
            'schema': 'HORDE_LABEL_CONTRACT_V1',
            'schema_sha256': worker.HORDE_LABEL_CONTRACT_V1_SHA256,
        },
        'generation': generation,
    }


def horde_file_bytes(manifest, payload):
    encoded = json.dumps(
        manifest,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    header = bytearray(worker.HORDE_BIN_V1_HEADER_SIZE)
    header[:8] = worker.HORDE_BIN_V1_MAGIC
    struct.pack_into(
        '<HHI',
        header,
        8,
        worker.HORDE_BIN_V1_VERSION,
        worker.HORDE_BIN_V1_HEADER_SIZE,
        len(encoded),
    )
    header[16:16 + len(encoded)] = encoded
    return bytes(header) + payload


class DatagenWorkerTests(unittest.TestCase):

    def test_horde_bin_v1_validator_accepts_bound_output(self):
        cfg, producer = horde_publication_config()
        payload = horde_record()
        manifest = horde_manifest(cfg, producer, payload)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, 'chunk.bin')
            output.write_bytes(horde_file_bytes(manifest, payload))
            summary = worker.validate_horde_bin_v1_output(
                cfg, str(output), producer
            )

        self.assertEqual(summary['record_count'], 1)
        self.assertEqual(summary['sides'], [0, 1])
        self.assertEqual(summary['results'], [0, 0, 1])
        self.assertEqual(summary['reasons'][2], 1)
        self.assertEqual(summary['white_buckets'], [1, 0, 0, 0])
        self.assertEqual(summary['flags'][0], 1)
        expected_file = horde_file_bytes(manifest, payload)
        self.assertEqual(
            summary['file_sha256'], hashlib.sha256(expected_file).hexdigest()
        )
        self.assertEqual(summary['file_bytes'], len(expected_file))

    def test_horde_bin_v1_validator_accepts_registered_rank8_contracts(self):
        cases = [
            (
                'HORDE_openings_v3_train.epd',
                worker.HORDE_V3_TRAIN_BOOK_SHA256,
            ),
            (
                'HORDE_openings_v3_validation.epd',
                worker.HORDE_V3_VALIDATION_BOOK_SHA256,
            ),
        ]
        for book_name, book_sha256 in cases:
            with (
                self.subTest(book=book_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                generation = worker.HORDE_BIN_V1_REGISTERED_GENERATIONS[
                    book_sha256
                ]
                cfg, producer = horde_publication_config(
                    book_name, book_sha256, generation
                )
                payload = horde_record()
                manifest = horde_manifest(cfg, producer, payload)
                output = Path(directory, 'chunk.bin')
                output.write_bytes(horde_file_bytes(manifest, payload))

                summary = worker.validate_horde_bin_v1_output(
                    cfg, str(output), producer
                )

                self.assertEqual(summary['record_count'], 1)
                self.assertEqual(
                    manifest['generation']['opening_count'],
                    generation['opening_count'],
                )

    def test_horde_bin_v1_validator_rejects_unregistered_book_contract(self):
        generation = {
            **worker.HORDE_BIN_V1_RANK8_GENERATION,
            'opening_count': 1,
        }
        cfg, producer = horde_publication_config(
            'HORDE_unregistered.epd', '0' * 64, generation
        )
        with self.assertRaisesRegex(
            worker.DatagenConfigurationError, 'registered Horde artifact'
        ):
            worker.validate_horde_bin_v1_output(
                cfg, 'missing.bin', producer
            )

    def test_horde_bin_v1_validator_rejects_rank8_contract_drift(self):
        book_sha256 = worker.HORDE_V3_TRAIN_BOOK_SHA256
        generation = worker.HORDE_BIN_V1_REGISTERED_GENERATIONS[book_sha256]
        cfg, producer = horde_publication_config(
            'HORDE_openings_v3_train.epd', book_sha256, generation
        )
        payload = horde_record()
        manifest = horde_manifest(cfg, producer, payload)
        manifest['generation']['opening_count'] = 297
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, 'chunk.bin')
            output.write_bytes(horde_file_bytes(manifest, payload))
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError,
                'generation setting opening_count',
            ):
                worker.validate_horde_bin_v1_output(
                    cfg, str(output), producer
                )

    def test_horde_bin_v1_validator_rejects_identity_and_setting_drift(self):
        cases = {
            'dirty source': (
                lambda document: document.update(source_dirty=True),
                'source identity',
            ),
            'network': (
                lambda document: document['network'].update(sha256='0' * 64),
                'network identity',
            ),
            'network shape': (
                lambda document: document.update(network=[]),
                'network identity',
            ),
            'book': (
                lambda document: document.update(book_sha256='0' * 64),
                'book or producer',
            ),
            'producer': (
                lambda document: document.update(producer_sha256='0' * 64),
                'book or producer',
            ),
            'seed': (
                lambda document: document['generation'].update(seed='104'),
                'seed or thread',
            ),
            'depth': (
                lambda document: document['generation'].update(depth=7),
                'generation setting depth',
            ),
            'record count type': (
                lambda document: document.update(record_count=True),
                'record count',
            ),
            'generation shape': (
                lambda document: document.update(generation=[]),
                'generation manifest',
            ),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                cfg, producer = horde_publication_config()
                payload = horde_record()
                manifest = horde_manifest(cfg, producer, payload)
                mutate(manifest)
                output = Path(directory, 'chunk.bin')
                output.write_bytes(horde_file_bytes(manifest, payload))
                with self.assertRaisesRegex(
                    worker.DatagenConfigurationError, message
                ):
                    worker.validate_horde_bin_v1_output(
                        cfg, str(output), producer
                    )

    def test_horde_bin_v1_validator_rejects_payload_and_position_corruption(self):
        cfg, producer = horde_publication_config()
        payload = horde_record()
        manifest = horde_manifest(cfg, producer, payload)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, 'chunk.bin')
            corrupt = bytearray(payload)
            corrupt[0] = 0xFB
            output.write_bytes(horde_file_bytes(manifest, bytes(corrupt)))
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError, 'reserved piece code'
            ):
                worker.validate_horde_bin_v1_output(
                    cfg, str(output), producer
                )

            manifest = horde_manifest(cfg, producer, payload)
            manifest['payload_sha256'] = '0' * 64
            output.write_bytes(horde_file_bytes(manifest, payload))
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError, 'payload SHA-256 mismatch'
            ):
                worker.validate_horde_bin_v1_output(
                    cfg, str(output), producer
                )

    def test_horde_run_command_rejects_output_before_compression(self):
        cfg, producer = horde_publication_config()
        heartbeat = SimpleNamespace(stop_requested=threading.Event())
        process = SimpleNamespace(
            stdin=SimpleNamespace(
                write=mock.Mock(), flush=mock.Mock(), close=mock.Mock()
            ),
            poll=mock.Mock(return_value=0),
            returncode=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, 'chunk.bin')
            log = Path(directory, 'engine.log')
            output.write_bytes(b'not-horde-bin-v1')
            with mock.patch.object(
                worker, 'render_datagen_command', return_value='generate'
            ), mock.patch.object(
                worker, 'Popen', return_value=process
            ), self.assertRaisesRegex(
                worker.DatagenConfigurationError, 'header is truncated'
            ):
                worker.run_datagen_command(
                    cfg,
                    'generator.exe',
                    str(output),
                    str(log),
                    heartbeat,
                    producer=producer,
                )

    def test_non_horde_output_is_not_interpreted_as_horde_bin_v1(self):
        self.assertIsNone(
            worker.validate_horde_bin_v1_output(
                config(), 'missing.bin', None
            )
        )

    def test_v41_worker_validates_contract_lease_and_full_network_before_launch(self):
        cfg, network_bytes = publication_config()
        attestation = worker.datagen_tablebase_attestation(cfg)
        self.assertFalse(attestation['tablebase_required'])
        self.assertEqual(
            attestation['publication_contract_sha256'],
            cfg.workload['test']['datagen'][
                'publication_contract_sha256'
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            network = os.path.join(directory, 'network.nnue')
            Path(network).write_bytes(network_bytes)
            rendered = worker.render_datagen_command(
                cfg, os.path.join('Datagen', 'chunk.bin'), network
            )
            self.assertIn(
                'network_sha256 '
                + hashlib.sha256(network_bytes).hexdigest(),
                rendered,
            )
            Path(network).write_bytes(network_bytes + b'drift')
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError, 'network.*bytes'
            ):
                worker.render_datagen_command(
                    cfg, os.path.join('Datagen', 'chunk.bin'), network
                )

    def test_v41_worker_rejects_network_byte_count_drift_even_when_rehashed(self):
        cfg, network_bytes = publication_config()
        data = cfg.workload['test']['datagen']
        data['publication_contract']['network']['bytes'] += 1
        data['publication_contract_sha256'] = hashlib.sha256(json.dumps(
            data['publication_contract'],
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()).hexdigest()
        data['environment_lease']['publication_contract_sha256'] = data[
            'publication_contract_sha256'
        ]
        data['environment_lease_sha256'] = hashlib.sha256(json.dumps(
            data['environment_lease'], sort_keys=True, separators=(',', ':')
        ).encode()).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            network = os.path.join(directory, 'network.nnue')
            Path(network).write_bytes(network_bytes)
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError, 'network.*bytes'
            ):
                worker.render_datagen_command(
                    cfg, os.path.join('Datagen', 'chunk.bin'), network
                )

    def test_v41_worker_rejects_semantic_contract_drift_even_when_rehashed(self):
        cfg, _ = publication_config(tablebase=True)
        data = cfg.workload['test']['datagen']
        data['publication_contract']['generation']['base_seed'] += 1
        data['publication_contract_sha256'] = hashlib.sha256(json.dumps(
            data['publication_contract'],
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()).hexdigest()
        data['environment_lease']['publication_contract_sha256'] = data[
            'publication_contract_sha256'
        ]
        data['environment_lease_sha256'] = hashlib.sha256(json.dumps(
            data['environment_lease'],
            sort_keys=True,
            separators=(',', ':'),
        ).encode()).hexdigest()

        with self.assertRaisesRegex(
            worker.DatagenConfigurationError, 'does not match'
        ):
            worker.datagen_tablebase_attestation(cfg)

    def test_v41_report_carries_publication_binding_without_local_paths(self):
        cfg, _ = publication_config()
        attestation = worker.datagen_tablebase_attestation(cfg)
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {'completed_chunks': 1, 'total_chunks': 1}
        with tempfile.NamedTemporaryFile(delete=False) as output:
            output.write(b'archive')
            path = output.name
        try:
            with mock.patch.object(
                worker.requests, 'post', return_value=response
            ) as post:
                worker.ServerReporter.report_datagen(
                    cfg,
                    path,
                    hashlib.sha256(b'archive').hexdigest(),
                    len(b'archive'),
                    tablebase=attestation,
                )
            payload = post.call_args.kwargs['data']
            self.assertEqual(
                payload['publication_contract_sha256'],
                cfg.workload['test']['datagen'][
                    'publication_contract_sha256'
                ],
            )
            self.assertNotIn('path', json.dumps(payload).lower())
        finally:
            os.remove(path)

    def test_atomic_v40_rejects_seven_man_even_with_matching_worker_inventory(self):
        cfg = tablebase_config()
        cfg.atomic_syzygy_max = 7
        test = cfg.workload['test']
        data = test['datagen']
        test['syzygy_wdl'] = '7-MAN'
        data['tablebase_max'] = 7
        data['environment_lease']['tablebase']['required_max'] = 7
        data['environment_lease']['tablebase']['worker_max'] = 7
        data['environment_lease_sha256'] = hashlib.sha256(
            json.dumps(
                data['environment_lease'],
                sort_keys=True,
                separators=(',', ':'),
            ).encode()
        ).hexdigest()

        with self.assertRaisesRegex(
            worker.DatagenConfigurationError, 'malformed or unsupported'
        ):
            worker.datagen_tablebase_attestation(cfg)

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

    def test_template_fails_closed_until_producer_is_authenticated(self):
        cfg = config()
        cfg.workload['test']['datagen'].update({
            'command': (
                'generate {SEED} {COUNT} {THREADS} {OUT} '
                'producer {PRODUCER_SHA256}'
            ),
            'producer_artifact_required': True,
        })

        with self.assertRaisesRegex(
            worker.DatagenConfigurationError, 'authenticated producer'
        ):
            worker.render_datagen_command(cfg, 'Datagen/chunk.bin')

        rendered = worker.render_datagen_command(
            cfg,
            'Datagen/chunk.bin',
            producer={
                'sha256': 'ab' * 32,
                'bytes': 123,
                'commit': 'cd' * 20,
            },
        )
        self.assertIn('producer ' + ('ab' * 32), rendered)

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

    def test_template_can_request_canonical_lowercase_asset_identities(self):
        cfg = config()
        cfg.workload['test']['book'] = {
            'name': 'crazyhouse.epd',
            'sha': '1' * 64,
            'raw_sha': 'ABCDEF0123456789' * 4,
            'source': 'https://example.test/crazyhouse.zip',
        }
        cfg.workload['test']['datagen']['command'] = (
            'generate book_sha256 {BOOK_SHA256_CANONICAL} '
            'network_sha256 {NETWORK_SHA256_CANONICAL} '
            'out {OUT} seed {SEED} count {COUNT} threads {THREADS}'
        )
        rendered = worker.render_datagen_command(
            cfg, os.path.join('Datagen', 'chunk.bin')
        )
        self.assertIn(
            'book_sha256 ' + ('abcdef0123456789' * 4), rendered
        )
        self.assertIn('network_sha256 none', rendered)

    def test_tablebase_template_uses_frozen_authenticated_lease(self):
        cfg = tablebase_config()
        rendered = worker.render_datagen_command(
            cfg, os.path.join('Datagen', 'chunk.bin')
        )
        self.assertIn('syzygy "C:/Atomic Tables/combined"', rendered)
        self.assertIn('syzygy_manifest_sha256 ' + ('b' * 64), rendered)
        self.assertIn('syzygy_max 6', rendered)
        self.assertIn('teacher_mode pure', rendered)

    def test_tablebase_template_fails_closed_on_capability_mismatch(self):
        cfg = tablebase_config()
        cfg.atomic_syzygy_max = 5
        with self.assertRaisesRegex(
            worker.DatagenConfigurationError, 'requires 6-man'
        ):
            worker.render_datagen_command(cfg, 'Datagen/chunk.bin')

        cfg = tablebase_config()
        cfg.atomic_syzygy_manifest_sha256 = 'd' * 64
        with self.assertRaisesRegex(
            worker.DatagenConfigurationError, 'pinned manifest'
        ):
            worker.render_datagen_command(cfg, 'Datagen/chunk.bin')

        cfg = tablebase_config('')
        with self.assertRaisesRegex(
            worker.DatagenConfigurationError, 'malformed or unsupported'
        ):
            worker.render_datagen_command(cfg, 'Datagen/chunk.bin')

    def test_tablebase_report_sends_attestation_without_local_path(self):
        cfg = tablebase_config('true')
        attestation = worker.datagen_tablebase_attestation(cfg)
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                'completed_chunks': 1,
                'total_chunks': 8,
                'environment_receipt_sha256': 'e' * 64,
            },
        )
        with tempfile.TemporaryDirectory() as cwd:
            chunk = Path(cwd, 'chunk.bz2')
            chunk.write_bytes(b'BZh9')
            with mock.patch.object(
                worker.requests, 'post', return_value=response
            ) as post:
                body = worker.ServerReporter.report_datagen(
                    cfg,
                    str(chunk),
                    'f' * 64,
                    4,
                    tablebase=attestation,
                )

        sent = post.call_args.kwargs['data']
        self.assertEqual(sent['tablebase_family'], 'atomic')
        self.assertEqual(sent['tablebase_max'], 6)
        self.assertEqual(sent['tablebase_worker_max'], 6)
        self.assertEqual(sent['tablebase_manifest_sha256'], 'b' * 64)
        self.assertEqual(sent['teacher_mode'], 'true')
        self.assertNotIn('path', sent)
        self.assertNotIn('C:\\Atomic', repr(sent))
        self.assertEqual(body['environment_receipt_sha256'], 'e' * 64)

    def test_prelaunch_revalidation_rejects_inventory_mutated_after_startup(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, '6-wdl')
            runtime = Path(root, 'combined')
            source.mkdir()
            runtime.mkdir()
            source_file = source / 'KPPPPvK.atbw'
            source_file.write_bytes(b'good')
            os.link(source_file, runtime / source_file.name)
            inventory = [{
                'directory': source.name,
                'name': source_file.name,
                'bytes': 4,
            }]
            raw_manifest = json.dumps(inventory).encode()
            manifest = Path(root, 'inventory.json')
            manifest.write_bytes(raw_manifest)
            manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
            marker = source / '.acquisition-complete.json'
            marker.write_text(json.dumps({
                'schema': 'atomic-syzygy-acquisition-v1',
                'directory': source.name,
                'files': 1,
                'bytes': 4,
                'source_inventory_sha256': manifest_sha256,
                'official_md5_verification': 'pass',
            }))

            cfg = tablebase_config()
            cfg.atomic_syzygy_path = str(runtime)
            cfg.atomic_syzygy_manifest = str(manifest)
            cfg.atomic_syzygy_manifest_sha256 = manifest_sha256
            data = cfg.workload['test']['datagen']
            data['tablebase_manifest_sha256'] = manifest_sha256
            data['environment_lease']['tablebase'][
                'manifest_sha256'
            ] = manifest_sha256
            data['environment_lease_sha256'] = hashlib.sha256(
                json.dumps(
                    data['environment_lease'],
                    sort_keys=True,
                    separators=(',', ':'),
                ).encode()
            ).hexdigest()
            attestation = worker.datagen_tablebase_attestation(cfg)

            # Simulate same-size corruption after startup/capability advert.
            source_file.write_bytes(b'evil')
            marker_mtime = marker.stat().st_mtime_ns
            os.utime(
                source_file,
                ns=(source_file.stat().st_atime_ns, marker_mtime + 1_000_000),
            )
            with mock.patch.object(
                worker, 'validate_syzygy_exists', return_value=True
            ), self.assertRaisesRegex(
                worker.DatagenConfigurationError,
                'failed pre-launch revalidation',
            ) as raised:
                worker.revalidate_datagen_tablebase_inventory(cfg, attestation)
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(root, str(raised.exception))

    def test_datagen_command_log_is_hashed_and_never_contains_local_path(self):
        cfg = tablebase_config()
        cfg.atomic_syzygy_manifest = 'inventory.json'
        attestation = worker.datagen_tablebase_attestation(cfg)
        heartbeat = SimpleNamespace(stop_requested=threading.Event())
        process = SimpleNamespace(
            stdin=SimpleNamespace(
                write=mock.Mock(), flush=mock.Mock(), close=mock.Mock()
            ),
            poll=mock.Mock(return_value=0),
            returncode=0,
        )

        with tempfile.TemporaryDirectory() as cwd:
            output = os.path.join(cwd, 'chunk.bin')
            log = os.path.join(cwd, 'engine.log')
            Path(output).write_bytes(b'data')
            rendered = worker.render_datagen_command(
                cfg, output, tablebase=attestation
            )
            command_sha256 = hashlib.sha256(rendered.encode()).hexdigest()
            with mock.patch.object(
                worker, 'revalidate_datagen_tablebase_inventory'
            ) as revalidate, mock.patch.object(
                worker, 'Popen', return_value=process
            ), mock.patch('builtins.print') as printed:
                worker.run_datagen_command(
                    cfg,
                    'engine.exe',
                    output,
                    log,
                    heartbeat,
                    tablebase=attestation,
                )

        revalidate.assert_called_once_with(cfg, attestation)
        messages = '\n'.join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn(command_sha256, messages)
        self.assertNotIn(cfg.atomic_syzygy_path, messages)
        self.assertNotIn(cfg.atomic_syzygy_path.replace('\\', '/'), messages)
        self.assertNotIn(rendered, messages)

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

    def test_opening_book_adapter_accepts_legacy_three_argument_helper(self):
        calls = []
        content = b'atomic opening\r\n'

        def legacy(book_sha, book_source, book_name):
            calls.append((book_sha, book_source, book_name))

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                Path('Books').mkdir()
                Path('Books', 'atomic.epd').write_bytes(content)
                with mock.patch.object(worker, 'download_opening_book', legacy):
                    worker.download_opening_book_compatible(
                        'normalized',
                        'source',
                        'atomic.epd',
                        hashlib.sha256(content).hexdigest(),
                    )
            finally:
                os.chdir(previous)

        self.assertEqual(calls, [('normalized', 'source', 'atomic.epd')])

    def test_opening_book_adapter_enforces_raw_sha_with_legacy_helper(self):
        def legacy(book_sha, book_source, book_name):
            pass

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                Path('Books').mkdir()
                book = Path('Books', 'atomic.epd')
                book.write_bytes(b'wrong bytes')
                with mock.patch.object(worker, 'download_opening_book', legacy):
                    with self.assertRaisesRegex(
                        worker.OpenBenchCorruptedBookException, 'Invalid raw sha'
                    ):
                        worker.download_opening_book_compatible(
                            'normalized', 'source', book.name, '0' * 64
                        )
                self.assertFalse(book.exists())
            finally:
                os.chdir(previous)

    def test_opening_book_adapter_passes_raw_sha_to_modern_helper(self):
        calls = []

        def modern(book_sha, book_source, book_name, book_raw_sha=None):
            calls.append((book_sha, book_source, book_name, book_raw_sha))

        with mock.patch.object(worker, 'download_opening_book', modern):
            worker.download_opening_book_compatible(
                'normalized', 'source', 'atomic.epd', 'raw'
            )

        self.assertEqual(
            calls, [('normalized', 'source', 'atomic.epd', 'raw')]
        )

    def test_opening_book_adapter_uses_keyword_for_kwargs_helper(self):
        calls = []

        def modern(book_sha, book_source, book_name, **kwargs):
            calls.append((book_sha, book_source, book_name, kwargs))

        with mock.patch.object(worker, 'download_opening_book', modern):
            worker.download_opening_book_compatible(
                'normalized', 'source', 'atomic.epd', 'raw'
            )

        self.assertEqual(
            calls,
            [(
                'normalized',
                'source',
                'atomic.epd',
                {'book_raw_sha': 'raw'},
            )],
        )

    def test_opening_book_adapter_does_not_hide_modern_internal_type_error(self):
        def modern(book_sha, book_source, book_name, book_raw_sha=None):
            raise TypeError('modern helper failed internally')

        with mock.patch.object(worker, 'download_opening_book', modern):
            with self.assertRaisesRegex(TypeError, 'failed internally'):
                worker.download_opening_book_compatible(
                    'normalized', 'source', 'atomic.epd', 'raw'
                )

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

    def test_complete_workload_benches_play_role_and_runs_producer_role(self):
        cfg = config()
        cfg.threads = 30
        captured = {}
        payload = b'opaque training records'
        source_identity = hashlib.sha256(payload).hexdigest(), len(payload)

        def generate(
            _config, engine, output_path, _log_path, _heartbeat, network_path
        ):
            captured['engine'] = engine
            captured['network'] = network_path
            with open(output_path, 'wb') as output:
                output.write(payload)
            return {
                'file_sha256': source_identity[0],
                'file_bytes': source_identity[1],
            }

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
                     mock.patch.object(
                         worker,
                         'safe_download_engine',
                         side_effect=['play.exe', 'producer.exe'],
                     ) as build, \
                     mock.patch.object(worker, 'safe_run_benchmarks', return_value=1000) as bench, \
                     mock.patch.object(worker, 'run_datagen_command', side_effect=generate), \
                     mock.patch.object(
                         worker,
                         'compress_datagen_output',
                         wraps=worker.compress_datagen_output,
                     ) as compress, \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(worker.ServerReporter, 'report_datagen', side_effect=upload):
                    worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(captured['payload'], payload)
        self.assertEqual(
            captured['engine'], os.path.join('Engines', 'producer.exe')
        )
        self.assertEqual(captured['network'], os.path.join('Networks', '12345678'))
        self.assertEqual(
            compress.call_args.kwargs['expected_source'], source_identity
        )
        self.assertEqual(build.call_count, 2)
        self.assertEqual(
            [call.kwargs['build_role'] for call in build.call_args_list],
            ['play', 'datagen'],
        )
        bench.assert_called_once()
        self.assertEqual(bench.call_args.args[1], 'dev')
        self.assertEqual(bench.call_args.args[2], 'play.exe')
        self.assertEqual(bench.call_args.kwargs, {'bench_threads': 1})

    def test_required_producer_is_uploaded_before_generator_and_bound_to_chunk(self):
        cfg = config()
        cfg.workload['test']['datagen'].update({
            'command': (
                'generate {SEED} {COUNT} {THREADS} {OUT} '
                'producer {PRODUCER_SHA256}'
            ),
            'producer_artifact_required': True,
        })
        producer_bytes = b'exact-generator-executable'
        producer_sha = hashlib.sha256(producer_bytes).hexdigest()
        calls = []
        real_hash = worker.datagen_file_sha256

        def record_hash(path):
            if '.producer' in os.fspath(path):
                calls.append('snapshot-hash')
            return real_hash(path)

        def register(
            _config, path, sha256, byte_count, commit, metadata_only=False
        ):
            if metadata_only:
                self.assertIsNone(path)
                return {'upload_required': True}
            calls.append('producer')
            self.assertEqual(Path(path).read_bytes(), producer_bytes)
            self.assertTrue(os.fspath(path).endswith('.exe'))
            self.assertEqual(sha256, producer_sha)
            self.assertEqual(byte_count, len(producer_bytes))
            self.assertEqual(commit, 'a' * 40)
            # A mutable build-cache path may change after publication. The
            # generator must still execute the authenticated private snapshot.
            Path('Engines', 'producer.exe').write_bytes(b'replaced-engine-b')
            return {
                'sha256': sha256,
                'bytes': byte_count,
                'commit': commit,
            }

        def generate(
            _config, engine, output_path, _log_path, _heartbeat,
            _network_path, producer,
        ):
            calls.append('generator')
            self.assertEqual(producer['sha256'], producer_sha)
            self.assertNotEqual(
                engine, os.path.join('Engines', 'producer.exe')
            )
            self.assertTrue(engine.endswith('.exe'))
            self.assertEqual(Path(engine).read_bytes(), producer_bytes)
            self.assertEqual(
                Path('Engines', 'producer.exe').read_bytes(),
                b'replaced-engine-b',
            )
            Path(output_path).write_bytes(b'v3-envelope')

        def upload(_config, _path, _sha256, _byte_count, producer):
            calls.append('chunk')
            self.assertEqual(producer['sha256'], producer_sha)
            return {'completed_chunks': 1, 'total_chunks': 8}

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            Path('Datagen').mkdir()
            Path('Engines').mkdir()
            Path('Engines', 'producer.exe').write_bytes(producer_bytes)
            try:
                with mock.patch.object(worker, 'download_opening_book'), \
                     mock.patch.object(
                         worker, 'safe_download_network_weights', return_value=None
                     ), \
                     mock.patch.object(
                         worker,
                         'safe_download_engine',
                         side_effect=['play.exe', 'producer.exe'],
                     ), \
                     mock.patch.object(
                         worker, 'safe_run_benchmarks', return_value=1000
                     ), \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(
                         worker.ServerReporter,
                         'report_datagen_producer',
                         side_effect=register,
                     ), \
                     mock.patch.object(
                         worker, 'datagen_file_sha256', side_effect=record_hash
                     ), \
                     mock.patch.object(
                         worker, 'run_datagen_command', side_effect=generate
                     ), \
                     mock.patch.object(
                         worker.ServerReporter, 'report_datagen', side_effect=upload
                     ):
                    worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(
            calls,
            [
                'snapshot-hash', 'producer', 'snapshot-hash',
                'generator', 'chunk',
            ],
        )

    def test_producer_snapshot_is_private_executable_and_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'engine')
            target = Path(directory, 'chunk.producer')
            source.write_bytes(b'producer-a')
            source.chmod(0o755)

            identity = worker.snapshot_datagen_producer(source, target)

            self.assertEqual(target.read_bytes(), b'producer-a')
            self.assertEqual(identity, worker.datagen_file_sha256(target))
            if os.name != 'nt':
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_producer_snapshot_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'engine')
            link = Path(directory, 'engine-link')
            target = Path(directory, 'snapshot')
            source.write_bytes(b'producer-a')
            try:
                link.symlink_to(source)
            except OSError as error:
                self.skipTest('symlink creation unavailable: %s' % error)

            with self.assertRaisesRegex(OSError, 'not a regular file'):
                worker.snapshot_datagen_producer(link, target)
            self.assertFalse(target.exists())

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
                        'upload failed after %d attempts'
                        % worker.DATAGEN_TRANSFER_RETRIES,
                    ):
                        worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(upload.call_count, worker.DATAGEN_TRANSFER_RETRIES)
        self.assertEqual(report.call_count, worker.DATAGEN_TRANSFER_RETRIES)
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
                        'bzip2 compression failed after %d attempts'
                        % worker.DATAGEN_TRANSFER_RETRIES,
                    ):
                        worker.complete_workload(cfg)
            finally:
                os.chdir(previous)

        self.assertEqual(compress.call_count, worker.DATAGEN_TRANSFER_RETRIES)
        report.assert_called_once()
        self.assertIn('transient failure', report.call_args.args[1])
        self.assertEqual(cfg.blacklist, [])

    def test_compression_binds_the_validated_source_bytes(self):
        heartbeat = SimpleNamespace(stop_requested=threading.Event())
        payload = b'validated Horde dataset bytes'
        expected = hashlib.sha256(payload).hexdigest(), len(payload)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'chunk.bin')
            target = Path(directory, 'chunk.bin.bz2')
            source.write_bytes(payload)
            observed = worker.compress_datagen_output(
                str(source),
                str(target),
                heartbeat,
                expected_source=expected,
            )
            with bz2.open(target, 'rb') as compressed:
                restored = compressed.read()

        self.assertEqual(observed, expected)
        self.assertEqual(restored, payload)

    def test_compression_rejects_source_replaced_after_validation(self):
        heartbeat = SimpleNamespace(stop_requested=threading.Event())
        validated = b'validated Horde dataset bytes'
        expected = hashlib.sha256(validated).hexdigest(), len(validated)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'chunk.bin')
            target = Path(directory, 'chunk.bin.bz2')
            source.write_bytes(b'replaced after validation')
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError,
                'source bytes changed after format validation',
            ):
                worker.compress_datagen_output(
                    str(source),
                    str(target),
                    heartbeat,
                    expected_source=expected,
                )
            self.assertFalse(target.exists())

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
        self.assertEqual(post.call_args.kwargs['data']['attempt'], 1)

    def test_producer_report_carries_exact_lease_commit_and_binary(self):
        cfg = config()
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                'sha256': 'ab' * 32,
                'bytes': 4,
                'commit': 'cd' * 20,
            },
        )
        with tempfile.TemporaryDirectory() as cwd:
            binary = Path(cwd, 'producer.bin')
            binary.write_bytes(b'ELF!')
            with mock.patch.object(
                worker.requests, 'post', return_value=response
            ) as post:
                body = worker.ServerReporter.report_datagen_producer(
                    cfg, str(binary), 'ab' * 32, 4, 'cd' * 20
                )

        sent = post.call_args.kwargs['data']
        self.assertEqual(sent['test_id'], 7)
        self.assertEqual(sent['chunk_idx'], 3)
        self.assertEqual(sent['attempt'], 1)
        self.assertEqual(sent['metadata_only'], 0)
        self.assertEqual(sent['machine_id'], 11)
        self.assertEqual(sent['secret'], 'secret')
        self.assertEqual(sent['commit'], 'cd' * 20)
        self.assertEqual(body['sha256'], 'ab' * 32)

    def test_producer_metadata_probe_has_no_file_payload(self):
        cfg = config()
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                'sha256': 'ab' * 32,
                'bytes': 4,
                'commit': 'cd' * 20,
                'upload_required': False,
            },
        )
        with mock.patch.object(
            worker.requests, 'post', return_value=response
        ) as post:
            body = worker.ServerReporter.report_datagen_producer(
                cfg, None, 'ab' * 32, 4, 'cd' * 20, metadata_only=True
            )

        self.assertNotIn('files', post.call_args.kwargs)
        sent = post.call_args.kwargs['data']
        self.assertEqual(sent['attempt'], 1)
        self.assertEqual(sent['metadata_only'], 1)
        self.assertFalse(body['upload_required'])

    def test_cached_producer_is_bound_without_resending_binary(self):
        cfg = config()
        heartbeat = SimpleNamespace(stop_requested=threading.Event())
        identity = {
            'sha256': 'ab' * 32,
            'bytes': 4,
            'commit': 'cd' * 20,
            'upload_required': False,
        }
        with mock.patch.object(
            worker.ServerReporter,
            'report_datagen_producer',
            return_value=identity,
        ) as report:
            result = worker.upload_datagen_producer(
                cfg, 'must-not-open.bin', 'ab' * 32, 4, 'cd' * 20,
                heartbeat,
            )

        self.assertEqual(result, {
            'sha256': 'ab' * 32,
            'bytes': 4,
            'commit': 'cd' * 20,
        })
        report.assert_called_once_with(
            cfg, None, 'ab' * 32, 4, 'cd' * 20, metadata_only=True
        )

    def test_bad_version_stops_producer_upload_without_retry(self):
        cfg = config()
        heartbeat = SimpleNamespace(stop_requested=threading.Event())
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {'error': 'Bad Client Version: expected 40'},
        )
        with mock.patch.object(
            worker.requests, 'post', return_value=response
        ) as post:
            with self.assertRaises(worker.BadVersionException):
                worker.upload_datagen_producer(
                    cfg, 'unused.bin', 'ab' * 32, 4, 'cd' * 20,
                    heartbeat,
                )
        post.assert_called_once()

    def test_chunk_report_carries_attempt_and_exact_producer_binding(self):
        cfg = config()
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {'completed_chunks': 1, 'total_chunks': 8},
        )
        producer = {
            'sha256': 'ab' * 32,
            'bytes': 4,
            'commit': 'cd' * 20,
        }
        with tempfile.TemporaryDirectory() as cwd:
            chunk = Path(cwd, 'chunk.bz2')
            chunk.write_bytes(b'BZh9')
            with mock.patch.object(
                worker.requests, 'post', return_value=response
            ) as post:
                worker.ServerReporter.report_datagen(
                    cfg, str(chunk), 'ef' * 32, 4, producer
                )

        sent = post.call_args.kwargs['data']
        self.assertEqual(sent['test_id'], 7)
        self.assertEqual(sent['chunk_idx'], 3)
        self.assertEqual(sent['attempt'], 1)
        self.assertEqual(sent['producer_sha256'], 'ab' * 32)
        self.assertEqual(sent['producer_bytes'], 4)
        self.assertEqual(sent['producer_commit'], 'cd' * 20)

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
        source_tree = 'b' * 40
        src_tree = 'c' * 40
        command = worker.makefile_command(
            os.path.join('Networks', '12345678'),
            '.',
            'engine',
            'g++',
            sha,
            'datagen',
            {
                'source_tree': source_tree,
                'src_tree': src_tree,
            },
        )
        self.assertIn('GIT_SHA_FULL=%s' % sha, command)
        self.assertIn('OPENBENCH_DATAGEN=1', command)
        self.assertIn('DATAGEN_SOURCE_COMMIT=%s' % sha, command)
        self.assertIn('DATAGEN_SOURCE_TREE=%s' % source_tree, command)
        self.assertIn('DATAGEN_SRC_TREE=%s' % src_tree, command)
        self.assertIn('DATAGEN_SOURCE_DIRTY=0', command)

        play_command = worker.makefile_command(
            None, '.', 'engine', 'g++', sha
        )
        self.assertIn('GIT_SHA_FULL=%s' % sha, play_command)
        self.assertNotIn('OPENBENCH_DATAGEN=1', play_command)
        self.assertFalse(any(
            argument.startswith('DATAGEN_SOURCE_')
            or argument.startswith('DATAGEN_SRC_TREE=')
            for argument in play_command
        ))

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
        provenance = {'source_tree': 'b' * 40, 'src_tree': 'c' * 40}
        cfg.workload['test']['dev']['build']['datagen_provenance'] = provenance
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
        self.assertEqual(download.call_args.args[9], provenance)

    def test_safe_download_engine_can_select_play_role_for_datagen_bench(self):
        cfg = config()
        cfg.workload['test']['dev']['build']['datagen_provenance'] = {
            'source_tree': 'b' * 40,
            'src_tree': 'c' * 40,
        }
        with mock.patch.object(
            worker, 'download_public_engine', return_value='engine.exe'
        ) as download:
            result = worker.safe_download_engine(
                cfg,
                'dev',
                os.path.join('Networks', '12345678'),
                build_role='play',
            )

        self.assertEqual(result, 'engine.exe')
        self.assertFalse(download.call_args.args[5].endswith('-DATAGEN'))
        self.assertEqual(download.call_args.args[8], 'play')
        self.assertIsNone(download.call_args.args[9])

    def test_datagen_build_rejects_malformed_archive_provenance(self):
        with self.assertRaisesRegex(
            ValueError, 'Invalid DATAGEN archive provenance identity'
        ):
            worker.makefile_command(
                None,
                '.',
                'engine',
                'g++',
                'a' * 40,
                'datagen',
                {'source_tree': 'not-a-tree', 'src_tree': 'c' * 40},
            )

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

    def test_private_generic_datagen_selects_explicit_datagen_artifact_role(self):
        cfg = config()
        cfg.workload['test']['dev']['private'] = True
        cfg.workload['test']['dev']['build'] = {
            'artifact_roles': ['play', 'datagen']
        }
        with mock.patch.object(
            worker, 'download_private_engine', return_value='engine.exe'
        ) as download:
            result = worker.safe_download_engine(
                cfg, 'dev', os.path.join('Networks', '12345678')
            )

        self.assertEqual(result, 'engine.exe')
        self.assertEqual(download.call_args.args[-1], 'datagen')
        self.assertTrue(download.call_args.args[3].endswith('-DATAGEN'))

    def test_private_generic_datagen_rejects_play_only_engine(self):
        cfg = config()
        cfg.workload['test']['dev']['private'] = True
        cfg.workload['test']['dev']['build'] = {'artifact_roles': ['play']}
        with mock.patch.object(
            worker, 'download_private_engine', return_value='engine.exe'
        ) as download:
            with self.assertRaisesRegex(
                worker.DatagenConfigurationError,
                'does not publish a datagen artifact role',
            ):
                worker.safe_download_engine(
                    cfg, 'dev', os.path.join('Networks', '12345678')
                )

        download.assert_not_called()

    def test_private_artifact_selection_never_crosses_roles(self):
        artifacts = {
            'horde-stockfish-linux-avx2-pext-play': {'name': 'play'},
            'horde-stockfish-linux-avx2-pext-datagen': {'name': 'datagen'},
        }
        flags = ['SSSE3', 'SSE41', 'SSE42', 'AVX', 'AVX2', 'FMA', 'BMI2']
        with mock.patch.object(
            worker.client_utils.platform, 'system', return_value='Linux'
        ):
            play = worker.select_best_artifact(
                artifacts, 'Intel CPU', flags, 'play'
            )
            datagen = worker.select_best_artifact(
                artifacts, 'Intel CPU', flags, 'datagen'
            )

        self.assertEqual(play['name'], 'play')
        self.assertEqual(datagen['name'], 'datagen')

    def test_private_datagen_rejects_legacy_untagged_artifact(self):
        artifacts = {
            'horde-stockfish-linux-avx2-pext': {'name': 'legacy'},
        }
        flags = ['SSSE3', 'SSE41', 'SSE42', 'AVX', 'AVX2', 'FMA', 'BMI2']
        with mock.patch.object(
            worker.client_utils.platform, 'system', return_value='Linux'
        ):
            self.assertEqual(
                worker.select_best_artifact(
                    artifacts, 'Intel CPU', flags, 'play'
                )['name'],
                'legacy',
            )
            with self.assertRaises(worker.OpenBenchMissingArtifactException):
                worker.select_best_artifact(
                    artifacts, 'Intel CPU', flags, 'datagen'
                )


if __name__ == '__main__':
    unittest.main()
