"""The delivery window, the backing weight, and the queue page.

Contributor lanes existed from 10 to 16 August and are gone: the binary
window was gameable by a one minute worker run, and the community chose the
pre-lane arrangement instead.  What this file asserts now:

- the delivery-window predicate that still feeds the rung selector,
- that running a worker buys NO queue position (the regression the removal
  must never undo silently),
- that backing a request still lends it the lightest requester's queue,
- and the per-account queue page.

Who gets served first, and in what proportion, is asserted in
``test_queue_fairness``: that file owns the order.
"""

import json
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from . import ingest, lanes, logic, views
from .models import AnalysisTask, Edge, Position, WorkerPing
from .testing import TestCase, worker_account


class ContributorPredicateTests(TestCase):
    """Contributing is evidenced by DELIVERED work, and by nothing else.

    The predicate no longer buys queue position.  The rung selector
    (``depth.may_choose``) still reads it, so what earns it stays load
    bearing.
    """

    def setUp(self):
        worker_account('miner', 'p')
        worker_account('lurker', 'p')

    def _ping(self, user, machine='m1', age=None, delivered=True):
        """A worker that has DELIVERED ``age`` ago, or one that never has."""
        ping = WorkerPing.objects.create(machine=machine, user=user)
        if delivered:
            WorkerPing.objects.filter(pk=ping.pk).update(
                last_result_at=timezone.now() - (age or timedelta()))
        return ping

    def test_a_recent_delivery_counts(self):
        self._ping('miner')

        self.assertEqual(lanes.contributor_accounts(), frozenset({'miner'}))

    def test_a_delivery_older_than_the_window_does_not(self):
        self._ping('miner', age=timedelta(days=lanes.CONTRIBUTOR_WINDOW_DAYS,
                                          hours=1))

        self.assertEqual(lanes.contributor_accounts(), frozenset())

    def test_saying_hello_forever_earns_nothing(self):
        """The reason the predicate moved off ``last_seen``.

        ``last_seen`` is auto_now, so a process that polls for work and never
        returns an analysis kept it as fresh as a machine that was actually
        searching.  Contributing is evidenced by delivered work.
        """
        self._ping('miner', delivered=False)

        self.assertEqual(lanes.contributor_accounts(), frozenset())
        self.assertFalse(lanes.ran_a_worker('miner'))

    def test_a_delivery_inside_the_window_counts_when_switched_off(self):
        # Somebody who turns the machine off for the weekend stays trusted:
        # the row stays, the window is what forgets.
        self._ping('miner', age=timedelta(days=3))

        self.assertEqual(lanes.contributor_accounts(), frozenset({'miner'}))

    def test_a_revoked_account_loses_the_privilege(self):
        from OpenBench.models import Profile
        self._ping('miner')
        Profile.objects.filter(user__username='miner').update(enabled=False)

        self.assertEqual(lanes.contributor_accounts(), frozenset())

    def test_an_account_that_never_delivered_is_not_contributing(self):
        self._ping('miner')

        self.assertFalse(lanes.ran_a_worker('lurker'))
        self.assertTrue(lanes.ran_a_worker('miner'))

    def test_staff_alone_earns_nothing(self):
        # The owner opens the rung selector with is_staff, and that shortcut
        # deliberately does NOT count as contributing: CPU is what counts.
        from django.contrib.auth.models import User
        User.objects.filter(username='lurker').update(is_staff=True)

        self.assertEqual(lanes.contributor_accounts(), frozenset())

    def test_the_rung_selector_reads_the_same_predicate(self):
        from django.contrib.auth.models import User
        from . import depth
        self._ping('miner')

        self.assertTrue(depth.may_choose(User.objects.get(username='miner')))
        self.assertFalse(depth.may_choose(User.objects.get(username='lurker')))


class DeliveryEvidenceTests(TestCase):
    """``last_result_at`` and ``delivered_by`` record a DELIVERY, not a hello."""

    def setUp(self):
        worker_account('u', 'p')
        self.client = Client()
        ingest.get_or_create_position(logic.start_fen())
        self.payload = {'username': 'u', 'password': 'p', 'machine': 'u-box',
                        'threads': 8, 'hash': 1024, 'os': 'TestOS'}

    def test_polling_for_work_never_stamps_a_delivery(self):
        self.client.post('/atomicdb/api/lease', self.payload)

        ping = WorkerPing.objects.get(machine='u-box')
        self.assertIsNotNone(ping.last_seen)
        self.assertIsNone(ping.last_result_at)

    def test_submitting_a_result_stamps_one(self):
        lease = self.client.post('/atomicdb/api/lease', self.payload)
        task_id = json.loads(lease.content)['tasks'][0]['id']

        self.client.post('/atomicdb/api/submit',
                         dict(self.payload, task_id=task_id, lines='[]',
                              elapsed='2.5', nodes='1000'))

        ping = WorkerPing.objects.get(machine='u-box')
        self.assertIsNotNone(ping.last_result_at)
        # And the TASK remembers the authenticated account that delivered it.
        # The machine name is worker-chosen and can be claimed by two
        # accounts, so this stamp is what the front page attributes by.
        task = AnalysisTask.objects.get(id=task_id)
        self.assertEqual(task.delivered_by, 'u')

    def test_the_column_starts_empty_for_every_row_that_already_existed(self):
        # The migration backfills nothing on purpose: a timestamp nobody
        # recorded would assert deliveries that were never observed.
        ping = WorkerPing.objects.create(machine='old', user='u')

        self.assertIsNone(ping.last_result_at)


class QueuePageTests(TestCase):
    """The page that lets the community CHECK fairness instead of trusting it."""

    def setUp(self):
        worker_account('miner', 'p')
        worker_account('lesha', 'p')
        self.client = Client()

    def _queue(self, owner, count, budget=128_000_000, offset=0):
        rows = [Position(key=f'{index + offset:064d}', fen=logic.start_fen(),
                         status='UNKNOWN', expanded=False)
                for index in range(count)]
        Position.objects.bulk_create(rows)
        AnalysisTask.objects.bulk_create([
            AnalysisTask(position=position, generation=0, budget_nodes=budget,
                         source=AnalysisTask.Source.USER, requested_by=owner,
                         state='PENDING')
            for position in rows])

    def test_every_account_gets_a_row_of_its_own(self):
        self._queue('miner', 2, offset=0)
        self._queue('lesha', 1, offset=100)

        table = lanes.measure_queue(timezone.now())

        self.assertEqual({row['account'] for row in table['rows']},
                         {'miner', 'lesha'})
        miner = next(row for row in table['rows'] if row['account'] == 'miner')
        self.assertEqual(miner['waiting'], 2)
        self.assertEqual(miner['nodes'], 256_000_000)

    def test_the_anonymous_tide_is_one_row(self):
        self._queue('', 3, offset=0)

        table = lanes.measure_queue(timezone.now())

        self.assertEqual([row['account'] for row in table['rows']], [''])
        self.assertEqual(table['rows'][0]['waiting'], 3)

    def test_an_account_with_nothing_queued_does_not_appear(self):
        self._queue('lesha', 1)

        table = lanes.measure_queue(timezone.now())

        self.assertNotIn('miner', {row['account'] for row in table['rows']})

    def test_a_closed_position_is_not_queue_anybody_is_waiting_behind(self):
        self._queue('lesha', 2)
        Position.objects.filter(key=f'{0:064d}').update(status='WHITE_WIN')

        table = lanes.measure_queue(timezone.now())

        self.assertEqual(table['rows'][0]['waiting'], 1)

    def test_the_page_renders_the_accounts(self):
        self._queue('miner', 1, offset=0)
        self._queue('', 1, offset=100)

        response = self.client.get('/atomicdb/queue/')

        self.assertContains(response, 'miner')
        self.assertContains(response, 'Anonymous')

    def test_the_page_says_so_when_the_queue_is_empty(self):
        response = self.client.get('/atomicdb/queue/')

        self.assertContains(response, 'Nothing is queued right now.')


class ServiceHarness(TestCase):
    """Drains the real lease endpoint, like ``test_queue_fairness`` does."""

    RUNG = 128_000_000

    def setUp(self):
        worker_account('w', 'p')
        self.client = Client()

    def _account(self, user):
        from django.contrib.auth.models import User
        if user and not User.objects.filter(username=user).exists():
            worker_account(user, 'p')

    def _contributor(self, user, machine=None):
        """An account with a fresh delivery on record.

        Since 16 August this buys NOTHING in the queue; the harness keeps it
        so the tests can assert exactly that.
        """
        self._account(user)
        ping = WorkerPing.objects.create(machine=machine or f'{user}-box',
                                         user=user)
        WorkerPing.objects.filter(pk=ping.pk).update(
            last_result_at=timezone.now())
        return ping

    def _queue(self, owner, budgets, offset=0):
        self._account(owner)
        rows = [Position(key=f'{index + offset:064d}', fen=logic.start_fen(),
                         status='UNKNOWN', expanded=False)
                for index in range(len(budgets))]
        Position.objects.bulk_create(rows, batch_size=1000)
        return AnalysisTask.objects.bulk_create([
            AnalysisTask(position=position, generation=0, budget_nodes=budget,
                         source=AnalysisTask.Source.USER, requested_by=owner,
                         state='PENDING')
            for position, budget in zip(rows, budgets)], batch_size=1000)

    def _serve(self, count):
        """The owners of the first ``count`` tasks the queue really hands out."""
        served = []
        for index in range(count):
            tasks = self.client.post('/atomicdb/api/lease', {
                'username': 'w', 'password': 'p', 'machine': f'drain{index}',
                'worker_build': '2026072203', 'lease_session': f'drain{index}',
            }).json()['tasks']
            if not tasks:
                break
            served.append(AnalysisTask.objects.get(id=tasks[0]['id']))
        return served


class AccountShareTests(ServiceHarness):
    """One commons.  Every account advances by the nodes it already has queued."""

    def test_equal_accounts_alternate(self):
        self._queue('miner', [self.RUNG] * 10, offset=0)
        self._queue('lesha', [self.RUNG] * 10, offset=100)

        owners = [task.requested_by for task in self._serve(8)]

        self.assertEqual(owners.count('miner'), 4)
        self.assertEqual(owners.count('lesha'), 4)

    def test_running_a_worker_buys_no_queue_position(self):
        """The 16 August decision, as a regression test.

        Between 10 and 16 August a fresh delivery opened a private lane and a
        one minute worker run was a toll anybody could pay for one.  Now a
        contributor with a delivery minutes old advances exactly like anybody
        else; the only thing a worker buys its owner is affinity while it
        runs, which ``test_queue_fairness`` asserts separately.
        """
        self._contributor('miner')
        self._queue('miner', [self.RUNG] * 10, offset=0)
        self._queue('lesha', [self.RUNG] * 10, offset=100)

        owners = [task.requested_by for task in self._serve(8)]

        self.assertEqual(owners.count('miner'), 4)
        self.assertEqual(owners.count('lesha'), 4)

    def test_an_account_with_nothing_queued_holds_nothing(self):
        self._contributor('miner')
        self._queue('lesha', [self.RUNG] * 4, offset=100)

        owners = [task.requested_by for task in self._serve(4)]

        self.assertEqual(owners, ['lesha'] * 4)

    def test_a_newcomers_first_request_is_not_walled_out(self):
        """The 6 August starvation case stays solved without lanes.

        An account queued 1,562 requests in an hour and the FIFO tail meant
        the next person waited behind all of them.  Per-account node weighting
        is what fixed it, and it never depended on the lanes.
        """
        self._queue('flooder', [self.RUNG] * 200, offset=0)
        self._queue('newcomer', [self.RUNG], offset=1000)

        owners = [task.requested_by for task in self._serve(2)]

        self.assertIn('newcomer', owners)

    def test_every_account_advances_at_the_same_rate(self):
        """Six accounts, one commons: an equal split, whoever they are.

        This is the honest cost of dropping the lanes: alt accounts are
        accounts.  The per-person cap and the public queue page are the
        guards, and the fleet owner can see the split this test pins down.
        """
        self._queue('miner', [self.RUNG] * 10, offset=0)
        for index in range(5):
            self._queue(f'alt{index}', [self.RUNG] * 10,
                        offset=100 + 20 * index)

        owners = [task.requested_by for task in self._serve(12)]

        self.assertEqual(owners.count('miner'), 2)
        for index in range(5):
            self.assertEqual(owners.count(f'alt{index}'), 2)


class BackedRequestLaneTests(ServiceHarness):
    """The 15 August case: backing must move a request, not decorate it.

    Reported by Eclipsia on a request authored by soothdest.  The author was
    about 2690 rows deep, so the request stayed buried and backing it changed
    nothing anybody could see.
    """

    def test_a_backed_request_rides_the_backers_empty_queue(self):
        self._account('eclipsia')
        self._account('soothdest')
        buried = self._queue('soothdest', [self.RUNG] * 200, offset=0)

        ingest.add_requester(buried[150], 'eclipsia')

        served = self._serve(1)[0]
        self.assertEqual(served.id, buried[150].id)
        self.assertEqual(served.requested_by, 'soothdest')
        self.assertEqual(served.lane_account, 'eclipsia')

    def test_the_author_keeps_the_request(self):
        """Lending a queue is not taking the request."""
        self._account('eclipsia')
        task, = self._queue('soothdest', [self.RUNG], offset=0)

        ingest.add_requester(task, 'eclipsia')

        task.refresh_from_db()
        self.assertEqual(task.requested_by, 'soothdest')
        self.assertEqual(task.also_requested_by, ['eclipsia'])

    def test_a_backer_with_the_heavier_queue_lends_nothing(self):
        self._account('eclipsia')
        self._queue('eclipsia', [self.RUNG] * 50, offset=200)
        task, = self._queue('soothdest', [self.RUNG], offset=0)

        ingest.add_requester(task, 'eclipsia')

        task.refresh_from_db()
        self.assertEqual(task.lane_account, '')

    def test_an_unbacked_request_is_untouched(self):
        task, = self._queue('soothdest', [self.RUNG], offset=0)

        task.refresh_from_db()
        self.assertEqual(task.lane_account, '')

    def test_a_named_backer_adopts_an_anonymous_request(self):
        self._account('eclipsia')
        task, = self._queue('', [self.RUNG], offset=0)

        ingest.add_requester(task, 'eclipsia')

        task.refresh_from_db()
        self.assertEqual(task.requested_by, 'eclipsia')
        self.assertEqual(task.lane_account, '')


class AccountQueueCapTests(TestCase):
    """The cap is per person, counted in requests, and it is a backstop.

    It used to be global: the total of every pending request in the project,
    so one busy account locked out people who had queued nothing.  That is the
    same hoarding the per-account weighting exists to undo, moved to the front
    door.
    """

    def setUp(self):
        worker_account('busy', 'p')
        worker_account('newcomer', 'p')
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.targets = [edge.child for edge in
                        Edge.objects.filter(parent=self.root)
                        .order_by('move_uci')[:2]]

    def _fill(self, owner, count):
        rows = [Position(key=f'{index:064d}', fen=logic.start_fen(),
                         status='UNKNOWN', expanded=False)
                for index in range(count)]
        Position.objects.bulk_create(rows, batch_size=1000)
        AnalysisTask.objects.bulk_create([
            AnalysisTask(position=position, generation=0,
                         budget_nodes=128_000_000,
                         source=AnalysisTask.Source.USER, requested_by=owner,
                         state='PENDING')
            for position in rows], batch_size=1000)

    def _request(self, key, username=None):
        if username:
            self.client.login(username=username, password='p')
        return self.client.post(f'/atomicdb/request/{key}/')

    def test_a_full_account_is_refused(self):
        self._fill('busy', views.REQUEST_QUEUE_MAX)

        response = self._request(self.targets[0].key, 'busy')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'queue-full-account')

    def test_a_full_account_blocks_nobody_else(self):
        """The whole difference from the global cap it replaces."""
        self._fill('busy', views.REQUEST_QUEUE_MAX)

        response = self._request(self.targets[0].key, 'newcomer')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'queued')

    def test_the_anonymous_tide_shares_one_allowance(self):
        # One shared identity, so one cap, the same decision the sharing and
        # the deep lease cap already make about anonymous traffic.
        self._fill('', views.REQUEST_QUEUE_MAX)

        response = self._request(self.targets[0].key)

        self.assertEqual(response.json()['status'], 'queue-full-account')

    def test_running_work_does_not_spend_the_allowance(self):
        self._fill('busy', views.REQUEST_QUEUE_MAX)
        AnalysisTask.objects.filter(requested_by='busy').update(state='LEASED')

        response = self._request(self.targets[0].key, 'busy')

        self.assertEqual(response.json()['status'], 'queued')

    def test_requests_on_solved_positions_do_not_spend_it_either(self):
        self._fill('busy', views.REQUEST_QUEUE_MAX)
        Position.objects.filter(key__in=[f'{i:064d}' for i in range(10)]
                                ).update(status='WHITE_WIN')

        response = self._request(self.targets[0].key, 'busy')

        self.assertEqual(response.json()['status'], 'queued')

    def test_the_refusal_leaves_a_receipt(self):
        from .models import DBEvent
        self._fill('busy', views.REQUEST_QUEUE_MAX)

        self._request(self.targets[0].key, 'busy')

        event = DBEvent.objects.filter(kind='LANE_CAP_HIT').first()
        self.assertIsNotNone(event)
        self.assertEqual(event.payload['account'], 'busy')

    def test_the_bulk_button_respects_the_same_allowance(self):
        self._fill('busy', views.REQUEST_QUEUE_MAX)
        self.client.login(username='busy', password='p')

        response = self.client.post(
            f'/atomicdb/request-unexplored/{self.root.key}/')

        self.assertEqual(response.json()['status'], 'queue-full-account')


class CancelledWorkCostsNothingTests(TestCase):
    """A request you took back stops counting, everywhere it counted.

    CANCELLED landed beside the per-account weighting, and the two features
    meet in three places: the nodes an account is charged, the allowance a
    person spends, and the queue page.  A cancelled row that still weighed in
    any of them would mean taking a request back did not give you anything
    back.
    """

    def setUp(self):
        worker_account('asfault', 'p')
        self.client = Client()

    def _queue(self, owner, count, state='PENDING', offset=0):
        rows = [Position(key=f'{index + offset:064d}', fen=logic.start_fen(),
                         status='UNKNOWN', expanded=False)
                for index in range(count)]
        Position.objects.bulk_create(rows, batch_size=1000)
        return AnalysisTask.objects.bulk_create([
            AnalysisTask(position=position, generation=0,
                         budget_nodes=128_000_000,
                         source=AnalysisTask.Source.USER, requested_by=owner,
                         state=state)
            for position in rows], batch_size=1000)

    def test_cancelled_rows_are_not_charged_to_an_account(self):
        self._queue('asfault', 3, state=AnalysisTask.TState.CANCELLED)

        loads = lanes.charged_loads(['asfault'])

        self.assertEqual(loads.get('asfault', 0), 0)

    def test_cancelled_rows_do_not_show_on_the_queue_page(self):
        self._queue('asfault', 2, state=AnalysisTask.TState.CANCELLED)

        table = lanes.measure_queue(timezone.now())

        self.assertEqual(table['rows'], [])

    def test_cancelled_rows_do_not_spend_the_allowance(self):
        from . import views
        self._queue('asfault', views.REQUEST_QUEUE_MAX,
                    state=AnalysisTask.TState.CANCELLED)

        self.assertFalse(views._account_queue_full('asfault'))

    def test_clearing_your_queue_gives_the_allowance_back(self):
        from . import views
        self._queue('asfault', views.REQUEST_QUEUE_MAX)
        self.assertTrue(views._account_queue_full('asfault'))

        ingest.clear_own_queue('asfault')

        self.assertFalse(views._account_queue_full('asfault'))


class HandoverLaneTests(TestCase):
    """Taking a request back hands it on, and the charge goes with the owner."""

    def setUp(self):
        for name in ('soothdest', 'eclipsia'):
            worker_account(name, 'p')

    def _task(self, owner, backers, offset=0):
        position = Position.objects.create(key=f'{offset:064d}',
                                           fen=logic.start_fen(),
                                           status='UNKNOWN', expanded=False)
        return AnalysisTask.objects.create(
            position=position, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER, requested_by=owner,
            also_requested_by=list(backers), backers=len(backers),
            state='PENDING')

    def test_a_handover_to_the_queue_lender_drops_the_loan(self):
        """The backer who lent the queue becomes the author, so the loan ends.

        Lending yourself a queue is what an empty column already means, and
        leaving the old name behind would charge the row to somebody who is
        no longer on it.
        """
        task = self._task('soothdest', ['eclipsia'])
        task.lane_account = 'eclipsia'
        task.save(update_fields=['lane_account'])

        self.assertEqual(ingest.withdraw_requester(task), 'handed-over')

        task.refresh_from_db()
        self.assertEqual(task.requested_by, 'eclipsia')
        self.assertEqual(task.lane_account, '')
