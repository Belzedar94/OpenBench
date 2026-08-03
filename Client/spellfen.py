"""Parsing helpers for the spell-chess FEN dialect and its move tokens.

Canonical FEN shape (verified against both engines and the oracle):

    rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR[JJFFFFjjfffff] \
        {F@a1:3,J@-:0,f@-:0,j@-:0} b KQkq - 0 1
    <---------- board -------------><-pocket-> <---- spell state ----> stm cast ep hm fm

Whitespace-separated fields:
    0  board+pocket   (the pocket is bracketed and glued to the board, no space)
    1  spell state    (single token, no spaces inside the braces)
    2  side to move
    3  castling rights
    4  en passant
    5  halfmove clock
    6  fullmove number

Move tokens:
    plain            e2e4, e7e8q
    cast (spell+move) f@e7,e5d6   -- exactly one comma, base move is the tail

We deliberately do NOT reimplement move generation or legality here. Everything in
this module is pure string surgery on FENs the oracle produced.
"""

FILES = "abcdefgh"


def fields(fen):
    return fen.split()


def side_to_move(fen):
    return fields(fen)[2]


def fullmove(fen):
    try:
        return int(fields(fen)[6])
    except (IndexError, ValueError):
        return 1


def board_part(fen):
    """Board ranks only, with the bracketed pocket stripped off."""
    head = fields(fen)[0]
    return head.split("[", 1)[0]


def repetition_key(fen):
    """Everything that defines the position, minus the two move counters.

    Castling rights and the en-passant square are kept: two positions that differ in
    those are NOT the same position, and dropping them would manufacture spurious
    threefold draws.
    """
    f = fields(fen)
    return " ".join(f[0:5])


def base_move(move):
    """Strip a leading `x@sq,` cast prefix; return the plain UCI base move."""
    if "," in move:
        return move.split(",", 1)[1]
    return move


def dest_square(move):
    """Destination square of the base move, e.g. `f@e7,e5d6` -> 'd6', `e7e8q` -> 'e8'.

    Base moves are `<from><to>[promo]`, so the destination is always characters 2:4.
    """
    b = base_move(move)
    if len(b) < 4:
        return None
    sq = b[2:4]
    if sq[0] not in FILES or not sq[1].isdigit():
        return None
    return sq


def piece_at(fen, square):
    """Piece letter on `square`, or None. Uppercase = white, lowercase = black."""
    if not square:
        return None
    file_idx = FILES.index(square[0])
    rank = int(square[1])
    ranks = board_part(fen).split("/")
    if not 1 <= rank <= len(ranks):
        return None
    row = ranks[len(ranks) - rank]  # FEN lists rank 8 first
    col = 0
    i = 0
    while i < len(row):
        ch = row[i]
        if ch.isdigit():
            # Run lengths can be multi-digit in principle; consume all digits.
            j = i
            while j < len(row) and row[j].isdigit():
                j += 1
            col += int(row[i:j])
            i = j
            continue
        if col == file_idx:
            return ch
        col += 1
        i += 1
    return None


def captures_king(fen, move, mover_is_white):
    """True if `move` lands on the square holding the OPPONENT's king in `fen`.

    This variant plays to king capture, so this is a terminal condition we must test
    ourselves rather than hoping the oracle keeps producing sensible positions.
    """
    target = piece_at(fen, dest_square(move))
    if target is None:
        return False
    return target == ("k" if mover_is_white else "K")


def has_king(fen, white):
    return ("K" if white else "k") in board_part(fen)
