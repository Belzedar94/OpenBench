"""How the worker searches: stop at the mate, a clean engine per task, only
exact lines from a finished iteration, and what it does when a search fails.

Release of 11-Nov-2026 (worker build 2026111101), item 5 of the AtomicDB plan:

  * ``go nodes N mate 250`` stops at the first mate.  Closures were 14 % of the
    fleet's compute and needed a median of ~2,400 nodes of their 128M;
  * ``ucinewgame`` + ``isready`` before every task, so a result does not
    depend on the position the engine searched before;
  * the lines sent are the last complete iteration's, never a
    ``lowerbound``/``upperbound`` line or the half of an iteration that the
    node limit cut;
  * an engine that dies hands its lease back at once, an empty result is
    never sent, and an engine that is not an atomic one is refused at start;
  * one engine per thread of -T unless --jobs says otherwise.
"""

import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

_SPEC = importlib.util.spec_from_file_location(
    'atomicdb_worker_search_under_test',
    pathlib.Path(__file__).resolve().parent.parent / 'Client'
    / 'atomicdb_worker.py')
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)

START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'


def _info(depth, multipv, score, nodes, pv, bound=''):
    kind, value = score
    bound = f' {bound}' if bound else ''
    return (f'info depth {depth} seldepth {depth + 2} multipv {multipv} '
            f'score {kind} {value}{bound} nodes {nodes} nps 1000000 '
            f'hashfull 1 tbhits 0 time 10 pv {pv}\n')


class _Process:
    """A stand-in for the engine's Popen: scripted output, recorded input."""

    def __init__(self, output, code=3):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)
        self.code = code

    def wait(self, timeout=None):
        return self.code

    def sent(self):
        return [line for line in self.stdin.getvalue().splitlines() if line]


def _engine(output):
    engine = worker.Engine.__new__(worker.Engine)
    engine.p = _Process(output)
    engine.last_nodes = 0
    return engine


class GoCommandTests(SimpleTestCase):
    """What the worker tells the engine for one task, in order."""

    def test_every_task_clears_the_engine_and_waits_before_searching(self):
        engine = _engine('readyok\n' + _info(1, 1, ('cp', 12), 100, 'e2e4')
                         + 'bestmove e2e4\n')

        engine.analyse(START_FEN, 1000, 3)

        self.assertEqual(engine.p.sent(), [
            'ucinewgame', 'setoption name MultiPV value 3', 'isready',
            f'position fen {START_FEN}', 'go nodes 1000 mate 250'])

    def test_searchmoves_stays_last_on_the_go_line(self):
        engine = _engine('readyok\n' + _info(1, 1, ('cp', 12), 100, 'e2e4')
                         + 'bestmove e2e4\n')

        engine.analyse(START_FEN, 1000, 2, searchmoves=['e2e4', 'd2d4'])

        self.assertEqual(engine.p.sent()[-1],
                         'go nodes 1000 mate 250 searchmoves e2e4 d2d4')

    def test_the_mate_stop_can_be_turned_off(self):
        engine = _engine('readyok\n' + _info(1, 1, ('cp', 12), 100, 'e2e4')
                         + 'bestmove e2e4\n')

        engine.analyse(START_FEN, 1000, 1, mate=0)

        self.assertEqual(engine.p.sent()[-1], 'go nodes 1000')

    def test_an_engine_that_dies_while_clearing_raises(self):
        engine = _engine('')          # no readyok: the process is gone

        with self.assertRaises(worker.EngineExited) as caught:
            engine.analyse(START_FEN, 1000, 1)

        self.assertNotIn('position', ' '.join(engine.p.sent()))
        self.assertIn('exit code 3', str(caught.exception))


class IterationFilterTests(SimpleTestCase):
    """Only the last complete, exact iteration becomes the result."""

    def analyse(self, body, multipv=2):
        engine = _engine('readyok\n' + body + 'bestmove e2e4\n')
        return engine.analyse(START_FEN, 10 ** 9, multipv), engine

    def test_a_bound_line_never_becomes_the_result(self):
        lines, _ = self.analyse(
            _info(10, 1, ('cp', 30), 1000, 'e2e4 e7e5')
            + _info(10, 2, ('cp', 20), 1000, 'd2d4 d7d5')
            # Fail high at depth 11, then the node limit: no exact depth 11.
            + _info(11, 1, ('cp', 75), 2000, 'g1f3', bound='lowerbound')
            + _info(11, 1, ('cp', 75), 2000, 'g1f3', bound='lowerbound')
            + _info(10, 2, ('cp', 20), 2000, 'd2d4 d7d5'))

        self.assertEqual([(line['move'], line['eval_cp']) for line in lines],
                         [('e2e4', 30), ('d2d4', 20)])
        self.assertTrue(all('bound' not in line['raw'] for line in lines))

    def test_an_iteration_cut_by_the_node_limit_is_dropped(self):
        # The last group: line 1 already at depth 12, line 2 still the
        # depth-11 line it was.  A mix of two iterations, not a result.
        lines, engine = self.analyse(
            _info(11, 1, ('cp', 30), 1000, 'e2e4')
            + _info(11, 2, ('cp', 20), 1000, 'd2d4')
            + _info(12, 1, ('cp', 45), 1800, 'c2c4')
            + _info(11, 2, ('cp', 20), 1800, 'd2d4'))

        self.assertEqual([line['move'] for line in lines], ['e2e4', 'd2d4'])
        self.assertIn('depth 11', lines[0]['raw'])
        # The nodes are the search's, not the kept iteration's.
        self.assertEqual(engine.last_nodes, 1800)

    def test_the_last_complete_iteration_wins(self):
        lines, _ = self.analyse(
            _info(11, 1, ('cp', 30), 1000, 'e2e4')
            + _info(11, 2, ('cp', 20), 1000, 'd2d4')
            + _info(12, 1, ('cp', 45), 1800, 'c2c4')
            + _info(12, 2, ('cp', 40), 1900, 'e2e4'))

        self.assertEqual([(line['move'], line['eval_cp']) for line in lines],
                         [('c2c4', 45), ('e2e4', 40)])

    def test_fewer_legal_moves_than_lines_asked_for(self):
        lines, _ = self.analyse(
            _info(5, 1, ('cp', 30), 1000, 'e2e4')
            + _info(5, 2, ('cp', 20), 1000, 'd2d4'), multipv=5)

        self.assertEqual(len(lines), 2)

    def test_no_complete_iteration_means_no_lines(self):
        lines, _ = self.analyse(
            _info(1, 1, ('cp', 30), 10, 'e2e4', bound='upperbound'))

        self.assertEqual(lines, [])

    def test_black_to_move_scores_stay_white_point_of_view(self):
        fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
        engine = _engine('readyok\n' + _info(4, 1, ('cp', 25), 100, 'e7e5')
                         + _info(4, 2, ('mate', -3), 100, 'g8f6')
                         + 'bestmove e7e5\n')

        lines = engine.analyse(fen, 1000, 2)

        self.assertEqual(lines[0]['eval_cp'], -25)
        self.assertEqual(lines[1]['mate'], 3)
        self.assertEqual(set(lines[0]), {'move', 'pv', 'eval_cp', 'mate',
                                         'raw'})


# An engine as a process: it logs what it hears, with the time it heard it,
# and searches according to its go line.
_FAKE_ENGINE = r'''
import queue, sys, threading, time

log = open(sys.argv[1], 'a')
heard = queue.Queue()

def reader():
    for line in sys.stdin:
        heard.put((time.monotonic(), line.strip()))
    heard.put((time.monotonic(), None))

threading.Thread(target=reader, daemon=True).start()
ready_at = None
position_at = None
while True:
    at, line = heard.get()
    if line is None or line == 'quit':
        break
    words = line.split()
    if not words:
        continue
    log.write(line + '\n'); log.flush()
    if words[0] == 'uci':
        print('id name Atomic-Stockfish fake', flush=True)
        print('uciok', flush=True)
    elif words[0] == 'isready':
        time.sleep(0.3)            # clearing a big hash takes a moment
        ready_at = time.monotonic()
        print('readyok', flush=True)
    elif words[0] == 'position':
        position_at = at
        log.write('position-after-readyok %s\n'
                  % (ready_at is not None and position_at >= ready_at))
        log.flush()
    elif words[0] == 'go':
        if 'mate' in words:
            # Mate found at depth 3: the completed iteration ends the search.
            for depth in (1, 2, 3):
                score = 'cp 50' if depth < 3 else 'mate 2'
                print('info depth %d seldepth %d multipv 1 score %s nodes %d '
                      'nps 1000 pv e2e4 e7e5 d1h5' % (depth, depth, score,
                                                       1000 * depth),
                      flush=True)
        else:
            for depth in range(1, 21):
                score = 'cp 50' if depth < 3 else 'mate 2'
                print('info depth %d seldepth %d multipv 1 score %s nodes %d '
                      'nps 1000 pv e2e4 e7e5 d1h5' % (depth, depth, score,
                                                       100000 * depth),
                      flush=True)
        print('bestmove e2e4', flush=True)
'''


class EngineProcessTests(SimpleTestCase):
    """The same, against a real process that answers like Stockfish."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)
        self.script = self.dir / 'fake_engine.py'
        self.script.write_text(_FAKE_ENGINE, encoding='utf-8')
        self.log = self.dir / 'heard.log'
        real = subprocess.Popen

        def popen(command, **kwargs):
            return real([sys.executable, str(self.script), str(self.log)],
                        **kwargs)

        patcher = mock.patch.object(worker.subprocess, 'Popen', popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def heard(self):
        return self.log.read_text(encoding='utf-8').splitlines()

    def test_the_search_starts_only_once_the_engine_is_cleared(self):
        engine = worker.Engine('engine', hash_mb=16)
        self.addCleanup(engine.close)

        engine.analyse(START_FEN, 2_000_000, 1)
        engine.analyse(START_FEN, 2_000_000, 1)

        heard = self.heard()
        self.assertEqual(heard.count('ucinewgame'), 2)
        self.assertEqual(heard.count('position-after-readyok True'), 2)
        self.assertNotIn('position-after-readyok False', heard)
        for start in [i for i, line in enumerate(heard)
                      if line == 'ucinewgame']:
            self.assertEqual(heard[start + 1:start + 3],
                             ['setoption name MultiPV value 1', 'isready'])

    def test_a_mate_stops_the_search_and_reports_the_nodes_it_took(self):
        engine = worker.Engine('engine', hash_mb=16)
        self.addCleanup(engine.close)

        lines = engine.analyse(START_FEN, 2_000_000, 1)

        self.assertEqual(lines[0]['mate'], 2)
        self.assertEqual(lines[0]['pv'], ['e2e4', 'e7e5', 'd1h5'])
        self.assertEqual(engine.last_nodes, 3000)
        self.assertIn('go nodes 2000000 mate 250', self.heard())

    def test_without_the_stop_the_whole_budget_is_spent(self):
        engine = worker.Engine('engine', hash_mb=16)
        self.addCleanup(engine.close)

        engine.analyse(START_FEN, 2_000_000, 1, mate=0)

        self.assertEqual(engine.last_nodes, 2_000_000)

    def test_the_identity_check_accepts_an_atomic_engine(self):
        self.assertIsNone(worker.check_engine_identity('engine'))


class IdentityTests(SimpleTestCase):
    """The worker refuses to start on an engine for another game."""

    def test_atomic_stockfish_passes(self):
        self.assertIsNone(worker.engine_identity_problem(
            'id name Atomic-Stockfish dev-20261102-7f705c8f\n'
            'id author the Atomic-Stockfish developers\n'
            'option name Hash type spin default 16 min 1 max 33554432\n'
            'uciok\n'))

    def test_a_chess_engine_is_refused(self):
        problem = worker.engine_identity_problem(
            'id name Stockfish 17.1\nuciok\n')
        self.assertIn('Stockfish 17.1', problem)

    def test_fairy_stockfish_on_chess_is_refused(self):
        problem = worker.engine_identity_problem(
            'id name Fairy-Stockfish atomic build\n'
            'option name UCI_Variant type combo default chess var chess '
            'var atomic\nuciok\n')
        self.assertIn('chess', problem)

    def test_a_variant_engine_that_defaults_to_atomic_passes(self):
        self.assertIsNone(worker.engine_identity_problem(
            'id name Fairy-Stockfish atomic\n'
            'option name UCI_Variant type combo default atomic var chess '
            'var atomic\nuciok\n'))

    def test_no_answer_is_refused(self):
        self.assertIn('id name', worker.engine_identity_problem(''))


class _ScriptedEngine:
    """An ``Engine`` whose searches ``behaviour`` decides, one per call."""

    started = 0

    def __init__(self, path, threads=1, hash_mb=256, syzygy=''):
        type(self).started += 1
        self.ready = True
        self.last_nodes = 0

    def analyse(self, fen, nodes, multipv, searchmoves=None, progress=None):
        return self.behaviour(self)

    def close(self):
        pass


class SlotFailureTests(SimpleTestCase):
    """``_analysis_slot`` when a search does not produce a result."""

    def run_slot(self, behaviour, release_answer=(1, '')):
        stop = threading.Event()
        held = worker.HeldLeases()
        submitted, released = [], []
        script = [[{'id': 9, 'fen': START_FEN, 'budget_nodes': 128_000_000,
                    'multipv': 1, 'lease_token': 'tok-9'}]]

        def request_tasks(server, auth, lease_session):
            if not script:
                stop.set()
                return []
            return script.pop(0)

        def submit(server, payload, task_id):
            submitted.append(payload)
            return SimpleNamespace(status_code=200, text='',
                                   json=lambda: {'ok': True})

        def release(server, auth, leases, **kwargs):
            released.append(leases)
            return release_answer

        engine = type('Engine', (_ScriptedEngine,), {
            'started': 0, 'behaviour': staticmethod(behaviour)})
        a = SimpleNamespace(engine='engine', hash=16, syzygy='',
                            S='https://server', no_auto_update=True,
                            once=False)
        with mock.patch.object(worker, 'Engine', engine), \
                mock.patch.object(worker, '_request_tasks', request_tasks), \
                mock.patch.object(worker, '_submit_until_definitive', submit), \
                mock.patch.object(worker, '_release_leases', release), \
                mock.patch.object(worker, 'ENGINE_FAILURE_BACKOFF_SECONDS',
                                  0.01), \
                mock.patch.object(worker, '_heartbeat_loop',
                                  lambda *args: None), \
                redirect_stdout(io.StringIO()) as out:
            worker._analysis_slot(0, a, None, {'machine': 'box-atomicdb'},
                                  {}, 1, stop, False, held=held)
        return submitted, released, held, engine, out.getvalue()

    def test_a_dead_engine_hands_its_lease_back_at_once(self):
        def dies(engine):
            raise worker.EngineExited('the engine stopped in the middle of '
                                      'a search (exit code 1)', 1)

        submitted, released, held, engine, out = self.run_slot(dies)

        self.assertEqual(submitted, [])
        self.assertEqual(released, [[{
            'kind': 'analysis', 'machine': 'box-atomicdb', 'task_id': 9,
            'lease_token': 'tok-9'}]])
        self.assertEqual(held.snapshot(), [])
        self.assertEqual(engine.started, 2, 'the engine was not restarted')
        self.assertIn('handed back', out)

    def test_a_refused_hand_back_keeps_the_lease_for_the_stop(self):
        def dies(engine):
            raise worker.EngineExited('gone', 1)

        _, released, held, _, out = self.run_slot(
            dies, release_answer=(None, 'HTTP 404'))

        self.assertEqual(len(released), 1)
        self.assertEqual([lease['task_id'] for lease in held.snapshot()], [9])
        self.assertIn('when its lease expires', out)

    def test_an_empty_result_is_never_sent(self):
        submitted, released, held, _, out = self.run_slot(lambda engine: [])

        self.assertEqual(submitted, [])
        self.assertEqual([lease['task_id'] for lease in released[0]], [9])
        self.assertEqual(held.snapshot(), [])
        self.assertIn('not sent', out)

    def test_a_mate_stop_sends_the_nodes_it_really_ran(self):
        def mates_early(engine):
            engine.last_nodes = 2_432
            return [{'move': 'e2e4', 'pv': ['e2e4'], 'eval_cp': 9_997,
                     'mate': 2, 'raw': 'info depth 3 seldepth 3 multipv 1 '
                     'score mate 2 nodes 2432 pv e2e4'}]

        submitted, released, held, _, _ = self.run_slot(mates_early)

        self.assertEqual(released, [])
        payload, = submitted
        self.assertEqual(payload['nodes'], 2_432)
        self.assertEqual(json.loads(payload['lines'])[0]['mate'], 2)
        self.assertEqual(held.snapshot(), [])


class DefaultJobsTests(SimpleTestCase):
    """-T without --jobs: one engine of one thread per thread lent."""

    def test_one_job_per_thread(self):
        for total in (1, 2, 4, 8, 16, 24):
            jobs, per_slot, _ = worker.plan_threads(
                total, worker.default_jobs(total, False), False)
            self.assertEqual((jobs, set(per_slot)), (total, {1}))

    def test_the_solver_keeps_its_core(self):
        jobs, per_slot, solver = worker.plan_threads(
            8, worker.default_jobs(8, True), True)
        self.assertEqual((jobs, per_slot, solver), (7, [1] * 7, 1))
        self.assertEqual(worker.default_jobs(1, True), 1)

    def test_a_big_machine_stops_at_the_server_cap(self):
        jobs, per_slot, _ = worker.plan_threads(
            64, worker.default_jobs(64, False), False)
        self.assertEqual(jobs, worker.AUTO_JOBS_MAX)
        self.assertEqual(sum(per_slot), 64)

    def test_the_cap_matches_the_server(self):
        from . import views
        self.assertEqual(worker.AUTO_JOBS_MAX, views.LEASES_PER_MACHINE)


class AutoHashTests(SimpleTestCase):
    """--hash not given: the free memory, shared out between the engines."""

    def test_a_roomy_machine_keeps_the_old_default(self):
        self.assertEqual(worker.auto_hash_mb(8, (65536, 48000)), 512)

    def test_the_hash_shrinks_with_the_free_memory(self):
        # 16 GB with 8 GB free, 16 engines: (8000 - 2048 - 16*128) / 16.
        self.assertEqual(worker.auto_hash_mb(16, (16384, 8000)), 128)

    def test_a_full_machine_still_gets_a_minimal_table(self):
        self.assertEqual(worker.auto_hash_mb(32, (8192, 1000)),
                         worker.AUTO_HASH_MIN_MB)

    def test_unknown_memory_is_sized_as_four_gigabytes(self):
        self.assertEqual(worker.auto_hash_mb(4, (None, None)), 512)
        self.assertEqual(worker.auto_hash_mb(16, (None, None)), 128)

    def test_sizes_are_powers_of_two(self):
        for engines in range(1, 40):
            size = worker.auto_hash_mb(engines, (32768, 20000))
            self.assertEqual(size & (size - 1), 0, size)

    def test_this_machine_can_be_measured_or_says_it_cannot(self):
        total, available = worker.memory_mb()
        if total is not None:
            self.assertGreater(total, 0)
            self.assertGreaterEqual(available, 0)


class StartupTests(SimpleTestCase):
    """What main() decides before the first lease."""

    def run_main(self, *flags, identity=None):
        argv = ['atomicdb_worker.py', '-U', 'user', '-P', 'pass', '-S',
                'https://server', '--engine', 'my-engine',
                '--no-auto-update', '--no-fetch-syzygy', *flags]
        with mock.patch.object(worker.sys, 'argv', argv), \
                mock.patch.object(worker, 'check_engine_identity',
                                  return_value=identity), \
                mock.patch.object(worker, '_file_sha256', return_value=''), \
                mock.patch.object(worker, '_net_sha256', return_value=''), \
                mock.patch.object(worker, 'memory_mb',
                                  return_value=(65536, 48000)), \
                mock.patch.object(worker, '_analysis_slot') as slot, \
                redirect_stdout(io.StringIO()) as out:
            worker.main()
        return slot, out.getvalue()

    def test_a_wrong_engine_stops_the_worker_before_any_lease(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main(identity='it calls itself "Stockfish 17"')
        self.assertEqual(caught.exception.code, 2)

    def test_t_without_jobs_runs_one_engine_per_thread(self):
        slot, out = self.run_main('-T', '6')
        self.assertEqual(slot.call_count, 6)
        self.assertEqual({call.args[5] for call in slot.call_args_list}, {1})
        self.assertIn('6 jobs of 1 threads', out)
        self.assertIn('hash 512 MB each', out)

    def test_explicit_jobs_and_hash_are_kept(self):
        slot, out = self.run_main('-T', '6', '--jobs', '1', '--hash', '64')
        self.assertEqual(slot.call_count, 1)
        self.assertEqual(slot.call_args.args[5], 6)
        self.assertEqual(slot.call_args.args[1].hash, 64)


class HashGuardTests(SimpleTestCase):
    """An explicit --hash that the free memory cannot hold (build 2026111102).

    The memory in these tests: 32 GB installed, 12 GB free, so the margin is
    4 GB and 8192 MB are usable.
    """

    MEMORY = (32768, 12288)

    def test_a_hash_that_fits_is_kept(self):
        self.assertEqual(worker.fit_hash(8, 512, self.MEMORY), (8, 512))

    def test_the_old_single_engine_hash_shrinks_per_engine(self):
        # -T 8 --hash 4096 from before 2026111101: 8 x 4396 MB is too much;
        # 8192 / 8 - 300 = 724 -> 512.
        self.assertEqual(worker.fit_hash(8, 4096, self.MEMORY), (8, 512))

    def test_jobs_come_down_when_not_even_the_minimum_fits(self):
        # 8192 / (16 + 300) = 25 engines at most.
        self.assertEqual(worker.fit_hash(32, 4096, self.MEMORY), (25, 16))
        self.assertEqual(worker.fit_hash(32, 4096, (8192, 100)), (1, 16))

    def test_unknown_memory_changes_nothing(self):
        self.assertEqual(worker.fit_hash(8, 4096, (None, None)), (8, 4096))

    def run_main(self, *flags, memory=MEMORY):
        argv = ['atomicdb_worker.py', '-U', 'user', '-P', 'pass', '-S',
                'https://server', '--engine', 'my-engine',
                '--no-auto-update', '--no-fetch-syzygy', *flags]
        with mock.patch.object(worker.sys, 'argv', argv), \
                mock.patch.object(worker, 'check_engine_identity',
                                  return_value=None), \
                mock.patch.object(worker, '_file_sha256', return_value=''), \
                mock.patch.object(worker, '_net_sha256', return_value=''), \
                mock.patch.object(worker, 'memory_mb', return_value=memory), \
                mock.patch.object(worker, '_analysis_slot') as slot, \
                redirect_stdout(io.StringIO()) as out:
            worker.main()
        return slot, out.getvalue()

    def test_main_shrinks_the_hash_and_says_why(self):
        slot, out = self.run_main('-T', '8', '--hash', '4096')
        self.assertEqual(slot.call_count, 8)
        self.assertEqual(slot.call_args.args[1].hash, 512)
        self.assertIn('--hash is per engine since build 2026111101', out)
        self.assertIn('8 engines x 4096 MB would need 34.3 GB', out)
        self.assertIn('using 512 MB each', out)
        self.assertIn('--jobs 1', out)
        self.assertEqual(out.count('warning:'), 1)

    def test_main_reduces_the_jobs_when_it_must(self):
        slot, out = self.run_main('-T', '32', '--hash', '4096')
        self.assertEqual(slot.call_count, 25)
        self.assertEqual(slot.call_args.args[1].hash, 16)
        self.assertEqual(sum(call.args[5] for call in slot.call_args_list),
                         32)
        self.assertIn('using 25 jobs of 16 MB each', out)

    def test_one_big_engine_that_fits_is_left_alone(self):
        slot, out = self.run_main('-T', '8', '--jobs', '1', '--hash', '4096')
        self.assertEqual(slot.call_count, 1)
        self.assertEqual(slot.call_args.args[1].hash, 4096)
        self.assertNotIn('warning:', out)

    def test_the_override_keeps_the_hash(self):
        slot, out = self.run_main('-T', '8', '--hash', '4096',
                                  '--allow-hash-overcommit')
        self.assertEqual(slot.call_count, 8)
        self.assertEqual(slot.call_args.args[1].hash, 4096)
        self.assertNotIn('warning:', out)
