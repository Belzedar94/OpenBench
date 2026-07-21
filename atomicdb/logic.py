"""Nucleo exacto de AtomicDB: identidad canonica, expansion con movegen
propio (pyffish, nunca el motor), verificacion de PVs de mate y backup
minimax de tres valores. Todo lo exacto pasa por aqui; el motor solo
aporta evals y candidatos."""

import hashlib

import pyffish as pf

VARIANT = 'atomic'


# ---------- identidad (§3.5) ----------

def canonical_fen(fen):
    """Piezas+turno+enroques+ep, contadores a 0 1."""
    parts = fen.split()
    return ' '.join(parts[:4] + ['0', '1'])


def key_of(fen):
    return hashlib.sha256(canonical_fen(fen).encode()).hexdigest()


def start_fen():
    return canonical_fen(pf.start_fen(VARIANT))


# ---------- reglas (movegen del ingestor) ----------

def legal_moves(fen):
    return pf.legal_moves(VARIANT, fen, [])


def apply_move(fen, uci):
    return canonical_fen(pf.get_fen(VARIANT, fen, [uci]))


def terminal_status(fen):
    """Si la posicion es terminal, devuelve (status, razon); si no, None.
    status en {'WHITE_WIN','BLACK_WIN','DRAW'}."""
    immediate, _ = pf.is_immediate_game_end(VARIANT, fen, [])
    moves = legal_moves(fen)
    if not immediate and moves:
        return None
    # game_result: valor desde la perspectiva del que mueve
    res = pf.game_result(VARIANT, fen, [])
    stm_white = fen.split()[1] == 'w'
    if res > 0:
        return ('WHITE_WIN' if stm_white else 'BLACK_WIN', 'terminal')
    if res < 0:
        return ('BLACK_WIN' if stm_white else 'WHITE_WIN', 'terminal')
    return ('DRAW', 'terminal')


# ---------- verificacion de PV de mate (§3.2) ----------

def verify_mate_pv(root_fen, pv_ucis, winner_is_white):
    """True si la PV es legal jugada a jugada, sin repetir posiciones,
    y acaba en terminal ganado por `winner`. Rechaza todo lo demas."""
    fen = canonical_fen(root_fen)
    seen = {fen}
    for uci in pv_ucis:
        if terminal_status(fen) is not None:
            return False                      # terminal antes de agotar la PV
        if uci not in legal_moves(fen):
            return False
        fen = apply_move(fen, uci)
        if fen in seen:
            return False                      # repeticion interna
        seen.add(fen)
    t = terminal_status(fen)
    if t is None:
        return False
    want = 'WHITE_WIN' if winner_is_white else 'BLACK_WIN'
    return t[0] == want


def prove_forced_mate(fen, winner_is_white, max_plies,
                      budget_positions=200_000, hint_pv=None):
    """Prove a bounded forced mate with an exhaustive AND/OR search.

    The result is one of ``PROVEN``, ``INCONCLUSIVE`` or ``NO_MATE``:

    * on the winner's turn, one proven continuation is sufficient;
    * on the defender's turn, every legal continuation must be proven;
    * a repeated position on the current branch is a failed branch;
    * a non-terminal leaf at ``max_plies`` is a failed branch; and
    * exhausting ``budget_positions`` makes the whole attempt inconclusive.

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

    def search(node_fen, plies_left, path, remaining_hint):
        nonlocal visited
        if visited >= budget_positions:
            raise _BudgetExhausted
        visited += 1

        # Generate the legal set once on non-terminal nodes.  Calling
        # terminal_status() unconditionally would make pyffish generate it a
        # second time, which is material at the 200k-position ceiling.
        immediate, _ = pf.is_immediate_game_end(VARIANT, node_fen, [])
        moves = list(legal_moves(node_fen))
        if immediate or not moves:
            terminal = terminal_status(node_fen)
            return terminal is not None and terminal[0] == winner_status
        if plies_left == 0:
            return False
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
                if search(child, plies_left - 1, path | {child},
                          child_hint):
                    return True
            return False

        # Defender node: a single failed reply refutes the bounded proof.
        for move in moves:
            child = apply_move(node_fen, move)
            if child in path:
                return False
            child_hint = remaining_hint[1:] if move == hinted else ()
            if not search(child, plies_left - 1, path | {child}, child_hint):
                return False
        return True

    try:
        return ('PROVEN' if search(root, max_plies, {root}, hint)
                else 'NO_MATE')
    except _BudgetExhausted:
        return 'INCONCLUSIVE'


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
