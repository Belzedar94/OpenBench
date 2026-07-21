"""API de AtomicDB (worker) + paginas publicas del Explorer."""

import json
from datetime import timedelta

from django.contrib.auth import authenticate
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

import re

from django.db.models import F, Sum

from . import ingest, logic
from .models import (AnalysisTask, Campaign, DBEvent, Edge, Position,
                     RequestLog, WorkerPing)

LEASE_MINUTES = 30
BATCH_SIZE = 25
REQUESTS_PER_IP_HOUR = 30
REQUEST_QUEUE_MAX = 200


def _auth(request):
    user = authenticate(username=request.POST.get('username', ''),
                        password=request.POST.get('password', ''))
    return user


# ---------------- API worker ----------------

@csrf_exempt
def api_lease(request):
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    machine = request.POST.get('machine', user.username)
    ping, _ = WorkerPing.objects.get_or_create(machine=machine,
                                               user=user.username)
    ping.threads = int(request.POST.get('threads', 0) or 0)
    ping.hash_mb = int(request.POST.get('hash', 0) or 0)
    ping.os = request.POST.get('os', '')[:64]
    ping.save()   # auto_now refresca last_seen

    with transaction.atomic():
        # recuperar leases caducados
        stale = timezone.now() - timedelta(minutes=LEASE_MINUTES)
        AnalysisTask.objects.filter(state='LEASED', leased_at__lt=stale) \
                            .update(state='PENDING', machine='')
        # '-source': USER > AUTO alfabeticamente, las peticiones van primero
        batch = list(AnalysisTask.objects.select_for_update(skip_locked=True)
                     .filter(state='PENDING')
                     .order_by('-source', '-position__priority')[:BATCH_SIZE])
        if not batch:
            ingest.next_tasks(BATCH_SIZE)
            batch = list(AnalysisTask.objects.select_for_update(skip_locked=True)
                         .filter(state='PENDING')
                         .order_by('-source', '-position__priority')[:BATCH_SIZE])
        for t in batch:
            t.state, t.machine, t.leased_at = 'LEASED', machine, timezone.now()
            t.attempts += 1
            t.save(update_fields=['state', 'machine', 'leased_at', 'attempts'])

    return JsonResponse({'tasks': [
        {'id': t.id, 'fen': t.position.fen, 'budget_nodes': t.budget_nodes,
         'multipv': t.multipv} for t in batch]})


@csrf_exempt
def api_submit(request):
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    try:
        task = AnalysisTask.objects.get(id=int(request.POST['task_id']))
        lines = json.loads(request.POST['lines'])
        assert isinstance(lines, list) and len(lines) <= 32
    except Exception as e:
        return JsonResponse({'error': f'malformed: {e}'}, status=400)
    if task.state == 'COMPLETED':
        return JsonResponse({'ok': True, 'dup': True})

    try:
        elapsed = min(max(float(request.POST.get('elapsed', 0) or 0), 0.0),
                      86_400.0)
    except ValueError:
        elapsed = 0.0
    if elapsed:
        Position.objects.filter(key=task.position_id).update(
            time_invested=F('time_invested') + elapsed)

    tb_wdl = request.POST.get('tb_wdl')
    if tb_wdl not in (None, ''):
        closed = ingest.close_by_tb(task.position_id, int(tb_wdl))
        task.state, task.completed = 'COMPLETED', timezone.now()
        task.save(update_fields=['state', 'completed'])
        return JsonResponse({'ok': True, 'summary': {'tb_closed': closed}})

    summary = ingest.ingest_analysis(task.position_id, lines,
                                     task.budget_nodes,
                                     machine=request.POST.get('machine', ''))
    task.state, task.completed = 'COMPLETED', timezone.now()
    task.save(update_fields=['state', 'completed'])
    WorkerPing.objects.filter(machine=request.POST.get('machine', ''),
                              user=user.username).update(
        tasks_done=F('tasks_done') + 1, last_seen=timezone.now())
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
    if AnalysisTask.objects.filter(state='PENDING', source='USER') \
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
    return redirect(f'/atomicdb/explore/{child.key}/')


def _san_line(key, max_plies=16):
    """Linea SAN numerada hasta la raiz para milestones ("1. Nf3 f6 ...")."""
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return ''
    top, line = _line_to_root(pos)
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
    if len(parts) > max_plies:
        parts = parts[-max_plies:]
        prefix = '… '
    return prefix + ' '.join(parts)


def _friendly_events(events):
    out = []
    for e in events:
        pl = e.payload or {}
        key = pl.get('key', '')
        san = _san_line(key) if key else ''
        if e.kind == 'NODE_CLOSED':
            txt = f"Solved: {pl.get('status', '?')} via {pl.get('closure', '?')}"
        elif e.kind == 'CAMPAIGN_CLOSED':
            txt = f"Campaign {pl.get('campaign', '?')} SOLVED: {pl.get('status', '?')}"
            key = ''
        else:
            txt = e.kind
        out.append({'ts': e.ts, 'text': txt, 'key': key, 'san': san})
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
                      (f'M{mate}' if mate > 0 else f'-M{-mate}'),
                      'visits': c.visits, 'css': _move_css(c.status, score, win)})
    moves.sort(key=lambda m: -m['rank'])
    return moves


def home(request):
    total = Position.objects.count()
    closed = Position.objects.exclude(status='UNKNOWN').count()
    analyses = AnalysisTask.objects.filter(state='COMPLETED').count()
    requested = AnalysisTask.objects.filter(state='PENDING',
                                            source='USER').count()
    nodes = Position.objects.aggregate(n=Sum('nodes_invested'))['n'] or 0
    day_ago = timezone.now() - timedelta(hours=24)
    closed_24h = DBEvent.objects.filter(kind='NODE_CLOSED',
                                        ts__gte=day_ago).count()
    nodes_24h = AnalysisTask.objects.filter(
        state='COMPLETED', completed__gte=day_ago).aggregate(
        n=Sum('budget_nodes'))['n'] or 0
    root_key = logic.key_of(logic.start_fen())
    try:
        first_moves = _child_moves(Position.objects.get(key=root_key))
    except Position.DoesNotExist:
        first_moves = []
    solved_first = sum(1 for m in first_moves if m['status'] != 'UNKNOWN')
    solved_pct = round(100.0 * closed / total, 1) if total else 0.0
    events = _friendly_events(DBEvent.objects.order_by('-ts')[:12])
    campaigns = Campaign.objects.order_by('-created')[:6]
    root = ingest.get_or_create_position(logic.start_fen())
    return render(request, 'atomicdb/home.html', {
        'total_h': _human(total), 'closed_h': _human(closed),
        'analyses_h': _human(analyses), 'nodes_h': _human(nodes),
        'requested_h': _human(requested),
        'solved_first': solved_first, 'n_first': len(first_moves),
        'solved_pct': solved_pct,
        'closed_24h_h': _human(closed_24h), 'nodes_24h_h': _human(nodes_24h),
        'first_moves': first_moves, 'events': events, 'campaigns': campaigns,
        'root_key': root_key, 'board': _ctx_board(root.fen),
        'board_key': root.key,
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
        'visits': pos.visits, 'nodes': pos.nodes_invested, 'moves': moves})


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
    en orden de juego, con SAN via pyffish desde el nodo superior."""
    import pyffish as pf
    steps = []
    cur, seen = pos, {pos.key}
    while len(steps) < max_plies:
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
        'board': _ctx_board(pos.fen),
        'stm': 'White' if stm_white else 'Black',
        'eval_stm_str': None if eval_stm is None else f'{eval_stm:+d}cp',
        'nodes_h': _human(pos.nodes_invested),
        'time_str': f'{pos.time_invested:,.1f}s',
        'unexplored': unexplored,
        'verdict_mate': (f'M{(pos.mate_in + 1) // 2}'
                         if pos.status != 'UNKNOWN' and pos.mate_in
                         else None),
        'verdict_css': _move_css(pos.status, eval_stm, win)})


def method(request):
    return render(request, 'atomicdb/method.html')
