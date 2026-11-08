"""Cola durable de ingesta: lo que un submit deja escrito y lo que un
proceso aparte aplica despues al arbol.

POR QUE.  Aplicar un submit cuesta lo caro del sistema: verificar PVs de mate
jugada a jugada, expandir la lista legal entera, crear aristas, cascada de
cierres y, desde la fase A, retropropagacion de evals por el DAG.  Todo eso
corria DENTRO de la request del worker, asi que la carga de los workers y la
latencia de los visitantes compartian el mismo SQLite y el mismo hilo.

QUE CAMBIA.  El submit sigue validando lo barato (identidad, lease vivo,
formato) y RECLAMA la tarea igual que antes, con el mismo compare-and-swap;
en el mismo commit deja el payload crudo aqui.  El contrato con el worker no
se mueve: sigue recibiendo 2xx con ``ok`` verdadero, y sus 4xx definitivos
siguen siendo los mismos.

LEASE QUE CADUCA MIENTRAS ESPERA EN LA COLA.  No puede pasar: la reclamacion
(``state='COMPLETED'``) y el encolado son un solo commit.  O la tarea queda
reclamada Y el payload durable, o no ocurre ninguna de las dos.  Una tarea
COMPLETED ya no se re-arrienda, asi que nadie puede re-analizar mientras el
trabajo espera.  El precio, explicito: si un trabajo acaba en FAILED
definitivo, esa tarea no vuelve sola: la posicion se queda con sus ``visits``
sin avanzar y su payload guardado, listo para reintentarse a mano con
``process_ingest_queue --retry-failed``.

IDEMPOTENCIA.  Una tarea tiene como mucho un trabajo (OneToOne), y el
procesador pasa a DONE dentro de la MISMA transaccion que aplica el
resultado.  Reprocesar un trabajo ya hecho devuelve su resumen sin tocar el
arbol.
"""

import logging
import time

from django.conf import settings
from django.contrib.auth.models import User
from django.db import DatabaseError
from django.db.models import Count, F, Q
from django.utils import timezone

from . import ingest, logic, preferences, propagation_queue
from .database import atomic
from .models import AnalysisTask, IngestJob, Position, RequestNotification

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# Reintento con backoff: los fallos reales aqui son "database is locked" y
# cortes puntuales, no errores de datos; media hora de paciencia total.
BACKOFF_SECONDS = (5, 30, 120, 600)
# Un procesador que muere a media faena deja su fila en PROCESSING. Pasado
# este margen otro (o el mismo, tras reiniciar) puede recuperarla: la
# transaccion de aplicacion ya hizo rollback, asi que no hay nada a medias.
CLAIM_STALE_SECONDS = 900


def synchronous_ingest_enabled():
    """Interruptor de despliegue: aplicar en la propia request.

    Con ``ATOMICDB_SYNCHRONOUS_INGEST = True`` el submit encola Y aplica antes
    de responder.  Es el comportamiento de antes de esta fase, pero pasando
    por el MISMO codigo de aplicacion, no por una copia paralela: si el
    servicio procesador se cae, el propietario recupera el comportamiento
    viejo con una linea de settings y sin desplegar otro camino de codigo.
    """
    return bool(getattr(settings, 'ATOMICDB_SYNCHRONOUS_INGEST', False))


def enqueue(task, payload):
    """Deja el payload crudo. El llamante es dueno de la transaccion."""
    job, created = IngestJob.objects.get_or_create(
        task=task,
        defaults={'position_id': task.position_id, 'payload': payload,
                  'next_attempt_at': timezone.now()})
    return job, created


def ready_jobs():
    now = timezone.now()
    stale = now - timezone.timedelta(seconds=CLAIM_STALE_SECONDS)
    return (IngestJob.objects
            .filter(Q(state=IngestJob.JState.PENDING,
                      next_attempt_at__lte=now)
                    | Q(state=IngestJob.JState.PENDING,
                        next_attempt_at__isnull=True)
                    | Q(state=IngestJob.JState.PROCESSING,
                        claimed_at__lt=stale))
            .order_by('id'))


def claim_next():
    """Toma el siguiente trabajo listo, en orden de llegada, o ``None``."""
    with atomic():
        job = ready_jobs().select_for_update(skip_locked=True).first()
        if job is None:
            return None
        job.state = IngestJob.JState.PROCESSING
        job.claimed_at = timezone.now()
        job.save(update_fields=['state', 'claimed_at', 'updated'])
        return job


def _submitting_user(username):
    if not username:
        return None
    return User.objects.filter(username=username).first()


def _notify_requester(job, task):
    """Deja el aviso de que ESTA tarea, que alguien pidio, ya esta servida.

    Corre dentro de la transaccion que pasa el trabajo a DONE: o el resultado
    entra en el arbol Y el aviso existe, o no ocurre ninguna de las dos cosas.
    Aqui es donde tiene sentido y no en el submit, que solo RECLAMA la tarea:
    avisar alli mandaria a la persona a una posicion cuyo resultado todavia
    esta en la cola.

    A TODOS LOS QUE LA PIDIERON, no solo al primero.  Una tarea puede llevar
    varias cuentas encima desde que sumarse a una peticion ajena sustituye a
    crear una fila duplicada (§ ``ingest.add_requester``): avisar solo al autor
    seria cobrarle al segundo la precedencia que su click compro y no darle el
    resultado.  Devuelve la lista de avisos creados o ya existentes.

    Los nombres son texto libre que viajo desde una sesion, asi que un aviso
    solo se crea si hay una cuenta con ese nombre.  La comprobacion toca la
    base de OpenBench, no la de AtomicDB, y va en UNA consulta para todos los
    peticionarios de la tarea — que casi siempre es uno, y solo en los trabajos
    que alguien pidio: la exploracion autonoma no llega aqui.

    EL PRECIO, EXPLICITO: la tarea guarda UNA ruta, la del primero que la
    pidio, asi que todos los avisos de una tarea compartida enlazan por ese
    orden de jugadas.  La posicion es la misma para todos — es literalmente la
    que cada uno pidio — y quien llegue por otra transposicion vera la linea
    del primero en vez de la suya.  Guardar una ruta por persona pide otra
    tabla; hasta que haga falta, esto es una etiqueta imprecisa, no un enlace
    equivocado.
    """
    task = task or {}
    wanted, seen = [], set()
    for name in [task.get('requested_by') or ''] + list(
            task.get('also_requested_by') or []):
        if name and name not in seen:
            seen.add(name)
            wanted.append(name)
    if not wanted:
        return []
    if not ingest.notification_deserved(task.get('source'),
                                        task.get('budget_nodes')):
        return []
    known = set(User.objects.filter(username__in=wanted)
                .values_list('username', flat=True))
    # Quien apago el aviso en sus ajustes no lo recibe; los demas
    # peticionarios de la misma tarea, si.
    known -= preferences.ready_notice_muted(sorted(known))
    notifications = []
    for name in wanted:
        if name not in known:
            continue
        notification, _created = RequestNotification.objects.get_or_create(
            task_id=job.task_id, username=name,
            defaults={'position_id': job.position_id,
                      'route': task.get('route') or ''})
        notifications.append(notification)
    return notifications


def _backoff_seconds(attempts):
    index = min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)
    return BACKOFF_SECONDS[index]


def _mark_failure(job, error):
    """Fuera de la transaccion que acaba de hacer rollback."""
    attempts = job.attempts + 1
    exhausted = attempts >= MAX_ATTEMPTS
    IngestJob.objects.filter(pk=job.pk).update(
        state=(IngestJob.JState.FAILED if exhausted
               else IngestJob.JState.PENDING),
        attempts=attempts,
        claimed_at=None,
        last_error=str(error)[:2000],
        next_attempt_at=(None if exhausted else timezone.now()
                         + timezone.timedelta(
                             seconds=_backoff_seconds(attempts))),
        updated=timezone.now())
    logger.warning('AtomicDB ingest job %s attempt %s failed: %s',
                   job.pk, attempts, error)
    return exhausted


def _absorb_shadowed(job, searched):
    """Cierra las peticiones que este analisis acaba de dejar sin trabajo.

    Reporte de comunidad: peticiones que "are stuck forever in your queue on
    profile description... if you click them, position is already analysed".
    Dos tareas sobre la MISMA posicion — la del selector y la de un click, o
    dos clicks a profundidades distintas — y solo una llega a un worker: la
    otra se quedaba PENDING para siempre, porque nada en el ciclo de vida
    miraba a las demas cuando una entregaba su resultado.  La posicion aparece
    analizada al abrirla y la fila sigue en la cola de su autor, contradiciendo
    a la pagina que la pinta.

    El corte es ``budget_nodes <= searched``: nodos REALMENTE buscados, no lo
    que la tarea servida tenia presupuestado.  Lo que pedia menos de lo que
    acaba de correr ya no puede comprar nada nuevo; lo que pide mas se queda
    esperando, que es exactamente lo que su autor encargo.

    El COMO de absorber (COMPLETED sin nodos ni maquina, y LEASED intacta)
    vive en ``ingest.absorb_tasks``: aqui solo se dice QUE filas.  Este pase y
    el cierre de la posicion (§ ingest._emit_closure_events) responden a la
    misma pregunta desde dos sitios distintos, y dos escrituras parecidas
    acabarian discrepando el dia que cambie una.
    """
    return ingest.absorb_tasks(AnalysisTask.objects
                               .filter(position_id=job.position_id,
                                       budget_nodes__lte=searched)
                               .exclude(pk=job.task_id))


def apply_job(job):
    """Aplica UN trabajo con la semantica transaccional de siempre.

    Lo caro y puro (probar mates, sondear TB) corre FUERA de la transaccion,
    igual que hacia el submit sincrono; la escritura entera, incluido el paso
    a DONE, va dentro de una sola.
    """
    payload = job.payload or {}
    lines = payload.get('lines') or []
    searched = int(payload.get('nodes') or 0)
    elapsed = float(payload.get('elapsed') or 0.0)
    machine = payload.get('machine') or ''
    tb_wdl = payload.get('tb_wdl')
    tb_dtz = payload.get('tb_dtz')
    # El pase venia acotado a las jugadas sin resolver: sus lineas no hablan de
    # la posicion entera (§ views._live_moves).  Ausente = pase completo, que
    # es lo que manda un worker anterior a esa marca.
    restricted = bool(payload.get('restricted'))

    tb_prepared, mate_proofs, rejected = None, None, False
    if tb_wdl is not None:
        tb_prepared = ingest.prepare_tb_closure(
            job.position_id, tb_wdl, dtz=tb_dtz,
            user=_submitting_user(payload.get('username') or ''))
        # Un WDL que el servidor no confirma nunca muta el arbol, y
        # reintentarlo daria exactamente el mismo rechazo: el trabajo se cierra
        # igualmente, registrado (DBEvent TB_REJECTED) y sin efecto.
        rejected = tb_prepared is None
    else:
        fen = Position.objects.only('fen').get(key=job.position_id).fen
        # A position the server's tables cover closes on their exact result,
        # whatever tables the worker had.
        tb_prepared = ingest.prepare_server_tb_closure(job.position_id, fen)
        if tb_prepared is None:
            mate_proofs = ingest.prepare_mate_proofs(fen, lines)

    # De QUE cola salio este trabajo.  Es lo unico que sabe la procedencia de
    # todos los cierres que esta aplicacion vaya a producir — incluidos los
    # que caigan sobre ancestros lejanos en la cascada — asi que se pone aqui
    # y el emisor de eventos lo recoge.  Una tarea borrada deja ``NONE``, que
    # es la verdad: ya no hay a quien apuntarselo.  En la misma lectura viaja
    # lo que decide el aviso al peticionario, que se pregunta lo mismo por la
    # misma fila.
    task = AnalysisTask.objects.filter(pk=job.task_id).values(
        'source', 'requested_by', 'also_requested_by', 'budget_nodes',
        'route', 'then_verify').first()
    source = (task or {}).get('source')
    # Stage (b), the climb through the ancestors, waits in its own durable
    # queue when deferral is on (§ propagation_queue); the job then commits
    # its result on its own position only, and the requested PV
    # verification rides with the climb, because it must read the position
    # after the cascade has had its say.
    deferral = None
    if propagation_queue.deferral_enabled():
        deferral = {'job': job.pk}
        if (task or {}).get('then_verify'):
            deferral['verify'] = {'requested_by': task.get('requested_by'),
                                  'route': task.get('route')}

    with ingest.carried_cuts() as cut, atomic(), \
            ingest.closure_attribution(source):
        current = IngestJob.objects.select_for_update().get(pk=job.pk)
        if current.state == IngestJob.JState.DONE:
            return current.summary or {}
        if rejected:
            summary = {'tb_rejected': True}
        elif tb_prepared is not None:
            closed = ingest._apply_prepared_tb(job.position_id, tb_prepared)
            summary = ({'tb_closed': True} if closed
                       else {'tb_rejected': True})
        else:
            summary = ingest.ingest_analysis(
                job.position_id, lines, searched, machine=machine,
                mate_proofs=mate_proofs, restricted=restricted,
                defer_propagation=deferral)
            # Las peticiones que este pase deja sin trabajo se cierran AQUI,
            # dentro de la misma transaccion que lo aplica: si el analisis se
            # revierte, vuelven enteras a su cola.
            absorbed = _absorb_shadowed(job, searched)
            if absorbed:
                summary['absorbed'] = absorbed
        # El tiempo de motor solo se contabiliza cuando el resultado entra:
        # un WDL rechazado no compra nada y no gasta reloj.
        if elapsed and not summary.get('tb_rejected'):
            Position.objects.filter(key=job.position_id).update(
                time_invested=F('time_invested') + elapsed)
        current.state = IngestJob.JState.DONE
        current.summary = summary
        current.claimed_at = None
        current.next_attempt_at = None
        current.last_error = ''
        current.save(update_fields=['state', 'summary', 'claimed_at',
                                    'next_attempt_at', 'last_error',
                                    'updated'])
        _notify_requester(job, task)
    # A deferred climb does what follows itself, once it has run
    # (§ propagation_queue._after_commit): the proof numbers read what the
    # cascade decides, and the verification reads the position after it.
    climb_pending = summary.get('propagation') == 'deferred'
    # Hook del gestor de prueba.  ``backup_cascade`` ya refresca el cono que
    # cambio, pero un trabajo puede no cascadear nada (WDL rechazado, posicion
    # ya cerrada) y aun asi haber movido la eval que inicializa la hoja.  Esto
    # es lo que garantiza que TODO submit toca los pn/dn de su posicion.
    if not climb_pending:
        ingest.proof.refresh_proof_numbers([job.position_id])
    # An ascent this job cut short is carried on now that the job has
    # committed, instead of waiting for the sweep (see the block above
    # ingest.CARRY_MAX_KEYS).  The job is applied whatever happens here: a
    # carry that fails leaves its frontier on record for the sweep.  What
    # the carry rewrote goes into the job's summary, the one record of it.
    if cut:
        try:
            carried = ingest.carry_cut_ascents(cut)
            if carried is not None:
                summary = {**summary, 'carried': carried}
                IngestJob.objects.filter(pk=job.pk).update(summary=summary)
        except DatabaseError:
            logger.exception('AtomicDB ingest job %s: carrying %d cut keys '
                             'on failed', job.pk, len(set(cut)))
    if ((task or {}).get('then_verify') and not summary.get('tb_rejected')
            and not climb_pending):
        verified = _verify_after(job, task)
        if verified is not None:
            summary = {**summary, 'then_verify': verified}
            IngestJob.objects.filter(pk=job.pk).update(summary=summary)
    return summary


def _verify_after(job, task):
    """Walk the line this job's search found, for its requester.

    "Request + verify PV" (seaside_cliff, 4-Nov-2026): the request asked
    for its line to be verified once the search landed.  This is the Verify
    PV button's purchase (§ ``ingest.enqueue_pv_verification``), run after
    the job has committed so the walk reads the new line, in its own short
    transaction so a failure here never touches the job.  It respects the
    requester's queue cap like the button does, and does nothing on a
    position the search closed.  Returns the number of searches queued, or
    None when nothing was attempted.
    """
    from .views import _account_queue_full
    requested_by = task.get('requested_by') or ''
    if _account_queue_full(requested_by):
        return None
    try:
        with atomic():
            pos = (Position.objects.select_for_update()
                   .filter(key=job.position_id).first())
            if pos is None or pos.status != 'UNKNOWN':
                return None
            outcome = ingest.enqueue_pv_verification(
                pos, requested_by=requested_by,
                route=task.get('route') or '')
        return int(outcome)
    except DatabaseError:
        logger.exception('AtomicDB ingest job %s: verifying the line of '
                         '%s failed', job.pk, job.position_id)
        return None


def process_job(job):
    """``apply_job`` con reintentos: devuelve (resumen, error)."""
    started = time.monotonic()
    try:
        summary = apply_job(job)
    except Exception as error:
        # Cualquier fallo (tipicamente "database is locked") deja la
        # transaccion revertida entera: el arbol no queda a medias y el
        # trabajo vuelve a la cola con backoff.
        exhausted = _mark_failure(job, error)
        return None, ('failed' if exhausted else 'retry')
    logger.info('AtomicDB ingest job %s applied in %.3fs: %s',
                job.pk, time.monotonic() - started, summary)
    return summary, None


def drain(limit=None):
    """Aplica los trabajos listos y devuelve cuantos salieron bien/mal."""
    done, failed = 0, 0
    while limit is None or done + failed < limit:
        job = claim_next()
        if job is None:
            break
        summary, error = process_job(job)
        if error is None:
            done += 1
        else:
            failed += 1
        del summary
    return {'done': done, 'failed': failed}


# How many queued positions one sweep of the server's tables settles at most,
# and how often the processor runs it.  A covered position waits at most one
# period for its closure; no worker is ever served it (§ views choose_pending).
TB_SWEEP_LIMIT = 200
TB_SWEEP_SECONDS = 30.0


def close_queued_by_tables(limit=TB_SWEEP_LIMIT):
    """Close from the server's own tables the queued positions they cover.

    The lease skips a task whose position the server's tables cover, so no
    worker spends a search on a result that sits on the server's disk; this
    is what closes it.  Each closure goes through the same validation and
    landing as a worker's WDL (§ ingest.prepare_server_tb_closure), one
    transaction per position, and the requesters get the same notice as for
    an analysis.  The piece count is read from the FEN in Python, so only a
    covered position reaches the probe.  Returns how many positions closed.
    """
    rows = (AnalysisTask.objects
            .filter(state=AnalysisTask.TState.PENDING,
                    position__status='UNKNOWN')
            .order_by('id')
            .values('id', 'position_id', 'position__fen', 'source',
                    'requested_by', 'also_requested_by', 'budget_nodes',
                    'route'))
    server_max = ingest.tb_server_max_men()
    waiting = {}
    for row in rows.iterator():
        fen = row['position__fen']
        if not logic.tb_applicable(fen, server_max):
            continue
        if row['position_id'] not in waiting and len(waiting) >= limit:
            continue
        waiting.setdefault(row['position_id'], (fen, []))[1].append(row)
    closed = 0
    for key, (fen, tasks) in waiting.items():
        prepared = ingest.prepare_server_tb_closure(key, fen)
        if prepared is None:
            continue
        with atomic(), ingest.closure_attribution(tasks[0]['source']):
            if not ingest._apply_prepared_tb(key, prepared):
                continue
            for task in tasks:
                _notify_requester(_TaskJob(task['id'], key), task)
        closed += 1
    return closed


class _TaskJob:
    """What _notify_requester reads of a job, for a task closed without one."""

    __slots__ = ('task_id', 'position_id')

    def __init__(self, task_id, position_id):
        self.task_id = task_id
        self.position_id = position_id


def retry_failed():
    """Devuelve los FAILED a la cola, con su payload intacto."""
    return IngestJob.objects.filter(state=IngestJob.JState.FAILED).update(
        state=IngestJob.JState.PENDING, attempts=0, claimed_at=None,
        next_attempt_at=timezone.now(), updated=timezone.now())


def queue_depth():
    rows = (IngestJob.objects.values('state')
            .annotate(total=Count('id')).order_by('state'))
    return {row['state']: row['total'] for row in rows}
