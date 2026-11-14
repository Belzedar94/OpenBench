"""What the AtomicDB worker prints, and the lease fields it prints from.

Report from the community (cz_eclipsia, 9-Nov-2026): the worker printed

    [0] task 3587968 (128000000n, 58.5s) -> {'queued': True, 'ingest_job':
    3406728, 'submit_timing_seconds': {'transaction': 0.015, ...}}

for every search.  Nobody knew what the dict meant, and nothing said which
position was being analysed.  A search now prints one line when it starts,
with the FEN of its position, and one when it ends, with the server's answer
in words.  The raw reply only prints with ``--verbose``.

The lease response also carries a line of moves and who the task is for
(``source`` and ``requested_by``).  The three keys are additive: a worker
that does not know them never reads them, and the lease never waits for the
line nor fails because of it.  Since build 2026111204 the worker no longer
prints the line: it often arrives abbreviated, and it cannot be pasted into
the website, while the FEN can (a contributor's request, 12-Nov-2026).
"""

import importlib.util
import io
import pathlib
import threading
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from . import ingest, logic, revalidate, views
from .models import AnalysisTask, Edge
from .testing import TestCase, worker_account

_SPEC = importlib.util.spec_from_file_location(
    'atomicdb_worker_console_under_test',
    pathlib.Path(__file__).resolve().parent.parent / 'Client'
    / 'atomicdb_worker.py')
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)

START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
E4_FEN = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
E4_SHOWN = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -'

CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-worker-console-tests',
    },
}


class NumberTests(SimpleTestCase):

    def test_counts_read_like_the_site(self):
        self.assertEqual(worker.human_count(128_000_000), '128M')
        self.assertEqual(worker.human_count(1_000_000_000), '1G')
        self.assertEqual(worker.human_count(2_190_000), '2.19M')
        self.assertEqual(worker.human_count(512), '512')
        self.assertEqual(worker.human_count(0), '0')
        self.assertEqual(worker.human_count(128_000_000, 1), '128.0M')
        self.assertEqual(worker.human_count(2_186_324, 2), '2.19M')

    def test_durations_stay_short(self):
        self.assertEqual(worker.human_duration(58.46), '58.5s')
        self.assertEqual(worker.human_duration(125), '2m05s')
        self.assertEqual(worker.human_duration(3725), '1h02m')
        self.assertEqual(worker.human_duration(90061), '1d01h')
        self.assertEqual(worker.human_duration(-3), '0.0s')

    def test_evals_are_from_whites_point_of_view(self):
        self.assertEqual(worker.format_eval({'eval_cp': 123, 'mate': None}),
                         '+1.23')
        self.assertEqual(worker.format_eval({'eval_cp': -40, 'mate': None}),
                         '-0.40')
        self.assertEqual(worker.format_eval({'eval_cp': 0, 'mate': None}),
                         '+0.00')
        self.assertEqual(worker.format_eval({'eval_cp': 9995, 'mate': 5}),
                         '#5')
        self.assertEqual(worker.format_eval({'eval_cp': -9997, 'mate': -3}),
                         '#-3')
        self.assertEqual(worker.format_eval({}), '?')


class AsciiTests(SimpleTestCase):
    """A console that cannot encode a character must not end the job."""

    def test_the_servers_typography_becomes_ascii(self):
        self.assertEqual(
            worker.ascii_text('deep line, ply ≥96 · … Qh5'),
            'deep line, ply >=96 - ... Qh5')

    def test_anything_else_is_replaced_not_raised(self):
        self.assertEqual(worker.ascii_text('café ♞'), 'caf? ?')

    def test_every_line_printed_is_ascii(self):
        task = {'id': 1, 'fen': E4_FEN, 'budget_nodes': 1,
                'line': '1. e4 … deep', 'source': 'USER',
                'requested_by': 'Jürgen'}
        for text in (worker.format_task_start(task, 'me'),
                     worker.describe_submit_reply(
                         200, {'ok': True,
                               'summary': {'ingest_error': '≥ fail'}})):
            self.assertTrue(text.isascii(), text)


class PositionTests(SimpleTestCase):

    def test_the_fen_is_printed_even_with_a_line(self):
        for line in ('1. e4', '1. e4 … Kf8 …', '…'):
            self.assertEqual(
                worker.task_position({'fen': E4_FEN, 'line': line}),
                'FEN ' + E4_SHOWN)

    def test_without_a_line_the_fen_is_printed_to_paste(self):
        self.assertEqual(worker.task_position({'fen': E4_FEN}),
                         'FEN ' + E4_SHOWN)
        self.assertEqual(worker.task_position({'fen': E4_FEN, 'line': ''}),
                         'FEN ' + E4_SHOWN)

    def test_the_fen_keeps_castling_and_en_passant_not_the_counters(self):
        fen = 'rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w Kq d6 0 3'
        self.assertEqual(worker.task_position({'fen': '  ' + fen + '\n'}),
                         'FEN rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR'
                         ' w Kq d6')

    def test_the_printed_fen_is_one_the_website_takes(self):
        fen = 'rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w Kq d6 0 3'
        shown = worker.task_position({'fen': fen})[len('FEN '):]
        self.assertEqual(views._parse_public_fen(shown),
                         views._parse_public_fen(fen))
        self.assertEqual(logic.key_of(views._parse_public_fen(shown)),
                         logic.key_of(fen))

    def test_a_long_fen_is_never_cut(self):
        fen = ('r1bqk2r/pp1n1ppp/2pbpn2/3p4/2PP4/2NBPN2/PP3PPP/R1BQK2R '
               'w KQkq -')
        task = {'fen': fen, 'budget_nodes': 128_000_000,
                'searchmoves': ['e1g1', 'd1c2'], 'source': 'USER',
                'requested_by': 'SomeoneWithAVeryLongUserNameIndeed'}
        for line in ('', '1. d4 Nf6 2. c4 e6 3. Nc3 d5 4. e3 c6 5. Nf3 Nbd7 '
                         '6. Bd3 Bd6 7. Bd2 … 13. Qb3'):
            shown = worker.format_task_start(dict(task, line=line), 'me')
            self.assertGreater(len(shown), 100)
            self.assertTrue(shown.startswith('searching FEN ' + fen + ' |'),
                            shown)
            self.assertNotIn('...', shown)

    def test_the_identity_of_a_position_is_still_board_and_side(self):
        self.assertEqual(worker.short_fen(E4_FEN),
                         'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b')
        self.assertEqual(worker.task_position(
            {'fen': 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - - 5 9'}),
            'FEN rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - -'
            ' (the start position)')

    def test_the_start_position_prints_its_fen_and_says_so(self):
        for line in ('', None):
            self.assertEqual(
                worker.task_position({'fen': START_FEN, 'line': line}),
                'FEN rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -'
                ' (the start position)')

    def test_the_move_line_is_never_printed(self):
        task = {'fen': E4_FEN, 'budget_nodes': 128_000_000,
                'line': '1. Nf3 f6 2. Nc3 Nh6 3. g4 c6 4. d4 e6 … Kf8 '
                        '10. b4 b6 …'}
        shown = worker.format_task_start(task, 'me')
        self.assertEqual(shown, 'searching FEN ' + E4_SHOWN + ' | 128M nodes')
        self.assertFalse(hasattr(worker, 'shorten_line'))


class RequesterTests(SimpleTestCase):

    def test_who_the_search_is_for(self):
        self.assertEqual(worker.task_requester(
            {'source': 'USER', 'requested_by': 'Eclipsia'}, 'eclipsia'),
            'your request')
        self.assertEqual(worker.task_requester(
            {'source': 'USER', 'requested_by': 'Torom'}, 'eclipsia'),
            "Torom's request")
        self.assertEqual(worker.task_requester(
            {'source': 'USER', 'requested_by': ''}, 'eclipsia'),
            'an anonymous request')
        for source in ('AUTO', 'FILL'):
            self.assertEqual(worker.task_requester(
                {'source': source, 'requested_by': ''}, 'eclipsia'),
                'server work')

    def test_an_older_server_says_nothing_and_neither_does_the_line(self):
        task = {'id': 7, 'fen': E4_FEN, 'budget_nodes': 128_000_000,
                'multipv': 5, 'searchmoves': [], 'lease_token': 't'}
        self.assertEqual(worker.task_requester(task, 'me'), '')
        self.assertEqual(
            worker.format_task_start(task, 'me'),
            'searching FEN ' + E4_SHOWN + ' | 128M nodes')


class StartLineTests(SimpleTestCase):

    TASK = {'id': 7, 'fen': E4_FEN, 'budget_nodes': 128_000_000,
            'multipv': 5, 'searchmoves': [], 'lease_token': 't',
            'line': '1. e4', 'source': 'USER', 'requested_by': 'me'}

    def test_one_job_prints_no_slot(self):
        self.assertEqual(worker.format_task_start(self.TASK, 'me'),
                         'searching FEN ' + E4_SHOWN
                         + ' | 128M nodes | your request')

    def test_several_jobs_print_their_slot(self):
        self.assertEqual(
            worker.format_task_start(self.TASK, 'me', slot=2, jobs=4),
            '[2] searching FEN ' + E4_SHOWN + ' | 128M nodes | your request')

    def test_a_restricted_search_says_so(self):
        task = dict(self.TASK, searchmoves=['e7e5', 'd7d5', 'g8f6'])
        self.assertEqual(
            worker.format_task_start(task, 'me'),
            'searching FEN ' + E4_SHOWN
            + ' | 128M nodes | 3 root moves | your request')


class SubmitReplyTests(SimpleTestCase):
    """Every answer ``api/submit`` gives, in words."""

    def test_the_reply_eclipsia_reported(self):
        body = {'ok': True, 'summary': {
            'queued': True, 'ingest_job': 3406728,
            'submit_timing_seconds': {'transaction': 0.015, 'apply': 0.0,
                                      'total': 0.382}}}
        self.assertEqual(worker.describe_submit_reply(200, body),
                         'sent, queued for the tree')

    def test_a_result_applied_on_the_spot(self):
        self.assertEqual(worker.describe_submit_reply(
            200, {'ok': True, 'summary': {'children': 5}}),
            'sent, added to the tree')
        self.assertEqual(worker.describe_submit_reply(
            200, {'ok': True, 'summary': {'ingest_job': 4,
                                          'ingest_error': 'boom'}}),
            'sent, but the server could not add it to the tree: boom')

    def test_a_replayed_submit(self):
        self.assertEqual(
            worker.describe_submit_reply(200, {'ok': True, 'dup': True}),
            'already delivered earlier; nothing more to do')

    def test_the_rejections(self):
        cases = {
            (400, 'not-leased'): 'no longer leased',
            (409, 'not-your-lease'): 'handed to another worker',
            (409, 'stale-lease'): 'lease expired',
            (403, 'bad credentials'): 'wrong username or password',
        }
        for (status, error), words in cases.items():
            text = worker.describe_submit_reply(status, {'error': error})
            self.assertTrue(text.startswith('not accepted: '), text)
            self.assertIn(words, text)

    def test_an_engine_the_server_refuses_carries_its_detail(self):
        text = worker.describe_submit_reply(
            403, {'error': 'engine-not-accepted',
                  'detail': 'download the reference engine'})
        self.assertEqual(
            text, 'not accepted: the server does not take results from this '
                  'engine build (download the reference engine)')

    def test_a_malformed_submit(self):
        self.assertEqual(worker.describe_submit_reply(
            400, {'error': 'malformed: invalid tb_wdl'}),
            'rejected as malformed: invalid tb_wdl')

    def test_anything_unknown_still_reads(self):
        self.assertEqual(worker.describe_submit_reply(
            418, {'error': 'teapot'}), 'not accepted (HTTP 418): teapot')
        self.assertEqual(worker.describe_submit_reply(404, {}),
                         'not accepted (HTTP 404)')
        self.assertEqual(worker.describe_submit_reply(403, None),
                         'unexpected server answer (HTTP 403)')


class DoneLineTests(SimpleTestCase):

    LINES = [{'move': 'e2e4', 'pv': ['e2e4'], 'eval_cp': 123, 'mate': None,
              'raw': ''}]

    def test_the_line_a_finished_search_prints(self):
        self.assertEqual(
            worker.format_task_done(58.5, 128_000_000, self.LINES,
                                    'sent, queued for the tree'),
            'done in 58.5s | 128.0M nodes | 2.19M nodes/s | best move e2e4 '
            '(+1.23) | sent, queued for the tree')

    def test_with_slots_and_without_lines(self):
        self.assertEqual(
            worker.format_task_done(1.0, 0, [], 'not sent: timeout',
                                    slot=1, jobs=2),
            '[1] done in 1.0s | not sent: timeout')

    def test_a_tablebase_answer(self):
        self.assertEqual(
            worker.format_tb_done(2, 13, 'sent, queued for the tree'),
            'tablebase: win for the side to move (dtz 13) | sent, queued '
            'for the tree')
        self.assertEqual(
            worker.format_tb_done(0, None, 'sent'),
            'tablebase: draw for the side to move | sent')


class SessionStatusTests(SimpleTestCase):

    def test_the_status_line(self):
        now = [1000.0]
        stats = worker.SessionStats(clock=lambda: now[0], interval=300)
        self.assertEqual(stats.status_line(), 'status: up 0.0s | 0 tasks done')
        stats.record(100_000_000, 50.0)
        stats.record(0, 0)            # a tablebase answer: no search
        now[0] += 3725
        self.assertEqual(
            stats.status_line(),
            'status: up 1h02m | 2 tasks done | average 2.00M nodes/s per '
            'engine | machine total 26.85k nodes/s')

    def test_the_machine_total_adds_up_the_jobs(self):
        # Four jobs searching at once for the whole hour, 2M nodes/s each.
        now = [0.0]
        stats = worker.SessionStats(clock=lambda: now[0], interval=300)
        for _ in range(4):
            stats.record(7_200_000_000, 3600.0)
        now[0] = 3600.0
        self.assertIn('average 2.00M nodes/s per engine', stats.status_line())
        self.assertIn('machine total 8.00M nodes/s', stats.status_line())


class SyzygyDirsTests(SimpleTestCase):

    def test_semicolons_work_everywhere(self):
        self.assertEqual(worker.split_syzygy_dirs('/a;/b', pathsep=':'),
                         ['/a', '/b'])
        self.assertEqual(worker.split_syzygy_dirs(r'C:\a;D:\b', pathsep=';'),
                         [r'C:\a', r'D:\b'])

    def test_colons_split_only_where_they_are_the_separator(self):
        self.assertEqual(worker.split_syzygy_dirs('/a:/b', pathsep=':'),
                         ['/a', '/b'])
        self.assertEqual(worker.split_syzygy_dirs(r'C:\tb', pathsep=';'),
                         [r'C:\tb'])

    def test_empty_pieces_are_dropped(self):
        self.assertEqual(worker.split_syzygy_dirs(';/a;;/b:', pathsep=':'),
                         ['/a', '/b'])

    def test_it_is_due_at_most_once_per_interval(self):
        now = [0.0]
        stats = worker.SessionStats(clock=lambda: now[0], interval=300)
        self.assertFalse(stats.due())
        now[0] = 299
        self.assertFalse(stats.due())
        now[0] = 300
        self.assertTrue(stats.due())
        self.assertFalse(stats.due())     # a second job's heartbeat
        now[0] = 599
        self.assertFalse(stats.due())
        now[0] = 600
        self.assertTrue(stats.due())

    def test_never_more_than_once_a_minute(self):
        self.assertGreaterEqual(worker.STATUS_INTERVAL_SECONDS, 60)


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = repr(body)

    def json(self):
        return self._body


class _Engine:
    def __init__(self, path, threads=1, hash_mb=256, syzygy=''):
        self.ready = True

    def analyse(self, fen, nodes, multipv, searchmoves=None, progress=None):
        return [{'move': 'e7e5', 'pv': ['e7e5'], 'eval_cp': 31, 'mate': None,
                 'raw': 'info depth 30 seldepth 40 multipv 1 score cp -31 '
                        'nodes 128000000 nps 2190000 pv e7e5'}]

    def close(self):
        pass


class SlotOutputTests(SimpleTestCase):
    """What one search prints, end to end through ``_analysis_slot``."""

    REPLY = {'ok': True, 'summary': {
        'queued': True, 'ingest_job': 3406728,
        'submit_timing_seconds': {'transaction': 0.015, 'apply': 0.0,
                                  'total': 0.382}}}

    def run_slot(self, verbose):
        stop = threading.Event()
        script = [[{'id': 3587968, 'fen': E4_FEN,
                    'budget_nodes': 128_000_000, 'multipv': 5,
                    'searchmoves': [], 'lease_token': 'tok', 'line': '1. e4',
                    'source': 'USER', 'requested_by': 'Torom'}]]

        def request_tasks(server, auth, lease_session):
            if not script:
                stop.set()
                return []
            return script.pop(0)

        a = SimpleNamespace(engine='engine', hash=16, syzygy='',
                            S='https://server', no_auto_update=True,
                            once=False, verbose=verbose)
        with mock.patch.object(worker, 'Engine', _Engine), \
                mock.patch.object(worker, '_request_tasks', request_tasks), \
                mock.patch.object(worker, '_submit_until_definitive',
                                  lambda *args: _Response(200, self.REPLY)), \
                mock.patch.object(worker, '_heartbeat_loop',
                                  lambda *args: None), \
                mock.patch.object(worker, 'SESSION', worker.SessionStats()), \
                redirect_stdout(io.StringIO()) as out:
            worker._analysis_slot(
                0, a, None, {'machine': 'box-atomicdb', 'username': 'cz'},
                {}, 1, stop, False)
            recorded = worker.SESSION.tasks
        return out.getvalue().splitlines(), recorded

    def test_the_console_reads_without_the_dict(self):
        lines, recorded = self.run_slot(verbose=False)
        self.assertEqual(lines[0], 'searching FEN ' + E4_SHOWN
                         + " | 128M nodes | Torom's request")
        # The fake engine answers at once, so the speed may not print.
        self.assertRegex(lines[1], r'^done in \d+\.\ds \| 128\.0M nodes '
                                   r'(\| \S+ nodes/s )?\| best move e7e5 '
                                   r'\(\+0\.31\) \| sent, queued for the tree$')
        self.assertEqual(lines[2:], ['[0] no tasks available; waiting 60s'])
        self.assertNotIn('submit_timing_seconds', '\n'.join(lines))
        self.assertEqual(recorded, 1)

    def test_verbose_keeps_the_raw_reply(self):
        lines, _ = self.run_slot(verbose=True)
        text = '\n'.join(lines)
        self.assertIn('[0] task 3587968: fen ' + E4_FEN, text)
        self.assertIn('[0] task 3587968 (128000000n, ', text)
        self.assertIn("'submit_timing_seconds'", text)
        self.assertIn('sent, queued for the tree', text)


def _play(parent, uci):
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


class _Deferred:
    """Collects the background refreshes instead of running them."""

    def __init__(self):
        self.jobs = []

    def __call__(self, work, name):
        self.jobs.append(work)

    def run_all(self):
        pending, self.jobs = self.jobs, []
        for work in pending:
            work()
        return len(pending)


@override_settings(CACHES=CACHE_FOR_TESTS)
class LeaseLineTests(TestCase):
    """The three keys the lease gained for the worker's console."""

    def setUp(self):
        worker_account('u', 'p')
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.e4 = _play(self.root, 'e2e4')
        self.refreshes = _Deferred()
        patch = mock.patch.object(revalidate, 'EXECUTOR', self.refreshes)
        patch.start()
        self.addCleanup(patch.stop)

    def lease(self):
        response = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'box-atomicdb',
            'tb': '1', 'worker_build': '2026110901', 'lease_session': 's1'})
        self.assertEqual(response.status_code, 200)
        tasks = response.json()['tasks']
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def task(self, position, **fields):
        return AnalysisTask.objects.create(
            position=position, generation=0, budget_nodes=1_000_000,
            state=AnalysisTask.TState.PENDING, **fields)

    def test_a_request_says_whose_it_is_and_its_line(self):
        self.task(self.e4, source=AnalysisTask.Source.USER,
                  requested_by='alice')
        views._line_labels_many([self.e4.key])      # the home page did it
        task = self.lease()
        self.assertEqual(task['source'], 'USER')
        self.assertEqual(task['requested_by'], 'alice')
        self.assertEqual(task['line'], '1. e4')

    def test_server_work_says_so(self):
        self.task(self.e4)
        views._line_labels_many([self.e4.key])
        task = self.lease()
        self.assertEqual(task['source'], 'AUTO')
        self.assertEqual(task['requested_by'], '')
        self.assertEqual(task['line'], '1. e4')

    def test_the_lease_never_walks_the_line_itself(self):
        self.task(self.e4)
        with mock.patch.object(views, '_walk_lines_to_root',
                               wraps=views._walk_lines_to_root) as walk:
            task = self.lease()
            self.assertEqual(walk.call_count, 0)
            self.assertEqual(task['line'], '')
            # ...and leaves the walk to the background, for the next reader.
            self.assertEqual(self.refreshes.run_all(), 1)
            self.assertEqual(walk.call_count, 1)
        self.assertEqual(
            views._line_labels_many([self.e4.key], wait=False),
            {self.e4.key: ('1. e4', '1. e4')})

    def test_the_declared_route_wins_over_the_lineage(self):
        # Two move orders into one position: the requester came by the
        # second, and that is the line its worker shows.
        a = _play(_play(_play(self.root, 'g1f3'), 'g8f6'), 'b1c3')
        b = _play(_play(_play(self.root, 'b1c3'), 'g8f6'), 'g1f3')
        self.assertEqual(a.key, b.key)
        route = 'b1c3,g8f6,g1f3'
        self.task(a, source=AnalysisTask.Source.USER, requested_by='alice',
                  route=route)
        views._route_labels_many([(route, a.key)])
        task = self.lease()
        self.assertEqual(task['line'], '1. Nc3 Nf6 2. Nf3')

    def test_a_broken_label_does_not_break_the_lease(self):
        self.task(self.e4)
        with mock.patch.object(views, '_line_labels_many',
                               side_effect=RuntimeError('boom')), \
                self.assertLogs('atomicdb.views', 'ERROR'):
            task = self.lease()
        self.assertEqual(task['line'], '')
        self.assertEqual(task['source'], 'AUTO')

    def test_an_old_worker_reads_the_fields_it_always_read(self):
        self.task(self.e4)
        task = self.lease()
        for key in ('id', 'fen', 'budget_nodes', 'multipv', 'searchmoves',
                    'lease_token'):
            self.assertIn(key, task)
