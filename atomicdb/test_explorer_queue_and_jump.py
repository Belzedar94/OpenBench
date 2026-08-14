"""The waiting queue row: which opening the request is in.

The row used to spend its words on "N requests ahead", which is the number
the "#N" beside it already gives, so the room now goes to something the row
could not say at all.

What is fixed here:

  * a waiting row names its line and stops repeating its place in the queue;
  * the name belongs to the MOVE ORDER the row prints, which matters because
    an inherited name depends on the order and the same position is reachable
    through differently named ancestors;
  * a line the catalogue does not name prints no name rather than a guess.
"""

import html as html_module
import re

from . import contributors, ingest, logic, views
from .models import AnalysisTask, Edge
from .testing import TestCase


# Two orders into the SAME position, each through a differently named
# ancestor: 1.Nf3 d6 is the Villager Defense and 1.Nc3 is Tipau, and the four
# moves commute, so the position after either order is one position with two
# honest names. That coincidence is the whole point of the route tests.
VILLAGER_FIRST = ['g1f3', 'd7d6', 'b1c3', 'b8c6']
TIPAU_FIRST = ['b1c3', 'b8c6', 'g1f3', 'd7d6']
UNNAMED = ['a2a3', 'a7a6']


def _reading(body):
    """What a visitor READS: no tags, no attributes, spaces collapsed."""
    stripped = html_module.unescape(re.sub(r'<[^>]+>', ' ', body))
    return ' '.join(stripped.split())


def _positions(ucis, *, connect):
    """Materialise a line, with its edges when the lineage has to find it."""
    fen = logic.start_fen()
    node = ingest.get_or_create_position(fen)
    nodes = [node]
    for uci in ucis:
        fen = logic.apply_move(fen, uci)
        child = ingest.get_or_create_position(fen)
        if connect:
            Edge.objects.get_or_create(parent=node, move_uci=uci,
                                       defaults={'child': child})
        node = child
        nodes.append(child)
    return nodes


def _waiting(position, route='', username='alice'):
    return AnalysisTask.objects.create(
        position=position, generation=0, budget_nodes=128_000_000,
        source=AnalysisTask.Source.USER, requested_by=username, route=route)


class WaitingRowOpeningTests(TestCase):
    """The waiting row says which line it is in, once."""

    def setUp(self):
        contributors.reset_cache()

    def _body(self, username='alice'):
        return _reading(
            self.client.get(f'/atomicdb/user/{username}/').content.decode())

    def test_a_waiting_row_names_its_line_and_stops_counting_queue(self):
        target = _positions(VILLAGER_FIRST, connect=True)[-1]
        _waiting(target)

        body = self._body()

        self.assertIn('Villager Defense', body)
        self.assertNotIn('request ahead', body)
        self.assertNotIn('requests ahead', body)

    def test_the_place_in_the_queue_is_still_there_as_the_number(self):
        """Removing the words removed a duplicate, not the information."""
        target = _positions(VILLAGER_FIRST, connect=True)[-1]
        _waiting(target)

        self.assertIn('#1', self._body())

    def test_the_name_comes_from_the_move_order_the_row_prints(self):
        """A row that showed one order and named another would contradict
        itself, and the inherited name really does depend on the order."""
        # Positions only, so the reverse walk can find one order and only one.
        _positions(VILLAGER_FIRST, connect=False)
        target = _positions(TIPAU_FIRST, connect=True)[-1]
        _waiting(target, route=','.join(VILLAGER_FIRST))

        body = self._body()

        self.assertIn('1. Nf3 d6 2. Nc3 Nc6', body)
        self.assertIn('Villager Defense', body)
        self.assertNotIn('Tipau', body)

    def test_without_a_route_the_row_names_the_lineage_it_shows(self):
        """Same position, no declared order: the row prints the canonical
        lineage, so it has to name THAT one."""
        target = _positions(TIPAU_FIRST, connect=True)[-1]
        _waiting(target)

        body = self._body()

        self.assertIn('1. Nc3 Nc6 2. Nf3 d6', body)
        self.assertIn('Tipau', body)
        self.assertNotIn('Villager Defense', body)

    def test_a_line_the_catalogue_does_not_name_prints_no_name(self):
        target = _positions(UNNAMED, connect=True)[-1]
        _waiting(target)

        body = self._body()

        self.assertIn('1. a3 a6', body)
        self.assertIn('Waiting', body)

    def test_the_name_of_a_line_is_the_last_one_it_crossed(self):
        keys = [position.key
                for position in _positions(VILLAGER_FIRST, connect=True)]

        self.assertEqual(views._opening_name_for_keys(keys),
                         'Villager Defense')
        self.assertEqual(views._opening_name_for_keys(keys[:2]),
                         'King Knight Opening')

    def test_a_line_that_is_not_a_line_names_nothing_instead_of_failing(self):
        """A queue row exists to say what was asked for, and no malformed key
        sequence is allowed to take the page down with it."""
        self.assertEqual(views._opening_name_for_keys([]), '')
        self.assertEqual(views._opening_name_for_keys(['not-a-key']), '')
