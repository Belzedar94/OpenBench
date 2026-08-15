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


def _machine_totals(since=None):
    """``{maquina: {nodes, tasks, seconds}}`` de las tareas COMPLETADAS.

    Un GROUP BY sobre el indice ``(state, completed)``.  El numero de maquinas
    distintas es de decenas, asi que lo que vuelve cabe en memoria por
    construccion — a diferencia de las tareas, que no.
    """
    rows = AnalysisTask.objects.filter(state=COMPLETED)
    if since is not None:
        rows = rows.filter(completed__gte=since)
    rows = rows.values('machine').annotate(
        nodes=Sum('nodes_searched'), tasks=Count('id'),
        seconds=Sum('elapsed_seconds')).order_by()
    return {row['machine']: {'nodes': row['nodes'] or 0,
                             'tasks': row['tasks'] or 0,
                             'seconds': row['seconds'] or 0.0}
            for row in rows}


def _by_owner(machine_totals, owner):
    """Los mismos totales plegados por CUENTA.

    Una maquina sin ``WorkerPing`` no tiene dueno conocido y no entra en
    ninguna cuenta: sus nodos siguen contando en el denominador de la flota,
    porque se buscaron de verdad, pero atribuirselos a alguien seria inventar.
    """
    folded = {}
    for machine, totals in machine_totals.items():
        user = owner.get(machine)
        if not user:
            continue
        row = folded.setdefault(user, {'nodes': 0, 'tasks': 0})
        row['nodes'] += totals['nodes']
        row['tasks'] += totals['tasks']
    return folded


def measure_fleet(now):
    """Totales por maquina y por cuenta, 24h y all-time.  DOS GROUP BY caros.

    El de "all time" agrupa TODAS las tareas completadas del proyecto y suma
    tres columnas que no estan en el indice: a 12,8M de posiciones es el
    trabajo mas caro que se hacia dentro de una peticion HTTP en todo el
    sitio.  No entra en la portada porque la portada lo necesite vivo — no lo
    necesita — sino porque era lo unico que quedaba sin publicar desde fuera.
    """
    owner = dict(WorkerPing.objects.values_list('machine', 'user'))
    machines_all = _machine_totals()
    machines_24h = _machine_totals(since=now - timedelta(hours=24))
    return {
        'owner': owner,
        'machines_all': machines_all,
        'machines_24h': machines_24h,
        'users_all': _by_owner(machines_all, owner),
        'users_24h': _by_owner(machines_24h, owner),
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


def leaderboard(now=None):
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
         **_standings(snapshot['users_24h'], snapshot['nodes_24h'])},
        {'window': 'All time',
         'empty': 'No worker has searched anything yet.',
         **_standings(snapshot['users_all'], snapshot['nodes_all'])},
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
        task.ahead = places.get(task.id)
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


def _labels(keys):
    from . import views
    return views._line_readings_many([key for key in keys if key])


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


def present(username, now=None):
    """Todo el contexto de la pagina de ``username``. Sin escribir nada."""
    from . import views

    now = now or timezone.now()
    snapshot = fleet(now=now)
    machines = sorted(WorkerPing.objects.filter(user=username)
                      .values_list('machine', flat=True))

    pending, leased, done = _queue_rows(username)
    mine = _requests_of(username)
    pending, pending_more = _overflow(pending, QUEUE_ROWS,
                                      mine.filter(state=PENDING))
    # Lo que dice el boton de vaciar, SIN una consulta mas: ``_overflow`` ya
    # conto la banda entera cuando desbordo, y cuando no desbordo la lista ES
    # la banda entera.  Un ``count()`` aparte aqui seria un segundo recuento de
    # lo mismo en la pagina que menos se puede permitir consultas de sobra.
    pending_total = len(pending) + pending_more
    done, done_more = _overflow(done, QUEUE_ROWS, mine.filter(state=COMPLETED))

    suggestions = list(OpeningNameSuggestion.objects
                       .filter(suggested_by=username)
                       .order_by('-created')[:SUGGESTION_ROWS + 1])
    suggestions, suggestions_more = _overflow(
        suggestions, SUGGESTION_ROWS,
        OpeningNameSuggestion.objects.filter(suggested_by=username))

    # Lo que las maquinas de esta cuenta tienen arrendado AHORA: es otra
    # pregunta que la cola de arriba (aquello es lo que PIDIO, esto lo que
    # CORRE), y las dos caben en una consulta pequena cada una.
    running = {}
    if machines:
        for task in AnalysisTask.objects.filter(state=LEASED,
                                                machine__in=machines):
            running.setdefault(task.machine, task)

    labels = _labels([task.position_id for task in pending + leased + done]
                     + [task.position_id for task in running.values()]
                     + [row.position_id for row in suggestions])

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
                   {'place': task.ahead + 1,
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

    online_cutoff = now - timedelta(seconds=ONLINE_SECONDS)
    workers = []
    nodes_24h = tasks_24h = nodes_all = tasks_all = 0
    for ping in WorkerPing.objects.filter(user=username).order_by('machine'):
        day = snapshot['machines_24h'].get(ping.machine,
                                           {'nodes': 0, 'tasks': 0})
        life = snapshot['machines_all'].get(
            ping.machine, {'nodes': 0, 'tasks': 0, 'seconds': 0.0})
        nodes_24h += day['nodes']
        tasks_24h += day['tasks']
        nodes_all += life['nodes']
        tasks_all += life['tasks']
        seconds = life.get('seconds') or 0.0
        task = running.get(ping.machine)
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
            'nps_h': human(int(life['nodes'] / seconds)) if seconds else '',
        }
        if task is not None:
            row['searching'] = _presented(task, labels, now)
        workers.append(row)
    # Lo vivo arriba y lo grande primero: una maquina apagada hace meses no
    # puede encabezar la lista solo porque su nombre empiece por 'a'.
    workers.sort(key=lambda row: (not row['online'], -row['nodes_all'],
                                  row['machine']))

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

    requests_total = mine.count()
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
        'fleet_share': share,
        'fleet_nodes_24h_h': human(snapshot['nodes_24h']),
        'nodes_24h_h': human(nodes_24h), 'tasks_24h_h': human(tasks_24h),
        'nodes_all_h': human(nodes_all), 'tasks_all_h': human(tasks_all),
        'has_nodes': bool(nodes_all),
        'rank_24h': rank_24h, 'rank_all': rank_all,
        'badges': _badges(nodes_all),
        'suggestions': suggestion_rows,
        'suggestions_more': suggestions_more,
        'first_at': _first_contribution(username, machines),
    }
    context.update(_spark(machines, now))
    return context


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
