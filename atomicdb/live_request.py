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
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .ingest import notification_deserved
from .models import AnalysisTask, WorkerPing

PENDING = AnalysisTask.TState.PENDING
LEASED = AnalysisTask.TState.LEASED
USER = AnalysisTask.Source.USER

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


def queue_ahead(task):
    """Cuantas peticiones de visitante cobran antes que ``task``, o ``None``.

    El mismo orden que ``choose_pending`` (nombradas por delante de las
    anonimas, FIFO dentro de cada estrato), sin el matiz own-first porque
    depende de que worker pregunte.  Es transparencia, no contrato.

    UNA sola sentencia: los dos recuentos que hacen falta salen del mismo
    barrido con ``filter=`` encima, porque esto ya no lo llama solo el recibo
    de un click — lo llama tambien el explorador, en cada render con una
    peticion esperando.
    """
    if task is None or task.state != PENDING or task.source != USER:
        return None
    rows = AnalysisTask.objects.filter(state=PENDING, source=USER).aggregate(
        named=Count('id', filter=Q(requested_by__gt='')),
        named_before=Count('id', filter=Q(requested_by__gt='',
                                          id__lt=task.id)),
        anon_before=Count('id', filter=Q(requested_by='', id__lt=task.id)))
    if task.requested_by:
        return rows['named_before']
    return rows['named'] + rows['anon_before']


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
    now = now or timezone.now()
    task = _live_task(pos)
    if task is None:
        return None
    from .views import LEASE_MINUTES, _human

    budget = f'{_human(task.budget_nodes)} nodes'
    if task.state == PENDING:
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
        return {'text': f'Waiting for a worker · {budget} · {place}',
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


def context(pos, now=None):
    """Lo que la plantilla consume: ``{}`` cuando no hay nada que decir.

    Mismo patron que ``depth.context`` y que ``views._suggestions_badge`` — sin
    clave, la plantilla no pinta ni el hueco.
    """
    row = summary(pos, now=now)
    return {'live_request': row} if row is not None else {}
