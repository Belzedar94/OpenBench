"""Win / draw / loss shares for the explorer's move rows.

Display only.  Nothing here reads or writes a stored value, a closure or a
backup: it turns what a row of the move table already shows into three
shares, from the side to move's point of view (the orientation of every
number in that table, § views._child_moves).

Two kinds of bar:

  * PROVEN.  A row whose position is closed (mate, tablebase, minimax, a
    terminal position: any WHITE_WIN / BLACK_WIN / DRAW status) or whose
    value on the current line is the draw of a repetition shows 100 % of
    that outcome.

  * ESTIMATE.  Every other row with a number gets a logistic estimate from
    that number alone.  Nothing in the tree stores a win/draw/loss: the
    workers keep the engine's centipawns, and Atomic-Stockfish's own WDL
    output reuses orthodox Stockfish's fit, which its ``to_cp`` already calls
    uncalibrated for Atomic.  So the page draws its own simple curve and says
    it is an estimate.

The estimate has the shape of Stockfish's win-rate model,

    win(cp)  = 1 / (1 + exp((MIDPOINT - cp) / SCALE))
    loss(cp) = win(-cp)
    draw     = 1 - win - loss

with MIDPOINT = 400 cp (the score where the side to move is given even odds
of winning) and SCALE = 150 cp.  The two numbers are a rough, uncalibrated
fit to the scale of AtomicDB's evaluations (the start position stands near
+1000 cp for White, and an even position near 0), not a measurement: at 0 the
bar reads 6 / 88 / 6, at +100 12 / 85 / 3, at +400 50 / 50 / 0, at +1000
98 / 2 / 0.
"""

import math

WDL_MIDPOINT_CP = 400
WDL_SCALE_CP = 150

ESTIMATE_NOTE = 'estimate from score'


def _win_per_mille(cp):
    """The win share of a score from the side to move, in per mille."""
    # exp() of a huge exponent overflows; past this the share is 0 or 1000.
    exponent = (WDL_MIDPOINT_CP - cp) / WDL_SCALE_CP
    if exponent > 50:
        return 0.0
    if exponent < -50:
        return 1000.0
    return 1000.0 / (1.0 + math.exp(exponent))


def estimate(cp):
    """``(win, draw, loss)`` in whole percent for a centipawn score.

    The three always add up to 100: win and loss are rounded and the draw
    takes the rest.
    """
    win = int(round(_win_per_mille(cp) / 10.0))
    loss = int(round(_win_per_mille(-cp) / 10.0))
    return win, max(0, 100 - win - loss), loss


def _bar(win, draw, loss, proven, label):
    return {'w': win, 'd': draw, 'l': loss, 'proven': proven,
            'label': label,
            'title': '%s: win %d%% · draw %d%% · loss %d%%, for the side to move'
                     % (label, win, draw, loss)}


def row_wdl(row, win_status, loss_status):
    """The bar of one move-table row, or None when the row has no number.

    ``row`` is a row of ``views._child_moves``; ``win_status`` and
    ``loss_status`` are the statuses that mean the side to move wins or
    loses.
    """
    status = row.get('status')
    if row.get('repetition'):
        # On this line the move repeats, and a repetition is the draw the
        # row already shows as 0.
        return _bar(0, 100, 0, True, 'repetition draw')
    if status == win_status:
        return _bar(100, 0, 0, True, 'proven win (%s)' % row.get('closure'))
    if status == loss_status:
        return _bar(0, 0, 100, True, 'proven loss (%s)' % row.get('closure'))
    if status == 'DRAW':
        return _bar(0, 100, 0, True, 'proven draw (%s)' % row.get('closure'))
    score = row.get('score')
    if score is None:
        return None
    win, draw, loss = estimate(score)
    return _bar(win, draw, loss, False, ESTIMATE_NOTE)
