"""Ingesta y backup de AtomicDB. Unico punto de escritura del arbol.

Flujo por resultado de analisis (§2):
  upsert de hijos (expansion COMPLETA con movegen propio) -> cierres locales
  (terminal / MATE_PV) -> backup minimax en cascada hacia arriba -> eventos.
"""

from django.db import transaction
from django.utils import timezone

from . import logic
from .models import AnalysisTask, Campaign, DBEvent, Edge, Position

WALL_VISITS = 5
BUDGET_LADDER = [100_000, 400_000, 1_600_000, 6_400_000, 25_600_000]


def get_or_create_position(fen, campaign=None):
    fen = logic.canonical_fen(fen)
    key = logic.key_of(fen)
    pos, created = Position.objects.get_or_create(
        key=key, defaults={'fen': fen, 'campaign': campaign})
    if created:
        t = logic.terminal_status(fen)
        if t:
            pos.status, pos.closure = t[0], 'TERMINAL'
            pos.save(update_fields=['status', 'closure'])
    return pos


def expand(pos):
    """Crea TODAS las aristas legales (movegen del ingestor, §3.3)."""
    if pos.expanded or pos.status != 'UNKNOWN':
        return []
    children = []
    for uci in logic.legal_moves(pos.fen):
        child_fen = logic.apply_move(pos.fen, uci)
        child = get_or_create_position(child_fen, campaign=pos.campaign)
        Edge.objects.get_or_create(parent=pos, move_uci=uci,
                                   defaults={'child': child})
        children.append(child)
    pos.expanded = True
    pos.save(update_fields=['expanded'])
    return children


def ingest_analysis(position_key, lines, nodes_budget, machine=''):
    """lines = [{'move': uci, 'eval_cp': int|None, 'mate': int|None,
                 'pv': [uci...]}] del MultiPV del motor (perspectiva blanca).
    Devuelve dict con resumen."""
    with transaction.atomic():
        pos = Position.objects.select_for_update().get(key=position_key)
        if pos.status != 'UNKNOWN':
            return {'skipped': 'already-closed'}

        expand(pos)
        stm_white = pos.fen.split()[1] == 'w'

        # evals de hijos reportados por el motor
        best_eval, best_move = None, None
        closed_here = 0
        for ln in lines:
            uci = ln['move']
            try:
                edge = Edge.objects.select_related('child').get(parent=pos, move_uci=uci)
            except Edge.DoesNotExist:
                continue  # el motor propuso algo que nuestro movegen no reconoce: fuera
            child = edge.child
            ev = ln.get('eval_cp')
            if ev is not None and child.status == 'UNKNOWN':
                if child.eval_cp is None or True:
                    child.eval_cp = ev
                    child.save(update_fields=['eval_cp', 'updated'])
            # cierre por mate verificado (§3.2)
            mate = ln.get('mate')
            if mate is not None and child.status == 'UNKNOWN' and ln.get('pv'):
                winner_white = (mate > 0) == stm_white
                pv_rest = ln['pv'][1:]  # pv[0] es `uci`: verificamos desde el hijo
                if logic.verify_mate_pv(child.fen, pv_rest, winner_white):
                    child.status = 'WHITE_WIN' if winner_white else 'BLACK_WIN'
                    child.closure = 'MATE_PV'
                    child.save(update_fields=['status', 'closure', 'updated'])
                    closed_here += 1
            if ev is not None and (best_eval is None
                                   or (stm_white and ev > best_eval)
                                   or (not stm_white and ev < best_eval)):
                best_eval, best_move = ev, uci

        pos.visits += 1
        pos.nodes_invested += nodes_budget
        if best_move:
            pos.best_move = best_move
        prev_eval = pos.eval_cp
        if best_eval is not None:
            pos.eval_cp = best_eval
        # muro (§4.3): escalera agotada sin progreso
        if (pos.visits >= WALL_VISITS and pos.status == 'UNKNOWN'
                and prev_eval is not None and best_eval is not None
                and abs(best_eval - prev_eval) < 30):
            pos.is_wall = True
            DBEvent.objects.create(kind='WALL', payload={
                'key': pos.key, 'fen': pos.fen, 'eval': best_eval})
        pos.save()

    changed = backup_cascade([pos.key])
    return {'closed_children': closed_here, 'backed_up': changed}


def backup_cascade(seed_keys):
    """Recalcula status/eval hacia arriba hasta punto fijo (§3.3)."""
    frontier = set(seed_keys)
    for pid in Edge.objects.filter(child_id__in=list(seed_keys)).values_list(
            'parent_id', flat=True):
        frontier.add(pid)
    changed_total = 0
    guard = 0
    while frontier and guard < 100_000:
        guard += 1
        key = frontier.pop()
        pos = Position.objects.get(key=key)
        edges = list(Edge.objects.filter(parent=pos).select_related('child'))
        if edges:
            statuses = [e.child.status for e in edges]
            evals = [e.child.eval_cp if e.child.status == 'UNKNOWN' else
                     _status_eval(e.child.status) for e in edges]
            new_status = (pos.status if pos.status != 'UNKNOWN' else
                          logic.backup_status(pos.fen, pos.expanded, statuses))
            new_eval = logic.backup_eval(pos.fen, evals)
            dirty = False
            if new_status != pos.status and pos.status == 'UNKNOWN':
                pos.status, pos.closure = new_status, 'MINIMAX'
                dirty = True
                _emit_closure_events(pos)
            if new_eval is not None and new_eval != pos.eval_cp:
                pos.eval_cp = new_eval
                dirty = True
            if dirty:
                pos.save()
                changed_total += 1
                for e in Edge.objects.filter(child=pos).values_list(
                        'parent_id', flat=True):
                    frontier.add(e)
    return changed_total


def _status_eval(status):
    return {'WHITE_WIN': 10_000, 'BLACK_WIN': -10_000, 'DRAW': 0}.get(status)


def _emit_closure_events(pos):
    DBEvent.objects.create(kind='NODE_CLOSED', payload={
        'key': pos.key, 'status': pos.status, 'closure': pos.closure})
    for camp in Campaign.objects.filter(root=pos, active=True):
        DBEvent.objects.create(kind='CAMPAIGN_CLOSED', payload={
            'campaign': camp.name, 'status': pos.status})
        camp.active = False
        camp.save(update_fields=['active'])


def refresh_priorities(campaign=None):
    """§4.1 — recalculo simple por lotes (llamado por el selector)."""
    qs = Position.objects.filter(status='UNKNOWN', is_wall=False)
    if campaign:
        qs = qs.filter(campaign=campaign)
    for pos in qs.iterator(chunk_size=2000):
        e = abs(pos.eval_cp) if pos.eval_cp is not None else 0
        prio = (min(e, 1500) / 100.0          # cercania al cierre
                + (2.0 if not pos.expanded else 0.0)
                - 1.5 * pos.visits)           # frescura
        if pos.priority != prio:
            pos.priority = prio
            pos.save(update_fields=['priority'])


def next_tasks(n, campaign=None):
    """Selector: crea/devuelve hasta n AnalysisTask PENDING."""
    refresh_priorities(campaign)
    qs = Position.objects.filter(status='UNKNOWN', is_wall=False)
    if campaign:
        qs = qs.filter(campaign=campaign)
    tasks = []
    for pos in qs.order_by('-priority')[:n]:
        gen = pos.visits
        budget = BUDGET_LADDER[min(gen, len(BUDGET_LADDER) - 1)]
        task, _ = AnalysisTask.objects.get_or_create(
            position=pos, generation=gen,
            defaults={'budget_nodes': budget})
        if task.state == 'PENDING':
            tasks.append(task)
    return tasks


def close_by_tb(position_key, wdl):
    """Cierra una posicion con el WDL probeado por un worker (perspectiva del
    que mueve). Verifica aplicabilidad server-side; el valor viene del worker
    (estandar practico, worker propio)."""
    with transaction.atomic():
        pos = Position.objects.select_for_update().get(key=position_key)
        if pos.status != 'UNKNOWN' or not logic.tb_applicable(pos.fen):
            return False
        stm_white = pos.fen.split()[1] == 'w'
        pos.status = logic.wdl_to_status(int(wdl), stm_white)
        pos.closure = 'TB'
        pos.save(update_fields=['status', 'closure', 'updated'])
        DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': pos.key, 'status': pos.status, 'closure': 'TB'})
    backup_cascade([position_key])
    return True
