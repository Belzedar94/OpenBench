"""El ranking de contribuidores de la PORTADA: quien ha buscado que.

Modulo aparte de ``test_profile`` a proposito: alli el sujeto es la pagina de
UNA persona (que existe, que ensena, que no filtra), y aqui es una seccion de
la portada, que es publica, cacheada y compartida.  Las dos comparten los
agregados de ``contributors``, asi que este fichero reutiliza los ayudantes de
fixture del otro en vez de escribir una segunda copia que podria irse
desviando.

Lo que se fija: que el orden es por nodos con empates COMPARTIDOS, que las dos
ventanas pueden discrepar y aun asi salir bien las dos, que una maquina sin
cuenta cuenta para la flota y para nadie mas, que la portada no gana ni una
consulta por contribuidor, y que la tabla no empuja el telefono fuera de la
pantalla.
"""

import pathlib
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import contributors, metrics
from .models import AnalysisTask, WorkerPing
from .test_profile import _child, _completed, _ping, _reading
from .testing import TestCase, worker_account


def _boards():
    """Las dos tablas con el snapshot recien calculado: 24h y all-time."""
    contributors.reset_cache()
    return contributors.leaderboard()['contrib_boards']


def _names(board):
    return [row['name'] for row in board['rows']]


def _places(board):
    return [row['rank'] for row in board['rows']]


def _searched(name, nodes, ago_hours=1, uci='a2a3'):
    """``name`` pone ``nodes`` con su maquina, hace ``ago_hours`` horas."""
    machine = f'{name}-box-atomicdb'
    if not WorkerPing.objects.filter(machine=machine).exists():
        _ping(machine, name)
    return _completed(_child(uci), machine, nodes, 1.0, ago_hours=ago_hours)


class OrderTests(TestCase):
    """Por nodos, y los empates comparten puesto igual que en la pagina."""

    def setUp(self):
        contributors.reset_cache()

    def test_the_order_is_by_engine_nodes(self):
        _searched('alice', 9_000_000)
        _searched('bob', 3_000_000)
        _searched('carol', 5_000_000)

        day, life = _boards()

        self.assertEqual(_names(day), ['alice', 'carol', 'bob'])
        self.assertEqual(_names(life), ['alice', 'carol', 'bob'])
        self.assertEqual(_places(day), [1, 2, 3])

    def test_a_tie_shares_its_place_and_the_next_one_skips(self):
        # Dos terceros no existen si hay dos segundos: el siguiente es CUARTO.
        _searched('alice', 9_000_000)
        _searched('bob', 3_000_000)
        _searched('carol', 3_000_000)
        _searched('dave', 1_000_000)

        day, _life = _boards()

        self.assertEqual(_names(day), ['alice', 'bob', 'carol', 'dave'])
        self.assertEqual(_places(day), [1, 2, 2, 4])

    def test_a_tie_gets_the_same_medal_and_nobody_gets_the_next_one(self):
        _searched('alice', 9_000_000)
        _searched('bob', 3_000_000)
        _searched('carol', 3_000_000)
        _searched('dave', 1_000_000)

        day, _life = _boards()

        self.assertEqual([row['medal'] for row in day['rows']],
                         ['first', 'second', 'second', ''])

    def test_the_place_matches_what_the_personal_page_says(self):
        # El mismo numero en los dos sitios o uno de los dos miente.
        _searched('alice', 9_000_000)
        _searched('bob', 3_000_000)
        _searched('carol', 3_000_000)

        day, _life = _boards()
        contributors.reset_cache()

        by_name = {row['name']: row['rank'] for row in day['rows']}
        for name, place in by_name.items():
            self.assertEqual(contributors.present(name)['rank_24h'],
                             {'rank': place, 'of': 3})

    def test_only_ten_rows_and_an_honest_remainder(self):
        for index in range(12):
            _searched(f'runner{index:02d}', (index + 1) * 1_000_000)

        day, _life = _boards()

        self.assertEqual(len(day['rows']), contributors.LEADERBOARD_ROWS)
        self.assertEqual(day['more'], 2)
        self.assertEqual(day['accounts'], 12)
        # Salen los DIEZ mejores, no los diez primeros que llegaron.
        self.assertEqual(_names(day)[0], 'runner11')
        self.assertNotIn('runner00', _names(day))


class WindowTests(TestCase):
    """Las dos ventanas contestan preguntas distintas y pueden discrepar."""

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()
        # alice lleva meses; bob ha encendido su maquina hoy.
        _searched('alice', 20_000_000, ago_hours=24 * 5)
        _searched('alice', 1_000_000)
        _searched('bob', 6_000_000)

    def test_the_two_windows_can_put_different_people_first(self):
        day, life = _boards()

        self.assertEqual(_names(day), ['bob', 'alice'])
        self.assertEqual(_names(life), ['alice', 'bob'])

    def test_and_the_page_shows_both_orders_without_mixing_them(self):
        body = self.client.get('/atomicdb/').content.decode()

        after_day, rest = (body.split('Last 24 hours', 1)[1]
                           .split('All time', 1))
        after_life = rest.split('id="campaigns"', 1)[0]
        self.assertLess(after_day.index('>bob</a>'),
                        after_day.index('>alice</a>'))
        self.assertLess(after_life.index('>alice</a>'),
                        after_life.index('>bob</a>'))

    def test_a_window_with_nothing_in_it_says_so(self):
        AnalysisTask.objects.all().update(
            completed=timezone.now() - timedelta(days=5))

        day, life = _boards()

        self.assertEqual(day['rows'], [])
        self.assertEqual(_names(life), ['alice', 'bob'])
        self.assertIn('last 24 hours', day['empty'])


class AttributionTests(TestCase):
    """Quien no ha buscado no sale, y lo que no es de nadie no se regala."""

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()

    def test_a_worker_that_never_searched_is_not_on_the_board(self):
        _ping('idle-box-atomicdb', 'carol')
        _searched('alice', 4_000_000)

        day, life = _boards()

        self.assertEqual(_names(day), ['alice'])
        self.assertEqual(_names(life), ['alice'])

    def test_a_machine_with_no_account_counts_only_for_the_fleet(self):
        # Sin ``WorkerPing`` no hay dueno conocido: sus nodos son reales y
        # entran en el denominador, pero atribuirselos a alguien seria
        # inventar (§ contributors._by_owner).
        _searched('alice', 3_000_000)
        _completed(_child('b2b3'), 'orphan-box-atomicdb', 1_000_000, 1.0)

        day, _life = _boards()

        self.assertEqual(_names(day), ['alice'])
        self.assertEqual(day['fleet_h'], '4.0M')
        self.assertEqual(day['unclaimed_h'], '1.0M')
        # La cuota se mide contra la flota entera: 3 de 4, no 3 de 3.
        self.assertEqual(day['rows'][0]['share'], 75.0)

    def test_a_real_contribution_is_never_written_as_a_round_zero(self):
        # Un contribuidor pequeno al lado de una flota enorme: su cuota
        # redondea a cero, pero sus nodos no son cero y la pagina no puede
        # decir que si.
        _searched('whale', 12_000_000_000_000)
        _searched('minnow', 123_000_000, uci='b2b3')

        day, _life = _boards()
        body = _reading(self.client.get('/atomicdb/').content.decode())

        minnow = [row for row in day['rows'] if row['name'] == 'minnow'][0]
        self.assertEqual(minnow['share'], 0.0)
        self.assertEqual(minnow['share_h'], '<0.1')
        self.assertIn('<0.1% of the fleet', body)
        self.assertNotIn('0.0', [row['share_h'] for row in day['rows']])

    def test_with_every_machine_claimed_there_is_nothing_left_over(self):
        _searched('alice', 3_000_000)
        _searched('bob', 1_000_000)

        day, _life = _boards()

        self.assertEqual(day['unclaimed_h'], '')
        self.assertEqual(day['rows'][0]['share'], 75.0)

    def test_the_page_says_out_loud_what_belongs_to_nobody(self):
        _searched('alice', 3_000_000)
        _completed(_child('b2b3'), 'orphan-box-atomicdb', 1_000_000, 1.0)

        body = _reading(self.client.get('/atomicdb/').content.decode())

        self.assertIn('1.0M of them by machines with no account', body)


class PageTests(TestCase):
    """La seccion en la portada: enlaces, estado vacio y vecinos intactos."""

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()

    def test_a_name_links_to_the_page_that_account_already_has(self):
        worker_account('alice')
        _searched('alice', 4_000_000)

        body = self.client.get('/atomicdb/').content.decode()

        self.assertIn('href="/atomicdb/user/alice/"', body)
        self.assertEqual(
            self.client.get('/atomicdb/user/alice/').status_code, 200)

    def test_a_name_that_needs_quoting_still_links_to_its_page(self):
        _searched('a b+c', 4_000_000)

        body = self.client.get('/atomicdb/').content.decode()

        self.assertIn('href="/atomicdb/user/a%20b%2Bc/"', body)

    def test_the_front_page_survives_with_no_contributors_at_all(self):
        response = self.client.get('/atomicdb/')

        self.assertEqual(response.status_code, 200)
        body = _reading(response.content.decode())
        self.assertIn('No worker has searched anything yet', body)
        # Sin nadie no se pintan dos tablas vacias, solo la frase.
        self.assertNotIn('Last 24 hours', body)

    def test_the_numbers_are_the_ones_that_were_searched(self):
        _searched('alice', 2_400_000_000)
        _searched('alice', 100_000_000, uci='b2b3')
        _searched('bob', 500_000_000)

        body = _reading(self.client.get('/atomicdb/').content.decode())

        # Los 2.5B de alice y los 3.0B de la flota, en la misma escala que el
        # resto del sitio.
        self.assertIn('2.50B', body)
        self.assertIn('2 accounts · 3.00B nodes searched by the whole fleet',
                      body)

    def test_the_section_sits_between_the_tree_and_the_campaigns(self):
        _searched('alice', 4_000_000)

        body = self.client.get('/atomicdb/').content.decode()

        for marker in ('Now analyzing', 'Up next', 'First moves',
                       'Proposed campaigns', 'Milestones'):
            self.assertIn(marker, body)
        here = body.index('id="contributors"')
        self.assertLess(body.index('First moves'), here)
        self.assertLess(here, body.index('id="campaigns"'))


class MobileOverflowTests(TestCase):
    """Con datos REALES la tabla no cabe en 375px: tiene que desplazarse ella.

    La leccion es de la pagina de contribuidor, donde la tabla de maquinas
    medio 492px de min-content y empujaba el body entero fuera de la pantalla.
    El peor caso aqui es el mismo: nombre de cuenta largo y sin espacios, y
    seis cifras en las dos columnas numericas.
    """

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()

    def test_each_board_scrolls_inside_its_own_container(self):
        _searched('belzedar-ryzen-9950x3d-node', 987_654_321)
        _searched('a-very-long-contributor-name', 123_456_789)

        body = self.client.get('/atomicdb/').content.decode()

        # Una por ventana, y siempre POR FUERA de su tabla.
        self.assertEqual(body.count('contrib-scroll'), 2)
        self.assertLess(body.index('contrib-scroll'), body.index('lb-table'))

    def test_the_container_really_scrolls(self):
        css = (pathlib.Path(settings.BASE_DIR) / 'atomicdb' / 'static'
               / 'atomicdb' / 'atomicdb.css').read_text(encoding='utf-8')

        block = css.split('.contrib-scroll {', 1)[1].split('}', 1)[0]

        self.assertIn('overflow-x: auto', block)

    def test_a_long_account_name_can_break_instead_of_pushing(self):
        css = (pathlib.Path(settings.BASE_DIR) / 'atomicdb' / 'static'
               / 'atomicdb' / 'atomicdb.css').read_text(encoding='utf-8')

        block = css.split('.lb-name {', 1)[1].split('}', 1)[0]

        self.assertIn('overflow-wrap: anywhere', block)
        self.assertIn('max-width: 100%', block)

    def test_the_two_boards_stack_before_they_overflow(self):
        # ``min(22rem, 100%)``: pide 22rem por columna, pero nunca mas ancho
        # del que hay.  Sin el ``min()`` una pantalla de 320px se lleva una
        # columna de 352px y el body entero scrollea en horizontal.
        css = (pathlib.Path(settings.BASE_DIR) / 'atomicdb' / 'static'
               / 'atomicdb' / 'atomicdb.css').read_text(encoding='utf-8')

        block = css.split('.lb-grid {', 1)[1].split('}', 1)[0]

        self.assertIn('minmax(min(22rem, 100%), 1fr)', block)


class QueryCostTests(TestCase):
    """Lo que cuesta la PORTADA no puede depender de cuanta gente contribuye.

    El ranking se sirve del snapshot compartido de flota, que son dos GROUP BY
    y una lectura de pings para todo el sitio: doce contribuidores se leen en
    las mismas tres consultas que tres.  Si alguien anadiera una consulta por
    fila — el nombre de la cuenta, su ultimo heartbeat — este test lo dice.
    """

    def setUp(self):
        contributors.reset_cache()
        self.client = Client()

    def _grow(self, first, last):
        for index in range(first, last):
            _searched(f'runner{index:02d}', (index + 1) * 1_000_000)

    def _cost(self):
        """Consultas de una portada con las TRES caches en frio.

        La de pagina (15s), el snapshot de flota (60s) y la de medidores (30s,
        que ademas es de proceso y no de Django): sin vaciar las tres, la
        segunda medida saldria mas barata por el calendario y no por el codigo.
        """
        cache.clear()
        contributors.reset_cache()
        metrics.reset_metrics_cache()
        alias = settings.ATOMICDB_DATABASE_ALIAS
        with CaptureQueriesContext(connections[alias]) as queries:
            response = self.client.get('/atomicdb/')
        self.assertEqual(response.status_code, 200)
        return len(queries), response.content.decode()

    def test_three_contributors_and_twelve_cost_the_same(self):
        self._grow(0, 3)
        three, _body = self._cost()

        self._grow(3, 12)
        twelve, body = self._cost()

        self.assertEqual(three, twelve)
        # Y no por servir menos: la segunda portada trae la tabla llena.
        self.assertIn('and 2 more', body)
