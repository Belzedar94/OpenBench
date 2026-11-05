"""La pagina publica de un CONTRIBUIDOR: su cola, sus maquinas y su huella.

QUE ES.  Una pagina por cuenta con actividad en AtomicDB — peticiones de
analisis, workers o nombres propuestos — a la que se llega desde la cabecera
(``/atomicdb/me/``) o por su URL directa.  Todo lo que ensena ya es publico:
la cola de peticiones se ve en la portada, la flota en los medidores y el
buzon de nombres en su pagina; lo unico que hace esto es agruparlo POR
PERSONA.  Lo que NO sale es la IP de una sugerencia, que es el unico dato
personal que guarda ese modelo.

DE DONDE SALE CADA COSA.  La propiedad de una maquina es ``WorkerPing.user``
y nunca el prefijo del string ``machine``: un worker se presenta con el
nombre que quiere, dos personas pueden compartir nombre de host y una
maquina con ``--jobs`` se parte en slots (``base#3``).  La autoria de una
peticion es ``AnalysisTask.requested_by``, que es lo que escribe el
explorador cuando quien pincha tiene sesion.

POR QUE UN AGREGADO COMPARTIDO Y CACHEADO.  Los totales por maquina no son de
nadie: la misma consulta contesta "cuantos nodos ha puesto esta maquina" y
"que puesto ocupa esta cuenta entre todas".  Se calculan UNA vez por minuto
para todo el sitio (dos GROUP BY sobre el indice de tareas completadas) y de
ahi salen la tabla de maquinas, la cuota de flota y el ranking.  La
alternativa — una tanda de consultas por cuenta y por visita — repetiria el
mismo trabajo tantas veces como visitantes haya, y esta pagina
deliberadamente NO lleva cache de pagina (§ urls): es personal.
"""

import hashlib
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.timesince import timesince

from . import live_request
from .models import AnalysisTask, OpeningNameSuggestion, WorkerPing


# Un worker manda heartbeat cada ~30-60s.  Cuatro minutos de silencio ya no es
# un ciclo lento: es una maquina apagada.  Mas generoso que los 180s de los
# medidores de la portada a proposito — alli se cuenta capacidad viva, aqui se
# le dice a una persona si su ordenador esta puesto.
ONLINE_SECONDS = 240
# La velocidad de una maquina es la de sus busquedas de la ULTIMA HORA, aqui y
# en la pagina de workers.  La media de toda la vida no se movia cuando la
# maquina cambiaba: mas rapida tras una mejora, o con mas hilos por ranura tras
# otro ``--jobs`` (Eclipsia y Torom en #suggestions, 1-nov).  Una hora son
# decenas de busquedas para una ranura normal.
SPEED_MINUTES = 60
# Listas acotadas: esto es una pagina de cortesia, no un registro de auditoria.
QUEUE_ROWS = 20
SUGGESTION_ROWS = 20
# Dos semanas: suficiente para ver una racha y para que un dia flojo no
# desaparezca dentro de una barra mensual.
SPARK_DAYS = 14
SPARK_WIDTH = 280
SPARK_HEIGHT = 40
SPARK_BASELINE = 38
SPARK_MAX_BAR = 34
# FRESCURA de la entrada de flota, no su vida: pasado este minuto la entrada
# se sigue sirviendo mientras alguien la renueva (§ ``fleet``).  Sesenta
# segundos porque es la cadencia del servicio que la publica.
FLEET_CACHE_SECONDS = 60
FLEET_CACHE_KEY = 'atomicdb.contributors.fleet.v1'
# La portada ensena DIEZ y dice cuantas quedan fuera.  La lista completa seria
# un directorio, y lo que hace falta en una portada es el cabecero honesto y
# un camino a la pagina de cada cual.
LEADERBOARD_ROWS = 10
# The profile's workers table: the most recently seen machines, and a "Show
# all" for the rest.  An account with dozens of slots made the page long and
# paid a lease and speed read for every one of them.
WORKER_ROWS = 10
# Per-account aggregates that are scans growing with the account (§
# ``_account_cached``): the daily chart, the request counts, the first date.
ACCOUNT_CACHE_SECONDS = 300
ACCOUNT_COUNT_SECONDS = 60
FIRST_CACHE_SECONDS = 3600
# Oro, plata y bronce sin escribir ninguna de las tres palabras y sin un solo
# emoji: el numero del puesto se pinta con los colores que el sitio ya tiene.
# Un empate en el segundo puesto da DOS platas y ningun bronce, que es
# exactamente lo que significa compartir puesto.
MEDALS = {1: 'first', 2: 'second', 3: 'third'}
# Clubes de nodos.  De mayor a menor: se pintan todos los alcanzados, y el
# primero de la lista es el que manda en el titulo.
MILESTONES = ((10_000_000_000_000, '10T club'),
              (1_000_000_000_000, '1T club'),
              (100_000_000_000, '100B club'),
              (1_000_000_000, '1B club'))

CANCELLED = AnalysisTask.TState.CANCELLED
COMPLETED = AnalysisTask.TState.COMPLETED
LEASED = AnalysisTask.TState.LEASED
PENDING = AnalysisTask.TState.PENDING
USER = AnalysisTask.Source.USER


def _requests_of(username):
    """Las peticiones de una cuenta que TODAVIA cuentan como suyas.

    Lo RETIRADO queda fuera, y esta es la unica linea del modulo que tiene que
    saberlo: las tres listas de la cola filtran por estado y una fila cancelada
    no esta en ninguno de los tres, pero el medidor de "requests made" contaba
    filas a secas y habria seguido contando lo que su autor deshizo.  Esa cifra
    vive en una rejilla de medidores de CONTRIBUCION, al lado de los nodos y de
    los analisis servidos: una peticion retirada no es ninguna de las dos cosas.
    """
    return AnalysisTask.objects.filter(source=USER,
                                       requested_by=username).exclude(
        state=CANCELLED)


def _ago(moment, now):
    """"3 minutes ago" en UNA unidad, o "just now". Igual que la campana."""
    if moment is None:
        return ''
    text = timesince(moment, now=now, depth=1)
    if text.split()[:1] == ['0']:
        return 'just now'
    return f'{text} ago'


def _totals(owner, since=None):
    """Totales por MAQUINA y por CUENTA de una ventana, en UN solo GROUP BY.

    Agrupar por ``(machine, delivered_by)`` cuesta lo mismo que agrupar por
    maquina (el indice recorrido es el de ``(state, completed)`` igual, y la
    cardinalidad que vuelve sigue siendo de decenas), y de esa unica pasada
    salen las dos vistas: la de maquinas sumando sus estampas, y la de
    cuentas atribuyendo cada grupo a su cuenta real.  Cuando la atribucion
    estampada llego (§ ``delivered_by``) esto eran consultas separadas y el
    refresco del snapshot paso de dos barridos caros a seis; el warmer de
    cinco minutos convertia cada uno en un pico de latencia de portada.
    """
    rows = AnalysisTask.objects.filter(state=COMPLETED)
    if since is not None:
        rows = rows.filter(completed__gte=since)
    rows = (rows.values('machine', 'delivered_by')
            .annotate(nodes=Sum('nodes_searched'), tasks=Count('id'),
                      seconds=Sum('elapsed_seconds')).order_by())
    machines, users = {}, {}
    for row in rows:
        m = machines.setdefault(row['machine'],
                                {'nodes': 0, 'tasks': 0, 'seconds': 0.0})
        m['nodes'] += row['nodes'] or 0
        m['tasks'] += row['tasks'] or 0
        m['seconds'] += row['seconds'] or 0.0
        # La cuenta REAL del grupo: la estampada si la hay (un hecho), y si
        # la fila es anterior al estampado, el dueno sin disputa de la
        # maquina (una inferencia honesta).  Sin ninguna de las dos, los
        # nodos cuentan para la flota y para nadie mas.
        user = row['delivered_by'] or owner.get(row['machine'])
        if user:
            _fold(users, user, row['nodes'], row['tasks'])
    return machines, users


def _owner_of_machine():
    """``{maquina: cuenta}`` SOLO donde el nombre no esta en disputa.

    El nombre de maquina lo ELIGE el worker, asi que dos cuentas pueden
    anunciar el mismo (paso el 16-ago: soothdest y NitroColoraze declarando
    los dos "NitroColoraze-zen4").  El ``dict`` plano de antes se quedaba con
    una fila arbitraria, y la portada atribuia los nodos de esa maquina a
    quien no los busco — asi aparecio de contribuidor una cuenta que jamas
    entrego nada.  Una maquina con dos duenos declarados no tiene dueno
    conocido: sus filas viejas quedan sin atribuir, exactamente como las de
    una maquina sin ``WorkerPing``.
    """
    claims = {}
    for machine, user in WorkerPing.objects.values_list('machine', 'user'):
        claims.setdefault(machine, set()).add(user)
    return {machine: next(iter(users))
            for machine, users in claims.items() if len(users) == 1}


def _fold(folded, account, nodes, tasks):
    row = folded.setdefault(account, {'nodes': 0, 'tasks': 0})
    row['nodes'] += nodes or 0
    row['tasks'] += tasks or 0


def measure_fleet(now):
    """Totales por maquina y por cuenta, 24h y all-time.  DOS GROUP BY caros.

    El de "all time" agrupa TODAS las tareas completadas del proyecto y suma
    tres columnas que no estan en el indice: a 12,8M de posiciones es el
    trabajo mas caro que se hacia dentro de una peticion HTTP en todo el
    sitio.  No entra en la portada porque la portada lo necesite vivo — no lo
    necesita — sino porque era lo unico que quedaba sin publicar desde fuera.
    Cada ventana paga UNA pasada y de ella salen maquinas y cuentas (§
    ``_totals``): la atribucion estampada no anade barridos, solo una columna
    al GROUP BY.
    """
    owner = _owner_of_machine()
    machines_all, users_all = _totals(owner)
    machines_24h, users_24h = _totals(owner, since=now - timedelta(hours=24))
    return {
        'machines_all': machines_all,
        'machines_24h': machines_24h,
        'users_all': users_all,
        'users_24h': users_24h,
        'nodes_24h': sum(row['nodes'] for row in machines_24h.values()),
        'nodes_all': sum(row['nodes'] for row in machines_all.values()),
    }


def fleet(now=None):
    """Snapshot compartido: totales por maquina y por cuenta, 24h y all-time.

    QUE CAMBIA RESPECTO AL ``cache.get``/``set`` de antes, y por que importa.
    Era un minuto de cache sin nada mas: al vencer, el visitante que llegaba
    primero pagaba los dos GROUP BY enteros DENTRO de su peticion, y como no
    habia cerrojo, todos los que llegaban mientras tanto los pagaban tambien
    — cinco procesos de gunicorn recorriendo a la vez la tabla de tareas para
    escribir el mismo diccionario.  Con la maquinaria compartida
    (§ metrics.shared_snapshot) una entrada vieja se SIRVE mientras UNO sola
    la renueva, y en produccion ni siquiera la renueva un visitante: la
    publica el servicio del selector en su propio ciclo.

    La ventana de 24h se mueve dentro de la entrada, que es el mismo trato que
    ya tienen los medidores de la portada: un indicador de tendencia no cambia
    ninguna decision por sesenta segundos.

    ``required``: esta pagina y el ranking de la portada tienen que salir con
    numeros.  Sin entrada y con el cerrojo cogido se mide, igual que los
    contadores publicos.
    """
    from . import metrics
    return metrics.shared_snapshot(FLEET_CACHE_KEY, build=measure_fleet,
                                   required=True, now=now,
                                   fresh_seconds=FLEET_CACHE_SECONDS)


def reset_cache():
    """Tira el snapshot compartido (tests y despliegues).

    El cerrojo se va con el: dejarlo puesto haria que la siguiente lectura se
    encontrase sin entrada y con el refresco "ya cogido" por un proceso que no
    existe.
    """
    cache.delete_many([FLEET_CACHE_KEY, FLEET_CACHE_KEY + '.lock'])


def _rank(totals, username):
    """``{'rank': 2, 'of': 7}`` por nodos, o ``None`` si no ha puesto ninguno.

    Empates COMPARTEN puesto: dos cuentas con los mismos nodos son las dos
    segundas, y la siguiente es cuarta.  Un desempate por cualquier otra cosa
    seria un orden inventado.
    """
    mine = totals.get(username, {}).get('nodes', 0)
    if not mine:
        return None
    ladder = sorted((row['nodes'] for row in totals.values() if row['nodes']),
                    reverse=True)
    return {'rank': ladder.index(mine) + 1, 'of': len(ladder)}


def _standings(totals, fleet_nodes, limit=LEADERBOARD_ROWS):
    """Una ventana del ranking: filas ya ordenadas, con puesto y pie.

    El puesto sale de la MISMA escalera que ``_rank`` — la lista de valores
    con sus repeticiones — asi que dos cuentas con los mismos nodos son las
    dos segundas y la siguiente es cuarta.  Dentro de un empate se imprime por
    nombre: una lista hay que escribirla en algun orden, pero el numero que se
    ve es el mismo para las dos y no hay desempate escondido.

    El denominador de la cuota es la flota ENTERA, maquinas sin cuenta
    incluidas (§ ``_by_owner``): esos nodos se buscaron de verdad.  Por eso las
    cuotas pueden no sumar cien, y el pie dice cuanto falta en vez de
    repartirlo entre quienes si tienen cuenta.
    """
    ladder = sorted((row['nodes'] for row in totals.values() if row['nodes']),
                    reverse=True)
    ranked = sorted(((row['nodes'], name) for name, row in totals.items()
                     if row['nodes']), key=lambda pair: (-pair[0], pair[1]))
    rows = []
    for nodes, name in ranked[:limit]:
        rank = ladder.index(nodes) + 1
        share = round(100.0 * nodes / fleet_nodes, 1) if fleet_nodes else 0.0
        rows.append({
            'rank': rank,
            'medal': MEDALS.get(rank, ''),
            'name': name,
            'nodes_h': human(nodes),
            'tasks_h': human(totals[name]['tasks']),
            'share': share,
            # Una cuota REAL que redondea a cero se escribe "<0.1", nunca
            # "0.0": ciento veintitres millones de nodos son ciento veintitres
            # millones aunque la flota entera lleve un billon, y un cero
            # rotundo al lado de una cifra que no lo es se lee como que esa
            # persona no puso nada.  Misma disciplina que la barra de un pixel
            # que conserva el dia flojo en la grafica de contribuidor.
            'share_h': f'{share:.1f}' if share else '<0.1',
        })
    claimed = sum(row['nodes'] for row in totals.values())
    unclaimed = max(0, fleet_nodes - claimed)
    return {
        'rows': rows,
        'more': len(ladder) - len(rows),
        'accounts': len(ladder),
        'fleet_h': human(fleet_nodes),
        'unclaimed_h': human(unclaimed) if unclaimed else '',
    }


# The home page's short boards (seaside_cliff, 3-Nov-2026: "bring back
# contributors to home page").
HOME_LEADERBOARD_ROWS = 5
# The contributors page: the all-time board keeps every account, the ones
# past LEADERBOARD_ROWS behind a "show more" (seaside_cliff, 3-Nov-2026).
FULL_LEADERBOARD_ROWS = 500


def leaderboard(now=None, limit=LEADERBOARD_ROWS, all_time_limit=None):
    """Las dos tablas de la portada, del MISMO snapshot que ya se calcula.

    Ni una consulta por fila ni una consulta por ventana: los dos ordenes
    salen de los dos diccionarios que ``fleet`` ya trae, asi que lo unico que
    la portada paga por esta seccion es ese snapshot compartido — tres
    consultas por minuto para todo el sitio — y nada que crezca con el numero
    de contribuidores.  Encima va la cache de pagina de 15s (§ urls), que es
    la que absorbe las tormentas de F5.

    Las claves llevan prefijo ``contrib_``: la portada ya usa ``board`` para
    el tablero de la posicion inicial y dos cosas distintas con el mismo
    nombre en el mismo contexto es una plantilla que se rompe callando.
    """
    snapshot = fleet(now=now)
    boards = [
        {'window': 'Last 24 hours',
         'empty': 'No worker has searched anything in the last 24 hours.',
         **_standings(snapshot['users_24h'], snapshot['nodes_24h'], limit)},
        {'window': 'All time',
         'empty': 'No worker has searched anything yet.',
         **_standings(snapshot['users_all'], snapshot['nodes_all'],
                      all_time_limit or limit)},
    ]
    return {'contrib_boards': boards,
            'has_contrib_board': any(board['rows'] for board in boards)}


def has_activity(username):
    """Existe pagina para esta cuenta?  Workers, peticiones o nombres."""
    if not username:
        return False
    return (WorkerPing.objects.filter(user=username).exists()
            or _requests_of(username).exists()
            or OpeningNameSuggestion.objects.filter(
                suggested_by=username).exists())


def _queue_rows(username):
    """Las tres caras de la cola de esta cuenta, sin etiquetar todavia.

    SOLO ``source=USER``: una tarea AUTO nace del selector y no es de nadie,
    aunque la maquina que la sirva sea suya — eso se cuenta en la flota, que es
    otra seccion y otra pregunta.
    """
    mine = _requests_of(username)
    # Cuantas peticiones cobran antes que esta.  Mismo orden que
    # ``choose_pending``, leido del mismo sitio: desde el reparto justo del
    # 7-ago el sitio en la cola ya no es "cuantas hay con id menor", asi que
    # contarlo aqui a mano volveria a mentirle a quien mira su propio perfil.
    # Una sola sentencia para las veinte filas (``queue_ahead_map`` ordena la
    # banda una vez), que es lo que evita el N+1 que esta pagina no puede
    # pagar.
    # ``queue_seq`` ANTES QUE ``id``, que es el orden en el que esta cuenta
    # cobra de verdad (§ live_request.fair_share).  Con la columna a cero —
    # nadie ha adelantado nada — es literalmente el orden de llegada de antes;
    # en cuanto alguien adelanta una suya, listarlas por ``id`` pintaria la
    # adelantada en su sitio viejo con el numero de sitio nuevo al lado.
    pending = list(mine.filter(state=PENDING)
                   .order_by('queue_seq', 'id')[:QUEUE_ROWS + 1])
    places = live_request.queue_ahead_map(pending)
    for task in pending:
        # El mapa devuelve ``(cuantas delante, cuantas tuyas)``: aqui se pinta
        # la primera, que es la columna que esta lista ya tenia.  La segunda
        # la usa el panel de la posicion (§ ``live_request._place_text``).
        place = places.get(task.id)
        task.ahead = None if place is None else place[0]
    leased = list(mine.filter(state=LEASED)
                  .order_by('-leased_at', '-id')[:QUEUE_ROWS])
    done = list(mine.filter(state=COMPLETED)
                .order_by('-completed', '-id')[:QUEUE_ROWS + 1])
    return pending, leased, done


def _overflow(rows, limit, queryset):
    """Corta a ``limit`` y dice cuantas quedan fuera (0 si ninguna)."""
    if len(rows) <= limit:
        return rows, 0
    return rows[:limit], queryset.count() - limit


def _spark(machines, now):
    """Nodos por dia (UTC) de las ultimas dos semanas, ya en barras SVG.

    Se agrupa en SQL: una fila por dia en vez de una por tarea.  Un dia sin
    tareas sale con altura cero y ocupa su sitio — un hueco silencioso mentiria
    sobre la forma de la racha.
    """
    if not machines:
        return {'has_spark': False}
    midnight = datetime(now.year, now.month, now.day, tzinfo=dt_timezone.utc)
    since = midnight - timedelta(days=SPARK_DAYS - 1)
    rows = (AnalysisTask.objects
            .filter(state=COMPLETED, machine__in=machines,
                    completed__gte=since)
            .annotate(day=TruncDate('completed', tzinfo=dt_timezone.utc))
            .values('day').annotate(nodes=Sum('nodes_searched')).order_by())
    by_day = {}
    for row in rows:
        day = row['day']
        if isinstance(day, datetime):
            day = day.date()
        elif isinstance(day, str):
            day = date.fromisoformat(day[:10])
        by_day[day] = (row['nodes'] or 0) + by_day.get(day, 0)
    days = [(since + timedelta(days=index)).date()
            for index in range(SPARK_DAYS)]
    values = [by_day.get(day, 0) for day in days]
    peak = max(values)
    if not peak:
        return {'has_spark': False}
    slot = SPARK_WIDTH // SPARK_DAYS
    bars = []
    for index, (day, value) in enumerate(zip(days, values)):
        # Un dia sin nodos conserva su barra de un pixel: ocupa su sitio, se
        # distingue del dia flojo por el color y sigue teniendo tooltip.  Un
        # hueco vacio se leeria como que ese dia no existe.
        height = max(1, round(SPARK_MAX_BAR * value / peak)) if value else 1
        bars.append({
            'x': index * slot + 2,
            'y': SPARK_BASELINE - height,
            'w': slot - 4,
            'h': height,
            'day': day.strftime('%b %d'),
            'nodes': human(value),
            'quiet': not value,
        })
    return {
        'has_spark': True,
        'spark_bars': bars,
        'spark_width': SPARK_WIDTH,
        'spark_height': SPARK_HEIGHT,
        'spark_baseline': SPARK_BASELINE,
        'spark_days': SPARK_DAYS,
        'spark_peak_h': human(peak),
        'spark_total_h': human(sum(values)),
        'spark_from': days[0].strftime('%b %d'),
        'spark_to': days[-1].strftime('%b %d'),
    }


def human(n):
    """3322 -> '3,322'; 2400000000 -> '2.40B'. La misma escala del resto."""
    from .views import _human
    return _human(n)


def _badges(nodes_total):
    """El club MAS ALTO alcanzado, y solo ese.

    Los cuatro son la misma escalera: quien esta en el de 10T esta en los
    otros tres por definicion, y pintarlos todos convierte un logro en una
    fila de ruido.
    """
    for threshold, label in MILESTONES:
        if nodes_total >= threshold:
            return [label]
    return []


def _labels(keys, opening=True):
    from . import views
    return views._line_readings_many([key for key in keys if key],
                                     opening=opening)


def _presented(task, labels, now, extra=None):
    """Una fila de cola con su linea ya legible y su enlace al explorador.

    La ruta declarada por el peticionario gana al linaje canonico cuando sigue
    siendo valida: el DAG transpone y nadie reconoce su propia peticion pintada
    en otro orden de jugadas (§ ``AnalysisTask.route``).

    El nombre de apertura viaja SIEMPRE con el rotulo que lo acompana — los
    dos salen de la misma lectura — porque el nombre heredado depende del
    orden de jugadas y una fila que los mezclara estaria nombrando una linea
    que no es la que pinta.
    """
    from . import views

    preview, full, opening = labels.get(task.position_id, ('', '', ''))
    play = ''
    if task.route:
        routed = views._route_labels_full(task.route, task.position_id)
        if routed is not None and routed[0]:
            preview, full, opening = routed
            play = task.route
    row = {
        'key': task.position_id,
        'san': preview or 'the start position',
        'full': full or 'the start position',
        'opening': opening,
        'play': play,
        'budget': human(task.budget_nodes),
    }
    row.update(extra or {})
    return row


def speeds(pings, now):
    """``{(cuenta, maquina): (nodos por segundo, busquedas)}`` de esos pings.

    Los nodos de las busquedas que la maquina termino en los ultimos
    ``SPEED_MINUTES`` entre el tiempo que tardaron: la razon que
    ``api_submit`` guarda como ``last_nps`` para UNA busqueda, sobre la
    ventana, asi que cada busqueda pesa lo que duro.  Con el mismo tope que
    esa.  La cuenta que entrego la busqueda la distingue ademas del nombre,
    porque dos cuentas pueden anunciar la misma maquina (§
    ``_owner_of_machine``).  UNA lectura: la banda COMPLETED del indice
    ``(state, completed)`` de una hora, filtrada a esas maquinas.
    """
    from . import views

    machines = sorted({ping.machine for ping in pings})
    if not machines:
        return {}
    rows = (AnalysisTask.objects
            .filter(state=COMPLETED, machine__in=machines,
                    completed__gte=now - timedelta(minutes=SPEED_MINUTES),
                    nodes_searched__gt=0, elapsed_seconds__gt=0)
            .values('delivered_by', 'machine')
            .annotate(searches=Count('id'), nodes=Sum('nodes_searched'),
                      seconds=Sum('elapsed_seconds'))
            .order_by())
    return {(row['delivered_by'], row['machine']):
            (min(row['nodes'] / row['seconds'], views.MAX_REPORTED_NPS),
             row['searches'])
            for row in rows}


def _account_cached(kind, username, build, seconds=ACCOUNT_CACHE_SECONDS):
    """``build()`` for this account, kept ``seconds`` in the shared cache.

    For the per-account aggregates that do not have to be live (the daily
    chart, the first contribution, the request counts): each one is a scan
    that grows with the account, and the page is loaded far more often than
    those numbers move.  The value is wrapped so that ``None`` is cached too.
    """
    digest = hashlib.sha1(username.encode('utf-8')).hexdigest()
    key = f'atomicdb.contributors.{kind}.v1.{digest}'
    hit = cache.get(key)
    if hit is None:
        hit = (build(),)
        cache.set(key, hit, seconds)
    return hit[0]


def _worker_rows(pings, snapshot, now):
    """The rows of the workers table for ``pings``, sorted for display.

    Two reads, both bounded by the machines given: what they have leased now
    and their speed over the last hour.  The page asks for the most recently
    seen ones only (§ ``WORKER_ROWS``); "Show all" asks for the rest.
    """
    machines = sorted({ping.machine for ping in pings})
    running = {}
    if machines:
        for task in AnalysisTask.objects.filter(state=LEASED,
                                                machine__in=machines):
            running.setdefault(task.machine, task)
    # The table names no opening, so the labels skip the community map.
    labels = _labels([task.position_id for task in running.values()],
                     opening=False)
    online_cutoff = now - timedelta(seconds=ONLINE_SECONDS)
    speed = speeds(pings, now)
    workers = []
    for ping in pings:
        day = snapshot['machines_24h'].get(ping.machine,
                                           {'nodes': 0, 'tasks': 0})
        life = snapshot['machines_all'].get(
            ping.machine, {'nodes': 0, 'tasks': 0, 'seconds': 0.0})
        task = running.get(ping.machine)
        nps, searches = speed.get((ping.user, ping.machine), (0, 0))
        row = {
            'machine': ping.machine,
            'threads': ping.threads,
            'hash_mb': ping.hash_mb,
            'os': ping.os,
            'online': ping.last_seen >= online_cutoff,
            'seen': _ago(ping.last_seen, now),
            # El contador propio del ping (``tasks_done``) NO se pinta: cuenta
            # entregas y el sitio entero cuenta tareas COMPLETADAS (la portada
            # incluida), asi que ensenar los dos numeros seria publicar dos
            # definiciones de "analisis" que no tienen por que coincidir.  El
            # que sale es el que comparte denominador con los nodos de al lado.
            'nodes_24h_h': human(day['nodes']),
            'tasks_24h': day['tasks'],
            'nodes_all': life['nodes'],
            'nodes_all_h': human(life['nodes']),
            'tasks_all': life['tasks'],
            'nps_h': human(round(nps)) if nps else '',
            'searches': searches,
            'searches_h': human(searches),
        }
        if task is not None:
            row['searching'] = _presented(task, labels, now)
        workers.append(row)
    # Lo vivo arriba y lo grande primero: una maquina apagada hace meses no
    # puede encabezar la lista solo porque su nombre empiece por 'a'.
    workers.sort(key=lambda row: (not row['online'], -row['nodes_all'],
                                  row['machine']))
    return workers


def all_workers(username, now=None):
    """Every worker of the account: what "Show all" loads."""
    now = now or timezone.now()
    pings = list(WorkerPing.objects.filter(user=username))
    return {'workers': _worker_rows(pings, fleet(now=now), now),
            'has_workers': bool(pings), 'workers_more': 0,
            'profile_user': username}


def present(username, now=None, every_worker=False):
    """Todo el contexto de la pagina de ``username``. Sin escribir nada."""
    from . import views

    now = now or timezone.now()
    snapshot = fleet(now=now)
    # Most recently seen first: the table shows the first ``WORKER_ROWS``.
    pings = list(WorkerPing.objects.filter(user=username)
                 .order_by('-last_seen', 'machine'))
    machines = sorted(ping.machine for ping in pings)

    pending, leased, done = _queue_rows(username)
    mine = _requests_of(username)
    pending, pending_more = _overflow(pending, QUEUE_ROWS,
                                      mine.filter(state=PENDING))
    # Lo que dice el boton de vaciar, SIN una consulta mas: ``_overflow`` ya
    # conto la banda entera cuando desbordo, y cuando no desbordo la lista ES
    # la banda entera.  Un ``count()`` aparte aqui seria un segundo recuento de
    # lo mismo en la pagina que menos se puede permitir consultas de sobra.
    pending_total = len(pending) + pending_more
    if len(done) > QUEUE_ROWS:
        # Only the "and N more served" line needs this count, and it is a
        # count over everything the account was ever served: cached.
        done_total = _account_cached(
            'served', username, mine.filter(state=COMPLETED).count,
            seconds=ACCOUNT_COUNT_SECONDS)
        done, done_more = done[:QUEUE_ROWS], max(0, done_total - QUEUE_ROWS)
    else:
        done_more = 0

    suggestions = list(OpeningNameSuggestion.objects
                       .filter(suggested_by=username)
                       .order_by('-created')[:SUGGESTION_ROWS + 1])
    suggestions, suggestions_more = _overflow(
        suggestions, SUGGESTION_ROWS,
        OpeningNameSuggestion.objects.filter(suggested_by=username))

    # Only a waiting row prints the opening of its line, and naming one reads
    # the community map: without waiting rows there is nothing to name.
    labels = _labels([task.position_id for task in pending + leased + done]
                     + [row.position_id for row in suggestions],
                     opening=bool(pending))

    lease_cutoff = now - timedelta(minutes=views.LEASE_MINUTES)
    leased_rows = []
    for task in leased:
        beat = task.lease_heartbeat_at or task.leased_at
        leased_rows.append(_presented(task, labels, now, {
            'machine': task.machine,
            'since': _ago(task.leased_at, now),
            'quiet': beat is None or beat < lease_cutoff,
        }))
    # ``place`` es el sitio en la cola y es lo UNICO que se pinta de la espera:
    # decir ademas "N requests ahead" era el mismo numero dos veces con dos
    # redacciones (sugerencia de Eclipsia, #suggestions).
    pending_rows = [
        # NO HAY BOTON DE ADELANTAR EN ESTAS FILAS, y es deliberado desde el
        # 15-ago: una lista ordenada por turno ensena arriba lo que ya iba
        # primero, asi que un control por fila ofrecia adelantar justo lo que
        # no hacia falta adelantar, y la peticion que si lo merece esta en el
        # puesto ciento y pico, fuera de esta pagina.  El control vive ahora
        # en la pagina de la POSICION, donde la peticion se mira de verdad
        # (§ ``live_request.bump_control``), y el enlace de la linea es el
        # camino hasta el.
        _presented(task, labels, now,
                   # Sin sitio (fuera de la banda servible, § queue_ahead_map)
                   # se pinta un guion, no un 500 (reporte de Eclipsia, 5-sep).
                   {'place': None if task.ahead is None else task.ahead + 1,
                    'when': _ago(task.created, now),
                    # RETIRAR SI SE QUEDA AQUI, y la asimetria con el boton
                    # que se acaba de ir no es un descuido.  Adelantar en esta
                    # lista no servia porque la lista YA esta ordenada por
                    # turno: lo de arriba es lo que ya iba primero.  Retirar no
                    # tiene ese problema — lo que se quiere quitar puede ser
                    # cualquiera, y en una racha de peticiones que ya no se
                    # quieren son todas — y ademas esta es la unica pagina que
                    # las ensena juntas.  El de la POSICION es el otro camino,
                    # para la que se esta mirando (§ live_request).
                    'task_id': task.id,
                    'backers': task.backers})
        for task in pending]
    done_rows = [
        _presented(task, labels, now,
                   {'when': _ago(task.completed, now),
                    'nodes': human(task.nodes_searched)})
        for task in done]

    # The account totals add up every machine (dictionary lookups in the
    # shared snapshot); the table itself shows the most recently seen ones.
    nodes_24h = tasks_24h = nodes_all = tasks_all = 0
    for ping in pings:
        day = snapshot['machines_24h'].get(ping.machine,
                                           {'nodes': 0, 'tasks': 0})
        life = snapshot['machines_all'].get(
            ping.machine, {'nodes': 0, 'tasks': 0, 'seconds': 0.0})
        nodes_24h += day['nodes']
        tasks_24h += day['tasks']
        nodes_all += life['nodes']
        tasks_all += life['tasks']
    shown = pings if every_worker else pings[:WORKER_ROWS]
    workers = _worker_rows(shown, snapshot, now)
    workers_more = len(pings) - len(shown)

    rank_24h = _rank(snapshot['users_24h'], username)
    rank_all = _rank(snapshot['users_all'], username)
    share = (round(100.0 * nodes_24h / snapshot['nodes_24h'], 1)
             if snapshot['nodes_24h'] else 0.0)

    chips = {OpeningNameSuggestion.SState.APPROVED: 'won',
             OpeningNameSuggestion.SState.REJECTED: 'lost',
             OpeningNameSuggestion.SState.PENDING: 'hot'}
    suggestion_rows = []
    for row in suggestions:
        preview, full, _opening = labels.get(row.position_id, ('', '', ''))
        suggestion_rows.append({
            'name': row.proposed_name,
            'previous': row.previous_name,
            'edit': row.kind == OpeningNameSuggestion.SKind.EDIT,
            'status': row.get_status_display().lower(),
            'css': chips.get(row.status, 'cold'),
            'key': row.position_id,
            'san': preview or 'the start position',
            'full': full or 'the start position',
            'when': _ago(row.created, now),
        })

    requests_total = _account_cached('requests', username, mine.count,
                                     seconds=ACCOUNT_COUNT_SECONDS)
    context = {
        'profile_user': username,
        'requests_total_h': human(requests_total),
        # Sin una sola cifra que ensenar, la fila de medidores es un "0"
        # suelto en una rejilla de seis: la pagina de quien acaba de llegar
        # empieza directamente por lo que puede hacer.
        'has_counters': bool(requests_total or nodes_all),
        'queue_pending': pending_rows, 'queue_pending_more': pending_more,
        'queue_pending_total': pending_total,
        'queue_leased': leased_rows,
        'queue_done': done_rows, 'queue_done_more': done_more,
        'has_queue': bool(pending_rows or leased_rows or done_rows),
        'workers': workers, 'has_workers': bool(workers),
        'workers_more': workers_more, 'workers_total': len(pings),
        'fleet_share': share,
        'fleet_nodes_24h_h': human(snapshot['nodes_24h']),
        'nodes_24h_h': human(nodes_24h), 'tasks_24h_h': human(tasks_24h),
        'nodes_all_h': human(nodes_all), 'tasks_all_h': human(tasks_all),
        'has_nodes': bool(nodes_all),
        'rank_24h': rank_24h, 'rank_all': rank_all,
        'badges': _badges(nodes_all),
        'suggestions': suggestion_rows,
        'suggestions_more': suggestions_more,
        # The first date cannot move once it exists, and the chart is per day:
        # both are scans over the completed band, cached per account.
        'first_at': _account_cached(
            'first', username,
            lambda: _first_contribution(username, machines),
            seconds=FIRST_CACHE_SECONDS),
    }
    context.update(_account_cached('spark', username,
                                   lambda: _spark(machines, now)))
    return context


def present_queue(username, now=None):
    """Only the queue of ``username``: running, waiting and served.

    The profile's three lists without the rest of the profile (workers,
    counters, chart, names), so it costs the queue reads and nothing else.
    """
    now = now or timezone.now()
    pending, leased, done = _queue_rows(username)
    mine = _requests_of(username)
    pending, pending_more = _overflow(pending, QUEUE_ROWS,
                                      mine.filter(state=PENDING))
    if len(done) > QUEUE_ROWS:
        done_total = _account_cached(
            'served', username, mine.filter(state=COMPLETED).count,
            seconds=ACCOUNT_COUNT_SECONDS)
        done, done_more = done[:QUEUE_ROWS], max(0, done_total - QUEUE_ROWS)
    else:
        done_more = 0
    labels = _labels([task.position_id for task in pending + leased + done],
                     opening=bool(pending))
    from . import views
    lease_cutoff = now - timedelta(minutes=views.LEASE_MINUTES)
    leased_rows = []
    for task in leased:
        beat = task.lease_heartbeat_at or task.leased_at
        leased_rows.append(_presented(task, labels, now, {
            'machine': task.machine,
            'since': _ago(task.leased_at, now),
            'quiet': beat is None or beat < lease_cutoff,
        }))
    pending_rows = [
        _presented(task, labels, now,
                   {'place': None if task.ahead is None else task.ahead + 1,
                    'when': _ago(task.created, now),
                    'backers': task.backers})
        for task in pending]
    done_rows = [_presented(task, labels, now,
                            {'when': _ago(task.completed, now),
                             'nodes': human(task.nodes_searched)})
                 for task in done]
    return {
        'profile_user': username,
        'queue_pending': pending_rows, 'queue_pending_more': pending_more,
        'queue_leased': leased_rows, 'queue_done': done_rows,
        'queue_done_more': done_more,
        # The totals the lists already counted: no query of their own.
        'waiting_total_h': human(len(pending_rows) + pending_more),
        'running_total_h': human(len(leased_rows)),
        'served_total_h': human(len(done_rows) + done_more),
    }


def present_anonymous(now=None):
    """The queue of the requests made without an account, like a profile's.

    Every query is bounded by an index the hot pages already use, never by
    the whole history of anonymous requests: ``requested_by=''`` is also every
    AUTO task, so "served" reads only the last day of the completed band.
    """
    now = now or timezone.now()
    mine = _requests_of('')
    pending = list(mine.filter(state=PENDING)
                   .order_by('queue_seq', 'id')[:QUEUE_ROWS + 1])
    places = live_request.queue_ahead_map(pending)
    pending, pending_more = _overflow(pending, QUEUE_ROWS,
                                      mine.filter(state=PENDING))
    leased = list(mine.filter(state=LEASED)
                  .order_by('-leased_at', '-id')[:QUEUE_ROWS])
    done = list(AnalysisTask.objects
                .filter(state=COMPLETED,
                        completed__gte=now - timedelta(hours=24),
                        source=USER, requested_by='')
                .order_by('-completed', '-id')[:QUEUE_ROWS])
    labels = _labels([task.position_id for task in pending + leased + done],
                     opening=bool(pending))
    from . import views
    lease_cutoff = now - timedelta(minutes=views.LEASE_MINUTES)
    leased_rows = []
    for task in leased:
        beat = task.lease_heartbeat_at or task.leased_at
        leased_rows.append(_presented(task, labels, now, {
            'machine': task.machine,
            'since': _ago(task.leased_at, now),
            'quiet': beat is None or beat < lease_cutoff,
        }))
    pending_rows = []
    for task in pending:
        place = places.get(task.id)
        pending_rows.append(_presented(task, labels, now, {
            'place': None if place is None else place[0] + 1,
            'when': _ago(task.created, now),
            'backers': task.backers}))
    done_rows = [_presented(task, labels, now,
                            {'when': _ago(task.completed, now),
                             'nodes': human(task.nodes_searched)})
                 for task in done]
    return {
        'queue_pending': pending_rows, 'queue_pending_more': pending_more,
        'queue_leased': leased_rows, 'queue_done': done_rows,
    }


def _first_contribution(username, machines):
    """La fecha mas antigua que esta cuenta puede reclamar, o ``None``.

    Tres candidatos y el minimo de los que existan: el primer analisis servido
    por una maquina suya, la primera peticion que hizo y el primer nombre que
    propuso.  Quien solo pide analisis tambien tiene una primera vez, y
    empezar la biografia de alguien por su primer worker seria borrarla.
    """
    stamps = []
    if machines:
        first = (AnalysisTask.objects
                 .filter(state=COMPLETED, machine__in=machines,
                         completed__isnull=False)
                 .order_by('completed').values_list('completed', flat=True)
                 .first())
        if first is not None:
            stamps.append(first)
    first_request = (AnalysisTask.objects
                     .filter(source=USER, requested_by=username)
                     .order_by('created').values_list('created', flat=True)
                     .first())
    if first_request is not None:
        stamps.append(first_request)
    first_name = (OpeningNameSuggestion.objects
                  .filter(suggested_by=username)
                  .order_by('created').values_list('created', flat=True)
                  .first())
    if first_name is not None:
        stamps.append(first_name)
    return min(stamps) if stamps else None
