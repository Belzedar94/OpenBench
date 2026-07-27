"""API de AtomicDB (worker) + paginas publicas del Explorer."""

import json
import logging
import math
import secrets
import time
from datetime import timedelta
from urllib.parse import quote, urlsplit

from django.contrib.auth import authenticate
from django.core import signing
from django.db import OperationalError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

import re

from django.db.models import (Case, Count, F, IntegerField, Q, Sum, Value,
                              When, Window)
from django.db.models.functions import RowNumber

from . import community_names, ingest, ingest_queue, logic, openings, solve
from .database import atomic
from .metrics import worker_metrics
from .models import (AnalysisTask, Campaign, DBEvent, Edge,
                     OpeningNameSuggestion, Position, RequestLog, SolveTask,
                     WorkerPing)

LEASE_MINUTES = 10
LEGACY_LEASE_MINUTES = 24 * 60
POST_DEPLOY_LEGACY_LEASE_MINUTES = 60
LEGACY_DISPLAY_MINUTES = 60
# One task per lease is intentional. It makes the queue truthful and prevents
# a later task in a sequential batch expiring before the worker even starts it.
TASK_REFILL_COUNT = 4
LEASE_TOKEN_BUILD = 2026072203
LEGACY_MAX_BUDGET = 128_000_000
MAX_REPORTED_NPS = 1_000_000_000_000
REQUESTS_PER_IP_HOUR = 30
REQUEST_QUEUE_MAX = 1000000  # efectivamente sin tope (orden 27-jul; el propietario lo monitoriza)
MAX_SUBMIT_LINES_BYTES = 512 * 1024
MAX_SUBMIT_PV_PLIES = 512
# Breadcrumb reconstruction is public-request work. Keep the cycle-safe
# reverse search useful for recent rows that are not in a materialized map
# yet, but put hard ceilings on graph fan-out and memory.
LINEAGE_SEARCH_MAX_PLIES = 64
LINEAGE_SEARCH_MAX_NODES = 1024
LINEAGE_SEARCH_MAX_FRONTIER = 64
LINEAGE_SEARCH_MAX_PARENTS_PER_CHILD = 16
LINEAGE_SEARCH_MAX_EDGE_ROWS = 4096
PLAY_ROUTE_MAX_PLIES = 64
PLAY_ROUTE_MAX_CHARS = PLAY_ROUTE_MAX_PLIES * 6
PLAY_UCI_RE = re.compile(r'^[a-h][1-8][a-h][1-8][qrbn]?$')
OPENING_ANCHOR_PARAM = 'opening'
OPENING_ANCHOR_SALT = 'atomicdb.explorer.opening-anchor.v1'
OPENING_ANCHOR_MAX_CHARS = 1024
# board.js predates opening anchors and carries only one ``play`` value.
# A distinct, signed sentinel keeps drag/drop navigation working after the
# bounded replay route rolls over, without accepting any extra replay tokens.
OPENING_ANCHOR_PLAY_PREFIX = 'signed-opening.'
# Propuestas publicas de nombre de apertura (§ community_names). Mismo patron
# de rate-limit que RequestLog: contar filas propias por IP en una ventana.
SUGGESTION_NAME_MAX_CHARS = 60
SUGGESTION_NAME_MIN_CHARS = 2
SUGGESTION_COMMENT_MAX_CHARS = 280
SUGGESTIONS_PER_IP_DAY = 10
SUGGESTION_MODERATION_PAGE = 100
# Letras (con acentos), cifras y la puntuacion que un nombre de apertura de
# verdad necesita. Nada de <>&"' ni control chars: el escapado de plantilla ya
# protege, pero un nombre no tiene por que contener markup para empezar.
SUGGESTION_NAME_RE = re.compile(r"^[0-9A-Za-zÀ-ÿ][0-9A-Za-zÀ-ÿ .,:'’\-()/]*$")

logger = logging.getLogger(__name__)


class PlayRouteError(ValueError):
    """An explicit explorer route is malformed or illegal."""

    status_code = 400


class PlayRouteConflict(PlayRouteError):
    """A legal route does not identify the requested AtomicDB position."""

    status_code = 409


def _auth(request):
    user = authenticate(username=request.POST.get('username', ''),
                        password=request.POST.get('password', ''))
    return user


def _touch_worker(request, user):
    """Refresh capacity without changing, extending or reclaiming any lease."""
    machine = request.POST.get('machine', user.username)[:64]
    try:
        threads = max(0, int(request.POST.get('threads', 0) or 0))
        hash_mb = max(0, int(request.POST.get('hash', 0) or 0))
    except ValueError:
        threads, hash_mb = 0, 0
    now = timezone.now()
    capacity = {
        'threads': threads,
        'hash_mb': hash_mb,
        'os': request.POST.get('os', '')[:64],
        'last_seen': now,
    }
    ping, created = WorkerPing.objects.get_or_create(
        machine=machine, user=user.username, defaults=capacity)
    if not created:
        # Do not save the stale model instance: submit and heartbeat update
        # telemetry concurrently and a full save could roll those fields back.
        WorkerPing.objects.filter(pk=ping.pk).update(**capacity)
    return ping


# ---------------- API worker ----------------

def _live_moves(task):
    """Jugadas sin resolver de la posicion: el motor no debe gastar ni un
    nodo re-derivando defensas ya demostradas (go searchmoves). Vacio = sin
    restriccion (nada resuelto aun, o posicion sin expandir)."""
    pos = task.position
    if not pos.expanded:
        return []
    edges = list(Edge.objects.filter(parent=pos).select_related('child'))
    live = [e.move_uci for e in edges if e.child.status == 'UNKNOWN']
    if 0 < len(live) < len(edges):
        return live
    return []


@csrf_exempt
def api_lease(request):
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    machine = ping.machine

    try:
        worker_build = int(request.POST.get('worker_build', 0) or 0)
    except ValueError:
        worker_build = 0
    supports_lease_token = worker_build >= LEASE_TOKEN_BUILD
    lease_session = request.POST.get('lease_session', '')[:64]
    active_task_id = None

    with atomic():
        # recuperar leases caducados
        now = timezone.now()
        stale = now - timedelta(minutes=LEASE_MINUTES)
        legacy_stale = now - timedelta(minutes=LEGACY_LEASE_MINUTES)
        post_legacy_stale = now - timedelta(
            minutes=POST_DEPLOY_LEGACY_LEASE_MINUTES)
        AnalysisTask.objects.filter(
            state='LEASED', lease_token__gt='', leased_at__lt=stale,
        ).filter(
            Q(lease_heartbeat_at__isnull=True)
            | Q(lease_heartbeat_at__lt=stale)
        ).update(state='PENDING', machine='', lease_heartbeat_at=None,
                 lease_token='', lease_session='')
        # A pre-deploy worker cannot heartbeat or fence a deep result. Give
        # already-running tokenless work a one-time long drain window; new
        # legacy assignments below are limited to short first attempts.
        AnalysisTask.objects.filter(
            state='LEASED', lease_token='', lease_heartbeat_at__isnull=False,
            lease_heartbeat_at__lt=post_legacy_stale,
        ).update(state='PENDING', machine='', lease_heartbeat_at=None,
                 lease_session='')
        AnalysisTask.objects.filter(
            state='LEASED', lease_token='', lease_heartbeat_at__isnull=True,
            leased_at__lt=legacy_stale,
        ).update(state='PENDING', machine='', lease_heartbeat_at=None,
                 lease_session='')

        # A second process using the same machine identity must not steal a
        # healthy assignment. The per-assignment token below fences old
        # processes once a genuinely stale lease is recycled.
        active_same_machine = (AnalysisTask.objects.select_for_update()
            .select_related('position')
            .filter(state='LEASED', machine=machine)
            .filter(Q(lease_token='', lease_heartbeat_at__isnull=True,
                      leased_at__gte=legacy_stale)
                    | Q(lease_token='',
                        lease_heartbeat_at__gte=post_legacy_stale)
                    | Q(lease_token__gt='', lease_heartbeat_at__gte=stale)
                    | Q(lease_token__gt='', lease_heartbeat_at__isnull=True,
                        leased_at__gte=stale))
            .order_by('leased_at', 'id').first())

        def choose_pending():
            # Scan the ordered queue, rather than an arbitrary prefix: a
            # non-TB worker must not receive a TB task merely because several
            # TB positions happen to be ahead of the first compatible task.
            first = None
            supports_tb = request.POST.get('tb') == '1'
            queryset = (AnalysisTask.objects
                .select_for_update(skip_locked=True)
                .select_related('position').filter(state='PENDING')
                .order_by('-source', '-position__priority', 'id'))
            for candidate in queryset.iterator(chunk_size=64):
                # A queued follow-up is an intent for the next visit, not a
                # parallel analysis. It becomes runnable only after the prior
                # generation commits, and never runs if that visit solved the
                # position outright.
                if (candidate.position.status != 'UNKNOWN'
                        or candidate.generation > candidate.position.visits):
                    continue
                # A tokenless first attempt remains compatible with a worker
                # that was already deployed before this protocol. Once an
                # attempt has been recycled, only a token-capable build may
                # take it: otherwise the deceased process could submit against
                # a new tokenless lease with the same machine identity.
                if (not supports_lease_token
                        and (candidate.attempts > 0
                             or candidate.budget_nodes > LEGACY_MAX_BUDGET)):
                    continue
                if first is None:
                    first = candidate
                if supports_tb or not logic.tb_applicable(candidate.position.fen):
                    return candidate
            # Preserve the historic fallback when every queued task is TB.
            return first

        batch = []
        replayed = False
        if active_same_machine is not None:
            if (supports_lease_token and lease_session
                    and secrets.compare_digest(
                        active_same_machine.lease_session, lease_session)):
                # The first HTTP response may have been lost after commit.
                # Replay the exact task/token without changing the attempt.
                batch = [active_same_machine]
                replayed = True
            else:
                active_task_id = active_same_machine.id
        else:
            chosen = choose_pending()
            if chosen is None:
                ingest.next_tasks(TASK_REFILL_COUNT)
                chosen = choose_pending()
            if chosen is not None:
                batch = [chosen]
        if not replayed:
            assigned_at = timezone.now()
            for t in batch:
                t.state, t.machine, t.leased_at = 'LEASED', machine, assigned_at
                t.lease_heartbeat_at = assigned_at
                t.lease_token = (secrets.token_urlsafe(32)
                                 if supports_lease_token else '')
                t.lease_session = (lease_session
                                   if supports_lease_token else '')
                t.attempts += 1
                t.save(update_fields=['state', 'machine', 'leased_at',
                                      'lease_heartbeat_at', 'lease_token',
                                      'lease_session', 'attempts'])

    # With one task per lease, the server can expose the exact current task
    # immediately even to the previous worker build, before its first heartbeat.
    if batch:
        WorkerPing.objects.filter(pk=ping.pk).update(
            current_task_id=batch[0].id,
            last_nps=0,
            nps_updated=None,
            last_seen=timezone.now(),
        )
    elif active_task_id is not None:
        WorkerPing.objects.filter(pk=ping.pk).update(
            current_task_id=active_task_id,
            last_seen=timezone.now(),
        )
    else:
        WorkerPing.objects.filter(pk=ping.pk).update(
            current_task_id=None,
            last_nps=0,
            nps_updated=None,
            last_seen=timezone.now(),
        )

    return JsonResponse({'tasks': [
        {'id': t.id, 'fen': t.position.fen, 'budget_nodes': t.budget_nodes,
         'multipv': t.multipv, 'searchmoves': _live_moves(t),
         'lease_token': t.lease_token}
        for t in batch]})


@csrf_exempt
def api_heartbeat(request):
    """Publish live work and keep only that authenticated assignment alive."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    try:
        heartbeat_nps = max(0, min(int(request.POST.get('nps', 0) or 0),
                                   MAX_REPORTED_NPS))
    except ValueError:
        return JsonResponse({'error': 'invalid nps'}, status=400)
    raw_task_id = request.POST.get('task_id', '')
    current_task_id = None
    if raw_task_id:
        try:
            candidate = int(raw_task_id)
        except ValueError:
            return JsonResponse({'error': 'invalid task_id'}, status=400)
        keepalive_at = timezone.now()
        lease_token = request.POST.get('lease_token', '')
        renewed = (AnalysisTask.objects
            .filter(id=candidate, state='LEASED', machine=ping.machine)
            .filter(Q(lease_token='') | Q(lease_token=lease_token))
            .update(lease_heartbeat_at=keepalive_at))
        if renewed == 1:
            current_task_id = candidate
        else:
            return JsonResponse({'error': 'stale-lease'}, status=409)
    updates = {'current_task_id': current_task_id,
               'last_seen': timezone.now(),
               'last_nps': heartbeat_nps if current_task_id else 0,
               'nps_updated': timezone.now() if current_task_id else None}
    WorkerPing.objects.filter(pk=ping.pk).update(**updates)
    return JsonResponse({'ok': True, 'machine': ping.machine,
                         'current_task_id': current_task_id})


@csrf_exempt
def api_submit(request):
    submit_started = time.monotonic()
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    try:
        task_id = int(request.POST['task_id'])
        raw_lines = request.POST['lines']
        if len(raw_lines.encode('utf-8')) > MAX_SUBMIT_LINES_BYTES:
            raise ValueError('lines payload is too large')
        lines = json.loads(raw_lines)
        if not isinstance(lines, list) or len(lines) > 32:
            raise ValueError('lines must be a list with at most 32 entries')
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError('each line must be an object')
            move = line.get('move')
            if not isinstance(move, str) or not move or len(move) > 16:
                raise ValueError('invalid root move')
            for score_name in ('eval_cp', 'mate'):
                score = line.get(score_name)
                if score is not None and (not isinstance(score, int)
                                          or isinstance(score, bool)):
                    raise ValueError(f'invalid {score_name}')
            pv = line.get('pv')
            if pv is not None and (not isinstance(pv, list)
                                   or len(pv) > MAX_SUBMIT_PV_PLIES
                                   or any(not isinstance(item, str)
                                          or len(item) > 16 for item in pv)):
                raise ValueError('invalid or excessively long PV')
            raw = line.get('raw')
            if raw is not None and (not isinstance(raw, str)
                                    or len(raw) > 65_536):
                raise ValueError('raw line is too large')
    except Exception as e:
        return JsonResponse({'error': f'malformed: {e}'}, status=400)

    try:
        elapsed = float(request.POST.get('elapsed', 0) or 0)
        if not math.isfinite(elapsed):
            raise ValueError
        elapsed = min(max(elapsed, 0.0), 86_400.0)
    except (TypeError, ValueError):
        elapsed = 0.0
    try:
        searched = max(0, int(request.POST.get('nodes', 0) or 0))
    except ValueError:
        searched = 0
    tb_wdl = request.POST.get('tb_wdl')
    try:
        parsed_wdl = None if tb_wdl in (None, '') else int(tb_wdl)
    except ValueError:
        return JsonResponse({'error': 'malformed: invalid tb_wdl'}, status=400)
    # DTZ opcional y ADITIVO: sin el, el cierre TB vale solo con el reloj a
    # cero (clock_slack 0), que es lo que ya ocurria.  Un valor ilegible se
    # ignora en vez de rechazar el submit entero: es contexto de reloj, no el
    # hecho exacto, y el hecho exacto ya viene verificado aparte.
    tb_dtz = request.POST.get('tb_dtz')
    try:
        parsed_dtz = None if tb_dtz in (None, '') else int(tb_dtz)
    except (TypeError, ValueError):
        parsed_dtz = None
    if parsed_dtz is not None and abs(parsed_dtz) > 1024:
        parsed_dtz = None

    machine = request.POST.get('machine', '')
    engine_sha = _sha_field(request.POST.get('engine_sha', ''))
    net_sha = _sha_field(request.POST.get('net_sha', ''))
    provided_lease_token = request.POST.get('lease_token', '')
    try:
        snapshot = AnalysisTask.objects.select_related('position').get(id=task_id)
    except AnalysisTask.DoesNotExist:
        return JsonResponse({'error': 'malformed: unknown task'}, status=400)
    if snapshot.state not in ('LEASED', 'COMPLETED'):
        return JsonResponse({'error': 'not-leased'}, status=400)
    if not machine or machine != snapshot.machine:
        return JsonResponse({'error': 'not-your-lease'}, status=409)
    if (snapshot.lease_token
            and not secrets.compare_digest(snapshot.lease_token,
                                           provided_lease_token)):
        return JsonResponse({'error': 'stale-lease'}, status=409)
    if snapshot.state == 'COMPLETED':
        return JsonResponse({'ok': True, 'dup': True})

    # A partir de aqui el submit solo RECLAMA y ENCOLA: la parte cara (probar
    # mates, expandir, aristas, cascadas, retropropagacion) la hace despues
    # process_ingest_queue.  Reclamacion y encolado son un unico commit, asi
    # que un lease no puede caducar con el payload esperando en la cola.
    transaction_started = time.monotonic()
    with atomic():
        claimed = AnalysisTask.objects.filter(
            id=task_id, state='LEASED', machine=machine,
            attempts=snapshot.attempts, leased_at=snapshot.leased_at,
            lease_token=snapshot.lease_token,
        ).update(state='COMPLETED')
        if claimed != 1:
            current = AnalysisTask.objects.get(id=task_id)
            if current.state == 'COMPLETED':
                return JsonResponse({'ok': True, 'dup': True})
            if current.state != 'LEASED':
                return JsonResponse({'error': 'not-leased'}, status=400)
            if (current.machine == machine
                    and (current.attempts != snapshot.attempts
                         or current.leased_at != snapshot.leased_at
                         or current.lease_token != snapshot.lease_token)):
                return JsonResponse({'error': 'stale-lease'}, status=409)
            return JsonResponse({'error': 'not-your-lease'}, status=409)

        task = (AnalysisTask.objects.select_for_update()
                .select_related('position').get(id=task_id))
        searched = min(searched, 2 * task.budget_nodes)
        job, _created = ingest_queue.enqueue(task, {
            'lines': lines,
            'nodes': searched,
            'elapsed': elapsed,
            'machine': machine,
            'username': user.username,
            'tb_wdl': parsed_wdl,
            'tb_dtz': parsed_dtz,
        })
        task.state, task.machine = 'COMPLETED', machine
        task.completed = timezone.now()
        task.nodes_searched = searched
        task.elapsed_seconds = elapsed
        # Procedencia opcional: un worker anterior a este build no la manda y
        # las columnas se quedan vacias, que es exactamente lo que ya pasaba.
        task.engine_sha = engine_sha
        task.net_sha = net_sha
        task.save(update_fields=[
            'state', 'machine', 'completed', 'nodes_searched',
            'elapsed_seconds', 'engine_sha', 'net_sha'])
        ping_updates = {
            'tasks_done': F('tasks_done') + 1,
            'last_seen': timezone.now(),
        }
        if searched > 0 and elapsed > 0:
            ping_updates.update({
                'last_nps': min(round(searched / elapsed),
                                MAX_REPORTED_NPS),
                'nps_updated': timezone.now(),
            })
        ping_updates['current_task_id'] = None
        WorkerPing.objects.filter(machine=machine, user=user.username).update(
            **ping_updates)
    transaction_seconds = time.monotonic() - transaction_started

    summary = {'queued': True, 'ingest_job': job.pk}
    apply_seconds = 0.0
    if ingest_queue.synchronous_ingest_enabled():
        # Interruptor de despliegue: mismo codigo de aplicacion, aqui mismo.
        apply_started = time.monotonic()
        applied, error = ingest_queue.process_job(job)
        apply_seconds = time.monotonic() - apply_started
        summary = applied if error is None else {'ingest_job': job.pk,
                                                 'ingest_error': error}
    total_seconds = time.monotonic() - submit_started
    logger.info(
        'AtomicDB submit task=%s machine=%s kind=%s lines=%s job=%s '
        'transaction_seconds=%.3f apply_seconds=%.3f total_seconds=%.3f',
        task_id, machine, 'tb' if parsed_wdl is not None else 'engine',
        len(lines), job.pk, transaction_seconds, apply_seconds, total_seconds)
    summary = dict(summary or {})
    summary['submit_timing_seconds'] = {
        'transaction': round(transaction_seconds, 3),
        'apply': round(apply_seconds, 3),
        'total': round(total_seconds, 3),
    }
    return JsonResponse({'ok': True, 'summary': summary})


def _sha_field(raw):
    """Un sha256 hex en minusculas, o cadena vacia. Nunca levanta.

    Es telemetria opcional que llega de un cliente no confiable: se sanea a
    lo que la columna admite y punto.  No cierra nada, no autoriza nada; solo
    hace ATRIBUIBLE un sesgo de evals a un binario concreto.
    """
    raw = str(raw or '').strip().lower()
    if len(raw) != 64 or any(ch not in '0123456789abcdef' for ch in raw):
        return ''
    return raw


# ---------------- API SOLVE (aditiva) ----------------
#
# Un worker anterior a este protocolo no llama a ninguno de estos endpoints y
# no cambia nada para el.  El patron de arriendo es EL MISMO que el de
# AnalysisTask — token de fencing por asignacion, heartbeat separado, sesion
# replayable — porque ya esta probado contra los modos de fallo reales
# (respuesta perdida tras el commit, proceso zombi con la misma identidad de
# maquina) y dos patrones distintos serian dos superficies de fallo.

SOLVE_LEASE_MINUTES = 20
MAX_SOLVE_CERTIFICATE_BYTES = solve.MAX_COMPRESSED_BYTES


@csrf_exempt
def api_solve_acquire(request):
    """Arrienda UNA tarea de prueba. Sin tareas, responde con la lista vacia."""
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    machine = ping.machine
    lease_session = request.POST.get('lease_session', '')[:64]

    with atomic():
        now = timezone.now()
        stale = now - timedelta(minutes=SOLVE_LEASE_MINUTES)
        SolveTask.objects.filter(
            state='LEASED', leased_at__lt=stale,
        ).filter(Q(lease_heartbeat_at__isnull=True)
                 | Q(lease_heartbeat_at__lt=stale)).update(
            state='PENDING', machine='', lease_heartbeat_at=None,
            lease_token='', lease_session='')

        active = (SolveTask.objects.select_for_update()
                  .select_related('position')
                  .filter(state='LEASED', machine=machine,
                          lease_heartbeat_at__gte=stale)
                  .order_by('leased_at', 'id').first())
        if active is not None:
            if lease_session and secrets.compare_digest(
                    active.lease_session, lease_session):
                # The first response may have been lost after the commit.
                # Replay the same assignment without burning an attempt.
                return JsonResponse({'tasks': [_solve_payload(active)]})
            return JsonResponse({'tasks': []})

        task = (SolveTask.objects.select_for_update(skip_locked=True)
                .select_related('position').filter(state='PENDING')
                .order_by('-budget_nodes', 'id').first())
        if task is None:
            return JsonResponse({'tasks': []})
        assigned_at = timezone.now()
        task.state, task.machine, task.leased_at = 'LEASED', machine, assigned_at
        task.lease_heartbeat_at = assigned_at
        task.lease_token = secrets.token_urlsafe(32)
        task.lease_session = lease_session
        task.attempts += 1
        task.save(update_fields=['state', 'machine', 'leased_at',
                                 'lease_heartbeat_at', 'lease_token',
                                 'lease_session', 'attempts'])
    return JsonResponse({'tasks': [_solve_payload(task)]})


def _solve_payload(task):
    return {
        'id': task.id,
        'fen': task.position.fen,
        'goal': task.goal,
        'budget_nodes': task.budget_nodes,
        'ruleset': logic.RULESET_ID,
        'lease_token': task.lease_token,
    }


@csrf_exempt
def api_solve_heartbeat(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    try:
        task_id = int(request.POST.get('task_id', ''))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'invalid task_id'}, status=400)
    renewed = (SolveTask.objects
               .filter(id=task_id, state='LEASED', machine=ping.machine,
                       lease_token=request.POST.get('lease_token', ''))
               .update(lease_heartbeat_at=timezone.now()))
    if renewed != 1:
        return JsonResponse({'error': 'stale-lease'}, status=409)
    return JsonResponse({'ok': True, 'current_task_id': task_id})


@csrf_exempt
def api_solve_submit(request):
    """Recibe un resultado SOLVE. El certificado se VERIFICA aqui, entero."""
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    try:
        task_id = int(request.POST['task_id'])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({'error': 'malformed: task_id'}, status=400)
    outcome = request.POST.get('outcome', '')
    if outcome not in ('PROVED', 'DISPROVED', 'UNKNOWN'):
        return JsonResponse({'error': 'malformed: outcome'}, status=400)
    machine = request.POST.get('machine', '')
    provided_token = request.POST.get('lease_token', '')

    try:
        task = SolveTask.objects.select_related('position').get(id=task_id)
    except SolveTask.DoesNotExist:
        return JsonResponse({'error': 'malformed: unknown task'}, status=400)
    if task.state == 'COMPLETED':
        return JsonResponse({'ok': True, 'dup': True})
    if task.state != 'LEASED':
        return JsonResponse({'error': 'not-leased'}, status=400)
    if not machine or machine != task.machine:
        return JsonResponse({'error': 'not-your-lease'}, status=409)
    if task.lease_token and not secrets.compare_digest(task.lease_token,
                                                       provided_token):
        return JsonResponse({'error': 'stale-lease'}, status=409)

    certificate = request.FILES.get('certificate')
    blob = certificate.read(MAX_SOLVE_CERTIFICATE_BYTES + 1) \
        if certificate is not None else b''
    if len(blob) > MAX_SOLVE_CERTIFICATE_BYTES:
        return JsonResponse({'error': 'certificate too large'}, status=413)

    def as_int(name):
        try:
            return int(request.POST.get(name, ''))
        except (TypeError, ValueError):
            return None

    summary = ingest.apply_solve_result(
        task, outcome=outcome, certificate_blob=blob or None,
        advisory_pn=as_int('pn'), advisory_dn=as_int('dn'),
        searched_nodes=as_int('nodes') or 0,
        elapsed_seconds=request.POST.get('elapsed', ''),
        solver_build=_sha_field(request.POST.get('solver_build', ''))
        or request.POST.get('solver_build', '')[:64])
    return JsonResponse({'ok': True, 'summary': summary})


def _client_ip(request):
    """Ultima entrada de XFF: la puso nuestro nginx, el cliente no la falsea."""
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[-1].strip()
            or request.META.get('REMOTE_ADDR', '0.0.0.0'))


def _api_request_once(request, key):
    """Peticion publica de analisis (estilo chessdb.cn), sin cuenta.
    Protecciones: rate-limit por IP, dedup ip+posicion, tope global de cola."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    ip = _client_ip(request)
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return JsonResponse({'status': 'unknown-position'}, status=404)
    hour_ago = timezone.now() - timedelta(hours=1)
    # A recent click is only a duplicate while the request it represented is
    # still queued or running.  Once that task has completed, the same visitor
    # must be able to request the next 128M -> 512M -> 2B -> 10B rung.  A
    # RequestLog may also have been created merely by adding a new FEN, without
    # ever creating an AnalysisTask, so the log alone is not evidence of work.
    recent_same_position = RequestLog.objects.filter(
        ip=ip, created__gte=hour_ago, position=pos,
    ).exists()
    active_user_request = AnalysisTask.objects.filter(
        position=pos,
        source=AnalysisTask.Source.USER,
        state__in=(AnalysisTask.TState.PENDING, AnalysisTask.TState.LEASED),
    ).exists()
    if recent_same_position and not active_user_request \
            and ingest.ladder_exhausted(pos):
        # A frontier expansion puts its tasks on the CHILDREN, so the parent
        # row alone can no longer prove that the previous click is still
        # being served.  One click stays one expansion event.
        active_user_request = AnalysisTask.objects.filter(
            position__edges_in__parent=pos,
            source=AnalysisTask.Source.USER,
            state__in=(AnalysisTask.TState.PENDING,
                       AnalysisTask.TState.LEASED),
        ).exists()
    if recent_same_position and active_user_request:
        return JsonResponse({'status': 'already-requested'})
    if RequestLog.objects.filter(ip=ip, created__gte=hour_ago) \
                         .count() >= REQUESTS_PER_IP_HOUR:
        return JsonResponse({'status': 'rate-limited'}, status=429)
    if AnalysisTask.objects.filter(state='PENDING', source='USER',
                                   position__status='UNKNOWN') \
                           .count() >= REQUEST_QUEUE_MAX:
        return JsonResponse({'status': 'queue-full'}, status=503)
    # Task creation/promotion and its rate-limit receipt are one commit.  A
    # lock while writing RequestLog must roll the task mutation back as well;
    # otherwise a browser retry could accidentally request the next rung.
    with atomic():
        outcome = ingest.request_analysis(pos)
        # 'saturated' bought nothing, but it walked the tree to find that out
        # and the honest answer is still an answer: one click, one receipt,
        # so the hourly allowance keeps bounding what a descent costs us.
        if outcome in ('queued', 'already-queued', 'expanded', 'saturated'):
            RequestLog.objects.create(ip=ip, position=pos)
    payload = {'status': str(outcome)}
    # Only a frontier expansion carries counters; every other status keeps
    # the exact single-key body explore.html has always parsed.
    payload.update(getattr(outcome, 'detail', None) or {})
    return JsonResponse(payload)


@csrf_exempt
def api_request(request, key):
    try:
        return _api_request_once(request, key)
    except OperationalError as error:
        # SQLite can reject a read->write transaction upgrade immediately
        # while the worker is committing a large analysis, even with a long
        # busy_timeout.  The atomic task+receipt block above guarantees that a
        # structured busy response has no committed side effect and is safe
        # for the browser to retry.
        message = str(error).lower()
        if 'database is locked' not in message \
                and 'database table is locked' not in message:
            raise
        response = JsonResponse({'status': 'busy'}, status=503)
        response['Retry-After'] = '2'
        return response


# ---------------- paginas publicas ----------------

def _board_rows(fen):
    """Filas 8..1 de [(casilla, pieza)] para la plantilla SVG."""
    board = fen.split()[0]
    rows = []
    for rank_str in board.split('/'):
        row = []
        for ch in rank_str:
            if ch.isdigit():
                row.extend([''] * int(ch))
            else:
                row.append(ch)
        rows.append(row)
    return rows


def _piece_code(ch):
    if not ch:
        return ''
    color = 'w' if ch.isupper() else 'b'
    return color + ch.upper()


def _ctx_board(fen):
    rows = _board_rows(fen)
    out = []
    for r, row in enumerate(rows):
        rank = 8 - r
        out.append([(_piece_code(p), (r + c) % 2 == 1,
                     'abcdefgh'[c] + str(rank))
                    for c, p in enumerate(row)])
    return out


def _route_query(ucis, opening_anchor=None):
    """Canonical query suffix for validated route or signed opening state."""
    parts = []
    if ucis:
        parts.append('play=' + ','.join(ucis))
    if opening_anchor:
        parts.append(
            f'{OPENING_ANCHOR_PARAM}='
            + quote(opening_anchor, safe=''),
        )
    return '' if not parts else '?' + '&'.join(parts)


def _explore_url(key, ucis=None, opening_anchor=None):
    return (
        f'/atomicdb/explore/{key}/'
        + _route_query(ucis, opening_anchor)
    )


def _goto_url(key, uci, ucis=None, opening_anchor=None):
    return (
        f'/atomicdb/goto/{key}/{uci}/'
        + _route_query(ucis, opening_anchor)
    )


def _signed_opening_anchor(target_key, match, route_ply):
    """Bind the last catalogued opening to one exact explorer target.

    The token is only an overflow continuation for a route that was already
    replayed and validated.  Names never come from the token: consumers load
    the signed opening key from the immutable catalogue again.
    """
    if match is None:
        return None
    opening_key = match.get('position_key')
    matched_ply = match.get('matched_ply')
    if (
        not isinstance(opening_key, str)
        or isinstance(matched_ply, bool)
        or not isinstance(matched_ply, int)
        or matched_ply < 0
        or isinstance(route_ply, bool)
        or not isinstance(route_ply, int)
        or route_ply < matched_ply
    ):
        return None
    exact = openings.lookup_key(opening_key)
    if exact is None or exact.get('position_key') != opening_key:
        return None
    return signing.dumps(
        {
            'v': 1,
            'target': target_key,
            'opening': opening_key,
            'matched_ply': matched_ply,
            'route_ply': route_ply,
        },
        salt=OPENING_ANCHOR_SALT,
        compress=True,
    )


def _validated_opening_anchor(raw, target_key):
    """Return a catalog-backed opening match and route ply, or fail closed."""
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > OPENING_ANCHOR_MAX_CHARS
    ):
        return None, None
    try:
        payload = signing.loads(raw, salt=OPENING_ANCHOR_SALT)
    except (signing.BadSignature, TypeError, ValueError):
        return None, None
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            'v', 'target', 'opening', 'matched_ply', 'route_ply',
        }
        or payload.get('v') != 1
        or payload.get('target') != target_key
    ):
        return None, None
    opening_key = payload.get('opening')
    matched_ply = payload.get('matched_ply')
    route_ply = payload.get('route_ply')
    if (
        not isinstance(opening_key, str)
        or isinstance(matched_ply, bool)
        or not isinstance(matched_ply, int)
        or matched_ply < 0
        or isinstance(route_ply, bool)
        or not isinstance(route_ply, int)
        or route_ply < matched_ply
    ):
        return None, None
    exact = openings.lookup_key(opening_key)
    if exact is None or exact.get('position_key') != opening_key:
        return None, None
    match = dict(exact)
    match.update({
        'matched_ply': matched_ply,
        'current_key': target_key,
        'exact': opening_key == target_key,
    })
    return match, route_ply


def _navigation_opening(active_ucis, raw_anchor, target_key, *,
                        explicit_play=False):
    """Resolve opening state and the route that navigation should propagate.

    An explicitly supplied and validated ``play`` route wins.  Otherwise a
    valid target-bound opening anchor wins over reconstructed lineage: the
    latter may be a shorter transposition and must not erase overflow state.
    """
    if explicit_play and active_ucis is not None:
        try:
            match = openings.match_line(active_ucis)
        except openings.InvalidOpeningLine:
            match = None
        return match, len(active_ucis), active_ucis
    anchored, route_ply = _validated_opening_anchor(raw_anchor, target_key)
    if anchored is not None:
        return anchored, route_ply, None
    if active_ucis is not None:
        try:
            match = openings.match_line(active_ucis)
        except openings.InvalidOpeningLine:
            match = None
        return match, len(active_ucis), active_ucis
    exact = openings.lookup_key(target_key)
    if exact is None:
        return None, None, None
    return exact, exact['matched_ply'], None


def _opening_after_move(current, child_key, child_ply):
    """Advance a trusted opening anchor by one already-validated legal move."""
    exact = openings.lookup_key(child_key)
    if exact is not None:
        result = dict(exact)
        if child_ply is not None:
            result['matched_ply'] = child_ply
        result.update({'current_key': child_key, 'exact': True})
        return result
    if current is None:
        return None
    result = dict(current)
    result.update({'current_key': child_key, 'exact': False})
    return result


def _child_navigation_state(active_ucis, current_opening, route_ply,
                            child_key, move_uci):
    """Choose a bounded replay route or a signed overflow anchor for a child."""
    child_ply = None if route_ply is None else route_ply + 1
    child_opening = _opening_after_move(
        current_opening, child_key, child_ply)
    child_route = (
        None if active_ucis is None else [*active_ucis, move_uci]
    )
    if child_route is not None and len(child_route) <= PLAY_ROUTE_MAX_PLIES:
        return child_route, None
    anchor_ply = child_ply
    if anchor_ply is None and child_opening is not None:
        anchor_ply = child_opening.get('matched_ply')
    child_anchor = _signed_opening_anchor(
        child_key, child_opening, anchor_ply)
    return None, child_anchor


def _validated_play_route(raw, target_key):
    """Return a read-only, startpos-rooted route or ``None``.

    ``play`` is deliberately a compact list of UCI tokens rather than trusted
    lineage.  Every move is replayed with the Atomic rules, the terminal key
    must be the requested page, and every prefix must already exist in
    AtomicDB.  This makes a GET incapable of materialising a route merely by
    naming one.
    """
    if raw is None:
        return None
    if len(raw) > PLAY_ROUTE_MAX_CHARS:
        raise PlayRouteError('move path exceeds the supported length')
    ucis = [] if raw == '' else raw.split(',')
    if len(ucis) > PLAY_ROUTE_MAX_PLIES or any(
            not PLAY_UCI_RE.fullmatch(uci) for uci in ucis):
        raise PlayRouteError('move path contains an invalid UCI token')

    import pyffish as pf

    fen = logic.start_fen()
    prefix_keys = [logic.key_of(fen)]
    white = True
    line = []
    for uci in ucis:
        if uci not in logic.legal_moves(fen):
            raise PlayRouteError('move path contains an illegal Atomic move')
        try:
            san = pf.get_san('atomic', fen, uci)
            fen = logic.apply_move(fen, uci)
        except Exception as exc:
            raise PlayRouteError(
                'move path could not be replayed under Atomic rules') from exc
        child_key = logic.key_of(fen)
        prefix_keys.append(child_key)
        line.append({
            'uci': uci, 'san': san, 'key': child_key, 'white': white,
        })
        white = not white

    if prefix_keys[-1] != target_key:
        raise PlayRouteConflict(
            'move path does not reach the requested position')
    positions = Position.objects.only('key', 'fen').in_bulk(prefix_keys)
    if any(key not in positions for key in prefix_keys):
        raise PlayRouteConflict(
            'move path is not fully materialized in AtomicDB')
    return positions[prefix_keys[0]], line, ucis


def _play_route_error_response(exc):
    response = HttpResponse(
        str(exc), status=exc.status_code, content_type='text/plain')
    response['Cache-Control'] = 'no-store'
    return response


def _canonical_route(pos):
    """Fallback lineage plus a replayable route when startpos was reached."""
    top, line = _line_to_root(pos)
    if top.fen != logic.start_fen():
        return top, line, None
    return top, line, [step['uci'] for step in line]


def goto(request, key, uci):
    """Navegacion jugando: valida la jugada, crea/encuentra el hijo y salta."""
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return redirect('/atomicdb/')
    raw_play = request.GET.get('play')
    board_anchor = None
    if (
        isinstance(raw_play, str)
        and raw_play.startswith(OPENING_ANCHOR_PLAY_PREFIX)
    ):
        board_anchor = raw_play[len(OPENING_ANCHOR_PLAY_PREFIX):]
        raw_play = None
    try:
        route = _validated_play_route(raw_play, key)
    except PlayRouteError as exc:
        return _play_route_error_response(exc)
    if route is None:
        _top, _line, active_ucis = _canonical_route(pos)
    else:
        _top, _line, active_ucis = route
    current_opening, route_ply, active_ucis = _navigation_opening(
        active_ucis,
        request.GET.get(OPENING_ANCHOR_PARAM) or board_anchor,
        pos.key,
        explicit_play=route is not None,
    )
    current_anchor = (
        None if active_ucis is not None
        else _signed_opening_anchor(pos.key, current_opening, route_ply)
    )
    if uci not in logic.legal_moves(pos.fen):
        return redirect(_explore_url(key, active_ucis, current_anchor))
    child = ingest.get_or_create_position(logic.apply_move(pos.fen, uci),
                                          campaign=pos.campaign)
    Edge.objects.get_or_create(parent=pos, move_uci=uci,
                               defaults={'child': child})
    if child.priority <= ingest.DEAD / 2:
        child.priority = 0.0   # ruta nueva: revive de la lapida
        child.save(update_fields=['priority'])
    child_route, child_anchor = _child_navigation_state(
        active_ucis, current_opening, route_ply, child.key, uci)
    return redirect(_explore_url(child.key, child_route, child_anchor))


def _format_san_line(top, line, max_plies=16, keep_head=False):
    """Linea SAN numerada hasta la raiz ("1. Nf3 f6 ..."). Al truncar,
    keep_head conserva el PRINCIPIO (para ver el opening); de lo contrario se
    conserva el final. Un fragmento que no alcanza startpos queda sin numeros:
    el FEN canonico no conserva el ply absoluto y no debemos inventar "1..."."""
    if not line:
        return '' if top.fen == logic.start_fen() else '…'
    from_start = top.fen == logic.start_fen()
    parts = []
    if from_start:
        n = 1
        for i, st in enumerate(line):
            if st['white']:
                parts.append(f"{n}. {st['san']}")
            else:
                parts.append(f"{n}... {st['san']}" if i == 0 else st['san'])
                n += 1
    else:
        parts = [st['san'] for st in line]
    prefix = '' if from_start else '… '
    suffix = ''
    if len(parts) > max_plies:
        if keep_head:
            parts = parts[:max_plies]
            suffix = ' …'
        else:
            parts = parts[-max_plies:]
            prefix = '… '
    return prefix + ' '.join(parts) + suffix


def _lines_to_root(keys, max_plies=LINEAGE_SEARCH_MAX_PLIES):
    """Resolve canonical minimum-ply breadcrumbs from startpos in batches.

    The home page contains queue rows and milestones with heavily overlapping
    ancestry. Resolving each independently caused hundreds of small queries.
    A single-parent greedy walk is not safe here: reversible moves and
    transpositions can make the lexicographically first parent enter a cycle
    even though another parent leads to startpos. Reverse BFS explores every
    parent at the current distance, remains cycle-safe, and uses one Edge query
    per ply for all requested positions, up to the public-search ceilings.
    """
    import pyffish as pf

    keys = list(dict.fromkeys(key for key in keys if key))
    positions = Position.objects.only('key', 'fen').in_bulk(keys)
    start_key = logic.key_of(logic.start_fen())
    states = {}
    for key in keys:
        if key not in positions:
            continue
        states[key] = {
            'frontier': {key},
            'seen': {key},
            # For every discovered ancestor: the first edge towards target.
            'toward_target': {},
            'nodes': {key: positions[key]},
            'distance': {key: 0},
            'sources': [],
            'found_start': key == start_key,
        }
    active = {
        key for key, state in states.items() if not state['found_start']
    }

    for _ in range(min(max_plies, LINEAGE_SEARCH_MAX_PLIES)):
        child_ids = set()
        for key in active:
            child_ids.update(states[key]['frontier'])
        if not child_ids:
            break
        parents_by_child = {}
        root_first = Case(
            When(parent_id=start_key, then=Value(0)),
            default=Value(1), output_field=IntegerField(),
        )
        edges = (Edge.objects.filter(child_id__in=child_ids)
                 .select_related('parent')
                 .only('child_id', 'move_uci', 'parent__key', 'parent__fen')
                 .annotate(_lineage_rank=Window(
                     expression=RowNumber(),
                     partition_by=[F('child_id')],
                     order_by=(
                         root_first.asc(), F('parent_id').asc(),
                         F('move_uci').asc(),
                     ),
                 ))
                 .filter(_lineage_rank__lte=
                         LINEAGE_SEARCH_MAX_PARENTS_PER_CHILD)
                 # Rank-first ordering gives every frontier child its best
                 # parent before any child receives a second one. The root
                 # edge, when present, is always rank 1.
                 .order_by('_lineage_rank', 'child_id',
                           'parent_id', 'move_uci')
                 [:LINEAGE_SEARCH_MAX_EDGE_ROWS])
        for edge in edges:
            parents_by_child.setdefault(edge.child_id, []).append(edge)
        next_active = set()
        for key in sorted(active):
            state = states[key]
            next_frontier = set()
            for child_key in sorted(state['frontier']):
                child_parents = parents_by_child.get(child_key, ())
                if not child_parents:
                    state['sources'].append(
                        (state['distance'][child_key], child_key))
                for edge in child_parents:
                    parent_key = edge.parent_id
                    if parent_key in state['seen']:
                        continue
                    # Always admit startpos when it is on this layer. Other
                    # ancestry is bounded so a highly transposed component
                    # cannot turn the public home page into a graph scan.
                    if (parent_key != start_key
                            and (len(state['seen'])
                                 >= LINEAGE_SEARCH_MAX_NODES
                                 or len(next_frontier)
                                 >= LINEAGE_SEARCH_MAX_FRONTIER)):
                        continue
                    state['seen'].add(parent_key)
                    state['nodes'][parent_key] = edge.parent
                    state['distance'][parent_key] = (
                        state['distance'][child_key] + 1)
                    state['toward_target'][parent_key] = (
                        child_key, edge.move_uci)
                    next_frontier.add(parent_key)
            state['frontier'] = next_frontier
            if start_key in state['seen']:
                state['found_start'] = True
                continue
            if next_frontier:
                next_active.add(key)
        active = next_active
        if not active:
            break

    result = {}
    for key, state in states.items():
        if state['found_start']:
            top_key = start_key
        elif state['sources']:
            # Prefer the available boundary that provides the most context.
            _distance, top_key = min(
                state['sources'],
                key=lambda source: (-source[0], source[1]),
            )
        else:
            # A closed cyclic component or the max-depth boundary: retain the
            # longest acyclic context found, but do not pretend it is move 1.
            top_key = min(
                state['seen'],
                key=lambda node_key: (-state['distance'][node_key], node_key),
            )

        ordered = []
        cursor = top_key
        while cursor != key:
            step = state['toward_target'].get(cursor)
            if step is None:
                ordered = []
                top_key = key
                break
            child_key, move_uci = step
            ordered.append((move_uci, child_key))
            cursor = child_key

        top = state['nodes'][top_key]
        ucis = [uci for uci, _child_key in ordered]
        try:
            # One C++ call parses and advances the full line. Calling
            # get_san()+get_fen() for every ply dominated the home page once
            # milestones grew into long PVs (roughly 500 crossings/request).
            sans = list(pf.get_san_moves('atomic', top.fen, ucis))
            if len(sans) != len(ordered):
                raise ValueError('incomplete SAN line')
        except Exception:
            # Preserve the previous per-ply behaviour if this PyFFish build
            # cannot batch a line or returns an incomplete result.
            sans, fen = [], top.fen
            for uci in ucis:
                try:
                    sans.append(pf.get_san('atomic', fen, uci))
                except Exception:
                    sans.append(uci)
                fen = logic.apply_move(fen, uci)
        white = top.fen.split()[1] == 'w'
        line = []
        for (uci, child_key), san in zip(ordered, sans):
            line.append({
                'uci': uci, 'san': san, 'key': child_key, 'white': white,
            })
            white = not white
        result[key] = (top, line)
    return result


def _line_labels_many(keys, preview_plies=10):
    resolved = _lines_to_root(keys)
    labels = {}
    for key, (top, line) in resolved.items():
        full = _format_san_line(top, line, max_plies=512, keep_head=True)
        preview = _format_san_line(top, line, max_plies=preview_plies,
                                   keep_head=True)
        labels[key] = (preview, full)
    return labels


def _line_labels(key, preview_plies=10):
    """Opening-first preview and full breadcrumb from one batched traversal."""
    return _line_labels_many([key], preview_plies).get(key, ('', ''))


def _san_line(key, max_plies=16, keep_head=False):
    """Backward-compatible single label helper."""
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return ''
    top, line = _line_to_root(pos, max_plies=max(64, max_plies))
    return _format_san_line(top, line, max_plies=max_plies,
                            keep_head=keep_head)


def _friendly_events(events, labels=None):
    out = []
    for e in events:
        pl = e.payload or {}
        key = pl.get('key', '')
        if key:
            san, full = (labels or {}).get(key) or _line_labels(key)
        else:
            san, full = '', ''
        if e.kind == 'NODE_CLOSED':
            txt = f"Solved: {pl.get('status', '?')} via {pl.get('closure', '?')}"
        elif e.kind == 'CAMPAIGN_CLOSED':
            txt = f"Campaign {pl.get('campaign', '?')} SOLVED: {pl.get('status', '?')}"
            key = ''
        else:
            txt = e.kind
        out.append({'ts': e.ts, 'text': txt, 'key': key, 'san': san,
                    'full': full})
    return out


def _human(n):
    """3322 -> '3,322'; 108600000 -> '108.6M'; 2400000000 -> '2.40B'."""
    n = n or 0
    if n >= 1_000_000_000_000:
        return f'{n / 1e12:.2f}T'
    if n >= 1_000_000_000:
        return f'{n / 1e9:.2f}B'
    if n >= 1_000_000:
        return f'{n / 1e6:.1f}M'
    return f'{n:,}'


def _move_css(status, score, win_status):
    """Color RELATIVO AL QUE MUEVE: su victoria en verde, su derrota en rojo."""
    if status == 'DRAW':
        return 'draw'
    if status != 'UNKNOWN':
        return 'won' if status == win_status else 'lost'
    e = abs(score or 0)
    return 'hot' if e >= 500 else ('warm' if e >= 200 else 'cold')


def _child_moves(pos):
    """Tabla de hijos en perspectiva DEL QUE MUEVE (convencion chessdb.cn).
    El almacenamiento interno sigue siendo White-POV; solo la vista voltea —
    exactamente UN cambio de signo por ply mostrado.
    Cada fila muestra el MEJOR CONOCIMIENTO ACTUAL del hijo (status probado >
    backed > eval puntual); ``point`` conserva la eval puntual cruda para que
    el respaldo nunca borre lo que el motor dijo de verdad.
    Orden: victorias del que mueve, luego por score, derrotas al final."""
    stm_white = pos.fen.split()[1] == 'w'
    win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    loss = 'BLACK_WIN' if stm_white else 'WHITE_WIN'
    moves = []
    for e in Edge.objects.filter(parent=pos).select_related('child'):
        c = e.child
        mate = None
        point = None if c.eval_cp is None else (
            c.eval_cp if stm_white else -c.eval_cp)
        backed_plies = 0
        if c.status == win:
            score, rank = 10_000, 10_001
        elif c.status == loss:
            score, rank = -10_000, -10_001
        elif c.status == 'DRAW':
            score, rank = 0, 0
        elif c.backed_eval is not None or c.eval_cp is not None:
            known = ingest.best_known_eval(c)
            score = known if stm_white else -known
            rank = score
            if c.backed_eval is not None and c.backed_eval != c.eval_cp:
                backed_plies = c.backed_plies
        else:
            score, rank = None, -9_999.5   # sin analizar: encima de perder
        if c.status in (win, loss):
            # distancia de mate: mate_in propagado por minimax; fallback a la
            # linea verificada. La jugada de la fila cuenta como primer ply.
            plies = None
            if c.mate_in is not None:
                plies = 1 + c.mate_in
            elif c.closure == 'TERMINAL':
                plies = 1
            elif c.closure == 'MATE_PV' and c.won_line:
                plies = 1 + len(c.won_line.split())
            if plies is not None:
                n = (plies + 1) // 2
                mate = n if c.status == win else -n
                # mates cortos primero al ganar; resistencia larga primero al perder
                rank += ((999 - min(n, 999)) if c.status == win
                         else min(n, 999)) * 1e-3
        moves.append({'uci': e.move_uci, 'key': c.key, 'status': c.status,
                      'closure': c.closure, 'score': score, 'rank': rank,
                      'mate': mate, 'point': point,
                      'backed_plies': backed_plies,
                      'backed': bool(backed_plies),
                      'mate_str': None if mate is None else
                      (f'≤M{mate}' if mate > 0 else f'-≤M{-mate}'),
                      'visits': c.visits, 'css': _move_css(c.status, score, win)})
    moves.sort(key=lambda m: -m['rank'])
    return moves


def home(request):
    total = Position.objects.count()
    closed = Position.objects.exclude(status='UNKNOWN').count()
    analyses = AnalysisTask.objects.filter(state='COMPLETED').count()
    requested = AnalysisTask.objects.filter(
        state='PENDING', source='USER', position__status='UNKNOWN').count()
    nodes = Position.objects.aggregate(n=Sum('nodes_invested'))['n'] or 0
    day_ago = timezone.now() - timedelta(hours=24)
    closed_24h = DBEvent.objects.filter(kind='NODE_CLOSED',
                                        ts__gte=day_ago).count()
    nodes_24h = AnalysisTask.objects.filter(
        state='COMPLETED', completed__gte=day_ago).aggregate(
        n=Sum('nodes_searched'))['n'] or 0
    root_key = logic.key_of(logic.start_fen())
    try:
        first_moves = _child_moves(Position.objects.get(key=root_key))
    except Position.DoesNotExist:
        first_moves = []
    solved_first = sum(1 for m in first_moves if m['status'] != 'UNKNOWN')
    solved_pct = round(100.0 * closed / total, 1) if total else 0.0
    now = timezone.now()
    live_cutoff = now - timedelta(seconds=180)
    live_pings = list(WorkerPing.objects.filter(last_seen__gte=live_cutoff)
                      .order_by('machine'))
    lease_cutoff = now - timedelta(minutes=LEASE_MINUTES)
    legacy_display_cutoff = now - timedelta(minutes=LEGACY_DISPLAY_MINUTES)
    candidate_leases = list(AnalysisTask.objects.filter(
        state='LEASED',
    ).filter(
        Q(lease_token='', leased_at__gte=legacy_display_cutoff)
        | Q(lease_token__gt='', lease_heartbeat_at__gte=lease_cutoff)
        | Q(lease_token__gt='', lease_heartbeat_at__isnull=True,
            leased_at__gte=lease_cutoff)
    ).select_related('position').order_by('machine', 'leased_at', 'id'))
    legacy_leases_by_machine = {}
    leases_by_id = {task.id: task for task in candidate_leases}
    for task in candidate_leases:
        if not task.lease_token:
            legacy_leases_by_machine.setdefault(task.machine, task)
    # Prefer the exact task from a fresh worker. Still-valid leases remain a
    # compatibility fallback for the previous worker build, which only touched
    # WorkerPing at lease/submit boundaries.
    leased = []
    selected_ids = set()
    selected_machines = set()
    for ping in live_pings:
        task = leases_by_id.get(ping.current_task_id)
        if task is None:
            # Old workers did not publish an exact task heartbeat. Modern
            # workers deliberately clear current_task_id when engine nodes
            # stop advancing, so falling back for tokenized leases would call
            # a hung engine "Now analyzing" for up to another hour.
            task = legacy_leases_by_machine.get(ping.machine)
        if task is not None:
            leased.append(task)
            selected_ids.add(task.id)
            selected_machines.add(task.machine)
    for task in candidate_leases:
        if task.lease_token:
            continue
        if task.id in selected_ids or task.machine in selected_machines:
            continue
        leased.append(task)
        selected_machines.add(task.machine)
    leased = leased[:5]
    leased_keys = {t.position_id for t in leased}
    upnext_positions = []
    for pos in Position.objects.filter(status='UNKNOWN',
                                       priority__gt=-1e8) \
                               .order_by('-priority')[:12]:
        if pos.key in leased_keys:
            continue
        upnext_positions.append(pos)
        if len(upnext_positions) >= 5:
            break
    event_rows = list(DBEvent.objects.order_by('-ts')[:12])
    event_keys = [(event.payload or {}).get('key', '') for event in event_rows]
    labels = _line_labels_many(
        [task.position_id for task in leased]
        + [pos.key for pos in upnext_positions] + event_keys)
    analyzing = []
    for task in leased:
        preview, full = labels.get(task.position_id, ('', ''))
        analyzing.append({'key': task.position_id,
                          'san': preview or 'start position',
                          'full': full or 'start position',
                          'budget': _human(task.budget_nodes),
                          'machine': task.machine})
    upnext = []
    for pos in upnext_positions:
        preview, full = labels.get(pos.key, ('', ''))
        upnext.append({'key': pos.key,
                       'san': preview or 'start position',
                       'full': full or 'start position'})
    events = _friendly_events(event_rows, labels)
    campaigns = Campaign.objects.order_by('-created')[:6]
    root = ingest.get_or_create_position(logic.start_fen())
    root_legal_ucis = logic.legal_moves(root.fen)
    compute = worker_metrics()
    return render(request, 'atomicdb/home.html', {
        'analyzing': analyzing, 'upnext': upnext,
        'total_h': _human(total), 'closed_h': _human(closed),
        'analyses_h': _human(analyses), 'nodes_h': _human(nodes),
        'requested_h': _human(requested),
        'workers_h': _human(compute['workers']),
        'cores_h': _human(compute['cores']),
        'compute_nps_h': _human(compute['nps']),
        'positions_pm_h': (f"{compute['positions_per_minute']:.1f}"
                           if compute['positions_per_minute'] < 100
                           else _human(round(compute['positions_per_minute']))),
        'solved_first': solved_first, 'n_first': len(first_moves),
        'solved_pct': solved_pct,
        'closed_24h_h': _human(closed_24h), 'nodes_24h_h': _human(nodes_24h),
        'first_moves': first_moves, 'events': events, 'campaigns': campaigns,
        'root_key': root_key, 'board': _ctx_board(root.fen),
        'board_fen': root.fen,
        'board_turn': 'white' if root.fen.split()[1] == 'w' else 'black',
        'board_key': root.key, 'best_move': root.best_move, 'board_play': '',
        'legal_ucis': root_legal_ucis,
        'legal_move_links': [
            {'uci': uci, 'url': _goto_url(root.key, uci, [])}
            for uci in root_legal_ucis
        ]})


FEN_SHAPE = re.compile(
    r'^[pnbrqkPNBRQK1-8]+(/[pnbrqkPNBRQK1-8]+){7} [wb]'
    r' (-|[KQkqA-Ha-h]+) (-|[a-h][36])( \d+ \d+)?$')


def _parse_public_fen(raw):
    """Validacion estricta de FEN publica ANTES de pasarla a pyffish."""
    raw = ' '.join(raw.replace('_', ' ').split())
    if len(raw) > 100 or not FEN_SHAPE.match(raw):
        raise ValueError('malformed fen')
    board = raw.split()[0]
    if board.count('K') != 1 or board.count('k') != 1:
        raise ValueError('need both kings')
    for rank in board.split('/'):
        if sum(int(c) if c.isdigit() else 1 for c in rank) != 8:
            raise ValueError('bad rank')
    if len(raw.split()) == 4:
        raw += ' 0 1'
    fen = logic.canonical_fen(raw)
    logic.legal_moves(fen)   # pyffish la acepta
    return fen


def api_query(request):
    """API publica de consulta por FEN. Scores en perspectiva del que mueve
    (convencion chessdb.cn); el arbol interno almacena White-POV."""
    try:
        fen = _parse_public_fen(request.GET.get('fen', ''))
    except Exception:
        return JsonResponse({'error': 'invalid fen'}, status=400)
    try:
        pos = Position.objects.get(key=logic.key_of(fen))
    except Position.DoesNotExist:
        return JsonResponse({'error': 'unknown position'}, status=404)
    stm_white = pos.fen.split()[1] == 'w'
    # ``score`` es el mejor conocimiento actual (respaldado por el subarbol);
    # ``point`` conserva la eval puntual cruda de esta misma posicion.
    known = ingest.best_known_eval(pos) if pos.status == 'UNKNOWN' \
        else pos.eval_cp
    score = None if known is None else (known if stm_white else -known)
    point = None if pos.eval_cp is None else (
        pos.eval_cp if stm_white else -pos.eval_cp)
    moves = [{'uci': m['uci'], 'status': m['status'], 'closure': m['closure'],
              'score': m['score'], 'point': m['point'], 'mate': m['mate'],
              'backed_plies': m['backed_plies'], 'visits': m['visits']}
             for m in _child_moves(pos)]
    return JsonResponse({
        'fen': pos.fen, 'key': pos.key, 'status': pos.status,
        'closure': pos.closure, 'score': score, 'point': point,
        'backed_plies': pos.backed_plies, 'best_move': pos.best_move,
        'tier': 'PRACTICAL', 'trust': _trust_for(pos),
        'history_scope': 'COUNTERS_AND_REPETITION_IGNORED',
        'visits': pos.visits, 'nodes': pos.nodes_invested, 'moves': moves})


def api_frontier(request, key):
    """What a click bought BELOW this position, for the explorer's poller.

    Once the ladder is spent the work no longer lands on the position the
    visitor clicked, so watching that row alone says nothing for six minutes.
    This reports the level underneath instead: visitor tasks running, waiting
    and finished on the direct children, plus how many of those children the
    tree has already closed.

    Deliberately two statements and no more.  The level is folded into ONE
    aggregate because a position can have sixty children and this is polled
    every ten seconds; a query per child would make the page cost grow with
    the width of the tree.
    """
    status = (Position.objects.filter(key=key)
              .values_list('status', flat=True).first())
    if status is None:
        return JsonResponse({'error': 'unknown position'}, status=404)
    requested = Q(child__analysistask__source=AnalysisTask.Source.USER)

    def tasks_in(state):
        return Count('child__analysistask',
                     filter=requested & Q(child__analysistask__state=state))

    level = Edge.objects.filter(parent_id=key).aggregate(
        children_total=Count('child', distinct=True),
        children_solved=Count('child', distinct=True,
                              filter=~Q(child__status='UNKNOWN')),
        running=tasks_in(AnalysisTask.TState.LEASED),
        queued=tasks_in(AnalysisTask.TState.PENDING),
        done=tasks_in(AnalysisTask.TState.COMPLETED))
    return JsonResponse({'key': key, 'status': status, **level})


def _trust_for(pos):
    if pos.closure in ('TERMINAL', 'TB'):
        return 'VERIFIED'
    if pos.proof:
        return pos.proof
    if pos.closure in ('MATE_PV', 'MINIMAX'):
        return 'UNCLASSIFIED'
    return 'UNPROVEN'


def fen_jump(request):
    """Cajetin de FEN: salta a la posicion; si no existe la crea, bajo el
    mismo rate-limit por IP que las peticiones de analisis."""
    if request.method != 'POST':
        return redirect('/atomicdb/')
    try:
        fen = _parse_public_fen(request.POST.get('fen', ''))
    except Exception:
        return render(request, 'atomicdb/missing.html', status=400)
    key = logic.key_of(fen)
    if not Position.objects.filter(key=key).exists():
        ip = _client_ip(request)
        hour_ago = timezone.now() - timedelta(hours=1)
        if RequestLog.objects.filter(ip=ip, created__gte=hour_ago) \
                             .count() >= REQUESTS_PER_IP_HOUR:
            return render(request, 'atomicdb/missing.html', status=429)
        pos = ingest.get_or_create_position(fen)
        RequestLog.objects.create(ip=ip, position=pos)
    return redirect(f'/atomicdb/explore/{key}/')


def _line_to_root(pos, max_plies=64):
    """Single-position wrapper around the shared cycle-safe reconstruction."""
    return _lines_to_root([pos.key], max_plies=max_plies)[pos.key]


def _numbered_line(top, line, route_ucis=None, opening_match=None,
                   route_ply=None):
    """Explorer breadcrumbs with route- or anchor-preserving destinations."""
    from_start = top.fen == logic.start_fen()
    numbered, n = [], 1
    for i, step in enumerate(line):
        if not from_start:
            number = ''
        elif step['white']:
            number = f'{n}.'
        else:
            number = f'{n}...' if i == 0 else ''
            n += 1
        token = {
            'num': number, 'san': step['san'], 'key': step['key'],
        }
        if route_ucis is not None:
            token['url'] = _explore_url(step['key'], route_ucis[:i + 1])
        elif opening_match is not None and route_ply is not None:
            breadcrumb_ply = route_ply - (len(line) - i - 1)
            anchor = _signed_opening_anchor(
                step['key'], opening_match, breadcrumb_ply)
            if anchor is not None:
                token['url'] = _explore_url(
                    step['key'], opening_anchor=anchor)
        numbered.append(token)
    return numbered


def _opening_for_template(match):
    """Add presentation-only URL safety without changing catalog records."""
    if match is None:
        return None
    result = dict(match)
    result['aliases'] = list(match.get('aliases', ()))
    result['sources'] = []
    provenance_labels = {
        'source_line_number': 'Source line',
        'source_row': 'Source row',
        'same_atomix_position': 'ATOMIX matches',
        'same_eao_position': 'EAO match',
    }

    def provenance_value(value):
        if isinstance(value, dict):
            label = value.get('name') or value.get('label') or value.get('id')
            qualifier = value.get('code') or (
                value.get('id') if value.get('id') != label else None)
            if label and qualifier:
                return f'{label} ({qualifier})'
            if label:
                return str(label)
            return ', '.join(
                f'{key}: {provenance_value(item)}'
                for key, item in value.items()
                if item not in (None, '', [], {})
            )
        if isinstance(value, list):
            return '; '.join(provenance_value(item) for item in value)
        if isinstance(value, bool):
            return 'yes' if value else 'no'
        return str(value)

    for raw_source in match.get('sources', ()):
        source = dict(raw_source)
        source['evidence'] = []
        for raw_evidence in raw_source.get('evidence', ()):
            evidence = dict(raw_evidence)
            try:
                parsed = urlsplit(str(evidence.get('url', '')))
                evidence['safe_url'] = (
                    evidence['url']
                    if parsed.scheme in ('http', 'https') and parsed.netloc
                    else ''
                )
            except (TypeError, ValueError):
                evidence['safe_url'] = ''
            source['evidence'].append(evidence)
        source['provenance_rows'] = []
        for field, value in raw_source.get('provenance', {}).items():
            if value is None or value == '' or value == [] or value == {}:
                continue
            source['provenance_rows'].append({
                'label': provenance_labels.get(
                    field, field.replace('_', ' ').title()),
                'value': provenance_value(value),
            })
        result['sources'].append(source)
    return result


def _exact_child_opening(child_key, current_opening):
    match = openings.lookup_key(child_key)
    # El catalogo auditado manda; donde calla, puede hablar un nombre
    # comunitario ya moderado.
    name = match['name'] if match is not None \
        else community_names.name_for(child_key)
    if name is None:
        return None
    if current_opening is not None and name == current_opening['name']:
        return None
    return name


# ---------------- nombres de apertura de la comunidad ----------------

SUGGESTION_MESSAGES = {
    'ok': 'Thanks: your name is queued for review.',
    'duplicate': 'You already have a pending suggestion for this position.',
    'rate-limited': 'Daily suggestion limit reached, try again tomorrow.',
    'invalid': 'A name is 2 to 60 characters of ordinary text.',
    'already-named': 'This position already has a catalogued opening name.',
}


def _suggestion_return_url(request, pos, outcome):
    """Vuelve a la MISMA pagina de exploracion, ruta incluida.

    La ruta se revalida entera (no se confia en lo que venga en el POST), asi
    que esto no puede convertirse en un redirect abierto ni materializar una
    linea nombrandola.
    """
    ucis, anchor = None, None
    try:
        route = _validated_play_route(request.POST.get('play') or None,
                                      pos.key)
    except PlayRouteError:
        route = None
    if route is not None:
        ucis = route[2]
    else:
        candidate = request.POST.get('opening') or ''
        match, _ply = _validated_opening_anchor(candidate, pos.key)
        if match is not None:
            anchor = candidate
    url = _explore_url(pos.key, ucis, anchor)
    return f"{url}{'&' if '?' in url else '?'}suggested={outcome}"


def suggest_opening_name(request, key):
    """Propuesta publica de nombre, sin cuenta.

    Anti-spam: una sola propuesta pendiente por IP y posicion, tope diario por
    IP, longitud y alfabeto validados, y nada de nombres para posiciones que
    el catalogo auditado ya nombra.
    """
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return render(request, 'atomicdb/missing.html', status=404)
    if request.method != 'POST':
        return redirect(_explore_url(pos.key))

    def back(outcome):
        return redirect(_suggestion_return_url(request, pos, outcome))

    if openings.lookup_key(pos.key) is not None:
        return back('already-named')
    name = ' '.join((request.POST.get('name') or '').split())
    comment = ' '.join((request.POST.get('comment') or '').split())
    if (len(name) < SUGGESTION_NAME_MIN_CHARS
            or len(name) > SUGGESTION_NAME_MAX_CHARS
            or not SUGGESTION_NAME_RE.fullmatch(name)
            or len(comment) > SUGGESTION_COMMENT_MAX_CHARS):
        return back('invalid')

    ip = _client_ip(request)
    if OpeningNameSuggestion.objects.filter(
            ip=ip, position=pos,
            status=OpeningNameSuggestion.SState.PENDING).exists():
        return back('duplicate')
    day_ago = timezone.now() - timedelta(days=1)
    if OpeningNameSuggestion.objects.filter(
            ip=ip, created__gte=day_ago).count() >= SUGGESTIONS_PER_IP_DAY:
        return back('rate-limited')
    OpeningNameSuggestion.objects.create(
        position=pos, proposed_name=name, comment=comment, ip=ip)
    return back('ok')


def _approver_gate(request):
    """Mismo criterio que /networks/: login, y Profile.approver."""
    # Import local: mantiene a AtomicDB importable sin arrastrar OpenBench.
    from OpenBench.models import Profile
    if not request.user.is_authenticated:
        return redirect('/login/')
    if not Profile.objects.filter(user=request.user, approver=True).exists():
        return redirect('/index/')
    return None


def suggestions(request):
    """Moderacion de nombres propuestos. Solo para approvers."""
    denied = _approver_gate(request)
    if denied is not None:
        return denied

    if request.method == 'POST':
        decisions = {'approve': OpeningNameSuggestion.SState.APPROVED,
                     'reject': OpeningNameSuggestion.SState.REJECTED}
        decision = decisions.get(request.POST.get('action', ''))
        try:
            suggestion_id = int(request.POST.get('suggestion', ''))
        except (TypeError, ValueError):
            suggestion_id = None
        if decision is not None and suggestion_id is not None:
            with atomic():
                row = (OpeningNameSuggestion.objects.select_for_update()
                       .filter(id=suggestion_id,
                               status=OpeningNameSuggestion.SState.PENDING)
                       .first())
                if row is not None:
                    # El catalogo auditado pudo nombrarla mientras esperaba.
                    if (decision == OpeningNameSuggestion.SState.APPROVED
                            and openings.lookup_key(row.position_id)
                            is not None):
                        decision = OpeningNameSuggestion.SState.REJECTED
                    row.status = decision
                    row.resolved_by = request.user.username[:64]
                    row.resolved_at = timezone.now()
                    row.save(update_fields=['status', 'resolved_by',
                                            'resolved_at'])
        community_names.invalidate()
        return redirect('/atomicdb/suggestions/')

    pending = list(OpeningNameSuggestion.objects
                   .filter(status=OpeningNameSuggestion.SState.PENDING)
                   .select_related('position')
                   .order_by('created')[:SUGGESTION_MODERATION_PAGE])
    resolved = list(OpeningNameSuggestion.objects
                    .exclude(status=OpeningNameSuggestion.SState.PENDING)
                    .select_related('position')
                    .order_by('-resolved_at')[:20])
    labels = _line_labels_many([row.position_id for row in pending + resolved])
    rows = []
    for row in pending:
        preview, full = labels.get(row.position_id, ('', ''))
        rows.append({'row': row, 'san': preview or 'start position',
                     'full': full or 'start position'})
    history = []
    for row in resolved:
        preview, full = labels.get(row.position_id, ('', ''))
        history.append({'row': row, 'san': preview or 'start position',
                        'full': full or 'start position'})
    return render(request, 'atomicdb/suggestions.html', {
        'pending': rows, 'history': history,
        'pending_total': OpeningNameSuggestion.objects.filter(
            status=OpeningNameSuggestion.SState.PENDING).count(),
    })


def explore(request, key):
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return render(request, 'atomicdb/missing.html', status=404)
    moves = _child_moves(pos)
    parents = [{'key': e.parent_id, 'uci': e.move_uci}
               for e in Edge.objects.filter(child=pos)[:8]]
    raw_play = request.GET.get('play')
    try:
        explicit_route = _validated_play_route(raw_play, pos.key)
    except PlayRouteError as exc:
        return _play_route_error_response(exc)
    if explicit_route is None:
        top, line, active_ucis = _canonical_route(pos)
    else:
        top, line, active_ucis = explicit_route
    # tambien en posiciones resueltas: se puede explorar la winning line
    legal_ucis = ([] if pos.closure == 'TERMINAL'
                  else logic.legal_moves(pos.fen))
    known = {m['uci'] for m in moves}
    current_opening_match, route_ply, active_ucis = _navigation_opening(
        active_ucis,
        request.GET.get(OPENING_ANCHOR_PARAM),
        pos.key,
        explicit_play=explicit_route is not None,
    )
    current_anchor = (
        None if active_ucis is not None
        else _signed_opening_anchor(
            pos.key, current_opening_match, route_ply)
    )
    current_opening = _opening_for_template(current_opening_match)
    # Un nombre comunitario aprobado solo puede aparecer donde el catalogo
    # auditado calla sobre ESTA posicion, y gana a un nombre heredado de un
    # ancestro. La navegacion (anclas firmadas) sigue anclada al catalogo.
    community = community_names.opening_for(pos.key, pos.fen)
    if community is not None:
        current_opening = _opening_for_template(community)

    for move in moves:
        child_route, child_anchor = _child_navigation_state(
            active_ucis, current_opening_match, route_ply,
            move['key'], move['uci'],
        )
        move['url'] = _explore_url(
            move['key'], child_route, child_anchor)
        move['enters_opening'] = _exact_child_opening(
            move['key'], current_opening)
    unexplored = []
    for uci in legal_ucis:
        if uci in known:
            continue
        child_fen = logic.apply_move(pos.fen, uci)
        child_key = logic.key_of(child_fen)
        unexplored.append({
            'uci': uci,
            'url': _goto_url(
                pos.key, uci, active_ucis, current_anchor),
            'enters_opening': _exact_child_opening(
                child_key, current_opening),
        })
    numbered = _numbered_line(
        top,
        line,
        active_ucis,
        current_opening_match if current_anchor else None,
        route_ply,
    )
    board_play = (
        ','.join(active_ucis)
        if active_ucis is not None
        else (
            OPENING_ANCHOR_PLAY_PREFIX + current_anchor
            if current_anchor else ''
        )
    )
    legal_move_links = [
        {
            'uci': uci,
            'url': _goto_url(
                pos.key, uci, active_ucis, current_anchor),
        }
        for uci in legal_ucis
    ]
    stm_white = pos.fen.split()[1] == 'w'
    win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    # Cabecera: el valor RESPALDADO (lo que el subarbol ya sabe) con caida
    # limpia a la eval puntual. Un solo flip a la perspectiva del que mueve.
    known = None if pos.status != 'UNKNOWN' else (
        pos.backed_eval if pos.backed_eval is not None else pos.eval_cp)
    eval_stm = None if known is None else (known if stm_white else -known)
    point_stm = None if pos.eval_cp is None else (
        pos.eval_cp if stm_white else -pos.eval_cp)
    eval_backed = (pos.backed_eval is not None
                   and pos.backed_eval != pos.eval_cp
                   and pos.status == 'UNKNOWN')
    return render(request, 'atomicdb/explore.html', {
        'pos': pos, 'moves': moves, 'parents': parents,
        'line': numbered, 'line_from_root': top.fen == logic.start_fen(),
        'active_play': active_ucis, 'board_play': board_play,
        'opening': current_opening,
        'board_key': pos.key, 'legal_ucis': legal_ucis,
        'legal_move_links': legal_move_links,
        'board_fen': pos.fen, 'board_turn': 'white' if stm_white else 'black',
        'board': _ctx_board(pos.fen),
        'best_move': (None if pos.closure == 'TERMINAL'
                      else pos.best_move),
        'stm': 'White' if stm_white else 'Black',
        'eval_stm_str': None if eval_stm is None else f'{eval_stm:+d}cp',
        'eval_backed': eval_backed,
        'eval_backed_plies': pos.backed_plies,
        'eval_point_str': None if point_stm is None else f'{point_stm:+d}cp',
        # Propuesta de nombre: solo donde el catalogo auditado no dice nada.
        'may_suggest_name': openings.lookup_key(pos.key) is None,
        'suggest_play': '' if active_ucis is None else ','.join(active_ucis),
        'suggest_anchor': current_anchor or '',
        'suggest_name_max': SUGGESTION_NAME_MAX_CHARS,
        'suggest_comment_max': SUGGESTION_COMMENT_MAX_CHARS,
        'suggest_message': SUGGESTION_MESSAGES.get(
            request.GET.get('suggested', '')),
        'suggest_ok': request.GET.get('suggested') == 'ok',
        'nodes_h': _human(pos.nodes_invested),
        'time_str': f'{pos.time_invested:,.1f}s',
        'unexplored': unexplored,
        # Spent ladder: a click here lands below, so the page shows the
        # cascade underneath from the first render, before any click.
        'frontier_exhausted': ingest.ladder_exhausted(pos),
        'verdict_mate': (f'≤M{(pos.mate_in + 1) // 2}'
                         if pos.status != 'UNKNOWN' and pos.mate_in
                         else None),
        'verdict_css': _move_css(pos.status, eval_stm, win)})


def method(request):
    return render(request, 'atomicdb/method.html')


def conquest_map(request):
    """Public shell for the snapshot-backed Atomic move tree.

    Rendering this page never walks or mutates the solver graph.  The
    versioned map endpoint supplies the bounded display-tree projection.
    """
    root_fen = logic.start_fen()
    return render(request, 'atomicdb/map.html', {
        'root_key': logic.key_of(root_fen),
        'root_fen': root_fen,
    })
