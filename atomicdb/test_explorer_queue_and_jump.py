"""Two explorer rows: the opening a request is in, and the end of a proof.

Both come from the same report in #suggestions. The waiting row used to spend
its words on "N requests ahead", which is the number the "#N" beside it
already gives, so the room now goes to something the row could not say at
all: which opening the requested line is in. And a position closed by a
proven line had the line printed but no way to reach the end of it without
walking every ply by hand.

What is fixed here:

  * a waiting row names its line and stops repeating its place in the queue;
  * the name belongs to the MOVE ORDER the row prints, which matters because
    an inherited name depends on the order and the same position is reachable
    through differently named ancestors;
  * a line the catalogue does not name prints no name rather than a guess;
  * a proven position offers one jump, and it lands on the last position of
    the very line printed beside it;
  * nothing is offered where there is no proven line to walk.
"""

import html as html_module
import re

from . import contributors, ingest, logic, views
from .models import AnalysisTask, Edge, Position
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


def _chain(node, ucis):
    """Materialise a continuation from a position, edges included."""
    nodes = []
    for uci in ucis:
        child = ingest.get_or_create_position(logic.apply_move(node.fen, uci))
        Edge.objects.get_or_create(parent=node, move_uci=uci,
                                   defaults={'child': child})
        nodes.append(child)
        node = child
    return nodes


def _waiting(position, route='', username='alice'):
    return AnalysisTask.objects.create(
        position=position, generation=0, budget_nodes=128_000_000,
        source=AnalysisTask.Source.USER, requested_by=username, route=route)


def _close(position, **fields):
    """Close a position by hand: these tests navigate proofs, they do not
    produce them."""
    for name, value in fields.items():
        setattr(position, name, value)
    position.save(update_fields=list(fields))
    return position


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


class ProvenLineJumpTests(TestCase):
    """One click from a proof to the position it ends on."""

    def setUp(self):
        root = ingest.get_or_create_position(logic.start_fen())
        self.top = _chain(root, ['g1f3'])[0]
        self.line = ['d7d6', 'b1c3', 'b8c6']
        self.nodes = _chain(self.top, self.line)
        self.end = self.nodes[-1]
        _close(self.top, status='WHITE_WIN', closure='MATE_PV',
               proof='ANDOR', won_line=' '.join(self.line),
               best_move=self.line[0], mate_in=len(self.line))
        self.url = f'/atomicdb/explore/{self.top.key}/'
        self.jump = f'/atomicdb/proven-line-end/{self.top.key}/'

    def test_a_proven_position_offers_the_jump_beside_its_line(self):
        body = self.client.get(self.url).content.decode()

        self.assertIn('winning line:', body)
        self.assertIn(self.jump, body)
        self.assertIn('end of line', body)

    def test_the_chip_says_how_far_it_goes_before_anybody_clicks(self):
        """The distance is the length of the line printed beside it.

        A jump that announced a number of its own could disagree with the
        line the page is showing; taking the count from that very line is the
        only figure that cannot.
        """
        body = self.client.get(self.url).content.decode()

        self.assertIn(f'end of line ·{len(self.line)}', body)
        self.assertIn(f'{len(self.line)} plies below', body)

    def test_the_jump_lands_on_the_last_position_of_that_line(self):
        response = self.client.get(self.jump)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{self.end.key}/')

    def test_the_landing_is_never_cached(self):
        """The end moves the moment a shorter line is certified or a missing
        ply is materialised."""
        response = self.client.get(self.jump)

        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_the_jump_extends_the_route_the_visitor_came_with(self):
        """A visitor arrives with their own game, not with a reconstructed
        lineage, and leaves with it too."""
        response = self.client.get(self.jump + '?play=g1f3')

        self.assertEqual(
            response['Location'],
            f'/atomicdb/explore/{self.end.key}/?play=g1f3,'
            + ','.join(self.line))

    def test_a_route_that_does_not_reach_the_position_lands_without_one(self):
        response = self.client.get(self.jump + '?play=a2a3')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{self.end.key}/')

    def test_a_line_already_in_the_tree_is_walked_without_building_anything(
            self):
        """Nothing to build, nothing built: the chain is already there."""
        before = Position.objects.count(), Edge.objects.count()

        self.client.get(self.jump)

        self.assertEqual((Position.objects.count(), Edge.objects.count()),
                         before)

    def test_an_unsolved_position_offers_no_jump(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.end.key}/').content.decode()

        self.assertNotIn('/atomicdb/proven-line-end/', body)

    def test_a_refuted_witness_is_not_a_line_to_walk(self):
        """An open node keeps its disputed witness in ``won_line``. Walking it
        would be parading a line the system already called false."""
        disputed = _chain(self.end, ['h2h3'])[0]
        _close(disputed, proof='DISPUTED', won_line='a7a6')

        body = self.client.get(
            f'/atomicdb/explore/{disputed.key}/').content.decode()

        self.assertNotIn('/atomicdb/proven-line-end/', body)
        self.assertEqual(views._walk_proven_line(disputed),
                         (disputed, [], ''))

    def test_a_ply_missing_from_the_tree_no_longer_cuts_the_jump_short(self):
        """THE REPORTED BUG, and the whole reason this walk changed.

        A closure stores its winning line as text; the chain of positions it
        names becomes a real tree only when the materialiser runs over it.
        Closures older than that helper have as much of the chain as somebody
        happened to click, so a mate in two commonly had the first ply in the
        tree and nothing under it. Following edges, the jump stopped there and
        dropped the visitor on the position BEFORE the mate, one ply from the
        line it had just promised to walk to the end (Eclipsia, with the line
        "Bd7 Bxd7#" printed right beside the chip).

        The plies of a proven line are not a guess, so the walk builds the one
        it is missing and carries on to the end of the line.
        """
        top = _chain(self.end, ['h2h3'])[0]
        line = ['e7e5', 'd2d4']
        _close(top, status='WHITE_WIN', closure='MATE_PV', proof='ENGINE',
               won_line=' '.join(line), best_move=line[0], mate_in=2)
        # Only the FIRST ply exists, exactly as a single click leaves it.
        first = _chain(top, line[:1])[0]
        self.assertFalse(Edge.objects.filter(parent=first).exists())

        response = self.client.get(f'/atomicdb/proven-line-end/{top.key}/')

        landing = logic.key_of(logic.apply_move(
            logic.apply_move(top.fen, line[0]), line[1]))
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{landing}/')
        self.assertTrue(Edge.objects.filter(parent=first,
                                            move_uci=line[1]).exists())

    def test_a_line_no_ply_of_which_is_in_the_tree_is_still_walkable(self):
        """Same rule from the top: the chain is built out of the closure.

        The page used to refuse the jump whenever the first ply was not an
        edge yet, which meant the oldest proofs, the ones that most need the
        shortcut, were the ones that never offered it.
        """
        orphan = ingest.get_or_create_position(
            logic.apply_move(self.top.fen, 'g8f6'))
        Edge.objects.get_or_create(parent=self.top, move_uci='g8f6',
                                   defaults={'child': orphan})
        _close(orphan, status='WHITE_WIN', closure='MATE_PV', proof='ENGINE',
               won_line='b1c3 b8c6', best_move='b1c3', mate_in=2)

        body = self.client.get(
            f'/atomicdb/explore/{orphan.key}/').content.decode()
        response = self.client.get(
            f'/atomicdb/proven-line-end/{orphan.key}/')

        self.assertIn('winning line:', body)
        self.assertIn(f'/atomicdb/proven-line-end/{orphan.key}/', body)
        landing = logic.key_of(logic.apply_move(
            logic.apply_move(orphan.fen, 'b1c3'), 'b8c6'))
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{landing}/')

    def test_a_witness_that_no_longer_replays_lands_short_and_says_so(self):
        """The one case where a stored line genuinely ends early.

        Nothing can build a ply that is not a legal move any more, so the
        walk stops on the last one that was, and the landing carries the
        reason rather than passing itself off as the end of the line.
        """
        stale = _chain(self.end, ['h2h4'])[0]
        _close(stale, status='WHITE_WIN', closure='MATE_PV', proof='ENGINE',
               won_line='e7e5 h4h5 a1a8', best_move='e7e5', mate_in=3)

        response = self.client.get(f'/atomicdb/proven-line-end/{stale.key}/')

        landed = logic.key_of(logic.apply_move(
            logic.apply_move(stale.fen, 'e7e5'), 'h4h5'))
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{landed}/?incomplete=1')
        body = self.client.get(response['Location']).content.decode()
        self.assertIn('>chain breaks here</span>', body)

    def test_a_witness_chain_without_a_stored_line_is_followed_node_by_node(
            self):
        """A MINIMAX closure records no line, only the witness move of each
        node, so the descent reads that claim one position at a time."""
        for node, uci in zip([self.top, *self.nodes], self.line):
            _close(node, status='WHITE_WIN', closure='MINIMAX', won_line=None,
                   best_move=uci)
        _close(self.end, status='WHITE_WIN', closure='MINIMAX', won_line=None,
               best_move=None)

        body = self.client.get(self.url).content.decode()
        response = self.client.get(self.jump)

        self.assertIn('witness move:', body)
        self.assertIn(self.jump, body)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{self.end.key}/')

    def test_the_walk_stops_where_the_line_comes_back_on_itself(self):
        """1.Nf3 Nc6 2.Ng1 Nb8 IS the start position again once the counters
        are gone, and a proof line that closes on itself has no end to show."""
        root = ingest.get_or_create_position(logic.start_fen())
        cycle = ['g1f3', 'b8c6', 'f3g1', 'c6b8']
        nodes = _chain(root, cycle)
        _close(root, status='WHITE_WIN', closure='MATE_PV', proof='ENGINE',
               won_line=' '.join(cycle), best_move=cycle[0])

        end, walked, outcome = views._walk_proven_line(root)

        self.assertEqual(walked, cycle[:3])
        self.assertEqual(end.key, nodes[2].key)
        self.assertEqual(outcome, 'repetition')

    def test_the_walk_never_outruns_the_cap_the_line_was_stored_under(self):
        self.assertEqual(views.PROVEN_LINE_MAX_PLIES,
                         ingest.WON_LINE_MAX_PLIES)
