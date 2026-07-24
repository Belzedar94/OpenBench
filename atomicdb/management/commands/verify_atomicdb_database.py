import json
import os

from django.core.management.base import BaseCommand, CommandError

from OpenSite.atomicdb_identity import (
    AtomicDBIdentityError,
    parse_and_validate_receipt,
    validate_database_lineage,
)

from ._atomicdb_sqlite import (
    ATOMICDB_TABLES,
    configured_atomicdb_path,
    current_atomicdb_tables,
    database_summaries,
    open_read_only,
    require_wal,
    schema_digest,
    schema_snapshot,
    split_receipt_path,
    validate_database_health,
    validate_migrations,
    validate_model_columns,
    validate_model_indexes_and_foreign_keys,
    validate_split_receipt,
)


class Command(BaseCommand):
    help = (
        'Fail-closed verification of an already initialized AtomicDB SQLite '
        'alias. This command never creates or mutates the database.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--allow-pending-migrations',
            action='store_true',
            help=(
                'Accept a known applied migration prefix before deploy runs '
                'the pending AtomicDB migrations. Model/schema checks that '
                'could depend on those pending migrations are deferred.'
            ),
        )
        parser.add_argument(
            '--require-origin-snapshot',
            action='store_true',
            help=(
                'Require exact origin table, migration and schema digests. '
                'Use only during offline cutover before the first write.'),
        )

    def handle(self, *args, **options):
        allow_pending = bool(options['allow_pending_migrations'])
        require_origin = bool(options['require_origin_snapshot'])
        destination = configured_atomicdb_path()
        receipt_path = split_receipt_path(destination)

        if not os.path.isfile(receipt_path):
            raise CommandError(
                'AtomicDB split receipt is missing: {}'.format(receipt_path))
        try:
            with open(receipt_path, 'rb') as receipt_stream:
                receipt_bytes = receipt_stream.read()
            receipt = parse_and_validate_receipt(
                receipt_bytes, destination)
            validate_split_receipt(receipt, destination)
        except (OSError, AtomicDBIdentityError) as error:
            raise CommandError(
                'AtomicDB split identity is invalid: {}'.format(error)
            ) from error

        connection = open_read_only(destination)
        try:
            health = validate_database_health(connection)
            require_wal(connection)
            try:
                lineage = validate_database_lineage(
                    connection, receipt, receipt_bytes)
            except AtomicDBIdentityError as error:
                raise CommandError(
                    'AtomicDB split identity is invalid: {}'.format(error)
                ) from error
            migrations = validate_migrations(
                connection, allow_pending=allow_pending)
            expected_tables = (
                ATOMICDB_TABLES
                if allow_pending
                else current_atomicdb_tables()
            )
            snapshot = schema_snapshot(
                connection, expected_tables=expected_tables)

            # Before a deploy applies new migrations, the on-disk schema can
            # legitimately lag the code's model metadata. The immutable v1
            # split tables, receipt, health, WAL and migration-prefix checks
            # are still required. Strict post-migrate verification checks the
            # complete current model contract.
            if not allow_pending:
                validate_model_columns(connection)
                validate_model_indexes_and_foreign_keys(connection)

            summaries = database_summaries(
                connection, tables=expected_tables)
            if require_origin:
                if allow_pending:
                    raise CommandError(
                        '--require-origin-snapshot cannot be combined with '
                        '--allow-pending-migrations')
                if (
                        summaries != receipt['tables']
                        or migrations != receipt['migrations']
                        or schema_digest(snapshot)
                        != receipt['schema_sha256']):
                    raise CommandError(
                        'AtomicDB current state does not exactly match the '
                        'sealed origin snapshot')
        finally:
            connection.close()

        result = {
            'status': 'verified',
            'destination': destination,
            'receipt': receipt_path,
            'allow_pending_migrations': allow_pending,
            'require_origin_snapshot': require_origin,
            'migration_rows': migrations['rows'],
            'migration_sentinel': receipt['migration_sentinel'],
            'tables': {
                table: summaries[table]['rows']
                for table in expected_tables
            },
            'schema_sha256': schema_digest(snapshot),
            'journal_mode': 'wal',
            'health': health,
            'line_id': lineage['line_id'],
            'receipt_sha256': lineage['receipt_sha256'],
            'origin_snapshot_sha256': lineage[
                'origin_snapshot_sha256'],
        }
        self.stdout.write(json.dumps(result, sort_keys=True))
