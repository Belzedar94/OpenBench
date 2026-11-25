"""The lease reads the head of each account's queue, and hands out the task
the whole queue would (25-Nov-2026).

``views._lease_head_pages`` replaces, for the first ids of the lease order,
the fair-share window over every live row with the window over each
account's leased rows and first ``LEASE_HEAD_ROWS`` waiting rows.  Held here
to the whole list of ``ordered`` in two ways:

- every id list it yields is a prefix of the whole ordered list (with the
  worker's ``max_budget`` applied to both), and when it says it covered the
  whole list, it is the whole list;
- lease after lease, with heads of one, two and three rows, the task handed
  out is the one the whole list gives (the oracle of ``test_lease_paging``).
"""

import random
from datetime import timedelta
from unittest import mock

from django.db import connections, transaction
from django.test.utils import CaptureQueriesContext

from . import live_request, preferences, views
from .models import AnalysisTask, ContributorPref
from .test_lease_paging import AUTO, M, TB_FEN, USER, _LeaseCompare, no_heads


class _HeadCompare(_LeaseCompare):

    def setUp(self):
        super().setUp()
        self.records = []
        self.heads_used = 0
        real = self.real_heads = views._lease_head_pages
        test = self

        def spy(ordered, max_budget=None, **kwargs):
            # The whole list, read in the same transaction and before any
            # row of this lease changes: the oracle of every page.
            whole = views._lease_order(ordered, max_budget)
            test.heads_used += 1
            pages = real(ordered, max_budget, **kwargs)
            while True:
                try:
                    page = next(pages)
                except StopIteration as stop:
                    if stop.value:
                        test.records.append(('complete', page_seen, whole))
                    return stop.value
                page_seen = page
                test.records.append(('page', page, whole))
                yield page

        patcher = mock.patch.object(views, '_lease_head_pages', spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check_records(self):
        for kind, page, whole in self.records:
            if kind == 'page':
                self.assertEqual(page, whole[:len(page)])
                # How far the heads reached, and whether they stopped short
                # of the whole list (the pages then took over).
                self.head_rows_checked += len(page)
                self.head_cut_short += len(page) < len(whole)
            else:
                self.assertEqual(page, whole)
                self.head_complete += 1
        self.records.clear()

    head_rows_checked = head_cut_short = head_complete = 0

    def chosen_heads(self, head_rows, **post):
        with transaction.atomic(using=self.alias):
            with mock.patch.object(views, 'LEASE_HEAD_ROWS', head_rows), \
                    mock.patch.object(views, 'LEASE_PAGE_ROWS', 2):
                task_id = self._post(**post)
            transaction.set_rollback(True, using=self.alias)
        self.check_records()
        return task_id

    def assert_same_choice(self, head_rows=(1, 2, 3), **post):
        expected = self.chosen(False, **post)
        for rows in head_rows:
            with self.subTest(head_rows=rows, **post):
                self.assertEqual(self.chosen_heads(rows, **post), expected)
        return expected

    def close(self, task):
        task.position.status = 'WIN'
        task.position.save(update_fields=['status'])

    def age(self, task, hours):
        AnalysisTask.objects.filter(id=task.id).update(
            created=self.now - timedelta(hours=hours))


class HeadLeaseEquivalenceTests(_HeadCompare):

    def test_tablebase_rows_ahead_of_a_worker_without_tables(self):
        for _ in range(12):
            self.task(fen=TB_FEN, requested_by='bob')
        wanted = self.task(requested_by='bob')
        self.task(requested_by='carol', fen=TB_FEN)

        self.assertEqual(self.assert_same_choice(tb='0'), wanted.id)
        self.assertNotEqual(self.assert_same_choice(tb='1'), wanted.id)

    def test_only_tablebase_rows_falls_back_to_the_first(self):
        rows = [self.task(fen=TB_FEN, requested_by='bob') for _ in range(12)]
        self.task(fen=TB_FEN, requested_by='carol')

        self.assertEqual(self.assert_same_choice(tb='0'), rows[0].id)

    def test_an_account_whose_whole_head_is_over_max_budget(self):
        """More than a head of rows the worker cannot take, then one it can:
        the head is cut, and the pages after it find the row."""
        for _ in range(12):
            self.task(requested_by='bob', budget_nodes=2_000 * M)
        fits = self.task(requested_by='bob', budget_nodes=32 * M)
        for _ in range(4):
            self.task(requested_by='carol', budget_nodes=2_000 * M)

        self.assertEqual(self.assert_same_choice(max_budget=128 * M), fits.id)
        self.assertGreater(self.head_cut_short, 0)

    def test_an_account_whose_whole_head_is_on_closed_positions(self):
        for _ in range(6):
            self.close(self.task(requested_by='bob'))
        later = self.task(requested_by='bob', budget_nodes=8 * M)
        self.task(requested_by='carol', budget_nodes=2_000 * M)
        self.task(requested_by='carol', budget_nodes=2_000 * M)

        self.assert_same_choice()
        self.assert_same_choice(max_budget=128 * M)
        self.assertEqual(self.assert_same_choice(max_budget=16 * M), later.id)

    def test_deep_capped_requester_ahead_of_the_rest(self):
        self.task(state='LEASED', requested_by='bob',
                  budget_nodes=views.DEEP_TASK_NODES, machine='other#1',
                  leased_at=self.now, lease_heartbeat_at=self.now,
                  lease_token='t')
        for _ in range(6):
            self.task(requested_by='bob', budget_nodes=views.DEEP_TASK_NODES,
                      queue_seq=-5)
        wanted = self.task(requested_by='carol', budget_nodes=128 * M)
        self.task(requested_by='carol', budget_nodes=128 * M)

        self.assertEqual(self.assert_same_choice(), wanted.id)

    def test_background_rows_and_the_leased_ones_count_first(self):
        for index in range(6):
            self.task(requested_by='bob', background=index < 4)
            self.task(state='LEASED', requested_by='carol',
                      leased_at=self.now - timedelta(minutes=index),
                      machine=f'c#{index}', budget_nodes=512 * M)
            self.task(requested_by='carol', budget_nodes=8 * M,
                      queue_seq=-index)

        self.assert_same_choice()
        self.assert_same_choice(max_budget=64 * M)

    def test_lanes_and_rows_the_worker_backed(self):
        """Rows that do not share their account's keys: a lane lent by
        somebody else, and a deep row the worker's own account backed."""
        for _ in range(8):
            self.task(requested_by='bob')
            self.task(requested_by='carol')
        self.task(requested_by='alice', lane_account='bob')
        deep = self.task(requested_by='carol', also_requested_by=['w'],
                         backers=1)
        for order in ('own', 'queue', 'others'):
            ContributorPref.objects.update_or_create(
                account='w', defaults={'serve_order': order,
                                       'serve_others_first': order == 'others'})
            with self.subTest(order=order):
                chosen = self.assert_same_choice()
                if order == 'own':
                    # Mine first: the backed row is the worker's own.
                    self.assertEqual(chosen, deep.id)
                self.assert_same_choice(username='alice')

    def test_listed_accounts_with_others_first(self):
        for _ in range(8):
            self.task(requested_by='bob')
            self.task(requested_by='Carol')
            self.task(requested_by='dave')
        backed = self.task(requested_by='bob', also_requested_by=['dave'],
                           backers=1)
        ContributorPref.objects.update_or_create(
            account='w', defaults={'serve_order': 'others',
                                   'serve_others_first': True,
                                   'serve_for': 'carol dave'})
        self.assert_same_choice()
        ContributorPref.objects.update_or_create(
            account='w', defaults={'serve_order': 'others',
                                   'serve_others_first': True,
                                   'serve_for': 'anonymous'})
        self.task(requested_by='')
        self.assert_same_choice()
        self.assertTrue(backed.id)

    def test_starved_anonymous_rows(self):
        """An anonymous request past a day joins the named stratum: the
        anonymous queue is then read whole."""
        fresh = [self.task(requested_by='') for _ in range(6)]
        old = self.task(requested_by='', queue_seq=0)
        self.age(old, 30)
        # One row: bob's head is whole, so only the anonymous one is cut.
        self.task(requested_by='bob', budget_nodes=2_000 * M)
        self.assertEqual(self.assert_same_choice(max_budget=128 * M), old.id)
        self.assertTrue(fresh)

    def test_house_account(self):
        for _ in range(6):
            self.task(requested_by='bob')
            self.task(requested_by='w', budget_nodes=2_000 * M)
        self.task(source=AUTO)
        with mock.patch.object(views, 'HOUSE_ACCOUNTS', frozenset({'w'})):
            self.assert_same_choice()
            self.assert_same_choice(max_budget=128 * M)

    def test_personal_worker(self):
        for _ in range(6):
            self.task(requested_by='bob')
            self.task(requested_by='w', budget_nodes=2_000 * M)
            self.task(requested_by='carol', budget_nodes=8 * M)
        self.task(requested_by='dave', also_requested_by=['w'], backers=1)
        ContributorPref.objects.update_or_create(
            account='w', defaults={'serve_for': 'carol'})
        with mock.patch.object(preferences, 'worker_serving',
                               return_value=('personal', None)):
            self.assert_same_choice()
            self.assert_same_choice(max_budget=128 * M)
            self.assert_same_choice(max_budget=4 * M)

    def test_random_queues_drained_lease_by_lease(self):
        configs = [dict(), dict(tb='1'), dict(max_budget=128 * M),
                   dict(username='alice'), dict(username='alice', tb='1'),
                   dict(max_budget=16 * M)]
        for seed in range(8):
            with self.subTest(seed=seed):
                AnalysisTask.objects.all().delete()
                rng = random.Random(2000 + seed)
                self.random_queue(rng, rows=60, tb_share=0.3, deep_share=0.1)
                for task in AnalysisTask.objects.filter(source=USER):
                    roll = rng.random()
                    if roll < 0.08:
                        self.close(task)
                    elif roll < 0.14:
                        task.also_requested_by = [rng.choice(['w', 'alice',
                                                              'bob'])]
                        task.backers = 1
                        task.save(update_fields=['also_requested_by',
                                                 'backers'])
                    elif roll < 0.18 and not task.requested_by:
                        self.age(task, 30)
                order = rng.choice(['own', 'queue', 'others'])
                ContributorPref.objects.update_or_create(
                    account='w', defaults={
                        'serve_order': order,
                        'serve_others_first': order == 'others',
                        'serve_for': rng.choice(['', 'carol', 'dave bob'])})
                for step in range(14):
                    post = configs[step % len(configs)]
                    expected = self.assert_same_choice(head_rows=(1, 2),
                                                       **post)
                    if expected is None:
                        break
                    with mock.patch.object(views, 'LEASE_HEAD_ROWS', 1):
                        self.assertEqual(self._post(**post), expected)
                    self.check_records()
        # Not a vacuous comparison: the heads handed out real prefixes, and
        # were cut short often enough for the pages after them to matter.
        self.assertGreater(self.head_rows_checked, 200)
        self.assertGreater(self.head_cut_short, 50)


class HeadCostTests(_HeadCompare):

    def test_the_head_decides_without_reading_the_whole_queue(self):
        for _ in range(views.LEASE_HEAD_ROWS + 40):
            self.task(requested_by='bob')
            self.task(requested_by='carol', background=True)
        calls = []
        original = views._lease_order_pages

        def spy(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        with mock.patch.object(views, '_lease_order_pages', spy):
            self.assertIsNotNone(self._post())

        self.assertEqual(calls, [])
        self.assertEqual(self.heads_used, 1)
        self.check_records()

    def test_a_personal_worker_with_nothing_to_take_reads_no_page(self):
        for _ in range(views.LEASE_HEAD_ROWS + 10):
            self.task(requested_by='bob')
        self.task(requested_by='w', budget_nodes=2_000 * M)
        calls = []
        original = views._lease_order_pages

        def spy(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        with mock.patch.object(views, '_lease_order_pages', spy), \
                mock.patch.object(preferences, 'worker_serving',
                                  return_value=('personal', None)):
            self.assertIsNone(self._post(max_budget=128 * M))

        self.assertEqual(calls, [])
        self.check_records()

    def test_the_window_reads_only_the_heads(self):
        """The ordered statement the head runs names its ids: the window
        never sees the tail of an account."""
        for _ in range(views.LEASE_HEAD_ROWS * 3):
            self.task(requested_by='bob')
        self.task(requested_by='carol')

        # The real head, without the spy's whole-list oracle.
        with mock.patch.object(views, '_lease_head_pages', self.real_heads), \
                CaptureQueriesContext(connections[self.alias]) as queries:
            self.assertIsNotNone(self._post())
        windows = [q['sql'] for q in queries.captured_queries
                   if 'OVER' in q['sql']]
        self.assertEqual(len(windows), 1)
        self.assertIn(' IN (', windows[0])
        self.assertNotIn('LIMIT', windows[0])


class QueuedAccountsTests(_HeadCompare):

    def test_the_loose_scan_finds_the_accounts_distinct_finds(self):
        for seed in range(10):
            with self.subTest(seed=seed):
                AnalysisTask.objects.all().delete()
                self.random_queue(random.Random(3000 + seed), rows=50)
                distinct = set(
                    AnalysisTask.objects.filter(source=USER)
                    .filter(live_request.CHARGED)
                    .annotate(account=live_request._effective_account())
                    .values_list('account', flat=True))
                self.assertEqual(live_request.queued_accounts(), distinct)

    def test_heads_are_the_first_rows_in_window_order(self):
        rng = random.Random(7)
        for _ in range(40):
            self.task(requested_by=rng.choice(['bob', 'carol', '']),
                      lane_account=rng.choice(['', '', 'bob', 'dave']),
                      background=rng.random() < 0.3,
                      queue_seq=rng.choice([0, 0, -1, -2]),
                      state=rng.choice(['PENDING'] * 3 + ['LEASED']))
        accounts = live_request.queued_accounts()
        heads = live_request.queue_heads(accounts, 3)
        for account in accounts:
            rows = sorted(
                (task.background, task.queue_seq, task.id)
                for task in AnalysisTask.objects.filter(
                    live_request._account_q(account), source=USER,
                    state='PENDING'))
            self.assertEqual(heads[account], rows[:4], account)


class HeadsOffTests(_HeadCompare):
    """The switch the paging tests use turns the head off."""

    def test_no_heads(self):
        self.task(requested_by='bob')
        with no_heads():
            self.assertIsNotNone(self._post())
        self.assertEqual(self.heads_used, 0)
