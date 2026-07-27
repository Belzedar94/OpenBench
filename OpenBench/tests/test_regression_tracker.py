import datetime

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from OpenBench import stats
from OpenBench.models import Engine, Test


class RegressionTrackerTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.base = Engine.objects.create(
            name='release-v1.0',
            source='https://example.test/engine.zip',
            sha='b' * 40,
            bench=100,
        )

    def make_test(self, name='regression-20260727', engine='Example', **overrides):
        dev = Engine.objects.create(
            name=name,
            source='https://example.test/engine.zip',
            sha=('%040x' % (Engine.objects.count() + 1)),
            bench=101,
        )
        fields = {
            'author': 'regression',
            'book_name': 'UHO.epd',
            'dev': dev,
            'base': self.base,
            'dev_repo': 'https://github.com/example/engine',
            'base_repo': 'https://github.com/example/engine',
            'dev_engine': engine,
            'base_engine': engine,
            'dev_options': 'Threads=1 Hash=32',
            'base_options': 'Threads=1 Hash=32',
            'dev_time_control': '60+0.6',
            'base_time_control': '60+0.6',
            'test_mode': 'GAMES',
            'games': 200,
            'wins': 60,
            'losses': 50,
            'draws': 90,
            'LL': 5,
            'LD': 20,
            'DD': 45,
            'DW': 20,
            'WW': 10,
            'finished': True,
        }
        fields.update(overrides)
        return Test.objects.create(**fields)

    def test_index_groups_only_qualifying_tests_by_engine(self):
        self.make_test(engine='Atomic-Stockfish')
        self.make_test(name='regression-20260720', engine='Atomic-Stockfish')
        self.make_test(engine='Spell-Stockfish')
        self.make_test(name='feature-branch', engine='Ignored')
        self.make_test(engine='Ignored', finished=False)
        self.make_test(engine='Ignored', deleted=True)
        self.make_test(engine='Ignored', test_mode='SPRT')

        response = self.client.get(reverse('regression_index'))

        self.assertEqual(response.status_code, 200)
        engines = list(response.context['engines'])
        self.assertEqual(engines, [
            {'dev_engine': 'Atomic-Stockfish', 'test_count': 2},
            {'dev_engine': 'Spell-Stockfish', 'test_count': 1},
        ])
        self.assertContains(response, 'Atomic-Stockfish')
        self.assertNotContains(response, 'Ignored')

    def test_engine_page_is_newest_first_and_reuses_openbench_statistics(self):
        older = self.make_test(name='regression-20260720')
        newer = self.make_test(name='regression-20260727')
        Test.objects.filter(id=older.id).update(
            creation=timezone.now() - datetime.timedelta(days=7),
        )

        response = self.client.get(reverse(
            'regression_engine', kwargs={'engine': 'Example'},
        ))

        self.assertEqual(response.status_code, 200)
        tests = response.context['tests']
        self.assertEqual([test.id for test in tests], [newer.id, older.id])

        lower, elo, upper = stats.Elo(newer.results())
        self.assertAlmostEqual(tests[0].regression_elo, elo)
        self.assertAlmostEqual(
            tests[0].regression_error, max(upper - elo, elo - lower),
        )
        self.assertAlmostEqual(
            tests[0].regression_los, 100.0 * stats.LOS(newer.results()),
        )
        self.assertContains(response, newer.dev.sha[:8])
        self.assertContains(response, self.base.sha[:8])
        self.assertContains(response, '/test/%d/' % newer.id)

    def test_engine_without_measurements_returns_404(self):
        response = self.client.get(reverse(
            'regression_engine', kwargs={'engine': 'Missing'},
        ))

        self.assertEqual(response.status_code, 404)

    def test_los_handles_trinomial_pentanomial_and_degenerate_samples(self):
        self.assertGreater(stats.LOS((10, 20, 30)), 0.50)
        self.assertGreater(stats.LOS((5, 20, 45, 20, 10)), 0.50)
        self.assertEqual(stats.LOS((0, 20, 0)), 0.50)
        self.assertEqual(stats.LOS((0, 0, 20)), 1.00)
        self.assertEqual(stats.LOS((0, 0, 0)), 0.50)
