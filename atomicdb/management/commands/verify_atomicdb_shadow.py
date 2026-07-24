import json
import os

from django.core.management.base import BaseCommand, CommandError

from ._atomicdb_sqlite import (
    ATOMICDB_TABLES,
    configured_atomicdb_path,
    configured_default_path,
    current_atomicdb_tables,
    open_read_only,
    schema_digest,
    schema_snapshot,
    validate_database_health,
    validate_migrations,
    validate_model_columns,
    validate_model_indexes_and_foreign_keys,
)


class Command(BaseCommand):
    help = (
        'Read-only verification that legacy AtomicDB tables in default remain '
        'a schema/history-current rollback shadow of the active split alias.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--allow-pending-migrations',
            action='store_true',
            help=(
                'Accept independently advanced known migration prefixes while '
                'a deploy is resumably migrating the two databases.'),
        )
        parser.add_argument(
            '--compare-active-schema',
            action='store_true',
            help=(
                'Require strict current migration names and schema equality '
                'between the stale-data shadow and active AtomicDB database.'),
        )
        parser.add_argument(
            '--allow-active-pending-migrations',
            action='store_true',
            help=(
                'Require the default shadow to be current while allowing the '
                'active split alias to remain at a known prefix between the '
                'two resumable deploy migration steps.'),
        )

    def handle(self, *args, **options):
        allow_pending = bool(options['allow_pending_migrations'])
        compare_active = bool(options['compare_active_schema'])
        allow_active_pending = bool(
            options['allow_active_pending_migrations'])
        if allow_pending and allow_active_pending:
            raise CommandError(
                'Choose only one pending-migration verification mode')
        if compare_active and (allow_pending or allow_active_pending):
            raise CommandError(
                '--compare-active-schema requires strict current migrations')

        default_path = configured_default_path()
        atomicdb_path = configured_atomicdb_path()
        if os.path.normcase(default_path) == os.path.normcase(atomicdb_path):
            raise CommandError(
                'AtomicDB rollback shadow must differ from the active database')

        results = {}
        snapshots = {}
        migrations_by_alias = {}
        for alias, path in (
                ('default', default_path),
                ('atomicdb', atomicdb_path)):
            alias_allow_pending = (
                allow_pending
                or (alias == 'atomicdb' and allow_active_pending)
            )
            expected_tables = (
                ATOMICDB_TABLES
                if alias_allow_pending
                else current_atomicdb_tables()
            )
            connection = open_read_only(path)
            try:
                health = validate_database_health(connection)
                migrations = validate_migrations(
                    connection, allow_pending=alias_allow_pending)
                snapshot = schema_snapshot(
                    connection, expected_tables=expected_tables)
                if not alias_allow_pending:
                    validate_model_columns(connection)
                    validate_model_indexes_and_foreign_keys(connection)
            finally:
                connection.close()
            results[alias] = {
                'path': path,
                'migration_rows': migrations['rows'],
                'migration_names': migrations['names'],
                'schema_sha256': schema_digest(snapshot),
                'health': health,
                'allow_pending_migrations': alias_allow_pending,
            }
            snapshots[alias] = snapshot
            migrations_by_alias[alias] = migrations['names']

        if compare_active:
            if migrations_by_alias['default'] != \
                    migrations_by_alias['atomicdb']:
                raise CommandError(
                    'AtomicDB shadow and active migration histories differ')
            if snapshots['default'] != snapshots['atomicdb']:
                raise CommandError(
                    'AtomicDB shadow and active schemas differ')

        self.stdout.write(json.dumps({
            'status': 'verified',
            'allow_pending_migrations': allow_pending,
            'allow_active_pending_migrations': allow_active_pending,
            'compare_active_schema': compare_active,
            'databases': results,
        }, sort_keys=True))
