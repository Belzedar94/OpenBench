"""En que estado va la peticion de analisis VIVA de una posicion.

QUE ES.  El explorador pinta, debajo del boton, una linea sobre la tarea que
esta posicion tiene en vuelo: esperando en la cola (con cuanta cola por
delante) o buscandose ahora mismo (con que maquina la tiene y cuanto falta).
Sin tarea viva no se pinta nada: un hueco vacio con cara de panel es peor que
el silencio, porque promete que ahi va a aparecer algo.

POR QUE EXISTE.  Medido el 31-jul con el selector de profundidad recien
estrenado: alguien pidio 10B sobre una posicion, la tarea se creo a las
05:50:13, un worker la cogio un segundo despues y a los 9,48M nps de esa
maquina la busqueda tardaba 17-18 minutos.  Todo funcionaba.  La pagina, en
esos dieciocho minutos, no decia absolutamente nada, y la unica forma de saber
que la peticion vivia era abrir la base de datos.  Un selector que existe para
comprar busquedas caras necesita, del otro lado, una pagina que sepa decir
"esto va".

LA REGLA, Y ES LA UNICA QUE IMPORTA AQUI: la estimacion es honesta o NO ESTA.
Un numero inventado en esta linea es peor que la pagina muda de antes, porque
la muda no mentia.  De ahi las tres condiciones que hay que cumplir para
prometer un tiempo, y ninguna se puede saltar:

* que la maquina haya reportado velocidad de verdad (``last_nps`` puede ser 0
  — visto en produccion en varias filas — mientras el motor arranca o cuando
  el slot no tiene tarea);
* que esa lectura de velocidad sea RECIENTE (``nps_updated``);
* y que la tarea de aqui de progreso reciente (``lease_heartbeat_at``): un
  worker caido deja el arriendo puesto hasta que el servidor lo recicla, y
  restarle minutos a una cuenta atras de un motor que ya no existe es
  exactamente la mentira que este modulo no puede cometer.

Cuando alguna falla se dice lo que SI se sabe — que la tiene tal maquina, o
cuanto lleva sin reportar — y no se dice ningun tiempo.  Deliberadamente NO se
cae a la mediana de nps de la flota: la velocidad de otra maquina no es una
medida de esta, y una cuenta atras sacada de un ordenador ajeno tiene la misma
pinta de verdad que una de verdad.  Es justo lo que no puede pasar.

QUE TAREAS CUENTAN.  Las que ``ingest.notification_deserved`` considera de una
PERSONA — banda USER, o presupuesto de grado peticion — que es la misma
definicion con la que el sitio decide a quien avisar cuando el resultado
aterriza.  Asi el conjunto de tareas que te encienden la campana y el conjunto
de tareas que esta linea narra son el MISMO, y no dos criterios parecidos que
un dia discrepan.  Una sonda de cobertura de 8M que dura un segundo no sale:
nadie la pidio y nadie la esta esperando.

AND WHAT YOU CAN DO ABOUT IT, which lives here for the same reason: the
move-to-front control (§ ``bump_control``) talks about one request on one
position, reads the same live row this line reads, and reports its place in
the queue with the very rule that composes the text above.  Two modules
answering "how much queue is in front of this" is how one page ends up
contradicting the other.
"""

from datetime import timedelta

from django.db.models import (BigIntegerField, Case, CharField, F,
                              IntegerField, Q, Sum, Value, When, Window)
from django.utils import timezone

from .ingest import notification_deserved
from .models import AnalysisTask, WorkerPing

PENDING = AnalysisTask.TState.PENDING
LEASED = AnalysisTask.TState.LEASED
USER = AnalysisTask.Source.USER

# Cuanto espera una peticion antes de que la cola deje de tener estratos.
# El estrato NOMBRADO ordena el trafico del dia — un humano identificado no
# espera detras de la marea sin login (Lesha, 31-jul) — pero sin caducidad no
# ordena nada: mata de hambre.  Medido el 30-jul en produccion: 318 peticiones
# USER PENDING, 269 de mas de 24h y 126 de mas de 72h, con la cola sirviendo
# sin parar (6 arriendos, todos jovenes).  Mientras entre UNA nombrada al dia,
# la banda anonima no llega jamas al frente.  Un dia es el numero porque es la
# escala a la que el sitio ya habla de esta cola: la portada cuenta cierres en
# 24h y el perfil dice "asked N hours ago".
STARVED_AFTER = timedelta(hours=24)
# Una PENDING sobre una posicion CERRADA no la sirve nadie: ``choose_pending``
# salta lo que no esta en 'UNKNOWN'.  No es cola por delante de nadie, y desde
# el cierre tampoco se queda ahi (§ ingest._emit_closure_events).  Una LAPIDA
# si es serveable — ``choose_pending`` no mira la prioridad — asi que cuenta.
SERVEABLE = Q(position__status='UNKNOWN')
# Lo que a un peticionario le CUENTA como cola propia: lo que espera Y lo que
# ya esta corriendo.  Sin la parte arrendada el reparto justo no reparte nada:
# en cuanto la primera de una racha sale a un worker deja de sumar, la segunda
# vuelve a valer cero nodos y la racha se lleva los nueve slots otra vez.  Lo
# COMPLETADO no cuenta a proposito: si contase, la deuda no dejaria de crecer y
# quien mas ha usado el proyecto acabaria detras de todo el mundo para siempre.
CHARGED = Q(state__in=(PENDING, LEASED))
# Cuanto silencio invalida una PROMESA de tiempo.  Tres intervalos de heartbeat
# del worker (60s, § Client/atomicdb_worker HEARTBEAT_INTERVAL_SECONDS) y
# exactamente la ventana con la que los medidores de la portada deciden si una
# lectura de nps sigue viva (§ metrics.LIVE_SECONDS).  Se reusa el mismo numero
# a proposito: la cuenta atras se construye con ese nps, asi que solo debe
# existir cuando el sitio ya considera esa velocidad una medida actual.
PROMISE_FRESH_SECONDS = 180
# Cuanto silencio convierte el arriendo en un arriendo MUERTO no se escribe
# aqui: son ``views.LEASE_MINUTES``, los mismos con los que el servidor lo
# recicla en el siguiente lease, y se leen de alli (importado dentro de la
# funcion, como hace ``contributors.present``).  Un segundo numero igual al
# suyo seria una promesa que dejaria de ser cierta el dia que cambie el de
# verdad.
# Techo de lo que se promete.  El peldano mas alto de la escalera son 10B, que
# en la maquina mas lenta que ha pasado por la flota son minutos, no horas: una
# estimacion de medio dia no es una estimacion larga, es una lectura de
# velocidad rota (un motor recien arrancado, un nps de tres cifras).  Por
# encima de esto se dice que se esta buscando y no se dice cuanto.
MAX_PROMISE_SECONDS = 6 * 3600
# Tope de filas vivas que se leen de una posicion.  Son como mucho dos (el
# arriendo en curso y su relevo pendiente), pero la lectura va acotada por
# principio: esto corre en cada render del explorador.
MAX_LIVE_ROWS = 8


def named_tier(now=None):
    """Q de las que cobran en el estrato NOMBRADO de la banda USER.

    Un nombre O un dia de espera, por el mismo sitio.  El orden de servicio
    (``views.choose_pending``) y las dos cifras de "cuanta cola tengo delante"
    (``queue_ahead`` y ``contributors._queue_rows``) leen esta misma regla:
    tres implementaciones de un mismo estrato acabarian discrepando el dia que
    cambie, y la que discrepa le miente a un humano que esta esperando.
    """
    return (Q(requested_by__gt='')
            | Q(created__lt=(now or timezone.now()) - STARVED_AFTER))


def _effective_account():
    """La cuenta con la que una fila REPARTE: su autor, o quien le presto carril.

    Vacio en ``lane_account`` significa "el autor", que es lo que dice toda
    fila que no ha respaldado nadie, asi que esta expresion vale
    ``requested_by`` en la inmensa mayoria de la cola y la particion es la de
    siempre (§ ``models.AnalysisTask.lane_account``).
    """
    return Case(When(lane_account='', then=F('requested_by')),
                default=F('lane_account'), output_field=CharField())


def fair_share(queryset):
    """Anota ``lane_ahead``: los NODOS que ese mismo peticionario ya tiene
    encolados por delante de cada fila.

    SIN CARRILES DESDE EL 16-AGO.  El multiplicador por tamano de carril que
    vivio aqui del 10 al 16 de agosto se retiro con los carriles: el umbral
    binario que los abria era trampeable con un worker de un minuto, y la
    comunidad prefirio volver al reparto por cuenta a secas (la unica ventaja
    del contribuidor es la afinidad de su propio worker, § views.choose_pending).
    El nombre de la columna se queda: cada consumidor del orden lo lee ya y un
    renombre tocaria cada uno de ellos para no cambiar nada.

    QUE ES.  Deficit round-robin ponderado por coste, y es el ultimo escalon
    del orden de servicio: el primero que llega de cada cuenta lleva cero
    nodos delante, asi que la PRIMERA peticion de cualquiera cobra antes que
    la SEGUNDA de cualquier otro, y como lo que se acumula son nodos y no
    filas, una de 10B cede el turno sola frente a setenta y ocho de 128M — que
    es exactamente lo que cuestan.  Nadie pierde acceso y nadie tiene cupo:
    las mil quinientas de una racha se siguen sirviendo, intercaladas.

    POR QUE EXISTE.  El 6-ago a las 18:00 UTC una cuenta encolo 1.562
    peticiones en una hora sobre un pool de nueve procesos.  El ultimo escalon
    era FIFO por ``id``, asi que el siguiente en llegar — pidiese lo que
    pidiese — esperaba detras de las 1.562.  "The queue is completely
    paralized", y era verdad.

    LA SUMA EXCLUYE LA PROPIA FILA.  Incluirla ordenaria por hora de FIN
    virtual y una primera peticion de 10B saldria detras de las primeras de
    todo el mundo por el mero hecho de ser cara; excluyendola se ordena por
    hora de INICIO virtual, todos los primeros empatan a cero y el ``id``
    los desempata.  Un click es un click, tambien el caro.  Django 4.2 no
    admite ``ROWS ... 1 PRECEDING`` (``RowRange`` rechaza un limite
    negativo), asi que se toma el marco inclusivo y se resta lo propio.

    LA PARTICION LLEVA ``source``: sin el, la marea anonima de la banda USER
    (``requested_by`` vacio) compartiria cubo con AUTO y FILL, que tambien lo
    tienen vacio, y las sumas de una banda envenenarian el orden de la otra.
    Fuera de la banda USER la clave vale cero a proposito — dentro de AUTO y
    FILL manda la prioridad de POSICION, que es la que calcula el selector, y
    este escalon no debe tener ni voz ahi.

    LA VENTANA ORDENA POR ``queue_seq`` Y LUEGO POR ``id``, y con la columna a
    cero en todas las filas eso ES el orden de llegada de siempre: es lo que
    hace que adelantar una peticion propia (§ ``views.api_queue_bump``) no
    necesite un segundo camino de orden.  Adelantar RECOLOCA la suma dentro de
    la cuenta que adelanta y solo dentro de ella: la particion no cambia, asi
    que ninguna fila ajena ve moverse su ``fair_ahead``.

    LO ARRENDADO VA DELANTE DE LO PENDIENTE, y eso es lo que impide que
    adelantar sea un descuento.  Sin esta clave, una peticion adelantada se
    colocaba tambien por delante del arriendo VIVO de su propia cuenta y
    heredaba un ``fair_ahead`` de cero: la deuda de nodos que esa cuenta ya
    tiene contraida — diez mil millones buscandose ahora mismo — dejaba de
    contar, y bastaba con adelantar antes de cada arriendo para vivir
    permanentemente en el frente.  Fuera de ese caso no cambia nada: la fila
    que un worker se lleva es la primera de su cuenta, asi que lo arrendado ya
    era el prefijo de la particion.

    EL QUERYSET QUE ENTRA TIENE QUE LLEVAR ``CHARGED``, no solo lo pendiente:
    una ventana solo ve las filas que sobreviven al WHERE, asi que filtrar
    aqui lo arrendado seria descontarlo de la suma.  Las filas arrendadas
    salen ordenadas junto a las demas y quien llama las descarta despues —
    ``choose_pending`` porque su lote solo bloquea PENDING, ``queue_ahead``
    porque solo cuenta PENDING.
    """
    running_first = Case(When(state=LEASED, then=Value(0)), default=Value(1),
                         output_field=IntegerField())
    return (queryset
            .annotate(_fair_cumulative=Window(
                expression=Sum('budget_nodes'),
                partition_by=[F('source'), _effective_account()],
                order_by=(running_first.asc(), F('queue_seq').asc(),
                          F('id').asc())))
            .annotate(lane_ahead=Case(
                When(source=USER,
                     then=F('_fair_cumulative') - F('budget_nodes')),
                default=Value(0), output_field=BigIntegerField()))
            # El recuento de personas se lee ACOTADO A LA BANDA USER, por lo
            # mismo que el reparto: una tarea que nacio de un click puede
            # acabar DEGRADADA a AUTO (§ ``ingest``, la expansion de
            # cobertura) conservando lo que ya tenia encima.  Dentro de AUTO
            # todas las filas empatan a cero en el reparto, asi que un
            # recuento vivo ahi decidiria por delante de la prioridad de
            # POSICION — que es lo que el selector calcula precisamente para
            # mandar en esa banda.
            .annotate(backer_rank=Case(
                When(source=USER, then=F('backers')),
                default=Value(0), output_field=IntegerField())))


# Los escalones que el ORDEN DE SERVICIO y el ESTIMADOR comparten, LITERALES y
# en un solo sitio.  Cuando eran dos consultas parecidas escritas aparte, el
# dia que cambio una la otra empezo a mentirle a un humano que estaba
# esperando.  Lo que cada lado pone alrededor es lo unico que difiere:
# ``views.choose_pending`` antepone ``-source`` y su own-first (que depende de
# que worker pregunte, y por eso el estimador no puede tenerlo) y cierra con la
# prioridad de POSICION; ``queue_ahead`` cierra con el ``id`` y ya.
#
# ``-backer_rank`` ES EL ESCALON MAS BAJO, y ese sitio es la respuesta entera a
# "multiple people asking to analyse the same position should give it more
# precedence" (Eclipsia).  Va DESPUES del reparto justo, no antes: delante, una
# posicion popular adelantaria a toda la cola y bastaria con dos personas
# pidiendo lo mismo para dejar sin turno a quien pide solo — la inanicion que
# el estrato nombrado ya nos enseno a no repetir.  Detras, solo desempata entre
# filas que YA cobran a la vez, y ahi si decide: la peticion que espera media
# docena de personas sale antes que la que espera una.  Que las dos se
# encuentren empatadas no es casualidad — sumarse a una peticion ajena la manda
# al frente de la cola de SU autor (§ ``ingest.add_requester``), que es justo
# donde estan los ceros de todo el mundo.
SERVICE_KEYS = ('-named_first', 'lane_ahead', '-backer_rank')


def named_first_rank(now=None, band_only=False):
    """1 en el estrato nombrado, 0 en la marea anonima fresca.

    ``band_only`` acota la regla a la banda USER, y quien ordene la cola
    ENTERA lo necesita: ``named_tier`` tambien mira la edad, asi que sin el
    una tarea del selector de hace mas de un dia cobraria el 1 y se colaria
    por delante de la prioridad de posicion de sus companeras.  El estimador
    no lo usa porque ya trabaja sobre la banda sola.
    """
    condition = named_tier(now)
    if band_only:
        condition = Q(source=USER) & condition
    return Case(When(condition, then=Value(1)), default=Value(0),
                output_field=IntegerField())


def service_order(queryset, now=None):
    """La banda USER en ORDEN DE SERVICIO.  Devuelve el queryset ordenado.

    Solo vale para la banda USER: fuera de ella el orden lleva ademas
    ``-source`` y la prioridad de posicion, que aqui no pintan nada.
    """
    return (fair_share(queryset).annotate(named_first=named_first_rank(now))
            .order_by(*SERVICE_KEYS, 'id'))


def queue_ahead(task):
    """Cuantas peticiones de visitante cobran antes que ``task``, o ``None``.

    El mismo orden que ``choose_pending``, leido del mismo sitio
    (``service_order``), sin el matiz own-first porque depende de que worker
    pregunte.  Es transparencia, no contrato.

    Solo cuenta lo que de verdad puede cobrar antes: una PENDING sobre una
    posicion ya cerrada no la sirve nadie, y sumarla era decirle a quien acaba
    de pedir que tiene por delante una cola que no existe.

    UNA sola sentencia, y solo viajan los ``id``: el reparto justo es una suma
    con ventana, que ordena la banda entera de una pasada pero no se puede
    contar con un ``filter`` encima.  Buscar el sitio propio en la lista es
    trabajo de Python sobre una lista de enteros, que al lado de la
    ordenacion no se nota.

    El tope de arriendos profundos (``views.DEEP_TASK_NODES``) NO entra aqui:
    es una condicion del momento del reparto, no un sitio en la cola, y quien
    lo tiene puesto ya esta contado delante.  La cifra puede quedarse larga
    por eso, nunca corta, que es el lado por el que esta linea puede errar.
    """
    if task is None:
        return None
    return queue_ahead_map([task]).get(task.id)


def queue_ahead_map(tasks):
    """``{id: cuantas peticiones cobran antes}`` para varias, UNA consulta.

    El perfil de una cuenta pinta hasta veinte filas de su cola.  Preguntar el
    sitio de cada una por separado seria ordenar la banda entera veinte veces,
    que es justo el N+1 que esa pagina no puede pagar: se ordena una vez y de
    esa pasada salen todos los sitios.

    Una tarea que no este en la banda servible no sale en el mapa — no tiene
    sitio en la cola porque no la va a servir nadie — y quien pregunte recibe
    ``None`` en vez de un numero inventado.
    """
    wanted = {task.id for task in tasks
              if task.state == PENDING and task.source == USER}
    if not wanted:
        return {}
    band = (AnalysisTask.objects.filter(source=USER)
            .filter(CHARGED).filter(SERVEABLE))
    places, ahead = {}, 0
    for task_id, state in service_order(band).values_list('id', 'state'):
        if task_id in wanted:
            places[task_id] = ahead
        # Lo que ya esta corriendo pesa en el reparto pero no es COLA: quien
        # espera quiere saber cuantas PETICIONES le faltan por delante, y la
        # que tiene un worker encima ya no esta esperando a nadie.
        ahead += state == PENDING
    return places


def _live_task(pos):
    """La tarea viva de la que hay algo que contar, o ``None``. UNA consulta.

    Con arriendo y relevo a la vez gana el ARRIENDO: "se esta buscando ahora"
    es mas cierto y mas util que "hay otra detras", y la de detras se contara
    sola cuando le toque.
    """
    rows = [task for task in AnalysisTask.objects
            .filter(position=pos, state__in=(LEASED, PENDING))
            .order_by('id')[:MAX_LIVE_ROWS]
            if notification_deserved(task.source, task.budget_nodes)]
    for task in rows:
        if task.state == LEASED:
            return task
    return rows[0] if rows else None


def _machine_nps(task, now):
    """La velocidad VIVA del slot que tiene la tarea, o ``None``.

    ``machine`` es el slot (``base#3``), que es exactamente la fila de
    ``WorkerPing`` que reporta esa busqueda; dos cuentas podrian llamar igual a
    su maquina, asi que se coge la que se vio mas tarde.  Cero no es una
    velocidad: es "todavia no ha reportado ninguna".
    """
    if not task.machine:
        return None
    ping = (WorkerPing.objects.filter(machine=task.machine)
            .only('last_nps', 'nps_updated', 'last_seen')
            .order_by('-last_seen').first())
    if ping is None or not ping.last_nps or ping.nps_updated is None:
        return None
    if ping.nps_updated < now - timedelta(seconds=PROMISE_FRESH_SECONDS):
        return None
    return ping.last_nps


def _seconds_left(task, nps, now):
    """Segundos que le faltan a la busqueda, o ``None`` si no se puede prometer.

    ``nodes_searched`` solo se escribe al entregar, asi que lo buscado hasta
    ahora se estima por el reloj: el motor arranco con el arriendo y va a esa
    velocidad.  Es la misma cuenta que hace el que mira el log del worker, y
    tiene la propiedad que hace falta aqui — solo puede quedarse corta si la
    maquina se frena, y una maquina frenada deja de reportar nps fresco.
    """
    if nps is None or task.leased_at is None:
        return None
    elapsed = max(0.0, (now - task.leased_at).total_seconds())
    remaining = max(0, task.budget_nodes - int(elapsed * nps))
    seconds = remaining / nps
    if seconds > MAX_PROMISE_SECONDS:
        return None
    return seconds


def _left_text(seconds):
    """'about 18 min left'.  Nunca '17.63 min': es una estimacion, no un reloj.

    Por debajo del minuto no se dice un numero en absoluto — un "1 min" que
    dura cuarenta segundos y otro que dura ochenta son el mismo texto — sino
    que ya se acaba, que es lo unico que ahi es verdad.
    """
    if seconds < 60:
        return 'finishing any moment now'
    minutes = round(seconds / 60)
    if minutes < 120:
        return f'about {minutes} min left'
    return f'about {seconds / 3600:.1f} h left'


def _backers_text(task):
    """' · 3 people asked for this', o cadena vacia si solo la pidio uno.

    ``backers`` cuenta a los que se SUMARON, asi que el total lleva ademas al
    autor.  Se dice solo cuando hay algo que decir: "1 person asked for this"
    debajo de cada peticion seria ruido en todas las filas del sitio.
    """
    people = (task.backers or 0) + 1
    return f' · {people} people asked for this' if people > 1 else ''


def _quiet_text(silence, dead):
    """Lo que se dice de un arriendo que no reporta: el hecho, y nada mas.

    ``silence`` a ``None`` es la fila sin una sola marca de tiempo — no se sabe
    cuanto lleva callada, asi que no se dice un numero.
    """
    said = ('nothing reported at all' if silence is None else
            f'nothing reported for {max(1, round(silence / 60))} min')
    # Pasada la ventana de reciclado el servidor va a devolverla a la cola en
    # el siguiente lease, asi que decirlo no es una promesa: es el
    # comportamiento del propio servidor (§ views.api_lease).
    return f'{said}, so it goes back in the queue' if dead else said


def summary(pos, now=None):
    """La linea del explorador para esta posicion, o ``None`` si no hay nada.

    ``{'text': ..., 'chip': ...}``, con el texto YA compuesto: la pagina lo
    pinta al cargar y el sondeo lo vuelve a pedir tal cual, asi que la
    redaccion vive en un solo sitio y las dos no pueden discrepar.

    Coste: una consulta para la tarea y, como mucho, una segunda — la cola por
    delante si espera, la velocidad de su maquina si esta corriendo.
    """
    if pos.status != 'UNKNOWN':
        # Ya hay veredicto: la pagina entera cuenta otra cosa y el boton de
        # peticion ni existe.  Ademas asi el explorador de una posicion cerrada
        # no paga ni una consulta por esto.
        return None
    return _narrate(pos, _live_task(pos), now=now)


def _narrate(pos, task, now=None):
    """The line for ONE already-read task, or ``None`` when there is none.

    Split out of ``summary`` so the page can read the live task ONCE and use
    it for both things that hang off it: narrating it, and deciding whether
    the move-to-front control is pointing at that very row
    (see ``bump_control``).
    """
    if task is None:
        return None
    now = now or timezone.now()
    from .views import LEASE_MINUTES, _human

    budget = f'{_human(task.budget_nodes)} nodes'
    if task.state == PENDING:
        # Quien pincha en una posicion que ya tiene peticion no recibe tarea
        # nueva: se suma a la que hay.  Sin decirlo, ese click se veia igual
        # que un click perdido — la pagina seguia contando la peticion de otro
        # y nada acusaba recibo de la suya.
        crowd = _backers_text(task)
        ahead = queue_ahead(task)
        if ahead is None:
            # Una tarea de grado peticion que NO va por la banda de visitante
            # (§ ingest.enqueue_unexplored_children puede degradar a AUTO una
            # que nacio de un click): cobra despues de todas las de visitante,
            # asi que su sitio en la cola no es el que cuenta esa cifra y no se
            # inventa uno.
            place = 'visitor requests are served first'
        elif ahead:
            place = f'{ahead} request{"" if ahead == 1 else "s"} ahead'
        else:
            place = 'next up'
        return {'text': f'Waiting for a worker · {budget} · {place}{crowd}',
                'chip': 'cold'}

    machine = task.machine or 'a worker'
    beat = task.lease_heartbeat_at or task.leased_at
    silence = None if beat is None else (now - beat).total_seconds()
    if silence is None or silence > PROMISE_FRESH_SECONDS:
        dead = silence is None or silence > LEASE_MINUTES * 60
        return {'text': f'Sent to {machine} · {budget} · '
                        f'{_quiet_text(silence, dead)}',
                'chip': 'cold'}
    # Las dos formas de no tener cuenta atras se dicen distinto porque son
    # distintas: una es "esa maquina todavia no ha dicho a que velocidad va" —
    # que se arregla sola en el siguiente heartbeat — y la otra es "lo que dijo
    # no sostiene ninguna estimacion".  Ninguna de las dos ensena un numero.
    nps = _machine_nps(task, now)
    left = None if nps is None else _seconds_left(task, nps, now)
    if left is not None:
        tail = _left_text(left)
    else:
        tail = ('no speed reported yet' if nps is None
                else 'no time estimate yet')
    return {'text': f'Searching now on {machine} · {budget} · {tail}',
            'chip': 'hot'}


def _own_pending(pos, username, live):
    """The PENDING request of ``username`` on THIS position, or ``None``.

    Free in the common case: when the row the page is already narrating is
    the visitor's own and it is waiting, that row IS the answer and no second
    statement is needed.  And with no live row at all there is nothing to look
    for: every task in the USER band deserves a notification
    (see ``ingest.notification_deserved``), so if ``_live_task`` found none
    then none exists.

    THE TWO CONTROLS OF THIS PAGE HANG OFF THIS ONE LOOKUP: moving your own
    request to the front and taking it back are the same row, the same
    permission and the same return address, so asking twice who owns what here
    would be two answers waiting to disagree.
    """
    if not username or live is None:
        return None
    if live.state == PENDING and live.requested_by == username:
        return live
    return (AnalysisTask.objects
            .filter(position=pos, state=PENDING, source=USER,
                    requested_by=username)
            .order_by('id').first())


def bump_control(pos, username, live=None, back=''):
    """The move-to-front control for THIS position, or ``{}``.

    WHY IT LIVES HERE AND NOT ON THE ACCOUNT PAGE.  The button was born on
    the queue list, one per waiting row, and there it was close to useless:
    a list ordered by turn shows at the top the requests that already went
    first, while the one somebody actually wants to overtake sits in place
    one hundred and something, off the page.  "It would be better if the Move
    to front was on the page of the position, because usually you want to
    move something that's like hundreds in the queue to the front"
    (Opabinia); "those buttons clog the interface and essentially useless
    cause you suggest to move what is already basically in front of the queue
    to the very front" (Wolfram).  Here there is exactly one request to talk
    about, the visitor's own on the position in front of them, so there is one
    control and it sits where the impatience is.

    WHEN IT SHOWS: with a session, and with a PENDING request of your own
    here.  Whoever JOINED somebody else's request (``also_requested_by``) does
    not see it, because that row is not theirs and the endpoint would refuse
    it.  The BUTTON needs one thing more, that the request can really move,
    which costs one statement and earns it: a button that answers
    'already-first' is worse than no button.

    THE QUEUE PLACE PAINTED HERE IS THE PLACE OF THIS TASK, counted by the
    same rule the account page prints beside every waiting row
    (``queue_ahead``): a control has to show the number it acts on, or the
    click cannot be read.  The place OUTLIVES the button on purpose, because
    a click that moves a request also takes that request to the front of its
    own queue and so takes the button away with it: with the number gone too,
    the only visible effect of pressing would be that everything disappeared.

    And it is not said TWICE: when the status line above is already narrating
    this very request, the place is said there and only the button goes here.
    """
    return _bump_control_for(_own_pending(pos, username, live), live, back)


def _bump_control_for(task, live, back):
    """``bump_control`` over an ALREADY resolved row (§ ``_own_pending``).

    Existe para que ``context`` pregunte una sola vez de quien es la peticion
    de esta pagina y sirva con esa respuesta los dos controles.  La puerta
    publica se queda como estaba: quien solo tiene la cuenta y la posicion
    sigue llamando a ``bump_control``.
    """
    if task is None:
        return {}
    from .ingest import front_of_own_queue
    can_move = front_of_own_queue(task) is not None
    said = live is not None and live.id == task.id and live.state == PENDING
    ahead = None if said else queue_ahead(task)
    # ``None`` when the status line above already says it, and also when the
    # task left the visitor band and has no place to report: an invented
    # number here would be worse than none, exactly as in that status line.
    place = None if ahead is None else ahead + 1
    if not can_move and place is None:
        return {}
    return {'queue_bump': {
        'task_id': task.id,
        'can_move': can_move,
        'place': place,
        # Where the button comes back to, composed by the page and not echoed
        # from the request: the endpoint takes it as a hint and refuses
        # anything that is not a route of this site.
        'back': back,
    }}


def withdraw_control(pos, username, live=None, back=''):
    """The take-it-back control for THIS position, or ``{}``.

    Peticion de comunidad (asfault): "is there a way to cancel requests before
    they have started - for when you accidentally request analysis for some
    already deeply analysed line".

    VIVE AL LADO DEL DE ADELANTAR, y no por simetria: es que el sitio donde uno
    se da cuenta del error es este.  Acabas de pedir analisis de esta posicion
    y la tabla de abajo te esta ensenando que la linea ya estaba vista, asi que
    la pagina que te lo cuenta es la que tiene que ofrecerte deshacerlo.  Los
    dos controles hablan ADEMAS de la misma fila — tu peticion pendiente de
    aqui — con el mismo permiso y la misma vuelta, asi que salen de la misma
    consulta (§ ``_own_pending``).

    LA CONDICION ES MAS CORTA QUE LA DEL BOTON DE ADELANTAR, y es deliberado:
    aquel necesita ademas poder moverse, porque un boton que contesta
    'already-first' no hace nada.  Retirar SIEMPRE hace algo — la peticion sale
    de la cola — asi que basta con que la fila sea tuya y siga esperando.  La
    primera de tu cola es justamente la que mas se quiere retirar: es la ultima
    que pediste.

    ``backers`` viaja para que la pagina pueda decir la verdad antes del click:
    con mas gente detras, retirar te quita a TI y la peticion sigue viva bajo
    la siguiente persona (§ ``ingest.withdraw_requester``).
    """
    return _withdraw_control_for(_own_pending(pos, username, live), back)


def _withdraw_control_for(task, back):
    """``withdraw_control`` over an ALREADY resolved row (§ ``_own_pending``)."""
    if task is None:
        return {}
    return {'queue_withdraw': {
        'task_id': task.id,
        # Cuantas personas MAS esperan esta peticion.  Cero es el caso normal y
        # entonces retirar la cierra; por encima de cero, retirar es dejar de
        # esperarla tu.
        'backers': task.backers or 0,
        'back': back,
    }}


def context(pos, now=None, username='', back=''):
    """What the template consumes: ``{}`` when there is nothing to say.

    Same shape as ``depth.context`` and ``views._suggestions_badge``: with no
    key, the template does not paint even the gap.

    The live task is read ONCE and serves everything that hangs off it: the
    status line, the move-to-front control and the take-it-back control.  Whose
    request it is gets asked once too, and the two controls share the answer.
    """
    if pos.status != 'UNKNOWN':
        return {}
    live = _live_task(pos)
    out = {}
    row = _narrate(pos, live, now=now)
    if row is not None:
        out['live_request'] = row
    own = _own_pending(pos, username, live)
    out.update(_bump_control_for(own, live, back))
    out.update(_withdraw_control_for(own, back))
    return out
