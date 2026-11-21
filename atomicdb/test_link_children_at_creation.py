"""A node born outside ``expand`` is linked to the children it already has.

A click in the explorer (``goto`` and the move list), the Verify PV walk and
the seeding of a FEN used to write the node and, at most, the edge from its
parent.  Its own moves stayed unwritten until its first search was ingested,
so it did not see positions already in the tree through a transposition,
solved ones included.  A read-only sample of the live tree (21-Nov) found an
existing child on 39% of the unexpanded nodes, 2.1 on average, 7.6% of them
solved.  Now the node is linked at birth to the children that exist, and a
solved one climbs the way every other discovery of a solved child climbs.
"""

from unittest import mock

from django.conf import settings
from django.db import connections
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from . import ingest, logic
from .models import Edge, Position
from .testing import TestCase


def _after(fen, *ucis):
    for uci in ucis:
        fen = logic.apply_move(fen, uci)
    return fen


START = logic.start_fen()
# 1.Nf3 a6 2.Ng5: Black to move.  2...a5 reaches a position where 3.Nxf7
# explodes the black king, a terminal White win.
BEFORE_MATE = _after(START, 'g1f3', 'a7a6', 'f3g5')
MATE_REPLY = 'a6a5'
MATE_IN_ONE = _after(BEFORE_MATE, MATE_REPLY)
MATING_MOVE = 'g5f7'


def _goto(parent, uci):
    response = Client().get(f'/atomicdb/goto/{parent.key}/{uci}/')
    assert response.status_code == 302, response.status_code
    return Position.objects.get(key=logic.key_of(_after(parent.fen, uci)))


class LinkedAtBirthTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(START)

    def test_a_click_links_the_new_node_to_children_that_exist(self):
        after_e4 = _after(START, 'e2e4')
        existing = {uci: ingest.get_or_create_position(_after(after_e4, uci))
                    for uci in ('e7e5', 'd7d5', 'g8f6')}
        positions_before = Position.objects.count()

        node = _goto(self.root, 'e2e4')

        # Only the node itself was born; nothing below it was made.
        self.assertEqual(Position.objects.count(), positions_before + 1)
        self.assertFalse(node.expanded)
        edges = list(Edge.objects.filter(parent=node).order_by('id'))
        self.assertEqual({e.move_uci: e.child_id for e in edges},
                         {uci: pos.key for uci, pos in existing.items()})
        # In movegen order, as ``expand`` writes them.
        legal = logic.legal_moves(node.fen)
        self.assertEqual([e.move_uci for e in edges],
                         sorted((e.move_uci for e in edges), key=legal.index))
        # The edge from the parent is the one the click always wrote.
        self.assertTrue(Edge.objects.filter(
            parent=self.root, move_uci='e2e4', child=node).exists())

    def test_missing_children_are_not_created(self):
        positions_before = Position.objects.count()

        node = _goto(self.root, 'e2e4')

        self.assertEqual(Position.objects.count(), positions_before + 1)
        self.assertFalse(Edge.objects.filter(parent=node).exists())
        self.assertFalse(node.expanded)

    def test_a_node_that_already_existed_is_not_relinked(self):
        node = ingest.get_or_create_position(_after(START, 'e2e4'))
        ingest.get_or_create_position(_after(node.fen, 'e7e5'))

        _goto(self.root, 'e2e4')

        self.assertFalse(Edge.objects.filter(parent=node).exists())

    def test_linking_twice_or_over_existing_edges_writes_no_duplicate(self):
        node = ingest.get_or_create_position(_after(START, 'e2e4'))
        child = ingest.get_or_create_position(_after(node.fen, 'e7e5'))
        ingest.get_or_create_position(_after(node.fen, 'd7d5'))
        Edge.objects.create(parent=node, move_uci='e7e5', child=child)

        ingest.link_existing_children(node)
        ingest.link_existing_children(node)

        self.assertEqual(
            sorted(Edge.objects.filter(parent=node)
                   .values_list('move_uci', flat=True)),
            ['d7d5', 'e7e5'])
        # And the expansion that comes later only adds what was missing.
        ingest.expand(node)
        self.assertEqual(Edge.objects.filter(parent=node).count(),
                         len(logic.legal_moves(node.fen)))

    def test_a_new_route_revives_a_child_from_the_tombstone(self):
        node = ingest.get_or_create_position(_after(START, 'e2e4'))
        child = ingest.get_or_create_position(_after(node.fen, 'e7e5'))
        Position.objects.filter(key=child.key).update(priority=ingest.DEAD)

        ingest.link_existing_children(node)

        self.assertEqual(Position.objects.get(key=child.key).priority, 0.0)

    def test_the_cost_is_one_lookup_and_one_insert(self):
        node = ingest.get_or_create_position(_after(START, 'e2e4'))
        for uci in ('e7e5', 'd7d5'):
            ingest.get_or_create_position(_after(node.fen, uci))
        alias = settings.ATOMICDB_DATABASE_ALIAS

        with CaptureQueriesContext(connections[alias]) as linked:
            ingest.link_existing_children(node)
        orphan = ingest.get_or_create_position(_after(START, 'd2d4'))
        with CaptureQueriesContext(connections[alias]) as alone:
            ingest.link_existing_children(orphan)

        statements = [q['sql'].split()[0].upper()
                      for q in linked.captured_queries]
        self.assertEqual(statements, ['SELECT', 'INSERT'])
        self.assertEqual(len(alone.captured_queries), 1)

    @override_settings(ATOMICDB_LINK_CHILDREN_AT_CREATION=False)
    def test_the_setting_turns_it_off(self):
        ingest.get_or_create_position(_after(START, 'e2e4', 'e7e5'))

        node = _goto(self.root, 'e2e4')

        self.assertFalse(Edge.objects.filter(parent=node).exists())


class SolvedChildClimbsTests(TestCase):
    """A link to a solved child runs the climb a discovery always runs."""

    def setUp(self):
        self.parent = ingest.get_or_create_position(BEFORE_MATE)
        # The terminal child, already in the tree by some other road.
        self.won = ingest.get_or_create_position(
            _after(MATE_IN_ONE, MATING_MOVE))
        self.assertEqual(self.won.status, 'WHITE_WIN')
        self.assertEqual(self.won.closure, 'TERMINAL')

    def assert_closed_through_the_link(self, node):
        self.assertEqual(node.status, 'WHITE_WIN')
        self.assertEqual(node.closure, 'MINIMAX')
        self.assertEqual(node.best_move, MATING_MOVE)
        self.assertEqual(node.mate_in, 1)
        self.assertFalse(node.expanded)
        self.assertTrue(Edge.objects.filter(
            parent=node, move_uci=MATING_MOVE, child=self.won).exists())

    def test_a_click_closes_the_new_node_on_its_solved_child(self):
        node = _goto(self.parent, MATE_REPLY)

        self.assert_closed_through_the_link(node)
        # The parent was re-read by the climb.  Black to move, and its other
        # replies unknown, so it stays open: no rule changed.
        self.assertEqual(Position.objects.get(key=self.parent.key).status,
                         'UNKNOWN')

    def test_the_climb_is_the_one_every_discovery_runs(self):
        with mock.patch.object(ingest, 'backup_cascade',
                               wraps=ingest.backup_cascade) as cascade, \
                mock.patch.object(ingest, 'backup_backed_evals',
                                  wraps=ingest.backup_backed_evals) as backed:
            node = _goto(self.parent, MATE_REPLY)

        cascade.assert_called_once_with([node.key])
        backed.assert_called_once_with([node.key, self.parent.key])

    def test_no_climb_without_a_solved_child(self):
        self.won.delete()
        with mock.patch.object(ingest, 'backup_cascade') as cascade, \
                mock.patch.object(ingest, 'backup_backed_evals') as backed:
            node = _goto(self.parent, MATE_REPLY)

        self.assertEqual(node.status, 'UNKNOWN')
        cascade.assert_not_called()
        backed.assert_not_called()

    def test_the_pv_walk_closes_the_node_it_makes_on_its_solved_child(self):
        ingest._walk_pv_frontier(self.parent, [MATE_REPLY])

        node = Position.objects.get(key=logic.key_of(MATE_IN_ONE))
        self.assert_closed_through_the_link(node)

    def test_a_seeded_fen_closes_on_its_solved_child(self):
        response = Client().post('/atomicdb/fen/', {'fen': MATE_IN_ONE})

        self.assertIn(response.status_code, (301, 302))
        node = Position.objects.get(key=logic.key_of(MATE_IN_ONE))
        self.assert_closed_through_the_link(node)

    @override_settings(ATOMICDB_LINK_CHILDREN_AT_CREATION=False)
    def test_without_the_setting_the_node_waits_for_its_search(self):
        node = _goto(self.parent, MATE_REPLY)

        self.assertEqual(node.status, 'UNKNOWN')
        self.assertFalse(Edge.objects.filter(parent=node).exists())
