"""'Serve others first': an account's workers can give up own-first.

By default a worker serves its own account's requests before anybody else's
(§ ``views.choose_pending``, ``own_first``).  With the setting on, that
account's workers serve everybody else's requests before their own; with
nobody else waiting, its own requests are still served.
"""

import re

from django.test import Client

from . import ingest, logic, preferences
from .models import AnalysisTask, ContributorPref, Edge, Position
from .testing import TestCase, worker_account

PAGE = '/atomicdb/notifications/settings/'


class ServeOthersFirstLeaseTests(TestCase):

    def setUp(self):
        worker_account('w', 'p')
        worker_account('lesha', 'p')
        self.client = Client()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        edges = list(Edge.objects.filter(parent=root).order_by('move_uci')[:2])
        self.first, self.second = edges[0].child, edges[1].child
        Position.objects.filter(key=self.first.key).update(priority=-73.0)
        Position.objects.filter(key=self.second.key).update(priority=8.0)

    def _lease(self, machine, username='lesha'):
        return self.client.post('/atomicdb/api/lease', {
            'username': username, 'password': 'p', 'machine': machine,
            'worker_build': '2026072203', 'lease_session': machine,
        }).json()

    def _request(self, position, requested_by):
        return AnalysisTask.objects.create(
            position=position, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by=requested_by)

    def _stranger_then_own(self):
        stranger = self._request(self.first, 'w')
        own = self._request(self.second, 'lesha')
        return stranger, own

    def test_off_the_workers_own_request_still_jumps_the_queue(self):
        _stranger, own = self._stranger_then_own()

        leased = self._lease('m1')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_a_row_with_the_setting_off_keeps_own_first(self):
        ContributorPref.objects.create(account='lesha', lifo_queue=True)
        _stranger, own = self._stranger_then_own()

        leased = self._lease('m2')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_on_the_waiting_stranger_is_served_first(self):
        ContributorPref.objects.create(account='lesha',
                                       serve_others_first=True)
        stranger, own = self._stranger_then_own()

        first = self._lease('m3')['tasks'][0]
        second = self._lease('m4')['tasks'][0]

        self.assertEqual(first['id'], stranger.id)
        self.assertEqual(second['id'], own.id)

    def test_on_an_older_own_request_still_waits_for_the_stranger(self):
        # Skipped while anyone else's is waiting, however early it came.
        ContributorPref.objects.create(account='lesha',
                                       serve_others_first=True)
        self._request(self.first, 'lesha')
        stranger = self._request(self.second, 'w')

        leased = self._lease('m5')['tasks'][0]

        self.assertEqual(leased['id'], stranger.id)

    def test_on_with_only_own_requests_waiting_own_is_served(self):
        ContributorPref.objects.create(account='lesha',
                                       serve_others_first=True)
        own = self._request(self.second, 'lesha')

        leased = self._lease('m6')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_on_own_requests_still_beat_the_selector_bands(self):
        ContributorPref.objects.create(account='lesha',
                                       serve_others_first=True)
        AnalysisTask.objects.create(
            position=self.first, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)
        own = self._request(self.second, 'lesha')

        leased = self._lease('m7')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_another_accounts_setting_changes_nothing_here(self):
        ContributorPref.objects.create(account='w', serve_others_first=True)
        _stranger, own = self._stranger_then_own()

        leased = self._lease('m8')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_the_reader_is_off_without_a_row_or_a_name(self):
        self.assertFalse(preferences.serves_others_first('lesha'))
        self.assertFalse(preferences.serves_others_first(''))
        ContributorPref.objects.create(account='lesha',
                                       serve_others_first=True)
        self.assertTrue(preferences.serves_others_first('lesha'))


def _box(body):
    # The minifier reorders attributes, so the tag is found by its id.
    return re.search(r'<input[^>]*id="serve-others-first"[^>]*>',
                     body).group(0)


class ServeOthersFirstSettingPageTests(TestCase):

    def setUp(self):
        worker_account('lesha', 'pw')
        self.client = Client()
        self.client.login(username='lesha', password='pw')

    def test_the_box_starts_unchecked(self):
        body = self.client.get(PAGE).content.decode()

        self.assertIn("Let my workers serve other people's requests first",
                      body.replace('&#x27;', "'").replace('&#39;', "'"))
        self.assertNotIn('checked', _box(body))

    def test_checking_and_saving_turns_it_on_and_shows_it(self):
        response = self.client.post(PAGE, {'ready': ['0', '1'],
                                           'serve': ['0', '1']})

        self.assertEqual(response.status_code, 302)
        pref = ContributorPref.objects.get(account='lesha')
        self.assertTrue(pref.serve_others_first)
        self.assertTrue(pref.notify_ready)
        body = self.client.get(f'{PAGE}?saved=1').content.decode()
        self.assertIn('checked', _box(body))

    def test_unchecking_turns_it_back_off(self):
        ContributorPref.objects.create(account='lesha',
                                       serve_others_first=True,
                                       lifo_queue=True)

        self.client.post(PAGE, {'ready': ['0', '1'], 'serve': '0'})

        pref = ContributorPref.objects.get(account='lesha')
        self.assertFalse(pref.serve_others_first)
        self.assertTrue(pref.lifo_queue)

    def test_a_form_without_the_field_leaves_it_alone(self):
        ContributorPref.objects.create(account='lesha',
                                       serve_others_first=True)

        self.client.post(PAGE, {'ready': '0'})

        pref = ContributorPref.objects.get(account='lesha')
        self.assertTrue(pref.serve_others_first)
        self.assertFalse(pref.notify_ready)

    def test_the_cog_links_to_the_page(self):
        body = self.client.get('/atomicdb/notifications/').content.decode()

        self.assertIn(f'href="{PAGE}"', body)
