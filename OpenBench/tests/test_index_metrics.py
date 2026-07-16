import datetime
import math

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from OpenBench.datagen import initialize_chunks
from OpenBench.index_metrics import (
    get_index_metrics,
    reset_index_metrics_state,
)
from OpenBench.models import DatagenChunk, Engine, Machine, Result, Test


class IndexMetricsTests(TestCase):

    def setUp(self):
        reset_index_metrics_state()
        self.now = timezone.now()
        self.engine = Engine.objects.create(
            name='metrics-branch',
            source='https://example.test/engine.zip',
            sha='a' * 40,
            bench=123,
        )
        self.machine_number = 0

    def tearDown(self):
        reset_index_metrics_state()

    def make_machine(self, concurrency, mnps, updated):
        self.machine_number += 1
        user = User.objects.create_user('worker%d' % self.machine_number)
        machine = Machine.objects.create(
            user=user,
            mnps=mnps,
            info={
                'concurrency': concurrency,
                'physical_cores': concurrency,
                'sockets': 1,
            },
        )
        Machine.objects.filter(id=machine.id).update(updated=updated)
        machine.refresh_from_db()
        return machine

    def make_test(self, **overrides):
        fields = {
            'author': 'metrics',
            'book_name': 'NONE',
            'dev': self.engine,
            'base': self.engine,
            'dev_repo': 'https://github.com/example/engine',
            'base_repo': 'https://github.com/example/engine',
            'dev_engine': 'Example',
            'base_engine': 'Example',
            'dev_options': '',
            'base_options': '',
            'dev_time_control': '1.0+0.01',
            'base_time_control': '1.0+0.01',
            'test_mode': 'GAMES',
            'approved': True,
        }
        fields.update(overrides)
        return Test.objects.create(**fields)

    def test_live_machine_window_sums_threads_and_reported_nps(self):
        self.make_machine(
            concurrency=8,
            mnps=132.5,
            updated=self.now - datetime.timedelta(minutes=2, seconds=59),
        )
        self.make_machine(
            concurrency=16,
            mnps=500,
            updated=self.now - datetime.timedelta(minutes=3, seconds=1),
        )

        metrics = get_index_metrics(now=self.now)

        self.assertEqual(metrics['live_machines'], 1)
        self.assertEqual(metrics['cores'], 8)
        self.assertEqual(metrics['nodes_per_second'], 1.06e9)
        self.assertEqual(metrics['cards'][1]['value'], '1.06G')

    def test_partial_datagen_uses_rate_since_first_completed_chunk(self):
        self.make_machine(2, 1.0, self.now)
        test = self.make_test(
            test_mode='DATAGEN',
            datagen_command='datagen count {COUNT} out {OUT}',
            datagen_total_count=1000,
            datagen_positions_per_chunk=500,
            datagen_base_seed=10,
            max_games=1000,
        )
        initialize_chunks(test)
        DatagenChunk.objects.filter(test=test, idx=0).update(
            status=DatagenChunk.COMPLETED,
            completed=self.now - datetime.timedelta(seconds=100),
        )

        metrics = get_index_metrics(now=self.now)

        self.assertEqual(metrics['datagen_remaining_positions'], 500)
        self.assertAlmostEqual(metrics['datagen_positions_per_second'], 5.0)
        self.assertAlmostEqual(metrics['time_remaining_seconds'], 100.0)
        self.assertEqual(metrics['cards'][3]['value'], '2m')

    def test_sprt_uses_resolved_history_median_and_rolling_game_delta(self):
        self.make_machine(4, 1.0, self.now)
        for games, llr in [(1000, -3.0), (3000, 3.0)]:
            self.make_test(
                test_mode='SPRT',
                games=games,
                lowerllr=-2.0,
                currentllr=llr,
                upperllr=2.0,
                finished=True,
                passed=llr > 0,
                failed=llr < 0,
            )

        active = self.make_test(
            test_mode='SPRT',
            games=500,
            lowerllr=-2.0,
            currentllr=0.0,
            upperllr=2.0,
        )
        Test.objects.filter(id=active.id).update(
            creation=self.now - datetime.timedelta(hours=1)
        )
        result = Result.objects.create(
            test=active,
            machine=Machine.objects.get(updated__gte=self.now),
            games=0,
        )
        Result.objects.filter(id=result.id).update(updated=self.now)

        first = get_index_metrics(now=self.now)
        self.assertEqual(first['games_per_minute'], 0.0)
        self.assertEqual(first['sprt_expected_games'], 2000.0)

        later = self.now + datetime.timedelta(minutes=1)
        Result.objects.filter(id=result.id).update(games=100, updated=later)
        Test.objects.filter(id=active.id).update(games=600, updated=later)
        metrics = get_index_metrics(now=later)

        self.assertEqual(metrics['games_rate_source'], 'rolling')
        self.assertAlmostEqual(metrics['games_per_minute'], 100.0)
        self.assertEqual(metrics['game_remaining'], 1400.0)
        self.assertAlmostEqual(metrics['time_remaining_seconds'], 14 * 60)
        self.assertEqual(metrics['cards'][3]['value'], '14m')

    def test_spsa_uses_iterations_and_pairs_or_is_explicitly_excluded(self):
        self.make_machine(2, 1.0, self.now)
        self.make_test(
            test_mode='SPSA',
            games=20,
            spsa={'iterations': 10, 'pairs_per': 5},
        )
        self.make_test(test_mode='SPSA', games=10, spsa={})

        metrics = get_index_metrics(now=self.now)

        self.assertEqual(metrics['game_remaining'], 80)
        self.assertEqual(metrics['excluded_spsa'], 1)
        self.assertTrue(math.isinf(metrics['time_remaining_seconds']))
        self.assertIn('1 SPSA workload(s)', metrics['cards'][3]['tooltip'])

    def test_metrics_are_cached_for_thirty_seconds(self):
        machine = self.make_machine(2, 10.0, self.now)
        first = get_index_metrics(now=self.now)

        Machine.objects.filter(id=machine.id).update(
            info={'concurrency': 6, 'physical_cores': 6, 'sockets': 1},
            updated=self.now + datetime.timedelta(seconds=10),
        )
        cached = get_index_metrics(now=self.now + datetime.timedelta(seconds=10))
        refreshed = get_index_metrics(now=self.now + datetime.timedelta(seconds=31))

        self.assertIs(cached, first)
        self.assertEqual(cached['cores'], 2)
        self.assertEqual(refreshed['cores'], 6)

    def test_index_renders_exactly_four_metric_cards(self):
        response = Client().get('/index/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.count(b'class="index-metric"'), 4)
        for label in [b'Cores', b'Nodes/sec', b'Games/min', b'Time remaining']:
            self.assertContains(response, label)
        self.assertContains(response, b'ESTIMATE:')
