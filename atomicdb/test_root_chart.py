"""The start position's eval chart on the roots page (§ root_chart)."""

import re
from unittest import mock
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.db import connections
from django.conf import settings
from django.test.utils import CaptureQueriesContext

from . import root_chart
from .models import MainLineSnapshot, ProgressSnapshot
from .testing import TestCase

UTC = datetime_timezone.utc
T0 = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)


def _record(hours_values, nodes=True):
    for hours, value in hours_values:
        at = T0 + timedelta(hours=hours)
        MainLineSnapshot.objects.create(bucket_start=at, value=value)
        if nodes:
            ProgressSnapshot.objects.create(
                bucket_start=at, engine_nodes_total=1_000_000_000 * (hours + 1))


class MeasureTests(TestCase):

    def setUp(self):
        super().setUp()
        # These tests read the hourly record point by point; the daily
        # median has its own tests (DailyMedianTests).
        patcher = mock.patch.object(root_chart, 'DAY_HOURS', 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_chart_under_two_points(self):
        self.assertEqual(root_chart.measure(), {'chart': None})
        _record([(0, 1200)])
        self.assertEqual(root_chart.measure(), {'chart': None})

    def test_a_row_without_a_value_is_left_out(self):
        _record([(0, 1200), (1, 1180)])
        MainLineSnapshot.objects.create(bucket_start=T0 + timedelta(hours=2),
                                        value=None)
        chart = root_chart.measure()['chart']
        self.assertEqual(len(chart['columns']), 2)
        self.assertEqual(chart['last_at'], T0 + timedelta(hours=1))

    def test_values_are_white_side_pawns_and_the_axis_snaps(self):
        _record([(0, 1244), (1, 1156), (2, 1172)])
        chart = root_chart.measure()['chart']
        self.assertEqual(chart['first_h'], '+12.44')
        self.assertEqual(chart['latest_h'], '+11.72')
        # 1156..1244 snaps outward to 11.50..12.50.
        self.assertEqual([t['label'] for t in chart['ticks']],
                         ['+12.50', '+12.00', '+11.50'])
        self.assertEqual(chart['ticks'][0]['top'], '5')     # INSET / HEIGHT
        self.assertEqual(chart['nodes_last_h'], '3.00B')
        self.assertIn('+11.56', chart['columns'][1]['title'])
        self.assertIn('2.00B engine nodes', chart['columns'][1]['title'])

    def test_the_line_spans_the_whole_width_and_stays_inside(self):
        _record([(0, -300), (5, 0), (10, 450)])
        chart = root_chart.measure()['chart']
        coords = [tuple(map(float, pair.split()))
                  for pair in chart['line'][1:].split(' L')]
        self.assertEqual(coords[0][0], 0)
        self.assertEqual(coords[-1][0], root_chart.WIDTH)
        for _x, y in coords:
            self.assertGreaterEqual(y, root_chart.INSET)
            self.assertLessEqual(y, root_chart.HEIGHT - root_chart.INSET)
        # The lowest value is drawn lowest.
        self.assertEqual(coords[0][1], root_chart.HEIGHT - root_chart.INSET)

    def test_a_flat_series_still_has_an_axis(self):
        _record([(0, 1200), (1, 1200)])
        chart = root_chart.measure()['chart']
        self.assertEqual([t['label'] for t in chart['ticks']],
                         ['+12.25', '+12.00', '+11.75'])

    def test_a_long_history_is_downsampled_to_the_last_of_each_slot(self):
        hours = root_chart.MAX_POINTS * 3
        _record([(h, 1000 + h) for h in range(hours)], nodes=False)
        chart = root_chart.measure()['chart']
        self.assertLessEqual(len(chart['columns']), root_chart.MAX_POINTS)
        self.assertEqual(chart['step_hours'], 3)
        # The newest hour is always the last point.
        self.assertEqual(chart['last_at'], T0 + timedelta(hours=hours - 1))
        self.assertEqual(chart['node_area'], '')

    def test_missing_progress_rows_drop_only_the_node_series(self):
        _record([(0, 1200), (1, 1190), (2, 1210)], nodes=False)
        ProgressSnapshot.objects.create(bucket_start=T0, engine_nodes_total=5)
        chart = root_chart.measure()['chart']
        self.assertEqual(chart['node_area'], '')
        self.assertEqual(chart['nodes_last_h'], '')
        self.assertIn('5 engine nodes', chart['columns'][0]['title'])
        self.assertNotIn('engine nodes', chart['columns'][1]['title'])


class CacheTests(TestCase):

    def setUp(self):
        super().setUp()
        # These tests read the hourly record point by point; the daily
        # median has its own tests (DailyMedianTests).
        patcher = mock.patch.object(root_chart, 'DAY_HOURS', 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_warm_entry_costs_no_query(self):
        _record([(0, 1200), (1, 1190)])
        first = root_chart.chart()
        self.assertIsNotNone(first)
        alias = settings.ATOMICDB_DATABASE_ALIAS
        with CaptureQueriesContext(connections[alias]) as atomicdb_queries, \
                CaptureQueriesContext(connections['default']) as default_queries:
            again = root_chart.chart()
        self.assertEqual(again, first)
        self.assertEqual(len(atomicdb_queries), 0)
        self.assertEqual(len(default_queries), 0)

    def test_a_new_hour_shows_once_the_entry_is_refreshed(self):
        _record([(0, 1200), (1, 1190)])
        self.assertEqual(root_chart.chart()['latest_h'], '+11.90')
        _record([(2, 1250)])
        self.assertEqual(root_chart.chart()['latest_h'], '+11.90')
        self.assertEqual(root_chart.chart(force=True)['latest_h'], '+12.50')


class RootsPageTests(TestCase):

    def _home(self):
        response = self.client.get('/atomicdb/roots/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_no_history_no_chart(self):
        self.assertNotIn('class="root-chart"', self._home())

    def test_the_chart_is_drawn_inline(self):
        _record([(0, 1244), (24, 1156)])
        body = self._home()
        self.assertIn('class="root-chart"', body)
        self.assertIn('Start position eval', body)
        self.assertIn('+11.56', body)
        self.assertIn('26 Oct 2026', body)
        svg = re.search(r'<svg[^>]*class="root-chart-svg".*?</svg>', body, re.S)
        self.assertIsNotNone(svg)
        self.assertIn('preserveAspectRatio="none"', svg.group(0))
        self.assertEqual(svg.group(0).count('class="rc-hit"'), 2)
        # Nothing on it loads anything from elsewhere.
        self.assertNotIn('<script', svg.group(0))

    def test_the_home_page_has_no_chart(self):
        _record([(0, 1244), (24, 1156)])
        body = self.client.get('/atomicdb/').content.decode()
        self.assertNotIn('class="root-chart"', body)


class DailyMedianTests(TestCase):

    def test_one_point_per_day_at_the_median_of_the_day(self):
        # Day 0: 1100, 1300, 1200 -> median 1200; day 1: 900, 1000 -> 950.
        _record([(0, 1100), (1, 1300), (2, 1200), (24, 900), (25, 1000)])
        chart = root_chart.measure()['chart']
        self.assertEqual(chart['step_hours'], 24)
        self.assertEqual(len(chart['columns']), 2)
        self.assertIn('+12.00', chart['columns'][0]['title'])
        self.assertIn('median of the day', chart['columns'][0]['title'])
        self.assertIn('+9.50', chart['columns'][1]['title'])

    def test_the_headline_keeps_the_real_first_and_latest_readings(self):
        _record([(0, 1100), (1, 1300), (24, 900), (25, 1000)])
        chart = root_chart.measure()['chart']
        self.assertEqual(chart['first_h'], '+11.00')
        self.assertEqual(chart['latest_h'], '+10.00')
