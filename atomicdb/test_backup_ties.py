"""Ties between moves of equal value, and the claim that froze a header.

Two defects of the backup, found on 26-Oct-2026 behind three reports.

B. The tie-break (see the block above ingest._best_child).  A proven child
counted as a flat 10,000 with the heaviest support there is and no distance,
so among moves worth the same a proven loss beat every move still open, and
among proven moves the first edge the database returned won.  At 1725116b
(Black to move, every known move lost) the position stood on ...f4, a mate
in one, while ...Nc6 was still open at the same number.  What no key tells
apart now goes to the position's own search move, then to the move's name.

G2. The claim veto (see ingest._backed_for).  A favourable number nobody
searched does not displace the position's own search, and that stays.  But
when such a number topped the table and no searched move beat the own
search, the position returned that search outright, so its header stayed at
a number its own table refuted: d2501a13 held 1282 over ...Nc5 at 1285 and
...Bg7 at 1295; e214fcc0 held 1304 over ...g5 at 1492.  The refused number
is now set aside and the other moves decide, except when it sits on the own
search's own move (f8481e63, 2bd63fd6) or is a repetition: then the own
number stays, as before, unless a searched move beats it.
"""

import hashlib
import types

from . import ingest, views
from .models import AnalysisTask, Edge, Position
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


def _showcase(*lines):
    """Current lines of a pass, ``(move, eval_cp)`` in the engine's order."""
    return [{'move': move, 'eval_cp': value, 'pv': [move]}
            for move, value in lines]


def _backed(row):
    row.refresh_from_db()
    return row.backed_eval, row.backed_move, row.backed_plies, row.backed_nodes


# Children and rows for _best_child and _backed_for on their own.

def _proven(move, value, mate_in):
    return ingest._ChildValue(move, value, PROVEN, 0, exact=True,
                              mate_in=mate_in)


def _open(move, value, quality=ENGINE, plies=1):
    return ingest._ChildValue(move, value, quality, plies)


def _claim(move, value):
    return ingest._ChildValue(move, value, 0, 0)


def _repetition(move):
    child = ingest._ChildValue(move, 1_000, DEEP, 0)
    child.cycles = True       # _backed_for scores it 0, with no weight
    return child


def _unopened(move):
    return ingest._ChildValue(move, None, 0, 0)


def _row(stm, own, nodes=DEEP, best='own'):
    return types.SimpleNamespace(
        key='row', fen=FEN.format(stm), status='UNKNOWN', expanded=True,
        eval_cp=own, nodes_invested=nodes, best_move=best)


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

    def test_a_full_tie_goes_to_the_own_search_move_first(self):
        for stm_white in (True, False):
            moves = ['f5f4', 'a7a5', 'e8f8']
            for order in (moves, moves[::-1]):
                children = [_proven(move, 10_000, 3) for move in order]
                self.assertEqual(ingest._best_child(
                    children, stm_white, own_move='f5f4').move, 'f5f4')
                claims = [_open(move, 120, 0, plies=0) for move in order]
                self.assertEqual(ingest._best_child(
                    claims, stm_white, own_move='e8f8').move, 'e8f8')
                # An own move that is not among them changes nothing.
                self.assertEqual(ingest._best_child(
                    claims, stm_white, own_move='h7h6').move, 'a7a5')

    def test_the_own_move_only_breaks_full_ties(self):
        # Every key above it still decides first: the proof, the support,
        # the chain.
        for stm_white, lost in ((True, -10_000), (False, 10_000)):
            loss, still = _proven('loss', lost, 1), _open('open', lost)
            self.assertIs(ingest._best_child([loss, still], stm_white,
                                             own_move='loss'), still)
            heavy, light = _open('heavy', 50, DEEP), _open('light', 50, ENGINE)
            self.assertIs(ingest._best_child([light, heavy], stm_white,
                                             own_move='light'), heavy)
            short, deep = (_open('short', 50, plies=1),
                           _open('deep', 50, plies=4))
            self.assertIs(ingest._best_child([deep, short], stm_white,
                                             own_move='deep'), short)

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

    def test_a_full_tie_stands_on_the_engines_move(self):
        # Black to move, own 1324 through ...Bf8, whose position carries
        # the same 1324 as ...Bc8 (11c9e029, 26-Oct): nothing tells the two
        # apart but the engine's choice, and the arrow already names it.
        node = _pos('FULL-N', 'b', expanded=True, eval_cp=1324,
                    best_move='g7f8', nodes_invested=ENGINE)
        _edge(node, _pos('FULL-a6c8', 'w', eval_cp=1324), 'a6c8')
        _edge(node, _pos('FULL-g7f8', 'w', eval_cp=1324), 'g7f8')
        _edge(node, _pos('FULL-h7h6', 'w', eval_cp=1500,
                         nodes_invested=ENGINE), 'h7h6')

        ingest.backup_backed_evals([node.key])

        self.assertEqual(_backed(node), (1324, 'g7f8', 1, 0))
        rows = views._child_moves(node)
        self.assertEqual(views._arrow_move(rows, node), 'g7f8')


class VetoedClaimTests(TestCase):
    """G2 on the reported positions, as production stores them."""

    def test_d2501a13_stands_on_its_best_searched_move(self):
        node = _pos('D250-N', 'b', expanded=True, eval_cp=1282,
                    best_move='f8g7', nodes_invested=640_001_183,
                    last_analysis=_showcase(('f8g7', 1282), ('h8g8', 1306)))
        claim = _pos('D250-a8b8', 'w', eval_cp=1216)
        _edge(node, claim, 'a8b8')
        _edge(node, _pos('D250-h8g8', 'w', eval_cp=1306), 'h8g8')
        _edge(node, _pos('D250-a6c5', 'w', eval_cp=1285,
                         nodes_invested=128_002_746, backed_eval=1285,
                         backed_move='c1e3', backed_plies=1), 'a6c5')
        _edge(node, _pos('D250-f8g7', 'w', eval_cp=1295,
                         nodes_invested=512_002_046, backed_eval=1295,
                         backed_move='h4h5', backed_plies=1), 'f8g7')
        _edge(node, _pos('D250-d8c8', 'w', eval_cp=1576,
                         nodes_invested=512_008_836, backed_eval=1576,
                         backed_move='c1e3', backed_plies=1), 'd8c8')
        for move in ('a6b4', 'b6b5', 'c6c5'):
            _edge(node, _pos(f'D250-{move}', 'w'), move)

        ingest.backup_backed_evals([node.key])

        # It was (1282, None, 0, 640001183): the own number, frozen.
        self.assertEqual(_backed(node), (1285, 'a6c5', 2, 128_002_746))
        self.assertEqual(views._arrow_move(views._child_moves(node), node),
                         'a6c5')
        # The claim is still refused, and still bought.
        self.assertTrue(AnalysisTask.objects.filter(position=claim).exists())

    def test_e214fcc0_is_no_longer_frozen_at_its_own_search(self):
        node = _pos('E214-N', 'b', expanded=True, eval_cp=1304,
                    best_move='g6g5', nodes_invested=128_001_325,
                    last_analysis=_showcase(
                        ('g6g5', 1304), ('d5d4', 1353), ('b4e7', 1413),
                        ('b4a5', 1435), ('b8d7', 1442)))
        _edge(node, _pos('E214-b8d7', 'w', eval_cp=1129), 'b8d7')
        _edge(node, _pos('E214-b4e7', 'w', eval_cp=1413), 'b4e7')
        _edge(node, _pos('E214-b4a5', 'w', eval_cp=1435), 'b4a5')
        _edge(node, _pos('E214-g6g5', 'w', eval_cp=1315,
                         nodes_invested=128_006_971, backed_eval=1492,
                         backed_move='f1g1', backed_plies=9,
                         backed_nodes=128_009_182), 'g6g5')
        _edge(node, _pos('E214-d5d4', 'w', eval_cp=1536,
                         nodes_invested=128_002_263, backed_eval=1536,
                         backed_move='h5g6', backed_plies=1), 'd5d4')
        for move in ('a7a5', 'a7a6', 'h7h6'):
            _edge(node, _pos(f'E214-{move}', 'w'), move)

        ingest.backup_backed_evals([node.key])

        # It was (1304, None, 0, 128001325) while ...g5 read 1492.  Without
        # ...Nd7 the table's best is ...Be7, itself a seed worse than the
        # own number, which the veto has never refused (left as it is).
        self.assertEqual(_backed(node), (1413, 'b4e7', 1, 0))

    def test_c8b0b2e2_falls_back_to_its_second_line(self):
        # Found by the sampling of 26-Oct: the own move ...Kd8 was searched
        # since at 1260, and ...Kd7 carries 911 from another line.
        node = _pos('C8B0-N', 'b', expanded=True, eval_cp=1006,
                    best_move='c8d8', nodes_invested=128_002_803,
                    last_analysis=_showcase(
                        ('c8b7', 1049), ('f8d6', 1125), ('b6b5', 1129),
                        ('c8d7', 1148), ('c8d8', 1006)))
        _edge(node, _pos('C8B0-b6b5', 'w', eval_cp=1129), 'b6b5')
        _edge(node, _pos('C8B0-c8b7', 'w', eval_cp=1250,
                         nodes_invested=640_004_511, backed_eval=1250,
                         backed_move='f2g1', backed_plies=1), 'c8b7')
        _edge(node, _pos('C8B0-c8d7', 'w', eval_cp=911), 'c8d7')
        _edge(node, _pos('C8B0-c8d8', 'w', eval_cp=1134,
                         nodes_invested=128_004_009, backed_eval=1260,
                         backed_move='h1h3', backed_plies=2,
                         backed_nodes=512_007_237), 'c8d8')
        _edge(node, _pos('C8B0-f8d6', 'w', eval_cp=1125), 'f8d6')
        for move in ('a7a6', 'h6h7'):
            _edge(node, _pos(f'C8B0-{move}', 'w'), move)

        ingest.backup_backed_evals([node.key])

        self.assertEqual(_backed(node), (1125, 'f8d6', 1, 0))


class OwnMoveTests(TestCase):
    """A claim on the own search's own move keeps the own number."""

    def test_f8481e63_is_not_turned_into_a_loss(self):
        # Black to move, own 8M search -118 through ...d6, whose position
        # carries -129 from another line; every other known move is mated.
        # Setting ...d6 aside would publish the position lost.
        node = _pos('F848-N', 'b', expanded=True, eval_cp=-118,
                    best_move='d7d6', nodes_invested=8_005_586,
                    last_analysis=_showcase(
                        ('d7d6', -118), ('c6c5', 9995), ('a7a6', 9999),
                        ('b7b6', 9999), ('e7e6', 9999)))
        _edge(node, _pos('F848-d7d6', 'w', eval_cp=-129), 'd7d6')
        for move, mate in (('c6c5', 9), ('a7a6', 1), ('b7b6', 1),
                           ('e7e6', 1)):
            _edge(node, _decided(f'F848-{move}', 'w', mate), move)
        for move in ('g7g6', 'h6g4', 'd8a5'):
            _edge(node, _pos(f'F848-{move}', 'w'), move)

        ingest.backup_backed_evals([node.key])

        self.assertEqual(_backed(node), (-118, None, 0, 8_005_586))

    def test_2bd63fd6_keeps_its_best_line(self):
        # White to move, own 1137 through h6, whose position carries 1285
        # from another line: the second line, 873, is no reason to drop.
        node = _pos('2BD6-N', 'w', expanded=True, eval_cp=1137,
                    best_move='h5h6', nodes_invested=128_000_829,
                    last_analysis=_showcase(
                        ('h5h6', 1137), ('h2f2', 873), ('e4e5', 866),
                        ('d5d6', 831), ('f6f7', 831)))
        for move, value in (('h5h6', 1285), ('h2f2', 873), ('e4e5', 866),
                            ('d5d6', 831), ('f6f7', 831)):
            _edge(node, _pos(f'2BD6-{move}', 'b', eval_cp=value), move)
        for move in ('a2a3', 'c1c2'):
            _edge(node, _pos(f'2BD6-{move}', 'b'), move)

        ingest.backup_backed_evals([node.key])

        self.assertEqual(_backed(node), (1137, None, 0, 128_000_829))

    def test_a_searched_move_that_beats_it_still_wins(self):
        out = ingest._backed_for(
            _row('b', 1282, best='f8g7'),
            [_claim('f8g7', 1216), _open('a6c5', 1250), _open('d8c8', 1576),
             _unopened('a6b4')], bound=1306)
        self.assertEqual(out, (1250, 'a6c5', 2, ENGINE))

    def test_the_own_move_searched_is_no_claim(self):
        out = ingest._backed_for(
            _row('b', 1282, best='f8g7'),
            [_claim('a8b8', 1216), _open('f8g7', 1295), _open('a6c5', 1285),
             _unopened('a6b4')], bound=1306)
        self.assertEqual(out, (1285, 'a6c5', 2, ENGINE))


class VetoGuardTests(TestCase):
    """What the veto still does, unchanged by G2."""

    def test_without_a_bound_the_own_number_still_stands(self):
        # Partial coverage and no certificate: the directional guard keeps
        # the own number unless a move beats it, as before.
        picks, asked = [], []
        out = ingest._backed_for(
            _row('b', 1282), [_claim('a8b8', 1216), _open('f8g7', 1295),
                              _unopened('a6b4')], asked, cycle_picks=picks)
        self.assertEqual(out, (1282, None, 0, DEEP))
        self.assertEqual(asked, [('a8b8', 'row', DEEP)])

    def test_a_searched_move_that_beats_the_own_number_still_wins(self):
        out = ingest._backed_for(
            _row('b', 1282), [_claim('a8b8', 1216), _open('a6c5', 1250),
                              _open('f8g7', 1295), _unopened('a6b4')],
            bound=1306)
        self.assertEqual(out, (1250, 'a6c5', 2, ENGINE))

    def test_with_nothing_but_claims_the_own_number_stands(self):
        out = ingest._backed_for(
            _row('b', 1282), [_claim('a8b8', 1216), _claim('h8g8', 1100),
                              _unopened('a6b4')], bound=1306)
        self.assertEqual(out, (1282, None, 0, DEEP))

    def test_a_repetition_that_beats_the_own_number_keeps_it(self):
        # White to move at -50 with a repetition (0) and a searched move at
        # -80: the repetition is no claim, the node keeps its own number.
        for children in ([_repetition('d6f6'), _open('d6h6', -80)],
                         [_claim('c2c4', 300), _repetition('d6f6'),
                          _open('d6h6', -80)]):
            out = ingest._backed_for(_row('w', -50),
                                     children + [_unopened('a2a3')],
                                     bound=-200)
            self.assertEqual(out, (-50, None, 0, DEEP))

    def test_a_claim_hiding_a_repetition_hands_over_its_question(self):
        # The claim is set aside, and what decides is what would decide
        # without it, a repetition no better than the own number included:
        # same value, same move, same question bought.
        def rest():
            return [_repetition('d6f6'), _open('d6h6', -80),
                    _unopened('a2a3')]

        picks, alone = [], []
        out = ingest._backed_for(_row('w', 50), [_claim('c2c4', 300)] + rest(),
                                 bound=-200, cycle_picks=picks)
        without = ingest._backed_for(_row('w', 50), rest(), bound=-200,
                                     cycle_picks=alone)
        self.assertEqual(out, without)
        self.assertEqual(out, (0, 'd6f6', 1, 0))
        self.assertEqual(picks, alone)
        self.assertEqual(picks, [('d6f6', 'row', DEEP)])

    @staticmethod
    def _table(first, searched):
        return [first, _open('a6c5', searched[0]), _open('f8g7', searched[1]),
                _unopened('a6b4')]

    def test_set_aside_is_the_same_as_absent(self):
        # Whatever the table, a refused claim off the own move weighs as
        # much as a move nobody opened, bound or no bound.
        for bound in (None, 1306):
            for searched in ((1250, 1295), (1285, 1295), (1300, 1400)):
                out = ingest._backed_for(
                    _row('b', 1282),
                    self._table(_claim('a8b8', 1216), searched), bound=bound)
                self.assertEqual(out, ingest._backed_for(
                    _row('b', 1282),
                    self._table(_unopened('a8b8'), searched), bound=bound))
