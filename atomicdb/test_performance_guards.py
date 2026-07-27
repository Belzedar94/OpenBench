from unittest import mock

from . import ingest, logic
from .models import DBEvent, Edge
from .testing import TestCase


class PriorityRefreshCacheTests(TestCase):

    def setUp(self):
        ingest._priority_refresh_cache.update({'at': 0.0})

    def tearDown(self):
        ingest._priority_refresh_cache.update({'at': 0.0})

    def test_two_consecutive_task_requests_refresh_once(self):
        ingest.get_or_create_position(logic.start_fen())
        original = ingest._regret_from_root
        with mock.patch('atomicdb.ingest._regret_from_root',
                        wraps=original) as refresh:
            ingest.next_tasks(1)
            ingest.next_tasks(1)
        self.assertEqual(refresh.call_count, 1)


class CascadeGuardTests(TestCase):

    def test_guard_exhaustion_emits_event(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).first().child
        with mock.patch('atomicdb.ingest.CASCADE_GUARD_LIMIT', 1):
            ingest.backup_cascade([child.key])
        event = DBEvent.objects.get(kind='CASCADE_GUARD')
        self.assertEqual(event.payload['iterations'], 1)
        self.assertGreater(event.payload['remaining'], 0)
