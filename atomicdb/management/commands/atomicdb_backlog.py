"""Fill and inspect the community backlogs (see ``atomicdb.backlog``).

    manage.py atomicdb_backlog status
    manage.py atomicdb_backlog load-lost-lines --file KEYS [--skip N]
    manage.py atomicdb_backlog scan-shallow --state-file PATH
                                           [--batch 2000] [--pause 0.5]
                                           [--max-steps N] [--dry-run]

``load-lost-lines`` reads one position key per line and adds the ones not on
the list yet; ``--skip N`` leaves out the first N lines (the ones a drip
already queued).  Running it twice adds nothing the second time.

``scan-shallow`` walks the completed searches of 128M or more by id, newest
first, ``--batch`` ids per step with ``--pause`` seconds between steps, and
adds the open positions whose shown search is a shallow 128M one.  Each step
is an id range on the task table and a lookup of its positions by key; the
position table is never scanned.  With ``--state-file`` it saves where it
stands after every step and resumes from there when started again; a file
that says it finished starts nothing.  Delete the file to scan again.

Production, 15-Nov-2026: about 4.1M task ids, nine in ten of them a
completed search of 128M or more; a step of 2,000 ids takes 0.5 to 1.5 s
with a cold cache, so the whole walk is about 2,050 steps, 30 to 70 minutes
of queries plus the pauses (17 minutes at 0.5 s): one to one and a half
hours.  Run it niced, under ``flock -n``, so only one copy runs.
"""

import json
import os
import time

from django.core.management.base import BaseCommand, CommandError

from atomicdb import backlog


def _read_state(path):
    try:
        with open(path, encoding='utf-8') as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path, state):
    partial = path + '.tmp'
    with open(partial, 'w', encoding='utf-8') as handle:
        json.dump(state, handle)
    os.replace(partial, path)


class Command(BaseCommand):
    help = 'Fill and inspect the community backlogs.'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['status', 'load-lost-lines',
                                               'scan-shallow'])
        parser.add_argument('--file', help='keys to load, one per line')
        parser.add_argument('--skip', type=int, default=0,
                            help='leave out the first N lines of --file')
        parser.add_argument('--state-file',
                            help='where scan-shallow saves where it stands')
        parser.add_argument('--batch', type=int, default=2_000)
        parser.add_argument('--pause', type=float, default=0.5)
        parser.add_argument('--max-steps', type=int, default=None)
        parser.add_argument('--dry-run', action='store_true',
                            help='scan-shallow: read only, add nothing')

    def handle(self, *args, **options):
        action = options['action']
        if action == 'status':
            return self._status()
        if action == 'load-lost-lines':
            return self._load(options)
        return self._scan(options)

    def _status(self):
        for name, numbers in backlog.counts(fresh=True).items():
            self.stdout.write(
                f"{name}: open={numbers['open']} "
                f"adopted={numbers['adopted']} "
                f"adopters={numbers['adopters']}")

    def _load(self, options):
        path = options['file']
        if not path:
            raise CommandError('load-lost-lines needs --file')
        skip = max(0, options['skip'])
        with open(path, encoding='utf-8') as handle:
            keys = [line for line in handle][skip:]
        added, known, invalid = backlog.load_keys(backlog.LOST_LINES, keys)
        self.stdout.write(f'lost-lines: added={added} already={known} '
                          f'invalid={invalid} skipped_lines={skip}')

    def _scan(self, options):
        path = options['state_file']
        dry_run = options['dry_run']
        state = _read_state(path) if path else {}
        if state.get('done') and not dry_run:
            self.stdout.write(f'scan-shallow: already finished ({path}); '
                              'delete the file to scan again')
            return
        cursor = state.get('cursor')
        if not isinstance(cursor, int) or cursor <= 0:
            cursor = None
        started = time.time()
        totals = {'read': state.get('read', 0),
                  'added': state.get('added', 0)}

        def on_step(at, read, added):
            totals['read'] += read
            totals['added'] += added
            if path and not dry_run:
                _write_state(path, {'cursor': at, 'done': at <= 0,
                                    **totals})
            self.stdout.write(f'cursor={at} read={read} added={added} '
                              f'total_added={totals["added"]} '
                              f'elapsed={time.time() - started:.0f}s')

        result = backlog.scan_shallow(
            cursor=cursor, batch=max(1, options['batch']),
            pause=max(0.0, options['pause']),
            max_steps=options['max_steps'], on_step=on_step,
            dry_run=dry_run)
        verb = 'would add' if dry_run else 'added'
        self.stdout.write(
            f"scan-shallow: steps={result['steps']} read={result['read']} "
            f"{verb}={result['added']} cursor={result['cursor']} "
            f"{'finished' if result['cursor'] <= 0 else 'stopped'} in "
            f'{time.time() - started:.0f}s')
