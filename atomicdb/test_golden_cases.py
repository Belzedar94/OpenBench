"""Golden cases: v1's climbs and landings recorded for v2 to replay.

What is defended here:

1. off by default, and an unsampled batch pays only a random draw;
2. a sampled stage (a) job and a sampled climb each leave a case with their
   inputs, every statement in order, the rows read (on PostgreSQL, with the
   table of every column) and the rows written at each commit;
3. the caller reads exactly the rows it would have read unrecorded;
4. a failing recorder never fails the batch; a failing batch is recorded
   with its error and the error still reaches the caller;
5. the control file, the disk budget and empty polls are respected.
"""

import glob
import json
import os
import tempfile
import time
from unittest import mock

from django.db import connections
from django.test import SimpleTestCase, override_settings

from . import golden_cases, ingest, ingest_queue, logic, propagation_queue
from .database import current_alias
from .models import Position
from .test_deferred_propagation import _child, _expanded, _job_on
from .testing import TestCase, TransactionTestCase


def _postgres():
    return connections[current_alias()].vendor == 'postgresql'


class _GoldenDir:
    """Golden cases on, at rate 1, into a temporary directory."""

    env = {}

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        env = {golden_cases.ENV_ENABLE: '1', golden_cases.ENV_DIR: self.dir,
               golden_cases.ENV_RATE: '1', golden_cases.ENV_MIN_FREE_GB: '0',
               **self.env}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        golden_cases.reset_for_tests()
        self.addCleanup(golden_cases.reset_for_tests)

    def cases(self, kind=None):
        golden_cases.flush()
        out = []
        for path in sorted(glob.glob(os.path.join(self.dir, '*', '*.jsonl*'))):
            out.extend(golden_cases.iter_cases(path))
        return [c for c in out if kind is None or c['kind'] == kind]


def _tree():
    root = _expanded(logic.start_fen())
    target = _child(root, 'e2e3')
    ingest.expand(target)
    return root, target


@override_settings(ATOMICDB_DEFER_PROPAGATION=True)
class OffByDefaultTests(TestCase):

    def test_nothing_is_recorded_without_the_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {golden_cases.ENV_ENABLE: '',
                                              golden_cases.ENV_DIR: tmp}):
                golden_cases.reset_for_tests()
                try:
                    root, target = _tree()
                    ingest_queue.process_job(_job_on(target))
                    propagation_queue.drain()
                    with golden_cases.recording('drain') as case:
                        self.assertIsNone(case)
                finally:
                    golden_cases.reset_for_tests()
                self.assertEqual(os.listdir(tmp), [])

    def test_an_unsampled_batch_costs_a_random_draw(self):
        with mock.patch.dict(os.environ, {golden_cases.ENV_ENABLE: '1',
                                          golden_cases.ENV_RATE: '0'}):
            golden_cases.reset_for_tests()
            try:
                started = time.perf_counter()
                for _ in range(20_000):
                    with golden_cases.recording('drain') as case:
                        pass
                per_call = (time.perf_counter() - started) / 20_000
            finally:
                golden_cases.reset_for_tests()
        self.assertIsNone(case)
        self.assertLess(per_call, 50e-6)


@override_settings(ATOMICDB_DEFER_PROPAGATION=True)
class RecordedCasesTests(_GoldenDir, TestCase):

    def setUp(self):
        super().setUp()
        self.root, self.target = _tree()

    def test_a_job_and_its_climb_are_recorded(self):
        job = _job_on(self.target)
        summary, error = ingest_queue.process_job(job)
        self.assertIsNone(error)
        self.assertEqual(propagation_queue.drain()['settled'], 1)

        (landing,) = self.cases('stage_a')
        self.assertEqual(landing['inputs']['job'], job.pk)
        self.assertEqual(landing['inputs']['payload']['nodes'], 1_000_000)
        self.assertIn('mate_proofs', landing['notes'])
        self.assertEqual(landing['result']['propagation'], 'deferred')
        self.assertIsNone(landing.get('error'))
        self.assertTrue(any('atomicdb_ingestjob' in q['sql']
                            for q in landing['queries']))
        self.assertTrue(landing['code']['ingest'])
        self.assertIn('BATCH_SIZE', landing['constants']['propagation_queue'])
        self.assertGreaterEqual(landing['overhead_ms']['total'], 0)

        (climb,) = self.cases('drain')
        self.assertEqual(climb['result'], 1)
        written = {}
        for commit in climb['commits']:
            written.update(commit.get('positions') or {})
        if _postgres():
            self.assertEqual([row['position_id']
                              for row in climb['seed_rows']],
                             [self.target.key])
            # JSON columns are kept as the text Django receives and decodes.
            payload = climb['seed_rows'][0]['payload']
            if isinstance(payload, str):
                payload = json.loads(payload)
            self.assertEqual(payload['job'], job.pk)
            # The root's backed move is a row the climb wrote.
            self.assertEqual(written[self.root.key]['backed_move'], 'e2e3')
            pre = golden_cases.pre_image(climb)
            self.assertIn(self.target.key, pre['atomicdb_position'])
            self.assertIsNone(
                pre['atomicdb_position'][self.root.key].get('backed_move'))
            self.assertGreater(climb['stats']['rows_read'], 0)
        else:
            self.assertIn(self.root.key, climb['commits'][-1]['keys'])

    def test_the_caller_reads_the_same_rows(self):
        expected = list(Position.objects.order_by('key')
                        .values_list('key', 'eval_cp', 'expanded'))
        with golden_cases.recording('drain') as case:
            self.assertIsNotNone(case)
            seen = list(Position.objects.order_by('key')
                        .values_list('key', 'eval_cp', 'expanded'))
            first = Position.objects.order_by('key').first()
            count = Position.objects.count()
        self.assertEqual(seen, expected)
        self.assertEqual(first.key, expected[0][0])
        self.assertEqual(count, len(expected))
        (recorded,) = self.cases('drain')
        selects = [q for q in recorded['queries'] if q.get('cols')]
        self.assertEqual(len(selects), 3)
        if _postgres():
            self.assertEqual(len(selects[0]['rows']), len(expected))
            self.assertEqual(selects[0]['tables'],
                             ['atomicdb_position'] * 3)

    def test_an_empty_poll_is_not_kept(self):
        self.assertEqual(propagation_queue.drain()['batches'], 0)
        self.assertEqual(self.cases('drain'), [])

    def test_a_failing_recorder_never_fails_the_batch(self):
        job = _job_on(self.target)
        with mock.patch.object(golden_cases.Recorder, '_capture',
                               side_effect=RuntimeError('recorder bug')):
            summary, error = ingest_queue.process_job(job)
        self.assertIsNone(error)
        self.assertEqual(summary['propagation'], 'deferred')
        (landing,) = self.cases('stage_a')
        self.assertGreater(landing['stats']['capture_errors'], 0)

    def test_a_failing_batch_is_recorded_with_its_error(self):
        ingest_queue.process_job(_job_on(self.target))
        boom = mock.patch.object(ingest, 'propagate_landings',
                                 side_effect=RuntimeError('killed mid-climb'))
        with boom:
            self.assertEqual(propagation_queue.drain_batch(), -1)
            with self.assertRaises(RuntimeError):
                with golden_cases.recording('drain'):
                    raise RuntimeError('raised in the block')
        errors = [c.get('error') for c in self.cases('drain')]
        self.assertIn('RuntimeError: raised in the block', errors)

    def test_the_control_file_turns_it_off(self):
        with open(os.path.join(self.dir, 'control.json'), 'w') as handle:
            json.dump({'enabled': True, 'rate': {'drain': 0.0}}, handle)
        golden_cases.reset_for_tests()
        with golden_cases.recording('drain') as case:
            self.assertIsNone(case)
        with golden_cases.recording('stage_a') as case:
            self.assertIsNotNone(case)
        with open(os.path.join(self.dir, 'control.json'), 'w') as handle:
            json.dump({'enabled': False}, handle)
        golden_cases.reset_for_tests()
        with golden_cases.recording('stage_a') as case:
            self.assertIsNone(case)


class BudgetTests(_GoldenDir, TestCase):

    env = {golden_cases.ENV_MIN_FREE_GB: '1000000000'}

    def test_no_case_below_the_free_disk_floor(self):
        with golden_cases.recording('drain') as case:
            self.assertIsNone(case)
        writer = golden_cases._writer['current']
        self.assertIn('disk free', writer.paused)
        self.assertEqual(writer.counters['dropped_budget'], 1)


class CapTests(_GoldenDir, TestCase):

    env = {golden_cases.ENV_MAX_GB: '0.000001'}     # 1 kB

    def test_the_cap_stops_writing(self):
        with open(os.path.join(self.dir, 'filler.bin'), 'wb') as handle:
            handle.write(b'x' * 4096)
        with golden_cases.recording('drain') as case:
            self.assertIsNone(case)
        self.assertIn('cap', golden_cases._writer['current'].paused)


@override_settings(ATOMICDB_DEFER_PROPAGATION=True)
class CommitHookTests(_GoldenDir, TransactionTestCase):
    """With real commits, each COMMIT carries the rows its transaction
    wrote, read inside it."""

    def test_rows_written_are_read_at_each_commit(self):
        root, target = _tree()
        job = _job_on(target)
        ingest_queue.process_job(job)
        propagation_queue.drain()
        (climb,) = self.cases('drain')
        commits = [c for c in climb['commits'] if c['at'] == 'commit']
        self.assertTrue(commits)
        if _postgres():
            written = {}
            for commit in commits:
                written.update(commit.get('positions') or {})
            self.assertEqual(written[root.key]['backed_move'], 'e2e3')
        (landing,) = self.cases('stage_a')
        if _postgres():
            written = {}
            for commit in landing['commits']:
                written.update(commit.get('positions') or {})
            self.assertEqual(written[target.key]['visits'], 1)


class ValuesTests(SimpleTestCase):

    def test_jsonable(self):
        import datetime
        import decimal
        when = datetime.datetime(2026, 11, 28, 7, 0,
                                 tzinfo=datetime.timezone.utc)
        self.assertEqual(golden_cases.jsonable(
            {'a': (1, 2), 'b': when, 'c': decimal.Decimal('1.5'),
             'd': b'\x01\xff', 'e': float('nan'), 'f': {3, 1}}),
            {'a': [1, 2], 'b': '2026-11-28T07:00:00+00:00', 'c': '1.5',
             'd': {'$hex': '01ff'}, 'e': 'nan', 'f': [1, 3]})

    def test_keys_in_parameters(self):
        key = 'a' * 64
        found = set()
        golden_cases._keys_in(['x', [key, 3], {'k': 'b' * 63}], found)
        self.assertEqual(found, {key})

    def test_pre_image_keeps_the_first_value(self):
        case = {'queries': [
            {'cols': ['key', 'eval_cp'], 'tables': ['atomicdb_position'] * 2,
             'rows': [['k1', 10]]},
            {'cols': ['key', 'eval_cp'], 'tables': ['atomicdb_position'] * 2,
             'rows': [['k1', 20], ['k2', 5]]},
        ]}
        self.assertEqual(golden_cases.pre_image(case),
                         {'atomicdb_position': {'k1': {'key': 'k1',
                                                       'eval_cp': 10},
                                                'k2': {'key': 'k2',
                                                       'eval_cp': 5}}})
