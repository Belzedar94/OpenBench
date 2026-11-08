"""A request's wait is measured from when it became a request.

Torom, 8-Nov-2026: the queue page's "Oldest wait" showed a day or more for
requests made minutes ago.  It was ``Min('created')``, and ``created`` is set
once, when the row is born: a withdrawn row brought back, a server row a
visitor asks for and an anonymous request a name takes are all requests made
now on an old row.  ``requested_at`` is stamped on those paths and the ages
read ``Coalesce('requested_at', 'created')``.
"""

from datetime import timedelta

from django.utils import timezone

from . import contributors, ingest, lanes, logic
from .models import AnalysisTask
from .testing import TestCase, worker_account

USER = AnalysisTask.Source.USER
AUTO = AnalysisTask.Source.AUTO
OLD = timedelta(days=2)


class RequestTimeTests(TestCase):

    def setUp(self):
        worker_account('lesha', 'p')
        worker_account('miner', 'p')
        self.pos = ingest.get_or_create_position(logic.start_fen())
        self.now = timezone.now()

    def _old_task(self, **fields):
        fields.setdefault('budget_nodes', 128_000_000)
        task = AnalysisTask.objects.create(
            position=self.pos, generation=self.pos.visits, **fields)
        AnalysisTask.objects.filter(pk=task.pk).update(
            created=self.now - OLD)
        task.refresh_from_db()
        return task

    def _oldest(self, account):
        table = lanes.measure_queue(timezone.now())
        return next(row['oldest'] for row in table['rows']
                    if row['account'] == account)

    def _when(self, account):
        rows = contributors.present_queue(account)['queue_pending']
        self.assertEqual(len(rows), 1)
        return rows[0]['when']

    def _assert_fresh(self, account):
        self.assertGreater(self._oldest(account), self.now - timedelta(minutes=1))
        self.assertEqual(self._when(account), 'just now')

    def test_a_withdrawn_request_asked_again_is_a_fresh_wait(self):
        self._old_task(source=USER, requested_by='miner',
                       state=AnalysisTask.TState.CANCELLED,
                       requested_at=self.now - OLD)

        self.assertEqual(ingest._request_rung(self.pos, 'lesha'), 'queued')

        task = AnalysisTask.objects.get(position=self.pos)
        self.assertEqual(task.state, AnalysisTask.TState.PENDING)
        self.assertEqual(task.created, self.now - OLD)
        self._assert_fresh('lesha')

    def test_server_work_promoted_to_a_request_is_a_fresh_wait(self):
        self._old_task(source=AUTO)

        self.assertEqual(ingest._request_rung(self.pos, 'lesha'), 'queued')

        task = AnalysisTask.objects.get(position=self.pos)
        self.assertEqual(task.source, USER)
        self._assert_fresh('lesha')

    def test_a_verify_walk_promoting_server_work_is_a_fresh_wait(self):
        self._old_task(source=AUTO, budget_nodes=8_000_000)

        outcome, _budget = ingest._buy_verification(self.pos, 'lesha')

        self.assertEqual(outcome, 'queued')
        self._assert_fresh('lesha')

    def test_an_adopted_anonymous_request_is_a_fresh_wait_for_its_new_owner(self):
        self._old_task(source=USER, requested_by='',
                       requested_at=self.now - OLD)

        ingest._request_rung(self.pos, 'lesha')

        task = AnalysisTask.objects.get(position=self.pos)
        self.assertEqual(task.requested_by, 'lesha')
        self._assert_fresh('lesha')

    def test_joining_a_request_keeps_its_authors_wait(self):
        asked = self.now - OLD
        self._old_task(source=USER, requested_by='lesha', requested_at=asked)

        ingest._request_rung(self.pos, 'miner')

        task = AnalysisTask.objects.get(position=self.pos)
        self.assertEqual(task.also_requested_by, ['miner'])
        self.assertEqual(task.requested_at, asked)
        self.assertEqual(self._oldest('lesha'), asked)
        self.assertEqual(self._when('lesha'), '2\xa0days ago')

    def test_moving_a_request_to_the_front_keeps_its_wait(self):
        asked = self.now - OLD
        task = self._old_task(source=USER, requested_by='lesha',
                              requested_at=asked)
        AnalysisTask.objects.filter(pk=task.pk).update(queue_seq=-5)

        self.assertEqual(self._oldest('lesha'), asked)

    def test_rows_from_before_the_column_fall_back_to_created(self):
        task = self._old_task(source=USER, requested_by='lesha')

        self.assertIsNone(task.requested_at)
        self.assertEqual(task.request_time, self.now - OLD)
        self.assertEqual(self._oldest('lesha'), self.now - OLD)
        self.assertEqual(self._when('lesha'), '2\xa0days ago')

    def test_a_new_request_is_stamped_when_it_is_made(self):
        self.assertEqual(ingest._request_rung(self.pos, 'lesha'), 'queued')

        task = AnalysisTask.objects.get(position=self.pos)
        self.assertIsNotNone(task.requested_at)
        self._assert_fresh('lesha')
