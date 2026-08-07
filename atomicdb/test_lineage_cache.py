"""The line back to the root is computed once, not once per click.

The night a nightly batch and a traffic record landed on the same VPS, load
hit 34 and the explorer stopped being usable: every explore/goto render was
paying a reverse BFS to the root — up to 96 plies of window-function queries,
up to 1024 nodes inside a transposed component, ~300ms on a deep position —
to rebuild a breadcrumb that had not changed since the visitor's previous
click and almost never does.

What follows pins the mechanism, not a millisecond count: the walk runs once
per position per freshness window, the "deep line" marker survives the round
trip, and the live state of the position on screen (evals, move rows) is NOT
part of the deal — that must still be fresh on every render, warm cache or
not.

AND THE SECOND HALF, WHICH IS WHY THE CONSTANT CHANGED MEANING.  Caching it
was not enough: the visitor who arrived at the instant the entry expired paid
the whole walk anyway, and under the analysis bursts that was 10-26 seconds
(267 of them in seven hours, p50 15,1 s, p90 27,3 s).  So expiry stopped
being a wait.  ``LINEAGE_CACHE_SECONDS`` is now a FRESHNESS THRESHOLD over a
much longer hard life: past it the entry is still served, right away, and one
reader renews it in the background.  ``StaleLineageIsServedTests`` and
``StaleRouteLabelIsServedTests`` are that half.
"""

import time
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from . import ingest, logic, revalidate, views
from .models import Edge, Position
from .testing import TestCase, TransactionTestCase


CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-lineage-cache-tests',
    },
}


def _play(parent, uci):
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


def _chain(ucis):
    """Materialise a line from startpos and hand back its last position."""
    pos = ingest.get_or_create_position(logic.start_fen())
    for uci in ucis:
        pos = _play(pos, uci)
    return pos


def _counted_walk():
    """Patch the expensive half so a test can count how often it ran."""
    return mock.patch.object(views, '_walk_lines_to_root',
                             wraps=views._walk_lines_to_root)


def _aged(seconds):
    """Envejece lo guardado moviendo EL reloj de la cache, sin dormir.

    Es el reloj que escribio la marca de tiempo, asi que mover otro — o
    dormir — probaria otra cosa.  Un test de caducidad que duerme es un test
    que un dia falla en una maquina lenta y nadie sabe por que.
    """
    clock = time.time() + seconds
    return mock.patch.object(revalidate, '_now', lambda: clock)


class _Deferred:
    """El ejecutor de los tests: apunta el refresco y NO lo corre.

    Modela exactamente lo que hace el hilo de verdad — la respuesta sale
    ANTES de que el trabajo empiece — sin depender de un ``join`` ni de un
    ``sleep``, y deja contar cuantos refrescos se lanzaron, que es la mitad
    de lo que hay que demostrar aqui.
    """

    def __init__(self):
        self.jobs = []

    def __call__(self, work, name):
        self.jobs.append(work)

    def run_all(self):
        pending, self.jobs = self.jobs, []
        for work in pending:
            work()
        return len(pending)


@override_settings(CACHES=CACHE_FOR_TESTS)
class LineageCacheHitTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.target = _chain(['g1f3', 'g8f6', 'b1c3', 'b8c6'])

    def test_a_second_explore_render_does_not_walk_again(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        with _counted_walk() as walk:
            first = self.client.get(url)
            second = self.client.get(url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(walk.call_count, 1)

    def test_the_cached_breadcrumb_is_the_same_breadcrumb(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        first = self.client.get(url)
        second = self.client.get(url)

        self.assertTrue(first.context['line_from_root'])
        self.assertTrue(second.context['line_from_root'])
        self.assertEqual(
            [(token['num'], token['san']) for token in first.context['line']],
            [(token['num'], token['san']) for token in second.context['line']])
        self.assertEqual(second.context['line'][0]['san'], 'Nf3')

    def test_a_hit_and_a_miss_hand_back_the_same_shape(self):
        """Otherwise the difference only shows up under warm traffic."""
        miss_top, miss_line = views._line_to_root(self.target)
        hit_top, hit_line = views._line_to_root(self.target)

        self.assertEqual(type(miss_top), type(hit_top))
        self.assertEqual((miss_top.key, miss_top.fen),
                         (hit_top.key, hit_top.fen))
        self.assertEqual(miss_line, hit_line)

    def test_a_batch_walks_only_the_keys_the_cache_could_not_answer(self):
        """The home page asks for ~30 keys at a time and most repeat."""
        other = _chain(['e2e4', 'e7e6', 'd2d4'])
        views._line_labels_many([self.target.key])

        with _counted_walk() as walk:
            labels = views._line_labels_many([self.target.key, other.key])

        walk.assert_called_once()
        self.assertEqual(list(walk.call_args.args[0]), [other.key])
        self.assertEqual(set(labels), {self.target.key, other.key})

    def test_the_explorer_and_the_home_page_do_not_share_an_entry(self):
        """They walk different ply budgets; the same key can differ.

        The explorer resolves 64 plies and the home page 96.  One shared
        entry would let whichever page rendered first decide what the other
        one says about a line between those two depths.
        """
        views._lines_to_root([self.target.key], max_plies=64)

        with _counted_walk() as walk:
            views._lines_to_root([self.target.key], max_plies=96)

        self.assertEqual(walk.call_count, 1)

    def test_a_position_atomicdb_does_not_have_is_not_cached_as_absent(self):
        """`goto` materialises nodes; a two-minute "no such node" is a bug."""
        stranger = logic.key_of(logic.apply_move(logic.start_fen(), 'h2h4'))
        self.assertEqual(views._lines_to_root([stranger]), {})

        ingest.get_or_create_position(logic.apply_move(logic.start_fen(),
                                                       'h2h4'))

        self.assertEqual(set(views._lines_to_root([stranger])), {stranger})


@override_settings(CACHES=CACHE_FOR_TESTS)
class CappedMarkerSurvivesTheCacheTests(TestCase):
    """A capped walk must still read as DEEP after a cache round trip.

    ``lineage_capped`` reaches the renderer as an attribute glued onto the
    top position by the walk itself.  An ORM instance does not survive a
    cache, and neither would that marker if the payload were not explicit —
    the label would silently fall back to the orphan branch and the home page
    would show a headless "… Bb5 Kg2 Bc4 Bd7" again, but only once the cache
    was warm.
    """

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.deep = _chain(['a2a3', 'a7a6', 'b2b3', 'b7b6'])
        Position.objects.filter(key=self.deep.key).update(priority=9_000.0)

    def test_the_deep_line_label_is_identical_on_the_hit(self):
        with mock.patch.object(views, 'LINEAGE_SEARCH_MAX_NODES', 2), \
                _counted_walk() as walk:
            miss = views._line_labels(self.deep.key)
            hit = views._line_labels(self.deep.key)

        self.assertEqual(walk.call_count, 1)
        self.assertTrue(miss[0].startswith('deep line, ply ≥'), miss)
        self.assertEqual(miss, hit)

    def test_the_home_page_still_says_deep_line_when_the_cache_is_warm(self):
        # Two visitors, because the home page carries its own 15s
        # ``cache_page`` and a repeat visit would never reach the lineage
        # code at all.  Under gunicorn this is the ordinary case: a second
        # reader rendering the same rows the first one just resolved.
        other = Client()
        other.cookies['csrftoken'] = 'b' * 64

        with mock.patch.object(views, 'LINEAGE_SEARCH_MAX_NODES', 2), \
                _counted_walk() as walk:
            self.client.get('/atomicdb/')
            warm = other.get('/atomicdb/')

        self.assertEqual(walk.call_count, 1)
        row = next(entry for entry in warm.context['upnext']
                   if entry['key'] == self.deep.key)
        self.assertTrue(row['san'].startswith('deep line, ply ≥'), row['san'])
        self.assertFalse(row['san'].startswith('…'), row['san'])


@override_settings(CACHES=CACHE_FOR_TESTS)
class LiveStateIsNotCachedTests(TestCase):
    """Only the lineage. The position itself is read fresh, every render.

    The explorer exists to show current analysis.  Caching the breadcrumb is
    safe because a new edge can only make it shorter; caching what the engine
    currently thinks would be the explorer lying about its own subject.
    """

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.target = _chain(['g1f3', 'g8f6'])

    def test_a_new_eval_shows_on_the_next_render_with_the_line_still_cached(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        with _counted_walk() as walk:
            first = self.client.get(url)
            Position.objects.filter(key=self.target.key).update(eval_cp=777)
            second = self.client.get(url)

        # One walk across both renders: the breadcrumb really was served
        # from the cache on the second one...
        self.assertEqual(walk.call_count, 1)
        # ...and the number the visitor came for is still the current one.
        self.assertNotIn('+777cp', first.content.decode())
        self.assertIn('+777cp', second.content.decode())

    def test_a_new_child_move_row_shows_on_the_next_render(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        with _counted_walk() as walk:
            first = self.client.get(url)
            _play(self.target, 'b1c3')
            second = self.client.get(url)

        self.assertEqual(walk.call_count, 1)
        self.assertEqual(len(first.context['moves']), 0)
        self.assertEqual([move['uci'] for move in second.context['moves']],
                         ['b1c3'])

    def test_the_explore_response_is_still_never_cached_as_a_page(self):
        response = self.client.get(f'/atomicdb/explore/{self.target.key}/')

        self.assertNotIn('max-age', response.headers.get('Cache-Control', ''))


@override_settings(CACHES=CACHE_FOR_TESTS)
class LineageCacheTtlTests(TestCase):
    """Age is the only invalidation, so it has to be honoured and movable."""

    def setUp(self):
        cache.clear()
        self.target = _chain(['g1f3', 'g8f6'])

    def test_the_named_constant_is_the_freshness_threshold_not_the_backend_ttl(
            self):
        """EL INVARIANTE CAMBIO, Y ESTE TEST ES DONDE SE DICE.

        Antes ``LINEAGE_CACHE_SECONDS`` era literalmente el timeout que se le
        pasaba al backend: al vencer, la entrada DESAPARECIA y el siguiente
        render pagaba el paseo entero.  Eso es lo que producia los picos de
        10-26 s, asi que la constante dejo de ser eso.

        Ahora es el UMBRAL DE FRESCURA sobre una vida dura mayor: lo que llega
        al almacen es ``LINEAGE_CACHE_TTL_SECONDS``, y cruzar el umbral no
        borra nada — solo cambia quien paga el siguiente paseo, que pasa a ser
        un hilo de fondo en vez del visitante.  Las dos mitades se comprueban
        juntas a proposito: separadas, cambiar una y no la otra pasaria.
        """
        with mock.patch.object(views, 'LINEAGE_CACHE_SECONDS', 7), \
                mock.patch.object(views, 'LINEAGE_CACHE_TTL_SECONDS', 900), \
                mock.patch.object(views.cache, 'set_many',
                                  wraps=views.cache.set_many) as store:
            views._line_labels(self.target.key)

            # 1. Lo que llega al backend es la VIDA DURA, no el umbral.
            store.assert_called_once()
            self.assertEqual(store.call_args.args[1], 900)

            # 2. Y el umbral es donde se acaba la frescura: por debajo no se
            #    renueva nada, por encima se renueva sin dejar de servir.
            with _aged(6), _counted_walk() as fresh:
                views._line_labels(self.target.key)
            self.assertEqual(fresh.call_count, 0)

            executor = _Deferred()
            with _aged(8), mock.patch.object(revalidate, 'EXECUTOR', executor):
                served = views._line_labels(self.target.key)
            self.assertEqual(served[0], '1. Nf3 Nf6')
            self.assertEqual(len(executor.jobs), 1)

    def test_zero_seconds_turns_the_cache_off_entirely(self):
        """The kill switch: no reads, no writes, straight to the walk."""
        with mock.patch.object(views, 'LINEAGE_CACHE_SECONDS', 0), \
                mock.patch.object(views.cache, 'set_many') as store, \
                _counted_walk() as walk:
            views._line_labels(self.target.key)
            views._line_labels(self.target.key)

        self.assertEqual(walk.call_count, 2)
        store.assert_not_called()

    def test_an_expired_entry_is_walked_again(self):
        """"Caducada" es ahora que se acabo la VIDA DURA: ya no hay nada.

        Cruzar el umbral de frescura no deja al lector sin entrada — para eso
        esta el resto del fichero.  Esto es el otro extremo: cuando el
        almacen de verdad ya no la tiene, se vuelve a pasear, sincrono,
        porque no hay copia que servir.
        """
        with mock.patch.object(views, 'LINEAGE_CACHE_SECONDS', 300):
            views._line_labels(self.target.key)

        with mock.patch.object(views.cache, 'get_many', return_value={}), \
                _counted_walk() as walk:
            label = views._line_labels(self.target.key)

        self.assertEqual(walk.call_count, 1)
        self.assertEqual(label[0], '1. Nf3 Nf6')

    def test_the_default_freshness_is_short_enough_to_invalidate(self):
        self.assertGreater(views.LINEAGE_CACHE_SECONDS, 0)
        self.assertLessEqual(views.LINEAGE_CACHE_SECONDS, 300)

    def test_the_four_numbers_are_ordered_or_the_scheme_collapses(self):
        """Cada desigualdad es una propiedad, no una manida.

        Frescura < tope de edad: sin esa franja no hay ventana en la que
        servir lo viejo, y el vencimiento vuelve a ser una espera.  Tope de
        edad < vida dura: si el backend caduca antes que la guarda, quien
        decide es Redis y la guarda no llega a mirar nada.  Cerrojo < tope:
        un proceso muerto a mitad del refresco no puede dejar una entrada sin
        quien la renueve mas alla de su propia ventana.
        """
        self.assertLess(views.LINEAGE_CACHE_SECONDS,
                        views.LINEAGE_STALE_SECONDS)
        self.assertLess(views.LINEAGE_STALE_SECONDS,
                        views.LINEAGE_CACHE_TTL_SECONDS)
        self.assertLess(views.LINEAGE_REFRESH_LOCK_SECONDS,
                        views.LINEAGE_STALE_SECONDS)


@override_settings(CACHES=CACHE_FOR_TESTS)
class StaleLineageIsServedTests(TestCase):
    """Lo que el paseo de linaje tiene PROHIBIDO volver a hacer esperar.

    El incidente, con recibos: la entrada duraba 120 s y al vencer el
    siguiente render pagaba el paseo entero — hasta 96 plies de joins de
    ``atomicdb_edge`` compitiendo con los lotes de ``UPDATE ... reachable`` y
    con el autovacuum de la misma tabla.  Medido en siete horas: 267 eventos
    de 10-26 s, p50 15,1 s, p90 27,3 s, y huecos de 86-111 s entre ellos, que
    es exactamente el ritmo al que iba caducando esto.

    Lo que fijan estos tests, en orden de importancia:

    * que al cruzar el umbral se sirva la entrada VIEJA en vez de hacer
      esperar a nadie, y sin tocar la base;
    * que la renueve UNO, no todos los que lleguen mientras tanto;
    * que haya un tope: pasada cierta edad se pasea sincrono, porque servir
      un linaje de hace media hora es peor que esperar;
    * que un refresco que revienta suelte el turno y no se lleve nada por
      delante.
    """

    def setUp(self):
        cache.clear()
        self.target = _chain(['g1f3', 'g8f6'])
        self.refreshes = _Deferred()
        patch = mock.patch.object(revalidate, 'EXECUTOR', self.refreshes)
        patch.start()
        self.addCleanup(patch.stop)

    def _stale(self):
        return _aged(views.LINEAGE_CACHE_SECONDS + 1)

    def test_an_entry_past_the_freshness_threshold_is_served_not_walked(self):
        views._line_labels(self.target.key)

        with self._stale(), _counted_walk() as walk:
            label = views._line_labels(self.target.key)

        self.assertEqual(walk.call_count, 0)
        self.assertEqual(label[0], '1. Nf3 Nf6')

    def test_serving_the_aged_entry_costs_no_queries(self):
        """La prueba de que NADIE espera: cero consultas en la peticion.

        Es la afirmacion entera del cambio.  Antes, esta misma llamada —
        entrada recien vencida — era la que se comia el paseo de 10-26 s.
        """
        views._line_labels(self.target.key)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with self._stale(), CaptureQueriesContext(connection) as captured:
            views._line_labels(self.target.key)

        self.assertEqual(len(captured.captured_queries), 0)

    def test_the_refresh_is_launched_exactly_once_for_ten_readers(self):
        views._line_labels(self.target.key)

        with self._stale():
            for _ in range(10):
                views._line_labels(self.target.key)

        self.assertEqual(len(self.refreshes.jobs), 1)

    def test_the_refresh_leaves_a_fresh_entry_behind(self):
        views._line_labels(self.target.key)

        with self._stale():
            views._line_labels(self.target.key)
            with _counted_walk() as walk:
                self.assertEqual(self.refreshes.run_all(), 1)
            # El paseo lo pago el refresco...
            self.assertEqual(walk.call_count, 1)

            with _counted_walk() as after:
                label = views._line_labels(self.target.key)

        # ...y el siguiente lector vuelve a ser un acierto fresco: ni pasea
        # ni lanza otro refresco.
        self.assertEqual(after.call_count, 0)
        self.assertEqual(self.refreshes.jobs, [])
        self.assertEqual(label[0], '1. Nf3 Nf6')

    def test_the_refreshed_entry_is_the_line_of_now(self):
        """Servir lo viejo no puede convertirse en no volver a mirar nunca.

        Y de paso, el argumento entero del diseno en un solo caso: la arista
        nueva no vuelve FALSO el breadcrumb viejo, lo vuelve mas CORTO.  Lo
        que se servia — "no se de donde viene" — seguia siendo cierto en el
        instante en que se sirvio; el refresco trae la mejora, no una
        correccion.
        """
        root = ingest.get_or_create_position(logic.start_fen())
        # Una posicion cuya arista todavia no se ha materializado: el paseo no
        # llega a la raiz y el breadcrumb lo dice en vez de inventarse un "1.".
        orphan = ingest.get_or_create_position(
            logic.apply_move(root.fen, 'a2a3'))
        self.assertEqual(views._line_labels(orphan.key)[0], '…')

        _play(root, 'a2a3')

        with self._stale():
            served = views._line_labels(orphan.key)
            self.refreshes.run_all()
            after = views._line_labels(orphan.key)

        self.assertEqual(served[0], '…')
        self.assertEqual(after[0], '1. a3')

    def test_a_copy_past_the_age_guard_is_not_served_at_all(self):
        """El tope es para un fallo EN CADENA, no para el ritmo normal."""
        views._line_labels(self.target.key)

        with _aged(views.LINEAGE_STALE_SECONDS + 1), _counted_walk() as walk:
            label = views._line_labels(self.target.key)

        self.assertEqual(walk.call_count, 1)
        self.assertEqual(self.refreshes.jobs, [])
        self.assertEqual(label[0], '1. Nf3 Nf6')

    def test_an_exploding_refresh_releases_the_turn(self):
        views._line_labels(self.target.key)

        with self._stale():
            views._line_labels(self.target.key)
            with mock.patch.object(views, '_walk_lines_to_root',
                                   side_effect=RuntimeError('boom')), \
                    self.assertLogs('atomicdb.revalidate', 'ERROR'):
                self.refreshes.run_all()

            # El siguiente lector encuentra el turno libre y lo reintenta, en
            # vez de quedarse un minuto sin quien renueve.
            served = views._line_labels(self.target.key)

        self.assertEqual(served[0], '1. Nf3 Nf6')
        self.assertEqual(len(self.refreshes.jobs), 1)

    def test_a_batch_serves_the_aged_and_walks_only_what_is_missing(self):
        """La portada pide ~30 de golpe y llegan con edades distintas."""
        newcomer = _chain(['e2e4', 'e7e6'])
        views._line_labels_many([self.target.key])

        with self._stale(), _counted_walk() as walk:
            labels = views._line_labels_many([self.target.key, newcomer.key])

        # Solo se pasea el que no tenia entrada; el viejo se sirve.
        walk.assert_called_once()
        self.assertEqual(list(walk.call_args.args[0]), [newcomer.key])
        self.assertEqual(set(labels), {self.target.key, newcomer.key})
        self.assertEqual(len(self.refreshes.jobs), 1)


@override_settings(CACHES=CACHE_FOR_TESTS)
class StaleRouteLabelIsServedTests(TestCase):
    """La otra mitad del gasto: rejugar la ruta declarada con pyffish.

    Medido en produccion, 2,05 s de los 6,2 s de la portada.  Cachearlo dejo
    de pagarlo en cada visita; darle el mismo trato al vencer es lo que deja
    de pagarlo tambien en la visita que llega justo al caducar.
    """

    def setUp(self):
        cache.clear()
        ucis = ['g1f3', 'g8f6', 'b1c3']
        self.target = _chain(ucis)
        self.route = ','.join(ucis)
        self.refreshes = _Deferred()
        patch = mock.patch.object(revalidate, 'EXECUTOR', self.refreshes)
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_aged_label_is_served_without_replaying_the_route(self):
        first = views._route_labels(self.route, self.target.key)

        with _aged(views.LINEAGE_CACHE_SECONDS + 1), \
                mock.patch.object(views, '_walk_route_labels') as replay:
            second = views._route_labels(self.route, self.target.key)

        self.assertIsNotNone(first)
        self.assertEqual(second, first)
        replay.assert_not_called()
        self.assertEqual(len(self.refreshes.jobs), 1)

    def test_the_refresh_replays_it_once_and_puts_it_back(self):
        views._route_labels(self.route, self.target.key)

        with _aged(views.LINEAGE_CACHE_SECONDS + 1):
            views._route_labels(self.route, self.target.key)
            with mock.patch.object(
                    views, '_walk_route_labels',
                    wraps=views._walk_route_labels) as replay:
                self.assertEqual(self.refreshes.run_all(), 1)
                self.assertEqual(replay.call_count, 1)
                views._route_labels(self.route, self.target.key)

        # Una sola reproduccion en las tres llamadas, y ningun refresco extra.
        self.assertEqual(replay.call_count, 1)
        self.assertEqual(self.refreshes.jobs, [])

    def test_a_route_that_stopped_reaching_the_target_is_dropped(self):
        """Y el fallo no se guarda: tampoco al refrescar por detras."""
        views._route_labels(self.route, self.target.key)

        with _aged(views.LINEAGE_CACHE_SECONDS + 1):
            views._route_labels(self.route, self.target.key)
            with mock.patch.object(views, '_walk_route_labels',
                                   return_value=None):
                self.refreshes.run_all()
            # La entrada vieja sigue ahi — no se borra lo que ya valia — y el
            # turno quedo libre para volver a intentarlo.
            self.assertIsNotNone(views._route_labels(self.route,
                                                     self.target.key))
            self.assertEqual(len(self.refreshes.jobs), 1)


@override_settings(CACHES=CACHE_FOR_TESTS)
class RealRefreshThreadTests(TransactionTestCase):
    """El unico test que NO sustituye el ejecutor, y el unico que hace falta.

    Todo lo demas recoge el refresco en una lista para poder afirmar cosas
    deterministas.  Pero lo que corre en produccion es un HILO con SU propia
    conexion a la base, y eso o funciona de verdad o no funciona.

    ``TransactionTestCase`` y no ``TestCase`` porque el hilo abre su conexion:
    dentro de la transaccion de un ``TestCase`` no veria nada de lo que el
    test haya escrito.
    """

    def setUp(self):
        cache.clear()
        self.target = _chain(['g1f3', 'g8f6'])
        self.threads = []
        patch = mock.patch.object(
            revalidate, 'EXECUTOR',
            lambda work, name: self.threads.append(
                revalidate.background(work, name)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_real_background_thread_renews_the_entry(self):
        views._line_labels(self.target.key)

        with _aged(views.LINEAGE_CACHE_SECONDS + 1):
            served = views._line_labels(self.target.key)

            # Aqui NO se cuentan paseos: el hilo ya esta corriendo y contar
            # seria una carrera.  Que la llamada no espera esta demostrado sin
            # hilos en ``StaleLineageIsServedTests``; lo que hace falta probar
            # aqui es que el hilo de verdad, con SU conexion, deja hecho el
            # trabajo — y eso se ve en lo que pasa despues del ``join``.
            self.assertEqual(served[0], '1. Nf3 Nf6')
            self.assertEqual(len(self.threads), 1)
            self.threads[0].join(timeout=60)
            self.assertFalse(self.threads[0].is_alive())

            # Y dejo la entrada fresca detras: el siguiente es un acierto.
            with _counted_walk() as after:
                label = views._line_labels(self.target.key)

        self.assertEqual(after.call_count, 0)
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(label[0], '1. Nf3 Nf6')


class LineageCacheBackendTests(TestCase):
    """Django's implicit default holds 300 entries, and that is not a cache.

    One home render resolves a couple of dozen breadcrumbs and shares the
    same store with the ``cache_page`` bodies.  At 300 slots a quiet evening
    of clicking evicts the lineage before the TTL ever expires it, which is
    memory spent to still pay the walk.  The backend must also stay local:
    per-process copies are the design, not a compromise.
    """

    def test_the_project_configures_a_real_local_cache(self):
        from django.conf import settings

        default = settings.CACHES['default']
        self.assertIn('locmem', default['BACKEND'])
        self.assertGreater(
            default.get('OPTIONS', {}).get('MAX_ENTRIES', 300), 1000)
