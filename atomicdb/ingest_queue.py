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
from django.db.models import Count, F, Q
from django.utils import timezone

from . import ingest
from .database import atomic
from .models import IngestJob, Position

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

    tb_prepared, mate_proofs, rejected = None, None, False
    if tb_wdl is not None:
        tb_prepared = ingest.prepare_tb_closure(
            job.position_id, tb_wdl,
            user=_submitting_user(payload.get('username') or ''))
        # Un WDL que el servidor no confirma nunca muta el arbol, y
        # reintentarlo daria exactamente el mismo rechazo: el trabajo se cierra
        # igualmente, registrado (DBEvent TB_REJECTED) y sin efecto.
        rejected = tb_prepared is None
    else:
        fen = Position.objects.only('fen').get(key=job.position_id).fen
        mate_proofs = ingest.prepare_mate_proofs(fen, lines)

    with atomic():
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
                mate_proofs=mate_proofs)
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
    return summary


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


def retry_failed():
    """Devuelve los FAILED a la cola, con su payload intacto."""
    return IngestJob.objects.filter(state=IngestJob.JState.FAILED).update(
        state=IngestJob.JState.PENDING, attempts=0, claimed_at=None,
        next_attempt_at=timezone.now(), updated=timezone.now())


def queue_depth():
    rows = (IngestJob.objects.values('state')
            .annotate(total=Count('id')).order_by('state'))
    return {row['state']: row['total'] for row in rows}
