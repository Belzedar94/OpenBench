"""``expand`` and ``materialise_won_line`` write in a few statements.

They used to pay a ``get_or_create`` pair per move (per ply for the won
line): a SELECT, and for a new row a SAVEPOINT, an INSERT and a RELEASE,
twice.  On the live box that was half of an ingest writer's time.  The
batched forms must leave exactly the rows the step-by-step forms left, so
every test here runs the old code and the new code on the same tree, rolls
the first back, and compares what each wrote.
"""

from unittest import mock

from django.db import transaction
from django.test.utils import CaptureQueriesContext

from . import ingest, logic
from .database import connection, current_alias
from .models import Campaign, ClosureProvenance, DBEvent, Edge, Position
from .testing import TestCase
from .test_won_line_chain import FORCED_MATE_FEN


def _legacy_expand(pos):
    """``expand`` as it was before the batching, call for call."""
    if pos.expanded or pos.status != 'UNKNOWN':
        return []
    children = []
    for uci in logic.legal_moves(pos.fen):
        child_fen = logic.apply_move(pos.fen, uci)
        child = ingest.get_or_create_position(child_fen,
                                              campaign_id=pos.campaign_id)
        Edge.objects.get_or_create(parent=pos, move_uci=uci,
                                   defaults={'child': child})
        touched = []
        if child.priority <= ingest.DEAD / 2:
            child.priority = 0.0
            touched.append('priority')
        if pos.reachable and not child.reachable:
            child.reachable = True
            touched.append('reachable')
        if touched:
            child.save(update_fields=touched)
        children.append(child)
    pos.expanded = True
    pos.save(update_fields=['expanded'])
    return children


def _legacy_materialise(pos):
    """``materialise_won_line`` walking one ply at a time, as it did."""
    line = (pos.won_line or '').split()
    grade = (pos.proof if pos.proof in ('ANDOR', 'ENGINE',
                                        ingest.TB_LINE_VERDICT) else None)
    return ingest._materialise_stepwise(
        pos, line, pos.proof == ingest.TB_LINE_VERDICT, grade,
        pos.clock_slack)


_SKIP = {'updated'}


def _campaign_name(campaign_id):
    """A campaign by its name, not its id.

    Each run builds its own campaigns, and on Postgres the rolled-back run
    still spends sequence values, so the same campaign gets a different id
    in each run.  The name is what the two runs share.
    """
    if campaign_id is None:
        return None
    return Campaign.objects.get(pk=campaign_id).name


def _value(row, attname):
    value = getattr(row, attname)
    return _campaign_name(value) if attname == 'campaign_id' else value


def _snapshot():
    positions = sorted(
        tuple((field.attname, _value(row, field.attname))
              for field in Position._meta.concrete_fields
              if field.attname not in _SKIP)
        for row in Position.objects.all())
    edges = list(Edge.objects.order_by('id')
                 .values_list('parent_id', 'move_uci', 'child_id'))
    events = list(DBEvent.objects.order_by('id')
                  .values_list('kind', 'payload'))
    provenance = sorted(ClosureProvenance.objects.values_list(
        'key', 'status', 'closure', 'proof', 'source'))
    return {'positions': positions, 'edges': edges, 'events': events,
            'provenance': provenance}


def _children_view(children):
    return [(child.key, _campaign_name(child.campaign_id), child.reachable,
             child.priority, child.status) for child in children]


class _Equivalence(TestCase):

    def _both(self, setup, legacy, batched, view=lambda result: result):
        """Run ``legacy`` and ``batched`` on the tree ``setup`` builds.

        ``view`` turns a result into what is compared, inside its own run:
        the first run's rows are gone once it is rolled back.
        """
        with transaction.atomic(using=current_alias()):
            subject = setup()
            with CaptureQueriesContext(connection) as old_queries:
                old_result = legacy(subject)
            old_result = view(old_result)
            old = _snapshot()
            transaction.set_rollback(True, using=current_alias())
        subject = setup()
        with CaptureQueriesContext(connection) as new_queries:
            new_result = batched(subject)
        new_result = view(new_result)
        new = _snapshot()
        return (old, old_result, len(old_queries)), \
            (new, new_result, len(new_queries))


def _line(*ucis):
    parent = ingest.get_or_create_position(logic.start_fen())
    for uci in ucis:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        parent = child
    return parent


def _campaign(position, name):
    return Campaign.objects.create(name=name, root=position, line_san=name,
                                   state=Campaign.CState.ACTIVE, active=True)


class ExpandTests(_Equivalence):

    def _tree(self):
        """A tagged, reachable parent whose children are half there."""
        parent = _line('g1f3')
        mine = _campaign(parent, 'mine')
        theirs = _campaign(parent, 'theirs')
        Position.objects.filter(key=parent.key).update(campaign=mine,
                                                       reachable=True)
        moves = logic.legal_moves(parent.fen)
        dead = ingest.get_or_create_position(
            logic.apply_move(parent.fen, moves[0]))
        Position.objects.filter(key=dead.key).update(priority=ingest.DEAD)
        owned = ingest.get_or_create_position(
            logic.apply_move(parent.fen, moves[1]))
        Position.objects.filter(key=owned.key).update(campaign=theirs)
        marked = ingest.get_or_create_position(
            logic.apply_move(parent.fen, moves[2]))
        Position.objects.filter(key=marked.key).update(reachable=True)
        # An edge a click already wrote, ahead of the expansion.
        Edge.objects.create(parent_id=parent.key, move_uci=moves[3],
                            child=ingest.get_or_create_position(
                                logic.apply_move(parent.fen, moves[3])))
        return Position.objects.get(key=parent.key)

    def test_a_half_known_expansion_writes_what_the_loop_wrote(self):
        (old, old_children, old_count), (new, new_children, new_count) = \
            self._both(self._tree, _legacy_expand, ingest.expand,
                       view=_children_view)

        self.assertEqual(new, old)
        self.assertEqual(new_children, old_children)
        self.assertLess(new_count, old_count)

    def test_terminal_children_are_born_closed_as_before(self):
        def tree():
            # g1g7 blows up the black king: a child that is born terminal.
            return ingest.get_or_create_position(
                '7k/6p1/8/8/8/8/8/6QK w - - 0 1')

        (old, old_children, _), (new, new_children, _) = \
            self._both(tree, _legacy_expand, ingest.expand,
                       view=lambda children: (
                           _children_view(children),
                           [child.closure for child in children]))

        self.assertEqual(new, old)
        self.assertIn('TERMINAL', new_children[1])
        self.assertEqual(new_children, old_children)

    def test_an_expansion_is_a_handful_of_statements(self):
        parent = _line('g1f3')
        moves = len(logic.legal_moves(parent.fen))
        self.assertGreaterEqual(moves, 20)

        with CaptureQueriesContext(connection) as queries:
            ingest.expand(parent)

        # SELECT existing, INSERT the rest, SELECT them, INSERT the edges,
        # UPDATE expanded (plus the savepoint pair around the save): not one
        # statement per move.
        self.assertLessEqual(len(queries), 8)

    def test_a_reachable_parent_marks_its_old_children(self):
        parent = _line('g1f3')
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, logic.legal_moves(parent.fen)[0]))
        self.assertFalse(child.reachable)
        Position.objects.filter(key=parent.key).update(reachable=True)

        children = ingest.expand(Position.objects.get(key=parent.key))

        self.assertTrue(all(c.reachable for c in children))
        self.assertTrue(Position.objects.get(key=child.key).reachable)
        self.assertFalse(Position.objects.filter(
            edges_in__parent_id=parent.key, reachable=False).exists())


class MaterialiseTests(_Equivalence):

    def _line_from_fixture(self):
        fen = logic.canonical_fen(FORCED_MATE_FEN)
        for first in logic.legal_moves(fen):
            after = logic.apply_move(fen, first)
            for second in logic.legal_moves(after):
                mid = logic.apply_move(after, second)
                for third in logic.legal_moves(mid):
                    end = logic.apply_move(mid, third)
                    t = logic.terminal_status(end)
                    if t and t[0] == 'WHITE_WIN' and logic.verify_mate_pv(
                            fen, [first, second, third], True):
                        return [first, second, third]
        raise AssertionError('no winning line in the fixture')

    def _closed(self, line, proof='ANDOR'):
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', proof
        pos.won_line, pos.mate_in = ' '.join(line), len(line)
        pos.best_move, pos.clock_slack = line[0], 80
        pos.save()
        return pos

    def test_a_fresh_chain_writes_what_the_walk_wrote(self):
        line = self._line_from_fixture()

        def tree():
            pos = self._closed(line)
            camp = _campaign(pos, 'mine')
            Position.objects.filter(key=pos.key).update(campaign=camp)
            return Position.objects.get(key=pos.key)

        (old, old_result, old_count), (new, new_result, new_count) = \
            self._both(tree, _legacy_materialise, ingest.materialise_won_line)

        self.assertEqual(new, old)
        self.assertEqual(new_result, old_result)
        self.assertEqual(new_result['plies'], len(line))
        self.assertLess(new_count, old_count)

    def test_a_chain_through_known_nodes_inherits_as_the_walk_did(self):
        """A node in the middle that another campaign owns hands ITS tag to
        the new nodes after it; an edge already there is not counted."""
        line = self._line_from_fixture()

        def tree():
            pos = self._closed(line, proof='ENGINE')
            mine = _campaign(pos, 'mine')
            theirs = _campaign(pos, 'theirs')
            Position.objects.filter(key=pos.key).update(campaign=mine)
            first = ingest.get_or_create_position(
                logic.apply_move(pos.fen, line[0]))
            Position.objects.filter(key=first.key).update(campaign=theirs)
            Edge.objects.create(parent_id=pos.key, move_uci=line[0],
                                child_id=first.key)
            return Position.objects.get(key=pos.key)

        (old, old_result, _), (new, new_result, _) = \
            self._both(tree, _legacy_materialise, ingest.materialise_won_line)

        self.assertEqual(new, old)
        self.assertEqual(new_result, old_result)
        self.assertEqual(new_result['created_edges'], len(line) - 1)

    def test_an_illegal_move_stops_the_chain_where_the_walk_stopped(self):
        line = self._line_from_fixture()

        def tree():
            return self._closed([line[0], 'a1a1', *line[1:]])

        (old, old_result, _), (new, new_result, _) = \
            self._both(tree, _legacy_materialise, ingest.materialise_won_line)

        self.assertEqual(new, old)
        self.assertEqual(new_result, old_result)
        self.assertEqual(new_result['plies'], 1)

    def test_a_stored_fen_the_walk_did_not_compute_walks_step_by_step(self):
        line = self._line_from_fixture()

        def tree():
            pos = self._closed(line)
            first = ingest.get_or_create_position(
                logic.apply_move(pos.fen, line[0]))
            # A row whose stored text is not the canonical FEN the walk
            # computes: the batched walk must not guess, so it hands over.
            Position.objects.filter(key=first.key).update(
                fen=first.fen.replace(' 0 1', ' 3 7'))
            return Position.objects.get(key=pos.key)

        with mock.patch.object(ingest, '_materialise_stepwise',
                               wraps=ingest._materialise_stepwise) as spy:
            (old, old_result, _), (new, new_result, _) = self._both(
                tree, _legacy_materialise, ingest.materialise_won_line)

        # Once for the legacy run, once for the batched one handing over.
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(new, old)
        self.assertEqual(new_result, old_result)
