from contextlib import ExitStack
from io import StringIO
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest import mock, skipUnless

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import SimpleTestCase
from django.test.utils import override_settings

from OpenSite.atomicdb_identity import (
    LINEAGE_TABLE,
    split_activation_required,
)

from .management.commands._atomicdb_sqlite import (
    atomicdb_migration_names,
    atomicdb_tables_for_migrations,
    validate_migrations,
)
from .models import Position
from .testing import TransactionTestCase


class SQLiteSplitContractTests(SimpleTestCase):

    def test_activation_falls_back_only_when_both_artifacts_are_absent(self):
        self.assertFalse(split_activation_required(
            explicit_requested=False,
            database_exists=False,
            receipt_exists=False,
        ))
        for database_exists, receipt_exists in ((True, False), (False, True)):
            with self.assertRaisesRegex(
                    ValueError, 'requires both the database'):
                split_activation_required(
                    explicit_requested=False,
                    database_exists=database_exists,
                    receipt_exists=receipt_exists,
                )

    def test_migration_baseline_can_have_known_future_successors(self):
        with tempfile.TemporaryDirectory() as temporary:
            migration_dir = Path(temporary)
            for name in (
                    '0012_analysistask_lease_session.py',
                    '0013_progresssnapshot.py',
                    '0014_future_probe.py'):
                (migration_dir / name).touch()
            self.assertEqual(
                atomicdb_migration_names(migration_dir),
                (
                    '0012_analysistask_lease_session',
                    '0013_progresssnapshot',
                    '0014_future_probe',
                ),
            )

            (migration_dir / '0013_progresssnapshot.py').unlink()
            with self.assertRaisesMessage(
                    CommandError, 'required split baseline'):
                atomicdb_migration_names(migration_dir)

    def test_pending_migration_contract_accepts_exact_prefix_after_baseline(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute(
            """
            CREATE TABLE django_migrations (
                id INTEGER PRIMARY KEY,
                app TEXT NOT NULL,
                name TEXT NOT NULL,
                applied TEXT NOT NULL
            )
            """
        )
        names = (
            '0012_analysistask_lease_session',
            '0013_progresssnapshot',
            '0014_future_probe',
        )
        for row_id, name in enumerate(names[:2], start=1):
            connection.execute(
                """
                INSERT INTO django_migrations (id, app, name, applied)
                VALUES (?, 'atomicdb', ?, '2026-07-24T00:00:00Z')
                """,
                (row_id, name),
            )
        with mock.patch(
                'atomicdb.management.commands._atomicdb_sqlite.'
                'atomicdb_migration_names',
                return_value=names):
            validate_migrations(connection, allow_pending=True)
            with self.assertRaisesMessage(
                    CommandError, 'migrations are not current'):
                validate_migrations(connection, allow_pending=False)

            connection.execute(
                """
                INSERT INTO django_migrations (id, app, name, applied)
                VALUES (3, 'atomicdb', '0014_future_probe',
                        '2026-07-24T00:00:01Z')
                """
            )
            validate_migrations(connection, allow_pending=False)

            connection.execute(
                """
                UPDATE django_migrations
                   SET name = '0014_unknown'
                 WHERE id = 3
                """
            )
            with self.assertRaisesMessage(
                    CommandError, 'not an exact known prefix'):
                validate_migrations(connection, allow_pending=True)

    def test_historical_table_set_tracks_applied_migration_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_name = 'atomicdb_pending_test_migrations'
            package = Path(temporary) / package_name
            package.mkdir()
            (package / '__init__.py').touch()
            (package / '0001_initial.py').write_text(
                """
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name='Base',
            fields=[
                ('id', models.AutoField(primary_key=True)),
            ],
        ),
    ]
""".lstrip(),
                encoding='utf-8',
            )
            (package / '0002_future_table.py').write_text(
                """
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('atomicdb', '0001_initial')]
    operations = [
        migrations.CreateModel(
            name='FutureTable',
            fields=[
                ('id', models.AutoField(primary_key=True)),
            ],
        ),
        migrations.AddField(
            model_name='base',
            name='future',
            field=models.ManyToManyField(to='atomicdb.futuretable'),
        ),
    ]
""".lstrip(),
                encoding='utf-8',
            )
            (package / '0003_pending.py').write_text(
                """
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('atomicdb', '0002_future_table')]
    operations = []
""".lstrip(),
                encoding='utf-8',
            )
            sys.path.insert(0, temporary)
            importlib.invalidate_caches()
            try:
                with override_settings(
                        MIGRATION_MODULES={'atomicdb': package_name}):
                    self.assertEqual(
                        atomicdb_tables_for_migrations(('0001_initial',)),
                        ('atomicdb_base',),
                    )
                    self.assertEqual(
                        atomicdb_tables_for_migrations((
                            '0001_initial',
                            '0002_future_table',
                        )),
                        (
                            'atomicdb_base',
                            'atomicdb_base_future',
                            'atomicdb_futuretable',
                        ),
                    )
            finally:
                sys.path.remove(temporary)
                for module_name in tuple(sys.modules):
                    if module_name == package_name \
                            or module_name.startswith(package_name + '.'):
                        sys.modules.pop(module_name, None)


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
                offline_confirmed=True,
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
        self.assertEqual(payload['schema'], 'atomicdb.sqlite.split.v2')
        self.assertEqual(payload['status'], 'verified')
        self.assertEqual(
            payload['migration_sentinel'],
            'atomicdb.0013_progresssnapshot',
        )
        self.assertEqual(payload['tables']['atomicdb_position']['rows'], 1)
        self.assertEqual(len(payload['line_id']), 64)
        self.assertEqual(len(payload['origin_snapshot_sha256']), 64)

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
            self.assertEqual(
                destination.execute(
                    'SELECT COUNT(*) FROM "{}"'.format(
                        LINEAGE_TABLE)).fetchone()[0],
                1,
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
                    offline_confirmed=True,
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
                        offline_confirmed=True,
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

    def test_split_requires_explicit_offline_attestation(self):
        with self._default_source(), self.assertRaisesMessage(
                CommandError, '--offline-confirmed'):
            call_command(
                'split_atomicdb_sqlite',
                destination=str(self.destination),
            )
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.receipt.exists())

    def test_lineage_binding_rejects_receipt_tamper_but_allows_data_growth(self):
        self._split()
        with self._active_split():
            call_command(
                'verify_atomicdb_database',
                require_origin_snapshot=True,
            )

            connection = sqlite3.connect(self.destination)
            try:
                connection.execute(
                    """
                    INSERT INTO atomicdb_position
                        (key, fen, status, expanded, depth_invested,
                         nodes_invested, time_invested, visits, priority,
                         backed_plies, backed_nodes, updated)
                    VALUES (?, ?, 'UNKNOWN', 0, 0, 0, 0.0, 0, 0.0, 0, 0, ?)
                    """,
                    (
                        '2' * 64,
                        '8/8/8/8/8/8/8/K6k b - - 0 1',
                        '2026-07-24 00:00:00',
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            # Ordinary verification authenticates lineage and current schema
            # without comparing mutable rows to their origin digests.
            call_command('verify_atomicdb_database')
            with self.assertRaisesMessage(
                    CommandError, 'sealed origin snapshot'):
                call_command(
                    'verify_atomicdb_database',
                    require_origin_snapshot=True,
                )

            payload = json.loads(self.receipt.read_text(encoding='utf-8'))
            payload['line_id'] = '0' * 64
            self.receipt.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + '\n',
                encoding='utf-8',
            )
            with self.assertRaisesMessage(
                    CommandError, 'split identity is invalid'):
                call_command('verify_atomicdb_database')

    def test_lineage_binding_rejects_byte_change_and_database_tamper(self):
        self._split()
        original_receipt = self.receipt.read_bytes()
        with self._active_split():
            self.receipt.write_bytes(original_receipt + b'\n')
            with self.assertRaisesMessage(
                    CommandError, 'split identity is invalid'):
                call_command('verify_atomicdb_database')
            self.receipt.write_bytes(original_receipt)

            connection = sqlite3.connect(self.destination)
            try:
                connection.execute(
                    'UPDATE "{}" SET receipt_sha256 = ?'.format(
                        LINEAGE_TABLE),
                    ('0' * 64,),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesMessage(
                    CommandError, 'split identity is invalid'):
                call_command('verify_atomicdb_database')

    def test_lineage_binding_rejects_weakened_table_ddl(self):
        self._split()
        with self._active_split():
            connection = sqlite3.connect(self.destination)
            try:
                row = connection.execute(
                    'SELECT * FROM "{}"'.format(LINEAGE_TABLE)
                ).fetchone()
                connection.execute(
                    'ALTER TABLE "{}" RENAME TO lineage_original'.format(
                        LINEAGE_TABLE))
                connection.execute(
                    """
                    CREATE TABLE "{}" (
                        singleton INTEGER PRIMARY KEY,
                        schema TEXT,
                        line_id TEXT,
                        receipt_sha256 TEXT,
                        origin_snapshot_sha256 TEXT,
                        destination TEXT,
                        migration_baseline TEXT,
                        sealed_at TEXT
                    )
                    """.format(LINEAGE_TABLE)
                )
                connection.execute(
                    'INSERT INTO "{}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)'.format(
                        LINEAGE_TABLE),
                    row,
                )
                connection.execute('DROP TABLE lineage_original')
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesMessage(
                    CommandError, 'split identity is invalid'):
                call_command('verify_atomicdb_database')

    def test_shadow_verifier_compares_schema_not_stale_rows(self):
        self._split()
        with self._default_source(), self._active_split():
            output = StringIO()
            call_command(
                'verify_atomicdb_shadow',
                compare_active_schema=True,
                stdout=output,
            )
            result = json.loads(output.getvalue())
            self.assertEqual(result['status'], 'verified')

            # Runtime data intentionally diverges after cutover and must not
            # invalidate a schema/history rollback shadow.
            connection = sqlite3.connect(self.destination)
            try:
                connection.execute(
                    'UPDATE atomicdb_position SET visits = visits + 1')
                connection.commit()
            finally:
                connection.close()
            call_command(
                'verify_atomicdb_shadow',
                compare_active_schema=True,
                stdout=StringIO(),
            )

            connection = sqlite3.connect(self.destination)
            try:
                connection.execute(
                    """
                    CREATE INDEX atomicdb_test_schema_drift
                    ON atomicdb_position(eval_cp)
                    """
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesMessage(
                    CommandError, 'shadow and active schemas differ'):
                call_command(
                    'verify_atomicdb_shadow',
                    compare_active_schema=True,
                )
