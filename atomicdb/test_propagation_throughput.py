"""The drainer keeps up: deadlocks retry as a batch, pn/dn run beside it.

What is defended here (§ propagation_queue, TRANSIENT_RETRIES and
ProofPipeline):

1. a deadlock climbs the same rows again at once, as one batch, and the
   result is the one a clean batch writes; a deadlock that keeps coming
   back, or an error that is not transient, takes the old path (backoff,
   then one row at a time);
2. with a pipeline open, a batch hands its proof refresh over instead of
   running it, the calls handed over are the ones it would have run, and
   running them later ends in the same positions and proof nodes;
3. the pipeline runs refreshes in commit order, holds at most its depth,
   survives a failing refresh and waits for what is queued when it closes.
"""

import threading
import time
from unittest import mock

from django.db import OperationalError
from django.test import override_settings

from . import ingest, ingest_queue, logic, propagation_queue
from .database import atomic, current_alias
from .models import PropagationSeed
from .test_deferred_propagation import (_Rollback, _child, _expanded, _job_on,
                                        _land, _transposition)
from .test_proof_after_commit import _cycle, _state
from .testing import TestCase


class _Driver(Exception):
    """A driver error carrying a SQLSTATE the way psycopg2 does."""

    def __init__(self, code):
        super().__init__(f'sqlstate {code}')
        self.pgcode = code


def _deadlock():
    error = OperationalError('deadlock detected')
    error.__cause__ = _Driver('40P01')
    return error


class TransientTests(TestCase):

    def test_what_counts_as_transient(self):
        self.assertTrue(propagation_queue._is_transient(_deadlock()))
        serialization = OperationalError('could not serialize')
        serialization.__cause__ = _Driver('40001')
        self.assertTrue(propagation_queue._is_transient(serialization))

        class Psycopg3(Exception):
            sqlstate = '40P01'
        self.assertTrue(propagation_queue._is_transient(Psycopg3()))

        unique = OperationalError('duplicate key')
        unique.__cause__ = _Driver('23505')
        self.assertFalse(propagation_queue._is_transient(unique))
        self.assertFalse(propagation_queue._is_transient(RuntimeError('x')))


@override_settings(ATOMICDB_DEFER_PROPAGATION=True)
class DeadlockRetryTests(TestCase):
    """1. A deadlock is a race, not a bad row."""

    def setUp(self):
        self.root = _expanded(logic.start_fen())
        self.targets = []
        for uci in ('e2e3', 'd2d3'):
            target = _child(self.root, uci)
            ingest.expand(target)
            ingest_queue.apply_job(_job_on(target))
            self.targets.append(target.key)
        self.sleep = mock.patch.object(propagation_queue.time, 'sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def _climb_with(self, failures):
        """Drain once; the first ``failures`` climbs raise what they say."""
        calls = []
        real = ingest.propagate_landings

        def climb(landings):
            calls.append(sorted(landing.key for landing in landings))
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
            return real(landings)

        with mock.patch.object(ingest, 'propagate_landings', climb):
            done = propagation_queue.drain_batch()
        return done, calls

    def test_a_deadlock_climbs_the_same_rows_again_as_one_batch(self):
        done, calls = self._climb_with([_deadlock()])

        self.assertEqual(done, 2)
        self.assertEqual(calls, [sorted(self.targets)] * 2)
        self.assertFalse(PropagationSeed.objects.exists())
        self.root.refresh_from_db()
        self.assertIsNotNone(self.root.backed_eval)

    def test_the_retried_batch_writes_what_a_clean_batch_writes(self):
        def drained(failures):
            try:
                with atomic():
                    self._climb_with(failures)
                    state = _state()
                    raise _Rollback()
            except _Rollback:
                return state

        self.assertEqual(drained([]), drained([_deadlock(), _deadlock()]))

    def test_a_deadlock_that_keeps_coming_back_takes_the_old_path(self):
        failures = [_deadlock()] * (propagation_queue.TRANSIENT_RETRIES + 1)
        done, calls = self._climb_with(failures)

        self.assertEqual(done, -1)
        self.assertEqual(len(calls), propagation_queue.TRANSIENT_RETRIES + 1)
        self.assertEqual(
            sorted(PropagationSeed.objects.values_list('attempts', flat=True)),
            [1, 1])

    def test_an_error_that_is_not_transient_is_not_retried(self):
        done, calls = self._climb_with([RuntimeError('a bad row')])

        self.assertEqual(done, -1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            sorted(PropagationSeed.objects.values_list('attempts', flat=True)),
            [1, 1])


class _Collecting:
    def __init__(self):
        self.submitted = []

    def submit(self, calls):
        self.submitted.append(calls)


class HandOverTests(TestCase):
    """2. The batch hands its refresh over, and the refresh is the same."""

    def _run(self, build, pipeline):
        try:
            with atomic():
                for key, lines, budget, proofs in build():
                    _land(key, lines, budget, proofs, True)
                calls = []
                real = ingest.proof.refresh_proof_numbers

                def spy(seeds, *args, **kwargs):
                    calls.append(list(seeds))
                    return real(seeds, *args, **kwargs)

                with mock.patch.object(
                        propagation_queue, 'proof_after_commit_enabled',
                        return_value=True), \
                        mock.patch.object(ingest.proof,
                                          'refresh_proof_numbers', spy), \
                        mock.patch.dict(propagation_queue._pipeline,
                                        current=pipeline):
                    with self.captureOnCommitCallbacks(
                            using=current_alias(), execute=True):
                        propagation_queue.drain()
                    inline = list(calls)
                    if pipeline is not None:
                        # The refresh thread's turn, after the rechecks.
                        for submitted in pipeline.submitted:
                            propagation_queue._refresh_proof_after_commit(
                                submitted)
                state = _state()
                raise _Rollback()
        except _Rollback:
            return state, inline, calls

    def _assert_same(self, build):
        plain, plain_inline, _ = self._run(build, None)
        pipeline = _Collecting()
        piped, piped_inline, piped_all = self._run(build, pipeline)

        self.assertTrue(pipeline.submitted)
        handed = [seeds for submitted in pipeline.submitted
                  for seeds in submitted]
        # What the drainer's own thread no longer refreshes is exactly what
        # it handed over, seed for seed.
        self.assertEqual(piped_inline + handed, piped_all)
        self.assertEqual(sorted(map(tuple, plain_inline)),
                         sorted(map(tuple, piped_all)))
        self.assertEqual(plain[0], piped[0])
        self.assertTrue(plain[1], 'the fixture must write proof nodes')
        self.assertEqual(plain[1], piped[1])
        self.assertEqual(plain[2], piped[2])

    def test_a_transposition(self):
        self._assert_same(_transposition)

    def test_a_repetition_cycle(self):
        self._assert_same(_cycle)


class PipelineTests(TestCase):
    """3. Order, depth, failures and closing."""

    def test_refreshes_run_in_commit_order_and_close_waits(self):
        ran = []

        def refresh(calls):
            time.sleep(0.01)
            ran.append(calls)

        pipeline = propagation_queue.ProofPipeline(refresh=refresh)
        for n in range(4):
            pipeline.submit([n])
        pipeline.close()

        self.assertEqual(ran, [[0], [1], [2], [3]])
        self.assertFalse(pipeline._thread.is_alive())

    def test_a_drainer_too_far_ahead_waits(self):
        release = threading.Event()
        started = threading.Event()

        def refresh(calls):
            started.set()
            release.wait(5)

        pipeline = propagation_queue.ProofPipeline(depth=1, refresh=refresh)
        pipeline.submit(['running'])
        self.assertTrue(started.wait(5))
        pipeline.submit(['queued'])          # fills the queue
        third = threading.Thread(target=pipeline.submit, args=(['waits'],))
        third.start()
        third.join(0.2)
        self.assertTrue(third.is_alive(), 'a full queue must hold the drainer')
        release.set()
        third.join(5)
        self.assertFalse(third.is_alive())
        pipeline.close()

    def test_a_failing_refresh_does_not_stop_the_pipeline(self):
        ran = []

        def refresh(calls):
            if calls == ['boom']:
                raise RuntimeError('boom')
            ran.append(calls)

        pipeline = propagation_queue.ProofPipeline(refresh=refresh)
        pipeline.submit(['boom'])
        pipeline.submit(['after'])
        pipeline.close()
        self.assertEqual(ran, [['after']])

    def test_the_setting_keeps_the_refresh_inline(self):
        with self.settings(ATOMICDB_PROOF_PIPELINE=False):
            with propagation_queue.proof_pipeline() as pipeline:
                self.assertIsNone(pipeline)
                self.assertIsNone(propagation_queue._pipeline['current'])

    def test_an_open_pipeline_is_used_and_closed(self):
        with propagation_queue.proof_pipeline() as pipeline:
            self.assertIs(propagation_queue._pipeline['current'], pipeline)
            # A nested block reuses it instead of opening a second thread.
            with propagation_queue.proof_pipeline() as nested:
                self.assertIs(nested, pipeline)
        self.assertIsNone(propagation_queue._pipeline['current'])
        self.assertFalse(pipeline._thread.is_alive())
