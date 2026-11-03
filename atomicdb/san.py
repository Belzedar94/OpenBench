"""Moves as a person reads them: SAN on the pages, from the UCI the tree keeps.

Only the text on a page changes.  URLs, form values, data attributes and the
JSON API stay in UCI, which is what scripts and other sites read.

WHY NOT PYFFISH ALONE.  ``pf.get_san`` is right, but every call sets up the
variant and the position from scratch (~3.5 ms on the server, the same cost as
any other pyffish call, § logic.MOVE_CACHE_ENTRIES), and a move table names
thirty moves of ONE position: thirty calls to say what the board already
says.  So the common case is read off the FEN here (piece letter, capture,
promotion, castling) and the move generator is asked only for what needs it:
telling apart two pieces that can reach the same square.  A caller that
already has the legal moves (the explorer does) passes them and not even that
costs a call.  A move this cannot read keeps its UCI: no piece on its square,
or not among the legal moves it was given.  Asking pyffish instead would not
help there, since ``get_san`` names an illegal move as some other move
(``e4e5`` from the start position comes back as ``Ra1``).

Atomic SAN as pyffish writes it carries no check marks (``Ra8``, never
``Ra8+``), so none is added here.  ``test_san`` holds this to pyffish move by
move.
"""

from . import logic

_DIRECTIONS = {
    'B': ((1, 1), (1, -1), (-1, 1), (-1, -1)),
    'R': ((1, 0), (-1, 0), (0, 1), (0, -1)),
}
_DIRECTIONS['Q'] = _DIRECTIONS['B'] + _DIRECTIONS['R']

# Returned by ``_read`` when two pieces of the same kind can reach the target
# and only the legal moves can say which of them counts.
_NEEDS_LEGAL = object()


def _board(fen):
    """{(file, rank): piece letter}, both 0-based, from the placement field."""
    board = {}
    for row, text in enumerate(fen.split()[0].split('/')):
        file = 0
        for ch in text:
            if ch.isdigit():
                file += int(ch)
            else:
                board[(file, 7 - row)] = ch
                file += 1
    return board


def _square(name):
    return ord(name[0]) - 97, int(name[1]) - 1


def _name(square):
    return chr(97 + square[0]) + str(square[1] + 1)


def _reaches(board, kind, src, dst):
    """Whether a ``kind`` on ``src`` attacks ``dst`` on this board."""
    df, dr = dst[0] - src[0], dst[1] - src[1]
    if kind == 'N':
        return {abs(df), abs(dr)} == {1, 2}
    if (df, dr) == (0, 0):
        return False
    step = ((df > 0) - (df < 0), (dr > 0) - (dr < 0))
    if step not in _DIRECTIONS[kind] or (df and dr and abs(df) != abs(dr)):
        return False
    at = (src[0] + step[0], src[1] + step[1])
    while at != dst:
        if at in board:
            return False
        at = (at[0] + step[0], at[1] + step[1])
    return True


def _read(board, uci, legal):
    """The SAN of one move, ``_NEEDS_LEGAL``, or None when this cannot say."""
    src, dst = _square(uci[:2]), _square(uci[2:4])
    piece = board.get(src)
    if piece is None:
        return None
    kind = piece.upper()
    target = board.get(dst)
    own_target = target is not None and target.isupper() == piece.isupper()
    if kind == 'K':
        # Castling comes as the king's two-square step, or as the king onto
        # its own rook.  A king never captures in Atomic.
        if src[1] == dst[1] and (abs(dst[0] - src[0]) == 2 or (
                own_target and target.upper() == 'R')):
            return 'O-O' if dst[0] > src[0] else 'O-O-O'
        if target is not None:
            return None
        return 'K' + _name(dst)
    if own_target:
        return None
    if kind == 'P':
        san = (uci[0] + 'x' if src[0] != dst[0] else '') + _name(dst)
        return san + ('=' + uci[4].upper() if len(uci) > 4 else '')
    if kind not in 'NBRQ':
        return None
    others = [square for square, other in board.items()
              if other == piece and square != src
              and _reaches(board, kind, square, dst)]
    if others:
        if legal is None:
            return _NEEDS_LEGAL
        others = [square for square in others
                  if _name(square) + uci[2:4] in legal]
    if not others:
        where = ''
    elif all(square[0] != src[0] for square in others):
        where = uci[0]
    elif all(square[1] != src[1] for square in others):
        where = uci[1]
    else:
        where = uci[:2]
    return kind + where + ('x' if target is not None else '') + _name(dst)


def sans(fen, ucis, legal=None):
    """{uci: SAN} for moves of ONE position.

    ``legal`` is that position's legal moves when the caller has them; without
    them a move that needs them costs one move generation for the whole batch.
    """
    ucis = [uci for uci in ucis if uci]
    try:
        board = _board(fen)
    except Exception:
        return {uci: uci for uci in ucis}
    given = legal is not None
    legal = None if legal is None else set(legal)
    named = {}
    for uci in ucis:
        san = None
        try:
            if not given or uci in legal:
                san = _read(board, uci, legal)
                if san is _NEEDS_LEGAL:
                    legal = set(logic.legal_moves(fen))
                    san = _read(board, uci, legal)
        except Exception:
            san = None
        named[uci] = san if isinstance(san, str) else uci
    return named


def san(fen, uci, legal=None):
    """One move of ``fen`` in SAN; its UCI when it cannot be named."""
    if not (fen and uci):
        return uci
    return sans(fen, [uci], legal).get(uci, uci)
