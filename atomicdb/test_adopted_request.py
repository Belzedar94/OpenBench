"""A signed-in request on an anonymous one takes it, and the receipt says so
(cz_eclipsia and Opabinia, 3-Nov-2026: "In the queue" read as nothing
having happened)."""

from django.test import Client

from . import ingest, logic
from .models import AnalysisTask
from .testing import TestCase, worker_account


class AdoptedReceiptTests(TestCase):

    def setUp(self):
        self.pos = ingest.get_or_create_position(logic.start_fen())
        self.url = f'/atomicdb/request/{self.pos.key}/'
        worker_account('eclipsia')
        self.member = Client()
        self.member.login(username='eclipsia', password='pw')

    def _task(self):
        return AnalysisTask.objects.get(position=self.pos,
                                        state=AnalysisTask.TState.PENDING)

    def test_taking_an_anonymous_request_says_it_is_yours_now(self):
        Client(REMOTE_ADDR='10.0.0.1').post(self.url)
        self.assertEqual(self._task().requested_by, '')

        body = self.member.post(self.url, REMOTE_ADDR='10.0.0.2').json()

        self.assertEqual(body['status'], 'already-queued')
        self.assertIs(body.get('adopted'), True)
        self.assertEqual(self._task().requested_by, 'eclipsia')

    def test_joining_someone_elses_request_is_not_adopting_it(self):
        worker_account('opabinia')
        other = Client()
        other.login(username='opabinia', password='pw')
        other.post(self.url, REMOTE_ADDR='10.0.0.1')

        body = self.member.post(self.url, REMOTE_ADDR='10.0.0.2').json()

        self.assertEqual(body['status'], 'already-queued')
        self.assertNotIn('adopted', body)
        self.assertEqual(self._task().requested_by, 'opabinia')

    def test_the_page_words_the_adopted_receipt(self):
        page = self.member.get(f'/atomicdb/explore/{self.pos.key}/').content.decode()
        self.assertIn('Moved to your queue, waiting for a worker...', page)


class ContributorsWorkersLinkTests(TestCase):
    """cz_eclipsia, 3-Nov-2026: the workers page, one click from Contributors."""

    def test_the_contributors_heading_links_the_workers_page(self):
        body = Client().get('/atomicdb/contributors/').content.decode()
        start = body.index('id="contributors"')
        self.assertIn('/atomicdb/workers/', body[start:body.index('</h2>', start)])
