"""Small, honest compute snapshot for the public AtomicDB dashboard."""

import logging
import threading
import time
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

from . import revalidate
from .models import (AnalysisTask, Campaign, DBEvent, Position,
                     ProgressSnapshot, RequestLog, WorkerPing)


logger = logging.getLogger(__name__)

# El hilo que renueva y CIERRA LO QUE ABRE vive en § revalidate, que es de
# donde lo saca tambien § page_cache: un invariante que se puede violar en un
# sitio y no en el otro no es un invariante.  Es ademas el punto de
# sustitucion de los tests — lo unico que separa "por detras" de "ahora
# mismo" es esta funcion.
REVALIDATE_EXECUTOR = revalidate.background

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
# Los cuatro numeros de ACTIVIDAD que quedaban en vivo, y por que dejan de
# estarlo.  Se dejaron fuera por baratos ("consultas indexadas"), y a 3,5M de
# filas lo eran; a 12,8M ninguno de los cuatro se resuelve ya sin recorrer:
#
#   * ``analyses``    COUNT de TODAS las tareas COMPLETED: el indice
#                     ``atomic_task_state_done`` lo sirve, pero servirlo es
#                     recorrer una entrada por tarea completada del proyecto.
#   * ``requested``   COUNT con JOIN a ``Position`` para mirar el status de
#                     cada peticion PENDING de la banda USER.
#   * ``closed_24h``  COUNT de eventos de cierre de la ventana.
#   * ``nodes_24h``   SUM sobre las completadas de la ventana; el sumando no
#                     esta en el indice, asi que cada fila es una visita mas.
#
# Ninguno es de nadie y ninguno cambia por quien mira.  Se miden una vez por
# ciclo del servicio y se leen de la cache compartida, igual que los tres de
# arriba.  LA FRESCURA QUE SE CEDE ESTA ACOTADA Y ES LA MISMA: hasta
# ``PUBLIC_FRESH_SECONDS``, y el conteo sigue siendo EXACTO — de hace un rato,
# nunca estimado.  La portada ya iba con una cache de pagina de 15 s, asi que
# "en vivo" nunca significo "de este milisegundo".
PUBLIC_ACTIVITY_KEY = 'atomicdb.public-activity.v1'
# El progreso de las campanas ACTIVAS.  Tres conteos agrupados sobre las
# posiciones etiquetadas: una campana popular etiqueta cientos de miles de
# filas y los dos ``Count`` condicionados obligan a mirar ``status`` y
# ``nodes_invested`` de cada una.  Mismo trato que el resto: publico, derivado
# y de nadie.
CAMPAIGN_PROGRESS_KEY = 'atomicdb.campaign-progress.v1'
# Se escogio por encima del paso del selector (60 s) contando con que ese
# servicio la renovara antes de envejecer.  YA NO LO HACE: una pasada del
# selector dura mas de una hora (medido: 3.618 s y 4.724 s los dias 7-ago), y
# publica una vez por pasada, no cada minuto.  El timer de § Documentation/
# atomicdb-snapshot la republica cada 5 min, que es MAS que estos 90 s: la
# entrada pasa vieja la mayor parte del ciclo.  Eso ya no le cuesta una
# espera a nadie — vieja se sirve y se renueva por detras — pero el numero
# se queda en 90 porque sigue siendo lo que decide cuando SE MIDE otra vez.
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


def shared_snapshot(key, *, build, seed=None, required=False, now=None,
                    force=False, fresh_seconds=PUBLIC_FRESH_SECONDS,
                    ttl=PUBLIC_TTL_SECONDS):
    """Una entrada compartida, cara de calcular y barata de leer.

    Fresca: se sirve.  Vieja: SE SIRVE IGUAL, en el acto, y UN solo proceso la
    renueva POR DETRAS (``cache.add`` como cerrojo).  Ausente: la calcula
    quien coja el cerrojo, y quien no lo coja se lleva ``seed()`` — un valor
    de respaldo honesto en vez de una espera.

    QUE CAMBIO Y POR QUE.  La renovacion de "vieja" la hacia el propio lector,
    SINCRONA, y por eso la portada tenia dos precios muy distintos: 0,3 s
    cuando la entrada estaba fresca y el barrido entero para el primero que
    llegara pasados los 90 s.  Medido el 7-ago sobre la base real, ese barrido
    son 14,4 s de ``_measure_tree_totals`` mas 6,8 s de ``_measure_attribution``
    — 21 s que pagaba un visitante elegido por el azar de haber llegado el
    primero.  Con trafico esporadico ese azar cae seguido: entre visita y
    visita pasan mas de 90 s casi siempre.

    Lo que NO cambia es cuan vieja puede ser la cifra que se sirve.  El tope
    lo pone la vida dura (``ttl``), igual que antes, y ya era alcanzable: un
    lector que se encontraba el cerrojo cogido se llevaba la entrada vieja
    tal cual, sin mirar cuanto lo era.  Lo unico que cambia es QUIEN espera, y
    la respuesta ahora es nadie.  Es el mismo trato que § page_cache le da a
    la pagina, y § revalidate ya senalaba este modulo como el sitio donde
    seguia pagandolo el que llegaba.

    ``required`` es la diferencia entre las dos cosas que se guardan aqui.
    Los contadores TIENEN que salir con un numero: si no hay entrada, ni
    respaldo, ni cerrojo libre, se mide igualmente, porque pintar un cero es
    afirmar que el arbol esta vacio.  La atribucion de cierres no: ahi ``None``
    significa "no se ha medido" y la portada esconde el bloque, que es lo
    unico honesto que se puede hacer con un porcentaje que nadie ha calculado.

    Es publica porque tiene un lector de FUERA de este modulo: el snapshot de
    flota de ``contributors`` guardaba lo mismo con un ``cache.get``/``set``
    pelado y le faltaban las dos propiedades que aqui estan escritas una sola
    vez — servir lo viejo mientras uno renueva, y que renueve UNO.
    """
    now = now or timezone.now()
    if force:
        return refresh_shared(key, build=build, now=now, ttl=ttl)
    entry = cache.get(key)
    if entry is not None:
        age = (now - entry['measured_at']).total_seconds()
        if age >= fresh_seconds:
            # Vieja pero SERVIBLE: se sirve tal cual y se renueva por detras.
            # Antes la renovaba el propio lector, sincrona, y por eso la
            # portada tenia dos precios: 0,3 s casi siempre y el barrido
            # entero para el primero que llegaba pasados los 90 s.
            _revalidate(key, build=build, ttl=ttl)
        return entry
    # AUSENTE es otra cosa: no hay nada que servir, asi que aqui si se mide en
    # la peticion.  Lo acota la vida dura (``ttl``), no esta guarda.
    if cache.add(key + '.lock', 1, PUBLIC_LOCK_SECONDS):
        try:
            return refresh_shared(key, build=build, now=now, ttl=ttl)
        except Exception:                # noqa: BLE001
            # Un contador que no se puede medir no puede tumbar la portada:
            # se sirve lo que hubiera y se reintenta al vencer el cerrojo.
            logger.exception('atomicdb: could not refresh %s', key)
        finally:
            cache.delete(key + '.lock')
    seeded = None if seed is None else seed(now)
    if seeded is not None or not required:
        return seeded
    return refresh_shared(key, build=build, now=now, ttl=ttl)


def _revalidate(key, *, build, ttl):
    """Pide el turno y, si lo consigue, deja la medida lanzada por detras.

    Devuelve si la lanzo, que es lo unico que los tests necesitan saber para
    comprobar que mide UNO.  El cerrojo es el de siempre y con la misma vida,
    asi que un proceso que muera a media medida no bloquea mas de un ciclo.
    """
    lock = key + '.lock'
    if not cache.add(lock, 1, PUBLIC_LOCK_SECONDS):
        return False

    def work():
        try:
            refresh_shared(key, build=build, ttl=ttl)
        except Exception:                # noqa: BLE001
            # Nadie la espera: se anota, se suelta el cerrojo y lo reintenta
            # el siguiente lector que la encuentre vieja.
            logger.exception('atomicdb: could not refresh %s', key)
        finally:
            cache.delete(lock)

    REVALIDATE_EXECUTOR(work, 'atomicdb-snapshot-refresh')
    return True


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
    return shared_snapshot(PUBLIC_COUNTERS_KEY, build=_measure_tree_totals,
                           seed=_seed_tree_totals, required=True, now=now,
                           force=force)


def _measure_activity(now):
    """Los cuatro contadores de actividad, en el mismo instante.

    Se miden juntos a proposito: "analisis completados" y "cierres de las
    ultimas 24h" contados en momentos distintos son dos fotos que un lector
    lee como una, y aqui no cuesta nada que sean la misma.
    """
    day_ago = now - timedelta(hours=24)
    return {
        'analyses': AnalysisTask.objects.filter(state='COMPLETED').count(),
        'requested': AnalysisTask.objects.filter(
            state='PENDING', source='USER',
            position__status='UNKNOWN').count(),
        'closed_24h': DBEvent.objects.filter(kind='NODE_CLOSED',
                                             ts__gte=day_ago).count(),
        'nodes_24h': AnalysisTask.objects.filter(
            state='COMPLETED', completed__gte=day_ago).aggregate(
                n=Sum('nodes_searched'))['n'] or 0,
    }


def activity_totals(*, now=None, force=False):
    """``{'analyses', 'requested', 'closed_24h', 'nodes_24h'}``, compartidos.

    ``required`` y SIN respaldo: la captura horaria no trae estos cuatro, asi
    que la unica alternativa a medirlos seria pintar ceros — y un cero aqui
    afirma que la flota no ha analizado nada nunca.  Con la cache caliente,
    que es el caso de todas las visitas mientras el servicio corra, esto no
    cuesta ni una consulta.
    """
    return shared_snapshot(PUBLIC_ACTIVITY_KEY, build=_measure_activity,
                           required=True, now=now, force=force)


def _campaign_row(row):
    return {'total': row.get('total') or 0,
            'explored': row.get('explored') or 0,
            'resolved': row.get('resolved') or 0}


def _measure_campaign_progress(now):
    """Los tres conteos de CADA campana activa, en una consulta agrupada.

    Las campanas activas se listan aqui dentro y no las trae quien pregunta:
    el snapshot tiene que poder distinguir "esta campana tiene cero
    posiciones" de "esta campana no existia cuando se midio", y para eso la
    lista de ids conocidos forma parte de la medida.  Sin esa distincion, una
    campana recien activada se pintaria a cero durante minuto y medio.
    """
    active = list(Campaign.objects.filter(state=Campaign.CState.ACTIVE)
                  .values_list('id', flat=True))
    totals = {campaign_id: {'total': 0, 'explored': 0, 'resolved': 0}
              for campaign_id in active}
    if active:
        for row in (Position.objects.filter(campaign_id__in=active)
                    .values('campaign_id')
                    .annotate(total=Count('key'),
                              explored=Count('key',
                                             filter=Q(nodes_invested__gt=0)),
                              resolved=Count('key',
                                             filter=~Q(status='UNKNOWN')))):
            totals[row['campaign_id']] = _campaign_row(row)
    return {'totals': totals}


def campaign_progress(campaign_ids, *, now=None, force=False):
    """``{id: {'total', 'explored', 'resolved'}}`` de las campanas pedidas.

    Del snapshot lo que el snapshot conozca; lo que no — una campana activada
    despues de la ultima medida — se mide en el acto, que es una campana
    todavia pequena y una sola vez por ciclo.
    """
    snapshot = shared_snapshot(CAMPAIGN_PROGRESS_KEY,
                               build=_measure_campaign_progress,
                               required=True, now=now, force=force)
    known = snapshot['totals']
    rows = {}
    for campaign_id in campaign_ids:
        row = known.get(campaign_id)
        if row is None:
            row = _campaign_row(Position.objects.filter(
                campaign_id=campaign_id).aggregate(
                    total=Count('key'),
                    explored=Count('key', filter=Q(nodes_invested__gt=0)),
                    resolved=Count('key', filter=~Q(status='UNKNOWN'))))
        rows[campaign_id] = row
    return rows


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
    return shared_snapshot(ATTRIBUTION_KEY, build=_measure_attribution,
                           now=now, force=force)


# TODO lo que la portada agrega, en UNA lista.  Publicar es recorrerla; tirar
# lo publicado tambien.  Anadir un agregado y olvidarse de una de las dos
# cosas es exactamente como se acaba teniendo un numero que el servicio no
# refresca o un test que ve la medida del test anterior.
PUBLIC_SNAPSHOTS = (
    (PUBLIC_COUNTERS_KEY, lambda: _measure_tree_totals),
    (ATTRIBUTION_KEY, lambda: _measure_attribution),
    (PUBLIC_ACTIVITY_KEY, lambda: _measure_activity),
    (CAMPAIGN_PROGRESS_KEY, lambda: _measure_campaign_progress),
)


def _fleet_snapshot():
    """La medida de flota, importada tarde para no cerrar un ciclo.

    ``contributors`` lee de aqui (``shared_snapshot``), asi que este modulo no
    puede leer de el en el import.  Mismo patron que ``_closure_sources``.
    """
    from . import contributors
    return contributors.FLEET_CACHE_KEY, contributors.measure_fleet


def refresh_public_snapshot(*, now=None):
    """Republica TODO lo caro de la portada.  Lo llama el servicio del selector.

    Devuelve las claves refrescadas, para que el ciclo pueda registrarlo.
    """
    now = now or timezone.now()
    keys = []
    for key, build in PUBLIC_SNAPSHOTS:
        refresh_shared(key, build=build(), now=now)
        keys.append(key)
    fleet_key, fleet_build = _fleet_snapshot()
    refresh_shared(fleet_key, build=fleet_build, now=now)
    keys.append(fleet_key)
    return tuple(keys)


def reset_public_snapshot():
    """Tira lo publicado (tests y despliegues)."""
    keys = [key for key, _build in PUBLIC_SNAPSHOTS]
    keys.append(_fleet_snapshot()[0])
    cache.delete_many([entry for key in keys
                       for entry in (key, key + '.lock')])


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
