"""Run the proof-directed planner (phase 1: shadow mode only).

    manage.py run_planner --mode shadow [--interval 300] [--batch 512]

Loads the searched subgraph into memory (read-only, minutes on production),
then every ``--interval`` seconds refreshes it from what moved, selects the
batch it WOULD serve and records it with the cycle's metrics in
``PlannerShadowCycle`` / ``PlannerShadowPick``.  It serves nothing: the
lease path does not know it exists.  See atomicdb/planner/__init__.py and
docs/atomicdb-planner-design.md.

GUARDS.  One instance (an exclusive lock file in ``ATOMICDB_STATE_DIR``),
a lifetime cap (``--max-hours``, then a clean exit: systemd restarts it with
a fresh full load, which is also the reconciliation of anything the
incremental refresh missed) and a memory cap (``--max-rss-mb``: exit 3 when
the process grows past it).  SIGTERM/SIGINT stop it between cycles.

``--mode`` defaults to ``ATOMICDB_PLANNER`` (else ``shadow``).  ``off``
exits at once; ``fill``, ``auto`` and ``idle`` are phase 2 and refused.
"""

import json
import os
import signal
import sys
import tempfile
import time

from django.core.management.base import BaseCommand, CommandError

from atomicdb.management.commands.refresh_selector import     _drop_dead_connections
from atomicdb.planner import MODES, PHASE2_MODES
from atomicdb.planner.costmodel import load_model, state_dir


def acquire_lock(path):
    """Exclusive, non-blocking lock on ``path``; the handle must stay open."""
    fh = open(path, 'a+')
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:                    # pragma: no cover - Windows
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


class Command(BaseCommand):
    help = 'Run the AtomicDB proof-directed planner (shadow mode).'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default=None,
                            help='off | shadow (default: $ATOMICDB_PLANNER '
                                 'or shadow). fill/auto/idle are phase 2.')
        parser.add_argument('--interval', type=float, default=300.0,
                            help='Seconds between cycles (default: 300).')
        parser.add_argument('--batch', type=int, default=512,
                            help='Picks per shadow batch (default: 512).')
        parser.add_argument('--once', action='store_true',
                            help='Load, run one cycle, exit.')
        parser.add_argument('--no-record', action='store_true',
                            help='With --once: print the cycle, write no row.')
        parser.add_argument('--max-hours', type=float, default=72.0,
                            help='Lifetime cap; exit cleanly after it.')
        parser.add_argument('--max-rss-mb', type=float, default=4096.0,
                            help='Exit with status 3 past this RSS.')
        parser.add_argument('--retention-days', type=float, default=7.0,
                            help='Prune shadow cycles older than this.')
        parser.add_argument('--full-every-hours', type=float, default=6.0,
                            help='Full recompute at least this often.')
        parser.add_argument('--tol', type=float, default=1e-3,
                            help='Relative change that stops the climb.')
        parser.add_argument('--vl', type=float, default=1.0,
                            help='Virtual loss: in-flight pn x (1 + vl).')
        parser.add_argument('--spread', type=float, default=4.0,
                            help='Batch stop: no White move within this '
                                 'factor of the best pn.')
        parser.add_argument('--strategy-cap', type=int, default=200000,
                            help='Nodes visited by the strategy walk.')
        parser.add_argument('--load-batch', type=int, default=1000,
                            help='Parents per children query.')
        parser.add_argument('--model', default=None,
                            help='Cost model JSON (default: state dir, '
                                 'else the prototype fit).')
        parser.add_argument('--lock-file', default=None)

    def handle(self, *args, **opts):
        mode = (opts['mode'] or os.environ.get('ATOMICDB_PLANNER')
                or 'shadow').strip().lower()
        if mode in PHASE2_MODES:
            raise CommandError('mode %r is phase 2 and not implemented; '
                               'only shadow runs' % mode)
        if mode not in MODES:
            raise CommandError('unknown planner mode %r' % mode)
        if mode == 'off':
            self.stdout.write(json.dumps({'mode': 'off', 'ran': False}))
            return
        if opts['no_record'] and not opts['once']:
            raise CommandError('--no-record needs --once')

        lock_path = opts['lock_file'] or os.path.join(
            state_dir() or tempfile.gettempdir(), 'atomicdb-planner.lock')
        lock = acquire_lock(lock_path)
        if lock is None:
            raise CommandError('another run_planner holds %s' % lock_path)

        from atomicdb.planner.shadow import ShadowRunner, rss_mb
        stopping = {'now': False}

        def stop(signum, frame):
            del signum, frame
            stopping['now'] = True

        for name in ('SIGINT', 'SIGTERM'):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, stop)
                except (ValueError, OSError):
                    pass

        model = load_model(opts['model']) if opts['model'] else load_model()
        runner = ShadowRunner(
            batch=opts['batch'], retention_days=opts['retention_days'],
            full_every_hours=opts['full_every_hours'], vl=opts['vl'],
            spread=opts['spread'], tol=opts['tol'],
            strategy_cap=opts['strategy_cap'], load_batch=opts['load_batch'],
            model=model, log=lambda m: self.stderr.write(m))
        born = time.monotonic()
        try:
            load = runner.start()
            self.stdout.write(json.dumps({'event': 'loaded', 'mode': mode,
                                          'model': model.version, **load},
                                         sort_keys=True, default=str))
            failures = 0
            while True:
                started = time.monotonic()
                _drop_dead_connections()
                try:
                    report = runner.cycle(record=not opts['no_record'])
                    failures = 0
                except Exception as error:      # noqa: BLE001
                    if opts['once']:
                        raise
                    failures += 1
                    report = {'failed': '%s: %s' % (type(error).__name__,
                                                    error)}
                    if failures >= 5:
                        raise
                self.stdout.write(json.dumps({'event': 'cycle', **report},
                                             sort_keys=True, default=str))
                if opts['once'] or stopping['now']:
                    break
                rss = rss_mb()
                if rss is not None and rss > opts['max_rss_mb']:
                    self.stderr.write('run_planner: RSS %.0f MB over the cap '
                                      '%.0f MB, exiting'
                                      % (rss, opts['max_rss_mb']))
                    sys.exit(3)
                if time.monotonic() - born > opts['max_hours'] * 3600:
                    self.stdout.write(json.dumps({'event': 'lifetime-cap'}))
                    break
                deadline = started + opts['interval']
                while not stopping['now'] and time.monotonic() < deadline:
                    time.sleep(min(1.0, max(0.05,
                                            deadline - time.monotonic())))
                if stopping['now']:
                    break
        finally:
            lock.close()
