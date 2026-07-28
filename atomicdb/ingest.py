"""Ingesta y backup de AtomicDB. Unico punto de escritura del arbol.

Flujo por resultado de analisis (§2):
  upsert de hijos (expansion COMPLETA con movegen propio) -> cierres locales
  (terminal / MATE_PV) -> backup minimax en cascada hacia arriba -> eventos.
"""

import time

from django.conf import settings
from django.db.models import Max, Q
from django.utils import timezone

from . import logic, proof, tb
from .database import atomic
from .models import AnalysisTask, Campaign, DBEvent, Edge, Position

# Sondas profundas estilo chessdb.cn: sin TT persistente entre visitas, la
# profundidad se compra por sonda; evals fiables valen mas que anchura barata.
BUDGET_LADDER = [8_000_000, 32_000_000, 128_000_000, 512_000_000,
                 2_000_000_000]
# Visitor-requested reanalysis is deliberately steeper than autonomous tree
# exploration: 128M -> 512M -> 2B -> 10B.
REQUEST_BUDGET_LADDER = [128_000_000, 512_000_000, 2_000_000_000,
                         10_000_000_000]
# Once the last rung is spent, buying it again would only repeat a search we
# already have.  The request then becomes a proof-number style expansion of
# the frontier one ply below: an OR node (the attacker of the conjecture,
# White, to move) only needs ONE good try, so a narrow top-k is enough; an
# AND node (Black to move) has to answer EVERY reply, so it takes them all.
FRONTIER_OR_WIDTH = 3
FRONTIER_AND_CAP = 64
FRONTIER_BLIND_WIDTH = 8   # no ordering information at all: widen a little
FRONTIER_CLICK_CAP = 64    # hard ceiling of tasks queued by a single click
# When that frontier is itself spent, the click descends instead of giving
# up: proof-number search never abandons a node whose children are all
# searched, it walks to the most-proving one and grows the tree there.  The
# DAG transposes and can even close a cycle (1.Nf3 Nf6 2.Ng1 Ng8 IS the start
# position once the counters are stripped), so the descent carries a visited
# set and this hard ply guard-rail.
FRONTIER_DESCENT_MAX_PLIES = 32
MATE_BAND = 9_000   # |eval| >=: el motor ya vio mate; cerrar es cuestion de PV
# Tope de la PV que se guarda por linea.  ``last_analysis`` es un JSON por
# POSICION, asi que su tamano medio se multiplica por el numero de posiciones:
# a 1 KB por fila son 45 GB a 45M posiciones, mas que la tabla de posiciones
# entera.  Los plies 25 en adelante de una PV no-mate no deciden nada — ni
# ordenan la frontera ni sostienen un cierre — asi que se tiran.
#
# Las lineas de MATE se guardan ENTERAS y a proposito: son evidencia.  El
# testigo de un cierre MATE_PV se re-verifica jugada a jugada, y una PV de
# mate truncada dejaria de serlo.
STORED_PV_MAX_PLIES = 24
CASCADE_GUARD_LIMIT = 100_000
PRIORITY_REFRESH_SECONDS = 30.0
# A legal terminal mate PV remains useful ENGINE evidence when the stronger
# exhaustive AND/OR certificate cannot be produced promptly.  This is one
# shared wall-clock allowance for the complete online submission.
#
# 5s, no 20 (28-jul): con el backlog post-Postgres los applies eran CPU-bound
# en estas pruebas online y la cola se comia la latencia; la flota F0
# re-certifica en diferido todo lo que aqui se degrade a ENGINE, asi que el
# tiempo online solo compra lo barato.
ONLINE_MATE_PROOF_SECONDS = 5.0
_priority_refresh_cache = {'at': 0.0}


def capped_analysis(lines, max_plies=STORED_PV_MAX_PLIES):
    """Lo que se GUARDA de un analisis: PVs no-mate recortadas.

    No toca las lineas que el ingest ya uso para decidir nada — el recorte
    ocurre al persistir, no al razonar — y deja intactas las PVs con score de
    mate, que son evidencia y se re-verifican entera.
    """
    stored = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        pv = line.get('pv')
        if (line.get('mate') is None and isinstance(pv, list)
                and len(pv) > max_plies):
            line = dict(line, pv=pv[:max_plies], pv_truncated=True)
        stored.append(line)
    return stored


# A partir de este presupuesto, los nodos se gastan en PROFUNDIDAD.
DEPTH_BUDGET_THRESHOLD = 512_000_000
DEPTH_MULTIPV = 2


def multipv_for(visits, budget_nodes=None, seeding=False):
    """Cuantas variantes pedirle al motor, segun para que es este analisis.

    Medido (caso de la comunidad, 28-jul): con el MISMO presupuesto de nodos,
    MultiPV 5 llego a profundidad 18 y dijo "negras aguantan" (-89); MultiPV 1
    llego a 23 y dijo "negras perdidas" (-901).  A partir de cierto peldano la
    anchura no compra ordenacion, compra ruido caro.

    * SEMBRANDO (``bootstrap_root``): 5.  Ahi la anchura ES el producto — se
      esta ordenando un nivel entero por primera vez.
    * PRESUPUESTO ALTO: 2.  Dos, no una, para conservar una segunda opinion de
      ordenacion; una sola linea deja al arbol sin nada con que comparar.
    * El resto: la politica por visitas de siempre, 5 al sembrar y 3 despues.
    """
    if seeding:
        return 5
    if budget_nodes is not None and budget_nodes >= DEPTH_BUDGET_THRESHOLD:
        return DEPTH_MULTIPV
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
            # Una posicion terminal lo es con el contador que sea: mate y
            # explosion tienen precedencia sobre la adjudicacion por reloj.
            if t[0] != 'DRAW':
                pos.clock_slack = logic.CLOCK_SLACK_MAX
            pos.save(update_fields=['status', 'closure', 'mate_in',
                                    'clock_slack'])
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
        worst_run = None
        if deadline is not None and time.monotonic() >= deadline:
            proof_result = 'INCONCLUSIVE'
        else:
            proof_result, worst_run = logic.prove_forced_mate(
                child_fen, winner_white, max_plies=len(pv_rest) + 2,
                budget_positions=budget_positions, hint_pv=pv_rest,
                deadline=deadline, return_run=True)
        prepared[index] = (winner_white, pv_rest, proof_result, worst_run)
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

    with atomic():
        pos = Position.objects.select_for_update().get(key=position_key)
        if pos.status != 'UNKNOWN':
            return {'skipped': 'already-closed'}

        expand(pos)
        stm_white = pos.fen.split()[1] == 'w'

        # evals de hijos reportados por el motor
        best_eval, best_move = None, None
        closed_here = 0
        revoked_here = []
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
            if (child.status != 'UNKNOWN' and prepared_proof is not None
                    and prepared_proof[2] == 'NO_MATE'):
                # El hijo YA estaba cerrado por un testigo sin certificar y
                # esta busqueda exhaustiva acaba de refutar ese mismo testigo.
                # Antes esto se caia por el suelo (el bucle solo miraba hijos
                # UNKNOWN) y el cierre falso se quedaba vivo para siempre.
                revoked_keys = _revoke_contradicted_mate(
                    child, pos, prepared_proof)
                if revoked_keys:
                    revoked_here.extend(revoked_keys)
                    child.refresh_from_db()
            if child.status == 'UNKNOWN' and prepared_proof is not None:
                winner_white, pv_rest, proof_result, worst_run = prepared_proof
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
                # Prueba exhaustiva: la racha reversible REAL del arbol
                # probado.  Testigo sin certificar: la cota burda por
                # longitud, que se recalcula al certificar.
                child.clock_slack = (
                    logic.slack_from_run(worst_run)
                    if proof_result == 'PROVEN' and worst_run is not None
                    else logic.slack_from_witness_length(len(pv_rest)))
                if pv_rest:
                    child.best_move = pv_rest[0]
                child.save(update_fields=['status', 'closure', 'proof',
                                          'won_line', 'mate_in', 'clock_slack',
                                          'best_move', 'updated'])
                _emit_closure_events(child)   # tambien cuenta y sale en feed
                # La linea ganadora deja de ser una cadena de texto y pasa a
                # ser arbol navegable: sin esto el explorador enseña el cierre
                # en la cabecera y "unexplored" en todas las filas.
                materialise_won_line(child)
                closed_here += 1
            if ev is not None and (best_eval is None
                                   or (stm_white and ev > best_eval)
                                   or (not stm_white and ev < best_eval)):
                best_eval, best_move = ev, uci

        pos.visits += 1
        pos.nodes_invested += nodes_budget
        pos.last_analysis = capped_analysis(lines[:8])
        if best_move:
            pos.best_move = best_move
        if best_eval is not None:
            pos.eval_cp = best_eval
        # Campos EXPLICITOS: una revocacion disparada mas arriba en este mismo
        # bucle pudo recalcular status/closure/priority de esta misma fila, y
        # un save() completo los pisaria con el snapshot que cargamos antes.
        pos.save(update_fields=['visits', 'nodes_invested', 'last_analysis',
                                'best_move', 'eval_cp', 'updated'])

    changed = backup_cascade([pos.key])
    # El respaldo se siembra tambien con los PADRES.  Un analisis nuevo no
    # cambia necesariamente el backed de este nodo (un autoeco bloqueado se
    # queda igual y no propaga), pero SI cambia ``nodes_invested`` — que es
    # exactamente lo que las guardas de calidad de los padres esperan.  Sin
    # esta semilla, la compra de convergencia analizaba al hijo y el padre
    # bloqueado no se enteraba jamas.
    parent_keys = list(Edge.objects.filter(child_id=pos.key)
                       .values_list('parent_id', flat=True))
    backed = backup_backed_evals([pos.key, *parent_keys])
    summary = {'closed_children': closed_here, 'backed_up': changed,
               'backed_evals': backed}
    if revoked_here:
        summary['revoked'] = len(revoked_here)
    return summary


def _revoke_contradicted_mate(child, parent, prepared_proof):
    """Retira un cierre de mate que esta busqueda exhaustiva acaba de refutar.

    Solo actua sobre lo que la refutacion realmente cubre: un cierre de mate
    SIN certificar (``MATE_PV`` con ``proof`` distinto de ``ANDOR``) cuya
    distancia declarada cabe dentro de los plies que la busqueda agoto.  Una
    busqueda que no encuentra mate en N plies no dice nada sobre un mate en
    N+5, asi que un ``mate_in`` mayor (o desconocido) NO se toca.
    """
    winner_white, pv_rest = prepared_proof[0], prepared_proof[1]
    want = 'WHITE_WIN' if winner_white else 'BLACK_WIN'
    searched_plies = len(pv_rest) + 2
    if (child.status != want or child.closure != 'MATE_PV'
            or child.proof == 'ANDOR' or child.mate_in is None
            or child.mate_in > searched_plies):
        return []
    DBEvent.objects.create(kind='MATE_PROOF_DISPUTED', payload={
        'key': child.key, 'parent': parent.key,
        'winner': 'WHITE' if winner_white else 'BLACK',
        'max_plies': searched_plies, 'was_closed': True,
        'claimed_mate_in': child.mate_in})
    outcome = revoke_closure(child.key, reason='mate-witness-refuted',
                             mark_disputed=True)
    return outcome['revoked']


def backup_cascade(seed_keys):
    """Recalcula el STATUS hacia arriba hasta punto fijo (§3.3).

    Ya NO toca ``eval_cp``.  Durante un tiempo hubo aqui un minimax de evals
    "en el sitio" que pisaba la columna con el min/max sobre los hijos, y eso
    resultó ser el bug que la comunidad reportó como "la propagacion recursiva
    sigue rota": la eval respaldada tiene su propia columna, ``backed_eval``,
    con guardas de cobertura y de calidad, y este minimax no tenia ninguna de
    las dos.  Con los dos escribiendo el mismo campo ganaba el ultimo, asi que
    un nodo cuya busqueda propia de 512M decia 413 se publicaba como 506 — el
    min sobre tres hermanos con evals sembradas por una busqueda mas somera,
    mientras otras veintidos respuestas no tenian numero ninguno.

    Ahora cada columna significa una sola cosa: ``eval_cp`` es lo que dijo el
    ultimo analisis DE ESTA posicion, ``backed_eval`` es lo que su subarbol
    sabe, y ``best_known_eval`` elige entre ambas en un solo sitio.
    """
    frontier = set(seed_keys)
    for pid in Edge.objects.filter(child_id__in=list(seed_keys)).values_list(
            'parent_id', flat=True):
        frontier.add(pid)
    changed_total = 0
    changed_keys = []
    guard = 0
    while frontier and guard < CASCADE_GUARD_LIMIT:
        guard += 1
        key = frontier.pop()
        pos = Position.objects.get(key=key)
        edges = list(Edge.objects.filter(parent=pos).select_related('child'))
        if edges:
            statuses = [_supporting_status(pos, e) for e in edges]
            new_status = (pos.status if pos.status != 'UNKNOWN' else
                          logic.backup_status(pos.fen, pos.expanded, statuses))
            dirty = False
            if new_status != pos.status and pos.status == 'UNKNOWN':
                pos.status, pos.closure = new_status, 'MINIMAX'
                pos.proof = _minimax_proof(pos, edges, new_status)
                # Testigo del minimax: la arista hacia el mejor hijo exacto.
                # Entre varios hijos ganadores manda el VERIFICADO: la
                # estrategia que este arbol exporta no debe apoyarse en un
                # testigo ENGINE existiendo uno ANDOR/TERMINAL/TB al lado.
                want = new_status
                winning = [e for e in edges if e.child.status == want]
                witness = (
                    next((e for e in winning
                          if _child_is_verified(e.child)), None)
                    or next(iter(winning), None)
                    or next((e for e in edges
                             if e.child.status != 'UNKNOWN'), None))
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
                        best = min(winners, key=_witness_rank)
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
                slack = _minimax_slack(pos, edges)
                if slack != pos.clock_slack:
                    pos.clock_slack = slack
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
                        best = min(winners, key=_witness_rank)
                        if (pos.mate_in is None
                                or 1 + best.child.mate_in < pos.mate_in
                                or (1 + best.child.mate_in == pos.mate_in
                                    and pos.best_move != best.move_uci
                                    and _child_is_verified(best.child))):
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
            if dirty:
                pos.save()
                changed_total += 1
                changed_keys.append(pos.key)
                for e in Edge.objects.filter(child=pos).values_list(
                        'parent_id', flat=True):
                    frontier.add(e)
    if frontier:
        DBEvent.objects.create(kind='CASCADE_GUARD', payload={
            'iterations': guard, 'remaining': len(frontier),
            'seed_count': len(seed_keys),
        })
    # Hook del gestor de prueba: los pn/dn del cono que acaba de moverse.
    # Va DESPUES del punto fijo exacto a proposito — la prueba se apoya en el
    # status, no al reves — y nunca puede tumbar una ingesta: unos numeros de
    # prueba viejos ordenan peor, no mienten.
    proof.refresh_proof_numbers(list(seed_keys) + changed_keys)
    return changed_total


def _status_eval(status):
    return {'WHITE_WIN': 10_000, 'BLACK_WIN': -10_000, 'DRAW': 0}.get(status)


def fresh_context_enabled():
    """Interruptor de la regla fresh-context (encendida por defecto).

    Existe para la ventana de despliegue: hasta que ``backfill_clock_slack``
    pase, la base viva tiene ``clock_slack`` NULL en todas partes y la regla
    bloquea cierres nuevos a traves de aristas tranquilas.  Bloquear cierres
    es CONSERVADOR (no cierra de menos por error, cierra de menos por
    prudencia), pero el propietario puede querer decidir cuando pagar ese
    precio.
    """
    return bool(getattr(settings, 'ATOMICDB_FRESH_CONTEXT', True))


def _supporting_status(pos, edge):
    """Status del hijo TAL COMO puede sostener el cierre de ``pos``.

    Un hijo decisivo alcanzado por arista tranquila sin margen de reloj no
    sostiene nada: para el padre cuenta como UNKNOWN, que es exactamente
    "todavia no lo sabemos", no "es tablas".  Las tablas y los UNKNOWN pasan
    tal cual: las tablas son inmunes al reloj (subirlo solo degrada victorias)
    y un UNKNOWN ya no sostiene nada.

    IMPORTANTE: esto endurece los cierres NUEVOS.  Los ya existentes no se
    re-derivan con esta regla — ver ``revoke_closure``, que sigue usando el
    backup liso — porque hacerlo antes del backfill desharia el arbol entero
    por un NULL, no por una refutacion.
    """
    child = edge.child
    if child.status in ('UNKNOWN', 'DRAW') or not fresh_context_enabled():
        return child.status
    if logic.edge_supports(pos.fen, edge.move_uci, child.clock_slack):
        return child.status
    return 'UNKNOWN'


def _minimax_slack(pos, edges):
    """``clock_slack`` de un cierre MINIMAX desde el de sus hijos."""
    if pos.status not in ('WHITE_WIN', 'BLACK_WIN'):
        return None            # las tablas no llevan slack
    mover_win = ('WHITE_WIN' if pos.fen.split()[1] == 'w' else 'BLACK_WIN')
    if pos.status == mover_win:
        winning = [(e.move_uci, e.child.clock_slack) for e in edges
                   if e.child.status == pos.status]
        return logic.minimax_slack(pos.fen, winning, [], mover_wins=True)
    return logic.minimax_slack(
        pos.fen, [], [(e.move_uci, e.child.clock_slack) for e in edges],
        mover_wins=False)


def _witness_rank(edge):
    """Orden del testigo exportable: mate mas corto, y a igualdad, VERIFICADO.

    La distancia manda porque es la unica parte que un usuario puede
    comprobar jugando; el desempate por certificacion es el arreglo de F4b:
    con dos mates de la misma longitud, exportar el que tiene prueba
    exhaustiva en vez del primero que devolvio el movegen.
    """
    return (edge.child.mate_in, 0 if _child_is_verified(edge.child) else 1)


# ---------------- valor RESPALDADO (backed eval) ----------------
#
# CONVENCION DE SIGNO.  Todo lo que hay aqui vive en perspectiva BLANCA,
# exactamente como ``eval_cp``.  Un negamax en perspectiva blanca es un
# minimax liso: max en un nodo con blancas al turno, min con negras.  Dentro
# de este modulo NO hay ni un solo cambio de signo; el unico flip a la
# perspectiva del que mueve ocurre una vez por ply, al pintar, en views.py.
#
# QUE ES.  ``eval_cp`` es la eval ALMACENADA del nodo: la que dejo su ultimo
# analisis.  ``backup_cascade`` la refina en el sitio, pero SOLO en nodos
# completamente expandidos y sin guardas de cobertura, y cada analisis nuevo
# la vuelve a pisar con la lectura puntual del motor.  ``backed_eval`` es un
# valor aparte y explicito: el negamax sobre los hijos con informacion,
# usando el backed del hijo si existe y su eval almacenada si no.  Ninguna
# pisa a la otra, y el respaldo atraviesa tambien lo que la cascada se niega
# a tocar (expansiones parciales de /goto/).
#
# GUARDAS (§ refinamiento de fase A).  Sobre una lista de jugadas COMPLETA
# (``expanded`` y todos los hijos informados) el negamax es el minimax de
# verdad y sustituye a la eval propia en cualquier direccion.  Sobre
# cobertura PARCIAL solo vale como "valor que el que mueve puede alcanzar":
# un hijo analizado demuestra que ese valor esta disponible, pero los hijos
# sin mirar pueden esconder algo mejor.  Por eso, con cobertura parcial, el
# valor solo se mueve en la direccion que FAVORECE al que mueve (max en un
# nodo OR, min en uno AND) y nunca en la contraria.  Asi el nodo OR (el
# atacante de la conjetura) jamas baja sin cobertura completa, y el nodo AND
# (el defensor) jamas sube: el min parcial es sistematicamente optimista para
# el atacante y queda vetado.
#
# CALIDAD.  Un valor respaldado arrastra el presupuesto de busqueda que lo
# sostiene.  Con cobertura parcial, un hijo solo puede desplazar la eval
# propia si su respaldo pesa al menos tanto como la busqueda que produjo esa
# eval propia: asi un analisis de 128M no pisa lo que respaldo uno de 10B.
# Un hijo con status PROBADO pesa mas que cualquier busqueda.
BACKED_MAX_PLIES = 64        # tope de profundidad del ascenso (generoso)
BACKED_MAX_REVISITS = 2      # el DAG puede volver a un nodo por otro hijo
BACKED_MAX_NODES = 50_000    # tope duro de recomputos por llamada
BACKED_EPSILON_CP = 10       # ruido que no merece seguir subiendo
# TOLERANCIA DE LA GUARDA DE CALIDAD.
#
# La guarda nacio para que un analisis de 8M no pisara lo que respaldo uno de
# 10B.  Comparada de forma estricta hacia tambien esto (caso real, 28-jul):
# un nodo con 128.111.808 nodos propios y un hijo con 128.007.926 de soporte.
# Una diferencia del 0,08% — la misma busqueda, practicamente — bloqueaba un
# desplazamiento favorable al que mueve, y la pagina mostraba 416 en la fila
# del hijo mientras la cabecera del padre seguia diciendo 369.
#
# Media orden de magnitud es la linea: separa "la misma busqueda" de "una
# busqueda seria contra una superficial", que es lo unico que la guarda
# queria distinguir.
BACKED_QUALITY_TOLERANCE = 0.5
PROVEN_QUALITY = 1 << 60     # calidad de un valor exacto (gana a toda busqueda)

_BACKED_FIELDS = ['backed_eval', 'backed_move', 'backed_plies', 'backed_nodes']


class _ChildValue:
    """Lo que un hijo aporta al negamax del padre."""

    __slots__ = ('move', 'value', 'quality', 'plies')

    def __init__(self, move, value, quality, plies):
        self.move, self.value = move, value
        self.quality, self.plies = quality, plies


def _child_contribution(move_uci, status, eval_cp, backed_eval, backed_nodes,
                        nodes_invested, backed_plies):
    """Valor y calidad de un hijo, en perspectiva blanca.

    Un status resuelto entra con su valor de VERDAD (mate/tablas) y calidad
    de prueba; nunca con una eval. Si no, manda el backed del hijo, y solo en
    su ausencia su eval puntual.
    """
    exact = _status_eval(status)
    if exact is not None:
        return _ChildValue(move_uci, exact, PROVEN_QUALITY, 0)
    if backed_eval is not None:
        # La calidad del mejor conocimiento del hijo es TODO lo invertido en
        # el, no solo el soporte de la hoja que respaldo el valor.  Con solo
        # ``backed_nodes`` la cadena quedaba clavada en la hoja mas debil:
        # cualquier ancestro con busqueda propia honda bloqueaba, la compra
        # de convergencia le compraba al hijo un analisis profundo que subia
        # ``nodes_invested``... y esta contribucion no lo miraba, asi que el
        # bloqueo era permanente y silencioso (caso Wolfram, 28-jul: espinas
        # de 2.6B bloqueando respaldos con soporte 128M para siempre).  El
        # propio nodo ya ENSEÑA su backed como mejor conocimiento; su padre
        # debe verlo con el peso de todo lo que lo corrobora.
        return _ChildValue(move_uci, backed_eval,
                           max(backed_nodes or 0, nodes_invested or 0),
                           backed_plies or 0)
    if eval_cp is not None:
        return _ChildValue(move_uci, eval_cp, nodes_invested or 0, 0)
    return _ChildValue(move_uci, None, 0, 0)


def _better_for_mover(value, reference, stm_white):
    return value > reference if stm_white else value < reference


def _backed_for(row, children, discrepancies=None):
    """(valor, arista, plies, calidad) respaldados para ``row``.

    ``children`` son ``_ChildValue`` de TODAS las aristas del nodo (las que
    no aportan nada llevan ``value=None``).

    ``discrepancies``, si se pasa, recoge los casos en los que un hijo
    reclamaba un valor MEJOR para el que mueve y la guarda de calidad lo
    bloqueo de verdad (soporte por debajo de la tolerancia).  El llamante los
    convierte en trabajo: ver ``_queue_quality_convergence``.
    """
    exact = _status_eval(row.status)
    if exact is not None:
        # Un backed jamas puede contradecir el status probado del propio nodo.
        return exact, row.best_move, 0, PROVEN_QUALITY
    informed = [c for c in children if c.value is not None]
    if not informed:
        return None, None, 0, 0
    stm_white = row.fen.split()[1] == 'w'
    # Empates de valor: gana el respaldo mas pesado, luego el mas superficial
    # (menos plies), luego el orden de movegen, para que el resultado sea
    # determinista bajo replay.
    best = (max if stm_white else min)(
        informed,
        key=lambda c: (c.value, c.quality, -c.plies) if stm_white
        else (c.value, -c.quality, c.plies))
    complete = bool(row.expanded) and len(informed) == len(children)
    if not complete:
        own = row.eval_cp
        own_quality = row.nodes_invested or 0
        if own is not None:
            # GUARDA DIRECCIONAL (intacta): con cobertura parcial el valor
            # solo se mueve en la direccion que favorece al que mueve.
            favourable = _better_for_mover(best.value, own, stm_white)
            # GUARDA DE CALIDAD, ahora con tolerancia.
            outweighed = best.quality < own_quality * BACKED_QUALITY_TOLERANCE
            if not favourable or outweighed:
                if favourable and outweighed and discrepancies is not None:
                    # El hijo dice algo mejor y no tiene con que sostenerlo.
                    # Eso no es ruido que ignorar: es una pregunta abierta, y
                    # la respuesta se compra.
                    discrepancies.append((best.move, row.key, own_quality))
                return own, None, 0, own_quality
    return best.value, best.move, 1 + best.plies, best.quality


def _rung_at_least(nodes):
    """El peldano de la escalera que iguala (o supera) ese soporte."""
    for rung in BUDGET_LADDER:
        if rung >= nodes:
            return rung
    return BUDGET_LADDER[-1]


def _queue_quality_convergence(discrepancies):
    """Convierte en trabajo los bloqueos LEGITIMOS de la guarda de calidad.

    Un hijo que reclama un valor mejor con 8M de soporte frente a los 10B del
    padre tiene razon o no la tiene, y hoy no hay forma de saberlo: la guarda
    se limita a callarlo.  Se le compra al hijo el peldano que iguala el
    soporte del padre y la siguiente cascada lo resuelve sola.

    Con la tolerancia de arriba, toda discrepancia acaba o DESPLAZANDO (misma
    busqueda) o CONVERGIENDO (busqueda distinta, se iguala).  Ninguna se queda
    en silencio, que era la forma de este sintoma.
    """
    if not discrepancies:
        return 0
    pending = AnalysisTask.objects.filter(
        state='PENDING', source=AnalysisTask.Source.FILL).count()
    room = max(0, COVERAGE_QUEUE_CAP - pending)
    if room <= 0:
        return 0
    made = 0
    for move_uci, parent_key, own_quality in discrepancies:
        if made >= room:
            break
        edge = Edge.objects.filter(parent_id=parent_key,
                                   move_uci=move_uci).first()
        if edge is None:
            continue
        child = edge.child
        if child.status != 'UNKNOWN':
            continue
        budget = _rung_at_least(own_quality)
        if (child.nodes_invested or 0) >= budget:
            continue                  # ya se le compro esa profundidad
        task, created = AnalysisTask.objects.get_or_create(
            position=child, generation=child.visits,
            defaults={'budget_nodes': budget,
                      'multipv': multipv_for(child.visits, budget),
                      'source': AnalysisTask.Source.FILL})
        if created:
            made += 1
        elif task.state == 'PENDING' and task.budget_nodes < budget:
            task.budget_nodes = budget
            task.source = AnalysisTask.Source.FILL
            task.save(update_fields=['budget_nodes', 'source'])
    if made:
        DBEvent.objects.create(kind='QUALITY_CONVERGENCE', payload={
            'queued': made, 'discrepancies': len(discrepancies)})
    return made


def _backed_stored(row, value, move, plies, quality):
    return (row.backed_eval == value and row.backed_move == move
            and row.backed_plies == plies and row.backed_nodes == quality)


def _backed_worth_propagating(row, value, move):
    """Corte efectivo: sube solo si cambia la DECISION o el valor de verdad.

    Cerca de las hojas el "no cambio nada" casi nunca dispara (las evals
    difieren siempre en algun cp), asi que el corte util es este: la arista
    que respalda el valor sigue siendo la misma y el valor se movio menos de
    BACKED_EPSILON_CP.
    """
    if (row.backed_eval is None) != (value is None):
        return True
    if value is None:
        return False
    if row.backed_move != move:
        return True
    return abs(value - row.backed_eval) >= BACKED_EPSILON_CP


def _backed_children_by_parent(parent_keys):
    rows = Edge.objects.filter(parent_id__in=parent_keys).values_list(
        'parent_id', 'move_uci', 'child__status', 'child__eval_cp',
        'child__backed_eval', 'child__backed_nodes', 'child__nodes_invested',
        'child__backed_plies')
    by_parent = {}
    for (parent_id, move_uci, status, eval_cp, backed_eval, backed_nodes,
         nodes_invested, backed_plies) in rows:
        by_parent.setdefault(parent_id, []).append(_child_contribution(
            move_uci, status, eval_cp, backed_eval, backed_nodes,
            nodes_invested, backed_plies))
    return by_parent


def backup_backed_evals(seed_keys, max_plies=BACKED_MAX_PLIES):
    """Recalcula el valor respaldado y lo sube por el DAG hasta que deje de
    importar.

    El ascenso es por NIVELES para que el coste en consultas no crezca con la
    anchura del arbol: un nivel entero cuesta cuatro sentencias (posiciones,
    aristas+hijos, bulk_update, padres) tenga tres hijos o sesenta.  Un DAG
    tiene varios padres por nodo y puede cerrar ciclos por transposicion, asi
    que el ascenso lleva contador de visitas por nodo, tope de plies y tope
    global de recomputos.
    """
    frontier = [key for key in dict.fromkeys(seed_keys) if key]
    visits, changed_total, processed, plies = {}, 0, 0, 0
    while frontier and plies < max_plies:
        plies += 1
        rows = list(Position.objects.filter(key__in=frontier).only(
            'key', 'fen', 'status', 'expanded', 'eval_cp', 'nodes_invested',
            'best_move', *_BACKED_FIELDS))
        if not rows:
            break
        children = _backed_children_by_parent([row.key for row in rows])
        dirty, propagate, discrepancies = [], [], []
        for row in rows:
            processed += 1
            value, move, below, quality = _backed_for(
                row, children.get(row.key, ()), discrepancies)
            if _backed_stored(row, value, move, below, quality):
                continue
            if _backed_worth_propagating(row, value, move):
                propagate.append(row.key)
            row.backed_eval, row.backed_move = value, move
            row.backed_plies, row.backed_nodes = below, quality
            dirty.append(row)
        if dirty:
            Position.objects.bulk_update(dirty, _BACKED_FIELDS, batch_size=500)
            changed_total += len(dirty)
        _queue_quality_convergence(discrepancies)
        if not propagate:
            break
        if processed >= BACKED_MAX_NODES:
            DBEvent.objects.create(kind='BACKED_GUARD', payload={
                'reason': 'node-budget', 'processed': processed,
                'plies': plies, 'seed_count': len(seed_keys)})
            break
        frontier = []
        for key in set(Edge.objects.filter(child_id__in=propagate)
                       .values_list('parent_id', flat=True)):
            seen = visits.get(key, 0)
            if seen < BACKED_MAX_REVISITS:
                visits[key] = seen + 1
                frontier.append(key)
    else:
        if frontier:
            DBEvent.objects.create(kind='BACKED_GUARD', payload={
                'reason': 'ply-guard', 'processed': processed,
                'plies': plies, 'seed_count': len(seed_keys)})
    return changed_total


def best_known_eval(pos):
    """Mejor conocimiento actual del nodo, en perspectiva blanca.

    Es lo que deben pintar tanto la cabecera de la posicion como la fila de
    la arista que apunta a ella: status probado > backed > eval puntual.
    """
    exact = _status_eval(pos.status)
    if exact is not None:
        return exact
    if pos.backed_eval is not None:
        return pos.backed_eval
    return pos.eval_cp


# ---------------- materializacion de la linea ganadora ----------------
#
# Un cierre MATE_PV vive SOLO en el nodo: ``expand`` se salta todo lo que no
# esta UNKNOWN, asi que la cadena del testigo no se materializa nunca y la
# linea ganadora es una cadena de texto, no arbol.  El explorador lo enseña
# tal cual: la cabecera dice "WHITE_WIN via MATE_PV, ≤M6" y la tabla de
# jugadas dice "unexplored" en las 30 filas, la ganadora incluida, porque el
# nodo no tiene ni una arista.  Navegar la jugada ganadora aterriza en una
# posicion que la base ni conoce.
#
# Materializar la cadena arregla eso sin inventar nada: cada posicion del
# camino existe ya como hecho (el testigo fue re-verificado jugada a jugada al
# cerrar), y cada sufijo es un mate igual de probado que el original, un ply
# mas corto.  Se materializa SOLO la arista de la linea — nada de expandir —
# asi que el selector sigue ignorando estos nodos exactamente como hasta ahora.

WON_LINE_MAX_PLIES = 64


def materialise_won_line(pos, verify=False):
    """Crea la cadena de la ``won_line`` como arbol de verdad.

    Cada nodo del camino queda cerrado con el mismo status y la misma CLASE DE
    PRUEBA que el original: un ANDOR probo exhaustivamente ese subarbol, asi
    que sus sufijos son ANDOR; un ENGINE es un testigo legal sin certificar y
    sus sufijos heredan esa misma honestidad.  Un DISPUTED no materializa nada.

    ``clock_slack`` se hereda del nodo original.  Es CONSERVADOR por
    construccion: la racha reversible que le queda a un sufijo no puede ser
    mayor que la del arbol entero, asi que el slack real del sufijo es al
    menos este.  Prefiero subestimar el margen a inventarlo.
    """
    line = (pos.won_line or '').split()
    if (pos.closure != 'MATE_PV' or pos.status not in ('WHITE_WIN', 'BLACK_WIN')
            or pos.proof == 'DISPUTED' or not line):
        return {'created_edges': 0, 'closed': 0, 'plies': 0}
    if verify and not logic.verify_mate_pv(
            pos.fen, line, pos.status == 'WHITE_WIN'):
        return {'created_edges': 0, 'closed': 0, 'plies': 0,
                'rejected': 'witness-does-not-verify'}

    grade = pos.proof if pos.proof in ('ANDOR', 'ENGINE') else None
    slack = pos.clock_slack
    created_edges = closed = 0
    node, walked = pos, 0
    for index, uci in enumerate(line[:WON_LINE_MAX_PLIES]):
        if uci not in logic.legal_moves(node.fen):
            break                       # testigo historico ya no legal
        child = get_or_create_position(logic.apply_move(node.fen, uci),
                                       campaign=node.campaign)
        _edge, made = Edge.objects.get_or_create(
            parent=node, move_uci=uci, defaults={'child': child})
        if made:
            created_edges += 1
        walked += 1
        suffix = line[index + 1:]
        if suffix and child.status == 'UNKNOWN':
            child.status = pos.status
            child.closure = 'MATE_PV'
            child.proof = grade
            child.won_line = ' '.join(suffix)
            child.mate_in = len(suffix)
            child.best_move = suffix[0]
            child.clock_slack = slack
            child.save(update_fields=['status', 'closure', 'proof',
                                      'won_line', 'mate_in', 'best_move',
                                      'clock_slack', 'updated'])
            _emit_closure_events(child)
            closed += 1
        node = child
    return {'created_edges': created_edges, 'closed': closed, 'plies': walked}


def _child_is_verified(child):
    return (child.closure in ('TERMINAL', 'TB')
            or child.proof == 'ANDOR')


# ---------------- revocacion de cierres ----------------
#
# POR QUE EXISTE.  Hasta aqui un cierre era para siempre: `verify_mates` podia
# marcar `proof='DISPUTED'` sobre un MATE_PV y el nodo seguia con su
# `status=WHITE_WIN` intacto, envenenando por MINIMAX a todos sus ancestros.
# El unico camino que reabria algo era el DISPUTED del ingest online, y solo
# porque alli el hijo todavia estaba UNKNOWN.  Esta es la pieza que faltaba:
# retirar un hecho exacto que perdio su evidencia Y deshacer, hasta punto
# fijo, exactamente lo que se sostenia sobre el.
#
# QUE SE REVOCA Y QUE NO.  Solo se revoca lo DERIVADO.  Un cierre TERMINAL
# (la propia posicion es terminal), un cierre TB (probe re-verificado en el
# servidor) y un MATE_PV con `proof='ANDOR'` (mate forzado demostrado
# exhaustivamente) son hechos INDEPENDIENTES de sus hijos: no los toca esta
# via, y ademas CORTAN la cascada, porque su status no cambia y por tanto el
# backup de sus padres tampoco.  Lo unico que depende de los hijos es el
# cierre MINIMAX, asi que la cascada hacia arriba es exactamente "recorrer
# ancestros MINIMAX y re-derivar su backup".
#
# NO ES MONOTONO AL REVES.  Un ancestro puede sobrevivir a la revocacion de un
# hijo (le queda otra arista ganadora) y caer despues, cuando se revoca un
# segundo hijo.  Por eso la cascada no lleva un `seen` que bloquee revisitas:
# lleva un contador de guarda y se apoya en que revocar es monotono (un nodo
# pasa de cerrado a UNKNOWN como mucho una vez).
REVOKE_GUARD_LIMIT = 100_000
_REVOKED_FIELDS = ['status', 'closure', 'proof', 'won_line', 'mate_in',
                   'clock_slack']


def _closure_is_independent(pos):
    """True si el cierre no se apoya en los hijos (y corta la cascada)."""
    return (pos.closure in ('TERMINAL', 'TB')
            or (pos.closure != 'MINIMAX' and pos.proof == 'ANDOR'))


def _witness_runs_through(pos, child_key):
    """True si el testigo de ``pos`` pasa por ese hijo exacto.

    Un nodo de una cadena materializada cierra por su propio sufijo, no por un
    backup, asi que la regla "solo se revocan los MINIMAX" lo dejaria en pie
    con su testigo ya refutado.  Si la PRIMERA jugada de su ``won_line`` lleva
    al nodo que se acaba de revocar, su linea entera esta refutada tambien y
    tiene que caer con el.
    """
    line = (pos.won_line or '').split()
    if pos.closure != 'MATE_PV' or not line or not child_key:
        return False
    if pos.proof == 'ANDOR':
        # An exhaustive AND/OR search proved THIS node on its own; it does not
        # rest on the child's uncertified witness even though its line runs
        # through it.  ANDOR keeps cutting the cascade, as it always did.
        return False
    return Edge.objects.filter(parent=pos, move_uci=line[0],
                               child_id=child_key).exists()


def _clear_closure(pos, mark_disputed=False):
    """Devuelve el nodo a UNKNOWN sin tocar nada heuristico.

    ``eval_cp``/``backed_eval``/``visits`` son conocimiento heuristico y
    sobreviven: lo que se retira es la PRETENSION de exactitud.

    ``mark_disputed`` conserva el rastro de la refutacion exactamente como ya
    hacia el camino DISPUTED del ingest online: el nodo vuelve a UNKNOWN pero
    guarda ``proof='DISPUTED'`` y el testigo refutado en ``won_line``, para que
    la deuda quede visible en vez de evaporarse.  Los ancestros que caen por
    cascada NO se marcan: su evidencia no fue refutada, simplemente desaparecio.
    """
    pos.status = 'UNKNOWN'
    pos.closure = None
    pos.mate_in = None
    pos.clock_slack = None      # el slack pertenecia al cierre retirado
    if mark_disputed:
        pos.proof = 'DISPUTED'
    else:
        pos.proof = None
        pos.won_line = None
    # Las lapidas las levanta ``_revive_tombstones`` en un solo UPDATE por
    # nivel, para que el contador que devuelve la revocacion sea el numero real
    # de resurrecciones y no se pise con este save.
    pos.save(update_fields=_REVOKED_FIELDS + ['updated'])


def _revive_tombstones(keys):
    """Levanta las lapidas de los nodos reabiertos y de sus hijos directos.

    Un nodo se marca DEAD cuando TODOS sus padres estan cerrados
    (``_still_reachable``).  Reabrir un ancestro vuelve a hacer relevante ese
    subarbol, asi que la lapida deja de ser cierta.  Se levanta un solo nivel:
    los nietos vuelven solos en cuanto el selector visite a sus padres, y una
    resurreccion recursiva del cono entero seria un UPDATE sin cota.
    """
    if not keys:
        return 0
    revived = Position.objects.filter(
        key__in=list(keys), priority__lte=DEAD / 2).update(priority=0.0)
    child_keys = list(Edge.objects.filter(parent_id__in=list(keys))
                      .values_list('child_id', flat=True))
    if child_keys:
        revived += Position.objects.filter(
            key__in=child_keys, priority__lte=DEAD / 2).update(priority=0.0)
    return revived


def revoke_closure(position_key, reason='', requeue=True, mark_disputed=False):
    """Retira un cierre y todo lo que se apoyaba en el. Punto fijo hacia arriba.

    Devuelve ``{'revoked': [keys...], 'requeued': task_id|None,
    'revived': int}``.  ``revoked`` sale en el orden en que la cascada los fue
    abriendo: el nodo semilla primero, luego sus ancestros MINIMAX.

    El reanalisis a presupuesto maximo se encola SOLO para la semilla: es la
    unica que perdio su evidencia propia.  Los ancestros revocados vuelven al
    selector normal y se re-cerraran solos en cuanto la semilla se cierre otra
    vez; encolar 2B para cada uno quemaria el presupuesto del proyecto en
    trabajo que la cascada hara gratis.
    """
    revoked, guard = [], 0
    pending = [(position_key, None)]
    while pending and guard < REVOKE_GUARD_LIMIT:
        guard += 1
        key, via_child = pending.pop(0)
        try:
            pos = Position.objects.select_for_update().get(key=key)
        except Position.DoesNotExist:
            continue
        if pos.status == 'UNKNOWN':
            continue                      # ya abierto: nada que retirar
        if key != position_key:
            if pos.closure == 'MINIMAX':
                edges = list(Edge.objects.filter(parent=pos)
                             .select_related('child'))
                derived = logic.backup_status(
                    pos.fen, pos.expanded, [e.child.status for e in edges])
                if derived == pos.status:
                    continue              # el backup se sostiene sin el hijo
            elif not _witness_runs_through(pos, via_child):
                continue                  # hecho independiente: corta aqui
        elif _closure_is_independent(pos):
            # La semilla se revoca a peticion expresa del llamante (es su
            # evidencia la que fallo), pero un TERMINAL jamas: la posicion es
            # terminal por movegen, no por creencia.
            if pos.closure == 'TERMINAL':
                return {'revoked': [], 'requeued': None, 'revived': 0}
        _clear_closure(pos, mark_disputed=mark_disputed and key == position_key)
        revoked.append(key)
        pending.extend((parent_id, key) for parent_id in
                       Edge.objects.filter(child=pos)
                       .values_list('parent_id', flat=True))

    if not revoked:
        return {'revoked': [], 'requeued': None, 'revived': 0}

    revived = _revive_tombstones(revoked)
    # Punto fijo desde abajo otra vez: lo que TODAVIA se sostiene con la
    # evidencia superviviente vuelve a cerrarse, y los DTM/testigos de los
    # ancestros que sobrevivieron se recalculan sin el hijo retirado.
    backup_cascade(revoked)
    requeued = None
    if requeue:
        seed = Position.objects.get(key=position_key)
        if seed.status == 'UNKNOWN':
            requeued = _queue_disputed_reanalysis(seed).id
    DBEvent.objects.create(kind='CLOSURE_REVOKED', payload={
        'key': position_key, 'reason': reason or 'evidence-withdrawn',
        'chain': revoked[:64], 'revoked': len(revoked),
        'revived': revived, 'requeued': requeued,
        'truncated': len(revoked) > 64})
    if guard >= REVOKE_GUARD_LIMIT:
        DBEvent.objects.create(kind='REVOKE_GUARD', payload={
            'key': position_key, 'iterations': guard,
            'remaining': len(pending)})
    return {'revoked': revoked, 'requeued': requeued, 'revived': revived}


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
    # El mapa de valores usa el MEJOR CONOCIMIENTO, no la eval cruda:
    # status probado > respaldado > eval propia, igual que
    # ``best_known_eval``.
    #
    # POR QUE IMPORTA (fuga del 28-jul).  Un nodo con dos padres se
    # alcanza por el camino MENOS informado: si uno de esos padres nunca
    # se analizo directamente tiene ``eval_cp`` None aunque la cascada ya
    # le haya subido un ``backed_eval`` decidido.  Con la eval cruda ese
    # padre no aporta gap ninguno, sus hijos heredan regret bajo, y como
    # el Dijkstra toma el MINIMO sobre caminos, un subarbol ya refutado
    # entra por la puerta de atras.  Eso tuvo al selector ocho horas
    # taladrando posiciones bajo una jugada que pierde en dos.
    settled = set()
    for key, fen, eval_cp, backed_eval, status in \
            Position.objects.values_list(
                'key', 'fen', 'eval_cp', 'backed_eval', 'status'):
        known = backed_eval if backed_eval is not None else eval_cp
        val[key] = {'WHITE_WIN': 10_000, 'BLACK_WIN': -10_000,
                    'DRAW': 0}.get(status, known)
        white_stm[key] = fen.split()[1] == 'w'
        if status != 'UNKNOWN':
            settled.add(key)
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
        if k in settled:
            # UN NODO CERRADO NO RELAJA HACIA ABAJO (fuga #2, 28-jul).
            #
            # Un cierre conserva su valor de verdad, asi que en la raiz un
            # WHITE_WIN ES el mejor hijo: su propio gap es cero, y con la
            # relajacion normal ese cero lo heredaban todos sus descendientes.
            # El resultado era una autopista de regret cero hacia el interior
            # de un subarbol ya resuelto — y desde la materializacion de las
            # ``won_line`` hay CADENAS enteras de nodos cerrados, asi que la
            # autopista pasaba a ser sistematica en vez de anecdotica.
            #
            # Un nodo cerrado no necesita mas trabajo debajo: lo que haya bajo
            # el solo merece atencion si algun camino ABIERTO llega tambien
            # (una transposicion), y ese camino se relaja por su cuenta.  Si
            # no lo hay, el nodo es inalcanzable de forma util y
            # ``_still_reachable`` ya lo entierra.
            #
            # Ojo a lo que NO cambia: el cerrado sigue contando como HIJO al
            # calcular el gap de sus hermanos, que es justo lo que hace que
            # una alternativa perdedora cargue con su distancia al mate.
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


def refresh_priorities(force=False):
    """§4.1 — recalculo global (llamado por el selector). Prioridad =
    cercania al cierre local - regret acumulado desde la raiz - visitas.
    Respeta las lapidas (las ramas muertas no resucitan).

    Es O(grafo entero) — Dijkstra sobre todos los nodos y aristas, mas dos
    diccionarios con la base cargada en RAM.  A 450k posiciones son segundos
    de CPU; a 4,5M son decenas de segundos y gigabytes, multiplicados por cada
    proceso de gunicorn que lo dispare.  Por eso ya NO corre dentro de la
    request del worker (ver ``next_tasks``): lo llama el servicio
    ``refresh_selector``, un solo proceso, fuera del camino HTTP.

    ``force`` salta la cache de PRIORITY_REFRESH_SECONDS; el servicio la usa
    para que su propio intervalo sea el unico reloj que manda.
    """
    now = time.monotonic()
    cached = _priority_refresh_cache
    if (not force and cached['at']
            and now - cached['at'] < PRIORITY_REFRESH_SECONDS):
        return False

    regret = _regret_from_root()
    dirty = []
    for pos in Position.objects.filter(status='UNKNOWN',
                                       priority__gt=DEAD / 2) \
                               .iterator(chunk_size=2000):
        # Mismo criterio que el mapa del Dijkstra: un nodo sin eval
        # propia pero con respaldo decidido no es un nodo sin
        # informacion, ni en un sentido ni en el otro.
        known = best_known_eval(pos)
        e = abs(known) if known is not None else 0
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


def inline_selector_enabled():
    """Interruptor de emergencia: el Dijkstra global, dentro de la request.

    Con ``ATOMICDB_INLINE_SELECTOR = True`` vuelve el comportamiento anterior
    a P0d — ``next_tasks`` refresca prioridades el mismo, por el MISMO codigo.
    Es la salida de emergencia si el servicio ``refresh_selector`` se cae y no
    hay nadie para levantarlo: una linea de settings y un reinicio del web,
    sin desplegar otro camino de codigo.
    """
    return bool(getattr(settings, 'ATOMICDB_INLINE_SELECTOR', False))


def next_tasks(n):
    """Selector global best-first sobre todo el arbol (sin campanas).

    Consume ``priority`` TAL Y COMO ESTE.  Quien la mantiene es el servicio
    ``refresh_selector``; aqui no se recalcula nada porque este codigo corre
    dentro de ``/atomicdb/api/lease``, y un Dijkstra sobre el grafo entero no
    tiene sitio en una request HTTP.  Una prioridad de hace un minuto ordena
    la cola igual de bien: el selector es una heuristica de PARA DONDE MIRAR,
    no una fuente de verdad.
    """
    if proof.selector_mode() == 'pn':
        return _next_tasks_by_proof(n)
    if inline_selector_enabled():
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
        budget = budget_for(pos)
        task, _ = AnalysisTask.objects.get_or_create(
            position=pos, generation=pos.visits,
            defaults={'budget_nodes': budget,
                      'multipv': multipv_for(pos.visits, budget)})
        if task.state == 'PENDING':
            tasks.append(task)
    return tasks


def _task_counter():
    """Contador monotono y barato para el reparto blando del repertorio.

    El id mas alto de la tabla de tareas ya es monotono y esta indexado, asi
    que no hace falta ni una columna nueva ni un COUNT sobre millones de
    filas.  Lo unico que se le pide es que avance.
    """
    return AnalysisTask.objects.order_by('-id').values_list(
        'id', flat=True).first() or 0


def _next_tasks_by_proof(n):
    """Selector df-pn: baja desde la raiz de cada campana activa.

    Coste O(profundidad x branching) de LECTURAS por tarea, sin una sola
    pasada global.  La asignacion blanda del repertorio (80/15/5 por defecto)
    se resuelve con un contador determinista: el mismo estado produce la misma
    cola, y un replay es reproducible.
    """
    campaigns = proof.active_campaigns()
    if not campaigns:
        return []
    base = _task_counter()
    tasks, seen, attempts = [], set(), 0
    budget = 4 * max(1, n)
    while len(tasks) < n and attempts < budget:
        campaign = campaigns[attempts % len(campaigns)]
        pos, _plies = proof.descend(campaign, counter=base + attempts,
                                    avoid=seen)
        attempts += 1
        if pos is None or pos.key in seen:
            continue
        seen.add(pos.key)
        if not _still_reachable(pos):
            pos.priority = DEAD
            pos.save(update_fields=['priority'])
            continue
        budget = budget_for(pos)
        task, _created = AnalysisTask.objects.get_or_create(
            position=pos, generation=pos.visits,
            defaults={'budget_nodes': budget,
                      'multipv': multipv_for(pos.visits, budget)})
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
            # Sembrando la raiz: la anchura ES el producto aqui.
            AnalysisTask.objects.create(position=c, generation=gen,
                                        budget_nodes=budget, source='USER',
                                        multipv=multipv_for(0, seeding=True))
        made += 1
    return made


_LADDER_EXHAUSTED = 'ladder-exhausted'   # interno: nunca sale del modulo


class RequestOutcome(str):
    """Request status that can carry frontier-expansion counters.

    It stays a plain ``str`` on purpose: the view still drops it straight
    into ``{'status': ...}``, explore.html still switches on it and the M1
    tests still compare it against a literal.  Only the frontier path
    attaches a ``detail`` mapping, which the view merges into its payload.
    """

    def __new__(cls, value, **detail):
        outcome = super().__new__(cls, value)
        outcome.detail = detail
        return outcome


def _completed_max_budget(pos):
    return (AnalysisTask.objects.filter(
        position=pos, state=AnalysisTask.TState.COMPLETED)
        .order_by('-budget_nodes').values_list('budget_nodes', flat=True)
        .first())


def ladder_exhausted(pos):
    """True when the visitor ladder has nothing left to buy on ``pos`` itself.

    Read-only.  The view needs it to keep click deduplication anchored on
    the parent even though an expansion places its tasks on the children.
    """
    if pos.status != 'UNKNOWN':
        return False
    completed_max = _completed_max_budget(pos)
    return (completed_max is not None
            and completed_max >= REQUEST_BUDGET_LADDER[-1])


def _request_rung(pos):
    """Buy the next ladder rung for ONE position. The caller owns the tx.

    Devuelve 'queued' | 'already-queued' | 'already-solved', o el centinela
    interno _LADDER_EXHAUSTED cuando el ultimo peldano ya esta COMPLETED:
    repetirlo seria gastar 10B en una busqueda que ya tenemos."""
    # The caller may hold a stale Position instance while another submit has
    # just advanced visits. Lock and refresh before choosing the generation so
    # a 512M/2B/10B request cannot accidentally target the completed rung.
    pos = Position.objects.select_for_update().get(pk=pos.pk)
    if pos.status != 'UNKNOWN':
        return 'already-solved'
    completed_max = _completed_max_budget(pos)
    if (completed_max is not None
            and completed_max >= REQUEST_BUDGET_LADDER[-1]):
        return _LADDER_EXHAUSTED
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
                  'multipv': multipv_for(pos.visits, floor)})
    if created:
        return 'queued'
    if task.state == 'PENDING':
        task.budget_nodes = max(task.budget_nodes, floor)
        promoted = task.source != 'USER'
        task.source = 'USER'   # promocion: al frente de la cola
        task.save(update_fields=['source', 'budget_nodes'])
        if promoted:
            return 'queued'
    elif task.state == 'LEASED':
        if task.budget_nodes < floor:
            # The running engine cannot change its ``go nodes`` command.
            # Keep the user's deeper request as the next generation
            # instead of logging it as satisfied and silently losing it.
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
                    multipv=multipv_for(generation, floor))
            else:
                follow_up.budget_nodes = max(follow_up.budget_nodes, floor)
                follow_up.source = 'USER'
                follow_up.save(update_fields=['budget_nodes', 'source'])
            return 'queued'
        # The existing lease already satisfies the requested rung. Mark it
        # as visitor-requested so subsequent clicks can be deduplicated
        # without consuming the hourly allowance repeatedly.
        if task.source != 'USER':
            task.source = 'USER'
            task.save(update_fields=['source'])
    return 'already-queued'


def _frontier_rank(child, stm_white):
    """Explorer convention: an unanalysed child outranks a known bad one.

    "Unanalysed" has to mean no knowledge at all, not merely no eval of its
    own: a child whose subtree already backed a value IS informed, and ranking
    it as a blank sends visitor clicks at lines the tree has already judged.
    """
    known = best_known_eval(child)
    if known is None:
        return -9_999.5
    return known if stm_white else -known


def _frontier_children(parent, limit=None):
    """Unsolved children worth buying next, best first and already sliced.

    Ordering, by decreasing trust:

    1. the parent's stored MultiPV (``last_analysis``) — the engine's own
       ranking of the moves it just searched, emitted best first;
    2. what the tree already knows about each child (eval from the mover's
       point of view, the same convention the explorer's move table uses);
    3. nothing at all, in which case the first FRONTIER_BLIND_WIDTH legal
       moves stand in for the ordering we do not have.

    Solved children never take a slot: an OR node cannot be proven through
    them and an AND node has nothing left to ask of them.  ``limit`` is what
    a descending click has left of its budget, so an entire descent still
    fits inside one FRONTIER_CLICK_CAP allowance.
    """
    stm_white = parent.fen.split()[1] == 'w'
    # order_by('id') keeps the movegen order expand() wrote the edges in.
    edges = [e for e in Edge.objects.filter(parent=parent)
             .select_related('child').order_by('id')
             if e.child.status == 'UNKNOWN']
    live = [e.child for e in edges]
    by_move = {e.move_uci: e.child for e in edges}

    ranked, seen = [], set()
    for line in (parent.last_analysis or []):
        move = line.get('move') if isinstance(line, dict) else None
        child = by_move.get(move)
        if child is not None and child.key not in seen:
            seen.add(child.key)
            ranked.append(child)
    rest = [c for c in live if c.key not in seen]
    informed = bool(ranked) or any(c.eval_cp is not None for c in rest)
    # Stable sort over legal-move order: with no eval at all the fallback is
    # literally "the first N legal moves".
    rest.sort(key=lambda c: -_frontier_rank(c, stm_white))
    ranked.extend(rest)

    if stm_white:     # OR node: one good try is enough to prove the branch
        width = FRONTIER_OR_WIDTH if informed else FRONTIER_BLIND_WIDTH
    else:             # AND node: every reply has to be answered
        width = FRONTIER_AND_CAP
    width = min(width, FRONTIER_CLICK_CAP)
    if limit is not None:
        width = min(width, max(0, limit))
    return ranked[:width]


def _ladder_spent_keys(children):
    """Which of these children have nothing left to buy, in one aggregate.

    The descent probes a whole level at a time and only buys at the level
    where it stops, so calling ``_request_rung`` child by child on the way
    down would put thousands of statements inside a single click's write
    transaction to learn what one grouped query already says.
    """
    keys = [child.key for child in children]
    if not keys:
        return set()
    top_rung = REQUEST_BUDGET_LADDER[-1]
    rows = (AnalysisTask.objects
            .filter(position_id__in=keys,
                    state=AnalysisTask.TState.COMPLETED)
            .values('position_id')
            .annotate(top=Max('budget_nodes')))
    return {row['position_id'] for row in rows
            if row['top'] is not None and row['top'] >= top_rung}


def _ensure_expanded(pos):
    """Materialise the legal edges of a level before anyone reads it.

    A ladder can be spent on a position whose edges were never written (a
    task completed without its analysis ever being ingested), and both the
    descent probe and the frontier selection read edges before deciding
    anything, so the level has to exist first.  Returns the locked row.
    """
    if pos.expanded or pos.status != 'UNKNOWN':
        return pos
    pos = Position.objects.select_for_update().get(pk=pos.pk)
    expand(pos)
    return pos


def _expand_frontier(parent, limit=None):
    """Spend an exhausted request one ply deeper, proof-number style.

    Every selected child re-enters the ordinary ladder at its own natural
    floor (typically 128M).  A child whose own ladder is already spent is
    counted and left alone here; following it is the descent's job, and only
    when EVERY candidate at this level is spent.
    """
    parent = Position.objects.select_for_update().get(pk=parent.pk)
    expand(parent)   # no-op once the legal edges already exist
    counts = {'children_considered': 0, 'children_queued': 0,
              'children_solved': 0, 'children_exhausted': 0}
    for child in _frontier_children(parent, limit=limit):
        counts['children_considered'] += 1
        outcome = _request_rung(child)
        if outcome == _LADDER_EXHAUSTED:
            counts['children_exhausted'] += 1
        elif outcome == 'already-solved':
            counts['children_solved'] += 1
        else:
            counts['children_queued'] += 1
    return counts


def _descent_outcome(status, stop, node, plies, totals):
    """One payload shape for both endings, so the UI never branches on keys."""
    return RequestOutcome(status, descent_plies=plies, descent_key=node.key,
                          descent_stop=stop, **totals)


def _descend_frontier(pos):
    """Follow the spent line down until the click finds something to buy.

    Proof-number search does not give up on a node whose whole frontier is
    already searched: it walks to the most-proving node and grows the tree
    there.  This is the same move.  While every candidate at the current
    level has a spent ladder there is nothing left to buy here, so the
    request follows the single most promising one and asks the same question
    one ply lower:

      * an OR node (the attacker to move) only needs one good try, so the
        best eval leads;
      * an AND node (the defender to move) must survive every reply, so the
        unsolved child that is best FOR THE DEFENDER leads — the answer that
        is hardest to refute.

    Both are the same sentence in ``_frontier_children`` order, which ranks
    by the mover's own point of view.  Solved nodes are never followed: there
    is no question left to ask of them.  The visited set keeps a
    transposition cycle from looping (1.Nf3 Nf6 2.Ng1 Ng8 IS the start
    position once the counters are stripped) and the whole descent shares one
    FRONTIER_CLICK_CAP budget plus one hard ply guard.

    The way down writes nothing but the edges a level needs to be readable at
    all: a descent that finds everything reachable spent or solved answers
    'saturated' and has bought nothing.
    """
    totals = {'children_considered': 0, 'children_queued': 0,
              'children_solved': 0, 'children_exhausted': 0}
    visited = {pos.key}
    node, plies = pos, 0
    while True:
        remaining = FRONTIER_CLICK_CAP - totals['children_queued']
        if remaining <= 0:
            return _descent_outcome('saturated', 'budget-spent', node,
                                    plies, totals)
        node = _ensure_expanded(node)
        children = _frontier_children(node, limit=remaining)
        spent = _ladder_spent_keys(children)
        if len(spent) < len(children):
            for name, value in _expand_frontier(node, limit=remaining).items():
                totals[name] += value
            return _descent_outcome('expanded', 'queued', node, plies, totals)
        # Every candidate here is spent: charge the level and step down.
        totals['children_considered'] += len(children)
        totals['children_exhausted'] += len(children)
        following = next((c for c in children if c.key not in visited), None)
        if following is None:
            return _descent_outcome('saturated', 'no-candidate', node,
                                    plies, totals)
        if plies >= FRONTIER_DESCENT_MAX_PLIES:
            return _descent_outcome('saturated', 'depth-guard', node,
                                    plies, totals)
        visited.add(following.key)
        node, plies = following, plies + 1


def request_analysis(pos):
    """Peticion publica: encola (o promociona) la tarea de esta posicion.
    Suelo de 128M: quien pide analisis merece profundidad de verdad.

    Agotada la escalera (10B ya COMPLETED), repetir el peldano no compra
    informacion nueva: la peticion se convierte en expansion de frontera un
    ply mas abajo (estilo proof-number search), y si esa frontera tambien
    esta agotada el click DESCIENDE por el hijo mas prometedor hasta
    encontrar trabajo o declararse 'saturated'.
    Devuelve 'queued' | 'already-queued' | 'already-solved' | 'expanded'
    | 'saturated'."""
    with atomic():
        outcome = _request_rung(pos)
        if outcome != _LADDER_EXHAUSTED:
            return RequestOutcome(outcome)
        return _descend_frontier(pos)


def _queue_disputed_reanalysis(pos):
    """Queue one maximum-budget follow-up without disturbing live leases."""
    pending = (AnalysisTask.objects.filter(position=pos, state='PENDING')
               .order_by('-generation').first())
    if pending is not None:
        # Un reanalisis por testigo refutado va a presupuesto maximo, asi
        # que quiere PROFUNDIDAD: es exactamente el caso en el que la anchura
        # ya demostro no estar viendo el fondo.
        pending.budget_nodes = max(pending.budget_nodes, BUDGET_LADDER[-1])
        pending.multipv = multipv_for(pos.visits, pending.budget_nodes)
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
        multipv=multipv_for(pos.visits, BUDGET_LADDER[-1]), source='AUTO')


# ---------------- cierre por certificado SOLVE ----------------
#
# Un ``PROVED`` de un voluntario no es una prueba.  Lo que hace solido un
# solver distribuido es que el hecho exacto entra SOLO despues de que el
# servidor haya reproducido la estrategia entera, con OTRA implementacion de
# las reglas (pyffish aqui, el movegen del motor alli).  Un certificado
# rechazado no muta el arbol: deja evento y la tarea en FAILED.

def apply_solve_result(task, outcome, certificate_blob=None, advisory_pn=None,
                       advisory_dn=None, searched_nodes=0, elapsed_seconds=0,
                       solver_build='', telemetry=None,
                       trusted_submitter=False):
    """Aplica un resultado SOLVE. Devuelve el resumen que ve el worker."""
    from . import solve
    from .models import SolveTask

    disputed = False

    try:
        elapsed = max(0.0, min(float(elapsed_seconds or 0), 86_400.0))
    except (TypeError, ValueError):
        elapsed = 0.0

    verified, report, reason = False, None, ''
    verify_seconds = 0.0
    if outcome == 'PROVED':
        if not certificate_blob:
            reason = 'PROVED without a certificate'
        else:
            started = time.monotonic()
            try:
                text = solve.decompress(certificate_blob)
                report = solve.verify_certificate(
                    text, root_fen=task.position.fen, goal=task.goal)
                verified = True
            except solve.CertificateError as error:
                reason = str(error)
            except Exception as error:          # movegen/parse surprises
                reason = f'{type(error).__name__}: {error}'
            verify_seconds = time.monotonic() - started

    with atomic():
        current = SolveTask.objects.select_for_update().get(pk=task.pk)
        if current.state == 'COMPLETED':
            return {'dup': True}
        current.outcome = outcome
        current.advisory_pn = advisory_pn
        current.advisory_dn = advisory_dn
        current.searched_nodes = max(0, int(searched_nodes or 0))
        current.elapsed_seconds = elapsed
        current.solver_build = (solver_build or '')[:64]
        if telemetry:
            current.telemetry = telemetry
        current.completed = timezone.now()
        if outcome == 'PROVED' and not verified:
            current.state = 'FAILED'
            current.verified = False
            current.reject_reason = reason[:2000]
            current.save()
            DBEvent.objects.create(kind='SOLVE_REJECTED', payload={
                'task': current.pk, 'key': current.position_id,
                'reason': reason[:500], 'machine': current.machine})
            return {'rejected': True, 'reason': reason[:200]}

        current.state = 'COMPLETED'
        current.verified = verified
        current.reject_reason = ''
        if verified:
            current.certificate = certificate_blob
            current.certificate_bytes = len(certificate_blob)
            current.certificate_nodes = report['nodes']
        current.save()

        closed = upgraded = False
        if verified:
            # The verifier's own cost is a pilot gate, so it is measured
            # rather than assumed: a proof-carrying design is only worth it
            # while checking stays much cheaper than searching.
            DBEvent.objects.create(kind='SOLVE_VERIFIED', payload={
                'task': current.pk, 'key': current.position_id,
                'seconds': round(verify_seconds, 4),
                'nodes': report['nodes'], 'depth': report['depth'],
                'bytes': current.certificate_bytes,
                'solver_seconds': elapsed})
            # Una posicion ABIERTA se cierra; una ya cerrada (la deuda
            # ENGINE) solo sube de grado.  Son dos cosas distintas y ninguna
            # de las dos debe hacer el trabajo de la otra.
            if Position.objects.filter(key=current.position_id,
                                       status='UNKNOWN').exists():
                closed = _close_by_certificate(current, report)
            else:
                upgraded = _upgrade_by_certificate(current, report)
        elif outcome == 'DISPROVED':
            disputed = _dispute_from_solver(current, trusted_submitter)
    if verified or disputed:
        backup_cascade([task.position_id])
        backup_backed_evals([task.position_id])
    return {'verified': verified, 'closed': closed, 'upgraded': upgraded,
            'disputed': disputed,
            'certificate_nodes': (report or {}).get('nodes', 0)}


# Presupuesto del peldano F0 del doc 18: df-pn tactico + telemetria de
# fortaleza.  Certifica un mate tipico en menos de quince segundos, que es lo
# que hace viable pasarle la deuda entera a la flota.
DEBT_STAGE_NODES = 2_000_000
# Tope de cola SOLVE pendiente. Encolar 28.000 tareas de golpe no acelera
# nada: solo entierra las peticiones y el piloto bajo una montana de deuda.
DEBT_QUEUE_CAP = 500
DEBT_ARM = 'debt'


def enqueue_engine_debt(cap=DEBT_QUEUE_CAP, limit=None):
    """Pone la deuda de mates SIN CERTIFICAR en manos de la flota.

    QUE ES LA DEUDA.  Un cierre ``MATE_PV`` con ``proof='ENGINE'`` es un
    testigo legal cuyo certificado exhaustivo nunca se produjo: la busqueda
    online se quedo sin presupuesto.  Son verdad "probablemente", viven en la
    cascada exacta como si fueran verdad "seguro", y con la flota cerrando
    mates a este ritmo la deuda crece mas rapido de lo que un cron nocturno la
    absorbe.  Un df-pn a 2M nodos la certifica en segundos.

    BACKPRESSURE.  Solo se rellena hasta ``cap`` tareas SOLVE pendientes en
    total, contando las de todo el mundo: la deuda es importante pero NUNCA
    urgente, y una peticion de visitante o un brazo del piloto siempre van
    delante (ver el orden de ``api_solve_acquire``, que manda ``arm='debt'``
    al final).

    Devuelve el numero de tareas creadas.
    """
    from .models import SolveTask

    pending = SolveTask.objects.filter(state='PENDING').count()
    room = max(0, int(cap) - pending)
    if limit is not None:
        room = min(room, int(limit))
    if room <= 0:
        return 0

    # Ya tienen tarea viva o certificado: no se re-encolan.
    taken = set(SolveTask.objects.filter(
        arm=DEBT_ARM,
        state__in=('PENDING', 'LEASED', 'COMPLETED'),
    ).values_list('position_id', flat=True))

    candidates = Position.objects.filter(
        closure='MATE_PV', status__in=('WHITE_WIN', 'BLACK_WIN'),
        won_line__isnull=False,
    ).exclude(won_line='').filter(
        Q(proof='ENGINE') | Q(proof__isnull=True)
    ).exclude(key__in=taken).order_by('key')

    made = []
    for position in candidates[:room * 2]:
        if len(made) >= room:
            break
        if position.key in taken:
            continue
        taken.add(position.key)
        made.append(SolveTask(
            position=position, goal=position.status,
            budget_stage='F0', budget_nodes=DEBT_STAGE_NODES,
            arm=DEBT_ARM))
    if not made:
        return 0
    SolveTask.objects.bulk_create(made, ignore_conflicts=True)
    DBEvent.objects.create(kind='DEBT_ENQUEUED', payload={
        'created': len(made), 'pending_before': pending, 'cap': int(cap)})
    return len(made)


# ---------------- completado de cobertura ----------------
#
# EL CASO QUE LO PIDIO (Wolfram, 28-jul).  Un nodo con negras al turno tenia
# doce respuestas: nueve WHITE_WIN ya probadas (≤M8..M15), dos evaluadas en
# -1300 para el que mueve... y UNA sin analizar.  La guarda de cobertura hizo
# lo correcto — con una respuesta sin mirar, el minimax parcial es optimista y
# no se promueve — asi que el nodo siguio publicando su vieja eval de -150.
# Cuando un humano pidio a mano esa unica jugada y salio mate, la cobertura se
# completo y el valor salto a +1261 y subio al padre.
#
# El sistema sabia EXACTAMENTE lo que le faltaba y no hizo nada al respecto.
# Eso es lo que arregla esto: si un nodo esta a K jugadas de tener la verdad
# entera, se piden esas K jugadas.
COVERAGE_MISSING_MAX = 3
COVERAGE_DECISIVE_CP = 800
COVERAGE_QUEUE_CAP = 200
COVERAGE_SCAN_ROWS = 2_000
COVERAGE_SEED_NODES = 8_000_000


def _coverage_children(parent_keys):
    rows = Edge.objects.filter(parent_id__in=list(parent_keys)).values_list(
        'parent_id', 'move_uci', 'child_id', 'child__status',
        'child__eval_cp', 'child__backed_eval')
    by_parent = {}
    for parent_id, move, child_id, status, eval_cp, backed in rows:
        known = backed if backed is not None else eval_cp
        by_parent.setdefault(parent_id, []).append(
            (move, child_id, status, known))
    return by_parent


def _coverage_gap(fen, children, missing_max):
    """Las jugadas que faltan para tener la verdad entera, o ``None``.

    El gate es deliberadamente estrecho.  No basta con que falten pocas
    jugadas: lo YA MIRADO tiene que ser unilateral EN CONTRA del que mueve —
    todo cerrado a favor del rival o evaluado por debajo de
    ``-COVERAGE_DECISIVE_CP`` desde su punto de vista.  Solo entonces las
    jugadas que faltan son la diferencia entre "no sabemos" y un cierre
    MINIMAX completo, que es lo que hace que valga la pena gastar en ellas.

    Un nodo equilibrado no entra: ahi las jugadas que faltan no deciden nada y
    pedirlas seria exploracion normal disfrazada de prioridad.
    """
    stm_white = fen.split()[1] == 'w'
    mover_win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    mover_loss = 'BLACK_WIN' if stm_white else 'WHITE_WIN'
    missing, informed = [], 0
    for move, child_id, status, known in children:
        if status == mover_win:
            return None          # ya hay una ganadora: no es este caso
        if status == mover_loss:
            informed += 1
            continue
        if status == 'DRAW':
            return None          # unas tablas exactas rompen la unilateralidad
        if known is None:
            missing.append((move, child_id))
            continue
        mover_value = known if stm_white else -known
        if mover_value > -COVERAGE_DECISIVE_CP:
            return None          # algo que no pinta mal: no es unilateral
        informed += 1
    if not missing or len(missing) > missing_max or not informed:
        return None
    return missing


def enqueue_coverage_completion(cap=COVERAGE_QUEUE_CAP,
                                missing_max=COVERAGE_MISSING_MAX,
                                scan=COVERAGE_SCAN_ROWS):
    """Pide las pocas jugadas que le faltan a un nodo para cerrarse.

    Se escanea por ``updated`` descendente y con tope: la cobertura nueva
    aparece justo donde algo acaba de cambiar, asi que mirar lo mas reciente
    es a la vez lo mas barato y lo mas productivo.  El tope global de la
    politica se cuenta sobre ``source='FILL'``, que existe para esto.
    """
    pending = AnalysisTask.objects.filter(
        state='PENDING', source=AnalysisTask.Source.FILL).count()
    room = max(0, int(cap) - pending)
    if room <= 0:
        return 0

    candidates = list(Position.objects.filter(
        status='UNKNOWN', expanded=True,
    ).order_by('-updated').values_list('key', 'fen')[:scan])
    if not candidates:
        return 0
    children = _coverage_children([key for key, _fen in candidates])

    made = 0
    for key, fen in candidates:
        if made >= room:
            break
        gap = _coverage_gap(fen, children.get(key, ()), missing_max)
        if not gap:
            continue
        for _move, child_id in gap:
            if made >= room:
                break
            child = Position.objects.filter(key=child_id).first()
            if child is None or child.status != 'UNKNOWN':
                continue
            task, created = AnalysisTask.objects.get_or_create(
                position_id=child_id, generation=child.visits,
                defaults={'budget_nodes': max(COVERAGE_SEED_NODES,
                                              budget_for(child)),
                          'multipv': DEPTH_MULTIPV,
                          'source': AnalysisTask.Source.FILL})
            if created:
                made += 1
            elif task.state == 'PENDING' \
                    and task.source == AnalysisTask.Source.AUTO:
                # Ya estaba en la cola normal: se promociona sin gastar cupo
                # nuevo, porque la tarea ya existia.
                task.source = AnalysisTask.Source.FILL
                task.save(update_fields=['source'])
    if made:
        DBEvent.objects.create(kind='COVERAGE_ENQUEUED', payload={
            'created': made, 'pending_before': pending, 'cap': int(cap),
            'scanned': len(candidates)})
    return made


# Un click de visitante pide COMO MUCHO esto.  El completado automatico de
# cobertura se limita a tres jugadas porque busca cerrar un nodo; esto es otra
# cosa — "mirad todo lo que aqui no se ha mirado" — y sesenta y cuatro es una
# expansion generosa que sigue siendo una sola decision humana.
UNEXPLORED_CLICK_CAP = 64


def unexplored_children(pos):
    """Hijos materializados sobre los que el arbol no sabe NADA todavia.

    Ni status, ni respaldo, ni eval propia.  Un hijo con respaldo pero sin
    eval propia NO cuenta: de ese ya sabemos algo, y este boton existe para
    los huecos, no para re-pedir lo que ya tiene valor.
    """
    return [edge.child for edge in
            Edge.objects.filter(parent=pos).select_related('child')
            .order_by('id')
            if edge.child.status == 'UNKNOWN'
            and edge.child.eval_cp is None
            and edge.child.backed_eval is None]


def enqueue_unexplored_children(pos, cap=UNEXPLORED_CLICK_CAP,
                                source=AnalysisTask.Source.USER):
    """Encola las jugadas sin mirar de ``pos``. Devuelve cuantas se encolaron.

    Es ``enqueue_coverage_completion`` sin su guarda de unilateralidad: alli
    el sistema decide que un nodo esta a punto de cerrarse, aqui lo decide una
    persona que esta mirando la pagina.  Lo que si comparte es el dedup: una
    jugada que ya tiene tarea viva no gasta cupo ni crea una segunda.
    """
    queued = 0
    for child in unexplored_children(pos):
        if queued >= cap:
            break
        task, created = AnalysisTask.objects.get_or_create(
            position=child, generation=child.visits,
            defaults={'budget_nodes': max(COVERAGE_SEED_NODES,
                                          budget_for(child)),
                      'multipv': multipv_for(child.visits),
                      'source': source})
        if created:
            queued += 1
        elif task.state == 'PENDING' and task.source != source:
            task.source = source
            task.save(update_fields=['source'])
            queued += 1
    return queued


def _upgrade_by_certificate(task, report):
    """El cierre YA existe: esto sube su GRADO DE PRUEBA, no lo re-cierra.

    Es el camino de la deuda ENGINE.  El status no se toca — ya era ese — y
    tampoco el ``closure``: un ``MATE_PV`` certificado sigue siendo un
    MATE_PV, exactamente lo que produce ``verify_mates`` cuando le alcanza el
    presupuesto.  Lo que cambia es que deja de ser un testigo sin certificar.

    El slack se queda con el MEJOR de los dos: el almacenado venia de la cota
    burda ``100 - len(testigo)``, y el del certificado esta medido sobre la
    racha reversible real del arbol probado.
    """
    pos = Position.objects.select_for_update().get(key=task.position_id)
    if pos.status != task.goal or pos.status == 'UNKNOWN':
        return False
    if pos.proof == 'ANDOR':
        return False                       # ya estaba certificado
    pos.proof = 'ANDOR'
    certified = report.get('clock_slack')
    if certified is not None:
        pos.clock_slack = max(pos.clock_slack or 0, certified)
    pos.save(update_fields=['proof', 'clock_slack', 'updated'])
    DBEvent.objects.create(kind='PROOF_UPGRADED', payload={
        'key': pos.key, 'status': pos.status, 'closure': pos.closure,
        'certificate_nodes': report['nodes'],
        'clock_slack': pos.clock_slack, 'task': task.pk})
    return True


def _dispute_from_solver(task, trusted):
    """Un DISPROVED del solver sobre una posicion cerrada CON ESE MISMO status.

    El solver dice, con busqueda exhaustiva, que el objetivo NO se puede
    forzar; el arbol dice que si.  Uno de los dos miente y el arbol solo tiene
    un testigo sin certificar, asi que la sospecha es seria.

    Pero un DISPROVED NO trae certificado — mi solver solo los emite para
    PROVED — asi que es una afirmacion sin reproducir.  Aceptarla de
    cualquiera seria darle a un voluntario un boton para borrar cierres.  De
    modo que:

      * de una identidad de CONFIANZA (la misma puerta que los TB de seis
        piezas): revoca, porque ahi el operador responde por su maquina;
      * de cualquier otra: se registra la sospecha y se encola la
        re-certificacion servidor-side, que SI reproduce la refutacion antes
        de tocar nada.

    En los dos casos queda evento: la deuda sospechosa se ve.
    """
    pos = Position.objects.select_for_update().get(key=task.position_id)
    if pos.status != task.goal or pos.status == 'UNKNOWN':
        return False
    if _closure_is_independent(pos):
        return False                       # TERMINAL/TB/ANDOR: no se discute
    DBEvent.objects.create(kind='SOLVE_DISPUTE_SIGNAL', payload={
        'key': pos.key, 'status': pos.status, 'closure': pos.closure,
        'proof': pos.proof, 'task': task.pk, 'trusted': bool(trusted),
        'machine': task.machine})
    if not trusted:
        return False
    revoke_closure(pos.key, reason='solver-disproved-the-goal',
                   mark_disputed=True)
    return True


def _close_by_certificate(task, report):
    """Cierra la posicion con ``closure='SOLVE'`` y el slack del certificado."""
    pos = Position.objects.select_for_update().get(key=task.position_id)
    if pos.status != 'UNKNOWN':
        return False
    pos.status = task.goal
    pos.closure = 'SOLVE'
    pos.proof = 'ANDOR'          # reproducido entero, no un testigo
    pos.clock_slack = report.get('clock_slack')
    pos.save(update_fields=['status', 'closure', 'proof', 'clock_slack',
                            'updated'])
    DBEvent.objects.create(kind='NODE_CLOSED', payload={
        'key': pos.key, 'status': pos.status, 'closure': 'SOLVE',
        'certificate_nodes': report['nodes'], 'depth': report['depth'],
        'clock_slack': report.get('clock_slack'), 'task': task.pk})
    _emit_closure_events(pos)
    return True


def _tb_rejected(position_key, reason, **payload):
    DBEvent.objects.create(kind='TB_REJECTED', payload={
        'key': position_key, 'reason': reason, **payload})


def prepare_tb_closure(position_key, wdl, user=None, dtz=None):
    """Validate and, for <=5 men, probe TB before any encompassing write tx.

    ``dtz`` es ADITIVO y opcional: un worker anterior a este protocolo no
    lo manda y el cierre sigue exactamente igual, con ``clock_slack`` 0 —
    que es lo unico honesto, porque un WDL a secas asume haberse
    alcanzado justo tras un reset.  El WDL se sigue re-verificando en el
    servidor hasta cinco piezas; el DTZ se ACEPTA SIN VERIFICAR y queda
    marcado como tal en el evento.  La verificacion dura del DTZ llega
    con los certificados de P1c.
    """
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
    try:
        dtz = None if dtz is None else int(dtz)
    except (TypeError, ValueError):
        dtz = None
    return {'key': pos.key, 'fen': pos.fen, 'wdl': wdl, 'dtz': dtz}


def _apply_prepared_tb(position_key, prepared):
    """Apply a server-validated TB result inside the caller's transaction."""
    if prepared is None or prepared.get('key') != position_key:
        return False
    with atomic():
        pos = Position.objects.select_for_update().get(key=position_key)
        if (pos.status != 'UNKNOWN' or pos.fen != prepared.get('fen')
                or not logic.tb_applicable(pos.fen)):
            return False
        wdl = prepared['wdl']
        dtz = prepared.get('dtz')
        stm_white = pos.fen.split()[1] == 'w'
        pos.status = logic.wdl_to_status(wdl, stm_white)
        pos.closure = 'TB'
        pos.clock_slack = (None if pos.status == 'DRAW'
                           else logic.slack_from_dtz(dtz))
        pos.save(update_fields=['status', 'closure', 'clock_slack',
                                'updated'])
        DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': pos.key, 'status': pos.status, 'closure': 'TB',
            'dtz': dtz, 'dtz_verified': False,
            'clock_slack': pos.clock_slack})
    backup_cascade([position_key])
    backup_backed_evals([position_key])
    return True


def close_by_tb(position_key, wdl, user=None, dtz=None):
    """Cierra con WDL del lado al turno, dentro de una frontera verificable.

    Hasta cinco piezas el servidor repite siempre el probe con su set Atomic
    fijado. Las posiciones de seis piezas solo se aceptan de identidades
    explicitamente confiables porque el set completo no reside en el VPS.
    Todo rechazo queda registrado y nunca muta el arbol.
    """
    prepared = prepare_tb_closure(position_key, wdl, user=user, dtz=dtz)
    return _apply_prepared_tb(position_key, prepared)
