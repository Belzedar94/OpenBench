"""API de AtomicDB (worker) + paginas publicas del Explorer."""

import json
from datetime import timedelta

from django.contrib.auth import authenticate
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
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


UNICODE = {'K': '♔', 'Q': '♕', 'R': '♖', 'B': '♗',
           'N': '♘', 'P': '♙', 'k': '♚', 'q': '♛',
           'r': '♜', 'b': '♝', 'n': '♞', 'p': '♟'}


def _ctx_board(fen):
    rows = _board_rows(fen)
    return [[(UNICODE.get(p, ''), (r + c) % 2 == 1)
             for c, p in enumerate(row)] for r, row in enumerate(rows)]


def home(request):
    total = Position.objects.count()
    closed = Position.objects.exclude(status='UNKNOWN').count()
    walls = Position.objects.filter(is_wall=True).count()
    nodes = Position.objects.aggregate(n=__import__('django').db.models.Sum(
        'nodes_invested'))['n'] or 0
    root_key = logic.key_of(logic.start_fen())
    first_moves = []
    try:
        root = Position.objects.get(key=root_key)
        for e in Edge.objects.filter(parent=root).select_related('child'):
            c = e.child
            first_moves.append({
                'uci': e.move_uci, 'key': c.key, 'status': c.status,
                'eval': c.eval_cp,
                'css': _status_css(c.status, c.eval_cp)})
        first_moves.sort(key=lambda m: -(m['eval'] or 0))
    except Position.DoesNotExist:
        pass
    events = DBEvent.objects.order_by('-ts')[:12]
    campaigns = Campaign.objects.order_by('-created')[:6]
    return render(request, 'atomicdb/home.html', {
        'total': total, 'closed': closed, 'walls': walls, 'nodes': nodes,
        'first_moves': first_moves, 'events': events, 'campaigns': campaigns,
        'root_key': root_key})


def _status_css(status, eval_cp):
    if status == 'WHITE_WIN':
        return 'won'
    if status == 'BLACK_WIN':
        return 'lost'
    if status == 'DRAW':
        return 'draw'
    e = abs(eval_cp or 0)
    return 'hot' if e >= 500 else ('warm' if e >= 200 else 'cold')


def explore(request, key):
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return render(request, 'atomicdb/missing.html', status=404)
    edges = list(Edge.objects.filter(parent=pos).select_related('child'))
    moves = []
    for e in edges:
        c = e.child
        moves.append({'uci': e.move_uci, 'key': c.key, 'status': c.status,
                      'closure': c.closure, 'eval': c.eval_cp,
                      'visits': c.visits, 'css': _status_css(c.status, c.eval_cp)})
    moves.sort(key=lambda m: ({'WHITE_WIN': 0, 'UNKNOWN': 1, 'DRAW': 2,
                               'BLACK_WIN': 3}[m['status']],
                              -(m['eval'] or -99999)))
    parents = [{'key': e.parent_id, 'uci': e.move_uci}
               for e in Edge.objects.filter(child=pos)[:8]]
    return render(request, 'atomicdb/explore.html', {
        'pos': pos, 'moves': moves, 'parents': parents,
        'board': _ctx_board(pos.fen),
        'stm': 'blancas' if pos.fen.split()[1] == 'w' else 'negras',
        'verdict_css': _status_css(pos.status, pos.eval_cp)})


def walls(request):
    ws = Position.objects.filter(is_wall=True).order_by('-eval_cp')[:100]
    items = [{'pos': w, 'board': _ctx_board(w.fen)} for w in ws]
    return render(request, 'atomicdb/walls.html', {'items': items})


def method(request):
    return render(request, 'atomicdb/method.html')
