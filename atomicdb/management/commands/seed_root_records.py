"""Seed ``RootRecord`` once from the log of the record announcements.

The events feed has announced every new best of the start position since
1-Nov-2026 and logs one line per announcement::

    2026-11-01T23:36:54+00:00 host python[123]: evento [record] New record
    for the start position: +12.57

(one line in the log).  Feed it ``journalctl -u discord-events-feed -o
short-iso``, as a file or on stdin.  A row is stamped with the time of the
announcement, up to 15 minutes after the value.  A value already in the
table is skipped (the hourly capture may have recorded it first, with the
exact time), so running it twice adds nothing.  ``--dry-run`` prints the rows
without writing them.
"""

import re
import sys
from datetime import datetime

from django.core.management.base import BaseCommand

from atomicdb import logic
from atomicdb.database import atomic
from atomicdb.models import RootRecord

LINE = re.compile(
    r'^(?P<at>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2})\s.*'
    r'New record for the start position: (?P<value>[+−-]?\d+\.\d{2})\s*$')


def parse(lines):
    """``[(at, centipawns)]`` from the log lines that announce a record."""
    rows = []
    for line in lines:
        match = LINE.match(line.strip())
        if match is None:
            continue
        at = datetime.fromisoformat(match['at'])
        text = match['value'].replace('−', '-').replace('+', '')
        rows.append((at, round(float(text) * 100)))
    return rows


class Command(BaseCommand):
    help = __doc__.splitlines()[0]

    def add_arguments(self, parser):
        parser.add_argument('path', nargs='?', default='-',
                            help='The log, or - for stdin (the default).')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, path='-', dry_run=False, **options):
        if path == '-':
            rows = parse(sys.stdin)
        else:
            with open(path, encoding='utf-8') as fh:
                rows = parse(fh)
        key = logic.key_of(logic.start_fen())
        with atomic():
            known = set(RootRecord.objects.select_for_update()
                        .filter(key=key).values_list('value', flat=True))
            added = []
            for at, value in sorted(rows):
                if value in known:
                    continue
                added.append((at, value))
                known.add(value)
            if not dry_run:
                RootRecord.objects.bulk_create(
                    RootRecord(key=key, value=value, at=at)
                    for at, value in added)
        for at, value in added:
            self.stdout.write(f'{at.isoformat()} {value}')
        self.stdout.write(
            f"{'would add' if dry_run else 'added'} {len(added)} of "
            f'{len(rows)} announced records')
