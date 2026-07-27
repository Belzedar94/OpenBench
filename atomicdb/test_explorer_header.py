"""The approver's moderation link, and the request limit that is gone."""

from django.contrib.auth.models import User

from . import ingest, logic
from .models import OpeningNameSuggestion, Position, RequestLog
from .testing import TestCase


class SuggestionsBadgeTests(TestCase):

    def _user(self, name, approver):
        from OpenBench.models import Profile
        user = User.objects.create_user(name, password='pw')
        Profile.objects.create(user=user, approver=approver)
        return user

    def _pending(self, count):
        pos = ingest.get_or_create_position(logic.start_fen())
        for index in range(count):
            OpeningNameSuggestion.objects.create(
                position=pos, proposed_name=f'name {index}',
                ip='127.0.0.1',
                status=OpeningNameSuggestion.SState.PENDING)
        return pos

    def test_an_approver_sees_the_queue_and_its_size(self):
        self._user('mod', approver=True)
        pos = self._pending(2)
        self.client.login(username='mod', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('/atomicdb/suggestions/', body)
        self.assertIn('Suggestions (2)', body)

    def test_a_plain_user_sees_nothing_at_all(self):
        self._user('plain', approver=False)
        pos = self._pending(2)
        self.client.login(username='plain', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertNotIn('/atomicdb/suggestions/', body)

    def test_an_anonymous_visitor_sees_nothing_either(self):
        pos = self._pending(2)
        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()
        self.assertNotIn('/atomicdb/suggestions/', body)

    def test_only_pending_ones_are_counted(self):
        self._user('mod', approver=True)
        pos = self._pending(1)
        OpeningNameSuggestion.objects.create(
            position=pos, proposed_name='done', ip='127.0.0.1',
            status=OpeningNameSuggestion.SState.APPROVED)
        OpeningNameSuggestion.objects.create(
            position=pos, proposed_name='no', ip='127.0.0.1',
            status=OpeningNameSuggestion.SState.REJECTED)
        self.client.login(username='mod', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('Suggestions (1)', body)

    def test_an_empty_queue_shows_the_link_without_a_number(self):
        """A permanent "(0)" is noise that trains people to ignore a badge."""
        self._user('mod', approver=True)
        pos = ingest.get_or_create_position(logic.start_fen())
        self.client.login(username='mod', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('/atomicdb/suggestions/', body)
        self.assertNotIn('Suggestions (0)', body)

    def test_the_flat_cached_pages_never_carry_it(self):
        """``map`` and ``method`` cache WITHOUT varying on cookie, so an
        approver's render there would be handed to every visitor."""
        self._user('mod', approver=True)
        self._pending(2)
        self.client.login(username='mod', password='pw')

        for path in ('/atomicdb/map/', '/atomicdb/method/'):
            body = self.client.get(path).content.decode()
            self.assertNotIn('/atomicdb/suggestions/', body, path)


class NoHourlyRequestLimitTests(TestCase):

    def test_many_requests_from_one_ip_are_all_served(self):
        """The owner removed the hourly allowance: what bounds the spend is
        the per-position dedup and the queue ceiling, not a counter per IP."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        targets = list(Position.objects.filter(status='UNKNOWN')
                       .exclude(key=root.key)[:20])
        self.assertGreaterEqual(len(targets), 20)

        for position in targets:
            response = self.client.post(f'/atomicdb/request/{position.key}/')
            self.assertNotEqual(response.status_code, 429)
            self.assertNotEqual(response.json().get('status'), 'rate-limited')

    def test_the_same_position_is_still_deduplicated(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        first = self.client.post(f'/atomicdb/request/{pos.key}/')
        second = self.client.post(f'/atomicdb/request/{pos.key}/')
        self.assertEqual(first.json()['status'], 'queued')
        self.assertEqual(second.json()['status'], 'already-requested')

    def test_a_pile_of_receipts_no_longer_blocks_anything(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        other = Position.objects.filter(status='UNKNOWN').exclude(
            key=root.key).first()
        for _ in range(60):
            RequestLog.objects.create(ip='127.0.0.1', position=root)

        response = self.client.post(f'/atomicdb/request/{other.key}/')

        self.assertEqual(response.status_code, 200)
