import json
import os
import sqlite3

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ._atomicdb_sqlite import (
    ATOMICDB_TABLES,
    MIGRATION_SENTINEL,
    SPLIT_RECEIPT_SCHEMA,
    atomic_write_json_exclusive,
    canonical_path,
    cleanup_created_database,
    configured_default_path,
    create_split_target,
    current_atomicdb_tables,
    database_summaries,
    ensure_cutover_paths_absent,
    journal_mode,
    open_existing_read_write,
    open_read_only,
    reserve_database,
    schema_digest,
    schema_snapshot,
    set_and_persist_wal,
    split_receipt_path,
    utc_now,
    validate_database_health,
    validate_migrations,
    validate_model_columns,
    validate_model_indexes_and_foreign_keys,
)


class Command(BaseCommand):
    help = (
        'Offline, no-overwrite copy of only AtomicDB SQLite state out of the '
        'default OpenBench database. This command does not activate the alias.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--destination',
            default=os.path.join(settings.BASE_DIR, 'atomicdb.sqlite3'),
            help='New AtomicDB SQLite path (must not exist)',
        )
    def handle(self, *args, **options):
        source_path = configured_default_path()
        destination_path = canonical_path(options['destination'])
        receipt_path = split_receipt_path(destination_path)

        if os.path.normcase(source_path) == os.path.normcase(destination_path):
            raise CommandError(
                'AtomicDB destination must differ from the default database')
        if (
                os.path.normcase(receipt_path) == os.path.normcase(source_path)
                or os.path.normcase(receipt_path)
                == os.path.normcase(destination_path)):
            raise CommandError(
                'AtomicDB receipt must be distinct from both database files')
        if not os.path.isdir(os.path.dirname(receipt_path)):
            raise CommandError(
                'Receipt directory does not exist: {}'.format(
                    os.path.dirname(receipt_path)))

        ensure_cutover_paths_absent(destination_path, receipt_path)
        if current_atomicdb_tables() != ATOMICDB_TABLES:
            raise CommandError(
                'The v1 split protocol only supports the sealed eight-table '
                'AtomicDB schema; update the cutover protocol first')
        source = None
        destination = None
        destination_reserved = False
        try:
            source = open_existing_read_write(source_path)
            # A read transaction would provide a consistent snapshot but still
            # allow new writers to advance the source while the copy runs.
            # BEGIN IMMEDIATE is the command's enforceable offline boundary:
            # it waits out an existing writer, then blocks all new writers
            # until the verified destination and receipt are durable.
            source.execute('BEGIN IMMEDIATE')

            source_snapshot = schema_snapshot(source)
            validate_model_columns(source)
            validate_model_indexes_and_foreign_keys(source)
            source_health = validate_database_health(source)
            source_migrations = validate_migrations(
                source, allow_pending=False)
            source_summaries = database_summaries(source)

            # Re-check immediately before the O_EXCL reservation.  O_EXCL is
            # the authoritative race-safe test; this preflight only gives a
            # clearer error for sidecars and receipts.
            ensure_cutover_paths_absent(destination_path, receipt_path)
            reserve_database(destination_path)
            destination_reserved = True

            destination = sqlite3.connect(
                destination_path, timeout=30.0, isolation_level=None)
            destination.execute('PRAGMA busy_timeout=30000')
            create_split_target(source, destination, source_snapshot)

            target_snapshot = schema_snapshot(destination)
            if target_snapshot != source_snapshot:
                raise CommandError(
                    'AtomicDB destination schema/index copy is not exact')
            validate_model_columns(destination)
            validate_model_indexes_and_foreign_keys(destination)
            target_health = validate_database_health(destination)
            target_migrations = validate_migrations(
                destination, allow_pending=False)
            target_summaries = database_summaries(destination)
            if target_summaries != source_summaries:
                raise CommandError(
                    'AtomicDB destination logical table digests differ')
            if target_migrations != source_migrations:
                raise CommandError(
                    'AtomicDB destination migration digest differs')

            set_and_persist_wal(destination)
            target_health = validate_database_health(destination)
            target_journal_mode = journal_mode(destination)
            destination.close()
            destination = None

            # Verify from a new read-only connection so persistence is proven,
            # rather than trusting the connection that issued journal_mode.
            destination = open_read_only(destination_path)
            if journal_mode(destination) != 'wal':
                raise CommandError(
                    'AtomicDB WAL mode did not persist after reconnect')
            validate_database_health(destination)
            destination.close()
            destination = None

            receipt = {
                'schema': SPLIT_RECEIPT_SCHEMA,
                'status': 'verified',
                'created_at': utc_now(),
                'source': source_path,
                'destination': destination_path,
                'migration_sentinel': MIGRATION_SENTINEL,
                'tables': target_summaries,
                'migrations': target_migrations,
                'schema_sha256': schema_digest(target_snapshot),
                'journal_mode': target_journal_mode,
                'source_health': source_health,
                'destination_health': target_health,
            }
            atomic_write_json_exclusive(receipt_path, receipt)

        except CommandError:
            if destination is not None:
                destination.close()
                destination = None
            if destination_reserved:
                cleanup_created_database(destination_path)
            raise
        except (OSError, sqlite3.Error) as error:
            if destination is not None:
                destination.close()
                destination = None
            if destination_reserved:
                cleanup_created_database(destination_path)
            raise CommandError(
                'AtomicDB split failed: {}'.format(error)) from error
        except Exception as error:
            if destination is not None:
                destination.close()
                destination = None
            if destination_reserved:
                cleanup_created_database(destination_path)
            raise CommandError(
                'AtomicDB split failed unexpectedly: {}'.format(error)
            ) from error
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                try:
                    source.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
                source.close()

        result = {
            'status': 'verified',
            'source': source_path,
            'destination': destination_path,
            'receipt': receipt_path,
            'tables': list(ATOMICDB_TABLES),
            'migration_rows': len(migration_rows_from_receipt(receipt)),
        }
        self.stdout.write(json.dumps(result, sort_keys=True))


def migration_rows_from_receipt(receipt):
    """Small output helper kept separate from the cutover protocol."""
    return receipt.get('migrations', {}).get('names', [])
