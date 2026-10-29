"""The searches below a position: what the old 10B rung became.

A 10B search held a worker for half an hour on a single position.  The same
nodes are now spent as up to 78 searches of 128M on the lines below it
(``ingest.request_subtree``): the plain button buys them once the 2B search
of the position has run, and the last stop of the depth slider (the API's
``budget=10000000000``) buys them whenever a chooser asks.

What these tests pin, and why each one:

* the ladder ends at 2B, and the numbers that size the new request;
* both doors lead to the walk, a non-chooser's choice of the last stop is
  dropped as any choice of theirs is, and the API refuses the combination
  with ``only_if_missing`` for everybody;
* the walk itself: proof-number shape with the attacker taken from the value,
  never a search above 128M, at most 78, the best lines first when the cap
  cuts, safe on transpositions and cycles, solved positions skipped, searched
  positions walked through and the searches somebody else asked for left to
  them;
* a click repeated while its searches wait buys nothing new, wherever below
  they sit;
* and the cost of a click does not grow with the width of the levels it
  walks through.

Most trees here are synthetic (bare-king FENs under hashed keys, already
marked expanded), because the walk only reads edges and the rows of the
positions; the doors use real positions, where expansion is part of the
story.
"""

import hashlib
from datetime import timedelta
from unittest import mock

from django.db import connections
from django.test import Client, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import depth, ingest, logic, views
from .database import current_alias
from .models import (AnalysisTask, ApiRequestLog, DBEvent, Edge, Position,
                     RequestLog, WorkerPing)
from .testing import TestCase, worker_account

SEARCH = ingest.SUBTREE_SEARCH_NODES
SUBTREE = ingest.SUBTREE_REQUEST_NODES
LIVE = (AnalysisTask.TState.PENDING, AnalysisTask.TState.LEASED)


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _node(name, stm='w', **fields):
    """A synthetic position: its own key, a bare-king FEN, already expanded."""
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1',
        expanded=True, **fields)


def _children(parent, names, stm, multipv=False):
    """Children of ``parent`` in movegen order, with moves ``u0``, ``u1``...

    ``multipv`` writes the parent's engine lines over those moves, in that
    order, which is what makes the parent's ranking informed.
    """
    kids = []
    for index, name in enumerate(names):
        child = _node(name, stm)
        Edge.objects.create(parent=parent, move_uci=f'u{index}', child=child)
        kids.append(child)
    if multipv:
        Position.objects.filter(pk=parent.pk).update(last_analysis=[
            {'move': f'u{index}', 'eval_cp': 0, 'mate': None,
             'pv': [f'u{index}']} for index in range(len(names))])
    return kids


def _searched(pos, budget=SEARCH):
    """A search of ``budget`` that ran on ``pos``, as a worker leaves it."""
    pos.refresh_from_db()
    AnalysisTask.objects.create(
        position=pos, generation=pos.visits, budget_nodes=budget,
        nodes_searched=budget, state=AnalysisTask.TState.COMPLETED,
        source=AnalysisTask.Source.USER, machine='box0')
    Position.objects.filter(pk=pos.pk).update(visits=pos.visits + 1,
                                              nodes_invested=budget)


def _live(pos=None):
    """Live tasks, on ``pos`` or anywhere."""
    tasks = AnalysisTask.objects.filter(state__in=LIVE)
    return tasks.filter(position=pos) if pos is not None else tasks


def _subtree(pos, requested_by='', route=''):
    return ingest.request_subtree(Position.objects.get(pk=pos.pk),
                                  requested_by=requested_by, route=route)


def _worker(username):
    """An account that may choose: it delivered an analysis yesterday."""
    user = worker_account(username, 'p')
    ping = WorkerPing.objects.create(machine=f'{username}-box', user=username,
                                     threads=8, hash_mb=1024, os='Linux')
    WorkerPing.objects.filter(pk=ping.pk).update(
        last_result_at=timezone.now() - timedelta(days=1))
    return user


def _signed_in(username):
    client = Client()
    client.login(username=username, password='p')
    return client


def _start(eval_cp=30, multipv=('e2e4', 'd2d4', 'g1f3')):
    """The start position with an engine line and a value of its own."""
    root = ingest.get_or_create_position(logic.start_fen())
    Position.objects.filter(pk=root.pk).update(
        eval_cp=eval_cp,
        last_analysis=[{'move': uci, 'eval_cp': eval_cp - index,
                        'mate': None, 'pv': [uci]}
                       for index, uci in enumerate(multipv)])
    root.refresh_from_db()
    return root


def _child(parent, uci):
    return Edge.objects.get(parent=parent, move_uci=uci).child


class NumbersTests(SimpleTestCase):

    def test_the_ladder_ends_at_2b(self):
        self.assertEqual(ingest.REQUEST_BUDGET_LADDER,
                         [128_000_000, 512_000_000, 2_000_000_000])

    def test_the_request_spends_the_old_top_rung_in_128m_searches(self):
        self.assertEqual(SUBTREE, 10_000_000_000)
        self.assertEqual(SEARCH, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertEqual(ingest.SUBTREE_TASK_CAP, 78)
        # As many whole searches as fit in the 10B, and no more.
        self.assertLessEqual(ingest.SUBTREE_TASK_CAP * SEARCH, SUBTREE)
        self.assertGreater((ingest.SUBTREE_TASK_CAP + 1) * SEARCH, SUBTREE)

    def test_the_last_stop_is_named_from_those_numbers(self):
        self.assertEqual(depth.SUBTREE_LABEL, '78×128M')
        self.assertEqual(depth.short_nodes(2_000_000_000), '2B')
        self.assertEqual(depth.short_nodes(512_000_000), '512M')


class PlainButtonTests(TestCase):
    """The button once the 2B search has run: the searches below, not 2B."""

    def setUp(self):
        self.root = _start()
        _searched(self.root, ingest.REQUEST_BUDGET_LADDER[-1])

    def _click(self, **extra):
        return self.client.post(f'/atomicdb/request/{self.root.key}/',
                                **extra).json()

    def test_the_click_after_2b_buys_the_searches_below(self):
        body = self._click()

        self.assertEqual(body['status'], 'expanded')
        self.assertTrue(body['subtree'])
        self.assertEqual(body['searches_queued'], 3)
        self.assertEqual(body['subtree_plies'], 1)
        self.assertEqual(
            body['message'],
            'Queued 3 searches of 128M below this position (1 ply deep).')
        # White is better and to move: its three best moves, by the engine's
        # own order, and nothing on the position itself.
        self.assertEqual(
            sorted(Edge.objects.filter(parent=self.root,
                                       child__analysistask__state='PENDING')
                   .values_list('move_uci', flat=True)),
            ['d2d4', 'e2e4', 'g1f3'])
        self.assertFalse(_live(self.root).exists())
        for task in _live():
            self.assertEqual(
                (task.source, task.budget_nodes, task.requested_by),
                (AnalysisTask.Source.USER, SEARCH, ''))
        self.assertEqual(
            RequestLog.objects.filter(position=self.root).count(), 1)

    def test_the_click_leaves_its_receipt_off_the_public_feed(self):
        self._click()

        event = DBEvent.objects.get(kind=ingest.SUBTREE_EVENT)
        self.assertEqual((event.payload['key'], event.payload['queued']),
                         (self.root.key, 3))
        self.assertIn(ingest.SUBTREE_EVENT, views.FEED_HIDDEN_KINDS)

    def test_a_double_click_does_not_buy_twice(self):
        first = self._click()
        second = self._click()

        self.assertEqual(first['searches_queued'], 3)
        self.assertEqual(second, {'status': 'already-requested'})
        self.assertEqual(_live().count(), 3)

    def test_nor_when_the_searches_sit_further_down(self):
        """The parent's dedup looks one ply down; the walk covers the rest.

        The three best replies already have their search, so the click buys
        the level under them, and a second click from the same visitor finds
        no live request on the children.  It walks again and meets its own
        searches still waiting: nothing new is bought.
        """
        ingest.expand(self.root)
        for uci in ('e2e4', 'd2d4', 'g1f3'):
            _searched(_child(self.root, uci))

        first = self._click()
        bought = _live().count()
        second = self._click()

        self.assertEqual(first['status'], 'expanded')
        self.assertEqual(first['subtree_plies'], 2)
        self.assertEqual(first['searches_queued'], bought)
        self.assertGreater(bought, 3)
        self.assertEqual(second['status'], 'expanded')
        self.assertEqual(second['searches_queued'], 0)
        self.assertEqual(second['searches_waiting'], bought)
        self.assertIn('Nothing new to queue', second['message'])
        self.assertEqual(_live().count(), bought)

    def test_nothing_left_to_buy_is_said_and_still_leaves_one_receipt(self):
        ingest.expand(self.root)
        Position.objects.filter(
            pk__in=Edge.objects.filter(parent=self.root).values('child_id')
        ).update(status='DRAW', closure='TERMINAL')

        body = self._click()

        self.assertEqual(body['status'], 'saturated')
        self.assertEqual(body['searches_queued'], 0)
        self.assertTrue(body['message'].startswith('Nothing left to queue'))
        self.assertFalse(_live().exists())
        self.assertEqual(
            RequestLog.objects.filter(position=self.root).count(), 1)

    def test_the_explorer_has_somewhere_to_say_it(self):
        response = self.client.get(f'/atomicdb/explore/{self.root.key}/')

        self.assertContains(response, 'd.subtree')
        self.assertContains(response, 'd.message')
        # And the button says what the click will buy before anyone clicks.
        self.assertContains(
            response, 'this queues up to 78 searches of 128M on the lines '
                      'below it')

    def test_a_button_still_on_the_ladder_promises_nothing_of_the_sort(self):
        fresh = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))

        response = self.client.get(f'/atomicdb/explore/{fresh.key}/')

        self.assertNotContains(response, 'searches of 128M on the lines')


class SliderTests(TestCase):
    """The last stop of the depth slider buys the same searches, on demand."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.url = f'/atomicdb/request/{self.root.key}/'

    def test_a_chooser_buys_the_searches_below_without_the_ladder(self):
        _worker('wolfram')

        body = _signed_in('wolfram').post(
            self.url, {'budget': SUBTREE, 'confirm': '1'}).json()

        # Nothing was ever searched here: no engine line and no value, so
        # White is the attacker and its first legal moves stand in for the
        # order nobody has measured yet.
        self.assertEqual(body['status'], 'expanded')
        self.assertEqual(body['searches_queued'], ingest.FRONTIER_BLIND_WIDTH)
        self.assertFalse(_live(self.root).exists())
        self.assertEqual(
            set(_live().values_list('requested_by', 'budget_nodes')),
            {('wolfram', SEARCH)})

    def test_a_repeated_last_stop_does_not_buy_twice(self):
        # A chosen stop skips the parent's dedup on purpose (a chosen rung is
        # "what is queued falls short"), so this one is the walk's alone.
        _worker('wolfram')
        client = _signed_in('wolfram')
        client.post(self.url, {'budget': SUBTREE, 'confirm': '1'})

        again = client.post(self.url,
                            {'budget': SUBTREE, 'confirm': '1'}).json()

        self.assertEqual(again['searches_queued'], 0)
        self.assertEqual(again['searches_waiting'],
                         ingest.FRONTIER_BLIND_WIDTH)
        self.assertEqual(_live().count(), ingest.FRONTIER_BLIND_WIDTH)

    def test_an_anonymous_choice_of_the_last_stop_is_dropped(self):
        body = Client().post(self.url,
                             {'budget': SUBTREE, 'confirm': '1'}).json()

        self.assertEqual(body['status'], 'queued')
        self.assertEqual(_live(self.root).get().budget_nodes, SEARCH)
        self.assertEqual(_live().count(), 1)

    def test_so_is_the_choice_of_an_account_that_may_not_choose(self):
        worker_account('lesha', 'p')

        body = _signed_in('lesha').post(
            self.url, {'budget': SUBTREE, 'confirm': '1'}).json()

        self.assertEqual(body['status'], 'queued')
        self.assertEqual(_live().count(), 1)

    def test_the_explorer_names_the_last_stop_and_asks_first(self):
        _worker('wolfram')

        response = _signed_in('wolfram').get(
            f'/atomicdb/explore/{self.root.key}/')

        self.assertContains(response, '78×128M')
        self.assertContains(
            response, 'This queues up to 78 searches of 128M below this '
                      'position. Sure?')
        self.assertContains(response, 'id="depthunit"')


class ApiTests(TestCase):
    """``budget=10000000000`` through the scripted door."""

    def setUp(self):
        self.client = Client()
        _worker('torom')
        self.fen = logic.start_fen()

    def _post(self, **fields):
        return self.client.post('/atomicdb/api/request',
                                {'fen': self.fen, **fields},
                                REMOTE_ADDR='10.0.0.1')

    def test_the_old_top_value_buys_the_searches_below(self):
        payload = self._post(budget=str(SUBTREE), username='torom',
                             password='p').json()

        self.assertEqual(payload['status'], 'expanded')
        self.assertTrue(payload['subtree'])
        self.assertEqual(payload['searches_queued'],
                         ingest.FRONTIER_BLIND_WIDTH)
        self.assertIn('below this position', payload['message'])
        # Nothing was queued on the position itself.
        self.assertIsNone(payload['task'])
        self.assertEqual(
            set(_live().values_list('requested_by', 'budget_nodes')),
            {('torom', SEARCH)})

    def test_with_only_if_missing_it_is_refused_for_everybody(self):
        # A FEN the tree does not have, so a write would show as a seed.
        self.fen = logic.apply_move(logic.apply_move(self.fen, 'g1f3'),
                                    'g8f6')
        for fields in ({'username': 'torom', 'password': 'p'}, {}):
            with self.subTest(signed=bool(fields)):
                response = self._post(budget=str(SUBTREE),
                                      only_if_missing='1', **fields)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['status'], 'refused')
                self.assertIn('only_if_missing', response.json()['reason'])
        self.assertFalse(AnalysisTask.objects.exists())
        self.assertFalse(ApiRequestLog.objects.exists())
        # Refused before anything is written: the FEN is not even seeded.
        self.assertFalse(Position.objects.filter(
            key=logic.key_of(logic.canonical_fen(self.fen))).exists())

    def test_a_budget_off_the_list_names_what_is_on_it(self):
        response = self._post(budget='999', username='torom', password='p')

        self.assertEqual(response.status_code, 400)
        reason = response.json()['reason']
        for value in (*ingest.REQUEST_BUDGET_LADDER, SUBTREE):
            self.assertIn(str(value), reason)

    def test_without_the_right_to_choose_it_is_the_plain_request(self):
        payload = self._post(budget=str(SUBTREE)).json()

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['budget_nodes'], SEARCH)
        self.assertEqual(_live().count(), 1)

    def test_the_ingest_door_will_not_mix_the_two_either(self):
        pos = ingest.get_or_create_position(self.fen)

        with self.assertRaises(ValueError):
            ingest.request_analysis(pos, budget_floor=SUBTREE,
                                    only_if_missing=True)


class WalkShapeTests(TestCase):
    """Which positions the walk asks about, and in which order."""

    def test_white_ahead_and_to_move_takes_its_best_three(self):
        root = _node('S-white', 'w', eval_cp=40)
        kids = _children(root, [f'S-white-{i}' for i in range(5)], 'b',
                         multipv=True)

        outcome = _subtree(root)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(outcome.detail['searches_queued'], 3)
        self.assertEqual(set(_live().values_list('position_id', flat=True)),
                         {kid.key for kid in kids[:3]})

    def test_black_ahead_makes_white_the_defender(self):
        # Same position, Black better: White to move must answer everything.
        root = _node('S-black', 'w', eval_cp=-40)
        _children(root, [f'S-black-{i}' for i in range(5)], 'b',
                  multipv=True)

        outcome = _subtree(root)

        self.assertEqual(outcome.detail['searches_queued'], 5)

    def test_the_backed_value_decides_before_the_own_eval(self):
        root = _node('S-backed', 'w', eval_cp=40, backed_eval=-40)
        _children(root, [f'S-backed-{i}' for i in range(5)], 'b',
                  multipv=True)

        self.assertEqual(_subtree(root).detail['searches_queued'], 5)

    def test_a_level_value_keeps_white_as_the_attacker(self):
        root = _node('S-level', 'w', eval_cp=0)
        _children(root, [f'S-level-{i}' for i in range(5)], 'b',
                  multipv=True)

        self.assertEqual(_subtree(root).detail['searches_queued'], 3)

    def test_the_attacker_holds_for_the_whole_walk(self):
        # Black is better.  White to move at the root answers both moves;
        # one ply down Black is to move and needs only its best three.
        root = _node('S-hold', 'w', eval_cp=-40)
        left, right = _children(root, ['S-hold-l', 'S-hold-r'], 'b')
        for parent in (left, right):
            _searched(parent)
            _children(parent, [f'{parent.key[:6]}-{i}' for i in range(4)],
                      'w', multipv=True)

        outcome = _subtree(root)

        self.assertEqual(outcome.detail['searches_queued'], 6)
        self.assertEqual(outcome.detail['subtree_plies'], 2)
        for parent in (left, right):
            fourth = Edge.objects.get(parent=parent, move_uci='u3').child
            self.assertFalse(_live(fourth).exists())

    def test_solved_children_never_take_a_slot(self):
        root = _node('S-solved', 'w', eval_cp=40)
        kids = _children(root, [f'S-solved-{i}' for i in range(4)], 'b',
                         multipv=True)
        Position.objects.filter(pk=kids[0].pk).update(status='WHITE_WIN',
                                                      closure='MATE_PV')

        outcome = _subtree(root)

        self.assertFalse(AnalysisTask.objects.filter(
            position=kids[0]).exists())
        self.assertEqual(set(_live().values_list('position_id', flat=True)),
                         {kid.key for kid in kids[1:]})
        self.assertEqual(outcome.detail['searches_queued'], 3)

    def test_a_searched_position_is_walked_through_not_bought_again(self):
        root = _node('S-walk', 'w', eval_cp=40)
        middle, = _children(root, ['S-walk-m'], 'b', multipv=True)
        _searched(middle)
        leaves = _children(middle, ['S-walk-a', 'S-walk-b'], 'w')

        outcome = _subtree(root)

        self.assertFalse(_live(middle).exists())
        self.assertEqual(set(_live().values_list('position_id', flat=True)),
                         {leaf.key for leaf in leaves})
        self.assertEqual(outcome.detail['positions_searched'], 1)
        self.assertEqual(outcome.detail['subtree_plies'], 2)

    def test_a_rung_bought_but_never_searched_is_not_a_search(self):
        # Absorbed: COMPLETED with no nodes.  Walking through it would order
        # its children by a search nobody ran; it is bought instead.
        root = _node('S-absorbed', 'w', eval_cp=40)
        kid, = _children(root, ['S-absorbed-k'], 'b', multipv=True)
        AnalysisTask.objects.create(
            position=kid, generation=0, budget_nodes=SEARCH,
            state=AnalysisTask.TState.COMPLETED)

        outcome = _subtree(root)

        self.assertEqual(outcome.detail['searches_queued'], 1)
        self.assertEqual(_live(kid).get().budget_nodes, SEARCH)

    def test_a_position_in_flight_is_not_walked_through(self):
        # Its search has not landed, so nothing orders its children yet.
        root = _node('S-flight', 'w', eval_cp=40)
        kid, = _children(root, ['S-flight-k'], 'b', multipv=True)
        _children(kid, ['S-flight-a', 'S-flight-b'], 'w')
        AnalysisTask.objects.create(
            position=kid, generation=0, budget_nodes=SEARCH,
            source=AnalysisTask.Source.USER, requested_by='wolfram',
            state=AnalysisTask.TState.LEASED, machine='box1')

        outcome = _subtree(root, requested_by='wolfram')

        self.assertEqual(outcome.detail['searches_waiting'], 1)
        self.assertEqual(outcome.detail['searches_queued'], 0)
        self.assertEqual(_live().count(), 1)

    def test_the_route_follows_the_path(self):
        root = _node('S-route', 'w', eval_cp=40)
        middle, = _children(root, ['S-route-m'], 'b', multipv=True)
        _searched(middle)
        leaf, = _children(middle, ['S-route-l'], 'w')

        _subtree(root, requested_by='wolfram', route='e2e4,e7e5')

        task = _live(leaf).get()
        self.assertEqual(task.route, 'e2e4,e7e5,u0,u0')
        self.assertEqual(task.requested_by, 'wolfram')

    def test_without_a_route_there_is_none_to_extend(self):
        root = _node('S-noroute', 'w', eval_cp=40)
        kid, = _children(root, ['S-noroute-k'], 'b', multipv=True)

        _subtree(root)

        self.assertEqual(_live(kid).get().route, '')


class WalkBoundTests(TestCase):
    """Never above 128M, never more than 78, and the best lines first."""

    def test_no_search_is_bought_above_128m(self):
        # Three positions whose own ladder would ask for more: a 2B rung
        # bought without a search, a visit count that climbs to 2B, and a
        # mate-band value with no distance.
        root = _node('B-price', 'w', eval_cp=40)
        absorbed, visited, band = _children(
            root, ['B-price-a', 'B-price-v', 'B-price-m'], 'b', multipv=True)
        AnalysisTask.objects.create(
            position=absorbed, generation=0,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[-1],
            state=AnalysisTask.TState.COMPLETED)
        Position.objects.filter(pk=visited.pk).update(visits=4)
        Position.objects.filter(pk=band.pk).update(
            eval_cp=ingest.MATE_BAND + 100)

        outcome = _subtree(root)

        self.assertEqual(outcome.detail['searches_queued'], 3)
        self.assertEqual(set(_live().values_list('budget_nodes', flat=True)),
                         {SEARCH})

    def test_one_click_buys_78_at_most(self):
        root = _node('B-cap', 'w', eval_cp=40)
        lines = _children(root, ['B-cap-a', 'B-cap-b', 'B-cap-c'], 'b',
                          multipv=True)
        for line in lines:
            _searched(line)
            _children(line, [f'B-cap-{line.key[:6]}-{i}' for i in range(30)],
                      'w')

        outcome = _subtree(root)

        self.assertEqual(outcome.detail['searches_queued'], 78)
        self.assertEqual(outcome.detail['subtree_stop'], 'cap')
        self.assertEqual(outcome.detail['subtree_plies'], 2)
        self.assertEqual(_live().count(), 78)
        # The first two lines got all of theirs; the cut fell on the third.
        for line, expected in zip(lines, (30, 30, 18)):
            self.assertEqual(
                _live().filter(position__edges_in__parent=line).count(),
                expected)

    @mock.patch('atomicdb.ingest.SUBTREE_TASK_CAP', 5)
    def test_the_best_line_gets_its_searches_when_the_cap_cuts(self):
        root = _node('B-order', 'w', eval_cp=40)
        first, second, third = _children(
            root, ['B-order-1', 'B-order-2', 'B-order-3'], 'b')
        # The engine ranks the third move best and the first move worst, so
        # the walk takes them in that order, whatever the movegen order.
        Position.objects.filter(pk=root.pk).update(last_analysis=[
            {'move': 'u2', 'eval_cp': 60, 'mate': None, 'pv': ['u2']},
            {'move': 'u1', 'eval_cp': 50, 'mate': None, 'pv': ['u1']},
            {'move': 'u0', 'eval_cp': 40, 'mate': None, 'pv': ['u0']}])
        replies = {}
        for line in (first, second, third):
            _searched(line)
            replies[line.key] = _children(
                line, [f'B-order-{line.key[:6]}-{i}' for i in range(4)], 'w')

        _subtree(root)

        # Black has to answer everything in each line, so the best line takes
        # four of the five searches and the second best the fifth.
        bought = set(_live().values_list('position_id', flat=True))
        self.assertEqual(
            bought,
            {kid.key for kid in replies[third.key]} | {
                replies[second.key][0].key})

    @mock.patch('atomicdb.ingest.FRONTIER_DESCENT_MAX_PLIES', 2)
    def test_the_ply_guard_stops_a_long_searched_line(self):
        root = _node('B-guard', 'w', eval_cp=40)
        node, stm = root, 'b'
        for depth_index in range(4):
            node, = _children(node, [f'B-guard-{depth_index}'], stm,
                              multipv=True)
            _searched(node)
            stm = 'w' if stm == 'b' else 'b'

        outcome = _subtree(root)

        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['subtree_stop'], 'depth-guard')
        self.assertFalse(_live().exists())

    @mock.patch('atomicdb.ingest.SUBTREE_CHUNK', 1)
    @mock.patch('atomicdb.ingest.SUBTREE_SCAN_CAP', 3)
    def test_the_rows_one_click_reads_are_bounded(self):
        root = _node('B-scan', 'w', eval_cp=40)
        for line in _children(root, [f'B-scan-{i}' for i in range(3)], 'b',
                              multipv=True):
            _searched(line)
            _children(line, [f'B-scan-{line.key[:6]}-{i}' for i in range(2)],
                      'w')

        outcome = _subtree(root)

        # The root's three replies were read and all of them were searched;
        # reading further would cross the bound, so the click stops there and
        # says so rather than claiming there was nothing left.
        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['subtree_stop'], 'scan-limit')
        self.assertEqual(outcome.detail['positions_scanned'], 3)
        self.assertFalse(_live().exists())
        self.assertIn('one request can reach',
                      views._subtree_message(outcome.detail))

    def test_the_cost_does_not_grow_with_the_width_of_a_level(self):
        """One statement per chunk of a level, whatever its width."""
        alias = current_alias()

        def statements(name, width):
            # Black to move and White better: Black has to answer everything,
            # and every answer is a searched leaf the walk reads through.
            root = _node(name, 'b', eval_cp=40)
            for kid in _children(root, [f'{name}-{i}' for i in range(width)],
                                 'w'):
                _searched(kid)
            with CaptureQueriesContext(connections[alias]) as captured:
                _subtree(root)
            return len(captured.captured_queries)

        self.assertEqual(statements('B-narrow', 5), statements('B-wide', 40))


class TranspositionTests(TestCase):
    """The tree is a DAG: transpositions and cycles."""

    def test_a_transposition_is_asked_about_once(self):
        root = _node('T-root', 'w', eval_cp=40)
        left, right = _children(root, ['T-left', 'T-right'], 'b',
                                multipv=True)
        shared = _node('T-shared', 'w')
        for parent, own in ((left, 'T-left-own'), (right, 'T-right-own')):
            _searched(parent)
            Edge.objects.create(parent=parent, move_uci='s0', child=shared)
            Edge.objects.create(parent=parent, move_uci='s1',
                                child=_node(own, 'w'))

        outcome = _subtree(root, route='a1a2')

        self.assertEqual(outcome.detail['searches_queued'], 3)
        task = _live(shared).get()
        # Reached first through the better line, and named by that path.
        self.assertEqual(task.route, 'a1a2,u0,s0')

    def test_a_cycle_back_to_the_root_ends_the_walk(self):
        # Stripped of the counters, 1.Nf3 Nf6 2.Ng1 Ng8 IS the start
        # position: the DAG closes cycles for real.
        root = _node('T-cycle', 'w', eval_cp=40)
        knight, = _children(root, ['T-cycle-1'], 'b', multipv=True)
        back, = _children(knight, ['T-cycle-2'], 'w', multipv=True)
        again, = _children(back, ['T-cycle-3'], 'b', multipv=True)
        Edge.objects.create(parent=again, move_uci='u0', child=root)
        Position.objects.filter(pk=again.pk).update(last_analysis=[
            {'move': 'u0', 'eval_cp': 0, 'mate': None, 'pv': ['u0']}])
        for node in (knight, back, again):
            _searched(node)

        outcome = _subtree(root)

        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['subtree_stop'], 'exhausted')
        self.assertEqual(outcome.detail['positions_searched'], 3)
        self.assertFalse(_live().exists())


class WhoseSearchTests(TestCase):
    """A search somebody already has is never bought twice."""

    def setUp(self):
        self.root = _node('W-root', 'w', eval_cp=40)
        self.kids = _children(self.root, ['W-a', 'W-b', 'W-c'], 'b',
                              multipv=True)

    def _task(self, pos, **fields):
        return AnalysisTask.objects.create(
            position=pos, generation=0, budget_nodes=SEARCH, **fields)

    def test_somebody_elses_request_is_left_to_them(self):
        theirs = self._task(self.kids[0], source=AnalysisTask.Source.USER,
                            requested_by='eclipsia')

        outcome = _subtree(self.root, requested_by='wolfram')

        self.assertEqual(outcome.detail['positions_others'], 1)
        self.assertEqual(outcome.detail['searches_queued'], 2)
        theirs.refresh_from_db()
        self.assertEqual((theirs.requested_by, theirs.also_requested_by),
                         ('eclipsia', []))

    def test_server_work_is_promoted_and_counts_as_bought(self):
        picked = self._task(self.kids[0], source=AnalysisTask.Source.AUTO)

        outcome = _subtree(self.root, requested_by='wolfram')

        self.assertEqual(outcome.detail['searches_queued'], 3)
        picked.refresh_from_db()
        self.assertEqual((picked.source, picked.requested_by),
                         (AnalysisTask.Source.USER, 'wolfram'))
        self.assertEqual(AnalysisTask.objects.filter(
            position=self.kids[0]).count(), 1)

    def test_a_request_this_account_backs_is_its_own(self):
        self._task(self.kids[0], source=AnalysisTask.Source.USER,
                   requested_by='eclipsia', also_requested_by=['wolfram'],
                   backers=1)

        outcome = _subtree(self.root, requested_by='wolfram')

        self.assertEqual(outcome.detail['searches_waiting'], 1)
        self.assertEqual(outcome.detail['searches_queued'], 2)

    def test_anonymous_clicks_share_the_anonymous_name(self):
        _subtree(self.root)

        again = _subtree(self.root)

        self.assertEqual(again.detail['searches_waiting'], 3)
        self.assertEqual(again.detail['searches_queued'], 0)
        self.assertEqual(again, 'expanded')
        self.assertEqual(_live().count(), 3)

    def test_a_named_click_leaves_the_anonymous_searches_alone(self):
        # Nobody's name is on the anonymous searches, so they are not this
        # account's; with nothing else left to buy, the click says so.
        _subtree(self.root)

        outcome = _subtree(self.root, requested_by='wolfram')

        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['positions_others'], 3)
        self.assertEqual(_live().count(), 3)

    def test_a_closed_position_buys_nothing(self):
        Position.objects.filter(pk=self.root.pk).update(status='DRAW',
                                                        closure='MINIMAX')

        self.assertEqual(_subtree(self.root), 'already-solved')
        self.assertFalse(_live().exists())


class PagesTests(TestCase):
    """The pages that describe the ladder say what it is now."""

    def test_the_docs_describe_the_ladder_and_the_searches_below(self):
        body = self.client.get('/atomicdb/docs/').content.decode()

        self.assertIn('up the ladder of 128M, 512M and 2B nodes', body)
        self.assertIn('the next request queues up to 78 searches of 128M',
                      body)
        self.assertNotIn('10B', body)

    def test_the_method_page_too(self):
        body = self.client.get('/atomicdb/method/').content.decode()

        self.assertIn('128M, 512M and then 2B nodes', body)
        self.assertIn('to 78 searches of 128M on the lines below the position',
                      body)
        self.assertNotIn('10B', body)
