import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HORDE_ENGINE = 'Horde-Stockfish'
HORDE_BASELINE = 'Fairy-Stockfish-Hordetest-Baseline'
HORDE_BOOK = 'HORDE_openings.epd'
CLIENT_REF = '32143bd83dcb65b2e33ae2e2a52afc7b0b0d1cb1'


def load_json(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


class HordeOnboardingTests(unittest.TestCase):

    def setUp(self):
        self.general = load_json('Config/config.json')
        self.engine = load_json('Engines/%s.json' % HORDE_ENGINE)
        self.baseline = load_json('Engines/%s.json' % HORDE_BASELINE)
        self.book = load_json('Books/%s.json' % HORDE_BOOK)

    def test_client_v44_is_pinned_to_an_immutable_commit(self):
        self.assertEqual(self.general['client_version'], 44)
        self.assertRegex(self.general['client_repo_ref'], r'^[0-9a-f]{40}$')
        self.assertEqual(self.general['client_repo_ref'], CLIENT_REF)

    def test_incomplete_scaffolds_are_not_schedulable(self):
        self.assertNotIn(HORDE_ENGINE, self.general['engines'])
        self.assertNotIn(HORDE_BASELINE, self.general['engines'])
        self.assertNotIn(HORDE_BOOK, self.general['books'])
        self.assertFalse(self.engine['onboarding_ready'])
        self.assertFalse(self.baseline['onboarding_ready'])
        self.assertFalse(self.book['onboarding_ready'])
        self.assertTrue(self.book['sha'].startswith('PENDING_'))
        self.assertTrue(self.book['raw_sha'].startswith('PENDING_'))
        self.assertTrue(self.book['source'].startswith('PENDING_'))

    def test_private_engine_contracts_are_native_and_role_separated(self):
        self.assertTrue(self.engine['private'])
        self.assertTrue(self.baseline['private'])
        self.assertEqual(self.engine['variant_contract'], 'horde')
        self.assertEqual(self.baseline['variant_contract'], 'horde')
        self.assertEqual(self.engine['build']['cpuflags'], [])
        self.assertEqual(self.baseline['build']['cpuflags'], [])
        self.assertEqual(
            self.engine['build']['artifact_roles'], ['play', 'datagen']
        )
        self.assertEqual(
            self.baseline['build']['artifact_roles'], ['play']
        )

    def test_foundational_presets_are_fixed_games_at_three_time_controls(self):
        expected = {
            'Foundational VSTC': ('2.0+0.02', 400, 'Hash=16'),
            'Foundational STC': ('10.0+0.1', 300, 'Hash=32'),
            'Foundational LTC': ('30.0+0.3', 200, 'Hash=128'),
        }
        for name, (time_control, games, hash_option) in expected.items():
            with self.subTest(name=name):
                specialist = self.engine['test_presets'][name]
                baseline = self.baseline['test_presets'][name]
                self.assertEqual(specialist['both_time_control'], time_control)
                self.assertEqual(specialist['test_max_games'], games)
                self.assertIn('Threads=1', specialist['both_options'])
                self.assertIn(hash_option, specialist['both_options'])
                self.assertIn('UCI_Variant=horde', baseline['both_options'])

    def test_cross_engine_form_restores_baseline_bench_and_network(self):
        source = (
            ROOT / 'OpenBench' / 'static' / 'create_workload.js'
        ).read_text(encoding='utf-8')
        self.assertIn("set_option('base_bench', base_bench);", source)
        self.assertIn("set_option('base_network', base_network);", source)

    def test_bundled_cutechess_has_native_horde_on_both_platforms(self):
        expected = {
            'cutechess-ob.exe': (
                '4ea492b8e6459e3150f41b5d5a6e9cf472b3d58556d9807373ab85812fcda21f',
                b'MZ',
            ),
            'cutechess-ob': (
                'dd79fdb0905961b901fb6b2302c9387fae0f67df77278e494e079f3f9c02e825',
                b'\x7fELF',
            ),
        }
        needle = b"'horde': Horde Chess (v2)"
        for name, (digest, magic) in expected.items():
            with self.subTest(name=name):
                payload = (ROOT / 'Client' / name).read_bytes()
                self.assertTrue(payload.startswith(magic))
                self.assertIn(needle, payload)
                self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
