"""The depth column of the move table: how deep each move has been searched.

BManBJ, 29-Oct-2026 in #suggestions: "instead of showing the number of
visits, it would be more useful to see with which node budget that position
was analyzed with. Especially with now being able to multi select moves from
a position I don't want to accidentally queue something for 2B when on its
first visit it was analyzed at 512M if I'm just trying to update positions to
512M".  The last column of the explorer's move table was the visit count; it
now shows the budget of the deepest search each move's position has had
(``views._with_search_depth``).

What is pinned here:

  * a move whose position was searched shows its deepest search, named as
    the depth slider names a rung, and one no search has reached shows a
    dash;
  * only a search that ran counts: an absorbed row bought its rung without
    anybody searching, and it moves the column no more than it moves a
    request with ``only_if_missing``;
  * the visit count stays, in the cell's tooltip;
  * an off-tree row, which has no position yet, keeps its bare dash;
  * the query API carries the same number as ``searched_nodes``, beside
    ``visits``;
  * the whole table costs one statement, whatever its width;
  * it is the line ``ingest.searched_budget`` draws, the one a chosen depth
    in the multi-select is held to (test_multi_request, ``TargetDepthTests``).
"""

import re
from contextlib import ExitStack
from urllib.parse import urlencode

from django.conf import settings
from django.db import connections
from django.test import Client
from django.test.utils import CaptureQueriesContext

from . import ingest, logic, views
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


LADDER = ingest.REQUEST_BUDGET_LADDER
# The rows of the table, read the way the other tests read them.
ROW = re.compile(r'<tr><td><span class="chip.*?</tr>', re.S)
# A cell, its attributes and its content, whatever order the HTML minifier
# leaves the attributes in.
CELL = re.compile(r'<td\b([^>]*)>(.*?)</td>', re.S)


def _root():
    return ingest.get_or_create_position(logic.start_fen())


def _child(parent, uci):
    """The position ``uci`` leads to, made a row of ``parent``'s table."""
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


def _ran(position, budget):
    """A search of ``budget`` that ran on ``position``, as a worker leaves
    it."""
    position.refresh_from_db()
    AnalysisTask.objects.create(
        position=position, generation=position.visits, budget_nodes=budget,
        nodes_searched=budget, state=AnalysisTask.TState.COMPLETED,
        source=AnalysisTask.Source.USER, machine='box0')
    Position.objects.filter(pk=position.pk).update(visits=position.visits + 1)


def _absorbed(position, budget):
    """A rung of ``budget`` bought and never searched.

    COMPLETED with no nodes and no machine (``ingest.absorb_tasks``): the row
    the ladder counts as a spent rung and the column must not count as a
    search.
    """
    position.refresh_from_db()
    return AnalysisTask.objects.create(
        position=position, generation=position.visits, budget_nodes=budget,
        state=AnalysisTask.TState.COMPLETED, source=AnalysisTask.Source.AUTO)


def _depth_cell(body, uci):
    """``(attributes, content)`` of the last cell of ``uci``'s row."""
    row = next(row for row in ROW.findall(body)
               if f'<strong>{uci}</strong>' in row)
    return CELL.findall(row)[-1]


class DepthColumnTests(TestCase):
    """One row per case, on the table of the start position."""

    def setUp(self):
        self.root = _root()
        # 1.e4: searched at 128M and then at 512M.
        self.deep = _child(self.root, 'e2e4')
        _ran(self.deep, LADDER[0])
        _ran(self.deep, LADDER[1])
        # 1.d4: a row nobody has searched.
        self.blank = _child(self.root, 'd2d4')
        # 1.Nf3: a 512M rung absorbed, and nothing that ran.
        self.absorbed = _child(self.root, 'g1f3')
        self.absorbed_rows = [_absorbed(self.absorbed, LADDER[1])]
        # 1.Nc3: 128M ran, and a 512M rung was absorbed after it.
        self.mixed = _child(self.root, 'b1c3')
        _ran(self.mixed, LADDER[0])
        self.absorbed_rows.append(_absorbed(self.mixed, LADDER[1]))
        self.url = f'/atomicdb/explore/{self.root.key}/'

    def _rows(self):
        return {row['uci']: row for row in
                views._with_search_depth(views._child_moves(self.root))}

    def test_a_searched_move_shows_its_deepest_search(self):
        row = self._rows()['e2e4']

        self.assertEqual(row['searched_nodes'], LADDER[1])
        self.assertEqual(row['searched_h'], '512M')
        _attrs, content = _depth_cell(Client().get(self.url).content.decode(),
                                      'e2e4')
        self.assertEqual(content, '512M')

    def test_a_move_no_search_has_reached_shows_a_dash(self):
        row = self._rows()['d2d4']

        self.assertIsNone(row['searched_nodes'])
        self.assertIsNone(row['searched_h'])
        attrs, content = _depth_cell(Client().get(self.url).content.decode(),
                                     'd2d4')
        self.assertEqual(content, '-')
        self.assertIn('title="not searched yet · visits 0"', attrs)

    def test_an_absorbed_row_is_not_a_search(self):
        """The ladder counts it as a spent rung; the column counts searches."""
        for row in self.absorbed_rows:
            self.assertTrue(ingest.is_absorbed(row))
        rows = self._rows()
        body = Client().get(self.url).content.decode()

        self.assertIsNone(rows['g1f3']['searched_nodes'])
        self.assertEqual(_depth_cell(body, 'g1f3')[1], '-')
        # Beside a search that did run, the absorbed rung above it changes
        # nothing either.
        self.assertEqual(rows['b1c3']['searched_nodes'], LADDER[0])
        self.assertEqual(_depth_cell(body, 'b1c3')[1], '128M')
        # The two questions really do differ on these rows.
        self.assertEqual(ingest.request_ladder_state(self.absorbed)[0],
                         LADDER[1])
        self.assertEqual(ingest.request_ladder_state(self.mixed)[0],
                         LADDER[1])

    def test_the_visits_stay_in_the_tooltip(self):
        Position.objects.filter(pk=self.deep.pk).update(visits=7)

        attrs, _content = _depth_cell(Client().get(self.url).content.decode(),
                                      'e2e4')

        self.assertIn('title="searched to 512M nodes · visits 7"', attrs)

    def test_the_header_names_the_column(self):
        body = Client().get(self.url).content.decode()

        self.assertRegex(body, r'<th title="The node budget of the deepest '
                               r'search[^"]*">depth</th>')
        self.assertNotIn('<th>visits</th>', body)

    def test_an_off_tree_row_keeps_its_bare_dash(self):
        """No position, so nothing to have been searched and nothing asked."""
        body = Client().get(self.url).content.decode()

        attrs, content = _depth_cell(body, 'a2a3')
        self.assertIn('not in tree', next(row for row in ROW.findall(body)
                                          if '<strong>a2a3</strong>' in row))
        self.assertEqual(content, '-')
        self.assertNotIn('title=', attrs)

    def test_the_api_carries_it_beside_the_visits(self):
        payload = Client().get('/atomicdb/api/query',
                               {'fen': self.root.fen}).json()

        moves = {move['uci']: move for move in payload['moves']}
        self.assertEqual(moves['e2e4']['searched_nodes'], LADDER[1])
        self.assertEqual(moves['e2e4']['visits'], 2)
        self.assertIsNone(moves['d2d4']['searched_nodes'])
        self.assertIsNone(moves['g1f3']['searched_nodes'])
        self.assertEqual(moves['b1c3']['searched_nodes'], LADDER[0])

    def test_it_is_the_line_searched_budget_draws(self):
        """The one a chosen depth in the multi-select is held to."""
        positions = (self.deep, self.blank, self.absorbed, self.mixed)

        many = ingest.searched_budgets([pos.key for pos in positions])

        for pos in positions:
            self.assertEqual(many.get(pos.key), ingest.searched_budget(pos),
                             pos.key[:12])

    def test_an_odd_budget_is_still_short(self):
        """Every budget the tree buys is whole millions; a stray one is not
        printed with eleven digits in a narrow column."""
        self.assertEqual(views._budget_label(2_000_000_000), '2B')
        self.assertEqual(views._budget_label(8_000_000), '8M')
        self.assertEqual(views._budget_label(1_500_000), '1.5M')


class DepthColumnCostTests(TestCase):
    """One statement for the whole table, however wide it is."""

    def setUp(self):
        self.root = _root()
        self.url = f'/atomicdb/explore/{self.root.key}/'
        self.moves = logic.legal_moves(self.root.fen)

    def _searched_children(self, ucis):
        for uci in ucis:
            _ran(_child(self.root, uci), LADDER[0])

    def _page_statements(self, url):
        """Statements of one GET, on every database the page can touch."""
        with ExitStack() as stack:
            captured = [stack.enter_context(CaptureQueriesContext(
                connections[alias]))
                for alias in {'default', settings.ATOMICDB_DATABASE_ALIAS}]
            response = Client().get(url)
        self.assertEqual(response.status_code, 200)
        return sum(len(context.captured_queries) for context in captured)

    def test_the_whole_table_is_one_statement(self):
        self._searched_children(self.moves)
        rows = views._child_moves(self.root)
        self.assertEqual(len(rows), len(self.moves))

        with self.assertNumQueries(1, using=settings.ATOMICDB_DATABASE_ALIAS):
            views._with_search_depth(rows)

        self.assertEqual({row['searched_h'] for row in rows}, {'128M'})

    def test_an_empty_table_asks_nothing(self):
        with self.assertNumQueries(0, using=settings.ATOMICDB_DATABASE_ALIAS):
            self.assertEqual(views._with_search_depth([]), [])

    def test_the_page_costs_the_same_with_three_rows_or_twenty(self):
        self._searched_children(self.moves[:3])
        Client().get(self.url)          # warm the shared stores first
        narrow = self._page_statements(self.url)

        self._searched_children(self.moves[3:])
        Client().get(self.url)
        wide = self._page_statements(self.url)

        self.assertGreater(narrow, 0)
        self.assertEqual(wide, narrow)

    def test_the_query_api_too(self):
        url = '/atomicdb/api/query?' + urlencode({'fen': self.root.fen})
        self._searched_children(self.moves[:3])
        narrow = self._page_statements(url)

        self._searched_children(self.moves[3:])
        wide = self._page_statements(url)

        self.assertEqual(wide, narrow)


class DocsTests(TestCase):

    def test_the_docs_describe_the_column_and_the_field(self):
        response = Client().get('/atomicdb/docs/')

        self.assertContains(response,
                            'The last column, <strong>depth</strong>')
        self.assertContains(response, '<code>searched_nodes</code>')
