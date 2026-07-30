"""La cache compartida (Redis) y su caida a la de siempre.

POR QUE COMPARTIDA.  Cinco workers de gunicorn con ``LocMemCache`` son cinco
caches: el mismo linaje se camina cinco veces, la entrada de pagina de un
visitante esta en un worker y sus cuatro peticiones siguientes caen en los
otros cuatro, y — lo que ninguna capacidad arregla — un productor de FUERA de
la peticion (el servicio del selector) no puede publicar nada en una cache que
vive dentro de un worker web.

POR QUE NO PUEDE SER OBLIGATORIA.  El mismo codigo corre en un portatil sin
Redis y dentro de esta suite.  "No hay cache" nunca puede ser la diferencia
entre un sitio que funciona y una traza.  Lo que se prueba aqui es justo eso:
que un Redis ausente, caido o lento degrada a la cache por proceso que este
despliegue ya usaba, y que un error que NO es de transporte sigue subiendo.
"""

from unittest import mock

from django.core.cache.backends.redis import RedisCache
from django.test import SimpleTestCase

from OpenSite.cache import DEGRADED_SECONDS, ResilientRedisCache


def build_cache(location='redis://127.0.0.1:6379/15'):
    return ResilientRedisCache(location, {
        'FALLBACK_LOCATION': 'atomicdb-shared-cache-tests',
        'FALLBACK_OPTIONS': {'MAX_ENTRIES': 100},
    })


class Unreachable(Exception):
    """Se hace pasar por un error de ``redis-py`` para el clasificador."""

    __module__ = 'redis.exceptions'


class SharedCacheFallbackTests(SimpleTestCase):

    def setUp(self):
        self.cache = build_cache()
        self.cache._fallback.clear()

    def test_an_unreachable_redis_serves_from_the_local_cache(self):
        with mock.patch.object(RedisCache, 'set',
                               side_effect=Unreachable('down')):
            self.cache.set('k', 'v', 30)

        self.assertTrue(self.cache.degraded)
        # Y la lectura siguiente ya ni marca a Redis: sale de la local.
        self.assertEqual(self.cache.get('k'), 'v')

    def test_a_missing_redis_package_is_the_same_thing(self):
        with mock.patch.object(RedisCache, 'set',
                               side_effect=ImportError('No module named redis')):
            self.cache.set('k', 'v', 30)

        self.assertTrue(self.cache.degraded)
        self.assertEqual(self.cache.get('k'), 'v')

    def test_it_stops_dialling_a_dead_redis(self):
        with mock.patch.object(RedisCache, 'get',
                               side_effect=Unreachable('down')) as dialled:
            self.cache.get('k')
            self.cache.get('k')
            self.cache.get('k')

        self.assertEqual(dialled.call_count, 1)

    def test_it_comes_back_when_redis_does(self):
        with mock.patch.object(RedisCache, 'get',
                               side_effect=Unreachable('down')):
            self.cache.get('k')
        self.assertTrue(self.cache.degraded)

        # El reloj avanza: se vuelve a intentar, y esta vez contesta.
        self.cache._degraded_until -= DEGRADED_SECONDS + 1
        with mock.patch.object(RedisCache, 'get',
                               return_value='from-redis') as dialled:
            value = self.cache.get('k')

        self.assertEqual(value, 'from-redis')
        self.assertEqual(dialled.call_count, 1)
        self.assertFalse(self.cache.degraded)

    def test_a_caller_bug_is_not_swallowed(self):
        """``incr`` de una clave que no existe es un ValueError de verdad."""
        with mock.patch.object(RedisCache, 'incr',
                               side_effect=ValueError('missing key')):
            with self.assertRaises(ValueError):
                self.cache.incr('nope')

        self.assertFalse(self.cache.degraded)

    def test_every_operation_goes_through_the_fallback(self):
        """Ninguna operacion puede quedarse sin red por olvido.

        Se comprueba una por una y de verdad: con Redis levantando un error
        de transporte, la llamada tiene que contestar en vez de propagar.
        Una sola sin envolver es una portada que revienta el dia que Redis
        se cae, y es justo la que nadie miraria.
        """
        calls = {
            'add': ('k', 'v'), 'get': ('k',), 'set': ('k', 'v'),
            'touch': ('k',), 'delete': ('k',), 'get_many': (['k'],),
            'has_key': ('k',), 'set_many': ({'k': 'v'},),
            'delete_many': (['k'],), 'clear': (),
        }
        for name, args in calls.items():
            with self.subTest(operation=name):
                self.assertIsNot(getattr(ResilientRedisCache, name),
                                 getattr(RedisCache, name))
                self.cache._degraded_until = 0.0
                with mock.patch.object(RedisCache, name,
                                       side_effect=Unreachable('down')):
                    getattr(self.cache, name)(*args)
                self.assertTrue(self.cache.degraded)
        # ``incr`` aparte: sobre una clave que no existe la caida tambien
        # tiene que dejar subir el ValueError legitimo de la local.
        self.cache._degraded_until = 0.0
        with mock.patch.object(RedisCache, 'incr',
                               side_effect=Unreachable('down')):
            with self.assertRaises(ValueError):
                self.cache.incr('missing')
        self.assertTrue(self.cache.degraded)

    def test_the_local_cache_really_stores_things(self):
        with mock.patch.object(RedisCache, 'set_many',
                               side_effect=Unreachable('down')):
            self.cache.set_many({'a': 1, 'b': 2}, 30)

        self.assertEqual(self.cache.get_many(['a', 'b']), {'a': 1, 'b': 2})
        self.cache.delete('a')
        self.assertEqual(self.cache.get_many(['a', 'b']), {'b': 2})
