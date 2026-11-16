"""Fill and inspect the community backlogs (see ``atomicdb.backlog``).

    manage.py atomicdb_backlog status
    manage.py atomicdb_backlog top-up
    manage.py atomicdb_backlog load-lost-lines --file KEYS [--skip N]
    manage.py atomicdb_backlog load-clear-wins [--file PLAN] [--skip N]
                                              [--limit N]
    manage.py atomicdb_backlog scan-shallow --state-file PATH
                                           [--batch 2000] [--pause 0.5]
                                           [--max-steps N] [--dry-run]

``load-lost-lines`` reads one position key per line and adds the ones not on
the list yet; ``--skip N`` leaves out the first N lines (the ones a drip
already queued).  Running it twice adds nothing the second time.

``load-clear-wins`` adds the +16 sweep's list (``sweep16.json`` in
``ATOMICDB_STATE_DIR``, written by ``sweep_plus16 --rebuild``; or ``--file``,
that JSON or one key per line) to the clear-wins backlog, in its order:
White's strategy tree first.  Keys whose position no longer qualifies are
left out.  ``--skip N`` and ``--limit N`` take a slice of the list.  Running
it again adds only what is new, so a run cut short is resumed that way, and
the list of a later rebuild is added behind what is already there.

``top-up`` runs one pass of the subscriptions' top-up by hand, the same
pass ``warm_public_snapshot`` runs every five minutes.

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
from django.db.models import Count

from atomicdb import backlog, sweep
from atomicdb.models import BacklogSubscription


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
                                               'load-clear-wins',
                                               'scan-shallow', 'top-up'])
        parser.add_argument('--file', help='keys to load, one per line '
                            '(load-clear-wins: also the sweep\'s JSON list)')
        parser.add_argument('--skip', type=int, default=0,
                            help='leave out the first N keys')
        parser.add_argument('--limit', type=int, default=None,
                            help='load-clear-wins: at most N keys after '
                                 '--skip')
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
        if action == 'load-clear-wins':
            return self._load_clear_wins(options)
        if action == 'top-up':
            summary = backlog.top_up()
            self.stdout.write(' '.join(f'{key}={value}' for key, value
                                       in summary.items()))
            return
        return self._scan(options)

    def _status(self):
        subscribed = dict(BacklogSubscription.objects.filter(active=True)
                          .values_list('backlog')
                          .annotate(n=Count('id')))
        for name, numbers in backlog.counts(fresh=True).items():
            self.stdout.write(
                f"{name}: open={numbers['open']} "
                f"adopted={numbers['adopted']} "
                f"adopters={numbers['adopters']} "
                f"subscribed={subscribed.get(name, 0)}")

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

    def _load_clear_wins(self, options):
        path = options['file']
        if path:
            with open(path, encoding='utf-8') as handle:
                text = handle.read()
            try:
                keys = json.loads(text)['keys']
            except (ValueError, TypeError, KeyError):
                keys = text.splitlines()
            source = path
        else:
            plan = sweep.load_plan()
            if not plan:
                raise CommandError('no sweep list: run sweep_plus16 --rebuild '
                                   'or give --file')
            keys = plan['keys']
            source = f"sweep list {plan.get('build_id', '')}"
        if not isinstance(keys, list):
            raise CommandError(f'{source}: no list of keys')
        skip = max(0, options['skip'])
        keys = keys[skip:]
        if options['limit'] is not None:
            keys = keys[:max(0, options['limit'])]
        added, known, left_out = backlog.load_clear_wins(keys)
        self.stdout.write(f'{backlog.CLEAR_WINS}: added={added} '
                          f'already={known} left_out={left_out} '
                          f'read={len(keys)} skipped={skip} from {source}')

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
