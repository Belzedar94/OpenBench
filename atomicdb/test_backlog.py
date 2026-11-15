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
  file and reads only with ``--dry-run``.
"""

import hashlib
import json
import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client

from . import backlog, idle_repair, ingest, logic
from .models import AnalysisTask, BacklogItem, DBEvent, Position
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
