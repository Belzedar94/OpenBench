"""Nucleo exacto de AtomicDB: identidad canonica, expansion con movegen
propio (pyffish, nunca el motor), verificacion de PVs de mate y backup
minimax de tres valores. Todo lo exacto pasa por aqui; el motor solo
aporta evals y candidatos.

QUE TEOREMA SE ESTA PROBANDO (ruleset ``atomic-fide-claim-v1``)
--------------------------------------------------------------
"Reglas estandar" no es un enunciado exacto, y sin enunciado exacto no hay
teorema.  AtomicDB prueba bajo esta lectura, congelada y deliberadamente
PESIMISTA para el bando ganador:

* **El defensor SIEMPRE puede reclamar tablas al alcanzar 50 jugadas sin
  captura ni movimiento de peon** (100 plies del contador).  Se asume la
  reclamacion disponible siempre que la regla la permita, sin modelar si el
  jugador la ejerce.  Probar bajo este supuesto cubre a fortiori la variante
  de 75 jugadas automatica y la de 50 reclamable: si el ganador gana contra
  un defensor que puede reclamar en cuanto puede, gana tambien contra
  cualquier defensor mas indulgente.
* **Mate y explosion tienen precedencia** sobre la adjudicacion por contador:
  una posicion terminal es terminal, se llegue con el contador que se llegue.
* La repeticion se trata POR RAMA en las pruebas exhaustivas (ver
  ``prove_forced_mate``): una posicion repetida dentro de la rama es una rama
  fallida para el atacante.  Ese es el tratamiento correcto del problema GHI
  DENTRO de una invocacion; la reutilizacion de un hecho probado a traves de
  aristas reversibles tiene un residuo documentado en ``ingest`` (regla
  fresh-context).
* Semantica atomica del rey, jaque, enroque y captura al paso: la del movegen
  fijado (``pyffish``, variante ``atomic``), cuya identidad se audita aparte
  (``audit_en_passant``).

RELOJ Y ``clock_slack``
----------------------
``canonical_fen`` pone los contadores a cero, asi que cada nodo del DAG
representa "esta posicion con el contador de 50 a cero".  Un cierre decisivo
probado desde reloj cero NO vale automaticamente cuando se llega a la misma
posicion con reloj c > 0: si la prueba contiene una racha de plies
reversibles, el defensor puede llegar a reclamar antes del mate.
``clock_slack`` es el reloj de entrada MAXIMO para el que la prueba sigue
valiendo, en plies del contador de 50 (0..100).  Las tablas no llevan slack:
subir el reloj solo degrada victorias hacia tablas, nunca al reves.
"""

import hashlib
import time
from functools import lru_cache

import pyffish as pf

VARIANT = 'atomic'

# CUANTO CUESTA UNA PREGUNTA AL MOVEGEN, Y POR QUE SE RECUERDAN LAS RESPUESTAS.
#
# Medido: una llamada a ``pf.get_fen`` cuesta lo mismo con una jugada que con
# veinte (~3,5 ms en el servidor, ~16 ms en el portatil de desarrollo), y lo
# mismo cuesta ``legal_moves`` o ``game_result``.  El trabajo real es
# despreciable; lo que se paga es MONTAR LA POSICION una vez por llamada,
# porque la variante es argumento de TODA llamada de pyffish y construirla
# rehace la tabla de ataques entera (``UCI::init_variant``; el mismo hallazgo
# que motivo el verificador nativo, § test_survive_native).  Asi que el coste
# de cualquier cosa que use este modulo no se mide en plies ni en jugadas: se
# mide en NUMERO DE LLAMADAS.
#
# ``apply_move`` y el sondeo al paso son funciones PURAS de sus argumentos —
# el movegen esta fijado y nada en el proyecto llama a ``pf.set_option`` ni a
# ``pf.load_variant_config``, asi que la misma entrada da la misma salida
# durante toda la vida del proceso.  Recordarlas no cambia ninguna respuesta;
# solo deja de pagar la misma pregunta dos veces.  Quien mas lo nota es el
# explorador, que rejuega el mismo prefijo de apertura en cada click.
#
# El tope existe por memoria, no por correccion: una entrada son unos 350
# bytes, asi que 16384 son ~6 MB por proceso — nada al lado de los cinco
# workers de gunicorn, y de sobra para los prefijos que una sesion de
# navegacion repite.  Un fallo de cache cuesta un diccionario de mas sobre una
# llamada de milisegundos, asi que el ingestor y el solver, que ven posiciones
# nuevas casi siempre, no pagan nada medible por esto.
MOVE_CACHE_ENTRIES = 16384
# The same argument, measured on the ingest (18-nov): with the fleet doubled,
# 85% of an ingest process was ``prove_forced_mate``, and inside it every
# second went to asking pyffish the same few questions again — the legal
# moves and the immediate-end flag of a node reached by another move order,
# the terminal verdict of a leaf already scored, and ``is_zeroing`` replaying
# the very ``get_fen`` that ``apply_move`` had just paid for.  All of them are
# pure functions of their string arguments (same reasoning as above), so they
# are remembered too.  This is NOT a transposition table: no search RESULT is
# ever reused, the proof visits exactly the same nodes in exactly the same
# order and counts them against the budget exactly as before — only the
# movegen stops being asked twice.  A position entry (a tuple of ~30 short
# strings) is a couple of KB, so 32768 of them are ~60 MB per process.
POSITION_CACHE_ENTRIES = 32768

# Identidad congelada del enunciado que se prueba.  Cambiarla es cambiar el
# teorema, no un detalle de implementacion: todo cierre decisivo que exista
# quedaria referido a otro conjunto de reglas.
RULESET_ID = 'atomic-fide-claim-v1'
# Version del CANONICALIZADOR: como se convierte una posicion en una clave.
# Es independiente del ruleset (las reglas del juego no cambian) pero mueve
# claves, asi que toda migracion de re-clavado se referencia contra ella.
#
#   v1  piezas + turno + enroques + campo e.p. TAL CUAL lo emite el movegen.
#   v2  piezas + turno + enroques + campo e.p. SOLO SI la captura al paso es
#       realmente ejecutable.  El movegen atomic emite la casilla tambien
#       cuando la captura es pseudo-legal pero ilegal — porque explotaria al
#       propio rey, o porque el peon capturador esta clavado — y esas dos
#       posiciones son la MISMA a efectos de juego.  Ver ``canonical_fen``.
CANONICAL_VERSION = 2
# Plies del contador de 50 tras los cuales el defensor puede reclamar tablas.
FIFTY_MOVE_PLIES = 100
CLOCK_SLACK_MAX = 100


# ---------- identidad (§3.5) ----------

def en_passant_changes_the_game(parts):
    """True si el derecho al paso declarado altera realmente las jugadas.

    La pregunta se le hace al movegen fijado, no a una reimplementacion de la
    regla: se generan las jugadas legales con el campo e.p. y sin el, y si el
    conjunto es IDENTICO el derecho no existe.  Quitar un derecho solo puede
    quitar jugadas, asi que la comparacion es exactamente "aporta la captura
    al paso o no".

    En atomic hay dos formas de que el campo mienta y las dos estan
    verificadas contra el movegen: la captura al paso explotaria al propio rey
    (la explosion cubre el 3x3 alrededor de la casilla de destino), y el peon
    capturador esta clavado.  Cuando no hay ningun peon enemigo al lado, el
    movegen ya elimina el campo por su cuenta.

    Casos degenerados, medidos contra el movegen real y no supuestos: si la
    posicion no tiene NINGUNA jugada legal (mate, ahogado, rey explotado) los
    dos conjuntos salen vacios y el derecho se elimina — correcto, porque un
    derecho que no se puede ejercer no distingue nada, y ``pf.get_fen`` ya lo
    elimina por su cuenta en esas posiciones.  Una FEN que el movegen no sabe
    leer la reinterpreta en vez de rechazarla, asi que tampoco hay nada que
    conservar; ninguna de las dos cosas llega a la base, donde todas las FEN
    salen del propio movegen.

    Son DOS generaciones de movimientos, y ``canonical_fen`` es idempotente —
    ``key_of(apply_move(...))`` vuelve a canonicalizar una FEN que ya lo
    estaba — asi que la misma pregunta se repite constantemente.  Se recuerda
    (§ MOVE_CACHE_ENTRIES); la respuesta solo depende de los cuatro primeros
    campos de la FEN.
    """
    return _en_passant_right_exists(' '.join(parts[:4]))


@lru_cache(maxsize=MOVE_CACHE_ENTRIES)
def _en_passant_right_exists(prefix):
    # Both questions go through the position memo (``_legal_moves``), not
    # straight to pyffish.  The two FENs asked here are exactly the two
    # candidates for the canonical FEN — counters ``0 1``, with and without
    # the square — so whichever one ``canonical_fen`` keeps is the very next
    # thing every caller asks about (``terminal_status`` of a child born in
    # ``expand``, the next node of a proof), and it is then already
    # answered.  Same strings, same pyffish call, same answer: only the
    # second asking disappears.  An exception still propagates out of
    # ``_legal_moves`` (``lru_cache`` remembers nothing for a raise), so the
    # fallback below is unchanged.
    with_right = prefix + ' 0 1'
    without_right = ' '.join(prefix.split()[:3] + ['-', '0', '1'])
    try:
        return (set(_legal_moves(with_right))
                != set(_legal_moves(without_right)))
    except Exception:
        return True


def canonical_fen(fen):
    """Piezas+turno+enroques+ep EJECUTABLE, contadores a 0 1.

    El campo e.p. entra en la clave, asi que un derecho FANTASMA — declarado
    pero inejecutable — parte en dos la identidad de una posicion.  El coste
    tiene dos tamanos muy distintos: se pierden transposiciones (molesto), y
    el guard anti-repeticion de ``prove_forced_mate`` compara claves de rama,
    de modo que una repeticion REAL puede pasar desapercibida y una linea que
    en realidad son tablas certificarse como ganada (fallo de solidez).

    Por eso v2 del canonicalizador conserva la casilla solo cuando ejecutarla
    es legal.  La comprobacion cuesta dos generaciones de movimientos y solo
    corre en posiciones que declaran e.p. — el 0,8% del arbol.
    """
    parts = fen.split()
    if (len(parts) >= 4 and parts[3] != '-'
            and not en_passant_changes_the_game(parts)):
        parts = list(parts[:3]) + ['-']
    return ' '.join(parts[:4] + ['0', '1'])


def key_of(fen):
    return hashlib.sha256(canonical_fen(fen).encode()).hexdigest()


def start_fen():
    return canonical_fen(pf.start_fen(VARIANT))


# ---------- reglas (movegen del ingestor) ----------

@lru_cache(maxsize=POSITION_CACHE_ENTRIES)
def _legal_moves(fen):
    return tuple(pf.legal_moves(VARIANT, fen, []))


# THE IMMEDIATE-END FLAG WITH BOTH KINGS ON THE BOARD IS ALWAYS FALSE.
#
# The 23-nov ingest profile (8 workers, 5,338 samples) put 26% of all the CPU
# in ``pf.is_immediate_game_end`` — one full pyffish call, attack tables and
# all, per node of every proof and per child of every expansion — and the
# answer is fixed by the board for every position this module ever asks
# about.  ``Position::is_immediate_game_end`` in the pinned movegen, for the
# ``atomic`` variant and an empty move list, can only fire on:
#
# * EXTINCTION.  Atomic declares ``extinctionPieceTypes = {COMMONER}`` (the
#   king is a commoner) with the default counts, so it fires exactly when one
#   side has no king left: the king that was blown up.
# * Everything else in that function is off for atomic (capture-the-flag,
#   castling wins, n-check, connect-n, full-board adjudication, two boards,
#   tsume mode — ``pf.set_option`` is never called), or needs a move history
#   (bikjang / double pass look at ``st->previous`` only when
#   ``pliesFromNull > 0``; every call here passes ``[]``).
#
# So with a ``K`` and a ``k`` in the board field the flag is false, and that
# is all this shortcut answers.  A position missing a king still asks
# pyffish, as before: that is the only case where the flag can be true, and
# it is a terminal leaf (rare, and ``game_result`` is asked there anyway).
# The equivalence is pinned by ``MovegenShortcutTests`` against the real
# pyffish over positions from the live tree and every child of each.
def _both_kings_on_board(fen):
    board = fen.split(' ', 1)[0]
    return 'K' in board and 'k' in board


def _immediate_end(fen):
    if _both_kings_on_board(fen):
        return False
    return _immediate_end_movegen(fen)


@lru_cache(maxsize=POSITION_CACHE_ENTRIES)
def _immediate_end_movegen(fen):
    immediate, _ = pf.is_immediate_game_end(VARIANT, fen, [])
    return bool(immediate)


@lru_cache(maxsize=POSITION_CACHE_ENTRIES)
def _game_result(fen):
    return pf.game_result(VARIANT, fen, [])


def legal_moves(fen):
    # A fresh list every time: callers reorder it (the hint in
    # ``prove_forced_mate``), and the remembered answer must not move.
    return list(_legal_moves(fen))


@lru_cache(maxsize=4 * MOVE_CACHE_ENTRIES)
def _fen_after(fen, uci):
    """The movegen's FEN after one move, counters included.

    ``apply_move`` keeps the position and ``is_zeroing`` keeps the counter;
    both used to ask this same question separately.
    """
    return pf.get_fen(VARIANT, fen, [uci])


@lru_cache(maxsize=MOVE_CACHE_ENTRIES)
def apply_move(fen, uci):
    return canonical_fen(_fen_after(fen, uci))


def is_zeroing(fen, uci):
    """True si la jugada RESETEA el contador de 50 (captura o peon).

    No se reimplementa la regla: se le pregunta al movegen fijado.  Sobre una
    FEN canonica (contadores ``0 1``) el contador resultante solo puede ser 0
    — la jugada reseteo — o 1.  En atomic una captura ademas explota, asi que
    contar piezas seria una segunda implementacion de la regla con sus propios
    bordes; esto no lo es.
    """
    parts = _fen_after(canonical_fen(fen), uci).split()
    return len(parts) > 4 and parts[4] == '0'


def clear_caches():
    """Forget every remembered movegen answer, as a restarted process would."""
    for remembered in (_legal_moves, _immediate_end_movegen, _game_result,
                       _fen_after, apply_move, _en_passant_right_exists):
        remembered.cache_clear()


def terminal_status(fen):
    """Si la posicion es terminal, devuelve (status, razon); si no, None.
    status en {'WHITE_WIN','BLACK_WIN','DRAW'}."""
    # A position with no legal move is terminal whatever the immediate-end
    # flag says, so the flag is only asked when there are moves.
    if _legal_moves(fen) and not _immediate_end(fen):
        return None
    # game_result: valor desde la perspectiva del que mueve
    res = _game_result(fen)
    stm_white = fen.split()[1] == 'w'
    if res > 0:
        return ('WHITE_WIN' if stm_white else 'BLACK_WIN', 'terminal')
    if res < 0:
        return ('BLACK_WIN' if stm_white else 'WHITE_WIN', 'terminal')
    return ('DRAW', 'terminal')


# ---------- auditoria de identidad: casilla e.p. fantasma ----------
#
# POR QUE IMPORTA.  ``canonical_fen`` mete la casilla e.p. en la clave.  Si el
# movegen la emite tambien cuando NINGUNA captura al paso es legal, dos
# posiciones que son la MISMA a efectos de juego reciben claves distintas.  Las
# consecuencias son de dos tamanos muy distintos:
#
#   * transposicion perdida: dos claves donde deberia haber una.  Molesto,
#     barato, reversible.
#   * el guard anti-repeticion de ``prove_forced_mate`` compara claves de rama.
#     Con dos claves para la misma posicion, una repeticion REAL puede pasar
#     desapercibida y una linea que en realidad son tablas por repeticion se
#     certifica como ganada.  Eso si es un fallo de solidez.
#
# Esto AUDITA Y REPORTA. No toca ``canonical_fen`` ni re-clava nada: cambiar la
# identidad es una migracion mayor (todas las claves del arbol se mueven) que
# decide el propietario con el informe delante.

def en_passant_square(fen):
    parts = fen.split()
    if len(parts) < 4 or parts[3] == '-':
        return None
    return parts[3]


def _piece_at(fen, square):
    """Pieza en la casilla (notacion FEN), o ``None``."""
    if len(square) != 2:
        return None
    file_index = ord(square[0]) - ord('a')
    rank_index = 8 - int(square[1])
    if not (0 <= file_index < 8 and 0 <= rank_index < 8):
        return None
    rows = fen.split()[0].split('/')
    if rank_index >= len(rows):
        return None
    column = 0
    for character in rows[rank_index]:
        if character.isdigit():
            column += int(character)
        else:
            if column == file_index:
                return character
            column += 1
        if column > file_index:
            return None
    return None


def legal_en_passant_moves(fen):
    """Jugadas legales que son REALMENTE una captura al paso."""
    target = en_passant_square(fen)
    if target is None:
        return []
    stm_white = fen.split()[1] == 'w'
    pawn = 'P' if stm_white else 'p'
    found = []
    for uci in legal_moves(fen):
        if len(uci) < 4 or uci[2:4] != target:
            continue
        if _piece_at(fen, uci[0:2]) == pawn and uci[0] != uci[2]:
            found.append(uci)
    return found


def audit_en_passant(fen):
    """``None`` si la posicion no declara e.p.; si no, el veredicto.

    Devuelve ``{'square', 'legal_moves', 'phantom'}``.  ``phantom`` es True
    cuando la FEN declara una casilla al paso que ninguna jugada legal puede
    usar: ahi la clave distingue posiciones que el juego no distingue.
    """
    square = en_passant_square(fen)
    if square is None:
        return None
    moves = legal_en_passant_moves(fen)
    return {'square': square, 'legal_moves': moves, 'phantom': not moves}


# ---------- verificacion de PV de mate (§3.2) ----------

def verify_mate_pv(root_fen, pv_ucis, winner_is_white, deadline=None):
    """True si la PV es legal jugada a jugada, sin repetir posiciones,
    y acaba en terminal ganado por `winner`. Rechaza todo lo demas."""
    fen = canonical_fen(root_fen)
    seen = {fen}
    for uci in pv_ucis:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if terminal_status(fen) is not None:
            return False                      # terminal antes de agotar la PV
        if uci not in legal_moves(fen):
            return False
        fen = apply_move(fen, uci)
        if fen in seen:
            return False                      # repeticion interna
        seen.add(fen)
    if deadline is not None and time.monotonic() >= deadline:
        return False
    t = terminal_status(fen)
    if t is None:
        return False
    want = 'WHITE_WIN' if winner_is_white else 'BLACK_WIN'
    return t[0] == want


def walk_pv(root_fen, pv_ucis, deadline=None):
    """The positions of a PV, ``[root, after 1 ply, ...]``, or None.

    The same checks as ``verify_mate_pv`` up to its last ply: every move is
    legal, no position repeats and no position before the end is terminal.
    It says nothing about the end itself; the caller judges that.
    """
    fen = canonical_fen(root_fen)
    fens = [fen]
    seen = {fen}
    for uci in pv_ucis:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if terminal_status(fen) is not None:
            return None
        if uci not in legal_moves(fen):
            return None
        fen = apply_move(fen, uci)
        if fen in seen:
            return None
        seen.add(fen)
        fens.append(fen)
    return fens


def line_clock_slack(fens, pv_ucis, end_slack):
    """``clock_slack`` a line keeps at EVERY node, from the slack of its end.

    Walks back from the end with ``edge_slack``: a capture or pawn move
    resets the clock, a quiet move spends one ply.  Returns the smallest
    slack of the nodes before the end, so any of them may inherit the
    value (the end keeps its own), or None for an empty line or
    when a quiet move would need a negative margin: the line does not hold
    for any entry clock and must not close anything.
    """
    slack = end_slack
    smallest = None
    for index in range(len(pv_ucis) - 1, -1, -1):
        fen, uci = fens[index], pv_ucis[index]
        if not edge_supports(fen, uci, slack):
            return None
        slack = edge_slack(fen, uci, slack)
        smallest = slack if smallest is None else min(smallest, slack)
    return smallest


def prove_forced_mate(fen, winner_is_white, max_plies,
                      budget_positions=200_000, hint_pv=None,
                      deadline=None, return_run=False):
    """Prove a bounded forced mate with an exhaustive AND/OR search.

    With ``return_run`` the result becomes ``(verdict, worst_reversible_run)``.
    That run is the longest stretch of consecutive plies WITHOUT a capture or
    pawn move anywhere in the STRATEGY TREE that was actually proven — not in
    the engine's PV, and not in the branches the search merely visited.  It is
    what turns a proof from clock zero into a proof for a range of entry
    clocks: with a run of R, a defender entering at clock c reaches c + R at
    worst, so the closure survives every c up to ``CLOCK_SLACK_MAX - R``.

    The aggregation matches who chooses: at the winner's OR nodes the winner
    picks the continuation with the SMALLEST worst run (it is a free choice),
    at the defender's AND nodes the defender picks the LARGEST (it must hold
    against every reply).  A branch that ends here contributes the run that
    ends here, which is why every node takes ``max(run, ...)``: a run that
    finishes because the next move zeroes still happened.

    The result is one of ``PROVEN``, ``INCONCLUSIVE`` or ``NO_MATE``:

    * on the winner's turn, one proven continuation is sufficient;
    * on the defender's turn, every legal continuation must be proven;
    * a repeated position on the current branch is a failed branch;
    * a non-terminal leaf at ``max_plies`` is a failed branch; and
    * exhausting ``budget_positions`` or reaching the optional absolute
      monotonic ``deadline`` makes the whole attempt inconclusive.

    ``hint_pv`` affects move ordering only.  It never changes the set of
    moves searched or the result of a search that fits in the budget.

    Positions are counted on entry, including the root.  Repeated positions
    are rejected before entry and therefore do not consume the budget.
    Deliberately avoid a transposition cache here: whether a continuation
    repeats depends on the complete branch history, which is not part of
    AtomicDB's canonical FEN.
    """
    max_plies = int(max_plies)
    budget_positions = int(budget_positions)
    if max_plies < 0:
        raise ValueError('max_plies must be non-negative')
    if budget_positions < 0:
        raise ValueError('budget_positions must be non-negative')

    root = canonical_fen(fen)
    winner_status = 'WHITE_WIN' if winner_is_white else 'BLACK_WIN'
    hint = tuple(hint_pv or ())
    visited = 0

    class _BudgetExhausted(Exception):
        pass

    def search(node_fen, plies_left, path, remaining_hint, run):
        """Return ``(proved, worst_run)``; ``worst_run`` only means anything
        when ``proved`` is true."""
        nonlocal visited
        if (visited >= budget_positions
                or (deadline is not None and time.monotonic() >= deadline)):
            raise _BudgetExhausted
        visited += 1

        # The same questions terminal_status() asks, in the same order, so
        # on a terminal node it answers them from memory and pays only for
        # game_result.  With no legal move the node is terminal whatever the
        # immediate-end flag says, so the flag is only asked when there are
        # moves.
        moves = legal_moves(node_fen)
        if not moves or _immediate_end(node_fen):
            terminal = terminal_status(node_fen)
            won = terminal is not None and terminal[0] == winner_status
            return won, run
        if plies_left == 0:
            return False, run
        hinted = remaining_hint[0] if remaining_hint else None
        if hinted in moves:
            moves.remove(hinted)
            moves.insert(0, hinted)

        winner_to_move = ((node_fen.split()[1] == 'w')
                          == bool(winner_is_white))
        if winner_to_move:
            for move in moves:
                child = apply_move(node_fen, move)
                if child in path:
                    continue
                child_hint = (remaining_hint[1:]
                              if move == hinted else ())
                child_run = 0 if (track_run and is_zeroing(node_fen, move)) \
                    else run + 1
                proved, worst = search(child, plies_left - 1, path | {child},
                                       child_hint, child_run)
                # First success wins, exactly as before instrumenting the
                # run.  Minimising over every winning alternative would turn
                # this OR node from "stop at the first proof" into "search
                # them all", which is the difference between a 200k-position
                # online budget succeeding and timing out.  The price is a
                # slack measured on the strategy we FOUND rather than the
                # best one available: conservative, never wrong.
                if proved:
                    return True, max(run, worst)
            return False, run

        # Defender node: a single failed reply refutes the bounded proof.
        worst_here = run
        for move in moves:
            child = apply_move(node_fen, move)
            if child in path:
                return False, run
            child_hint = remaining_hint[1:] if move == hinted else ()
            child_run = 0 if (track_run and is_zeroing(node_fen, move)) \
                else run + 1
            proved, worst = search(child, plies_left - 1, path | {child},
                                   child_hint, child_run)
            if not proved:
                return False, run
            if track_run:
                worst_here = max(worst_here, worst)
        return True, worst_here

    track_run = bool(return_run)
    try:
        proved, worst = search(root, max_plies, {root}, hint, 0)
        verdict = 'PROVEN' if proved else 'NO_MATE'
        worst = worst if proved else None
    except _BudgetExhausted:
        verdict, worst = 'INCONCLUSIVE', None
    return (verdict, worst) if return_run else verdict


def slack_from_run(worst_reversible_run):
    """``clock_slack`` de una prueba exhaustiva con esa racha reversible.

    Con una racha maxima de R plies sin captura ni peon, un defensor que entra
    con el contador en c lo lleva a c + R en el peor caso, asi que la prueba
    vale para todo c <= CLOCK_SLACK_MAX - R.  ``None`` significa "no medida"
    (una prueba que no siguio la racha), no "sin limite".
    """
    if worst_reversible_run is None:
        return None
    return _clamp_slack(CLOCK_SLACK_MAX - int(worst_reversible_run))


def _clamp_slack(value):
    return max(0, min(CLOCK_SLACK_MAX, int(value)))


def slack_from_witness_length(plies):
    """Cota BURDA para un testigo sin prueba exhaustiva (cierre ENGINE).

    Un testigo legal de N plies no dice donde estan sus capturas ni que hace
    el defensor fuera de la linea, asi que lo unico defendible es suponer que
    los N plies fueron todos reversibles.  Se recalcula al certificar.
    """
    return _clamp_slack(CLOCK_SLACK_MAX - max(0, int(plies)))


def slack_from_dtz(dtz):
    """``clock_slack`` de un cierre TB a partir del DTZ, o 0 sin DTZ.

    El margen de +1 es deliberado: el DTZ de Syzygy tiene semantica de
    redondeo y puede quedarse a un ply del borde de la regla de 50.  Sin DTZ
    el probe solo dice WDL, que por construccion asume haberse alcanzado justo
    tras un reset — asi que lo unico honesto es slack 0: vale para la
    instancia con el reloj a cero y para ninguna otra.
    """
    if dtz is None:
        return 0
    return _clamp_slack(CLOCK_SLACK_MAX - (abs(int(dtz)) + 1))


def edge_slack(fen, uci, child_slack):
    """Slack que un hijo APORTA al padre a traves de esta arista.

    Una arista que RESETEA el contador (captura o peon) hace irrelevante el
    reloj de entrada del padre: despues de ella el hijo empieza de cero, que
    es exactamente el escenario para el que su cierre fue probado.
    Una arista TRANQUILA gasta un ply: el hijo se alcanza con el reloj del
    padre mas uno, asi que solo cubre hasta ``child_slack - 1``.  Un hijo sin
    slack medido se trata como slack 0 (conservador).
    """
    if is_zeroing(fen, uci):
        return CLOCK_SLACK_MAX
    if child_slack is None:
        return 0
    return _clamp_slack(min(int(child_slack) - 1, CLOCK_SLACK_MAX))


def edge_supports(fen, uci, child_slack):
    """Regla FRESH-CONTEXT (v1 pragmatica): puede este hijo sostener al padre.

    Tras una arista IRREVERSIBLE (captura o peon) el hijo se alcanza con el
    contador a cero y sin ninguna posicion anterior relevante para la
    repeticion: su hecho es reutilizable "en contexto fresco", que es
    exactamente el contexto para el que se probo.  A traves de una arista
    TRANQUILA el hijo se alcanza con un ply mas de reloj y arrastrando el
    sufijo reversible de entrada, asi que solo sostiene al padre si le queda
    al menos un ply de margen; el padre hereda ``slack - 1``.

    RESIDUO CONOCIDO Y ACEPTADO.  Esto trata el reloj de 50, no la
    REPETICION.  Un camino entrante que ya haya visitado dos veces una
    posicion interior del arbol de prueba puede convertir en tablas una linea
    que la prueba dio por ganada, aunque el contador de 50 cuadre.  Es el
    problema de graph-history interaction: la cura establecida
    (Kishimoto-Muller) es validar el hecho cacheado contra el sufijo
    reversible de entrada cuando se llega por otro camino, en vez de
    reutilizarlo a ciegas o duplicar cada historia.  En atomic, denso en
    capturas irreversibles, los casos condicionales seran minoria; la red de
    seguridad definitiva es el verificador de certificados de P1c, que
    reproduce la estrategia entera con historia completa.  Hasta entonces,
    esto es un riesgo declarado, no un descuido.
    """
    if is_zeroing(fen, uci):
        return True
    return child_slack is not None and int(child_slack) >= 1


def minimax_slack(fen, winning_edges, all_edges, mover_wins):
    """Recurrencia de slack en un cierre MINIMAX.

    ``winning_edges`` y ``all_edges`` son ``[(uci, child_slack), ...]``.

    * Victoria del que mueve (nodo OR): le basta UNA arista, asi que puede
      elegir la mejor — MAX sobre las aristas ganadoras utilizables.
    * Derrota del que mueve (nodo AND): tiene que fallar por TODAS, asi que
      manda la peor — MIN sobre TODAS las aristas.  Es la misma cuenta para
      las tablas del lado, pero esas no se persisten: las tablas son inmunes
      al reloj y no llevan slack.
    """
    if mover_wins:
        if not winning_edges:
            return None
        return max(edge_slack(fen, uci, slack)
                   for uci, slack in winning_edges)
    if not all_edges:
        return None
    return min(edge_slack(fen, uci, slack) for uci, slack in all_edges)


# ---------- backup minimax (§3.3) ----------

def _wins_for_stm(status, stm_white):
    return status == ('WHITE_WIN' if stm_white else 'BLACK_WIN')


def _loses_for_stm(status, stm_white):
    return status == ('BLACK_WIN' if stm_white else 'WHITE_WIN')


def backup_status(fen, expanded, child_statuses):
    """Estado exacto de un nodo desde sus hijos. child_statuses = lista de
    status de TODOS los hijos solo si expanded; si no, de los conocidos.
    Devuelve nuevo status o UNKNOWN."""
    stm_white = fen.split()[1] == 'w'
    if any(_wins_for_stm(s, stm_white) for s in child_statuses):
        return 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    if not expanded:
        return 'UNKNOWN'
    if all(_loses_for_stm(s, stm_white) for s in child_statuses) and child_statuses:
        return 'BLACK_WIN' if stm_white else 'WHITE_WIN'
    if all(s != 'UNKNOWN' for s in child_statuses) and child_statuses:
        # sin victoria stm, sin derrota total: lo mejor alcanzable exacto es tablas
        return 'DRAW'
    return 'UNKNOWN'


def backup_eval(fen, child_evals):
    """Negamax de evals heuristicos (perspectiva blanca). None si no hay."""
    stm_white = fen.split()[1] == 'w'
    vals = [v for v in child_evals if v is not None]
    if not vals:
        return None
    return max(vals) if stm_white else min(vals)


# ---------- tablebase (§3.1, applicability-lite) ----------

def piece_count(fen):
    return sum(ch.isalpha() for ch in fen.split()[0])


def tb_applicable(fen, max_men=6):
    """Cierre TB solo sin derechos de enroque, sin ep pendiente y <=max_men.
    Los contadores canonicos van a 0, asi que wdl=+-2 es decisivo bajo regla
    de 50 y |wdl|<=1 (cursed/blessed) es tablas practicas."""
    parts = fen.split()
    return (piece_count(fen) <= max_men
            and parts[2] == '-'
            and parts[3] == '-')


def wdl_to_status(wdl, stm_white):
    if wdl >= 2:
        return 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    if wdl <= -2:
        return 'BLACK_WIN' if stm_white else 'WHITE_WIN'
    return 'DRAW'
