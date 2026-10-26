"""Ties between moves of equal value (see the block above ingest._best_child).

A proven child counted as a flat 10,000 with the heaviest support there is
and no distance, so among moves worth the same a proven loss beat every
move still open, and among proven moves the first edge the database
returned won.  At 1725116b (Black to move, every known move lost, 25-Oct-2026)
the position stood on ...f4, a mate in one, while ...Nc6 was still open at
the same number.
"""

import hashlib

from . import ingest, views
from .models import Edge, Position
from .testing import TestCase

ENGINE = 128_000_000
DEEP = 512_000_000
PROVEN = ingest.PROVEN_QUALITY
FEN = '4k3/8/8/8/8/8/8/4K3 {} - - 0 1'


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm, **kw):
    return Position.objects.create(key=_key(name), fen=FEN.format(stm), **kw)


def _edge(parent, child, move):
    Edge.objects.create(parent=parent, move_uci=move, child=child)


def _decided(name, stm, mate_in, status='WHITE_WIN'):
    """A child closed as ``status``, ``mate_in`` plies from mate."""
    return _pos(name, stm, status=status, closure='MINIMAX', proof='ENGINE',
                mate_in=mate_in, nodes_invested=ENGINE)


def _backed(row):
    row.refresh_from_db()
    return row.backed_eval, row.backed_move, row.backed_plies, row.backed_nodes


# Children for _best_child on its own.

def _proven(move, value, mate_in):
    return ingest._ChildValue(move, value, PROVEN, 0, exact=True,
                              mate_in=mate_in)


def _open(move, value, quality=ENGINE, plies=1):
    return ingest._ChildValue(move, value, quality, plies)


def _old_best_child(children, stm_white):
    """ingest._best_child before 26-Oct-2026: the record of defect B."""
    if stm_white:
        return max(children, key=lambda c: (c.value, ingest._grounding(c),
                                            c.quality, -c.plies))
    return min(children, key=lambda c: (c.value, -ingest._grounding(c),
                                        -c.quality, c.plies))


class TieOrderTests(TestCase):
    """_best_child on its own, for both sides to move."""

    def test_an_open_move_beats_a_proven_loss_whatever_its_weight(self):
        for stm_white, lost in ((True, -10_000), (False, 10_000)):
            loss = _proven('loss', lost, 1)
            for quality in (0, ENGINE, PROVEN):
                still = _open('open', lost, quality)
                self.assertIs(ingest._best_child([loss, still], stm_white),
                              still)
                self.assertIs(ingest._best_child([still, loss], stm_white),
                              still)

    def test_among_proven_losses_the_latest_mate(self):
        for stm_white, lost in ((True, -10_000), (False, 10_000)):
            soon, late = _proven('soon', lost, 1), _proven('late', lost, 11)
            uncounted = _proven('none', lost, None)
            self.assertIs(
                ingest._best_child([soon, uncounted, late], stm_white), late)
            # A distance nobody counted ranks last, as in the table.
            self.assertIs(ingest._best_child([uncounted, soon], stm_white),
                          soon)

    def test_among_proven_wins_the_soonest_mate(self):
        for stm_white, won in ((True, 10_000), (False, -10_000)):
            soon, late = _proven('soon', won, 3), _proven('late', won, 7)
            uncounted = _proven('none', won, None)
            self.assertIs(
                ingest._best_child([late, uncounted, soon], stm_white), soon)
            self.assertIs(ingest._best_child([uncounted, late], stm_white),
                          late)

    def test_a_proven_win_still_beats_an_open_move_worth_the_same(self):
        for stm_white, won in ((True, 10_000), (False, -10_000)):
            win = _proven('win', won, 21)
            still = _open('open', won, PROVEN)
            self.assertIs(ingest._best_child([still, win], stm_white), win)

    def test_a_proven_draw_still_beats_an_open_zero(self):
        for stm_white in (True, False):
            draw = _proven('draw', 0, None)
            still = _open('open', 0, DEEP, plies=0)
            self.assertIs(ingest._best_child([still, draw], stm_white), draw)

    def test_grounding_still_comes_first(self):
        # A move that comes back here, or runs into a loop elsewhere, loses
        # its ties, even to a proven loss (#69, #70).
        for flag in ('returns', 'circular'):
            loss = _proven('loss', 10_000, 1)
            still = _open('open', 10_000)
            setattr(still, flag, True)
            self.assertIs(ingest._best_child([still, loss], False), loss)

    def test_a_better_value_still_wins(self):
        loss = _proven('loss', -10_000, 40)
        self.assertIs(ingest._best_child([loss, _open('open', -900)], True)
                      .move, 'open')

    def test_full_ties_go_to_the_first_move_alphabetically(self):
        for stm_white in (True, False):
            moves = ['f5f4', 'a7a5', 'e8f8']
            for order in (moves, moves[::-1]):
                children = [_proven(move, 10_000, 3) for move in order]
                self.assertEqual(
                    ingest._best_child(children, stm_white).move, 'a7a5')
                claims = [_open(move, 120, 0, plies=0) for move in order]
                self.assertEqual(
                    ingest._best_child(claims, stm_white).move, 'a7a5')

    def test_the_record_of_the_defect(self):
        # The children of 1725116b in the order production returned them.
        children = [_proven('f5f4', 10_000, 1), _proven('e8f8', 10_000, 11),
                    _open('b8c6', 10_000)]
        self.assertEqual(_old_best_child(children, False).move, 'f5f4')
        self.assertEqual(ingest._best_child(children, False).move, 'b8c6')
        self.assertEqual(ingest._best_child(children[:2], False).move, 'e8f8')


class LostPositionTests(TestCase):
    """1725116b, Black to move: every known move loses."""

    def _family(self, name, open_move=True):
        node = _pos(f'{name}-N', 'b', expanded=True, eval_cp=781,
                    best_move='b8c6', nodes_invested=640_000_000)
        # The mate in one first, as production returned it.
        _edge(node, _decided(f'{name}-f5f4', 'w', 1), 'f5f4')
        _edge(node, _decided(f'{name}-a7a5', 'w', 1), 'a7a5')
        _edge(node, _decided(f'{name}-d8c7', 'w', 9), 'd8c7')
        _edge(node, _decided(f'{name}-e8f8', 'w', 11), 'e8f8')
        if open_move:
            # Still open, at the same number: White's best reply there is a
            # proven win, so the move is worth 10,000 without being proven.
            _edge(node, _pos(f'{name}-b8c6', 'w', expanded=True,
                             eval_cp=1504, nodes_invested=ENGINE,
                             backed_eval=10_000, backed_move='g4g5',
                             backed_plies=1, backed_nodes=ENGINE), 'b8c6')
        return node

    def test_the_open_move_is_the_one_it_stands_on(self):
        node = self._family('OPEN')

        ingest.backup_backed_evals([node.key])

        self.assertEqual(_backed(node), (10_000, 'b8c6', 2, ENGINE))
        rows = views._child_moves(node)
        self.assertEqual(rows[0]['uci'], 'b8c6')
        self.assertEqual(views._arrow_move(rows, node), 'b8c6')

    def test_with_every_move_proven_the_longest_resistance(self):
        node = self._family('LOST', open_move=False)

        ingest.backup_backed_evals([node.key])

        self.assertEqual(_backed(node), (10_000, 'e8f8', 1, PROVEN))
        # The arrow and the top row of the table name the same move.
        rows = views._child_moves(node)
        self.assertEqual(rows[0]['uci'], 'e8f8')
        self.assertEqual(views._arrow_move(rows, node), 'e8f8')

    def test_a_won_position_stands_on_the_soonest_mate(self):
        node = _pos('WON-N', 'w', expanded=True, eval_cp=900,
                    nodes_invested=640_000_000)
        _edge(node, _decided('WON-long', 'b', 7), 'd1d8')
        _edge(node, _decided('WON-short', 'b', 3), 'h1h8')
        _edge(node, _pos('WON-open', 'b', expanded=True, eval_cp=2400,
                         nodes_invested=ENGINE, backed_eval=10_000,
                         backed_move='a7a6', backed_plies=4,
                         backed_nodes=ENGINE), 'c1c8')

        ingest.backup_backed_evals([node.key])

        self.assertEqual(_backed(node), (10_000, 'h1h8', 1, PROVEN))
