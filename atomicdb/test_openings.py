import copy
import json

from django.core.management import call_command
from django.test import SimpleTestCase

from atomicdb import logic, openings


class AtomicOpeningCatalogTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        openings.clear_catalog_cache()

    @classmethod
    def tearDownClass(cls):
        openings.clear_catalog_cache()
        super().tearDownClass()

    def test_committed_catalog_has_audited_baseline(self):
        catalog = openings.load_catalog()
        self.assertRegex(openings.catalog_sha256(), r'^[0-9a-f]{64}$')
        self.assertEqual(len(catalog), 350)
        records = [
            record for opening in catalog.values()
            for record in opening.records
        ]
        self.assertEqual(len(records), 407)
        self.assertEqual(
            sum(record.source_kind == 'atomix' for record in records),
            176,
        )
        self.assertEqual(
            len({
                record.name for record in records
                if record.source_kind == 'eao'
            }),
            191,
        )
        self.assertTrue(all(
            logic.key_of(opening.fen) == opening.position_key
            for opening in catalog.values()
        ))
        modern_names = {
            record.name for record in records
            if record.source_kind == 'modern'
        }
        self.assertTrue({
            'Vlasov Defence', 'Atomic Attack', 'Atomic Defence',
            'Two Knights Opening', 'Boat', 'Reversed Boat',
            'Fantasy Knight', 'Midnight', 'ZA', 'Yokke',
            'Right Horse', 'Mahiru',
        }.issubset(modern_names))

    def test_lookup_key_is_json_serializable(self):
        match = openings.match_line(['g1f3', 'f7f6', 'b1c3'])
        result = openings.lookup_key(match['position_key'])
        self.assertEqual(result['name'], 'Two Knights Opening')
        self.assertTrue(result['exact'])
        self.assertEqual(result['current_key'], result['position_key'])
        json.dumps(result)

    def test_modern_name_precedence_preserves_legacy_aliases(self):
        match = openings.match_line(['g1f3', 'f7f6', 'b1c3'])
        self.assertIsNotNone(match)
        self.assertEqual(match['name'], 'Two Knights Opening')
        self.assertIn('Two Knights Attack', match['aliases'])
        self.assertIn('King Knight, Two Knights Attack', match['aliases'])
        self.assertEqual(match['matched_ply'], 3)
        self.assertTrue(match['exact'])
        self.assertTrue(match['sources'])
        self.assertTrue(match['evidence'])

        caveman = openings.match_line(['g1h3', 'f7f6', 'e2e3'])
        self.assertEqual(caveman['name'], 'Caveman Attack')
        self.assertIn(
            'TrojanKnight, Old Defence, 2.e3',
            caveman['aliases'],
        )

    def test_position_matching_recognizes_transpositions(self):
        canonical = openings.match_line(['g1f3', 'f7f6', 'b1c3'])
        transposed = openings.match_line(['b1c3', 'f7f6', 'g1f3'])
        self.assertEqual(
            transposed['position_key'],
            canonical['position_key'],
        )
        self.assertEqual(transposed['name'], 'Two Knights Opening')

    def test_line_retains_last_exact_named_position(self):
        match = openings.match_line(
            ['g1f3', 'f7f6', 'b1c3', 'a7a6'])
        self.assertEqual(match['name'], 'Two Knights Opening')
        self.assertEqual(match['matched_ply'], 3)
        self.assertFalse(match['exact'])

    def test_illegal_line_fails_closed(self):
        with self.assertRaises(openings.InvalidOpeningLine):
            openings.match_line(['e2e5'])

    def test_catalog_digest_and_precedence_fail_closed(self):
        payload = json.loads(
            openings.CATALOG_PATH.read_text(encoding='utf-8'))
        corrupt = copy.deepcopy(payload)
        corrupt['entries'][0]['name'] = 'Corrupt'
        with self.assertRaisesRegex(
            openings.OpeningCatalogError, 'digest mismatch',
        ):
            openings.validate_catalog(corrupt, deep=False)

        corrupt['catalog_sha256'] = openings._catalog_digest(corrupt)
        with self.assertRaisesRegex(
            openings.OpeningCatalogError, 'display fields',
        ):
            openings.validate_catalog(corrupt, deep=False)

    def test_unreviewed_equal_precedence_name_conflict_fails_closed(self):
        payload = json.loads(
            openings.CATALOG_PATH.read_text(encoding='utf-8'))
        entry = next(
            candidate for candidate in payload['entries']
            if candidate['name'] == 'Two Knights Opening'
        )
        entry['records'][0]['id'] = 'unreviewed-primary-name'
        payload['catalog_sha256'] = openings._catalog_digest(payload)

        with self.assertRaisesRegex(
            openings.OpeningCatalogError,
            'unresolved equal-precedence opening names',
        ):
            openings.validate_catalog(payload, deep=False)

    def test_source_descriptor_hash_and_count_fail_closed(self):
        payload = json.loads(
            openings.CATALOG_PATH.read_text(encoding='utf-8'))
        payload['sources'][0]['sha256'] = 'not-a-sha'
        payload['catalog_sha256'] = openings._catalog_digest(payload)
        with self.assertRaisesRegex(
            openings.OpeningCatalogError, 'sha256 is not SHA-256',
        ):
            openings.validate_catalog(payload, deep=False)

        payload = json.loads(
            openings.CATALOG_PATH.read_text(encoding='utf-8'))
        payload['sources'][0]['records'] += 1
        payload['catalog_sha256'] = openings._catalog_digest(payload)
        with self.assertRaisesRegex(
            openings.OpeningCatalogError,
            'source descriptor record counts differ',
        ):
            openings.validate_catalog(payload, deep=False)

    def test_management_command_structural_validation(self):
        call_command('validate_atomic_openings', '--no-deep')


class AtomicOpeningDeepValidationTests(SimpleTestCase):

    def test_every_source_line_replays_under_pyffish_atomic(self):
        payload = json.loads(
            openings.CATALOG_PATH.read_text(encoding='utf-8'))
        result = openings.validate_catalog(
            payload, deep=True, require_atomix=True)
        self.assertEqual(result['positions'], 350)
        self.assertEqual(result['source_records'], 407)
