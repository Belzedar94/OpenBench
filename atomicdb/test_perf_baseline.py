"""The v1 baseline measurements: perf events and the minute sampler.

What is defended here:

1. ``perf_events`` writes nothing unless ``ATOMICDB_PERF_DIR`` names a
   directory, and then one JSON line per event;
2. an applied job and a climbed batch each leave their line, the batch with
   the jobs it made visible and its commit time;
3. the sampler's parsing (nginx timing lines, route classes, rotation and
   half-written lines, percentiles, CPU deltas) and one whole sample taken
   from files alone.
"""

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import ingest, ingest_queue, logic, perf_events, propagation_queue
from .test_deferred_propagation import _child, _expanded, _job_on
from .testing import TestCase


def _load_sampler():
    path = Path(settings.BASE_DIR) / 'Scripts' / 'atomicdb_perf_sample.py'
    spec = importlib.util.spec_from_file_location('atomicdb_perf_sample', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sampler = _load_sampler()


class _PerfDir:
    """A temporary ATOMICDB_PERF_DIR for one test."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        # Cleanups run last-in first-out: the file is closed before the
        # directory goes (Windows refuses to remove an open file).
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {perf_events.ENV_DIR: self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        perf_events._reset_for_tests()
        self.addCleanup(perf_events._reset_for_tests)

    def events(self):
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.startswith('events-'):
                with open(os.path.join(self.dir, name)) as handle:
                    out.extend(json.loads(line) for line in handle)
        return out


class PerfEventsTests(_PerfDir, SimpleTestCase):

    def test_nothing_is_written_without_the_directory(self):
        with mock.patch.dict(os.environ, {perf_events.ENV_DIR: ''}):
            perf_events._reset_for_tests()
            perf_events.event('job', ms=1.0)
            with perf_events.timed('drain'):
                pass
        self.assertEqual(os.listdir(self.dir), [])

    def test_one_line_per_event(self):
        perf_events.event('job', job=7, ms=12.5)
        with perf_events.timed('drain', limit=32) as timer:
            timer.set(settled=3)
        events = self.events()
        self.assertEqual([e['kind'] for e in events], ['job', 'drain'])
        self.assertEqual(events[0]['job'], 7)
        self.assertEqual((events[1]['limit'], events[1]['settled']), (32, 3))
        self.assertGreaterEqual(events[1]['ms'], 0)

    def test_an_error_is_recorded_and_raised(self):
        with self.assertRaises(ValueError):
            with perf_events.timed('drain'):
                raise ValueError('boom')
        self.assertEqual(self.events()[0]['error'], 'ValueError')

    def test_a_failing_write_never_fails_the_caller(self):
        with mock.patch.object(perf_events.os, 'write',
                               side_effect=OSError('disk full')):
            perf_events.event('job', ms=1.0)


@override_settings(ATOMICDB_DEFER_PROPAGATION=True)
class PerfEventsOfTheIngestTests(_PerfDir, TestCase):

    def setUp(self):
        super().setUp()
        self.root = _expanded(logic.start_fen())
        self.target = _child(self.root, 'e2e3')
        ingest.expand(self.target)

    def test_a_job_and_its_climb_leave_their_lines(self):
        job = _job_on(self.target)
        summary, error = ingest_queue.process_job(job)
        self.assertIsNone(error)
        self.assertEqual(summary['propagation'], 'deferred')
        before = time.time()
        self.assertEqual(propagation_queue.drain()['settled'], 1)

        events = self.events()
        jobs = [e for e in events if e['kind'] == 'job']
        drains = [e for e in events if e['kind'] == 'drain']
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['job'], jobs[0]['deferred']), (job.pk, True))
        self.assertEqual(len(drains), 1)
        self.assertEqual(drains[0]['jobs'], [job.pk])
        self.assertEqual(drains[0]['settled'], 1)
        self.assertGreaterEqual(drains[0]['commit_t'], before - 1)
        self.assertLessEqual(drains[0]['climb_ms'], drains[0]['ms'])

    def test_an_empty_queue_writes_nothing(self):
        self.assertEqual(propagation_queue.drain()['batches'], 0)
        self.assertEqual(self.events(), [])


class SamplerParsingTests(SimpleTestCase):

    def test_percentiles_by_nearest_rank(self):
        stats = sampler.percentiles(list(range(1, 101)))
        self.assertEqual((stats['n'], stats['p50'], stats['p95'],
                          stats['p99'], stats['max']), (100, 50, 95, 99, 100))
        self.assertEqual(sampler.percentiles([]), {'n': 0})
        self.assertEqual(sampler.percentiles([3.0])['p99'], 3.0)

    def test_route_classes(self):
        cases = {
            '/atomicdb/api/lease': 'lease',
            '/atomicdb/api/submit': 'submit',
            '/atomicdb/api/heartbeat': 'heartbeat',
            '/atomicdb/api/release': 'release',
            '/atomicdb/api/query': 'api_query',
            '/atomicdb/api/principal-variations': 'api_public',
            '/atomicdb/api/roots/': 'api_public',
            '/atomicdb/explore/abc/': 'explore',
            '/atomicdb/goto/abc/e2e4/': 'explore',
            '/atomicdb/': 'home',
            '/atomicdb/queue/': 'pages',
            '/atomicdb/engines/x.zip': 'engines',
            '/static/a.css': None,
            '/tests/12/': None,
        }
        for uri, expected in cases.items():
            self.assertEqual(sampler.classify(uri), expected, uri)

    def test_timing_lines(self):
        line = '1764312000.123|200|0.052|0.050|GET|/atomicdb/api/query\n'
        self.assertEqual(sampler.parse_timing_line(line),
                         (1764312000.123, 200, 0.052, 0.05, 'GET',
                          '/atomicdb/api/query'))
        two = '1.0|502|1.5|0.700, 0.300|POST|/atomicdb/api/submit'
        self.assertAlmostEqual(sampler.parse_timing_line(two)[3], 1.0)
        none = '1.0|444|0.000|-|GET|/atomicdb/'
        self.assertIsNone(sampler.parse_timing_line(none)[3])
        self.assertIsNone(sampler.parse_timing_line('garbage'))
        # A path with a bar in it stays whole.
        self.assertEqual(sampler.parse_timing_line(
            '1.0|200|0.1|0.1|GET|/atomicdb/a|b')[5], '/atomicdb/a|b')

    def test_requests_are_summarised_by_class(self):
        records = [(1.0, 200, 0.010, 0.009, 'POST', '/atomicdb/api/lease'),
                   (1.0, 200, 0.030, 0.029, 'POST', '/atomicdb/api/lease'),
                   (1.0, 429, 0.001, None, 'GET', '/atomicdb/api/query'),
                   (1.0, 200, 0.5, 0.5, 'GET', '/other')]
        out = sampler.summarize_requests(records)
        self.assertEqual(set(out), {'lease', 'api_query'})
        self.assertEqual(out['lease']['request_ms']['n'], 2)
        self.assertEqual(out['lease']['request_ms']['max'], 30.0)
        self.assertEqual(out['api_query']['status'], {'429': 1})
        self.assertEqual(out['api_query']['upstream_ms'], {'n': 0})

    def test_appended_lines_follow_rotation_and_wait_for_whole_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'timing.log')
            with open(path, 'w') as handle:
                handle.write('old\n')
            state = {}
            self.assertEqual(sampler.read_appended(path, state, 'k', 1 << 20),
                             ([], {'warmup': True}))
            with open(path, 'a') as handle:
                handle.write('one\ntw')
            lines, _ = sampler.read_appended(path, state, 'k', 1 << 20)
            self.assertEqual(lines, ['one'])
            with open(path, 'a') as handle:
                handle.write('o\nthree\n')
            os.replace(path, path + '.1')       # logrotate
            with open(path, 'w') as handle:
                handle.write('four\n')
            lines, info = sampler.read_appended(path, state, 'k', 1 << 20)
            self.assertEqual(lines, ['two', 'three', 'four'])
            self.assertTrue(info.get('rotated'))
            lines, _ = sampler.read_appended(path, state, 'k', 1 << 20)
            self.assertEqual(lines, [])

    def test_events_give_climbs_and_submit_to_visible(self):
        events = [
            {'kind': 'job', 'job': 1, 'ms': 40.0, 'deferred': True},
            {'kind': 'job', 'job': 2, 'ms': 60.0, 'deferred': True},
            {'kind': 'job', 'job': 3, 'ms': 5.0, 'error': 'OperationalError'},
            {'kind': 'drain', 'jobs': [1, 2], 'settled': 2, 'attempts': 1,
             'commit_t': 110.0, 'climb_ms': 800.0, 'ms': 900.0},
        ]
        times = {1: (100.0, 100.5, 'DONE'), 2: (105.0, 105.25, 'DONE')}
        out = sampler.summarize_events(events, times)
        self.assertEqual((out['jobs']['applied'], out['jobs']['failed'],
                          out['jobs']['deferred']), (2, 1, 2))
        self.assertEqual(out['submit_to_own_visible_ms']['max'], 500.0)
        self.assertEqual(out['submit_to_climbed_ms']['p50'], 5000.0)
        self.assertEqual(out['submit_to_climbed_ms']['max'], 10000.0)
        self.assertEqual(out['drain']['landings'], 2)
        self.assertEqual(out['drain']['climb_ms']['max'], 800.0)

    def test_cpu_of_a_unit_between_two_samples(self):
        units = {'a.service': {'cpu_usec': 3_000_000},
                 'b.service': {'cpu_usec': 10}}
        prev = {'a.service': {'cpu_usec': 1_000_000},
                'b.service': {'cpu_usec': 50}}     # restarted: no number
        sampler.cpu_percent(units, prev, 10.0)
        self.assertEqual(units['a.service']['cpu_pct'], 20.0)
        self.assertNotIn('cpu_pct', units['b.service'])

    def test_cgroup_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = os.path.join(tmp, 'system.slice', 'atomicdb-propagate.service')
            os.makedirs(unit)
            with open(os.path.join(unit, 'cpu.stat'), 'w') as handle:
                handle.write('usage_usec 123\nuser_usec 100\n')
            with open(os.path.join(unit, 'memory.current'), 'w') as handle:
                handle.write('4096\n')
            with open(os.path.join(unit, 'memory.stat'), 'w') as handle:
                handle.write('anon 1024\nfile 3072\nshmem 0\n')
            units = sampler.read_cgroups(tmp)
        self.assertEqual(units['atomicdb-propagate.service'],
                         {'cpu_usec': 123, 'mem': 4096, 'mem_anon': 1024,
                          'mem_file': 3072, 'mem_shmem': 0})

    def test_a_whole_sample_from_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'timing.log')
            open(log, 'w').close()
            now = time.time()
            day = time.strftime('%Y%m%d', time.gmtime(now))
            events = os.path.join(tmp, f'events-{day}.jsonl')
            open(events, 'w').close()
            state = {}
            first = sampler.take_sample(tmp, log, state, now=now, use_db=False)
            self.assertIsNone(first['window_s'])
            with open(log, 'a') as handle:
                handle.write(f'{now}|200|0.020|0.019|POST|/atomicdb/api/'
                             'lease\n')
            with open(events, 'a') as handle:
                handle.write(json.dumps({'kind': 'job', 'job': 9, 'ms': 30.0})
                             + '\n')
            second = sampler.take_sample(tmp, log, state, now=now + 60,
                                         use_db=False)
        self.assertEqual(second['window_s'], 60)
        self.assertEqual(second['http']['lease']['request_ms']['n'], 1)
        self.assertEqual(second['jobs']['applied'], 1)
        self.assertAlmostEqual(second['applied_per_s'], 1 / 60, places=3)
        json.dumps(second)


def _load_report():
    path = Path(settings.BASE_DIR) / 'Scripts' / 'atomicdb_perf_report.py'
    spec = importlib.util.spec_from_file_location('atomicdb_perf_report',
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReportTests(SimpleTestCase):

    def test_a_report_from_files(self):
        report = _load_report()
        with tempfile.TemporaryDirectory() as tmp:
            t0 = 1_795_850_000.0
            with open(os.path.join(tmp, 'events-20261128.jsonl'), 'w') as f:
                for i in range(120):
                    f.write(json.dumps({'t': t0 + i, 'kind': 'job', 'job': i,
                                        'ms': 100.0 + i}) + '\n')
                f.write(json.dumps({'t': t0 + 5, 'kind': 'drain',
                                    'jobs': [1, 2], 'settled': 2,
                                    'climb_ms': 400.0, 'ms': 450.0,
                                    'commit_t': t0 + 6}) + '\n')
                # Inside the excluded interval: left out.
                f.write(json.dumps({'t': t0 + 3000, 'kind': 'job', 'job': 999,
                                    'ms': 1.0}) + '\n')
            with open(os.path.join(tmp, 'samples-20261128.jsonl'), 'w') as f:
                for i, used in enumerate((100e9, 101e9)):
                    f.write(json.dumps({
                        't': t0 + 60 * i,
                        'units': {'atomicdb-propagate.service': {
                            'cpu_pct': 50.0, 'mem': 2**30}},
                        'sizes': {'database': 270e9 + i * 1e9,
                                  'position': 100e9, 'edge': 90e9,
                                  'positions_est': 200_000_000,
                                  'bytes_per_position': 1350.0,
                                  'tables': {'atomicdb_position': {
                                      'ins': 1000 + i * 500}}},
                        'host_stats': {'disk_used': used, 'disk_free': 400e9,
                                       'load': [8.0, 8.0, 8.0]},
                        'queues': {'propagation_seeds': 10,
                                   'propagation_oldest_s': 2.0,
                                   'ingest_pending': 3},
                        'sweeps': [{'stale': 338, 'changed': 20000,
                                    'checked': 7000}] if i else []}) + '\n')
            iso = report.iso
            args = report.argparse.Namespace(
                since=iso(t0 - 1).replace(' UTC', ''),
                until=iso(t0 + 3600).replace(' UTC', ''),
                exclude=[iso(t0 + 2900).replace(' UTC', '') + '/'
                         + iso(t0 + 3100).replace(' UTC', '')],
                perf_dir=tmp, timing_glob=os.path.join(tmp, 'none*'),
                no_db=True, no_http=True)
            text = report.report(args)
        self.assertIn('| Searches applied (stage a), total | 120 |', text)
        self.assertIn('| Climb batches | 1 |', text)
        self.assertIn('atomicdb-propagate', text)
        self.assertIn('| Stale rows found per sweep pass | 338.0 |', text)
        self.assertIn('| New positions per day (inserts) | 720,000 |', text)
