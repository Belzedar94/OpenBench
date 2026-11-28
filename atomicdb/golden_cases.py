"""Golden cases: v1's own climbs and landings, recorded for v2 to replay.

WHY (28-Nov-2026).  The v2 plan (docs/atomicdb/v2-plan.md, section 7.2) ports
v1's value rules exactly before it simplifies anything, and checks the port
on cases taken from production: for a sampled fraction of the drainer's
batches (``propagation_queue.drain_batch``, the deferred climb) and of the
stage (a) landings (``ingest_queue.apply_job``), a self-contained record of
everything the batch read from the database, its inputs, and everything it
wrote.  v2 replays each case in isolation, without the whole graph and
without the global order, and must write the same rows.

WHAT A CASE HOLDS (one JSON object per line, zstd-compressed files):

* ``inputs``: what the caller handed over (the job and its payload for stage
  (a); the batch limit for a climb, whose PropagationSeed rows are the
  result of its first SELECT and are copied to ``seed_rows``);
* ``queries``: every statement the batch ran on the AtomicDB connection, in
  order, with its parameters, its row count, and for a SELECT (or a
  RETURNING) the rows it returned with the table and column of every field,
  so the first image of every row the batch read can be rebuilt
  (``pre_image``);
* ``commits``: at each COMMIT (and at a ROLLBACK, which is marked), the rows
  of ``atomicdb_position`` written so far, read inside the transaction just
  before it commits: the columns that differ from what the case FIRST read
  of that row (its pre-image), plus the columns its UPDATEs set that it
  never read; a row it never read (inserted) comes whole.  Edges inserted
  are listed from the INSERTs;
* ``constants`` and ``settings`` (the module constants of the rules and the
  ATOMICDB settings in force), ``code`` (sha256 of the rule modules), the
  wall-clock start and end (v1 still has wall-clock budgets: owner decision
  D1), the result or the error, and the recorder's own overhead.

What it does not see: the proof-number refresh that runs on its own thread
with its own connection (Ã‚Â§ propagation_queue.ProofPipeline: pn/dn only order
the selector), anything on the ``default`` database (accounts), and rows
read through a server-side cursor (``.iterator()``), whose statement is
logged without rows (``stats.uncaptured_selects``).  A position UPDATE whose
parameters name no key is counted in ``stats.unkeyed_position_writes``.

HOW IT STAYS CHEAP.  Off unless ``ATOMICDB_GOLDEN_CASES`` is set.  An
unsampled batch pays one random draw.  A sampled batch installs a Django
execute wrapper on the AtomicDB connection for its duration (results are read
once more and the client-side cursor is rewound, so the caller sees exactly
what it would have seen), and one extra SELECT by primary key before each
commit.  Serialisation runs on a background thread and compression in the
``zstd`` command, one frame per case appended to the file (a kill loses at
most the case being written); a full hand-over queue drops the case rather
than wait.  Output is size-capped: rotation every ``ATOMICDB_GOLDEN_ROTATE_MB``
of JSON, a hard cap of ``ATOMICDB_GOLDEN_MAX_GB`` over the whole directory,
and a stop while the disk has less than ``ATOMICDB_GOLDEN_MIN_FREE_GB`` free.
The rate can be changed without a restart through ``<dir>/control.json``
(``{"enabled": true, "rate": {"drain": 0.05, "stage_a": 0.01}}``), re-read
every 30 seconds.  Each process writes its counters to
``<dir>/status-<host>-<pid>.json`` once a minute.

A failure of the recorder never fails the batch: every capture step is
guarded, and the case is then marked or dropped.
"""

import atexit
import contextlib
import datetime
import decimal
import gzip
import hashlib
import json
import logging
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid

logger = logging.getLogger(__name__)

ENV_ENABLE = 'ATOMICDB_GOLDEN_CASES'
ENV_DIR = 'ATOMICDB_GOLDEN_DIR'
ENV_RATE = 'ATOMICDB_GOLDEN_RATE'
ENV_MAX_GB = 'ATOMICDB_GOLDEN_MAX_GB'
ENV_MIN_FREE_GB = 'ATOMICDB_GOLDEN_MIN_FREE_GB'
ENV_ROTATE_MB = 'ATOMICDB_GOLDEN_ROTATE_MB'

DEFAULT_DIR = '/var/lib/atomicdb-golden'
DEFAULT_RATE = 0.01
DEFAULT_MAX_GB = 20.0
DEFAULT_MIN_FREE_GB = 300.0
DEFAULT_ROTATE_MB = 256.0
KINDS = ('drain', 'stage_a')
CONTROL_SECONDS = 30.0
BUDGET_SECONDS = 60.0
STATUS_SECONDS = 60.0
QUEUE_DEPTH = 8
# A case larger than this (JSON bytes) keeps its statements and commits but
# loses the rows of its SELECTs, and says so.
MAX_CASE_BYTES = 96 * 1024 * 1024
# Rows of one table attributed per case at most for the last-image map (the
# diff of the post images); beyond it the post image keeps every column.
MAX_IMAGES = 2_000_000
# The rule modules whose source identifies the code a case ran.
CODE_MODULES = ('ingest', 'propagation_queue', 'ingest_queue', 'proof',
                'logic', 'score_bands', 'idle_repair', 'autosolve')
CONSTANT_MODULES = ('ingest', 'propagation_queue', 'ingest_queue', 'proof')
SECRET_WORDS = ('PASSWORD', 'SECRET', 'TOKEN', 'KEY', 'CREDENTIAL')

_KEY_RE = re.compile(r'^[0-9a-f]{64}$')
_WRITE_RE = re.compile(
    r'^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"?([A-Za-z0-9_]+)"?',
    re.IGNORECASE)
_INSERT_COLS_RE = re.compile(
    r'^\s*INSERT\s+INTO\s+"?[A-Za-z0-9_]+"?\s*\(([^)]*)\)\s*VALUES',
    re.IGNORECASE)
_SET_RE = re.compile(r'\bSET\b(.*?)(\bWHERE\b|\bRETURNING\b|$)',
                     re.IGNORECASE | re.DOTALL)
_SET_COL_RE = re.compile(r'"([A-Za-z0-9_]+)"\s*=')

_JOIN_RE = re.compile(
    r'"atomicdb_edge"\."(child_id|parent_id)"\s*=\s*'
    r'(?:T\d+|"atomicdb_position")\."key"', re.IGNORECASE)


def _join_pk_index(sql, cols, tables, table):
    """The column naming the row of a position joined from an edge whose
    primary key is not selected (``edge JOIN position T3 ON edge.child_id =
    T3.key``, how the climb reads children): that edge column, when the
    statement joins one position and through one edge column."""
    if table != 'atomicdb_position' or not sql:
        return None
    joins = set(_JOIN_RE.findall(sql))
    if (len(joins) != 1
            or len(re.findall(r'JOIN\s+"atomicdb_position"', sql,
                              re.IGNORECASE)) != 1):
        return None
    fk = joins.pop()
    for index, (col, owner) in enumerate(zip(cols, tables)):
        if owner == 'atomicdb_edge' and col == fk:
            return index
    return None


_local = threading.local()
# A private generator: drawing from the global one would move the state that
# any seeded use of `random` elsewhere relies on.
_rng = random.Random()


# ---------------------------------------------------------------- config

class _Config:
    """Environment, plus the control file re-read every CONTROL_SECONDS."""

    def __init__(self):
        env = os.environ
        self.enabled = env.get(ENV_ENABLE, '').strip().lower() in (
            '1', 'true', 'yes', 'on')
        self.dir = env.get(ENV_DIR, '').strip() or DEFAULT_DIR
        self.rates = {kind: _float(env.get(f'{ENV_RATE}_{kind.upper()}'),
                                   _float(env.get(ENV_RATE), DEFAULT_RATE))
                      for kind in KINDS}
        self.max_bytes = _float(env.get(ENV_MAX_GB), DEFAULT_MAX_GB) * 1e9
        self.min_free = (_float(env.get(ENV_MIN_FREE_GB), DEFAULT_MIN_FREE_GB)
                         * 1e9)
        self.rotate_bytes = (_float(env.get(ENV_ROTATE_MB), DEFAULT_ROTATE_MB)
                             * 1e6)
        self.control = {}
        self.control_read = 0.0

    def refresh(self, now):
        if now - self.control_read < CONTROL_SECONDS:
            return
        self.control_read = now
        try:
            with open(os.path.join(self.dir, 'control.json')) as handle:
                control = json.load(handle)
            self.control = control if isinstance(control, dict) else {}
        except (OSError, ValueError):
            self.control = {}

    def active(self):
        return bool(self.control.get('enabled', True))

    def rate(self, kind):
        rate = self.control.get('rate')
        if isinstance(rate, dict) and kind in rate:
            return _float(rate[kind], 0.0)
        if isinstance(rate, (int, float)):
            return float(rate)
        return self.rates.get(kind, 0.0)


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_config = {'current': None}


def config():
    if _config['current'] is None:
        _config['current'] = _Config()
    return _config['current']


def reset_for_tests():
    """Forget the configuration and stop the writer (tests only)."""
    writer = _writer['current']
    if writer is not None:
        writer.close()
    _writer['current'] = None
    _config['current'] = None
    _static.clear()


def note(**fields):
    """Attach values the batch computed outside the database (no-op unless
    this thread is recording)."""
    recorder = getattr(_local, 'recorder', None)
    if recorder is not None:
        try:
            recorder.notes.update({k: jsonable(v) for k, v in fields.items()})
        except Exception:       # noqa: BLE001
            recorder.stats['capture_errors'] += 1


def is_recording():
    """True inside a sampled batch on this thread."""
    return getattr(_local, 'recorder', None) is not None


# ---------------------------------------------------------------- values

def jsonable(value):
    """A value as JSON can hold it, losslessly where it matters."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return repr(value)
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'$hex': bytes(value).hex()}
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    obj = getattr(value, 'obj', None)        # psycopg Json/Jsonb adapters
    if obj is not None:
        return jsonable(obj)
    adapted = getattr(value, 'adapted', None)
    if adapted is not None:
        return jsonable(adapted)
    return repr(value)


def _keys_in(value, out):
    """Every 64-hex string (a position key) anywhere inside ``value``."""
    if isinstance(value, str):
        if len(value) == 64 and _KEY_RE.match(value):
            out.add(value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _keys_in(item, out)
    elif isinstance(value, dict):
        for item in value.values():
            _keys_in(item, out)


def _flatten(params):
    out = []
    for value in params or ():
        if isinstance(value, (list, tuple)):
            out.extend(_flatten(value))
        else:
            out.append(value)
    return out


# ---------------------------------------------------------------- static

_static = {}


def _code_and_constants():
    """sha256 of the rule modules, and their constants (once a process)."""
    if 'code' in _static:
        return _static['code'], _static['constants']
    import importlib
    code, constants = {}, {}
    for name in CODE_MODULES:
        try:
            module = importlib.import_module(f'atomicdb.{name}')
            with open(module.__file__, 'rb') as handle:
                code[name] = hashlib.sha256(handle.read()).hexdigest()
        except Exception:       # noqa: BLE001
            code[name] = None
        if name not in CONSTANT_MODULES:
            continue
        try:
            module = importlib.import_module(f'atomicdb.{name}')
        except Exception:       # noqa: BLE001
            continue
        values = {}
        for attr, value in vars(module).items():
            if (attr.isupper() and not attr.startswith('_')
                    and isinstance(value, (bool, int, float, str, tuple,
                                           frozenset))):
                values[attr] = jsonable(value)
        constants[name] = values
    _static['code'], _static['constants'] = code, constants
    return code, constants


def _settings():
    try:
        from django.conf import settings
        out = {name: jsonable(getattr(settings, name))
               for name in dir(settings._wrapped)
               if name.startswith('ATOMICDB_')
               and not any(word in name for word in SECRET_WORDS)}
    except Exception:           # noqa: BLE001
        out = {}
    out['env'] = {name: value for name, value in os.environ.items()
                  if name.startswith('ATOMICDB_')
                  and not any(word in name for word in SECRET_WORDS)}
    return out


# ---------------------------------------------------------------- recorder

class Recorder:
    """What one sampled batch read and wrote, on one connection."""

    def __init__(self, kind, inputs, conn):
        self.kind = kind
        self.inputs = inputs
        self.conn = conn
        self.queries = []
        self.commits = []
        self.result_value = None
        self.notes = {}
        self.discarded = False
        self.started = time.time()
        self.t0 = time.perf_counter()
        self.capture_s = 0.0
        self.post_s = 0.0
        self.stats = {'rows_read': 0, 'uncaptured_selects': 0,
                      'unkeyed_position_writes': 0, 'capture_errors': 0,
                      'failed_statements': 0}
        self._last = {}             # (table, pk) -> first value read
        self._dirty = set()         # position keys written since the commit
        self._set_cols = {}         # table -> columns its UPDATEs set
        self._edges_inserted = []
        self._relnames = {}         # oid -> relname
        self._pks = {}              # relname -> primary key column
        self._backend = getattr(conn, 'vendor', '')
        self._real_commit = None
        self._real_rollback = None

    # -- the wrapper ------------------------------------------------------

    def execute_wrapper(self, execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            result = execute(sql, params, many, context)
        except Exception as error:
            took = time.perf_counter() - started
            try:
                self.stats['failed_statements'] += 1
                self.queries.append({'sql': sql, 'params': jsonable(params),
                                     'many': many or None,
                                     'ms': round(took * 1000, 3),
                                     'error': f'{type(error).__name__}: '
                                              f'{error}'[:500]})
            except Exception:   # noqa: BLE001
                pass
            raise
        took = time.perf_counter() - started
        mark = time.perf_counter()
        try:
            self._capture(sql, params, many, context.get('cursor'), took)
        except Exception as error:  # noqa: BLE001 - never fail the batch
            self.stats['capture_errors'] += 1
            self.queries.append({'sql': sql, 'capture_error':
                                 f'{type(error).__name__}: {error}'[:300]})
        self.capture_s += time.perf_counter() - mark
        return result

    def _capture(self, sql, params, many, cursor, took):
        entry = {'sql': sql, 'params': jsonable(params),
                 'ms': round(took * 1000, 3)}
        if many:
            entry['many'] = True
        raw = getattr(cursor, 'cursor', None)
        rowcount = getattr(raw, 'rowcount', None)
        if rowcount is not None and rowcount >= 0:
            entry['n'] = rowcount
        write = _WRITE_RE.match(sql)
        if write:
            self._note_write(write.group(1).upper().split()[0],
                             write.group(2), sql, params, many)
        description = getattr(raw, 'description', None)
        if description:
            entry['cols'] = [getattr(col, 'name', None) or col[0]
                             for col in description]
            rows = self._fetch_rewind(raw)
            if rows is None:
                self.stats['uncaptured_selects'] += 1
                entry['rows'] = None
            else:
                tables = self._tables(raw, len(description))
                if tables is not None:
                    entry['tables'] = tables
                rows = [list(row) for row in rows]
                entry['rows'] = [[jsonable(v) for v in row] for row in rows]
                self.stats['rows_read'] += len(rows)
                if tables is not None:
                    self._remember(sql, entry['cols'], tables, entry['rows'])
                if write and write.group(2) == 'atomicdb_edge':
                    entry['returned'] = True
        self.queries.append(entry)

    def _fetch_rewind(self, raw):
        """All the rows of a client-side cursor, rewound so the caller reads
        them again from the first; None when that cannot be done."""
        if getattr(raw, 'name', None):          # server-side cursor
            return None
        scroll = getattr(raw, 'scroll', None)
        if scroll is None:
            return None
        rownumber = getattr(raw, 'rownumber', 0)
        if rownumber not in (0, None):
            return None
        rowcount = getattr(raw, 'rowcount', -1)
        if rowcount == 0:
            return []           # nothing to read, nothing to rewind
        if rowcount is None or rowcount < 0:
            return None         # unknown size: leave the cursor alone
        rows = raw.fetchall()
        scroll(0, mode='absolute')
        return rows

    def _tables(self, raw, count):
        result = getattr(raw, 'pgresult', None)
        if result is None or not hasattr(result, 'ftable'):
            return None
        oids = [result.ftable(i) for i in range(count)]
        missing = sorted({oid for oid in oids
                          if oid and oid not in self._relnames})
        if missing:
            for oid, name in self._raw_query(
                    'SELECT oid, relname FROM pg_class WHERE oid = ANY(%s)',
                    [missing]):
                self._relnames[oid] = name
        return [self._relnames.get(oid) if oid else None for oid in oids]

    def _pk(self, table):
        if table not in self._pks:
            rows = self._raw_query(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = %s::regclass AND i.indisprimary",
                [table])
            self._pks[table] = rows[0][0] if len(rows) == 1 else None
        return self._pks[table]

    def _remember(self, sql, cols, tables, rows):
        by_table = {}
        for index, (col, table) in enumerate(zip(cols, tables)):
            if table:
                by_table.setdefault(table, []).append((index, col))
        for table, fields in by_table.items():
            pk = self._pk(table)
            pk_index = next((i for i, c in fields if c == pk), None)
            if pk_index is None:
                pk_index = _join_pk_index(sql, cols, tables, table)
            if pk_index is None:
                continue
            for row in rows:
                if len(self._last) >= MAX_IMAGES:
                    return
                image = self._last.setdefault((table, row[pk_index]), {})
                for index, col in fields:
                    image.setdefault(col, row[index])

    def _note_write(self, verb, table, sql, params, many):
        if verb == 'UPDATE':
            match = _SET_RE.search(sql)
            if match:
                self._set_cols.setdefault(table, set()).update(
                    _SET_COL_RE.findall(match.group(1)))
        if table == 'atomicdb_position':
            keys = set()
            _keys_in(params if not many else list(params or ()), keys)
            if keys:
                self._dirty.update(keys)
            elif verb != 'DELETE':
                self.stats['unkeyed_position_writes'] += 1
        elif table == 'atomicdb_edge' and verb == 'INSERT' and not many:
            match = _INSERT_COLS_RE.match(sql)
            if match:
                cols = [c.strip().strip('"') for c in match.group(1).split(',')]
                values = _flatten(params)
                if cols and len(values) % len(cols) == 0:
                    for start in range(0, len(values), len(cols)):
                        self._edges_inserted.append(dict(zip(
                            cols, jsonable(values[start:start + len(cols)]))))

    # -- raw access, outside the wrappers ---------------------------------

    def _raw_query(self, sql, params):
        raw_conn = getattr(self.conn, 'connection', None)
        if raw_conn is None:
            return []
        with raw_conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _post_images(self, label):
        """The written position rows as the transaction leaves them."""
        if not self._dirty and not self._edges_inserted:
            return None
        started = time.perf_counter()
        keys = sorted(self._dirty)
        self._dirty = set()
        edges, self._edges_inserted = self._edges_inserted, []
        record = {'after': len(self.queries), 'at': label}
        positions = {}
        if keys and self._backend == 'postgresql':
            raw_conn = self.conn.connection
            with raw_conn.cursor() as cursor:
                cursor.execute('SELECT * FROM atomicdb_position '
                               'WHERE key = ANY(%s)', [keys])
                cols = [col.name for col in cursor.description]
                rows = cursor.fetchall()
            set_cols = self._set_cols.get('atomicdb_position', set())
            for row in rows:
                values = dict(zip(cols, (jsonable(v) for v in row)))
                key = values['key']
                last = self._last.get(('atomicdb_position', key))
                if last is None:
                    positions[key] = {'$row': values}
                    continue
                changed = {}
                for col, value in values.items():
                    if col in last:
                        if last[col] != value:
                            changed[col] = value
                    elif col in set_cols:
                        changed[col] = value
                if changed:
                    positions[key] = changed
            missing = set(keys) - {row[cols.index('key')] for row in rows}
            if missing:
                record['deleted_or_unseen'] = sorted(missing)
        elif keys:
            record['keys'] = keys       # no post image off PostgreSQL
        record['positions'] = positions
        if edges:
            record['edges_inserted'] = edges
        self.commits.append(record)
        self.post_s += time.perf_counter() - started
        return record

    # -- commit and rollback ----------------------------------------------

    def install(self, stack):
        stack.enter_context(self.conn.execute_wrapper(self.execute_wrapper))
        self._real_commit = self.conn.commit
        self._real_rollback = self.conn.rollback
        recorder = self

        def commit(*args, **kwargs):
            try:
                recorder._post_images('commit')
            except Exception as error:      # noqa: BLE001
                recorder.stats['capture_errors'] += 1
                recorder.commits.append({'after': len(recorder.queries),
                                         'at': 'commit', 'capture_error':
                                         f'{type(error).__name__}: '
                                         f'{error}'[:300]})
            return recorder._real_commit(*args, **kwargs)

        def rollback(*args, **kwargs):
            recorder._dirty = set()
            recorder._edges_inserted = []
            recorder.commits.append({'after': len(recorder.queries),
                                     'at': 'rollback'})
            return recorder._real_rollback(*args, **kwargs)

        self.conn.commit = commit
        self.conn.rollback = rollback

        def restore():
            for name in ('commit', 'rollback'):
                try:
                    delattr(self.conn, name)
                except AttributeError:
                    pass
        stack.callback(restore)

    # -- the outcome ------------------------------------------------------

    def result(self, value):
        self.result_value = value

    def discard(self):
        self.discarded = True

    def finish(self, error=None):
        mark = time.perf_counter()
        try:
            # Writes outside a transaction (autocommit) have no COMMIT of
            # their own: their rows are read now, after the fact.
            self._post_images('end')
        except Exception:           # noqa: BLE001
            self.stats['capture_errors'] += 1
        ended = time.time()
        code, constants = _code_and_constants()
        case = {
            'v': 1, 'kind': self.kind,
            'id': f'{socket.gethostname()}-{os.getpid()}-'
                  f'{int(self.started * 1000)}-{_rng.getrandbits(24):06x}',
            'host': socket.gethostname(), 'pid': os.getpid(),
            'started': round(self.started, 6), 'ended': round(ended, 6),
            'elapsed_ms': round((time.perf_counter() - self.t0) * 1000, 3),
            'backend': self._backend,
            'inputs': jsonable(self.inputs),
            'queries': self.queries,
            'commits': self.commits,
            'result': jsonable(self.result_value),
            'notes': self.notes,
            'stats': dict(self.stats, queries=len(self.queries)),
            'code': code, 'constants': constants, 'settings': _settings(),
        }
        if self.kind == 'drain':
            case['seed_rows'] = _seed_rows(self.queries)
        if error is not None:
            case['error'] = f'{type(error).__name__}: {error}'[:2000]
        overhead = self.capture_s + self.post_s + (time.perf_counter() - mark)
        case['overhead_ms'] = {'capture': round(self.capture_s * 1000, 3),
                               'post_images': round(self.post_s * 1000, 3),
                               'total': round(overhead * 1000, 3)}
        return case


def _seed_rows(queries):
    """The PropagationSeed rows a climb claimed (its inputs)."""
    for entry in queries:
        if (entry.get('rows') and 'atomicdb_propagationseed' in entry['sql']
                and 'FOR UPDATE' in entry['sql'].upper()):
            return [dict(zip(entry['cols'], row)) for row in entry['rows']]
    return []


@contextlib.contextmanager
def recording(kind, inputs=None):
    """Record this block as a golden case of ``kind`` if it is sampled.

    Yields the Recorder, or None when the block is not recorded (off, not
    sampled, already recording on this thread, or out of budget).
    """
    cfg = config()
    if not cfg.enabled or is_recording():
        yield None
        return
    now = time.time()
    try:
        cfg.refresh(now)
        sampled = (cfg.active() and _rng.random() < cfg.rate(kind)
                   and _get_writer(cfg).accepting(now))
    except Exception:           # noqa: BLE001
        sampled = False
    if not sampled:
        yield None
        return
    try:
        from django.db import connections

        from .database import current_alias
        conn = connections[current_alias()]
        conn.ensure_connection()
        recorder = Recorder(kind, inputs, conn)
        stack = contextlib.ExitStack()
        recorder.install(stack)
    except Exception:           # noqa: BLE001 - not recorded, not failed
        logger.exception('AtomicDB golden cases: could not start a recorder')
        yield None
        return
    _local.recorder = recorder
    error = None
    try:
        yield recorder
    except BaseException as exc:
        error = exc
        raise
    finally:
        _local.recorder = None
        try:
            stack.close()
        except Exception:       # noqa: BLE001
            logger.exception('AtomicDB golden cases: restoring failed')
        if not recorder.discarded:
            try:
                case = recorder.finish(error=error)
                _get_writer(cfg).submit(case, recorder)
            except Exception:   # noqa: BLE001
                logger.exception('AtomicDB golden cases: finishing a case '
                                 'failed')


# ---------------------------------------------------------------- writer

class _Sink:
    """One output file, one compressed frame per case.

    Each case is compressed on its own (a zstd frame from the ``zstd``
    command, else a gzip member) and appended: concatenated frames are a
    valid stream for ``zstd -dc`` / ``gzip -dc``, and a process killed in
    the middle of a write loses at most that last case, never the file.
    """

    def __init__(self, path_base):
        self.started = time.time()
        self.bytes = 0
        self.compressed = 0
        self._zstd = shutil.which('zstd')
        self.path = path_base + ('.jsonl.zst' if self._zstd else '.jsonl.gz')
        self._out = open(self.path, 'ab')

    def write(self, data):
        if self._zstd:
            frame = subprocess.run(
                [self._zstd, '-q', '-3', '-T1', '-c'], input=data,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=True, timeout=120).stdout
        else:
            frame = gzip.compress(data, compresslevel=4)
        self._out.write(frame)
        self._out.flush()
        self.bytes += len(data)
        self.compressed += len(frame)

    def close(self):
        self._out.close()

class _Writer:
    """Background thread: serialise, compress, rotate, respect the budget."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.queue = queue.Queue(maxsize=QUEUE_DEPTH)
        self.sinks = {}
        self.counters = {'sampled': 0, 'written': 0, 'dropped_queue': 0,
                         'dropped_budget': 0, 'dropped_size': 0,
                         'oversize': 0, 'bytes_json': 0, 'errors': 0}
        self.overheads = []
        self.paused = None
        self.dir_bytes = 0
        self.budget_checked = 0.0
        self.status_written = 0.0
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name='atomicdb-golden-cases')
        self.thread.start()

    # -- budget -----------------------------------------------------------

    def _scan(self, now):
        total = 0
        for root, _dirs, files in os.walk(self.cfg.dir):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        self.dir_bytes = total
        try:
            free = shutil.disk_usage(self.cfg.dir).free
        except OSError:
            free = 0
        if free < self.cfg.min_free:
            self.paused = f'disk free {free / 1e9:.0f} GB < ' \
                          f'{self.cfg.min_free / 1e9:.0f} GB'
        elif total >= self.cfg.max_bytes:
            self.paused = f'cap {total / 1e9:.1f} GB >= ' \
                          f'{self.cfg.max_bytes / 1e9:.1f} GB'
        else:
            self.paused = None
        self.budget_checked = now

    def accepting(self, now):
        with self.lock:
            if now - self.budget_checked >= BUDGET_SECONDS:
                os.makedirs(self.cfg.dir, exist_ok=True)
                self._scan(now)
            if self.paused:
                self.counters['dropped_budget'] += 1
                return False
            return True

    # -- hand-over --------------------------------------------------------

    def submit(self, case, recorder):
        with self.lock:
            self.counters['sampled'] += 1
            self.overheads.append(case['overhead_ms']['total'])
            if len(self.overheads) > 1000:
                self.overheads = self.overheads[-1000:]
        try:
            self.queue.put_nowait(case)
        except queue.Full:
            with self.lock:
                self.counters['dropped_queue'] += 1

    def close(self, timeout=10.0):
        try:
            self.queue.put(None, timeout=timeout)
        except queue.Full:
            pass
        self.thread.join(timeout)

    # -- the thread -------------------------------------------------------

    def _run(self):
        try:
            while True:
                try:
                    case = self.queue.get(timeout=STATUS_SECONDS)
                except queue.Empty:
                    case = False
                if case is None:
                    break
                if case:
                    try:
                        self._write(case)
                    except Exception:       # noqa: BLE001
                        self.counters['errors'] += 1
                        logger.exception('AtomicDB golden cases: writing a '
                                         'case failed')
                self._maybe_rotate(time.time())
                self._maybe_status(time.time())
        finally:
            for sink in list(self.sinks.values()):
                try:
                    sink.close()
                except Exception:           # noqa: BLE001
                    pass
            self.sinks.clear()
            self._maybe_status(time.time(), force=True)

    def _write(self, case):
        data = json.dumps(case, separators=(',', ':'),
                          default=jsonable).encode()
        if len(data) > MAX_CASE_BYTES:
            for entry in case['queries']:
                if entry.get('rows'):
                    entry['rows'] = None
                    entry['rows_dropped'] = True
            case['oversize'] = True
            self.counters['oversize'] += 1
            data = json.dumps(case, separators=(',', ':'),
                              default=jsonable).encode()
            if len(data) > MAX_CASE_BYTES:
                self.counters['dropped_size'] += 1
                return
        with self.lock:
            if self.paused or self.dir_bytes >= self.cfg.max_bytes:
                self.counters['dropped_budget'] += 1
                return
        sink = self._sink(case['kind'])
        before = sink.compressed
        sink.write(data + b'\n')
        self.counters['written'] += 1
        self.counters['bytes_json'] += len(data) + 1
        with self.lock:
            self.dir_bytes += sink.compressed - before

    def _sink(self, kind):
        sink = self.sinks.get(kind)
        if sink is None:
            folder = os.path.join(self.cfg.dir, kind)
            os.makedirs(folder, exist_ok=True)
            stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
            base = os.path.join(folder, f'{kind}-{socket.gethostname()}-'
                                        f'{os.getpid()}-{stamp}')
            sink = self.sinks[kind] = _Sink(base)
        return sink

    def _maybe_rotate(self, now):
        for kind, sink in list(self.sinks.items()):
            if (sink.bytes >= self.cfg.rotate_bytes
                    or (sink.bytes and now - sink.started >= 3600)):
                sink.close()
                del self.sinks[kind]

    def _maybe_status(self, now, force=False):
        if not force and now - self.status_written < STATUS_SECONDS:
            return
        self.status_written = now
        with self.lock:
            overheads = sorted(self.overheads)
            status = dict(self.counters, paused=self.paused,
                          dir_bytes=self.dir_bytes, t=round(now, 3),
                          rates={k: self.cfg.rate(k) for k in KINDS},
                          enabled=self.cfg.active())
        if overheads:
            status['overhead_ms'] = {
                'n': len(overheads),
                'p50': overheads[len(overheads) // 2],
                'p99': overheads[min(len(overheads) - 1,
                                     (len(overheads) * 99) // 100)],
                'max': overheads[-1]}
        path = os.path.join(self.cfg.dir, f'status-{socket.gethostname()}-'
                                          f'{os.getpid()}.json')
        try:
            os.makedirs(self.cfg.dir, exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w') as handle:
                json.dump(status, handle)
            os.replace(tmp, path)
        except OSError:
            pass


_writer = {'current': None}
_writer_lock = threading.Lock()


def _get_writer(cfg):
    with _writer_lock:
        if _writer['current'] is None:
            _writer['current'] = _Writer(cfg)
            atexit.register(_close_at_exit)
        return _writer['current']


def _close_at_exit():
    writer = _writer['current']
    if writer is not None:
        writer.close(timeout=10.0)


def flush(timeout=30.0):
    """Wait until every handed-over case is written and the files are
    closed (tests and tools; the next case opens new files)."""
    writer = _writer['current']
    if writer is None:
        return
    writer.close(timeout=timeout)
    with _writer_lock:
        _writer['current'] = None


# ---------------------------------------------------------------- reading

def iter_cases(path):
    """The cases in one output file (.jsonl, .jsonl.gz or .jsonl.zst)."""
    if path.endswith('.zst'):
        zstd = shutil.which('zstd')
        if not zstd:
            raise RuntimeError('reading .zst needs the zstd command')
        proc = subprocess.Popen([zstd, '-dc', path], stdout=subprocess.PIPE)
        try:
            for line in proc.stdout:
                if line.strip():
                    yield json.loads(line)
        finally:
            proc.stdout.close()
            proc.wait()
        return
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rb') as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def pre_image(case):
    """``{table: {pk: {column: value}}}``: the first value the case read of
    every column of every row it could attribute to a table with its
    primary key among the columns (what v2 loads before replaying)."""
    pks = case.get('primary_keys') or {'atomicdb_position': 'key',
                                       'atomicdb_edge': 'id'}
    images = {}
    for entry in case.get('queries', ()):
        rows, tables, cols = entry.get('rows'), entry.get('tables'), \
            entry.get('cols')
        if not rows or not tables or not cols:
            continue
        by_table = {}
        for index, (col, table) in enumerate(zip(cols, tables)):
            if table:
                by_table.setdefault(table, []).append((index, col))
        for table, fields in by_table.items():
            pk = pks.get(table)
            pk_index = next((i for i, c in fields if c == pk), None)
            if pk_index is None:
                pk_index = _join_pk_index(entry.get('sql'), cols, tables,
                                          table)
            if pk_index is None:
                continue
            target = images.setdefault(table, {})
            for row in rows:
                image = target.setdefault(row[pk_index], {})
                for index, col in fields:
                    image.setdefault(col, row[index])
    return images
