"""The notification settings page and the setting it stores."""

import re

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from . import ingest, ingest_queue, logic, preferences
from .models import AnalysisTask, ContributorPref, Edge, RequestNotification
from .testing import TestCase

PAGE = '/atomicdb/notifications/settings/'


def _box(body):
    # The minifier reorders attributes, so the tag is found by its id.
    return re.search(r'<input[^>]*id="notify-ready"[^>]*>', body).group(0)


def _child(uci='e2e4'):
    root = ingest.get_or_create_position(logic.start_fen())
    child = ingest.get_or_create_position(logic.apply_move(root.fen, uci))
    Edge.objects.get_or_create(parent=root, move_uci=uci,
                               defaults={'child': child})
    return child


def _serve(position, requested_by, also=()):
    task = AnalysisTask.objects.create(
        position=position, generation=0,
        budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
        source=AnalysisTask.Source.USER, requested_by=requested_by,
        also_requested_by=list(also))
    lines = [{'move': uci, 'eval_cp': -40, 'pv': [uci]}
             for uci in logic.legal_moves(position.fen)[:3]]
    job, _created = ingest_queue.enqueue(task, {
        'lines': lines, 'nodes': 1_000, 'elapsed': 1.0,
        'machine': 'm1', 'username': 'w'})
    AnalysisTask.objects.filter(pk=task.pk).update(
        state=AnalysisTask.TState.COMPLETED, completed=timezone.now())
    ingest_queue.process_job(job)
    return task


class ReadyNoticeSettingTests(TestCase):

    def setUp(self):
        User.objects.create_user(username='lesha', password='pw')
        User.objects.create_user(username='otra', password='pw')
        self.position = _child()

    def test_the_notice_is_on_without_a_row(self):
        _serve(self.position, 'lesha')

        self.assertEqual(RequestNotification.objects.get().username, 'lesha')
        self.assertFalse(ContributorPref.objects.exists())

    def test_turned_off_no_notification_is_created(self):
        ContributorPref.objects.create(account='lesha', notify_ready=False)

        _serve(self.position, 'lesha')

        self.assertEqual(RequestNotification.objects.count(), 0)

    def test_the_other_requesters_of_the_task_still_get_theirs(self):
        ContributorPref.objects.create(account='lesha', notify_ready=False)

        _serve(self.position, 'lesha', also=['otra'])

        self.assertEqual(
            list(RequestNotification.objects.values_list(
                'username', flat=True)), ['otra'])

    def test_the_queue_option_does_not_mute_the_notice(self):
        ContributorPref.objects.create(account='lesha', lifo_queue=True)

        self.assertEqual(preferences.ready_notice_muted(['lesha']), set())


class NotificationSettingsPageTests(TestCase):

    def setUp(self):
        User.objects.create_user(username='lesha', password='pw')
        self.client = Client()
        self.client.login(username='lesha', password='pw')

    def test_the_page_requires_a_session(self):
        response = Client().get(PAGE)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/?next=', response['Location'])
        self.assertIn('notifications%2Fsettings', response['Location'])

    def test_a_post_without_a_session_changes_nothing(self):
        response = Client().post(PAGE, {'ready': '0'})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContributorPref.objects.exists())

    def test_the_box_starts_checked(self):
        body = self.client.get(PAGE).content.decode()

        self.assertIn('Notify me when my analyses are ready', body)
        self.assertIn('checked', _box(body))

    def test_unchecking_and_saving_turns_it_off(self):
        response = self.client.post(PAGE, {'ready': '0'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], f'{PAGE}?saved=1')
        self.assertFalse(ContributorPref.objects.get(
            account='lesha').notify_ready)
        body = self.client.get(f'{PAGE}?saved=1').content.decode()
        self.assertNotIn('checked', _box(body))
        self.assertIn('Saved.', body)

    def test_checking_it_again_turns_it_back_on(self):
        ContributorPref.objects.create(account='lesha', notify_ready=False,
                                       lifo_queue=True)

        self.client.post(PAGE, {'ready': ['0', '1']})

        pref = ContributorPref.objects.get(account='lesha')
        self.assertTrue(pref.notify_ready)
        self.assertTrue(pref.lifo_queue)

    def test_the_notices_page_links_here(self):
        body = self.client.get('/atomicdb/notifications/').content.decode()

        self.assertIn(f'href="{PAGE}"', body)
