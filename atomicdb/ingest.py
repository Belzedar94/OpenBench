"""Ingesta y backup de AtomicDB. Unico punto de escritura del arbol.

Flujo por resultado de analisis (§2):
  upsert de hijos (expansion COMPLETA con movegen propio) -> cierres locales
  (terminal / MATE_PV) -> backup minimax en cascada hacia arriba -> eventos.
"""

import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import logic, tb
from .models import AnalysisTask, Campaign, DBEvent, Edge, Position

# Sondas profundas estilo chessdb.cn: sin TT persistente entre visitas, la
# profundidad se compra por sonda; evals fiables valen mas que anchura barata.
BUDGET_LADDER = [8_000_000, 32_000_000, 128_000_000, 512_000_000,
                 2_000_000_000]
# Visitor-requested reanalysis is deliberately steeper than autonomous tree
# exploration: 128M -> 512M -> 2B -> 10B, then stays at 10B.
REQUEST_BUDGET_LADDER = [128_000_000, 512_000_000, 2_000_000_000,
                         10_000_000_000]
MATE_BAND = 9_000   # |eval| >=: el motor ya vio mate; cerrar es cuestion de PV
CASCADE_GUARD_LIMIT = 100_000
PRIORITY_REFRESH_SECONDS = 30.0
# A legal terminal mate PV remains useful ENGINE evidence when the stronger
# exhaustive AND/OR certificate cannot be produced promptly.  This is one
# shared wall-clock allowance for the complete online submission.
ONLINE_MATE_PROOF_SECONDS = 20.0
_priority_refresh_cache = {'at': 0.0}


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


def prepare_mate_proofs(parent_fen, lines, budget_positions=200_000,
                        deadline_seconds=ONLINE_MATE_PROOF_SECONDS):
    """Run CPU-heavy mate checks before opening a write transaction.

    Deadline exhaustion degrades a legally verified terminal PV to ENGINE
    evidence (``INCONCLUSIVE``).  It must never manufacture either an ANDOR
    proof or a dispute.
    """
    deadline = (None if deadline_seconds is None else
                time.monotonic() + max(0.0, float(deadline_seconds)))
    legal = set(logic.legal_moves(parent_fen))
    prepared = {}
    candidates = []
    for index, line in enumerate(lines):
        move = line.get('move')
        pv = line.get('pv')
        mate = line.get('mate')
        if (mate is None or not isinstance(pv, list) or not pv
                or move not in legal or pv[0] != move):
            continue
        try:
            child_fen = logic.apply_move(parent_fen, move)
        except Exception:
            continue
        winner_white = mate > 0
        pv_rest = pv[1:]
        if not logic.verify_mate_pv(
                child_fen, pv_rest, winner_white, deadline=deadline):
            continue
        candidates.append((len(pv_rest), index, child_fen,
                           winner_white, pv_rest))

    # Certify shortest witnesses first while preserving original MultiPV
    # indexes for the ingestion map.
    for _, index, child_fen, winner_white, pv_rest in sorted(candidates):
        if deadline is not None and time.monotonic() >= deadline:
            proof_result = 'INCONCLUSIVE'
        else:
            proof_result = logic.prove_forced_mate(
                child_fen, winner_white, max_plies=len(pv_rest) + 2,
                budget_positions=budget_positions, hint_pv=pv_rest,
                deadline=deadline)
        prepared[index] = (winner_white, pv_rest, proof_result)
    return prepared


def ingest_analysis(position_key, lines, nodes_budget, machine='',
                    mate_proofs=None):
    """lines = [{'move': uci, 'eval_cp': int|None, 'mate': int|None,
                 'pv': [uci...]}] del MultiPV del motor (perspectiva blanca).
    Devuelve dict con resumen."""
    if mate_proofs is None:
        snapshot = Position.objects.only('fen', 'status').get(key=position_key)
        if snapshot.status != 'UNKNOWN':
            return {'skipped': 'already-closed'}
        mate_proofs = prepare_mate_proofs(snapshot.fen, lines)

    with transaction.atomic():
        pos = Position.objects.select_for_update().get(key=position_key)
        if pos.status != 'UNKNOWN':
            return {'skipped': 'already-closed'}

        expand(pos)
        stm_white = pos.fen.split()[1] == 'w'

        # evals de hijos reportados por el motor
        best_eval, best_move = None, None
        closed_here = 0
        for index, ln in enumerate(lines):
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
            prepared_proof = mate_proofs.get(index)
            if child.status == 'UNKNOWN' and prepared_proof is not None:
                winner_white, pv_rest, proof_result = prepared_proof
                if proof_result == 'NO_MATE':
                    child.proof = 'DISPUTED'
                    child.won_line = ' '.join(pv_rest)
                    child.save(update_fields=['proof', 'won_line', 'updated'])
                    DBEvent.objects.create(kind='MATE_PROOF_DISPUTED', payload={
                        'key': child.key, 'parent': pos.key,
                        'winner': 'WHITE' if winner_white else 'BLACK',
                        'max_plies': len(pv_rest) + 2,
                    })
                    _queue_disputed_reanalysis(child)
                    continue
                child.status = 'WHITE_WIN' if winner_white else 'BLACK_WIN'
                child.closure = 'MATE_PV'
                child.proof = ('ANDOR' if proof_result == 'PROVEN'
                               else 'ENGINE')
                child.won_line = ' '.join(pv_rest)
                child.mate_in = len(pv_rest)   # linea probada (cota superior)
                if pv_rest:
                    child.best_move = pv_rest[0]
                child.save(update_fields=['status', 'closure', 'proof',
                                          'won_line', 'mate_in',
                                          'best_move', 'updated'])
                _emit_closure_events(child)   # tambien cuenta y sale en feed
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
    while frontier and guard < CASCADE_GUARD_LIMIT:
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
                pos.proof = _minimax_proof(pos, edges, new_status)
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
            if pos.closure == 'MINIMAX' and pos.status != 'UNKNOWN':
                inherited = _minimax_proof(pos, edges, pos.status)
                if inherited != pos.proof:
                    pos.proof = inherited
                    dirty = True
            if pos.status in ('WHITE_WIN', 'BLACK_WIN'):
                # refinamiento retroactivo del DTM: si aparece (o se acorta)
                # una linea probada mas corta, la distancia y el testigo
                # mejoran y la mejora se propaga hacia arriba
                stm_white_pos = pos.fen.split()[1] == 'w'
                mover_win = 'WHITE_WIN' if stm_white_pos else 'BLACK_WIN'
                if pos.status == mover_win:
                    winners = [e for e in edges
                               if e.child.status == pos.status
                               and e.child.mate_in is not None]
                    if winners:
                        best = min(winners, key=lambda e: e.child.mate_in)
                        if (pos.mate_in is None
                                or 1 + best.child.mate_in < pos.mate_in):
                            pos.mate_in = 1 + best.child.mate_in
                            pos.best_move = best.move_uci
                            dirty = True
                else:
                    dists = [e.child.mate_in for e in edges]
                    if dists and all(d is not None for d in dists):
                        new_mate = 1 + max(dists)
                        if new_mate != pos.mate_in:
                            pos.mate_in = new_mate
                            dirty = True
            if new_eval is not None and new_eval != pos.eval_cp:
                pos.eval_cp = new_eval
                dirty = True
            if dirty:
                pos.save()
                changed_total += 1
                for e in Edge.objects.filter(child=pos).values_list(
                        'parent_id', flat=True):
                    frontier.add(e)
    if frontier:
        DBEvent.objects.create(kind='CASCADE_GUARD', payload={
            'iterations': guard, 'remaining': len(frontier),
            'seed_count': len(seed_keys),
        })
    return changed_total


def _status_eval(status):
    return {'WHITE_WIN': 10_000, 'BLACK_WIN': -10_000, 'DRAW': 0}.get(status)


def _child_is_verified(child):
    return (child.closure in ('TERMINAL', 'TB')
            or child.proof == 'ANDOR')


def _minimax_proof(pos, edges, status):
    """Inherit the weakest assurance needed by this exact backup shape."""
    if not edges or status == 'UNKNOWN':
        return None
    stm_white = pos.fen.split()[1] == 'w'
    mover_win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    if status == mover_win:
        witnesses = [e.child for e in edges if e.child.status == status]
        return ('ANDOR' if any(_child_is_verified(c) for c in witnesses)
                else 'ENGINE')
    # Losses and draws require complete coverage, so every child matters.
    return ('ANDOR' if all(_child_is_verified(e.child) for e in edges)
            else 'ENGINE')


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
    now = time.monotonic()
    cached = _priority_refresh_cache
    if cached['at'] and now - cached['at'] < PRIORITY_REFRESH_SECONDS:
        return False

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
    cached['at'] = now
    return True


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
    # The caller may hold a stale Position instance while another submit has
    # just advanced visits. Lock and refresh before choosing the generation so
    # a 512M/2B/10B request cannot accidentally target the completed rung.
    with transaction.atomic():
        pos = Position.objects.select_for_update().get(pk=pos.pk)
        if pos.status != 'UNKNOWN':
            return 'already-solved'
        completed_max = (AnalysisTask.objects.filter(
            position=pos, state=AnalysisTask.TState.COMPLETED)
            .order_by('-budget_nodes').values_list('budget_nodes', flat=True)
            .first())
        floor = REQUEST_BUDGET_LADDER[0]
        if completed_max is not None:
            floor = REQUEST_BUDGET_LADDER[-1]
            for candidate in REQUEST_BUDGET_LADDER:
                if candidate > completed_max:
                    floor = candidate
                    break
        floor = max(floor, budget_for(pos))
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
        elif task.state == 'LEASED' and task.budget_nodes < floor:
            # The running engine cannot change its ``go nodes`` command. Keep
            # the user's deeper request as the next generation instead of
            # logging it as satisfied and silently losing it for an hour.
            follow_up = (AnalysisTask.objects.filter(
                position=pos, state=AnalysisTask.TState.PENDING,
                generation__gt=task.generation)
                .order_by('generation').first())
            if follow_up is None:
                generation = max(pos.visits + 1, task.generation + 1)
                while AnalysisTask.objects.filter(
                        position=pos, generation=generation).exists():
                    generation += 1
                AnalysisTask.objects.create(
                    position=pos, generation=generation,
                    budget_nodes=floor, source='USER',
                    multipv=multipv_for(generation))
            else:
                follow_up.budget_nodes = max(follow_up.budget_nodes, floor)
                follow_up.source = 'USER'
                follow_up.save(update_fields=['budget_nodes', 'source'])
            return 'queued'
        return 'already-queued'


def _queue_disputed_reanalysis(pos):
    """Queue one maximum-budget follow-up without disturbing live leases."""
    pending = (AnalysisTask.objects.filter(position=pos, state='PENDING')
               .order_by('-generation').first())
    if pending is not None:
        pending.budget_nodes = max(pending.budget_nodes, BUDGET_LADDER[-1])
        pending.multipv = max(pending.multipv, multipv_for(pos.visits))
        pending.save(update_fields=['budget_nodes', 'multipv'])
        return pending

    generation = max(pos.visits, 0)
    used = set(AnalysisTask.objects.filter(position=pos)
               .values_list('generation', flat=True))
    while generation in used:
        generation += 1
    return AnalysisTask.objects.create(
        position=pos, generation=generation,
        budget_nodes=BUDGET_LADDER[-1],
        multipv=multipv_for(pos.visits), source='AUTO')


def _tb_rejected(position_key, reason, **payload):
    DBEvent.objects.create(kind='TB_REJECTED', payload={
        'key': position_key, 'reason': reason, **payload})


def prepare_tb_closure(position_key, wdl, user=None):
    """Validate and, for <=5 men, probe TB before any encompassing write tx."""
    try:
        pos = Position.objects.only('key', 'fen', 'status').get(
            key=position_key)
    except Position.DoesNotExist:
        return None
    if pos.status != 'UNKNOWN' or not logic.tb_applicable(pos.fen):
        return None
    try:
        wdl = int(wdl)
    except (TypeError, ValueError):
        wdl = 99
    if wdl not in (-2, -1, 0, 1, 2):
        _tb_rejected(pos.key, 'invalid-wdl', worker_wdl=wdl)
        return None

    men = logic.piece_count(pos.fen)
    if men <= 5:
        server_wdl = tb.probe_wdl(pos.fen, max_pieces=5)
        if server_wdl is None or int(server_wdl) != wdl:
            _tb_rejected(
                pos.key,
                'server-probe-unavailable' if server_wdl is None
                else 'wdl-mismatch',
                worker_wdl=wdl, server_wdl=server_wdl)
            return None
    else:
        trusted = set(getattr(settings, 'ATOMICDB_TB_TRUSTED', ()))
        username = getattr(user, 'username', '') if user is not None else ''
        if not (getattr(user, 'is_staff', False) or username in trusted):
            _tb_rejected(pos.key, 'untrusted-six-piece', worker_wdl=wdl,
                         username=username)
            return None
    return {'key': pos.key, 'fen': pos.fen, 'wdl': wdl}


def _apply_prepared_tb(position_key, prepared):
    """Apply a server-validated TB result inside the caller's transaction."""
    if prepared is None or prepared.get('key') != position_key:
        return False
    with transaction.atomic():
        pos = Position.objects.select_for_update().get(key=position_key)
        if (pos.status != 'UNKNOWN' or pos.fen != prepared.get('fen')
                or not logic.tb_applicable(pos.fen)):
            return False
        wdl = prepared['wdl']
        stm_white = pos.fen.split()[1] == 'w'
        pos.status = logic.wdl_to_status(wdl, stm_white)
        pos.closure = 'TB'
        pos.save(update_fields=['status', 'closure', 'updated'])
        DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': pos.key, 'status': pos.status, 'closure': 'TB'})
    backup_cascade([position_key])
    return True


def close_by_tb(position_key, wdl, user=None):
    """Cierra con WDL del lado al turno, dentro de una frontera verificable.

    Hasta cinco piezas el servidor repite siempre el probe con su set Atomic
    fijado. Las posiciones de seis piezas solo se aceptan de identidades
    explicitamente confiables porque el set completo no reside en el VPS.
    Todo rechazo queda registrado y nunca muta el arbol.
    """
    prepared = prepare_tb_closure(position_key, wdl, user=user)
    return _apply_prepared_tb(position_key, prepared)
