"""The planner's count of distinct parents in atomicdb_edge (migration 0065).

On PostgreSQL the migration pins n_distinct(parent_id) to a fraction of the
rows, so the climb's read of the children of many parents is planned for the
handful of rows it returns, not for a thousand per parent.  The tests marked
so run against it in the PostgreSQL CI job.
"""

import importlib
from unittest import skipUnless

from django.db import connections

from .database import connection, current_alias
from .testing import TestCase

MIGRATION = importlib.import_module(
    'atomicdb.migrations.0065_edge_parent_ndistinct')
POSTGRES = connection.vendor == 'postgresql'


def _attoptions():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT attoptions FROM pg_attribute "
            "WHERE attrelid = 'atomicdb_edge'::regclass "
            "AND attname = 'parent_id'")
        row = cursor.fetchone()
    return list(row[0] or ()) if row else []


class _SQLiteEditor:
    class connection:
        vendor = 'sqlite'

    def execute(self, sql):
        raise AssertionError(f'nothing to run on SQLite: {sql}')


class EdgeParentDistinctTests(TestCase):

    def test_the_pin_is_a_fraction_of_the_rows(self):
        # Negative, so it follows the table's size; five edges a parent,
        # where the sample of 12-Nov found 4.8.
        self.assertLess(MIGRATION.N_DISTINCT, 0)
        self.assertGreaterEqual(MIGRATION.N_DISTINCT, -1)
        self.assertAlmostEqual(-1 / MIGRATION.N_DISTINCT, 5)

    def test_sqlite_is_left_alone(self):
        MIGRATION.pin(None, _SQLiteEditor())
        MIGRATION.unpin(None, _SQLiteEditor())

    @skipUnless(POSTGRES, 'n_distinct is a PostgreSQL statistic')
    def test_the_migrated_database_carries_the_pin(self):
        self.assertIn(f'n_distinct={MIGRATION.N_DISTINCT}', _attoptions())

    @skipUnless(POSTGRES, 'n_distinct is a PostgreSQL statistic')
    def test_reversing_takes_it_off_and_reapplying_puts_it_back(self):
        editor = connections[current_alias()].schema_editor
        with editor(atomic=False) as schema_editor:
            MIGRATION.unpin(None, schema_editor)
        self.assertFalse([option for option in _attoptions()
                          if option.startswith('n_distinct=')])
        with editor(atomic=False) as schema_editor:
            MIGRATION.pin(None, schema_editor)
        self.assertIn(f'n_distinct={MIGRATION.N_DISTINCT}', _attoptions())
