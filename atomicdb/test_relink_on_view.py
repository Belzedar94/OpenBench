"""Opening an unexpanded node in the explorer links it to existing children.

#287 links a node born outside ``expand`` to the children that already exist,
but only at birth.  A node that existed before, or was made by a path that
does not link, kept showing only the moves somebody had clicked until its
first search landed, even with the other children in the tree through a
transposition, and the only way to see them was the "Request" button (MoKy,
24-Nov).  Now the explorer links the node it shows, on demand: no sweep, no
node created, the node stays unexpanded, and a link to a solved child climbs
the way every other discovery of one climbs.
"""

from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connections
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from . import ingest, logic, views
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


def _view(pos, query=''):
    response = Client().get(f'/atomicdb/explore/{pos.key}/{query}')
    assert response.status_code == 200, response.status_code
    return response


def _edges(pos):
    return {e.move_uci: e.child_id for e in Edge.objects.filter(parent=pos)}


class RelinkOnViewTests(TestCase):

    def setUp(self):
        cache.clear()
        # A node that existed before #287: its row and nothing below it.
        self.node = ingest.get_or_create_position(_after(START, 'e2e4'))
        self.existing = {
            uci: ingest.get_or_create_position(_after(self.node.fen, uci))
            for uci in ('e7e5', 'd7d5', 'g8f6')}

    def test_viewing_links_the_node_to_children_that_exist(self):
        positions_before = Position.objects.count()

        response = _view(self.node)

        self.assertEqual(_edges(self.node),
                         {uci: pos.key for uci, pos in self.existing.items()})
        # Nothing was created and the node is still unexpanded.
        self.assertEqual(Position.objects.count(), positions_before)
        self.assertFalse(Position.objects.get(key=self.node.key).expanded)
        # And the table drawn by that same view already shows them.
        shown = {m['uci'] for m in response.context['moves']}
        self.assertEqual(shown, set(self.existing))
        offtree = {m['uci'] for m in response.context['offtree']}
        self.assertFalse(offtree & set(self.existing))

    def test_the_edges_are_the_ones_expand_writes(self):
        _view(self.node)

        edges = list(Edge.objects.filter(parent=self.node).order_by('id'))
        legal = logic.legal_moves(self.node.fen)
        self.assertEqual([e.move_uci for e in edges],
                         sorted((e.move_uci for e in edges), key=legal.index))
        ingest.expand(Position.objects.get(key=self.node.key))
        self.assertEqual(Edge.objects.filter(parent=self.node).count(),
                         len(legal))

    def test_existing_edges_are_kept_and_not_duplicated(self):
        Edge.objects.create(parent=self.node, move_uci='e7e5',
                            child=self.existing['e7e5'])

        _view(self.node)
        cache.clear()
        _view(self.node)

        self.assertEqual(sorted(Edge.objects.filter(parent=self.node)
                                .values_list('move_uci', flat=True)),
                         ['d7d5', 'e7e5', 'g8f6'])

    def test_a_route_in_the_url_links_only_the_node_shown(self):
        root = ingest.get_or_create_position(START)
        Edge.objects.create(parent=root, move_uci='e2e4', child=self.node)
        positions_before = Position.objects.count()

        _view(self.node, '?play=e2e4')

        self.assertEqual(set(_edges(self.node)), set(self.existing))
        self.assertEqual(Position.objects.count(), positions_before)
        # The root along the route is not relinked by this view.
        self.assertEqual(set(_edges(root)), {'e2e4'})

    def test_an_expanded_node_is_left_alone(self):
        Position.objects.filter(key=self.node.key).update(expanded=True)

        _view(self.node)

        self.assertEqual(_edges(self.node), {})

    def test_a_closed_node_is_left_alone(self):
        Position.objects.filter(key=self.node.key).update(status='DRAW')

        _view(self.node)

        self.assertEqual(_edges(self.node), {})

    @override_settings(ATOMICDB_LINK_CHILDREN_AT_CREATION=False)
    def test_the_birth_setting_turns_it_off_too(self):
        _view(self.node)

        self.assertEqual(_edges(self.node), {})

    def test_a_repeated_view_inside_the_window_does_not_look_again(self):
        _view(self.node)
        with mock.patch.object(ingest, 'link_existing_children',
                               wraps=ingest.link_existing_children) as link:
            _view(self.node)
        link.assert_not_called()

    def test_a_child_born_after_the_window_is_linked_on_the_next_view(self):
        _view(self.node)
        late = ingest.get_or_create_position(_after(self.node.fen, 'c7c5'))

        _view(self.node)
        self.assertNotIn('c7c5', _edges(self.node))

        cache.clear()   # the window ran out
        _view(self.node)
        self.assertEqual(_edges(self.node)['c7c5'], late.key)

    def test_a_node_with_every_edge_costs_one_read(self):
        legal = logic.legal_moves(self.node.fen)
        ingest.expand(self.node)
        Position.objects.filter(key=self.node.key).update(expanded=False)
        node = Position.objects.get(key=self.node.key)
        self.assertEqual(Edge.objects.filter(parent=node).count(), len(legal))
        alias = settings.ATOMICDB_DATABASE_ALIAS

        with mock.patch.object(ingest, 'link_existing_children') as link, \
                CaptureQueriesContext(connections[alias]) as queries:
            views._relink_on_view(node, legal)

        link.assert_not_called()
        self.assertEqual(len(queries.captured_queries), 1)

    def test_the_view_pays_no_extra_move_generation(self):
        legal = logic.legal_moves(self.node.fen)
        with mock.patch.object(logic, 'apply_move',
                               wraps=logic.apply_move) as apply_move, \
                mock.patch.object(logic, 'legal_moves',
                                  wraps=logic.legal_moves) as legal_moves:
            views._relink_on_view(self.node, legal)
            # The table's own lookup of the moves still off the tree is
            # served by the keys the relink just computed.
            missing = [uci for uci in legal if uci not in self.existing]
            views._child_keys(self.node, missing)

        legal_moves.assert_not_called()
        self.assertEqual(apply_move.call_count, len(legal))

    def test_a_database_error_does_not_break_the_page(self):
        with mock.patch.object(ingest, 'link_existing_children',
                               side_effect=views.DatabaseError('boom')), \
                self.assertLogs('atomicdb.views', 'ERROR'):
            _view(self.node)

        self.assertEqual(_edges(self.node), {})
        # Not remembered as done: the next view tries again.
        _view(self.node)
        self.assertEqual(set(_edges(self.node)), set(self.existing))

    def test_anonymous_and_signed_in_viewers_both_link(self):
        from OpenBench.models import Profile
        _view(self.node)   # ``_view`` is anonymous
        self.assertEqual(set(_edges(self.node)), set(self.existing))

        other = ingest.get_or_create_position(_after(START, 'd2d4'))
        child = ingest.get_or_create_position(_after(other.fen, 'd7d5'))
        user = User.objects.create_user('viewer', password='pw')
        Profile.objects.create(user=user)
        client = Client()
        client.login(username='viewer', password='pw')
        response = client.get(f'/atomicdb/explore/{other.key}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_edges(other), {'d7d5': child.key})


class RelinkOnViewSolvedChildTests(TestCase):
    """A view that links a solved child runs the climb of a discovery."""

    def setUp(self):
        cache.clear()
        self.parent = ingest.get_or_create_position(BEFORE_MATE)
        # The node existed already, linked from its parent but never linked
        # below: the shape every node made before #287 has.
        self.node = ingest.get_or_create_position(MATE_IN_ONE)
        Edge.objects.create(parent=self.parent, move_uci=MATE_REPLY,
                            child=self.node)
        self.won = ingest.get_or_create_position(
            _after(MATE_IN_ONE, MATING_MOVE))
        self.assertEqual(self.won.status, 'WHITE_WIN')
        self.assertEqual(self.node.status, 'UNKNOWN')

    def test_viewing_closes_the_node_on_its_solved_child(self):
        response = _view(self.node)

        node = Position.objects.get(key=self.node.key)
        self.assertEqual(node.status, 'WHITE_WIN')
        self.assertEqual(node.closure, 'MINIMAX')
        self.assertEqual(node.best_move, MATING_MOVE)
        self.assertEqual(node.mate_in, 1)
        self.assertFalse(node.expanded)
        self.assertTrue(Edge.objects.filter(
            parent=node, move_uci=MATING_MOVE, child=self.won).exists())
        # The page drawn by that view already shows the closed node.
        self.assertEqual(response.context['pos'].status, 'WHITE_WIN')

    def test_the_climb_is_the_one_every_discovery_runs(self):
        with mock.patch.object(ingest, 'backup_cascade',
                               wraps=ingest.backup_cascade) as cascade, \
                mock.patch.object(ingest, 'backup_backed_evals',
                                  wraps=ingest.backup_backed_evals) as backed:
            _view(self.node)

        cascade.assert_called_once_with([self.node.key])
        backed.assert_called_once_with([self.node.key, self.parent.key])

    def test_no_climb_without_a_solved_child(self):
        self.won.delete()
        with mock.patch.object(ingest, 'backup_cascade') as cascade, \
                mock.patch.object(ingest, 'backup_backed_evals') as backed:
            _view(self.node)

        self.assertEqual(Position.objects.get(key=self.node.key).status,
                         'UNKNOWN')
        cascade.assert_not_called()
        backed.assert_not_called()
