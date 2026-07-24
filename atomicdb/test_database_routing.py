import ast
from pathlib import Path
import sqlite3
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connections, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase

from OpenSite.db_routers import AtomicDBRouter

from .database import DATABASE_ALIAS, atomic, connection
from .management.commands._atomicdb_sqlite import (
    current_atomicdb_tables,
    schema_snapshot,
    validate_migrations,
)
from .models import Position
from .testing import TransactionTestCase


class AtomicDBRouterTests(SimpleTestCase):

    def test_atomic_models_use_selected_alias(self):
        router = AtomicDBRouter()
        self.assertEqual(router.db_for_read(Position), DATABASE_ALIAS)
        self.assertEqual(router.db_for_write(Position), DATABASE_ALIAS)
        self.assertEqual(
            router.allow_migrate(DATABASE_ALIAS, 'atomicdb'), True)

    def test_non_atomic_models_cannot_migrate_to_split_alias(self):
        if DATABASE_ALIAS == 'default':
            self.skipTest('AtomicDB split alias is not active')
        router = AtomicDBRouter()
        self.assertEqual(
            router.allow_migrate(DATABASE_ALIAS, 'auth'), False)
        self.assertEqual(
            router.allow_relation(Position(), get_user_model()()), False)

    def test_split_keeps_default_atomic_schema_as_migration_shadow(self):
        router = AtomicDBRouter()
        with mock.patch.object(
                settings, 'ATOMICDB_DATABASE_ALIAS', 'atomicdb'):
            self.assertTrue(
                router.allow_migrate('default', 'atomicdb'))
            self.assertTrue(
                router.allow_migrate('atomicdb', 'atomicdb'))
            self.assertFalse(
                router.allow_migrate('atomicdb', 'auth'))

        with mock.patch.object(
                settings, 'ATOMICDB_DATABASE_ALIAS', 'default'):
            self.assertTrue(
                router.allow_migrate('default', 'atomicdb'))
            self.assertFalse(
                router.allow_migrate('atomicdb', 'atomicdb'))


class AtomicTransactionBoundaryTests(TransactionTestCase):

    def test_atomic_helper_opens_the_selected_connection(self):
        self.assertFalse(connection.in_atomic_block)
        with atomic():
            self.assertTrue(connection.in_atomic_block)
            if DATABASE_ALIAS != 'default':
                self.assertFalse(connections['default'].in_atomic_block)
        self.assertFalse(connection.in_atomic_block)


@skipUnless(
    DATABASE_ALIAS == 'atomicdb'
    and settings.DATABASES['default']['ENGINE']
    == 'django.db.backends.sqlite3'
    and settings.DATABASES['atomicdb']['ENGINE']
    == 'django.db.backends.sqlite3',
    'requires two separate SQLite test databases',
)
class SeparateSQLiteLockIsolationTests(TransactionTestCase):

    @staticmethod
    def _raw_lock(alias):
        db_path = connections[alias].settings_dict['NAME']
        db_name = str(db_path)
        raw = sqlite3.connect(
            db_name,
            timeout=0.2,
            uri=db_name.startswith('file:'),
        )
        raw.execute('PRAGMA busy_timeout = 200')
        raw.execute('BEGIN IMMEDIATE')
        return raw

    def test_default_writer_lock_does_not_block_atomicdb_write(self):
        lock = self._raw_lock('default')
        try:
            Position.objects.create(
                key='a' * 64,
                fen='8/8/8/8/8/8/8/K6k w - - 0 1',
            )
        finally:
            lock.rollback()
            lock.close()

    def test_atomicdb_writer_lock_does_not_block_default_write(self):
        lock = self._raw_lock('atomicdb')
        try:
            get_user_model().objects.create_user(
                username='lock-isolation-user',
                password='unused',
            )
        finally:
            lock.rollback()
            lock.close()


@skipUnless(
    DATABASE_ALIAS == 'atomicdb'
    and settings.DATABASES['default']['ENGINE']
    == 'django.db.backends.sqlite3'
    and settings.DATABASES['atomicdb']['ENGINE']
    == 'django.db.backends.sqlite3',
    'requires two separate SQLite test databases',
)
class ShadowMigrationParityTests(TransactionTestCase):

    @staticmethod
    def _apply_probe(alias):
        connection = connections[alias]
        migration = migrations.Migration(
            '0014_shadow_probe', 'atomicdb')
        migration.operations = [
            migrations.RunSQL(
                """
                CREATE INDEX atomicdb_shadow_probe
                ON atomicdb_position(eval_cp)
                """,
                reverse_sql='DROP INDEX atomicdb_shadow_probe',
            ),
        ]
        state = MigrationExecutor(connection).loader.project_state()
        with connection.schema_editor() as schema_editor:
            migration.apply(state, schema_editor)
        MigrationRecorder(connection).record_applied(
            'atomicdb', '0014_shadow_probe')

    def test_interrupted_future_migration_is_recoverable_on_both_aliases(self):
        known_names = (
            '0001_initial',
            '0002_position_won_line',
            '0003_position_last_analysis',
            '0004_remove_position_is_wall',
            '0005_analysistask_source_requestlog',
            '0006_workerping_workerping_uniq_worker_machine',
            '0007_position_time_invested',
            '0008_position_mate_in',
            '0009_analysistask_nodes_searched',
            '0010_position_proof',
            '0011_worker_metrics',
            '0012_analysistask_lease_session',
            '0013_progresssnapshot',
            '0014_shadow_probe',
        )
        self._apply_probe('default')
        with mock.patch(
                'atomicdb.management.commands._atomicdb_sqlite.'
                'atomicdb_migration_names',
                return_value=known_names):
            # Deploy interrupted after shadow migration: default is current,
            # active is still a valid prefix and no restart is allowed yet.
            validate_migrations(
                connections['default'].connection,
                allow_pending=False,
            )
            validate_migrations(
                connections['atomicdb'].connection,
                allow_pending=True,
            )

            self._apply_probe('atomicdb')
            default_migrations = validate_migrations(
                connections['default'].connection,
                allow_pending=False,
            )
            active_migrations = validate_migrations(
                connections['atomicdb'].connection,
                allow_pending=False,
            )
            self.assertEqual(
                default_migrations['names'],
                active_migrations['names'],
            )
            expected_tables = current_atomicdb_tables()
            self.assertEqual(
                schema_snapshot(
                    connections['default'].connection,
                    expected_tables=expected_tables,
                ),
                schema_snapshot(
                    connections['atomicdb'].connection,
                    expected_tables=expected_tables,
                ),
            )

        for alias in ('default', 'atomicdb'):
            connection = connections[alias]
            columns = connection.cursor().execute(
                """
                SELECT name FROM sqlite_master
                 WHERE type = 'index'
                   AND name = 'atomicdb_shadow_probe'
                """
            ).fetchall()
            self.assertEqual(columns, [('atomicdb_shadow_probe',)])
            self.assertTrue(
                MigrationRecorder(connection).migration_qs.filter(
                    app='atomicdb',
                    name='0014_shadow_probe',
                ).exists())


class AtomicDatabaseStaticGuards(SimpleTestCase):

    def test_deploy_health_check_fails_on_http_errors(self):
        deploy = (
            Path(__file__).resolve().parents[1]
            / 'Scripts'
            / 'deploy.sh'
        ).read_text(encoding='utf-8')
        self.assertIn(
            'curl --fail --silent --show-error --output /dev/null',
            deploy,
        )

    def test_runtime_code_has_no_implicit_default_transaction_or_connection(self):
        root = Path(__file__).resolve().parent
        failures = []
        ignored_dirs = {'migrations', '__pycache__'}
        ignored_files = {
            'database.py',
            'test_database_routing.py',
        }

        for path in root.rglob('*.py'):
            if any(part in ignored_dirs for part in path.parts):
                continue
            if path.name.startswith('test_') or path.name in ignored_files:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.ImportFrom) \
                            and node.module == 'django.db' \
                            and any(alias.name == 'connection'
                                    for alias in node.names):
                        failures.append(
                            f'{path.name}:{node.lineno}: django.db.connection')
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == 'atomic'
                    and isinstance(func.value, ast.Name)
                    and func.value.id == 'transaction'
                ):
                    continue
                if not any(keyword.arg == 'using' for keyword in node.keywords):
                    failures.append(
                        f'{path.name}:{node.lineno}: transaction.atomic '
                        'without using=')

        self.assertEqual(failures, [])
