import json
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase

import OpenBench.live_elo
from OpenBench.models import Engine, Profile, Test as Workload


def engine(name):
    return Engine.objects.create(
        name=name, source='https://example.invalid/%s' % (name),
        sha='0' * 40, bench=1234)


def workload(**kwargs):
    defaults = dict(
        author='belzedar', book_name='SPELL_8moves_v3.epd',
        dev=engine(kwargs.pop('dev_name', 'dev')),
        dev_repo='https://example.invalid', dev_engine='Spell-Stockfish',
        dev_options='Threads=1 Hash=16', dev_time_control='10.0+0.10',
        base=engine('base'),
        base_repo='https://example.invalid', base_engine='Spell-Stockfish',
        base_options='Threads=1 Hash=16', base_time_control='10.0+0.10',
    )
    defaults.update(kwargs)
    return Workload.objects.create(**defaults)


def sprt(**kwargs):
    defaults = dict(
        test_mode='SPRT', elolower=0.00, eloupper=3.00,
        lowerllr=-2.94, upperllr=2.94, currentllr=1.37,
        LL=40, LD=1800, DD=4700, DW=1900, WW=45,
        games=16970, wins=1990, losses=1880, draws=13100)
    defaults.update(kwargs)
    return workload(**defaults)


class LiveEloPayloadTests(TestCase):

    def test_sprt_reports_the_llr_and_its_bounds(self):
        payload = OpenBench.live_elo.live_elo_payload(sprt())

        self.assertEqual(payload['mode'], 'SPRT')
        self.assertEqual(payload['llr'], 1.37)
        self.assertEqual(payload['llr_lower'], -2.94)
        self.assertEqual(payload['llr_upper'], 2.94)
        self.assertEqual(payload['games'], 16970)
        self.assertEqual(payload['penta'], [40, 1800, 4700, 1900, 45])
        self.assertFalse(payload['finished'])

    def test_fixed_games_has_no_hypothesis_to_report(self):
        payload = OpenBench.live_elo.live_elo_payload(
            sprt(test_mode='GAMES', max_games=40000))

        self.assertIsNone(payload['llr'])
        self.assertIsNone(payload['llr_lower'])
        self.assertIsNone(payload['llr_upper'])
        self.assertNotIn('LLR', payload['summary'])
        self.assertEqual(payload['max_games'], 40000)

    def test_the_elo_dial_contains_the_interval_and_zero(self):
        payload = OpenBench.live_elo.live_elo_payload(sprt())

        self.assertLess(payload['elo_axis_lower'], payload['elo_lower'])
        self.assertGreater(payload['elo_axis_upper'], payload['elo_upper'])
        self.assertLess(payload['elo_axis_lower'], 0)
        self.assertGreater(payload['elo_axis_upper'], 0)

        self.assertLessEqual(payload['elo_lower'], payload['elo'])
        self.assertLessEqual(payload['elo'], payload['elo_upper'])

    def test_the_axis_still_has_room_for_a_decided_test(self):
        # An interval far from zero must not pin the needle to the rim.
        lower, upper = OpenBench.live_elo.elo_axis(18.0, 24.0)
        self.assertLessEqual(lower, -2.0)
        self.assertGreaterEqual(upper, 25.0)

    def test_los_is_reported_as_a_percentage(self):
        payload = OpenBench.live_elo.live_elo_payload(sprt())

        self.assertGreater(payload['los'], 90.0)
        self.assertLessEqual(payload['los'], 100.0)
        self.assertIn('LOS: %0.1f%%' % (payload['los']), payload['summary'])

    def test_a_trinomial_only_workload_is_not_read_as_five_zeroes(self):
        # Tests that predate the pentanomial switch keep their (L, D, W) and
        # nothing else. Elo() over five zeroes answers 0.00 for a test that
        # played thousands of games.
        legacy = sprt(LL=0, LD=0, DD=0, DW=0, WW=0,
                      games=1000, wins=400, losses=300, draws=300)

        self.assertEqual(
            OpenBench.live_elo.live_elo_results(legacy), (300, 300, 400))
        self.assertGreater(
            OpenBench.live_elo.live_elo_payload(legacy)['elo'], 0.0)

    def test_only_gameplay_workloads_report_an_elo(self):
        self.assertTrue(OpenBench.live_elo.has_live_elo(sprt()))
        self.assertTrue(
            OpenBench.live_elo.has_live_elo(sprt(test_mode='GAMES')))
        self.assertFalse(
            OpenBench.live_elo.has_live_elo(sprt(test_mode='SPSA')))
        self.assertFalse(
            OpenBench.live_elo.has_live_elo(sprt(test_mode='DATAGEN')))


class LiveEloEndpointTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_the_endpoint_answers_the_reading_of_a_test(self):
        test = sprt()

        response = self.client.get('/api/liveElo/%d/' % (test.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')

        payload = json.loads(response.content)
        self.assertEqual(payload['id'], test.id)
        self.assertEqual(payload['llr'], 1.37)
        self.assertEqual(payload['games'], 16970)

    def test_a_tune_has_no_elo_endpoint(self):
        tune = sprt(test_mode='SPSA')
        response = self.client.get('/api/liveElo/%d/' % (tune.id))
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_workload_is_not_a_server_error(self):
        response = self.client.get('/api/liveElo/424242/')
        self.assertEqual(response.status_code, 404)
        self.assertIn('error', json.loads(response.content))


class LiveEloPageTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_the_test_page_ships_the_dials_and_their_numbers(self):
        test = sprt()

        page = self.client.get('/test/%d/' % (test.id)).content.decode()

        self.assertIn('id="live-elo"', page)
        self.assertIn('data-endpoint="/api/liveElo/%d/"' % (test.id), page)
        self.assertIn('data-gauge="llr"', page)
        self.assertIn('data-gauge="los"', page)
        self.assertIn('data-gauge="elo"', page)
        self.assertIn('live_elo.js', page)

        # Still live, so the page asks to be kept up to date
        self.assertIn('data-live="1"', page)

    def test_a_decided_test_does_not_ask_to_be_polled(self):
        test = sprt(finished=True, passed=True)
        page = self.client.get('/test/%d/' % (test.id)).content.decode()
        self.assertIn('data-live="0"', page)

    def test_a_fixed_games_test_has_no_llr_dial(self):
        test = sprt(test_mode='GAMES', max_games=40000)
        page = self.client.get('/test/%d/' % (test.id)).content.decode()

        self.assertIn('data-gauge="elo"', page)
        self.assertIn('data-gauge="los"', page)
        self.assertNotIn('data-gauge="llr"', page)

    def test_a_tune_page_is_left_alone(self):
        tune = sprt(
            test_mode='SPSA', games=1024,
            spsa={'Alpha': 0.602, 'Gamma': 0.101, 'A_ratio': 0.1, 'A': 10,
                  'reporting_type': 'BULK', 'distribution_type': 'SINGLE',
                  'iterations': 100, 'pairs_per': 8, 'parameters': {}})

        page = self.client.get('/tune/%d/' % (tune.id)).content.decode()
        self.assertNotIn('id="live-elo"', page)
        self.assertNotIn('live_elo.js', page)


class LiveEloContractTests(TestCase):

    static = Path(settings.BASE_DIR) / 'OpenBench' / 'static' / 'live_elo.js'

    def test_the_script_reads_only_fields_the_payload_carries(self):
        # The dials are drawn from the page's data-* attributes on load and
        # from the endpoint afterwards. If the two shapes drift, the first
        # refresh silently blanks a needle.
        source = self.static.read_text(encoding='utf-8')
        payload = OpenBench.live_elo.live_elo_payload(sprt())

        for field in re.findall(r'data\.([a-z_]+)', source):
            if field == 'error':  # the endpoint's failure shape, not a reading
                continue
            with self.subTest(field=field):
                self.assertIn(field, payload)

        # scipy hands back numpy scalars, which serialize by accident today
        # (numpy.float64 subclasses float) and would stop the day they do not.
        for key in ['elo', 'elo_lower', 'elo_upper', 'los']:
            self.assertIs(type(payload[key]), float, key)

    def test_the_page_stops_polling_once_the_test_is_decided(self):
        source = self.static.read_text(encoding='utf-8')
        self.assertIn('data.finished || data.passed || data.failed', source)

    def test_the_index_result_box_links_to_its_test(self):
        summary = (
            Path(settings.BASE_DIR) / 'Templates' / 'OpenBench' / 'Blocks'
            / 'testsummary.html'
        ).read_text(encoding='utf-8')

        self.assertIn(
            '<a class="statblock-link" href="{{test|workload_url}}">', summary)

    def test_the_result_box_click_target_covers_the_whole_box(self):
        style = (
            Path(settings.BASE_DIR) / 'OpenBench' / 'static' / 'style.css'
        ).read_text(encoding='utf-8')

        # The padding lives on the anchor, not on the cell, or the visitor is
        # aiming at a box whose border is not a link.
        block = style[style.index('#content .statblock-link {'):]
        block = block[:block.index('}')]
        self.assertIn('display: block', block)
        self.assertIn('padding: 7px 10px 4px', block)
        self.assertIn('padding: 0', style[style.index('#content td.statblock {'):])
