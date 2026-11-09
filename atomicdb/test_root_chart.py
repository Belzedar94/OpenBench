"""The start position's best record chart on the roots page (§ root_chart)."""

import io
import re
from unittest import mock
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.management import call_command
from django.db import connections
from django.conf import settings
from django.test.utils import CaptureQueriesContext

from . import logic, root_chart, roots
from .models import (MainLineSnapshot, ProgressSnapshot, RootExtreme,
                     RootRecord)
from .testing import TestCase

UTC = datetime_timezone.utc
T0 = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)


def _start_key():
    return logic.key_of(logic.start_fen())


def _best(hours, value, status='UNKNOWN'):
    return RootRecord.objects.create(key=_start_key(), value=value,
                                     status=status,
                                     at=T0 + timedelta(hours=hours))


NOW = T0 + timedelta(hours=10)


def _nodes(hours_totals):
    for hours, total in hours_totals:
        ProgressSnapshot.objects.create(bucket_start=T0 + timedelta(hours=hours),
                                        engine_nodes_total=total)


def _steps(path):
    """``[(x, y)]`` corners of a step path ``M x y H x V y ... H x``."""
    x = y = None
    corners = []
    for token in re.findall(r'[MHV][^MHV]+', path):
        op, args = token[0], token[1:].split()
        if op == 'M':
            x, y = float(args[0]), float(args[1])
        elif op == 'H':
            x = float(args[0])
        else:
            y = float(args[0])
        corners.append((x, y))
    return corners


class MeasureTests(TestCase):

    def test_no_record_no_chart(self):
        MainLineSnapshot.objects.create(bucket_start=T0, value=1200)
        self.assertEqual(root_chart.measure(NOW), {'chart': None})

    def test_a_step_per_record_held_to_the_right_edge(self):
        _best(0, 1257)
        _best(5, 1258)
        _best(8, 1275)
        chart = root_chart.measure(NOW)['chart']
        corners = _steps(chart['line'])
        self.assertEqual(corners[0][0], 0)
        self.assertEqual(corners[-1][0], root_chart.WIDTH)
        xs = [x for x, _y in corners]
        self.assertEqual(xs, sorted(xs))
        # Up the screen at each record, never down.
        ys = [y for _x, y in corners]
        self.assertEqual(ys, sorted(ys, reverse=True))
        # The second record steps up exactly at its time, half way across.
        self.assertIn(root_chart.WIDTH / 2, xs)
        for _x, y in corners:
            self.assertGreaterEqual(y, root_chart.INSET)
            self.assertLessEqual(y, root_chart.HEIGHT - root_chart.INSET)
        # The latest record is drawn highest.
        self.assertEqual(corners[-1][1], root_chart.INSET)

    def test_the_axis_fits_the_records_with_a_readable_step(self):
        _best(0, 1257)
        _best(5, 1275)
        chart = root_chart.measure(NOW)['chart']
        # 12.57..12.75 snaps outward to 12.55..12.75 in steps of 0.05.
        self.assertEqual([t['label'] for t in chart['ticks']],
                         ['+12.75', '+12.70', '+12.65', '+12.60', '+12.55'])
        self.assertEqual(chart['ticks'][0]['top'], '5')     # INSET / HEIGHT
        self.assertEqual(len(chart['gridlines']), 5)

    def test_a_wide_range_takes_a_wider_step(self):
        _best(0, 1100)
        _best(5, 1290)
        chart = root_chart.measure(NOW)['chart']
        self.assertEqual([t['label'] for t in chart['ticks']],
                         ['+13.00', '+12.50', '+12.00', '+11.50', '+11.00'])

    def test_a_single_record_still_has_an_axis(self):
        _best(0, 1260)
        chart = root_chart.measure(NOW)['chart']
        self.assertEqual([t['label'] for t in chart['ticks']],
                         ['+12.61', '+12.60', '+12.59'])
        self.assertEqual(_steps(chart['line'])[-1][0], root_chart.WIDTH)

    def test_the_header_is_the_latest_record_and_its_time(self):
        _best(0, 1257)
        latest = _best(8, 1275)
        chart = root_chart.measure(NOW)['chart']
        self.assertEqual(chart['record_h'], '+12.75')
        self.assertEqual(chart['record_at'], latest.at)
        self.assertEqual(chart['first_at'], T0)
        self.assertEqual(chart['last_at'], NOW)
        self.assertEqual(chart['records'], 2)

    def test_each_step_has_a_hover_title_with_its_time_and_value(self):
        _best(0, 1257)
        _best(5, 1275)
        chart = root_chart.measure(NOW)['chart']
        self.assertEqual([c['title'] for c in chart['columns']],
                         ['2026-10-26 15:00 UTC · record +12.57',
                          '2026-10-26 20:00 UTC · record +12.75'])
        self.assertEqual([c['x'] for c in chart['columns']], ['0', '300'])
        self.assertEqual([c['w'] for c in chart['columns']], ['300', '300'])

    def test_a_proven_record_does_not_stretch_the_axis(self):
        _best(2, 1150)
        _best(6, 10000, 'WHITE_WIN')
        chart = root_chart.measure(NOW)['chart']
        self.assertEqual(chart['ticks'][0]['label'], '+11.51')
        self.assertEqual(chart['record_h'], 'White win')
        self.assertIn('record White win', chart['columns'][1]['title'])
        for _x, y in _steps(chart['line']):
            self.assertGreaterEqual(y, root_chart.INSET)
            self.assertLessEqual(y, root_chart.HEIGHT - root_chart.INSET)

    def test_another_position_is_not_the_start(self):
        RootRecord.objects.create(key='x' * 64, value=1300, at=T0)
        self.assertEqual(root_chart.measure(NOW), {'chart': None})

    def test_engine_nodes_are_a_faint_area_over_the_same_window(self):
        _best(0, 1257)
        _nodes([(-5, 1), (0, 2_000_000_000), (5, 3_000_000_000),
                (10, 4_000_000_000)])
        chart = root_chart.measure(NOW)['chart']
        self.assertTrue(chart['node_area'].startswith('M0 '))
        self.assertTrue(chart['node_area'].endswith(' Z'))
        self.assertEqual(chart['nodes_last_h'], '4.00B')

    def test_under_two_node_rows_no_area(self):
        _best(0, 1257)
        _nodes([(5, 3_000_000_000)])
        chart = root_chart.measure(NOW)['chart']
        self.assertEqual(chart['node_area'], '')
        self.assertEqual(chart['nodes_last_h'], '')

    def test_a_long_node_history_is_downsampled(self):
        _best(0, 1257)
        hours = root_chart.MAX_POINTS * 3
        _nodes([(h, h + 1) for h in range(hours)])
        chart = root_chart.measure(T0 + timedelta(hours=hours - 1))['chart']
        # The area is the points plus two corners on the bottom edge.
        points = chart['node_area'].count(' L') + 1 - 2
        self.assertLessEqual(points, root_chart.MAX_POINTS + 1)
        self.assertEqual(chart['nodes_last_h'], f'{hours:,}')

    def test_the_cache_key_moved_with_the_shape(self):
        self.assertEqual(root_chart.CACHE_KEY, 'atomicdb.root-chart.v4')


class CacheTests(TestCase):

    def test_a_warm_entry_costs_no_query(self):
        _best(0, 1257)
        _nodes([(0, 1), (1, 2)])
        first = root_chart.chart()
        self.assertIsNotNone(first)
        alias = settings.ATOMICDB_DATABASE_ALIAS
        with CaptureQueriesContext(connections[alias]) as atomicdb_queries, \
                CaptureQueriesContext(connections['default']) as default_queries:
            again = root_chart.chart()
        self.assertEqual(again, first)
        self.assertEqual(len(atomicdb_queries), 0)
        self.assertEqual(len(default_queries), 0)

    def test_a_new_record_shows_once_the_entry_is_refreshed(self):
        _best(0, 1257)
        self.assertEqual(root_chart.chart()['record_h'], '+12.57')
        _best(1, 1275)
        self.assertEqual(root_chart.chart()['record_h'], '+12.57')
        self.assertEqual(root_chart.chart(force=True)['record_h'], '+12.75')


class RootsPageTests(TestCase):

    def _roots(self):
        response = self.client.get('/atomicdb/roots/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_no_record_no_chart(self):
        MainLineSnapshot.objects.create(bucket_start=T0, value=1200)
        self.assertNotIn('class="root-chart"', self._roots())

    def test_the_chart_is_drawn_inline(self):
        _best(0, 1257)
        _best(24, 1275)
        _nodes([(0, 1_000_000_000), (24, 2_000_000_000)])
        body = self._roots()
        self.assertIn('class="root-chart"', body)
        self.assertIn('Start position best record', body)
        self.assertIn('+12.75', body)
        self.assertIn('set 27 Oct 2026 15:00 UTC', body)
        svg = re.search(r'<svg[^>]*class="root-chart-svg".*?</svg>', body, re.S)
        self.assertIsNotNone(svg)
        self.assertIn('preserveAspectRatio="none"', svg.group(0))
        self.assertIn('class="rc-best"', svg.group(0))
        self.assertIn('class="rc-nodes"', svg.group(0))
        self.assertNotIn('rc-line', svg.group(0))
        self.assertEqual(svg.group(0).count('class="rc-hit"'), 2)
        self.assertIn('record +12.57', svg.group(0))
        # Nothing on it loads anything from elsewhere.
        self.assertNotIn('<script', svg.group(0))
        self.assertIn('engine nodes, 2.00B now', body)
        self.assertNotIn('median', body)

    def test_the_home_page_has_no_chart(self):
        _best(0, 1257)
        body = self.client.get('/atomicdb/').content.decode()
        self.assertNotIn('class="root-chart"', body)


class CaptureBestRecordTests(TestCase):

    def _extreme(self, value, at, status='UNKNOWN'):
        RootExtreme.objects.update_or_create(key=_start_key(), defaults=dict(
            best_value=value, best_status=status, best_at=at,
            worst_value=0, worst_status='UNKNOWN', worst_at=T0,
            last_value=value, last_status=status, changed_at=at, since=T0))

    def test_nothing_without_an_extremes_row(self):
        self.assertIsNone(roots.capture_best_record())
        self.assertFalse(RootRecord.objects.exists())

    def test_a_new_best_is_appended_once_with_its_own_time(self):
        at = T0 + timedelta(minutes=17)
        self._extreme(1270, at)
        row = roots.capture_best_record()
        self.assertEqual((row.value, row.at), (1270, at))
        self.assertIsNone(roots.capture_best_record())
        self._extreme(1275, at + timedelta(hours=3))
        self.assertEqual(roots.capture_best_record().value, 1275)
        self.assertEqual(list(RootRecord.objects.values_list('value',
                                                             flat=True)),
                         [1270, 1275])

    def test_the_hourly_command_records_it(self):
        self._extreme(1270, T0)
        out = io.StringIO()
        call_command('capture_atomicdb_progress', stdout=out)
        self.assertIn('start record: 1270', out.getvalue())
        self.assertEqual(RootRecord.objects.count(), 1)

    def test_records_are_append_only(self):
        row = _best(0, 1200)
        row.value = 1300
        with self.assertRaises(Exception):
            row.save()
        with self.assertRaises(Exception):
            row.delete()


class SeedRootRecordsTests(TestCase):

    LOG = (
        '2026-11-01T22:02:59+00:00 host systemd[1]: Starting feed...\n'
        '2026-11-01T23:36:54+00:00 host python[1]: evento [record] New record'
        ' for the start position: +12.57\n'
        '2026-11-02T02:22:23+00:00 host python[2]: evento [record] New record'
        ' for the start position: +12.58\n'
        '2026-11-09T15:50:12+00:00 host python[3]: evento [record] New record'
        ' for the start position: +12.75\n'
    )

    def _seed(self, *args):
        out = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO(self.LOG)):
            call_command('seed_root_records', *args, stdout=out)
        return out.getvalue()

    def test_seeds_from_the_log_once(self):
        self.assertIn('added 3 of 3', self._seed())
        rows = list(RootRecord.objects.values_list('at', 'value'))
        self.assertEqual(rows[0], (datetime(2026, 11, 1, 23, 36, 54,
                                            tzinfo=UTC), 1257))
        self.assertEqual([v for _at, v in rows], [1257, 1258, 1275])
        self.assertIn('added 0 of 3', self._seed())

    def test_a_value_the_capture_already_has_is_skipped(self):
        exact = datetime(2026, 11, 9, 15, 43, tzinfo=UTC)
        RootRecord.objects.create(key=_start_key(), value=1275, at=exact)
        self.assertIn('added 2 of 3', self._seed())
        self.assertEqual(RootRecord.objects.get(value=1275).at, exact)

    def test_dry_run_writes_nothing(self):
        self.assertIn('would add 3 of 3', self._seed('--dry-run'))
        self.assertFalse(RootRecord.objects.exists())
