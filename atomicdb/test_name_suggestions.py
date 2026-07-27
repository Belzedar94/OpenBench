"""Nombres de apertura propuestos por la comunidad: envio publico,
moderacion por approvers y publicacion encima del catalogo auditado.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from OpenBench.models import Profile

from . import community_names, ingest, logic, openings
from .models import Edge, OpeningNameSuggestion, Position
from .testing import TestCase


UNNAMED_ROUTE = ('a2a3', 'a7a6', 'a1a2')


def _unnamed_position():
    """Una posicion real, con su linea materializada, que el catalogo
    auditado NO nombra."""
    parent = ingest.get_or_create_position(logic.start_fen())
    for uci in UNNAMED_ROUTE:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        parent = child
    assert openings.lookup_key(parent.key) is None
    return parent


def _named_position():
    """Una posicion que el catalogo auditado SI nombra."""
    fen = logic.apply_move(logic.start_fen(), 'g1f3')
    pos = ingest.get_or_create_position(fen)
    assert openings.lookup_key(pos.key) is not None
    return pos


class SuggestionFormTests(TestCase):

    def setUp(self):
        self.pos = _unnamed_position()
        self.client = Client()

    def _post(self, **data):
        payload = {'name': 'Belfast Gambit', 'comment': 'seen on Discord'}
        payload.update(data)
        return self.client.post(f'/atomicdb/suggest/{self.pos.key}/', payload)

    def test_the_explore_page_offers_the_form(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode()

        self.assertIn('Suggest a name for this position', body)
        self.assertIn(f'/atomicdb/suggest/{self.pos.key}/', body)
        self.assertIn('csrfmiddlewaretoken', body)

    def test_a_catalogued_position_is_not_up_for_renaming(self):
        named = _named_position()

        body = self.client.get(
            f'/atomicdb/explore/{named.key}/').content.decode()

        self.assertNotIn('Suggest a name for this position', body)
        response = self.client.post(f'/atomicdb/suggest/{named.key}/',
                                    {'name': 'Something Else'})
        self.assertIn('suggested=already-named', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 0)

    def test_a_public_visitor_can_suggest_without_an_account(self):
        response = self._post()

        self.assertIn('suggested=ok', response['Location'])
        row = OpeningNameSuggestion.objects.get()
        self.assertEqual(row.proposed_name, 'Belfast Gambit')
        self.assertEqual(row.comment, 'seen on Discord')
        self.assertEqual(row.status, 'PENDING')
        self.assertEqual(row.position_id, self.pos.key)

    def test_the_route_survives_the_round_trip(self):
        route = ','.join(UNNAMED_ROUTE)
        response = self.client.post(
            f'/atomicdb/suggest/{self.pos.key}/',
            {'name': 'Belfast Gambit', 'play': route})

        self.assertEqual(
            response['Location'],
            f'/atomicdb/explore/{self.pos.key}/?play={route}&suggested=ok')

    def test_a_forged_route_is_dropped_instead_of_followed(self):
        response = self.client.post(
            f'/atomicdb/suggest/{self.pos.key}/',
            {'name': 'Belfast Gambit', 'play': 'e2e4,e7e5'})

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{self.pos.key}/?suggested=ok')

    def test_names_are_validated(self):
        for bad in ('', 'x', 'y' * 61, '<script>alert(1)</script>',
                    '   ', '"quoted"'):
            with self.subTest(name=bad):
                response = self._post(name=bad)
                self.assertIn('suggested=invalid', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 0)

    def test_an_overlong_comment_is_rejected(self):
        response = self._post(comment='c' * 281)

        self.assertIn('suggested=invalid', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 0)

    def test_accented_names_are_accepted(self):
        response = self._post(name='Defensa Atómica de Belzedar')

        self.assertIn('suggested=ok', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.get().proposed_name,
                         'Defensa Atómica de Belzedar')

    def test_the_same_ip_cannot_pile_up_on_one_position(self):
        self._post()

        response = self._post(name='Another Try')

        self.assertIn('suggested=duplicate', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 1)

    def test_a_resolved_suggestion_reopens_the_position_for_that_ip(self):
        self._post()
        OpeningNameSuggestion.objects.update(
            status=OpeningNameSuggestion.SState.REJECTED,
            resolved_at=timezone.now(), resolved_by='belzedar')

        response = self._post(name='Second Attempt')

        self.assertIn('suggested=ok', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 2)

    def test_a_daily_cap_bounds_one_ip(self):
        for index in range(10):
            other = ingest.get_or_create_position(
                logic.apply_move(self.pos.fen,
                                 logic.legal_moves(self.pos.fen)[index]))
            self.client.post(f'/atomicdb/suggest/{other.key}/',
                             {'name': f'Name Number {index}'})
        self.assertEqual(OpeningNameSuggestion.objects.count(), 10)

        response = self._post(name='One Too Many')

        self.assertIn('suggested=rate-limited', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 10)

    def test_yesterdays_suggestions_do_not_count(self):
        for index in range(10):
            OpeningNameSuggestion.objects.create(
                position=self.pos, proposed_name=f'Old Name {index}',
                ip='127.0.0.1',
                status=OpeningNameSuggestion.SState.REJECTED)
        OpeningNameSuggestion.objects.update(
            created=timezone.now() - timedelta(days=2))

        response = self._post()

        self.assertIn('suggested=ok', response['Location'])

    def test_a_get_just_goes_back_to_the_position(self):
        response = self.client.get(f'/atomicdb/suggest/{self.pos.key}/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(OpeningNameSuggestion.objects.count(), 0)

    def test_an_unknown_position_is_a_404(self):
        response = self.client.post(f'/atomicdb/suggest/{"f" * 64}/',
                                    {'name': 'Ghost Opening'})

        self.assertEqual(response.status_code, 404)


class ModerationGateTests(TestCase):

    def setUp(self):
        self.pos = _unnamed_position()
        self.suggestion = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='Belfast Gambit', ip='10.0.0.1')

    def _user(self, username, approver):
        user = User.objects.create_user(username=username, password='pw')
        Profile.objects.create(user=user, enabled=True, approver=approver)
        client = Client()
        client.login(username=username, password='pw')
        return client

    def test_anonymous_visitors_are_sent_to_the_login(self):
        response = Client().get('/atomicdb/suggestions/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/')

    def test_a_plain_user_is_sent_away(self):
        client = self._user('plain', approver=False)

        response = client.get('/atomicdb/suggestions/')

        self.assertEqual(response['Location'], '/index/')

    def test_a_plain_user_cannot_approve_by_posting(self):
        client = self._user('plain2', approver=False)

        client.post('/atomicdb/suggestions/',
                    {'suggestion': self.suggestion.id, 'action': 'approve'})

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'PENDING')

    def test_an_approver_sees_the_queue(self):
        client = self._user('boss', approver=True)

        body = client.get('/atomicdb/suggestions/').content.decode()

        self.assertIn('Belfast Gambit', body)
        self.assertIn(f'/atomicdb/explore/{self.pos.key}/', body)
        self.assertIn('Approve', body)
        self.assertIn('Reject', body)


class ModerationFlowTests(TestCase):

    def setUp(self):
        self.pos = _unnamed_position()
        self.suggestion = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='Belfast Gambit', ip='10.0.0.1')
        user = User.objects.create_user(username='boss', password='pw')
        Profile.objects.create(user=user, enabled=True, approver=True)
        self.client = Client()
        self.client.login(username='boss', password='pw')
        community_names.invalidate()

    def tearDown(self):
        community_names.invalidate()

    def _decide(self, action, suggestion=None):
        return self.client.post('/atomicdb/suggestions/', {
            'suggestion': (self.suggestion if suggestion is None
                           else suggestion).id,
            'action': action})

    def test_approving_publishes_the_name_on_the_position(self):
        self._decide('approve')

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'APPROVED')
        self.assertEqual(self.suggestion.resolved_by, 'boss')
        self.assertIsNotNone(self.suggestion.resolved_at)
        body = Client().get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode()
        self.assertIn('Belfast Gambit', body)
        self.assertIn('Community name', body)
        self.assertIn('approved by boss', body)

    def test_an_approved_name_labels_the_parent_edge(self):
        self._decide('approve')
        parent_key = Edge.objects.get(child=self.pos).parent_id

        body = Client().get(
            f'/atomicdb/explore/{parent_key}/').content.decode()

        self.assertIn('enters Belfast Gambit', body)

    def test_rejecting_publishes_nothing(self):
        self._decide('reject')

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'REJECTED')
        body = Client().get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode()
        self.assertNotIn('Community name', body)

    def test_a_decision_is_taken_once(self):
        self._decide('approve')

        self._decide('reject')

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'APPROVED')

    def test_the_audited_catalogue_keeps_precedence(self):
        named = _named_position()
        sneaky = OpeningNameSuggestion.objects.create(
            position=named, proposed_name='Not King Knight', ip='10.0.0.2')

        self._decide('approve', suggestion=sneaky)

        sneaky.refresh_from_db()
        self.assertEqual(sneaky.status, 'REJECTED')
        body = Client().get(f'/atomicdb/explore/{named.key}/').content.decode()
        self.assertNotIn('Not King Knight', body)
        self.assertIn('King Knight', body)

    def test_a_name_with_markup_is_escaped_not_rendered(self):
        # El endpoint publico ya rechaza el markup; esto cubre la fila que
        # entrase por cualquier otra via (import, admin, shell).
        raw = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='<b>bold</b>', ip='10.0.0.3')

        body = self.client.get('/atomicdb/suggestions/').content.decode()

        self.assertIn('&lt;b&gt;bold&lt;/b&gt;', body)
        self.assertNotIn('<b>bold</b>', body)
        del raw

    def test_the_resolved_history_is_shown(self):
        self._decide('approve')

        body = self.client.get('/atomicdb/suggestions/').content.decode()

        self.assertIn('Recently resolved', body)
        self.assertIn('approved', body)
        self.assertIn('Nothing waiting for review.', body)


class CommunityOverlayTests(TestCase):

    def setUp(self):
        community_names.invalidate()

    def tearDown(self):
        community_names.invalidate()

    def test_the_overlay_is_empty_without_approvals(self):
        pos = _unnamed_position()
        OpeningNameSuggestion.objects.create(
            position=pos, proposed_name='Pending Name', ip='10.0.0.1')

        self.assertEqual(community_names.approved_map(), {})
        self.assertIsNone(community_names.opening_for(pos.key, pos.fen))

    def test_the_overlay_never_shadows_the_audited_catalogue(self):
        named = _named_position()
        OpeningNameSuggestion.objects.create(
            position=named, proposed_name='Impostor', ip='10.0.0.1',
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

        self.assertIsNone(community_names.opening_for(named.key, named.fen))

    def test_the_latest_approval_wins_for_one_position(self):
        pos = _unnamed_position()
        now = timezone.now()
        for index, name in enumerate(('First Name', 'Second Name')):
            OpeningNameSuggestion.objects.create(
                position=pos, proposed_name=name, ip=f'10.0.0.{index}',
                status=OpeningNameSuggestion.SState.APPROVED,
                resolved_by='boss',
                resolved_at=now + timedelta(minutes=index))
        community_names.invalidate()

        self.assertEqual(community_names.name_for(pos.key), 'Second Name')

    def test_the_overlay_fails_open(self):
        # Si la capa comunitaria no se puede leer, el explorador sigue vivo.
        pos = _unnamed_position()
        original = OpeningNameSuggestion.objects.filter

        def boom(*args, **kwargs):
            raise RuntimeError('no database')

        OpeningNameSuggestion.objects.filter = boom
        try:
            self.assertEqual(community_names.approved_map(), {})
            response = Client().get(f'/atomicdb/explore/{pos.key}/')
        finally:
            OpeningNameSuggestion.objects.filter = original
        self.assertEqual(response.status_code, 200)
