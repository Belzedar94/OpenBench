"""Rules of Alice Atomic: Alice chess on two boards, with atomic captures.

This is the server's copy of the rules.  The browser runs the same rules from
``static/atomicdb/funquests/alice_atomic.js``; the two are kept in step by the
shared test vectors in ``alice_atomic_vectors.json`` (positions with their
legal moves and outcome, written by this module, checked by both).

THE RULES, as the page explains them:

* Two 8x8 boards, A and B.  The usual start position stands on A, B is empty,
  and White moves first.
* A move is made on the board the moving piece stands on, with ordinary chess
  movement on that board: only pieces on that board block it.
* A move that does not capture also needs the same square on the OTHER board
  to be empty, and the piece then passes through to that square of the other
  board.
* A capture explodes on the board where it is made: the captured piece, the
  capturing piece and every piece other than a pawn on the eight squares
  around the destination of that board are removed.  Nothing passes through
  on a capture, so the other board does not matter for it, and the blast
  never reaches the other board.  A king cannot capture.
* En passant: after a pawn's double step the pawn stands on the other board,
  and only a pawn on THAT board may take it en passant, on the next move,
  landing on the square the pawn passed over (which has to be empty on that
  board).  The blast is centred on that landing square.
* Promotion is the usual one; a promotion without capture passes through to
  the other board like any quiet move, and a capturing promotion explodes.
* Castling: king and rook unmoved, the squares between them empty and the
  king's start, crossing and landing squares not attacked, all on their
  board; the king's and the rook's landing squares must be empty on the
  other board too, and both pass through.
* A king is in check when an enemy piece on the same board attacks it, except
  when both kings stand next to each other on that board.  A move that leaves
  the mover's king in check, or that blows up the mover's own king, is
  illegal.  A move that blows up the enemy king wins at once, even if the
  mover's king is left attacked.
* Checkmate wins; stalemate, threefold repetition and the fifty-move rule are
  draws.

NOTATION.  A position is written as ``<board A>|<board B> <side> <castling>
<en passant> <halfmove> <fullmove>``: each board is a FEN piece placement,
and en passant is ``-`` or the board letter and square, as in ``Be3``.  A
move is the board letter it is played on followed by its UCI, as in
``Ae2e4``, ``Bd7d8q`` or ``Ae1g1`` (castling).

Squares are 0..63 with a1 = 0, h1 = 7, a8 = 56.  Empty squares are ``''``.
"""

import hashlib

START_AFEN = ('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR|8/8/8/8/8/8/8/8 '
              'w KQkq - 0 1')

# EXPERIMENTAL rule sets, picked with ``variant=`` (the default 'v0' is the
# rules above, unchanged).  Only the Python copy knows them so far.
#
# * 'v1', kings on B: both kings start alone on B (e1, e8), everything else
#   on A as usual.  No castling: the rights start empty.
# * 'v2', capture here, explode there: a capture removes the victim from the
#   board where it is made; the capturer then passes to the same square of
#   the OTHER board and explodes there, removing itself and every piece
#   other than a pawn on the eight squares around it on that board.  Like
#   every other move, a capture needs that square empty on the other board.
#   Nothing explodes on the board where the capture was made.  A king is in
#   check when an enemy piece on its board attacks it and that capture could
#   be made (its square empty on the other board, and the attacker's own
#   king not next to that square on the other board); kings side by side on
#   the same board do check each other.
# * 'v3', pawns on both boards: the usual start on A, and both sides' pawns
#   also on B (ranks 2 and 7).
# * 'v4', 'v1' plus 'v3' pawns on B in front of the kings (the kings keep a
#   pawn shield on their own board).
# * 'v5', 'v2' captures with the 'v3' start.
VARIANT_STARTS = {
    'v0': START_AFEN,
    'v1': ('rnbq1bnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQ1BNR|4k3/8/8/8/8/8/8/4K3 '
           'w - - 0 1'),
    'v2': START_AFEN,
    'v3': ('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR|'
           '8/pppppppp/8/8/8/8/PPPPPPPP/8 w KQkq - 0 1'),
    'v4': ('rnbq1bnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQ1BNR|'
           '4k3/pppppppp/8/8/8/8/PPPPPPPP/4K3 w - - 0 1'),
    'v5': ('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR|'
           '8/pppppppp/8/8/8/8/PPPPPPPP/8 w KQkq - 0 1'),
}
# Rule sets whose captures explode on the other board.
CROSS_BLAST = frozenset(('v2', 'v5'))
BOARD_LETTERS = 'AB'
FILES = 'abcdefgh'

KNIGHT_STEPS = ((1, 2), (2, 1), (-1, 2), (-2, 1),
                (1, -2), (2, -1), (-1, -2), (-2, -1))
KING_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1),
              (1, 1), (1, -1), (-1, 1), (-1, -1))
ROOK_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
BISHOP_DIRS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
PROMOTIONS = 'qrbn'

# Move kinds (internal): quiet, double pawn step, capture, en passant, castle.
QUIET, DOUBLE, CAPTURE, EN_PASSANT, CASTLE = 'q', 'd', 'c', 'e', 'k'


class IllegalMove(ValueError):
    pass


def square_name(sq):
    return FILES[sq % 8] + str(sq // 8 + 1)


def parse_square(name):
    if (len(name) != 2 or name[0] not in FILES
            or name[1] not in '12345678'):
        raise ValueError(f'bad square {name!r}')
    return FILES.index(name[0]) + 8 * (int(name[1]) - 1)


def color_of(piece):
    return 'w' if piece.isupper() else 'b'


def opponent(color):
    return 'b' if color == 'w' else 'w'


class State:
    __slots__ = ('boards', 'turn', 'castling', 'ep', 'halfmove', 'fullmove',
                 'variant')

    def __init__(self, boards, turn, castling, ep, halfmove, fullmove,
                 variant='v0'):
        self.boards = boards        # [list of 64, list of 64]
        self.turn = turn            # 'w' or 'b'
        self.castling = castling    # subset of 'KQkq', in that order
        self.ep = ep                # None or (board, square)
        self.halfmove = halfmove
        self.fullmove = fullmove
        self.variant = variant

    def copy(self):
        return State([self.boards[0][:], self.boards[1][:]], self.turn,
                     self.castling, self.ep, self.halfmove, self.fullmove,
                     self.variant)


# -- reading and writing positions -------------------------------------------

def _parse_placement(text):
    ranks = text.split('/')
    if len(ranks) != 8:
        raise ValueError('a board needs 8 ranks')
    board = [''] * 64
    for i, row in enumerate(ranks):
        rank = 7 - i
        file = 0
        for ch in row:
            if ch.isdigit():
                file += int(ch)
            elif ch in 'pnbrqkPNBRQK':
                if file > 7:
                    raise ValueError('rank too long')
                board[rank * 8 + file] = ch
                file += 1
            else:
                raise ValueError(f'bad piece {ch!r}')
        if file != 8:
            raise ValueError('rank of the wrong length')
    return board


def _placement(board):
    rows = []
    for rank in range(7, -1, -1):
        row, empty = '', 0
        for file in range(8):
            piece = board[rank * 8 + file]
            if piece:
                if empty:
                    row += str(empty)
                    empty = 0
                row += piece
            else:
                empty += 1
        if empty:
            row += str(empty)
        rows.append(row)
    return '/'.join(rows)


def parse(afen, variant='v0'):
    if variant not in VARIANT_STARTS:
        raise ValueError(f'unknown variant {variant!r}')
    parts = afen.split()
    if len(parts) != 6:
        raise ValueError('a position has six fields')
    boards_text, turn, castling, ep, halfmove, fullmove = parts
    halves = boards_text.split('|')
    if len(halves) != 2:
        raise ValueError('a position has two boards')
    if turn not in ('w', 'b'):
        raise ValueError('bad side to move')
    if castling != '-' and (not castling
                            or any(c not in 'KQkq' for c in castling)):
        raise ValueError('bad castling field')
    if ep == '-':
        ep_value = None
    else:
        if len(ep) != 3 or ep[0] not in BOARD_LETTERS:
            raise ValueError('bad en passant field')
        ep_value = (BOARD_LETTERS.index(ep[0]), parse_square(ep[1:]))
    rights = ''.join(c for c in 'KQkq' if c in castling)
    return State([_parse_placement(halves[0]), _parse_placement(halves[1])],
                 turn, rights, ep_value, int(halfmove), int(fullmove),
                 variant)


def _ep_text(state):
    if state.ep is None:
        return '-'
    return BOARD_LETTERS[state.ep[0]] + square_name(state.ep[1])


def to_afen(state):
    return ' '.join((
        _placement(state.boards[0]) + '|' + _placement(state.boards[1]),
        state.turn, state.castling or '-', _ep_text(state),
        str(state.halfmove), str(state.fullmove)))


def position_key(state):
    """What threefold repetition compares: everything but the counters."""
    return ' '.join(to_afen(state).split()[:4])


def position_hash(state):
    return hashlib.sha1(position_key(state).encode()).hexdigest()[:16]


def start_state(variant='v0'):
    return parse(VARIANT_STARTS[variant], variant)


# -- attacks and check -------------------------------------------------------

def _on_board(file, rank):
    return 0 <= file < 8 and 0 <= rank < 8


def attacked(board, sq, by):
    """Whether a piece of colour ``by`` other than its king attacks ``sq`` on
    ``board``.  A king never attacks: it cannot capture."""
    f, r = sq % 8, sq // 8
    white = by == 'w'
    pawn_rank = r - 1 if white else r + 1
    pawn = 'P' if white else 'p'
    if 0 <= pawn_rank < 8:
        for df in (-1, 1):
            if _on_board(f + df, pawn_rank) and \
                    board[pawn_rank * 8 + f + df] == pawn:
                return True
    knight = 'N' if white else 'n'
    for df, dr in KNIGHT_STEPS:
        if _on_board(f + df, r + dr) and board[(r + dr) * 8 + f + df] == knight:
            return True
    for dirs, movers in ((ROOK_DIRS, 'RQ'), (BISHOP_DIRS, 'BQ')):
        if not white:
            movers = movers.lower()
        for df, dr in dirs:
            ff, rr = f + df, r + dr
            while _on_board(ff, rr):
                piece = board[rr * 8 + ff]
                if piece:
                    if piece in movers:
                        return True
                    break
                ff += df
                rr += dr
    return False


def king_location(state, color):
    king = 'K' if color == 'w' else 'k'
    for b in (0, 1):
        board = state.boards[b]
        for sq in range(64):
            if board[sq] == king:
                return b, sq
    return None


def _kings_adjacent(board, sq, color):
    enemy_king = 'k' if color == 'w' else 'K'
    f, r = sq % 8, sq // 8
    for df, dr in KING_STEPS:
        if _on_board(f + df, r + dr) and \
                board[(r + dr) * 8 + f + df] == enemy_king:
            return True
    return False


def _square_in_check(board, sq, color, other=None, variant='v0'):
    """Whether a king of ``color`` standing on ``sq`` of ``board`` would be in
    check there (``other`` is the other board, needed by 'v2'/'v5')."""
    if variant in CROSS_BLAST:
        # The capture lands and explodes on the other board: impossible if
        # that square is taken there, or if it would blow up the capturer's
        # own king.
        if other[sq] or _kings_adjacent(other, sq, color):
            return False
        return attacked(board, sq, opponent(color))
    if _kings_adjacent(board, sq, color):
        return False
    return attacked(board, sq, opponent(color))


def in_check(state, color):
    loc = king_location(state, color)
    if loc is None:
        return False
    b, sq = loc
    return _square_in_check(state.boards[b], sq, color, state.boards[1 - b],
                            state.variant)


# -- move generation ---------------------------------------------------------

def move_text(move):
    b, frm, to, promo, _kind = move
    return BOARD_LETTERS[b] + square_name(frm) + square_name(to) + (promo or '')


def _pseudo_moves(state):
    turn = state.turn
    white = turn == 'w'
    cross = state.variant in CROSS_BLAST
    moves = []
    for b in (0, 1):
        board = state.boards[b]
        other = state.boards[1 - b]
        for sq in range(64):
            piece = board[sq]
            if not piece or color_of(piece) != turn:
                continue
            kind = piece.upper()
            f, r = sq % 8, sq // 8
            if kind == 'P':
                step = 8 if white else -8
                start_rank = 1 if white else 6
                last_rank = 7 if white else 0
                one = sq + step
                if 0 <= one < 64 and not board[one]:
                    if not other[one]:
                        if one // 8 == last_rank:
                            for p in PROMOTIONS:
                                moves.append((b, sq, one, p, QUIET))
                        else:
                            moves.append((b, sq, one, None, QUIET))
                    two = one + step
                    if r == start_rank and not board[two] and not other[two]:
                        moves.append((b, sq, two, None, DOUBLE))
                rr = r + (1 if white else -1)
                for df in (-1, 1):
                    if not _on_board(f + df, rr):
                        continue
                    target = rr * 8 + f + df
                    victim = board[target]
                    if cross and other[target]:
                        continue
                    if victim and color_of(victim) != turn:
                        if rr == last_rank:
                            for p in PROMOTIONS:
                                moves.append((b, sq, target, p, CAPTURE))
                        else:
                            moves.append((b, sq, target, None, CAPTURE))
                    elif (state.ep == (b, target) and not victim
                          and board[target - step] == ('p' if white
                                                       else 'P')):
                        moves.append((b, sq, target, None, EN_PASSANT))
                continue
            if kind in ('N', 'K'):
                for df, dr in (KNIGHT_STEPS if kind == 'N' else KING_STEPS):
                    if not _on_board(f + df, r + dr):
                        continue
                    target = (r + dr) * 8 + f + df
                    victim = board[target]
                    if not victim:
                        if not other[target]:
                            moves.append((b, sq, target, None, QUIET))
                    elif (color_of(victim) != turn and kind != 'K'
                          and not (cross and other[target])):
                        moves.append((b, sq, target, None, CAPTURE))
                if kind == 'K':
                    moves.extend(_castling_moves(state, b, sq))
                continue
            dirs = {'R': ROOK_DIRS, 'B': BISHOP_DIRS,
                    'Q': ROOK_DIRS + BISHOP_DIRS}[kind]
            for df, dr in dirs:
                ff, rr = f + df, r + dr
                while _on_board(ff, rr):
                    target = rr * 8 + ff
                    victim = board[target]
                    if not victim:
                        if not other[target]:
                            moves.append((b, sq, target, None, QUIET))
                    else:
                        if color_of(victim) != turn and not (
                                cross and other[target]):
                            moves.append((b, sq, target, None, CAPTURE))
                        break
                    ff += df
                    rr += dr
    return moves


def _castling_moves(state, b, sq):
    """Castling, always on board A: the rights only survive while king and
    rook stand untouched on their start squares of board A."""
    white = state.turn == 'w'
    home = 0 if white else 56
    if b != 0 or sq != home + 4:
        return []
    board, other = state.boards[0], state.boards[1]
    color = state.turn
    rook = 'R' if white else 'r'
    out = []
    sides = (('K' if white else 'k', 7, (5, 6), 6, 5, (4, 5, 6)),
             ('Q' if white else 'q', 0, (1, 2, 3), 2, 3, (4, 3, 2)))
    for right, rook_file, between, king_to, rook_to, safe in sides:
        if right not in state.castling:
            continue
        if board[home + rook_file] != rook:
            continue
        if any(board[home + f] for f in between):
            continue
        if other[home + king_to] or other[home + rook_to]:
            continue
        if any(_square_in_check(board, home + f, color, other, state.variant)
               for f in safe):
            continue
        out.append((0, home + 4, home + king_to, None, CASTLE))
    return out


def apply_move(state, move):
    """The position after ``move``, which must come from the generator.  The
    mover's own king may be gone afterwards: legality is checked apart."""
    b, frm, to, promo, kind = move
    s = state.copy()
    board, other = s.boards[b], s.boards[1 - b]
    piece = board[frm]
    white = state.turn == 'w'
    board[frm] = ''
    if kind == CASTLE:
        home = frm - 4
        rook_from, rook_to = ((home + 7, home + 5) if to == home + 6
                              else (home, home + 3))
        rook = board[rook_from]
        board[rook_from] = ''
        other[to] = piece
        other[rook_to] = rook
    elif kind in (CAPTURE, EN_PASSANT):
        if kind == EN_PASSANT:
            board[to - 8 if white else to + 8] = ''
        board[to] = ''
        # 'v2'/'v5': the capturer passes through and explodes over there
        # (the square itself is empty there: the generator checked).
        blasted = other if state.variant in CROSS_BLAST else board
        f, r = to % 8, to // 8
        for df, dr in KING_STEPS:
            if _on_board(f + df, r + dr):
                n = (r + dr) * 8 + f + df
                if blasted[n] and blasted[n] not in 'Pp':
                    blasted[n] = ''
    else:
        if promo:
            piece = promo.upper() if white else promo
        other[to] = piece
    s.ep = None
    if kind == DOUBLE:
        middle = (frm + to) // 2
        if not other[middle]:
            s.ep = (1 - b, middle)
    a = s.boards[0]
    rights = ''
    for right, home, king in (('K', 0, 'K'), ('Q', 0, 'K'),
                              ('k', 56, 'k'), ('q', 56, 'k')):
        if right not in state.castling:
            continue
        rook_sq = home + (7 if right in 'Kk' else 0)
        if a[home + 4] == king and a[rook_sq] == ('R' if king == 'K' else 'r'):
            rights += right
    s.castling = rights
    if piece in 'Pp' or kind in (CAPTURE, EN_PASSANT):
        s.halfmove = 0
    else:
        s.halfmove = state.halfmove + 1
    if not white:
        s.fullmove = state.fullmove + 1
    s.turn = opponent(state.turn)
    return s


def _is_legal_result(after, mover):
    if king_location(after, mover) is None:
        return False
    if king_location(after, opponent(mover)) is None:
        return True
    return not in_check(after, mover)


def legal_moves_detailed(state):
    """Every legal move with the position it leads to."""
    out = []
    for move in _pseudo_moves(state):
        after = apply_move(state, move)
        if _is_legal_result(after, state.turn):
            out.append((move, after))
    return out


def legal_moves(state):
    return [move_text(m) for m, _after in legal_moves_detailed(state)]


def play(state, text):
    """The position after the move written ``text``; ``IllegalMove`` if it
    is not legal here."""
    for move, after in legal_moves_detailed(state):
        if move_text(move) == text:
            return after
    raise IllegalMove(text)


# -- game end ----------------------------------------------------------------

def outcome(state, repetitions=1):
    """``(result, reason)`` when the game is over in ``state``, else None.

    ``repetitions`` is how many times this position has stood on the board
    with the same side to move, this time included.
    """
    if king_location(state, 'w') is None:
        return '0-1', 'explosion'
    if king_location(state, 'b') is None:
        return '1-0', 'explosion'
    if not legal_moves_detailed(state):
        if in_check(state, state.turn):
            return ('0-1' if state.turn == 'w' else '1-0'), 'checkmate'
        return '1/2-1/2', 'stalemate'
    if repetitions >= 3:
        return '1/2-1/2', 'repetition'
    if state.halfmove >= 100:
        return '1/2-1/2', 'fifty-move'
    return None


def replay(moves, afen=None, variant='v0'):
    """Play ``moves`` from ``afen`` (the variant's start by default).
    Returns ``(state, outcome)``; raises ``IllegalMove`` on the first
    illegal one or on a move after the end."""
    state = parse(afen or VARIANT_STARTS[variant], variant)
    seen = {position_hash(state): 1}
    for text in moves:
        if outcome(state, seen.get(position_hash(state), 1)):
            raise IllegalMove(text)
        state = play(state, text)
        if state.halfmove == 0:
            seen = {}
        key = position_hash(state)
        seen[key] = seen.get(key, 0) + 1
    return state, outcome(state, seen.get(position_hash(state), 1))


def perft(state, depth):
    if depth == 0:
        return 1
    total = 0
    for move, after in legal_moves_detailed(state):
        if depth == 1:
            total += 1
        elif king_location(after, opponent(state.turn)) is None:
            continue    # the game ended on this move: no deeper nodes
        else:
            total += perft(after, depth - 1)
    return total
