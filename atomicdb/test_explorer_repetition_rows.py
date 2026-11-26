"""The move table follows the line the visitor came by (owner, 26-Nov-2026).

A community report on a real loop below the Sicilian family: the backup and
the closures handled the repetition correctly, and the explorer did not.

  * A move that lands on the current route was blocked and then passed over
    by the arrow, so the defender was shown a worse defence (castling into
    -1160) instead of the draw that repeating gives him.
  * A move whose own line comes back was shown only when the position
    published exactly 0, so a returning move the backup values at its way
    out (+29) showed the number its position stores, which came round the
    loop (+1141), tied with the real best move.

The rule stands: a repetition along the current route is a draw (0), never a
proof, only display and minimax.  The fix is in the view; backup, closures,
statuses and stored values are untouched.

The fixture is that loop, four plies long, with the positions' real FENs:

  X --g5h6--> Y --f8e7--> P --h6g5--> C --e7f8--> X

P stands on e4f5 at +1141.  On the line through P, h6g5 is worth +29:
Black answers Bf8, and White's best move that does not repeat is O-O.
C's own way out, O-O for Black, is worth +1160 to White.
"""

from unittest import mock

from django.test import Client

from . import ingest, logic, views
from .models import Edge, Position
from .testing import TestCase

ENGINE = 128_000_000
X_FEN = 'r1b1kb1r/pp5p/2np2p1/5qB1/1P1PP3/5Q2/P1P2PPP/R3K2R w KQkq - 0 1'


def _position(fen, **kw):
    return Position.objects.create(key=logic.key_of(fen), fen=fen, **kw)


def _edge(parent, child, uci):
    Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _leaf(parent, uci, value):
    """A searched reply of ``parent``: the way out of the loop."""
    child = _position(logic.apply_move(parent.fen, uci), eval_cp=value,
                      nodes_invested=ENGINE)
    _edge(parent, child, uci)
    return child


class SicilianLoopFixture(TestCase):

    def setUp(self):
        y_fen = logic.apply_move(X_FEN, 'g5h6')
        p_fen = logic.apply_move(y_fen, 'f8e7')
        c_fen = logic.apply_move(p_fen, 'h6g5')
        # The premise, checked and not assumed: Bf8 comes back to X.
        self.assertEqual(logic.key_of(logic.apply_move(c_fen, 'e7f8')),
                         logic.key_of(X_FEN))
        self.x, self.y, self.p, self.c = (
            _position(fen, expanded=True)
            for fen in (X_FEN, y_fen, p_fen, c_fen))
        _edge(self.x, self.y, 'g5h6')
        _edge(self.y, self.p, 'f8e7')
        _edge(self.p, self.c, 'h6g5')
        _edge(self.c, self.x, 'e7f8')
        _leaf(self.p, 'e4f5', 1141)    # P's real best move
        _leaf(self.x, 'e1g1', 29)      # White's way out at X
        _leaf(self.c, 'e8g8', 1160)    # Black's way out at C
        _leaf(self.y, 'f8g7', 1500)    # Black's way out at Y, worse
        keys = [node.key for node in (self.x, self.y, self.p, self.c)]
        for _ in range(4):
            if not ingest.backup_backed_evals(keys):
                break
        for node in (self.x, self.y, self.p, self.c):
            node.refresh_from_db()

    @staticmethod
    def _rows(pos, route=None):
        rows = views._child_moves(
            pos, None if route is None else [node.key for node in route])
        return rows, {row['uci']: row for row in rows}


class BackupPinTests(SicilianLoopFixture):
    """What the backup does with the loop, which the view must match."""

    def test_p_stands_on_e4f5(self):
        self.assertEqual((self.p.backed_eval, self.p.backed_move),
                         (1141, 'e4f5'))

    def test_h6g5_on_the_line_is_worth_its_way_out(self):
        self.assertEqual(ingest.repetition_moves(self.p), {'h6g5': 29})
        self.assertEqual(ingest.line_moves(self.p), {'h6g5': (29, False)})

    def test_the_stored_values_are_round_the_loop(self):
        # C, X and Y each publish P's +1141 through the loop: the number
        # the h6g5 row used to show.
        self.assertEqual([self.c.backed_eval, self.x.backed_eval,
                          self.y.backed_eval], [1141, 1141, 1141])
        self.assertEqual(self.c.backed_move, 'e7f8')


class ReturningRowTests(SicilianLoopFixture):

    def test_the_gate_on_a_published_0_is_gone(self):
        # P publishes +1141, not 0, and still lists its returning move.
        self.assertNotEqual(self.p.backed_eval, 0)
        self.assertEqual(views._repetition_moves(self.p),
                         {'h6g5': (29, False)})

    def test_without_a_route_the_rows_follow_the_backup(self):
        rows, by_uci = self._rows(self.p)
        self.assertEqual(by_uci['h6g5']['score'], 29)
        self.assertTrue(by_uci['h6g5']['returns'])
        self.assertFalse(by_uci['h6g5']['repetition'])
        self.assertEqual(by_uci['h6g5']['line_value'], 29)
        self.assertFalse(by_uci['h6g5']['backed'])
        self.assertEqual(by_uci['e4f5']['score'], 1141)
        self.assertEqual([row['uci'] for row in rows], ['e4f5', 'h6g5'])
        self.assertEqual(views._arrow_move(rows, self.p), self.p.backed_move)

    def test_on_a_route_through_p_the_row_shows_its_way_out(self):
        # Reached from another parent, the line is that parent and P: Bf8
        # does not repeat, and the row is worth White's way out, not 1141.
        other = _position('8/8/8/8/8/8/8/K6k b - - 0 1')
        _edge(other, self.p, 'a1a2')
        rows, by_uci = self._rows(self.p, [other, self.p])
        self.assertEqual(by_uci['h6g5']['score'], 29)
        self.assertTrue(by_uci['h6g5']['returns'])
        self.assertEqual(views._arrow_move(rows, self.p), 'e4f5')

    def test_on_the_route_through_x_the_returning_move_is_a_draw(self):
        # Through X, Bf8 repeats X: Black takes the draw rather than castle.
        rows, by_uci = self._rows(self.p, [self.x, self.y, self.p])
        self.assertEqual(by_uci['h6g5']['score'], 0)
        self.assertTrue(by_uci['h6g5']['repetition'])
        self.assertEqual(views._arrow_move(rows, self.p), 'e4f5')

    def test_stored_values_are_never_touched(self):
        fields = ('backed_eval', 'backed_move', 'backed_plies',
                  'backed_nodes', 'status', 'closure', 'eval_cp')
        before = list(Position.objects.order_by('key').values(*fields))
        self._rows(self.p)
        self._rows(self.c, [self.x, self.y, self.p, self.c])
        Client().get(f'/atomicdb/explore/{self.p.key}/')
        self.assertEqual(
            list(Position.objects.order_by('key').values(*fields)), before)


class DefenderTests(SicilianLoopFixture):
    """C on the route X, Y, P: Black may repeat with Bf8."""

    def route(self):
        return [self.x, self.y, self.p, self.c]

    def test_the_repeating_row_is_worth_a_draw_and_is_the_arrow(self):
        rows, by_uci = self._rows(self.c, self.route())
        self.assertEqual(by_uci['e7f8']['score'], 0)
        self.assertTrue(by_uci['e7f8']['repetition'])
        self.assertEqual(by_uci['e8g8']['score'], -1160)
        self.assertEqual(rows[0]['uci'], 'e7f8')
        self.assertEqual(views._arrow_move(rows, self.c), 'e7f8')

    def test_castling_is_never_the_arrow_while_the_draw_is_better(self):
        rows, _by_uci = self._rows(self.c, self.route())
        for row in rows:
            row['blocked'] = row['key'] == self.x.key   # as explore sets it
        self.assertNotEqual(views._arrow_move(rows, self.c), 'e8g8')

    def test_without_a_route_c_reads_its_stored_line(self):
        rows, by_uci = self._rows(self.c)
        self.assertEqual(by_uci['e7f8']['score'], -1141)
        self.assertFalse(by_uci['e7f8']['repetition'])
        self.assertEqual(views._arrow_move(rows, self.c), 'e7f8')


class AttackerTests(SicilianLoopFixture):
    """X on the route Y, P, C: White's g5h6 re-enters, O-O does not."""

    def test_the_re_entering_move_is_0_and_the_arrow_leaves_the_loop(self):
        rows, by_uci = self._rows(self.x, [self.y, self.p, self.c, self.x])
        self.assertEqual(by_uci['g5h6']['score'], 0)
        self.assertTrue(by_uci['g5h6']['repetition'])
        self.assertEqual(by_uci['e1g1']['score'], 29)
        # X's header stands on g5h6 at +1141; on this line that is a draw.
        self.assertEqual(self.x.backed_move, 'g5h6')
        self.assertEqual(views._arrow_move(rows, self.x), 'e1g1')


class BudgetTests(SicilianLoopFixture):

    def test_out_of_budget_the_row_shows_its_stored_value(self):
        with mock.patch.object(ingest, 'BACKED_CYCLE_MAX_NODES', 0):
            self.assertEqual(ingest.line_moves(self.p), {})
            rows, by_uci = self._rows(self.p)
            response = Client().get(f'/atomicdb/explore/{self.p.key}/')
        self.assertEqual(by_uci['h6g5']['score'], 1141)
        self.assertFalse(by_uci['h6g5']['returns'])
        self.assertEqual(response.status_code, 200)

    def test_out_of_time_the_page_still_renders(self):
        with mock.patch.object(ingest, 'BACKED_CYCLE_MAX_SECONDS', -1):
            response = Client().get(f'/atomicdb/explore/{self.p.key}/')
        self.assertEqual(response.status_code, 200)


class PageTests(SicilianLoopFixture):

    def _page(self, pos, route):
        """The explore page of ``pos`` as reached along ``route``.

        The loop is not reachable from the start position in a fixture, so
        the canonical route is handed to the view as the breadcrumb would.
        """
        line = []
        for parent, child in zip(route, route[1:]):
            uci = Edge.objects.get(parent=parent, child=child).move_uci
            line.append({'uci': uci, 'san': uci, 'key': child.key,
                         'white': parent.fen.split()[1] == 'w'})
        canonical = (route[0], line, [step['uci'] for step in line])
        with mock.patch.object(views, '_canonical_route',
                               return_value=canonical):
            return Client().get(f'/atomicdb/explore/{pos.key}/')

    def test_the_defender_page_points_at_the_draw(self):
        response = self._page(self.c, [self.x, self.y, self.p, self.c])
        self.assertEqual(response.status_code, 200)
        row = next(m for m in response.context['moves']
                   if m['uci'] == 'e7f8')
        self.assertTrue(row['blocked'])
        self.assertIsNone(row['url'])
        self.assertEqual(row['score'], 0)
        self.assertEqual(response.context['best_move'], 'e7f8')
        self.assertContains(response, 'repeat = draw')
        # The header quotes -1141, the best row 0: one line says why.
        self.assertTrue(response.context['line_note'])
        self.assertContains(response, 'follow the line you came by')

    def test_the_returning_row_wears_its_chip(self):
        response = Client().get(f'/atomicdb/explore/{self.p.key}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="backed-mark line-returns"')
        self.assertEqual(response.context['best_move'], 'e4f5')
        # No route, no note: the rows are the backup's own reading.
        self.assertFalse(response.context['line_note'])

    def test_the_api_adds_the_two_flags(self):
        block = views._query_block(self.p)
        row = next(m for m in block['moves'] if m['uci'] == 'h6g5')
        self.assertEqual(row['score'], 29)
        self.assertTrue(row['returns'])
        self.assertFalse(row['repetition'])
        self.assertEqual(block['best_move'], 'e4f5')


class ReturningLineChipTests(SicilianLoopFixture):
    """The chip says WHERE the line returns (report of 26-Nov-2026).

    On P, Bg5 is no repetition: it leads to C, and the game is back on P
    only after …Bf8 Bh6 Be7.  A bare "returns" read as if Bg5 itself
    repeated ("one ply off").  The value is White's way out at X, O-O.
    """

    def test_line_moves_reports_the_path_and_the_exit(self):
        detail = {}
        self.assertEqual(ingest.line_moves(self.p, detail=detail),
                         {'h6g5': (29, False)})
        self.assertEqual(detail['h6g5']['path'],
                         [(self.c.key, 'e7f8'), (self.x.key, 'g5h6'),
                          (self.y.key, 'f8e7')])
        # The second position of the line, X, leaves with O-O at +29.
        self.assertEqual(detail['h6g5']['exit'], (1, 'e1g1', 29))

    def test_the_row_carries_the_san_line_and_the_exit(self):
        _rows, by_uci = self._rows(self.p)
        returning = by_uci['h6g5']['returning']
        self.assertEqual(returning['ucis'], ['e7f8', 'g5h6', 'f8e7'])
        self.assertEqual(returning['full_san'], '…Bf8 Bh6 Be7')
        self.assertEqual(returning['exit']['san'], 'O-O')
        self.assertEqual(returning['exit']['side'], 'White')
        self.assertEqual(returning['exit']['pawns'], '+0.29')
        self.assertIsNone(by_uci['e4f5']['returning'])

    def test_the_bg5_chip_shows_the_line_and_the_tooltip_the_exit(self):
        response = Client().get(f'/atomicdb/explore/{self.p.key}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'after …<span data-uci="e7f8 g5h6 f8e7">Bf8 Bh6 Be7'
                      '</span>')
        self.assertContains(response, 'in 3</span>')
        self.assertContains(
            response, "White's best way to avoid going back is O-O "
                      "(+0.29 for White)")
        self.assertContains(
            response, 'the line comes back to a position already played '
                      'after …Bf8 Bh6 Be7')

    def test_the_api_gives_the_line_and_the_exit(self):
        block = views._query_block(self.p)
        row = next(m for m in block['moves'] if m['uci'] == 'h6g5')
        self.assertEqual(row['returns_line'], ['e7f8', 'g5h6', 'f8e7'])
        self.assertEqual(row['returns_exit'],
                         {'index': 1, 'uci': 'e1g1', 'score': 29})
        best = next(m for m in block['moves'] if m['uci'] == 'e4f5')
        self.assertIsNone(best['returns_line'])
        self.assertIsNone(best['returns_exit'])

    def test_one_query_names_the_whole_line(self):
        detail = {}
        ingest.line_moves(self.p, detail=detail)
        with self.assertNumQueries(1):
            views._returning_lines(self.p, detail)
        with self.assertNumQueries(0):
            self.assertEqual(views._returning_lines(self.p, {}), {})

    def test_a_long_line_is_cut_in_the_middle(self):
        detail = {}
        ingest.line_moves(self.p, detail=detail)
        path = detail['h6g5']['path']
        # Eight plies round the same positions: head, ellipsis, tail.
        detail['h6g5']['path'] = (path * 3)[:8]
        detail['h6g5']['exit'] = None
        named = views._returning_lines(self.p, detail)['h6g5']
        self.assertEqual(named['plies'], 8)
        self.assertEqual(named['short_san'], 'Bf8 Bh6 Be7 … Bf8 Bh6')
        self.assertEqual(named['full_san'],
                         '…Bf8 Bh6 Be7 Bf8 Bh6 Be7 Bf8 Bh6')
        self.assertIsNone(named['exit'])

    def test_out_of_budget_the_page_still_renders_without_a_line(self):
        with mock.patch.object(ingest, 'BACKED_CYCLE_MAX_NODES', 0):
            detail = {}
            self.assertEqual(ingest.line_moves(self.p, detail=detail), {})
            self.assertEqual(detail, {})
            response = Client().get(f'/atomicdb/explore/{self.p.key}/')
            block = views._query_block(self.p)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'line-returns')
        row = next(m for m in block['moves'] if m['uci'] == 'h6g5')
        self.assertIsNone(row['returns_line'])
        self.assertIsNone(row['returns_exit'])

    def test_an_exit_the_walk_cannot_name_keeps_the_old_tooltip(self):
        with mock.patch.object(ingest, '_line_exit', return_value=None):
            response = Client().get(f'/atomicdb/explore/{self.p.key}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Bf8 Bh6 Be7</span>')
        self.assertContains(response, 'It is worth the best way out of '
                                      'that loop: 29 here')
