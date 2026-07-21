"""API de AtomicDB (worker) + paginas publicas del Explorer."""

import json
from datetime import timedelta

from django.contrib.auth import authenticate
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import ingest, logic
from .models import AnalysisTask, Campaign, DBEvent, Edge, Position

LEASE_MINUTES = 30
BATCH_SIZE = 25


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

    with transaction.atomic():
        # recuperar leases caducados
        stale = timezone.now() - timedelta(minutes=LEASE_MINUTES)
        AnalysisTask.objects.filter(state='LEASED', leased_at__lt=stale) \
                            .update(state='PENDING', machine='')
        batch = list(AnalysisTask.objects.select_for_update(skip_locked=True)
                     .filter(state='PENDING')
                     .order_by('-position__priority')[:BATCH_SIZE])
        if not batch:
            camp = Campaign.objects.filter(active=True).first()
            ingest.next_tasks(BATCH_SIZE, campaign=camp)
            batch = list(AnalysisTask.objects.select_for_update(skip_locked=True)
                         .filter(state='PENDING')
                         .order_by('-position__priority')[:BATCH_SIZE])
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
    return JsonResponse({'ok': True, 'summary': summary})


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
        elif e.kind == 'WALL':
            txt = f"Wall detected at {pl.get('eval', '?')}cp"
        elif e.kind == 'CAMPAIGN_CLOSED':
            txt = f"Campaign {pl.get('campaign', '?')} SOLVED: {pl.get('status', '?')}"
            key = ''
        else:
            txt = e.kind
        out.append({'ts': e.ts, 'text': txt, 'key': key, 'san': san})
    return out


def _child_moves(pos):
    """Tabla de movimientos hijos, formato compartido home/explorador."""
    moves = []
    for e in Edge.objects.filter(parent=pos).select_related('child'):
        c = e.child
        moves.append({'uci': e.move_uci, 'key': c.key, 'status': c.status,
                      'closure': c.closure, 'eval': c.eval_cp,
                      'visits': c.visits, 'css': _status_css(c.status, c.eval_cp)})
    moves.sort(key=lambda m: ({'WHITE_WIN': 0, 'UNKNOWN': 1, 'DRAW': 2,
                               'BLACK_WIN': 3}[m['status']],
                              -(m['eval'] or -99999)))
    return moves


def home(request):
    total = Position.objects.count()
    closed = Position.objects.exclude(status='UNKNOWN').count()
    walls = Position.objects.filter(is_wall=True).count()
    nodes = Position.objects.aggregate(n=__import__('django').db.models.Sum(
        'nodes_invested'))['n'] or 0
    root_key = logic.key_of(logic.start_fen())
    try:
        first_moves = _child_moves(Position.objects.get(key=root_key))
    except Position.DoesNotExist:
        first_moves = []
    events = _friendly_events(DBEvent.objects.order_by('-ts')[:12])
    campaigns = Campaign.objects.order_by('-created')[:6]
    root = ingest.get_or_create_position(logic.start_fen())
    return render(request, 'atomicdb/home.html', {
        'total': total, 'closed': closed, 'walls': walls, 'nodes': nodes,
        'first_moves': first_moves, 'events': events, 'campaigns': campaigns,
        'root_key': root_key, 'board': _ctx_board(root.fen),
        'board_key': root.key,
        'legal_ucis': logic.legal_moves(root.fen)})


def _status_css(status, eval_cp):
    if status == 'WHITE_WIN':
        return 'won'
    if status == 'BLACK_WIN':
        return 'lost'
    if status == 'DRAW':
        return 'draw'
    e = abs(eval_cp or 0)
    return 'hot' if e >= 500 else ('warm' if e >= 200 else 'cold')


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
    legal_ucis = logic.legal_moves(pos.fen) if pos.status == 'UNKNOWN' else []
    numbered, n = [], 1
    for i, st in enumerate(line):
        if st['white']:
            numbered.append({'num': f'{n}.', 'san': st['san'], 'key': st['key']})
        else:
            pre = f'{n}...' if i == 0 else ''
            numbered.append({'num': pre, 'san': st['san'], 'key': st['key']})
            n += 1
    return render(request, 'atomicdb/explore.html', {
        'pos': pos, 'moves': moves, 'parents': parents,
        'line': numbered, 'line_from_root': top.fen == logic.start_fen(),
        'board_key': pos.key, 'legal_ucis': legal_ucis,
        'board': _ctx_board(pos.fen),
        'stm': 'White' if pos.fen.split()[1] == 'w' else 'Black',
        'verdict_css': _status_css(pos.status, pos.eval_cp)})


def walls(request):
    ws = Position.objects.filter(is_wall=True).order_by('-eval_cp')[:100]
    items = [{'pos': w, 'board': _ctx_board(w.fen)} for w in ws]
    return render(request, 'atomicdb/walls.html', {'items': items})


def method(request):
    return render(request, 'atomicdb/method.html')
