"""Stale-while-revalidate sobre la cache de pagina de la portada.

QUE ESTABA MAL.  La portada lleva una cache de 15 s (§ urls).  Al vencer, el
SIGUIENTE visitante paga el render entero, y bajo las rafagas de analisis
(500-900 en diez minutos) ese render compite con lotes de
``UPDATE atomicdb_position SET reachable=true WHERE key IN (...)`` de ~9,5 s y
con el autovacuum de la misma tabla: medido, entre 10 y 26 s.  El monitor
externo mira con un tope de 30 s, asi que varias veces por noche veia
``http=000``.  Con la cache caliente la misma pagina se sirve en 0,2 s: lo
que falla no es la pagina, es el instante en que la cache caduca.

QUE SE HACE.  Al vencer el TTL no se recalcula en la peticion: se sirve la
copia CADUCADA tal cual y se dispara el recalculo por detras.  Un cerrojo en
la cache compartida garantiza que lo recalcule UNO — los otros cuatro
workers, y los otros siete hilos de cada uno, siguen sirviendo lo caducado —
y cuando termina, la entrada fresca vuelve a su sitio.  Nadie espera nunca a
un render frio mientras haya una copia que servir.

Es el mismo trato que ya recibe lo caro del NIVEL DE DATOS
(§ metrics.shared_snapshot: fresca se sirve, vieja se sirve igual y uno la
renueva), subido un piso: alli el que coge el cerrojo recalcula SINCRONO y
paga la espera, y aqui no paga nadie.  Esa es toda la diferencia, y es la que
importa cuando lo que se recalcula tarda veinte segundos.

QUE PRECIO TIENE, DICHO EN CLARO.  Un visitante puede recibir una portada de
hasta ``STALE_SECONDS`` de antiguedad en vez de una de 15 s.  Eso solo pasa
mientras el refresco esta en vuelo — segundos — porque el refresco arranca en
el MISMO instante en que se sirve lo caducado.  El tope existe para el caso
en que los refrescos fallen en cadena: pasado el, se recalcula sincrono, que
es preferible a servir una portada de hace media hora.  Y la edad viaja en la
respuesta (``X-AtomicDB-Page-Age``) para poder auditarla desde fuera sin
mirar dentro del proceso.

LO QUE NO CAMBIA, Y ES LO MAS IMPORTANTE.  Las claves son las de Django: se
usan ``get_cache_key``/``learn_cache_key`` con el mismo prefijo que
``cache_page``, asi que una entrada escrita por este modulo la lee un
``cache_page`` pelado y al reves — volver atras es cambiar una linea de
``urls`` y nada mas.  Y se conservan LAS TRES GUARDAS de Django, con el orden
de decoradores intacto: no se guarda una respuesta que no sea 200, ni una que
declare ``private``, ni — sobre todo — una que ESTRENE cookie ante una
peticion sin cookies.  Esa ultima es el incidente del token CSRF compartido
escrito como codigo (§ urls, § test_view_cache.CsrfIsolationTests): una copia
caducada tampoco se le sirve jamas a un visitante sin cookies, porque la
entrada de la que sale varia por cookie igual que antes.
"""

import io
import logging
import time
from functools import wraps
from importlib import import_module

from django.conf import settings
from django.contrib.auth import get_user
from django.core.cache import cache
from django.core.handlers.wsgi import WSGIRequest
from django.utils.cache import (get_cache_key, has_vary_header,
                                learn_cache_key, patch_response_headers)
from django.utils.functional import SimpleLazyObject

from . import revalidate

logger = logging.getLogger(__name__)


# Frescura anunciada al navegador y vida de la entrada "fresca".  Es el 15 de
# siempre: lo que cambia no es cuanto dura una portada, es quien paga cuando
# se acaba.
FRESH_SECONDS = 15
# Cuanto puede tener una copia caducada y seguir sirviendose mientras se
# renueva.  Diez minutos es el tope de un fallo EN CADENA de refrescos, no la
# edad esperada: la esperada es la que tarde un render, porque el refresco
# sale disparado a la vez que la respuesta.
STALE_SECONDS = 600
# Vida DURA de la copia en el almacen, muy por encima del tope de edad, para
# que quien decida sea la guarda de arriba y no la expiracion del backend
# (mismo reparto que § metrics: PUBLIC_FRESH_SECONDS contra PUBLIC_TTL).
STALE_TTL_SECONDS = 3600
# El cerrojo dura mas que el peor render medido (26 s) y mucho menos que el
# tope de edad: un worker que muera a mitad del refresco no puede bloquear el
# siguiente mas de un minuto.
LOCK_SECONDS = 60

# Sufijos de las DOS claves nuevas.  Cuelgan de la clave de pagina de Django,
# asi que una entrada y su copia caducada viven y mueren con el mismo
# visitante y el mismo ``Vary``.
STALE_SUFFIX = '.stale'
LOCK_SUFFIX = '.refresh'

# El marcador de auditoria.  Va en cabeceras ``X-`` propias y no en ``Age``
# porque ``Age`` tiene significado para los intermediarios y esto no es una
# afirmacion sobre el protocolo, es una sonda para nosotros.
STATE_HEADER = 'X-AtomicDB-Page-Cache'
AGE_HEADER = 'X-AtomicDB-Page-Age'

HIT, STALE, MISS = 'hit', 'stale', 'miss'


def _now():
    """El reloj de la edad de la copia, en un sitio donde se pueda mover.

    La edad se escribe aqui y se lee aqui, asi que una prueba que quiera
    comprobar la guarda de antiguedad necesita adelantar EL MISMO reloj que
    escribio la marca — y no el del proceso entero.
    """
    return time.time()


# El hilo que refresca y cierra las conexiones que abre vive en § revalidate:
# la cache de linaje necesita EL MISMO hilo con la misma responsabilidad, y un
# invariante que se puede violar en un sitio y no en el otro no es un
# invariante.  El nombre local se conserva porque es el punto de sustitucion
# de los tests de este modulo, y patchearlo aqui no debe tocar al de datos.
_executor = revalidate.background

# Punto de sustitucion de los tests: lo unico que separa "en segundo plano"
# de "ahora mismo" es esta funcion.
REFRESH_EXECUTOR = _executor


def _key_prefix():
    return settings.CACHE_MIDDLEWARE_KEY_PREFIX


def _storable(request, response):
    """Las guardas de ``UpdateCacheMiddleware``, sin quitar ninguna.

    La tercera es la que no se puede tocar: una respuesta que ESTRENA cookie
    ante una peticion sin cookies es personal — lleva un token CSRF recien
    hecho — y guardarla seria servirsela al siguiente visitante sin cookies,
    que ademas no estrenaria la suya y veria su POST rechazado con 403.  Paso
    de verdad (§ urls) y por eso ``csrf_protect`` va POR DENTRO: la cookie ya
    esta puesta cuando esto se pregunta.
    """
    if getattr(response, 'streaming', False) or response.status_code != 200:
        return False
    if not request.COOKIES and response.cookies and has_vary_header(response,
                                                                    'Cookie'):
        return False
    return 'private' not in response.get('Cache-Control', '')


def _mark(response, state, age):
    response.headers[STATE_HEADER] = state
    response.headers[AGE_HEADER] = str(int(age))
    return response


def _store(request, response, fresh_seconds, stale_ttl_seconds):
    """Guarda la entrada fresca Y la copia caducable, bajo la misma clave.

    La cabecera de ``Vary`` se aprende con la vida LARGA a proposito: es el
    mapa URL -> cabeceras que varian, y si caducara con los 15 s de la
    entrada fresca, ``get_cache_key`` devolveria ``None`` justo cuando hace
    falta encontrar la copia caducada — que es el unico momento en que este
    modulo hace algo.  El mapa no es un dato del visitante y no envejece.
    """
    patch_response_headers(response, fresh_seconds)
    key = learn_cache_key(request, response, stale_ttl_seconds, _key_prefix(),
                          cache=cache)

    def keep(rendered):
        cache.set(key, rendered, fresh_seconds)
        cache.set(key + STALE_SUFFIX,
                  {'stored_at': _now(), 'response': rendered},
                  stale_ttl_seconds)

    if hasattr(response, 'render') and callable(response.render):
        response.add_post_render_callback(keep)
    else:
        keep(response)
    return response


def _detached(request):
    """Un ``request`` PROPIO para el refresco, del mismo visitante.

    No se reutiliza el del visitante y la razon no es elegancia: mientras el
    hilo re-renderiza, el original sigue vivo bajando por la cadena de
    middleware de respuesta, y ``request.META`` es EL MISMO diccionario que
    ``csrf_protect`` escribe.  Compartirlo seria dejar que el refresco le
    pusiera una cookie a la respuesta que ya va de camino a otro — la familia
    exacta del incidente que fijo el orden de los decoradores.

    La sesion y el usuario los pone el middleware, que aqui no corre, asi que
    se reconstruyen DESDE LA MISMA COOKIE con las dos lineas que usan
    ``SessionMiddleware`` y ``AuthenticationMiddleware``.  Sin ellas el
    refresco renderizaria una portada anonima y la guardaria bajo la entrada
    de un visitante con sesion, que perderia su nombre en la cabecera hasta el
    siguiente ciclo.
    """
    environ = dict(request.environ)
    environ['REQUEST_METHOD'] = 'GET'
    environ['CONTENT_LENGTH'] = '0'
    environ['wsgi.input'] = io.BytesIO(b'')
    clone = WSGIRequest(environ)
    engine = import_module(settings.SESSION_ENGINE)
    clone.session = engine.SessionStore(
        clone.COOKIES.get(settings.SESSION_COOKIE_NAME))
    clone.user = SimpleLazyObject(lambda: get_user(clone))
    return clone


def _refresh(view, request, key, fresh_seconds, stale_ttl_seconds,
             lock_seconds):
    """Pide el turno y, si lo consigue, deja el recalculo lanzado.

    Devuelve si lo lanzo, que es lo unico que los tests necesitan saber para
    comprobar que recalcula UNO.
    """
    lock = key + LOCK_SUFFIX
    if not cache.add(lock, 1, lock_seconds):
        return False
    clone = _detached(request)

    def work():
        try:
            response = view(clone)
            if _storable(clone, response):
                _store(clone, response, fresh_seconds, stale_ttl_seconds)
        except Exception:               # noqa: BLE001
            # Un refresco que revienta no puede tumbar a nadie: no hay nadie
            # esperandolo.  Se anota, se suelta el cerrojo y lo reintenta el
            # siguiente visitante que encuentre la copia caducada.
            logger.exception('atomicdb: could not refresh the front page')
        finally:
            cache.delete(lock)

    REFRESH_EXECUTOR(work, 'atomicdb-page-refresh')
    return True


def stale_while_revalidate(view, *, fresh_seconds=None, stale_seconds=None,
                           stale_ttl_seconds=None, lock_seconds=None):
    """``cache_page`` que, al caducar, sirve lo viejo y renueva por detras.

    Sustituye a ``cache_page(FRESH_SECONDS)`` y se envuelve igual: por FUERA
    de ``vary_on_cookie`` y de ``csrf_protect``, que es lo que hace que la
    cookie ya este puesta cuando se decide guardar.

    Los cuatro numeros se leen en cada peticion, no al decorar, para que un
    test pueda moverlos sin reconstruir el ``urlpatterns``.
    """
    @wraps(view)
    def served(request, *args, **kwargs):
        fresh = FRESH_SECONDS if fresh_seconds is None else fresh_seconds
        stale = STALE_SECONDS if stale_seconds is None else stale_seconds
        hard = (STALE_TTL_SECONDS if stale_ttl_seconds is None
                else stale_ttl_seconds)
        held = LOCK_SECONDS if lock_seconds is None else lock_seconds

        if request.method not in ('GET', 'HEAD'):
            return view(request, *args, **kwargs)

        key = get_cache_key(request, _key_prefix(), 'GET', cache=cache)
        if key is not None:
            response = cache.get(key)
            if response is not None:
                return _mark(response, HIT, 0)
            entry = cache.get(key + STALE_SUFFIX)
            if entry is not None:
                age = max(0.0, _now() - entry['stored_at'])
                if age <= stale:
                    _refresh(view, request, key, fresh, hard, held)
                    return _mark(entry['response'], STALE, age)
                # Demasiado vieja para servirla: mejor esperar el render que
                # afirmar que la portada dice lo que decia hace media hora.

        response = view(request, *args, **kwargs)
        if _storable(request, response):
            _store(request, response, fresh, hard)
        return _mark(response, MISS, 0)

    return served
