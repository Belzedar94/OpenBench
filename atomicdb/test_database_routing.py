import ast
from pathlib import Path
import sqlite3
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import SimpleTestCase

from OpenSite.db_routers import AtomicDBRouter

from .database import DATABASE_ALIAS, atomic, connection
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


class AtomicDatabaseStaticGuards(SimpleTestCase):

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
