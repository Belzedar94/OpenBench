"""API de AtomicDB (worker) + paginas publicas del Explorer."""

import json
import logging
import math
import secrets
import time
from datetime import timedelta

from django.contrib.auth import authenticate
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

import re

from django.db.models import F, Q, Sum

from . import ingest, logic
from .metrics import worker_metrics
from .models import (AnalysisTask, Campaign, DBEvent, Edge, Position,
                     RequestLog, WorkerPing)

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
REQUEST_QUEUE_MAX = 200
MAX_SUBMIT_LINES_BYTES = 512 * 1024
MAX_SUBMIT_PV_PLIES = 512

logger = logging.getLogger(__name__)


class _SubmitRejected(Exception):
    pass


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

    with transaction.atomic():
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

    machine = request.POST.get('machine', '')
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

    prepare_started = time.monotonic()
    tb_prepared = None
    mate_proofs = None
    if parsed_wdl is not None:
        tb_prepared = ingest.prepare_tb_closure(
            snapshot.position_id, parsed_wdl, user=user)
        if tb_prepared is None:
            return JsonResponse({'error': 'tb-rejected'}, status=409)
    else:
        mate_proofs = ingest.prepare_mate_proofs(snapshot.position.fen, lines)
    prepare_seconds = time.monotonic() - prepare_started

    transaction_started = time.monotonic()
    try:
        with transaction.atomic():
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
            if parsed_wdl is not None:
                closed = ingest._apply_prepared_tb(
                    task.position_id, tb_prepared)
                if not closed:
                    raise _SubmitRejected
                summary = {'tb_closed': True}
            else:
                summary = ingest.ingest_analysis(
                    task.position_id, lines, searched, machine=machine,
                    mate_proofs=mate_proofs)

            if elapsed:
                Position.objects.filter(key=task.position_id).update(
                    time_invested=F('time_invested') + elapsed)
            task.state, task.machine = 'COMPLETED', machine
            task.completed = timezone.now()
            task.nodes_searched = searched
            task.elapsed_seconds = elapsed
            task.save(update_fields=[
                'state', 'machine', 'completed', 'nodes_searched',
                'elapsed_seconds'])
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
    except _SubmitRejected:
        return JsonResponse({'error': 'tb-rejected'}, status=409)
    transaction_seconds = time.monotonic() - transaction_started
    total_seconds = time.monotonic() - submit_started
    logger.info(
        'AtomicDB submit task=%s machine=%s kind=%s lines=%s '
        'prepare_seconds=%.3f transaction_seconds=%.3f total_seconds=%.3f',
        task_id, machine, 'tb' if parsed_wdl is not None else 'engine',
        len(lines), prepare_seconds, transaction_seconds, total_seconds)
    summary['submit_timing_seconds'] = {
        'prepare': round(prepare_seconds, 3),
        'transaction': round(transaction_seconds, 3),
        'total': round(total_seconds, 3),
    }
    return JsonResponse({'ok': True, 'summary': summary})


@csrf_exempt
def api_request(request, key):
    """Peticion publica de analisis (estilo chessdb.cn), sin cuenta.
    Protecciones: rate-limit por IP, dedup ip+posicion, tope global de cola."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    # ultima entrada de XFF: la puso nuestro nginx, el cliente no puede falsearla
    ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[-1].strip()
          or request.META.get('REMOTE_ADDR', '0.0.0.0'))
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return JsonResponse({'status': 'unknown-position'}, status=404)
    hour_ago = timezone.now() - timedelta(hours=1)
    if RequestLog.objects.filter(ip=ip, created__gte=hour_ago,
                                 position=pos).exists():
        return JsonResponse({'status': 'already-requested'})
    if RequestLog.objects.filter(ip=ip, created__gte=hour_ago) \
                         .count() >= REQUESTS_PER_IP_HOUR:
        return JsonResponse({'status': 'rate-limited'}, status=429)
    if AnalysisTask.objects.filter(state='PENDING', source='USER',
                                   position__status='UNKNOWN') \
                           .count() >= REQUEST_QUEUE_MAX:
        return JsonResponse({'status': 'queue-full'}, status=503)
    outcome = ingest.request_analysis(pos)
    if outcome in ('queued', 'already-queued'):
        RequestLog.objects.create(ip=ip, position=pos)
    return JsonResponse({'status': outcome})


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


def goto(request, key, uci):
    """Navegacion jugando: valida la jugada, crea/encuentra el hijo y salta."""
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return redirect('/atomicdb/')
    if uci not in logic.legal_moves(pos.fen):
        return redirect(f'/atomicdb/explore/{key}/')
    child = ingest.get_or_create_position(logic.apply_move(pos.fen, uci),
                                          campaign=pos.campaign)
    Edge.objects.get_or_create(parent=pos, move_uci=uci,
                               defaults={'child': child})
    if child.priority <= ingest.DEAD / 2:
        child.priority = 0.0   # ruta nueva: revive de la lapida
        child.save(update_fields=['priority'])
    return redirect(f'/atomicdb/explore/{child.key}/')


def _format_san_line(top, line, max_plies=16, keep_head=False):
    """Linea SAN numerada hasta la raiz ("1. Nf3 f6 ..."). Al truncar,
    keep_head conserva el PRINCIPIO (para ver el opening); de lo contrario se
    conserva el final."""
    if not line:
        return ''
    parts, n = [], 1
    for i, st in enumerate(line):
        if st['white']:
            parts.append(f"{n}. {st['san']}")
        else:
            parts.append(f"{n}... {st['san']}" if i == 0 else st['san'])
            n += 1
    prefix = '' if top.fen == logic.start_fen() else '… '
    suffix = ''
    if len(parts) > max_plies:
        if keep_head:
            parts = parts[:max_plies]
            suffix = ' …'
        else:
            parts = parts[-max_plies:]
            prefix = '… '
    return prefix + ' '.join(parts) + suffix


def _lines_to_root(keys, max_plies=512):
    """Resolve many breadcrumbs together, batching one parent query per ply.

    The home page contains queue rows and milestones with heavily overlapping
    ancestry. Resolving each independently caused hundreds of small queries.
    """
    import pyffish as pf

    keys = list(dict.fromkeys(key for key in keys if key))
    positions = Position.objects.only('key', 'fen').in_bulk(keys)
    current = {key: positions[key] for key in keys if key in positions}
    active = set(current)
    seen = {key: {key} for key in current}
    steps = {key: [] for key in current}
    start_key = logic.key_of(logic.start_fen())

    for _ in range(max_plies):
        child_ids = {current[key].key for key in active
                     if current[key].key != start_key}
        if not child_ids:
            break
        first_parent = {}
        edges = (Edge.objects.filter(child_id__in=child_ids)
                 .select_related('parent')
                 .only('child_id', 'move_uci', 'parent__key', 'parent__fen')
                 .order_by('child_id', 'parent_id'))
        for edge in edges:
            first_parent.setdefault(edge.child_id, edge)
        next_active = set()
        for key in active:
            cur = current[key]
            if cur.key == start_key:
                continue
            edge = first_parent.get(cur.key)
            if edge is None or edge.parent_id in seen[key]:
                continue
            steps[key].append((edge.move_uci, cur.key))
            seen[key].add(edge.parent_id)
            current[key] = edge.parent
            next_active.add(key)
        active = next_active
        if not active:
            break

    result = {}
    for key, top in current.items():
        ordered = list(reversed(steps[key]))
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
        for (_uci, child_key), san in zip(ordered, sans):
            line.append({'san': san, 'key': child_key, 'white': white})
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


def _arrow(uci):
    """Coordenadas (en % del tablero) de la flecha del mejor movimiento,
    con la punta retraida para que la cabeza no invada la casilla."""
    if not uci or len(uci) < 4:
        return None
    try:
        x1 = (ord(uci[0]) - 96 - 0.5) * 12.5
        y1 = (8 - int(uci[1]) + 0.5) * 12.5
        x2 = (ord(uci[2]) - 96 - 0.5) * 12.5
        y2 = (8 - int(uci[3]) + 0.5) * 12.5
    except ValueError:
        return None
    dx, dy = x2 - x1, y2 - y1
    dist = (dx * dx + dy * dy) ** 0.5
    if dist > 4:
        x2 -= dx / dist * 3.2
        y2 -= dy / dist * 3.2
    return {'x1': f'{x1:.2f}', 'y1': f'{y1:.2f}',
            'x2': f'{x2:.2f}', 'y2': f'{y2:.2f}'}


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
    El almacenamiento interno sigue siendo White-POV; solo la vista voltea.
    Orden: victorias del que mueve, luego por score, derrotas al final."""
    stm_white = pos.fen.split()[1] == 'w'
    win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    loss = 'BLACK_WIN' if stm_white else 'WHITE_WIN'
    moves = []
    for e in Edge.objects.filter(parent=pos).select_related('child'):
        c = e.child
        mate = None
        if c.status == win:
            score, rank = 10_000, 10_001
        elif c.status == loss:
            score, rank = -10_000, -10_001
        elif c.status == 'DRAW':
            score, rank = 0, 0
        elif c.eval_cp is not None:
            score = c.eval_cp if stm_white else -c.eval_cp
            rank = score
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
                      'mate': mate,
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
        'board_key': root.key, 'arrow': _arrow(root.best_move),
        'legal_ucis': logic.legal_moves(root.fen)})


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
    score = None if pos.eval_cp is None else (
        pos.eval_cp if stm_white else -pos.eval_cp)
    moves = [{'uci': m['uci'], 'status': m['status'], 'closure': m['closure'],
              'score': m['score'], 'mate': m['mate'], 'visits': m['visits']}
             for m in _child_moves(pos)]
    return JsonResponse({
        'fen': pos.fen, 'key': pos.key, 'status': pos.status,
        'closure': pos.closure, 'score': score, 'best_move': pos.best_move,
        'tier': 'PRACTICAL', 'trust': _trust_for(pos),
        'history_scope': 'COUNTERS_AND_REPETITION_IGNORED',
        'visits': pos.visits, 'nodes': pos.nodes_invested, 'moves': moves})


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
        ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[-1].strip()
              or request.META.get('REMOTE_ADDR', '0.0.0.0'))
        hour_ago = timezone.now() - timedelta(hours=1)
        if RequestLog.objects.filter(ip=ip, created__gte=hour_ago) \
                             .count() >= REQUESTS_PER_IP_HOUR:
            return render(request, 'atomicdb/missing.html', status=429)
        pos = ingest.get_or_create_position(fen)
        RequestLog.objects.create(ip=ip, position=pos)
    return redirect(f'/atomicdb/explore/{key}/')


def _line_to_root(pos, max_plies=64):
    """Camino canonico (determinista) hacia arriba; con transposiciones se
    elige siempre el padre de key minima. Devuelve (top, [(san, child_key)...])
    en orden de juego, con SAN via pyffish desde el nodo superior. La posicion
    inicial es una frontera absoluta aunque un ciclo reversible haya creado
    aristas entrantes hacia ella."""
    import pyffish as pf
    steps = []
    cur, seen = pos, {pos.key}
    start_key = logic.key_of(logic.start_fen())
    while len(steps) < max_plies:
        if cur.key == start_key:
            break
        e = (Edge.objects.filter(child=cur).select_related('parent')
             .order_by('parent_id').first())
        if e is None or e.parent_id in seen:
            break
        steps.append((e.move_uci, cur.key))
        seen.add(e.parent_id)
        cur = e.parent
    steps.reverse()
    fen, out = cur.fen, []
    for uci, child_key in steps:
        try:
            san = pf.get_san('atomic', fen, uci)
        except Exception:
            san = uci
        out.append({'san': san, 'key': child_key,
                    'white': fen.split()[1] == 'w'})
        fen = logic.apply_move(fen, uci)
    return cur, out


def explore(request, key):
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return render(request, 'atomicdb/missing.html', status=404)
    moves = _child_moves(pos)
    parents = [{'key': e.parent_id, 'uci': e.move_uci}
               for e in Edge.objects.filter(child=pos)[:8]]
    top, line = _line_to_root(pos)
    # tambien en posiciones resueltas: se puede explorar la winning line
    legal_ucis = ([] if pos.closure == 'TERMINAL'
                  else logic.legal_moves(pos.fen))
    known = {m['uci'] for m in moves}
    unexplored = [u for u in legal_ucis if u not in known]
    numbered, n = [], 1
    for i, st in enumerate(line):
        if st['white']:
            numbered.append({'num': f'{n}.', 'san': st['san'], 'key': st['key']})
        else:
            pre = f'{n}...' if i == 0 else ''
            numbered.append({'num': pre, 'san': st['san'], 'key': st['key']})
            n += 1
    stm_white = pos.fen.split()[1] == 'w'
    win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    eval_stm = None if pos.eval_cp is None else (
        pos.eval_cp if stm_white else -pos.eval_cp)
    return render(request, 'atomicdb/explore.html', {
        'pos': pos, 'moves': moves, 'parents': parents,
        'line': numbered, 'line_from_root': top.fen == logic.start_fen(),
        'board_key': pos.key, 'legal_ucis': legal_ucis,
        'board_fen': pos.fen, 'board_turn': 'white' if stm_white else 'black',
        'board': _ctx_board(pos.fen),
        'arrow': None if pos.closure == 'TERMINAL' else _arrow(pos.best_move),
        'stm': 'White' if stm_white else 'Black',
        'eval_stm_str': None if eval_stm is None else f'{eval_stm:+d}cp',
        'nodes_h': _human(pos.nodes_invested),
        'time_str': f'{pos.time_invested:,.1f}s',
        'unexplored': unexplored,
        'verdict_mate': (f'≤M{(pos.mate_in + 1) // 2}'
                         if pos.status != 'UNKNOWN' and pos.mate_in
                         else None),
        'verdict_css': _move_css(pos.status, eval_stm, win)})


def method(request):
    return render(request, 'atomicdb/method.html')


def conquest_map(request):
    """Public shell for the snapshot-backed Conquest Map.

    Rendering this page never walks or mutates the solver graph.  The
    versioned map endpoint supplies the bounded display-tree projection.
    """
    root_fen = logic.start_fen()
    return render(request, 'atomicdb/map.html', {
        'root_key': logic.key_of(root_fen),
        'root_fen': root_fen,
    })
