import gzip
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc
from unittest import mock, skipUnless

from django.core.management import call_command
from django.test import (
    SimpleTestCase,
    override_settings,
)
from django.test.utils import CaptureQueriesContext

from . import conquest_map, ingest, logic
from .database import connection
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


def _key(number):
    return f'{number:064x}'


def _row(key, *, fen=None, eval_cp=None, status='UNKNOWN', closure=None,
         proof=None, best_move=None, depth_invested=0, nodes=0, seconds=0.0,
         visits=0, priority=0.0, expanded=False):
    return {
        'key': key,
        'fen': fen or logic.start_fen(),
        'eval_cp': eval_cp,
        'status': status,
        'closure': closure,
        'proof': proof,
        'best_move': best_move,
        'depth_invested': depth_invested,
        'nodes_invested': nodes,
        'time_invested': seconds,
        'visits': visits,
        'priority': priority,
        'expanded': expanded,
    }


class DisplayTreeProjectionTests(SimpleTestCase):

    def _diamond_with_cycle(self, prior=None, reverse=False):
        root, left, right, joined = (_key(index) for index in range(1, 5))
        rows = [
            _row(root, eval_cp=0, nodes=10),
            _row(left, eval_cp=20, nodes=20),
            _row(right, eval_cp=-20, nodes=30),
            _row(joined, eval_cp=10, nodes=40),
        ]
        edges = [
            (root, left, 'a1a2'),
            (root, right, 'b1b2'),
            (left, joined, 'a2a3'),
            (right, joined, 'c2c3'),
            (joined, root, 'd2d3'),  # reversible cycle back to start
        ]
        if reverse:
            rows.reverse()
            edges.reverse()
        with mock.patch(
                'atomicdb.conquest_map._san_for_edge',
                side_effect=lambda _fen, uci: f'SAN-{uci}'):
            snapshot = conquest_map.build_snapshot_data(
                rows, edges, prior_snapshot=prior, root_key=root,
            )
        return snapshot, (root, left, right, joined)

    def test_cycle_terminates_and_transposition_is_attributed_once(self):
        snapshot, (root, left, right, joined) = self._diamond_with_cycle()
        nodes = snapshot['nodes']

        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[joined]['r'], left)
        self.assertEqual(nodes[joined]['i'], 2)
        self.assertEqual(nodes[joined]['x'], 1)
        self.assertEqual(nodes[root]['i'], 1)
        self.assertEqual(nodes[root]['x'], 1)
        self.assertEqual(nodes[root]['m'][conquest_map.M_POSITIONS], 4)
        self.assertEqual(nodes[root]['m'][conquest_map.M_NODES], 100)
        child_positions = sum(
            nodes[key]['m'][conquest_map.M_POSITIONS]
            for key in nodes[root]['k']
        )
        self.assertEqual(
            child_positions + 1,
            nodes[root]['m'][conquest_map.M_POSITIONS],
        )

    def test_projection_is_deterministic_for_reordered_bulk_reads(self):
        first, _keys = self._diamond_with_cycle()
        second, _keys = self._diamond_with_cycle(reverse=True)

        # Timestamp is the only intentionally variable field.
        first['snapshot']['generated_at'] = second['snapshot']['generated_at']
        first = conquest_map.seal_snapshot(
            conquest_map._unsealed_copy(first))
        second = conquest_map.seal_snapshot(
            conquest_map._unsealed_copy(second))
        self.assertEqual(first, second)

    def test_existing_valid_display_parent_remains_stable(self):
        initial, (_root, _left, right, joined) = self._diamond_with_cycle()
        initial['nodes'][joined]['r'] = right
        stable, _keys = self._diamond_with_cycle(prior=initial)

        self.assertEqual(stable['nodes'][joined]['r'], right)

    def test_invalid_old_parent_is_ignored(self):
        prior, (_root, left, _right, joined) = self._diamond_with_cycle()
        prior['nodes'][joined]['r'] = _key(99)
        rebuilt, _keys = self._diamond_with_cycle(prior=prior)

        self.assertEqual(rebuilt['nodes'][joined]['r'], left)

    def test_relevant_frontier_excludes_historical_tombstones(self):
        root, child = _key(10), _key(11)
        with mock.patch('atomicdb.conquest_map._san_for_edge',
                        return_value='a3'):
            snapshot = conquest_map.build_snapshot_data(
                [_row(root), _row(child, priority=-1e9)],
                [(root, child, 'a2a3')],
                root_key=root,
            )
        metrics = snapshot['nodes'][root]['m']

        self.assertEqual(metrics[conquest_map.M_UNKNOWN], 2)
        self.assertEqual(metrics[conquest_map.M_FRONTIER], 1)
        self.assertEqual(metrics[conquest_map.M_HISTORICAL], 1)

    def test_non_finite_database_metrics_fail_closed(self):
        with self.assertRaises(conquest_map.SnapshotError):
            conquest_map.build_snapshot_data(
                [_row(_key(12), seconds=float('nan'))],
                [],
                root_key=_key(12),
            )

    def test_render_is_bounded_and_keeps_full_line_from_start(self):
        snapshot, (root, left, right, joined) = self._diamond_with_cycle()
        document = conquest_map.render_map(
            snapshot, left, weight='explored', limit=2, relative_depth=8)

        self.assertEqual(document['schema'], 'atomicdb.map.v1')
        self.assertLessEqual(document['marks'], 2)
        self.assertEqual(document['snapshot']['start_key'], root)
        self.assertEqual(document['request']['root'], left)
        self.assertEqual(document['root']['line_san'], '1. SAN-a1a2')
        self.assertEqual(document['root']['line_uci'], ['a1a2'])
        self.assertEqual(
            [item['key'] for item in document['lineage']['positions']],
            [root, left],
        )
        self.assertEqual(document['lineage']['line_san'], '1. SAN-a1a2')
        self.assertEqual(document['lineage']['line_uci'], ['a1a2'])
        self.assertEqual(
            [item['key'] for item in document['first_moves']],
            [left, right],
        )
        self.assertNotIn('children', document['first_moves'][0])
        self.assertEqual(
            document['root']['children'][0]['key'], joined)
        self.assertIn('metrics', document['root'])
        self.assertIn('transpositions', document['root'])

    def test_depth_limit_marks_node_as_zoomable_and_truncated(self):
        snapshot, (root, _left, _right, _joined) = self._diamond_with_cycle()
        document = conquest_map.render_map(
            snapshot, root, limit=600, relative_depth=1)

        self.assertEqual(document['marks'], 3)
        for child in document['root']['children']:
            if child['zoomable']:
                self.assertTrue(child['truncated'])
                self.assertGreater(child['hidden_children'], 0)

    def test_direct_10k_deep_root_materialises_line_once(self):
        count = 10_000
        rows = [_row(_key(index + 1)) for index in range(count)]
        edges = [
            (_key(index), _key(index + 1), 'a1a2')
            for index in range(1, count)
        ]
        with mock.patch(
                'atomicdb.conquest_map._san_for_edge',
                return_value='a2'):
            snapshot = conquest_map.build_snapshot_data(
                rows, edges, root_key=_key(1))

        with mock.patch(
                'atomicdb.conquest_map._san_token',
                wraps=conquest_map._san_token) as token:
            started = time.perf_counter()
            document = conquest_map.render_map(
                snapshot, _key(count), limit=1, relative_depth=1)
            elapsed = time.perf_counter() - started

        self.assertEqual(document['marks'], 1)
        self.assertEqual(len(document['root']['line_uci']), count - 1)
        self.assertEqual(
            len(document['lineage']['positions']), count)
        self.assertTrue(document['root']['line_san'].startswith('1. a2 a2'))
        self.assertEqual(token.call_count, count - 1)
        # The old tuple/string-per-ancestor implementation was quadratic.
        # One linear walk/join is comfortably below this generous CI budget.
        self.assertLess(elapsed, 1.0)


class SnapshotArtifactTests(SimpleTestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'map.json.gz'
        root = _key(20)
        self.snapshot = conquest_map.build_snapshot_data(
            [_row(root)], [], root_key=root,
        )

    def tearDown(self):
        conquest_map.reset_snapshot_cache()
        self.temporary.cleanup()

    def test_atomic_publication_and_content_authentication(self):
        with mock.patch(
                'atomicdb.conquest_map.os.replace',
                wraps=os.replace) as replace:
            conquest_map.publish_snapshot(self.snapshot, self.path)

        replace.assert_called_once()
        self.assertEqual(
            conquest_map.read_snapshot(self.path), self.snapshot)
        self.assertFalse(any(
            path.suffix == '.tmp'
            for path in self.path.parent.iterdir()
        ))

    def test_reader_uses_bounded_chunks_and_not_the_512mb_ceiling(self):
        conquest_map.publish_snapshot(self.snapshot, self.path)
        sizes = []
        underlying = gzip.open(self.path, 'rb')

        class GuardedStream:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                underlying.close()

            def read(self, size):
                sizes.append(size)
                if size > conquest_map.SNAPSHOT_READ_CHUNK:
                    raise AssertionError('unbounded gzip read')
                return underlying.read(size)

        tracemalloc.start()
        try:
            with mock.patch(
                    'atomicdb.conquest_map.gzip.open',
                    return_value=GuardedStream()):
                loaded = conquest_map.read_snapshot(self.path)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(loaded, self.snapshot)
        self.assertTrue(sizes)
        self.assertLessEqual(
            max(sizes), conquest_map.SNAPSHOT_READ_CHUNK)
        # A small artifact must not reserve the 512 MiB safety ceiling.
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_failed_replace_preserves_previous_snapshot(self):
        conquest_map.publish_snapshot(self.snapshot, self.path)
        previous = self.path.read_bytes()
        changed = json.loads(json.dumps(self.snapshot))
        changed['snapshot']['generated_at'] = '2099-01-01T00:00:00Z'
        changed = conquest_map.seal_snapshot(
            conquest_map._unsealed_copy(changed))

        with mock.patch(
                'atomicdb.conquest_map.os.replace',
                side_effect=OSError('simulated rename failure')):
            with self.assertRaises(OSError):
                conquest_map.publish_snapshot(changed, self.path)

        self.assertEqual(self.path.read_bytes(), previous)

    def test_corrupt_or_tampered_artifact_fails_closed(self):
        conquest_map.publish_snapshot(self.snapshot, self.path)
        tampered = json.loads(
            gzip.decompress(self.path.read_bytes()).decode('utf-8'))
        root = tampered['snapshot']['root_key']
        tampered['nodes'][root]['s'] = 'WHITE_WIN'
        self.path.write_bytes(gzip.compress(
            json.dumps(tampered).encode('utf-8')))

        with self.assertRaises(conquest_map.SnapshotError):
            conquest_map.read_snapshot(self.path)

    def test_rehashed_non_additive_aggregate_still_fails_closed(self):
        tampered = json.loads(json.dumps(self.snapshot))
        root = tampered['snapshot']['root_key']
        tampered['nodes'][root]['m'][conquest_map.M_POSITIONS] += 1
        tampered = conquest_map.seal_snapshot(
            conquest_map._unsealed_copy(tampered))

        with self.assertRaises(conquest_map.SnapshotError):
            conquest_map.validate_snapshot(tampered)

    def test_rehashed_orphan_node_still_fails_closed(self):
        root, child = _key(30), _key(31)
        with mock.patch('atomicdb.conquest_map._san_for_edge',
                        return_value='a3'):
            snapshot = conquest_map.build_snapshot_data(
                [_row(root), _row(child)],
                [(root, child, 'a2a3')],
                root_key=root,
            )
        snapshot['nodes'][root]['k'] = []
        snapshot = conquest_map.seal_snapshot(
            conquest_map._unsealed_copy(snapshot))

        with self.assertRaises(conquest_map.SnapshotError):
            conquest_map.validate_snapshot(snapshot)

    def test_corrupt_failure_is_cached_until_file_signature_changes(self):
        conquest_map.publish_snapshot(self.snapshot, self.path)
        valid = self.path.read_bytes()
        corrupt = bytearray(valid)
        corrupt[len(corrupt) // 2] ^= 0xFF
        self.path.write_bytes(corrupt)
        conquest_map.reset_snapshot_cache()

        with mock.patch(
                'atomicdb.conquest_map.read_snapshot',
                wraps=conquest_map.read_snapshot) as reader:
            for _attempt in range(2):
                with self.assertRaises(conquest_map.SnapshotError):
                    conquest_map.published_snapshot(self.path)
            self.assertEqual(reader.call_count, 1)

            failed_stat = self.path.stat()
            self.path.write_bytes(valid)
            os.utime(
                self.path,
                ns=(failed_stat.st_atime_ns,
                    failed_stat.st_mtime_ns + 1_000_000),
            )
            loaded = conquest_map.published_snapshot(self.path)

        self.assertEqual(reader.call_count, 2)
        self.assertEqual(loaded, self.snapshot)


class ConquestMapCommandAndApiTests(TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'published-map.json.gz'
        self.override = override_settings(
            ATOMICDB_MAP_SNAPSHOT_PATH=str(self.path))
        self.override.enable()
        conquest_map.reset_snapshot_cache()
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.child = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'g1f3'))
        Edge.objects.create(
            parent=self.root, child=self.child, move_uci='g1f3')
        self.root.nodes_invested = 123
        self.root.save(update_fields=['nodes_invested'])
        AnalysisTask.objects.create(
            position=self.root, budget_nodes=128_000_000,
            state='LEASED', machine='worker')
        AnalysisTask.objects.create(
            position=self.child, budget_nodes=128_000_000,
            generation=1, state='PENDING')

    def tearDown(self):
        conquest_map.reset_snapshot_cache()
        self.override.disable()
        self.temporary.cleanup()

    def _build(self):
        stdout = StringIO()
        before = {
            'positions': Position.objects.count(),
            'edges': Edge.objects.count(),
            'tasks': AnalysisTask.objects.count(),
        }
        call_command(
            'build_conquest_map', output=str(self.path), stdout=stdout)
        after = {
            'positions': Position.objects.count(),
            'edges': Edge.objects.count(),
            'tasks': AnalysisTask.objects.count(),
        }
        self.assertEqual(before, after)
        return json.loads(stdout.getvalue())

    def test_command_builds_receipted_snapshot_without_model_writes(self):
        receipt = self._build()
        snapshot = conquest_map.read_snapshot(self.path)

        self.assertEqual(
            receipt['schema'], 'atomicdb.map.build-receipt.v1')
        self.assertEqual(receipt['snapshot_id'],
                         snapshot['snapshot']['id'])
        self.assertEqual(snapshot['snapshot']['positions'], 2)
        self.assertEqual(snapshot['snapshot']['edges'], 1)
        self.assertEqual(
            snapshot['nodes'][self.root.key]['m'][conquest_map.M_ACTIVE], 1)
        self.assertEqual(
            snapshot['nodes'][self.root.key]['m'][conquest_map.M_QUEUED], 1)

    def test_api_is_hierarchical_bounded_gzipped_and_database_free(self):
        self._build()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                '/atomicdb/api/map/v1',
                {'weight': 'frontier', 'limit': '600'},
                HTTP_ACCEPT_ENCODING='gzip',
            )

        self.assertEqual(len(queries), 0)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Encoding'], 'gzip')
        payload = json.loads(gzip.decompress(response.content))
        self.assertEqual(payload['schema'], 'atomicdb.map.v1')
        self.assertLessEqual(payload['marks'], 600)
        self.assertEqual(payload['root']['key'], self.root.key)
        child = payload['root']['children'][0]
        self.assertEqual(child['move'], {'uci': 'g1f3', 'san': 'Nf3'})
        self.assertEqual(child['line_san'], '1. Nf3')
        self.assertEqual(child['line_uci'], ['g1f3'])
        self.assertLessEqual(
            len(json.dumps(payload).encode('utf-8')),
            conquest_map.MAX_API_BYTES,
        )

    def test_etag_304_is_stable_across_content_codings(self):
        self._build()
        first = self.client.get('/atomicdb/api/map/v1')
        etag = first['ETag']
        second = self.client.get(
            '/atomicdb/api/map/v1',
            HTTP_ACCEPT_ENCODING='gzip',
            HTTP_IF_NONE_MATCH=etag,
        )

        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b'')
        self.assertEqual(second['ETag'], etag)
        self.assertIn('Accept-Encoding', second['Vary'])

    def test_head_matches_get_representation_headers_without_a_body(self):
        self._build()
        get_response = self.client.get(
            '/atomicdb/api/map/v1', HTTP_ACCEPT_ENCODING='gzip')
        head_response = self.client.head(
            '/atomicdb/api/map/v1', HTTP_ACCEPT_ENCODING='gzip')

        self.assertEqual(head_response.status_code, 200)
        self.assertEqual(head_response.content, b'')
        self.assertEqual(head_response['ETag'], get_response['ETag'])
        self.assertEqual(head_response['Content-Encoding'], 'gzip')
        self.assertEqual(
            int(head_response['Content-Length']), len(get_response.content))

    def test_query_limits_and_unknown_roots_fail_explicitly(self):
        self._build()
        cases = (
            ({'weight': 'regret'}, 400, 'invalid_weight'),
            ({'limit': '601'}, 400, 'invalid_query'),
            ({'depth': '0'}, 400, 'invalid_query'),
            ({'surprise': '1'}, 400, 'invalid_query'),
            ({'root': 'not-a-key'}, 400, 'invalid_root'),
            ({'root': _key(999)}, 404, 'root_not_found'),
        )
        for query, status, code in cases:
            with self.subTest(query=query):
                response = self.client.get(
                    '/atomicdb/api/map/v1', query)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()['error']['code'], code)

    def test_missing_and_corrupt_snapshot_return_503_without_db_fallback(self):
        missing = self.client.get('/atomicdb/api/map/v1')
        self.assertEqual(missing.status_code, 503)
        self.assertEqual(missing['Cache-Control'], 'no-store')

        self.path.write_bytes(b'not gzip')
        conquest_map.reset_snapshot_cache()
        with CaptureQueriesContext(connection) as queries:
            corrupt = self.client.get('/atomicdb/api/map/v1')
        self.assertEqual(len(queries), 0)
        self.assertEqual(corrupt.status_code, 503)
        self.assertEqual(
            corrupt.json()['error']['code'], 'snapshot_unavailable')

    def test_second_build_preserves_valid_display_parent_and_replaces_file(self):
        first = self._build()
        first_bytes = self.path.read_bytes()
        second = self._build()

        self.assertNotEqual(first['snapshot_id'], second['snapshot_id'])
        self.assertNotEqual(first_bytes, self.path.read_bytes())
        self.assertEqual(
            conquest_map.read_snapshot(self.path)['nodes'][self.child.key]['r'],
            self.root.key,
        )


@skipUnless(
    os.environ.get('ATOMICDB_MAP_SCALE_TESTS') in ('1', '1000000'),
    'set ATOMICDB_MAP_SCALE_TESTS=1 for the 100k projection gate',
)
class ConquestMapScaleGateTests(SimpleTestCase):

    def test_synthetic_100k_chain_build_and_bounded_render(self):
        count = 100_000
        rows = [_row(_key(index + 1)) for index in range(count)]
        edges = [
            (_key(index), _key(index + 1), 'a1a2')
            for index in range(1, count)
        ]
        with mock.patch(
                'atomicdb.conquest_map._san_for_edge',
                return_value='a2'):
            snapshot = conquest_map.build_snapshot_data(
                rows, edges, root_key=_key(1))
        document = conquest_map.render_map(
            snapshot, _key(1), limit=600,
            relative_depth=conquest_map.MAX_DEPTH)

        self.assertEqual(snapshot['snapshot']['positions'], count)
        self.assertLessEqual(document['marks'], 600)

    @skipUnless(
        os.environ.get('ATOMICDB_MAP_SCALE_TESTS') == '1000000',
        'set ATOMICDB_MAP_SCALE_TESTS=1000000 for the 1M projection gate',
    )
    def test_synthetic_1m_chain_build_and_bounded_render(self):
        count = 1_000_000
        rows = [_row(_key(index + 1)) for index in range(count)]
        edges = [
            (_key(index), _key(index + 1), 'a1a2')
            for index in range(1, count)
        ]
        with mock.patch(
                'atomicdb.conquest_map._san_for_edge',
                return_value='a2'):
            snapshot = conquest_map.build_snapshot_data(
                rows, edges, root_key=_key(1))
        document = conquest_map.render_map(
            snapshot, _key(1), limit=600,
            relative_depth=conquest_map.MAX_DEPTH)

        self.assertEqual(snapshot['snapshot']['positions'], count)
        self.assertLessEqual(document['marks'], 600)
