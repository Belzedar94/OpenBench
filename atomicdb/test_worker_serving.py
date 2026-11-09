"""One worker's own serving mode and campaign (cz_eclipsia, 8-Nov-2026).

An account's workers follow the account's serve order unless the
contributor gives one machine a mode of its own (§ ``WorkerServing``):
``server`` serves the queue with no preference for the account's requests,
``personal`` serves only the account's requests and its listed accounts',
and nothing else.  A worker or the account can also follow a campaign,
whose selector tasks then go before the rest of the selector's work.
Nothing changes for a worker or account that set none of this.
"""

from datetime import timedelta
from unittest import mock

from django.test import Client
from django.utils import timezone

from . import ingest, logic, preferences, views
from .models import (AnalysisTask, Campaign, ContributorPref, Edge, Position,
                     WorkerPing, WorkerServing)
from .testing import TestCase, worker_account

PAGE = '/atomicdb/notifications/settings/'
BOX = 'lesha-box-atomicdb'


class WorkerServingLeaseTests(TestCase):

    def setUp(self):
        worker_account('w', 'p')
        worker_account('lesha', 'p')
        worker_account('friend', 'p')
        self.client = Client()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        self.root = root
        edges = list(Edge.objects.filter(parent=root).order_by('move_uci')[:4])
        self.p = [edge.child for edge in edges]
        for position, priority in zip(self.p, (-73.0, 8.0, 3.0, 50.0)):
            Position.objects.filter(key=position.key).update(
                priority=priority)
        self.slot = 0

    def _lease(self, machine=BOX, username='lesha'):
        # Each lease from its own slot of the machine, so one held lease
        # does not answer the next as a replay.
        self.slot += 1
        name = f'{machine}#{self.slot}'
        return self.client.post('/atomicdb/api/lease', {
            'username': username, 'password': 'p', 'machine': name,
            'worker_build': '2026072203', 'lease_session': name,
        }).json()['tasks']

    def _task(self, position, source, requested_by=''):
        return AnalysisTask.objects.create(
            position=position, generation=0, budget_nodes=8_000_000,
            source=source, requested_by=requested_by)

    def _request(self, position, requested_by):
        return self._task(position, AnalysisTask.Source.USER, requested_by)

    def _auto(self, position):
        return self._task(position, AnalysisTask.Source.AUTO)

    def _mode(self, mode, campaign=None, machine=BOX, account='lesha'):
        WorkerServing.objects.create(account=account, machine=machine,
                                     mode=mode, campaign=campaign)

    def _campaign(self, *positions):
        campaign = Campaign.objects.create(
            name=f'line-{Campaign.objects.count()}', root=self.root,
            state=Campaign.CState.ACTIVE)
        Position.objects.filter(
            key__in=[p.key for p in positions]).update(campaign=campaign)
        return campaign

    # ---- defaults -------------------------------------------------------

    def test_no_row_reads_the_account_default(self):
        self.assertEqual(preferences.worker_serving('lesha', BOX), ('', None))
        self.assertEqual(preferences.worker_serving('', BOX), ('', None))

    def test_no_row_keeps_own_first(self):
        self._request(self.p[0], 'w')
        own = self._request(self.p[1], 'lesha')

        self.assertEqual(self._lease()[0]['id'], own.id)

    def test_a_row_for_another_machine_changes_nothing_here(self):
        self._mode('server', machine='lesha-other-atomicdb')
        self._request(self.p[0], 'w')
        own = self._request(self.p[1], 'lesha')

        self.assertEqual(self._lease()[0]['id'], own.id)

    def test_no_campaign_keeps_the_selector_order(self):
        self._campaign(self.p[0])
        self._auto(self.p[0])           # priority -73, in a campaign
        best = self._auto(self.p[3])    # priority 50

        self.assertEqual(self._lease()[0]['id'], best.id)

    # ---- server ---------------------------------------------------------

    def test_server_mode_serves_the_older_request_over_its_own(self):
        stranger = self._request(self.p[0], 'w')
        self._request(self.p[1], 'lesha')
        self._mode('server')

        self.assertEqual(self._lease()[0]['id'], stranger.id)

    def test_server_mode_ignores_the_listed_accounts(self):
        ContributorPref.objects.create(account='lesha', serve_order='others',
                                       serve_for='friend')
        stranger = self._request(self.p[0], 'w')
        self._request(self.p[1], 'friend')
        self._mode('server')

        self.assertEqual(self._lease()[0]['id'], stranger.id)

    def test_server_mode_still_serves_own_requests_in_turn(self):
        own = self._request(self.p[1], 'lesha')
        self._auto(self.p[3])
        self._mode('server')

        self.assertEqual(self._lease()[0]['id'], own.id)

    def test_every_slot_of_the_machine_shares_the_row(self):
        self._mode('server')
        self.assertEqual(preferences.worker_serving('lesha', BOX + '#7'),
                         ('server', None))

    # ---- personal -------------------------------------------------------

    def test_personal_mode_takes_its_own_request(self):
        self._request(self.p[0], 'w')
        self._auto(self.p[3])
        own = self._request(self.p[1], 'lesha')
        self._mode('personal')

        self.assertEqual(self._lease()[0]['id'], own.id)

    def test_personal_mode_is_idle_without_own_requests(self):
        self._request(self.p[0], 'w')
        self._auto(self.p[3])
        self._mode('personal')

        with mock.patch.object(ingest, 'next_tasks') as refill:
            self.assertEqual(self._lease(), [])
        refill.assert_not_called()
        self.assertFalse(AnalysisTask.objects.filter(state='LEASED').exists())

    def test_personal_mode_serves_the_listed_accounts(self):
        # Under every serve order, queue order included.
        ContributorPref.objects.create(account='lesha', serve_order='queue',
                                       serve_for='friend, anonymous')
        self._request(self.p[0], 'w')
        friend = self._request(self.p[1], 'friend')
        anonymous = self._request(self.p[2], '')
        self._mode('personal')

        served = {self._lease()[0]['id'], self._lease()[0]['id']}

        self.assertEqual(served, {friend.id, anonymous.id})
        self.assertEqual(self._lease(), [])

    # ---- campaign -------------------------------------------------------

    def test_a_campaign_task_goes_before_other_selector_work(self):
        campaign = self._campaign(self.p[0])
        mine = self._auto(self.p[0])       # priority -73
        self._auto(self.p[3])              # priority 50
        self._task(self.p[1], AnalysisTask.Source.FILL)
        self._mode('', campaign=campaign.id)

        self.assertEqual(self._lease()[0]['id'], mine.id)

    def test_requests_still_go_before_the_campaign(self):
        campaign = self._campaign(self.p[0])
        self._auto(self.p[0])
        stranger = self._request(self.p[1], 'w')
        self._mode('', campaign=campaign.id)

        self.assertEqual(self._lease()[0]['id'], stranger.id)

    def test_an_empty_campaign_falls_back_to_the_selector(self):
        campaign = self._campaign(self.p[2])   # no task on it
        self._auto(self.p[0])
        best = self._auto(self.p[3])
        self._mode('server', campaign=campaign.id)

        self.assertEqual(self._lease()[0]['id'], best.id)

    def test_the_account_campaign_reaches_a_worker_without_a_row(self):
        campaign = self._campaign(self.p[0])
        ContributorPref.objects.create(account='lesha',
                                       serve_campaign=campaign.id)
        mine = self._auto(self.p[0])
        self._auto(self.p[3])

        self.assertEqual(self._lease()[0]['id'], mine.id)

    def test_no_campaign_on_the_worker_overrides_the_account(self):
        campaign = self._campaign(self.p[0])
        ContributorPref.objects.create(account='lesha',
                                       serve_campaign=campaign.id)
        self._auto(self.p[0])
        best = self._auto(self.p[3])
        self._mode('', campaign=0)

        self.assertEqual(self._lease()[0]['id'], best.id)

    def test_personal_mode_takes_the_campaign_once_its_own_are_done(self):
        campaign = self._campaign(self.p[0])
        line = self._auto(self.p[0])
        self._auto(self.p[3])
        self._request(self.p[2], 'w')
        own = self._request(self.p[1], 'lesha')
        self._mode('personal', campaign=campaign.id)

        self.assertEqual(self._lease()[0]['id'], own.id)
        self.assertEqual(self._lease()[0]['id'], line.id)
        self.assertEqual(self._lease(), [])

    # ---- other accounts -------------------------------------------------

    def test_another_account_with_the_same_machine_name_is_unaffected(self):
        self._mode('personal', machine='box', account='lesha')
        stranger = self._request(self.p[0], 'lesha')
        own = self._request(self.p[1], 'w')

        self.assertEqual(self._lease('box', username='w')[0]['id'], own.id)
        self.assertEqual(self._lease('box', username='w')[0]['id'],
                         stranger.id)

    def test_another_accounts_campaign_does_not_reorder_this_one(self):
        campaign = self._campaign(self.p[0])
        ContributorPref.objects.create(account='lesha',
                                       serve_campaign=campaign.id)
        self._mode('personal', machine='w-box-atomicdb', account='lesha')
        self._auto(self.p[0])
        best = self._auto(self.p[3])

        self.assertEqual(
            self._lease('w-box-atomicdb', username='w')[0]['id'], best.id)


class BackedCountsAsOwnTests(TestCase):
    """A request the account only BACKED counts as its own (Torom, 8-Nov).

    Joining somebody's request (``also_requested_by``) is asking for it, so
    the account's worker prefers it as it prefers its own requests: Mine
    first, Mine only, and the accounts listed to be served.  The owner is
    still the one charged in the fair share, and every other worker leases
    exactly as before.
    """

    setUp = WorkerServingLeaseTests.setUp
    _lease = WorkerServingLeaseTests._lease
    _task = WorkerServingLeaseTests._task
    _request = WorkerServingLeaseTests._request
    _auto = WorkerServingLeaseTests._auto
    _mode = WorkerServingLeaseTests._mode

    def _backed(self, position, owner, *backers):
        task = self._request(position, owner)
        AnalysisTask.objects.filter(pk=task.pk).update(
            also_requested_by=list(backers), backers=len(backers))
        return task

    def _queue(self):
        # ``w`` owns both; its second is behind its first in the fair share
        # (8M of its own ahead), and lesha only backed the second.
        first = self._request(self.p[0], 'w')
        backed = self._backed(self.p[1], 'w', 'lesha')
        return first, backed

    # ---- Mine first -----------------------------------------------------

    def test_mine_first_takes_the_backed_request_before_the_queue(self):
        _first, backed = self._queue()

        self.assertEqual(self._lease()[0]['id'], backed.id)

    def test_another_account_still_leases_in_queue_order(self):
        first, backed = self._queue()

        self.assertEqual(self._lease('friend-box', 'friend')[0]['id'],
                         first.id)
        # ...and each request goes out once: the backer's worker gets the
        # one it backed, and a third lease gets neither (the selector's
        # refill, if anything).
        self.assertEqual(self._lease()[0]['id'], backed.id)
        third = [t['id'] for t in self._lease('friend-box', 'friend')]
        self.assertNotIn(first.id, third)
        self.assertNotIn(backed.id, third)
        self.assertEqual(AnalysisTask.objects.filter(
            pk__in=[first.id, backed.id], state='LEASED', attempts=1).count(),
            2)

    def test_the_owner_is_still_the_one_charged(self):
        _first, backed = self._queue()
        self._lease()

        backed.refresh_from_db()
        self.assertEqual(backed.requested_by, 'w')
        self.assertEqual(backed.also_requested_by, ['lesha'])

    def test_queue_order_gives_the_backed_request_no_preference(self):
        ContributorPref.objects.create(account='lesha', serve_order='queue')
        first, _backed = self._queue()

        self.assertEqual(self._lease()[0]['id'], first.id)

    def test_others_first_puts_the_backed_request_behind_the_rest(self):
        # The backed request is "mine" there too, so it waits.
        ContributorPref.objects.create(account='lesha', serve_order='others')
        backed = self._backed(self.p[0], 'w', 'lesha')
        stranger = self._request(self.p[1], 'friend')

        self.assertEqual(self._lease()[0]['id'], stranger.id)
        self.assertEqual(self._lease()[0]['id'], backed.id)

    def test_a_request_backed_by_someone_else_is_not_mine(self):
        first = self._request(self.p[0], 'w')
        self._backed(self.p[1], 'w', 'friend')

        self.assertEqual(self._lease()[0]['id'], first.id)

    # ---- Mine only ------------------------------------------------------

    def test_mine_only_takes_the_backed_request(self):
        _first, backed = self._queue()
        self._auto(self.p[3])
        self._mode('personal')

        with mock.patch.object(ingest, 'next_tasks') as refill:
            self.assertEqual(self._lease()[0]['id'], backed.id)
            self.assertEqual(self._lease(), [])
        refill.assert_not_called()

    def test_mine_only_idles_with_nothing_own_or_backed(self):
        self._request(self.p[0], 'w')
        self._backed(self.p[1], 'w', 'friend')
        self._auto(self.p[3])
        self._mode('personal')

        with mock.patch.object(ingest, 'next_tasks') as refill:
            self.assertEqual(self._lease(), [])
        refill.assert_not_called()
        self.assertFalse(AnalysisTask.objects.filter(state='LEASED').exists())

    # ---- the listed accounts --------------------------------------------

    def test_mine_only_serves_what_a_listed_account_backed(self):
        # Listed in another case: the list is matched without case.
        ContributorPref.objects.create(account='lesha', serve_order='queue',
                                       serve_for='Friend')
        self._request(self.p[0], 'w')
        theirs = self._backed(self.p[1], 'w', 'friend')
        self._mode('personal')

        self.assertEqual(self._lease()[0]['id'], theirs.id)
        self.assertEqual(self._lease(), [])

    def test_others_first_serves_what_a_listed_account_backed_first(self):
        ContributorPref.objects.create(account='lesha', serve_order='others',
                                       serve_for='friend')
        self._request(self.p[0], 'w')
        theirs = self._backed(self.p[1], 'w', 'friend')

        self.assertEqual(self._lease()[0]['id'], theirs.id)

    # ---- nobody ---------------------------------------------------------

    def test_an_anonymous_backer_is_never_matched(self):
        # "anonymous" in the list names the requests made without an
        # account; a backer without a name is nobody's.
        ContributorPref.objects.create(account='lesha', serve_order='queue',
                                       serve_for='anonymous')
        self._backed(self.p[0], 'w', '')
        self._mode('personal')

        self.assertEqual(self._lease(), [])
        self.assertFalse(AnalysisTask.objects.filter(
            views._backed_q(['']) | views._backed_q([])).exists())

    def test_postgres_uses_json_containment(self):
        connections = mock.MagicMock()
        connections.__getitem__.return_value = mock.Mock(vendor='postgresql')
        with mock.patch('django.db.connections', connections):
            backed = views._backed_q(['lesha', ''])

        self.assertIn(('also_requested_by__contains', ['lesha']),
                      backed.children)
        self.assertNotIn(('also_requested_by__contains', ['']),
                         backed.children)


class WorkerServingSettingPageTests(TestCase):

    def setUp(self):
        worker_account('lesha', 'pw')
        self.client = Client()
        self.client.login(username='lesha', password='pw')
        WorkerPing.objects.create(machine=BOX + '#0', user='lesha')
        WorkerPing.objects.create(machine=BOX + '#1', user='lesha')
        WorkerPing.objects.create(machine='w-box-atomicdb', user='w')
        root = ingest.get_or_create_position(logic.start_fen())
        self.campaign = Campaign.objects.create(
            name='kings-gambit', root=root, state=Campaign.CState.ACTIVE)

    def test_the_page_lists_each_machine_once(self):
        body = self.client.get(PAGE).content.decode()

        self.assertEqual(body.count(f'name="mode:{BOX}"'), 1)
        self.assertNotIn('w-box-atomicdb', body)
        self.assertIn('kings-gambit', body)
        self.assertIn('Per machine', body)

    def test_no_worker_no_list(self):
        WorkerPing.objects.all().delete()

        self.assertNotIn('Per machine', self.client.get(PAGE).content.decode())

    def test_a_worker_mode_and_campaign_save(self):
        self.client.post(PAGE, {
            'ready': ['0', '1'], 'worker': BOX, f'mode:{BOX}': 'personal',
            f'campaign:{BOX}': str(self.campaign.id),
            'serve_campaign': str(self.campaign.id)})

        row = WorkerServing.objects.get(account='lesha', machine=BOX)
        self.assertEqual((row.mode, row.campaign),
                         ('personal', self.campaign.id))
        self.assertEqual(
            ContributorPref.objects.get(account='lesha').serve_campaign,
            self.campaign.id)
        self.assertEqual(preferences.worker_serving('lesha', BOX + '#1'),
                         ('personal', self.campaign.id))

    def test_back_to_the_defaults_removes_the_row(self):
        WorkerServing.objects.create(account='lesha', machine=BOX,
                                     mode='server')

        self.client.post(PAGE, {'ready': ['0', '1'], 'worker': BOX,
                                f'mode:{BOX}': '', f'campaign:{BOX}': '',
                                'serve_campaign': '0'})

        self.assertFalse(WorkerServing.objects.exists())
        self.assertIsNone(
            ContributorPref.objects.get(account='lesha').serve_campaign)

    def test_an_unknown_machine_or_mode_is_not_written(self):
        self.client.post(PAGE, {
            'ready': ['0', '1'], 'worker': ['w-box-atomicdb', BOX],
            'mode:w-box-atomicdb': 'personal', f'mode:{BOX}': 'everything'})

        self.assertFalse(WorkerServing.objects.exists())

    def test_a_form_without_the_fields_leaves_them_alone(self):
        WorkerServing.objects.create(account='lesha', machine=BOX,
                                     mode='server')
        ContributorPref.objects.create(account='lesha',
                                       serve_campaign=self.campaign.id)

        self.client.post(PAGE, {'ready': '0'})

        self.assertEqual(WorkerServing.objects.get().mode, 'server')
        self.assertEqual(
            ContributorPref.objects.get(account='lesha').serve_campaign,
            self.campaign.id)


class WorkerServingPageWordingTests(TestCase):
    """The per-machine section reads on its own (cz_eclipsia, 9-Nov-2026):
    each select says what it is and what its default resolves to, stale
    machines fold away, and odd machine names are shown tidily."""

    def setUp(self):
        worker_account('lesha', 'pw')
        self.client = Client()
        self.client.login(username='lesha', password='pw')
        WorkerPing.objects.create(machine=BOX + '#0', user='lesha')
        root = ingest.get_or_create_position(logic.start_fen())
        self.campaign = Campaign.objects.create(
            name='kings-gambit', root=root, state=Campaign.CState.ACTIVE)

    def _age(self, machine, days):
        # ``last_seen`` is auto_now, so it is moved with an update.
        WorkerPing.objects.filter(machine=machine).update(
            last_seen=timezone.now() - timedelta(days=days))

    def _older(self, body):
        return body[body.index('<details'):body.index('</details>')]

    def test_the_defaults_name_what_they_resolve_to(self):
        ContributorPref.objects.create(account='lesha', serve_order='others',
                                       serve_campaign=self.campaign.id)

        body = self.client.get(PAGE).content.decode()

        self.assertIn('Same as account (Others first)', body)
        self.assertIn('Same as account (kings-gambit)', body)

    def test_without_a_row_the_defaults_are_mine_first_and_no_campaign(self):
        body = self.client.get(PAGE).content.decode()

        self.assertIn('Same as account (Mine first)', body)
        self.assertIn('Same as account (no campaign)', body)

    def test_both_selects_are_labelled(self):
        body = self.client.get(PAGE).content.decode()

        self.assertIn('<span>Order</span>', body)
        self.assertIn('<span>Campaign</span>', body)
        self.assertNotIn('>Account setting<', body)
        self.assertNotIn('>Account campaign<', body)

    def test_each_machine_shows_when_it_was_seen(self):
        self._age(BOX + '#0', 3)

        body = self.client.get(PAGE).content.decode()

        self.assertIn('seen 3\xa0days ago', body)

    def test_a_stale_machine_goes_under_older_machines(self):
        WorkerPing.objects.create(machine='lesha-old-atomicdb#0', user='lesha')
        self._age('lesha-old-atomicdb#0', preferences.WORKER_RECENT_DAYS + 5)

        body = self.client.get(PAGE).content.decode()

        older = self._older(body)
        self.assertIn('Older machines (1)', older)
        self.assertIn('name="mode:lesha-old-atomicdb"', older)
        self.assertNotIn(f'name="mode:{BOX}"', older)
        self.assertIn(f'name="mode:{BOX}"', body)

    def test_no_stale_machine_no_older_section(self):
        self.assertNotIn('Older machines',
                         self.client.get(PAGE).content.decode())

    def test_a_configured_machine_never_seen_is_listed_under_older(self):
        WorkerServing.objects.create(account='lesha', machine='gone',
                                     mode='server')

        older = self._older(self.client.get(PAGE).content.decode())

        self.assertIn('name="mode:gone"', older)
        self.assertIn('not seen lately', older)

    def test_a_stale_machine_still_saves(self):
        WorkerPing.objects.create(machine='lesha-old-atomicdb#0', user='lesha')
        self._age('lesha-old-atomicdb#0', preferences.WORKER_RECENT_DAYS + 5)

        self.client.post(PAGE, {'ready': ['0', '1'],
                                'worker': 'lesha-old-atomicdb',
                                'mode:lesha-old-atomicdb': 'personal',
                                'campaign:lesha-old-atomicdb': ''})

        self.assertEqual(WorkerServing.objects.get().mode, 'personal')

    def test_odd_names_are_escaped_and_tidied(self):
        WorkerPing.objects.create(machine='<h1>Odd   worker</h1>',
                                  user='lesha')
        WorkerPing.objects.create(machine='  spaced \t out ', user='lesha')

        body = self.client.get(PAGE).content.decode()

        self.assertNotIn('<h1>Odd', body)
        self.assertIn('&lt;h1&gt;Odd worker&lt;/h1&gt;', body)
        self.assertIn('>spaced out<', body)

    def test_a_blank_name_still_has_a_label(self):
        self.assertEqual(preferences._worker_label('lesha', '   '),
                         '(no name)')
