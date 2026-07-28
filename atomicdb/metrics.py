"""Small, honest compute snapshot for the public AtomicDB dashboard."""

import threading
import time
from datetime import timedelta

from django.db.models import Min
from django.utils import timezone

from .models import AnalysisTask, DBEvent, RequestLog, WorkerPing


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
