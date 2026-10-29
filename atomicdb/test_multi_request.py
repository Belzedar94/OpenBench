"""Tick several moves in the explorer and request them all with one click.

The move list of an unsolved position carries a box on every move that can
be requested, for a signed-in visitor, and a bar under the table with one
button that requests every ticked move (``views.api_request_moves``).

What is pinned here:

  * who gets the boxes: a signed-in visitor on an unsolved position, on the
    rows that can be requested; an anonymous visitor gets the table as it
    was, and the rows keep the shape the other tests read;
  * which moves count: legal ones, deduplicated, in the order given, at most
    ``MULTI_REQUEST_CAP`` a click;
  * every move takes the single path of a request (``views._queue_request``),
    so dedup, ladder, account cap and receipts apply move by move;
  * a move that is not a node yet is made the way following it makes it, and
    a move back into the line is skipped;
  * the bar has its own depth choice, for whoever may choose a rung: every
    rung of the ladder whatever the page's own depth, and never the searches
    below a position, which are refused when posted; it reads no slider;
  * a chosen rung is a target: a move already searched that deep is skipped
    and says so, every other move gets exactly that rung, and with no rung
    chosen each move climbs its own ladder as before;
  * one search a move at most: a move searched to the top of the ladder buys
    nothing and says so, where its own button would buy the searches below
    it;
  * the account's queue cap, before the batch and part way through it;
  * the route of the page reaching every task, and the tally coming back.
"""

import re
from unittest import mock

from django.contrib.auth.models import User
from django.db import OperationalError
from django.test import Client, override_settings
from django.utils import timezone

from . import depth, ingest, logic, views
from .models import (AnalysisTask, Campaign, DBEvent, Edge, Position,
                     RequestLog, WorkerPing)
from .testing import TestCase, worker_account


LADDER = ingest.REQUEST_BUDGET_LADDER
# The slider's last stop: the searches below a position, not a rung.
SUBTREE = ingest.SUBTREE_REQUEST_NODES
# A box, whatever order the HTML minifier leaves its attributes in.
BOX = re.compile(r'<input\b[^>]*\bdata-ms="([^"]+)"')
# What a page names only when it has the depth slider (test_depth_slider).
SLIDER_MARKS = ('depthrow', 'atomicdb-depth-rungs', 'chosenRung')


def _root():
    return ingest.get_or_create_position(logic.start_fen())


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


def _signed_in(username='eclipsia', staff=False):
    user = worker_account(username)
    if staff:
        user.is_staff = True
        user.save(update_fields=['is_staff'])
    client = Client()
    client.login(username=username, password='pw')
    return client


def _child_of(parent, uci):
    return Position.objects.get(
        key=logic.key_of(logic.apply_move(parent.fen, uci)))


def _pending(position):
    return AnalysisTask.objects.get(position=position,
                                    state=AnalysisTask.TState.PENDING)


def _ran(position, budget):
    """A search of ``budget`` that ran on ``position``, as a worker leaves it."""
    position.refresh_from_db()
    AnalysisTask.objects.create(
        position=position, generation=position.visits, budget_nodes=budget,
        nodes_searched=budget, state=AnalysisTask.TState.COMPLETED,
        source=AnalysisTask.Source.USER, machine='box0')
    Position.objects.filter(pk=position.pk).update(visits=position.visits + 1)


class WhoGetsTheBoxesTests(TestCase):
    """A box on every move that can be requested, and only for the signed in."""

    def setUp(self):
        self.root = _root()
        # e2e4 is a node and open, d2d4 a node and solved, and the other
        # eighteen replies are not nodes yet.
        _chain(self.root, ['e2e4'])
        solved = _chain(self.root, ['d2d4'])[0]
        Position.objects.filter(pk=solved.pk).update(status='WHITE_WIN',
                                                     closure='MINIMAX')
        self.offtree = [uci for uci in logic.legal_moves(self.root.fen)
                        if uci not in ('e2e4', 'd2d4')]
        self.url = f'/atomicdb/explore/{self.root.key}/'

    def test_an_anonymous_visitor_gets_the_table_as_it_was(self):
        body = Client().get(self.url).content.decode()

        self.assertEqual(BOX.findall(body), [])
        for mark in ('msbar', '/atomicdb/request-moves/', 'Select all',
                     'min-width:3rem'):
            self.assertNotIn(mark, body)
        self.assertIn('<th></th><th>move</th>', body)

    def test_the_chip_column_keeps_room_for_the_box(self):
        """On a phone the table is wider than the screen and every column
        shrinks to its narrowest; without a floor the box drops under the
        chip and every row doubles in height."""
        body = _signed_in().get(self.url).content.decode()

        self.assertRegex(body, r'<th style="min-width:3rem;'
                               r'box-sizing:content-box"></th><th>move</th>')

    def test_a_signed_in_visitor_gets_one_on_every_requestable_move(self):
        body = _signed_in().get(self.url).content.decode()

        boxes = BOX.findall(body)
        self.assertEqual(sorted(boxes), sorted(['e2e4', *self.offtree]))
        self.assertIn('aria-label="Select e2e4"', body)
        self.assertIn('id="msbar"', body)

    def test_a_solved_reply_gets_none(self):
        body = _signed_in().get(self.url).content.decode()

        self.assertNotIn('d2d4', BOX.findall(body))

    def test_a_solved_position_offers_no_box_at_all(self):
        Position.objects.filter(pk=self.root.pk).update(status='WHITE_WIN',
                                                        closure='MINIMAX')

        body = _signed_in().get(self.url).content.decode()

        self.assertEqual(BOX.findall(body), [])
        self.assertNotIn('msbar', body)

    def test_the_rows_keep_the_shape_the_other_tests_read(self):
        """A box in a first cell of its own, or a mark on the <tr>, would hide
        every row of the tree from the readers of ``<tr><td><span class="chip``
        on a signed-in page.  The box sits inside the chip cell, after it."""
        body = _signed_in().get(self.url).content.decode()

        rows = re.findall(r'<tr><td><span class="chip.*?</tr>', body)
        tree = [row for row in rows if '/atomicdb/explore/' in row]
        self.assertEqual(len(rows), 2 + len(self.offtree))
        self.assertEqual(len(tree), 2)
        open_row = next(row for row in tree if '<strong>e2e4</strong>' in row)
        first_cell = re.match(r'<tr><td>(.*?)</td>', open_row).group(1)
        self.assertEqual(BOX.findall(first_cell), ['e2e4'])

    def test_an_account_that_may_not_choose_names_no_depth_at_all(self):
        """No depth choice in the bar and no trace of the slider: a script
        naming a control the page does not have is the leak test_depth_slider
        exists to catch."""
        body = _signed_in('lesha').get(self.url).content.decode()

        self.assertTrue(BOX.findall(body))
        self.assertEqual([mark for mark in SLIDER_MARKS if mark in body], [])
        self.assertNotIn('msdepth', body)
        self.assertRegex(body, r'function pickedRung\(\) \{\s*return null;')

    def test_a_chooser_gets_the_bar_s_own_choice_and_the_bar_reads_no_slider(
            self):
        """The slider stays for the page's own button; the bar has its own
        depth choice (BarDepthTests) and never reads the slider, so the two
        controls cannot disagree."""
        body = _signed_in('belzedar', staff=True).get(self.url).content.decode()
        bar = body[body.index('id="msbar"'):]

        self.assertTrue(BOX.findall(body))
        self.assertEqual(sorted(mark for mark in SLIDER_MARKS if mark in body),
                         sorted(SLIDER_MARKS))
        self.assertIn('id="msdepth"', bar)
        self.assertRegex(bar, r'function pickedRung\(\) \{\s*const nodes = '
                              r'Number\(pick\.value\);')
        for slider in ("getElementById('depth')", 'atomicdb-depth-rungs',
                       'sliderRung'):
            self.assertNotIn(slider, bar)


class BlockedRowTests(TestCase):
    """The knight loop of test_repetition_ui: after 1.Nf3 Nf6 2.Ng1, the reply
    ...Ng8 is the start position again, so it re-enters the line."""

    def setUp(self):
        self.root = _root()
        self.route = ['g1f3', 'g8f6', 'f3g1']
        self.p3 = _chain(self.root, self.route)[-1]
        self.assertEqual(
            logic.key_of(logic.apply_move(self.p3.fen, 'f6g8')), self.root.key)
        self.play = ','.join(self.route)
        self.page = f'/atomicdb/explore/{self.p3.key}/?play={self.play}'
        self.url = f'/atomicdb/request-moves/{self.p3.key}/'

    def test_a_reply_into_the_line_that_is_not_a_node_gets_no_box(self):
        boxes = BOX.findall(_signed_in().get(self.page).content.decode())

        self.assertNotIn('f6g8', boxes)
        self.assertIn('b8c6', boxes)

    def test_nor_does_one_that_is_a_node(self):
        Edge.objects.create(parent=self.p3, move_uci='f6g8', child=self.root)

        boxes = BOX.findall(_signed_in().get(self.page).content.decode())

        self.assertNotIn('f6g8', boxes)

    def test_the_endpoint_skips_it_and_builds_nothing(self):
        """``goto`` writes nothing for that move, and neither does this."""
        response = _signed_in().post(self.url, {'moves': 'f6g8,b8c6',
                                                'route': self.play})

        self.assertEqual(response.json()['moves'],
                         [{'uci': 'f6g8', 'status': 'blocked'},
                          {'uci': 'b8c6', 'status': 'queued'}])
        self.assertFalse(Edge.objects.filter(parent=self.p3,
                                             move_uci='f6g8').exists())
        self.assertFalse(AnalysisTask.objects.filter(
            position=self.root).exists())

    def test_without_a_route_the_canonical_line_blocks_it_too(self):
        """The page draws the canonical lineage without ``?play=``, and that
        is the line the move would re-enter."""
        response = _signed_in().post(self.url, {'moves': 'f6g8'})

        self.assertEqual(response.json()['counts'], {'blocked': 1})
        self.assertFalse(AnalysisTask.objects.exists())


class RequestMovesTests(TestCase):
    """The endpoint: who may call it, which moves count, what it makes."""

    def setUp(self):
        self.root = _root()
        self.url = f'/atomicdb/request-moves/{self.root.key}/'

    def test_an_anonymous_caller_is_refused(self):
        response = Client().post(self.url, {'moves': 'e2e4'})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {'status': 'login-required'})
        self.assertFalse(AnalysisTask.objects.exists())
        self.assertFalse(Edge.objects.filter(parent=self.root).exists())

    def test_a_get_is_not_a_request(self):
        self.assertEqual(_signed_in().get(self.url).status_code, 405)

    def test_an_unknown_position_is_a_404(self):
        response = _signed_in().post(f'/atomicdb/request-moves/{"0" * 64}/',
                                     {'moves': 'e2e4'})

        self.assertEqual(response.status_code, 404)

    def test_a_solved_position_buys_nothing(self):
        Position.objects.filter(pk=self.root.pk).update(status='DRAW',
                                                        closure='MINIMAX')

        response = _signed_in().post(self.url, {'moves': 'e2e4'})

        self.assertEqual(response.json(), {'status': 'already-solved'})
        self.assertFalse(AnalysisTask.objects.exists())

    def test_legal_moves_deduplicated_in_the_order_given(self):
        response = _signed_in().post(
            self.url, {'moves': 'g1f3, e2e4,zz,g1f3,e2e5,,d2d4'})

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'done')
        self.assertEqual([move['uci'] for move in body['moves']],
                         ['g1f3', 'e2e4', 'd2d4'])
        self.assertEqual(body['ignored'], ['zz', 'e2e5'])
        self.assertEqual(body['counts'], {'queued': 3, 'illegal': 2})
        self.assertEqual(body['left_out'], 0)
        # Queued in that order too, each at the first rung of its own ladder
        # and in the name of the account that clicked.
        tasks = list(AnalysisTask.objects.order_by('id'))
        self.assertEqual([task.position_id for task in tasks],
                         [_child_of(self.root, uci).key
                          for uci in ('g1f3', 'e2e4', 'd2d4')])
        self.assertEqual({task.budget_nodes for task in tasks}, {LADDER[0]})
        self.assertEqual({task.requested_by for task in tasks}, {'eclipsia'})

    def test_a_list_with_nothing_legal_is_refused_whole(self):
        response = _signed_in().post(self.url, {'moves': 'e2e5,zz'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'status': 'no-moves',
                                           'ignored': ['e2e5', 'zz']})
        self.assertFalse(AnalysisTask.objects.exists())

    def test_so_is_an_empty_one(self):
        response = _signed_in().post(self.url, {'moves': ''})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'no-moves')

    def test_the_cap_is_sixty_four_moves(self):
        self.assertEqual(views.MULTI_REQUEST_CAP, 64)

    def test_more_moves_than_the_cap_are_refused_whole(self):
        with mock.patch.object(views, 'MULTI_REQUEST_CAP', 2):
            response = _signed_in().post(self.url,
                                         {'moves': 'e2e4,d2d4,g1f3'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'status': 'too-many', 'cap': 2})
        self.assertFalse(AnalysisTask.objects.exists())
        self.assertFalse(Edge.objects.filter(parent=self.root).exists())

    def test_the_cap_counts_moves_not_tokens(self):
        """Repeats and junk do not count against it."""
        with mock.patch.object(views, 'MULTI_REQUEST_CAP', 2):
            response = _signed_in().post(self.url,
                                         {'moves': 'e2e4,e2e4,zz,d2d4'})

        self.assertEqual(response.json()['counts'],
                         {'queued': 2, 'illegal': 1})

    def test_a_move_that_is_not_a_node_yet_is_made_with_its_edge(self):
        child_key = logic.key_of(logic.apply_move(self.root.fen, 'e2e4'))
        self.assertFalse(Position.objects.filter(key=child_key).exists())

        response = _signed_in().post(self.url, {'moves': 'e2e4'})

        self.assertEqual(response.json()['counts'], {'queued': 1})
        edge = Edge.objects.get(parent=self.root, move_uci='e2e4')
        self.assertEqual(edge.child_id, child_key)
        self.assertEqual(_pending(edge.child).requested_by, 'eclipsia')

    def test_it_is_made_by_the_helper_behind_goto_and_only_when_missing(self):
        """One way for a click to make a node, shared with following a move."""
        _chain(self.root, ['e2e4'])

        with mock.patch.object(views, '_materialise_child',
                               wraps=views._materialise_child) as made:
            _signed_in().post(self.url, {'moves': 'e2e4,d2d4'})
            Client().get(f'/atomicdb/goto/{self.root.key}/g1f3/')

        self.assertEqual([call.args[1] for call in made.call_args_list],
                         ['d2d4', 'g1f3'])

    def test_it_belongs_to_the_campaign_of_its_parent_like_a_followed_move(
            self):
        campaign = Campaign.objects.create(
            name='1. e4', root=self.root, line_san='1. e4',
            state=Campaign.CState.ACTIVE, votes=0, proposed_by='',
            active=True)
        Position.objects.filter(pk=self.root.pk).update(campaign=campaign)

        _signed_in().post(self.url, {'moves': 'e2e4'})

        self.assertEqual(_child_of(self.root, 'e2e4').campaign_id,
                         campaign.id)

    def test_the_place_in_the_queue_is_not_worked_out_per_move(self):
        """A place sorts the whole visitor band, and the bar shows none: one
        click would pay that sort once per move for nothing."""
        with mock.patch.object(views, '_user_queue_ahead') as place:
            response = _signed_in().post(self.url,
                                         {'moves': 'e2e4,d2d4,g1f3'})

        self.assertEqual(response.json()['counts'], {'queued': 3})
        place.assert_not_called()

    def test_the_click_leaves_its_receipts(self):
        _signed_in().post(self.url, {'moves': 'e2e4,d2d4'})

        # The receipt of each request, from the single path...
        self.assertEqual(RequestLog.objects.count(), 2)
        # ...and the audit of the batch, with the other batch button's kind,
        # which the public feed already leaves out.
        event = DBEvent.objects.get(kind='BULK_REQUEST')
        self.assertEqual(event.payload['via'], 'moves')
        self.assertEqual(event.payload['key'], self.root.key)
        self.assertEqual(event.payload['queued'], 2)
        self.assertIn('BULK_REQUEST', views.FEED_HIDDEN_KINDS)

    def test_a_locked_database_answers_busy_and_asks_for_a_retry(self):
        with mock.patch.object(views, '_queue_request',
                               side_effect=OperationalError(
                                   'database is locked')):
            response = _signed_in().post(self.url, {'moves': 'e2e4'})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'busy'})
        self.assertEqual(response['Retry-After'], '2')

    def test_any_other_database_error_is_not_swallowed(self):
        client = _signed_in()

        with mock.patch.object(views, '_queue_request',
                               side_effect=OperationalError('disk I/O error')):
            with self.assertRaises(OperationalError):
                client.post(self.url, {'moves': 'e2e4'})


class DepthTests(TestCase):
    """The rung chosen in the bar goes with every move, for whoever may
    choose."""

    def setUp(self):
        self.root = _root()
        self.url = f'/atomicdb/request-moves/{self.root.key}/'

    def _budgets(self):
        return sorted(AnalysisTask.objects.values_list('budget_nodes',
                                                       flat=True))

    def test_a_chooser_gets_the_chosen_rung_on_every_move(self):
        response = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4,d2d4', 'budget': LADDER[2]})

        self.assertEqual(response.json()['counts'], {'queued': 2})
        self.assertEqual(self._budgets(), [LADDER[2], LADDER[2]])

    def test_an_account_that_may_not_choose_has_its_choice_dropped(self):
        response = _signed_in('lesha').post(
            self.url, {'moves': 'e2e4,d2d4', 'budget': LADDER[2]})

        self.assertEqual(response.json()['counts'], {'queued': 2})
        self.assertEqual(self._budgets(), [LADDER[0], LADDER[0]])

    def test_the_last_stop_is_refused_and_nothing_is_made(self):
        # The searches below a position, not a rung: one position at a time.
        response = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4', 'budget': SUBTREE, 'confirm': '1'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'status': 'top-rung-single'})
        self.assertFalse(AnalysisTask.objects.exists())
        self.assertFalse(Edge.objects.filter(parent=self.root).exists())

    def test_the_last_stop_from_an_account_that_may_not_choose_is_dropped(self):
        """Not refused: that account was never choosing anything."""
        response = _signed_in('lesha').post(
            self.url, {'moves': 'e2e4', 'budget': SUBTREE})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._budgets(), [LADDER[0]])

    def test_the_last_rung_is_a_choice_like_the_others(self):
        # The top of the ladder is 2B now, and nothing about it is single.
        response = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4,d2d4', 'budget': LADDER[-1]})

        self.assertEqual(response.json()['counts'], {'queued': 2})
        self.assertEqual(self._budgets(), [LADDER[-1], LADDER[-1]])

    def test_a_rung_outside_the_ladder_is_refused(self):
        response = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4', 'budget': 999})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'status': 'bad-budget'})
        self.assertFalse(AnalysisTask.objects.exists())


class SpentLadderTests(TestCase):
    """One search a move at most, even where the move's own button buys more.

    From its own page, a move whose 2B search has run buys up to 78 searches
    below it (``ingest.request_subtree``).  Ticked in a batch of sixty-four
    such moves, that would be about five thousand searches from one click,
    the whole of an account's queue.  So the batch buys nothing there and
    says so.
    """

    def setUp(self):
        self.root = _root()
        self.url = f'/atomicdb/request-moves/{self.root.key}/'
        self.spent = _chain(self.root, ['e2e4'])[0]
        _ran(self.spent, LADDER[-1])

    def _nothing_below(self, position):
        self.assertFalse(AnalysisTask.objects.filter(
            position=position, state=AnalysisTask.TState.PENDING).exists())
        self.assertFalse(AnalysisTask.objects.filter(
            position__edges_in__parent=position).exists())

    def test_a_move_searched_to_the_top_buys_nothing_and_says_so(self):
        body = _signed_in().post(self.url, {'moves': 'e2e4,d2d4'}).json()

        self.assertEqual(body['moves'],
                         [{'uci': 'e2e4', 'status': 'ladder-spent'},
                          {'uci': 'd2d4', 'status': 'queued'}])
        self.assertEqual(body['counts'], {'ladder-spent': 1, 'queued': 1})
        self._nothing_below(self.spent)
        self.assertFalse(DBEvent.objects.filter(
            kind=ingest.SUBTREE_EVENT).exists())
        self.assertEqual(AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING).count(), 1)

    def test_a_rung_chosen_for_it_changes_nothing(self):
        # A chosen rung is a target (TargetDepthTests), and the 2B search
        # that ran reaches every rung: the move is skipped as already
        # searched that deep, and still nothing is bought below it.
        body = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4', 'budget': LADDER[-1]}).json()

        self.assertEqual(body['counts'], {'already-analyzed': 1})
        self._nothing_below(self.spent)

    def test_its_own_button_still_buys_the_searches_below(self):
        # The contrast: the batch rule is the batch's, not the move's.
        body = Client().post(f'/atomicdb/request/{self.spent.key}/').json()

        self.assertEqual(body['status'], 'expanded')
        self.assertGreater(body['searches_queued'], 1)

    @override_settings(ATOMICDB_BREADTH_SWAP=True)
    def test_nor_does_the_breadth_swap_buy_replies_in_a_batch(self):
        # 512M ran and the value is open: a plain click there would buy the
        # replies with the swap on.  In a batch the move gets its next rung.
        deep = _chain(self.root, ['d2d4'])[0]
        _ran(deep, LADDER[1])
        Position.objects.filter(pk=deep.pk).update(eval_cp=30)

        body = _signed_in().post(self.url, {'moves': 'd2d4'}).json()

        self.assertEqual(body['counts'], {'queued': 1})
        self.assertEqual(_pending(deep).budget_nodes, LADDER[2])
        self.assertFalse(AnalysisTask.objects.filter(
            position__edges_in__parent=deep).exists())
        self.assertFalse(DBEvent.objects.filter(kind='BREADTH_SWAP').exists())

    def test_the_bar_says_what_full_depth_means(self):
        response = _signed_in().get(f'/atomicdb/explore/{self.root.key}/')

        self.assertContains(
            response, 'at full depth, open a move to search below it')


class TargetDepthTests(TestCase):
    """A chosen rung is a target, not a floor to climb from.

    BManBJ, 29-Oct-2026: "I don't want to accidentally queue something for 2B
    when on its first visit it was analyzed at 512M if I'm just trying to
    update positions to 512M".  As a floor, a rung ticked at 512M bought each
    move its next rung from 512M up, so a move already searched at 512M got
    2B.  As a target (``only_if_missing``, the rule the API already had) a
    move a search already reached that deep is skipped, and every other move
    gets exactly that rung.  With no rung chosen nothing changes: each move
    climbs its own ladder, as from its own button.
    """

    def setUp(self):
        self.root = _root()
        self.url = f'/atomicdb/request-moves/{self.root.key}/'
        # 1.e4 has been searched at 512M, 1.d4 at 128M.
        self.at_512 = _chain(self.root, ['e2e4'])[0]
        self.at_128 = _chain(self.root, ['d2d4'])[0]
        _ran(self.at_512, LADDER[1])
        _ran(self.at_128, LADDER[0])

    def _waiting(self, position):
        return [task.budget_nodes for task in AnalysisTask.objects.filter(
            position=position, state=AnalysisTask.TState.PENDING)]

    def _absorbed(self, position, budget):
        """A rung bought and never searched, on the slot visits point at."""
        position.refresh_from_db()
        row = AnalysisTask.objects.create(
            position=position, generation=position.visits,
            budget_nodes=budget, state=AnalysisTask.TState.COMPLETED,
            source=AnalysisTask.Source.AUTO)
        self.assertTrue(ingest.is_absorbed(row))

    def test_a_chosen_512m_skips_a_move_at_512m_and_buys_512m_for_one_at_128m(
            self):
        body = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4,d2d4', 'budget': LADDER[1]}).json()

        self.assertEqual(body['moves'],
                         [{'uci': 'e2e4', 'status': 'already-analyzed'},
                          {'uci': 'd2d4', 'status': 'queued'}])
        self.assertEqual(body['counts'], {'already-analyzed': 1, 'queued': 1})
        self.assertEqual(self._waiting(self.at_512), [])
        self.assertEqual(self._waiting(self.at_128), [LADDER[1]])
        # A skipped move was not requested: it leaves no receipt, so the next
        # real request for it from this address is not taken for a repeat.
        self.assertFalse(
            RequestLog.objects.filter(position=self.at_512).exists())

    def test_no_rung_chosen_keeps_climbing(self):
        # The slider on its first stop sends nothing: each move gets its own
        # next rung, 2B for the move searched at 512M.
        body = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4,d2d4'}).json()

        self.assertEqual(body['counts'], {'queued': 2})
        self.assertEqual(self._waiting(self.at_512), [LADDER[2]])
        self.assertEqual(self._waiting(self.at_128), [LADDER[1]])

    def test_a_choice_dropped_for_an_account_that_may_not_choose_climbs_too(
            self):
        """Its rung never counted, so it sets no target either."""
        body = _signed_in('lesha').post(
            self.url, {'moves': 'e2e4,d2d4', 'budget': LADDER[1]}).json()

        self.assertEqual(body['counts'], {'queued': 2})
        self.assertEqual(self._waiting(self.at_512), [LADDER[2]])
        self.assertEqual(self._waiting(self.at_128), [LADDER[1]])

    def test_a_deeper_search_covers_a_shallower_choice(self):
        body = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4,d2d4', 'budget': LADDER[0]}).json()

        self.assertEqual(body['counts'], {'already-analyzed': 2})
        self.assertFalse(AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING).exists())

    def test_a_rung_bought_but_never_searched_does_not_raise_the_price(self):
        # 1.d4 also holds a 512M rung absorbed without a search.  The ladder
        # reads it as spent, so as a floor the click bought 2B; as a target
        # it buys the 512M search that never ran, and nothing above it.
        self._absorbed(self.at_128, LADDER[1])

        body = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'd2d4', 'budget': LADDER[1]}).json()

        self.assertEqual(body['counts'], {'queued': 1})
        self.assertEqual(self._waiting(self.at_128), [LADDER[1]])

    def test_a_target_never_reaches_below_the_move(self):
        # Both guarantees at once.  The 2B rung of 1.e4 was absorbed, so its
        # ladder reads as spent, and with no target the batch would answer
        # ladder-spent (SpentLadderTests).  No 2B search ran there, so the
        # target buys exactly that one search: one task, at 2B, and nothing
        # among the replies.
        self._absorbed(self.at_512, LADDER[2])
        self.assertTrue(ingest.ladder_exhausted(self.at_512))

        body = _signed_in('belzedar', staff=True).post(
            self.url, {'moves': 'e2e4', 'budget': LADDER[2]}).json()

        self.assertEqual(body['counts'], {'queued': 1})
        self.assertEqual(self._waiting(self.at_512), [LADDER[2]])
        self.assertFalse(AnalysisTask.objects.filter(
            position__edges_in__parent=self.at_512).exists())
        self.assertFalse(DBEvent.objects.filter(
            kind=ingest.SUBTREE_EVENT).exists())

    def test_the_bar_says_a_chosen_rung_is_a_target(self):
        body = _signed_in('belzedar', staff=True).get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()

        # The note while a rung is chosen, and the receipt's count.
        self.assertIn('nodes is a target: moves already ', body)
        self.assertIn(' already searched that deep', body)


class BarDepthTests(TestCase):
    """The bar's own depth choice, which reads no slider.

    The slider prices the position the page is on: its stops start at that
    position's own next rung, and its first stop sends nothing.  Read by the
    bar, it left 512M out of reach on any page already searched to 128M or
    more, the very case BManBJ asked about (29-Oct-2026: "I'm just trying to
    update positions to 512M").  So the bar offers every rung of the ladder,
    to the accounts the slider is offered to, whatever the page's own depth.
    """

    def setUp(self):
        self.root = _root()
        self.page = f'/atomicdb/explore/{self.root.key}/'
        self.url = f'/atomicdb/request-moves/{self.root.key}/'

    def _options(self, client):
        """``[(value, label)]`` of the bar's depth choice, or None without one.

        Read by pattern, whatever order the HTML minifier leaves the
        attributes in.
        """
        body = client.get(self.page).content.decode()
        select = re.search(r'<select\b[^>]*\bid="msdepth"[^>]*>(.*?)</select>',
                           body, re.S)
        if select is None:
            return None
        return re.findall(r'<option\b[^>]*?value="([^"]*)"[^>]*>([^<]*)'
                          r'</option>', select.group(1))

    def _slider(self, username):
        """The stops the page's own slider offers that account."""
        user = User.objects.get(username=username)
        request = type('R', (), {'user': user})()
        context = depth.context(request,
                                Position.objects.get(pk=self.root.pk))
        return [stop['nodes'] for stop in context.get('depth_rungs', [])]

    def test_a_chooser_gets_every_rung_and_never_the_searches_below(self):
        options = self._options(_signed_in('belzedar', staff=True))

        self.assertEqual(options, [('', "each move's next rung"),
                                   (str(LADDER[0]), '128M'),
                                   (str(LADDER[1]), '512M'),
                                   (str(LADDER[2]), '2B')])
        self.assertNotIn(str(SUBTREE), [value for value, _label in options])

    def test_a_contributor_gets_it_under_the_rule_of_the_slider(self):
        client = _signed_in('wolfram')
        ping = WorkerPing.objects.create(machine='wolfram-box', user='wolfram',
                                         threads=8, hash_mb=1024, os='Linux')
        WorkerPing.objects.filter(pk=ping.pk).update(
            last_result_at=timezone.now())

        self.assertEqual(len(self._options(client)), 1 + len(LADDER))
        self.assertTrue(self._slider('wolfram'))

    def test_an_account_that_may_not_choose_gets_none(self):
        self.assertIsNone(self._options(_signed_in('lesha')))
        self.assertEqual(self._slider('lesha'), [])

    def test_the_choice_does_not_follow_the_depth_of_the_page(self):
        # The page itself is searched to 512M: its slider starts at 2B, and
        # the bar still offers 128M and 512M for the moves.
        client = _signed_in('belzedar', staff=True)
        _ran(self.root, LADDER[1])

        self.assertEqual(self._slider('belzedar'), [LADDER[2], SUBTREE])
        self.assertEqual([value for value, _label in self._options(client)],
                         [''] + [str(rung) for rung in LADDER])

    def test_a_spent_page_has_no_slider_and_the_bar_keeps_its_choice(self):
        client = _signed_in('belzedar', staff=True)
        _ran(self.root, LADDER[2])

        self.assertEqual(self._slider('belzedar'), [])
        self.assertEqual(len(self._options(client)), 1 + len(LADDER))

    def test_512m_on_a_page_searched_to_512m_still_targets_512m(self):
        # Where the slider has no 512M to offer, the bar's 512M reaches the
        # ticked moves, as a target: the move at 128M gets 512M and the move
        # already at 512M is skipped.
        client = _signed_in('belzedar', staff=True)
        _ran(self.root, LADDER[1])
        at_128 = _chain(self.root, ['e2e4'])[0]
        at_512 = _chain(self.root, ['d2d4'])[0]
        _ran(at_128, LADDER[0])
        _ran(at_512, LADDER[1])
        self.assertNotIn(LADDER[1], self._slider('belzedar'))

        body = client.post(self.url, {'moves': 'e2e4,d2d4',
                                      'budget': LADDER[1]}).json()

        self.assertEqual(body['moves'],
                         [{'uci': 'e2e4', 'status': 'queued'},
                          {'uci': 'd2d4', 'status': 'already-analyzed'}])
        self.assertEqual(_pending(at_128).budget_nodes, LADDER[1])
        self.assertFalse(AnalysisTask.objects.filter(
            position=at_512, state=AnalysisTask.TState.PENDING).exists())

    def test_a_budget_posted_by_an_account_that_may_not_choose_is_dropped(
            self):
        """The page offers it no choice, and the endpoint drops one it posts
        anyway: an unsearched move gets the first rung, not the 512M."""
        body = _signed_in('lesha').post(
            self.url, {'moves': 'e2e4', 'budget': LADDER[1]}).json()

        self.assertEqual(body['counts'], {'queued': 1})
        self.assertEqual(_pending(_child_of(self.root, 'e2e4')).budget_nodes,
                         LADDER[0])


class AccountCapTests(TestCase):
    """The per-account queue cap, before the batch and part way through."""

    def setUp(self):
        self.root = _root()
        self.url = f'/atomicdb/request-moves/{self.root.key}/'

    def test_a_full_queue_refuses_before_anything_is_made(self):
        with mock.patch.object(views, 'REQUEST_QUEUE_MAX', 0):
            response = _signed_in().post(self.url, {'moves': 'e2e4,d2d4'})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'queue-full-account'})
        self.assertFalse(Edge.objects.filter(parent=self.root).exists())
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_queue_that_fills_up_part_way_stops_the_batch(self):
        with mock.patch.object(views, 'REQUEST_QUEUE_MAX', 2):
            response = _signed_in().post(
                self.url, {'moves': 'e2e4,d2d4,g1f3,b1c3'})

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'partial')
        self.assertEqual(body['left_out'], 2)
        self.assertEqual(body['counts'], {'queued': 2, 'queue-full': 2})
        self.assertEqual(body['moves'],
                         [{'uci': 'e2e4', 'status': 'queued'},
                          {'uci': 'd2d4', 'status': 'queued'},
                          {'uci': 'g1f3', 'status': 'queue-full'},
                          {'uci': 'b1c3', 'status': 'queue-full'}])
        self.assertEqual(AnalysisTask.objects.count(), 2)
        # The refusal leaves the receipt the single button's refusal leaves.
        self.assertTrue(DBEvent.objects.filter(kind='LANE_CAP_HIT').exists())


class RouteTests(TestCase):
    """The route of the page reaches every task, plus that task's own move."""

    def setUp(self):
        self.root = _root()
        self.after_e4 = _chain(self.root, ['e2e4'])[0]
        self.url = f'/atomicdb/request-moves/{self.after_e4.key}/'

    def _routes(self):
        return sorted(AnalysisTask.objects.values_list('route', flat=True))

    def test_every_task_inherits_the_route_and_its_own_move(self):
        _signed_in().post(self.url, {'moves': 'e7e5,c7c5', 'route': 'e2e4'})

        self.assertEqual(self._routes(), ['e2e4,c7c5', 'e2e4,e7e5'])

    def test_a_route_that_does_not_reach_the_position_is_dropped(self):
        _signed_in().post(self.url, {'moves': 'e7e5', 'route': 'd2d4'})

        self.assertEqual(self._routes(), [''])

    def test_without_a_route_the_tasks_carry_none(self):
        _signed_in().post(self.url, {'moves': 'e7e5'})

        self.assertEqual(self._routes(), [''])


class TallyTests(TestCase):
    """What came of each move, said move by move and counted."""

    def test_each_outcome_is_counted_and_reported_in_order(self):
        root = _root()
        url = f'/atomicdb/request-moves/{root.key}/'
        client = _signed_in()
        client.post(url, {'moves': 'e2e4'})
        solved = _chain(root, ['d2d4'])[0]
        Position.objects.filter(pk=solved.pk).update(status='WHITE_WIN',
                                                     closure='MINIMAX')
        theirs = _chain(root, ['g1f3'])[0]
        AnalysisTask.objects.create(
            position=theirs, generation=0, budget_nodes=LADDER[0],
            source=AnalysisTask.Source.USER, requested_by='soothdest')

        response = client.post(url, {'moves': 'e2e4,d2d4,g1f3,b1c3,zz'})

        body = response.json()
        self.assertEqual(body['status'], 'done')
        self.assertEqual(body['moves'],
                         [{'uci': 'e2e4', 'status': 'already-requested'},
                          {'uci': 'd2d4', 'status': 'already-solved'},
                          {'uci': 'g1f3', 'status': 'already-queued'},
                          {'uci': 'b1c3', 'status': 'queued'}])
        self.assertEqual(body['counts'], {'already-requested': 1,
                                          'already-solved': 1,
                                          'already-queued': 1,
                                          'queued': 1,
                                          'illegal': 1})
        self.assertEqual(body['ignored'], ['zz'])
        self.assertEqual(body['left_out'], 0)
        # Joining somebody else's request puts this account's name on it,
        # which is what rings its bell when the analysis lands.
        self.assertIn('eclipsia', AnalysisTask.objects.get(
            position=theirs).also_requested_by)


class CsrfTests(TestCase):
    """The page's own fetch goes through; a foreign page's does not."""

    def setUp(self):
        self.root = _root()
        self.url = f'/atomicdb/request-moves/{self.root.key}/'
        worker_account('eclipsia')
        self.strict = Client(enforce_csrf_checks=True)
        self.strict.login(username='eclipsia', password='pw')

    def test_a_post_without_the_token_is_rejected(self):
        response = self.strict.post(self.url, {'moves': 'e2e4'})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AnalysisTask.objects.exists())
        self.assertFalse(Edge.objects.filter(parent=self.root).exists())

    def test_the_pages_own_fetch_goes_through(self):
        self.strict.get(f'/atomicdb/explore/{self.root.key}/')
        token = self.strict.cookies['csrftoken'].value

        response = self.strict.post(self.url, {'moves': 'e2e4'},
                                    HTTP_X_CSRFTOKEN=token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['counts'], {'queued': 1})


class DocsTests(TestCase):

    def test_the_docs_say_so_with_the_cap_that_applies(self):
        response = Client().get('/atomicdb/docs/')

        self.assertContains(
            response, f'<strong>{views.MULTI_REQUEST_CAP}</strong> at a time')
