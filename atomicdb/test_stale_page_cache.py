"""Lo que la portada tiene PROHIBIDO volver a hacer esperar.

El incidente, con recibos: la cache de pagina de la portada dura 15 s, y al
vencer el siguiente visitante pagaba el render entero.  Bajo las rafagas de
analisis ese render tardaba entre 10 y 26 s — compitiendo con lotes de
``UPDATE ... reachable=true`` de ~9,5 s y con el autovacuum — y el monitor
externo, que corta a los 30, registraba ``http=000`` varias veces por noche.

Lo que fijan estos tests, en orden de importancia:

* que al caducar se sirva la copia VIEJA en vez de hacer esperar a nadie;
* que la recalcule UNO, no todos los que lleguen mientras tanto;
* que haya un tope: pasada cierta edad se recalcula sincrono, porque servir
  una portada de hace media hora es peor que esperar;
* que la respuesta DIGA de cuando es, para poder auditarlo desde fuera.

Y por encima de todo eso, lo que NO puede cambiar: las tres guardas de Django
al guardar, y muy en particular la de la cookie.  Esas viven en
``test_view_cache`` y siguen ahi — este modulo sustituyo a ``cache_page`` por
debajo de ellas, asi que esos tests son ahora la red de este codigo.

CON EL RELOJ Y EL EJECUTOR EN LA MANO.  Nada aqui duerme ni lanza hilos: la
edad se mueve empujando ``page_cache._now`` y el refresco se recoge en una
lista en vez de irse a un hilo.  Un test de concurrencia que duerme es un test
que un dia falla en una maquina lenta y nadie sabe por que.
"""

from unittest import mock

from django.core.cache import cache
from django.test import Client, override_settings

from . import ingest, logic, page_cache
from .models import Position
from .testing import TestCase, TransactionTestCase, worker_account


CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-stale-page-cache-tests',
    },
}


class _Deferred:
    """El ejecutor de los tests: apunta el refresco y no lo corre.

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
class StalePageCacheTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.refreshes = _Deferred()
        patch = mock.patch.object(page_cache, 'REFRESH_EXECUTOR',
                                  self.refreshes)
        patch.start()
        self.addCleanup(patch.stop)

    # -- utilidades -------------------------------------------------------
    def _warm(self, fresh_seconds=0):
        """Deja una copia caducable y NINGUNA entrada fresca.

        ``fresh_seconds=0`` es "guardala ya vencida" para Django, asi que el
        estado que queda es exactamente el de una entrada de 15 s que acaba
        de expirar: la copia larga sigue ahi y la corta no.  Sin dormir.

        Las dos visitas son necesarias y no son un detalle: la primera SIN
        cookies estrena token CSRF y por eso no se guarda (§ urls).
        """
        with mock.patch.object(page_cache, 'FRESH_SECONDS', fresh_seconds):
            self.client.get('/atomicdb/')
            return self.client.get('/atomicdb/')

    def _state(self, response):
        return response.headers.get(page_cache.STATE_HEADER)

    def _age(self, response):
        return int(response.headers[page_cache.AGE_HEADER])

    # -- servir lo caducado ------------------------------------------------
    def test_an_expired_entry_is_served_from_the_stale_copy(self):
        stored = self._warm()

        served = self.client.get('/atomicdb/')

        self.assertEqual(served.status_code, 200)
        self.assertEqual(self._state(served), page_cache.STALE)
        self.assertEqual(served.content, stored.content)

    def test_serving_the_stale_copy_costs_no_queries(self):
        """La prueba de que NADIE espera: cero consultas en la peticion.

        Es la afirmacion entera del cambio.  Antes, esta misma peticion —
        entrada vencida — era la que se comia el render de 10-26 s.
        """
        self._warm()

        with self.assertNumQueries(0, using='default'):
            served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.STALE)

    def test_the_stale_copy_is_not_served_to_a_cookieless_visitor(self):
        """Lo caducado tampoco cruza de un visitante a otro.

        El incidente del token CSRF compartido no se reabre por servir cosas
        viejas: la copia caducada cuelga de la clave que VARIA POR COOKIE, y
        quien llega sin ninguna no tiene entrada que heredar.
        """
        warmed = self._warm()

        newcomer = Client().get('/atomicdb/')

        self.assertEqual(newcomer.status_code, 200)
        self.assertIn('csrftoken', newcomer.cookies)
        self.assertNotEqual(newcomer.content, warmed.content)
        self.assertEqual(self._state(newcomer), page_cache.MISS)

    # -- que recalcule UNO -------------------------------------------------
    def test_the_expiry_launches_exactly_one_refresh(self):
        self._warm()

        self.client.get('/atomicdb/')

        self.assertEqual(len(self.refreshes.jobs), 1)

    def test_the_readers_behind_the_first_do_not_recompute(self):
        """Diez visitantes con la entrada vencida: un refresco, diez portadas.

        Este es el caso de la rafaga.  Con ``cache_page`` pelado cada uno de
        los que llegaban durante el render se ponia a renderizar tambien.
        """
        self._warm()

        served = [self.client.get('/atomicdb/') for _ in range(10)]

        self.assertEqual(len(self.refreshes.jobs), 1)
        self.assertTrue(all(page.status_code == 200 for page in served))
        self.assertTrue(all(self._state(page) == page_cache.STALE
                            for page in served))

    def test_a_second_expiry_refreshes_again_once_the_lock_is_gone(self):
        """El cerrojo no se queda pegado: se suelta al terminar el trabajo.

        Las tres visitas van bajo ``FRESH_SECONDS=0`` porque el refresco se
        lleva los plazos de LA PETICION que lo encolo: si la del medio
        guardase con los 15 s de verdad, la tercera seria un acierto fresco y
        este test no estaria mirando el cerrojo.
        """
        self._warm()

        with mock.patch.object(page_cache, 'FRESH_SECONDS', 0):
            self.client.get('/atomicdb/')
            self.refreshes.run_all()
            self.client.get('/atomicdb/')

        self.assertEqual(len(self.refreshes.jobs), 1)

    # -- el refresco deja la entrada nueva ---------------------------------
    def test_the_refresh_puts_a_fresh_entry_back(self):
        self._warm()
        self.client.get('/atomicdb/')

        self.assertEqual(self.refreshes.run_all(), 1)

        served = self.client.get('/atomicdb/')
        self.assertEqual(self._state(served), page_cache.HIT)
        self.assertIn('max-age=15', served.headers.get('Cache-Control', ''))

    def test_the_refreshed_body_is_the_body_of_now(self):
        """No basta con que refresque: tiene que refrescar DE VERDAD."""
        self._warm()
        Position.objects.filter(key=logic.key_of(logic.start_fen())).update(
            eval_cp=4_321, visits=77)

        stale = self.client.get('/atomicdb/')
        self.refreshes.run_all()
        served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.HIT)
        self.assertNotEqual(served.content, stale.content)

    def test_the_refresh_renders_for_the_same_visitor(self):
        """El refresco corre SIN middleware: sesion y usuario se reconstruyen.

        Si no lo hicieran, el hilo renderizaria una portada anonima y la
        guardaria bajo la entrada de alguien con sesion — que perderia su
        nombre de la cabecera hasta el siguiente ciclo.  Este test es esa
        regresion.
        """
        worker_account('refresher')
        self.client.login(username='refresher', password='pw')
        self._warm()

        self.client.get('/atomicdb/')
        self.refreshes.run_all()
        served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.HIT)
        self.assertIn('refresher', served.content.decode())

    # -- la guarda de antiguedad -------------------------------------------
    def test_a_copy_past_the_guard_is_recomputed_synchronously(self):
        self._warm()

        with mock.patch.object(page_cache, '_now',
                               lambda: page_cache.time.time()
                               + page_cache.STALE_SECONDS + 1):
            served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.MISS)
        self.assertEqual(self.refreshes.jobs, [])

    def test_the_recomputed_body_is_current_again(self):
        """La guarda existe para no MENTIR, no solo para no servir viejo."""
        stale = self._warm()
        Position.objects.filter(key=logic.key_of(logic.start_fen())).update(
            eval_cp=1_234)

        with mock.patch.object(page_cache, '_now',
                               lambda: page_cache.time.time()
                               + page_cache.STALE_SECONDS + 1):
            served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.MISS)
        self.assertNotEqual(served.content, stale.content)

    def test_a_copy_just_inside_the_guard_is_still_served(self):
        self._warm()

        with mock.patch.object(page_cache, '_now',
                               lambda: page_cache.time.time()
                               + page_cache.STALE_SECONDS - 1):
            served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.STALE)

    # -- el marcador de auditoria ------------------------------------------
    def test_the_stale_copy_says_how_old_it_is(self):
        self._warm()

        with mock.patch.object(page_cache, '_now',
                               lambda: page_cache.time.time() + 42):
            served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.STALE)
        self.assertEqual(self._age(served), 42)

    def test_a_fresh_hit_says_it_is_fresh(self):
        self.client.get('/atomicdb/')
        self.client.get('/atomicdb/')

        served = self.client.get('/atomicdb/')

        self.assertEqual(self._state(served), page_cache.HIT)
        self.assertEqual(self._age(served), 0)

    def test_the_marker_never_reaches_the_visitor_as_content(self):
        """"Discreto" quiere decir cabecera: el HTML no cambia ni un byte."""
        stored = self._warm()

        served = self.client.get('/atomicdb/')

        self.assertEqual(served.content, stored.content)
        self.assertNotIn(page_cache.AGE_HEADER, served.content.decode())


@override_settings(CACHES=CACHE_FOR_TESTS)
class RefreshFailureTests(TestCase):
    """Un refresco que revienta no puede llevarse la portada por delante."""

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.refreshes = _Deferred()
        patch = mock.patch.object(page_cache, 'REFRESH_EXECUTOR',
                                  self.refreshes)
        patch.start()
        self.addCleanup(patch.stop)

    def _warm(self):
        with mock.patch.object(page_cache, 'FRESH_SECONDS', 0):
            self.client.get('/atomicdb/')
            self.client.get('/atomicdb/')

    def test_an_exploding_refresh_releases_the_lock(self):
        self._warm()
        self.client.get('/atomicdb/')

        with mock.patch.object(page_cache, '_store',
                               side_effect=RuntimeError('boom')):
            with self.assertLogs('atomicdb.page_cache', 'ERROR'):
                self.refreshes.run_all()

        # El siguiente visitante encuentra el cerrojo libre y vuelve a
        # intentarlo, en vez de quedarse un minuto sin quien renueve.
        self.client.get('/atomicdb/')
        self.assertEqual(len(self.refreshes.jobs), 1)

    def test_an_exploding_refresh_still_serves_the_stale_copy(self):
        self._warm()

        with mock.patch.object(page_cache, '_store',
                               side_effect=RuntimeError('boom')):
            served = self.client.get('/atomicdb/')
            with self.assertLogs('atomicdb.page_cache', 'ERROR'):
                self.refreshes.run_all()

        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers[page_cache.STATE_HEADER],
                         page_cache.STALE)


@override_settings(CACHES=CACHE_FOR_TESTS)
class RealThreadTests(TransactionTestCase):
    """El unico test que NO sustituye el ejecutor, y el unico que hace falta.

    Todo lo demas recoge el refresco en una lista para poder afirmar cosas
    deterministas sobre el.  Pero el camino que corre en produccion es un
    HILO, con su propia conexion a la base y con un ``request`` reconstruido
    a mano, y esas dos cosas o funcionan de verdad o no funcionan.  Por eso
    aqui se lanza el hilo real y se le espera.

    ``TransactionTestCase`` y no ``TestCase`` porque el hilo abre SU conexion:
    dentro de la transaccion de un ``TestCase`` no veria nada de lo que el
    test haya escrito.
    """

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.threads = []
        patch = mock.patch.object(
            page_cache, 'REFRESH_EXECUTOR',
            lambda work, name: self.threads.append(
                page_cache._executor(work, name)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_real_background_thread_refreshes_the_entry(self):
        with mock.patch.object(page_cache, 'FRESH_SECONDS', 0):
            self.client.get('/atomicdb/')
            self.client.get('/atomicdb/')

        served = self.client.get('/atomicdb/')
        self.assertEqual(served.headers[page_cache.STATE_HEADER],
                         page_cache.STALE)

        self.assertEqual(len(self.threads), 1)
        self.threads[0].join(timeout=60)
        self.assertFalse(self.threads[0].is_alive())

        # Y dejo la entrada fresca detras: la siguiente visita es un acierto.
        after = self.client.get('/atomicdb/')
        self.assertEqual(after.headers[page_cache.STATE_HEADER],
                         page_cache.HIT)
        self.assertEqual(after.status_code, 200)


@override_settings(CACHES=CACHE_FOR_TESTS)
class UnchangedContractTests(TestCase):
    """Lo que este modulo hereda de ``cache_page`` y no puede perder."""

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())

    def test_a_post_never_goes_through_the_cache(self):
        posted = self.client.post('/atomicdb/fen/', {'fen': self.root.fen})

        self.assertNotIn(page_cache.STATE_HEADER, posted.headers)

    def test_the_stored_entry_is_the_one_django_would_read(self):
        """Volver a ``cache_page(15)`` tiene que ser una linea, no una purga.

        Las claves son las de Django y el prefijo tambien, asi que una
        entrada escrita aqui la encuentra un ``cache_page`` pelado.  Se
        comprueba con la funcion que ese decorador usa para buscarla.
        """
        from django.test import RequestFactory
        from django.utils.cache import get_cache_key

        self.client.get('/atomicdb/')
        self.client.get('/atomicdb/')

        probe = RequestFactory().get('/atomicdb/')
        probe.COOKIES = self.client.cookies
        probe.META['HTTP_COOKIE'] = '; '.join(
            f'{name}={morsel.value}'
            for name, morsel in self.client.cookies.items())
        key = get_cache_key(probe, '', 'GET', cache=cache)

        self.assertIsNotNone(key)
        self.assertIsNotNone(cache.get(key))
