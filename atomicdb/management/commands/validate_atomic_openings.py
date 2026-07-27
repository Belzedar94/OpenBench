"""Validate the committed Atomic opening catalogue."""

import json

from django.core.management.base import BaseCommand, CommandError

from atomicdb import openings


class Command(BaseCommand):
    help = (
        'Validate the versioned Atomic opening catalogue. By default every '
        'source line is replayed with PyFFish Atomic rules.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-deep',
            action='store_true',
            help='Validate schema/digest/precedence without replaying every line.',
        )

    def handle(self, *args, **options):
        try:
            payload = json.loads(
                openings.CATALOG_PATH.read_text(encoding='utf-8'))
            result = openings.validate_catalog(
                payload, deep=not options['no_deep'], require_atomix=True)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            openings.OpeningCatalogError,
        ) as exc:
            raise CommandError(str(exc)) from exc
        mode = 'structural' if options['no_deep'] else 'deep'
        self.stdout.write(self.style.SUCCESS(
            f"Atomic openings {mode} validation OK: "
            f"positions={result['positions']} "
            f"records={result['source_records']} "
            f"EAO-names={result['eao_unique_exact_names']} "
            f"sha256={result['catalog_sha256']}"
        ))
