"""The worker keeps the lease of a long search, and drops one it lost.

Worker build 2026112101:

  * the heartbeat names the task while its engine makes progress, and the
    grace for a silent engine grows with the search: twice the age the search
    had at its last ``nodes`` counter, never under five minutes.  Late in a 2B
    search on a slow slot one iteration takes longer than the old fixed five
    minutes, the heartbeat stopped naming the task, the server requeued it
    after LEASE_MINUTES and the search ended in a 409 at submit;
  * a heartbeat answered 409 ``stale-lease``/``not-your-lease`` stops that
    slot's search and the slot sends nothing; the other slots go on.

Worker build 2026112501: with a known budget and speed the grace above no
longer applies.  The lease is renewed while the engine process runs and the
search is younger than twice its expected length plus ten minutes
(``ExpectedLengthRenewalTests``).
"""

import io
import threading
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from Client import atomicdb_worker as worker

START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
MINUTE = 60.0


def _state(now):
    return worker.CurrentTaskState(clock=lambda: now[0])


class ScaledGraceTests(SimpleTestCase):
    """``CurrentTaskState.snapshot`` with a grace that grows with the search."""

    def test_a_30_minute_old_search_survives_a_9_minute_iteration(self):
        now = [0.0]
        state = _state(now)
        state.start(41, 'tok-41')
        now[0] = 21 * MINUTE                     # last iteration ended here
        state.progress(1_500_000_000, now[0])
        now[0] = 30 * MINUTE                     # 9 minutes of silence

        snapshot = state.snapshot()

        self.assertEqual(state.grace(), 42 * MINUTE)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['id'], 41)
        self.assertEqual(snapshot['lease_token'], 'tok-41')

    def test_a_2_minute_old_search_keeps_the_5_minute_floor(self):
        now = [0.0]
        state = _state(now)
        state.start(42, 'tok-42')
        now[0] = 2 * MINUTE
        state.progress(100_000_000, now[0])

        self.assertEqual(state.grace(),
                         worker.HEARTBEAT_PROGRESS_GRACE_SECONDS)
        self.assertEqual(state.grace(), 5 * MINUTE)
        now[0] = 2 * MINUTE + 5 * MINUTE - 1
        self.assertIsNotNone(state.snapshot())
        now[0] = 2 * MINUTE + 5 * MINUTE + 1
        self.assertIsNone(state.snapshot())

    def test_a_new_search_gets_the_floor_before_its_first_counter(self):
        now = [1000.0]
        state = _state(now)
        state.start(43, 'tok-43')
        now[0] += 5 * MINUTE - 1
        self.assertIsNotNone(state.snapshot())
        now[0] += 2
        self.assertIsNone(state.snapshot())

    def test_a_frozen_engine_is_still_dropped_past_the_scaled_grace(self):
        now = [0.0]
        state = _state(now)
        state.start(44, 'tok-44')
        now[0] = 10 * MINUTE
        state.progress(800_000_000, now[0])     # grace becomes 20 minutes

        now[0] = 10 * MINUTE + 20 * MINUTE - 1
        self.assertIsNotNone(state.snapshot())
        now[0] = 10 * MINUTE + 20 * MINUTE + 1
        self.assertIsNone(state.snapshot())
        # The silence only grows; the grace does not grow with the clock.
        now[0] = 10 * 60 * MINUTE
        self.assertIsNone(state.snapshot())

    def test_the_grace_is_measured_from_the_task_not_the_previous_one(self):
        now = [0.0]
        state = _state(now)
        state.start(45, 'tok-45')
        now[0] = 40 * MINUTE
        state.progress(2_000_000_000, now[0])
        state.clear()
        state.start(46, 'tok-46')
        self.assertEqual(state.grace(), 5 * MINUTE)
        now[0] += 5 * MINUTE + 1
        self.assertIsNone(state.snapshot())


class ExpectedLengthRenewalTests(SimpleTestCase):
    """Worker build 2026112501: a search with a known budget and speed keeps
    its lease while its engine process runs and it is younger than twice its
    expected length plus ten minutes, however long it goes without a node
    counter.  Task 5275448 (2G nodes) was leased 217 times without a result:
    each deep iteration went quiet past the grace and the lease lapsed."""

    def search(self, now, alive=lambda: True, budget=2_000_000_000):
        state = _state(now)
        state.start(5275448, 'tok', budget, alive=alive)
        return state

    def test_a_2g_search_keeps_its_lease_through_a_20_minute_iteration(self):
        now = [0.0]
        state = self.search(now)
        now[0] = 8 * MINUTE
        state.progress(480_000_000, now[0])     # 1M nodes per second
        now[0] = 28 * MINUTE                    # 20 minutes of silence

        self.assertLess(state.grace(), 20 * MINUTE)  # the old rule dropped it
        self.assertEqual(state.renewal_limit(), 2 * 2000 + 10 * MINUTE)
        snapshot = state.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['id'], 5275448)

    def test_a_dead_engine_process_stops_the_renewal_at_once(self):
        now = [0.0]
        running = [True]
        state = self.search(now, alive=lambda: running[0])
        now[0] = MINUTE
        state.progress(60_000_000, now[0])
        self.assertIsNotNone(state.snapshot())

        running[0] = False                       # the engine died
        self.assertIsNone(state.snapshot())

    def test_a_probe_that_fails_counts_as_a_dead_engine(self):
        def probe():
            raise OSError('gone')

        state = self.search([0.0], alive=probe)
        self.assertIsNone(state.snapshot())

    def test_a_search_far_past_twice_its_expected_length_is_dropped(self):
        now = [0.0]
        state = self.search(now)
        now[0] = MINUTE
        state.progress(60_000_000, now[0])      # 2000 s expected
        limit = 2 * 2000 + 10 * MINUTE

        now[0] = limit - 1
        self.assertIsNotNone(state.snapshot())  # a hung engine, still running
        now[0] = limit + 1
        self.assertIsNone(state.snapshot())
        now[0] = 10 * limit
        self.assertIsNone(state.snapshot())

    def test_before_its_first_counter_a_search_uses_the_known_speed(self):
        now = [0.0]
        state = self.search(now)
        now[0] = MINUTE
        state.progress(60_000_000, now[0])
        state.clear()
        state.start(2, 'tok-2', 2_000_000_000, alive=lambda: True)
        now[0] += 6 * MINUTE                     # past the old 5-minute floor

        self.assertIsNotNone(state.snapshot())
        self.assertEqual(state.renewal_limit(), 2 * 2000 + 10 * MINUTE)

    def test_without_a_known_speed_the_old_grace_applies(self):
        now = [0.0]
        state = self.search(now)
        self.assertIsNone(state.renewal_limit())
        now[0] = 5 * MINUTE + 1
        self.assertIsNone(state.snapshot())

    def test_engine_alive_asks_the_process(self):
        self.assertTrue(worker._engine_alive(
            SimpleNamespace(p=SimpleNamespace(poll=lambda: None))))
        self.assertFalse(worker._engine_alive(
            SimpleNamespace(p=SimpleNamespace(poll=lambda: -9))))
        self.assertTrue(worker._engine_alive(SimpleNamespace()))

    def test_the_slot_gives_its_engine_and_budget_to_the_heartbeat(self):
        started = {}
        real_start = worker.CurrentTaskState.start

        def start(state, task_id, lease_token='', budget_nodes=0,
                  alive=None):
            started.update(budget=budget_nodes, alive=alive())
            return real_start(state, task_id, lease_token, budget_nodes,
                              alive)

        stop = threading.Event()

        class Engine:
            p = SimpleNamespace(poll=lambda: None)

            def __init__(self, *args, **kwargs):
                self.ready = True

            def analyse(self, *args, **kwargs):
                stop.set()
                return []

            def close(self):
                pass

        tasks = [[{'id': 9, 'fen': START_FEN, 'budget_nodes': 2_000_000_000,
                   'multipv': 1, 'lease_token': 'tok-9'}]]
        a = SimpleNamespace(engine='engine', hash=16, syzygy='',
                            S='https://server', no_auto_update=True,
                            once=False)
        with mock.patch.object(worker.CurrentTaskState, 'start', start), \
                mock.patch.object(worker, 'Engine', Engine), \
                mock.patch.object(worker, '_request_tasks',
                                  lambda *args: tasks.pop(0) if tasks
                                  else stop.set() or []), \
                mock.patch.object(worker, '_heartbeat_loop',
                                  lambda *args: None), \
                redirect_stdout(io.StringIO()):
            worker._analysis_slot(0, a, None, {'machine': 'box#0'}, {}, 1,
                                  stop, False)

        self.assertEqual(started, {'budget': 2_000_000_000, 'alive': True})


class _Answer:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.raised = False

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            self.raised = True
            raise worker.requests.HTTPError(str(self.status_code))


class _OneBeat:
    """A stop event that lets ``_heartbeat_loop`` beat exactly once."""

    def __init__(self):
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > 1


class LostLeaseHeartbeatTests(SimpleTestCase):
    """A 409 lost-lease answer stops only the slot whose beat got it."""

    def setUp(self):
        self.session = mock.Mock()
        patcher = mock.patch.object(worker, '_http_session',
                                    return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def beat(self, state, on_lost, answer, machine='box-atomicdb#0'):
        self.session.post.return_value = answer
        with redirect_stdout(io.StringIO()) as out:
            worker._heartbeat_loop('https://server', {'machine': machine},
                                   state, _OneBeat(), on_lost)
        return out.getvalue()

    def test_stale_lease_stops_that_slot_only(self):
        now = [0.0]
        slot0, slot1 = _state(now), _state(now)
        slot0.start(51, 'tok-51')
        slot1.start(52, 'tok-52')
        stopped = []

        out = self.beat(slot0, lambda: stopped.append(0),
                        _Answer(409, {'error': 'stale-lease'}))

        self.assertEqual(stopped, [0])
        self.assertTrue(slot0.lost(51))
        self.assertIn('stale-lease', out)
        self.assertNotIn('heartbeat skipped', out)
        self.assertFalse(slot1.lost(52))
        self.assertIsNotNone(slot1.snapshot())
        # The lost task is not named again.
        self.assertIsNone(slot0.snapshot())

    def test_not_your_lease_stops_the_search_too(self):
        state = _state([0.0])
        state.start(53, 'tok-53')
        stopped = []

        self.beat(state, lambda: stopped.append(53),
                  _Answer(409, {'error': 'not-your-lease'}))

        self.assertEqual(stopped, [53])
        self.assertTrue(state.lost(53))

    def test_another_409_or_an_error_page_stops_nothing(self):
        for answer in (_Answer(409, {'error': 'something-else'}),
                       _Answer(409, ValueError('not json')),
                       _Answer(500, {'error': 'stale-lease'})):
            with self.subTest(status=answer.status_code):
                state = _state([0.0])
                state.start(54, 'tok-54')
                stopped = []
                if isinstance(answer._body, Exception):
                    answer.json = mock.Mock(side_effect=answer._body)

                out = self.beat(state, lambda: stopped.append(54), answer)

                self.assertEqual(stopped, [])
                self.assertFalse(state.lost(54))
                self.assertIn('heartbeat skipped', out)

    def test_a_beat_without_a_task_never_marks_anything(self):
        state = _state([0.0])
        stopped = []

        self.beat(state, lambda: stopped.append(1),
                  _Answer(409, {'error': 'stale-lease'}))

        self.assertEqual(stopped, [])

    def test_an_answer_for_a_task_the_slot_already_left_changes_nothing(self):
        state = _state([0.0])
        state.start(55, 'tok-55')
        stopped = []
        self.assertFalse(state.mark_lost(54, 'tok-54',
                                         lambda: stopped.append(54)))
        self.assertFalse(state.mark_lost(55, 'other-token',
                                         lambda: stopped.append(55)))
        self.assertEqual(stopped, [])
        self.assertFalse(state.lost(55))
        # A second answer for the same lost lease stops it only once.
        self.assertTrue(state.mark_lost(55, 'tok-55',
                                        lambda: stopped.append(55)))
        self.assertFalse(state.mark_lost(55, 'tok-55',
                                         lambda: stopped.append(55)))
        self.assertEqual(stopped, [55])
        # The next task starts clean.
        state.start(56, 'tok-56')
        self.assertFalse(state.lost(56))


class _Process:
    """Engine Popen stand-in; ``on_read`` runs before each line is read."""

    def __init__(self, lines, on_read=None):
        self.stdin = io.StringIO()
        self._lines = list(lines)
        self._on_read = on_read
        self.stdout = self

    def readline(self):
        if self._on_read is not None:
            self._on_read(self)
        return self._lines.pop(0) if self._lines else ''

    def wait(self, timeout=None):
        return 0

    def sent(self):
        return [line for line in self.stdin.getvalue().splitlines() if line]


def _info(depth, nodes, move):
    return (f'info depth {depth} seldepth {depth} multipv 1 score cp 10 '
            f'nodes {nodes} nps 1000000 time 10 pv {move}\n')


def _engine(process):
    engine = worker.Engine.__new__(worker.Engine)
    engine._io_lock = threading.RLock()
    engine._stop_requested = False
    engine._searching = False
    engine.p = process
    engine.last_nodes = 0
    return engine


class EngineStopTests(SimpleTestCase):
    """``Engine.stop_search`` from the heartbeat thread."""

    def test_stop_during_the_search_sends_stop_after_go(self):
        engine = None

        def stop_once_searching(process):
            if engine._searching and 'stop' not in process.sent():
                engine.stop_search()

        process = _Process(['readyok\n', _info(1, 1000, 'e2e4'),
                            'bestmove e2e4\n'], on_read=stop_once_searching)
        engine = _engine(process)

        lines = engine.analyse(START_FEN, 2_000_000_000, 1)

        sent = process.sent()
        self.assertEqual(sent[-2:], ['go nodes 2000000000 mate 250', 'stop'])
        self.assertEqual([line['move'] for line in lines], ['e2e4'])
        self.assertFalse(engine._searching)

    def test_stop_before_go_never_starts_the_search(self):
        process = _Process(['readyok\n', 'bestmove e2e4\n'])
        engine = _engine(process)
        engine.stop_search()

        self.assertEqual(engine.analyse(START_FEN, 1000, 1), [])
        self.assertFalse(any(line.startswith('go') for line in process.sent()))
        self.assertNotIn('stop', process.sent())

    def test_reset_stop_forgets_a_stop_for_the_previous_task(self):
        process = _Process(['readyok\n', _info(1, 1000, 'e2e4'),
                            'bestmove e2e4\n'])
        engine = _engine(process)
        engine.stop_search()
        engine.reset_stop()

        lines = engine.analyse(START_FEN, 1000, 1)

        self.assertEqual([line['move'] for line in lines], ['e2e4'])
        self.assertIn('go nodes 1000 mate 250', process.sent())


class _SlotEngine:
    """Scripted engine for ``_analysis_slot``; the class keeps the record."""

    behaviour = None
    stops = 0
    resets = 0

    def __init__(self, path, threads=1, hash_mb=256, syzygy=''):
        self.ready = True
        self.last_nodes = 0

    def analyse(self, fen, nodes, multipv, searchmoves=None, progress=None):
        return type(self).behaviour(self)

    def stop_search(self):
        type(self).stops += 1

    def reset_stop(self):
        type(self).resets += 1

    def close(self):
        pass


class SlotLostLeaseTests(SimpleTestCase):
    """``_analysis_slot`` when the heartbeat loses the lease mid-search."""

    def test_a_lost_lease_stops_the_search_and_skips_the_submit(self):
        stop = threading.Event()
        held = worker.HeldLeases()
        submitted, released, beats = [], [], {}
        script = [
            [{'id': 61, 'fen': START_FEN, 'budget_nodes': 2_000_000_000,
              'multipv': 1, 'lease_token': 'tok-61'}],
            [{'id': 62, 'fen': START_FEN, 'budget_nodes': 128_000_000,
              'multipv': 1, 'lease_token': 'tok-62'}],
        ]
        line = {'move': 'e2e4', 'pv': ['e2e4'], 'eval_cp': 10, 'mate': None,
                'raw': 'info depth 30 multipv 1 score cp 10 nodes 1000 pv e2e4'}

        def heartbeat(server, auth, current_task, stop_event,
                      on_lease_lost=None):
            beats['state'] = current_task
            beats['on_lost'] = on_lease_lost

        def search(engine):
            snap = beats['state'].snapshot()
            if snap['id'] == 61:
                # What the heartbeat thread does on a 409 stale-lease.
                self.assertTrue(beats['state'].mark_lost(
                    61, 'tok-61', beats['on_lost']))
            return [dict(line)]

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
            return (1, '')

        engine = type('Engine', (_SlotEngine,), {
            'behaviour': staticmethod(search), 'stops': 0, 'resets': 0})
        a = SimpleNamespace(engine='engine', hash=16, syzygy='',
                            S='https://server', no_auto_update=True,
                            once=False)
        with mock.patch.object(worker, 'Engine', engine), \
                mock.patch.object(worker, '_request_tasks', request_tasks), \
                mock.patch.object(worker, '_submit_until_definitive', submit), \
                mock.patch.object(worker, '_release_leases', release), \
                mock.patch.object(worker, '_heartbeat_loop', heartbeat), \
                redirect_stdout(io.StringIO()) as out:
            worker._analysis_slot(0, a, None, {'machine': 'box-atomicdb'},
                                  {}, 1, stop, False, held=held)

        self.assertEqual(engine.stops, 1)
        self.assertEqual(engine.resets, 2)
        self.assertEqual([p['task_id'] for p in submitted], [62])
        self.assertEqual(released, [], 'a lost lease is not ours to hand back')
        self.assertEqual(held.snapshot(), [])
        self.assertIn('task 61: lease lost', out.getvalue())
