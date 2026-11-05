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
                                       serve_order='others')
        stranger, own = self._stranger_then_own()

        first = self._lease('m3')['tasks'][0]
        second = self._lease('m4')['tasks'][0]

        self.assertEqual(first['id'], stranger.id)
        self.assertEqual(second['id'], own.id)

    def test_on_an_older_own_request_still_waits_for_the_stranger(self):
        # Skipped while anyone else's is waiting, however early it came.
        ContributorPref.objects.create(account='lesha',
                                       serve_order='others')
        self._request(self.first, 'lesha')
        stranger = self._request(self.second, 'w')

        leased = self._lease('m5')['tasks'][0]

        self.assertEqual(leased['id'], stranger.id)

    def test_on_with_only_own_requests_waiting_own_is_served(self):
        ContributorPref.objects.create(account='lesha',
                                       serve_order='others')
        own = self._request(self.second, 'lesha')

        leased = self._lease('m6')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_on_own_requests_still_beat_the_selector_bands(self):
        ContributorPref.objects.create(account='lesha',
                                       serve_order='others')
        AnalysisTask.objects.create(
            position=self.first, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)
        own = self._request(self.second, 'lesha')

        leased = self._lease('m7')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_queue_order_serves_the_older_request_whoever_asked(self):
        ContributorPref.objects.create(account='lesha', serve_order='queue')
        stranger, own = self._stranger_then_own()

        leased = self._lease('mq')['tasks'][0]

        self.assertEqual(leased['id'], stranger.id)

    def test_queue_order_serves_an_older_own_request_first(self):
        ContributorPref.objects.create(account='lesha', serve_order='queue')
        own = self._request(self.second, 'lesha')
        self._request(self.first, 'w')

        leased = self._lease('mq2')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_another_accounts_setting_changes_nothing_here(self):
        ContributorPref.objects.create(account='w', serve_order='others')
        _stranger, own = self._stranger_then_own()

        leased = self._lease('m8')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_the_reader_is_off_without_a_row_or_a_name(self):
        self.assertFalse(preferences.serves_others_first('lesha'))
        self.assertFalse(preferences.serves_others_first(''))
        ContributorPref.objects.create(account='lesha',
                                       serve_order='others')
        self.assertTrue(preferences.serves_others_first('lesha'))


def _checked(body):
    """The value of the serve-order radio that is checked, or None.

    Found by name and value, whatever order the minifier leaves the
    attributes in.
    """
    for tag in re.findall(r'<input[^>]*name="serve_order"[^>]*>', body):
        if re.search(r'\bchecked\b', tag):
            return re.search(r'value="([^"]*)"', tag).group(1)
    return None


class ServeOthersFirstSettingPageTests(TestCase):

    def setUp(self):
        worker_account('lesha', 'pw')
        self.client = Client()
        self.client.login(username='lesha', password='pw')

    def test_three_choices_and_mine_first_is_the_default(self):
        body = self.client.get(PAGE).content.decode()

        for label in ('Mine first', 'Queue order', 'Others first'):
            self.assertIn(label, body)
        self.assertEqual(_checked(body), 'own')

    def test_each_choice_saves_and_shows(self):
        for order in ('others', 'queue', 'own'):
            with self.subTest(order=order):
                response = self.client.post(PAGE, {'ready': ['0', '1'],
                                                   'serve_order': order})
                self.assertEqual(response.status_code, 302)
                pref = ContributorPref.objects.get(account='lesha')
                self.assertEqual(pref.serve_order, order)
                # The old field stays in step for code that only knows it.
                self.assertEqual(pref.serve_others_first, order == 'others')
                self.assertTrue(pref.notify_ready)
                body = self.client.get(f'{PAGE}?saved=1').content.decode()
                self.assertEqual(_checked(body), order)

    def test_the_old_checkbox_still_saves(self):
        """A page loaded before the three choices posts ``serve``."""
        self.client.post(PAGE, {'ready': ['0', '1'], 'serve': ['0', '1']})
        self.assertEqual(
            ContributorPref.objects.get(account='lesha').serve_order, 'others')
        self.client.post(PAGE, {'ready': ['0', '1'], 'serve': '0'})
        self.assertEqual(
            ContributorPref.objects.get(account='lesha').serve_order, 'own')

    def test_a_form_without_the_field_leaves_it_alone(self):
        ContributorPref.objects.create(account='lesha', serve_order='queue')

        self.client.post(PAGE, {'ready': '0'})

        pref = ContributorPref.objects.get(account='lesha')
        self.assertEqual(pref.serve_order, 'queue')
        self.assertFalse(pref.notify_ready)

    def test_a_value_that_is_not_a_choice_changes_nothing(self):
        ContributorPref.objects.create(account='lesha', serve_order='others')

        self.client.post(PAGE, {'ready': '1', 'serve_order': 'mine!'})

        self.assertEqual(
            ContributorPref.objects.get(account='lesha').serve_order, 'others')

    def test_the_cog_links_to_the_page(self):
        body = self.client.get('/atomicdb/notifications/').content.decode()

        self.assertIn(f'href="{PAGE}"', body)


class ServeForListTests(TestCase):
    """seaside_cliff, 4-Nov-2026: with Others first, name the accounts to
    serve before anyone; empty serves everyone alike."""

    def setUp(self):
        worker_account('w', 'p')
        worker_account('lesha', 'p')
        worker_account('ally', 'p')
        self.client = Client()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        edges = list(Edge.objects.filter(parent=root).order_by('move_uci')[:3])
        self.a, self.b, self.c = (e.child for e in edges)

    def _request(self, position, requested_by):
        return AnalysisTask.objects.create(
            position=position, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by=requested_by)

    def _lease(self, machine):
        return self.client.post('/atomicdb/api/lease', {
            'username': 'lesha', 'password': 'p', 'machine': machine,
            'worker_build': '2026072203', 'lease_session': machine,
        }).json()['tasks'][0]['id']

    def test_a_listed_account_is_served_before_anyone_else(self):
        ContributorPref.objects.create(account='lesha', serve_order='others',
                                       serve_for='ALLY')
        self._request(self.a, 'w')
        self._request(self.b, 'lesha')
        ally = self._request(self.c, 'ally')

        self.assertEqual(self._lease('m1'), ally.id)

    def test_the_list_means_nothing_without_others_first(self):
        ContributorPref.objects.create(account='lesha', serve_order='own',
                                       serve_for='ally')
        self._request(self.a, 'w')
        own = self._request(self.b, 'lesha')
        self._request(self.c, 'ally')

        self.assertEqual(self._lease('m2'), own.id)

    def test_the_page_saves_a_clean_list_and_newest_first(self):
        worker_account('pagey', 'pw')
        client = Client()
        client.login(username='pagey', password='pw')
        client.post(PAGE, {'ready': '1', 'serve_order': 'others',
                           'serve_for': ' ally,  Ally , bob ', 'lifo': ['0', '1']})
        pref = ContributorPref.objects.get(account='pagey')
        self.assertEqual(pref.serve_for, 'ally, bob')
        self.assertTrue(pref.lifo_queue)
        self.assertEqual(preferences.serve_for('pagey'), ['ally', 'bob'])
