"""Community backlogs: re-searches anyone signed in can adopt.

What these tests pin, and why each one:

* the box counts what is left and what was adopted, by how many accounts,
  and anonymous visitors see it but are sent to sign in;
* adopting a batch queues the adopter's own requests (USER, their name, the
  backlog's arm) at the price that can replace the search it redoes, five
  lines for lost lines and the house width for a shallow revisit;
* each position is checked again when it is adopted: closed, no longer
  qualifying, already queued or running, or shown by a search a 128M one
  could not beat, it leaves the list instead and the batch takes the next;
* a search the server queued on the slot and nobody has taken becomes the
  adopter's request, the way a click promotes it;
* a batch counts against the account's allowance of waiting requests and
  against its own ceiling of adopted ones, and is refused or cut short there;
* the loader is idempotent, and the shallow scan finds what the repair arm's
  definition finds, skips what it already repaired, resumes from its state
  file and reads only with ``--dry-run``;
* clear wins buy what the +16 sweep buys (one line, 128.64M, arm sweep16),
  are checked again with the sweep's own rule, take over a sweep task nobody
  leased, are never bought twice by the drip, and load from the sweep's list
  in its order, idempotently;
* deep wins buy one line at 514.56M (arm deep25), outbid a shown search
  already on the 512M rung without leaving it, are checked again with their
  own rule (visits 2, +25 for the side to move), come in batches of 5, 10
  or 20, take four places each under the ceiling of adopted positions, and
  load from a resumable scan of the searches, the highest value first.
"""

import hashlib
import json
import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import Client, override_settings

from . import backlog, idle_repair, ingest, logic, sweep
from .models import (AnalysisTask, BacklogItem, BacklogSubscription, DBEvent,
                     Edge, Position)
from .testing import TestCase, worker_account

USER = AnalysisTask.Source.USER
FILL = AnalysisTask.Source.FILL
PENDING = AnalysisTask.TState.PENDING
LEASED = AnalysisTask.TState.LEASED
COMPLETED = AnalysisTask.TState.COMPLETED
FEN = '4k3/8/8/8/8/8/8/4K3 w - - 0 1'     # five legal moves
SEARCHED = 128_001_500


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _raw(depth, nodes, uci, score=30):
    return (f'info depth {depth} seldepth {depth + 9} multipv 1 score cp '
            f'{score} nodes {nodes} nps 6000000 time 21000 pv {uci}')


def _lines(count, depth=24, nodes=SEARCHED):
    """``last_analysis`` with ``count`` lines, the pass's nodes on line 1."""
    moves = logic.legal_moves(FEN)
    lines = [{'move': uci, 'eval_cp': 30 - index, 'mate': None, 'pv': [uci],
              'raw': _raw(depth, nodes, uci, 30 - index)}
             for index, uci in enumerate(moves[:count])]
    lines[0]['_budget'] = nodes
    return lines


def _pos(name, lines, visits=1, **fields):
    return Position.objects.create(key=_key(name), fen=FEN, visits=visits,
                                   last_analysis=lines, **fields)


def _short(name, **fields):
    """A lost-lines position: its search kept two lines of five."""
    return _pos(name, _lines(2), **fields)


def _shallow(name, **fields):
    """A shallow showcase: 128M at depth 15."""
    return _pos(name, _lines(5, depth=15), **fields)


def _offer(name, positions):
    backlog.load_keys(name, [pos.key for pos in positions])


class BacklogTestCase(TestCase):

    def setUp(self):
        self.user = worker_account('moky')
        self.client = Client()
        self.client.force_login(self.user)


class CountsAndBox(BacklogTestCase):

    def test_counts_left_adopted_and_adopters(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        _offer(backlog.SHALLOW, [_shallow(f's{i}') for i in range(3)])
        worker_account('torom')
        backlog.adopt('moky', backlog.LOST_LINES, 25)
        backlog.adopt('torom', backlog.LOST_LINES, 25)
        numbers = backlog.counts(fresh=True)
        self.assertEqual(numbers[backlog.LOST_LINES],
                         {'open': 0, 'adopted': 30, 'adopters': 2})
        self.assertEqual(numbers[backlog.SHALLOW],
                         {'open': 3, 'adopted': 0, 'adopters': 0})

    def test_the_queue_page_shows_the_box_with_buttons(self):
        _offer(backlog.LOST_LINES, [_short('a'), _short('b')])
        body = self.client.get('/atomicdb/queue/').content.decode()
        self.assertIn('Community backlog', body)
        self.assertIn('Lost lines', body)
        self.assertIn('Adopt 25', body)
        self.assertIn('value="lost-lines"', body)
        self.assertIn('action="/atomicdb/queue/adopt/"', body)
        # Nothing was ever offered there: no buttons, and no "all adopted".
        self.assertIn('nothing to adopt', body)

    def test_anonymous_visitors_see_the_box_and_a_sign_in_link(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        body = Client().get('/atomicdb/queue/').content.decode()
        self.assertIn('Community backlog', body)
        self.assertIn('to adopt a batch', body)
        self.assertNotIn('Adopt 25', body)

    def test_anonymous_visitors_cannot_adopt(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        response = Client().post('/atomicdb/queue/adopt/',
                                 {'backlog': 'lost-lines', 'size': '25'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login/'))
        response = Client().post('/atomicdb/queue/adopt/',
                                 {'backlog': 'lost-lines', 'size': '25',
                                  'format': 'json'})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(AnalysisTask.objects.exists())
        self.assertEqual(backlog.adopt('', backlog.LOST_LINES, 25).status,
                         'sign-in')

    def test_get_is_refused(self):
        self.assertEqual(self.client.get('/atomicdb/queue/adopt/')
                         .status_code, 405)


class Adopting(BacklogTestCase):

    def test_a_batch_becomes_the_adopters_requests(self):
        positions = [_short(f'l{i}', visits=i % 2) for i in range(30)]
        _offer(backlog.LOST_LINES, positions)
        response = self.client.post('/atomicdb/queue/adopt/',
                                    {'backlog': 'lost-lines', 'size': '25'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('adopt=adopted', response['Location'])
        tasks = AnalysisTask.objects.all()
        self.assertEqual(tasks.count(), 25)
        for task in tasks:
            self.assertEqual(task.source, USER)
            self.assertEqual(task.state, PENDING)
            self.assertEqual(task.requested_by, 'moky')
            self.assertEqual(task.arm, 'relines')
            self.assertEqual(task.budget_nodes, 128_640_000)
            self.assertEqual(task.multipv, 5)
            self.assertEqual(task.generation, task.position.visits)
            self.assertIsNotNone(task.requested_at)
        adopted = BacklogItem.objects.filter(state='adopted')
        self.assertEqual(adopted.count(), 25)
        self.assertEqual(set(adopted.values_list('adopted_by', flat=True)),
                         {'moky'})
        self.assertEqual(
            set(adopted.values_list('task_id', flat=True)),
            set(tasks.values_list('id', flat=True)))
        # The redirect lands on the box with its receipt.
        body = self.client.get(response['Location']).content.decode()
        self.assertIn('Adopted', body)
        self.assertIn('<strong>25</strong> positions from Lost lines', body)

    def test_the_price_beats_the_search_it_redoes(self):
        """An engine stops a little past its budget, and a re-search bought
        at exactly 128M would lose the showcase to the one it redoes."""
        self.assertGreater(backlog.ADOPT_BUDGET, SEARCHED)
        self.assertEqual(ingest._rung_matching(backlog.ADOPT_BUDGET),
                         128_000_000)

    def test_shallow_positions_get_the_house_width_of_a_revisit(self):
        first = _shallow('s1', visits=1)
        later = _shallow('s2', visits=4)
        _offer(backlog.SHALLOW, [first, later])
        outcome = backlog.adopt('moky', backlog.SHALLOW, 25)
        self.assertEqual(outcome.adopted, 2)
        by_key = {task.position_id: task
                  for task in AnalysisTask.objects.all()}
        self.assertEqual(by_key[first.key].multipv,
                         ingest.multipv_for(1, backlog.ADOPT_BUDGET))
        self.assertEqual(by_key[later.key].multipv,
                         ingest.multipv_for(4, backlog.ADOPT_BUDGET))
        self.assertEqual(by_key[first.key].arm, 'shallow')
        # A USER task of its own arm, so the repair arm's narrow-slot rule
        # does not hold it back.
        self.assertTrue(idle_repair.fits_slot(by_key[first.key], 64))

    def test_the_json_answer_for_scripts(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        response = self.client.post('/atomicdb/queue/adopt/',
                                    {'backlog': 'lost-lines', 'size': '25',
                                     'format': 'json'})
        self.assertEqual(response.json(),
                         {'status': 'adopted', 'adopted': 1, 'dropped': 0})
        response = self.client.post('/atomicdb/queue/adopt/',
                                    {'backlog': 'lost-lines', 'size': '7',
                                     'format': 'json'})
        self.assertEqual(response.status_code, 400)

    def test_an_empty_backlog_says_so(self):
        outcome = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual((outcome.status, outcome.adopted), ('empty', 0))


class RecheckAtAdoption(BacklogTestCase):

    def test_closed_done_queued_and_deep_positions_leave_the_list(self):
        closed = _short('closed', status='WIN')
        dead = _short('dead', priority=ingest.DEAD)
        done = _pos('done', _lines(5))
        deep = _pos('deep', _lines(2, nodes=512_004_000))
        queued = _short('queued')
        AnalysisTask.objects.create(position=queued, generation=1,
                                    budget_nodes=128_000_000, source=USER,
                                    requested_by='torom')
        running = _short('running')
        AnalysisTask.objects.create(position=running, generation=1,
                                    budget_nodes=8_000_000, state=LEASED)
        good = _short('good')
        _offer(backlog.LOST_LINES,
               [closed, dead, done, deep, queued, running, good])
        outcome = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual((outcome.adopted, outcome.dropped), (1, 6))
        reasons = dict(BacklogItem.objects.filter(state='dropped')
                       .values_list('position_key', 'reason'))
        self.assertEqual(reasons, {
            closed.key: 'closed', dead.key: 'closed', done.key: 'done',
            deep.key: 'deep', queued.key: 'queued', running.key: 'queued'})
        # Nothing of anybody else's was touched.
        self.assertEqual(AnalysisTask.objects.get(position=queued)
                         .requested_by, 'torom')
        self.assertEqual(AnalysisTask.objects.filter(
            requested_by='moky').get().position_id, good.key)

    def test_a_short_search_at_the_adoption_budget_is_still_offered(self):
        # A re-search that came back short at 128.64M (an old worker's
        # MultiPV rule) is redone at the same budget: with more lines it
        # takes the showcase, so it is not "deep".
        # The engine stops a little past its budget, so the shown search ran
        # a few nodes more than 128.64M; the new one is bought over that and
        # stays on the 128M rung.
        shown = backlog.ADOPT_BUDGET + 68
        again = _pos('again', _lines(2, nodes=shown))
        _offer(backlog.LOST_LINES, [again])
        outcome = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual((outcome.adopted, outcome.dropped), (1, 0))
        task = AnalysisTask.objects.get(requested_by='moky')
        self.assertGreater(task.budget_nodes, shown)
        self.assertEqual(ingest._rung_matching(task.budget_nodes),
                         128_000_000)
        self.assertEqual(task.multipv, backlog.LOST_LINES_MULTIPV)

    def test_a_restricted_search_is_not_short(self):
        # The server searched only the replies still open (searchmoves), so
        # fewer lines is the right answer, not a lost one.
        narrow = _pos('narrow', _lines(1),
                      analysis_passes=[{'showcase': True, 'restricted': True,
                                        'lines': 1}])
        _offer(backlog.LOST_LINES, [narrow])
        outcome = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual((outcome.adopted, outcome.dropped), (0, 1))
        self.assertEqual(BacklogItem.objects.get().reason, 'done')

    def test_a_shallow_adoption_keeps_the_fixed_price(self):
        self.assertEqual(
            backlog.adopt_budget(backlog.SHALLOW, _lines(2, nodes=SEARCHED)),
            backlog.ADOPT_BUDGET)
        self.assertEqual(
            backlog.adopt_budget(backlog.LOST_LINES, _lines(2, nodes=SEARCHED)),
            backlog.ADOPT_BUDGET)

    def test_a_batch_takes_the_next_rows_past_the_dropped_ones(self):
        rows = [_short(f'c{i}', status='WIN') for i in range(10)]
        rows += [_short(f'g{i}') for i in range(30)]
        _offer(backlog.LOST_LINES, rows)
        outcome = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual((outcome.adopted, outcome.dropped), (25, 10))
        self.assertEqual(BacklogItem.objects.filter(state='open').count(), 5)

    def test_a_shallow_position_already_repaired_or_deep_now_is_dropped(self):
        repaired = _shallow('repaired')
        AnalysisTask.objects.create(
            position=repaired, generation=0, budget_nodes=128_640_000,
            arm=idle_repair.REPAIR_ARM, state=COMPLETED,
            nodes_searched=128_641_000)
        deepened = _pos('deepened', _lines(5, depth=22))
        good = _shallow('good')
        _offer(backlog.SHALLOW, [repaired, deepened, good])
        outcome = backlog.adopt('moky', backlog.SHALLOW, 25)
        self.assertEqual((outcome.adopted, outcome.dropped), (1, 2))

    def test_a_server_search_waiting_on_the_slot_becomes_the_request(self):
        pos = _short('drip')
        drip = AnalysisTask.objects.create(
            position=pos, generation=1, budget_nodes=128_000_000, multipv=5,
            source=FILL, arm='relines')
        _offer(backlog.LOST_LINES, [pos])
        outcome = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual(outcome.adopted, 1)
        drip.refresh_from_db()
        self.assertEqual((drip.source, drip.requested_by, drip.arm,
                          drip.budget_nodes),
                         (USER, 'moky', 'relines', 128_640_000))
        self.assertIsNotNone(drip.requested_at)
        self.assertEqual(AnalysisTask.objects.count(), 1)

    def test_a_withdrawn_row_on_the_slot_comes_back_as_the_request(self):
        pos = _short('withdrawn')
        old = AnalysisTask.objects.create(
            position=pos, generation=1, budget_nodes=512_000_000,
            source=USER, requested_by='torom',
            state=AnalysisTask.TState.CANCELLED)
        _offer(backlog.LOST_LINES, [pos])
        self.assertEqual(backlog.adopt('moky', backlog.LOST_LINES, 25)
                         .adopted, 1)
        old.refresh_from_db()
        self.assertEqual((old.state, old.requested_by, old.budget_nodes,
                          old.arm), (PENDING, 'moky', 128_640_000,
                                     'relines'))


class Caps(BacklogTestCase):

    def test_a_full_queue_refuses_the_batch_with_the_usual_receipt(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(5)])
        mine = [_pos(f'mine{i}', _lines(5)) for i in range(3)]
        for pos in mine:
            AnalysisTask.objects.create(position=pos, generation=1,
                                        budget_nodes=128_000_000,
                                        source=USER, requested_by='moky')
        with patch('atomicdb.views.REQUEST_QUEUE_MAX', 3):
            response = self.client.post(
                '/atomicdb/queue/adopt/',
                {'backlog': 'lost-lines', 'size': '25'})
        self.assertIn('adopt=queue-full', response['Location'])
        self.assertEqual(AnalysisTask.objects.filter(arm='relines').count(),
                         0)
        self.assertTrue(DBEvent.objects.filter(
            kind='LANE_CAP_HIT', payload__account='moky').exists())
        body = self.client.get(response['Location']).content.decode()
        self.assertIn('Your queue is full', body)

    def test_a_batch_stops_at_the_room_left_in_the_queue(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        with patch('atomicdb.views.REQUEST_QUEUE_MAX', 10):
            outcome = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual(outcome.adopted, 10)

    def test_the_ceiling_of_adopted_positions_waiting(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(60)])
        with patch.object(backlog, 'ADOPT_WAITING_MAX', 30):
            first = backlog.adopt('moky', backlog.LOST_LINES, 25)
            second = backlog.adopt('moky', backlog.LOST_LINES, 25)
            third = backlog.adopt('moky', backlog.LOST_LINES, 25)
            # Another account has its own ceiling.
            worker_account('torom')
            other = backlog.adopt('torom', backlog.LOST_LINES, 25)
        self.assertEqual((first.adopted, second.adopted), (25, 5))
        self.assertEqual(third.status, 'adopted-full')
        self.assertEqual(other.adopted, 25)

    def test_served_adoptions_free_the_ceiling(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(40)])
        with patch.object(backlog, 'ADOPT_WAITING_MAX', 25):
            backlog.adopt('moky', backlog.LOST_LINES, 25)
            AnalysisTask.objects.filter(requested_by='moky').update(
                state=COMPLETED)
            again = backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual(again.adopted, 15)

    def test_only_the_offered_sizes(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        self.assertEqual(backlog.adopt('moky', backlog.LOST_LINES, 1000)
                         .status, 'bad-request')
        self.assertEqual(backlog.adopt('moky', 'nope', 25).status,
                         'bad-request')


class Loading(TestCase):

    def test_the_loader_is_idempotent(self):
        keys = [_key(f'k{i}') for i in range(5)]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'targets.txt')
            with open(path, 'w') as handle:
                handle.write('\n'.join(keys + ['not-a-key', '']) + '\n')
            out = StringIO()
            call_command('atomicdb_backlog', 'load-lost-lines', '--file',
                         path, '--skip', '2', stdout=out)
            self.assertIn('added=3', out.getvalue())
            self.assertIn('invalid=1', out.getvalue())
            out = StringIO()
            call_command('atomicdb_backlog', 'load-lost-lines', '--file',
                         path, stdout=out)
            self.assertIn('added=2 already=3', out.getvalue())
            call_command('atomicdb_backlog', 'load-lost-lines', '--file',
                         path, stdout=StringIO())
        self.assertEqual(BacklogItem.objects.count(), 5)
        self.assertEqual(set(BacklogItem.objects.values_list(
            'position_key', flat=True)), set(keys))

    def test_an_adopted_row_is_not_reopened_by_a_reload(self):
        pos = _short('a')
        backlog.load_keys(backlog.LOST_LINES, [pos.key])
        worker_account('moky')
        backlog.adopt('moky', backlog.LOST_LINES, 25)
        self.assertEqual(backlog.load_keys(backlog.LOST_LINES, [pos.key]),
                         (0, 1, 0))
        self.assertEqual(BacklogItem.objects.get().state, 'adopted')

    def test_status(self):
        backlog.load_keys(backlog.SHALLOW, [_key('x')])
        out = StringIO()
        call_command('atomicdb_backlog', 'status', stdout=out)
        self.assertIn('shallow: open=1', out.getvalue())


class ShallowScan(TestCase):

    def _searched(self, pos, nodes=SEARCHED):
        return AnalysisTask.objects.create(
            position=pos, generation=pos.visits - 1, budget_nodes=128_000_000,
            nodes_searched=nodes, state=COMPLETED)

    def setUp(self):
        self.shallow = _shallow('shallow')
        self.deep = _pos('deep', _lines(5, depth=23))
        self.small = _pos('small', _lines(5, depth=12, nodes=32_000_500))
        self.closed = _shallow('closed', status='WIN')
        self.repaired = _shallow('repaired', visits=2)
        for pos in (self.shallow, self.deep, self.closed, self.repaired):
            self._searched(pos)
        self._searched(self.small, nodes=32_000_500)
        AnalysisTask.objects.create(
            position=self.repaired, generation=2, budget_nodes=128_640_000,
            arm=idle_repair.REPAIR_ARM)

    def test_the_scan_finds_the_shallow_showcases_only(self):
        result = backlog.scan_shallow(batch=2, pause=0)
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['cursor'], 0)
        self.assertEqual(list(BacklogItem.objects.values_list(
            'backlog', 'position_key')),
            [(backlog.SHALLOW, self.shallow.key)])

    def test_dry_run_adds_nothing(self):
        out = StringIO()
        call_command('atomicdb_backlog', 'scan-shallow', '--dry-run',
                     '--pause', '0', stdout=out)
        self.assertIn('would add=1', out.getvalue())
        self.assertFalse(BacklogItem.objects.exists())

    def test_the_scan_resumes_from_its_state_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'scan.json')
            top = AnalysisTask.objects.order_by('-id').first().id
            args = ['atomicdb_backlog', 'scan-shallow', '--state-file', path,
                    '--batch', '1', '--pause', '0']
            call_command(*args, '--max-steps', '1', stdout=StringIO())
            with open(path) as handle:
                state = json.load(handle)
            self.assertEqual(state['cursor'], top)
            self.assertFalse(state['done'])
            call_command(*args, stdout=StringIO())
            with open(path) as handle:
                self.assertTrue(json.load(handle)['done'])
            out = StringIO()
            call_command(*args, stdout=out)
            self.assertIn('already finished', out.getvalue())
        self.assertEqual(BacklogItem.objects.count(), 1)


# ---------------- clear wins (the +16 sweep) ----------------

ONCE = 128_001_500


def _win(name, lines=None, **fields):
    """A clear win: searched once at 128M, White to move at +20."""
    base = {'nodes_invested': ONCE, 'eval_cp': 2000}
    base.update(fields)
    visits = base.pop('visits', 1)
    return _pos(name, _lines(1) if lines is None else lines, visits=visits,
                **base)


@override_settings(ATOMICDB_ARM_RATE_SWEEP16='1000')
class ClearWins(BacklogTestCase):

    def setUp(self):
        super().setUp()
        sweep.forget_plan()
        self.addCleanup(sweep.forget_plan)

    def test_the_box_offers_it(self):
        _offer(backlog.CLEAR_WINS, [_win('a')])
        body = self.client.get('/atomicdb/queue/').content.decode()
        self.assertIn('Clear wins', body)
        self.assertIn('value="clear-wins"', body)
        self.assertEqual([box['name'] for box in backlog.boxes()],
                         ['lost-lines', 'shallow', 'clear-wins', 'deep-wins'])

    def test_a_batch_buys_what_the_sweep_buys(self):
        positions = [_win(f'w{i}') for i in range(3)]
        _offer(backlog.CLEAR_WINS, positions)
        outcome = backlog.adopt('moky', backlog.CLEAR_WINS, 25)
        self.assertEqual((outcome.adopted, outcome.dropped), (3, 0))
        for task in AnalysisTask.objects.all():
            self.assertEqual((task.source, task.requested_by, task.arm),
                             (USER, 'moky', 'sweep16'))
            self.assertEqual(task.budget_nodes, sweep.SWEEP_NODES)
            self.assertEqual(task.budget_nodes, 128_640_000)
            self.assertEqual(task.multipv, 1)
            self.assertEqual(task.generation, 1)

    def test_closed_done_deep_and_queued_positions_leave_the_list(self):
        closed = _win('closed', status='WHITE_WIN')
        dead = _win('dead', priority=ingest.DEAD)
        again = _win('again', visits=2)
        small = _win('small', eval_cp=1500)
        mated = _win('mated', eval_cp=9500)
        deep = _win('deep', lines=_lines(1, nodes=512_004_000))
        cut_off = _win('cut-off')
        proven_parent = _pos('proven-parent', _lines(5), status='WHITE_WIN')
        Edge.objects.create(parent=proven_parent, child=cut_off,
                            move_uci='e1e2')
        running = _win('running')
        AnalysisTask.objects.create(position=running, generation=1,
                                    budget_nodes=sweep.SWEEP_NODES,
                                    source=FILL, arm='sweep16', state=LEASED)
        good = _win('good')
        _offer(backlog.CLEAR_WINS, [closed, dead, again, small, mated, deep,
                                    cut_off, running, good])
        outcome = backlog.adopt('moky', backlog.CLEAR_WINS, 25)
        self.assertEqual((outcome.adopted, outcome.dropped), (1, 8))
        reasons = dict(BacklogItem.objects.filter(state='dropped')
                       .values_list('position_key', 'reason'))
        self.assertEqual(reasons, {
            closed.key: 'closed', dead.key: 'closed', cut_off.key: 'closed',
            again.key: 'done', small.key: 'done', mated.key: 'done',
            deep.key: 'deep', running.key: 'queued'})
        self.assertEqual(AnalysisTask.objects.get(requested_by='moky')
                         .position_id, good.key)

    def test_it_bids_over_a_search_already_at_the_adoption_budget(self):
        shown = backlog.ADOPT_BUDGET + 68
        pos = _win('again', lines=_lines(1, nodes=shown))
        _offer(backlog.CLEAR_WINS, [pos])
        self.assertEqual(backlog.adopt('moky', backlog.CLEAR_WINS, 25)
                         .adopted, 1)
        task = AnalysisTask.objects.get(requested_by='moky')
        self.assertGreater(task.budget_nodes, shown)
        self.assertEqual(ingest._rung_matching(task.budget_nodes),
                         128_000_000)
        self.assertEqual(backlog.adopt_budget(backlog.CLEAR_WINS,
                                              _lines(1, nodes=SEARCHED)),
                         sweep.SWEEP_NODES)

    def test_a_sweep_task_waiting_on_the_slot_becomes_the_request(self):
        pos = _win('drip')
        sweep.save_plan({'build_id': 'b1', 'keys': [pos.key]})
        self.assertEqual(sweep.drip(), 1)
        _offer(backlog.CLEAR_WINS, [pos])
        self.assertEqual(backlog.adopt('moky', backlog.CLEAR_WINS, 25)
                         .adopted, 1)
        task = AnalysisTask.objects.get()
        self.assertEqual((task.source, task.requested_by, task.arm,
                          task.budget_nodes, task.multipv),
                         (USER, 'moky', 'sweep16', 128_640_000, 1))
        # The drip does not buy it again, and it is no longer the drip's.
        sweep.rewind()
        self.assertEqual(sweep.drip(), 0)
        self.assertEqual(sweep.pending_count(), 0)
        self.assertEqual(sweep.status()['adopted_pending'], 1)
        self.assertEqual(sweep.withdraw_pending(), 0)
        self.assertEqual(AnalysisTask.objects.count(), 1)

    def test_the_drip_passes_over_an_adopted_position(self):
        adopted, other = _win('adopted'), _win('other')
        _offer(backlog.CLEAR_WINS, [adopted])
        backlog.adopt('moky', backlog.CLEAR_WINS, 25)
        sweep.save_plan({'build_id': 'b1', 'keys': [adopted.key, other.key]})
        self.assertEqual(sweep.drip(), 1)
        self.assertEqual(AnalysisTask.objects.get(source=FILL).position_id,
                         other.key)
        # Absorbed without a search, the adopted request is not revived as
        # a sweep task by the drip.
        ingest.absorb_tasks(AnalysisTask.objects.filter(source=USER))
        sweep.rewind()
        self.assertEqual(sweep.drip(), 0)

    def test_the_loader_keeps_the_sweeps_order_and_is_idempotent(self):
        keys = [_win(f'w{i}').key for i in range(4)]
        stale = _win('stale', visits=2).key
        plan = [keys[2], stale, keys[0], keys[3], keys[1]]
        sweep.save_plan({'build_id': 'b1', 'keys': plan})
        out = StringIO()
        call_command('atomicdb_backlog', 'load-clear-wins', '--limit', '3',
                     stdout=out)
        self.assertIn('added=2 already=0 left_out=1', out.getvalue())
        out = StringIO()
        call_command('atomicdb_backlog', 'load-clear-wins', stdout=out)
        self.assertIn('added=2 already=2 left_out=1', out.getvalue())
        call_command('atomicdb_backlog', 'load-clear-wins', stdout=StringIO())
        self.assertEqual(
            list(BacklogItem.objects.filter(backlog='clear-wins')
                 .order_by('id').values_list('position_key', flat=True)),
            [keys[2], keys[0], keys[3], keys[1]])
        # A batch takes them in that order.
        outcome = backlog.adopt('moky', backlog.CLEAR_WINS, 25)
        self.assertEqual(outcome.task_ids, sorted(outcome.task_ids))
        self.assertEqual(
            [AnalysisTask.objects.get(pk=pk).position_id
             for pk in outcome.task_ids], [keys[2], keys[0], keys[3], keys[1]])

    def test_the_loader_reads_a_file_too(self):
        keys = [_win(f'w{i}').key for i in range(3)]
        with tempfile.TemporaryDirectory() as folder:
            as_json = os.path.join(folder, 'sweep16.json')
            with open(as_json, 'w') as handle:
                json.dump({'build_id': 'x', 'keys': keys}, handle)
            as_lines = os.path.join(folder, 'keys.txt')
            with open(as_lines, 'w') as handle:
                handle.write('\n'.join(keys + ['not-a-key']) + '\n')
            out = StringIO()
            call_command('atomicdb_backlog', 'load-clear-wins', '--file',
                         as_json, '--skip', '1', stdout=out)
            self.assertIn('added=2', out.getvalue())
            out = StringIO()
            call_command('atomicdb_backlog', 'load-clear-wins', '--file',
                         as_lines, stdout=out)
            self.assertIn('added=1 already=2 left_out=1', out.getvalue())

    def test_without_a_list_the_loader_says_so(self):
        with self.assertRaises(CommandError):
            call_command('atomicdb_backlog', 'load-clear-wins',
                         stdout=StringIO())


# ---------------- deep wins ----------------

BLACK_FEN = '4k3/8/8/8/8/8/8/4K3 b - - 0 1'
TWICE = 2 * ONCE


def _deep(name, lines=None, fen=FEN, **fields):
    """A deep win: searched twice at 128M, White to move at +30."""
    base = {'nodes_invested': TWICE, 'eval_cp': 3000, 'visits': 2}
    base.update(fields)
    return Position.objects.create(
        key=_key(name), fen=fen,
        last_analysis=_lines(1) if lines is None else lines, **base)


def _row(pos):
    return Position.objects.filter(key=pos.key).values(
        'key', 'fen', 'status', 'visits', 'nodes_invested', 'eval_cp',
        'backed_eval', 'last_analysis', 'analysis_passes').get()


class DeepWins(BacklogTestCase):

    def test_the_box_offers_it_with_its_own_sizes(self):
        _offer(backlog.DEEP_WINS, [_deep('a')])
        body = self.client.get('/atomicdb/queue/').content.decode()
        self.assertIn('Deep wins', body)
        self.assertIn('value="deep-wins"', body)
        for size in (5, 10, 20):
            self.assertIn(f'Adopt {size}<', body)
        # The other lists are empty: none of their sizes is offered.
        self.assertNotIn('Adopt 25', body)
        self.assertIn('<option selected="" value="10">', body)
        self.assertIn('A position from Deep wins takes 4 places', body)

    def test_a_batch_buys_one_line_at_512m(self):
        positions = [_deep(f'd{i}') for i in range(3)]
        _offer(backlog.DEEP_WINS, positions)
        outcome = backlog.adopt('moky', backlog.DEEP_WINS, 5)
        self.assertEqual((outcome.adopted, outcome.dropped), (3, 0))
        for task in AnalysisTask.objects.all():
            self.assertEqual((task.source, task.requested_by, task.arm),
                             (USER, 'moky', 'deep25'))
            self.assertEqual(task.budget_nodes, 514_560_000)
            self.assertEqual(ingest._rung_matching(task.budget_nodes),
                             512_000_000)
            self.assertEqual((task.multipv, task.generation), (1, 2))

    def test_the_rule_reads_the_value_as_the_sweep_does(self):
        cases = {
            'white-25': (_deep('w25', eval_cp=2500), True),
            'white-24': (_deep('w24', eval_cp=2499), False),
            'black-25': (_deep('b25', fen=BLACK_FEN, eval_cp=-2500), True),
            'black-behind': (_deep('bb', fen=BLACK_FEN, eval_cp=3000), False),
            'backed-lower': (_deep('bl', eval_cp=3000, backed_eval=1000),
                             False),
            'backed-higher': (_deep('bh', eval_cp=0, backed_eval=2600), True),
            'mate-band': (_deep('mb', eval_cp=9500), True),
            'no-value': (_deep('nv', eval_cp=None), False),
            'once': (_deep('once', visits=1), False),
            'thrice': (_deep('thrice', visits=3), False),
        }
        for label, (pos, expected) in cases.items():
            self.assertEqual(backlog.deep_win_now(_row(pos)), expected, label)
        # The SQL form agrees with the Python one.
        from django.db.models.functions import Coalesce
        found = set(Position.objects.annotate(
            deep_value=Coalesce('backed_eval', 'eval_cp'))
            .filter(backlog.deep_wins_q()).values_list('key', flat=True))
        self.assertEqual(found, {pos.key for pos, expected in cases.values()
                                 if expected})

    def test_closed_done_deep_and_queued_positions_leave_the_list(self):
        closed = _deep('closed', status='WHITE_WIN')
        dead = _deep('dead', priority=ingest.DEAD)
        once = _deep('once', visits=1)
        again = _deep('again', visits=3)
        small = _deep('small', eval_cp=2000)
        searched = _deep('searched')
        AnalysisTask.objects.create(
            position=searched, generation=1, budget_nodes=514_560_000,
            arm='deep25', state=COMPLETED, nodes_searched=514_561_000)
        deep = _deep('deep', lines=_lines(1, nodes=2_000_100_000))
        cut_off = _deep('cut-off')
        proven_parent = _pos('proven-parent', _lines(5), status='WHITE_WIN')
        Edge.objects.create(parent=proven_parent, child=cut_off,
                            move_uci='e1e2')
        running = _deep('running')
        AnalysisTask.objects.create(position=running, generation=2,
                                    budget_nodes=8_000_000, state=LEASED)
        good = _deep('good')
        _offer(backlog.DEEP_WINS, [closed, dead, once, again, small,
                                   searched, deep, cut_off, running, good])
        outcome = backlog.adopt('moky', backlog.DEEP_WINS, 20)
        self.assertEqual((outcome.adopted, outcome.dropped), (1, 9))
        reasons = dict(BacklogItem.objects.filter(state='dropped')
                       .values_list('position_key', 'reason'))
        self.assertEqual(reasons, {
            closed.key: 'closed', dead.key: 'closed', cut_off.key: 'closed',
            once.key: 'done', again.key: 'done', small.key: 'done',
            searched.key: 'done', deep.key: 'deep', running.key: 'queued'})
        self.assertEqual(AnalysisTask.objects.get(requested_by='moky')
                         .position_id, good.key)

    def test_the_bid_stays_on_the_512m_rung(self):
        self.assertEqual(backlog.DEEP_BUDGET, 514_560_000)
        budget = backlog.adopt_budget
        name = backlog.DEEP_WINS
        # A 128M search shown: the plain price.
        self.assertEqual(budget(name, _lines(1, nodes=SEARCHED)),
                         backlog.DEEP_BUDGET)
        self.assertFalse(backlog._off_rung(name, _lines(1, nodes=SEARCHED)))
        # A 512M search already shown, a hair past its budget: bid over it.
        for shown in (512_000_099, backlog.DEEP_BUDGET + 68, 516_000_000):
            bid = budget(name, _lines(1, nodes=shown))
            self.assertGreater(bid, shown)
            self.assertEqual(ingest._rung_matching(bid), 512_000_000)
            self.assertFalse(backlog._off_rung(name, _lines(1, nodes=shown)))
        # A bid that would leave the rung is "deep".
        self.assertTrue(backlog._off_rung(name, _lines(1, nodes=517_000_000)))
        self.assertTrue(backlog._off_rung(name,
                                          _lines(1, nodes=2_000_100_000)))
        pos = _deep('again', lines=_lines(1, nodes=backlog.DEEP_BUDGET + 68))
        _offer(name, [pos])
        backlog.adopt('moky', name, 5)
        self.assertGreater(AnalysisTask.objects.get().budget_nodes,
                           backlog.DEEP_BUDGET + 68)

    def test_a_server_search_waiting_on_the_slot_becomes_the_request(self):
        pos = _deep('drip')
        server = AnalysisTask.objects.create(
            position=pos, generation=2, budget_nodes=128_000_000, multipv=2,
            source=FILL, arm='coverage')
        _offer(backlog.DEEP_WINS, [pos])
        self.assertEqual(backlog.adopt('moky', backlog.DEEP_WINS, 5).adopted,
                         1)
        server.refresh_from_db()
        self.assertEqual((server.source, server.requested_by,
                          server.budget_nodes),
                         (USER, 'moky', 514_560_000))

    def test_only_its_own_sizes(self):
        _offer(backlog.DEEP_WINS, [_deep('a')])
        _offer(backlog.LOST_LINES, [_short('b')])
        self.assertEqual(backlog.adopt('moky', backlog.DEEP_WINS, 25).status,
                         'bad-request')
        self.assertEqual(backlog.adopt('moky', backlog.LOST_LINES, 5).status,
                         'bad-request')
        response = self.client.post('/atomicdb/queue/adopt/',
                                    {'backlog': 'deep-wins', 'size': '25',
                                     'format': 'json'})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/atomicdb/queue/adopt/',
                                    {'backlog': 'deep-wins', 'size': '20',
                                     'format': 'json'})
        self.assertEqual(response.json(),
                         {'status': 'adopted', 'adopted': 1, 'dropped': 0})

    def test_each_waiting_position_takes_four_places(self):
        _offer(backlog.DEEP_WINS, [_deep(f'd{i}') for i in range(20)])
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(10)])
        with patch.object(backlog, 'ADOPT_WAITING_MAX', 30):
            first = backlog.adopt('moky', backlog.DEEP_WINS, 20)
            self.assertEqual(first.adopted, 7)          # 7 x 4 = 28 places
            self.assertEqual(backlog.adopted_waiting('moky'), 28)
            self.assertEqual(backlog.adopt('moky', backlog.DEEP_WINS, 5)
                             .status, 'adopted-full')
            self.assertEqual(backlog.adopt('moky', backlog.LOST_LINES, 25)
                             .adopted, 2)
            self.assertEqual(backlog.adopted_waiting('moky'), 30)
        body = self.client.get(
            '/atomicdb/queue/?adopt=adopted-full&backlog=deep-wins&n=0'
            '&dropped=0').content.decode()
        self.assertIn('take the 500 places an account has', body)


class DeepWinsSubscriptions(BacklogTestCase):

    def test_the_default_batch_is_ten_and_only_its_sizes(self):
        _offer(backlog.DEEP_WINS, [_deep(f'd{i}') for i in range(50)])
        self.client.post('/atomicdb/queue/subscribe/',
                         {'backlog': 'deep-wins'})
        self.assertEqual(BacklogSubscription.objects.get().batch_size, 10)
        response = self.client.post('/atomicdb/queue/subscribe/',
                                    {'backlog': 'deep-wins', 'size': '50',
                                     'format': 'json'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(backlog.subscribe('moky', backlog.LOST_LINES, 10),
                         'bad-request')
        self.assertEqual(backlog.subscribe('moky', backlog.DEEP_WINS, 20),
                         'subscribed')
        summary = backlog.top_up()
        self.assertEqual(summary['adopted'], 40)
        self.assertEqual(_adopted_of('moky', 'deep25').count(), 40)
        # 40 waiting, not below 20: nothing more.
        self.assertEqual(backlog.top_up()['adopted'], 0)

    def test_a_stored_size_of_another_list_is_refused_by_the_top_up(self):
        _offer(backlog.DEEP_WINS, [_deep('a')])
        BacklogSubscription.objects.create(username='moky',
                                           backlog='deep-wins', batch_size=50)
        backlog.top_up()
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.active, sub.last_status),
                         (False, 'bad-request'))
        self.assertFalse(AnalysisTask.objects.exists())


class DeepWinsLoader(TestCase):

    def _searched(self, pos):
        for generation in range(pos.visits):
            AnalysisTask.objects.create(
                position=pos, generation=generation,
                budget_nodes=128_000_000, nodes_searched=ONCE,
                state=COMPLETED)

    def setUp(self):
        self.white = _deep('white', eval_cp=3000)
        self.black = _deep('black', fen=BLACK_FEN, eval_cp=-5000)
        self.mate = _deep('mate', eval_cp=9800)
        self.small = _deep('small', eval_cp=1000)
        self.once = _deep('once', visits=1)
        self.closed = _deep('closed', status='WHITE_WIN')
        for pos in (self.white, self.black, self.mate, self.small,
                    self.once, self.closed):
            self._searched(pos)
        self.folder = tempfile.mkdtemp()
        self.path = os.path.join(self.folder, 'deep.json')

    def tearDown(self):
        for name in os.listdir(self.folder):
            os.remove(os.path.join(self.folder, name))
        os.rmdir(self.folder)

    def _run(self, *extra):
        out = StringIO()
        call_command('atomicdb_backlog', 'load-deep-wins', '--state-file',
                     self.path, '--pause', '0', *extra, stdout=out)
        return out.getvalue()

    def _listed(self):
        return list(BacklogItem.objects.filter(backlog='deep-wins')
                    .order_by('id').values_list('position_key', flat=True))

    def test_it_resumes_then_loads_the_highest_value_first(self):
        first = self._run('--batch', '1', '--max-steps', '3')
        self.assertIn('stopped at cursor=', first)
        with open(self.path) as handle:
            state = json.load(handle)
        self.assertFalse(state['done'])
        self.assertEqual(self._listed(), [])
        out = self._run('--batch', '2')
        self.assertIn('found=3 added=3', out)
        self.assertEqual(self._listed(),
                         [self.mate.key, self.black.key, self.white.key])
        with open(self.path) as handle:
            self.assertTrue(json.load(handle)['loaded'])
        self.assertIn('already finished', self._run())
        # A fresh scan adds nothing already there.
        os.remove(self.path)
        os.remove(self.path + '.found.jsonl')
        self.assertIn('added=0 already=3', self._run())
        self.assertEqual(len(self._listed()), 3)

    def test_dry_run_adds_nothing(self):
        out = self._run('--dry-run')
        self.assertIn('found=3 would add=3', out)
        self.assertFalse(BacklogItem.objects.exists())
        self.assertFalse(os.path.exists(self.path))

    def test_below_mate_leaves_the_mate_band_out(self):
        out = self._run('--below-mate')
        self.assertIn('added=2', out)
        self.assertEqual(self._listed(), [self.black.key, self.white.key])

    def test_the_tree_comes_first(self):
        root = _pos('root', _lines(5), backed_move='e1e2')
        reply = _pos('reply', _lines(5))
        Edge.objects.create(parent=root, child=reply, move_uci='e1e2')
        Edge.objects.create(parent=reply, child=self.white, move_uci='e8e7')
        with patch.object(ingest, 'root_key', return_value=root.key):
            out = self._run('--tree')
        self.assertIn('tree_first=1', out)
        self.assertEqual(self._listed(),
                         [self.white.key, self.mate.key, self.black.key])

    def test_the_order_itself(self):
        found = {'a': (3000, 1.0), 'b': (-4000, 0.0), 'c': (3000, 5.0),
                 'd': (9500, 0.0)}
        self.assertEqual(backlog.order_deep_wins(found),
                         ['d', 'b', 'c', 'a'])
        self.assertEqual(backlog.order_deep_wins(found, tree=['a', 'x']),
                         ['a', 'd', 'b', 'c'])
        self.assertEqual(backlog.order_deep_wins(found, below_mate=True),
                         ['b', 'c', 'a'])


# ---------------- subscriptions ----------------

def _adopted_of(username, arm='relines'):
    return AnalysisTask.objects.filter(requested_by=username, arm=arm,
                                       source=USER)


class Subscribing(BacklogTestCase):

    def test_subscribe_and_unsubscribe_from_the_box(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        response = self.client.post('/atomicdb/queue/subscribe/',
                                    {'backlog': 'lost-lines', 'size': '100'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('sub=subscribed', response['Location'])
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.username, sub.backlog, sub.batch_size,
                          sub.active), ('moky', 'lost-lines', 100, True))
        body = self.client.get(response['Location']).content.decode()
        self.assertIn('Subscribed to Lost lines', body)
        self.assertIn('Subscribed, 100 at a time', body)
        self.assertIn('Unsubscribe', body)
        # Subscribing never adopts by itself: the top-up does.
        self.assertFalse(AnalysisTask.objects.exists())

        response = self.client.post('/atomicdb/queue/subscribe/',
                                    {'backlog': 'lost-lines',
                                     'action': 'unsubscribe'})
        self.assertIn('sub=unsubscribed', response['Location'])
        sub.refresh_from_db()
        self.assertFalse(sub.active)
        self.assertEqual(sub.last_status, backlog.SUB_STOPPED)
        body = self.client.get(response['Location']).content.decode()
        self.assertIn('Unsubscribed from Lost lines', body)
        self.assertNotIn('Subscribed, 100 at a time', body)

    def test_the_default_batch_is_fifty_and_resubscribing_resizes(self):
        _offer(backlog.SHALLOW, [_shallow('s')])
        self.client.post('/atomicdb/queue/subscribe/', {'backlog': 'shallow'})
        self.assertEqual(BacklogSubscription.objects.get().batch_size, 50)
        body = self.client.get('/atomicdb/queue/').content.decode()
        self.assertIn('Subscribed, 50 at a time', body)
        self.client.post('/atomicdb/queue/subscribe/',
                         {'backlog': 'shallow', 'size': '25'})
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.batch_size, sub.active), (25, True))

    def test_the_box_offers_subscribe_with_fifty_preselected(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        body = self.client.get('/atomicdb/queue/').content.decode()
        self.assertIn('action="/atomicdb/queue/subscribe/"', body)
        self.assertIn('<option selected="" value="50">', body)

    def test_signed_in_only(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        anonymous = Client()
        response = anonymous.post('/atomicdb/queue/subscribe/',
                                  {'backlog': 'lost-lines', 'size': '50'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login/'))
        response = anonymous.post('/atomicdb/queue/subscribe/',
                                  {'backlog': 'lost-lines', 'format': 'json'})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(BacklogSubscription.objects.exists())
        self.assertNotIn('action="/atomicdb/queue/subscribe/"',
                         anonymous.get('/atomicdb/queue/').content.decode())
        self.assertEqual(backlog.subscribe('', backlog.LOST_LINES, 50),
                         'sign-in')
        self.assertEqual(self.client.get('/atomicdb/queue/subscribe/')
                         .status_code, 405)

    def test_only_the_offered_sizes_and_backlogs(self):
        response = self.client.post('/atomicdb/queue/subscribe/',
                                    {'backlog': 'lost-lines', 'size': '7',
                                     'format': 'json'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(backlog.subscribe('moky', 'nope', 50),
                         'bad-request')
        self.assertFalse(BacklogSubscription.objects.exists())

    def test_the_own_queue_page_shows_the_subscription_to_its_owner(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(10)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        backlog.top_up()
        body = self.client.get('/atomicdb/queue/moky/').content.decode()
        self.assertIn('Backlog subscriptions', body)
        self.assertIn('on, 25 at a time', body)
        self.assertIn('10 adopted by this subscription', body)
        # Nobody else sees it, signed in or not.
        worker_account('torom')
        other = Client()
        other.force_login(worker_account('eclipsia'))
        self.assertNotIn('Backlog subscriptions',
                         other.get('/atomicdb/queue/moky/').content.decode())
        self.assertNotIn('Backlog subscriptions',
                         Client().get('/atomicdb/queue/moky/')
                         .content.decode())


class TopUp(BacklogTestCase):

    def test_below_one_batch_it_fills_up_to_two_and_not_otherwise(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(120)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        summary = backlog.top_up()
        self.assertEqual((summary['topped_up'], summary['adopted']), (1, 50))
        mine = _adopted_of('moky')
        self.assertEqual(mine.count(), 50)
        for task in mine:
            self.assertEqual((task.budget_nodes, task.multipv, task.state),
                             (128_640_000, 5, PENDING))
        # 50 waiting: not below 25, nothing more.
        self.assertEqual(backlog.top_up()['adopted'], 0)
        # 25 served, 25 waiting: still not below 25.
        served = list(mine.values_list('id', flat=True)[:25])
        AnalysisTask.objects.filter(id__in=served).update(state=COMPLETED)
        self.assertEqual(backlog.top_up()['adopted'], 0)
        self.assertEqual(BacklogSubscription.objects.get().last_status,
                         backlog.SUB_ENOUGH)
        # One more served, 24 waiting: two batches, back above 50.
        AnalysisTask.objects.filter(id=mine.filter(state=PENDING)
                                    .first().id).update(state=COMPLETED)
        self.assertEqual(backlog.top_up()['adopted'], 50)
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.adopted_total, sub.last_status, sub.active),
                         (100, backlog.SUB_ADOPTED, True))
        self.assertIsNotNone(sub.checked_at)
        self.assertEqual(BacklogItem.objects.filter(
            state='adopted', adopted_by='moky').count(), 100)

    def test_a_refusal_after_a_batch_keeps_the_adopted_status(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(60)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        with patch.object(backlog, 'ADOPT_WAITING_MAX', 30):
            self.assertEqual(backlog.top_up()['adopted'], 30)
        self.assertEqual(BacklogSubscription.objects.get().last_status,
                         backlog.SUB_ADOPTED)

    def test_only_that_backlogs_waiting_positions_count(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        _offer(backlog.SHALLOW, [_shallow(f's{i}') for i in range(60)])
        backlog.adopt('moky', backlog.LOST_LINES, 25)
        backlog.subscribe('moky', backlog.SHALLOW, 25)
        self.assertEqual(backlog.top_up()['adopted'], 50)
        self.assertEqual(_adopted_of('moky', 'shallow').count(), 50)

    def test_it_respects_the_adopted_ceiling(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(60)])
        _offer(backlog.SHALLOW, [_shallow(f's{i}') for i in range(10)])
        with patch.object(backlog, 'ADOPT_WAITING_MAX', 30):
            backlog.adopt('moky', backlog.LOST_LINES, 25)
            backlog.subscribe('moky', backlog.SHALLOW, 25)
            self.assertEqual(backlog.top_up()['adopted'], 5)
            backlog.subscribe('moky', backlog.LOST_LINES, 100)
            backlog.top_up()
        subs = {sub.backlog: sub
                for sub in BacklogSubscription.objects.all()}
        self.assertEqual(subs['lost-lines'].last_status, 'adopted-full')
        self.assertTrue(subs['lost-lines'].active)
        self.assertEqual(backlog.adopted_waiting('moky'), 30)

    def test_it_respects_the_queue_allowance_and_leaves_the_receipt(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        for i in range(3):
            AnalysisTask.objects.create(position=_pos(f'mine{i}', _lines(5)),
                                        generation=1,
                                        budget_nodes=128_000_000,
                                        source=USER, requested_by='moky')
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        with patch('atomicdb.views.REQUEST_QUEUE_MAX', 3):
            self.assertEqual(backlog.top_up()['adopted'], 0)
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.last_status, sub.active), ('queue-full', True))
        self.assertTrue(DBEvent.objects.filter(
            kind='LANE_CAP_HIT', payload__account='moky').exists())
        with patch('atomicdb.views.REQUEST_QUEUE_MAX', 10):
            self.assertEqual(backlog.top_up()['adopted'], 7)

    def test_it_stops_when_the_backlog_is_empty(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        self.assertEqual(backlog.top_up()['adopted'], 30)
        AnalysisTask.objects.update(state=COMPLETED)
        summary = backlog.top_up()
        self.assertEqual((summary['adopted'], summary['ended']), (0, 1))
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.active, sub.last_status),
                         (False, backlog.SUB_EMPTY))
        self.assertEqual(backlog.top_up()['checked'], 0)
        body = self.client.get('/atomicdb/queue/').content.decode()
        self.assertIn('Your subscription ended when this list ran out', body)

    def test_a_list_whose_rows_all_left_on_the_way_ends_it_too(self):
        _offer(backlog.LOST_LINES, [_short(f'c{i}', status='WIN')
                                    for i in range(5)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        backlog.top_up()
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.active, sub.last_status),
                         (False, backlog.SUB_EMPTY))
        self.assertEqual(BacklogItem.objects.filter(state='dropped').count(),
                         5)

    def test_an_untick_wins_over_a_pass_in_flight(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        real_adopt = backlog.adopt

        def adopt_then_untick(*args):
            outcome = real_adopt(*args)
            backlog.unsubscribe('moky', backlog.LOST_LINES)
            return outcome

        with patch.object(backlog, 'adopt', adopt_then_untick):
            backlog.top_up()
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.active, sub.last_status),
                         (False, backlog.SUB_STOPPED))

    def test_a_disabled_account_is_turned_off(self):
        _offer(backlog.LOST_LINES, [_short('a')])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        self.user.is_active = False
        self.user.save()
        backlog.top_up()
        sub = BacklogSubscription.objects.get()
        self.assertEqual((sub.active, sub.last_status),
                         (False, backlog.SUB_ACCOUNT))
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_pass_is_bounded_and_the_rest_wait_their_turn(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(200)])
        names = [f'acct{i}' for i in range(4)]
        for name in names:
            worker_account(name)
            backlog.subscribe(name, backlog.LOST_LINES, 25)
        first = backlog.top_up(max_subscriptions=2)
        self.assertEqual(first['checked'], 2)
        second = backlog.top_up(max_subscriptions=2)
        self.assertEqual(second['checked'], 2)
        served = set(AnalysisTask.objects.values_list('requested_by',
                                                      flat=True))
        self.assertEqual(served, set(names))
        # Out of time before the first one: nothing started, all deferred.
        AnalysisTask.objects.update(state=COMPLETED)
        late = backlog.top_up(seconds=-1)
        self.assertEqual((late['checked'], late['deferred']), (0, 4))

    def test_one_failing_subscription_does_not_stop_the_others(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(100)])
        worker_account('torom')
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        backlog.subscribe('torom', backlog.LOST_LINES, 25)
        real_adopt = backlog.adopt

        def flaky(username, *args):
            if username == 'moky':
                raise RuntimeError('the database said no')
            return real_adopt(username, *args)

        with patch.object(backlog, 'adopt', flaky), \
                self.assertLogs('atomicdb.backlog', 'ERROR'):
            summary = backlog.top_up()
        self.assertEqual((summary['errors'], summary['adopted']), (1, 50))
        subs = {sub.username: sub
                for sub in BacklogSubscription.objects.all()}
        self.assertEqual((subs['moky'].last_status, subs['moky'].active),
                         (backlog.SUB_ERROR, True))
        self.assertEqual(_adopted_of('torom').count(), 50)

    def test_the_host_command_runs_the_top_up(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        out = StringIO()
        call_command('warm_public_snapshot', stdout=out, stderr=StringIO())
        self.assertIn('backlog top-up: checked=1 topped_up=1 adopted=30',
                      out.getvalue())
        self.assertEqual(_adopted_of('moky').count(), 30)

    def test_the_host_command_can_skip_it(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        call_command('warm_public_snapshot', '--no-backlog-top-up',
                     stdout=StringIO(), stderr=StringIO())
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_failing_top_up_does_not_break_the_host_command(self):
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        errors = StringIO()
        with patch.object(backlog, 'top_up',
                          side_effect=RuntimeError('boom')):
            call_command('warm_public_snapshot', stdout=StringIO(),
                         stderr=errors)
        self.assertIn('backlog top-up: failed', errors.getvalue())
        # Nor does a failure reading the subscriptions escape top_up itself.
        with patch.object(backlog.BacklogSubscription.objects, 'filter',
                          side_effect=RuntimeError('boom')), \
                self.assertLogs('atomicdb.backlog', 'ERROR'):
            self.assertEqual(backlog.top_up()['errors'], 1)

    def test_the_manual_pass_and_the_status_line(self):
        _offer(backlog.LOST_LINES, [_short(f'l{i}') for i in range(30)])
        backlog.subscribe('moky', backlog.LOST_LINES, 25)
        out = StringIO()
        call_command('atomicdb_backlog', 'status', stdout=out)
        self.assertIn('lost-lines: open=30 adopted=0 adopters=0 subscribed=1',
                      out.getvalue())
        out = StringIO()
        call_command('atomicdb_backlog', 'top-up', stdout=out)
        self.assertIn('adopted=30', out.getvalue())
