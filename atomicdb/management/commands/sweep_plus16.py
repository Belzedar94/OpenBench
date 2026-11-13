"""The +16 sweep, by hand or from a timer (see ``atomicdb.sweep``).

    manage.py sweep_plus16 --status        what is queued, served, left
    manage.py sweep_plus16 --rebuild       walk the tree, scan, store the list
    manage.py sweep_plus16 --rebuild --dry-run   the same without storing it
    manage.py sweep_plus16 --drip          one step now (the selector service
                                           does this every pass by itself)
    manage.py sweep_plus16 --rewind        walk the current list again from
                                           its head (a running selector takes
                                           the new place when it starts)
    manage.py sweep_plus16 --stop          delete the list: nothing more queued
    manage.py sweep_plus16 --withdraw      also remove the PENDING sweep tasks

``--rebuild`` reads the database for minutes (about 1.75M positions in the
tree walk and one scan of the open positions in 256 short statements) and
writes nothing to it; it is meant for a daily timer, at low priority.  Only
one copy runs at a time: a second one exits at once.
"""

import json
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from atomicdb import sweep

LOCK_FILE = 'sweep16.lock'
# A lock older than this is from a copy that died without cleaning up.
LOCK_STALE_SECONDS = 6 * 3600


def _lock_path():
    root = str(getattr(settings, 'ATOMICDB_STATE_DIR', '') or '')
    return os.path.join(root, LOCK_FILE) if root else None


class _SingleCopy:
    """An O_EXCL lock file in the state directory, with a stale age."""

    def __init__(self):
        self.path = _lock_path()
        self.held = False

    def __enter__(self):
        if self.path is None:
            return self
        try:
            if time.time() - os.path.getmtime(self.path) > LOCK_STALE_SECONDS:
                os.remove(self.path)
        except OSError:
            pass
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise CommandError('another sweep_plus16 --rebuild is running '
                               f'({self.path})') from None
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        self.held = True
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


class Command(BaseCommand):
    help = 'The +16 sweep: build its list, step it, inspect it or stop it.'

    def add_arguments(self, parser):
        parser.add_argument('--status', action='store_true')
        parser.add_argument('--rebuild', action='store_true')
        parser.add_argument('--dry-run', action='store_true',
                            help='With --rebuild: report, store nothing.')
        parser.add_argument('--max-nodes', type=int,
                            default=sweep.TREE_MAX_NODES)
        parser.add_argument('--max-plies', type=int,
                            default=sweep.TREE_MAX_PLIES)
        parser.add_argument('--time-limit', type=float, default=7200.0,
                            help='Seconds for the whole rebuild (0: none).')
        parser.add_argument('--drip', action='store_true')
        parser.add_argument('--cap', type=int,
                            default=sweep.SWEEP_PENDING_CAP)
        parser.add_argument('--rewind', action='store_true')
        parser.add_argument('--stop', action='store_true')
        parser.add_argument('--withdraw', action='store_true')

    def handle(self, *args, **options):
        out = {}
        if options['stop'] or options['withdraw']:
            sweep.forget_plan()
            out['stopped'] = True
            if options['withdraw']:
                out['withdrawn'] = sweep.withdraw_pending()
        if options['rebuild']:
            with _SingleCopy():
                plan = sweep.build_plan(
                    max_nodes=options['max_nodes'],
                    max_plies=options['max_plies'],
                    time_limit=options['time_limit'],
                    save=not options['dry_run'])
            out['rebuilt'] = {key: plan[key] for key in (
                'build_id', 'built_at', 'tree_count', 'rest_count',
                'tree_stats', 'scan_stats', 'seconds')}
            out['rebuilt']['stored'] = not options['dry_run']
        if options['rewind']:
            out['rewound'] = sweep.rewind()
        if options['drip']:
            out['queued'] = sweep.drip(cap=options['cap'])
        if options['status'] or not out:
            out['status'] = sweep.status()
        self.stdout.write(json.dumps(out, sort_keys=True, default=str))
