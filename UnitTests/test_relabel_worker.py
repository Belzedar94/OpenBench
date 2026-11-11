import bz2
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Client'))

import relabel
import worker


# Three consecutive ALICEV2_RECORD_V1 records from T349 chunk 0, with the FEN
# the Alice trainer's alice_v2_record.unpack_record produces for each and the
# stored depth-6 score. '|' marks a piece standing on board B.
T349_RECORDS = [
    (
        '04020300060300040101010100010101000000000000000000000000110200000000'
        '190005000000190000001900000000091d09000909090c0a0b000e0b0a0cfc0e0000'
        '36ff090001010f0103000500c10c3979',
        'rnb1kbnr/1p|qp1ppp/|p3|p3/2|p1Q3/4|PN2/8/PPPP1PPP/RNB1KB1R b KQkq - 3 5',
        -202,
    ),
    (
        '04020300060300040101010100010101000000000000000000000000110200000000'
        '190005000000190000001900000000091d09000909090c0a0b1e000b0a0cdd090000'
        '01010a00ff0003010400060023322107',
        'rnb|k1bnr/1p|qp1ppp/|p3|p3/2|p1Q3/4|PN2/8/PPPP1PPP/RNB1KB1R w KQ - 4 6',
        257,
    ),
    (
        '04020300060300040101010100010101000000000000000000000000110000000000'
        '190005000012190000001900000000091d09000909090c0a0b1e000b0a0cfe0b0000'
        '6eff0b000101030105000600c50c6815',
        'rnb|k1bnr/1p|qp1ppp/|p3|p3/2|p1Q2|N/4|P3/8/PPPP1PPP/RNB1KB1R b KQ - 5 6',
        -146,
    ),
]

FMT = dict(relabel.FORMATS['alice-v2-record-v1'], name='alice-v2-record-v1')


def records():
    return b''.join(bytes.fromhex(hex_) for hex_, _fen, _score in T349_RECORDS)


def with_crc(record):
    record = bytearray(record)
    struct.pack_into('<I', record, 80, zlib.crc32(bytes(record[:80])) & 0xFFFFFFFF)
    return bytes(record)


# A stand-in UCI engine: reports a fail-high line first, then an exact score
# derived from the FEN, so the tests can tell which line was used.
FAKE_ENGINE = textwrap.dedent('''
    import sys
    fen = ''
    for line in sys.stdin:
        line = line.strip()
        if line == 'uci':
            print('id name fake'); print('uciok', flush=True)
        elif line == 'isready':
            print('readyok', flush=True)
        elif line.startswith('position fen '):
            fen = line[len('position fen '):]
        elif line.startswith('go'):
            if 'MATE' in fen:
                print('info depth 3 score mate 2 pv a1a2')
            else:
                print('info depth 3 score cp 9999 lowerbound nodes 1')
                print('info depth 3 multipv 2 score cp -77 nodes 1')
                print('info depth 3 score cp %d nodes 5 pv a1a2' % (len(fen) % 40 - 20))
            print('bestmove a1a2', flush=True)
        elif line == 'quit':
            break
''')


class FakeEngineCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = os.path.join(self.tmp.name, 'fake_engine.py')
        Path(self.script).write_text(FAKE_ENGINE)

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self):
        script = self.script

        def popen(_argv, **kwargs):
            kwargs.pop('creationflags', None)
            return subprocess.Popen([sys.executable, script], **kwargs)

        return lambda: relabel.UciSearcher('unused', popen=popen)


class AliceCodecTests(unittest.TestCase):

    def test_decoder_reproduces_trainer_fens(self):
        for hex_, fen, _score in T349_RECORDS:
            decoded, material = relabel.alice_decode(bytes.fromhex(hex_))
            self.assertEqual(decoded, fen)
            self.assertGreater(material, 0)

    def test_stored_score_is_read_from_the_record(self):
        for hex_, _fen, score in T349_RECORDS:
            self.assertEqual(relabel.record_score(bytes.fromhex(hex_), FMT), score)

    def test_corrupt_or_impossible_records_are_refused(self):
        good = bytes.fromhex(T349_RECORDS[0][0])
        broken_crc = good[:80] + bytes(4)
        two_white_kings = bytearray(good)
        two_white_kings[20] = 6
        pawn_on_back_rank = bytearray(good)
        pawn_on_back_rank[0] = 1
        reserved_bits = bytearray(good)
        reserved_bits[0] |= 0x20
        bad_side = bytearray(good)
        bad_side[73] = 2
        for record in (
            broken_crc,
            with_crc(two_white_kings),
            with_crc(pawn_on_back_rank),
            with_crc(reserved_bits),
            with_crc(bad_side),
            good[:83],
        ):
            with self.assertRaises(relabel.RelabelFormatError):
                relabel.alice_decode(record)

    def test_rewrite_changes_only_score_and_checksum(self):
        original = bytes.fromhex(T349_RECORDS[1][0])
        rewritten = relabel.rewrite_score(original, FMT, -31999)
        self.assertEqual(relabel.record_score(rewritten, FMT), -31999)
        self.assertEqual(relabel.alice_decode(rewritten)[0], T349_RECORDS[1][1])
        changed = [i for i in range(84) if original[i] != rewritten[i]]
        self.assertTrue(set(changed) <= set(range(68, 70)) | set(range(80, 84)))
        with self.assertRaises(relabel.RelabelFormatError):
            relabel.rewrite_score(original, FMT, 32001)

    def test_verify_rejects_count_shift_key_and_checksum_changes(self):
        source = records()
        good = b''.join(
            relabel.rewrite_score(source[i:i + 84], FMT, 5) for i in range(0, len(source), 84)
        )
        self.assertEqual(relabel.verify_relabel_output(source, good, FMT), 3)
        moved = bytearray(good)
        moved[64] ^= 1  # the stored move is part of the key
        bad_crc = bytearray(good)
        bad_crc[69] ^= 0x01
        for output in (
            good[:-84],
            good[84:] + good[:84],
            with_crc(bytes(moved[:84])) + good[84:],
            bytes(bad_crc),
        ):
            with self.assertRaises(relabel.RelabelFormatError):
                relabel.verify_relabel_output(source, output, FMT)


class ScoreConversionTests(unittest.TestCase):

    def test_mate_scores_map_to_internal_mate_values(self):
        self.assertEqual(relabel.uci_score_to_internal('mate', 1, 40), 31999)
        self.assertEqual(relabel.uci_score_to_internal('mate', 3, 40), 31995)
        self.assertEqual(relabel.uci_score_to_internal('mate', -1, 40), -31998)
        self.assertEqual(relabel.uci_score_to_internal('mate', 0, 40), -32000)

    def test_centipawns_invert_the_engines_to_cp_within_rounding(self):
        for material in (5, 17, 40, 58, 78, 90):
            a = relabel.win_rate_a(material)
            for value in (-3000, -517, -1, 0, 1, 99, 328, 2500):
                cp = int(round(100 * value / a))
                back = relabel.uci_score_to_internal('cp', cp, material)
                self.assertLessEqual(abs(back - value), a / 200 + 1)

    def test_info_parser_reports_bound_and_multipv(self):
        self.assertEqual(
            relabel.parse_info_score('info depth 9 seldepth 12 multipv 1 score cp 31 nodes 9'),
            ('cp', 31, None, 1),
        )
        self.assertEqual(
            relabel.parse_info_score('info depth 9 score cp -12 upperbound nodes 9'),
            ('cp', -12, 'upperbound', 1),
        )
        self.assertEqual(
            relabel.parse_info_score('info depth 9 multipv 3 score mate -2'),
            ('mate', -2, None, 3),
        )
        self.assertIsNone(relabel.parse_info_score('info string NNUE loaded'))


class RelabelChunkTests(FakeEngineCase):

    def test_chunk_is_rescored_from_the_last_exact_primary_line(self):
        source = records()
        out = relabel.relabel_chunk(
            source, 'alice-v2-record-v1', 'unused', depth=3, engines=2,
            searcher_factory=self.factory(),
            progress=relabel.RelabelProgress(3, printer=lambda _line: None),
        )
        self.assertEqual(relabel.verify_relabel_output(source, out, FMT), 3)
        for index, (_hex, fen, _score) in enumerate(T349_RECORDS):
            record = out[index * 84:(index + 1) * 84]
            material = relabel.alice_decode(record)[1]
            expected = relabel.uci_score_to_internal('cp', len(fen) % 40 - 20, material)
            self.assertEqual(relabel.record_score(record, FMT), expected)

    def test_bad_record_fails_before_any_engine_starts(self):
        source = bytearray(records())
        source[84] = 0x6F  # unknown piece code
        started = []

        def factory():
            started.append(1)
            raise AssertionError('engine must not start')

        with self.assertRaises(relabel.RelabelFormatError):
            relabel.relabel_chunk(bytes(source), 'alice-v2-record-v1', 'unused',
                                  depth=3, searcher_factory=factory)
        self.assertEqual(started, [])

    def test_stop_request_aborts_the_chunk(self):
        with self.assertRaises(relabel.RelabelStopped):
            relabel.relabel_chunk(
                records(), 'alice-v2-record-v1', 'unused', depth=3,
                searcher_factory=self.factory(), should_stop=lambda: True,
                progress=relabel.RelabelProgress(3, printer=lambda _line: None),
            )

    def test_depth_and_nodes_are_mutually_exclusive(self):
        for depth, nodes in ((0, 0), (3, 1000)):
            with self.assertRaises(relabel.RelabelFormatError):
                relabel.relabel_chunk(records(), 'alice-v2-record-v1', 'unused',
                                      depth=depth, nodes=nodes)

    def test_dead_engine_is_an_engine_error(self):
        def popen(_argv, **kwargs):
            kwargs.pop('creationflags', None)
            return subprocess.Popen([sys.executable, '-c', 'pass'], **kwargs)

        with self.assertRaises(relabel.RelabelEngineError):
            relabel.relabel_chunk(
                records(), 'alice-v2-record-v1', 'unused', depth=3,
                searcher_factory=lambda: relabel.UciSearcher('unused', popen=popen),
            )


def relabel_config(data):
    compressed = bz2.compress(data)
    return SimpleNamespace(
        workload={
            'result': {'id': 9},
            'test': {
                'id': 7,
                'type': 'DATAGEN',
                'book': {'name': 'NONE', 'sha': None, 'raw_sha': None, 'source': None},
                'dev': {
                    'engine': 'Alice-Stockfish', 'name': 'main', 'sha': 'a' * 40,
                    'source': 'https://example.test/archive.zip', 'network': '9F9E5570',
                    'netname': 'net.nnue', 'private': False, 'bench': 1,
                    'build': {'path': 'src'}, 'options': 'Threads=1 Hash=8 "Use NNUE=true"',
                },
                'base': {'engine': 'Alice-Stockfish', 'name': 'main'},
                'datagen': {
                    'command': 'RELABEL set=s format=alice-v2-record-v1 depth=3 nodes=0',
                    'total_count': 3, 'positions_per_chunk': 3, 'chunk_idx': 0,
                    'chunk_count': 3, 'seed': 0, 'attempt': 1,
                    'relabel': {
                        'protocol': relabel.RELABEL_PROTOCOL, 'set': 's',
                        'format': 'alice-v2-record-v1', 'variant': 'alice',
                        'record_bytes': 84, 'depth': 3, 'nodes': 0,
                        'max_engines': 8,
                        'input': {
                            'file': 'chunk_0.bz2', 'records': 3,
                            'sha256': hashlib.sha256(compressed).hexdigest(),
                            'bytes': len(compressed),
                            'raw_sha256': hashlib.sha256(data).hexdigest(),
                            'keys_sha256': hashlib.sha256(
                                relabel.key_projection(data, FMT)).hexdigest(),
                        },
                    },
                },
            },
        },
        threads=2, blacklist=[], machine_id=11, secret_token='secret',
        server='http://localhost:8001', ram_total_mb=16384,
    ), compressed


class WorkerRelabelTests(FakeEngineCase):

    def test_capabilities_are_advertised(self):
        self.assertEqual(relabel.RELABEL_PROTOCOL, 1)
        self.assertIn('alice-v2-record-v1', relabel.FORMATS)

    def test_engine_options_force_one_thread_and_keep_the_rest(self):
        hash_mb, extra = worker.relabel_engine_options('Threads=4 Hash=64 "Use NNUE=true"')
        self.assertEqual(hash_mb, 64)
        self.assertEqual(extra, [('Use NNUE', 'true')])
        self.assertEqual(worker.relabel_engine_options(''), (16, []))

    def test_relabel_workload_builds_the_playing_engine(self):
        cfg, _ = relabel_config(records())
        cfg.compilers = {'Alice-Stockfish': ['g++']}
        with mock.patch.object(worker, 'download_public_engine',
                               return_value='engine.exe') as download:
            worker.safe_download_engine(cfg, 'dev', None)
        self.assertEqual(download.call_args.args[8], 'play')

    def test_complete_workload_downloads_relabels_verifies_and_uploads(self):
        source = records()
        cfg, compressed = relabel_config(source)
        uploaded = {}

        def fake_download(_config, path, sha256, byte_count):
            Path(path).write_bytes(compressed)

        def fake_upload(_config, path, sha256, byte_count, _heartbeat):
            uploaded['data'] = bz2.decompress(Path(path).read_bytes())
            return {'completed_chunks': 1, 'total_chunks': 1}

        real_chunk = relabel.relabel_chunk

        def chunk_with_fake_engine(*args, **kwargs):
            uploaded['engines'] = kwargs['engines']
            uploaded['hash'] = kwargs['hash_mb']
            kwargs['searcher_factory'] = self.factory()
            return real_chunk(*args, **kwargs)

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch.object(worker, 'safe_download_network_weights',
                                       return_value=os.path.join('Networks', '9F9E5570')), \
                     mock.patch.object(worker, 'safe_download_engine', return_value='engine.exe') as build, \
                     mock.patch.object(worker, 'safe_run_benchmarks', return_value=1000), \
                     mock.patch.object(worker, 'relabel_engine_count', side_effect=lambda c, n, r: r), \
                     mock.patch.object(worker, 'download_relabel_input', side_effect=fake_download), \
                     mock.patch.object(worker.client_relabel, 'relabel_chunk',
                                       side_effect=chunk_with_fake_engine), \
                     mock.patch.object(worker, "upload_relabel_output", side_effect=fake_upload), \
                     mock.patch.object(worker.ServerReporter, 'report_nps'), \
                     mock.patch.object(worker.DatagenHeartbeat, '_run'):
                    worker.complete_workload(cfg)
                leftovers = os.listdir('Datagen')
            finally:
                os.chdir(previous)

        build.assert_called_once()
        self.assertEqual(uploaded['engines'], 2)
        self.assertEqual(uploaded['hash'], 8)
        self.assertEqual(relabel.verify_relabel_output(source, uploaded['data'], FMT), 3)
        self.assertNotEqual(uploaded['data'], source)
        self.assertEqual(leftovers, [])

    def test_input_with_wrong_records_is_refused_before_searching(self):
        source = records()
        cfg, _ = relabel_config(source)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'in.bz2')
            Path(path).write_bytes(bz2.compress(source[:-84]))
            with self.assertRaises(relabel.RelabelFormatError):
                worker.read_relabel_input(
                    path, cfg.workload['test']['datagen']['relabel']['input'], 84
                )
            tampered = bytearray(source)
            tampered[70] ^= 1
            Path(path).write_bytes(bz2.compress(bytes(tampered)))
            with self.assertRaises(relabel.RelabelFormatError):
                worker.read_relabel_input(
                    path, cfg.workload['test']['datagen']['relabel']['input'], 84
                )

    def test_server_rejection_is_final_and_not_retried(self):
        cfg, _ = relabel_config(records())
        heartbeat = SimpleNamespace(stop_requested=threading.Event())
        rejection = RuntimeError('RELABEL chunk 0 rejected: Position keys differ')
        with mock.patch.object(worker.ServerReporter, 'report_datagen',
                               side_effect=rejection) as report:
            with self.assertRaises(worker.RelabelRejected):
                worker.upload_relabel_output(cfg, 'unused', 'a' * 64, 1, heartbeat)
        report.assert_called_once()


if __name__ == '__main__':
    unittest.main()
