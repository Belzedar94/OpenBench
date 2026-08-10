"""Nombres de apertura propuestos por la comunidad: envio publico,
moderacion por approvers y publicacion encima del catalogo auditado.

Desde el 28-jul una propuesta puede ser tambien una CORRECCION de un nombre ya
existente, por la misma caja y con la misma aprobacion.  Lo que fija este
fichero es donde estan los limites de eso: que se congela al proponer, que ve
el moderador, y que puede y que no puede desplazar al catalogo auditado.
"""

import html as html_module
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from OpenBench.models import Profile

from . import community_names, ingest, logic, openings
from .models import Edge, OpeningNameSuggestion, Position
from .testing import TestCase


UNNAMED_ROUTE = ('a2a3', 'a7a6', 'a1a2')
# El nombre que el catalogo AUDITADO le da a 1.Nf3, y la correccion que el
# propietario uso de ejemplo al pedir la funcion.
CATALOGUED_NAME = 'King Knight Opening'
CORRECTED_NAME = "King's Knight Attack"


def _reading(body):
    """Lo que un moderador LEE: sin etiquetas y con los espacios colapsados.

    Una asercion sobre el HTML crudo se rompe cada vez que alguien mete un
    <strong> por el medio; lo que tiene que quedar fijado es la frase.
    """
    return ' '.join(html_module.unescape(re.sub(r'<[^>]+>', ' ', body)).split())


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
    """Una posicion que el catalogo auditado SI nombra, con su arista."""
    root = ingest.get_or_create_position(logic.start_fen())
    pos = ingest.get_or_create_position(logic.apply_move(root.fen, 'g1f3'))
    Edge.objects.get_or_create(parent=root, move_uci='g1f3',
                               defaults={'child': pos})
    assert openings.lookup_key(pos.key)['name'] == CATALOGUED_NAME
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

    def test_a_name_where_there_was_none_is_filed_as_new(self):
        self._post()

        row = OpeningNameSuggestion.objects.get()
        self.assertEqual(row.kind, 'NEW')
        self.assertEqual(row.previous_name, '')

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


class EditSuggestionFormTests(TestCase):
    """La misma caja, sobre una posicion que YA tiene nombre."""

    def setUp(self):
        self.pos = _named_position()
        self.client = Client()
        community_names.invalidate()

    def tearDown(self):
        community_names.invalidate()

    def _post(self, **data):
        payload = {'name': CORRECTED_NAME}
        payload.update(data)
        return self.client.post(f'/atomicdb/suggest/{self.pos.key}/', payload)

    def test_the_box_asks_for_a_correction_not_for_a_new_name(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode()

        self.assertIn('Suggest a correction to this name', body)
        self.assertNotIn('Suggest a name for this position', body)
        self.assertIn(f'Current name: {CATALOGUED_NAME}', _reading(body))
        self.assertIn(f'/atomicdb/suggest/{self.pos.key}/', body)

    def test_a_correction_freezes_the_name_it_was_written_against(self):
        response = self._post()

        self.assertIn('suggested=ok', response['Location'])
        row = OpeningNameSuggestion.objects.get()
        self.assertEqual(row.kind, 'EDIT')
        self.assertEqual(row.previous_name, CATALOGUED_NAME)
        self.assertEqual(row.proposed_name, CORRECTED_NAME)
        self.assertEqual(row.status, 'PENDING')

    def test_the_frozen_name_is_read_here_not_taken_from_the_post(self):
        """Si el "actual" viniera del cliente, la cola de moderacion pintaria
        un antes que cualquiera podria escribir."""
        self._post(previous_name='Whatever I Type', kind='NEW')

        row = OpeningNameSuggestion.objects.get()
        self.assertEqual(row.previous_name, CATALOGUED_NAME)
        self.assertEqual(row.kind, 'EDIT')

    def test_proposing_the_name_it_already_has_is_not_a_correction(self):
        response = self._post(name=CATALOGUED_NAME)

        self.assertIn('suggested=unchanged', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 0)

    def test_a_community_name_is_editable_the_same_way(self):
        unnamed = _unnamed_position()
        OpeningNameSuggestion.objects.create(
            position=unnamed, proposed_name='Belfast Gambit', ip='10.0.0.9',
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

        self.client.post(f'/atomicdb/suggest/{unnamed.key}/',
                         {'name': 'Belfast Countergambit'})

        row = OpeningNameSuggestion.objects.get(status='PENDING')
        self.assertEqual(row.kind, 'EDIT')
        self.assertEqual(row.previous_name, 'Belfast Gambit')

    def test_the_same_validation_applies_to_a_correction(self):
        for bad in ('', 'x', 'y' * 61, '<script>alert(1)</script>', '   '):
            with self.subTest(name=bad):
                response = self._post(name=bad)
                self.assertIn('suggested=invalid', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 0)

    def test_one_pending_correction_per_ip_and_position(self):
        self._post()

        response = self._post(name='Yet Another Knight Attack')

        self.assertIn('suggested=duplicate', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 1)

    def test_the_daily_cap_counts_corrections_too(self):
        """El tope diario es anti-spam de texto que el propietario aprueba a
        mano; una edicion es exactamente el mismo texto."""
        for index in range(10):
            other = ingest.get_or_create_position(
                logic.apply_move(self.pos.fen,
                                 logic.legal_moves(self.pos.fen)[index]))
            self.client.post(f'/atomicdb/suggest/{other.key}/',
                             {'name': f'Name Number {index}'})
        self.assertEqual(OpeningNameSuggestion.objects.count(), 10)

        response = self._post()

        self.assertIn('suggested=rate-limited', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 10)

    def test_a_correction_counts_against_the_cap_for_a_later_new_name(self):
        for index in range(10):
            self.client.post(f'/atomicdb/suggest/{self.pos.key}/',
                             {'name': f'Knight Attack {index}'})
            OpeningNameSuggestion.objects.update(
                status=OpeningNameSuggestion.SState.REJECTED)
        self.assertEqual(
            OpeningNameSuggestion.objects.filter(kind='EDIT').count(), 10)

        fresh = _unnamed_position()
        response = self.client.post(f'/atomicdb/suggest/{fresh.key}/',
                                    {'name': 'Belfast Gambit'})

        self.assertIn('suggested=rate-limited', response['Location'])
        self.assertEqual(OpeningNameSuggestion.objects.count(), 10)


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


class EditModerationTests(TestCase):
    """Como se lee una edicion en la cola y que pasa al aprobarla."""

    def setUp(self):
        self.pos = _named_position()
        self.suggestion = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name=CORRECTED_NAME,
            kind=OpeningNameSuggestion.SKind.EDIT,
            previous_name=CATALOGUED_NAME, ip='10.0.0.1')
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

    def test_the_queue_reads_current_then_proposed(self):
        body = self.client.get('/atomicdb/suggestions/').content.decode()

        self.assertIn(f"EDIT: '{CATALOGUED_NAME}' → '{CORRECTED_NAME}'",
                      _reading(body))

    def test_a_new_name_still_reads_as_new(self):
        OpeningNameSuggestion.objects.create(
            position=_unnamed_position(), proposed_name='Belfast Gambit',
            ip='10.0.0.2')

        body = self.client.get('/atomicdb/suggestions/').content.decode()

        self.assertIn('NEW: Belfast Gambit', _reading(body))

    def test_a_name_that_moved_while_waiting_is_flagged(self):
        """El "actual" de la fila es el de cuando se propuso.  Aprobar esto
        sustituye lo que hay HOY, asi que el moderador tiene que ver que se
        movio."""
        other = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='Interim Name', ip='10.0.0.3',
            kind=OpeningNameSuggestion.SKind.EDIT,
            previous_name=CATALOGUED_NAME)
        self._decide('approve', suggestion=other)

        reading = _reading(
            self.client.get('/atomicdb/suggestions/').content.decode())

        self.assertIn(f"EDIT: '{CATALOGUED_NAME}' → '{CORRECTED_NAME}'",
                      reading)
        self.assertIn("now reads 'Interim Name'", reading)

    def test_a_settled_edit_is_not_flagged(self):
        reading = _reading(
            self.client.get('/atomicdb/suggestions/').content.decode())

        self.assertNotIn('now reads', reading)

    def test_approving_an_edit_publishes_it_over_the_audited_name(self):
        self._decide('approve')

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'APPROVED')
        self.assertEqual(self.suggestion.resolved_by, 'boss')
        reading = _reading(Client().get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode())
        self.assertIn(CORRECTED_NAME, reading)
        self.assertIn('Community correction', reading)

    def test_the_page_still_says_what_the_catalogue_reads(self):
        self._decide('approve')

        reading = _reading(Client().get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode())

        self.assertIn(f'Previously “{CATALOGUED_NAME}”',
                      reading)

    def test_the_static_catalogue_itself_is_untouched(self):
        digest = openings.catalog_sha256()

        self._decide('approve')

        openings.clear_catalog_cache()
        self.assertEqual(openings.catalog_sha256(), digest)
        self.assertEqual(openings.lookup_key(self.pos.key)['name'],
                         CATALOGUED_NAME)

    def test_an_approved_edit_relabels_the_parent_edge(self):
        self._decide('approve')
        parent_key = Edge.objects.get(child=self.pos).parent_id

        reading = _reading(Client().get(
            f'/atomicdb/explore/{parent_key}/').content.decode())

        self.assertIn(f'enters {CORRECTED_NAME}', reading)

    def test_rejecting_an_edit_leaves_the_audited_name_alone(self):
        self._decide('reject')

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'REJECTED')
        reading = _reading(Client().get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode())
        self.assertIn(CATALOGUED_NAME, reading)
        self.assertNotIn(CORRECTED_NAME, reading)
        self.assertNotIn('Community correction', reading)

    def test_an_edit_is_decided_once(self):
        self._decide('approve')

        self._decide('reject')

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'APPROVED')

    def test_a_later_correction_replaces_an_earlier_one(self):
        self._decide('approve')
        again = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='Zukertort Opening',
            kind=OpeningNameSuggestion.SKind.EDIT,
            previous_name=CORRECTED_NAME, ip='10.0.0.4')

        self._decide('approve', suggestion=again)

        self.assertEqual(community_names.name_for(self.pos.key),
                         'Zukertort Opening')

    def test_a_correction_is_undone_by_correcting_it_back(self):
        """Aprobar la vuelta al nombre auditado retira la capa entera, no
        pinta una "correccion" que dice exactamente lo mismo."""
        self._decide('approve')
        back = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name=CATALOGUED_NAME,
            kind=OpeningNameSuggestion.SKind.EDIT,
            previous_name=CORRECTED_NAME, ip='10.0.0.5')

        self._decide('approve', suggestion=back)

        self.assertIsNone(community_names.name_for(self.pos.key))
        reading = _reading(Client().get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode())
        self.assertIn(f'Atomic opening {CATALOGUED_NAME}', reading)
        self.assertNotIn('Community correction', reading)

    def test_a_plain_user_cannot_approve_an_edit_either(self):
        plain = User.objects.create_user(username='plain', password='pw')
        Profile.objects.create(user=plain, enabled=True, approver=False)
        client = Client()
        client.login(username='plain', password='pw')

        client.post('/atomicdb/suggestions/',
                    {'suggestion': self.suggestion.id, 'action': 'approve'})

        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, 'PENDING')
        self.assertIsNone(community_names.name_for(self.pos.key))

    def test_the_history_shows_a_resolved_edit_as_one(self):
        self._decide('approve')

        reading = _reading(
            self.client.get('/atomicdb/suggestions/').content.decode())

        self.assertIn(f"EDIT: '{CATALOGUED_NAME}' → '{CORRECTED_NAME}'",
                      reading)
        self.assertIn('Recently resolved', reading)


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

    def test_a_new_name_never_shadows_the_audited_catalogue(self):
        """Nadie miro esa fila CONTRA el nombre oficial, asi que no lo tapa
        aunque este aprobada."""
        named = _named_position()
        OpeningNameSuggestion.objects.create(
            position=named, proposed_name='Impostor', ip='10.0.0.1',
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

        self.assertIsNone(community_names.opening_for(named.key, named.fen))
        self.assertIsNone(community_names.name_for(named.key))

    def test_only_an_approved_edit_shadows_the_audited_catalogue(self):
        """La unica excepcion, y lleva escrito contra que se propuso."""
        named = _named_position()
        OpeningNameSuggestion.objects.create(
            position=named, proposed_name=CORRECTED_NAME, ip='10.0.0.1',
            kind=OpeningNameSuggestion.SKind.EDIT,
            previous_name=CATALOGUED_NAME,
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

        record = community_names.opening_for(named.key, named.fen)
        self.assertEqual(record['name'], CORRECTED_NAME)
        self.assertEqual(record['corrects'], CATALOGUED_NAME)
        self.assertEqual(record['catalog_name'], CATALOGUED_NAME)

    def test_a_pending_edit_shadows_nothing(self):
        named = _named_position()
        OpeningNameSuggestion.objects.create(
            position=named, proposed_name=CORRECTED_NAME, ip='10.0.0.1',
            kind=OpeningNameSuggestion.SKind.EDIT,
            previous_name=CATALOGUED_NAME)
        community_names.invalidate()

        self.assertIsNone(community_names.opening_for(named.key, named.fen))

    def test_a_community_name_carries_no_catalogue_reading(self):
        pos = _unnamed_position()
        OpeningNameSuggestion.objects.create(
            position=pos, proposed_name='Belfast Gambit', ip='10.0.0.1',
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

        record = community_names.opening_for(pos.key, pos.fen)
        self.assertEqual(record['catalog_name'], '')
        self.assertEqual(record['corrects'], '')

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

    def test_cross_process_staleness_is_bounded_by_a_short_ttl(self):
        # M9: con LocMem cada proceso de gunicorn tiene SU cache, asi que
        # ``invalidate()`` solo alcanza al proceso que atendio la moderacion.
        # El limite real de obsolescencia en los demas es el TTL: si alguien
        # lo sube "porque total, se invalida a mano", vuelve el nombre viejo
        # sirviendose un minuto segun a que worker te toque.
        self.assertGreater(community_names.CACHE_SECONDS, 0)
        self.assertLessEqual(community_names.CACHE_SECONDS, 10)


class SuggesterIdentityTests(TestCase):
    """El aprobador quiere ver QUIEN propone (orden del 30-jul).

    Cuenta logueada -> ``suggested_by`` viaja con la propuesta y se pinta en
    la cola de moderacion; anonimo -> vacio, como siempre (el formulario
    sigue siendo publico y sin cuenta)."""

    def _propose(self, name):
        pos = ingest.get_or_create_position(logic.start_fen())
        return self.client.post(f'/atomicdb/suggest/{pos.key}/',
                                {'name': name, 'play': '', 'opening': ''})

    def test_a_logged_in_suggestion_records_the_account(self):
        from django.contrib.auth.models import User
        User.objects.create_user('lesha', password='p')
        self.client.login(username='lesha', password='p')

        self._propose('Lab Defence')

        row = OpeningNameSuggestion.objects.get(proposed_name='Lab Defence')
        self.assertEqual(row.suggested_by, 'lesha')

    def test_an_anonymous_suggestion_stays_nameless(self):
        self._propose('Ghost Gambit')

        row = OpeningNameSuggestion.objects.get(proposed_name='Ghost Gambit')
        self.assertEqual(row.suggested_by, '')

    def test_the_review_queue_shows_the_suggester(self):
        from django.contrib.auth.models import User
        from OpenBench.models import Profile
        User.objects.create_user('lesha', password='p')
        self.client.login(username='lesha', password='p')
        self._propose('Lab Defence')
        approver = User.objects.create_user('belz', password='p')
        Profile.objects.create(user=approver, approver=True, enabled=True)
        self.client.login(username='belz', password='p')

        body = self.client.get('/atomicdb/suggestions/').content.decode()

        self.assertIn('<strong>lesha</strong>', body)
