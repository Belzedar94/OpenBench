"""Gestor de prueba de AtomicDB: pn/dn por CAMPANA, no por posicion.

POR QUE UN MODULO APARTE
------------------------
El DAG universal (``Position``/``Edge``) es una cache de posiciones: evals que
se comparten por transposicion, listas legales, hechos exactos reutilizables.
Los numeros de prueba no son propiedad de una posicion: dependen de la
PROPOSICION que se intenta demostrar (``WHITE_WIN`` o ``BLACK_WIN``), de la
raiz de la campana, de la politica de repertorio y de la version del
algoritmo.  Meter dos columnas ``pn``/``dn`` en ``Position`` confundiria tres
objetos distintos — caches de tablero, estado de juego y estado de prueba — y
haria imposible tener dos campanas a la vez.  Por eso viven en
``ProofCampaign``/``ProofNode``, y el DAG se queda como lo que es.

LAS RECURRENCIAS
----------------
Para ``goal=WHITE_WIN`` el atacante es el blanco:

    blancas al turno   (nodo OR):   pn = min(hijos.pn)   dn = suma(hijos.dn)
    negras al turno    (nodo AND):  pn = suma(hijos.pn)  dn = min(hijos.dn)

Para ``goal=BLACK_WIN`` los papeles se invierten.  La aritmetica satura en
``PROOF_INFINITY``: una suma que lo alcanza YA es infinito, y un ``min`` sobre
una lista vacia tambien (un nodo sin hijos materializados no se puede probar
por sus hijos).

Un nodo con status probado es una HOJA de la prueba, con su valor de verdad:
si el status es el objetivo, ``(0, INF)``; si es cualquier otro cierre
(la otra victoria o tablas), el objetivo queda REFUTADO ahi: ``(INF, 0)``.
El caracter binario de PNS es deliberado — las tablas se establecen refutando
las dos proposiciones de victoria, no con un tercer numero.

INICIALIZACION DE HOJAS
-----------------------
Una hoja UNKNOWN no vale ``1/1``: eso es PNS clasico y desperdicia lo unico
que ya hemos comprado, que son evals.  La inicializacion documentada aqui
combina dos senales, ambas baratas y ambas disponibles:

* el BRANCHING: en una hoja donde mueve el defensor, refutar cuesta UNA
  respuesta buena (dn pequeno) pero probar exige tratar con TODAS (pn crece
  con el numero de jugadas legales).  En una hoja donde mueve el atacante,
  simetricamente al reves.
* la BANDA DE EVAL en perspectiva del ATACANTE: +800 no significa "gana",
  significa "probarlo es mucho mas barato que refutarlo".  La eval entra aqui
  como lo que es — una estimacion de coste, no un valor de verdad — que es
  exactamente su papel legitimo en df-pn con heuristica.

Sin eval y sin lista legal conocida, se cae al ``1/1`` clasico.

MANTENIMIENTO
-------------
Incremental y por NIVELES, con la misma forma que ``ingest.backup_backed_evals``:
un nivel entero cuesta un punado de sentencias tenga tres hijos o sesenta,
lleva contador de visitas (el DAG tiene varios padres por nodo y transpone),
tope de plies y tope global de recomputos.  Si salta el tope se emite un
``PROOF_GUARD`` y se corta: unos pn/dn viejos ordenan peor, pero no mienten,
porque no son una fuente de verdad.
"""

import hashlib

from django.conf import settings
from django.db.models import Q

from . import logic
from .models import DBEvent, Edge, Position, ProofCampaign, ProofNode

# 2^62: infinito de la aritmetica de prueba.  Cabe en un BigInteger con sitio
# de sobra para que sumar varios "infinitos" en Python no desborde antes del
# clamp, y esta lejos de cualquier pn/dn real.
PROOF_INFINITY = 1 << 62
# Tope de una hoja heuristica.  Una hoja no puede fingir ser infinita: eso lo
# reserva la aritmetica para lo REFUTADO.
PROOF_MAX_LEAF = 1 << 20

ALGORITHM_VERSION = 1
DEFAULT_CAMPAIGN_NAME = 'root-white-win'
DEFAULT_REPERTOIRE_POLICY = {'primary': 0.8, 'backup': 0.15, 'explore': 0.05}

# Mantenimiento incremental (mismos topes de forma que el respaldo de evals).
PROOF_MAX_PLIES = 64
PROOF_MAX_REVISITS = 2
PROOF_MAX_NODES = 50_000

# Bandas de eval en perspectiva del ATACANTE: (umbral, peso pn, peso dn).
# Monotonas por construccion — cuanto mejor va el atacante, mas barato parece
# probar y mas caro refutar.  Los pesos son ordinales, no probabilidades.
EVAL_BANDS = (
    (9_000, 1, 64),     # el motor ya ve mate: cerrar es cuestion de PV
    (800, 2, 16),
    (300, 4, 8),
    (-300, 8, 8),       # equilibrio: sin informacion util en ninguna direccion
    (-800, 16, 4),
    (-9_000, 32, 2),
)
DEFAULT_EVAL_BAND = (64, 1)


# ---------------- aritmetica saturada ----------------

def saturating_sum(values):
    total = 0
    for value in values:
        total += max(0, int(value))
        if total >= PROOF_INFINITY:
            return PROOF_INFINITY
    return total


def saturating_min(values):
    best = PROOF_INFINITY
    seen = False
    for value in values:
        seen = True
        value = max(0, int(value))
        if value < best:
            best = value
    return best if seen else PROOF_INFINITY


def selector_mode():
    """``regret`` (por defecto) o ``pn``. El default no cambia sin una orden."""
    return str(getattr(settings, 'ATOMICDB_SELECTOR', 'regret')).lower()


# ---------------- recurrencias ----------------

def attacker_is_white(goal):
    return goal == ProofCampaign.Goal.WHITE_WIN


def is_or_node(fen, goal):
    """True si en esta posicion mueve el ATACANTE de la proposicion."""
    stm_white = fen.split()[1] == 'w'
    return stm_white == attacker_is_white(goal)


def eval_band(attacker_score):
    if attacker_score is None:
        return None
    for threshold, pn_weight, dn_weight in EVAL_BANDS:
        if attacker_score >= threshold:
            return pn_weight, dn_weight
    return DEFAULT_EVAL_BAND


def terminal_numbers(status, goal):
    """(pn, dn) de un status ya cerrado, o ``None`` si sigue abierto."""
    if status == 'UNKNOWN':
        return None
    if status == goal:
        return 0, PROOF_INFINITY
    # Cualquier otro cierre — la otra victoria o tablas — REFUTA la
    # proposicion en este nodo.  PNS es binario a proposito.
    return PROOF_INFINITY, 0


def leaf_numbers(fen, status, eval_cp, goal, legal_moves=None):
    """Inicializacion heuristica documentada de una hoja de la prueba.

    ``eval_cp`` viene en perspectiva BLANCA, como todo lo demas del sistema;
    el unico cambio de signo esta aqui dentro, para mirarlo desde el atacante.
    ``legal_moves`` es el numero de jugadas legales del bando al turno cuando
    se conoce (aristas materializadas o lista legal), o ``None``.
    """
    exact = terminal_numbers(status, goal)
    if exact is not None:
        return exact

    attacker_white = attacker_is_white(goal)
    attacker_score = (None if eval_cp is None
                      else (eval_cp if attacker_white else -eval_cp))
    band = eval_band(attacker_score)
    if band is None and not legal_moves:
        return 1, 1                       # PNS clasico: sin informacion

    pn_weight, dn_weight = band or (1, 1)
    branching = max(1, int(legal_moves or 1))
    if is_or_node(fen, goal):
        # Mueve el atacante: le basta UNA jugada buena para probar, pero el
        # defensor solo necesita que TODAS fallen para refutar.
        base_pn, base_dn = 1, branching
    else:
        base_pn, base_dn = branching, 1
    pn = min(PROOF_MAX_LEAF, max(1, base_pn * pn_weight))
    dn = min(PROOF_MAX_LEAF, max(1, base_dn * dn_weight))
    return pn, dn


def internal_numbers(fen, goal, child_numbers):
    """Recurrencias binarias estandar sobre ``[(pn, dn), ...]`` de los hijos."""
    if not child_numbers:
        return None
    pns = [pn for pn, _ in child_numbers]
    dns = [dn for _, dn in child_numbers]
    if is_or_node(fen, goal):
        return saturating_min(pns), saturating_sum(dns)
    return saturating_sum(pns), saturating_min(dns)


# ---------------- campanas ----------------

def default_campaign(create=True):
    """La campana raiz (startpos, WHITE_WIN). La crea la migracion de datos."""
    campaign = ProofCampaign.objects.filter(
        name=DEFAULT_CAMPAIGN_NAME).first()
    if campaign is not None or not create:
        return campaign
    from . import ingest
    root = ingest.get_or_create_position(logic.start_fen())
    return ProofCampaign.objects.create(
        name=DEFAULT_CAMPAIGN_NAME, root=root,
        goal=ProofCampaign.Goal.WHITE_WIN,
        algorithm_version=ALGORITHM_VERSION,
        repertoire_policy=dict(DEFAULT_REPERTOIRE_POLICY))


def active_campaigns():
    return list(ProofCampaign.objects.filter(active=True).select_related(
        'root').order_by('id'))


def normalized_policy(campaign):
    """Politica saneada: fracciones no negativas que suman 1."""
    raw = campaign.repertoire_policy or {}
    weights = []
    for name in ('primary', 'backup', 'explore'):
        try:
            weights.append(max(0.0, float(raw.get(
                name, DEFAULT_REPERTOIRE_POLICY[name]))))
        except (TypeError, ValueError):
            weights.append(DEFAULT_REPERTOIRE_POLICY[name])
    total = sum(weights)
    if total <= 0:
        weights = [DEFAULT_REPERTOIRE_POLICY[name]
                   for name in ('primary', 'backup', 'explore')]
        total = sum(weights)
    return {'primary': weights[0] / total, 'backup': weights[1] / total,
            'explore': weights[2] / total}


# ---------------- nodos ----------------

def _node_rows(campaign, keys):
    return {row.position_id: row for row in ProofNode.objects.filter(
        campaign=campaign, position_id__in=list(keys))}


def _children_by_parent(parent_keys):
    """(move_uci, child_key, status, eval_cp, fen) por padre, en orden estable."""
    rows = Edge.objects.filter(parent_id__in=list(parent_keys)).order_by(
        'id').values_list('parent_id', 'move_uci', 'child_id',
                          'child__status', 'child__eval_cp', 'child__fen')
    by_parent = {}
    for parent_id, move_uci, child_id, status, eval_cp, fen in rows:
        by_parent.setdefault(parent_id, []).append(
            (move_uci, child_id, status, eval_cp, fen))
    return by_parent


def compute_numbers(campaign, position, children, child_nodes):
    """(pn, dn, expanded_in_proof, selected_child) de un nodo.

    ``children`` es la lista de aristas materializadas; ``child_nodes`` el
    mapa de ``ProofNode`` de los hijos que ya existen.  Un hijo sin nodo de
    prueba aporta su inicializacion de hoja al vuelo, para que un cono a medio
    materializar no invente numeros optimistas.
    """
    goal = campaign.goal
    exact = terminal_numbers(position.status, goal)
    if exact is not None:
        return exact[0], exact[1], False, None

    if not children:
        pn, dn = leaf_numbers(position.fen, position.status,
                              position.eval_cp, goal)
        return pn, dn, False, None

    numbers, moves = [], []
    for move_uci, child_id, status, eval_cp, fen in children:
        node = child_nodes.get(child_id)
        if node is not None:
            numbers.append((node.pn, node.dn))
        else:
            numbers.append(leaf_numbers(fen, status, eval_cp, goal))
        moves.append(move_uci)

    pn, dn = internal_numbers(position.fen, goal, numbers)
    if is_or_node(position.fen, goal):
        index = min(range(len(numbers)), key=lambda i: (numbers[i][0], i))
    else:
        index = min(range(len(numbers)), key=lambda i: (numbers[i][1], i))
    return pn, dn, True, moves[index]


def refresh_proof_numbers(seed_keys, campaigns=None,
                          max_plies=PROOF_MAX_PLIES):
    """Recomputa pn/dn del cono de ancestros de ``seed_keys``, por niveles.

    Devuelve el numero de filas escritas.  No es una fuente de verdad: si el
    presupuesto salta, se emite ``PROOF_GUARD`` y lo que quede sin refrescar
    simplemente ordena peor hasta la siguiente pasada.
    """
    seeds = [key for key in dict.fromkeys(seed_keys) if key]
    if not seeds:
        return 0
    if campaigns is None:
        campaigns = active_campaigns()
    if not campaigns:
        return 0

    written = 0
    for campaign in campaigns:
        written += _refresh_campaign(campaign, seeds, max_plies)
    return written


def _refresh_campaign(campaign, seeds, max_plies):
    frontier = list(seeds)
    visits, processed, plies, written = {}, 0, 0, 0
    guard_reason = None
    while frontier and plies < max_plies:
        plies += 1
        positions = list(Position.objects.filter(key__in=frontier).only(
            'key', 'fen', 'status', 'eval_cp'))
        if not positions:
            break
        keys = [row.key for row in positions]
        children = _children_by_parent(keys)
        child_keys = {child_id for rows in children.values()
                      for _, child_id, _, _, _ in rows}
        existing = _node_rows(campaign, set(keys) | child_keys)
        create, update, propagate = [], [], []
        for row in positions:
            processed += 1
            pn, dn, expanded, selected = compute_numbers(
                campaign, row, children.get(row.key, ()), existing)
            node = existing.get(row.key)
            if node is None:
                create.append(ProofNode(
                    campaign=campaign, position_id=row.key, pn=pn, dn=dn,
                    expanded_in_proof=expanded, selected_child=selected))
                propagate.append(row.key)
                continue
            if (node.pn == pn and node.dn == dn
                    and node.expanded_in_proof == expanded
                    and node.selected_child == selected):
                continue
            node.pn, node.dn = pn, dn
            node.expanded_in_proof = expanded
            node.selected_child = selected
            update.append(node)
            propagate.append(row.key)
        if create:
            ProofNode.objects.bulk_create(create, ignore_conflicts=True)
            written += len(create)
        if update:
            ProofNode.objects.bulk_update(
                update, ['pn', 'dn', 'expanded_in_proof', 'selected_child'],
                batch_size=500)
            written += len(update)
        if not propagate:
            break
        if processed >= PROOF_MAX_NODES:
            guard_reason = 'node-budget'
            break
        frontier = []
        for key in set(Edge.objects.filter(child_id__in=propagate)
                       .values_list('parent_id', flat=True)):
            seen = visits.get(key, 0)
            if seen < PROOF_MAX_REVISITS:
                visits[key] = seen + 1
                frontier.append(key)
    else:
        if frontier:
            guard_reason = 'ply-guard'
    if guard_reason:
        DBEvent.objects.create(kind='PROOF_GUARD', payload={
            'campaign': campaign.name, 'reason': guard_reason,
            'processed': processed, 'plies': plies,
            'seed_count': len(seeds)})
    return written


def format_number(value):
    """pn/dn para consumo humano: saturado es infinito, no 4.6e18.

    Vive aqui y no en una plantilla porque lo usan la home, ``proof_status`` y
    el snapshot, y tres formateos distintos del mismo numero es como se
    empieza a desconfiar de un panel.
    """
    if value is None:
        return '-'
    if value >= PROOF_INFINITY:
        return '\u221e'
    return f'{value:,}'


def headline_numbers():
    """(pn, dn) de la campana por defecto, o ``(None, None)`` si no hay.

    Deliberadamente SIN porcentaje: el denominador de "% resuelto" crece cada
    vez que se descubre una obligacion nueva, asi que una barra de progreso
    aqui seria una mentira que ademas retrocede.
    """
    campaign = ProofCampaign.objects.filter(active=True).order_by('id').first()
    if campaign is None:
        return None, None
    return root_numbers(campaign)


def root_numbers(campaign):
    node = ProofNode.objects.filter(
        campaign=campaign, position_id=campaign.root_id).first()
    if node is not None:
        return node.pn, node.dn
    root = campaign.root
    return leaf_numbers(root.fen, root.status, root.eval_cp, campaign.goal)


# ---------------- seleccion (df-pn descent) ----------------
#
# Un descenso cuesta O(profundidad x branching) LECTURAS y no toca el resto
# del grafo: nada de pasadas globales.  La asignacion blanda del repertorio se
# resuelve con un contador determinista, no con ``random``: dos servidores con
# el mismo estado eligen lo mismo, y un replay de la cola es reproducible.

DESCENT_MAX_PLIES = 96


def _bucket(counter, policy):
    """Reparto determinista 80/15/5 por hash del contador.

    Un ``random`` haria irreproducible la cola; un modulo puro agruparia las
    exploraciones en rachas.  El hash del contador reparte uniforme y es
    replayable.
    """
    digest = hashlib.sha256(str(int(counter)).encode()).digest()
    point = int.from_bytes(digest[:8], 'big') / float(1 << 64)
    if point < policy['primary']:
        return 'primary'
    if point < policy['primary'] + policy['backup']:
        return 'backup'
    return 'explore'


def _ranked_children(campaign, position, children, child_nodes):
    """Hijos vivos ordenados por 'lo mas demostrador primero'."""
    or_node = is_or_node(position.fen, campaign.goal)
    ranked = []
    for index, (move_uci, child_id, status, eval_cp, fen) in enumerate(
            children):
        if status != 'UNKNOWN':
            continue          # ya resuelto: no queda pregunta que hacerle
        node = child_nodes.get(child_id)
        pn, dn = ((node.pn, node.dn) if node is not None
                  else leaf_numbers(fen, status, eval_cp, campaign.goal))
        key = (pn, index) if or_node else (dn, index)
        ranked.append((key, move_uci, child_id, fen))
    ranked.sort(key=lambda item: item[0])
    return ranked


def descend(campaign, counter=0, max_plies=DESCENT_MAX_PLIES, avoid=()):
    """Baja desde la raiz hasta la posicion mas demostradora sin resolver.

    Devuelve ``(Position, plies)`` o ``(None, plies)`` si la campana no tiene
    nada abierto por debajo de su raiz.

    ``avoid`` es la RESERVA: las posiciones que este mismo lote ya reparte.
    Sin ella, un arbol somero devolveria la misma hoja una y otra vez y un
    lote de cuatro tareas serian cuatro copias de la misma pregunta — el
    "trabajo duplicado" que la literatura de PNS distribuida evita con virtual
    loss.  Aqui es mas simple: un nodo reservado no se puntua.
    """
    policy = normalized_policy(campaign)
    node = campaign.root
    reserved = set(avoid)
    visited = {node.key}
    plies = 0
    while plies < max_plies:
        if node.status != 'UNKNOWN':
            return None, plies
        children = _children_by_parent([node.key]).get(node.key, ())
        if not children:
            # Frontera: aqui es donde se compra, salvo que ya este reservada.
            return (None if node.key in reserved else node), plies
        child_nodes = _node_rows(
            campaign, [child_id for _, child_id, _, _, _ in children])
        ranked = _ranked_children(campaign, node, children, child_nodes)
        ranked = [item for item in ranked
                  if item[2] not in visited and item[2] not in reserved]
        if not ranked:
            return (None if node.key in reserved else node), plies
        bucket = _bucket(counter + plies, policy)
        if bucket == 'primary':
            chosen = ranked[0]
        elif bucket == 'backup':
            chosen = ranked[1] if len(ranked) > 1 else ranked[0]
        else:
            chosen = ranked[-1]
        visited.add(chosen[2])
        node = Position.objects.get(key=chosen[2])
        plies += 1
    return node, plies


def open_and_obligations(campaign, limit=10):
    """Obligaciones AND abiertas mas antiguas: respuestas del defensor vivas.

    Son las que hay que refutar TODAS, asi que son el trabajo obligatorio de
    la prueba; se ordenan por antiguedad para que la mas olvidada salga
    primero.
    """
    attacker_white = attacker_is_white(campaign.goal)
    defender_to_move = Q(position__fen__contains=' b ') if attacker_white \
        else Q(position__fen__contains=' w ')
    return list(ProofNode.objects.filter(
        campaign=campaign, position__status='UNKNOWN',
    ).filter(defender_to_move).select_related('position')
        .order_by('updated')[:limit])


def most_proving_frontier(campaign, limit=10):
    """Los nodos abiertos con menor pn: donde una prueba rendiria mas."""
    return list(ProofNode.objects.filter(
        campaign=campaign, position__status='UNKNOWN',
    ).select_related('position').order_by('pn', 'position_id')[:limit])
