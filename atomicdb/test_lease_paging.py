"""The lease got cheaper without choosing a different task (25-Nov-2026).

Two changes on the lease path, both held here to the code they replace:

- ``live_request.served_rank`` reads one last lease per account that has
  USER rows in the queue, instead of grouping every lease of the last hour.
  The old implementation lives on below as the oracle, and random queues must
  order the same under both.
- ``views._lease_order_pages`` reads the ordered ids in pages instead of the
  whole list.  The task a lease hands out must be the one the whole list gives,
  also when the first pages hold nothing this worker can take.
"""

import random
from datetime import timedelta
from unittest import mock

from django.db import connections, transaction
from django.db.models import (CharField, Case, F, IntegerField, Max, Q, Value,
                              When)
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import ingest, live_request, logic, sweep, views
from .models import AnalysisTask, ContributorPref, Position
from .testing import TestCase, worker_account

USER = AnalysisTask.Source.USER
AUTO = AnalysisTask.Source.AUTO
FILL = AnalysisTask.Source.FILL
M = 1_000_000
TB_FEN = '4k3/8/8/8/8/8/PPP5/KQ6 w - - 0 1'


def old_served_rank(now=None):
    """``live_request.served_rank`` as it was before 25-Nov-2026: the oracle."""
    cutoff = (now or timezone.now()) - live_request.SERVED_WINDOW
    effective = Case(When(lane_account='', then=F('requested_by')),
                     default=F('lane_account'), output_field=CharField())
    last = list(AnalysisTask.objects
                .filter(source=USER, leased_at__gte=cutoff)
                .filter(Q(state=AnalysisTask.TState.LEASED)
                        | Q(state=AnalysisTask.TState.COMPLETED,
                            completed__gte=cutoff))
                .annotate(_served_account=effective)
                .order_by().values('_served_account')
                .annotate(last=Max('leased_at'))
                .values_list('_served_account', 'last'))
    stamps = sorted({stamp for _account, stamp in last})
    rank = {stamp: index + 1 for index, stamp in enumerate(stamps)}
    whens = [When(Q(source=USER) & live_request._account_q(account),
                  then=Value(rank[stamp]))
             for account, stamp in last]
    if not whens:
        return Value(0, output_field=IntegerField())
    return Case(*whens, default=Value(0), output_field=IntegerField())


class _Queue(TestCase):

    ACCOUNTS = ('', 'alice', 'bob', 'carol', 'dave', 'eve', 'w')

    def setUp(self):
        self.now = timezone.now()
        self.serial = 0

    def position(self, fen=None):
        self.serial += 1
        return Position.objects.create(
            key=f'{70_000 + self.serial:064d}', fen=fen or logic.start_fen(),
            status='UNKNOWN', expanded=False)

    def task(self, fen=None, **fields):
        values = dict(generation=0, budget_nodes=128 * M, source=USER,
                      state='PENDING')
        values.update(fields)
        return AnalysisTask.objects.create(position=self.position(fen),
                                           **values)

    def random_queue(self, rng, rows=60, tb_share=0.0, deep_share=0.0):
        """A queue with every shape the order knows: accounts with only
        finished work (dropped from the new lookup), lanes, background rows,
        ties on ``leased_at``, selector rows, cancelled and old leases."""
        minutes = [-130, -61, -59, -40, -40, -20, -5, -5, -1]
        for _ in range(rows):
            source = rng.choice([USER] * 6 + [AUTO, FILL])
            state = rng.choice(['PENDING'] * 4 + ['LEASED', 'COMPLETED',
                                                   'COMPLETED', 'CANCELLED'])
            author = rng.choice(self.ACCOUNTS) if source == USER else ''
            lane = ''
            if source == USER and rng.random() < 0.2:
                lane = rng.choice(self.ACCOUNTS[1:])
            leased_at = completed = None
            if state != 'PENDING' or rng.random() < 0.2:
                leased_at = self.now + timedelta(minutes=rng.choice(minutes))
            if state == 'COMPLETED':
                completed = leased_at + timedelta(
                    minutes=rng.choice([0, 1, 30, 70]))
            budget = rng.choice([8 * M, 32 * M, 128 * M, 512 * M, 2_000 * M])
            if rng.random() < deep_share:
                budget = views.DEEP_TASK_NODES
            self.task(fen=TB_FEN if rng.random() < tb_share else None,
                      source=source, state=state, requested_by=author,
                      lane_account=lane, leased_at=leased_at,
                      completed=completed, budget_nodes=budget,
                      background=rng.random() < 0.15,
                      backers=rng.choice([0, 0, 0, 1]),
                      queue_seq=rng.choice([0, 0, 0, -1]))


class ServedRankEquivalenceTests(_Queue):
    """Only the accounts in the queue are looked up, and the order is the
    same: the rank is a stamp's place among the distinct stamps, so dropping
    the accounts with no row to order renumbers the rest without reordering
    them, ties and zeros included."""

    def charged(self):
        return (AnalysisTask.objects.filter(live_request.CHARGED)
                .order_by('id'))

    def assert_same_ranking(self):
        old = list(self.charged()
                   .annotate(rank=old_served_rank(self.now))
                   .values_list('source', 'rank'))
        new = list(self.charged()
                   .annotate(rank=live_request.served_rank(self.now))
                   .values_list('source', 'rank'))
        self.assertEqual(len(old), len(new))
        for (source, a), (_source, b) in zip(old, new):
            self.assertEqual(a == 0, b == 0)
            if source != USER:
                self.assertEqual((a, b), (0, 0))
        for i in range(len(old)):
            for j in range(len(old)):
                a = (old[i][1] > old[j][1]) - (old[i][1] < old[j][1])
                b = (new[i][1] > new[j][1]) - (new[i][1] < new[j][1])
                self.assertEqual(a, b, (i, j, old, new))

    def service_order(self, rank):
        with mock.patch.object(live_request, 'served_rank', rank):
            return list(live_request.service_order(
                AnalysisTask.objects.filter(source=USER)
                .filter(live_request.CHARGED), self.now)
                .values_list('id', flat=True))

    def test_random_queues_rank_and_order_the_same(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                AnalysisTask.objects.all().delete()
                self.random_queue(random.Random(seed))
                self.assert_same_ranking()
                self.assertEqual(self.service_order(old_served_rank),
                                 self.service_order(live_request.served_rank))

    def test_an_account_with_only_finished_work_is_left_out(self):
        self.task(state='COMPLETED', requested_by='gone',
                  leased_at=self.now - timedelta(minutes=3),
                  completed=self.now - timedelta(minutes=1))
        self.task(state='LEASED', requested_by='bob',
                  leased_at=self.now - timedelta(minutes=10))
        self.task(requested_by='bob')
        self.task(requested_by='alice')

        last = live_request._served_stamps(
            self.now - live_request.SERVED_WINDOW)

        self.assertEqual(set(last), {'bob'})
        self.assert_same_ranking()

    def test_lanes_and_the_anonymous_tide_are_their_own_accounts(self):
        # Served through bob's lane: charged to bob, not to alice.
        self.task(state='LEASED', requested_by='alice', lane_account='bob',
                  leased_at=self.now - timedelta(minutes=2))
        self.task(state='COMPLETED', requested_by='',
                  leased_at=self.now - timedelta(minutes=30),
                  completed=self.now - timedelta(minutes=20))
        self.task(state='COMPLETED', requested_by='alice',
                  leased_at=self.now - timedelta(minutes=50),
                  completed=self.now - timedelta(minutes=45))
        for author, lane in (('alice', ''), ('carol', 'bob'), ('', '')):
            self.task(requested_by=author, lane_account=lane)

        last = live_request._served_stamps(
            self.now - live_request.SERVED_WINDOW)

        self.assertEqual(set(last), {'', 'alice', 'bob'})
        self.assertLess(last['alice'], last[''])
        self.assertLess(last[''], last['bob'])
        self.assert_same_ranking()

    def test_a_lease_older_than_the_window_does_not_count(self):
        self.task(state='COMPLETED', requested_by='alice',
                  leased_at=self.now - timedelta(minutes=65),
                  completed=self.now - timedelta(minutes=1))
        self.task(state='LEASED', requested_by='bob',
                  leased_at=self.now - timedelta(minutes=61))
        self.task(requested_by='alice')
        self.task(requested_by='bob')

        self.assertEqual(live_request._served_stamps(
            self.now - live_request.SERVED_WINDOW), {})
        self.assert_same_ranking()

    def test_it_is_one_statement_whatever_the_number_of_accounts(self):
        for index in range(30):
            account = f'acct{index}'
            self.task(requested_by=account)
            self.task(state='COMPLETED', requested_by=account,
                      leased_at=self.now - timedelta(minutes=index + 1),
                      completed=self.now - timedelta(minutes=index))
        alias = AnalysisTask.objects.db

        with CaptureQueriesContext(connections[alias]) as queries:
            last = live_request._served_stamps(
                self.now - live_request.SERVED_WINDOW)

        self.assertEqual(len(last), 30)
        self.assertEqual(len(queries), 1)

    def test_each_account_is_one_probe_of_its_index(self):
        """No row of the window is read: per account, the planner walks the
        (account, leased_at) index and nothing else."""
        connection = connections[AnalysisTask.objects.db]
        if connection.vendor != 'sqlite':
            self.skipTest('the plan text checked here is SQLite\'s')
        self.task(requested_by='alice')
        with CaptureQueriesContext(connection) as queries:
            live_request._served_stamps(self.now - live_request.SERVED_WINDOW)
        captured = queries.captured_queries[0]['sql']
        with connection.cursor() as cursor:
            cursor.execute('EXPLAIN QUERY PLAN ' + captured)
            plan = ' | '.join(str(row[-1]) for row in cursor.fetchall())

        self.assertIn('atomic_task_user_served', plan)
        self.assertIn('atomic_task_lane_served', plan)


class _LeaseCompare(_Queue):
    """The same lease, read once in pages and once as the whole list."""

    def setUp(self):
        super().setUp()
        worker_account('w', 'p')
        worker_account('alice', 'p')
        self.client = Client()
        self.alias = AnalysisTask.objects.db
        self.machines = 0
        # The sweep's share counts leases in the cache, and the tables would
        # probe the disk: neither belongs to the comparison.
        for patcher in (
                mock.patch.object(sweep, 'take_share', return_value=False),
                mock.patch.object(ingest, 'server_tables_wdl',
                                  return_value=None),
                mock.patch.object(views.logic, 'tb_applicable',
                                  side_effect=lambda fen: fen == TB_FEN)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, username='w', tb='0', max_budget=None):
        self.machines += 1
        machine = f'box{self.machines}-atomicdb'
        data = {'username': username, 'password': 'p', 'machine': machine,
                'worker_build': str(views.LEASE_TOKEN_BUILD),
                'lease_session': machine, 'tb': tb}
        if max_budget is not None:
            data['max_budget'] = str(max_budget)
        response = self.client.post('/atomicdb/api/lease', data)
        self.assertEqual(response.status_code, 200, response.content)
        tasks = response.json()['tasks']
        return tasks[0]['id'] if tasks else None

    def chosen(self, paged, page_rows=2, **post):
        """The task this lease would hand out, with nothing kept."""
        whole = (lambda ordered, max_budget=None:
                 iter([views._lease_order(ordered, max_budget)]))
        patches = ([mock.patch.object(views, 'LEASE_PAGE_ROWS', page_rows)]
                   if paged else
                   [mock.patch.object(views, '_lease_order_pages', whole)])
        with transaction.atomic(using=self.alias):
            for patch in patches:
                patch.start()
            try:
                task_id = self._post(**post)
            finally:
                for patch in patches:
                    patch.stop()
            transaction.set_rollback(True, using=self.alias)
        return task_id

    def assert_same_choice(self, page_rows=(1, 2, 3), **post):
        expected = self.chosen(False, **post)
        for rows in page_rows:
            with self.subTest(page_rows=rows, **post):
                self.assertEqual(self.chosen(True, page_rows=rows, **post),
                                 expected)
        return expected


class PagedLeaseEquivalenceTests(_LeaseCompare):

    def test_tablebase_rows_ahead_of_a_worker_without_tables(self):
        # Twelve: with one-row pages the target is only in the full list.
        for _ in range(12):
            self.task(fen=TB_FEN, requested_by='bob')
        # Same account: the fair share would otherwise put it second.
        wanted = self.task(requested_by='bob')

        self.assertEqual(self.assert_same_choice(tb='0'), wanted.id)
        self.assertNotEqual(self.assert_same_choice(tb='1'), wanted.id)

    def test_only_tablebase_rows_falls_back_to_the_first(self):
        rows = [self.task(fen=TB_FEN, requested_by='bob') for _ in range(12)]

        self.assertEqual(self.assert_same_choice(tb='0'), rows[0].id)

    def test_rows_over_max_budget_ahead_of_one_that_fits(self):
        for _ in range(12):
            self.task(requested_by='bob', budget_nodes=2_000 * M)
        fits = self.task(requested_by='bob', budget_nodes=32 * M)

        self.assertEqual(self.assert_same_choice(max_budget=128 * M), fits.id)

    def test_deep_capped_requester_ahead_of_the_rest(self):
        self.task(state='LEASED', requested_by='bob',
                  budget_nodes=views.DEEP_TASK_NODES, machine='other#1',
                  leased_at=self.now, lease_heartbeat_at=self.now,
                  lease_token='t')
        for _ in range(6):
            self.task(requested_by='bob', budget_nodes=views.DEEP_TASK_NODES,
                      queue_seq=-5)
        wanted = self.task(requested_by='carol', budget_nodes=128 * M)

        self.assertEqual(self.assert_same_choice(), wanted.id)

    def test_lanes_background_rows_and_serve_orders(self):
        for index in range(8):
            self.task(requested_by='w', background=index % 2 == 0)
            self.task(requested_by='alice', lane_account='bob')
            self.task(requested_by='carol', background=True)
        for order, serve_for in (('own', ''), ('queue', ''),
                                 ('others', ''), ('others', 'carol')):
            ContributorPref.objects.update_or_create(
                account='w', defaults={'serve_order': order,
                                       'serve_others_first': order == 'others',
                                       'serve_for': serve_for})
            with self.subTest(order=order, serve_for=serve_for):
                self.assert_same_choice()
                self.assert_same_choice(max_budget=64 * M)

    def test_random_queues_drained_lease_by_lease(self):
        """Lease after lease, each kept, on queues with every shape: the
        paged scan hands out the task the whole list does every time."""
        configs = [dict(), dict(tb='1'), dict(max_budget=128 * M),
                   dict(username='alice'), dict(username='alice', tb='1')]
        for seed in range(6):
            with self.subTest(seed=seed):
                AnalysisTask.objects.all().delete()
                rng = random.Random(1000 + seed)
                self.random_queue(rng, rows=50, tb_share=0.3, deep_share=0.1)
                order = rng.choice(['own', 'queue', 'others'])
                ContributorPref.objects.update_or_create(
                    account='w', defaults={
                        'serve_order': order,
                        'serve_others_first': order == 'others'})
                for step in range(12):
                    post = configs[step % len(configs)]
                    expected = self.assert_same_choice(page_rows=(2,), **post)
                    if expected is None:
                        break
                    # Keep this lease and go on with the queue it leaves.
                    with mock.patch.object(views, 'LEASE_PAGE_ROWS', 2):
                        self.assertEqual(self._post(**post), expected)

    def test_the_first_page_alone_is_read_when_it_decides(self):
        for _ in range(views.LEASE_PAGE_ROWS + 50):
            self.task(requested_by='bob')
        calls = []
        original = views._lease_order

        def spy(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        with mock.patch.object(views, '_lease_order', spy), \
                CaptureQueriesContext(connections[self.alias]) as queries:
            self.assertIsNotNone(self._post())

        self.assertEqual(calls, [])
        self.assertTrue(any('LIMIT %d' % views.LEASE_PAGE_ROWS in query['sql']
                            for query in queries.captured_queries))
