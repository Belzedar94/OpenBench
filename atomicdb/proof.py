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
import math
import time
from collections import namedtuple

from django.conf import settings
from django.db.models import Q

from . import logic, solve_estimate
from .models import (AnalysisTask, Campaign, DBEvent, Edge, Position,
                     ProofCampaign, ProofNode)

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
# TOPE DEL PASEO QUE BUSCA LA REPETICION (§ _cycling_edges).  El mismo tope de
# cordura que ``ingest.BACKED_CYCLE_MAX_PLIES`` y por la misma razon: una
# espina mas larga que esto se deja pasar SIN reclamar ciclo, que es el error
# seguro — equivocarse hacia "no hay ciclo" deja unos numeros que ordenan
# peor, equivocarse hacia "hay ciclo" refutaria una arista sana.  No se
# importa de ``ingest`` porque ``ingest`` importa este modulo.
PROOF_CYCLE_MAX_PLIES = 32

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
        stale = [k for k, ts in _solve_gate_logged.items() if ts < cutoff]
        for key_out in stale:
            del _solve_gate_logged[key_out]
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


# ---------------- repeticion: la arista que vuelve a su propio nodo ---------
#
# EL MISMO DEFECTO QUE EL RESPALDO, EN LA OTRA COLUMNA (invariante 6 de
# docs/value-semantics.md).  El grafo es un DAG con ciclos de verdad, y las
# recurrencias pn/dn suman sobre TODAS las aristas: un dn puede salir de X,
# dar la vuelta por la lanzadera y volver a entrar en la suma de X como si
# fuera esfuerzo nuevo.  Como los ``ProofNode`` PERSISTEN entre pasadas, cada
# refresco relee lo que la pasada anterior escribio y la realimentacion es un
# trinquete multiplicativo: con dos caminos de reentrada el dn se DUPLICA por
# pasada, y en unas decenas de pasadas esta clavado en ``PROOF_INFINITY``
# (caso Eclipsia, 6-ago: seis nodos UNSOLVED con dn = 2^62 exacto y pn = 1,
# un estado imposible — dn infinito significa probado, y probado es pn = 0).
#
# La regla: una arista cuyo VALOR vuelve al nodo en calculo se esta
# justificando pasando por el.  Ese numero no dice nada sobre lo que cuesta
# probar o refutar nada — es el eco del propio nodo — asi que la arista entra
# con la INICIALIZACION DE HOJA de su hijo: la estimacion estatica de
# siempre, la que se usa para un hijo que todavia no tiene fila de prueba.
#
# POR QUE NO ``(INF, 0)``, QUE ES LO QUE ESTO HACIA HASTA EL 6-ago POR LA
# TARDE.  "Repeticion = tablas = proposicion refutada" es verdad sobre el
# JUEGO y es lo que hacen los certificados (solve.py) y el respaldo
# (_draw_cycling_children).  Pero aqui la adjudicacion se PERSISTIA en el
# nodo, y eso rompe dos cosas a la vez:
#
#   * el invariante 6 en su propia letra: la repeticion vive en el espacio de
#     CAMINOS, nunca en el nodo.  Un ``(INF, 0)`` guardado dice "este nodo
#     esta refutado" cuando lo unico cierto es "este nodo, POR ESTE CAMINO,
#     se repite";
#   * la estabilidad: ``(INF, 0)`` destruye los valores por los que camina el
#     propio detector.  Medido en vivo sobre el corredor de Eclipsia — cinco
#     nodos oscilando entre ``(INF, 0)`` y ``(1, 392)`` en cada pasada, once
#     escrituras por pasada y ``recascade_proof`` sin alcanzar nunca el punto
#     fijo que es su criterio de parada.
#
# La inicializacion de hoja no tiene ninguno de los dos problemas: es
# acotada (``PROOF_MAX_LEAF``), no depende de ningun numero del ciclo — asi
# que la realimentacion se corta igual de limpiamente — y no colapsa el
# nodo, asi que el detector sigue viendo el arbol que veia.  Afirma menos, y
# afirmar poco es exactamente lo que puede hacer una estimacion de coste que
# el propio modulo declara que no es fuente de verdad.
#
# POR DONDE SE CAMINA, y por que NO por ``selected_child``.  El primer
# borrador seguia la espina de primarios, como hace el respaldo con
# ``backed_move`` — y el respaldo puede, porque su negamax toma el valor de
# UN hijo.  Las recurrencias de prueba SUMAN: la realimentacion fluye por
# donde fluyen los numeros, no por donde apunta el primario, y la histeresis
# puede dejar al primario clavado FUERA del ciclo mientras la suma sigue
# dando la vuelta (medido en la fixture de test_proof_cycles: +48 de dn por
# pasada con los primarios quietos).  Asi que el paseo sigue a los
# PORTADORES del valor vigente: en un nodo AND el ``min`` de dn viene de su
# argmin, y en un nodo OR la suma la domina su sumando mayor — y dualmente
# para pn.  Ese es exactamente el camino por el que un numero puede volver a
# entrar en si mismo, que es lo unico que este paseo pregunta.
#
# Los pn/dn no son fuente de verdad y esta regla tampoco: ordena mejor, no
# prueba nada.  Un corte que no dispara deja numeros que ordenan peor; uno
# que dispara de mas refutaria una arista sana, y por eso todas las paradas
# dudosas — tope, hijo sin numeros, ciclo ajeno — paran SIN reclamar.


class _CarrierCache:
    """Lo que el paseo de portadores recuerda durante UNA pasada.

    ``children`` son hechos del grafo por nodo visitado: a donde llevan sus
    aristas, con status y fen del hijo.  ``numbers`` es el (pn, dn) VIGENTE
    de cada ``ProofNode`` leido o escrito; el bucle de niveles llama a
    ``wrote`` con cada fila que actualiza, para que un paseo posterior de la
    misma pasada vea el numero nuevo y no el que acaba de morir.
    """

    __slots__ = ('children', 'numbers')

    def __init__(self):
        self.children = {}   # nodo -> ((child_id, status, fen), ...)
        self.numbers = {}    # nodo -> (pn, dn) del ProofNode, o None

    def wrote(self, key, pn, dn):
        self.numbers[key] = (pn, dn)


def _resolve_carrier_steps(campaign, cache, cursors):
    """Dos consultas para los cursores del nivel que la cache no conoce."""
    unknown = [node for node in cursors if node not in cache.children]
    if not unknown:
        return
    by_node = {node: [] for node in unknown}
    fresh = set()
    for parent_id, child_id, status, fen in (
            Edge.objects.filter(parent_id__in=unknown)
            .order_by('id')
            .values_list('parent_id', 'child_id', 'child__status',
                         'child__fen')):
        by_node[parent_id].append((child_id, status, fen))
        if child_id not in cache.numbers:
            fresh.add(child_id)
    for node, rows in by_node.items():
        cache.children[node] = tuple(rows)
    if fresh:
        found = {key: (pn, dn) for key, pn, dn in ProofNode.objects.filter(
            campaign=campaign, position_id__in=fresh).values_list(
            'position_id', 'pn', 'dn')}
        for child_id in fresh:
            cache.numbers.setdefault(child_id, found.get(child_id))


def _carrier_next(goal, cache, node, fen, want_dn):
    """El hijo que PORTA el valor de ``node``, o ``None`` si nadie lo porta.

    Solo un hijo ABIERTO y con ``ProofNode`` puede portar realimentacion: un
    cierre vale su verdad y una hoja sin fila vale su inicializacion, y ni la
    una ni la otra dependen de nadie.  Empates: el primero en orden de
    arista, el mismo desempate determinista del resto del modulo.
    """
    best_key, best_value = None, None
    or_node = is_or_node(fen, goal)
    for child_id, status, _child_fen in cache.children.get(node, ()):
        if status != 'UNKNOWN':
            continue
        numbers = cache.numbers.get(child_id)
        if numbers is None:
            continue
        value = numbers[1] if want_dn else numbers[0]
        if best_value is None:
            best_key, best_value = child_id, value
            continue
        if want_dn:
            # dn: en OR es una suma (manda el sumando mayor), en AND un min.
            better = value > best_value if or_node else value < best_value
        else:
            # pn: en OR es un min, en AND una suma (manda el sumando mayor).
            better = value < best_value if or_node else value > best_value
        if better:
            best_key, best_value = child_id, value
    return best_key


def _cycling_edges(campaign, parents, children_by_parent, node_rows, cache):
    """``{(padre, jugada)}`` cuyos valores vuelven al propio padre.

    Dos paseos por arista — el portador de dn y el de pn — porque las dos
    recurrencias realimentan por caminos duales; basta que uno vuelva.
    Paradas, todas ellas "no hay ciclo": ningun hijo abierto con numeros que
    portar, ciclo que se cierra sin pasar por el padre (problema de OTRO
    padre, que lo vera cuando le toque) y el tope de plies.
    """
    walks = []
    for parent_key in parents:
        for move_uci, child_id, status, _eval_cp, fen in (
                children_by_parent.get(parent_key) or ()):
            if status != 'UNKNOWN':
                continue      # bajo un cierre no hay valor prestado que rodar
            if node_rows.get(child_id) is None \
                    and cache.numbers.get(child_id) is None:
                continue      # sin fila de prueba no hay numero que volver
            for want_dn in (True, False):
                walks.append([parent_key, move_uci, child_id, fen, want_dn,
                              {child_id}])
    cycling = set()
    for _ in range(PROOF_CYCLE_MAX_PLIES):
        if not walks:
            break
        _resolve_carrier_steps(campaign, cache,
                               {walk[2] for walk in walks})
        alive = []
        for walk in walks:
            parent_key, move0, node, fen, want_dn, seen = walk
            if (parent_key, move0) in cycling:
                continue      # el otro paseo de la arista ya la corto
            below = _carrier_next(campaign.goal, cache, node, fen, want_dn)
            if below is None:
                continue
            if below == parent_key:
                cycling.add((parent_key, move0))
                continue
            if below in seen:
                continue
            step = next((row for row in cache.children.get(node, ())
                         if row[0] == below), None)
            if step is None:
                continue
            seen.add(below)
            walk[2], walk[3] = below, step[2]
            alive.append(walk)
        walks = alive
    return cycling


def compute_numbers(campaign, position, children, child_nodes, previous=None,
                    cycling=frozenset()):
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

    ``cycling`` son las aristas de ESTE nodo cuyo valor vuelve a el
    (§ ``_cycling_edges``): entran con la inicializacion de HOJA de su hijo
    —  la estimacion estatica de siempre — en vez del numero que el ciclo se
    inventa.  La histeresis no necesita caso especial: un primario que solo
    era el mejor por el eco del ciclo deja de serlo en cuanto su pn pasa a
    ser el de una hoja, y el retador lo destrona por la aritmetica de
    siempre.
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
        cycles = (position.key, move_uci) in cycling
        if node is not None and not cycles:
            numbers.append((node.pn, node.dn))
        else:
            # Hoja: o el hijo no tiene fila de prueba todavia, o la tiene
            # pero su numero es el eco de este mismo nodo (§ arriba).  En
            # los dos casos la estimacion estatica es lo unico que se puede
            # afirmar sin inventar.
            pn, dn = leaf_numbers(
                fen, status, eval_cp, goal,
                annoyance=shallow_annoyance(fen, eval_cp))
            if cycles:
                # Y una repeticion se COBRA lo que cuesta: repitiendo no se
                # prueba nada, asi que probar POR AQUI es lo mas caro que
                # puede ser una hoja.  Es una afirmacion sobre el
                # PRESUPUESTO — que es lo unico que ``pn`` significa — y no
                # sobre la verdad del nodo, que es lo que un ``INF`` diria.
                # El tope de hoja lo mantiene acotado: sin esto el descenso
                # se quedaba en el bucle en cuanto la eval del bucle era
                # optimista, que es justo el caso de Eclipsia.  ``dn`` no se
                # toca: refutar una linea que repite no es mas caro por
                # repetir — basta una respuesta.
                pn = PROOF_MAX_LEAF
            numbers.append((pn, dn))
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
    carriers = _CarrierCache()  # memoria del paseo de repeticion, por pasada
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
        # Antes de que nadie sume: la arista que se justifica pasando por su
        # propio nodo entra a las recurrencias como REFUTADA, no con el
        # numero del ciclo (§ _cycling_edges, invariante 6).  Las filas que ya
        # estan en la mano siembran la cache: el paseo no vuelve a pedirlas.
        for node_key, node in existing.items():
            carriers.numbers.setdefault(node_key, (node.pn, node.dn))
        cycling = _cycling_edges(campaign, keys, children, existing, carriers)
        create, update, propagate = [], [], []
        for row in positions:
            processed += 1
            # La fila de prueba de este mismo nodo ya viene en ``existing``
            # (se lee junto a la de sus hijos), asi que el primario anterior
            # no cuesta ni una sentencia extra.
            node = existing.get(row.key)
            pn, dn, expanded, selected = compute_numbers(
                campaign, row, children.get(row.key, ()), existing,
                previous=None if node is None else node.selected_child,
                cycling=cycling)
            carriers.wrote(row.key, pn, dn)    # la cache no se queda vieja
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


def _point(counter, salt=''):
    """Punto uniforme en [0, 1) a partir del contador, por dominios.

    El ``salt`` separa sorteos que comparten contador.  Sin el, "que bucket
    del repertorio" y "arranco en una campana" saldrian del MISMO byte y
    quedarian correlacionados: el 35% de las campanas caeria siempre sobre los
    mismos contadores que el 5% de exploracion, y ninguno de los dos repartos
    seria el que dice ser.  El dominio vacio conserva el hash historico del
    reparto 80/15/5, que es lo que hace que esto no cambie ni una cola.
    """
    digest = hashlib.sha256(f'{salt}{int(counter)}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big') / float(1 << 64)


def _bucket(counter, policy):
    """Reparto determinista 80/15/5 por hash del contador.

    Un ``random`` haria irreproducible la cola; un modulo puro agruparia las
    exploraciones en rachas.  El hash del contador reparte uniforme y es
    replayable.
    """
    point = _point(counter)
    if point < policy['primary']:
        return 'primary'
    if point < policy['primary'] + policy['backup']:
        return 'backup'
    return 'explore'


# ---------------- de donde ARRANCA el descenso (campanas) ----------------
#
# EL PROBLEMA, en una linea: con ``ATOMICDB_SELECTOR=pn`` el trabajo lo
# reparte este descenso, y este descenso no lee ``Position.priority`` — que es
# donde vive el bono de campana (``ingest.CAMPAIGN_BONUS``).  Una campana
# ACTIVE con votos ganaba la columna por goleada y no recibia ni una tarea.
# El principio del propietario es explicito: si se establece una campana, el
# solver tiene que ir por esos arboles.
#
# LA REGLA.  Antes de cada descenso se sortea si arranca en la raiz global o
# en la raiz de una campana ACTIVE.  Lo demas del descenso NO cambia: primaria
# pegajosa, reparto 80/15/5, reserva del lote, tope de plies.  La campana
# decide DONDE se empieza a mirar, no como se prueba.
#
# EL TOPE ES LA MITAD IMPORTANTE.  Sin el, tres campanas votadas se llevarian
# el solver entero y el teorema de la raiz — que es lo que este proyecto
# prueba — se quedaria sin motor.  Con el, dos de cada tres descensos siguen
# saliendo de la raiz global pase lo que pase.  El razonamiento entero, con
# los numeros de la auditoria, en docs/solver-allocation.md.
CAMPAIGN_DESCENT_SHARE = 0.35
# ``ln(1+0) = 0``: una campana ACTIVE sin votos pesaria cero y volveria a ser
# inerte, que es justo el fallo que esto arregla.  Activar es el voto del
# propietario y vale uno.  PROPOSED y PAUSED siguen valiendo cero descensos:
# la linea entre "la comunidad pide" y "el propietario concede" no se mueve.
CAMPAIGN_ACTIVATION_VOTES = 1


def campaign_descent_enabled():
    """Descensos con raiz de campana.  ENCENDIDO, con vuelta atras."""
    return bool(getattr(settings, 'ATOMICDB_CAMPAIGN_DESCENT', True))


def campaign_roots():
    """``[(Position, peso)]`` de las campanas ACTIVE con raiz abierta.

    El peso es ``ln(1+votos)`` con el suelo de activacion, el mismo logaritmo
    que ya usaba el bono de la columna.  Una raiz ya cerrada no entra: no hay
    descenso que hacer por debajo de ella, y el cierre ya la pasa a DONE por
    el camino de siempre.
    """
    if not campaign_descent_enabled():
        return []
    rows = Campaign.objects.filter(
        state=Campaign.CState.ACTIVE,
        root__status='UNKNOWN').select_related('root').order_by('id')
    return [(row.root,
             math.log1p(max(CAMPAIGN_ACTIVATION_VOTES, int(row.votes or 0))))
            for row in rows]


def campaign_start(counter, roots=None):
    """La raiz donde arranca ESTE descenso, o ``None`` para la raiz global.

    Sorteo determinista por dominio propio (ver ``_point``): el mismo estado
    produce la misma cola y un replay sigue siendo reproducible.  La fraccion
    de cada campana es su peso normalizado POR el tope global, asi que la suma
    de todas no puede pasar de ``CAMPAIGN_DESCENT_SHARE``.
    """
    roots = campaign_roots() if roots is None else roots
    total = sum(weight for _, weight in roots)
    if not roots or total <= 0:
        return None
    point = _point(counter, 'campaign:')
    edge = 0.0
    for root, weight in roots:
        edge += CAMPAIGN_DESCENT_SHARE * weight / total
        if point < edge:
            return root
    return None


# ---------------- el clamp de los nodos OR resueltos ----------------
#
# En un nodo OR mueve el atacante y le basta UNA jugada buena.  Si ya hay un
# hijo con el objetivo PROBADO — un cierre, no una eval optimista — el nodo
# esta probado y sus demas hijos no le deben nada a la prueba.  Medido el
# 5-ago sobre la base viva: 151 nodos OR en esa situacion con 3.936 hermanos
# todavia UNKNOWN colgando, que a 8M por tarea son 31,5 G nodos de motor que
# la prueba no necesita.
#
# Es ``_short_mate_clamp`` generalizado a distancia DESCONOCIDA: aquel compra
# la verificacion de un mate reclamado en vez de la excavacion; este compra
# una mirada barata a un hermano en vez de un peldano entero.  Mismo par
# ``(presupuesto, multipv)`` y mismo camino por ``ingest.multipv_for``.
#
# NO ES CERO a proposito: un hermano sin ninguna eval es un agujero en el DAG
# (lo mira el explorador, lo respalda la cascada, lo ordena la tabla de
# jugadas).  Se le compra una mirada, no el silencio.
OR_CLAMP_NODES = 250_000
OR_CLAMP_MULTIPV = 1
# Tope de padres que se miran por nodo.  El DAG transpone y esto corre dentro
# del camino HTTP del arriendo: con mas padres que esto, no se abarata nada.
# Conservador por diseno — dudar cuesta presupuesto, equivocarse cuesta una
# prueba.
OR_CLAMP_MAX_PARENTS = 32


def or_clamp_enabled():
    """El presupuesto minimo de hermanos.  ENCENDIDO, con vuelta atras."""
    return bool(getattr(settings, 'ATOMICDB_OR_CLAMP', True))


def _settled_for(goal, parents, proved):
    """True si NINGUN padre necesita ya este nodo para esta campana.

    Un padre cerrado no influye hacia arriba (la misma razon por la que
    ``ingest._still_reachable`` pone lapidas).  Un padre OR con un hijo
    probado esta cerrado PARA LA PRUEBA aunque el DAG no haya llegado todavia
    a escribirle el cierre.  Cualquier otro padre — y en particular cualquier
    nodo AND, donde hay que refutar TODAS las respuestas — mantiene vivo al
    hijo, y basta uno para que no se abarate nada.
    """
    for key, fen, status in parents:
        if status != 'UNKNOWN':
            continue
        if is_or_node(fen, goal) and key in proved:
            continue
        return False
    return True


def proved_or_clamp(position_key, campaigns=None):
    """``(nodos, multipv)`` minimos si la prueba ya no necesita este nodo.

    ``None`` significa "aqui no manda esta politica" y el presupuesto sigue
    siendo el de la escalera.  Se exige que el nodo este resuelto para TODAS
    las campanas activas: una sola que todavia lo necesite paga su peldano.
    """
    if not or_clamp_enabled():
        return None
    campaigns = active_campaigns() if campaigns is None else campaigns
    if not campaigns:
        return None
    parent_ids = list(Edge.objects.filter(child_id=position_key)
                      .values_list('parent_id', flat=True)
                      .distinct()[:OR_CLAMP_MAX_PARENTS + 1])
    if not parent_ids or len(parent_ids) > OR_CLAMP_MAX_PARENTS:
        # Sin padres es la raiz (o un cajetin FEN suelto): nadie puede
        # declararlo inutil.  Con demasiados, no se mira.
        return None
    parents = list(Position.objects.filter(key__in=parent_ids)
                   .values_list('key', 'fen', 'status'))
    for campaign in campaigns:
        proved = set(Edge.objects.filter(
            parent_id__in=parent_ids, child__status=campaign.goal)
            .values_list('parent_id', flat=True))
        if not proved or not _settled_for(campaign.goal, parents, proved):
            return None
    return OR_CLAMP_NODES, OR_CLAMP_MULTIPV


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


def descend(campaign, counter=0, max_plies=DESCENT_MAX_PLIES, avoid=(),
            start=None):
    """Baja desde la raiz hasta la posicion mas demostradora sin resolver.

    Devuelve ``(Position, plies)`` o ``(None, plies)`` si la campana no tiene
    nada abierto por debajo de su raiz.

    ``avoid`` es la RESERVA: las posiciones que este mismo lote ya reparte.
    Sin ella, un arbol somero devolveria la misma hoja una y otra vez y un
    lote de cuatro tareas serian cuatro copias de la misma pregunta — el
    "trabajo duplicado" que la literatura de PNS distribuida evita con virtual
    loss.  Aqui es mas simple: un nodo reservado no se puntua.

    ``start`` es de DONDE se arranca, y por defecto es la raiz de la campana
    de prueba.  Cualquier otra cosa es la raiz de una campana de la comunidad
    (§ ``campaign_start``): la prueba, la politica y el objetivo siguen siendo
    los de ``campaign``, solo cambia el punto de entrada al arbol.
    """
    policy = normalized_policy(campaign)
    node = campaign.root if start is None else start
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


# ---------------- el descenso por VALOR: el walker de la espina ------------
#
# POR QUE HAY UN SEGUNDO DESCENSO, con los numeros que lo pidieron.  El de
# arriba es df-pn puro y optimiza UNA cosa: lo que cuesta completar la prueba
# formal.  Medido en produccion el 11-ago sobre la campana WHITE_WIN desde
# startpos: en la raiz, ``1.e3`` tiene pn = 119 y ``g1f3`` pn = 475, asi que el
# minimo se va por ``1.e3`` — y el reparto REAL de las ultimas 40 tareas AUTO
# fue 26 bajo ``1.e3``, UNA bajo ``g1f3``, y las 40 al primer peldano (8M).
# ``g1f3`` es, por respaldo, la mejor jugada del tablero: +812.
#
# El diagnostico no es que los pn esten mal calculados.  Es que a esta
# distancia de la prueba TODOS los pn son ficcion — nadie va a cerrar Atomic
# desde startpos esta decada — y un minimo sobre ficciones degenera en "la
# linea floja mas barata de enumerar".  Un pn de 119 no dice que ``1.e3`` gane;
# dice que a ``1.e3`` todavia le quedan pocos hijos informados.
#
# LO QUE MIRA ESTE DESCENSO EN SU LUGAR: la ESPINA de ``backed_move``.  Esa
# cadena ya es el negamax de los DOS bandos — mejor jugada del atacante, mejor
# defensa del defensor — calculada por ``ingest.backup_backed_evals`` sobre
# busqueda real, y es la misma que el explorador pinta con el chip de respaldo.
# Se baja por ella hasta la hoja cuyo ``eval_cp`` crudo sostiene el numero de
# arriba y se compra analisis AHI.  El enunciado del propietario, entero:
# "cogeria el mejor opening, miraria la mejor respuesta negra y bajaria los 22
# plies hasta analizar las jugadas concretas que sostienen el 812.  Buen
# analisis ahi, y al siguiente cuello de botella".
#
# QUE NO CAMBIA.  El objetivo de la campana, el reparto blando 80/15/5, la
# reserva del lote, el tope de plies, las campanas de la comunidad como punto
# de arranque y los clamps de presupuesto siguen siendo exactamente los de
# antes.  Cambia por DONDE se baja y, en consecuencia, cuanto se compra.
#
# DONDE vs CUANTO, otra vez separados.  Este modulo decide el sitio y devuelve
# un BRAZO que lo explica; el precio de cada brazo lo pone ``ingest``
# (§ ``value_budget``), que es donde viven las dos escaleras.  Es la misma
# division que ya tenian ``descend`` y ``budget_for``, y es lo que permite
# cambiar el precio de un brazo sin tocar el paseo.

DESCENT_PROOF = 'proof'
DESCENT_VALUE = 'value'

# Brazos del walker.  Viajan dentro del plan, no a la columna ``arm`` de la
# tarea: alli sigue mandando quien la PIDIO (el descenso normal, o una campana
# de la comunidad), que es lo que hace visible el voto de quien vota.
VALUE_ARM_SPINE = 'spine'            # la punta de la espina: donde se compra
VALUE_ARM_BOTTLENECK = 'bottleneck'  # punta comprada: el cuello de mas arriba
VALUE_ARM_RUNG = 'rung'              # punta comprada y sin cuello: mas hondo
VALUE_ARM_CYCLE = 'cycle'            # la espina se muerde la cola
VALUE_ARM_BACKUP = 'backup'          # desvio por la alternativa competitiva
VALUE_ARM_EXPLORE = 'explore'        # el mejor hijo sin explorar del camino

# CUANDO UNA ALTERNATIVA SIGUE SIENDO UNA PREGUNTA.  Peon y medio de
# distancia respecto a la primaria, PARA EL QUE MUEVE.  Por debajo de eso las
# dos jugadas compiten de verdad y cual sostiene el valor es una pregunta
# abierta; por encima, gastar en la segunda es comprar lo que ya se sabe.  Es
# el mismo umbral con el que el ingest declara que dos afirmaciones sobre un
# nodo se contradicen (``UNCERTAINTY_GAP``), y por la misma razon: es donde
# una diferencia deja de ser ruido de busqueda.
VALUE_ALTERNATIVE_MARGIN_CP = 150

# Tope de desvios que prueba UN intento, y es el guardarrail del riesgo que
# tiene este descenso por construccion: UNA espina y sesenta y cuatro huecos
# de colchon.  Cada tarea que se mintea reserva su punta, asi que la siguiente
# tiene que ofrecer otro sitio; si el tope fuera pequeno, el colchon se
# quedaria a medio llenar y la flota mirando al techo — que es peor que la
# dispersion que venimos a quitar.  Sesenta y cuatro es el tamano del colchon
# (``ingest.ANALYSIS_POOL_TARGET``) por eso mismo: el paseo tiene que poder
# ofrecer tantos sitios distintos como se le piden.  El coste no se dispara
# porque los desvios son PEREZOSOS — solo se camina el que hace falta — y
# porque una alternativa a menos de peon y medio de la primaria es rara: en
# una linea decidida no hay sesenta, hay dos o tres por nodo.
VALUE_BACKTRACK_MAX = 64

# ``(posicion, plies, brazo, suelo de nodos)``.  ``floor_nodes`` solo viaja
# cuando el brazo lo necesita para elegir peldano: los nodos que la punta ya
# tiene (revisita) o los del hijo que cierra el ciclo (desambiguacion).
ValueTarget = namedtuple('ValueTarget', 'position plies arm floor_nodes')

_VALUE_CHILD_COLUMNS = ('move_uci', 'child_id', 'child__status', 'child__fen',
                        'child__eval_cp', 'child__backed_eval',
                        'child__backed_move', 'child__backed_nodes',
                        'child__nodes_invested')


def descent_mode():
    """``proof`` (df-pn, el historico y el DEFAULT) o ``value`` (la espina).

    El default vive en el codigo y es el comportamiento de siempre: encender
    el walker es una linea en el env file del despliegue y quitarla es el
    rollback entero, sin desplegar nada.  Cualquier valor que no sea
    exactamente ``value`` deja el descenso df-pn, que es la respuesta segura a
    un entorno mal escrito.
    """
    mode = str(getattr(settings, 'ATOMICDB_DESCENT', DESCENT_PROOF)).lower()
    return DESCENT_VALUE if mode == DESCENT_VALUE else DESCENT_PROOF


def _saturated_nodes():
    """Nodos a partir de los cuales la punta ya esta comprada de entrada.

    Es el MISMO numero que el objetivo primario compra (§ ``ingest``,
    ``VALUE_SPINE_NODES``), leido de alli y no copiado aqui: si el primer
    peldano de peticiones vuelve a moverse con la capacidad donada — ya paso
    dos veces — el umbral de "esto ya esta comprado" tiene que moverse con el
    o el walker empezaria a re-comprar lo que acaba de pagar.
    """
    from . import ingest
    return ingest.VALUE_SPINE_NODES


class _ValueChild:
    """Un hijo visto por el walker: una fila de arista con lo justo.

    Los atributos se llaman como las COLUMNAS de ``Position`` a proposito.
    Los dos predicados que este descenso pregunta — que es un hijo SIN JUZGAR
    y que es uno SIN EXPLORAR — viven en ``ingest``, son uno cada uno y son
    deliberadamente distintos entre si (§ ``is_unjudged``, ``is_unexplored``).
    Darles algo con forma de posicion es lo que evita a la vez materializar un
    ``Position`` por hijo y copiar aqui las reglas, que es como dos criterios
    que dicen ser el mismo dejan de serlo.
    """

    __slots__ = ('index', 'move_uci', 'key', 'status', 'fen', 'eval_cp',
                 'backed_eval', 'backed_move', 'backed_nodes',
                 'nodes_invested', 'value')

    def __init__(self, index, move_uci, key, status, fen, eval_cp, backed_eval,
                 backed_move, backed_nodes, nodes_invested):
        self.index = index
        self.move_uci = move_uci
        self.key = key
        self.status = status
        self.fen = fen
        self.eval_cp = eval_cp
        self.backed_eval = backed_eval
        self.backed_move = backed_move
        self.backed_nodes = backed_nodes
        self.nodes_invested = nodes_invested
        self.value = None       # best_known en POV blanca, lo pone el orden


class _ValueStep:
    """Un nodo del camino recorrido, con lo que se miro en el."""

    __slots__ = ('key', 'fen', 'nodes', 'children', 'ranked', 'primary')

    def __init__(self, key, fen, nodes, children, ranked):
        self.key = key
        self.fen = fen
        self.nodes = nodes or 0
        self.children = children
        self.ranked = ranked
        self.primary = None     # el hijo por el que siguio la espina


def _value_children(parent_key):
    """Los hijos de un nodo con todo lo que el walker mira, en UNA consulta."""
    rows = (Edge.objects.filter(parent_id=parent_key).order_by('id')
            .values_list(*_VALUE_CHILD_COLUMNS))
    return [_ValueChild(index, *row) for index, row in enumerate(rows)]


def _value_ranked(goal, fen, children, child_nodes):
    """Hijos VIVOS ordenados por "lo mejor para el que mueve, primero".

    El valor de un hijo es su ``best_known`` — respaldo si lo hay, eval propia
    si no — en perspectiva BLANCA, como toda columna de este sistema; el orden
    lo pone el bando al turno, que en un DAG cambia cada ply.  La precedencia
    no se reescribe aqui: la contesta ``ingest.known_eval_of``, que es la
    unica que la sabe.

    Un hijo SIN ningun valor no se mezcla con los que si lo tienen: va al
    final, porque "no se sabe" no es un numero con el que comparar.  Comprarle
    la primera mirada es un acto distinto y tiene su propio brazo.

    ``pn`` entra SOLO como desempate, y esa es toda su participacion en este
    descenso: dos hijos que valen lo mismo para el que mueve siguen
    diferenciandose en lo que cuesta cerrarlos, y esa es justo la pregunta que
    pn contesta bien.  Sin fila de prueba se puntua con la inicializacion de
    hoja de siempre, igual que hace ``_ranked_children``.
    """
    from . import ingest
    stm_white = fen.split()[1] == 'w'
    ranked = []
    for child in children:
        if child.status != 'UNKNOWN':
            continue          # ya resuelto: no queda pregunta que hacerle
        child.value = ingest.known_eval_of(child.status, child.backed_eval,
                                           child.eval_cp)
        node = child_nodes.get(child.key)
        pn = (node.pn if node is not None else leaf_numbers(
            child.fen, child.status, child.eval_cp, goal,
            annoyance=shallow_annoyance(child.fen, child.eval_cp))[0])
        if child.value is None:
            order = (1, 0, pn, child.index)
        else:
            order = (0, -child.value if stm_white else child.value, pn,
                     child.index)
        ranked.append((order, child))
    ranked.sort(key=lambda item: item[0])
    return [child for _order, child in ranked]


def _value_primary(ranked, node):
    """El primario del FALLBACK: la histeresis si sigue viva, si no el mejor.

    ``selected_child`` es lo unico escrito sobre quien era el primario de un
    nodo, y aqui es lo unico para lo que se usa: la espina no lo necesita
    — ``backed_move`` ya dice por donde va — y el reparto 80/15/5 tampoco.
    """
    if node is not None and node.selected_child:
        for child in ranked:
            if child.move_uci == node.selected_child:
                return child
    return ranked[0]


def _spine_step(backed_move, ranked, node):
    """El hijo por el que sigue la espina, o ``None`` si aqui se acaba.

    ``backed_move`` ES la espina.  Un nodo sin ella es la HOJA cuyo eval crudo
    sostiene el valor de todo lo que cuelga por encima, y por tanto el sitio
    donde comprar analisis mueve el numero: ahi termina el paseo.

    El fallback no es una segunda politica de descenso.  Cubre el caso en que
    la espina apunta a un hijo que ESTE paseo no puede seguir — cerrado desde
    la ultima cascada, o ya visitado — y entonces manda el orden por valor con
    la histeresis.  Sin el, una espina que acaba de cerrarse dejaria el paseo
    parado en un nodo que ya no tiene nada que preguntar.
    """
    if not backed_move or not ranked:
        return None
    for child in ranked:
        if child.move_uci == backed_move:
            return child
    return _value_primary(ranked, node)


def _walk_spine(campaign, key, fen, backed_move, nodes, visited, max_plies):
    """Baja la espina desde un nodo y devuelve ``(camino, hijo que cicla)``.

    ``visited`` se comparte y se MUTA: es el guardarrail de ciclos del paseo
    entero, y el DAG cierra ciclos de verdad (1.Nf3 Nf6 2.Ng1 Ng8 ES la
    posicion inicial una vez quitados los contadores).  El hijo que devuelve
    el segundo hueco es el que vuelve sobre algo ya pisado; ``None`` significa
    que el paseo termino por donde tenia que terminar.
    """
    path, plies = [], 0
    while True:
        children = _value_children(key)
        rows = _node_rows(campaign, [key] + [child.key for child in children])
        ranked = _value_ranked(campaign.goal, fen, children, rows)
        step = _ValueStep(key, fen, nodes, children, ranked)
        path.append(step)
        if plies >= max_plies:
            return path, None
        chosen = _spine_step(backed_move, ranked, rows.get(key))
        if chosen is None:
            return path, None
        if chosen.key in visited:
            return path, chosen
        step.primary = chosen
        visited.add(chosen.key)
        key, fen = chosen.key, chosen.fen
        backed_move, nodes = chosen.backed_move, chosen.nodes_invested
        plies += 1


def _has_unjudged(step):
    """True si a este nodo le cuelga alguna respuesta sin juzgar."""
    from . import ingest
    return any(ingest.is_unjudged(child) for child in step.children)


def _tip_plan(path, arm, plies):
    """El plan de la punta de un camino: comprarla, o subir al cuello.

    LA REVISITA.  Una punta que ya lleva encima el presupuesto del objetivo
    primario no necesita otra copia del mismo analisis: lo que la linea
    necesita entonces es el CUELLO DE BOTELLA, y ese es el primer nodo de la
    espina — de abajo arriba, que es como se busca lo mas cercano a donde se
    decide el valor — con respuestas que nadie ha juzgado todavia.  Una espina
    entera sin cuellos si es una linea que hay que profundizar, y entonces la
    punta cobra el peldano siguiente al que ya tiene.
    """
    tip = path[-1]
    if tip.nodes < _saturated_nodes():
        return tip.key, arm, None, plies
    for step in reversed(path):
        if _has_unjudged(step):
            return step.key, VALUE_ARM_BOTTLENECK, None, plies
    return tip.key, VALUE_ARM_RUNG, tip.nodes, plies


def _competitive(step):
    """Las alternativas a la primaria de un nodo que siguen siendo pregunta.

    Competitiva = a menos de ``VALUE_ALTERNATIVE_MARGIN_CP`` de la primaria
    PARA EL QUE MUEVE, y por tanto capaz de acabar sosteniendo el valor ella
    misma.  Una alternativa que ademas es MEJOR que la primaria entra sola por
    la misma desigualdad: la diferencia le sale negativa.

    Un hijo sin ningun valor no entra aqui — no se puede afirmar que compita
    con nada — y no se pierde: lo virgen tiene su propio brazo.
    """
    primary = step.primary or (step.ranked[0] if step.ranked else None)
    if primary is None or primary.value is None:
        return []
    stm_white = step.fen.split()[1] == 'w'
    rivals = []
    for child in step.ranked:
        if child is primary or child.value is None:
            continue
        loss = (primary.value - child.value if stm_white
                else child.value - primary.value)
        if loss < VALUE_ALTERNATIVE_MARGIN_CP:
            rivals.append(child)
    return rivals


def _value_detours(campaign, path, max_plies):
    """Desvios por alternativa competitiva, del nodo MAS PROFUNDO hacia arriba.

    Cada desvio se resuelve como el paseo principal: se baja por la
    alternativa y se sigue su espina hasta donde termine.  Perezoso a
    proposito — cada uno cuesta consultas — y de abajo arriba porque lo
    profundo es donde el valor se esta decidiendo de verdad; una alternativa
    en el ply 1 cambia de repertorio, no de respuesta.

    Es tambien el BACKTRACK del contrato de reserva: cuando la punta ya la
    reparte este mismo lote, o ya tiene una tarea viva, el paseo no se rinde
    ni entrega un duplicado — se va por la alternativa mas profunda que
    todavia es una pregunta abierta.
    """
    tried = 0
    for depth in range(len(path) - 1, -1, -1):
        step = path[depth]
        prefix = [item.key for item in path[:depth + 1]]
        for child in _competitive(step):
            if tried >= VALUE_BACKTRACK_MAX:
                return
            if child.key in prefix:
                continue
            tried += 1
            visited = set(prefix)
            visited.add(child.key)
            branch, _cycled = _walk_spine(
                campaign, child.key, child.fen, child.backed_move,
                child.nodes_invested, visited,
                max(0, max_plies - depth - 1))
            yield path[:depth + 1] + branch, depth + len(branch)


def _best_unexplored(path):
    """``(hijo, plies)`` sin explorar mas prometedor del camino, o ``None``.

    ES EL 5% DEL REPARTO, RE-SIGNIFICADO.  El descenso df-pn lo gastaba en
    ``ranked[-1]`` — el PEOR hijo del nodo — que es explorar lo que ya se sabe
    malo, y es uno de los defectos que este paquete viene a quitar.  Aqui se
    gasta en lo que NADIE ha mirado: una eval sembrada por el MultiPV del
    padre es una reclamacion heredada, no una medida, y este brazo compra la
    medida.

    Se busca de abajo arriba y se para en el primer nodo que tenga alguno,
    por lo mismo que los desvios: la comparacion entre dos hijos virgenes de
    NODOS distintos no existe — mueven bandos distintos a plies distintos —
    pero dentro de un nodo si, y el nodo mas profundo es el que esta mas cerca
    de donde se decide el valor.
    """
    from . import ingest
    for plies in range(len(path) - 1, -1, -1):
        step = path[plies]
        stm_white = step.fen.split()[1] == 'w'
        virgin = [child for child in step.children
                  if ingest.is_unexplored(child) and child.eval_cp is not None]
        if not virgin:
            continue
        best = max(virgin, key=lambda child: (child.eval_cp if stm_white
                                              else -child.eval_cp))
        return best, plies + 1
    return None


def _value_plans(campaign, path, cycled, bucket, max_plies):
    """Los objetivos que este intento probaria, en orden de preferencia.

    Generador a proposito: el primero que sea utilizable gana y los desvios
    — lo unico que cuesta consultas de mas — no se calculan si no hacen falta.
    """
    if cycled is not None:
        # La espina vuelve sobre si misma.  Repetir el peldano del hijo que
        # cierra el bucle reproduciria la misma espina; el siguiente re-ordena
        # de verdad.  Mismo espiritu que ``ingest._queue_cycle_disambiguation``
        # y misma cuenta: por encima de lo que ese hijo ya tiene.
        yield (path[-1].key, VALUE_ARM_CYCLE, cycled.nodes_invested + 1,
               len(path) - 1)
    detours = _value_detours(campaign, path, max_plies)
    if bucket == 'backup':
        branch = next(detours, None)
        if branch is not None:
            yield _tip_plan(branch[0], VALUE_ARM_BACKUP, branch[1])
    elif bucket == 'explore':
        virgin = _best_unexplored(path)
        if virgin is not None:
            yield virgin[0].key, VALUE_ARM_EXPLORE, None, virgin[1]
    yield _tip_plan(path, VALUE_ARM_SPINE, len(path) - 1)
    for branch, plies in detours:
        yield _tip_plan(branch, VALUE_ARM_SPINE, plies)


def _has_live_task(position_key):
    """True si esa posicion ya tiene una tarea PENDING o LEASED.

    ``avoid`` cubre el lote que se esta repartiendo AHORA; esto cubre el que
    se reparte desde hace un rato.  Una punta con la tarea arrendada no
    necesita una segunda copia de la misma pregunta: necesita que el paseo se
    vaya por la alternativa.
    """
    return AnalysisTask.objects.filter(
        position_id=position_key,
        state__in=(AnalysisTask.TState.PENDING,
                   AnalysisTask.TState.LEASED)).exists()


def descend_value(campaign, counter=0, max_plies=DESCENT_MAX_PLIES, avoid=(),
                  start=None):
    """El objetivo del descenso por VALOR, o ``None`` si no hay ninguno.

    Devuelve un ``ValueTarget``: la posicion donde comprar, a que profundidad
    se encontro, con que BRAZO — que es lo que ``ingest.value_budget`` traduce
    a presupuesto — y el suelo de nodos cuando el brazo lo necesita.

    ``avoid``, ``start`` y ``max_plies`` significan exactamente lo mismo que en
    ``descend``: la reserva del lote, la raiz de la campana de la comunidad
    donde arranca este descenso y el tope de plies del paseo.

    EL REPARTO 80/15/5 SE SORTEA UNA VEZ POR PASEO, no una por ply.  En el
    descenso df-pn el bucket elegia una jugada en CADA nodo, asi que tenia
    sentido re-sortearlo con la profundidad; aqui describe el paseo entero —
    la punta de la espina, un desvio competitivo o un hijo virgen — y sortearlo
    varias veces solo emborronaria las tres fracciones.  El sorteo sigue siendo
    el mismo hash determinista de siempre: mismo estado, misma cola, replay
    reproducible.
    """
    node = campaign.root if start is None else start
    if node.status != 'UNKNOWN':
        return None
    reserved = set(avoid)
    bucket = _bucket(counter, normalized_policy(campaign))
    path, cycled = _walk_spine(campaign, node.key, node.fen, node.backed_move,
                               node.nodes_invested, {node.key}, max_plies)
    for key, arm, floor, plies in _value_plans(campaign, path, cycled, bucket,
                                               max_plies):
        if key in reserved or _has_live_task(key):
            continue
        position = Position.objects.filter(key=key).first()
        if position is None or position.status != 'UNKNOWN':
            continue
        return ValueTarget(position, plies, arm, floor)
    return None


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


def saturated_open_count(campaign=None, column=None):
    """Nodos ABIERTOS con pn o dn saturados, contados aparte del frente.

    El frente los excluye con razon — no hay esfuerzo que estimar en un
    numero saturado — pero hasta el 6-ago los excluia EN SILENCIO, y 534
    nodos con dn saturado desaparecieron de la mediana, del frente mas
    demostrador y del brazo de reparacion de dn sin que ningun panel los
    contara.  Este contador es ese "aparte".

    NINGUNA SATURACION SUELTA SIGNIFICA AVERIA, y esto hay que leerlo antes
    de poner una cifra en una portada.  Medido sobre la base viva el 6-ago,
    despues del barrido de re-baseline:

    * ``pn`` saturado (1.168 nodos) es corriente y honesto: dice "por aqui no
      se prueba el objetivo", que es lo que vale un nodo con todas sus
      continuaciones refutadas — incluida la refutacion POR REPETICION que
      introduce el invariante 6.  Contarlo como averia seria llorar por la
      regla nueva haciendo su trabajo.
    * ``dn`` saturado (517 nodos) resulto ser, EN SU TOTALIDAD, la firma
      legitima de un nodo PROBADO al que la cascada todavia le debe el
      cierre: los 517 llevan ``pn = 0``, y 334 tienen un hijo WHITE_WIN
      colgando, que por la recurrencia de un nodo OR da exactamente
      ``(0, INF)``.  Publicar esa cifra como alarma seria publicar el
      backlog de la cascada con otro nombre.
    * ``impossible`` — ``dn`` saturado con ``pn`` FINITO y mayor que cero —
      es el unico estado que las recurrencias sanas no pueden producir: dn
      infinito significa probado y probado es pn = 0.  Es la firma exacta
      del trinquete de Eclipsia (pn=1, dn=2^62) y es la que vigila la
      portada.  Tras el barrido: CERO en toda la base.
    """
    rows = ProofNode.objects.filter(position__status='UNKNOWN')
    if column == 'dn':
        rows = rows.filter(dn__gte=PROOF_INFINITY)
    elif column == 'pn':
        rows = rows.filter(pn__gte=PROOF_INFINITY)
    elif column == 'impossible':
        rows = rows.filter(dn__gte=PROOF_INFINITY, pn__gt=0,
                           pn__lt=PROOF_INFINITY)
    else:
        rows = rows.filter(
            Q(pn__gte=PROOF_INFINITY) | Q(dn__gte=PROOF_INFINITY))
    if campaign is not None:
        rows = rows.filter(campaign=campaign)
    return rows.count()


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

    ``saturated`` viaja con las tres cifras del frente porque es su
    contrapartida (§ ``saturated_open_count``): los abiertos que el frente NO
    mira, contados aparte para que no desaparezcan en silencio.  Es la cifra
    IMPOSIBLE — dn saturado con pn finito — y no la saturacion a secas, que
    esta dominada por cierres pendientes y refutaciones honestas.
    """
    saturated = saturated_open_count(campaign, column='impossible')
    dns = sorted(row[3] for row in frontier_and_rows(campaign, limit=limit))
    if not dns:
        return {'and_nodes': 0, 'dn_median': 0, 'thin': 0,
                'saturated': saturated}
    middle = len(dns) // 2
    median = (dns[middle] if len(dns) % 2
              else (dns[middle - 1] + dns[middle]) // 2)
    return {'and_nodes': len(dns), 'dn_median': int(median),
            'thin': sum(1 for value in dns if value <= floor),
            'saturated': saturated}


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
