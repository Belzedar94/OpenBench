"""Verify an installed RELABEL input set before a workload is created from it.

    python manage.py relabel_check_input <set>

Checks manifest.json, then every chunk file: compressed SHA-256 and size,
bzip2 integrity, record count, raw SHA-256 and position-key SHA-256. It reads
only Media/relabel/inputs/<set>/ and writes nothing.
"""

import bz2
import hashlib
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

import OpenBench.relabel


class Command(BaseCommand):

    help = 'Verify every chunk of an installed RELABEL input set'

    def add_arguments(self, parser):
        parser.add_argument('set_name')

    def handle(self, *args, **options):
        set_name = options['set_name']
        try:
            manifest = OpenBench.relabel.load_manifest(settings.MEDIA_ROOT, set_name)
        except OpenBench.relabel.RelabelError as error:
            raise CommandError(str(error))

        fmt = OpenBench.relabel.RELABEL_FORMATS[manifest['format']]
        directory = OpenBench.relabel.input_directory(settings.MEDIA_ROOT, set_name)
        for chunk in manifest['chunks']:
            path = os.path.join(directory, chunk['file'])
            with open(path, 'rb') as handle:
                compressed = handle.read()
            if (hashlib.sha256(compressed).hexdigest(), len(compressed)) != (
                chunk['sha256'], chunk['bytes'],
            ):
                raise CommandError('%s: compressed SHA-256/bytes mismatch' % chunk['file'])
            try:
                data = bz2.decompress(compressed)
            except (OSError, EOFError) as error:
                raise CommandError('%s: bad bzip2 stream: %s' % (chunk['file'], error))
            if len(data) != chunk['records'] * fmt['record_bytes']:
                raise CommandError('%s: wrong record count' % chunk['file'])
            if hashlib.sha256(data).hexdigest() != chunk['raw_sha256']:
                raise CommandError('%s: raw SHA-256 mismatch' % chunk['file'])
            keys = OpenBench.relabel.key_projection(data, fmt)
            if hashlib.sha256(keys).hexdigest() != chunk['keys_sha256']:
                raise CommandError('%s: position-key SHA-256 mismatch' % chunk['file'])

        self.stdout.write(
            'RELABEL input set %s OK: %d chunks, %d records, format %s, manifest %s'
            % (
                set_name, len(manifest['chunks']), manifest['total_records'],
                manifest['format'], OpenBench.relabel.manifest_sha256(manifest),
            )
        )
