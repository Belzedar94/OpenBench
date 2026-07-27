"""Scale preparation: the stored-PV cap and the inert Postgres wiring (P2)."""

import os
from unittest.mock import patch

from django.test import SimpleTestCase

from . import ingest, logic
from .models import Edge, Position
from .testing import TestCase


class StoredPvCapTests(SimpleTestCase):

    def test_a_long_non_mate_pv_is_truncated(self):
        pv = [f'a{i % 8 + 1}a{(i + 1) % 8 + 1}' for i in range(60)]
        stored = ingest.capped_analysis([{'move': pv[0], 'eval_cp': 40,
                                          'mate': None, 'pv': pv}])
        self.assertEqual(len(stored[0]['pv']), ingest.STORED_PV_MAX_PLIES)
        self.assertTrue(stored[0]['pv_truncated'])

    def test_a_mate_line_is_kept_whole(self):
        """A mate PV is EVIDENCE: it gets re-verified move by move."""
        pv = [f'a{i % 8 + 1}a{(i + 1) % 8 + 1}' for i in range(60)]
        stored = ingest.capped_analysis([{'move': pv[0], 'eval_cp': 9999,
                                          'mate': 30, 'pv': pv}])
        self.assertEqual(len(stored[0]['pv']), 60)
        self.assertNotIn('pv_truncated', stored[0])

    def test_a_short_pv_is_untouched(self):
        line = {'move': 'e2e4', 'eval_cp': 20, 'mate': None,
                'pv': ['e2e4', 'e7e5']}
        self.assertEqual(ingest.capped_analysis([line]), [line])

    def test_non_dict_lines_are_dropped(self):
        self.assertEqual(ingest.capped_analysis(['nonsense', None]), [])


class IngestStoresCappedAnalysisTests(TestCase):

    def test_the_cap_applies_to_what_is_persisted(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        long_pv = ['e2e4'] + [f'a{i % 8 + 1}a{(i + 1) % 8 + 1}'
                              for i in range(60)]

        ingest.ingest_analysis(pos.key, [{
            'move': 'e2e4', 'eval_cp': 25, 'mate': None, 'pv': long_pv,
        }], nodes_budget=1_000)

        pos.refresh_from_db()
        self.assertEqual(len(pos.last_analysis[0]['pv']),
                         ingest.STORED_PV_MAX_PLIES)

    def test_the_child_still_gets_its_eval_from_the_full_line(self):
        """Trimming happens when PERSISTING, never when reasoning."""
        pos = ingest.get_or_create_position(logic.start_fen())
        long_pv = ['e2e4'] + ['a2a3'] * 60

        ingest.ingest_analysis(pos.key, [{
            'move': 'e2e4', 'eval_cp': 133, 'mate': None, 'pv': long_pv,
        }], nodes_budget=1_000)

        child = Edge.objects.get(parent=pos, move_uci='e2e4').child
        self.assertEqual(child.eval_cp, 133)


class PostgresWiringTests(SimpleTestCase):
    """The alias support must be completely inert without the environment."""

    def test_the_current_deployment_is_unaffected(self):
        from django.conf import settings
        self.assertEqual(settings.ATOMICDB_DATABASE_ALIAS,
                         'atomicdb' if 'atomicdb' in settings.DATABASES
                         else 'default')

    def test_settings_read_the_atomicdb_specific_variables(self):
        """Import the module fresh with the variables set."""
        import importlib
        import sys

        env = {
            'OPENBENCH_ATOMICDB_POSTGRES_DB': 'atomicdb_test',
            'OPENBENCH_ATOMICDB_POSTGRES_USER': 'proofs',
            'OPENBENCH_ATOMICDB_POSTGRES_HOST': '10.0.0.9',
            'OPENBENCH_ATOMICDB_POSTGRES_PORT': '6543',
        }
        with patch.dict(os.environ, env, clear=False):
            module = importlib.reload(sys.modules['OpenSite.settings'])
            try:
                config = module.DATABASES['atomicdb']
                self.assertEqual(config['ENGINE'],
                                 'django.db.backends.postgresql')
                self.assertEqual(config['NAME'], 'atomicdb_test')
                self.assertEqual(config['USER'], 'proofs')
                self.assertEqual(config['HOST'], '10.0.0.9')
                self.assertEqual(config['PORT'], '6543')
                self.assertEqual(module.ATOMICDB_DATABASE_ALIAS, 'atomicdb')
            finally:
                importlib.reload(sys.modules['OpenSite.settings'])

    def test_the_runbook_exists_and_names_its_triggers(self):
        from django.conf import settings
        path = os.path.join(settings.BASE_DIR, 'Documentation',
                            'postgres-migration.md')
        text = open(path, encoding='utf-8').read()
        self.assertIn('OPENBENCH_ATOMICDB_POSTGRES_DB', text)
        self.assertIn('Rollback', text)
        self.assertIn('BIGSERIAL', text)
        self.assertIn('verify_certificate --all', text)


class LegalMoveInventoryDesignTests(SimpleTestCase):
    """The design is a comment on purpose; this pins that it is still there."""

    def test_the_design_is_documented_next_to_the_models(self):
        import inspect

        from . import models
        source = inspect.getsource(models)
        self.assertIn('legal_move_inventory', source)
        self.assertIn('move_set_sha256', source)
        self.assertIn('packed_moves', source)

    def test_no_inventory_model_was_created_yet(self):
        from . import models
        self.assertFalse(hasattr(models, 'LegalMoveInventory'))
