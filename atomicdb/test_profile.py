"""La pagina de un contribuidor: su cola, sus maquinas y sus nombres.

Lo que se fija aqui: que ``/atomicdb/me/`` lleva a la pagina propia (o al
login), que una cuenta sin actividad NO tiene pagina, que la cola ensena
exactamente las peticiones de esa persona y de nadie mas, que los agregados
de flota salen con los numeros que se pusieron, y que la cabecera nueva no se
le puede servir a otro desde una pagina cacheada.
"""

import html as html_module
import re
from datetime import timedelta

from django.conf import settings
from django.db import connections
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import contributors, ingest, logic
from .models import AnalysisTask, Edge, OpeningNameSuggestion, WorkerPing
from .testing import TestCase, worker_account


SUGGESTION_IP = '203.0.113.44'


def _reading(body):
    """Lo que un visitante LEE: sin etiquetas y con los espacios colapsados."""
    return ' '.join(html_module.unescape(re.sub(r'<[^>]+>', ' ', body)).split())


def _child(uci):
    """Un hijo de la posicion inicial, con su arista materializada."""
    root = ingest.get_or_create_position(logic.start_fen())
    child = ingest.get_or_create_position(logic.apply_move(root.fen, uci))
    Edge.objects.get_or_create(parent=root, move_uci=uci,
                               defaults={'child': child})
    return child


def _task(position, **fields):
    """Una tarea sobre esa posicion, en la primera generacion libre."""
    generation = 0
    while AnalysisTask.objects.filter(position=position,
                                      generation=generation).exists():
        generation += 1
    fields.setdefault('budget_nodes', 128_000_000)
    return AnalysisTask.objects.create(position=position,
                                       generation=generation, **fields)


def _completed(position, machine, nodes, seconds, ago_hours=1, **fields):
    task = _task(position, machine=machine, nodes_searched=nodes,
                 elapsed_seconds=seconds,
                 state=AnalysisTask.TState.COMPLETED, **fields)
    AnalysisTask.objects.filter(pk=task.pk).update(
        completed=timezone.now() - timedelta(hours=ago_hours))
    task.refresh_from_db()
    return task


def _ping(machine, user, **fields):
    fields.setdefault('threads', 8)
    fields.setdefault('hash_mb', 1024)
    fields.setdefault('os', 'Linux')
    return WorkerPing.objects.create(machine=machine, user=user, **fields)


def _suggestion(username, name, status=OpeningNameSuggestion.SState.PENDING):
    return OpeningNameSuggestion.objects.create(
        position=_child('h2h3'), proposed_name=name, suggested_by=username,
        status=status, ip=SUGGESTION_IP)


class MeRedirectTests(TestCase):

    def setUp(self):
        contributors.reset_cache()
        self.alice = worker_account('alice')

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        response = Client().get('/atomicdb/me/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/?next=/atomicdb/me/')

    def test_a_signed_in_visitor_lands_on_their_own_page(self):
        client = Client()
        client.login(username='alice', password='pw')

        response = client.get('/atomicdb/me/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/atomicdb/user/alice/')

    def test_the_shortcut_survives_a_name_that_needs_quoting(self):
        worker_account('a b+c')
        client = Client()
        client.login(username='a b+c', password='pw')

        response = client.get('/atomicdb/me/')

        self.assertEqual(response['Location'], '/atomicdb/user/a%20b%2Bc/')


class ExistenceTests(TestCase):

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()

    def test_a_name_with_no_activity_has_no_page(self):
        worker_account('ghost')

        response = self.client.get('/atomicdb/user/ghost/')

        self.assertEqual(response.status_code, 404)
        self.assertIn('No AtomicDB activity under that name',
                      _reading(response.content.decode()))

    def test_a_worker_alone_is_enough(self):
        _ping('box-atomicdb', 'alice')

        self.assertEqual(
            self.client.get('/atomicdb/user/alice/').status_code, 200)

    def test_a_request_alone_is_enough(self):
        _task(_child('a2a3'), source=AnalysisTask.Source.USER,
              requested_by='alice')

        self.assertEqual(
            self.client.get('/atomicdb/user/alice/').status_code, 200)

    def test_a_proposed_name_alone_is_enough(self):
        _suggestion('alice', 'Belfast Gambit')

        self.assertEqual(
            self.client.get('/atomicdb/user/alice/').status_code, 200)

    def test_the_page_is_never_cached(self):
        _ping('box-atomicdb', 'alice')

        response = self.client.get('/atomicdb/user/alice/')

        self.assertNotIn('max-age',
                         response.headers.get('Cache-Control', ''))


class QueueTests(TestCase):
    """La cola es de UNA persona y de UN origen: sus peticiones USER."""

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()
        self.mine = _child('a2a3')        # 1.a3
        self.auto = _child('b2b3')        # 1.b3
        self.theirs = _child('c2c3')      # 1.c3

    def _body(self, username='alice'):
        return _reading(
            self.client.get(f'/atomicdb/user/{username}/').content.decode())

    def test_only_your_own_user_requests_show_up(self):
        _task(self.mine, source=AnalysisTask.Source.USER,
              requested_by='alice')
        # Del selector, servida por su maquina: cuenta como computo, no como
        # peticion suya.
        _task(self.auto, source=AnalysisTask.Source.AUTO,
              requested_by='alice')
        _task(self.theirs, source=AnalysisTask.Source.USER,
              requested_by='bob')

        body = self._body()

        self.assertIn('1. a3', body)
        self.assertNotIn('1. b3', body)
        self.assertNotIn('1. c3', body)

    def test_the_three_states_land_in_their_own_lists(self):
        _task(self.mine, source=AnalysisTask.Source.USER, requested_by='alice',
              state=AnalysisTask.TState.LEASED, machine='box-atomicdb',
              leased_at=timezone.now(), lease_heartbeat_at=timezone.now())
        _task(self.auto, source=AnalysisTask.Source.USER, requested_by='alice')
        _completed(self.theirs, 'box-atomicdb', 4_000_000, 4.0,
                   source=AnalysisTask.Source.USER, requested_by='alice')

        body = self._body()

        self.assertIn('Running now', body)
        self.assertIn('box-atomicdb', body)
        self.assertIn('Waiting', body)
        self.assertIn('Served', body)
        for line in ('1. a3', '1. b3', '1. c3'):
            self.assertIn(line, body)

    def test_the_waiting_row_says_how_much_queue_is_ahead(self):
        # Dos peticiones nombradas antes que la suya: su sitio es el tercero.
        _task(self.auto, source=AnalysisTask.Source.USER, requested_by='bob')
        _task(self.theirs, source=AnalysisTask.Source.USER, requested_by='bob')
        _task(self.mine, source=AnalysisTask.Source.USER, requested_by='alice')

        body = self._body()

        self.assertIn('2 requests ahead', body)

    def test_an_empty_queue_says_so_without_pretending(self):
        _ping('box-atomicdb', 'alice')

        body = self._body()

        self.assertIn('No analysis requests yet', body)


class WorkerAggregationTests(TestCase):
    """Los numeros de la flota, con un fixture de tres busquedas."""

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()
        self.pos = _child('a2a3')
        self.other = _child('b2b3')
        self.third = _child('c2c3')
        _ping('alice-box-atomicdb', 'alice')
        _ping('bob-box-atomicdb', 'bob')
        # Dos busquedas suyas dentro de las 24h y una de hace cinco dias.
        _completed(self.pos, 'alice-box-atomicdb', 2_000_000, 2.0)
        _completed(self.other, 'alice-box-atomicdb', 1_000_000, 2.0)
        _completed(self.third, 'alice-box-atomicdb', 6_000_000, 2.0,
                   ago_hours=24 * 5)
        # Y una de otra persona, que es la que hace que la cuota no sea 100%.
        _completed(self.pos, 'bob-box-atomicdb', 1_000_000, 1.0)

    def test_the_totals_are_exactly_what_was_searched(self):
        page = contributors.present('alice')

        self.assertEqual(page['nodes_24h_h'], '3.0M')
        self.assertEqual(page['tasks_24h_h'], '2')
        self.assertEqual(page['nodes_all_h'], '9.0M')
        self.assertEqual(page['tasks_all_h'], '3')

    def test_the_average_speed_is_nodes_over_engine_seconds(self):
        page = contributors.present('alice')

        # 9.0M nodos en 6 segundos de motor.
        self.assertEqual(page['workers'][0]['nps_h'], '1.5M')

    def test_the_fleet_share_counts_the_whole_last_day(self):
        page = contributors.present('alice')

        # 3.0M suyos de los 4.0M que se buscaron en 24h.
        self.assertEqual(page['fleet_share'], 75.0)
        self.assertEqual(page['fleet_nodes_24h_h'], '4.0M')

    def test_the_ranking_names_its_own_denominator(self):
        page = contributors.present('alice')

        self.assertEqual(page['rank_all'], {'rank': 1, 'of': 2})
        self.assertEqual(page['rank_24h'], {'rank': 1, 'of': 2})
        self.assertEqual(contributors.present('bob')['rank_all'],
                         {'rank': 2, 'of': 2})

    def test_a_machine_belongs_to_whoever_pinged_it_not_to_its_name(self):
        # El string de maquina EMPIEZA por 'alice-', y aun asi es de bob.
        WorkerPing.objects.filter(machine='alice-box-atomicdb').update(
            user='bob')
        contributors.reset_cache()

        page = contributors.present('alice')

        self.assertEqual(page['nodes_all_h'], '0')
        self.assertEqual(contributors.present('bob')['nodes_all_h'], '10.0M')

    def test_the_page_paints_the_machine_with_its_capacity(self):
        body = _reading(
            self.client.get('/atomicdb/user/alice/').content.decode())

        self.assertIn('alice-box-atomicdb', body)
        self.assertIn('8 threads', body)
        self.assertIn('1024 MB hash', body)
        self.assertIn('online', body)
        self.assertIn('3.0M nodes', body)

    def test_a_silent_machine_reads_as_offline(self):
        WorkerPing.objects.filter(machine='alice-box-atomicdb').update(
            last_seen=timezone.now() - timedelta(hours=3))

        page = contributors.present('alice')

        self.assertFalse(page['workers'][0]['online'])

    def test_the_node_clubs_follow_the_all_time_total(self):
        self.assertEqual(contributors.present('alice')['badges'], [])
        _completed(_child('d2d3'), 'alice-box-atomicdb', 2_000_000_000, 100.0)
        contributors.reset_cache()

        self.assertEqual(contributors.present('alice')['badges'], ['1B club'])

    def test_the_two_week_chart_has_one_bar_per_day(self):
        page = contributors.present('alice')

        self.assertTrue(page['has_spark'])
        self.assertEqual(len(page['spark_bars']), contributors.SPARK_DAYS)
        # Lo de hace cinco dias entra; el pico es el dia con 3.0M.
        self.assertEqual(page['spark_peak_h'], '6.0M')
        self.assertEqual(page['spark_total_h'], '9.0M')

    def test_a_worker_that_never_searched_still_gets_its_row(self):
        _ping('idle-box-atomicdb', 'carol')

        page = contributors.present('carol')

        self.assertTrue(page['has_workers'])
        self.assertFalse(page['has_nodes'])
        self.assertFalse(page['has_spark'])
        self.assertIsNone(page['rank_all'])
        self.assertEqual(page['workers'][0]['nps_h'], '')


class LiveLeaseTests(TestCase):

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()

    def test_a_machine_shows_what_it_is_searching(self):
        _ping('alice-box-atomicdb', 'alice')
        _task(_child('a2a3'), state=AnalysisTask.TState.LEASED,
              machine='alice-box-atomicdb', leased_at=timezone.now(),
              lease_heartbeat_at=timezone.now())

        body = _reading(
            self.client.get('/atomicdb/user/alice/').content.decode())

        self.assertIn('searching 1. a3', body)

    def test_a_quiet_lease_of_your_own_says_it_went_quiet(self):
        stale = timezone.now() - timedelta(hours=3)
        _task(_child('a2a3'), source=AnalysisTask.Source.USER,
              requested_by='alice', state=AnalysisTask.TState.LEASED,
              machine='someone-else-atomicdb', leased_at=stale,
              lease_heartbeat_at=stale)

        body = _reading(
            self.client.get('/atomicdb/user/alice/').content.decode())

        self.assertIn('no recent heartbeat', body)


class SuggestionTests(TestCase):

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()

    def test_the_names_are_listed_with_their_state(self):
        _suggestion('alice', 'Belfast Gambit')
        _suggestion('alice', 'Dublin Attack',
                    OpeningNameSuggestion.SState.APPROVED)
        _suggestion('alice', 'Cork Defence',
                    OpeningNameSuggestion.SState.REJECTED)

        body = _reading(
            self.client.get('/atomicdb/user/alice/').content.decode())

        for name in ('Belfast Gambit', 'Dublin Attack', 'Cork Defence'):
            self.assertIn(name, body)
        for state in ('pending', 'approved', 'rejected'):
            self.assertIn(state, body)

    def test_no_address_of_a_proposer_ever_reaches_the_page(self):
        _suggestion('alice', 'Belfast Gambit')

        body = self.client.get('/atomicdb/user/alice/').content.decode()

        self.assertIn('Belfast Gambit', body)
        self.assertNotIn(SUGGESTION_IP, body)

    def test_someone_elses_name_is_not_your_name(self):
        _suggestion('alice', 'Belfast Gambit')
        _suggestion('bob', 'Galway Variation')

        body = _reading(
            self.client.get('/atomicdb/user/alice/').content.decode())

        self.assertIn('Belfast Gambit', body)
        self.assertNotIn('Galway Variation', body)

    def test_a_contributor_without_names_gets_the_empty_state(self):
        _ping('alice-box-atomicdb', 'alice')

        body = _reading(
            self.client.get('/atomicdb/user/alice/').content.decode())

        self.assertIn('No opening names proposed yet', body)
        self.assertNotIn('No workers under this account', body)

    def test_a_contributor_without_workers_gets_the_other_empty_state(self):
        _suggestion('alice', 'Belfast Gambit')

        body = _reading(
            self.client.get('/atomicdb/user/alice/').content.decode())

        self.assertIn('No workers under this account', body)


class VoiceTests(TestCase):
    """La pagina es publica: tutea al dueno y habla en tercera del resto."""

    def setUp(self):
        contributors.reset_cache()
        worker_account('alice')
        _ping('alice-box-atomicdb', 'alice')

    def test_your_own_page_speaks_to_you(self):
        client = Client()
        client.login(username='alice', password='pw')

        body = _reading(client.get('/atomicdb/user/alice/').content.decode())

        self.assertIn('Your queue', body)
        self.assertIn('Your workers', body)

    def test_someone_elses_page_speaks_about_them(self):
        body = _reading(
            Client().get('/atomicdb/user/alice/').content.decode())

        self.assertIn("alice’s queue", body)
        self.assertNotIn('Your queue', body)


class QueryCostTests(TestCase):
    """Lo que cuesta la pagina NO puede depender de cuantas filas ensena."""

    PAWNS = ('a2a3', 'b2b3', 'c2c3', 'd2d3', 'e2e3', 'f2f3', 'g2g3', 'h2h3',
             'a2a4', 'b2b4', 'c2c4', 'd2d4')

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()
        _ping('alice-box-atomicdb', 'alice')

    def _cost(self, rows):
        for uci in self.PAWNS[:rows]:
            _completed(_child(uci), 'alice-box-atomicdb', 1_000_000, 1.0,
                       source=AnalysisTask.Source.USER, requested_by='alice')
        contributors.reset_cache()
        alias = settings.ATOMICDB_DATABASE_ALIAS
        with CaptureQueriesContext(connections[alias]) as queries:
            response = self.client.get('/atomicdb/user/alice/')
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_three_rows_and_twelve_rows_cost_the_same(self):
        three = self._cost(3)

        twelve = self._cost(12)

        self.assertEqual(three, twelve)


class HeaderCacheTests(TestCase):
    """El enlace nuevo de la cabecera NO puede filtrarse entre cuentas.

    ``map`` lleva ``cache_page`` y solo esta a salvo porque VARIA POR COOKIE
    (§ urls).  El enlace ademas apunta a ``/atomicdb/me/``, que es el mismo
    destino para todo el mundo: aunque una capa de cache aguas abajo se
    equivocara, el href no lleva el nombre de nadie.
    """

    def setUp(self):
        contributors.reset_cache()
        worker_account('alice')
        worker_account('bob')

    def _signed_in(self, username):
        client = Client()
        client.login(username=username, password='pw')
        return client

    def test_a_cached_page_never_serves_one_account_to_another(self):
        alice, bob = self._signed_in('alice'), self._signed_in('bob')

        # La primera visita de cada uno calienta SU entrada; la segunda es la
        # que puede venir del cache, y es la que se compara.
        alice.get('/atomicdb/map/')
        bob.get('/atomicdb/map/')
        mine = alice.get('/atomicdb/map/').content.decode()
        theirs = bob.get('/atomicdb/map/').content.decode()

        self.assertIn('>alice</a>', mine)
        self.assertNotIn('>bob</a>', mine)
        self.assertIn('>bob</a>', theirs)
        self.assertNotIn('>alice</a>', theirs)

    def test_an_anonymous_cached_body_does_not_become_a_session(self):
        anonymous = Client()
        anonymous.get('/atomicdb/map/')
        cached = anonymous.get('/atomicdb/map/').content.decode()

        mine = self._signed_in('alice').get('/atomicdb/map/').content.decode()

        self.assertIn('Sign in', cached)
        self.assertNotIn('alice', cached)
        self.assertIn('>alice</a>', mine)

    def test_the_header_link_is_the_same_destination_for_everyone(self):
        alice = self._signed_in('alice').get('/atomicdb/map/').content.decode()
        bob = self._signed_in('bob').get('/atomicdb/map/').content.decode()

        for body in (alice, bob):
            self.assertIn('class="identity-user" href="/atomicdb/me/"', body)
