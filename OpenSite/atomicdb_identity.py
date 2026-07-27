"""Pure, read-only validation for the durable AtomicDB split identity.

This module intentionally has no Django imports.  Settings use it before the
application registry exists, while the offline split and verifier reuse the
same byte-level contract.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


MIGRATION_BASELINE = 'atomicdb.0013_progresssnapshot'
SPLIT_RECEIPT_SCHEMA = 'atomicdb.sqlite.split.v2'
LINEAGE_SCHEMA = 'atomicdb.sqlite.lineage.v1'
LINEAGE_TABLE = 'openbench_atomicdb_split_identity'
LINEAGE_COLUMNS = (
    'singleton',
    'schema',
    'line_id',
    'receipt_sha256',
    'origin_snapshot_sha256',
    'destination',
    'migration_baseline',
    'sealed_at',
)
ORIGIN_ATOMICDB_TABLES = (
    'atomicdb_analysistask',
    'atomicdb_campaign',
    'atomicdb_dbevent',
    'atomicdb_edge',
    'atomicdb_position',
    'atomicdb_progresssnapshot',
    'atomicdb_requestlog',
    'atomicdb_workerping',
)
# Tablas anadidas DESPUES de la linea base de cutover.  ORIGIN_ATOMICDB_TABLES
# es historia congelada: cada recibo ya emitido enumera exactamente ese
# conjunto, y tocarlo invalidaria recibos vivos (y con ellos el arranque del
# sitio).  Una tabla nueva se declara aqui, a mano y a proposito: este modulo
# corre antes de que exista el registro de aplicaciones de Django, y el guard
# del comando de split existe justamente para obligar a esta revision.
POST_BASELINE_ATOMICDB_TABLES = (
    'atomicdb_openingnamesuggestion',   # 0015_opening_name_suggestion
)
# Ordenado: el verificador compara esta tupla contra lo que encuentra en el
# fichero, y ``current_atomicdb_tables()`` sale ordenado del registro.
CURRENT_ATOMICDB_TABLES = tuple(sorted(
    ORIGIN_ATOMICDB_TABLES + POST_BASELINE_ATOMICDB_TABLES))


def valid_receipt_table_set(tables) -> bool:
    """Un recibo debe cubrir el origen entero y nada ajeno al esquema actual.

    Un recibo antiguo (solo las ocho de origen) sigue siendo valido; uno nuevo
    puede ademas traer las posteriores.  La union con el digest exterior sigue
    atando el conjunto exacto que se sello.
    """
    if not isinstance(tables, dict):
        return False
    keys = set(tables)
    return (keys >= set(ORIGIN_ATOMICDB_TABLES)
            and keys <= set(CURRENT_ATOMICDB_TABLES))


class AtomicDBIdentityError(ValueError):
    pass


def split_activation_required(
        *,
        explicit_requested: bool,
        database_exists: bool,
        receipt_exists: bool,
) -> bool:
    """Return whether the split must activate, rejecting every partial pair."""
    if not explicit_requested and not database_exists and not receipt_exists:
        return False
    if not (database_exists and receipt_exists):
        raise AtomicDBIdentityError(
            'AtomicDB split activation requires both the database and its '
            'verified split receipt')
    return True


def canonical_path(value) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(value))))


def canonical_json_bytes(payload) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)
        + '\n'
    ).encode('utf-8')


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def is_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in '0123456789abcdef' for character in value)
    )


def origin_snapshot_payload(receipt) -> dict:
    return {
        'destination': receipt.get('destination'),
        'journal_mode': receipt.get('journal_mode'),
        'migration_baseline': receipt.get('migration_sentinel'),
        'migrations': receipt.get('migrations'),
        'schema_sha256': receipt.get('schema_sha256'),
        'tables': receipt.get('tables'),
    }


def origin_snapshot_sha256(receipt) -> str:
    return sha256_hex(canonical_json_bytes(origin_snapshot_payload(receipt)))


def derive_line_id(
        line_nonce: str,
        snapshot_sha256: str,
        destination: str,
        migration_baseline: str,
) -> str:
    payload = {
        'domain': 'openbench.atomicdb.split.lineage.v1',
        'destination': canonical_path(destination),
        'line_nonce': line_nonce,
        'migration_baseline': migration_baseline,
        'origin_snapshot_sha256': snapshot_sha256,
    }
    return sha256_hex(canonical_json_bytes(payload))


def parse_and_validate_receipt(receipt_bytes: bytes, destination: str) -> dict:
    try:
        receipt = json.loads(receipt_bytes.decode('utf-8'))
    except (UnicodeError, ValueError) as error:
        raise AtomicDBIdentityError(
            'AtomicDB split receipt must be valid UTF-8 JSON') from error
    if not isinstance(receipt, dict):
        raise AtomicDBIdentityError(
            'AtomicDB split receipt must contain a JSON object')

    expected = {
        'schema': SPLIT_RECEIPT_SCHEMA,
        'status': 'verified',
        'destination': canonical_path(destination),
        'migration_sentinel': MIGRATION_BASELINE,
        'journal_mode': 'wal',
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise AtomicDBIdentityError(
                'AtomicDB split receipt has invalid {!r}'.format(field))

    for field in (
            'line_nonce',
            'line_id',
            'origin_snapshot_sha256',
            'schema_sha256'):
        if not is_sha256(receipt.get(field)):
            raise AtomicDBIdentityError(
                'AtomicDB split receipt has invalid {!r}'.format(field))

    if not isinstance(receipt.get('created_at'), str) \
            or not receipt['created_at']:
        raise AtomicDBIdentityError(
            'AtomicDB split receipt has invalid created_at')
    if not isinstance(receipt.get('source'), str) or not receipt['source']:
        raise AtomicDBIdentityError(
            'AtomicDB split receipt has invalid source')

    tables = receipt.get('tables')
    if not valid_receipt_table_set(tables):
        raise AtomicDBIdentityError(
            'AtomicDB split receipt has invalid origin table set')
    for table, summary in tables.items():
        if (
                not isinstance(summary, dict)
                or not isinstance(summary.get('rows'), int)
                or isinstance(summary.get('rows'), bool)
                or summary['rows'] < 0
                or not is_sha256(summary.get('sha256'))):
            raise AtomicDBIdentityError(
                'AtomicDB split receipt has invalid digest for {}'.format(
                    table))

    migrations = receipt.get('migrations')
    if not isinstance(migrations, dict):
        raise AtomicDBIdentityError(
            'AtomicDB split receipt has invalid migrations')
    migration_names = migrations.get('names')
    if (
            not isinstance(migration_names, list)
            or not migration_names
            or any(not isinstance(name, str) or not name
                   for name in migration_names)
            or len(set(migration_names)) != len(migration_names)
            or migrations.get('rows') != len(migration_names)
            or not is_sha256(migrations.get('sha256'))
            or MIGRATION_BASELINE.split('.', 1)[1]
            not in migration_names):
        raise AtomicDBIdentityError(
            'AtomicDB split receipt has invalid migrations')

    snapshot_sha256 = origin_snapshot_sha256(receipt)
    if receipt['origin_snapshot_sha256'] != snapshot_sha256:
        raise AtomicDBIdentityError(
            'AtomicDB split receipt origin snapshot digest is invalid')
    expected_line_id = derive_line_id(
        receipt['line_nonce'],
        snapshot_sha256,
        destination,
        MIGRATION_BASELINE,
    )
    if receipt['line_id'] != expected_line_id:
        raise AtomicDBIdentityError(
            'AtomicDB split receipt lineage id is invalid')
    return receipt


def lineage_table_sql() -> str:
    return """
        CREATE TABLE "{}" (
            "singleton" INTEGER NOT NULL PRIMARY KEY
                CHECK ("singleton" = 1),
            "schema" TEXT NOT NULL,
            "line_id" TEXT NOT NULL,
            "receipt_sha256" TEXT NOT NULL,
            "origin_snapshot_sha256" TEXT NOT NULL,
            "destination" TEXT NOT NULL,
            "migration_baseline" TEXT NOT NULL,
            "sealed_at" TEXT NOT NULL
        )
    """.format(LINEAGE_TABLE)


def lineage_values(receipt: dict, receipt_bytes: bytes) -> tuple:
    return (
        1,
        LINEAGE_SCHEMA,
        receipt['line_id'],
        sha256_hex(receipt_bytes),
        receipt['origin_snapshot_sha256'],
        receipt['destination'],
        receipt['migration_sentinel'],
        receipt['created_at'],
    )


def create_lineage_table(
        connection: sqlite3.Connection,
        receipt: dict,
        receipt_bytes: bytes,
) -> None:
    connection.execute(lineage_table_sql())
    placeholders = ', '.join('?' for _ in LINEAGE_COLUMNS)
    connection.execute(
        'INSERT INTO "{}" ({}) VALUES ({})'.format(
            LINEAGE_TABLE,
            ', '.join('"{}"'.format(column) for column in LINEAGE_COLUMNS),
            placeholders,
        ),
        lineage_values(receipt, receipt_bytes),
    )


def validate_database_lineage(
        connection: sqlite3.Connection,
        receipt: dict,
        receipt_bytes: bytes,
) -> dict:
    table_rows = connection.execute(
        """
        SELECT name, sql
          FROM sqlite_master
         WHERE type = 'table' AND name = ?
        """,
        (LINEAGE_TABLE,),
    ).fetchall()
    if len(table_rows) != 1 or not table_rows[0][1]:
        raise AtomicDBIdentityError(
            'AtomicDB lineage table is missing or ambiguous')
    normalize_sql = lambda value: re.sub(r'\s+', ' ', value.strip())
    if normalize_sql(table_rows[0][1]) != \
            normalize_sql(lineage_table_sql()):
        raise AtomicDBIdentityError(
            'AtomicDB lineage table DDL is invalid')

    columns = connection.execute(
        'PRAGMA table_info("{}")'.format(LINEAGE_TABLE)).fetchall()
    actual_columns = tuple(
        (row[1], str(row[2]).upper(), bool(row[3]), bool(row[5]))
        for row in columns
    )
    expected_columns = (
        ('singleton', 'INTEGER', True, True),
        ('schema', 'TEXT', True, False),
        ('line_id', 'TEXT', True, False),
        ('receipt_sha256', 'TEXT', True, False),
        ('origin_snapshot_sha256', 'TEXT', True, False),
        ('destination', 'TEXT', True, False),
        ('migration_baseline', 'TEXT', True, False),
        ('sealed_at', 'TEXT', True, False),
    )
    if actual_columns != expected_columns:
        raise AtomicDBIdentityError(
            'AtomicDB lineage table schema is invalid')
    auxiliary_schema = connection.execute(
        """
        SELECT type, name
          FROM sqlite_master
         WHERE tbl_name = ?
           AND type IN ('index', 'trigger')
           AND sql IS NOT NULL
        """,
        (LINEAGE_TABLE,),
    ).fetchall()
    if auxiliary_schema:
        raise AtomicDBIdentityError(
            'AtomicDB lineage table has unsupported schema objects')

    rows = connection.execute(
        'SELECT {} FROM "{}" ORDER BY "singleton"'.format(
            ', '.join('"{}"'.format(column) for column in LINEAGE_COLUMNS),
            LINEAGE_TABLE,
        )
    ).fetchall()
    expected = lineage_values(receipt, receipt_bytes)
    if rows != [expected]:
        raise AtomicDBIdentityError(
            'AtomicDB database lineage does not match its split receipt')
    return {
        'line_id': receipt['line_id'],
        'receipt_sha256': expected[3],
        'origin_snapshot_sha256': receipt['origin_snapshot_sha256'],
    }


def validate_database_lineage_path(
        database_path: str,
        receipt_bytes: bytes,
) -> tuple[dict, dict]:
    database_path = canonical_path(database_path)
    receipt = parse_and_validate_receipt(receipt_bytes, database_path)
    if not os.path.isfile(database_path) or os.path.getsize(database_path) <= 0:
        raise AtomicDBIdentityError(
            'AtomicDB split database is missing or empty')
    uri = Path(database_path).resolve().as_uri() + '?mode=ro'
    try:
        connection = sqlite3.connect(
            uri, uri=True, timeout=30.0, isolation_level=None)
        connection.execute('PRAGMA query_only=ON')
        journal_row = connection.execute('PRAGMA journal_mode').fetchone()
        if not journal_row or str(journal_row[0]).lower() != 'wal':
            raise AtomicDBIdentityError(
                'AtomicDB split database must persist WAL mode')
        lineage = validate_database_lineage(
            connection, receipt, receipt_bytes)
    except sqlite3.Error as error:
        raise AtomicDBIdentityError(
            'AtomicDB split database lineage cannot be read') from error
    finally:
        if 'connection' in locals():
            connection.close()
    return receipt, lineage
