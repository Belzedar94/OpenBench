"""Estimador de COSTE DE RESOLVER, independiente del eval del motor.

POR QUE UNA SEGUNDA OPINION
---------------------------
El eval del motor contesta "quien esta mejor".  La prueba necesita contestar
otra cosa: "cuanto cuesta CERRAR esto".  Las dos preguntas se parecen lo
suficiente como para confundirlas y son distintas lo suficiente como para que
confundirlas salga caro: un final de torre pelado con +12 esta ganado y puede
costar cincuenta jugadas de tecnica, sesenta plies sin resetear el contador de
50 y una montana de nodos; una posicion con +2 y una amenaza de mate en cuatro
se cierra en una tarde.  Un df-pn que inicializa sus hojas SOLO con el eval
perfora exactamente las primeras, porque le prometen mucho y no le cobran
nada.

De ahi la PUERTA DOBLE (idea de Wolfram, 30-jul-2026): un nodo merece esfuerzo
de RESOLUCION cuando las dos senales coinciden — el motor lo ve prometedor Y
este estimador lo ve barato.  El cuadrante de DESACUERDO — motor optimista,
estimador pesimista — no se descarta: se REGISTRA, porque es justo donde viven
los callejones sin salida y porque esas filas son el conjunto de entrenamiento
del estimador que sustituira a este.

QUE PUEDE MIRAR Y QUE NO
------------------------
``annoyance`` es PURA, barata y OFFLINE: solo mira datos que ya estan en la
fila (o que el llamante ya tiene en la mano).  No llama al motor, no consulta
la base — ni siquiera indirectamente: los campos diferidos por un ``.only()``
se leen con ``_stored``, que devuelve ``None`` en vez de disparar el refresco
perezoso de Django.  Un estimador que costase una consulta por hoja no seria
un estimador de coste, seria un coste.

Se llama ``annoyance`` (molestia) y no ``difficulty`` a proposito: 0 no
significa "facil de ganar", significa "barato de RESOLVER", y 1 no es
"perdido" sino "pesadilla tediosa".  Es ortogonal a quien gana, y por eso
todas sus features son ciegas al color y al objetivo de la campana.

LAS CUATRO FEATURES Y SU RACIONAL
---------------------------------
Pesos FIJOS que suman 1.  No son probabilidades ni estan ajustados a nada:
son un orden de importancia declarado, y el orden es el argumento.

* ``reversible`` (0.35) — densidad de plies de la PV vigente que NO resetean
  el contador de 50.  Es la senal mas directa de "tedioso" que hay en la fila:
  una PV donde nadie captura ni mueve un peon es una PV de maniobra, y en este
  arbol una maniobra larga no solo cuesta plies — se come el ``clock_slack``
  del cierre, que es lo que hace que un cierre valga desde varios relojes de
  entrada y no solo desde cero.  Peso el mayor porque es la unica feature que
  habla del reloj.
* ``branching`` (0.25) — cuantas respuestas hay que tratar.  En un nodo del
  DEFENSOR es literalmente el numero de obligaciones (hay que refutarlas
  TODAS); en uno del atacante es una eleccion suya y la senal es mas debil,
  pero el tamano del subarbol crece con el ancho en los dos casos.  El
  estimador es ciego al objetivo (``annoyance(pos)`` no recibe ``goal``), asi
  que trata las dos igual y esto queda dicho en voz alta.
* ``material`` (0.20) — TIENDA, no rampa.  Por debajo del horizonte de
  tablebase el veredicto es una CONSULTA, no una prueba: molestia cero.  Justo
  por encima esta el peor sitio del arbol — demasiado pelado para un mate
  tactico, demasiado poblado para una consulta — y ahi la molestia es maxima.
  De ahi hacia arriba baja otra vez: en atomic una posicion densa es un campo
  de minas donde los mates llegan pronto, no un final de tecnica.
* ``eval_band`` (0.20) — el eval entra AQUI y solo como banda ordinal, con el
  mismo corte que ``proof.EVAL_BANDS``: mate visto = via rapida = molestia
  cero.  Es deliberadamente la contribucion mas pequena junto con la material:
  si el eval mandase, este modulo seria el motor otra vez y la puerta doble
  seria una puerta simple.

Una feature sin datos vale ``NEUTRAL`` (0.5): la ausencia de informacion ni
perdona ni condena.  La unica excepcion es ``branching`` sin numero de
aristas, donde el ancho del MultiPV solo puede EMPUJAR HACIA ARRIBA (ver
``branching_feature``).
"""

from collections import namedtuple

from . import logic

# --- pesos fijos (suman 1.0; hay un test que lo comprueba) ---
REVERSIBLE_WEIGHT = 0.35
BRANCHING_WEIGHT = 0.25
MATERIAL_WEIGHT = 0.20
EVAL_BAND_WEIGHT = 0.20

# Valor de una feature sin datos.
NEUTRAL = 0.5

# Ancho a partir del cual "tiene muchas respuestas" deja de decir mas.  Atomic
# ronda las 30-40 legales en el medio juego; por encima la diferencia entre 40
# y 60 ya no cambia si esto es tedioso, solo cuanto.
BRANCHING_SATURATION = 40
# Saturacion del PROXY de ancho (numero de lineas MultiPV vigentes).  Es otro
# numero porque mide otra cosa: el MultiPV mas ancho que este arbol pide es de
# un digito, asi que 5 lineas ya es "ancho" para lo que este proxy puede ver.
MULTIPV_SATURATION = 5

# Horizonte de tablebase (mismo default que ``logic.tb_applicable``): por
# debajo el coste de resolver no es una busqueda, es una consulta.
TB_MEN = 6
# El pico de la tienda: "final pelado" por encima del horizonte de consulta.
BARE_MEN = 12
# Posicion completa: 32 piezas.
FULL_MEN = 32

# Bandas de eval, en centipeones ABSOLUTOS.  Mismos cortes que
# ``proof.EVAL_BANDS`` — se replican y no se importan para que este modulo no
# dependa del gestor de prueba (la dependencia va en el otro sentido).
MATE_BAND = 9_000
WON_BAND = 800
EDGE_BAND = 300

# Tope de plies de PV que se recorren.  ``ingest.STORED_PV_MAX_PLIES`` ya deja
# 24 en las lineas no-mate, pero una linea de mate se guarda ENTERA y este
# recorrido tiene que seguir costando lo mismo en las dos.
PV_SCAN_MAX_PLIES = 32


# Vista minima de una posicion para el estimador.  La usan los sitios que solo
# tienen (fen, eval_cp) — las aristas materializadas del padre, sin el JSON del
# hijo — y que por tanto ven una version DEGRADADA del estimador: sin PV y sin
# ancho, dos de las cuatro features caen a NEUTRAL.  Es un compromiso
# deliberado: traerse ``last_analysis`` de cada hijo para puntuarlo seria
# arrastrar el campo mas grande de la tabla por cada arista del nivel.
Leaf = namedtuple('Leaf', ('fen', 'eval_cp', 'last_analysis', 'mate_in'),
                  defaults=(None, None))


def shallow(fen, eval_cp):
    """Vista degradada para un nodo del que solo se conoce fen y eval."""
    return Leaf(fen, eval_cp)


def clamp01(value):
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else float(value))


def _stored(row, name):
    """Campo de la fila SIN provocar una consulta.

    ``_refresh_campaign`` lee las posiciones con ``.only(...)``, y en Django
    tocar un campo diferido dispara un SELECT por FILA.  Un estimador que
    hiciera eso convertiria una pasada de mantenimiento en una tormenta de
    consultas sin que nadie lo viera venir.  Aqui un campo diferido se trata
    como un campo ausente, que es exactamente lo que es para este calculo.
    """
    deferred = getattr(row, 'get_deferred_fields', None)
    if deferred is not None and name in deferred():
        return None
    return getattr(row, name, None)


def current_line(row):
    """La linea VIGENTE de ``last_analysis``, o ``None``.

    Se salta las lineas marcadas ``prior_pass``: son el escaparate ancho de un
    pase anterior que ``ingest`` conserva a proposito, no el veredicto de
    ahora.  Mismo criterio que ``ingest.claimed_mate_plies``.
    """
    for line in (_stored(row, 'last_analysis') or []):
        if not isinstance(line, dict) or line.get('prior_pass'):
            continue
        return line
    return None


def current_lines(row):
    """Cuantas lineas VIGENTES trajo el ultimo analisis (0 si no hay)."""
    return sum(1 for line in (_stored(row, 'last_analysis') or [])
               if isinstance(line, dict) and not line.get('prior_pass'))


# ---------------- (a) densidad de plies reversibles ----------------
#
# PROXY, y se llama proxy.  Lo exacto seria preguntarle al movegen fijado
# (``logic.is_zeroing``) ply a ply, y eso son 24 generaciones de movimientos
# por HOJA: en una pasada de mantenimiento con miles de hojas deja de ser un
# estimador barato.  Lo que hace esto es leer el tablero UNA vez y arrastrarlo
# por la PV con dos reglas puramente sintacticas:
#
#   * la jugada es de peon si su casilla de origen tiene un peon en el mapa;
#   * es captura si su casilla de destino esta ocupada en el mapa.
#
# Las dos son EXACTAS en el primer ply (el mapa es la FEN de verdad) y se
# degradan con la profundidad, porque el mapa se actualiza sin la explosion
# atomica: una captura vuela al capturado y al capturador — eso si se
# reproduce, cuesta una linea — pero no la vecindad, que es donde empieza a
# reimplementarse la regla.  El sesgo resultante esta acotado y es del lado
# conservador: el mapa conserva piezas que ya volaron, asi que el proxy
# SOBREESTIMA capturas, es decir INFRAESTIMA la molestia.  Al paso y enroque
# entran por el mismo agujero (el enroque se lee como captura de la torre
# propia); son raros y no mueven la densidad de una PV de 24 plies.
#
# Que NO se hace: contar piezas del FEN resultante, ni reimplementar la
# explosion.  Esa seria una segunda implementacion de las reglas con sus
# propios bordes, y este arbol ya tiene una y solo quiere una.


def _board_map(fen):
    """``{casilla: pieza}`` del campo de piezas, en una sola pasada."""
    board = {}
    rows = fen.split()[0].split('/')
    for index, row in enumerate(rows[:8]):
        rank = 8 - index
        column = 0
        for character in row:
            if character.isdigit():
                column += int(character)
            elif column < 8:
                board[f'{chr(ord("a") + column)}{rank}'] = character
                column += 1
            else:
                break
    return board


def zeroing_plies(fen, pv, max_plies=PV_SCAN_MAX_PLIES):
    """``(plies_leidos, plies_que_resetean)`` del proxy documentado arriba."""
    board = _board_map(fen)
    read = zeroing = 0
    for uci in (pv or [])[:max_plies]:
        if not isinstance(uci, str) or len(uci) < 4:
            break
        origin, dest = uci[:2], uci[2:4]
        piece = board.get(origin)
        captured = board.get(dest)
        read += 1
        if len(uci) > 4 or captured is not None or (piece or '') in ('P', 'p'):
            zeroing += 1
        board.pop(origin, None)
        if captured is not None:
            board.pop(dest, None)     # atomic: vuelan capturado Y capturador
        elif piece is not None:
            board[dest] = piece
    return read, zeroing


def reversible_feature(row):
    """Fraccion de la PV vigente que NO resetea el contador de 50."""
    line = current_line(row)
    pv = line.get('pv') if isinstance(line, dict) else None
    fen = getattr(row, 'fen', None)
    if not pv or not fen:
        return NEUTRAL
    read, zeroing = zeroing_plies(fen, pv)
    if not read:
        return NEUTRAL
    return clamp01(1.0 - zeroing / read)


# ---------------- (b) ancho ----------------

def branching_feature(row, branching=None):
    """Molestia por ancho.  ``branching`` es el numero de aristas si se sabe.

    Sin ese numero queda el ancho del MultiPV vigente, que es un proxy TUERTO
    y se usa como tal: N lineas DEMUESTRAN que hay al menos N jugadas legales,
    asi que pueden empujar la feature hacia arriba; pero pocas lineas no
    demuestran nada — el pase pudo pedir MultiPV 1 — asi que nunca la empujan
    hacia abajo.  Sin esta asimetria, un pase profundo de una sola linea haria
    parecer facil cualquier posicion que tocara.
    """
    if branching is not None:
        return clamp01(int(branching) / BRANCHING_SATURATION)
    lines = current_lines(row)
    if not lines:
        return NEUTRAL
    return max(NEUTRAL, clamp01(lines / MULTIPV_SATURATION))


# ---------------- (c) material ----------------

def material_feature(row):
    """Tienda con el pico en ``BARE_MEN``; cero bajo el horizonte TB."""
    fen = getattr(row, 'fen', None)
    if not fen:
        return NEUTRAL
    men = logic.piece_count(fen)
    if men <= TB_MEN:
        return 0.0
    if men <= BARE_MEN:
        return clamp01((men - TB_MEN) / float(BARE_MEN - TB_MEN))
    return clamp01((FULL_MEN - men) / float(FULL_MEN - BARE_MEN))


# ---------------- (d) banda de eval ----------------

def eval_band_feature(row):
    """Banda ordinal del eval ABSOLUTO; mate visto es la via rapida."""
    if _stored(row, 'mate_in') is not None:
        return 0.0
    line = current_line(row)
    if isinstance(line, dict) and line.get('mate'):
        return 0.0
    eval_cp = _stored(row, 'eval_cp')
    if eval_cp is None:
        return NEUTRAL
    score = abs(int(eval_cp))
    if score >= MATE_BAND:
        return 0.0
    if score >= WON_BAND:
        return 0.2
    if score >= EDGE_BAND:
        return 0.5
    return 1.0


# ---------------- la combinacion ----------------

def features(row, branching=None):
    """Las cuatro features en ``[0, 1]``, sin pesar.  Util para depurar."""
    return {
        'reversible': reversible_feature(row),
        'branching': branching_feature(row, branching),
        'material': material_feature(row),
        'eval_band': eval_band_feature(row),
    }


FEATURE_WEIGHTS = {
    'reversible': REVERSIBLE_WEIGHT,
    'branching': BRANCHING_WEIGHT,
    'material': MATERIAL_WEIGHT,
    'eval_band': EVAL_BAND_WEIGHT,
}


def annoyance(pos, branching=None):
    """Coste estimado de RESOLVER ``pos``, en ``[0, 1]``.

    0 = prometedor de resolver, 1 = pesadilla tediosa.  Funcion pura: mismos
    datos, mismo numero, sin consultas ni motor.

    ``branching`` es el numero de aristas del nodo cuando el llamante ya lo
    tiene (el volcado del dataset lo cuenta por lotes, una sentencia por
    lote).  Contarlo aqui costaria una consulta por posicion, que es
    exactamente lo que este modulo no puede permitirse.
    """
    values = features(pos, branching)
    return clamp01(sum(FEATURE_WEIGHTS[name] * value
                       for name, value in values.items()))


# ---------------- la puerta ----------------

# Techo del encarecimiento.  La puerta es BLANDA a proposito: multiplica el pn
# de la hoja por un factor de 1 a K, nunca la veta.  Un veto seria una
# afirmacion sobre el arbol ("por aqui no se gana") que este estimador no esta
# en posicion de hacer; un factor es una afirmacion sobre el PRESUPUESTO ("por
# aqui sale caro"), que es justo lo que pn significa.  Con K=8 la hoja mas
# tediosa cuesta lo que ocho hojas limpias: se explora igual si no hay nada
# mejor, y se queda la ultima mientras lo haya.
MAX_FACTOR = 8


def gate_factor(value):
    """Factor de ``pn``: 1.0 sin molestia, ``MAX_FACTOR`` con toda.

    Lineal y sin sorpresas — la constante es la perilla, no la forma de la
    curva.  Lo que discrimina no es el valor absoluto (un factor comun a todas
    las hojas seria un cambio de unidades y el descenso no lo notaria) sino el
    RANGO entre hojas, mas la exencion de la banda de mate.
    """
    return 1.0 + (MAX_FACTOR - 1.0) * clamp01(value)
