"""Small, honest compute snapshot for the public AtomicDB dashboard."""

import logging
import threading
import time
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

from .models import (AnalysisTask, DBEvent, Position, ProgressSnapshot,
                     RequestLog, WorkerPing)


logger = logging.getLogger(__name__)

LIVE_SECONDS = 180
RATE_MINUTES = 10
CACHE_SECONDS = 30
# Ventana de la latencia humana: siete dias.  Un cierre que tarda semanas es
# informacion, pero una mediana calculada sobre toda la historia deja de
# moverse — y lo que hay que ver aqui es si el despliegue de esta semana
# acorto la espera de un visitante.
HUMAN_CLOSE_WINDOW_DAYS = 7
# Tope de eventos que entran en esa mediana.  Una materializacion de lineas
# ganadoras puede cerrar miles de nodos en un rato; leerlos todos para
# calcular una mediana convertiria un KPI en una consulta cara.
HUMAN_CLOSE_MAX_EVENTS = 5_000
# Tamano de lote al cruzar claves contra ``RequestLog``.
REQUEST_LOOKUP_BATCH = 900

_lock = threading.Lock()
_cached_at = 0.0
_cached = None


def _compute(now):
    live_since = now - timedelta(seconds=LIVE_SECONDS)
    pings = list(WorkerPing.objects.filter(last_seen__gte=live_since)
                 .values('threads', 'current_task_id', 'last_nps',
                         'nps_updated'))
    cores = sum(max(0, row['threads'] or 0) for row in pings)
    # A fresh lease resets the rate, heartbeats then publish the current one.
    # Requiring a current task prevents an idle lease poll from reviving the
    # completed task's historical NPS.  NPS has its own freshness timestamp:
    # a worker heartbeat can remain live after engine progress has stopped.
    nps = sum(max(0, row['last_nps'] or 0) for row in pings
              if (row['current_task_id'] is not None
                  and row['nps_updated'] is not None
                  and row['nps_updated'] >= live_since))
    completed = AnalysisTask.objects.filter(
        state='COMPLETED',
        completed__gte=now - timedelta(minutes=RATE_MINUTES),
    ).count()
    return {
        'workers': len(pings),
        'cores': cores,
        'nps': nps,
        'positions_per_minute': completed / RATE_MINUTES,
        'live_seconds': LIVE_SECONDS,
        'rate_minutes': RATE_MINUTES,
    }


def worker_metrics(*, now=None, force=False):
    """Return a 30-second cached snapshot (or deterministic forced snapshot)."""
    global _cached_at, _cached
    if now is not None:
        return _compute(now)
    clock = time.monotonic()
    if not force and _cached is not None and clock - _cached_at < CACHE_SECONDS:
        return dict(_cached)
    with _lock:
        clock = time.monotonic()
        if (not force and _cached is not None
                and clock - _cached_at < CACHE_SECONDS):
            return dict(_cached)
        _cached = _compute(timezone.now())
        _cached_at = clock
        return dict(_cached)


def reset_metrics_cache():
    """Test/deploy helper; each server process otherwise refreshes naturally."""
    global _cached_at, _cached
    with _lock:
        _cached_at = 0.0
        _cached = None


# ---------------- de QUIEN son los cierres ----------------
#
# La atribucion la escribe ``ingest.closure_attribution`` en el payload del
# evento, en el instante del cierre, porque despues no es derivable: los
# cuatro caminos (selector AUTO, completado FILL, click USER, certificado
# SOLVE) escriben exactamente las mismas filas.  Aqui solo se cuenta.
#
# EMPIEZA EN CERO EN EL DESPLIEGUE, a proposito.  Un ``NODE_CLOSED`` anterior
# no lleva la clave y no se cuenta en ninguna categoria — ni siquiera en
# ``NONE``, que significa "esto no salio de ninguna tarea" y es una
# afirmacion, no un cajon de sastre.  Por eso la portada dice "desde el
# despliegue" y por eso estos cinco no suman ``positions_closed``.

def _closure_sources():
    from . import ingest
    return ingest.CLOSURE_SOURCES


def closure_attribution_totals(since=None):
    """``{fuente: cierres}`` sobre los eventos etiquetados.

    Un ``COUNT`` por fuente y ninguna fila transferida.  La alternativa —
    agrupar por la clave JSON en SQL — depende de como cada backend decodifica
    un ``KeyTransform``, y un KPI de portada no es sitio para averiguarlo.
    """
    events = DBEvent.objects.filter(kind='NODE_CLOSED')
    if since is not None:
        events = events.filter(ts__gte=since)
    return {name: events.filter(payload__source=name).count()
            for name in _closure_sources()}


def closure_attribution_window(*, now=None, hours=24):
    """La atribucion de una ventana reciente, con su denominador.

    ``stamped`` es la suma de los cinco y ``total`` TODOS los cierres de la
    ventana: la diferencia son los eventos anteriores al despliegue de la
    etiqueta, y verla es la unica forma de saber si un porcentaje ya cubre lo
    que dice cubrir.
    """
    now = now or timezone.now()
    since = now - timedelta(hours=hours)
    counted = closure_attribution_totals(since=since)
    return {
        'hours': hours,
        'sources': counted,
        'stamped': sum(counted.values()),
        'total': DBEvent.objects.filter(kind='NODE_CLOSED',
                                        ts__gte=since).count(),
    }


# ---------------- LO CARO DE LA PORTADA, FUERA DE LA PETICION ----------------
#
# QUE ESTABA MAL.  La portada calculaba en cada fallo de su cache de pagina
# un ``COUNT(*)`` de las posiciones, otro de las cerradas y un
# ``SUM(nodes_invested)``: tres barridos de la tabla mas grande del proyecto,
# 1,57 s medidos sobre 3,5M de filas, mas otro segundo largo en los diez
# ``COUNT`` de la atribucion de cierres.  Ninguno de esos numeros es de
# nadie ni cambia por quien mira, asi que pagarlos POR VISITA era gastar el
# mismo trabajo tantas veces como visitantes hubiera.
#
# QUE SE HACE.  Se calculan UNA vez para todo el sitio y se publican en la
# cache compartida.  Quien los publica en produccion es el servicio del
# selector, que ya corre en bucle fuera de la ruta HTTP (§ refresh_selector):
# con eso, ninguna visita paga jamas un barrido de tabla.  La web tiene su
# propia red por si ese servicio esta parado — refresca ella misma, y SOLO un
# proceso a la vez gracias al cerrojo — pero es la red, no el camino normal.
#
# QUE PRECIO TIENE, DICHO EN CLARO.  El numero que se pinta es un conteo
# EXACTO tomado hace un rato, no una estimacion: nada aqui usa ``reltuples``
# ni ningun otro atajo que pueda equivocarse en una cifra.  Lo unico que se
# cede es la edad, y la edad viaja CON el numero (``measured_at``) hasta el
# tooltip de la portada, para que quien lo lea pueda ver de cuando es.  Un
# total acumulado de tres millones y medio no se mueve de forma perceptible
# en noventa segundos; una cifra inventada si mentiria.
PUBLIC_COUNTERS_KEY = 'atomicdb.public-counters.v1'
ATTRIBUTION_KEY = 'atomicdb.closure-attribution.v1'
# Por encima del paso del selector (60 s) a proposito: mientras ese servicio
# viva, la entrada nunca llega a envejecer y ninguna visita recalcula nada.
PUBLIC_FRESH_SECONDS = 90
# Vida DURA de la entrada.  Muy por encima de la frescura: entre las dos hay
# una ventana en la que se sirve un valor viejo mientras alguien lo renueva,
# que es justo lo que impide que una portada sin trafico deje a la siguiente
# visita pagando el barrido entero.
PUBLIC_TTL_SECONDS = 3600
# El cerrojo dura mas que el calculo que protege (medido en ~2 s sobre la
# base real) y menos que la frescura, para que un proceso muerto a media
# medicion no bloquee el refresco mas de un ciclo.
PUBLIC_LOCK_SECONDS = 60


def _shared_snapshot(key, *, build, seed=None, required=False, now=None,
                     force=False, fresh_seconds=PUBLIC_FRESH_SECONDS,
                     ttl=PUBLIC_TTL_SECONDS):
    """Una entrada compartida, cara de calcular y barata de leer.

    Fresca: se sirve.  Vieja: se sirve igual y UN solo proceso la renueva
    (``cache.add`` como cerrojo).  Ausente: la calcula quien coja el cerrojo,
    y quien no lo coja se lleva ``seed()`` — un valor de respaldo honesto en
    vez de una espera.

    ``required`` es la diferencia entre las dos cosas que se guardan aqui.
    Los contadores TIENEN que salir con un numero: si no hay entrada, ni
    respaldo, ni cerrojo libre, se mide igualmente, porque pintar un cero es
    afirmar que el arbol esta vacio.  La atribucion de cierres no: ahi ``None``
    significa "no se ha medido" y la portada esconde el bloque, que es lo
    unico honesto que se puede hacer con un porcentaje que nadie ha calculado.
    """
    now = now or timezone.now()
    if force:
        return refresh_shared(key, build=build, now=now, ttl=ttl)
    entry = cache.get(key)
    if entry is not None:
        age = (now - entry['measured_at']).total_seconds()
        if age < fresh_seconds:
            return entry
    if cache.add(key + '.lock', 1, PUBLIC_LOCK_SECONDS):
        try:
            return refresh_shared(key, build=build, now=now, ttl=ttl)
        except Exception:                # noqa: BLE001
            # Un contador que no se puede medir no puede tumbar la portada:
            # se sirve lo que hubiera y se reintenta al vencer el cerrojo.
            logger.exception('atomicdb: could not refresh %s', key)
        finally:
            cache.delete(key + '.lock')
    if entry is not None:
        return entry
    seeded = None if seed is None else seed(now)
    if seeded is not None or not required:
        return seeded
    return refresh_shared(key, build=build, now=now, ttl=ttl)


def refresh_shared(key, *, build, now=None, ttl=PUBLIC_TTL_SECONDS):
    """Mide y publica.  Lo que llama el servicio del selector."""
    now = now or timezone.now()
    entry = dict(build(now))
    entry['measured_at'] = now
    cache.set(key, entry, ttl)
    return entry


def _measure_tree_totals(now):
    """Los tres agregados caros de ``Position``, en UN barrido.

    Eran tres consultas y tres recorridos de la misma tabla de 2,5 GB.  La
    suma de ``nodes_invested`` obliga a leer las filas de todas formas, asi
    que los dos conteos viajan gratis en ese mismo recorrido — y de paso los
    tres pasan a ser del MISMO instante, cosa que tres consultas separadas no
    garantizaban (el porcentaje resuelto se calculaba con un numerador y un
    denominador medidos en momentos distintos).
    """
    row = Position.objects.aggregate(
        total=Count('key'),
        closed=Count('key', filter=~Q(status='UNKNOWN')),
        nodes=Sum('nodes_invested'))
    return {'total': row['total'] or 0,
            'closed': row['closed'] or 0,
            'nodes': row['nodes'] or 0,
            'from_snapshot': False}


def _seed_tree_totals(now):
    """El respaldo cuando la cache esta vacia y otro proceso ya esta midiendo.

    Sale de la ultima captura horaria (§ ``capture_atomicdb_progress``), que
    es una consulta indexada y trae los MISMOS tres numeros, medidos de
    verdad en su momento.  Sigue sin haber ninguna estimacion por ningun
    lado: solo un conteo exacto mas viejo, con su fecha al lado.

    ``None`` cuando no hay ninguna captura todavia, y ese ``None`` es
    importante: quien llama mide de verdad en vez de pintar un cero.
    """
    snapshot = (ProgressSnapshot.objects.order_by('-bucket_start')
                .values('bucket_start', 'positions_total', 'positions_closed',
                        'engine_nodes_total').first())
    if snapshot is None:
        return None
    return {'total': snapshot['positions_total'],
            'closed': snapshot['positions_closed'],
            'nodes': snapshot['engine_nodes_total'],
            'measured_at': snapshot['bucket_start'],
            'from_snapshot': True}


def tree_totals(*, now=None, force=False):
    """``{'total', 'closed', 'nodes', 'measured_at'}`` para la portada."""
    return _shared_snapshot(PUBLIC_COUNTERS_KEY, build=_measure_tree_totals,
                            seed=_seed_tree_totals, required=True, now=now,
                            force=force)


def _measure_attribution(now):
    return {'day': closure_attribution_window(now=now, hours=24),
            'week': closure_attribution_window(now=now, hours=24 * 7)}


def attribution_windows(*, now=None, force=False):
    """Las dos ventanas de atribucion de cierres, compartidas.

    Doce ``COUNT`` sobre ``atomicdb_dbevent`` filtrando por una clave del
    payload.  Se quedan tal cual estan escritos — agrupar por la clave JSON en
    SQL depende de como decodifique cada backend un ``KeyTransform``, y eso no
    se averigua en la portada (§ ``closure_attribution_totals``) — pero se
    pagan una vez por ciclo y no una vez por visitante.

    Sin valor de respaldo: ``None`` significa "no se ha medido", y la portada
    esconde el bloque en vez de pintar ceros que se leerian como "nadie ha
    cerrado nada".
    """
    return _shared_snapshot(ATTRIBUTION_KEY, build=_measure_attribution,
                            now=now, force=force)


def refresh_public_snapshot(*, now=None):
    """Republica TODO lo caro de la portada.  Lo llama el servicio del selector.

    Devuelve las claves refrescadas, para que el ciclo pueda registrarlo.
    """
    now = now or timezone.now()
    refresh_shared(PUBLIC_COUNTERS_KEY, build=_measure_tree_totals, now=now)
    refresh_shared(ATTRIBUTION_KEY, build=_measure_attribution, now=now)
    return (PUBLIC_COUNTERS_KEY, ATTRIBUTION_KEY)


def reset_public_snapshot():
    """Tira lo publicado (tests y despliegues)."""
    cache.delete_many([PUBLIC_COUNTERS_KEY, PUBLIC_COUNTERS_KEY + '.lock',
                       ATTRIBUTION_KEY, ATTRIBUTION_KEY + '.lock'])


def human_close_latency(*, now=None, days=HUMAN_CLOSE_WINDOW_DAYS,
                        max_events=HUMAN_CLOSE_MAX_EVENTS):
    """Mediana de segundos entre la PRIMERA peticion humana y el cierre.

    Mide lo unico que un visitante nota: pedi esta posicion, cuanto tardo en
    quedar resuelta.  Se toma la peticion mas ANTIGUA de cada posicion — la
    que de verdad empezo la espera — y el cierre de la ventana; una posicion
    pedida despues de cerrarse no cuenta (delta negativo), porque ahi el
    humano no espero nada.

    Devuelve ``{'median_seconds', 'samples'}``.  El tamano de muestra sale al
    lado del numero siempre: una mediana de dos casos no es una propiedad de
    la flota, y quien la lea tiene que poder verlo.
    """
    now = now or timezone.now()
    since = now - timedelta(days=days)
    events = list(DBEvent.objects.filter(kind='NODE_CLOSED', ts__gte=since)
                  .order_by('-ts').values_list('payload', 'ts')[:max_events])
    closed_at = {}
    for payload, ts in events:
        key = (payload or {}).get('key') if isinstance(payload, dict) else None
        if not key:
            continue
        # El evento mas ANTIGUO de la ventana es el cierre real; los
        # posteriores sobre la misma clave serian re-cierres tras revocacion.
        closed_at[key] = ts
    if not closed_at:
        return {'median_seconds': 0, 'samples': 0}
    # Por lotes: un ``IN`` con cinco mil claves de sha256 es una sentencia de
    # 350 KB y, en SQLite, coquetea con el tope de variables ligadas.  El
    # troceado es la diferencia entre un KPI que escala y uno que un dia falla
    # entero por haber tenido demasiado exito.
    keys = list(closed_at)
    deltas = []
    for start in range(0, len(keys), REQUEST_LOOKUP_BATCH):
        batch = keys[start:start + REQUEST_LOOKUP_BATCH]
        rows = (RequestLog.objects.filter(position_id__in=batch)
                .values('position_id').annotate(first=Min('created')))
        deltas.extend(
            int((closed_at[row['position_id']] - row['first']).total_seconds())
            for row in rows
            if closed_at[row['position_id']] >= row['first'])
    deltas.sort()
    if not deltas:
        return {'median_seconds': 0, 'samples': 0}
    middle = len(deltas) // 2
    median = (deltas[middle] if len(deltas) % 2
              else (deltas[middle - 1] + deltas[middle]) // 2)
    return {'median_seconds': int(median), 'samples': len(deltas)}
