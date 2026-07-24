"""Offline SQLite cutover helpers for AtomicDB.

These helpers deliberately use raw sqlite3 connections.  The split command
must run while AtomicDB still falls back to the default database, and opening a
Django connection for the prospective destination could create the file that
the no-overwrite protocol is trying to protect.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import struct
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from django.apps import apps
from django.conf import settings
from django.core.management.base import CommandError

from OpenSite.atomicdb_identity import (
    MIGRATION_BASELINE,
    ORIGIN_ATOMICDB_TABLES,
    SPLIT_RECEIPT_SCHEMA,
    canonical_json_bytes,
    canonical_path,
    create_lineage_table,
    is_sha256,
)


ATOMICDB_TABLES = ORIGIN_ATOMICDB_TABLES
# Receipt field name retained for compatibility.  This is an immutable
# cutover baseline, not an assertion that 0013 must remain the newest file.
MIGRATION_SENTINEL = MIGRATION_BASELINE
RESTORE_RECEIPT_SCHEMA = 'atomicdb.sqlite.restore.v1'

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MIGRATION_FILE = re.compile(r'^(\d{4}_[A-Za-z0-9_]+)\.py$')


def sqlite_path_from_config(config: Mapping, label: str) -> str:
    if config.get('ENGINE') != 'django.db.backends.sqlite3':
        raise CommandError('{} must use Django SQLite'.format(label))
    name = config.get('NAME')
    if not name or str(name) == ':memory:':
        raise CommandError('{} must be a persistent SQLite file'.format(label))
    return canonical_path(name)


def atomicdb_migration_names(migration_dir=None) -> tuple[str, ...]:
    migration_dir = (
        Path(migration_dir)
        if migration_dir is not None
        else Path(__file__).resolve().parents[2] / 'migrations'
    )
    names = []
    for path in migration_dir.iterdir():
        match = _MIGRATION_FILE.match(path.name)
        if match:
            names.append(match.group(1))
    names.sort()
    baseline_name = MIGRATION_BASELINE.split('.', 1)[1]
    if baseline_name not in names:
        raise CommandError(
            'AtomicDB migration files do not contain required split baseline '
            '{}'.format(MIGRATION_BASELINE))
    return tuple(names)


def split_receipt_path(destination: str, explicit=None) -> str:
    return canonical_path(
        explicit if explicit else destination + '.split-receipt.json')


def restore_receipt_path(default_database: str, explicit=None) -> str:
    return canonical_path(
        explicit if explicit else default_database + '.atomicdb-restore-receipt.json')


def quote_identifier(identifier: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(identifier):
        raise CommandError('Unsafe SQLite identifier: {!r}'.format(identifier))
    return '"{}"'.format(identifier)


def open_read_only(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise CommandError('SQLite database is missing or empty: {}'.format(path))
    # URI mode prevents an accidental database creation on a typo.
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(
        uri, uri=True, timeout=30.0, isolation_level=None)
    connection.execute('PRAGMA busy_timeout=30000')
    return connection


def open_existing_read_write(path: str) -> sqlite3.Connection:
    """Open an existing SQLite file without allowing accidental creation."""
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise CommandError('SQLite database is missing or empty: {}'.format(path))
    uri = Path(path).resolve().as_uri() + '?mode=rw'
    connection = sqlite3.connect(
        uri, uri=True, timeout=30.0, isolation_level=None)
    connection.execute('PRAGMA busy_timeout=30000')
    return connection


def reserve_database(path: str) -> None:
    """Atomically reserve a new database path without following symlinks."""
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise CommandError(
            'Destination directory does not exist: {}'.format(parent))
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, 'O_BINARY'):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)


def ensure_cutover_paths_absent(database: str, receipt: str) -> None:
    candidates = (
        database,
        database + '-wal',
        database + '-shm',
        database + '-journal',
        receipt,
    )
    existing = [path for path in candidates if os.path.lexists(path)]
    if existing:
        raise CommandError(
            'Cutover is no-overwrite; path already exists: {}'.format(
                ', '.join(existing)))


def cleanup_created_database(database: str) -> None:
    """Remove only artifacts belonging to a database reserved by this run."""
    for path in (
            database + '-wal',
            database + '-shm',
            database + '-journal',
            database):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def atomic_write_json_exclusive(path: str, payload: Mapping) -> None:
    """Create a durable immutable receipt using O_EXCL.

    The receipt is intentionally created last.  A database without its receipt
    remains inactive under the settings-side activation protocol.
    """
    encoded = canonical_json_bytes(payload)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, 'O_BINARY'):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('short write while creating receipt')
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(descriptor)
    try:
        _fsync_directory(os.path.dirname(path))
    except Exception:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path: str) -> None:
    # Windows does not expose a portable directory fsync through os.open.
    if os.name == 'nt':
        return
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_receipt(path: str) -> Mapping:
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            receipt = json.load(stream)
    except (OSError, UnicodeError, ValueError) as error:
        raise CommandError(
            'AtomicDB receipt is not valid UTF-8 JSON: {}'.format(path)
        ) from error
    if not isinstance(receipt, dict):
        raise CommandError('AtomicDB receipt must contain a JSON object')
    return receipt


def validate_split_receipt(receipt: Mapping, destination: str) -> None:
    expected = {
        'schema': SPLIT_RECEIPT_SCHEMA,
        'status': 'verified',
        'destination': canonical_path(destination),
        'migration_sentinel': MIGRATION_SENTINEL,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise CommandError(
                'AtomicDB split receipt has invalid {!r}'.format(field))
    tables = receipt.get('tables')
    if not isinstance(tables, dict) or set(tables) != set(ATOMICDB_TABLES):
        raise CommandError('AtomicDB split receipt has an invalid table set')
    for table, summary in tables.items():
        if (
                not isinstance(summary, dict)
                or not isinstance(summary.get('rows'), int)
                or summary['rows'] < 0
                or not _is_sha256(summary.get('sha256'))):
            raise CommandError(
                'AtomicDB split receipt has invalid digest for {}'.format(table))


def _is_sha256(value) -> bool:
    return is_sha256(value)


def current_atomicdb_tables() -> tuple[str, ...]:
    return tuple(sorted(
        model._meta.db_table
        for model in apps.get_app_config('atomicdb').get_models()
    ))


def schema_snapshot(
        connection: sqlite3.Connection,
        expected_tables: Sequence[str] = ATOMICDB_TABLES,
) -> dict:
    table_rows = connection.execute(
        """
        SELECT name, sql
          FROM sqlite_master
         WHERE type = 'table' AND name LIKE 'atomicdb\\_%' ESCAPE '\\'
         ORDER BY name
        """
    ).fetchall()
    names = tuple(row[0] for row in table_rows)
    expected_tables = tuple(expected_tables)
    if names != expected_tables:
        raise CommandError(
            'AtomicDB table set is not exact: expected {}, found {}'.format(
                ', '.join(expected_tables), ', '.join(names)))
    if any(not row[1] for row in table_rows):
        raise CommandError('AtomicDB contains a table without SQL schema')

    migration_table = connection.execute(
        """
        SELECT sql FROM sqlite_master
         WHERE type = 'table' AND name = 'django_migrations'
        """
    ).fetchone()
    if not migration_table or not migration_table[0]:
        raise CommandError('django_migrations schema is missing')

    index_rows = connection.execute(
        """
        SELECT name, tbl_name, sql
          FROM sqlite_master
         WHERE type = 'index'
           AND tbl_name LIKE 'atomicdb\\_%' ESCAPE '\\'
           AND sql IS NOT NULL
         ORDER BY name
        """
    ).fetchall()
    trigger_rows = connection.execute(
        """
        SELECT name FROM sqlite_master
         WHERE type = 'trigger'
           AND tbl_name LIKE 'atomicdb\\_%' ESCAPE '\\'
        """
    ).fetchall()
    if trigger_rows:
        raise CommandError(
            'AtomicDB triggers are not supported by the exact-copy protocol')

    return {
        'tables': {name: sql for name, sql in table_rows},
        'migration_table': migration_table[0],
        'indexes': {
            name: {'table': table, 'sql': sql}
            for name, table, sql in index_rows
        },
    }


def schema_digest(snapshot: Mapping) -> str:
    payload = json.dumps(
        snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def validate_model_columns(connection: sqlite3.Connection) -> None:
    """Require the live schema to match every current AtomicDB model column."""
    expected_tables = {}
    for model in apps.get_app_config('atomicdb').get_models():
        expected_tables[model._meta.db_table] = {
            field.column: bool(field.primary_key)
            for field in model._meta.local_fields
        }
    for table, expected_columns in expected_tables.items():
        actual_rows = connection.execute(
            'PRAGMA table_info({})'.format(quote_identifier(table))).fetchall()
        actual_columns = {row[1]: bool(row[5]) for row in actual_rows}
        if actual_columns != expected_columns:
            raise CommandError(
                'AtomicDB schema columns are incorrect for {}'.format(table))


def validate_model_indexes_and_foreign_keys(
        connection: sqlite3.Connection) -> None:
    """Check index and FK shapes implied by current model metadata."""
    for model in apps.get_app_config('atomicdb').get_models():
        table = model._meta.db_table
        index_rows = connection.execute(
            'PRAGMA index_list({})'.format(quote_identifier(table))).fetchall()
        actual_indexes = []
        for index_row in index_rows:
            index_name = index_row[1]
            columns = tuple(
                row[2] for row in connection.execute(
                    'PRAGMA index_info({})'.format(
                        quote_identifier(index_name))).fetchall()
            )
            actual_indexes.append((columns, bool(index_row[2])))

        required_indexes = []
        for field in model._meta.local_fields:
            if field.primary_key:
                continue
            if field.unique:
                required_indexes.append(((field.column,), True))
            elif field.db_index:
                required_indexes.append(((field.column,), False))
        for index in model._meta.indexes:
            columns = tuple(
                model._meta.get_field(name.lstrip('-')).column
                for name in index.fields
            )
            required_indexes.append((columns, False))
        for constraint in model._meta.constraints:
            fields = getattr(constraint, 'fields', ())
            if fields:
                columns = tuple(
                    model._meta.get_field(name).column for name in fields)
                required_indexes.append((columns, True))
        for field_names in model._meta.unique_together:
            columns = tuple(
                model._meta.get_field(name).column for name in field_names)
            required_indexes.append((columns, True))

        for columns, require_unique in required_indexes:
            if not any(
                    actual_columns == columns
                    and (actual_unique or not require_unique)
                    for actual_columns, actual_unique in actual_indexes):
                raise CommandError(
                    'AtomicDB required index is missing on {}({})'.format(
                        table, ', '.join(columns)))

        actual_foreign_keys = {
            (row[2], row[3], row[4])
            for row in connection.execute(
                'PRAGMA foreign_key_list({})'.format(
                    quote_identifier(table))).fetchall()
        }
        expected_foreign_keys = set()
        for field in model._meta.local_fields:
            remote = getattr(field, 'remote_field', None)
            if (
                    remote is None
                    or not getattr(field, 'db_constraint', True)
                    or not getattr(field, 'many_to_one', False)):
                continue
            expected_foreign_keys.add((
                remote.model._meta.db_table,
                field.column,
                field.target_field.column,
            ))
        if actual_foreign_keys != expected_foreign_keys:
            raise CommandError(
                'AtomicDB foreign-key schema is incorrect for {}'.format(table))


def validate_database_health(connection: sqlite3.Connection) -> dict:
    integrity = connection.execute('PRAGMA integrity_check').fetchall()
    if integrity != [('ok',)]:
        raise CommandError(
            'SQLite integrity_check failed: {}'.format(integrity[:3]))
    foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_keys:
        raise CommandError(
            'SQLite foreign_key_check failed: {}'.format(foreign_keys[:3]))
    return {
        'integrity_check': 'ok',
        'foreign_key_violations': 0,
    }


def journal_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute('PRAGMA journal_mode').fetchone()
    return str(row[0]).lower() if row else ''


def require_wal(connection: sqlite3.Connection) -> None:
    if journal_mode(connection) != 'wal':
        raise CommandError('AtomicDB SQLite database must persist WAL mode')


def migration_rows(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        """
        SELECT id, app, name, applied
          FROM django_migrations
         WHERE app = 'atomicdb'
         ORDER BY name, id
        """
    ).fetchall()


def validate_migrations(
        connection: sqlite3.Connection,
        *,
        allow_pending: bool,
) -> dict:
    expected = atomicdb_migration_names()
    rows = migration_rows(connection)
    applied = tuple(row[2] for row in rows)
    if len(set(applied)) != len(applied):
        raise CommandError('AtomicDB contains duplicate migration rows')
    if MIGRATION_BASELINE.split('.', 1)[1] not in applied:
        raise CommandError(
            'AtomicDB migration baseline is not applied: {}'.format(
                MIGRATION_BASELINE))
    if allow_pending:
        if applied != expected[:len(applied)]:
            raise CommandError(
                'AtomicDB applied migrations are not an exact known prefix')
    elif applied != expected:
        raise CommandError(
            'AtomicDB migrations are not current (applied {}, expected {})'.format(
                len(applied), len(expected)))
    return {
        'rows': len(rows),
        'names': list(applied),
        'sha256': logical_rows_digest(
            ('id', 'app', 'name', 'applied'), rows),
    }


def table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        'PRAGMA table_info({})'.format(quote_identifier(table))).fetchall()
    if not rows:
        raise CommandError('SQLite table is missing: {}'.format(table))
    return tuple(row[1] for row in rows)


def table_order_columns(
        connection: sqlite3.Connection,
        table: str,
        columns: Sequence[str],
) -> tuple[str, ...]:
    rows = connection.execute(
        'PRAGMA table_info({})'.format(quote_identifier(table))).fetchall()
    primary = tuple(
        row[1] for row in sorted(rows, key=lambda item: item[5]) if row[5])
    return primary or tuple(columns)


def iter_rows(
        connection: sqlite3.Connection,
        table: str,
        *,
        where: str = '',
        parameters: Sequence = (),
        batch_size: int = 1000,
) -> Iterable[list[tuple]]:
    columns = table_columns(connection, table)
    order = table_order_columns(connection, table, columns)
    sql = 'SELECT {} FROM {}'.format(
        ', '.join(quote_identifier(column) for column in columns),
        quote_identifier(table),
    )
    if where:
        sql += ' WHERE ' + where
    sql += ' ORDER BY ' + ', '.join(
        quote_identifier(column) for column in order)
    cursor = connection.execute(sql, tuple(parameters))
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        yield rows


def logical_rows_digest(columns: Sequence[str], rows: Iterable[Sequence]) -> str:
    digest = hashlib.sha256()
    _digest_frame(digest, b'atomicdb.logical.rows.v1')
    for column in columns:
        _digest_frame(digest, column.encode('utf-8'))
    for row in rows:
        _digest_frame(digest, b'row')
        for value in row:
            _digest_value(digest, value)
    return digest.hexdigest()


def table_summary(connection: sqlite3.Connection, table: str) -> dict:
    columns = table_columns(connection, table)
    digest = hashlib.sha256()
    _digest_frame(digest, b'atomicdb.logical.rows.v1')
    for column in columns:
        _digest_frame(digest, column.encode('utf-8'))
    count = 0
    for batch in iter_rows(connection, table):
        for row in batch:
            _digest_frame(digest, b'row')
            for value in row:
                _digest_value(digest, value)
            count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def database_summaries(
        connection: sqlite3.Connection,
        tables: Sequence[str] = ATOMICDB_TABLES,
) -> dict:
    return {
        table: table_summary(connection, table)
        for table in tables
    }


def copy_table(
        source: sqlite3.Connection,
        destination: sqlite3.Connection,
        table: str,
        *,
        where: str = '',
        parameters: Sequence = (),
) -> None:
    source_columns = table_columns(source, table)
    destination_columns = table_columns(destination, table)
    if source_columns != destination_columns:
        raise CommandError(
            'Column mismatch while copying {}'.format(table))
    insert = 'INSERT INTO {} ({}) VALUES ({})'.format(
        quote_identifier(table),
        ', '.join(quote_identifier(column) for column in source_columns),
        ', '.join('?' for _ in source_columns),
    )
    for batch in iter_rows(
            source, table, where=where, parameters=parameters):
        destination.executemany(insert, batch)


def create_split_target(
        source: sqlite3.Connection,
        destination: sqlite3.Connection,
        snapshot: Mapping,
        *,
        receipt: Mapping,
        receipt_bytes: bytes,
) -> None:
    destination.execute('PRAGMA foreign_keys=OFF')
    destination.execute('BEGIN IMMEDIATE')
    try:
        destination.execute(snapshot['migration_table'])
        for table in ATOMICDB_TABLES:
            destination.execute(snapshot['tables'][table])
        for table in ATOMICDB_TABLES:
            copy_table(source, destination, table)
        copy_table(
            source,
            destination,
            'django_migrations',
            where='"app" = ?',
            parameters=('atomicdb',),
        )
        for name in sorted(snapshot['indexes']):
            destination.execute(snapshot['indexes'][name]['sql'])
        create_lineage_table(destination, receipt, receipt_bytes)
        validate_database_health(destination)
        destination.execute('COMMIT')
    except Exception:
        try:
            destination.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        destination.execute('PRAGMA foreign_keys=ON')


def set_and_persist_wal(connection: sqlite3.Connection) -> None:
    cursor = connection.execute('PRAGMA journal_mode=WAL')
    try:
        mode = cursor.fetchone()
    finally:
        cursor.close()
    if not mode or str(mode[0]).lower() != 'wal':
        raise CommandError('Unable to enable WAL mode on AtomicDB destination')
    # No write occurs after switching journal mode, so there is no WAL payload
    # to checkpoint here. A fresh read-only connection below proves that the
    # mode persisted without taking another write/checkpoint lock.
    require_wal(connection)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _digest_frame(digest, payload: bytes) -> None:
    digest.update(struct.pack('>Q', len(payload)))
    digest.update(payload)


def _digest_value(digest, value) -> None:
    if value is None:
        _digest_frame(digest, b'n')
    elif isinstance(value, bytes):
        _digest_frame(digest, b'b' + value)
    elif isinstance(value, int):
        _digest_frame(digest, b'i' + str(value).encode('ascii'))
    elif isinstance(value, float):
        _digest_frame(digest, b'f' + value.hex().encode('ascii'))
    else:
        _digest_frame(digest, b's' + str(value).encode('utf-8'))


def configured_default_path() -> str:
    return sqlite_path_from_config(
        settings.DATABASES.get('default', {}), 'default database')


def configured_atomicdb_path() -> str:
    if 'atomicdb' not in settings.DATABASES:
        raise CommandError(
            'AtomicDB alias is not active; activate the verified split first')
    path = sqlite_path_from_config(
        settings.DATABASES['atomicdb'], 'atomicdb database')
    default = settings.DATABASES.get('default', {})
    if default.get('ENGINE') == 'django.db.backends.sqlite3':
        if os.path.normcase(path) == os.path.normcase(
                sqlite_path_from_config(default, 'default database')):
            raise CommandError('AtomicDB alias must differ from default database')
    return path
