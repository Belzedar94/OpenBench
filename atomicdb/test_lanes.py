"""Lanes: who owns a share of the fleet, what earns one, and who rides in it.

The community voted contributor lanes in on 10 August.  This file covers the
vocabulary (who owns a lane), the evidence (what earns one), and the two things
that decide which lane a REQUEST travels in: the account that asked for it, and
the account that lent it a lighter queue by backing it.

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
    """A lane is earned by plugging in CPU, and by nothing else."""

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

    def test_a_recent_delivery_earns_a_lane(self):
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
        searching.  A lane is bought with delivered work.
        """
        self._ping('miner', delivered=False)

        self.assertEqual(lanes.contributor_accounts(), frozenset())
        self.assertFalse(lanes.ran_a_worker('miner'))

    def test_a_delivery_inside_the_window_counts_when_switched_off(self):
        # Somebody who turns the machine off for the weekend keeps their lane:
        # the row stays, the window is what forgets.
        self._ping('miner', age=timedelta(days=3))

        self.assertEqual(lanes.contributor_accounts(), frozenset({'miner'}))

    def test_a_revoked_account_loses_its_lane(self):
        from OpenBench.models import Profile
        self._ping('miner')
        Profile.objects.filter(user__username='miner').update(enabled=False)

        self.assertEqual(lanes.contributor_accounts(), frozenset())

    def test_an_account_that_never_delivered_has_no_lane(self):
        self._ping('miner')

        self.assertFalse(lanes.ran_a_worker('lurker'))
        self.assertTrue(lanes.ran_a_worker('miner'))

    def test_staff_alone_earns_no_lane(self):
        # The owner opens the rung selector with is_staff, and that shortcut
        # deliberately does NOT open a lane: the vote said CPU earns lanes.
        from django.contrib.auth.models import User
        User.objects.filter(username='lurker').update(is_staff=True)

        self.assertEqual(lanes.contributor_accounts(), frozenset())

    def test_the_rung_selector_reads_the_same_predicate(self):
        from django.contrib.auth.models import User
        from . import depth
        self._ping('miner')

        self.assertTrue(depth.may_choose(User.objects.get(username='miner')))
        self.assertFalse(depth.may_choose(User.objects.get(username='lurker')))


class LaneAssignmentTests(TestCase):
    """Contributors get their own lane; everybody else shares the commons."""

    def test_a_contributor_owns_a_lane_named_after_the_account(self):
        self.assertEqual(lanes.lane_of('miner', frozenset({'miner'})), 'miner')

    def test_a_named_non_contributor_falls_into_the_commons(self):
        self.assertEqual(lanes.lane_of('lesha', frozenset({'miner'})),
                         lanes.LANE_COMMONS)

    def test_anonymous_falls_into_the_commons(self):
        self.assertEqual(lanes.lane_of('', frozenset({'miner'})),
                         lanes.LANE_COMMONS)

    def test_alt_accounts_all_land_in_the_same_lane(self):
        contributors = frozenset({'miner'})
        alts = {lanes.lane_of(f'alt{index}', contributors)
                for index in range(5)}

        self.assertEqual(alts, {lanes.LANE_COMMONS})


class DeliveryEvidenceTests(TestCase):
    """``last_result_at`` records a DELIVERY, not a hello."""

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

    def _contributor(self, user, machine='m1'):
        """An account that has delivered, which is what opens a lane."""
        ping = WorkerPing.objects.create(machine=machine, user=user)
        WorkerPing.objects.filter(pk=ping.pk).update(
            last_result_at=timezone.now())
        return ping

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

    def test_a_contributor_gets_a_row_of_their_own(self):
        self._contributor('miner')
        self._queue('miner', 2)

        table = lanes.measure_lanes(timezone.now())

        self.assertEqual([row['lane'] for row in table['rows']], ['miner'])
        self.assertEqual(table['rows'][0]['waiting'], 2)
        self.assertEqual(table['rows'][0]['nodes'], 256_000_000)

    def test_named_strangers_and_the_anonymous_tide_share_one_row(self):
        self._queue('lesha', 2, offset=0)
        self._queue('', 3, offset=100)

        table = lanes.measure_lanes(timezone.now())

        self.assertEqual([row['lane'] for row in table['rows']],
                         [lanes.LANE_COMMONS])
        commons = table['rows'][0]
        self.assertEqual(commons['waiting'], 5)
        # Two members: the named stranger, and the anonymous tide as ONE.
        self.assertEqual(commons['member_count'], 2)

    def test_a_contributor_and_the_commons_are_separate_rows(self):
        self._contributor('miner')
        self._queue('miner', 1, offset=0)
        self._queue('lesha', 1, offset=100)

        table = lanes.measure_lanes(timezone.now())

        self.assertEqual({row['lane'] for row in table['rows']},
                         {'miner', lanes.LANE_COMMONS})

    def test_a_lane_with_nothing_queued_does_not_appear(self):
        self._contributor('miner')
        self._queue('lesha', 1)

        table = lanes.measure_lanes(timezone.now())

        self.assertNotIn('miner', {row['lane'] for row in table['rows']})

    def test_a_closed_position_is_not_queue_anybody_is_waiting_behind(self):
        self._queue('lesha', 2)
        Position.objects.filter(key=f'{0:064d}').update(status='WHITE_WIN')

        table = lanes.measure_lanes(timezone.now())

        self.assertEqual(table['rows'][0]['waiting'], 1)

    def test_the_page_renders_the_lanes(self):
        self._contributor('miner')
        self._queue('miner', 1, offset=0)
        self._queue('', 1, offset=100)

        response = self.client.get('/atomicdb/queue/')

        self.assertContains(response, 'miner')
        self.assertContains(response, 'Commons')

    def test_the_page_says_so_when_the_queue_is_empty(self):
        response = self.client.get('/atomicdb/queue/')

        self.assertContains(response, 'Nothing is queued right now.')


class LaneServiceHarness(TestCase):
    """Drains the real lease endpoint, like ``test_queue_fairness`` does."""

    RUNG = 128_000_000

    def setUp(self):
        worker_account('w', 'p')
        self.client = Client()

    def _account(self, user):
        """A real account: user AND enabled profile.

        Not optional scaffolding.  A lane needs an ENABLED profile, so a test
        that queues to a bare username puts everybody in the commons and then
        passes for the wrong reason.
        """
        from django.contrib.auth.models import User
        if user and not User.objects.filter(username=user).exists():
            worker_account(user, 'p')

    def _contributor(self, user, machine=None):
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


class LaneShareTests(LaneServiceHarness):
    """A lane is the unit that gets an equal share, whoever is standing in it."""

    def test_a_contributor_and_the_commons_split_the_fleet(self):
        self._contributor('miner')
        self._queue('miner', [self.RUNG] * 10, offset=0)
        self._queue('lesha', [self.RUNG] * 10, offset=100)

        owners = [task.requested_by for task in self._serve(8)]

        # One lane each: the contributor and the commons alternate.
        self.assertEqual(owners.count('miner'), 4)
        self.assertEqual(owners.count('lesha'), 4)

    def test_alt_accounts_buy_nothing(self):
        """Five alts in the commons get, between them, what one account gets.

        This is the property the vote asked for in as many words: "creating
        alt accounts gains nothing: only plugging in CPU earns you a lane".
        """
        self._contributor('miner')
        self._queue('miner', [self.RUNG] * 10, offset=0)
        for index in range(5):
            self._queue(f'alt{index}', [self.RUNG] * 10,
                        offset=100 + 20 * index)

        owners = [task.requested_by for task in self._serve(10)]

        self.assertEqual(owners.count('miner'), 5)
        self.assertEqual(sum(owners.count(f'alt{i}') for i in range(5)), 5)

    def test_a_contributor_with_nothing_queued_holds_nothing(self):
        self._contributor('miner')
        self._queue('lesha', [self.RUNG] * 4, offset=100)

        owners = [task.requested_by for task in self._serve(4)]

        self.assertEqual(owners, ['lesha'] * 4)

    def test_a_lane_that_falls_out_of_the_window_joins_the_commons(self):
        ping = self._contributor('miner')
        self._queue('miner', [self.RUNG] * 6, offset=0)
        self._queue('lesha', [self.RUNG] * 6, offset=100)
        WorkerPing.objects.filter(pk=ping.pk).update(
            last_result_at=timezone.now()
            - timedelta(days=lanes.CONTRIBUTOR_WINDOW_DAYS, hours=1))

        owners = [task.requested_by for task in self._serve(4)]

        # Two members of one commons now, so they still alternate, but on the
        # commons share rather than on a lane of their own.  Nothing is lost.
        self.assertEqual(owners.count('miner'), 2)
        self.assertEqual(owners.count('lesha'), 2)
        self.assertEqual(AnalysisTask.objects.filter(state='PENDING').count(), 8)


class BackedRequestLaneTests(LaneServiceHarness):
    """The 15 August case: backing must move a request, not decorate it.

    Reported by Eclipsia on a request authored by soothdest.  The author was
    about 2690 rows deep, so the request stayed buried and backing it changed
    nothing anybody could see.
    """

    def test_a_backed_request_rides_the_backers_empty_queue(self):
        self._contributor('eclipsia')
        self._contributor('soothdest')
        buried = self._queue('soothdest', [self.RUNG] * 200, offset=0)

        ingest.add_requester(buried[150], 'eclipsia')

        served = self._serve(1)[0]
        self.assertEqual(served.id, buried[150].id)
        self.assertEqual(served.requested_by, 'soothdest')
        self.assertEqual(served.lane_account, 'eclipsia')

    def test_the_author_keeps_the_request(self):
        """Lending a lane is not taking the request."""
        self._contributor('eclipsia')
        task, = self._queue('soothdest', [self.RUNG], offset=0)

        ingest.add_requester(task, 'eclipsia')

        task.refresh_from_db()
        self.assertEqual(task.requested_by, 'soothdest')
        self.assertEqual(task.also_requested_by, ['eclipsia'])

    def test_a_backer_with_the_heavier_queue_lends_nothing(self):
        self._contributor('eclipsia')
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
        task, = self._queue('', [self.RUNG], offset=0)

        ingest.add_requester(task, 'eclipsia')

        task.refresh_from_db()
        self.assertEqual(task.requested_by, 'eclipsia')
        self.assertEqual(task.lane_account, '')


class AccountQueueCapTests(TestCase):
    """The cap is per person, counted in requests, and it is a backstop.

    It used to be global: the total of every pending request in the project,
    so one busy account locked out people who had queued nothing.  That is the
    same hoarding the lanes exist to undo, moved to the front door.
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
        # One member of the commons, so one cap, the same decision the sharing
        # and the deep lease cap already make about anonymous traffic.
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

    CANCELLED landed beside the lanes, and the two features meet in three
    places: the nodes a lane is charged, the allowance a person spends, and
    the queue page.  A cancelled row that still weighed in any of them would
    mean taking a request back did not give you anything back.
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

    def test_cancelled_rows_are_not_charged_to_a_lane(self):
        self._queue('asfault', 3, state=AnalysisTask.TState.CANCELLED)

        loads = lanes.charged_loads(['asfault'])

        self.assertEqual(loads.get('asfault', 0), 0)

    def test_cancelled_rows_do_not_show_on_the_queue_page(self):
        self._queue('asfault', 2, state=AnalysisTask.TState.CANCELLED)

        table = lanes.measure_lanes(timezone.now())

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
    """Taking a request back hands it on, and the lane goes with the owner."""

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

    def test_a_handover_to_the_lane_lender_drops_the_loan(self):
        """The backer who lent the lane becomes the author, so the loan ends.

        Lending yourself a lane is what an empty column already means, and
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
