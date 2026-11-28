"""One line per drainer batch and per applied job, for the v1 baseline.

WHY (28-Nov-2026).  The AtomicDB v2 plan (docs/atomicdb/v2-plan.md, section 9)
measures every phase against the v1 baseline: searches applied per second,
submit-to-visible, climb batch latency, and the rest.  Most of those numbers
are already in the tables (``IngestJob.created`` and ``updated``) or in
nginx, but how long ONE climb of the drainer takes is written nowhere: the
job summaries say how many landings a batch merged, not how long it held its
locks.  This module writes that, and the time of each stage (a) job, as one
JSON line per event to a daily file that ``Scripts/atomicdb_perf_sample.py``
reads every minute.

OFF unless ``ATOMICDB_PERF_DIR`` names a directory.  Writing is one
``os.write`` of a short line to a file opened with O_APPEND, so the eight
ingest writers and the drainer can share a file without locks (POSIX makes
appends of less than PIPE_BUF bytes atomic) and the cost is a few
microseconds.  Any error is swallowed: a measurement never fails a climb.
Nothing here reads or changes the tree.
"""

import contextlib
import json
import os
import time

ENV_DIR = 'ATOMICDB_PERF_DIR'

_state = {'dir': None, 'checked': False, 'fd': None, 'day': None}


def perf_dir():
    """The directory events go to, or None (read once per process)."""
    if not _state['checked']:
        _state['checked'] = True
        path = os.environ.get(ENV_DIR, '').strip()
        _state['dir'] = path if path and os.path.isdir(path) else None
    return _state['dir']


def _reset_for_tests():
    if _state['fd'] is not None:
        try:
            os.close(_state['fd'])
        except OSError:
            pass
    _state.update({'dir': None, 'checked': False, 'fd': None, 'day': None})


def _fd(now):
    day = time.strftime('%Y%m%d', time.gmtime(now))
    if _state['fd'] is None or _state['day'] != day:
        if _state['fd'] is not None:
            try:
                os.close(_state['fd'])
            except OSError:
                pass
        path = os.path.join(_state['dir'], f'events-{day}.jsonl')
        _state['fd'] = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                               0o644)
        _state['day'] = day
    return _state['fd']


def event(kind, **fields):
    """Append one event line (no-op when the directory is not configured)."""
    if perf_dir() is None:
        return
    try:
        now = time.time()
        line = json.dumps({'t': round(now, 3), 'kind': kind, 'pid': os.getpid(),
                           **fields}, separators=(',', ':'), default=str)
        os.write(_fd(now), (line + '\n').encode())
    except Exception:       # noqa: BLE001 - a measurement never fails the work
        pass


class _Timer:
    __slots__ = ('fields',)

    def __init__(self, fields):
        self.fields = fields

    def set(self, **fields):
        self.fields.update(fields)


@contextlib.contextmanager
def timed(kind, **fields):
    """Time the block and write one event with its duration in ``ms``.

    The block can add fields with ``timer.set(...)``.  An exception in the
    block is recorded (``error``) and re-raised untouched.
    """
    if perf_dir() is None:
        yield _Timer(fields)
        return
    timer = _Timer(dict(fields))
    started = time.perf_counter()
    try:
        yield timer
    except BaseException as error:
        timer.fields['error'] = type(error).__name__
        raise
    finally:
        ms = (time.perf_counter() - started) * 1000.0
        event(kind, ms=round(ms, 2), **timer.fields)
