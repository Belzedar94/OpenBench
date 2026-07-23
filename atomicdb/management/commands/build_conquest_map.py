"""Build and atomically publish the AtomicDB Conquest Map snapshot."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from atomicdb.conquest_map import (
    SnapshotError,
    build_snapshot_from_database,
    publish_snapshot,
    read_snapshot,
)


class Command(BaseCommand):
    help = (
        'Build the cycle-safe AtomicDB display tree and atomically publish '
        'its versioned gzip snapshot.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--output',
            default=settings.ATOMICDB_MAP_SNAPSHOT_PATH,
            help='Destination .json.gz (default: ATOMICDB_MAP_SNAPSHOT_PATH)',
        )
        parser.add_argument(
            '--no-stability',
            action='store_true',
            help='Do not preserve valid display parents from the prior snapshot',
        )

    def handle(self, *args, **options):
        output = Path(options['output'])
        prior = None
        if not options['no_stability'] and output.exists():
            try:
                prior = read_snapshot(output)
            except SnapshotError as exc:
                # A corrupt public artifact remains fail-closed in the API, but
                # it must not prevent an operator from replacing it with a
                # fresh independently derived snapshot.
                self.stderr.write(
                    self.style.WARNING(
                        f'Ignoring unauthenticated prior snapshot: {exc}'
                    )
                )
        try:
            snapshot = build_snapshot_from_database(prior_snapshot=prior)
            publish_snapshot(snapshot, output)
        except SnapshotError as exc:
            raise CommandError(str(exc)) from exc
        metadata = snapshot['snapshot']
        result = {
            'schema': 'atomicdb.map.build-receipt.v1',
            'snapshot_id': metadata['id'],
            'generated_at': metadata['generated_at'],
            'positions': metadata['positions'],
            'edges': metadata['edges'],
            'max_depth': metadata['max_depth'],
            'unreachable_positions': metadata['unreachable_positions'],
            'output': str(output.resolve()),
        }
        self.stdout.write(json.dumps(result, sort_keys=True))
