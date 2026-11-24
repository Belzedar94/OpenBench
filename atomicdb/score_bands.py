"""How an engine score becomes a stored AtomicDB value: one monotone map.

THE CHANGE (24-Nov-2026).  The worker used to clamp every centipawn score to
+-9500 before it sent it.  Two very different things landed on that one
number: a tablebase win the engine had found a few plies down, and a huge
ordinary eval (MoKy's 10k-16k cp positions).  Neither could be told apart
from the other, and two tablebase wins could not be told apart by how far
away the tables were.  Torom proposed (28-Oct) to keep the distance to the
tables in the value instead, so that tablebase wins sort by distance; the
owner approved it on 24-Nov.

THE ENGINE'S SCALE (Atomic-Stockfish, ``uci.cpp`` ``format_score`` and
``score.cpp``).  A decided score that is not a mate is printed as
``score cp +-(20000 - d)``, where ``d`` is the number of plies from the
searched position to the position where the tables were probed
(``VALUE_TB - |v|``); ``d`` is at most the engine's ``MAX_PLY`` (246), so a
tablebase score is at least 19754 in absolute value.  An ordinary score is
``100 * v / PawnValue`` with ``|v| < VALUE_TB_WIN_IN_MAX_PLY``, at most 15146.
The two ranges do not meet, which is what lets the server tell them apart
from the number alone.  Mates are ``score mate N`` and never come here.

THE STORED BANDS, by absolute value (White's point of view, symmetric):

    0 .. 8999      plain centipawns, unchanged
    9000 .. 9249   a large ordinary eval, compressed (9000 + (cp-9000)*249/6200,
                   15200 cp and above all at 9249)
    9250 .. 9500   a tablebase win reached d plies down: 9500 - d
                   (d capped at 250).  9500 itself is d = 0, or a capped
                   value from a worker that did not send its raw line
    9501 .. 9999   an engine mate, 10000 - moves (unchanged)
    10000          proven (unchanged)

Every band is above the one before it, and within a band a better engine
score is never a smaller stored value, so sorting stored values sorts what
the engine said: a shorter way into the tables ranks above a longer one, and
both rank above any ordinary eval.  The two thresholds the rest of the code
reads did not move: ``MATE_BAND`` (9000) still starts at the first value
that is not plain, and ``TB_CLAMP_CEILING`` (9500) is still the largest
value that is not a mate count, so every row that read as "in the band" or
"a mate" before still does.

WHERE IT IS APPLIED.  On the server, from the raw UCI line every worker
already sends with each result (``normalize_lines``), so no worker has to
change and the values of old results can be recomputed from what is stored
(``manage.py recompute_tb_distance``).  A line without a raw line keeps the
value it came with.
"""

import re

# The engine's scale (see the top of this module).
ENGINE_TB_CP = 20_000
ENGINE_MAX_PLY = 246
ENGINE_CP_MAX = 15_200      # above the largest ordinary score (15146)

# The stored bands.
PLAIN_MAX = 8_999
BIG_LOW = 9_000
BIG_HIGH = 9_249
TB_TOP = 9_500
TB_DISTANCE_MAX = TB_TOP - BIG_HIGH - 1      # 250
TB_LOW = TB_TOP - TB_DISTANCE_MAX            # 9250
MATE_BASE = 10_000

# A raw engine score this large is a tablebase score.  The engine never
# prints one between ENGINE_CP_MAX and here; anything there is treated as
# an ordinary score at the top of the compressed band.
ENGINE_TB_MIN_CP = ENGINE_TB_CP - TB_DISTANCE_MAX    # 19750

# What the worker used to send for every cp score.
LEGACY_CLAMP = TB_TOP

_SCORE_RE = re.compile(r'\bscore (cp|mate) (-?\d+)\b')


def _sign(value):
    return -1 if value < 0 else 1


def tb_distance(cp):
    """Plies to the tables of a raw engine cp score, or None if it is not one."""
    magnitude = abs(int(cp))
    if magnitude < ENGINE_TB_MIN_CP:
        return None
    return min(TB_DISTANCE_MAX, max(0, ENGINE_TB_CP - magnitude))


def engine_cp_to_stored(cp):
    """The stored value of a raw engine ``score cp`` (same point of view)."""
    cp = int(cp)
    magnitude, sign = abs(cp), _sign(cp)
    if magnitude <= PLAIN_MAX:
        return cp
    distance = tb_distance(cp)
    if distance is not None:
        return sign * (TB_TOP - distance)
    if magnitude >= ENGINE_CP_MAX:
        return sign * BIG_HIGH
    return sign * (BIG_LOW + (magnitude - BIG_LOW) * (BIG_HIGH - BIG_LOW)
                   // (ENGINE_CP_MAX - BIG_LOW))


def legacy_value(cp):
    """What the worker stored for a raw ``score cp`` before this change."""
    return max(-LEGACY_CLAMP, min(LEGACY_CLAMP, int(cp)))


def raw_score(raw):
    """``('cp'|'mate', int)`` from a raw ``info`` line, side to move, or None."""
    if not isinstance(raw, str) or not raw:
        return None
    match = _SCORE_RE.search(raw)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def band(value):
    """The name of the band a stored value is in (for docs and reports)."""
    if value is None:
        return None
    magnitude = abs(int(value))
    if magnitude <= PLAIN_MAX:
        return 'plain'
    if magnitude <= BIG_HIGH:
        return 'big'
    if magnitude <= TB_TOP:
        return 'tb'
    if magnitude < MATE_BASE:
        return 'mate'
    return 'proven'


def stored_from_raw(line, stm_white):
    """``(new, legacy)`` White-POV values of a line's raw cp score, or None.

    None when the line carries a mate, no raw line, or a raw line without a
    ``score cp``.
    """
    if not isinstance(line, dict) or line.get('mate') is not None:
        return None
    score = raw_score(line.get('raw'))
    if score is None or score[0] != 'cp':
        return None
    sign = 1 if stm_white else -1
    return (sign * engine_cp_to_stored(score[1]),
            sign * legacy_value(score[1]))


def normalize_line(line, stm_white):
    """``line`` with its ``eval_cp`` on the stored scale (a copy if it moves).

    Only a value the raw line accounts for is rewritten: the worker's old
    clamp of it, or already the new value (so this is idempotent).  A line
    whose ``eval_cp`` says something else is left as it came.
    """
    values = stored_from_raw(line, stm_white)
    if values is None:
        return line
    new, legacy = values
    current = line.get('eval_cp')
    if current == new or current not in (legacy, None):
        return line
    return dict(line, eval_cp=new)


def normalize_lines(lines, stm_white):
    """``normalize_line`` over a list of MultiPV lines (others pass through)."""
    if not isinstance(lines, list):
        return lines
    return [normalize_line(line, stm_white) for line in lines]
