"""Bounded snapshot builds and the shared response cache of the move tree."""

import gzip
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings

from . import conquest_map, ingest, logic
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


API = '/atomicdb/api/map/v1'


class _MapFixture(TestCase):
    """root -> {d2d4, e2e3, g1f3}; e2e3 -> {d7d5, e7e5}; g1f3 -> {e7e6, g8f6}."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'published-map.json.gz'
        self.override = override_settings(
            ATOMICDB_MAP_SNAPSHOT_PATH=str(self.path))
        self.override.enable()
        conquest_map.reset_snapshot_cache()
        cache.clear()
        self.root = self._position(logic.start_fen(), 100)
        self.children = {}
        for move, nodes in (('d2d4', 0), ('e2e3', 80), ('g1f3', 50)):
            self.children[move] = self._link(self.root, move, nodes)
        self.grandchildren = {}
        for parent, moves in (('e2e3', ('d7d5', 'e7e5')),
                              ('g1f3', ('e7e6', 'g8f6'))):
            for move in moves:
                self.grandchildren[(parent, move)] = self._link(
                    self.children[parent], move, 0)

    def tearDown(self):
        conquest_map.reset_snapshot_cache()
        cache.clear()
        self.override.disable()
        self.temporary.cleanup()

    def _position(self, fen, nodes):
        position = ingest.get_or_create_position(fen)
        Position.objects.filter(key=position.key).update(
            nodes_invested=nodes)
        return position

    def _link(self, parent, move, nodes):
        child = self._position(logic.apply_move(parent.fen, move), nodes)
        Edge.objects.create(parent=parent, child=child, move_uci=move)
        return child

    def _build(self, *extra):
        stdout = StringIO()
        call_command('build_conquest_map', *extra, output=str(self.path),
                     stdout=stdout)
        return json.loads(stdout.getvalue())


class BoundedBuildTests(_MapFixture):

    def test_unbounded_budget_reads_the_whole_reachable_tree(self):
        receipt = self._build()
        snapshot = conquest_map.read_snapshot(self.path)

        self.assertEqual(receipt['positions'], 8)
        self.assertEqual(snapshot['snapshot']['positions'], 8)
        self.assertEqual(receipt['projection']['complete'], True)
        self.assertEqual(snapshot['snapshot']['projection'],
                         receipt['projection'])

    def test_budget_reads_heaviest_parent_first_and_children_all_or_none(self):
        receipt = self._build('--max-positions', '6')
        snapshot = conquest_map.read_snapshot(self.path)
        nodes = snapshot['nodes']

        self.assertEqual(receipt['positions'], 6)
        self.assertEqual(receipt['projection'], {
            'complete': False, 'max_positions': 6, 'parents_read': 2,
        })
        # e2e3 (80 nodes invested) is read before g1f3 (50); g1f3's two
        # children no longer fit, so neither of them is published.
        for move in ('d7d5', 'e7e5'):
            self.assertIn(self.grandchildren[('e2e3', move)].key, nodes)
        for move in ('e7e6', 'g8f6'):
            self.assertNotIn(self.grandchildren[('g1f3', move)].key, nodes)
        self.assertEqual(
            nodes[self.root.key]['m'][conquest_map.M_POSITIONS], 6)

    def test_bounded_build_reads_no_more_rows_than_its_budget(self):
        fetched = []
        original = Position.objects.filter

        def counting_filter(*args, **kwargs):
            keys = kwargs.get('key__in')
            if keys is not None:
                fetched.extend(keys)
            return original(*args, **kwargs)

        with mock.patch.object(Position.objects, 'filter',
                               side_effect=counting_filter):
            conquest_map.bounded_database_rows(max_positions=4)
        self.assertLessEqual(len(fetched), 4)
        self.assertEqual(len(fetched), len(set(fetched)))

    def test_open_tasks_are_counted_only_inside_the_slice(self):
        AnalysisTask.objects.create(
            position=self.children['e2e3'], budget_nodes=8_000_000,
            state='PENDING')
        AnalysisTask.objects.create(
            position=self.grandchildren[('g1f3', 'g8f6')],
            budget_nodes=8_000_000, state='LEASED')
        self._build('--max-positions', '6')
        snapshot = conquest_map.read_snapshot(self.path)
        root_metrics = snapshot['nodes'][self.root.key]['m']

        self.assertEqual(root_metrics[conquest_map.M_QUEUED], 1)
        self.assertEqual(root_metrics[conquest_map.M_ACTIVE], 0)

    def test_rebuild_reuses_the_san_of_unchanged_display_edges(self):
        self._build()
        first = conquest_map.read_snapshot(self.path)
        with mock.patch('atomicdb.conquest_map._san_for_edge',
                        side_effect=AssertionError('recomputed')):
            self._build()
        second = conquest_map.read_snapshot(self.path)
        self.assertEqual(
            {key: node['a'] for key, node in first['nodes'].items()},
            {key: node['a'] for key, node in second['nodes'].items()},
        )
        self.assertEqual(
            second['nodes'][self.children['g1f3'].key]['a'], 'Nf3')

    def test_build_never_writes_the_tree(self):
        before = (Position.objects.count(), Edge.objects.count(),
                  list(Position.objects.order_by('key').values_list(
                      'key', 'nodes_invested', 'status', 'priority')))
        self._build('--max-positions', '6')
        after = (Position.objects.count(), Edge.objects.count(),
                 list(Position.objects.order_by('key').values_list(
                     'key', 'nodes_invested', 'status', 'priority')))
        self.assertEqual(before, after)


class SharedResponseCacheTests(_MapFixture):

    page_query = {'weight': 'frontier', 'limit': '600'}

    def _page_query(self):
        return dict(self.page_query, root=self.root.key)

    def test_build_warms_the_page_view_so_a_cold_worker_never_loads(self):
        receipt = self._build()
        self.assertEqual(receipt['warmed_views'], 2)
        conquest_map.reset_snapshot_cache()     # a fresh gunicorn worker

        with mock.patch('atomicdb.conquest_map.read_snapshot',
                        side_effect=AssertionError('loaded')):
            for query in (self._page_query(), self.page_query):
                with self.subTest(query=query):
                    response = self.client.get(
                        API, query, HTTP_ACCEPT_ENCODING='gzip')
                    self.assertEqual(response.status_code, 200)
                    payload = json.loads(gzip.decompress(response.content))
                    self.assertEqual(payload['root']['key'], self.root.key)

    def test_cached_answer_is_byte_identical_to_a_direct_render(self):
        self._build('--no-warm')
        first = self.client.get(API, self._page_query())
        conquest_map.reset_snapshot_cache()
        with mock.patch('atomicdb.conquest_map.read_snapshot',
                        side_effect=AssertionError('loaded')):
            second = self.client.get(API, self._page_query())
        self.assertEqual(first.content, second.content)
        self.assertEqual(first['ETag'], second['ETag'])

        snapshot = conquest_map.read_snapshot(self.path)
        raw = conquest_map._canonical_json(conquest_map.render_map(
            snapshot, self.root.key, 'frontier', 600,
            conquest_map.DEFAULT_DEPTH))
        self.assertEqual(second.content, raw)

    def test_etag_304_still_works_from_the_shared_cache(self):
        self._build()
        conquest_map.reset_snapshot_cache()
        first = self.client.get(API, self._page_query())
        again = self.client.get(
            API, self._page_query(), HTTP_IF_NONE_MATCH=first['ETag'],
            HTTP_ACCEPT_ENCODING='gzip')
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again['ETag'], first['ETag'])
        self.assertIn('Accept-Encoding', again['Vary'])

    def test_a_new_publication_changes_the_key_without_invalidation(self):
        self._build()
        before = json.loads(self.client.get(API, self._page_query()).content)
        Position.objects.filter(key=self.children['d2d4'].key).update(
            nodes_invested=999)
        self._build()
        after = json.loads(self.client.get(API, self._page_query()).content)
        self.assertNotEqual(before['snapshot']['id'],
                            after['snapshot']['id'])
        self.assertEqual(
            after['snapshot']['id'],
            conquest_map.read_snapshot(self.path)['snapshot']['id'])

    def test_errors_are_not_cached(self):
        self._build('--no-warm')
        signature = conquest_map.snapshot_signature()
        missing_root = 'f' * 64
        response = self.client.get(API, {'root': missing_root})
        self.assertEqual(response.status_code, 404)
        key = conquest_map.map_cache_key(
            signature, missing_root, 'frontier', conquest_map.DEFAULT_MARKS,
            conquest_map.DEFAULT_DEPTH)
        self.assertIsNone(cache.get(key))
        self.assertIsNone(cache.get(key + ':lock'))

        self.path.write_bytes(b'not gzip')
        conquest_map.reset_snapshot_cache()
        corrupt = self.client.get(API, self._page_query())
        self.assertEqual(corrupt.status_code, 503)
        self.assertEqual(corrupt['Cache-Control'], 'no-store')
        key = conquest_map.map_cache_key(
            conquest_map.snapshot_signature(), self.root.key, 'frontier',
            600, conquest_map.DEFAULT_DEPTH)
        self.assertIsNone(cache.get(key))

    def test_waiter_takes_the_answer_of_the_worker_holding_the_lock(self):
        self._build('--no-warm')
        snapshot = conquest_map.read_snapshot(self.path)
        conquest_map.reset_snapshot_cache()
        signature = conquest_map.snapshot_signature()
        key = conquest_map.map_cache_key(
            signature, self.root.key, 'frontier', 600,
            conquest_map.DEFAULT_DEPTH)
        cache.add(key + ':lock', 1, timeout=60)
        raw = conquest_map._canonical_json(conquest_map.render_map(
            snapshot, self.root.key, 'frontier', 600,
            conquest_map.DEFAULT_DEPTH))
        publisher = threading.Timer(
            0.2, lambda: cache.set(key, conquest_map._entry_from_raw(raw)))
        publisher.start()
        try:
            with (
                mock.patch.object(conquest_map, 'MAP_CACHE_POLL_SECONDS',
                                  0.05),
                mock.patch('atomicdb.conquest_map.read_snapshot',
                           side_effect=AssertionError('loaded')),
            ):
                response = self.client.get(API, self._page_query())
        finally:
            publisher.cancel()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, raw)

    def test_waiter_renders_itself_when_the_lock_goes_without_an_answer(self):
        self._build('--no-warm')
        conquest_map.reset_snapshot_cache()
        key = conquest_map.map_cache_key(
            conquest_map.snapshot_signature(), self.root.key, 'frontier',
            600, conquest_map.DEFAULT_DEPTH)
        cache.add(key + ':lock', 1, timeout=60)
        release = threading.Timer(0.2, lambda: cache.delete(key + ':lock'))
        release.start()
        try:
            with mock.patch.object(conquest_map, 'MAP_CACHE_POLL_SECONDS',
                                   0.05):
                response = self.client.get(API, self._page_query())
        finally:
            release.cancel()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(cache.get(key))

    def test_a_broken_shared_cache_degrades_to_direct_rendering(self):
        self._build('--no-warm')
        broken = mock.Mock()
        for operation in ('get', 'set', 'add', 'delete', 'has_key'):
            getattr(broken, operation).side_effect = RuntimeError('down')
        with mock.patch.object(conquest_map, '_map_cache',
                               return_value=broken):
            response = self.client.get(API, self._page_query())
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload['root']['key'], self.root.key)
