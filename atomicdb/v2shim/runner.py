"""A Django test runner that runs v1's tests on v1 or v2 and reports per test.

    [ATOMICDB_ENGINE=v2] python manage.py test atomicdb \\
        --testrunner atomicdb.v2shim.runner.ShimRunner \\
        --portability portable --shim-report report.json

``--portability`` keeps the tests whose ``portability`` in the test map is in
the list (``portable``, ``mixed``, ``v1-specific``; default: all, plus tests
the map does not list).  ``--shim-report`` writes, for every test run, its
outcome and which v1 functions it called on v2 and on v1.  The run is serial
(``--parallel`` is forced to 1: the counters live in this process).
"""

import csv
import json
import os
import time
import traceback
import unittest

from django.test.runner import DiscoverRunner

from . import bridge, engine, intercept, is_v2


def _key(test_id):
    """``atomicdb.test_x.Class.method`` -> (``test_x.py``, Class, method)."""
    parts = test_id.split('.')
    if len(parts) < 4:
        return None
    return f'{parts[-3]}.py', parts[-2], parts[-1]


def load_test_map(path=None):
    path = path or os.environ.get(intercept.TEST_MAP_ENV) or intercept.DEFAULT_TEST_MAP
    out = {}
    with open(path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            out[(row['file'], row['test_class'], row['test_name'])] = row
    return out


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def classify(outcome, stats):
    """The shim class of one test (see ``shim_report.py``)."""
    if outcome == 'skip':
        return 'skip'
    if stats.get('v2_calls'):
        return 'v2-pass' if outcome in ('pass', 'expected_failure') else 'v2-fail'
    if outcome not in ('pass', 'expected_failure'):
        return 'fail-without-v2'
    return 'v1-only' if stats.get('v1_calls') else 'no-rule-call'


class ShimResult(unittest.TextTestResult):
    records = None

    def startTest(self, test):
        bridge.STATS.reset()
        self._started = time.perf_counter()
        self._outcome = 'pass'
        self._detail = ''
        super().startTest(test)

    def _mark(self, outcome, err=None):
        # The first failure wins (a subtest failing then the test passing).
        if self._outcome == 'pass':
            self._outcome = outcome
            if err:
                self._detail = ''.join(traceback.format_exception(*err))[-2000:]

    def addError(self, test, err):
        self._mark('error', err)
        super().addError(test, err)

    def addFailure(self, test, err):
        self._mark('fail', err)
        super().addFailure(test, err)

    def addSkip(self, test, reason):
        self._mark('skip')
        self._detail = reason
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._mark('expected_failure')
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._mark('unexpected_success')
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            failure = issubclass(err[0], test.failureException)
            self._mark('fail' if failure else 'error', err)
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        super().stopTest(test)
        stats = bridge.STATS.as_dict()
        record = {'outcome': self._outcome,
                  'ms': round((time.perf_counter() - self._started) * 1000, 1),
                  **stats}
        record['class'] = classify(self._outcome, stats)
        if self._detail and self._outcome not in ('pass', 'skip'):
            record['detail'] = self._detail
        type(self).records[test.id()] = record


class ShimRunner(DiscoverRunner):

    def __init__(self, portability=None, test_map=None, shim_report=None, **kwargs):
        kwargs['parallel'] = 1
        super().__init__(**kwargs)
        self.portability = {p.strip() for p in portability.split(',')} if portability else None
        self.test_map_path = test_map
        self.shim_report = shim_report or os.environ.get('ATOMICDB_V2_REPORT')
        self.routed = set()

    @classmethod
    def add_arguments(cls, parser):
        super().add_arguments(parser)
        parser.add_argument('--portability', default=None,
                            help='Comma-separated portability classes of the test map to run.')
        parser.add_argument('--test-map', default=None, help='Path of test-map.csv.')
        parser.add_argument('--shim-report', default=None, help='Write the per-test JSON report here.')

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        if is_v2():
            self.routed = intercept.install()
            if self.verbosity >= 1:
                print(f'AtomicDB shim: engine=v2, {len(intercept.installed())} v1 functions wrapped, '
                      f'{len(self.routed)} routed to v2: {sorted(self.routed)}')

    def teardown_test_environment(self, **kwargs):
        if is_v2():
            intercept.uninstall()
        super().teardown_test_environment(**kwargs)

    def build_suite(self, *args, **kwargs):
        suite = super().build_suite(*args, **kwargs)
        if not self.portability:
            return suite
        test_map = load_test_map(self.test_map_path)
        kept = unittest.TestSuite()
        for test in _iter_tests(suite):
            key = _key(test.id())
            row = test_map.get(key) if key else None
            if row is not None and row['portability'] in self.portability:
                kept.addTest(test)
        return kept

    def get_resultclass(self):
        ShimResult.records = {}
        return ShimResult

    def run_suite(self, suite, **kwargs):
        ShimResult.records = {}
        started = time.time()
        result = super().run_suite(suite, **kwargs)
        if self.shim_report:
            self._write_report(started)
        return result

    def _write_report(self, started):
        records = ShimResult.records or {}
        counts = {}
        for r in records.values():
            counts[r['class']] = counts.get(r['class'], 0) + 1
        report = {
            'engine': engine(),
            'routed': sorted(self.routed),
            'entries': sorted(bridge.av2().entries()) if is_v2() else [],
            'portability': sorted(self.portability) if self.portability else None,
            'started': started,
            'seconds': round(time.time() - started, 1),
            'counts': counts,
            'tests': records,
        }
        folder = os.path.dirname(os.path.abspath(self.shim_report))
        os.makedirs(folder, exist_ok=True)
        with open(self.shim_report, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=1, sort_keys=True)
        if self.verbosity >= 1:
            print(f'AtomicDB shim report: {self.shim_report} {counts}')
