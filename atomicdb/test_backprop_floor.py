"""A move nobody has searched does not beat the lines of the position above.

Live on 5-Oct (Eclipsia).  After 1. e3 d6 2. Nc3 the position, Black to move,
had 648M nodes of its own, two current lines, and said 743 with ...e5.  Its
header read 363 through ...Bh3: a move outside both lines, which nobody had
searched, whose position knew one of White's thirty replies, valued from an
8M search further down.  That 363 is what White gets AT LEAST after ...Bh3, a
floor; the position above read it as what ...Bh3 is worth, against its own
search, which had rated every move outside its lines no better than the last
of them.  Searched at 128M, ...Bh3 came back at 1491.

Values are stored from White's side, so for Black to move lower is better.
"""

import hashlib

from . import ingest, views
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


# Two current lines: the engine's word that no move outside them is better
# for the mover than the second one.
_SHOWCASE = [{'move': 'e7e5', 'eval_cp': 743}, {'move': 'e7e6', 'eval_cp': 800}]


def _family(name, floor_move='c8h3', reply=363, searched=776,
            showcase=_SHOWCASE):
    """Black to move with a 648M search of its own, and three of its moves.

    ``floor_move`` was never searched and knows one reply, which an engine
    did search; ``e7e5`` has a search of its own; ``a7a6`` is unopened.
    """
    node = _pos(f'{name}-N', 'b', eval_cp=743, nodes_invested=648_000_000,
                expanded=True, last_analysis=showcase)
    floor = _pos(f'{name}-floor', 'w')
    _edge(node, floor, floor_move)
    _edge(floor, _pos(f'{name}-reply', 'b', eval_cp=reply,
                      nodes_invested=8_003_625), 'c3e4')
    if floor_move != 'e7e5':
        _edge(node, _pos(f'{name}-searched', 'w', eval_cp=searched,
                         nodes_invested=136_000_000), 'e7e5')
    _edge(node, _pos(f'{name}-unopened', 'w'), 'a7a6')
    # The ascent starts where the news is, as it does after an ingest: the
    # unsearched move borrows its reply's number and wakes the node above.
    ingest.backup_backed_evals([floor.key])
    node.refresh_from_db()
    floor.refresh_from_db()
    return node, floor


class FloorTests(TestCase):

    def test_a_floor_outside_the_lines_does_not_beat_them(self):
        node, floor = _family('OUTSIDE')

        # The unsearched move still says what it knows on its own page...
        self.assertEqual((floor.backed_eval, floor.backed_move), (363, 'c3e4'))
        self.assertTrue(ingest.is_floor(floor.status, floor.nodes_invested,
                                        floor.backed_eval))
        # ...and the position above stands on the move an engine searched.
        self.assertEqual((node.backed_eval, node.backed_move), (776, 'e7e5'))
        self.assertEqual(node.backed_nodes, 136_000_000)

    def test_the_search_that_would_settle_it_is_bought(self):
        node, floor = _family('BOUGHT')

        # At the rung a visitor's click buys, not at the 648M of the node: a
        # search of the move itself settles it whatever its size.
        task = AnalysisTask.objects.get(position=floor)
        self.assertEqual(task.budget_nodes, 128_000_000)

    def test_the_bound_is_the_last_line_and_not_the_own_number(self):
        # 760 does not improve on Black's own 743, but it does beat the 800
        # the engine put on every move outside its lines, and it would have
        # been Black's best row: the certificate is what it contradicts.
        node, floor = _family('BOUND', reply=760)

        self.assertEqual((node.backed_eval, node.backed_move), (776, 'e7e5'))

    def test_a_floor_that_respects_the_bound_still_travels(self):
        # White gets at least 900 after ...Bh3: worse for Black than the last
        # line, so nothing is contradicted, and with every other move worse
        # still it is Black's best known row.
        node, floor = _family('RESPECTS', reply=900, searched=1200)

        self.assertEqual((node.backed_eval, node.backed_move), (900, 'c8h3'))
        self.assertFalse(ingest.refuses_floor(node, floor, 'c8h3'))

    def test_a_move_that_heads_a_line_refines_its_line(self):
        # The reported case of 28-Jul, with a showcase on the table: the
        # engine named this move, and a search further down its line moves
        # the header whether or not anybody searched the move itself.
        node, floor = _family('LINE', floor_move='e7e5', reply=600)

        self.assertEqual((node.backed_eval, node.backed_move), (600, 'e7e5'))
        self.assertFalse(ingest.refuses_floor(node, floor, 'e7e5'))

    def test_without_two_lines_there_is_no_certificate_to_hold(self):
        # A single line bounds nothing (see _showcase_bound), so the rule of
        # 8-Aug stands alone: a favourable value with engine nodes behind it
        # displaces at once, and the depth that would confirm it is bought.
        node, floor = _family('SINGLE', showcase=[{'move': 'e7e5',
                                                   'eval_cp': 743}])

        self.assertEqual((node.backed_eval, node.backed_move), (363, 'c8h3'))
        self.assertTrue(AnalysisTask.objects.filter(position=floor).exists())

    def test_once_the_move_is_searched_its_value_backs_up(self):
        # A child an engine searched backs up against any parent: 128M moves
        # 648M, lines or no lines.  What was refused was the absence of a
        # search, never the size of one.
        node, floor = _family('SEARCHED')
        Position.objects.filter(key=floor.key).update(
            eval_cp=500, nodes_invested=128_000_000, backed_eval=None,
            backed_move=None, backed_plies=0, backed_nodes=0)

        ingest.backup_backed_evals([node.key])

        node.refresh_from_db()
        self.assertEqual((node.backed_eval, node.backed_move), (500, 'c8h3'))

    def test_only_a_borrowed_number_with_no_search_is_a_floor(self):
        self.assertTrue(ingest.is_floor('UNKNOWN', 0, 363))
        # A search of its own, however small, makes the number a value.
        self.assertFalse(ingest.is_floor('UNKNOWN', 8_003_625, 363))
        # A seeded claim borrowed nothing: the claim veto deals with it.
        self.assertFalse(ingest.is_floor('UNKNOWN', 0, None))
        # A proven position is exact whoever searched it.
        self.assertFalse(ingest.is_floor('WHITE_WIN', 0, 10000))


class FloorRowTests(TestCase):
    """The table and the arrow tell the same story as the header."""

    def test_the_row_keeps_its_place_and_says_what_it_is(self):
        node, floor = _family('ROW')
        rows = views._child_moves(node)
        by_move = {row['uci']: row for row in rows}

        # Value and nothing else decides the order: -363 sits above -776.
        self.assertEqual(rows[0]['uci'], 'c8h3')
        self.assertEqual(rows[0]['score'], -363)
        self.assertTrue(by_move['c8h3']['floor'])
        self.assertIn('not what the move is worth', by_move['c8h3']['claim'])
        self.assertFalse(by_move['e7e5']['floor'])

    def test_the_arrow_points_at_the_move_the_header_stands_on(self):
        node, floor = _family('ARROW')

        self.assertEqual(
            views._arrow_move(views._child_moves(node), node), 'e7e5')

    def test_a_floor_the_position_does_take_is_an_ordinary_row(self):
        node, floor = _family('TAKEN', reply=900, searched=1200)
        rows = views._child_moves(node)
        by_move = {row['uci']: row for row in rows}

        self.assertFalse(by_move['c8h3']['floor'])
        self.assertEqual(views._arrow_move(rows, node), 'c8h3')


class FloorAndTheBulkButtonTests(TestCase):
    """Two predicates about "nobody searched this", and where they part.

    The floor rule asks whether the MOVE was searched (``ingest.is_floor``:
    no nodes of its own).  The bulk button asks whether ANYTHING was searched
    there (``ingest.is_unexplored``: no nodes of its own and no search weight
    in its backing).  A number a visitor walked up a line satisfies both.  A
    floor that stands on a search further down satisfies only the first: the
    button leaves it out, as it leaves out every reply backed by a real
    search, and the rule is what buys the search of the move.

    Neither may leave such a move without its search, and together they must
    not buy it twice.
    """

    def test_a_floor_standing_on_a_search_is_bought_by_the_rule(self):
        # The reported shape: the unsearched move knows one reply, which an
        # engine searched at 8M.  The button does not count it...
        node, floor = _family('ENGINE')

        self.assertTrue(ingest.refuses_floor(node, floor, 'c8h3'))
        self.assertFalse(ingest.is_unexplored(floor))
        self.assertNotIn(floor.key,
                         [c.key for c in ingest.unexplored_children(node)])
        # ...and it is not left without a search: the rule queued one, at the
        # rung a click buys, as automatic work.
        task = AnalysisTask.objects.get(position=floor)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertEqual(task.source, AnalysisTask.Source.FILL)
        self.assertEqual(task.arm, ingest.QUALITY_ARM)

        # A click on the button then buys the move nobody opened and leaves
        # that task as it is.
        queued = ingest.enqueue_unexplored_children(node, requested_by='w')

        self.assertEqual(queued, 1)
        task.refresh_from_db()
        self.assertEqual(task.source, AnalysisTask.Source.FILL)
        self.assertEqual(AnalysisTask.objects.filter(position=floor).count(),
                         1)

    def test_the_row_says_both_things_about_it(self):
        node, floor = _family('ENGINE-ROW')

        row = next(m for m in views._child_moves(node)
                   if m['uci'] == 'c8h3')

        # Not "unexplored": a search stands below it.  And a floor all the
        # same: the number is muted and the reason is in the tooltip.
        self.assertFalse(row['unexplored'])
        self.assertTrue(row['floor'])
        self.assertEqual(row['claim'], views._FLOOR_NOTE)

    def _walked_family(self, name):
        """Like ``_family``, with no engine node anywhere below the move.

        The reply carries a seeded number, so what the move borrows is a
        walked one: zero nodes of its own and zero weight in its backing.
        """
        node = _pos(f'{name}-N', 'b', eval_cp=743, nodes_invested=648_000_000,
                    expanded=True, last_analysis=_SHOWCASE)
        walked = _pos(f'{name}-walked', 'w')
        _edge(node, walked, 'c8h3')
        _edge(walked, _pos(f'{name}-reply', 'b', eval_cp=363), 'c3e4')
        _edge(node, _pos(f'{name}-searched', 'w', eval_cp=776,
                         nodes_invested=136_000_000), 'e7e5')
        ingest.backup_backed_evals([walked.key])
        node.refresh_from_db()
        walked.refresh_from_db()
        return node, walked

    def test_a_walked_floor_is_refused_and_counted_and_bought_once(self):
        node, walked = self._walked_family('WALKED')

        # Both predicates hold, and the position stands on the searched move.
        self.assertEqual((walked.backed_eval, walked.backed_nodes), (363, 0))
        self.assertTrue(ingest.is_unexplored(walked))
        self.assertTrue(ingest.refuses_floor(node, walked, 'c8h3'))
        self.assertEqual((node.backed_eval, node.backed_move), (776, 'e7e5'))
        # The rule already bought its search, at the first request rung and
        # not at the 648M of the position above.
        task = AnalysisTask.objects.get(position=walked)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertEqual(task.source, AnalysisTask.Source.FILL)

        # The button counts the move, finds that task and makes it the
        # visitor's request.  One search, not two.
        queued = ingest.enqueue_unexplored_children(node, requested_by='w')

        self.assertEqual(queued, 1)
        task.refresh_from_db()
        self.assertEqual(task.source, AnalysisTask.Source.USER)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertEqual(AnalysisTask.objects.filter(position=walked).count(),
                         1)

    def test_the_row_of_a_walked_floor_is_unexplored_and_muted(self):
        node, walked = self._walked_family('WALKED-ROW')
        rows = views._child_moves(node)
        row = next(m for m in rows if m['uci'] == 'c8h3')

        self.assertTrue(row['unexplored'])
        self.assertTrue(row['walked'])
        self.assertTrue(row['floor'])
        # One tooltip, and it is the floor's: it says why the position does
        # not use the number, which the walked note alone would not.
        self.assertEqual(row['claim'], views._FLOOR_NOTE)
        # The arrow goes to the move the header stands on.
        self.assertEqual(views._arrow_move(rows, node), 'e7e5')
