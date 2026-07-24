from contextlib import ExitStack
from io import StringIO
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest import mock, skipUnless

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections

from .models import Position
from .testing import TransactionTestCase


@skipUnless(
    settings.ATOMICDB_DATABASE_ALIAS == 'default'
    and settings.DATABASES['default']['ENGINE']
    == 'django.db.backends.sqlite3',
    'split command tests require legacy AtomicDB-on-default SQLite mode',
)
class SQLiteSplitCommandTests(TransactionTestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / 'source.sqlite3'
        self.destination = root / 'atomicdb.sqlite3'
        self.receipt = Path(str(self.destination) + '.split-receipt.json')

        Position.objects.create(
            key='1' * 64,
            fen='8/8/8/8/8/8/8/K6k w - - 0 1',
            eval_cp=17,
            nodes_invested=1234,
        )
        connections['default'].ensure_connection()
        backup = sqlite3.connect(self.source)
        try:
            connections['default'].connection.backup(backup)
        finally:
            backup.close()

    def _default_source(self):
        return mock.patch.dict(
            settings.DATABASES['default'],
            {'NAME': str(self.source)},
        )

    def _split(self):
        output = StringIO()
        with self._default_source():
            call_command(
                'split_atomicdb_sqlite',
                destination=str(self.destination),
                stdout=output,
            )
        return json.loads(output.getvalue())

    def _active_split(self):
        atomic_config = {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(self.destination),
            'OPTIONS': {'timeout': 30},
        }
        stack = ExitStack()
        stack.enter_context(mock.patch.dict(
            settings.DATABASES, {'atomicdb': atomic_config}))
        stack.enter_context(mock.patch.object(
            settings, 'ATOMICDB_DATABASE_ALIAS', 'atomicdb'))
        return stack

    def test_split_is_exact_no_overwrite_and_persists_wal(self):
        result = self._split()

        self.assertEqual(result['status'], 'verified')
        self.assertTrue(self.destination.is_file())
        self.assertTrue(self.receipt.is_file())

        payload = json.loads(self.receipt.read_text(encoding='utf-8'))
        self.assertEqual(payload['schema'], 'atomicdb.sqlite.split.v1')
        self.assertEqual(payload['status'], 'verified')
        self.assertEqual(
            payload['migration_sentinel'],
            'atomicdb.0013_progresssnapshot',
        )
        self.assertEqual(payload['tables']['atomicdb_position']['rows'], 1)

        source = sqlite3.connect(self.source)
        destination = sqlite3.connect(self.destination)
        try:
            self.assertEqual(
                source.execute(
                    'SELECT COUNT(*) FROM atomicdb_position').fetchone()[0],
                1,
            )
            self.assertEqual(
                destination.execute(
                    'SELECT COUNT(*) FROM atomicdb_position').fetchone()[0],
                1,
            )
            self.assertEqual(
                destination.execute('PRAGMA journal_mode').fetchone()[0].lower(),
                'wal',
            )
            self.assertEqual(
                destination.execute('PRAGMA integrity_check').fetchone()[0],
                'ok',
            )
            self.assertEqual(
                destination.execute('PRAGMA foreign_key_check').fetchall(),
                [],
            )
        finally:
            source.close()
            destination.close()

        before = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        with self.assertRaises(CommandError):
            self._split()
        self.assertEqual(
            hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
            before,
        )

    def test_failure_removes_only_artifacts_reserved_by_this_run(self):
        with self._default_source(), mock.patch(
                'atomicdb.management.commands.split_atomicdb_sqlite.'
                'create_split_target',
                side_effect=RuntimeError('injected-copy-failure')):
            with self.assertRaisesMessage(
                    CommandError, 'injected-copy-failure'):
                call_command(
                    'split_atomicdb_sqlite',
                    destination=str(self.destination),
                )

        self.assertFalse(self.destination.exists())
        self.assertFalse(self.receipt.exists())
        self.assertTrue(self.source.exists())

    def test_source_writer_lock_fails_without_publishing_destination(self):
        blocker = sqlite3.connect(self.source, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')

        def quick_open(path):
            uri = Path(path).resolve().as_uri() + '?mode=rw'
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=0.05,
                isolation_level=None,
            )
            connection.execute('PRAGMA busy_timeout=50')
            return connection

        try:
            with self._default_source(), mock.patch(
                    'atomicdb.management.commands.split_atomicdb_sqlite.'
                    'open_existing_read_write',
                    side_effect=quick_open):
                with self.assertRaises(CommandError):
                    call_command(
                        'split_atomicdb_sqlite',
                        destination=str(self.destination),
                    )
        finally:
            blocker.rollback()
            blocker.close()

        self.assertFalse(self.destination.exists())
        self.assertFalse(self.receipt.exists())

    def test_verify_is_read_only_and_rejects_non_wal_database(self):
        self._split()
        with self._active_split():
            output = StringIO()
            call_command('verify_atomicdb_database', stdout=output)
            result = json.loads(output.getvalue())
            self.assertEqual(result['status'], 'verified')
            self.assertEqual(result['journal_mode'], 'wal')

            connection = sqlite3.connect(self.destination)
            try:
                mode = connection.execute(
                    'PRAGMA journal_mode=DELETE').fetchone()[0]
                self.assertEqual(mode.lower(), 'delete')
            finally:
                connection.close()

            with self.assertRaisesMessage(
                    CommandError, 'must persist WAL'):
                call_command('verify_atomicdb_database')
