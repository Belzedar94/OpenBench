"""Cache corta sobre las vistas de lectura calientes de AtomicDB.

Lo que se comprueba: que las cacheadas siguen sirviendo (y que la segunda
visita no vuelve a tocar la base), que las interactivas y las del protocolo
de workers NO llevan cabecera de cache, y que el interruptor de tema
sobrevive (la eleccion es client-side, el HTML es el mismo en ambos temas).

Y, por encima de todo lo demas, LO QUE NUNCA PUEDE VOLVER: el orden de
``csrf_protect`` dentro de ``cache_page`` (§ urls) esta ahi porque una vez se
sirvio el token CSRF de un visitante al siguiente.  Los tests de
``CsrfIsolationTests`` son ese incidente escrito como prueba: dos visitantes
sin cookies, y el segundo NO puede recibir el token del primero.
"""

import re

from django.conf import settings
from django.core.cache import cache
from django.test import Client, override_settings

from . import ingest, logic
from .models import Position
from .testing import TestCase


CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-view-cache-tests',
    },
}

# El minificador de HTML reordena atributos, asi que ``value`` no siempre va
# pegado a ``name``: se busca dentro de la MISMA etiqueta y nada mas.
CSRF_INPUT = re.compile(r'csrfmiddlewaretoken"[^>]*?value="([^"]+)"')


def _max_age(response):
    return response.headers.get('Cache-Control', '')


def _tokens_in(response):
    """Todos los tokens CSRF que viajan en un cuerpo HTML."""
    return set(CSRF_INPUT.findall(response.content.decode()))


@override_settings(CACHES=CACHE_FOR_TESTS)
class CachedReadViewTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = Client()

    def test_home_still_serves_and_advertises_a_short_cache(self):
        # La PRIMERA visita sin cookies estrena token CSRF: es una respuesta
        # personal, no se guarda y no se anuncia cacheable — ni aqui ni aguas
        # abajo.  Desde la segunda, el visitante ya trae su cookie y entra al
        # cache con su propia entrada, que es donde estan las tormentas de F5.
        self.client.get('/atomicdb/')
        response = self.client.get('/atomicdb/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('max-age=15', _max_age(response))
        self.assertIn('AtomicDB', response.content.decode())

    def test_a_first_cookieless_visit_is_never_stored(self):
        response = Client().get('/atomicdb/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)
        self.assertNotIn('max-age', _max_age(response))

    def test_map_and_method_serve_with_a_short_cache(self):
        for url, seconds in (('/atomicdb/map/', 30),
                             ('/atomicdb/method/', 30)):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'max-age={seconds}', _max_age(response))

    def test_a_repeat_visit_to_home_costs_no_queries(self):
        # La primera respuesta ESTRENA la cookie CSRF y Django, con razon, no
        # la cachea; la siguiente ya viaja con cookie y si entra al cache.
        self.client.get('/atomicdb/')
        self.client.get('/atomicdb/')

        # Cero sobre la conexion del ALIAS: con la base partida es la unica
        # que tocaria la home; contar default alli seria vacuo.
        with self.assertNumQueries(0, using=settings.ATOMICDB_DATABASE_ALIAS):
            hit = self.client.get('/atomicdb/')
        self.assertEqual(hit.status_code, 200)

    def test_the_cached_body_is_the_same_body(self):
        self.client.get('/atomicdb/')
        first = self.client.get('/atomicdb/')
        Position.objects.filter(key=logic.key_of(logic.start_fen())).update(
            eval_cp=4_321)

        second = self.client.get('/atomicdb/')

        self.assertEqual(first.content, second.content)

    def test_a_different_cookie_gets_its_own_entry(self):
        # Nadie debe recibir el token CSRF de otro visitante.
        self.client.get('/atomicdb/')
        mine = self.client.get('/atomicdb/')
        other = Client()
        other.cookies['csrftoken'] = 'a' * 64
        theirs = other.get('/atomicdb/')

        self.assertEqual(theirs.status_code, 200)
        self.assertNotEqual(mine.content, theirs.content)

    def test_the_second_visit_is_served_from_the_stored_entry(self):
        """Que la entrada se GUARDA y se SIRVE, no solo que se anuncia.

        Se comprueba con la unica prueba que no se puede fingir: el cuerpo
        cambia debajo — otra posicion en cola, con su fila y su rotulo — y la
        respuesta sigue siendo la de antes.  Con cabecera de cache, sin tocar
        el alias de AtomicDB, y byte a byte igual.
        """
        self.client.get('/atomicdb/')
        stored = self.client.get('/atomicdb/')
        self.assertIn('max-age=15', _max_age(stored))

        Position.objects.filter(key=logic.key_of(logic.start_fen())).update(
            eval_cp=1234, visits=99)

        with self.assertNumQueries(0, using=settings.ATOMICDB_DATABASE_ALIAS):
            served = self.client.get('/atomicdb/')

        self.assertEqual(served.status_code, 200)
        self.assertIn('max-age=15', _max_age(served))
        self.assertEqual(served.content, stored.content)

    def test_the_theme_switch_survives_the_cache(self):
        self.client.get('/atomicdb/')
        body = self.client.get('/atomicdb/').content.decode()
        cached = self.client.get('/atomicdb/').content.decode()

        for markup in ('atomicdb-theme', 'id="theme-toggle"',
                       'root.dataset.theme = theme'):
            self.assertIn(markup, body)
        # El tema no entra en el HTML: la misma entrada sirve claro y oscuro.
        self.assertEqual(body, cached)
        self.assertNotIn('Vary: Accept', str(body))


@override_settings(CACHES=CACHE_FOR_TESTS)
class CsrfIsolationTests(TestCase):
    """El incidente que fijo el orden de los decoradores, como prueba.

    Con ``csrf_protect`` FUERA de ``cache_page``, la cookie CSRF la ponia el
    middleware DESPUES de que el cache decidiera: ``response.cookies`` estaba
    vacio en ese instante, la guarda de Django no disparaba, y la respuesta
    del primer visitante — token incluido — se guardaba bajo la entrada de
    "sin cookies" y se le servia al siguiente, que ademas no estrenaba cookie
    propia y veia su POST rechazado con 403.

    Estos tests no miran decoradores: miran el sintoma.  Si alguien reordena
    ``urls.py`` para que la portada cachee "un poco mas", esto se pone rojo.
    """

    def setUp(self):
        cache.clear()

    def test_two_cookieless_visitors_never_share_a_token(self):
        first = Client().get('/atomicdb/')
        second = Client().get('/atomicdb/')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # Cada uno estrena el suyo...
        self.assertIn('csrftoken', first.cookies)
        self.assertIn('csrftoken', second.cookies)
        self.assertNotEqual(first.cookies['csrftoken'].value,
                            second.cookies['csrftoken'].value)
        # ...y ninguno lleva dentro el del otro.
        mine, theirs = _tokens_in(first), _tokens_in(second)
        self.assertTrue(mine and theirs)
        self.assertFalse(mine & theirs)

    def test_a_warmed_entry_is_not_handed_to_a_cookieless_visitor(self):
        """El caso exacto del fallo: un visitante YA cacheado y otro nuevo."""
        warm = Client()
        warm.get('/atomicdb/')
        cached = warm.get('/atomicdb/')
        self.assertIn('max-age=15', _max_age(cached))

        newcomer = Client().get('/atomicdb/')

        self.assertEqual(newcomer.status_code, 200)
        self.assertIn('csrftoken', newcomer.cookies)
        self.assertFalse(_tokens_in(cached) & _tokens_in(newcomer))

    def test_a_cookieless_response_is_never_stored(self):
        """Ni aqui ni aguas abajo: una respuesta personal no se anuncia cacheable."""
        response = Client().get('/atomicdb/')

        self.assertNotIn('max-age', _max_age(response))
        self.assertNotIn('Expires', response.headers)

    def test_the_token_in_the_body_is_the_visitor_s_own(self):
        """El token pintado tiene que validar contra la cookie que se llevo."""
        client = Client(enforce_csrf_checks=True)
        page = client.get('/atomicdb/')
        token = next(iter(_tokens_in(page)))

        posted = client.post('/atomicdb/fen/',
                             {'fen': logic.start_fen(),
                              'csrfmiddlewaretoken': token})

        self.assertNotEqual(posted.status_code, 403)


@override_settings(CACHES=CACHE_FOR_TESTS)
class UncachedViewTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())

    def test_explore_is_never_cached(self):
        response = self.client.get(f'/atomicdb/explore/{self.root.key}/')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('max-age', _max_age(response))

    def test_read_apis_are_never_cached(self):
        for url, params in (
                ('/atomicdb/api/query', {'fen': self.root.fen}),
                (f'/atomicdb/api/frontier/{self.root.key}/', {})):
            with self.subTest(url=url):
                response = self.client.get(url, params)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('max-age', _max_age(response))

    def test_explore_still_reflects_a_change_immediately(self):
        first = self.client.get(f'/atomicdb/explore/{self.root.key}/')
        Position.objects.filter(key=self.root.key).update(eval_cp=777)

        second = self.client.get(f'/atomicdb/explore/{self.root.key}/')

        self.assertNotIn('+777cp', first.content.decode())
        self.assertIn('+777cp', second.content.decode())

    def test_the_worker_protocol_is_never_cached(self):
        for url in ('/atomicdb/api/lease', '/atomicdb/api/heartbeat',
                    '/atomicdb/api/submit'):
            with self.subTest(url=url):
                response = self.client.post(url, {})
                self.assertNotIn('max-age', _max_age(response))

    def test_the_request_endpoint_is_never_cached(self):
        response = self.client.post(f'/atomicdb/request/{self.root.key}/')

        self.assertNotIn('max-age', _max_age(response))
