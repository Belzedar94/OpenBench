import datetime

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

    def make_datagen(self, chunks=4, per_chunk=500, command=None):
        test = self.make_test(
            test_mode='DATAGEN',
            datagen_command=(
                command or 'datagen nodes 1000 count {COUNT} out {OUT}'
            ),
            datagen_total_count=chunks * per_chunk,
            datagen_positions_per_chunk=per_chunk,
            datagen_base_seed=10,
            max_games=chunks * per_chunk,
        )
        initialize_chunks(test)
        return test

    def complete_chunk(self, test, idx, completed):
        DatagenChunk.objects.filter(test=test, idx=idx).update(
            status=DatagenChunk.COMPLETED, completed=completed,
        )

    def test_datagen_rate_measures_intervals_between_completions(self):
        # Incidente 2026-07-22: con la tasa medida desde el primer chunk
        # completado hasta `now`, el KPI decia "4h" para una run de una
        # semana justo despues del primer chunk. Los intervalos ENTRE
        # completions excluyen las posiciones del chunk ancla.
        self.make_machine(2, 1.0, self.now)
        test = self.make_datagen(chunks=4, per_chunk=500)
        self.complete_chunk(test, 0, self.now - datetime.timedelta(seconds=300))
        self.complete_chunk(test, 1, self.now - datetime.timedelta(seconds=200))
        self.complete_chunk(test, 2, self.now - datetime.timedelta(seconds=100))

        metrics = get_index_metrics(now=self.now)

        # 1000 posiciones (chunks 1 y 2) en 200s = 5 pos/s; queda 1 chunk.
        self.assertEqual(metrics['datagen_remaining_positions'], 500)
        self.assertAlmostEqual(metrics['datagen_positions_per_second'], 5.0)
        self.assertFalse(metrics['datagen_estimated'])
        self.assertAlmostEqual(metrics['time_remaining_seconds'], 100.0)
        self.assertEqual(metrics['cards'][3]['value'], '2m')

    def test_datagen_single_completion_falls_back_to_fleet_heuristic(self):
        # Un solo chunk completado no da intervalo: antes esto inflaba la
        # tasa (posiciones/minutos-desde-la-subida). Sin historia previa,
        # usa la heuristica de flota: nps / (nodes x overhead).
        self.make_machine(2, 1.0, self.now)  # 2 threads x 1 Mnps = 2e6 nps
        test = self.make_datagen(chunks=2, per_chunk=500)
        self.complete_chunk(test, 0, self.now - datetime.timedelta(seconds=60))

        metrics = get_index_metrics(now=self.now)

        expected_rate = 2e6 / (1000 * 18.0)
        self.assertEqual(metrics['datagen_remaining_positions'], 500)
        self.assertAlmostEqual(
            metrics['datagen_positions_per_second'], expected_rate
        )
        self.assertTrue(metrics['datagen_estimated'])
        self.assertAlmostEqual(
            metrics['time_remaining_seconds'], 500 / expected_rate
        )
        self.assertEqual(metrics['cards'][3]['value'], '<1m')

    def test_datagen_without_completions_still_counts_via_heuristic(self):
        # "Ni siquiera tiene en cuenta el datagen de atomic": tests DATAGEN
        # encolados sin ningun chunk completado deben sumar tiempo estimado
        # en lugar de aportar cero segundos.
        self.make_machine(2, 1.0, self.now)
        self.make_datagen(chunks=4, per_chunk=500)

        metrics = get_index_metrics(now=self.now)

        expected_rate = 2e6 / (1000 * 18.0)
        self.assertAlmostEqual(
            metrics['time_remaining_seconds'], 2000 / expected_rate
        )
        self.assertEqual(metrics['cards'][3]['value'], '<1m')

    def test_datagen_unparsable_nodes_uses_pessimistic_history(self):
        # Sin `nodes` parseable el valor sigue siendo ABSOLUTO: gana el
        # candidato mas lento entre la mediana historica (5 pos/s, medida
        # del test hermano) y la heuristica con 10k nodos asumidos (11.1).
        self.make_machine(2, 1.0, self.now)
        self.make_datagen(
            chunks=2, per_chunk=500, command='datagen count {COUNT} out {OUT}'
        )
        measured = self.make_datagen(chunks=4, per_chunk=500)
        self.complete_chunk(
            measured, 0, self.now - datetime.timedelta(seconds=200)
        )
        self.complete_chunk(
            measured, 1, self.now - datetime.timedelta(seconds=100)
        )

        metrics = get_index_metrics(now=self.now)

        # Medido: 1000 restantes a 5 pos/s = 200s. Sin nodes: 1000 restantes
        # a min(5 historico, 11.1 heuristica) = 5 pos/s = 200s. Total 400s.
        self.assertAlmostEqual(metrics['time_remaining_seconds'], 400.0)
        self.assertTrue(metrics['datagen_estimated'])
        self.assertEqual(metrics['cards'][3]['value'], '7m')

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

        # La SPSA sin metadata ya no desaparece: se le carga la mediana SPRT
        # (fallback 15000 al no haber historia resuelta).
        self.assertEqual(metrics['game_remaining'], 80 + 15000)
        self.assertEqual(metrics['excluded_spsa'], 1)
        # Sin ratio medido, contra la capacidad de 2 threads (60/45
        # partidas/min por thread) — antes esto colapsaba a infinito.
        capacity = 2 * 60.0 / 45.0
        self.assertAlmostEqual(
            metrics['time_remaining_seconds'], 15080 * 60.0 / capacity
        )
        self.assertEqual(metrics['cards'][3]['value'], '3d 22h')
        self.assertIn('1 SPSA workload(s)', metrics['cards'][3]['tooltip'])

    def test_sprt_llr_extrapolation_and_stall_floor(self):
        from OpenBench import index_metrics as im
        median = 2000.0

        moving = self.make_test(
            test_mode='SPRT', games=4000,
            lowerllr=-2.0, currentllr=1.5, upperllr=2.0,
        )
        self.assertAlmostEqual(
            im._sprt_remaining_games(moving, median), 4000 / 0.75 - 4000
        )

        # LLR ~0 con la mediana ya superada: suelo del 25% en vez de cero.
        stalled = self.make_test(
            test_mode='SPRT', games=4000,
            lowerllr=-2.0, currentllr=0.05, upperllr=2.0,
        )
        self.assertAlmostEqual(
            im._sprt_remaining_games(stalled, median), 500.0
        )

        # Progreso hacia el bound INFERIOR tambien cuenta como progreso.
        failing = self.make_test(
            test_mode='SPRT', games=1000,
            lowerllr=-2.0, currentllr=-1.0, upperllr=2.0,
        )
        self.assertAlmostEqual(
            im._sprt_remaining_games(failing, median), 1000.0
        )

    def test_gameplay_uses_last_measured_rate_when_starved(self):
        from OpenBench import index_metrics as im
        im._last_gameplay_rate = (50.0, self.now)
        seconds, estimated = im._gameplay_seconds(8, 0.0, 1000)
        self.assertAlmostEqual(seconds, 1200.0)
        self.assertTrue(estimated)

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

    def test_starved_gameplay_always_gets_absolute_estimate(self):
        # Incidente 2026-07-17: SPRTs encoladas (0 games/min) con datagen
        # activo colapsaban el total a infinito. Ahora SIEMPRE hay numero:
        # capacidad por threads, con suelo de 1 thread en el caso degenerado.
        from OpenBench import index_metrics as im

        gameplay, estimated = im._gameplay_seconds(
            cores=32, games_per_minute=0.0, game_remaining=5000
        )
        self.assertAlmostEqual(gameplay, 5000 * 60.0 / (32 * 60.0 / 45.0))
        self.assertTrue(estimated)

        gameplay, estimated = im._gameplay_seconds(
            cores=0, games_per_minute=0.0, game_remaining=5000
        )
        self.assertAlmostEqual(gameplay, 5000 * 60.0 / (60.0 / 45.0))
        self.assertTrue(estimated)
