import hashlib
import json
from pathlib import Path
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
HORDE_ENGINE = 'Horde-Stockfish'
HORDE_BASELINE = 'Fairy-Stockfish-Hordetest-Baseline'
HORDE_BOOK = 'HORDE_openings.epd'
HORDE_BOOK_V2 = 'HORDE_openings_v2.epd'
HORDE_BOOK_V3_INTERIM = 'HORDE_openings_v3_interim.epd'
PLAY_REF = 'cee98c4d2f41295378c9cc02a9fb5153ae956d73'
BASELINE_REF = 'fd044be239564a489056e358d157a4064f0b01a0'
DATAGEN_REF = 'f176a518166b7c27632a211127148c8e361b3844'
BOOK_ARTIFACT_REF = 'cd0560081f6433b58a8aa8d0c3fd4a91e969f1dd'
BOOK_SHA256 = '93e97b27d5df054b8a649b8be92a0a8b058384dae35bad142f9a610896eb6958'
BOOK_V2_ARTIFACT_REF = 'ca1028edabd2f172b17adc951b9582a86d49e8e2'
BOOK_V2_SHA256 = '05753975c2baf80e0908988186113d2b72c7eb781b9ff628a7e1d6e945d4ff99'
BOOK_V3_INTERIM_ARTIFACT_REF = '3d81c4fdef2115458cb697cd7f9d7f30fc56b47b'
BOOK_V3_INTERIM_SHA256 = '39f113beb9fda02531a614e0cd766893cb89cb0706df81c58f4a1637ee0fc814'


def load_json(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


class HordeOnboardingTests(unittest.TestCase):

    def setUp(self):
        self.general = load_json('Config/config.json')
        self.engine = load_json('Engines/%s.json' % HORDE_ENGINE)
        self.baseline = load_json('Engines/%s.json' % HORDE_BASELINE)
        self.book = load_json('Books/%s.json' % HORDE_BOOK)
        self.book_v2 = load_json('Books/%s.json' % HORDE_BOOK_V2)
        self.book_v3_interim = load_json(
            'Books/%s.json' % HORDE_BOOK_V3_INTERIM
        )

    def test_active_client_is_pinned_to_an_immutable_commit(self):
        # The pin itself lives only in ``Config/config.json``; duplicating the
        # ref in the test suite is what made it drift. Assert the active
        # protocol and an immutable 40-digit commit, never a branch name.
        self.assertEqual(self.general['client_version'], 49)
        self.assertRegex(self.general['client_repo_ref'], r'^[0-9a-f]{40}$')

    def test_specialist_onboarding_is_schedulable(self):
        self.assertIn(HORDE_ENGINE, self.general['engines'])
        self.assertNotIn(HORDE_BASELINE, self.general['engines'])
        self.assertIn(HORDE_BOOK, self.general['books'])
        self.assertIn(HORDE_BOOK_V2, self.general['books'])
        self.assertIn(HORDE_BOOK_V3_INTERIM, self.general['books'])
        self.assertTrue(self.engine['onboarding_ready'])
        self.assertFalse(self.baseline['onboarding_ready'])
        self.assertTrue(self.book['onboarding_ready'])
        self.assertEqual(self.book['sha'], BOOK_SHA256)
        self.assertEqual(self.book['raw_sha'], BOOK_SHA256)
        self.assertEqual(
            self.book['source'],
            'https://raw.githubusercontent.com/Belzedar94/OpenBench/'
            + BOOK_ARTIFACT_REF
            + '/Books/HORDE_openings.epd.zip',
        )
        self.assertTrue(self.book_v2['onboarding_ready'])
        self.assertFalse(self.book_v2['datagen_enabled'])
        self.assertEqual(self.book_v2['sha'], BOOK_V2_SHA256)
        self.assertEqual(self.book_v2['raw_sha'], BOOK_V2_SHA256)
        self.assertEqual(
            self.book_v2['source'],
            'https://raw.githubusercontent.com/Belzedar94/OpenBench/'
            + BOOK_V2_ARTIFACT_REF
            + '/Books/HORDE_openings_v2.epd.zip',
        )
        self.assertTrue(self.book_v3_interim['onboarding_ready'])
        self.assertFalse(self.book_v3_interim['datagen_enabled'])
        self.assertEqual(self.book_v3_interim['sha'], BOOK_V3_INTERIM_SHA256)
        self.assertEqual(
            self.book_v3_interim['raw_sha'], BOOK_V3_INTERIM_SHA256
        )
        self.assertEqual(
            self.book_v3_interim['source'],
            'https://raw.githubusercontent.com/Belzedar94/OpenBench/'
            + BOOK_V3_INTERIM_ARTIFACT_REF
            + '/Books/HORDE_openings_v3_interim.epd.zip',
        )

    def test_horde_book_archive_is_exact_and_single_file(self):
        archive = ROOT / 'Books' / 'HORDE_openings.epd.zip'
        self.assertEqual(
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            '556b8bd549dc22797238268f9737c6a1f074a82f4cae2f4d62005ffbac6ccd18',
        )
        with zipfile.ZipFile(archive) as container:
            self.assertEqual(container.namelist(), ['HORDE_openings.epd'])
            payload = container.read('HORDE_openings.epd')
        self.assertEqual(len(payload), 196008)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), BOOK_SHA256)
        lines = payload.decode('ascii').splitlines()
        self.assertEqual(len(lines), 2486)
        self.assertEqual(len(set(lines)), 1431)

    def test_horde_book_v2_archive_is_exact_and_single_file(self):
        archive = ROOT / 'Books' / 'HORDE_openings_v2.epd.zip'
        self.assertEqual(
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            'dc085a2f4385713ee360446ffc2a067a37905bbb3de4e2ee2acf9a7d26b86253',
        )
        with zipfile.ZipFile(archive) as container:
            self.assertEqual(container.namelist(), ['HORDE_openings_v2.epd'])
            payload = container.read('HORDE_openings_v2.epd')
        self.assertEqual(len(payload), 436111)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), BOOK_V2_SHA256)
        lines = payload.decode('ascii').splitlines()
        self.assertEqual(len(lines), 5608)
        self.assertEqual(len(set(lines)), 5608)

    def test_horde_book_v3_interim_archive_is_exact_and_single_file(self):
        archive = ROOT / 'Books' / 'HORDE_openings_v3_interim.epd.zip'
        self.assertEqual(
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            '19bcbcdd8e99af52c9e10e4762ff2196aa555680b8524c26f3d179b059407706',
        )
        with zipfile.ZipFile(archive) as container:
            self.assertEqual(
                container.namelist(), ['HORDE_openings_v3_interim.epd']
            )
            payload = container.read('HORDE_openings_v3_interim.epd')
        self.assertEqual(len(payload), 117921)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(), BOOK_V3_INTERIM_SHA256
        )
        lines = payload.decode('ascii').splitlines()
        self.assertEqual(len(lines), 1508)
        self.assertEqual(len(set(lines)), 1508)

    def test_engine_contracts_are_native_and_role_separated(self):
        self.assertFalse(self.engine['private'])
        self.assertTrue(self.baseline['private'])
        self.assertEqual(self.engine['variant_contract'], 'LICHESS_HORDE_V1')
        self.assertEqual(self.baseline['variant_contract'], 'LICHESS_HORDE_V1')
        self.assertEqual(self.book['variant_contract'], 'LICHESS_HORDE_V1')
        self.assertEqual(self.book_v2['variant_contract'], 'LICHESS_HORDE_V1')
        self.assertEqual(
            self.book_v3_interim['variant_contract'], 'LICHESS_HORDE_V1'
        )
        self.assertEqual(self.engine['build']['cpuflags'], [])
        self.assertEqual(self.baseline['build']['cpuflags'], [])
        self.assertEqual(self.engine['build']['path'], 'src')
        self.assertEqual(self.engine['build']['compilers'], ['g++'])
        self.assertEqual(
            self.baseline['build']['artifact_roles'], ['play']
        )
        self.assertEqual(
            self.baseline['source'],
            'https://github.com/Belzedar94/Horde-Stockfish',
        )
        self.assertEqual(
            self.baseline['test_presets']['default']['base_branch'],
            BASELINE_REF,
        )
        self.assertEqual(
            self.engine['test_presets']['default']['base_branch'], PLAY_REF
        )
        self.assertEqual(
            self.engine['test_presets']['default']['both_bench'], 315576
        )
        self.assertEqual(
            self.baseline['test_presets']['default']['both_bench'], 130284
        )
        self.assertEqual(self.engine['nps'], 1488566)
        self.assertEqual(self.baseline['nps'], 527465)

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
                self.assertIn('UCI_Variant=hordetest', baseline['both_options'])

    def test_horde_datagen_canary_is_frozen_without_atomic_filters(self):
        preset = self.engine['datagen_presets']['default']
        command = preset['datagen_command']
        self.assertEqual(preset['dev_branch'], DATAGEN_REF)
        self.assertEqual(preset['both_bench'], 440088)
        self.assertEqual(
            preset['both_network'], 'hordetest_run6b_e37_l06.nnue'
        )
        self.assertEqual(preset['book_name'], HORDE_BOOK)
        self.assertEqual(preset['datagen_total_count'], 500000)
        self.assertEqual(preset['datagen_positions_per_chunk'], 250000)
        self.assertEqual(preset['datagen_base_seed'], 202608060000000)
        self.assertEqual(preset['datagen_publication_protocol'], '41')
        self.assertEqual(
            preset['datagen_campaign_id'],
            'horde-v1-run6b-canary-20260806',
        )
        self.assertEqual(
            preset['datagen_external_workload_id'],
            'horde-v1-run6b-g0-canary',
        )
        self.assertEqual(preset['datagen_role'], 'g0-canary')
        self.assertEqual(preset['datagen_cohort'], 'run6b-d6')
        self.assertNotIn('priority', preset)
        for placeholder in (
            '{THREADS}',
            '{NETWORK}',
            '{NETWORK_SHA256}',
            '{PRODUCER_SHA256}',
            '{COUNT}',
            '{SEED}',
            '{BOOK}',
            '{BOOK_SHA256}',
            '{OUT}',
        ):
            self.assertIn(placeholder, command)
        for atomic_setting in (
            'filter_captures',
            'filter_checks',
            'filter_promotions',
            'adjudicate_draws_by_insufficient_material',
            'teacher_mode',
            'syzygy',
        ):
            self.assertNotIn(atomic_setting, command.lower())

    def test_cross_engine_form_restores_baseline_bench_and_network(self):
        source = (
            ROOT / 'OpenBench' / 'static' / 'create_workload.js'
        ).read_text(encoding='utf-8')
        self.assertIn("set_option('base_bench', base_bench);", source)
        self.assertIn("set_option('base_network', base_network);", source)

    def test_variant_contract_has_a_persistent_workload_field(self):
        model_source = (ROOT / 'OpenBench' / 'models.py').read_text()
        migration_source = (
            ROOT / 'OpenBench' / 'migrations' / '0010_test_variant_contract.py'
        ).read_text()
        self.assertIn('variant_contract = CharField', model_source)
        self.assertIn("name='variant_contract'", migration_source)

    def test_bundled_cutechess_has_native_horde_on_both_platforms(self):
        expected = {
            'cutechess-ob.exe': (
                '1c0bbab69e15a277c0b68bf032848b513f706749999cd5f6d09a1fb60f05b8a6',
                b'MZ',
            ),
            'cutechess-ob': (
                '38f757ce9a735189e89305e5590320d0ae161c74092d1851e2049fff212c4485',
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
