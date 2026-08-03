"""Ingesta y backup de AtomicDB. Unico punto de escritura del arbol.

Flujo por resultado de analisis (§2):
  upsert de hijos (expansion COMPLETA con movegen propio) -> cierres locales
  (terminal / MATE_PV) -> backup minimax en cascada hacia arriba -> eventos.
"""

import contextlib
import contextvars
import functools
import heapq
import math
import time

from django.conf import settings
from django.db.models import Max, Q
from django.utils import timezone

from . import logic, proof, solve_estimate, tb
from .database import atomic
from .models import AnalysisTask, Campaign, DBEvent, Edge, Position

# Sondas profundas estilo chessdb.cn: sin TT persistente entre visitas, la
# profundidad se compra por sonda; evals fiables valen mas que anchura barata.
BUDGET_LADDER = [8_000_000, 32_000_000, 128_000_000, 512_000_000,
                 2_000_000_000]
# Visitor-requested reanalysis is deliberately steeper than autonomous tree
# exploration: 128M -> 512M -> 2B -> 10B.  El primer peldano SIGUE a la
# capacidad donada (orden del propietario): 512M mientras las cajas de 288
# cores masticaban sondas en segundos (28-jul), de vuelta a 128M cuando la
# flota se fue (29-jul).  Si vuelve capacidad grande, subirlo es cambiar
# esta lista y nada mas: el suelo del boton masivo y los tests siguen a
# REQUEST_BUDGET_LADDER[0] mecanicamente.
REQUEST_BUDGET_LADDER = [128_000_000, 512_000_000, 2_000_000_000,
                         10_000_000_000]
# Suelo para AVISAR a quien pidio un analisis.  Sigue al primer peldano de la
# escalera de peticiones — hoy 128M — porque es lo que define "esto lo pidio
# una persona": todo lo que sale de un click entra por ahi o por encima
# (``_request_rung``, la expansion de la frontera y el boton de jugadas sin
# mirar usan los tres el mismo suelo).
NOTIFY_MIN_BUDGET = REQUEST_BUDGET_LADDER[0]
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
# ---------------- mate CORTO: verificacion, no excavacion ----------------
#
# LA UNIDAD, PRIMERO, porque aqui se mezclan dos.  ``Position.mate_in`` cuenta
# PLIES (medias jugadas): es la LONGITUD del testigo guardado en ``won_line``
# — los tres sitios que lo escriben lo hacen como ``len(pv_rest)`` — y un nodo
# TERMINAL vale 0.  El MOTOR, en cambio, habla en JUGADAS: el ``score mate N``
# de UCI, que el worker convierte a ``eval_cp = (10_000 - |N|) * signo``
# (``Client/atomicdb_worker.py``).  Todo lo que sigue razona en PLIES y hace
# esa conversion una sola vez, en ``_mate_moves_to_plies``.
#
# EL DESPERDICIO.  ``budget_for`` sube el presupuesto a 128M en cuanto |eval|
# entra en la banda de mate, "para extraer la PV entera".  Para un mate LARGO
# es exactamente lo que hay que comprar.  Para un M2 es absurdo: el motor lo ve
# en unos miles de nodos, el worker manda ``go nodes N`` sin parada temprana
# (no hay forma de decirle "para cuando lo tengas") y el resto del minuto se
# gasta en alternativas que no deciden nada.
#
# LO QUE SE COMPRA EN SU LUGAR: una VERIFICACION.  Presupuesto proporcional a
# la distancia y UNA sola linea, porque para cerrar un nodo OR ganador basta la
# linea ganadora — un cierre ``MATE_PV`` no usa la segunda opinion de
# ordenacion para nada.  Y si la reclamacion era falsa, no se pierde: el
# testigo refutado ya re-arma la revisita profunda por su cuenta
# (``_revoke_contradicted_mate`` -> ``_queue_disputed_reanalysis``, y la deuda
# F0 de la flota), maquinaria que este clamp no toca.
#
# NI LOS MATES LARGOS NI LA BANDA SIN DISTANCIA SE MUEVEN.  Solo se clampa una
# distancia CONOCIDA y corta.  Un cp de tablebase entra en la banda recortado a
# +-9_500 por el worker y a proposito "sin fingir distancia de mate":
# decodificarlo daria cientos de jugadas, asi que no pasa el umbral y conserva
# su 128M.  Un ``backed_eval`` de banda caminado por un visitante tampoco tiene
# distancia, y ``budget_for`` ni lo mira.
MATE_CLAMP_PLIES = 6            # hasta M3 contando el ply del defensor
MATE_CLAMP_FLOOR = 2_000_000    # el F0 con el que el solver contesta esto
MATE_CLAMP_PER_PLY = 4_000_000
MATE_CLAMP_CAP = 32_000_000
MATE_CLAMP_MULTIPV = 1
# Techo del recorte que el worker aplica a los cp de tablebase.  Por ENCIMA de
# el, un |eval_cp| solo puede venir de la formula de mate.
TB_CLAMP_CEILING = 9_500
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


def _mate_moves_to_plies(moves, winner_white, stm_white):
    """``score mate N`` de UCI (JUGADAS) -> plies hasta el mate.

    El que manda es el bando al turno: entrega el mate en su jugada N, o sea
    en el ply ``2N-1``.  Si el que gana es el otro, el mate cae en la jugada N
    del defensor y el turno actual no cuenta: ``2N`` plies.  Es la misma
    aritmetica que ya documenta la cascada ("4 plies, es decir mate en 3").
    """
    if moves <= 0:
        return 0
    return 2 * moves - 1 if winner_white == stm_white else 2 * moves


def claimed_mate_plies(pos):
    """Plies hasta el mate que ``pos`` RECLAMA, o ``None`` si no se sabe.

    Tres fuentes, de la mas explicita a la mas indirecta, y ninguna inventa un
    numero que no este escrito:

    1. ``mate_in``, que ya viene en plies.
    2. La distancia DECLARADA por el motor en la linea que encabeza
       ``last_analysis``: el campo ``mate`` es el ``score mate N`` de UCI tal
       cual, sin decodificar nada.  Se salta las lineas de un pase ANTERIOR
       (``prior_pass``), que no son el veredicto vigente.
    3. ``eval_cp`` decodificado, y solo por ENCIMA del recorte de tablebase:
       ahi el valor solo puede venir de la formula de mate del worker.  Es la
       fuente que cubre el caso frecuente — un hijo SEMBRADO desde la linea
       MultiPV del padre (``_seed_child_eval``) tiene ``eval_cp`` y nada mas.

    Un ``mate`` de la banda pero sin distancia (cp de tablebase recortado)
    cae por el suelo de las tres y devuelve ``None``, que es lo que hace que
    conserve el comportamiento de siempre.
    """
    if pos.mate_in is not None:
        return max(0, int(pos.mate_in))
    stm_white = pos.fen.split()[1] == 'w'
    for line in (pos.last_analysis or []):
        if not isinstance(line, dict) or line.get('prior_pass'):
            continue
        mate = line.get('mate')
        if mate:
            return _mate_moves_to_plies(abs(int(mate)), int(mate) > 0,
                                        stm_white)
        break            # la linea vigente no reclama mate: no hay distancia
    eval_cp = pos.eval_cp
    # Franja ABIERTA por los dos lados.  Por abajo, el recorte de tablebase.
    # Por arriba, +-10_000 exacto, que en este arbol significa "ganado" sin
    # distancia declarada — es el valor con el que la cascada respalda una
    # victoria probada — y no un mate en cero jugadas.
    if eval_cp is not None and TB_CLAMP_CEILING < abs(eval_cp) < 10_000:
        return _mate_moves_to_plies(10_000 - abs(eval_cp), eval_cp > 0,
                                    stm_white)
    return None


def _short_mate_clamp(pos):
    """``(budget_nodes, multipv)`` de la verificacion barata, o ``None``.

    ``None`` significa "aqui no manda esta politica": o no hay distancia
    conocida, o el mate es lo bastante largo como para que extraer la PV
    entera siga siendo lo que hay que comprar.  Es el UNICO sitio donde se
    decide que cuenta como mate corto; los sitios que encolan solo consultan.
    """
    plies = claimed_mate_plies(pos)
    if plies is None or plies > MATE_CLAMP_PLIES:
        return None
    return (min(MATE_CLAMP_CAP,
                max(MATE_CLAMP_FLOOR, MATE_CLAMP_PER_PLY * plies)),
            MATE_CLAMP_MULTIPV)


def multipv_for(visits, budget_nodes=None, seeding=False, clamp=None):
    """Cuantas variantes pedirle al motor, segun para que es este analisis.

    Medido (caso de la comunidad, 28-jul): con el MISMO presupuesto de nodos,
    MultiPV 5 llego a profundidad 18 y dijo "negras aguantan" (-89); MultiPV 1
    llego a 23 y dijo "negras perdidas" (-901).  A partir de cierto peldano la
    anchura no compra ordenacion, compra ruido caro.

    * SEMBRANDO (``bootstrap_root``): 5.  Ahi la anchura ES el producto — se
      esta ordenando un nivel entero por primera vez.
    * PRIMERA VISITA: 5, a cualquier presupuesto.  Desde que el primer
      peldano de peticion es 512M (28-jul), la regla de presupuesto pisaba a
      la de primera mirada — y una primera mirada existe para lo mismo que la
      siembra: informar un nivel entero de hijos.  Profundidad sin anchura en
      un nodo virgen deja al arbol dos hijos informados y nada que comparar.
    * REVISITA CON PRESUPUESTO ALTO: 2.  Dos, no una, para conservar una
      segunda opinion de ordenacion.
    * VERIFICACION DE MATE CORTO (``clamp`` de ``_short_mate_clamp``): 1.
      Aqui no se esta ordenando nada — se esta comprobando UNA linea que el
      motor ya afirma — y la segunda opinion cuesta la mitad del presupuesto
      de un nodo que se cierra con la primera.  Solo manda si el presupuesto
      que se va a comprar es de verdad el del clamp: cuando la escalera por
      visitas ya pide mas, esto es una revisita normal y la anchura vuelve a
      la politica de la casa.
    * El resto: la politica por visitas de siempre, 5 al empezar y 3 despues.
    """
    if seeding:
        return 5
    if (clamp is not None and budget_nodes is not None
            and budget_nodes <= clamp[0]):
        return clamp[1]
    if (budget_nodes is not None and visits >= 1
            and budget_nodes >= DEPTH_BUDGET_THRESHOLD):
        return DEPTH_MULTIPV
    return 5 if visits < 3 else 3


@functools.lru_cache(maxsize=1)
def root_key():
    """La clave de la posicion inicial, calculada una vez por proceso.

    ``logic.start_fen()`` es una constante de la variante: mismo FEN, misma
    canonicalizacion, mismo sha256 hasta que alguien cambie de juego.  Se
    cachea porque hay caminos — ``get_or_create_position``, y por tanto cada
    hijo de cada expansion — que la preguntan decenas de veces por nodo.
    """
    return logic.key_of(logic.start_fen())


def get_or_create_position(fen, campaign_id=None):
    """Upsert de una posicion por su identidad canonica.

    ``campaign_id`` solo viaja en los ``defaults``, y eso es la mitad de la
    politica de etiquetado: una posicion que YA existia conserva su dueno (o
    su ausencia de dueno) pase lo que pase.  La otra mitad la ponen los tres
    sitios que MATERIALIZAN nodos bajo un padre — ``expand``,
    ``materialise_won_line`` y el ``goto`` del explorador — pasando el dueno
    del padre.  El razonamiento entero esta en ``expand``.

    LA RAIZ NACE ALCANZABLE, y aqui porque es el unico sitio por el que pasa
    entera: la siembran ``bootstrap_root``, el gestor de prueba y cada test
    que monta un arbol, y todos entran por esta puerta.  Cualquier otra
    posicion nace SIN marcar y se la gana por herencia en ``expand`` — tener
    un FEN no demuestra un camino.
    """
    fen = logic.canonical_fen(fen)
    key = logic.key_of(fen)
    is_root = key == root_key()
    pos, created = Position.objects.get_or_create(
        key=key, defaults={'fen': fen, 'campaign_id': campaign_id,
                           'reachable': is_root})
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
    elif is_root and not pos.reachable:
        # Raiz de una base anterior a la columna: se corrige sola en la
        # primera pasada en vez de esperar al backfill, que es lo que hace
        # que el BFS pueda arrancar aunque nadie lo haya sembrado.
        pos.reachable = True
        pos.save(update_fields=['reachable'])
    return pos


def expand(pos):
    """Crea TODAS las aristas legales (movegen del ingestor, §3.3).

    HERENCIA DE CAMPANA.  Un hijo que NACE aqui bajo una posicion etiquetada
    nace con la misma etiqueta: es lo que hace que una campana siga creciendo
    con el arbol en vez de quedarse congelada en el subarbol que existia el
    dia que se activo.  Un hijo que YA EXISTIA no se toca ni aunque no tenga
    dueno, y eso es deliberado: por transposicion, media base cuelga de media
    base, y "adoptar al vuelo lo que me encuentro" convierte a la primera
    campana activa en propietaria del arbol entero.  El stock que si le toca a
    una campana se lo da el BFS de activacion, una vez, y con tope.

    Se pasa el ``campaign_id`` y no la instancia para no pagar una consulta a
    ``Campaign`` por expansion: aqui solo hace falta la clave ajena.

    HERENCIA DE ALCANZABILIDAD.  La arista que se acaba de crear ES el camino:
    si el padre cuelga de la raiz, el hijo cuelga de la raiz, y con eso el
    selector acotado deja de necesitar el recorrido global que antes contestaba
    esa misma pregunta (§ docs/selector-incremental.md).  Se propaga UN PLY y
    a proposito: marcar transitivamente desde aqui convertiria una expansion —
    trabajo acotado, dentro de una peticion — en un BFS de profundidad
    desconocida.  Lo que ese ply no alcanza (una transposicion que aterriza
    sobre un subarbol que nacio suelto) lo recoge ``backfill_reachable``, que
    recalcula la columna entera y esta hecho para volver a correr.
    """
    if pos.expanded or pos.status != 'UNKNOWN':
        return []
    children = []
    for uci in logic.legal_moves(pos.fen):
        child_fen = logic.apply_move(pos.fen, uci)
        child = get_or_create_position(child_fen,
                                       campaign_id=pos.campaign_id)
        Edge.objects.get_or_create(parent=pos, move_uci=uci,
                                   defaults={'child': child})
        touched = []
        if child.priority <= DEAD / 2:
            child.priority = 0.0   # ruta nueva via transposicion: revive
            touched.append('priority')
        if pos.reachable and not child.reachable:
            child.reachable = True
            touched.append('reachable')
        if touched:
            child.save(update_fields=touched)
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


def _seed_child_eval(child, ev):
    """Siembra con update CONDICIONAL: entre cargar al hijo y escribirlo
    puede colarse, en otro consumer, el ingest del analisis PROPIO del
    hijo.  Un save() normal pisaria ese analisis con la linea MultiPV
    del padre (mas vieja y menos fiable); el filtro eval_cp IS NULL hace
    que el ultimo en llegar NO gane: gana el analisis propio."""
    won = Position.objects.filter(
        key=child.key, eval_cp__isnull=True).update(
        eval_cp=ev, updated=timezone.now())
    if won:
        child.eval_cp = ev
    return bool(won)


def ingest_analysis(position_key, lines, nodes_budget, machine='',
                    mate_proofs=None, restricted=False):
    """lines = [{'move': uci, 'eval_cp': int|None, 'mate': int|None,
                 'pv': [uci...]}] del MultiPV del motor (perspectiva blanca).
    Devuelve dict con resumen.

    ``restricted``: el pase se busco con ``searchmoves`` acotado a las jugadas
    sin resolver (§ views._live_moves), asi que su mejor linea es la mejor
    ENTRE LO QUE QUEDABA, no la de la posicion.  Con la jugada buena ya cerrada
    y fuera de la lista, ese numero es peor que la posicion, y ``eval_cp``
    alimenta budget_for, el breadth-swap, witness-refuted y la incertidumbre.
    Un pase asi puede MEJORAR ``eval_cp`` para el que mueve — eso lo ha
    demostrado — pero nunca empeorarlo, que es lo unico que no ha mirado.
    """
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
        certified_here = 0
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
                _seed_child_eval(child, ev)
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
            if (child.status != 'UNKNOWN' and prepared_proof is not None
                    and prepared_proof[2] == 'PROVEN'
                    and _adopt_certified_line(child, pos, prepared_proof)):
                certified_here += 1
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
                # LONGITUD DEL TESTIGO que se acaba de guardar, ni mas ni
                # menos.  Con ``proof='ANDOR'`` es ademas una cota de verdad
                # (la busqueda exhaustiva cubrio len+2 plies); con
                # ``proof='ENGINE'`` es solo una linea legal que acaba en mate
                # si el defensor colabora, y puede quedarse CORTA — es
                # exactamente el caso que la certificacion existe para cazar.
                # Este brazo cierra un hijo UNKNOWN: aqui no se arbitra nada
                # contra un valor anterior, porque no lo hay.  El arbitraje
                # cuando el hijo YA estaba cerrado esta en
                # ``_adopt_certified_line``.
                child.mate_in = len(pv_rest)
                # Prueba exhaustiva: la racha reversible REAL del arbol
                # probado.  Testigo sin certificar: la cota burda por
                # longitud, que se recalcula al certificar.
                child.clock_slack = (
                    logic.slack_from_run(worst_run)
                    if proof_result == 'PROVEN' and worst_run is not None
                    else logic.slack_from_witness_length(len(pv_rest)))
                if pv_rest:
                    child.best_move = pv_rest[0]
                # CAS sobre el estado leido: el DAG transpone y el MISMO hijo
                # cuelga de dos padres — dos consumers pueden llegar aqui a la
                # vez con fotos UNKNOWN y el segundo pisaria un cierre ya
                # commiteado (incluso degradando ANDOR a ENGINE o cambiando el
                # veredicto).  Solo cierra quien encuentra el hijo AUN abierto;
                # el que pierde no emite eventos ni materializa nada.
                claimed = Position.objects.filter(
                    key=child.key, status='UNKNOWN',
                ).update(status=child.status, closure=child.closure,
                         proof=child.proof, won_line=child.won_line,
                         mate_in=child.mate_in, clock_slack=child.clock_slack,
                         best_move=child.best_move, updated=timezone.now())
                if not claimed:
                    continue
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
        # El ESCAPARATE de lineas no se pisa NI hacia abajo NI hacia atras.
        #
        # ANCHURA.  Un visitante pide MultiPV 5 y ve sus cinco lineas; si
        # luego un pase FILL con searchmoves (1-2 lineas, 8M) re-toca la
        # posicion, ese pase aporta conocimiento pero no debe sustituir la
        # foto ancha por una rendija — 275 de 400 posiciones revisitadas
        # tenian el clobber cuando Wolfram lo reporto (29-jul).  Un analisis
        # mas PROFUNDO si sustituye aunque sea mas estrecho: esa es la
        # politica deliberada de MultiPV 2 en revisitas.
        #
        # PROFUNDIDAD.  La anchura entraba por su propia puerta — bastaba
        # empatar el numero de lineas para sustituir — asi que un pase de
        # 128M con cinco lineas borraba una foto de 640M con cinco lineas y
        # la posicion se quedaba contando la version pobre de lo que ya
        # sabia.  Un pase por DEBAJO del presupuesto que escribio la foto
        # vigente no tiene nada que anadirle: no manda ni el escaparate, ni
        # ``eval_cp``, ni ``best_move``.  Todo lo demas del ingest si ocurre
        # — visitas, nodos invertidos, hijos, mates, respaldo, credito de
        # maquina: el trabajo se hizo y se paga, simplemente no arbitra.  A
        # IGUAL presupuesto vuelve a decidir la anchura.
        #
        # DESCARTADO a proposito: colar al pase superficial cuando reclama
        # mate.  Los mates ya viajan por su propio canal — la prueba
        # certificada y ``won_line`` — que este arbitraje no toca; abrirles
        # ademas esta puerta seria decidir dos veces la misma cosa, y la
        # segunda con la evidencia peor.
        snapshot = capped_analysis(lines[:8])
        if snapshot:
            snapshot[0]['_budget'] = nodes_budget
        stored = pos.last_analysis or []
        current = [ln for ln in stored
                   if not (isinstance(ln, dict) and ln.get('prior_pass'))]
        prior = [ln for ln in stored
                 if isinstance(ln, dict) and ln.get('prior_pass')]
        stored_budget = (current[0].get('_budget', 0)
                         if current and isinstance(current[0], dict) else 0)
        shallower = bool(current) and nodes_budget < stored_budget
        if not shallower and (not current or nodes_budget > stored_budget
                              or len(snapshot) >= len(current)):
            # Un pase mas ANCHO que el nuevo no se tira: sus lineas 3..N
            # llevan informacion que el pase profundo de 2 lineas no repite
            # (peticion de Wolfram).  Se conservan marcadas como pase
            # anterior; un pase nuevo igual de ancho las sustituye.
            if current and len(current) > len(snapshot):
                prior = [dict(ln, prior_pass=True) for ln in current]
            elif len(snapshot) >= len(current or prior):
                prior = []
            pos.last_analysis = (snapshot + prior)[:10]
        # Un pase restringido no puede EMPEORAR la posicion para el que mueve:
        # no ha mirado las jugadas que le faltan, asi que su peor numero no es
        # una afirmacion sobre ellas.  Si mejora, si: eso lo ha demostrado con
        # una linea concreta, y el best_move la acompana.
        if restricted and best_eval is not None and pos.eval_cp is not None:
            improves = (best_eval > pos.eval_cp if stm_white
                        else best_eval < pos.eval_cp)
            if not improves:
                best_eval, best_move = None, None
        if shallower:
            # Mismo arbitraje que el escaparate, y por lo mismo: este numero
            # sale de una busqueda que la foto vigente ya supero.
            best_eval, best_move = None, None
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
    uncertainty = _uncertainty_expand([pos.key, *parent_keys])
    refuted = _witness_refuted_revisit(pos, parent_keys)
    summary = {'closed_children': closed_here, 'backed_up': changed,
               'backed_evals': backed, 'uncertainty_expanded': uncertainty,
               'witness_refuted': refuted}
    if revoked_here:
        summary['revoked'] = len(revoked_here)
    if certified_here:
        summary['certified'] = certified_here
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


# QUE PASA CUANDO UN ANALISIS NUEVO TRAE OTRA LINEA DE MATE PARA UN HIJO QUE YA
# ESTABA CERRADO.  La pregunta se plantea sola en cuanto la linea nueva es MAS
# LARGA que la guardada y del mismo signo: ¿manda el minimo (la cota mas
# fuerte) o manda la ultima (la busqueda mas profunda)?
#
# NINGUNA DE LAS DOS, y la razon esta en lo que ``mate_in`` significa aqui.  No
# es una cota flotante sobre la distancia: es la LONGITUD DEL TESTIGO guardado
# en ``won_line``.  Los dos numeros son propiedades de dos lineas distintas, no
# dos mediciones de la misma cosa:
#
# * Quedarse con el minimo es exactamente el bug que Wolfram reporto, un nivel
#   mas abajo: una PV de motor con horizonte (``proof='ENGINE'``) puede ser
#   corta porque la defensa que la refuta cae fuera de la busqueda, y el minimo
#   la conserva para siempre.  El testigo de 4 plies del nodo Ne5 es justo eso.
# * Quedarse con la ultima porque sea mas profunda tampoco vale: la evidencia
#   llega por la PV del PADRE, y en un DAG el mismo hijo cuelga de padres
#   analizados a presupuestos distintos.  "La ultima" haria ping-pong entre
#   dos afirmaciones igual de incertificadas, y encima dejaria el numero
#   apuntando a una linea que no es la guardada.
#
# ASI QUE: un testigo sin certificar solo lo retira quien puede refutarlo — una
# busqueda exhaustiva de SU horizonte.  Esa maquinaria ya existe y no se
# duplica aqui: ``_revoke_contradicted_mate`` online, ``recertify_mates`` y la
# deuda de la flota (``enqueue_engine_debt``, que encola TODO ``MATE_PV`` con
# ``proof='ENGINE'``) en diferido.  Una linea nueva mas larga no refuta nada:
# no dice que el mate corto no exista, solo que esta busqueda no lo vio.
#
# LO QUE SI ES EVIDENCIA MEJOR es un veredicto ``PROVEN``: un certificado
# AND/OR exhaustivo.  Hasta ahora se tiraba a la basura cuando el hijo ya
# estaba cerrado (el bucle de ingesta solo miraba hijos UNKNOWN), y con el se
# tiraba la unica forma que tenia el sistema de CORREGIR una distancia online.
# Sustituye al testigo sin certificar entero — linea, distancia, ``proof`` y
# ``clock_slack`` — en cualquier direccion, que es la misma regla que
# ``recertify_mates`` aplica en diferido, aplicada donde el certificado ya
# estaba en la mano.  De propina saca al nodo de la cola de deuda.
def _adopt_certified_line(child, parent, prepared_proof):
    """Un certificado exhaustivo sustituye a un testigo sin certificar."""
    winner_white, pv_rest, _verdict, worst_run = prepared_proof
    want = 'WHITE_WIN' if winner_white else 'BLACK_WIN'
    if (child.status != want or child.closure != 'MATE_PV'
            or child.proof == 'ANDOR' or not pv_rest):
        return False
    was = {'proof': child.proof, 'mate_in': child.mate_in,
           'won_line': child.won_line}
    child.proof = 'ANDOR'
    child.won_line = ' '.join(pv_rest)
    child.mate_in = len(pv_rest)
    child.best_move = pv_rest[0]
    child.clock_slack = (logic.slack_from_run(worst_run)
                         if worst_run is not None
                         else logic.slack_from_witness_length(len(pv_rest)))
    child.save(update_fields=['proof', 'won_line', 'mate_in', 'clock_slack',
                              'best_move', 'updated'])
    DBEvent.objects.create(kind='MATE_WITNESS_CERTIFIED', payload={
        'key': child.key, 'parent': parent.key,
        'winner': 'WHITE' if winner_white else 'BLACK',
        'mate_in': child.mate_in, 'was_mate_in': was['mate_in'],
        'was_proof': was['proof'],
        'line_changed': was['won_line'] != child.won_line})
    # La linea nueva tambien tiene que ser navegable, igual que en el cierre.
    materialise_won_line(child)
    return True


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
                # El DTM practico (min para el ganador, max para el perdedor,
                # y un hijo sin distancia la deja desconocida) lo pone el
                # refresco de abajo, que corre en esta misma vuelta: la regla
                # vive en UN sitio, ``mate_distance_refresh``.
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
                # refresco retroactivo del DTM: la distancia de un nodo ya
                # cerrado se RECALCULA desde sus hijos, en cualquier direccion.
                #
                # Aqui vivia el bug que Wolfram reporto (1.Nf3 d5): el bloque
                # OR solo se dejaba ACORTAR (``1 + hijo < pos.mate_in``),
                # mientras el AND de al lado ya recalculaba exacto.  Cuando un
                # hijo CORREGIA su distancia hacia arriba — porque su testigo
                # de motor resulto ser una cota de horizonte y la certificacion
                # lo alargo — el ancestro OR se quedaba con la cota vieja para
                # siempre: el padre enseñaba "≤M2" con un unico hijo ganador a
                # 4 plies, es decir mate en 3 (1+4 = 5 plies).  Nada en el
                # sistema podia volver a subir ese numero.
                #
                # Que se conserva: el minimo de las cotas ACTUALES sigue siendo
                # la mejor cota actual, asi que la propiedad de cota superior no
                # se pierde por recalcular en las dos direcciones — se pierde
                # justamente por NO hacerlo, que es quedarse con una cota que
                # ya no sostiene ningun hijo.
                new_mate, new_move = mate_distance_refresh(pos, edges)
                if (new_mate, new_move) != (pos.mate_in, pos.best_move):
                    pos.mate_in, pos.best_move = new_mate, new_move
                    dirty = True
            if dirty:
                # Solo las columnas que ESTA cascada decide.  Un save()
                # completo escribia la fila entera desde una lectura sin
                # bloqueo y REVERTIA lo que otro consumer acababa de
                # commitear en el mismo ancestro (visits, eval, backed,
                # priority, campaign, expanded...) — la misma carrera que
                # ingest_analysis documenta y evita con update_fields unas
                # llamadas mas arriba.  En PG la escritura espera al commit
                # ajeno y luego lo pisaba: perdida garantizada, no azar.
                pos.save(update_fields=['status', 'closure', 'proof',
                                        'best_move', 'mate_in',
                                        'clock_slack', 'updated'])
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


def mate_distance_refresh(pos, edges):
    """``(mate_in, best_move)`` de un nodo DECISIVO ya cerrado, desde sus hijos.

    UNA sola regla, y publica: la usan la cascada exacta y el backfill
    ``backfill_mate_distance``, que asi convergen al MISMO punto fijo en vez de
    a dos parecidos.  Solo lee ``status``/``closure``/``proof``/``mate_in``/
    ``won_line``/``best_move`` del nodo y de los hijos, de modo que sirve
    igual con filas ORM que con proyecciones ligeras.

    * Nodo OR (gana el que mueve): ``min(testigo propio, 1 + mejor hijo
      ganador)``.  El minimo sobre el subconjunto de hijos CERRADOS es una
      cota, no un exacto, asi que un hijo puede acortar la distancia pero no
      alargarla por encima de la linea que el propio nodo tiene guardada.  Un
      cierre MINIMAX no guarda linea ninguna: su distancia es exactamente la
      de sus hijos, en las dos direcciones — ahi estaba el bug.
    * Nodo AND (pierde el que mueve): ``1 + max`` sobre TODOS los hijos, y solo
      con todos informados.  Eso es informacion completa, luego es exacto, y
      por eso si puede superar al testigo propio: el testigo sigue UNA defensa
      y el defensor elige la peor.
    * Sin nada que nombrar, la distancia se queda en ``None``: un numero que
      ya no sostiene ningun hijo es peor que no tener numero.
    """
    mover_win = 'WHITE_WIN' if pos.fen.split()[1] == 'w' else 'BLACK_WIN'
    if pos.status != mover_win:
        dists = [e.child.mate_in for e in edges]
        if dists and all(d is not None for d in dists):
            return 1 + max(dists), pos.best_move
        return pos.mate_in, pos.best_move

    winners = [e for e in edges if e.child.status == pos.status
               and e.child.mate_in is not None]
    best = min(winners, key=_witness_rank) if winners else None
    own = _own_witness_plies(pos)
    derived = 1 + best.child.mate_in if best else None
    new_mate = min([d for d in (own, derived) if d is not None], default=None)
    from_child = best is not None and derived == new_mate
    if from_child and (new_mate != pos.mate_in
                       or (pos.best_move != best.move_uci
                           and _child_is_verified(best.child))):
        # el mate probado mas corto, y a igualdad el CERTIFICADO (F4b)
        return new_mate, best.move_uci
    if not from_child and new_mate != pos.mate_in:
        line = (pos.won_line or '').split()
        if own is not None and line:
            # la distancia vuelve a ser la del testigo propio (el hijo que la
            # acortaba se perdio): el testigo exportado vuelve con ella
            return new_mate, line[0]
    return new_mate, pos.best_move


def _own_witness_plies(pos):
    """Distancia que este nodo sostiene POR SI MISMO, o ``None``.

    Un cierre ``MATE_PV`` lleva su propia linea re-verificada jugada a jugada:
    ese numero no lo derivo de las filas de sus hijos y por tanto ningun hijo
    puede ALARGARLO — si la linea guardada es una cota de horizonte del motor,
    quien la retira es la certificacion (``recertify_mates``, la deuda de la
    flota, ``_revoke_contradicted_mate``), no un minimax sobre hermanos.

    Las filas historicas que cerraron por MATE_PV antes de que existiera la
    columna ``won_line`` no tienen linea que medir; su ``mate_in`` almacenado
    sigue siendo una afirmacion propia, asi que vale de testigo.

    Cualquier otro cierre (MINIMAX, TB, SOLVE) no afirma distancia por su
    cuenta: la suya es exactamente la de sus hijos.
    """
    if pos.closure != 'MATE_PV':
        return None
    line = (pos.won_line or '').split()
    return len(line) if line else pos.mate_in


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


def coverage_is_partial(row, children):
    """Le falta a este nodo alguna respuesta por mirar.

    Es la condicion que degrada el respaldo en ``_backed_for``: mientras la
    cobertura sea parcial, el valor de los hijos entra con guardas y la
    autoridad de PRUEBA se corta.  Vive en una funcion con nombre porque
    ademas de gobernar el respaldo es lo que define una afirmacion FRAGIL
    (ver ``fragile_mate_claims``), y dos copias de este predicado son dos
    copias que se separan.
    """
    informed = [child for child in children if child.value is not None]
    if not informed:
        return True
    return not (bool(row.expanded) and len(informed) == len(children))


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
    complete = not coverage_is_partial(row, children)
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
    quality = best.quality
    if not complete and quality >= PROVEN_QUALITY:
        # La autoridad de una PRUEBA termina en el primer nodo cuyas
        # alternativas no estan probadas.  Sin este corte, una linea que un
        # visitante CAMINO hasta un mate terminal (nodos sin eval propia, la
        # guarda direccional sin ancla) subia el valor de banda de mate con
        # peso de prueba por toda la cadena, y el explorador pintaba 9994
        # BACKED sobre territorio sin un solo analisis (caso Wolfram,
        # 28-jul).  El valor puede subir — es el mejor conocimiento — pero
        # con el soporte de BUSQUEDA del propio nodo (0 en un nodo caminado):
        # el primer ancestro con eval real lo bloquea y la convergencia
        # compra analisis de motor exactamente en la linea que el humano
        # exploro.  El soporte de busqueda real (no-prueba) sigue
        # atravesando nodos conectores como siempre.
        quality = row.nodes_invested or 0
    return best.value, best.move, 1 + best.plies, quality


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
    # Cupo PROPIO: lo que hayan encolado cobertura o la reparacion de dn no es
    # cola de este brazo y no se le cobra (§ cupos por brazo).
    pending = AnalysisTask.objects.filter(
        state='PENDING', arm=QUALITY_ARM).count()
    room = max(0, QUALITY_QUEUE_CAP - pending)
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
                      'source': AnalysisTask.Source.FILL,
                      'arm': QUALITY_ARM})
        if created:
            made += 1
        elif task.state == 'PENDING' and task.budget_nodes < budget:
            # Adoptarla la mete en la cola de ESTE brazo igual que crearla, asi
            # que cuesta cupo igual que crearla.  Y se escribe sobre el estado
            # que se leyo: si otro la arrendo entre medias, no hay nada que
            # subir y tampoco se cobra.
            adopted = AnalysisTask.objects.filter(
                id=task.id, state='PENDING',
                budget_nodes=task.budget_nodes).update(
                    budget_nodes=budget, source=AnalysisTask.Source.FILL,
                    arm=QUALITY_ARM)
            if adopted:
                made += 1
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


def known_eval_of(status, backed_eval, eval_cp):
    """``best_known_eval`` a partir de las COLUMNAS, sin instancia delante.

    Existe la version por columnas porque el selector acotado lee la base con
    ``values_list`` y no tiene fila que preguntar: materializar un
    ``Position`` por nodo para contestar lo que ya trae en la mano es justo el
    gasto que ese selector viene a quitar.  La regla de precedencia es UNA y
    vive aqui; ``best_known_eval`` la llama con los campos de la instancia.
    """
    exact = _status_eval(status)
    if exact is not None:
        return exact
    if backed_eval is not None:
        return backed_eval
    return eval_cp


def best_known_eval(pos):
    """Mejor conocimiento actual del nodo, en perspectiva blanca.

    Es lo que deben pintar tanto la cabecera de la posicion como la fila de
    la arista que apunta a ella: status probado > backed > eval puntual.
    """
    return known_eval_of(pos.status, pos.backed_eval, pos.eval_cp)


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
        # Misma herencia que en ``expand``: la PV verificada tambien
        # materializa nodos nuevos, y los que nacen bajo una campana son
        # suyos.  Por el ``campaign_id`` y no por la instancia, que aqui
        # ademas se pagaria una vez por ply.
        child = get_or_create_position(logic.apply_move(node.fen, uci),
                                       campaign_id=node.campaign_id)
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
            # Mismo CAS que el cierre de hijos del ingest: por transposicion
            # este nodo puede estar cerrandose a la vez desde otra PV en otro
            # consumer, y el que pierde no debe pisar ni duplicar eventos.
            claimed = Position.objects.filter(
                key=child.key, status='UNKNOWN',
            ).update(status=child.status, closure=child.closure,
                     proof=child.proof, won_line=child.won_line,
                     mate_in=child.mate_in, best_move=child.best_move,
                     clock_slack=child.clock_slack, updated=timezone.now())
            if claimed:
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


# ---------------- atribucion de cierres ----------------
#
# LA PREGUNTA QUE NO SE PODIA RESPONDER.  "De los cierres de las ultimas 24
# horas, cuantos los produjo el explorador automatico y cuantos una persona
# pidiendo una jugada a mano?"  Hasta aqui no habia forma: un ``NODE_CLOSED``
# guardaba la posicion, el status y la clase de cierre, y nada mas.  La
# procedencia no es derivable a posteriori — un cierre MINIMAX en la cascada
# puede venir de una tarea AUTO, de un completado FILL, del click de un
# visitante o de un certificado SOLVE, y las cuatro escriben exactamente las
# mismas filas — asi que se REGISTRA en el momento, o se pierde.
#
# POR QUE UN CONTEXTVAR Y NO UN PARAMETRO.  El cierre no ocurre donde se sabe
# la procedencia.  ``apply_job`` conoce el ``source`` de su ``AnalysisTask``;
# el cierre lo emite ``backup_cascade`` cinco llamadas mas abajo, a veces
# sobre un ANCESTRO que nada tiene que ver con la posicion analizada, y por un
# camino (``ingest_analysis`` -> ``materialise_won_line`` -> cascada) que ya
# tiene tres puntos de emision.  Pasar el dato a mano seria tocar seis firmas
# y confiar en que ningun sitio nuevo se olvide; un contextvar lo lleva solo,
# es por hilo (cada worker de gunicorn tiene el suyo) y su valor por defecto
# es la respuesta honesta cuando nadie lo puso: NONE, "esto no salio de una
# tarea".
#
# LO QUE NO ES.  No es una fuente de verdad ni toca el cierre: es una etiqueta
# en el evento.  Un cierre mal atribuido sigue siendo un cierre correcto.
CLOSURE_SOURCE_NONE = 'NONE'
CLOSURE_SOURCE_SOLVE = 'SOLVE'
# El orden en el que se pintan y se cuentan: las tres colas de analisis en el
# mismo orden en que se sirven, luego los certificados, luego lo que no salio
# de ninguna tarea (comandos de mantenimiento, siembra).
CLOSURE_SOURCES = ('USER', 'FILL', 'AUTO', CLOSURE_SOURCE_SOLVE,
                   CLOSURE_SOURCE_NONE)

_closure_source = contextvars.ContextVar('atomicdb_closure_source',
                                         default=CLOSURE_SOURCE_NONE)


@contextlib.contextmanager
def closure_attribution(source):
    """Marca a quien se le apuntan los cierres que ocurran aqui dentro.

    Se restaura siempre, incluso si la aplicacion revienta: un token de
    contextvar no es un global que haya que acordarse de limpiar.
    """
    label = str(source or CLOSURE_SOURCE_NONE).upper()
    if label not in CLOSURE_SOURCES:
        label = CLOSURE_SOURCE_NONE
    token = _closure_source.set(label)
    try:
        yield label
    finally:
        _closure_source.reset(token)


def current_closure_source():
    return _closure_source.get()


def _emit_closure_events(pos, **extra):
    """El UNICO emisor de ``NODE_CLOSED``.

    ``extra`` son los campos que este camino de cierre sabe y los demas no
    (el DTZ de un TB, los nodos del certificado de un SOLVE).  Antes cada
    camino especial creaba SU evento y ademas llamaba aqui, asi que un cierre
    por certificado contaba DOS veces en el "cierres en 24h" de la portada y
    un cierre TB no cerraba la campana que colgaba de el.  Una sola puerta
    arregla las dos cosas.
    """
    DBEvent.objects.create(kind='NODE_CLOSED', payload={
        'key': pos.key, 'status': pos.status, 'closure': pos.closure,
        'source': _closure_source.get(), **extra})
    for camp in Campaign.objects.filter(root=pos,
                                        state=Campaign.CState.ACTIVE):
        DBEvent.objects.create(kind='CAMPAIGN_CLOSED', payload={
            'campaign': camp.name, 'status': pos.status})
        camp.apply_state(Campaign.CState.DONE)


DEAD = -1e9   # lapida: rama muerta, fuera de la cola para siempre
REGRET_WEIGHT = 3.0      # unidades de prioridad por cada 100cp de regret
DISCONNECTED_REGRET = 5  # posiciones sin camino a la raiz (cajetin FEN)
# Peso de una campana ACTIVA en la prioridad del selector.  La escala esta
# elegida contra el resto de la formula, no al azar: el termino de cercania al
# cierre vale como mucho 15, el salto de "mate visto" 50 y el regret resta
# hasta 90.  Cuarenta unidades por ``log1p(votos)`` ponen una campana muy
# votada por delante de la exploracion normal SIN llegar a tapar un mate a la
# vista, y el logaritmo hace que el voto veinte pese bastante menos que el
# segundo — un brigadeo de cookies mueve el orden, no lo compra.
CAMPAIGN_BONUS = 40.0
# Los tres sumandos y los dos restandos de la formula, con nombre.  Estaban
# escritos como literales dentro del bucle y ahi se quedaron mientras el bucle
# fue uno; con dos motores (v1 y el acotado) y un horizonte de poda que
# necesita el TECHO de lo que la formula puede sumar, un numero suelto en tres
# sitios es una divergencia esperando su turno.  Mismos valores, un solo sitio.
EVAL_PROXIMITY_CAP = 1500   # centipeones: tope del termino de cercania (15 u.)
MATE_BAND_BONUS = 50.0      # el motor ya vio mate: rematar
UNEXPANDED_BONUS = 2.0      # un nodo sin abrir cuesta poco y ensena mucho
VISIT_WEIGHT = 1.5          # frescura: cada visita ya pagada resta
REGRET_CAP = 3000           # centipeones: saturacion del regret (30 unidades)
# Precio del nodo CONECTADO pero fuera de la bola explorada.  Es la saturacion
# del regret, no un numero nuevo: el Dijkstra completo le habria dado como
# mucho esto, y quedarse fuera del horizonte significa exactamente "su camino
# desde la raiz cuesta al menos lo que ya no compensa medir".  Se llama aparte
# de ``DISCONNECTED_REGRET`` porque son la MISMA pregunta con respuestas
# opuestas: lejos (30) castiga, suelto (5) perdona.
FAR_REGRET = REGRET_CAP / 100.0


def _active_campaign_votes():
    """{id de campana: votos} de las ACTIVE, en UNA consulta por pasada.

    Se precarga entero a proposito: la alternativa es una consulta por
    posicion dentro de un bucle que recorre la base al completo.
    """
    return dict(Campaign.objects
                .filter(state=Campaign.CState.ACTIVE)
                .values_list('id', 'votes'))


def _campaign_bonus(campaign_id, campaign_votes):
    """El sumando de campana, o cero.

    Solo puntua una campana ACTIVE: una propuesta sin activar, o una en pausa,
    no aparece en el diccionario.  Es exactamente la linea entre "la comunidad
    pide" y "el propietario concede".
    """
    if campaign_id is None:
        return 0.0
    votes = campaign_votes.get(campaign_id)
    if votes is None:
        return 0.0
    return CAMPAIGN_BONUS * math.log1p(max(0, votes))


def _runits(regret):
    """Regret en UNIDADES de prioridad, con sus dos topes.

    ``inf`` no es "infinitamente malo": es "no cuelga de la raiz", y eso es el
    cajetin FEN que alguien pego a mano, que merece mirada aunque no este en
    el arbol.  El otro tope, la saturacion, dice que a partir de 30 unidades
    dar mas detalle no ordena nada mejor.
    """
    if regret == float('inf'):
        return DISCONNECTED_REGRET
    return min(regret, REGRET_CAP) / 100.0


def priority_of(known, expanded, visits, campaign_id, runits, campaign_votes):
    """LA formula del selector, en un solo sitio (§ ``refresh_priorities``).

    Los dos motores — la foto global y el acotado — tienen que dar el MISMO
    numero para el mismo nodo, y esa igualdad es la unica prueba que autoriza
    a conmutar.  Compartir la funcion la hace cierta por construccion en vez
    de por vigilancia.
    """
    e = abs(known) if known is not None else 0
    return (min(e, EVAL_PROXIMITY_CAP) / 100.0      # cercania al cierre
            + (MATE_BAND_BONUS if e >= MATE_BAND else 0.0)
            + (0.0 if expanded else UNEXPANDED_BONUS)
            - REGRET_WEIGHT * runits                # relevancia hacia la raiz
            - VISIT_WEIGHT * visits                 # frescura
            # El bono va DESPUES de todo lo demas: es un sumando, no puede
            # reescribir ninguna decision anterior, solo desempatar a favor de
            # la linea que la comunidad voto.
            + _campaign_bonus(campaign_id, campaign_votes))


def _priority_ceiling(campaign_votes):
    """Techo de TODO lo que la formula puede sumarle a un nodo cualquiera.

    Es la mitad de arriba del horizonte de poda: si ni sumando el maximo
    imaginable — cercania al tope, mate visto, sin expandir y la campana mas
    votada que haya — un nodo alcanza el corte del top-N, tampoco lo alcanzan
    sus descendientes, cuyo regret solo puede ser mayor.  Los restandos no
    entran: ``visits`` es >= 0 y solo puede bajar el numero.
    """
    best_votes = max(campaign_votes.values(), default=0)
    return (EVAL_PROXIMITY_CAP / 100.0 + MATE_BAND_BONUS + UNEXPANDED_BONUS
            + CAMPAIGN_BONUS * math.log1p(max(0, best_votes)))


class _TopK:
    """Los K mejores por prioridad, con memoria acotada a K.

    Dos usos y los dos son el mismo problema.  El horizonte de poda necesita
    saber cual es la prioridad del N-esimo mejor VISTO HASTA AHORA, y el modo
    sombra necesita devolver una cima sin escribirla.  Guardar todo y ordenar
    al final habria sido reintroducir exactamente el diccionario de millones
    de filas que este trabajo viene a quitar.

    El corte (``cut``) solo puede SUBIR segun entran candidatos, y eso es lo
    que hace que podar con el sea seguro: un corte bajo poda de menos — se
    explora de mas y se tarda mas — mientras que uno alto tiraria nodos que
    merecian estar.  Se peca por el lado que solo cuesta tiempo.
    """

    def __init__(self, k):
        self._k = max(0, int(k))
        self._heap = []

    def offer(self, key, priority):
        if self._k == 0:
            return
        if len(self._heap) < self._k:
            heapq.heappush(self._heap, (priority, key))
        elif priority > self._heap[0][0]:
            heapq.heapreplace(self._heap, (priority, key))

    @property
    def cut(self):
        """Prioridad del K-esimo mejor, o ``-inf`` mientras no haya K."""
        if len(self._heap) < self._k or not self._heap:
            return float('-inf')
        return self._heap[0][0]

    def as_dict(self):
        return {key: priority for priority, key in self._heap}


SELECTOR_HORIZON_FLOOR = 1_000


def selector_horizon_width():
    """Cuantos candidatos sostienen el corte del horizonte.

    Es la anchura de lo que alguien consume de verdad: ``next_tasks`` mira el
    top ``4*n`` y quien lo llama pide ``TASK_REFILL_COUNT`` por arriendo.  El
    suelo de mil manda mientras ese lote sea pequeno, y esta escrito como un
    maximo para que subir el lote suba el horizonte solo, sin que nadie tenga
    que acordarse.

    ``TASK_REFILL_COUNT`` se importa AQUI DENTRO y no arriba porque ``views``
    importa este modulo: el ciclo se evita retrasando la lectura hasta que hay
    alguien preguntando.
    """
    from .views import TASK_REFILL_COUNT
    return max(4 * TASK_REFILL_COUNT, SELECTOR_HORIZON_FLOOR)


BOUNDED_BATCH = 500   # claves por lote de expansion: un IN que toda base traga


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
        val[key] = known_eval_of(status, backed_eval, eval_cp)
        white_stm[key] = fen.split()[1] == 'w'
        if status != 'UNKNOWN':
            settled.add(key)
    children = {}
    for pid, cid in Edge.objects.values_list('parent_id', 'child_id'):
        children.setdefault(pid, []).append(cid)

    INF = float('inf')
    regret = dict.fromkeys(val, INF)
    root = root_key()
    if root in regret:
        regret[root] = 0.0
    heap = [(0.0, root)]
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


class _BoundedNode:
    """La foto que el Dijkstra acotado necesita de un nodo, sin ORM detras.

    Un ``Position`` completo trae ``last_analysis`` (un JSON por fila), la FEN,
    la ``won_line`` y veinte columnas mas que aqui no pinta ninguna.  A
    millones de nodos eso es la diferencia entre una bola que cabe y una
    pasada que se come la maquina.
    """

    __slots__ = ('value', 'white_stm', 'settled', 'expanded', 'visits',
                 'campaign_id', 'priority')

    def __init__(self, fen, eval_cp, backed_eval, status, expanded, visits,
                 campaign_id, priority):
        self.value = known_eval_of(status, backed_eval, eval_cp)
        self.white_stm = fen.split()[1] == 'w'
        self.settled = status != 'UNKNOWN'
        self.expanded = expanded
        self.visits = visits
        self.campaign_id = campaign_id
        self.priority = priority


def _regret_from_root_bounded(campaign_votes=None, top_n=None,
                              batch_size=BOUNDED_BATCH):
    """El mismo regret que ``_regret_from_root``, sin la foto del grafo.

    MISMA SEMANTICA, OTRA MEMORIA.  Los gaps se calculan igual, los nodos
    cerrados siguen sin relajar hacia abajo y las transposiciones siguen
    tomando la mejor ruta.  Lo que cambia es de donde salen los datos: en vez
    de dos diccionarios con la base entera dentro, la adyacencia y los valores
    se leen POR LOTES al expandir la frontera, y el cache local solo guarda la
    BOLA explorada y su borde.  Devuelve ``{clave: regret}`` de los nodos
    FINALIZADOS — quien no este es que quedo fuera de la bola, y quien pregunta
    (``refresh_priorities_v2``) sabe ponerle precio a eso.

    EL HORIZONTE.  Dijkstra saca del heap en orden no decreciente de regret,
    asi que en cuanto el mejor caso imaginable de lo que queda por sacar
    — todo lo que la formula puede sumar, menos lo que ya resta este regret —
    no alcanza al corte del top-N, no queda nada por descubrir que importe: lo
    de mas abajo solo puede tener MAS regret.  Ahi se para.  No es una
    aproximacion sobre la cima; es la observacion de que nadie consume las
    prioridades del fondo de la tabla.

    Y LO QUE ESO NO GARANTIZA, porque la memoria acotada depende de ello: la
    bola es pequena cuando el corte es ALTO.  Si el N-esimo mejor puntua bajo
    — pocos candidatos, o un arbol sin nada urgente — el horizonte no llega a
    morder y esto recorre lo que haga falta.  No es un fallo, es el mismo
    trabajo que hacia la version global y por el mismo motivo; lo que ya no
    hace es cargar el grafo entero ANTES de saber si le hace falta.  Cuanto
    mide la bola de verdad lo dice ``selector_shadow`` sobre la base viva, que
    es justo para lo que esta.

    LA CARRERA MUERE AQUI, Y NO POR UN ``try``.  El ``KeyError`` de la version
    global salia de mezclar dos fotos tomadas en momentos distintos: una clave
    que la adyacencia conocia y el mapa de valores no.  Con lecturas por lotes
    no hay dos fotos que casar — una clave que no vuelve del lote es un nodo
    recien nacido o una fila que aun no se ve, se salta, y la pasada siguiente
    la encuentra.  Saltarla cuesta un minuto de prioridad vieja; el
    ``KeyError`` costaba la pasada entera.
    """
    if campaign_votes is None:
        campaign_votes = _active_campaign_votes()
    if top_n is None:
        top_n = selector_horizon_width()
    ceiling = _priority_ceiling(campaign_votes)
    candidates = _TopK(top_n)

    INF = float('inf')
    node = {}        # clave -> _BoundedNode (la bola y su borde, nada mas)
    regret = {}      # mejor regret conocido (incluye frontera sin sacar)
    done = {}        # regret FINAL de lo que ya salio del heap

    root = root_key()
    regret[root] = 0.0
    heap = [(0.0, root)]
    while heap:
        batch = []
        while heap and len(batch) < batch_size:
            r, key = heapq.heappop(heap)
            if key in done or r > regret.get(key, INF):
                continue          # entrada rancia: ya salio por mejor camino
            batch.append((r, key))
        if not batch:
            continue              # el heap solo tenia rancias

        # EL CORTE, ANTES DE PAGAR NINGUNA CONSULTA.  El primero del lote es
        # el de menor regret; si ni el llega, ninguno de los que vienen detras
        # va a llegar tampoco.
        if (ceiling - REGRET_WEIGHT * min(batch[0][0], REGRET_CAP) / 100.0
                < candidates.cut):
            break

        keys = [key for _, key in batch]
        _load_bounded_nodes(node, keys)
        for r, key in batch:
            done[key] = r
            row = node.get(key)
            if row is None:
                continue          # fila que no esta: se salta, nunca KeyError
            # Solo los nodos que la cola puede servir sostienen el corte: un
            # cerrado o una lapida con prioridad alta lo subiria sin ser nunca
            # candidato a nada, y con el corte inflado se podaria de mas.
            if not row.settled and row.priority > DEAD / 2:
                candidates.offer(key, priority_of(
                    row.value, row.expanded, row.visits, row.campaign_id,
                    _runits(r), campaign_votes))

        # Un nodo CERRADO no relaja hacia abajo (misma regla que la version
        # global, y por la misma razon), asi que su adyacencia ni se pide.
        expanding = [key for _, key in batch
                     if key in node and not node[key].settled]
        adjacency = {}
        if expanding:
            for parent, child in Edge.objects.filter(
                    parent_id__in=expanding).values_list('parent_id',
                                                         'child_id'):
                adjacency.setdefault(parent, []).append(child)
        _load_bounded_nodes(node, {child for kids in adjacency.values()
                                   for child in kids})

        for r, key in batch:
            row = node.get(key)
            if row is None or row.settled:
                continue
            kids = adjacency.get(key, ())
            if not kids:
                continue
            seen = [node[c].value for c in kids
                    if c in node and node[c].value is not None]
            best = ((max(seen) if row.white_stm else min(seen))
                    if seen else None)
            for child in kids:
                target = node.get(child)
                if target is None or child in done:
                    continue
                gap = 0.0
                if target.value is not None and best is not None:
                    gap = ((best - target.value) if row.white_stm
                           else (target.value - best))
                nr = r + gap
                if nr < regret.get(child, INF):
                    regret[child] = nr
                    heapq.heappush(heap, (nr, child))
    return done


def _load_bounded_nodes(node, keys):
    """Rellena el cache local con las claves que aun no estan, por lotes."""
    missing = sorted({key for key in keys if key not in node})
    for start in range(0, len(missing), BOUNDED_BATCH):
        chunk = missing[start:start + BOUNDED_BATCH]
        for row in Position.objects.filter(key__in=chunk).values_list(
                'key', 'fen', 'eval_cp', 'backed_eval', 'status', 'expanded',
                'visits', 'campaign_id', 'priority'):
            node[row[0]] = _BoundedNode(*row[1:])


def refresh_priorities_v1(force=False, top_k=None):
    """§4.1 — recalculo global (llamado por el selector). Prioridad =
    cercania al cierre local - regret acumulado desde la raiz - visitas
    + el bono de la campana ACTIVA a la que pertenezca la posicion.
    Respeta las lapidas (las ramas muertas no resucitan).

    EL BONO DE CAMPANA Y SUS TRES LIMITES.  Es un sumando y nada mas: se anade
    al final, sobre la prioridad ya calculada, y por eso no puede reescribir
    ninguna de las decisiones anteriores, solo desempatar a favor de la linea
    que la comunidad voto.  Los limites no son de estilo, son lo que impide
    que un voto compre cosas que un voto no puede comprar:

    1. NO RESUCITA LAPIDAS.  El filtro del bucle ya excluye ``priority <=
       DEAD/2``, asi que una rama enterrada no pasa por aqui ni con mil votos.
       Enterrarla fue una conclusion sobre el arbol; votar es una preferencia
       sobre en que mirar, y una preferencia no revoca una conclusion.
    2. NO PUNTUA LO CERRADO.  El mismo filtro exige ``status='UNKNOWN'``.  Una
       posicion resuelta no vuelve a la cola porque alguien la vote.
    3. NO TOCA LA BANDA USER.  El arriendo ordena por ``-source`` ANTES que
       por nada, y dentro de USER ignora la prioridad de la posicion a
       proposito (``views.api_lease``, ``human_rank``).  El bono vive en
       ``Position.priority``, que es lo que ordena AUTO y FILL: una campana
       muy votada nunca se pone por delante del click de una persona.

    Es O(grafo entero) — Dijkstra sobre todos los nodos y aristas, mas dos
    diccionarios con la base cargada en RAM.  A 450k posiciones son segundos
    de CPU; a 4,5M son decenas de segundos y gigabytes, multiplicados por cada
    proceso de gunicorn que lo dispare.  Por eso ya NO corre dentro de la
    request del worker (ver ``next_tasks``): lo llama el servicio
    ``refresh_selector``, un solo proceso, fuera del camino HTTP.

    ``force`` salta la cache de PRIORITY_REFRESH_SECONDS; el servicio la usa
    para que su propio intervalo sea el unico reloj que manda.

    ``top_k`` lo pone en MODO SOLO CALCULO: no escribe nada y devuelve
    ``{clave: prioridad}`` con los ``top_k`` mejores.  Existe para que el
    sombra (``selector_shadow``) pueda correr los dos motores sobre la MISMA
    base sin que ninguno pise al otro — comparar despues de escribir no
    compara nada.
    """
    now = time.monotonic()
    cached = _priority_refresh_cache
    if (top_k is None and not force and cached['at']
            and now - cached['at'] < PRIORITY_REFRESH_SECONDS):
        return False

    regret = _regret_from_root()
    campaign_votes = _active_campaign_votes()
    collector = _TopK(top_k) if top_k is not None else None
    dirty = []
    for pos in Position.objects.filter(status='UNKNOWN',
                                       priority__gt=DEAD / 2) \
                               .iterator(chunk_size=2000):
        # Mismo criterio que el mapa del Dijkstra: un nodo sin eval
        # propia pero con respaldo decidido no es un nodo sin
        # informacion, ni en un sentido ni en el otro.
        prio = priority_of(best_known_eval(pos), pos.expanded, pos.visits,
                           pos.campaign_id,
                           _runits(regret.get(pos.key, float('inf'))),
                           campaign_votes)
        if collector is not None:
            collector.offer(pos.key, prio)
            continue
        if pos.priority != prio:
            pos.priority = prio
            dirty.append(pos)
    if collector is not None:
        return collector.as_dict()
    for i in range(0, len(dirty), 500):
        Position.objects.bulk_update(dirty[i:i + 500], ['priority'])
    cached['at'] = now
    return True


PRIORITY_WRITE_BATCH = 500


def refresh_priorities_v2(force=False, top_k=None):
    """La MISMA pasada de ``refresh_priorities_v1``, acotada en memoria.

    Tres cosas cambian y ninguna es la formula:

    1. El regret sale del Dijkstra POR LOTES (``_regret_from_root_bounded``),
       que explora la bola competitiva en vez del grafo entero.
    2. El barrido lee COLUMNAS (``values_list`` + ``iterator``), no
       instancias: un ``Position`` completo arrastra ``last_analysis``, la FEN
       y la ``won_line``, y multiplicado por millones de filas eso era la
       mitad del problema.
    3. Las escrituras salen en lotes de ``PRIORITY_WRITE_BATCH`` sobre shells
       ``Position(key=...)`` con SOLO ``priority`` dentro, y la lista se vacia
       en cada vuelta.  La version global acumulaba todas las filas sucias
       antes del primer ``bulk_update``.

    LO QUE PASA CON LO QUE QUEDA FUERA DE LA BOLA, que es el unico sitio donde
    los dos motores pueden diferir: un nodo que el Dijkstra acotado no alcanzo
    cobra 30 unidades si cuelga de la raiz (``reachable``) y 5 si no.  Son las
    dos respuestas que ya daba la version global — la saturacion del regret y
    ``DISCONNECTED_REGRET`` — y con la columna se distinguen sin recorrer nada.
    Por eso el orden del despliegue no es negociable: migracion, luego
    ``backfill_reachable``, y solo entonces el conmutador.

    Y AHI HAY UNA DIFERENCIA DELIBERADA, la unica de todo esto, que conviene
    tener escrita antes de que alguien la encuentre en el sombra.  Un nodo cuyo
    UNICO camino desde la raiz pasa por un nodo CERRADO no lo alcanza ninguno
    de los dos motores (un cierre no relaja hacia abajo, misma regla en los
    dos), pero ``reachable`` si lo marca: la arista existe.  La version global
    le daba las 5 unidades del cajetin suelto — no porque lo hubiera decidido,
    sino porque "no me consta camino" y "no hay camino" eran el mismo ``inf``.
    Aqui cobra 30.  Es el precio correcto de los dos: un nodo enterrado bajo un
    subarbol ya resuelto no merece el beneficio de la duda que se le da a una
    posicion que un humano acaba de pegar en el cajetin.  Y no lo entierra este
    cambio: ``_still_reachable`` sigue siendo quien le pone la lapida cuando
    ``next_tasks`` se lo encuentra.

    ESCRIBIR MIENTRAS SE LEE, Y POR QUE ES SEGURO.  El barrido y los
    ``bulk_update`` se intercalan a proposito (es lo que acota la memoria), asi
    que una fila puede colarse o repetirse si el motor de base decide recorrer
    por el indice de ``priority``.  Repetida da el mismo numero; colada se
    queda con la prioridad de la pasada anterior — sesenta segundos de retraso
    en una heuristica sobre HACIA DONDE MIRAR, que es exactamente la tolerancia
    que esta columna declara desde que dejo de calcularse dentro de la request.
    """
    now = time.monotonic()
    cached = _priority_refresh_cache
    if (top_k is None and not force and cached['at']
            and now - cached['at'] < PRIORITY_REFRESH_SECONDS):
        return False

    campaign_votes = _active_campaign_votes()
    regret = _regret_from_root_bounded(campaign_votes=campaign_votes)
    collector = _TopK(top_k) if top_k is not None else None
    dirty = []
    for (key, eval_cp, backed_eval, expanded, visits, campaign_id, priority,
         reachable) in (Position.objects
                        .filter(status='UNKNOWN', priority__gt=DEAD / 2)
                        .values_list('key', 'eval_cp', 'backed_eval',
                                     'expanded', 'visits', 'campaign_id',
                                     'priority', 'reachable')
                        .iterator(chunk_size=2000)):
        r = regret.get(key)
        runits = (_runits(r) if r is not None
                  else (FAR_REGRET if reachable else DISCONNECTED_REGRET))
        prio = priority_of(known_eval_of('UNKNOWN', backed_eval, eval_cp),
                           expanded, visits, campaign_id, runits,
                           campaign_votes)
        if collector is not None:
            collector.offer(key, prio)
            continue
        if priority != prio:
            dirty.append(Position(key=key, priority=prio))
            if len(dirty) >= PRIORITY_WRITE_BATCH:
                Position.objects.bulk_update(dirty, ['priority'])
                dirty = []
    if collector is not None:
        return collector.as_dict()
    if dirty:
        Position.objects.bulk_update(dirty, ['priority'])
    cached['at'] = now
    return True


def refresh_priorities(force=False, top_k=None):
    """Puerta unica del recalculo: v1 por defecto, v2 con el conmutador.

    Los dos motores viven a la vez y eso es el plan, no un descuido.  El
    sombra (``selector_shadow``) necesita poder correr los dos sobre la misma
    base para publicar Jaccard y Kendall del top-1000 ANTES de que nadie
    conmute nada, y el dia que se conmute hace falta poder volver con una
    variable de entorno y un reinicio, sin desplegar codigo.
    """
    if selector_v2_enabled():
        return refresh_priorities_v2(force=force, top_k=top_k)
    return refresh_priorities_v1(force=force, top_k=top_k)


def _still_reachable(pos):
    """Con todos los padres cerrados, analizarlo ya no influye arriba."""
    parents = Edge.objects.filter(child=pos)
    if not parents.exists():
        return True   # raiz o semilla sin padres
    return parents.filter(parent__status='UNKNOWN').exists()


def budget_for(pos):
    """Escalera por visita + salto directo si el motor ya vio mate.

    CARVE-OUT del salto de banda: una reclamacion de mate CORTO y con
    distancia CONOCIDA no compra la excavacion de 128M, compra su verificacion
    (``_short_mate_clamp``).  La banda SIN distancia — un cp de tablebase
    recortado — y los mates largos siguen saltando a 128M como siempre.

    La escalera por visitas sigue mandando cuando ya pide mas que el clamp, y
    eso es deliberado: dos sondas baratas que no cerraron el nodo son la senal
    de que la reclamacion no se sostiene, y desde ahi escalar es lo correcto.
    El clamp abarata la PRIMERA mirada; no pone un techo permanente ni puede
    dejar un nodo en un bucle de sondas de dos millones.
    """
    budget = BUDGET_LADDER[min(pos.visits, len(BUDGET_LADDER) - 1)]
    clamp = _short_mate_clamp(pos)
    if clamp is not None:
        return max(budget, clamp[0])
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


def selector_v2_enabled():
    """El selector ACOTADO en lugar de la foto global del grafo.

    APAGADO por defecto, y va a seguir apagado hasta que el sombra publique
    sus numeros: v1 sigue siendo lo desplegado y lo medido, y un motor nuevo
    que ordena la cola de toda la flota no se enciende porque los tests pasen.
    El orden completo esta en ``docs/selector-incremental.md``; el resumen es
    migracion, ``backfill_reachable``, sombra, y entonces esta variable.
    """
    return bool(getattr(settings, 'ATOMICDB_SELECTOR_V2', False))


def next_tasks(n):
    """Selector global best-first sobre todo el arbol.

    Sigue siendo GLOBAL: no hay una cola por campana ni un cupo reservado.  Lo
    unico que hacen las campanas activas es pesar mas dentro de la MISMA
    ordenacion (§ ``refresh_priorities``, ``CAMPAIGN_BONUS``), asi que una
    linea votada no puede dejar sin servir al resto del arbol — solo se pone
    delante mientras el resto no traiga algo mas urgente.

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
                      'multipv': multipv_for(pos.visits, budget,
                                             clamp=_short_mate_clamp(pos))})
        if task.state == 'PENDING':
            tasks.append(task)
    return tasks


# Colchon de tareas PENDING que el servicio selector mantiene lleno.  El
# lease solo rellena (4) con la cola VACIA: con decenas de slots consumiendo
# sondas de 128M, eso es operar clavado en cola cero — cada lease vacio paga
# el minteo inline y dos leases concurrentes mintean los MISMOS top-4 (el
# dedup de get_or_create hace que la oferta no escale con los que piden).
# Valles de utilizacion medidos por la flota de Lesha (28-jul).
ANALYSIS_POOL_TARGET = 64


def top_up_analysis_pool(target=None):
    """Rellena la cola de analisis hasta ``target`` FUERA del camino HTTP.

    Devuelve cuantas tareas se mintearon.  Corre en el ciclo del servicio
    selector, DESPUES de los brazos con cupo (cobertura, dn, fragiles) y
    contando SOLO las tareas AUTO: si contase toda la cola, un backlog de
    peticiones USER o de FILL apagaria el colchon justo cuando la flota
    grande lo va a necesitar, y al reves, el colchon les comeria el cupo a
    los brazos si corriese antes."""
    target = ANALYSIS_POOL_TARGET if target is None else target
    pending = AnalysisTask.objects.filter(
        state='PENDING', source=AnalysisTask.Source.AUTO).count()
    if pending >= target:
        return 0
    return len(next_tasks(target - pending))


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
    # Dos presupuestos que no tienen nada que ver: cuantos DESCENSOS se
    # permite este selector, y cuantos NODOS se le encargan a la posicion que
    # encuentre.  Compartian nombre, y el segundo (>= 8.000.000) borraba la
    # cota del primero en cuanto el bucle acertaba una vez — dentro del camino
    # HTTP de /api/lease, que es donde menos gracia tiene.
    attempt_budget = 4 * max(1, n)
    while len(tasks) < n and attempts < attempt_budget:
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
                      'multipv': multipv_for(pos.visits, budget,
                                             clamp=_short_mate_clamp(pos))})
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


def _next_rung(completed_max):
    """El peldano que le toca comprar a lo ya COMPLETADO: el primero por
    encima, o el ultimo cuando no queda ninguno."""
    if completed_max is None:
        return REQUEST_BUDGET_LADDER[0]
    for candidate in REQUEST_BUDGET_LADDER:
        if candidate > completed_max:
            return candidate
    return REQUEST_BUDGET_LADDER[-1]


def request_ladder_state(pos):
    """``(completed_max, peldano que toca)`` de esta posicion, sin escribir.

    Una sola consulta para las dos respuestas.  Existe para que el explorador
    pueda pintar el selector de profundidad con EL MISMO peldano por defecto
    que comprara el click (§ ``depth.context``): dos calculos separados del
    "siguiente peldano" es como una pagina acaba prometiendo un presupuesto y
    la vista encolando otro.
    """
    completed_max = _completed_max_budget(pos)
    return completed_max, _next_rung(completed_max)


def _request_rung(pos, requested_by='', route='', budget_floor=None):
    """Buy the next ladder rung for ONE position. The caller owns the tx.

    Devuelve 'queued' | 'already-queued' | 'already-solved', o el centinela
    interno _LADDER_EXHAUSTED cuando el ultimo peldano ya esta COMPLETED:
    repetirlo seria gastar 10B en una busqueda que ya tenemos.
    ``requested_by`` y ``route`` viajan a la tarea (afinidad y orden de
    jugadas del peticionario); nunca PISAN lo ya guardado (el primer click
    conserva autoria y ruta).

    ``budget_floor`` es el peldano ELEGIDO por quien tiene derecho a elegirlo
    (§ ``depth``), ya validado por la vista.  Solo puede SUBIR el suelo, nunca
    bajarlo: una eleccion no puede abaratar una peticion por debajo de lo que
    la escalera compraria sola, asi que un peldano ya gastado (o cualquier cosa
    por debajo del que toca) no cambia absolutamente nada."""
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
    floor = _next_rung(completed_max)
    clamp = _short_mate_clamp(pos)
    if clamp is not None and clamp[0] > (completed_max or 0):
        # Un mate corto con distancia conocida se VERIFICA, y una verificacion
        # no tiene por que entrar por el suelo de la escalera de peticiones: el
        # click quiere cerrar el nodo, no excavarlo.  La condicion es lo que
        # impide que esto se vuelva un techo — en cuanto lo ya COMPLETADO
        # alcanza al clamp, la escalera recupera el mando y el siguiente click
        # escala como siempre.
        floor = clamp[0]
    floor = max(floor, budget_for(pos))
    if budget_floor:
        # Va el ULTIMO y es un maximo, asi que gana tambien al carve-out del
        # mate corto: ese abarata la PRIMERA mirada de un nodo que reclama M2,
        # y quien elige 10B a mano no esta pidiendo una verificacion barata.
        floor = max(floor, budget_floor)
    task, created = AnalysisTask.objects.get_or_create(
        position=pos, generation=pos.visits,
        defaults={'budget_nodes': floor, 'source': 'USER',
                  'requested_by': requested_by, 'route': route,
                  'multipv': multipv_for(pos.visits, floor, clamp=clamp)})
    if created:
        return 'queued'
    if task.state == 'PENDING':
        task.budget_nodes = max(task.budget_nodes, floor)
        promoted = task.source != 'USER'
        task.source = 'USER'   # promocion: al frente de la cola
        if requested_by and not task.requested_by:
            task.requested_by = requested_by
        if route and not task.route:
            task.route = route
        task.save(update_fields=['source', 'budget_nodes', 'requested_by',
                                 'route'])
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
                    requested_by=requested_by, route=route,
                    multipv=multipv_for(generation, floor, clamp=clamp))
            else:
                follow_up.budget_nodes = max(follow_up.budget_nodes, floor)
                follow_up.source = 'USER'
                if requested_by and not follow_up.requested_by:
                    follow_up.requested_by = requested_by
                if route and not follow_up.route:
                    follow_up.route = route
                follow_up.save(update_fields=['budget_nodes', 'source',
                                              'requested_by', 'route'])
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


def _expand_frontier(parent, limit=None, requested_by='', route=''):
    """Spend an exhausted request one ply deeper, proof-number style.

    Every selected child re-enters the ordinary ladder at its own natural
    floor (typically 128M).  A child whose own ladder is already spent is
    counted and left alone here; following it is the descent's job, and only
    when EVERY candidate at this level is spent.

    ``route`` es la ruta declarada del peticionario hasta ``parent``: cada
    hija compra su tarea con ``ruta + su jugada`` y el aviso de vuelta habla
    en el orden del autor (sin esto, los avisos de un swap salian en linaje
    canonico — mismo bug que el click masivo).
    """
    parent = Position.objects.select_for_update().get(pk=parent.pk)
    expand(parent)   # no-op once the legal edges already exist
    ucis = {}
    if route:
        ucis = {e.child_id: e.move_uci
                for e in Edge.objects.filter(parent=parent)}
    counts = {'children_considered': 0, 'children_queued': 0,
              'children_solved': 0, 'children_exhausted': 0}
    for child in _frontier_children(parent, limit=limit):
        counts['children_considered'] += 1
        child_route = (route + ',' + ucis[child.key]
                       if route and child.key in ucis else '')
        outcome = _request_rung(child, requested_by, child_route)
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


def _descend_frontier(pos, requested_by='', route=''):
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
    node, plies, walked_route = pos, 0, route
    while True:
        remaining = FRONTIER_CLICK_CAP - totals['children_queued']
        if remaining <= 0:
            return _descent_outcome('saturated', 'budget-spent', node,
                                    plies, totals)
        node = _ensure_expanded(node)
        children = _frontier_children(node, limit=remaining)
        spent = _ladder_spent_keys(children)
        if len(spent) < len(children):
            for name, value in _expand_frontier(
                    node, limit=remaining,
                    requested_by=requested_by,
                    route=walked_route).items():
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
        if walked_route:
            # La ruta del autor se alarga con cada paso del descenso; si la
            # arista no aparece (transposicion rara), la ruta se suelta y de
            # ahi para abajo el aviso hablara en canonico — honesto, no roto.
            step = Edge.objects.filter(parent=node, child=following) \
                               .values_list('move_uci', flat=True).first()
            walked_route = (walked_route + ',' + step) if step else ''
        visited.add(following.key)
        node, plies = following, plies + 1


# Punto 2 del breadth-swap: desacuerdo propio-vs-respaldado que ya mueve el
# veredicto.  Por debajo del umbral es ruido de profundidad; por encima, el
# arbol es fino justo donde soporta carga.
UNCERTAINTY_GAP = 150
UNCERTAINTY_EXPAND_CAP = 4
# Excepcion de profundidad del breadth-swap: cuando el analisis nuevo de un
# hijo refuta la PROPIA linea-1 de un padre (el valor del hijo es peor para
# el que mueve que lo que el padre reclamaba, mas alla del umbral), la
# resolucion no es esperar cobertura de hermanos: es re-buscar el padre a
# ancho completo, que re-arbitre entre sus alternativas o conceda.  La
# guarda direccional retiene el valor viejo A PROPOSITO mientras tanto; sin
# este disparador, esa retencion correcta parecia un backprop roto (tres
# avistamientos de comunidad solo el 29-jul).
WITNESS_REFUTED_CAP = 2


def _witness_refuted_revisit(pos, parent_keys):
    """Encola re-busqueda profunda de los padres cuya linea-1 refuto ``pos``.

    Devuelve cuantas se encolaron.  Mismo flag que el resto del breadth-swap:
    es la pata de profundidad de la misma politica."""
    if not getattr(settings, 'ATOMICDB_BREADTH_SWAP', False):
        return 0
    # Relectura minima: la cascada de backed acaba de correr y el objeto en
    # memoria del ingest lleva el respaldo de ANTES de este analisis.
    row = Position.objects.filter(key=pos.key).only(
        'key', 'status', 'eval_cp', 'backed_eval').first()
    if row is None or row.status != 'UNKNOWN':
        return 0
    child_value = (row.backed_eval if row.backed_eval is not None
                   else row.eval_cp)
    if child_value is None or abs(child_value) >= MATE_BAND:
        return 0
    queued = 0
    parents = Position.objects.filter(
        key__in=[key for key in parent_keys if key], status='UNKNOWN')
    for parent in parents:
        if queued >= WITNESS_REFUTED_CAP:
            break
        if parent.eval_cp is None or abs(parent.eval_cp) >= MATE_BAND:
            continue
        lines = parent.last_analysis or []
        first = lines[0] if lines and isinstance(lines[0], dict) else None
        pv = (first or {}).get('pv') or []
        witness = pv[0] if pv else parent.best_move
        if not witness:
            continue
        edge = Edge.objects.filter(parent=parent, move_uci=witness,
                                   child=pos).first()
        if edge is None:
            continue
        mover_white = parent.fen.split()[1] == 'w'
        refuted = (child_value < parent.eval_cp - UNCERTAINTY_GAP
                   if mover_white
                   else child_value > parent.eval_cp + UNCERTAINTY_GAP)
        if not refuted:
            continue
        budget = max(BUDGET_LADDER[1], budget_for(parent))
        task, created = AnalysisTask.objects.get_or_create(
            position=parent, generation=parent.visits,
            defaults={'budget_nodes': budget, 'multipv': DEPTH_MULTIPV,
                      'source': AnalysisTask.Source.AUTO})
        if created:
            queued += 1
            DBEvent.objects.create(kind='WITNESS_REFUTED', payload={
                'parent': parent.key, 'child': pos.key, 'move': witness,
                'claimed': parent.eval_cp, 'child_value': child_value})
    return queued


def _uncertainty_expand(keys):
    """Compra los mejores hijos virgenes alli donde eval propio y backed
    discrepan mas de ``UNCERTAINTY_GAP``.

    Corre tras el refresco de backed de cada ingesta, solo sobre el nodo
    analizado y sus padres directos (coste acotado).  Entra por banda AUTO:
    una expansion del sistema no adelanta a las peticiones humanas.  El dedup
    de tareas y el filtro de hijos virgenes hacen que repetirse sea gratis:
    en cuanto los hijos existen, el disparador se apaga solo."""
    if not getattr(settings, 'ATOMICDB_BREADTH_SWAP', False):
        return 0
    queued_total = 0
    rows = Position.objects.filter(
        key__in=[key for key in keys if key], status='UNKNOWN').only(
        'key', 'fen', 'eval_cp', 'backed_eval', 'visits', 'expanded')
    for row in rows:
        if row.eval_cp is None or row.backed_eval is None:
            continue
        if (abs(row.eval_cp) >= MATE_BAND
                or abs(row.backed_eval) >= MATE_BAND):
            continue
        if abs(row.eval_cp - row.backed_eval) <= UNCERTAINTY_GAP:
            continue
        queued = enqueue_unexplored_children(
            row, cap=2, source=AnalysisTask.Source.AUTO)
        if queued:
            queued_total += queued
            DBEvent.objects.create(kind='UNCERTAINTY_EXPAND', payload={
                'key': row.key, 'own': row.eval_cp,
                'backed': row.backed_eval, 'queued': queued})
        if queued_total >= UNCERTAINTY_EXPAND_CAP:
            break
    return queued_total


def _breadth_swap_eligible(pos):
    """Solo REVISITAS PROFUNDAS de nodos abiertos fuera de la banda de mate.

    La primera pasada siembra (MultiPV 5) y no se toca; un nodo en banda de
    mate quiere profundidad para extraer la PV entera; y un nodo cerrado no
    compra nada.  Medido en produccion (29-jul, n=800): un ply de hijos a
    128M reproduce el veredicto del re-search profundo el 96-99% de las
    veces — el peldano profundo por defecto compraba veredicto ya sabido.

    Y EL UMBRAL DE PROFUNDIDAD (Wolfram, 30-jul): aquella medicion era
    sobre re-searches de nodos que YA tenian su mirada profunda.  Con solo
    la primera pasada de 128M completada, el click siguiente es exactamente
    la primera busqueda honda MultiPV 2 — la que orienta el mejor camino —
    y convertirla en anchura le negaba al peticionario la informacion que
    estaba comprando ("I specifically wanted the high depth eval of the
    parent so the engine can hint me the best path").  El swap solo actua
    cuando el peldano de 512M ya esta gastado; hasta entonces, la escalera
    clasica manda."""
    if not getattr(settings, 'ATOMICDB_BREADTH_SWAP', False):
        return False
    if pos.status != 'UNKNOWN':
        return False
    if abs(pos.eval_cp or 0) >= MATE_BAND:
        return False
    completed = _completed_max_budget(pos)
    return completed is not None and completed >= REQUEST_BUDGET_LADDER[1]


def request_analysis(pos, requested_by='', route='', budget_floor=None):
    """Peticion publica: encola (o promociona) la tarea de esta posicion.
    Suelo de 128M: quien pide analisis merece profundidad de verdad.
    ``requested_by`` (cuenta OB del visitante logueado, o vacio) viaja hasta
    las tareas para la afinidad worker-peticionario del lease; ``route``
    (UCIs ya VALIDADOS por la vista) para pintar la peticion en el orden de
    jugadas de su autor.

    Con ``ATOMICDB_BREADTH_SWAP`` activo, una peticion sobre un nodo ya
    analizado compra ANCHURA en vez del siguiente peldano profundo: la
    frontera un ply mas abajo (estilo proof-number search), descendiendo por
    el hijo mas prometedor si hace falta.  Si el descenso se declara
    saturado, la informacion marginal vuelve a ser la profundidad y se cae
    al peldano clasico.  Sin el flag, la escalera de siempre; agotada
    (10B ya COMPLETED), la peticion se convierte en la misma expansion.
    ``budget_floor`` (peldano elegido a mano, ya validado por la vista) APAGA
    ese swap: el swap existe porque la informacion marginal de un click POR
    DEFECTO suele estar en la anchura, y quien elige un peldano esta diciendo
    exactamente lo contrario — "quiero la eval profunda de este nodo para que
    el motor me oriente el mejor camino", que es la queja que acoto el swap en
    primer lugar (§ ``_breadth_swap_eligible``).

    Devuelve 'queued' | 'already-queued' | 'already-solved' | 'expanded'
    | 'saturated'."""
    with atomic():
        swapped = False
        if budget_floor is None and _breadth_swap_eligible(pos):
            swapped = True
            outcome = _descend_frontier(pos, requested_by=requested_by,
                                        route=route)
            if outcome != 'saturated':
                DBEvent.objects.create(kind='BREADTH_SWAP', payload={
                    'key': pos.key, 'outcome': str(outcome),
                    **{name: value
                       for name, value in getattr(outcome, 'detail',
                                                  {}).items()
                       if isinstance(value, (int, str))}})
                return outcome
        outcome = _request_rung(pos, requested_by, route, budget_floor)
        if outcome != _LADDER_EXHAUSTED:
            return RequestOutcome(outcome)
        if swapped:
            return RequestOutcome('saturated')
        return _descend_frontier(pos, requested_by=requested_by, route=route)


def notification_deserved(source, budget_nodes):
    """Que tareas servidas merecen avisar a quien las pidio.

    La vuelta del circuito de ``request_analysis``, y con el mismo criterio
    que lo abrio: una tarea es de una PERSONA si entro por la banda USER o,
    como red de seguridad, si lleva al menos el suelo de la escalera de
    peticiones.  Lo segundo no es redundante — ``enqueue_unexplored_children``
    puede DEGRADAR a AUTO una tarea PENDING que nacio de un click y que
    conserva su ``requested_by`` — y por eso el criterio es una O y no una Y.

    Lo que queda fuera es lo que llenaria la campana de ruido: las semillas de
    cobertura (``FILL``, 8M, jugadas concretas por ``searchmoves``) y la
    exploracion autonoma del selector.  Ninguna de las dos la pidio nadie, y
    una expansion de cobertura puede encolar doscientas de golpe.

    Quien pide una EXPANSION no necesita nada especial aqui: las tareas hijas
    se crean con el mismo ``requested_by`` y por la misma banda USER, asi que
    el criterio por-tarea ya las cubre — cada hija que aterriza es un aviso
    hacia la posicion concreta que la persona podia querer ver.
    """
    return (source == AnalysisTask.Source.USER
            or (budget_nodes or 0) >= NOTIFY_MIN_BUDGET)


def _queue_disputed_reanalysis(pos):
    """Queue one maximum-budget follow-up without disturbing live leases."""
    pending = (AnalysisTask.objects.filter(position=pos, state='PENDING')
               .order_by('-generation').first())
    if pending is not None:
        # Un reanalisis por testigo refutado quiere PROFUNDIDAD por
        # definicion, tenga las visitas que tenga: es exactamente el caso en
        # el que la anchura ya demostro no estar viendo el fondo.  Intencion
        # explicita, no derivada de la politica por visitas.
        pending.budget_nodes = max(pending.budget_nodes, BUDGET_LADDER[-1])
        pending.multipv = DEPTH_MULTIPV
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
        multipv=DEPTH_MULTIPV, source='AUTO')


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
    from . import solve, survive
    from .models import SolveTask

    disputed = False

    try:
        elapsed = max(0.0, min(float(elapsed_seconds or 0), 86_400.0))
    except (TypeError, ValueError):
        elapsed = 0.0

    verified, report, reason = False, None, ''
    verify_seconds = 0.0
    survival = outcome == 'DISPROVED_WHITE_WIN'
    if outcome == 'PROVED' or survival:
        if not certificate_blob:
            reason = f'{outcome} without a certificate'
        else:
            started = time.monotonic()
            try:
                if survival:
                    # SURVIVE50.  A different proof object, a different
                    # verifier, and the routing picks the native one above the
                    # size where the reference stops being affordable.
                    text = survive.decompress(certificate_blob)
                    report = survive.verify_certificate_auto(
                        text, root_fen=task.position.fen)
                else:
                    text = solve.decompress(certificate_blob)
                    report = solve.verify_certificate(
                        text, root_fen=task.position.fen, goal=task.goal)
                verified = True
            except solve.CertificateError as error:
                reason = str(error)
            except Exception as error:          # movegen/parse surprises
                reason = f'{type(error).__name__}: {error}'
            verify_seconds = time.monotonic() - started

    with atomic(), closure_attribution(CLOSURE_SOURCE_SOLVE):
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
        if (outcome == 'PROVED' or survival) and not verified:
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
            current.certificate_nodes = (report['states'] if survival
                                         else report['nodes'])
            current.certificate_format = (survive.CERTIFICATE_FORMAT if survival
                                          else solve.CERTIFICATE_FORMAT)
            if survival:
                current.survival_tau = report['root_tau']
                current.survival_states = report['states']
        current.save()

        closed = upgraded = False
        if survival and verified:
            # DELIBERATELY NOT A CLOSURE.  This refutes the boolean objective
            # WHITE_WIN and says nothing about BLACK_WIN: Black survives, which
            # is compatible with a draw AND with a Black win, and telling those
            # apart needs a different proof.  ``Position.status`` has no
            # NOT_WHITE_WIN, and inventing one out of this certificate is
            # exactly the conflation doc 18 §6.1 forbids.  So the fact lands on
            # the task and in the event log, the orchestrator drops the White
            # candidate, and the position stays as open as it was.
            DBEvent.objects.create(kind='SURVIVE_VERIFIED', payload={
                'task': current.pk, 'key': current.position_id,
                'seconds': round(verify_seconds, 4),
                'verifier': report.get('verifier', 'reference'),
                'tau': report['root_tau'],
                'entry_clock': report['entry_clock'],
                'states': report['states'], 'edges': report['edges'],
                'reachable': report['reachable'],
                'positions': report.get('positions', 0),
                'bytes': current.certificate_bytes,
                'stage': current.budget_stage,
                'solver_seconds': elapsed})
        elif verified:
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
        # La cascada tambien va bajo la etiqueta: un certificado que cierra su
        # posicion cierra ademas ancestros por MINIMAX, y esos cierres son
        # tan del SOLVE como el primero.
        with closure_attribution(CLOSURE_SOURCE_SOLVE):
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
# CUPOS POR BRAZO.  El tope de FILL se media sobre ``source='FILL'``, que es la
# MEZCLA de tres productores repartidos en dos procesos: el convergedor de
# calidad (dentro del submit), el completado de cobertura y la reparacion de dn
# (dentro del servicio selector).  Cada uno leia la cola de los otros como si
# fuera suya, asi que el que llegaba segundo se encontraba el cupo gastado sin
# haber encolado nada — y las promociones AUTO->FILL engordaban esa poblacion
# sin contar contra el ``made`` de nadie.  Ahora cada brazo cuenta SU marca,
# como ya hacia el brazo fragil con ``arm=FRAGILE_ARM``.
#
# Los tres cupos suman los 200 de antes: el limite global sigue queriendo decir
# lo mismo, pero ahora cada brazo tiene su parte GARANTIZADA en vez de
# competida.  Cobertura se lleva la mitad porque es el unico que cierra nodos;
# los otros dos informan.
COVERAGE_ARM = 'coverage'
DN_ARM = 'dn'
QUALITY_ARM = 'quality'
COVERAGE_QUEUE_CAP = 100
DN_REPAIR_QUEUE_CAP = 50
QUALITY_QUEUE_CAP = 50
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
    es a la vez lo mas barato y lo mas productivo.  El tope se cuenta sobre la
    marca de ESTE brazo (§ cupos por brazo), no sobre todo lo que sea FILL.
    """
    pending = AnalysisTask.objects.filter(
        state='PENDING', arm=COVERAGE_ARM).count()
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
                          'source': AnalysisTask.Source.FILL,
                          'arm': COVERAGE_ARM})
            if created:
                made += 1
            elif task.state == 'PENDING' \
                    and task.source == AnalysisTask.Source.AUTO:
                # Promocionarla mete una tarea mas en la cola de este brazo,
                # asi que cuesta cupo igual que crearla: contarla como gratis
                # era justo lo que dejaba crecer la poblacion por encima del
                # tope.  Se escribe sobre el estado leido y solo cuenta quien
                # encuentra la tarea todavia como la vio.
                promoted = AnalysisTask.objects.filter(
                    id=task.id, state='PENDING',
                    source=AnalysisTask.Source.AUTO).update(
                        source=AnalysisTask.Source.FILL, arm=COVERAGE_ARM)
                if promoted:
                    made += 1
    if made:
        DBEvent.objects.create(kind='COVERAGE_ENQUEUED', payload={
            'created': made, 'pending_before': pending, 'cap': int(cap),
            'scanned': len(candidates)})
    return made


# ---------------- reparacion de dn: el brazo adversarial ----------------
#
# EL HALLAZGO DE LA COMUNIDAD.  Wolfram comparo sus lineas exploradas a mano
# con las que dejo el selector automatico y encontro una diferencia que no es
# de gusto: las humanas acumulan ``dn`` ALTO — cada respuesta del defensor que
# alguien miro es una via mas de refutacion que habria que cerrar — mientras
# el selector deja ESPINAS con ``dn`` 1 o 2.  Una espina es una afirmacion a
# UNA pregunta sin responder de derrumbarse: basta que la unica replica viva
# resulte buena para que el subarbol entero deje de valer.  El arbol de prueba
# humano es mas robusto porque el humano se refuta a si mismo.
#
# QUE HACE ESTO.  Que el explorador automatico haga eso mismo: cuando un nodo
# AND del frente esta por debajo del suelo, se le compran las replicas del
# defensor que nadie ha mirado.  Si aguantan, el nodo sube de dn y la
# afirmacion queda de verdad sostenida; si una de ellas refuta, mejor
# enterarse ahora que despues de construir cien plies encima.
#
# QUE NO HACE.  No toca ``api_lease``.  Las tareas salen como ``FILL``, que ya
# existe y ya se sirve DESPUES de ``USER`` por el orden alfabetico descendente
# de ``-source``; el orden de servicio no se toca porque no hace falta
# tocarlo.  Y esta acotado dos veces: por su cupo PROPIO
# (``DN_REPAIR_QUEUE_CAP``, contado sobre la marca de este brazo y no sobre
# todo lo que sea FILL) y por un tope propio por ciclo.
#
# Suelo 2, no 1.  ``dn`` 1 es la espina pura; ``dn`` 2 es la espina con una
# sola replica mirada, que sigue siendo una afirmacion que dos preguntas
# tumban.  Subirlo mas convertiria el brazo en exploracion normal disfrazada.
DN_REPAIR_FLOOR = 2
# Cuantas replicas sin mirar se compran por nodo.  Tres, como el completado de
# cobertura: lo bastante para que el ``dn`` deje de ser trivial, lo bastante
# poco para que un nodo con setenta respuestas no se coma el ciclo entero.
DN_REPAIR_REPLIES = 3
# Tope por pasada del servicio del selector.  El brazo corre en cada ciclo, no
# una vez: repartirlo en el tiempo es lo que lo mantiene detras de lo urgente.
DN_REPAIR_MAX_PER_CYCLE = 24
# Cuantos nodos finos se MIRAN por pasada, se compre algo en ellos o no.  Sin
# esto el coste de una pasada lo fija el numero de espinas del arbol, no una
# constante: un frente con miles de nodos finos ya mirados haria dos consultas
# por cada uno para acabar sin encolar nada.
#
# El riesgo de mirar siempre los mismos primeros N esta acotado por como se
# vacia el conjunto: un nodo fino con replicas sin mirar sube de dn en cuanto
# se compran, y uno permanentemente fino sin nada que comprar es un nodo cuyo
# pn ya es infinito — es decir, refutado — y el frente no lo incluye.
DN_REPAIR_MAX_NODES = 200
DN_REPAIR_SEED_NODES = COVERAGE_SEED_NODES


def _dn_repair_replies(parent, replies):
    """Las replicas sin mirar de un nodo AND, en el orden que no tenemos.

    ``unexplored_children`` es exactamente el predicado que hace falta — ni
    status, ni respaldo, ni eval propia — y devuelve en orden de movegen.  No
    hay nada mejor: una replica que el motor hubiera rankeado en su MultiPV
    tendria ``eval_cp`` sembrada por ``ingest_analysis`` y por tanto NO estaria
    en esta lista.  Las que quedan son las que ninguna busqueda menciono, asi
    que el orden de movegen es honesto (es el mismo argumento que
    ``FRONTIER_BLIND_WIDTH``) y ademas reproducible bajo replay.
    """
    return unexplored_children(parent)[:max(0, int(replies))]


def enqueue_dn_repair(cap=DN_REPAIR_QUEUE_CAP, floor=DN_REPAIR_FLOOR,
                      replies=DN_REPAIR_REPLIES,
                      per_cycle=DN_REPAIR_MAX_PER_CYCLE,
                      max_nodes=DN_REPAIR_MAX_NODES, campaigns=None):
    """Compra replicas sin mirar en los nodos AND finos del frente de prueba.

    Devuelve cuantas tareas se crearon.  Emite un ``DN_REPAIR`` por tanda con
    los conteos, que es de donde sale el KPI.
    """
    pending = AnalysisTask.objects.filter(
        state='PENDING', arm=DN_ARM).count()
    room = min(max(0, int(cap) - pending), max(0, int(per_cycle)))
    if room <= 0:
        return 0
    if campaigns is None:
        campaigns = proof.active_campaigns()
    if not campaigns:
        return 0

    made, nodes_repaired, scanned = 0, 0, 0
    for campaign in campaigns:
        if made >= room or scanned >= max_nodes:
            break
        for key, _fen, _pn, dn in proof.frontier_and_rows(campaign):
            if made >= room or scanned >= max_nodes:
                break
            if dn > floor:
                # El frente viene ordenado por pn, no por dn: aqui no se puede
                # cortar, hay que seguir mirando.  Y no cuenta como mirado:
                # descartarlo cuesta una comparacion, no una consulta.
                continue
            scanned += 1
            # ``expanded``, y no "tiene aristas": a un nodo sin expandir no le
            # faltan REPLICAS que mirar, le falta correr el movegen, y de eso
            # ya se encarga el analisis normal cuando le toque.  Contar sus
            # aristas parciales como si fueran la lista legal entera es
            # exactamente el agujero que la guarda de cobertura tapa.
            parent = Position.objects.filter(key=key, status='UNKNOWN',
                                             expanded=True).first()
            if parent is None:
                continue
            queued_here = 0
            for child in _dn_repair_replies(parent, replies):
                if made >= room:
                    break
                budget = max(DN_REPAIR_SEED_NODES, budget_for(child))
                task, created = AnalysisTask.objects.get_or_create(
                    position=child, generation=child.visits,
                    defaults={'budget_nodes': budget,
                              # Anchura de PRIMERA MIRADA, no la de cobertura.
                              # El completado de cobertura pide profundidad
                              # porque va a cerrar un nodo; esto va a INFORMAR
                              # una replica virgen, que es para lo que la
                              # politica de la casa reserva MultiPV 5.
                              'multipv': multipv_for(child.visits, budget),
                              'source': AnalysisTask.Source.FILL,
                              'arm': DN_ARM})
                if created:
                    made += 1
                    queued_here += 1
                elif (task.state == 'PENDING'
                      and task.source == AnalysisTask.Source.AUTO):
                    # Promocionarla engorda la cola de este brazo igual que
                    # crearla, asi que cuesta cupo igual que crearla.
                    promoted = AnalysisTask.objects.filter(
                        id=task.id, state='PENDING',
                        source=AnalysisTask.Source.AUTO).update(
                            source=AnalysisTask.Source.FILL, arm=DN_ARM)
                    if promoted:
                        made += 1
                        queued_here += 1
            if queued_here:
                nodes_repaired += 1
    if made:
        DBEvent.objects.create(kind='DN_REPAIR', payload={
            'queued': made, 'nodes': nodes_repaired, 'examined': scanned,
            'floor': int(floor), 'pending_before': pending,
            'cap': int(cap), 'per_cycle': int(per_cycle)})
    return made


# ---------------- afirmaciones FRAGILES de mate ----------------
#
# EL CASO EXACTO (Wolfram, 28-jul).  Un nodo publicaba ``backed_eval`` 9994 —
# banda de mate — sobre territorio en el que nadie habia corrido un analisis:
# un visitante habia CAMINADO una linea hasta un mate terminal y el valor
# subio con peso de prueba por toda la cadena.  El corte de autoridad de
# ``_backed_for`` ya degrada la CALIDAD de ese valor en cuanto la cobertura es
# parcial, que es lo correcto y basta para que no envenene decisiones.  Pero
# deja la pregunta en pie: o ese mate existe, y entonces cerrar el nodo vale
# muchisimo, o no existe, y entonces la afirmacion es ruido caro.
#
# Un F0 de 2M la contesta en segundos.  Es el uso mas barato que tiene el
# solver y es exactamente el que el piloto no probo.
#
# LA LECCION DEL PILOTO, CLAVADA AQUI.  23 de las 36 tareas del piloto fueron
# DISPROVED instantaneos porque se preguntaba WHITE_WIN sobre posiciones con
# eval -10000 — es decir, se preguntaba si ganaba el bando que estaba
# perdiendo.  El objetivo va POR SIGNO del valor respaldado, siempre.
FRAGILE_ARM = 'fragile'
# Cupo PROPIO y pequeno.  Separado del de la deuda a proposito: son dos
# apetitos distintos y compartir cap significaria que una purga de deuda mata
# este brazo o al reves.  Pequeno porque cada tarea es una pregunta concreta,
# no una barrida.
FRAGILE_QUEUE_CAP = 50
FRAGILE_SCAN_ROWS = 2_000
# Mismo peldano F0 que la deuda HOY.  Constante propia para que ajustar uno no
# mueva el otro sin querer: son dos politicas que coinciden en un numero, no
# un numero compartido.
FRAGILE_STAGE_NODES = DEBT_STAGE_NODES


def fragile_mate_claims(scan=FRAGILE_SCAN_ROWS):
    """Nodos UNKNOWN que AFIRMAN mate y no lo tienen mirado por todos lados.

    Se escanea por ``updated`` descendente y con tope, igual que el completado
    de cobertura: una afirmacion fragil aparece justo donde algo acaba de
    moverse.  Devuelve ``[(Position, goal)]``, con el objetivo ya resuelto POR
    SIGNO.
    """
    rows = list(Position.objects.filter(status='UNKNOWN').filter(
        Q(backed_eval__gte=MATE_BAND) | Q(backed_eval__lte=-MATE_BAND)
    ).order_by('-updated')[:scan])
    if not rows:
        return []
    children = _backed_children_by_parent([row.key for row in rows])
    found = []
    for row in rows:
        if not coverage_is_partial(row, children.get(row.key, ())):
            continue
        goal = ('WHITE_WIN' if row.backed_eval > 0 else 'BLACK_WIN')
        found.append((row, goal))
    return found


def enqueue_fragile_mate_solves(cap=FRAGILE_QUEUE_CAP, scan=FRAGILE_SCAN_ROWS):
    """Pone las afirmaciones fragiles de mate delante del solver, a F0.

    Devuelve cuantas tareas se crearon.  El resultado ``DISPROVED`` que vuelva
    sigue siendo advisory y no cierra nada: sobre una posicion ``UNKNOWN``,
    ``_dispute_from_solver`` no tiene status que discutir y se limita a dejar
    la senal.  Esa semantica es de doc 18 y aqui no se toca.
    """
    from .models import SolveTask

    pending = SolveTask.objects.filter(state='PENDING',
                                       arm=FRAGILE_ARM).count()
    room = max(0, int(cap) - pending)
    if room <= 0:
        return 0

    candidates = fragile_mate_claims(scan=scan)
    if not candidates:
        return 0
    keys = [row.key for row, _goal in candidates]
    # Dedup en dos direcciones: nada que este brazo ya pregunto (aunque haya
    # terminado — repetir la misma pregunta al mismo presupuesto da la misma
    # respuesta) y nada que tenga YA un solve vivo por cualquier otra via.
    taken = set(SolveTask.objects.filter(
        position_id__in=keys, arm=FRAGILE_ARM,
        state__in=('PENDING', 'LEASED', 'COMPLETED'),
    ).values_list('position_id', flat=True))
    taken |= set(SolveTask.objects.filter(
        position_id__in=keys, state__in=('PENDING', 'LEASED'),
    ).values_list('position_id', flat=True))

    made = []
    for row, goal in candidates:
        if len(made) >= room:
            break
        if row.key in taken:
            continue
        taken.add(row.key)
        made.append(SolveTask(
            position=row, goal=goal, budget_stage='F0',
            budget_nodes=FRAGILE_STAGE_NODES, arm=FRAGILE_ARM))
    if not made:
        return 0
    SolveTask.objects.bulk_create(made, ignore_conflicts=True)
    DBEvent.objects.create(kind='FRAGILE_ENQUEUED', payload={
        'created': len(made), 'pending_before': pending, 'cap': int(cap),
        'candidates': len(candidates),
        'white': sum(1 for task in made if task.goal == 'WHITE_WIN'),
        'black': sum(1 for task in made if task.goal == 'BLACK_WIN')})
    return len(made)


def adversarial_arms_enabled():
    """Interruptor de despliegue de los dos brazos adversariales.

    Apagado por DEFECTO, y no por prudencia generica: el paquete existe para
    MEDIR si el explorador que se refuta a si mismo cierra mas, y esa medida
    necesita un "antes" tomado con el codigo ya desplegado y los brazos
    todavia quietos.  Se enciende con ``ATOMICDB_ADVERSARIAL = True`` cuando
    el snapshot de referencia esta en la tabla.
    """
    return bool(getattr(settings, 'ATOMICDB_ADVERSARIAL', False))


# Un click de visitante pide COMO MUCHO esto.  El completado automatico de
# cobertura se limita a tres jugadas porque busca cerrar un nodo; esto es otra
# cosa — "mirad todo lo que aqui no se ha mirado" — y sesenta y cuatro es una
# expansion generosa que sigue siendo una sola decision humana.
UNEXPLORED_CLICK_CAP = 64


def is_unexplored(child):
    """SIN EXPLORAR: el arbol no sabe NADA de esta jugada todavia.

    Ni status, ni respaldo, ni eval propia.  Un hijo con respaldo pero sin
    eval propia NO cuenta: de ese ya sabemos algo, y el boton de la pagina
    existe para los huecos, no para re-pedir lo que ya tiene valor.

    Vive aqui, con nombre, porque la pagina tiene que ETIQUETAR con el mismo
    criterio con el que el boton CUENTA: mientras fueron dos predicados, el
    boton ofrecia "analizar 20 respuestas sin explorar" encima de una tabla
    con cero filas asi.
    """
    return (child.status == 'UNKNOWN' and child.eval_cp is None
            and child.backed_eval is None)


def unexplored_children(pos):
    """Los hijos MATERIALIZADOS de ``pos`` que siguen sin explorar."""
    return [edge.child for edge in
            Edge.objects.filter(parent=pos).select_related('child')
            .order_by('id')
            if is_unexplored(edge.child)]


def enqueue_unexplored_children(pos, cap=UNEXPLORED_CLICK_CAP,
                                source=AnalysisTask.Source.USER,
                                requested_by='', route=''):
    """Encola las jugadas sin mirar de ``pos``. Devuelve cuantas se encolaron.

    Es ``enqueue_coverage_completion`` sin su guarda de unilateralidad: alli
    el sistema decide que un nodo esta a punto de cerrarse, aqui lo decide una
    persona que esta mirando la pagina.  Lo que si comparte es el dedup: una
    jugada que ya tiene tarea viva no gasta cupo ni crea una segunda.

    ``route`` es la ruta declarada del peticionario HASTA ``pos``; cada tarea
    hija viaja con ``ruta + su jugada`` para que el aviso de vuelta cuente la
    linea en el orden del autor (el click masivo era el unico camino USER que
    perdia la ruta, y sus avisos salian en el linaje canonico).
    """
    edges_by_child = {}
    if route:
        edges_by_child = {e.child_id: e.move_uci
                          for e in Edge.objects.filter(parent=pos)}
    queued = 0
    for child in unexplored_children(pos):
        if queued >= cap:
            break
        clamp = _short_mate_clamp(child)
        if clamp is None:
            # Un click humano compra sondas de grado peticion, no semillas de
            # cobertura: el primer analisis de cada respuesta entra por el
            # primer peldano de la escalera de peticiones (orden 28-jul, 512M).
            budget = max(REQUEST_BUDGET_LADDER[0], budget_for(child))
        else:
            # Salvo que la respuesta ya reclame un mate corto con distancia
            # conocida: eso se verifica barato.  Hoy es una guarda estructural
            # mas que un caso vivo — ``unexplored_children`` exige que el hijo
            # no tenga NI eval propia NI respaldo, asi que lo normal es que no
            # haya distancia que leer y este brazo no se active.  Esta escrito
            # para que la politica no dependa de ese filtro: si algun dia un
            # hijo llega aqui con distancia, no se le compra una excavacion.
            # ``budget_for`` ya trae la escalera por visitas, que sigue
            # mandando cuando pide mas que el clamp.
            budget = budget_for(child)
        child_route = ''
        if route and child.key in edges_by_child:
            child_route = route + ',' + edges_by_child[child.key]
        task, created = AnalysisTask.objects.get_or_create(
            position=child, generation=child.visits,
            defaults={'budget_nodes': budget,
                      'multipv': multipv_for(child.visits, budget,
                                             clamp=clamp),
                      'source': source, 'requested_by': requested_by,
                      'route': child_route})
        if created:
            queued += 1
        elif task.state == 'PENDING' and task.source != source:
            task.source = source
            if requested_by and not task.requested_by:
                task.requested_by = requested_by
            if child_route and not task.route:
                task.route = child_route
            task.save(update_fields=['source', 'requested_by', 'route'])
            queued += 1
    return queued


# ---------------- verificar la PV vigente de una posicion ----------------
#
# EL CASO QUE LO PIDIO (Wolfram, 30-jul).  Un nodo dice +9 a mucha profundidad
# y su respaldo dice +6.  Una de las dos afirmaciones es falsa: o la PV propia
# reclama una linea que los analisis de los hijos no sostienen, o el respaldo
# viene de un subarbol que nadie ha mirado lo suficiente.  El unico modo de
# saber cual era pedir analisis a mano, posicion por posicion, bajando por la
# linea — que es lo que este boton hace de una vez.
#
# DIECISEIS PLIES.  Es una sola decision humana, igual que el boton masivo, asi
# que su coste tiene que estar acotado por algo que no dependa de lo que el
# motor decidiera guardar.  Ocho jugadas por bando es donde una discrepancia de
# tres peones ya se ha manifestado; mas alla, lo que hace falta no es este
# boton sino una campana.  ``STORED_PV_MAX_PLIES`` (24) recorta lo que se
# ALMACENA y no vale como tope aqui: es un limite de disco, no de gasto.
PV_VERIFY_MAX_PLIES = 16


def enqueue_pv_verification(pos, requested_by='', route=''):
    """Encola analisis por CADA posicion de la PV vigente. Devuelve cuantos.

    Camina la linea 1 de ``last_analysis`` — la VIGENTE, saltando el escaparate
    ancho de un pase anterior, con el mismo criterio que ``claimed_mate_plies``
    y por la misma funcion que lo dice, ``solve_estimate.current_line``.

    QUE MATERIALIZA.  Lo mismo que un click de navegacion y por las mismas tres
    llamadas que usa el ``goto`` del explorador: legalidad por
    ``logic.legal_moves``, nodo por ``get_or_create_position`` heredando la
    campana del padre, y arista por ``Edge.get_or_create``.  Aqui no se
    reimplementa ninguna regla; una PV es una ruta como cualquier otra y tiene
    que producir exactamente el mismo arbol que producirla a mano.

    QUE CORTA Y QUE SALTA, que no es lo mismo:

      * CORTA — y sin error — en cuanto una jugada de la PV no es legal en la
        posicion en la que toca.  Pasa de verdad: una PV guardada antes de un
        rekey, o simplemente vieja, deja de aplicar y lo unico honesto es
        quedarse con el prefijo que si aplica.  Tambien corta, por el mismo
        camino, al llegar a un TERMINAL: ahi no hay jugada legal que seguir.
      * SALTA una posicion YA CERRADA y SIGUE CAMINANDO.  Un cierre a mitad de
        linea no invalida el resto — la discrepancia que se esta persiguiendo
        puede estar debajo — y comprarle analisis a un nodo resuelto es gastar
        en una pregunta ya contestada.

    NO se pide nada sobre ``pos``: su eval profundo es justamente la mitad del
    desacuerdo que hay que contrastar, y ya esta comprado.  Lo que falta es la
    otra mitad, que vive en la linea.

    Presupuesto: ``max(REQUEST_BUDGET_LADDER[0], budget_for(child))``, el mismo
    suelo de grado peticion que el boton masivo.  ``budget_for`` manda cuando
    pide mas, y con el viaja su clamp de mates cortos: un nodo de la PV que ya
    reclama un mate corto con distancia conocida se VERIFICA barato en vez de
    excavarse.  Dedup como siempre: una tarea viva en la generacion actual no
    se duplica — y un nodo que ya tiene COMPLETADO ese presupuesto o mas se
    salta entero: la linea se camina igual, pero no se le compra una busqueda
    mas floja que la que ya tiene guardada.

    El llamante es dueno de la transaccion, igual que en
    ``enqueue_unexplored_children``.
    """
    line = solve_estimate.current_line(pos)
    pv = (line or {}).get('pv')
    if not isinstance(pv, list):
        return 0
    queued = 0
    current = pos
    # La ruta declarada del peticionario se alarga con cada ply caminado: el
    # aviso de cada nodo de la PV vuelve contando SU linea, no la canonica.
    walked_route = route
    for uci in pv[:PV_VERIFY_MAX_PLIES]:
        if not isinstance(uci, str) \
                or uci not in logic.legal_moves(current.fen):
            break
        if walked_route:
            walked_route = walked_route + ',' + uci
        child = get_or_create_position(logic.apply_move(current.fen, uci),
                                       campaign_id=current.campaign_id)
        Edge.objects.get_or_create(parent=current, move_uci=uci,
                                   defaults={'child': child})
        if child.priority <= DEAD / 2:
            child.priority = 0.0   # ruta nueva: revive de la lapida
            child.save(update_fields=['priority'])
        current = child
        if child.status != 'UNKNOWN':
            continue
        clamp = _short_mate_clamp(child)
        budget = (max(REQUEST_BUDGET_LADDER[0], budget_for(child))
                  if clamp is None else budget_for(child))
        # Lo que este nodo YA tiene comprado manda sobre el suelo de peticion.
        # El suelo existe para que un click no pague calderilla, no para
        # encargar una busqueda peor que la guardada: un hijo con 512M
        # COMPLETADOS no necesita los 128M del boton del padre, y encargarlos
        # gastaba computo donado en repetir la respuesta que ya estaba.  Vale
        # para las dos ramas del presupuesto — el clamp de mate corto pide
        # aun menos, asi que lo respeta con mas razon.
        done = _completed_max_budget(child)
        if done is not None and done >= budget:
            continue
        task, created = AnalysisTask.objects.get_or_create(
            position=child, generation=child.visits,
            defaults={'budget_nodes': budget,
                      'multipv': multipv_for(child.visits, budget,
                                             clamp=clamp),
                      'source': AnalysisTask.Source.USER,
                      'requested_by': requested_by,
                      'route': walked_route})
        if created:
            queued += 1
        elif (task.state == 'PENDING'
                and task.source != AnalysisTask.Source.USER):
            task.source = AnalysisTask.Source.USER
            if requested_by and not task.requested_by:
                task.requested_by = requested_by
            if walked_route and not task.route:
                task.route = walked_route
            task.save(update_fields=['source', 'requested_by', 'route'])
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
    _emit_closure_events(pos, certificate_nodes=report['nodes'],
                        depth=report['depth'],
                        clock_slack=report.get('clock_slack'), task=task.pk)
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
        _emit_closure_events(pos, dtz=dtz, dtz_verified=False,
                             clock_slack=pos.clock_slack)
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
