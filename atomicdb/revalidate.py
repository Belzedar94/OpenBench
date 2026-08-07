"""Servir lo caducado al instante y renovarlo por detras.  La parte comun.

DE DONDE SALE ESTO.  La portada ya lo hace desde § page_cache: al vencer su
entrada no recalcula en la peticion, sirve la copia caducada y manda el
recalculo a un hilo, con un ``cache.add`` como cerrojo para que lo recalcule
UNO.  Ese modulo es todo pagina — claves de Django, guardas de cookie, un
``request`` reconstruido — y nada de eso sirve un piso mas abajo, donde lo que
se cachea no es una respuesta sino DATOS: entradas por posicion, pedidas de
treinta en treinta, que se resuelven en lote.

Lo que SI es identico en los dos sitios, y por eso vive aqui, son dos cosas:

* el hilo de refresco CIERRA LO QUE ABRE.  Django guarda las conexiones por
  hilo y nadie las va a cerrar por el; sin esto cada refresco dejaria una
  conexion de Postgres colgando hasta que muriera el proceso.  Escrito dos
  veces seria un invariante que se puede violar en un sitio y no en el otro;
* el reparto de numeros: una FRESCURA corta sobre una VIDA DURA larga.  La
  entrada vive en el almacen mucho mas de lo que se considera fresca, y en
  esa ventana es donde se sirve lo viejo mientras alguien lo renueva.  Quien
  decide es la guarda de edad de aqui, no la expiracion del backend — que
  solo esta para que nada se quede eternamente.  Mismo reparto que
  § metrics.PUBLIC_FRESH_SECONDS contra PUBLIC_TTL_SECONDS.

LA DIFERENCIA CON § metrics, que es la que importa.  Alli el que coge el
cerrojo recalcula SINCRONO y paga la espera; aqui no la paga nadie, porque lo
que se recalcula puede tardar veinte segundos y el que llega no tiene por que
enterarse.
"""

import logging
import threading
import time

from django.core.cache import cache
from django.db import connections

logger = logging.getLogger(__name__)

# Sufijo del cerrojo.  Cuelga de la clave de la entrada, asi que el turno es
# POR ENTRADA: dos peticiones que piden treinta rotulos cada una y comparten
# veinte se reparten el trabajo en vez de bloquearse la una a la otra.
LOCK_SUFFIX = '.refresh'


def _now():
    """El reloj de la edad de las entradas, en un sitio donde se pueda mover.

    La edad se escribe aqui y se lee aqui, asi que un test que quiera
    comprobar la guarda de antiguedad adelanta ESTE reloj — y no el del
    proceso entero, que tambien mueve los TTL del backend y las marcas de
    tiempo de la base.
    """
    return time.time()


def background(work, name):
    """Lanza el refresco en un hilo y CIERRA LO QUE ABRA.

    Va en el ejecutor y no en el trabajo porque es responsabilidad de QUIEN
    abre el hilo — los tests sustituyen el ejecutor y corren el trabajo en el
    hilo de la prueba, donde cerrar la conexion seria cerrar la transaccion
    del test.

    Se devuelve el hilo para que una prueba de integracion pueda ESPERARLO.
    Nadie en la ruta de la peticion lo mira: esperarlo alli seria deshacer el
    cambio entero.
    """
    def run():
        try:
            work()
        except Exception:               # noqa: BLE001
            logger.exception('atomicdb: refresh thread died (%s)', name)
        finally:
            connections.close_all()

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


# Punto de sustitucion de los tests: lo unico que separa "en segundo plano"
# de "ahora mismo" es esta funcion.
EXECUTOR = background


def store_many(cache_keys, payloads, ttl_seconds):
    """Guarda payloads FECHADOS bajo la vida dura.

    La fecha es el dato que convierte una entrada en algo con edad: sin ella
    la unica pregunta que se le puede hacer al almacen es "¿sigue ahi?", y
    entonces el vencimiento vuelve a ser una espera para alguien.
    """
    stored_at = _now()
    cache.set_many(
        {cache_keys[token]: {'stored_at': stored_at, 'payload': payload}
         for token, payload in payloads.items()},
        ttl_seconds)


def revalidate(cache_keys, tokens, compute, *, ttl_seconds, lock_seconds,
               name):
    """Pide el turno de cada token y deja lanzado el recalculo de los que gane.

    Devuelve los tokens que se lleva, que es lo unico que un test necesita
    saber para comprobar que recalcula UNO.
    """
    locks = {token: cache_keys[token] + LOCK_SUFFIX for token in tokens}
    claimed = [token for token in tokens
               if cache.add(locks[token], 1, lock_seconds)]
    if not claimed:
        return []

    def work():
        try:
            fresh = compute(claimed)
            if fresh:
                store_many(cache_keys, fresh, ttl_seconds)
        except Exception:               # noqa: BLE001
            # Un refresco que revienta no puede tumbar a nadie: no hay nadie
            # esperandolo.  Se anota, se suelta el turno y lo reintenta el
            # siguiente que encuentre la entrada vieja.
            logger.exception('atomicdb: could not refresh %s', name)
        finally:
            cache.delete_many([locks[token] for token in claimed])

    EXECUTOR(work, name)
    return claimed


def resolve_many(cache_keys, compute, *, fresh_seconds, stale_seconds,
                 ttl_seconds, lock_seconds, name):
    """``{token: payload}`` de todo lo que se pudo resolver, sin esperas.

    ``cache_keys`` es ``{token: clave de cache}``.  El token es lo que maneja
    el llamante — una clave de posicion, un par (ruta, destino) — y como se
    convierte en clave de cache es cosa suya: aqui solo se usa para agrupar.

    ``compute(tokens) -> {token: payload}`` es la mitad cara.  Puede devolver
    MENOS tokens de los que se le piden, y lo que no devuelve NO SE GUARDA:
    asi es como se evita cachear una ausencia, que es una decision del
    llamante y no de este modulo.

    Las tres edades posibles de una entrada, y lo que se hace con cada una:

    * mas joven que ``fresh_seconds``: se sirve y ya esta;
    * entre ``fresh_seconds`` y ``stale_seconds``: SE SIRVE IGUAL, en el acto,
      y se lanza el recalculo por detras.  Esta es la linea entera del
      cambio: el vencimiento deja de ser una espera;
    * mas vieja que ``stale_seconds``: no se sirve.  Cuenta como ausente y se
      recalcula sincrono, porque el tope existe para el caso en que los
      refrescos fallen EN CADENA, y ahi es preferible esperar a afirmar algo
      de hace media hora.
    """
    if not cache_keys:
        return {}
    stored = cache.get_many(list(cache_keys.values()))
    now = _now()
    resolved, stale, missing = {}, [], []
    for token, key in cache_keys.items():
        entry = stored.get(key)
        if entry is None:
            missing.append(token)
            continue
        age = max(0.0, now - entry['stored_at'])
        if age > stale_seconds:
            missing.append(token)
            continue
        resolved[token] = entry['payload']
        if age > fresh_seconds:
            stale.append(token)

    if missing:
        fresh = compute(missing)
        if fresh:
            store_many(cache_keys, fresh, ttl_seconds)
            resolved.update(fresh)

    if stale:
        revalidate(cache_keys, stale, compute, ttl_seconds=ttl_seconds,
                   lock_seconds=lock_seconds, name=name)

    return resolved
