"""La linea que dice en que va la peticion de analisis de esta posicion.

EL FALLO QUE ARREGLA, medido el 31-jul: alguien pidio 10B con el selector de
profundidad, la tarea se creo a las 05:50:13, un worker la cogio un segundo
despues y a los 9,48M nps de esa maquina la busqueda tardaba 17-18 minutos.
Todo iba bien.  La pagina no dijo nada en esos dieciocho minutos, asi que el
que pidio creyo que no se habia analizado.

Lo que se fija aqui, en orden de importancia:

1. Que NO se promete un tiempo que no se puede sostener.  Sin velocidad
   reportada, con una lectura vieja o con un arriendo que lleva minutos callado,
   la linea dice lo que sabe y NINGUN numero.  Es la mitad de los tests de este
   fichero a proposito: un contador inventado seria peor que la pagina muda de
   antes, porque la muda no mentia.
2. Que sin tarea viva no se pinta nada — ni linea, ni hueco.
3. Que la cifra de cola por delante es la MISMA que devuelve el recibo del
   click, porque la calcula la misma funcion.
4. Y que esto no vuelve a subir el coste del explorador: una consulta para
   saber que no hay nada que decir, dos cuando lo hay, y ni una llamada mas al
   movegen (§ test_explore_performance, que es donde vive ese presupuesto).
"""

import re
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import community_names, ingest, live_request, logic, openings, views
from .models import AnalysisTask, Position, WorkerPing
from .test_explore_performance import CACHE_FOR_TESTS, build_explorer_fixture
from .testing import TestCase

# El texto de la linea, tal y como sale del minificador (que reordena
# atributos, asi que no se asume ningun orden).
_TEXT = re.compile(r'id="livereq-text">([^<]*)</span>')
_ELEMENT = re.compile(r'<p class="livereq"([^>]*)>')

TOP_RUNG = ingest.REQUEST_BUDGET_LADDER[-1]
# La maquina del caso real: 9,48M nps.  Con 10B eso son 1054s, 17,6 minutos.
MEASURED_NPS = 9_481_948


def _position(fen=None):
    return ingest.get_or_create_position(fen or logic.start_fen())


def _other_position(nth=0):
    """Otra posicion, para llenar la cola sin tocar la que se esta mirando."""
    start = logic.start_fen()
    return _position(logic.apply_move(start, logic.legal_moves(start)[nth]))


def _generation(pos):
    generation = pos.visits
    while AnalysisTask.objects.filter(position=pos,
                                      generation=generation).exists():
        generation += 1
    return generation


def _waiting(pos, budget=TOP_RUNG, requested_by='lesha', source='USER'):
    return AnalysisTask.objects.create(
        position=pos, generation=_generation(pos), budget_nodes=budget,
        source=source, requested_by=requested_by)


def _searching(pos, machine='box0#1', budget=TOP_RUNG, started_ago=0,
               silent_for=0):
    """Una tarea arrendada hace ``started_ago`` segundos.

    ``silent_for`` es la edad del ULTIMO heartbeat, que es otra cosa que la del
    arriendo: un worker sano late cada 60s durante toda la busqueda, asi que lo
    normal es un arriendo viejo con un latido reciente.  Los dos viejos a la
    vez es justamente la forma que tiene un worker caido.
    """
    now = timezone.now()
    beat = now - timedelta(seconds=silent_for)
    return AnalysisTask.objects.create(
        position=pos, generation=_generation(pos), budget_nodes=budget,
        source='USER', requested_by='lesha',
        state=AnalysisTask.TState.LEASED, machine=machine,
        leased_at=now - timedelta(seconds=started_ago),
        lease_heartbeat_at=beat)


def _ping(machine='box0#1', user='lesha', nps=MEASURED_NPS, nps_age=0):
    """Un slot de worker con su ultima lectura de velocidad.

    ``last_seen`` es ``auto_now``: se pone con un UPDATE o el create lo pisa.
    """
    ping = WorkerPing.objects.create(
        machine=machine, user=user, threads=16, last_nps=nps,
        nps_updated=timezone.now() - timedelta(seconds=nps_age))
    WorkerPing.objects.filter(pk=ping.pk).update(last_seen=timezone.now())
    return ping


def _line(response):
    """El texto que la pagina pinta en la linea de estado, o ``None``.

    ``None`` es "la pagina no pinta linea": el elemento viene vacio y
    ``hidden``, que no ocupa sitio.  Se comprueba el HTML y no el contexto
    porque lo que importa es lo que ve una persona.
    """
    body = response.content.decode()
    element = _ELEMENT.search(body)
    assert element is not None, 'the explorer lost the live-request element'
    text = _TEXT.search(body).group(1).strip()
    if 'hidden' in element.group(1):
        assert text == '', f'a hidden line is still saying {text!r}'
        return None
    return text


def _has_a_number(text):
    """Lleva este texto una cifra de TIEMPO dentro?

    El presupuesto ("10.00B nodes") y la cola ("3 requests ahead") tambien son
    numeros, asi que se busca lo unico que aqui es una promesa: minutos u horas.
    """
    return bool(re.search(r'\d+(\.\d+)?\s*(min|h)\b', text))


class WaitingTests(TestCase):
    """Esperando en la cola: cuanta cola, dicha con el numero que ya existia."""

    def setUp(self):
        self.pos = _position()
        self.url = f'/atomicdb/explore/{self.pos.key}/'

    def test_the_first_in_line_says_it_is_next(self):
        _waiting(self.pos)

        line = _line(Client().get(self.url))

        self.assertIn('Waiting for a worker', line)
        self.assertIn('next up', line)
        # Y dice lo que se compro, que es la mitad de la explicacion de por que
        # una peticion tarda: 10B no es lo mismo que 128M.
        self.assertIn('10.00B nodes', line)

    def test_a_queue_in_front_is_counted(self):
        for nth in range(3):
            _waiting(_other_position(nth))
        _waiting(self.pos)

        self.assertIn('3 requests ahead', _line(Client().get(self.url)))

    def test_one_in_front_is_not_pluralised(self):
        _waiting(_other_position())
        _waiting(self.pos)

        self.assertIn('1 request ahead', _line(Client().get(self.url)))

    def test_the_line_says_what_the_click_receipt_says(self):
        """Dos sitios, un solo numero: lo calcula la misma funcion."""
        for nth in range(2):
            _waiting(_other_position(nth))
        _waiting(self.pos)

        receipt = views._user_queue_ahead(self.pos)
        line = _line(Client().get(self.url))

        self.assertEqual(receipt, 2)
        self.assertIn(f'{receipt} requests ahead', line)

    def test_a_request_promoted_out_of_the_user_band_claims_no_place(self):
        """Una tarea de grado peticion degradada a AUTO cobra la ULTIMA.

        ``enqueue_unexplored_children`` puede degradar a AUTO una tarea que
        nacio de un click (§ ingest.notification_deserved).  Sigue siendo de
        una persona y sigue contandose, pero su sitio en la cola no es el que
        cuenta la cifra de la banda de visitante, asi que no se inventa uno.
        """
        _waiting(self.pos, source='AUTO')

        line = _line(Client().get(self.url))

        self.assertIn('visitor requests are served first', line)
        self.assertNotIn('ahead', line)


class SearchingTests(TestCase):
    """Buscandose ahora: que maquina, y cuanto falta cuando se puede decir."""

    def setUp(self):
        self.pos = _position()
        self.url = f'/atomicdb/explore/{self.pos.key}/'

    def test_a_running_search_says_the_machine_and_the_time_left(self):
        _searching(self.pos, started_ago=0)
        _ping()

        line = _line(Client().get(self.url))

        self.assertIn('Searching now on box0#1', line)
        self.assertIn('10.00B nodes', line)
        # 10B a 9,48M nps son 1054s: 17,6 minutos, dichos como 18.
        self.assertIn('about 18 min left', line)

    def test_the_time_left_shrinks_while_the_search_runs(self):
        _searching(self.pos, started_ago=600)
        _ping()

        # Diez minutos dentro quedan 7,6: se dice 8, no 17,6 menos nada.
        self.assertIn('about 8 min left', _line(Client().get(self.url)))

    def test_a_search_past_its_budget_says_it_is_about_to_land(self):
        _searching(self.pos, started_ago=1200)
        _ping()

        line = _line(Client().get(self.url))

        self.assertIn('finishing any moment now', line)
        # Y no un "0 min" ni un numero negativo disfrazado.
        self.assertFalse(_has_a_number(line), line)

    def test_a_slow_machine_gets_hours_and_not_two_hundred_minutes(self):
        _searching(self.pos, started_ago=0)
        _ping(nps=1_000_000)

        # 10B a 1M nps son 2,8 horas.
        self.assertIn('about 2.8 h left', _line(Client().get(self.url)))


class NoPromiseTests(TestCase):
    """La regla entera: sin datos para sostenerla, la cuenta atras NO SE PINTA.

    Cada test de aqui es un camino por el que un numero inventado podria
    colarse en la pagina.  Todos terminan igual: se dice lo que se sabe y nada
    mas.
    """

    def setUp(self):
        self.pos = _position()
        self.url = f'/atomicdb/explore/{self.pos.key}/'

    def _line(self):
        return _line(Client().get(self.url))

    def test_a_machine_that_has_not_reported_speed_yet_promises_nothing(self):
        """``last_nps`` 0 es lo que hay en produccion en varias filas."""
        _searching(self.pos, started_ago=5)
        _ping(nps=0)

        line = self._line()

        self.assertIn('Searching now on box0#1', line)
        self.assertIn('no speed reported yet', line)
        self.assertFalse(_has_a_number(line), line)

    def test_a_machine_with_no_ping_at_all_promises_nothing(self):
        _searching(self.pos, started_ago=5)

        line = self._line()

        self.assertIn('Searching now on box0#1', line)
        self.assertFalse(_has_a_number(line), line)

    def test_an_old_speed_reading_promises_nothing(self):
        """El heartbeat sigue fresco pero la velocidad es de hace diez minutos.

        Un slot puede seguir latiendo con una lectura de nps vieja pegada
        (§ views.api_heartbeat solo la refresca con tarea en curso), y una
        cuenta atras construida sobre ella es una cuenta atras sobre una
        medida que el sitio ya no considera viva (§ metrics.LIVE_SECONDS).
        """
        _searching(self.pos, started_ago=5)
        _ping(nps_age=600)

        self.assertFalse(_has_a_number(self._line()), self._line())

    def test_a_lease_that_stopped_reporting_promises_nothing(self):
        """El worker cayo: el arriendo sigue puesto y el motor ya no existe."""
        _searching(self.pos, started_ago=400, silent_for=360)
        _ping()

        line = self._line()

        self.assertIn('Sent to box0#1', line)
        self.assertIn('nothing reported for 6 min', line)
        self.assertNotIn('left', line)

    def test_a_lease_past_its_deadline_says_it_goes_back_to_the_queue(self):
        """Y ahi ya no es un silencio: el servidor la va a reciclar."""
        _searching(self.pos, started_ago=1800, silent_for=1500)
        _ping()

        line = self._line()

        self.assertIn('nothing reported for 25 min', line)
        self.assertIn('goes back in the queue', line)
        self.assertNotIn('left', line)

    def test_the_deadline_is_the_servers_own_lease_window(self):
        """Ese borde no es un numero de esta linea: es el del servidor.

        ``views.LEASE_MINUTES`` es lo que decide cuando un arriendo callado
        vuelve a la cola de verdad (§ views.api_lease), asi que la frase que lo
        anuncia se mueve con el.  Escrito a mano seria una promesa que caduca
        el dia que cambie el numero real.
        """
        window = views.LEASE_MINUTES * 60
        just_inside = _position(logic.apply_move(
            logic.start_fen(), logic.legal_moves(logic.start_fen())[7]))
        _searching(self.pos, silent_for=window + 60)
        _searching(just_inside, silent_for=window - 60)

        past = self._line()
        inside = _line(Client().get(f'/atomicdb/explore/{just_inside.key}/'))

        self.assertIn('goes back in the queue', past)
        self.assertNotIn('goes back in the queue', inside)

    def test_a_lease_still_reporting_is_not_called_quiet(self):
        """El otro lado del borde: dos minutos de silencio son un ciclo lento."""
        _searching(self.pos, started_ago=200, silent_for=120)
        _ping()

        line = self._line()

        self.assertIn('Searching now on box0#1', line)
        self.assertIn('left', line)

    def test_no_countdown_is_ever_borrowed_from_another_machine(self):
        """La flota entera puede ir rapidisima: no dice nada de ESTA maquina.

        Es la caida que NO se implemento a proposito.  Una mediana de la flota
        daria un numero con la misma pinta de verdad que uno medido, y en una
        maquina diez veces mas lenta seria una mentira redonda.
        """
        _searching(self.pos, machine='slow#0', started_ago=5)
        _ping(machine='fast#0', user='wolfram', nps=200_000_000)
        _ping(machine='fast#1', user='wolfram', nps=180_000_000)

        line = self._line()

        self.assertIn('Searching now on slow#0', line)
        self.assertFalse(_has_a_number(line), line)


class SilenceTests(TestCase):
    """Sin nada que decir no se dice nada: ni linea, ni hueco, ni consulta."""

    def setUp(self):
        self.pos = _position()
        self.url = f'/atomicdb/explore/{self.pos.key}/'

    def test_a_position_with_no_live_task_paints_nothing(self):
        self.assertIsNone(_line(Client().get(self.url)))

    def test_a_finished_task_paints_nothing(self):
        AnalysisTask.objects.create(
            position=self.pos, generation=_generation(self.pos),
            budget_nodes=TOP_RUNG, source='USER',
            state=AnalysisTask.TState.COMPLETED, completed=timezone.now())

        self.assertIsNone(_line(Client().get(self.url)))

    def test_a_task_on_another_position_is_not_this_one(self):
        _waiting(_other_position())

        self.assertIsNone(_line(Client().get(self.url)))

    def test_a_coverage_probe_is_not_somebodys_request(self):
        """8M por la banda FILL: nadie la pidio y nadie la esta esperando."""
        _waiting(self.pos, budget=8_000_000, source='FILL', requested_by='')

        self.assertIsNone(_line(Client().get(self.url)))

    def test_a_solved_position_says_nothing_and_asks_nothing(self):
        """Con veredicto la pagina cuenta otra cosa, y esto no cuesta ni una
        consulta: el boton de peticion ni existe ahi."""
        _searching(self.pos)
        Position.objects.filter(key=self.pos.key).update(status='WHITE_WIN',
                                                         closure='MINIMAX')
        pos = Position.objects.get(key=self.pos.key)

        with self.assertNumQueries(0, using=settings.ATOMICDB_DATABASE_ALIAS):
            self.assertIsNone(live_request.summary(pos))


class RoundingTests(TestCase):
    """Redondeo con criterio: una estimacion no se pinta con decimales falsos."""

    def test_a_minute_and_a_half_is_not_two_decimal_places(self):
        self.assertEqual(live_request._left_text(1058), 'about 18 min left')

    def test_under_a_minute_says_no_number_at_all(self):
        self.assertEqual(live_request._left_text(59),
                         'finishing any moment now')
        self.assertEqual(live_request._left_text(0),
                         'finishing any moment now')

    def test_the_minute_below_the_hour_is_still_minutes(self):
        self.assertEqual(live_request._left_text(7139), 'about 119 min left')

    def test_two_hours_and_over_are_hours(self):
        self.assertEqual(live_request._left_text(9000), 'about 2.5 h left')

    def test_an_impossible_estimate_is_no_estimate(self):
        """Medio dia para 10B no es una maquina lenta: es una lectura rota."""
        pos = _position()
        task = _searching(pos, started_ago=0)

        self.assertIsNone(live_request._seconds_left(task, 100, timezone.now()))

    def test_a_broken_reading_reaches_the_page_as_no_estimate(self):
        pos = _position()
        _searching(pos, started_ago=0)
        _ping(nps=100)

        line = _line(Client().get(f'/atomicdb/explore/{pos.key}/'))

        self.assertIn('no time estimate yet', line)
        self.assertFalse(_has_a_number(line), line)


class EndpointTests(TestCase):
    """El sondeo: las mismas palabras que la pagina, y un final claro."""

    def setUp(self):
        self.pos = _position()
        self.url = f'/atomicdb/api/live-request/{self.pos.key}/'

    def test_the_poll_says_exactly_what_the_page_says(self):
        _searching(self.pos, started_ago=30)
        _ping()

        body = Client().get(self.url).json()
        page = _line(Client().get(f'/atomicdb/explore/{self.pos.key}/'))

        self.assertTrue(body['live'])
        self.assertEqual(body['text'], page)
        self.assertEqual(body['chip'], 'hot')

    def test_a_waiting_request_polls_as_waiting(self):
        _waiting(self.pos)

        body = Client().get(self.url).json()

        self.assertTrue(body['live'])
        self.assertIn('Waiting for a worker', body['text'])
        self.assertEqual(body['chip'], 'cold')

    def test_nothing_in_flight_is_how_the_poll_stops(self):
        self.assertEqual(Client().get(self.url).json(), {'live': False})

    def test_a_position_that_does_not_exist_is_not_a_finished_task(self):
        response = Client().get('/atomicdb/api/live-request/' + 'ff' * 32 + '/')

        self.assertEqual(response.status_code, 404)
        self.assertIn('error', response.json())

    def test_the_poll_is_two_statements(self):
        _searching(self.pos, started_ago=30)
        _ping()
        alias = settings.ATOMICDB_DATABASE_ALIAS

        # La posicion, la tarea y la velocidad de su maquina.  Ni una por fila
        # de nada: esto lo pide una pestana abierta cada veinte segundos.
        with self.assertNumQueries(3, using=alias):
            Client().get(self.url)

    def test_the_empty_poll_is_the_cheapest_of_all(self):
        alias = settings.ATOMICDB_DATABASE_ALIAS

        with self.assertNumQueries(2, using=alias):
            Client().get(self.url)


@override_settings(CACHES=CACHE_FOR_TESTS)
class CostTests(TestCase):
    """Lo que esta linea tiene PROHIBIDO costarle al explorador.

    La pagina bajo de 0,47s a 0,08s por render y no puede volver a subir
    (§ test_explore_performance).  Los numeros no se escriben a mano: se
    comparan contra el MISMO render con la linea apagada, asi que el test
    sigue siendo cierto el dia que el resto de la pagina cambie de coste.
    """

    def setUp(self):
        cache.clear()
        self.leaf, self.route, _legal = build_explorer_fixture()
        self.url = f'/atomicdb/explore/{self.leaf}/?play={self.route}'
        self.pos = Position.objects.get(key=self.leaf)
        Client().get(self.url)   # almacen de claves caliente, como en produccion

    def _queries(self, silent=False):
        # APAGAR ES APAGAR ``context``, que es lo que el explorador llama y lo
        # que engloba TODO lo que este modulo le cuesta a la pagina: la linea
        # de estado y el control de adelantar que cuelga de la misma lectura
        # (§ live_request.bump_control).  Con la narracion sola apagada, la
        # lectura de la tarea viva seguiria pagandose en las dos medidas y la
        # resta ya no mediria nada.  Las cifras que se comparan no cambian:
        # sin sesion el control no cuesta ni una sentencia.
        alias = settings.ATOMICDB_DATABASE_ALIAS
        with CaptureQueriesContext(connections[alias]) as counted:
            if silent:
                with mock.patch('atomicdb.live_request.context',
                                return_value={}):
                    response = Client().get(self.url)
            else:
                response = Client().get(self.url)
        self.assertEqual(response.status_code, 200)
        return len(counted.captured_queries)

    def test_finding_out_there_is_nothing_to_say_costs_one_statement(self):
        self.assertEqual(self._queries(), self._queries(silent=True) + 1)

    def test_a_waiting_request_costs_two(self):
        _waiting(self.pos)

        self.assertEqual(self._queries(), self._queries(silent=True) + 2)

    def test_a_running_search_costs_two(self):
        _searching(self.pos, started_ago=30)
        _ping()

        self.assertEqual(self._queries(), self._queries(silent=True) + 2)

    def _movegen(self):
        """Lo que cuesta de verdad un render: cuantas veces se monta posicion."""
        with mock.patch('atomicdb.logic.apply_move',
                        wraps=logic.apply_move) as applied, \
                mock.patch('atomicdb.logic.legal_moves',
                           wraps=logic.legal_moves) as generated, \
                mock.patch('atomicdb.openings.match_line',
                           wraps=openings.match_line) as replayed, \
                mock.patch('atomicdb.community_names.approved_map',
                           wraps=community_names.approved_map) as names:
            response = Client().get(self.url)
        self.assertEqual(response.status_code, 200)
        return (applied.call_count, generated.call_count,
                replayed.call_count, names.call_count)

    def test_the_line_does_not_ask_the_movegen_for_anything(self):
        """El presupuesto que fija test_explore_performance, intacto.

        Es la comprobacion que de verdad protege los 0,08s: la base son
        milisegundos y lo caro es PyFFish, asi que una linea de estado que
        montara una sola posicion mas seria el principio de la vuelta atras.
        """
        before = self._movegen()
        _searching(self.pos, started_ago=30)
        _ping()

        self.assertEqual(self._movegen(), before)
