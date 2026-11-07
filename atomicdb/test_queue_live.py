"""Queue pages are live for a signed-in visitor and cached for the rest.

seaside_cliff, 6-Nov-2026: "it would be better if u didnt cache the queue
sizes when im requesting".  Somebody signed in is watching their own requests
move, so the counts they see must be the counts of now; crawler bursts and
F5 storms come without a session, and keep the short cache.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from . import logic
from .models import AnalysisTask, Position
from .testing import TestCase, worker_account


class QueueLiveForMembersTests(TestCase):

    def setUp(self):
        cache.clear()
        worker_account('miner', 'p')
        self.offset = 0

    def _queue(self, owner, count):
        rows = [Position(key=f'{index + self.offset:064d}',
                         fen=logic.start_fen(), status='UNKNOWN',
                         expanded=False)
                for index in range(count)]
        self.offset += count
        Position.objects.bulk_create(rows)
        AnalysisTask.objects.bulk_create([
            AnalysisTask(position=position, generation=0,
                         budget_nodes=128_000_000,
                         source=AnalysisTask.Source.USER, requested_by=owner,
                         state='PENDING')
            for position in rows])

    def _waiting(self, client, url):
        body = client.get(url).content.decode()
        return body

    def test_signed_in_queue_page_shows_new_requests_at_once(self):
        member = Client()
        member.force_login(User.objects.get(username='miner'))
        self._queue('miner', 2)
        first = self._waiting(member, '/atomicdb/queue/')
        self._queue('miner', 3)
        second = self._waiting(member, '/atomicdb/queue/')
        self.assertNotEqual(first, second)
        self.assertIn('>5<', second.replace(' ', ''))

    def test_anonymous_queue_page_stays_cached(self):
        visitor = Client()
        self._queue('miner', 2)
        first = self._waiting(visitor, '/atomicdb/queue/')
        self._queue('miner', 3)
        self.assertEqual(first, self._waiting(visitor, '/atomicdb/queue/'))

    def test_signed_in_account_queue_page_is_live(self):
        member = Client()
        member.force_login(User.objects.get(username='miner'))
        self._queue('miner', 1)
        first = self._waiting(member, '/atomicdb/queue/miner/')
        self._queue('miner', 2)
        self.assertNotEqual(first, self._waiting(member, '/atomicdb/queue/miner/'))


class ServedLastHourTests(QueueLiveForMembersTests):
    """seaside_cliff, 6-Nov-2026: the served count that helps is the rate."""

    def test_account_queue_shows_served_in_the_last_hour(self):
        from datetime import timedelta
        from django.utils import timezone
        self._queue('miner', 3)
        now = timezone.now()
        tasks = list(AnalysisTask.objects.filter(requested_by='miner')
                     .order_by('id'))
        AnalysisTask.objects.filter(id=tasks[0].id).update(
            state='COMPLETED', completed=now - timedelta(minutes=10))
        AnalysisTask.objects.filter(id=tasks[1].id).update(
            state='COMPLETED', completed=now - timedelta(hours=3))
        member = Client()
        member.force_login(User.objects.get(username='miner'))
        body = ' '.join(member.get('/atomicdb/queue/miner/')
                        .content.decode().split())
        self.assertIn('<strong>1</strong> served in the last hour', body)
        self.assertIn('2 served in total', body)


class WriteBacklogLineTests(QueueLiveForMembersTests):
    """BManBJ and cz_eclipsia, 7-Nov-2026: show the write delay."""

    def test_queue_page_says_when_nothing_is_waiting(self):
        body = Client().get('/atomicdb/queue/').content.decode()
        self.assertIn('Finished searches are stored as they arrive.', body)

    def test_queue_page_counts_waiting_writes(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import IngestJob
        self._queue('miner', 2)
        tasks = list(AnalysisTask.objects.order_by('id'))
        for task in tasks:
            IngestJob.objects.create(task=task, position=task.position,
                                     payload={}, state=IngestJob.JState.PENDING)
        IngestJob.objects.update(created=timezone.now() - timedelta(minutes=12))
        body = ' '.join(Client().get('/atomicdb/queue/').content.decode().split())
        self.assertIn('<strong>2</strong> finished searches waiting to be stored', body)
        self.assertIn('the oldest for 12', body)
