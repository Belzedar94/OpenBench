"""Fill and inspect the community backlogs (see ``atomicdb.backlog``).

    manage.py atomicdb_backlog status
    manage.py atomicdb_backlog top-up
    manage.py atomicdb_backlog load-lost-lines --file KEYS [--skip N]
    manage.py atomicdb_backlog load-clear-wins [--file PLAN] [--skip N]
                                              [--limit N]
    manage.py atomicdb_backlog scan-shallow --state-file PATH
                                           [--batch 2000] [--pause 0.5]
                                           [--max-steps N] [--dry-run]
    manage.py atomicdb_backlog load-deep-wins --state-file PATH
                                             [--batch 2000] [--pause 0.5]
                                             [--max-steps N] [--dry-run]
                                             [--tree] [--tree-seconds 1800]
                                             [--below-mate]
    manage.py atomicdb_backlog load-best-line --state-file PATH
                                             [--batch 2000] [--pause 0.5]
                                             [--max-steps N] [--dry-run]
                                             [--tree-seconds 1800]
                                             [--max-plies 200]

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

``load-deep-wins`` fills the deep-wins backlog in two halves.  The scan walks
the completed searches by id exactly like ``scan-shallow`` (same steps, same
cost) and keeps the positions that qualify now: open, ``visits == 2``, the
side to move at +25 or more.  What it finds goes to ``PATH.found.jsonl``,
appended after each step, and where it stands to ``PATH``, so a scan cut
short resumes there.  Only when the walk reaches the oldest search is the
list loaded, all at once, in its order: with ``--tree``, first the ones a
walk of White's strategy tree meets (the sweep's walk, minutes of reads,
capped by ``--tree-seconds``), then the rest, the highest value first.
``--below-mate`` leaves out the values in the mate band.  The state file
then says it finished, and running again does nothing; delete both files to
scan again (a later load adds only what is new, behind what is there).
``--dry-run`` reads only and says what it would add.

``load-best-line`` fills White's best line in two halves.  First the sweep's
walk of White's strategy tree (White the move its value stands on, Black
every open reply; capped by ``--tree-seconds`` and ``--max-plies``) writes
every open position it meets, both colours, the shallowest first, to
``PATH.walk``.  Then the positions are read back by key, ``--batch`` per
step with ``--pause`` seconds between steps, and the ones whose shown search
ran under 128M nodes, reached a depth under 14 and found no mate are added,
in the walk's order.  ``PATH`` says where it stands after every step: a run
cut short during the checks resumes there, one cut short during the walk
walks again.  The state file then says it finished, and running again does
nothing; delete both files to walk again (a later load adds only what is
new, behind what is there).  ``--dry-run`` reads only and says what it
would add.
"""

import json
import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from atomicdb import backlog, sweep
from atomicdb.models import AnalysisTask, BacklogItem, BacklogSubscription

# Where load-deep-wins keeps what its scan found, next to its state file.
FOUND_SUFFIX = '.found.jsonl'
# Where load-best-line keeps the positions its walk met, one key per line.
WALK_SUFFIX = '.walk'


def _read_found(path):
    """``{key: (value, priority)}`` from the scan's file; later lines win."""
    found = {}
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                try:
                    key, value, priority = json.loads(line)
                except (ValueError, TypeError):
                    continue        # a line cut short by a stop
                if isinstance(key, str) and isinstance(value, int):
                    found[key] = (value, priority)
    except OSError:
        pass
    return found


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
                                               'load-deep-wins',
                                               'load-best-line',
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
                            help='scan-shallow, load-deep-wins: read only, '
                                 'add nothing')
        parser.add_argument('--tree', action='store_true',
                            help='load-deep-wins: White\'s strategy tree '
                                 'first')
        parser.add_argument('--tree-seconds', type=float, default=1800.0,
                            help='load-deep-wins, load-best-line: at most '
                                 'this long walking the tree')
        parser.add_argument('--max-plies', type=int, default=None,
                            help='load-best-line: walk at most this many '
                                 'plies from the start position')
        parser.add_argument('--below-mate', action='store_true',
                            help='load-deep-wins: leave out the values in '
                                 'the mate band')

    def handle(self, *args, **options):
        action = options['action']
        if action == 'status':
            return self._status()
        if action == 'load-lost-lines':
            return self._load(options)
        if action == 'load-clear-wins':
            return self._load_clear_wins(options)
        if action == 'load-deep-wins':
            return self._load_deep_wins(options)
        if action == 'load-best-line':
            return self._load_best_line(options)
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

    def _load_deep_wins(self, options):
        path = options['state_file']
        dry_run = options['dry_run']
        name = backlog.DEEP_WINS
        state = _read_state(path) if path else {}
        if state.get('loaded') and not dry_run:
            self.stdout.write(f'{name}: already finished ({path}); delete it '
                              f'and {path}{FOUND_SUFFIX} to scan again')
            return
        found_path = path + FOUND_SUFFIX if path else None
        found = _read_found(found_path) if found_path else {}
        started = time.time()
        totals = {'read': state.get('read', 0)}
        cursor = state.get('cursor')
        if not isinstance(cursor, int) or cursor <= 0:
            cursor = None
        if cursor is None and not state.get('done'):
            cursor = 1 + (AnalysisTask.objects.order_by('-id')
                          .values_list('id', flat=True).first() or 0)
        steps = 0
        while cursor is not None and cursor > 0:
            if (options['max_steps'] is not None
                    and steps >= options['max_steps']):
                break
            cursor, read, rows = backlog.scan_deep_step(
                cursor, max(1, options['batch']))
            steps += 1
            totals['read'] += read
            for key, value, priority in rows:
                found[key] = (value, priority)
            if path and not dry_run:
                if rows:
                    with open(found_path, 'a', encoding='utf-8') as handle:
                        for row in rows:
                            handle.write(json.dumps(list(row)) + '\n')
                _write_state(path, {'cursor': cursor, 'done': cursor <= 0,
                                    'found': len(found), **totals})
            self.stdout.write(f'cursor={cursor} read={read} '
                              f'qualifying={len(rows)} found={len(found)} '
                              f'elapsed={time.time() - started:.0f}s')
            if cursor > 0 and options['pause']:
                time.sleep(max(0.0, options['pause']))
        if cursor is not None and cursor > 0:
            self.stdout.write(f'{name}: stopped at cursor={cursor} after '
                              f'{steps} steps, found={len(found)}; run it '
                              'again to go on')
            return
        tree = []
        if options['tree']:
            deadline = time.monotonic() + max(0.0, options['tree_seconds'])
            tree, stats = sweep.walk_strategy_tree(
                deadline=deadline, match=backlog.deep_tree_match)
            self.stdout.write(f"tree walk: {len(tree)} found, "
                              f"{stats['nodes']} positions, "
                              f"stopped={stats['stopped']}")
        keys = backlog.order_deep_wins(found, tree,
                                       below_mate=options['below_mate'])
        head = len(set(tree) & set(keys))
        if dry_run:
            known = set()
            for start in range(0, len(keys), 2_000):
                known.update(BacklogItem.objects.filter(
                    backlog=name, position_key__in=keys[start:start + 2_000])
                    .values_list('position_key', flat=True))
            self.stdout.write(f'{name}: found={len(found)} '
                              f'would add={len(set(keys) - known)} '
                              f'tree_first={head} (dry run)')
            return
        added, known, invalid = backlog.load_keys(name, keys)
        if path:
            _write_state(path, {'cursor': 0, 'done': True, 'loaded': True,
                                'found': len(found), 'added': added,
                                **totals})
        self.stdout.write(f'{name}: found={len(found)} added={added} '
                          f'already={known} left_out={len(found) - len(keys)}'
                          f' tree_first={head} in '
                          f'{time.time() - started:.0f}s')

    def _load_best_line(self, options):
        path = options['state_file']
        if not path:
            raise CommandError('load-best-line needs --state-file')
        dry_run = options['dry_run']
        name = backlog.BEST_LINE
        state = _read_state(path)
        if state.get('loaded') and not dry_run:
            self.stdout.write(f'{name}: already finished ({path}); delete it '
                              f'and {path}{WALK_SUFFIX} to walk again')
            return
        walk_path = path + WALK_SUFFIX
        started = time.time()
        if dry_run or not state.get('walked'):
            keys = []
            deadline = time.monotonic() + max(0.0, options['tree_seconds'])
            stats = backlog.walk_best_line(keys.extend, deadline=deadline,
                                           max_plies=options['max_plies'])
            self.stdout.write(f"tree walk: {len(keys)} positions, "
                              f"{stats['plies']} plies, "
                              f"stopped={stats['stopped']} in "
                              f'{time.time() - started:.0f}s')
            if not dry_run:
                partial = walk_path + '.tmp'
                with open(partial, 'w', encoding='utf-8') as handle:
                    for key in keys:
                        handle.write(key + '\n')
                os.replace(partial, walk_path)
                state = {'walked': True, 'walk_positions': len(keys),
                         'walk_stopped': stats['stopped'], 'checked': 0,
                         'found': 0, 'added': 0}
                _write_state(path, state)
        else:
            with open(walk_path, encoding='utf-8') as handle:
                keys = [line.strip() for line in handle if line.strip()]
        checked = 0 if dry_run else max(0, state.get('checked', 0))
        totals = {'found': 0 if dry_run else state.get('found', 0),
                  'added': 0 if dry_run else state.get('added', 0)}
        batch = max(1, options['batch'])
        steps = 0
        while checked < len(keys):
            if (options['max_steps'] is not None
                    and steps >= options['max_steps']):
                break
            part = keys[checked:checked + batch]
            found = backlog.best_line_check(part)
            if dry_run:
                known = set(BacklogItem.objects.filter(
                    backlog=name, position_key__in=found)
                    .values_list('position_key', flat=True)) if found else ()
                added = len(set(found) - set(known))
            else:
                added = backlog.load_keys(name, found)[0] if found else 0
            checked += len(part)
            steps += 1
            totals['found'] += len(found)
            totals['added'] += added
            if not dry_run:
                state.update(checked=checked, **totals)
                _write_state(path, state)
            self.stdout.write(f'checked={checked}/{len(keys)} '
                              f'qualifying={len(found)} '
                              f"found={totals['found']} "
                              f"added={totals['added']} "
                              f'elapsed={time.time() - started:.0f}s')
            if checked < len(keys) and options['pause']:
                time.sleep(max(0.0, options['pause']))
        if checked < len(keys):
            self.stdout.write(f'{name}: stopped at {checked}/{len(keys)} '
                              f"after {steps} steps, found={totals['found']}; "
                              'run it again to go on')
            return
        if dry_run:
            self.stdout.write(f"{name}: walked={len(keys)} "
                              f"found={totals['found']} "
                              f"would add={totals['added']} (dry run)")
            return
        state.update(loaded=True)
        _write_state(path, state)
        self.stdout.write(f"{name}: walked={len(keys)} "
                          f"found={totals['found']} added={totals['added']} "
                          f'already={totals["found"] - totals["added"]} in '
                          f'{time.time() - started:.0f}s')

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
