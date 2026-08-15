"""Lanes: who owns a share of the fleet, and what earns one.

The community voted contributor lanes in on 10 August.  This file covers the
vocabulary and the evidence, which is all that ships in the first step: the
ordering does not read lanes yet, so every assertion here is about what the
server KNOWS, not about who gets served first.
"""

import json
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from . import ingest, lanes, logic
from .models import AnalysisTask, Position, WorkerPing
from .testing import TestCase, worker_account


class ContributorPredicateTests(TestCase):
    """A lane is earned by plugging in CPU, and by nothing else."""

    def setUp(self):
        worker_account('miner', 'p')
        worker_account('lurker', 'p')

    def _ping(self, user, machine='m1', age=None):
        ping = WorkerPing.objects.create(machine=machine, user=user)
        if age is not None:
            WorkerPing.objects.filter(pk=ping.pk).update(
                last_seen=timezone.now() - age)
        return ping

    def test_a_recent_worker_earns_a_lane(self):
        self._ping('miner')

        self.assertEqual(lanes.contributor_accounts(), frozenset({'miner'}))

    def test_a_worker_older_than_the_window_does_not(self):
        self._ping('miner', age=timedelta(days=lanes.CONTRIBUTOR_WINDOW_DAYS,
                                          hours=1))

        self.assertEqual(lanes.contributor_accounts(), frozenset())

    def test_a_worker_inside_the_window_still_counts_when_switched_off(self):
        # Somebody who turns the machine off for the weekend keeps their lane:
        # the row stays, the window is what forgets.
        self._ping('miner', age=timedelta(days=3))

        self.assertEqual(lanes.contributor_accounts(), frozenset({'miner'}))

    def test_a_revoked_account_loses_its_lane(self):
        from OpenBench.models import Profile
        self._ping('miner')
        Profile.objects.filter(user__username='miner').update(enabled=False)

        self.assertEqual(lanes.contributor_accounts(), frozenset())

    def test_an_account_that_never_ran_a_worker_has_no_lane(self):
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
        WorkerPing.objects.create(machine='m1', user='miner')
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
        WorkerPing.objects.create(machine='m1', user='miner')
        self._queue('miner', 1, offset=0)
        self._queue('lesha', 1, offset=100)

        table = lanes.measure_lanes(timezone.now())

        self.assertEqual({row['lane'] for row in table['rows']},
                         {'miner', lanes.LANE_COMMONS})

    def test_a_lane_with_nothing_queued_does_not_appear(self):
        WorkerPing.objects.create(machine='m1', user='miner')
        self._queue('lesha', 1)

        table = lanes.measure_lanes(timezone.now())

        self.assertNotIn('miner', {row['lane'] for row in table['rows']})

    def test_a_closed_position_is_not_queue_anybody_is_waiting_behind(self):
        self._queue('lesha', 2)
        Position.objects.filter(key=f'{0:064d}').update(status='WHITE_WIN')

        table = lanes.measure_lanes(timezone.now())

        self.assertEqual(table['rows'][0]['waiting'], 1)

    def test_the_page_renders_the_lanes(self):
        WorkerPing.objects.create(machine='m1', user='miner')
        self._queue('miner', 1, offset=0)
        self._queue('', 1, offset=100)

        response = self.client.get('/atomicdb/queue/')

        self.assertContains(response, 'miner')
        self.assertContains(response, 'Commons')

    def test_the_page_says_so_when_the_queue_is_empty(self):
        response = self.client.get('/atomicdb/queue/')

        self.assertContains(response, 'Nothing is queued right now.')
