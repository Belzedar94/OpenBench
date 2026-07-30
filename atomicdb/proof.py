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

LA PUERTA DOBLE (``ATOMICDB_SOLVE_GATE``, apagada por defecto)
--------------------------------------------------------------
La inicializacion de arriba tiene un punto ciego conocido: el eval contesta
"quien esta mejor" y la prueba necesita "cuanto cuesta cerrar esto".  Un final
pelado con +1200 promete mucho y no cobra nada, asi que el descenso lo perfora
una y otra vez mientras la conversion real cuesta cincuenta jugadas de
tecnica.  Con la puerta encendida entra una SEGUNDA opinion independiente —
``solve_estimate.annoyance``, que no llama al motor — y el ``pn`` de la hoja
se multiplica por un factor de 1 a K creciente con la molestia.

Es una puerta BLANDA: encarece, no veta.  Vetar seria afirmar algo sobre el
arbol ("por aqui no se gana") que un estimador hand-crafted no esta en
posicion de afirmar; encarecer afirma algo sobre el PRESUPUESTO, que es
exactamente lo que ``pn`` significa.  Dos exenciones explicitas: la banda de
mate no se encarece (ya es la via rapida) y el ``1/1`` clasico tampoco — "sin
informacion" tiene que seguir siendo lo mas demostrador, que es lo que manda
el descenso a los hermanos sin explorar.

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
import time

from django.conf import settings
from django.db.models import Q

from . import logic, solve_estimate
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

# Histeresis del hijo primario de un nodo OR (anti-baile).
#
# ``pn`` es una ESTIMACION de coste, no una medida: dos jugadas del atacante
# igual de prometedoras se adelantan la una a la otra cada vez que llega un
# analisis, y el descenso, que sigue al minimo, se iba con la que acabara de
# parpadear.  El resultado es que ninguna de las dos acumula la profundidad
# que la cerraria — el "baile" que reporto la comunidad.  Aqui el retador
# tiene que ser K VECES mas barato de probar para destronar a la primaria:
# no es una preferencia estetica, es la diferencia entre "mejor dentro del
# ruido" y "mejor de verdad".
#
# Solo afecta a QUIEN es la primaria.  El reparto de visitas sigue siendo la
# asignacion blanda 80/15/5 de siempre: la histeresis elige a quien va el 80%,
# no cuanto es.  Un hijo ya PROBADO (pn = 0) no lo destrona nadie: ningun pn
# puede ser menor que cero, asi que la condicion es imposible por
# construccion, no por un caso especial.
SELECTED_CHILD_HYSTERESIS = 3

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

# La banda EXENTA de la puerta: donde el motor ya ve mate.  Se lee de
# ``EVAL_BANDS`` y no se copia para que las dos no puedan separarse.
SOLVE_GATE_MATE_BAND = EVAL_BANDS[0][0]

# EL CUADRANTE DE DESACUERDO.  Umbral, en perspectiva del atacante, a partir
# del cual "el motor lo ve ganado": el mismo corte que separa la banda de
# ventaja clara en ``EVAL_BANDS``, para que la frontera del log y la de la
# aritmetica sean la misma frontera.
#
# Cuando la puerta encarece una hoja QUE EL MOTOR VE GANADA, las dos senales
# se contradicen, y esa contradiccion es lo mas informativo que produce este
# sistema: el motor promete y el estimador cobra.  Ahi es donde viven los
# callejones sin salida — la fortaleza que se cierra, el final que no se
# convierte — y ese log es ademas el conjunto de entrenamiento del estimador
# que sustituira al hand-crafted.  Por eso se registra en vez de descartarse.
SOLVE_GATE_OPTIMISTIC = 300

# Rate limit del log.  Sin el, una pasada de mantenimiento emitiria un evento
# por hoja y por nivel, y la tabla de eventos — que es un diario operativo,
# no una serie temporal — se llenaria de la misma frase.  Dedup por clave con
# ventana: una hoja cuenta UNA vez por ventana, y lo que interesa del
# cuadrante (que posiciones estan ahi) no se pierde por no repetirlo.
#
# El cache es de PROCESO, y eso esta bien: con varios workers el peor caso son
# unos pocos duplicados con el mismo payload, y el consumidor del dataset
# deduplica por clave de todas formas.
SOLVE_GATE_LOG_WINDOW = 900.0
SOLVE_GATE_LOG_MAX = 4_096
_solve_gate_logged = {}

# Campos que la pasada de mantenimiento pide de cada posicion.  ``.only`` esta
# aqui porque ``last_analysis`` es el campo mas grande de la tabla y el
# mantenimiento no lo mira... salvo con la puerta encendida, que lee de el la
# PV vigente.  Y entonces hay que pedirlo DE ENTRADA: tocarlo diferido dispara
# un SELECT por fila, que es como una pasada por niveles se convierte en una
# tormenta de consultas sin que nadie lo vea venir.
MAINTENANCE_FIELDS = ('key', 'fen', 'status', 'eval_cp')
SOLVE_GATE_FIELDS = ('last_analysis', 'mate_in')


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


# ---------------- la puerta de resolucion ----------------

def solve_gate_enabled():
    """La puerta esta APAGADA salvo que el despliegue la encienda."""
    return bool(getattr(settings, 'ATOMICDB_SOLVE_GATE', False))


def maintenance_fields():
    """Columnas del ``.only`` de la pasada de mantenimiento."""
    if solve_gate_enabled():
        return MAINTENANCE_FIELDS + SOLVE_GATE_FIELDS
    return MAINTENANCE_FIELDS


def gate_annoyance(row, branching=None):
    """Molestia de una FILA COMPLETA, o ``None`` con la puerta apagada.

    Devolver ``None`` y no ``0.0`` es lo que hace que el flag apagado no
    cueste ni el estimador ni una rama distinta de aritmetica: ``leaf_numbers``
    ve exactamente lo que veia antes.
    """
    if not solve_gate_enabled():
        return None
    return solve_estimate.annoyance(row, branching)


def shallow_annoyance(fen, eval_cp):
    """Molestia de lo que se sabe de una ARISTA: fen y eval, nada mas.

    Los hijos de un nivel viajan como tuplas de ``_children_by_parent``, sin
    ``last_analysis``: traerlo por arista seria arrastrar el campo mas grande
    de la tabla por cada hijo del nivel.  El estimador degrada dos de sus
    cuatro features a neutral y lo dice; a cambio, la ordenacion del descenso
    y la de la pasada de mantenimiento usan la MISMA puerta.
    """
    if not solve_gate_enabled():
        return None
    return solve_estimate.annoyance(solve_estimate.shallow(fen, eval_cp))


def reset_solve_gate_log():
    """Vacia el dedup del log.  Existe para los tests y para un reinicio."""
    _solve_gate_logged.clear()


def _log_disagreement(key, eval_cp, annoyance, factor):
    """``SOLVE_GATE_DISAGREE`` con rate limit por clave.

    ``eval_cp`` va en perspectiva BLANCA, como la columna de la que sale: el
    consumidor de este log es un dataset que se une a ``Position`` por clave,
    y una segunda convencion de signo dentro del payload seria una trampa.
    """
    now = time.monotonic()
    seen = _solve_gate_logged.get(key)
    if seen is not None and now - seen < SOLVE_GATE_LOG_WINDOW:
        return False
    if len(_solve_gate_logged) >= SOLVE_GATE_LOG_MAX:
        cutoff = now - SOLVE_GATE_LOG_WINDOW
        for stale in [k for k, ts in _solve_gate_logged.items() if ts < cutoff]:
            del _solve_gate_logged[stale]
        if len(_solve_gate_logged) >= SOLVE_GATE_LOG_MAX:
            _solve_gate_logged.clear()
    _solve_gate_logged[key] = now
    DBEvent.objects.create(kind='SOLVE_GATE_DISAGREE', payload={
        'key': key, 'eval_cp': eval_cp,
        'annoyance': round(float(annoyance), 4),
        'factor': round(float(factor), 3)})
    return True


def gated_pn(pn, annoyance, attacker_score, key=None, eval_cp=None):
    """``pn`` encarecido por la molestia.  Blando: multiplica, no veta.

    Exento el mate en banda: ahi el coste de cerrar ya lo mide la PV y
    encarecerlo solo alejaria al descenso de la via rapida.  El tope de hoja
    se vuelve a aplicar despues del producto — una hoja no puede fingir ser
    infinita, que es lo que la aritmetica reserva para lo REFUTADO.

    ``key`` es OPTATIVA y es lo unico que enciende el log del cuadrante de
    desacuerdo.  Sin ella la funcion es aritmetica pura y no toca la base:
    asi las hojas puntuadas con la vista degradada de una arista — que ven dos
    de sus cuatro features en neutral — no ensucian un dataset que se quiere
    de fidelidad completa.
    """
    if annoyance is None:
        return pn
    if attacker_score is not None and attacker_score >= SOLVE_GATE_MATE_BAND:
        return pn
    factor = solve_estimate.gate_factor(annoyance)
    gated = min(PROOF_MAX_LEAF, max(1, int(pn * factor)))
    if (key and gated > pn and attacker_score is not None
            and attacker_score >= SOLVE_GATE_OPTIMISTIC):
        _log_disagreement(key, eval_cp, annoyance, factor)
    return gated


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


def leaf_numbers(fen, status, eval_cp, goal, legal_moves=None,
                 annoyance=None, key=None):
    """Inicializacion heuristica documentada de una hoja de la prueba.

    ``eval_cp`` viene en perspectiva BLANCA, como todo lo demas del sistema;
    el unico cambio de signo esta aqui dentro, para mirarlo desde el atacante.
    ``legal_moves`` es el numero de jugadas legales del bando al turno cuando
    se conoce (aristas materializadas o lista legal), o ``None``.

    ``annoyance`` es la segunda opinion de la puerta doble (``[0, 1]``, o
    ``None`` = puerta apagada, que es el default y el comportamiento historico
    byte a byte).  Solo encarece ``pn``: ``dn`` mide refutar, y refutar una
    posicion tediosa no es mas caro por ser tediosa — basta una respuesta.

    ``key`` solo sirve para el log del cuadrante de desacuerdo (ver
    ``gated_pn``); sin ella esta funcion no escribe nada en ninguna parte.
    """
    exact = terminal_numbers(status, goal)
    if exact is not None:
        return exact

    attacker_white = attacker_is_white(goal)
    attacker_score = (None if eval_cp is None
                      else (eval_cp if attacker_white else -eval_cp))
    band = eval_band(attacker_score)
    if band is None and not legal_moves:
        # PNS clasico: sin informacion.  La puerta tampoco entra aqui — "sin
        # informacion" tiene que seguir siendo lo mas demostrador del arbol, o
        # el descenso dejaria de ir a los hermanos sin explorar.
        return 1, 1

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
    return gated_pn(pn, annoyance, attacker_score, key, eval_cp), dn


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


def sticky_index(best, held, numbers):
    """Indice del primario respetando la histeresis, en aritmetica entera.

    ``best`` es el mejor por pn de esta pasada y ``held`` el que ya era
    primario (o ``None`` si no habia).  El retador destrona solo si
    ``pn_retador < pn_actual / K``, escrito como producto para no perder el
    caso ``pn_actual == 0`` por division entera.
    """
    if held is None or held == best:
        return best
    if numbers[best][0] * SELECTED_CHILD_HYSTERESIS < numbers[held][0]:
        return best
    return held


def compute_numbers(campaign, position, children, child_nodes, previous=None):
    """(pn, dn, expanded_in_proof, selected_child) de un nodo.

    ``children`` es la lista de aristas materializadas; ``child_nodes`` el
    mapa de ``ProofNode`` de los hijos que ya existen.  Un hijo sin nodo de
    prueba aporta su inicializacion de hoja al vuelo, para que un cono a medio
    materializar no invente numeros optimistas.

    ``previous`` es el ``selected_child`` que este nodo ya tenia: en un nodo
    OR el primario es PEGAJOSO (ver ``SELECTED_CHILD_HYSTERESIS``).  Un nodo
    AND no lo es: ahi el primario es la defensa mas dura de refutar, y
    sostenerla contra evidencia nueva retrasaria justo la refutacion que la
    prueba necesita.
    """
    goal = campaign.goal
    exact = terminal_numbers(position.status, goal)
    if exact is not None:
        return exact[0], exact[1], False, None

    if not children:
        # La hoja de la FRONTERA, y la unica que se puntua con la fila
        # entera delante: es donde la puerta tiene todo lo que necesita.
        pn, dn = leaf_numbers(position.fen, position.status,
                              position.eval_cp, goal,
                              annoyance=gate_annoyance(position),
                              key=position.key)
        return pn, dn, False, None

    numbers, moves = [], []
    for move_uci, child_id, status, eval_cp, fen in children:
        node = child_nodes.get(child_id)
        if node is not None:
            numbers.append((node.pn, node.dn))
        else:
            numbers.append(leaf_numbers(
                fen, status, eval_cp, goal,
                annoyance=shallow_annoyance(fen, eval_cp)))
        moves.append(move_uci)

    pn, dn = internal_numbers(position.fen, goal, numbers)
    if is_or_node(position.fen, goal):
        index = min(range(len(numbers)), key=lambda i: (numbers[i][0], i))
        held = moves.index(previous) if previous in moves else None
        index = sticky_index(index, held, numbers)
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
            *maintenance_fields()))
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
            # La fila de prueba de este mismo nodo ya viene en ``existing``
            # (se lee junto a la de sus hijos), asi que el primario anterior
            # no cuesta ni una sentencia extra.
            node = existing.get(row.key)
            pn, dn, expanded, selected = compute_numbers(
                campaign, row, children.get(row.key, ()), existing,
                previous=None if node is None else node.selected_child)
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
    return leaf_numbers(root.fen, root.status, root.eval_cp, campaign.goal,
                        annoyance=gate_annoyance(root))


# ---------------- seleccion (df-pn descent) ----------------
#
# Un descenso cuesta O(profundidad x branching) LECTURAS y no toca el resto
# del grafo: nada de pasadas globales.  La asignacion blanda del repertorio se
# resuelve con un contador determinista, no con ``random``: dos servidores con
# el mismo estado eligen lo mismo, y un replay de la cola es reproducible.
#
# Dos decisiones distintas, deliberadamente separadas: QUIEN es la primaria de
# un nodo lo fija ``selected_child`` con histeresis (``compute_numbers``), y
# CUANTO le toca lo reparte ``_bucket`` 80/15/5.  Mezclarlas es como se acaba
# teniendo un descenso que baila entre dos jugadas o un repertorio que gasta
# el 95% en una sola.

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
                  else leaf_numbers(fen, status, eval_cp, campaign.goal,
                                    annoyance=shallow_annoyance(fen, eval_cp)))
        key = (pn, index) if or_node else (dn, index)
        ranked.append((key, move_uci, child_id, fen))
    ranked.sort(key=lambda item: item[0])
    return ranked


def _primary_index(ranked, node):
    """Cual de los candidatos VIVOS es la primaria de este nodo.

    Quien es primaria ya lo decidio la histeresis y vive en
    ``selected_child``; aqui solo se respeta mientras siga siendo un candidato
    de este descenso.  Si la primaria esta resuelta, ya visitada o reservada
    por el mismo lote, no queda pregunta que hacerle y manda el orden pn — que
    es exactamente lo que hacia esta funcion antes de existir.
    """
    if node is not None and node.selected_child:
        for index, item in enumerate(ranked):
            if item[1] == node.selected_child:
                return index
    return 0


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
        # La fila del propio nodo viaja con las de sus hijos: el primario
        # pegajoso se lee en la MISMA sentencia que ya se pagaba.
        rows = _node_rows(campaign, [node.key] + [
            child_id for _, child_id, _, _, _ in children])
        ranked = _ranked_children(campaign, node, children, rows)
        ranked = [item for item in ranked
                  if item[2] not in visited and item[2] not in reserved]
        if not ranked:
            return (None if node.key in reserved else node), plies
        bucket = _bucket(counter + plies, policy)
        primary = _primary_index(ranked, rows.get(node.key))
        if bucket == 'primary':
            chosen = ranked[primary]
        elif bucket == 'backup':
            # El backup es la mejor ALTERNATIVA a la primaria, no "el segundo
            # por pn": con una primaria pegajosa que no encabeza el orden,
            # ranked[1] podria ser la primaria otra vez y el 15% se gastaria
            # en la misma jugada que el 80%.
            alternatives = [item for index, item in enumerate(ranked)
                            if index != primary]
            chosen = alternatives[0] if alternatives else ranked[primary]
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


# ---------------- el FRENTE DE PRUEBA ----------------
#
# DEFINICION OPERATIVA, y por que esta.  "Frente" no es una metafora: es el
# conjunto de nodos sobre los que la prueba de esta campana esta trabajando
# AHORA, y hace falta fijarlo con precision porque de el salen las tres cosas
# que este paquete mide y repara.  Un nodo pertenece al frente cuando cumple
# las tres condiciones:
#
#   1. tiene fila ``ProofNode`` en la campana — es decir, la prueba ya lo
#      alcanzo alguna vez.  Un nodo del DAG que ninguna cascada toco no es
#      frente de nadie: es cache de posiciones;
#   2. su ``Position.status`` sigue siendo ``UNKNOWN``.  Un nodo cerrado es
#      una HOJA de la prueba con su valor de verdad, no una pregunta abierta;
#   3. ``pn`` y ``dn`` son ambos FINITOS.  ``pn`` infinito significa que el
#      objetivo esta refutado ahi y ese nodo ya no es una obligacion; ``dn``
#      infinito significa que esta probado.  En los dos casos no queda
#      esfuerzo que estimar y contarlo solo ensuciaria la mediana.
#
# Y esta ACOTADO: los ``limit`` de MENOR pn primero.  Dos razones, ninguna
# estetica.  La barata: ``(campaign, pn)`` esta indexado, asi que el corte lo
# hace el indice y no un scan.  La honesta: el frente que importa es a donde
# va el descenso, y el descenso va al minimo pn.  El sesgo que esto introduce
# hay que decirlo en voz alta — mirar los mas demostradores primero mide la
# parte del arbol donde la prueba cree que esta cerca, no el arbol entero — y
# es exactamente el sesgo que queremos: una espina de dn=1 en un rincon que
# nadie visita no tumba nada; una en la linea principal si.
PROOF_FRONTIER_SCAN = 20_000


def frontier_rows(campaign, limit=PROOF_FRONTIER_SCAN):
    """``(position_id, fen, pn, dn)`` del frente de prueba, mas proving first.

    Devuelve tuplas y no modelos a proposito: el frente se recorre entero para
    medir y para reparar, y traer ``Position`` completas arrastraria el JSON
    de ``last_analysis`` de cada fila — el mayor campo de la tabla — para leer
    de el un color al turno.
    """
    return list(ProofNode.objects.filter(
        campaign=campaign, position__status='UNKNOWN',
        pn__lt=PROOF_INFINITY, dn__lt=PROOF_INFINITY,
    ).order_by('pn', 'position_id').values_list(
        'position_id', 'position__fen', 'pn', 'dn')[:limit])


def frontier_and_rows(campaign, limit=PROOF_FRONTIER_SCAN):
    """El frente restringido a nodos AND: mueve el DEFENSOR de la campana.

    Son las OBLIGACIONES — hay que refutar todas las respuestas — y por eso
    son las unicas donde un ``dn`` bajo es una mala noticia en vez de una
    buena.  El filtro se hace en Python sobre el FEN que ya viajo con la fila:
    en SQL seria un ``LIKE`` sobre la columna mas larga de la tabla, que ni
    usa indice ni cabe en una portada.
    """
    return [row for row in frontier_rows(campaign, limit=limit)
            if not is_or_node(row[1], campaign.goal)]


def frontier_dn_stats(campaign, floor, limit=PROOF_FRONTIER_SCAN):
    """Salud del frente AND: cuantos son, su ``dn`` MEDIANO y cuantos finos.

    ``dn`` mediano sobre el frente AND es la metrica que la comunidad
    (Wolfram) hizo evidente: una linea humana acumula dn alto porque cada
    replica del defensor analizada suma una via mas de refutacion que habria
    que cerrar; una espina que el selector automatico dejo con dn=1 es UNA
    pregunta sin responder de distancia de caerse.  La mediana, no la media:
    un punado de nodos saturados no debe poder tapar mil espinas.

    ``floor`` es el suelo de reparacion vigente (``ingest.DN_REPAIR_FLOOR``);
    se pasa y no se importa para que la constante viva en UN solo sitio, que
    es donde esta el brazo que la usa.
    """
    dns = sorted(row[3] for row in frontier_and_rows(campaign, limit=limit))
    if not dns:
        return {'and_nodes': 0, 'dn_median': 0, 'thin': 0}
    middle = len(dns) // 2
    median = (dns[middle] if len(dns) % 2
              else (dns[middle - 1] + dns[middle]) // 2)
    return {'and_nodes': len(dns), 'dn_median': int(median),
            'thin': sum(1 for value in dns if value <= floor)}


def frontier_dn_headline(floor):
    """Las mismas cifras para la campana de portada, o ``None`` si no hay."""
    campaign = ProofCampaign.objects.filter(active=True).order_by('id').first()
    if campaign is None:
        return None
    return frontier_dn_stats(campaign, floor)


def most_proving_frontier(campaign, limit=10):
    """Los nodos abiertos con menor pn: donde una prueba rendiria mas."""
    return list(ProofNode.objects.filter(
        campaign=campaign, position__status='UNKNOWN',
    ).select_related('position').order_by('pn', 'position_id')[:limit])
