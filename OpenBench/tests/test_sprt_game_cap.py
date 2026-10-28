import math
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, SimpleTestCase, TestCase

import OpenBench.config
import OpenBench.views # Before the workloads, which on their own trip an import cycle
from OpenBench.models import Engine, LogEvent, Machine, Profile, Result, Test
from OpenBench.utils import SPRT_MAX_GAMES_LTC, SPRT_MAX_GAMES_STC, sprt_max_games
from OpenBench.workloads import create_workload


CAPPED = 'Max games reached (%d): stopped without a verdict'

# Pentanomial counts of 39,990 games. Symmetric: between symmetric bounds the
# LLR stays at 0 however many games are played, so no bound is ever crossed.
EVEN   = (1999, 4000, 7997, 4000, 1999)
AHEAD  = (999, 3000, 9997, 4000, 1999) # Clearly better: passes [0, 5]
BEHIND = (1999, 4000, 9997, 3000, 999) # Clearly worse: fails [0, 5]


def trinomial(penta):
    # Losses, draws, and wins of the game pairs, reading DD as two draws
    LL, LD, DD, DW, WW = penta
    return (2 * LL + LD, LD + 2 * DD + DW, DW + 2 * WW)


class SprtGameCapTests(TestCase):

    ## An SPRT whose LLR has not crossed a bound stops once it has played
    ## max_games, with neither verdict. A cap of 0 means no cap at all, and an
    ## LLR that crosses a bound on the update that reaches the cap decides.

    def setUp(self):
        self.engine = Engine.objects.create(
            name='cap-branch',
            source='https://example.test/archive.zip',
            sha='c' * 40,
            bench=1234,
        )
        user = User.objects.create_user('cap-worker', password='pass')
        Profile.objects.create(user=user, enabled=True)
        self.machine = Machine.objects.create(user=user, secret='secret', info={
            'client_ver': OpenBench.config.OPENBENCH_CONFIG['client_version'],
            'concurrency': 2,
        })

    def make_test(self, penta, **overrides):
        losses, draws, wins = trinomial(penta)
        values = dict(
            author='cap-author',
            book_name='spell_openings.epd',
            dev=self.engine,
            base=self.engine,
            dev_repo='https://github.com/example/engine',
            base_repo='https://github.com/example/engine',
            dev_engine='Spell-Stockfish',
            base_engine='Spell-Stockfish',
            dev_options='Threads=1 Hash=32',
            base_options='Threads=1 Hash=32',
            dev_time_control='8.0+0.08',
            base_time_control='8.0+0.08',
            test_mode='SPRT',
            elolower=-5.0,
            eloupper=5.0,
            alpha=0.05,
            beta=0.05,
            lowerllr=math.log(0.05 / 0.95),
            upperllr=math.log(0.95 / 0.05),
            max_games=SPRT_MAX_GAMES_STC,
            approved=True,
            games=2 * sum(penta),
            losses=losses, draws=draws, wins=wins,
            LL=penta[0], LD=penta[1], DD=penta[2], DW=penta[3], WW=penta[4],
        )
        values.update(overrides)
        return Test.objects.create(**values)

    def report(self, test, penta=(1, 1, 1, 1, 1)):
        # What a worker sends after a batch: five game pairs by default
        result, created = Result.objects.get_or_create(test=test, machine=self.machine)
        return Client().post('/clientSubmitResults/', {
            'machine_id': self.machine.id,
            'secret': 'secret',
            'test_id': test.id,
            'result_id': result.id,
            'crashes': 0,
            'timelosses': 0,
            'illegals': 0,
            'trinomial': '%d %d %d' % trinomial(penta),
            'pentanomial': '%d %d %d %d %d' % penta,
        }).json()

    def cap_events(self, test):
        return LogEvent.objects.filter(test_id=test.id)

    # Reaching the cap without a verdict

    def test_reaching_the_cap_stops_the_test_without_a_verdict(self):
        test = self.make_test(EVEN)

        self.assertEqual(self.report(test), {'stop': True})

        test.refresh_from_db()
        self.assertEqual(test.games, SPRT_MAX_GAMES_STC)
        self.assertTrue(test.lowerllr < test.currentllr < test.upperllr)
        self.assertTrue(test.finished)
        self.assertFalse(test.passed)
        self.assertFalse(test.failed)

        event = self.cap_events(test).get()
        self.assertEqual(event.author, 'OpenBench')
        self.assertEqual(event.summary, CAPPED % SPRT_MAX_GAMES_STC)
        self.assertEqual(event.log_file, '')
        self.assertEqual(event.machine_id, 0)

    def test_later_reports_add_nothing_and_log_nothing(self):
        test = self.make_test(EVEN)
        self.report(test)

        self.assertEqual(self.report(test), {'stop': True})

        test.refresh_from_db()
        self.assertEqual(test.games, SPRT_MAX_GAMES_STC)
        self.assertEqual(self.cap_events(test).count(), 1)

    def test_a_report_that_overshoots_the_cap_also_stops(self):
        test = self.make_test(EVEN, max_games=39995)

        self.assertEqual(self.report(test), {'stop': True})

        test.refresh_from_db()
        self.assertEqual(test.games, 40000)
        self.assertTrue(test.finished)
        self.assertFalse(test.passed or test.failed)
        self.assertEqual(self.cap_events(test).get().summary, CAPPED % 39995)

    def test_below_the_cap_the_test_keeps_running(self):
        test = self.make_test(EVEN, max_games=40010)

        self.assertEqual(self.report(test), {})

        test.refresh_from_db()
        self.assertEqual(test.games, 40000)
        self.assertFalse(test.finished)
        self.assertFalse(self.cap_events(test).exists())

    def test_a_cap_of_zero_means_no_cap(self):
        # 52,886 games, like T471 before it was stopped by hand
        test = self.make_test((2643, 5286, 10585, 5286, 2643), max_games=0)
        self.assertEqual(test.games, 52886)

        self.assertEqual(self.report(test), {})

        test.refresh_from_db()
        self.assertEqual(test.games, 52896)
        self.assertFalse(test.finished)
        self.assertFalse(self.cap_events(test).exists())

    # The LLR decides on the update that reaches the cap

    def test_an_llr_that_passes_at_the_cap_keeps_its_verdict(self):
        test = self.make_test(AHEAD, elolower=0.0, eloupper=5.0)

        self.assertEqual(self.report(test), {'stop': True})

        test.refresh_from_db()
        self.assertEqual(test.games, SPRT_MAX_GAMES_STC)
        self.assertGreater(test.currentllr, test.upperllr)
        self.assertTrue(test.finished and test.passed)
        self.assertFalse(test.failed)
        self.assertFalse(self.cap_events(test).exists())

    def test_an_llr_that_fails_at_the_cap_keeps_its_verdict(self):
        test = self.make_test(BEHIND, elolower=0.0, eloupper=5.0)

        self.assertEqual(self.report(test), {'stop': True})

        test.refresh_from_db()
        self.assertEqual(test.games, SPRT_MAX_GAMES_STC)
        self.assertLess(test.currentllr, test.lowerllr)
        self.assertTrue(test.finished and test.failed)
        self.assertFalse(test.passed)
        self.assertFalse(self.cap_events(test).exists())

    # Other modes keep their own rules

    def test_games_tests_still_end_with_their_own_verdict(self):
        test = self.make_test(
            (100, 250, 395, 150, 100), test_mode='GAMES', max_games=2000)

        self.assertEqual(self.report(test), {'stop': True})

        test.refresh_from_db()
        self.assertEqual(test.games, 2000)
        self.assertTrue(test.finished and test.failed) # Fewer wins than losses
        self.assertFalse(test.passed)
        self.assertFalse(self.cap_events(test).exists())

    def test_legacy_datagen_still_passes_at_max_games(self):
        test = self.make_test(
            (100, 200, 395, 200, 100), test_mode='DATAGEN', max_games=2000,
            dev_time_control='', base_time_control='')

        self.assertEqual(self.report(test), {'stop': True})

        test.refresh_from_db()
        self.assertEqual(test.games, 2000)
        self.assertTrue(test.finished and test.passed)
        self.assertFalse(self.cap_events(test).exists())


class SprtGameCapCreationTests(TestCase):

    ## The web form sends "N/A" as Max Games for an SPRT. The server gives the
    ## test the cap of its time control; a GAMES test keeps the number given.

    def setUp(self):
        self.user = User.objects.create_user('cap-author', password='pass')
        Profile.objects.create(user=self.user, enabled=True)

    def create(self, time_control, test_mode='SPRT', max_games='N/A'):
        payload = {
            'book_name': 'spell_openings.epd',
            'upload_pgns': 'FALSE',
            'dev_repo': 'https://github.com/example/engine',
            'dev_engine': 'Spell-Stockfish',
            'dev_options': 'Threads=1 Hash=32',
            'dev_network': '',
            'dev_time_control': time_control,
            'base_repo': 'https://github.com/example/engine',
            'base_engine': 'Spell-Stockfish',
            'base_options': 'Threads=1 Hash=32',
            'base_network': '',
            'base_time_control': time_control,
            'workload_size': '32',
            'priority': '0',
            'throughput': '1000',
            'syzygy_wdl': 'DISABLED',
            'syzygy_adj': 'DISABLED',
            'win_adj': 'None',
            'draw_adj': 'None',
            'test_mode': test_mode,
            'test_bounds': '[0.00, 5.00]',
            'test_confidence': '[0.05, 0.05]',
            'test_max_games': max_games,
        }
        request = RequestFactory().post('/newTest/', payload)
        request.user = self.user
        engine_info = (
            (('https://example.test/dev.zip', 'cap-dev', 'd' * 40, 1234), True),
            (('https://example.test/base.zip', 'cap-base', 'e' * 40, 1234), True),
        )
        with mock.patch.object(
            create_workload, 'verify_workload', return_value=([], engine_info)
        ):
            test, errors = create_workload.create_new_test(request)
        self.assertIsNone(errors)
        return Test.objects.get(id=test.id)

    def test_stc_sprt_tests_get_the_stc_cap(self):
        for time_control in ('8.0+0.08', '10+0.1'):
            with self.subTest(time_control=time_control):
                self.assertEqual(self.create(time_control).max_games, SPRT_MAX_GAMES_STC)

    def test_ltc_sprt_tests_get_the_ltc_cap(self):
        for time_control in ('40.0+0.4', '60+0.6'):
            with self.subTest(time_control=time_control):
                self.assertEqual(self.create(time_control).max_games, SPRT_MAX_GAMES_LTC)

    def test_fixed_nodes_sprt_tests_count_as_stc(self):
        self.assertEqual(self.create('N=25000').max_games, SPRT_MAX_GAMES_STC)

    def test_games_tests_keep_the_max_games_they_were_given(self):
        test = self.create('40.0+0.4', test_mode='GAMES', max_games='2000')
        self.assertEqual(test.max_games, 2000)


class SprtMaxGamesTests(SimpleTestCase):

    def test_ltc_starts_at_twenty_seconds_of_base_time(self):
        expected = {
            '8.0+0.08'     : SPRT_MAX_GAMES_STC, # Our STC preset
            '10.0+0.10'    : SPRT_MAX_GAMES_STC, # Upstream STC default
            '19.9+0.20'    : SPRT_MAX_GAMES_STC,
            '20.0+0.20'    : SPRT_MAX_GAMES_LTC,
            '40.0+0.4'     : SPRT_MAX_GAMES_LTC, # Our LTC preset, as scripts store it
            '40.0+0.40'    : SPRT_MAX_GAMES_LTC, # The same, as the web form stores it
            '60.0+0.60'    : SPRT_MAX_GAMES_LTC, # Upstream LTC default
            '40/10.0+0.10' : SPRT_MAX_GAMES_STC,
            '40/60.0+0.60' : SPRT_MAX_GAMES_LTC,
            'N=25000'      : SPRT_MAX_GAMES_STC,
            'D=12'         : SPRT_MAX_GAMES_STC,
            'MT=1000'      : SPRT_MAX_GAMES_STC,
        }
        for time_control, cap in expected.items():
            with self.subTest(time_control=time_control):
                self.assertEqual(sprt_max_games(time_control), cap)

    def test_the_caps_are_40000_games_at_stc_and_30000_at_ltc(self):
        self.assertEqual((SPRT_MAX_GAMES_STC, SPRT_MAX_GAMES_LTC), (40000, 30000))
