"""Campanas de exploracion: propuesta publica, voto, y activacion del dueno.

Lo que fija este fichero es el REPARTO DE PODER, que es la razon de ser de la
funcion: cualquiera propone una linea y cualquiera la vota, pero solo el
propietario la enciende.  De ahi salen las tres familias de pruebas:

* el buzon publico (proponer, deduplicar, votar una sola vez por cookie);
* la puerta del dueno (``/state/`` responde 403 a todo el mundo menos a el, y
  activar adopta el subarbol que ya existia, con tope y sin robar etiquetas);
* el peso en el selector, con sus tres limites duros — una campana votada
  reordena la exploracion, pero no resucita una rama enterrada, no puntua una
  posicion ya resuelta y no se pone delante del click de una persona.
"""

import html as html_module
import math
import re

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from . import ingest, logic, views
from .models import AnalysisTask, Campaign, CampaignVote, Edge, Position
from .testing import TestCase


def _reading(body):
    """Lo que un visitante LEE: sin etiquetas y con los espacios colapsados.

    La respuesta pasa por el minificador de HTML, asi que una asercion sobre
    saltos de linea del template comprobaria el minificador y no la pagina.
    """
    return ' '.join(html_module.unescape(re.sub(r'<[^>]+>', ' ', body)).split())


def _line(*ucis):
    """Materializa una linea desde la raiz y devuelve su ultima posicion."""
    parent = ingest.get_or_create_position(logic.start_fen())
    for uci in ucis:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        parent = child
    return parent


def _campaign_on(position, state=Campaign.CState.ACTIVE, votes=0, name=None):
    """Una campana ya montada sobre ``position``, sin pasar por la vista."""
    campaign = Campaign.objects.create(
        name=name or f'campaign-{position.key[:10]}', root=position,
        line_san='1. Nf3', state=state, votes=votes,
        active=state == Campaign.CState.ACTIVE)
    return campaign


def _tag(campaign, *positions):
    Position.objects.filter(key__in=[p.key for p in positions]).update(
        campaign=campaign)


def _owner_client(username='belzedar', staff=True):
    user = User.objects.create_user(username=username, password='pw')
    if staff:
        user.is_staff = True
        user.save(update_fields=['is_staff'])
    client = Client()
    client.login(username=username, password='pw')
    return client


class ProposeTests(TestCase):
    """El buzon publico: quien propone, que se guarda y que no se acepta."""

    def setUp(self):
        self.pos = _line('g1f3', 'f7f6')
        self.client = Client()

    def _propose(self, key=None, **extra):
        payload = {'key': key or self.pos.key}
        payload.update(extra)
        return self.client.post('/atomicdb/campaign/propose/', payload)

    def test_a_public_visitor_files_a_proposal_without_an_account(self):
        response = self._propose(name='Wolfram')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
        campaign = Campaign.objects.get()
        self.assertEqual(campaign.state, Campaign.CState.PROPOSED)
        self.assertEqual(campaign.root_id, self.pos.key)
        self.assertEqual(campaign.proposed_by, 'Wolfram')
        self.assertEqual(campaign.votes, 0)
        self.assertIsNone(campaign.activated_at)

    def test_a_proposal_is_not_an_activation(self):
        """El espejo deprecado tambien tiene que decir que esto no corre."""
        self._propose()

        self.assertFalse(Campaign.objects.get().active)

    def test_the_line_is_written_from_the_lineage_not_from_the_client(self):
        self._propose(line_san='whatever I feel like')

        campaign = Campaign.objects.get()
        self.assertTrue(campaign.line_san)
        self.assertIn('Nf3', campaign.line_san)
        self.assertNotIn('whatever', campaign.line_san)

    def test_the_returned_id_is_the_campaign_that_was_created(self):
        body = self._propose().json()

        self.assertEqual(body['id'], Campaign.objects.get().id)

    def test_proposing_the_same_line_twice_returns_the_one_that_exists(self):
        first = self._propose().json()

        second = self._propose().json()

        self.assertEqual(second['status'], 'exists')
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(Campaign.objects.count(), 1)

    def test_the_dedup_also_covers_a_campaign_the_owner_paused(self):
        campaign = _campaign_on(self.pos, state=Campaign.CState.PAUSED)

        body = self._propose().json()

        self.assertEqual(body['status'], 'exists')
        self.assertEqual(body['id'], campaign.id)

    def test_an_archived_line_can_be_proposed_again(self):
        """``DONE`` es lo unico que libera la raiz: una campana cerrada no
        puede bloquear para siempre a quien quiera retomar esa linea."""
        _campaign_on(self.pos, state=Campaign.CState.DONE)

        body = self._propose().json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(Campaign.objects.count(), 2)

    def test_a_visitor_files_three_proposals_a_day_and_no_more(self):
        lines = [_line('g1f3'), _line('b1c3'), _line('e2e3'), _line('d2d3')]
        outcomes = [self._propose(key=pos.key).json()['status']
                    for pos in lines]

        self.assertEqual(outcomes, ['ok', 'ok', 'ok', 'rate-limited'])
        self.assertEqual(Campaign.objects.count(), 3)

    def test_the_daily_limit_counts_the_cookie_and_not_the_address(self):
        """Un CGNAT es mucha gente: contar por IP los frenaria a todos."""
        for pos in (_line('g1f3'), _line('b1c3'), _line('e2e3')):
            self._propose(key=pos.key)

        stranger = Client()
        response = stranger.post('/atomicdb/campaign/propose/',
                                 {'key': _line('d2d3').key})

        self.assertEqual(response.json()['status'], 'ok')

    def test_a_position_the_tree_does_not_have_is_refused_as_such(self):
        response = self._propose(key='0' * 64)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['status'], 'unknown-position')
        self.assertFalse(Campaign.objects.exists())

    def test_a_get_buys_nothing(self):
        response = self.client.get('/atomicdb/campaign/propose/')

        self.assertEqual(response.status_code, 405)
        self.assertFalse(Campaign.objects.exists())

    def test_a_button_press_lands_back_on_the_overview(self):
        response = self._propose(back='1')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/atomicdb/?campaign=ok', response['Location'])

    def test_the_explorer_offers_the_proposal_form(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.pos.key}/').content.decode()

        self.assertIn('/atomicdb/campaign/propose/', body)
        self.assertIn('csrfmiddlewaretoken', body)


class VoteTests(TestCase):

    def setUp(self):
        self.pos = _line('g1f3', 'f7f6')
        self.campaign = _campaign_on(self.pos,
                                     state=Campaign.CState.PROPOSED)
        self.client = Client()

    def _vote(self, client=None, **extra):
        return (client or self.client).post(
            f'/atomicdb/campaign/{self.campaign.id}/vote/', extra)

    def test_a_first_vote_counts_and_stamps_the_voter_cookie(self):
        response = self._vote(name='Lesha')

        self.assertEqual(response.json(), {'status': 'voted', 'votes': 1})
        self.assertIn(views.CAMPAIGN_VOTER_COOKIE, response.cookies)
        row = CampaignVote.objects.get()
        self.assertEqual(row.campaign_id, self.campaign.id)
        self.assertEqual(row.name, 'Lesha')

    def test_voting_twice_from_the_same_browser_does_not_count_twice(self):
        self._vote()

        response = self._vote()

        self.assertEqual(response.json(), {'status': 'already', 'votes': 1})
        self.assertEqual(CampaignVote.objects.count(), 1)

    def test_the_cached_count_matches_the_rows_that_back_it(self):
        for _ in range(3):
            self._vote(client=Client())

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.votes, 3)
        self.assertEqual(
            self.campaign.votes,
            CampaignVote.objects.filter(campaign=self.campaign).count())

    def test_the_cached_count_is_rewritten_not_incremented(self):
        """Un voto que ya existia no puede hacer subir el contador."""
        Campaign.objects.filter(id=self.campaign.id).update(votes=99)
        self._vote()

        response = self._vote()

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.votes, 1)
        self.assertEqual(response.json()['votes'], 1)

    def test_a_forged_cookie_is_replaced_instead_of_trusted(self):
        self.client.cookies[views.CAMPAIGN_VOTER_COOKIE] = 'not a token!!'

        response = self._vote()

        self.assertEqual(response.json()['status'], 'voted')
        self.assertNotEqual(CampaignVote.objects.get().token, 'not a token!!')

    def test_a_campaign_that_does_not_exist_says_so(self):
        response = self.client.post('/atomicdb/campaign/999999/vote/')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['status'], 'unknown-campaign')

    def test_a_get_buys_nothing(self):
        response = self.client.get(
            f'/atomicdb/campaign/{self.campaign.id}/vote/')

        self.assertEqual(response.status_code, 405)
        self.assertFalse(CampaignVote.objects.exists())


class OwnerGateTests(TestCase):
    """``/state/`` es la unica asimetria del diseno y se comprueba sola."""

    def setUp(self):
        self.pos = _line('g1f3', 'f7f6')
        self.campaign = _campaign_on(self.pos,
                                     state=Campaign.CState.PROPOSED)
        self.url = f'/atomicdb/campaign/{self.campaign.id}/state/'

    def test_an_anonymous_visitor_cannot_activate_anything(self):
        response = Client().post(self.url, {'action': 'activate'})

        self.assertEqual(response.status_code, 403)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, Campaign.CState.PROPOSED)

    def test_a_logged_in_visitor_who_is_not_the_owner_cannot_either(self):
        client = _owner_client(username='plain', staff=False)

        response = client.post(self.url, {'action': 'activate'})

        self.assertEqual(response.status_code, 403)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, Campaign.CState.PROPOSED)

    def test_hiding_the_button_is_not_the_protection(self):
        """La portada esconde el boton por cortesia; el 403 lo pone la vista,
        y por eso un POST a mano tampoco pasa."""
        for action in ('activate', 'pause', 'done'):
            with self.subTest(action=action):
                response = Client().post(self.url, {'action': action})
                self.assertEqual(response.status_code, 403)

    def test_the_owner_activates_and_the_row_records_when(self):
        client = _owner_client()

        response = client.post(self.url, {'action': 'activate'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'activated')
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, Campaign.CState.ACTIVE)
        self.assertTrue(self.campaign.active)
        self.assertIsNotNone(self.campaign.activated_at)

    def test_pausing_and_closing_move_the_state_and_the_mirror(self):
        client = _owner_client()
        client.post(self.url, {'action': 'activate'})

        for action, state in (('pause', Campaign.CState.PAUSED),
                              ('done', Campaign.CState.DONE)):
            with self.subTest(action=action):
                client.post(self.url, {'action': action})
                self.campaign.refresh_from_db()
                self.assertEqual(self.campaign.state, state)
                self.assertFalse(self.campaign.active)

    def test_an_action_nobody_defined_changes_nothing(self):
        client = _owner_client()

        response = client.post(self.url, {'action': 'delete-everything'})

        self.assertEqual(response.status_code, 400)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, Campaign.CState.PROPOSED)


class ActivationTaggingTests(TestCase):
    """Activar adopta el subarbol que YA estaba, una vez y con tope."""

    def setUp(self):
        self.root = _line('g1f3')
        self.children = ingest.expand(
            Position.objects.get(key=self.root.key))
        self.campaign = _campaign_on(self.root,
                                     state=Campaign.CState.PROPOSED)

    def _activate(self):
        client = _owner_client()
        return client.post(
            f'/atomicdb/campaign/{self.campaign.id}/state/',
            {'action': 'activate'})

    def test_activating_tags_the_root_and_the_subtree_below_it(self):
        self.assertTrue(self.children)

        self._activate()

        self.assertEqual(Position.objects.get(key=self.root.key).campaign_id,
                         self.campaign.id)
        tagged = set(Position.objects.filter(campaign=self.campaign)
                     .values_list('key', flat=True))
        self.assertTrue({child.key for child in self.children} <= tagged)

    def test_the_answer_says_how_much_it_adopted(self):
        body = self._activate().json()

        self.assertEqual(body['tagged'], 1 + len(self.children))
        self.assertEqual(body['visited'], 1 + len(self.children))

    def test_a_position_another_campaign_already_owns_is_not_stolen(self):
        theirs = _campaign_on(self.children[0],
                              state=Campaign.CState.ACTIVE, name='theirs')
        _tag(theirs, self.children[0])

        self._activate()

        self.assertEqual(
            Position.objects.get(key=self.children[0].key).campaign_id,
            theirs.id)

    def test_the_walk_stops_at_its_cap(self):
        tagged, visited = views._tag_campaign_subtree(self.campaign, cap=3)

        self.assertEqual(visited, 3)
        self.assertEqual(tagged, 3)
        self.assertEqual(
            Position.objects.filter(campaign=self.campaign).count(), 3)

    def test_pausing_does_not_hand_the_subtree_back(self):
        client = _owner_client()
        url = f'/atomicdb/campaign/{self.campaign.id}/state/'
        client.post(url, {'action': 'activate'})
        before = Position.objects.filter(campaign=self.campaign).count()

        client.post(url, {'action': 'pause'})

        self.assertEqual(
            Position.objects.filter(campaign=self.campaign).count(), before)


class InheritanceTests(TestCase):
    """Lo que nace bajo una campana es suyo; lo que ya estaba, no."""

    def test_a_child_born_under_a_tagged_parent_inherits_the_tag(self):
        parent = _line('g1f3')
        campaign = _campaign_on(parent)
        _tag(campaign, parent)

        children = ingest.expand(Position.objects.get(key=parent.key))

        self.assertTrue(children)
        self.assertEqual({child.campaign_id for child in children},
                         {campaign.id})

    def test_a_child_that_already_existed_keeps_having_no_owner(self):
        parent = _line('g1f3')
        stranger = ingest.get_or_create_position(
            logic.apply_move(parent.fen, 'g8f6'))
        campaign = _campaign_on(parent)
        _tag(campaign, parent)

        ingest.expand(Position.objects.get(key=parent.key))

        self.assertIsNone(
            Position.objects.get(key=stranger.key).campaign_id)

    def test_an_untagged_parent_does_not_invent_an_owner(self):
        parent = _line('g1f3')

        children = ingest.expand(Position.objects.get(key=parent.key))

        self.assertEqual({child.campaign_id for child in children}, {None})

    def test_a_node_born_from_a_click_in_the_explorer_inherits_too(self):
        """``expand`` no es el unico sitio que materializa nodos: navegar por
        el tablero tambien crea el hijo, y la herencia tiene que ser la misma
        o una campana se llenaria de agujeros por donde paso una persona."""
        parent = _line('g1f3')
        campaign = _campaign_on(parent)
        _tag(campaign, parent)

        Client().get(f'/atomicdb/goto/{parent.key}/g8f6/')

        child_key = logic.key_of(logic.canonical_fen(
            logic.apply_move(parent.fen, 'g8f6')))
        self.assertEqual(Position.objects.get(key=child_key).campaign_id,
                         campaign.id)


class SelectorBonusTests(TestCase):
    """El peso de una campana en el selector, y lo que NO puede comprar."""

    def setUp(self):
        root = ingest.get_or_create_position(logic.start_fen())
        self.pair = []
        for uci in ('g1f3', 'b1c3'):
            child = ingest.get_or_create_position(
                logic.apply_move(root.fen, uci))
            Edge.objects.get_or_create(parent=root, move_uci=uci,
                                       defaults={'child': child})
            Position.objects.filter(key=child.key).update(eval_cp=20)
            self.pair.append(Position.objects.get(key=child.key))
        self.tagged, self.plain = self.pair

    def _priorities(self):
        return {pos.key: pos.priority for pos in Position.objects.all()}

    def test_an_active_campaign_lifts_its_positions_over_identical_ones(self):
        campaign = _campaign_on(self.tagged, votes=7)
        _tag(campaign, self.tagged)

        ingest.refresh_priorities(force=True)

        priorities = self._priorities()
        self.assertGreater(priorities[self.tagged.key],
                           priorities[self.plain.key])
        self.assertAlmostEqual(
            priorities[self.tagged.key] - priorities[self.plain.key],
            ingest.CAMPAIGN_BONUS * math.log1p(7), places=6)

    def test_more_votes_weigh_more_but_with_diminishing_returns(self):
        campaign = _campaign_on(self.tagged, votes=1)
        _tag(campaign, self.tagged)
        ingest.refresh_priorities(force=True)
        one = self._priorities()[self.tagged.key]
        Campaign.objects.filter(id=campaign.id).update(votes=2)
        ingest.refresh_priorities(force=True)
        two = self._priorities()[self.tagged.key]
        Campaign.objects.filter(id=campaign.id).update(votes=3)
        ingest.refresh_priorities(force=True)
        three = self._priorities()[self.tagged.key]

        self.assertGreater(two, one)
        self.assertGreater(three, two)
        self.assertLess(three - two, two - one)

    def test_a_campaign_that_is_only_proposed_weighs_nothing(self):
        campaign = _campaign_on(self.tagged, state=Campaign.CState.PROPOSED,
                                votes=50)
        _tag(campaign, self.tagged)

        ingest.refresh_priorities(force=True)

        priorities = self._priorities()
        self.assertEqual(priorities[self.tagged.key],
                         priorities[self.plain.key])

    def test_a_paused_campaign_stops_weighing_without_losing_its_tags(self):
        campaign = _campaign_on(self.tagged, votes=9)
        _tag(campaign, self.tagged)
        campaign.apply_state(Campaign.CState.PAUSED)

        ingest.refresh_priorities(force=True)

        priorities = self._priorities()
        self.assertEqual(priorities[self.tagged.key],
                         priorities[self.plain.key])
        self.assertEqual(
            Position.objects.get(key=self.tagged.key).campaign_id,
            campaign.id)

    def test_a_tombstone_is_not_resurrected_by_any_number_of_votes(self):
        """Enterrar una rama fue una conclusion sobre el arbol; votar es una
        preferencia, y una preferencia no revoca una conclusion."""
        campaign = _campaign_on(self.tagged, votes=1000)
        _tag(campaign, self.tagged)
        Position.objects.filter(key=self.tagged.key).update(
            campaign=campaign, priority=ingest.DEAD)

        ingest.refresh_priorities(force=True)

        self.assertEqual(Position.objects.get(key=self.tagged.key).priority,
                         ingest.DEAD)

    def test_a_closed_position_does_not_come_back_because_it_was_voted(self):
        campaign = _campaign_on(self.tagged, votes=25)
        Position.objects.filter(key=self.tagged.key).update(
            campaign=campaign, status='WHITE_WIN', priority=0.0)

        ingest.refresh_priorities(force=True)

        self.assertEqual(
            Position.objects.get(key=self.tagged.key).priority, 0.0)

    def test_the_user_band_of_the_lease_is_untouched(self):
        """El arriendo ordena por procedencia ANTES que por prioridad, y
        dentro de USER ignora la prioridad de la posicion: por muchos votos
        que junte una campana, no se pone delante del click de una persona.

        Se comprueba contra el endpoint real y no contra una copia de su
        ordenacion, porque lo que hay que garantizar es lo que sirve el
        servidor, no lo que la prueba cree que sirve.
        """
        User.objects.create_user(username='w', password='p')
        campaign = _campaign_on(self.tagged, votes=500)
        _tag(campaign, self.tagged)
        ingest.refresh_priorities(force=True)
        boosted = Position.objects.get(key=self.tagged.key)
        clicked_on = Position.objects.get(key=self.plain.key)
        self.assertGreater(boosted.priority, clicked_on.priority)
        AnalysisTask.objects.create(
            position=boosted, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)
        clicked = AnalysisTask.objects.create(
            position=clicked_on, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)

        leased = Client().post('/atomicdb/api/lease', {
            'username': 'w', 'password': 'p', 'machine': 'm-campaign',
            'worker_build': '2026072203', 'lease_session': 'm-campaign',
        }).json()['tasks'][0]

        self.assertEqual(leased['id'], clicked.id)


class HomeCampaignTests(TestCase):
    """La portada: la campana viva arriba con sus numeros, y el buzon."""

    def setUp(self):
        self.root = _line('g1f3', 'f7f6')
        self.campaign = _campaign_on(self.root, votes=4)
        Campaign.objects.filter(id=self.campaign.id).update(
            line_san='1. Nf3 f6', proposed_by='Wolfram')
        extras = [_line('g1f3', 'f7f6', 'b1c3'),
                  _line('g1f3', 'f7f6', 'e2e3'),
                  _line('g1f3', 'f7f6', 'd2d3')]
        _tag(self.campaign, self.root, *extras)
        Position.objects.filter(
            key__in=[extras[0].key, extras[1].key]).update(
            nodes_invested=5_000_000)
        Position.objects.filter(key=extras[2].key).update(status='DRAW')
        self.client = Client()

    def _home(self):
        cache.clear()
        return _reading(self.client.get('/atomicdb/').content.decode())

    def test_the_running_campaign_is_named_on_the_front_page(self):
        cache.clear()
        raw = self.client.get('/atomicdb/').content.decode()

        self.assertIn('Campaign: 1. Nf3 f6', _reading(raw))
        self.assertIn(f'/atomicdb/explore/{self.root.key}/', raw)

    def test_the_card_shows_votes_progress_and_the_share_solved(self):
        body = self._home()

        self.assertIn('4 votes', body)
        self.assertIn('2 of 4 positions explored', body)
        self.assertIn('1 solved', body)
        self.assertIn('25.0% of the line solved', body)

    def test_a_campaign_still_waiting_is_listed_but_not_pinned_on_top(self):
        waiting = _campaign_on(_line('b1c3'),
                               state=Campaign.CState.PROPOSED, votes=2,
                               name='waiting')
        Campaign.objects.filter(id=waiting.id).update(line_san='1. Nc3')

        body = self._home()

        self.assertIn('Proposed campaigns', body)
        self.assertIn('1. Nc3', body)
        self.assertNotIn('Campaign: 1. Nc3', body)

    def test_every_proposal_carries_its_own_vote_button(self):
        waiting = _campaign_on(_line('b1c3'),
                               state=Campaign.CState.PROPOSED, name='waiting')

        cache.clear()
        body = self.client.get('/atomicdb/').content.decode()

        self.assertIn(f'/atomicdb/campaign/{waiting.id}/vote/', body)
        self.assertIn('csrfmiddlewaretoken', body)

    def test_a_visitor_is_not_offered_the_owner_buttons(self):
        waiting = _campaign_on(_line('b1c3'),
                               state=Campaign.CState.PROPOSED, name='waiting')

        cache.clear()
        body = self.client.get('/atomicdb/').content.decode()

        self.assertNotIn(f'/atomicdb/campaign/{waiting.id}/state/', body)
        self.assertNotIn('Activate', _reading(body))

    def test_the_owner_is_offered_the_buttons_that_decide(self):
        waiting = _campaign_on(_line('b1c3'),
                               state=Campaign.CState.PROPOSED, name='waiting')
        client = _owner_client()

        cache.clear()
        body = client.get('/atomicdb/').content.decode()

        self.assertIn(f'/atomicdb/campaign/{waiting.id}/state/', body)
        self.assertIn('Activate', _reading(body))

    def test_the_page_says_who_decides(self):
        body = self._home()

        self.assertIn('The site owner decides which one the solver actually '
                      'runs.', body)

    def test_an_outcome_from_a_button_comes_back_as_a_sentence(self):
        cache.clear()
        body = _reading(
            self.client.get('/atomicdb/?campaign=already').content.decode())

        self.assertIn('You had already voted for that campaign.', body)

    def test_an_outcome_nobody_defined_prints_nothing(self):
        """El parametro no se hace eco: se busca en una tabla de frases y lo
        que no esta no sale."""
        cache.clear()
        raw = self.client.get(
            '/atomicdb/?campaign=<script>alert(1)</script>').content.decode()

        self.assertNotIn('campaign-status', raw)
        self.assertNotIn('alert(1)', raw)
