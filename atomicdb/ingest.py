"""Ingesta y backup de AtomicDB. Unico punto de escritura del arbol.

Flujo por resultado de analisis (§2):
  upsert de hijos (expansion COMPLETA con movegen propio) -> cierres locales
  (terminal / MATE_PV) -> backup minimax en cascada hacia arriba -> eventos.
"""

from django.db import transaction
from django.utils import timezone

from . import logic
from .models import AnalysisTask, Campaign, DBEvent, Edge, Position

# Sondas profundas estilo chessdb.cn: sin TT persistente entre visitas, la
# profundidad se compra por sonda; evals fiables valen mas que anchura barata.
BUDGET_LADDER = [8_000_000, 32_000_000, 128_000_000, 512_000_000,
                 2_000_000_000]
MATE_BAND = 9_000   # |eval| >=: el motor ya vio mate; cerrar es cuestion de PV


def multipv_for(visits):
    """Anchura al sembrar (primeras visitas), profundidad en los peldanos
    altos: MultiPV 3 alli gana ~1-2 plies."""
    return 5 if visits < 3 else 3


def get_or_create_position(fen, campaign=None):
    fen = logic.canonical_fen(fen)
    key = logic.key_of(fen)
    pos, created = Position.objects.get_or_create(
        key=key, defaults={'fen': fen, 'campaign': campaign})
    if created:
        t = logic.terminal_status(fen)
        if t:
            pos.status, pos.closure, pos.mate_in = t[0], 'TERMINAL', 0
            pos.save(update_fields=['status', 'closure', 'mate_in'])
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
        if child.priority <= DEAD / 2:
            child.priority = 0.0   # ruta nueva via transposicion: revive
            child.save(update_fields=['priority'])
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
            # solo SIEMBRA hijos sin eval: el valor propio del hijo (analisis
            # directo o backup de su subarbol) es mas fiable que la linea
            # MultiPV del padre y no debe ser pisado
            if ev is not None and child.status == 'UNKNOWN' \
                    and child.eval_cp is None:
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
                    child.won_line = ' '.join(pv_rest)
                    child.mate_in = len(pv_rest)   # linea probada (cota superior)
                    if pv_rest:
                        child.best_move = pv_rest[0]
                    child.save(update_fields=['status', 'closure', 'won_line',
                                              'mate_in', 'best_move', 'updated'])
                    closed_here += 1
            if ev is not None and (best_eval is None
                                   or (stm_white and ev > best_eval)
                                   or (not stm_white and ev < best_eval)):
                best_eval, best_move = ev, uci

        pos.visits += 1
        pos.nodes_invested += nodes_budget
        pos.last_analysis = lines[:8]
        if best_move:
            pos.best_move = best_move
        if best_eval is not None:
            pos.eval_cp = best_eval
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
            # minimax de evals SOLO con lista de movimientos completa: sobre
            # una expansion parcial (aristas de /goto/) el min/max es basura
            # optimista (p.ej. un unico hijo perdido pondria 10000)
            new_eval = (logic.backup_eval(pos.fen, evals)
                        if pos.expanded else None)
            dirty = False
            if new_status != pos.status and pos.status == 'UNKNOWN':
                pos.status, pos.closure = new_status, 'MINIMAX'
                # testigo del minimax: la arista hacia el mejor hijo exacto
                want = new_status
                witness = next((e for e in edges if e.child.status == want),
                               None) or next(
                    (e for e in edges if e.child.status != 'UNKNOWN'), None)
                if witness:
                    pos.best_move = witness.move_uci
                # DTM practico: min para el ganador, max para el perdedor;
                # un hijo sin distancia (cierre TB) la deja en desconocida
                stm_white_pos = pos.fen.split()[1] == 'w'
                mover_win = 'WHITE_WIN' if stm_white_pos else 'BLACK_WIN'
                if new_status == mover_win:
                    winners = [e for e in edges if e.child.status == new_status
                               and e.child.mate_in is not None]
                    if winners:
                        best = min(winners, key=lambda e: e.child.mate_in)
                        pos.mate_in = 1 + best.child.mate_in
                        pos.best_move = best.move_uci  # el mate probado mas corto
                elif new_status != 'DRAW':
                    dists = [e.child.mate_in for e in edges]
                    if all(d is not None for d in dists):
                        pos.mate_in = 1 + max(dists)
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


DEAD = -1e9   # lapida: rama muerta, fuera de la cola para siempre
REGRET_WEIGHT = 3.0      # unidades de prioridad por cada 100cp de regret
DISCONNECTED_REGRET = 5  # posiciones sin camino a la raiz (cajetin FEN)


def _regret_from_root():
    """Descenso por variante principal (estilo chessdb.cn): regret(pos) =
    suma, a lo largo del mejor camino desde la raiz, de cuanto peor es cada
    jugada respecto a la mejor alternativa del minimax en ese punto. Bajo la
    linea principal ~0; bajo un opening refutado, todo el subarbol carga con
    la diferencia. Dijkstra sobre el DAG (gaps >= 0, transposiciones toman
    la mejor ruta). Hijos sin eval heredan el regret del padre (optimismo)."""
    import heapq

    val, white_stm = {}, {}
    for key, fen, eval_cp, status in Position.objects.values_list(
            'key', 'fen', 'eval_cp', 'status'):
        val[key] = {'WHITE_WIN': 10_000, 'BLACK_WIN': -10_000,
                    'DRAW': 0}.get(status, eval_cp)
        white_stm[key] = fen.split()[1] == 'w'
    children = {}
    for pid, cid in Edge.objects.values_list('parent_id', 'child_id'):
        children.setdefault(pid, []).append(cid)

    INF = float('inf')
    regret = dict.fromkeys(val, INF)
    root_key = logic.key_of(logic.start_fen())
    if root_key in regret:
        regret[root_key] = 0.0
    heap = [(0.0, root_key)]
    while heap:
        r, k = heapq.heappop(heap)
        if r > regret.get(k, INF):
            continue
        kids = children.get(k, ())
        if not kids:
            continue
        known = [val[c] for c in kids if val.get(c) is not None]
        best = (max(known) if white_stm[k] else min(known)) if known else None
        for c in kids:
            v = val.get(c)
            gap = 0.0
            if v is not None and best is not None:
                gap = (best - v) if white_stm[k] else (v - best)
            nr = r + gap
            if nr < regret.get(c, INF):
                regret[c] = nr
                heapq.heappush(heap, (nr, c))
    return regret


def refresh_priorities():
    """§4.1 — recalculo global (llamado por el selector). Prioridad =
    cercania al cierre local - regret acumulado desde la raiz - visitas.
    Respeta las lapidas (las ramas muertas no resucitan)."""
    regret = _regret_from_root()
    dirty = []
    for pos in Position.objects.filter(status='UNKNOWN',
                                       priority__gt=DEAD / 2) \
                               .iterator(chunk_size=2000):
        e = abs(pos.eval_cp) if pos.eval_cp is not None else 0
        r = regret.get(pos.key, float('inf'))
        runits = DISCONNECTED_REGRET if r == float('inf') \
            else min(r, 3000) / 100.0
        prio = (min(e, 1500) / 100.0          # cercania al cierre
                + (50.0 if e >= MATE_BAND else 0.0)  # mate visto: rematar
                + (2.0 if not pos.expanded else 0.0)
                - REGRET_WEIGHT * runits      # relevancia hacia la raiz
                - 1.5 * pos.visits)           # frescura
        if pos.priority != prio:
            pos.priority = prio
            dirty.append(pos)
    for i in range(0, len(dirty), 500):
        Position.objects.bulk_update(dirty[i:i + 500], ['priority'])


def _still_reachable(pos):
    """Con todos los padres cerrados, analizarlo ya no influye arriba."""
    parents = Edge.objects.filter(child=pos)
    if not parents.exists():
        return True   # raiz o semilla sin padres
    return parents.filter(parent__status='UNKNOWN').exists()


def budget_for(pos):
    """Escalera por visita + salto directo si el motor ya vio mate."""
    budget = BUDGET_LADDER[min(pos.visits, len(BUDGET_LADDER) - 1)]
    if abs(pos.eval_cp or 0) >= MATE_BAND:
        budget = max(budget, BUDGET_LADDER[2])  # extraer la PV entera
    return budget


def next_tasks(n):
    """Selector global best-first sobre todo el arbol (sin campanas)."""
    refresh_priorities()
    tasks = []
    for pos in Position.objects.filter(status='UNKNOWN') \
                               .order_by('-priority')[:4 * n]:
        if len(tasks) >= n:
            break
        if not _still_reachable(pos):
            pos.priority = DEAD   # lapida (refresh_priorities la respeta)
            pos.save(update_fields=['priority'])
            continue
        task, _ = AnalysisTask.objects.get_or_create(
            position=pos, generation=pos.visits,
            defaults={'budget_nodes': budget_for(pos),
                      'multipv': multipv_for(pos.visits)})
        if task.state == 'PENDING':
            tasks.append(task)
    return tasks


def bootstrap_root(budget=None):
    """Base inicial solida: una pasada profunda (512M por defecto) para CADA
    movimiento desde startpos, servida antes que el selector (source USER)."""
    budget = budget or BUDGET_LADDER[-1]
    root = get_or_create_position(logic.start_fen())
    expand(root)
    made = 0
    for e in Edge.objects.filter(parent=root).select_related('child'):
        c = e.child
        if c.status != 'UNKNOWN':
            continue
        task = AnalysisTask.objects.filter(position=c, state='PENDING').first()
        if task:
            task.budget_nodes = max(task.budget_nodes, budget)
            task.source = 'USER'
            task.save(update_fields=['budget_nodes', 'source'])
        else:
            gen = c.visits
            while AnalysisTask.objects.filter(position=c,
                                              generation=gen).exists():
                gen += 1
            AnalysisTask.objects.create(position=c, generation=gen,
                                        budget_nodes=budget, source='USER')
        made += 1
    return made


def request_analysis(pos):
    """Peticion publica: encola (o promociona) la tarea de esta posicion.
    Suelo de 128M: quien pide analisis merece profundidad de verdad.
    Devuelve 'queued' | 'already-queued' | 'already-solved'."""
    if pos.status != 'UNKNOWN':
        return 'already-solved'
    floor = max(budget_for(pos), BUDGET_LADDER[2])
    task, created = AnalysisTask.objects.get_or_create(
        position=pos, generation=pos.visits,
        defaults={'budget_nodes': floor, 'source': 'USER',
                  'multipv': multipv_for(pos.visits)})
    if created:
        return 'queued'
    if task.state == 'PENDING':
        task.budget_nodes = max(task.budget_nodes, floor)
        promoted = task.source != 'USER'
        task.source = 'USER'   # promocion: al frente de la cola
        task.save(update_fields=['source', 'budget_nodes'])
        if promoted:
            return 'queued'
    return 'already-queued'


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
