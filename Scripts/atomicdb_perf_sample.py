#!/usr/bin/env python3
"""Once a minute: the AtomicDB v1 baseline numbers, appended as one JSON line.

WHY (28-Nov-2026).  The v2 plan (docs/atomicdb/v2-plan.md, section 9) is
judged phase by phase against the v1 baseline, measured the same way every
time: searches applied per second, submit-to-visible (p50/p95/p99), climb
batch latency, lease and submit latency, explorer and API latency, CPU and
RAM by systemd unit, disk bytes per position and per day, stale rows found
per sweep pass, and the depth of the propagation queue.  This script takes
one sample of all of them and appends it to
``<out>/samples-YYYYMMDD.jsonl``.  A systemd timer runs it every minute
(docs/server/atomicdb-perf-sample.{service,timer}).

LOW OVERHEAD BY CONSTRUCTION.  It is a plain script (no Django start-up) and
it never scans a big table:

* the drainer and the ingest writers write one line per climbed batch and
  per applied job (atomicdb/perf_events.py, ``ATOMICDB_PERF_DIR``); this
  script reads what was appended since its last run, and looks up the
  ``created``/``updated`` of exactly those jobs by primary key;
* latencies come from a second nginx access log with request times
  (docs/server/nginx-atomicdb-timing.conf), read from the last offset;
* CPU and memory come from the cgroup files of each unit;
* sizes come from ``pg_database_size``/``pg_total_relation_size`` (file
  stats) and ``pg_class.reltuples`` (an estimate); the queue depths from the
  small propagation table and the partial index of open ingest jobs; the
  sweep passes from ``DBEvent`` through its ``(kind, ts)`` index.

Every query runs under a short statement timeout; a block that fails is
listed in ``errors`` and the rest of the sample is still written.
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import socket
import sys
import time

DEFAULT_OUT = '/var/lib/atomicdb-perf'
DEFAULT_TIMING_LOG = '/var/log/nginx/atomicdb-timing.log'
CGROUP_ROOT = '/sys/fs/cgroup'
# Units measured.  Globs are relative to the cgroup root.
UNIT_GLOBS = (
    'system.slice/atomicdb-*.service',
    'system.slice/openbench.service',
    'system.slice/nginx.service',
    'system.slice/redis-server.service',
    'system-postgresql.slice/postgresql@*.service',
    'system.slice/system-postgresql.slice/postgresql@*.service',
)
DISKS = ('md2', 'nvme0n1', 'nvme1n1')
MAX_LOG_BYTES = 64 * 1024 * 1024      # per run, so a burst never stalls it
MAX_EVENT_BYTES = 32 * 1024 * 1024
PERCENTILES = (50, 90, 95, 99)

# ---------------------------------------------------------------- helpers


def percentiles(values, ps=PERCENTILES):
    """``{'n', 'mean', 'p50'..., 'max'}`` of a list of numbers (nearest rank)."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'n': 0}
    n = len(values)
    out = {'n': n, 'mean': round(sum(values) / n, 3)}
    for p in ps:
        rank = max(1, -(-p * n // 100))     # ceil(p * n / 100)
        out[f'p{p}'] = round(values[min(n, rank) - 1], 3)
    out['max'] = round(values[-1], 3)
    return out


WORKER_ROUTES = {
    '/atomicdb/api/lease': 'lease',
    '/atomicdb/api/heartbeat': 'heartbeat',
    '/atomicdb/api/submit': 'submit',
    '/atomicdb/api/release': 'release',
    '/atomicdb/api/solve/acquire': 'solve',
    '/atomicdb/api/solve/heartbeat': 'solve',
    '/atomicdb/api/solve/submit': 'solve',
}
PUBLIC_API = ('query', 'principal-variations', 'backed-source', 'request',
              'request-children', 'my-queue', 'my-workers', 'queue-bump',
              'queue-cancel', 'roots', 'map', 'frontier', 'live-request')


def classify(uri):
    """The route class of a request path, or None outside /atomicdb/."""
    if not uri.startswith('/atomicdb/'):
        return None
    path = uri.rstrip('/') if uri != '/atomicdb/' else uri
    if path in WORKER_ROUTES:
        return WORKER_ROUTES[path]
    rest = uri[len('/atomicdb/'):]
    head = rest.split('/', 1)[0]
    if head == 'api':
        name = rest[len('api/'):].split('/', 1)[0]
        if name == 'query':
            return 'api_query'
        if name in PUBLIC_API:
            return 'api_public'
        return 'api_other'
    if head in ('explore', 'goto', 'lite', 'backed-source', 'proven-line-end',
                'borrowed-source'):
        return 'explore'
    if head == 'engines':
        return 'engines'
    if head == '':
        return 'home'
    return 'pages'


def parse_timing_line(line):
    """``msec|status|request_time|upstream_response_time|method|uri``."""
    parts = line.rstrip('\n').split('|', 5)
    if len(parts) != 6:
        return None
    try:
        ts = float(parts[0])
        status = int(parts[1])
        rt = float(parts[2])
    except ValueError:
        return None
    upstream = None
    raw = parts[3].strip()
    if raw and raw != '-':
        # Several upstreams are ", " or " : " separated; their sum is the
        # time spent behind nginx.
        total = 0.0
        ok = False
        for piece in raw.replace(':', ',').split(','):
            piece = piece.strip()
            if piece and piece != '-':
                try:
                    total += float(piece)
                    ok = True
                except ValueError:
                    pass
        upstream = total if ok else None
    return ts, status, rt, upstream, parts[4], parts[5]


def summarize_requests(records):
    """Per route class: count, statuses, request and upstream times in ms."""
    by_class = {}
    for ts, status, rt, upstream, method, uri in records:
        cls = classify(uri)
        if cls is None:
            continue
        slot = by_class.setdefault(cls, {'rt': [], 'up': [], 'status': {}})
        slot['rt'].append(rt * 1000.0)
        if upstream is not None:
            slot['up'].append(upstream * 1000.0)
        bucket = f'{status // 100}xx' if status != 429 else '429'
        slot['status'][bucket] = slot['status'].get(bucket, 0) + 1
    out = {}
    for cls, slot in sorted(by_class.items()):
        out[cls] = {'request_ms': percentiles(slot['rt']),
                    'upstream_ms': percentiles(slot['up']),
                    'status': slot['status']}
    return out


def read_appended(path, state, key, max_bytes):
    """Lines appended to ``path`` since the offset kept in ``state[key]``.

    Follows a rotation (new inode): the rest of the rotated file
    (``path.1``, if it still has the old inode) is read first, then the new
    file from the start.  The first run starts at the end (no history).
    Returns (lines, info).
    """
    prev = state.get(key) or {}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return [], {'missing': True}
    lines, info = [], {}
    if not prev:
        state[key] = {'ino': st.st_ino, 'off': st.st_size}
        return [], {'warmup': True}
    if prev.get('ino') != st.st_ino:
        rotated = path + '.1'
        try:
            rst = os.stat(rotated)
            if rst.st_ino == prev.get('ino') and rst.st_size > prev['off']:
                with open(rotated, 'rb') as handle:
                    handle.seek(prev['off'])
                    lines.extend(_whole_lines(handle.read(max_bytes))[0])
                info['rotated'] = True
        except FileNotFoundError:
            pass
        prev = {'ino': st.st_ino, 'off': 0}
    if st.st_size < prev['off']:        # truncated in place
        prev = {'ino': st.st_ino, 'off': 0}
    with open(path, 'rb') as handle:
        handle.seek(prev['off'])
        data = handle.read(max_bytes)
    chunk, used = _whole_lines(data)
    lines.extend(chunk)
    if len(data) >= max_bytes:
        info['truncated'] = True
    state[key] = {'ino': st.st_ino, 'off': prev['off'] + used}
    return lines, info


def _whole_lines(data):
    """Complete lines of ``data`` and the bytes they span (a half-written
    last line is left for the next run)."""
    end = data.rfind(b'\n')
    if end < 0:
        return [], 0
    return data[:end + 1].decode('utf-8', 'replace').splitlines(), end + 1


# ---------------------------------------------------------------- events


def read_events(out_dir, state, now):
    """Events appended since the last run, from today's and yesterday's
    files (a run right after midnight finishes yesterday's)."""
    events, infos = [], {}
    days = sorted({time.strftime('%Y%m%d', time.gmtime(now - 120)),
                   time.strftime('%Y%m%d', time.gmtime(now))})
    offsets = state.setdefault('events', {})
    for day in days:
        path = os.path.join(out_dir, f'events-{day}.jsonl')
        if not os.path.exists(path):
            continue
        sub = {'ev': offsets.get(day)} if day in offsets else {}
        if day not in offsets and state.get('events_started'):
            # A file created after we started: read it from the start.
            sub = {'ev': {'ino': os.stat(path).st_ino, 'off': 0}}
        lines, info = read_appended(path, sub, 'ev', MAX_EVENT_BYTES)
        offsets[day] = sub['ev']
        if info:
            infos[day] = info
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    state['events_started'] = True
    for day in list(offsets):
        if day not in days:
            del offsets[day]
    return events, infos


def summarize_events(events, job_times):
    """Climb batches, applied jobs and submit-to-visible.

    ``job_times`` maps a job id to ``(created, updated, state)`` epoch
    seconds, read by primary key for the jobs these events name.
    """
    drains = [e for e in events if e.get('kind') == 'drain']
    jobs = [e for e in events if e.get('kind') == 'job']
    ok_jobs = [e for e in jobs if not e.get('error')]
    own_visible, climbed = [], []
    for e in ok_jobs:
        times = job_times.get(e.get('job'))
        if times and times[0] is not None and times[1] is not None:
            own_visible.append((times[1] - times[0]) * 1000.0)
    for e in drains:
        commit = e.get('commit_t')
        for job in e.get('jobs') or ():
            times = job_times.get(job)
            if commit and times and times[0] is not None:
                climbed.append((commit - times[0]) * 1000.0)
    return {
        'drain': {
            'batches': len(drains),
            'landings': sum(e.get('settled') or 0 for e in drains),
            'climb_ms': percentiles([e.get('climb_ms') for e in drains]),
            'batch_ms': percentiles([e.get('ms') for e in drains]),
            'size': percentiles([e.get('settled') for e in drains]),
            'retried': sum(1 for e in drains if (e.get('attempts') or 1) > 1),
        },
        'jobs': {
            'applied': len(ok_jobs),
            'failed': len(jobs) - len(ok_jobs),
            'deferred': sum(1 for e in ok_jobs if e.get('deferred')),
            'apply_ms': percentiles([e.get('ms') for e in ok_jobs]),
        },
        'submit_to_own_visible_ms': percentiles(own_visible),
        'submit_to_climbed_ms': percentiles(climbed),
    }


# ---------------------------------------------------------------- system


def read_cgroups(root=CGROUP_ROOT, globs=UNIT_GLOBS):
    units = {}
    for pattern in globs:
        for path in glob.glob(os.path.join(root, pattern)):
            name = os.path.basename(path)
            entry = {}
            try:
                with open(os.path.join(path, 'cpu.stat')) as handle:
                    for line in handle:
                        field, _, value = line.partition(' ')
                        if field == 'usage_usec':
                            entry['cpu_usec'] = int(value)
            except OSError:
                pass
            for field, filename in (('mem', 'memory.current'),
                                    ('mem_peak', 'memory.peak'),
                                    ('swap', 'memory.swap.current')):
                try:
                    with open(os.path.join(path, filename)) as handle:
                        entry[field] = int(handle.read().strip())
                except (OSError, ValueError):
                    pass
            try:
                with open(os.path.join(path, 'memory.stat')) as handle:
                    for line in handle:
                        field, _, value = line.partition(' ')
                        if field in ('anon', 'file', 'shmem'):
                            entry['mem_' + field] = int(value)
            except (OSError, ValueError):
                pass
            if entry:
                units[name] = entry
    return units


def cpu_percent(units, prev_units, elapsed):
    """CPU of each unit since the previous sample, in % of one thread."""
    if not elapsed or elapsed <= 0:
        return
    for name, entry in units.items():
        before = (prev_units or {}).get(name, {}).get('cpu_usec')
        now = entry.get('cpu_usec')
        if before is not None and now is not None and now >= before:
            entry['cpu_pct'] = round((now - before) / 1e4 / elapsed, 2)


def read_diskstats(path='/proc/diskstats', disks=DISKS):
    out = {}
    try:
        with open(path) as handle:
            for line in handle:
                f = line.split()
                if len(f) >= 14 and f[2] in disks:
                    out[f[2]] = {'reads': int(f[3]), 'rsect': int(f[5]),
                                 'writes': int(f[7]), 'wsect': int(f[9]),
                                 'io_ms': int(f[12])}
    except OSError:
        pass
    return out


def disk_rates(disks, prev, elapsed):
    if not elapsed or elapsed <= 0:
        return
    for name, d in disks.items():
        p = (prev or {}).get(name)
        if not p:
            continue
        d['r_s'] = round((d['reads'] - p['reads']) / elapsed, 1)
        d['w_s'] = round((d['writes'] - p['writes']) / elapsed, 1)
        d['rMB_s'] = round((d['rsect'] - p['rsect']) * 512 / 1e6 / elapsed, 2)
        d['wMB_s'] = round((d['wsect'] - p['wsect']) * 512 / 1e6 / elapsed, 2)
        d['util_pct'] = round((d['io_ms'] - p['io_ms']) / 10.0 / elapsed, 1)


def read_host():
    host = {}
    try:
        with open('/proc/loadavg') as handle:
            host['load'] = [float(x) for x in handle.read().split()[:3]]
    except OSError:
        pass
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                field, _, value = line.partition(':')
                if field in ('MemTotal', 'MemAvailable', 'SwapTotal',
                             'SwapFree', 'Dirty', 'Cached'):
                    host[field] = int(value.split()[0]) * 1024
    except OSError:
        pass
    try:
        usage = shutil.disk_usage('/')
        host['disk_free'] = usage.free
        host['disk_used'] = usage.used
    except OSError:
        pass
    return host


# ---------------------------------------------------------------- database


def connect():
    import psycopg
    env = os.environ
    return psycopg.connect(
        dbname=env.get('OPENBENCH_ATOMICDB_POSTGRES_DB', 'atomicdb'),
        user=env.get('OPENBENCH_ATOMICDB_POSTGRES_USER'),
        password=env.get('OPENBENCH_ATOMICDB_POSTGRES_PASSWORD'),
        host=env.get('OPENBENCH_ATOMICDB_POSTGRES_HOST') or None,
        port=env.get('OPENBENCH_ATOMICDB_POSTGRES_PORT') or None,
        application_name='atomicdb-perf-sample', autocommit=True,
        connect_timeout=5)


def db_sample(conn, state, job_ids, errors):
    out = {}
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '5s'")
    cur.execute("SET lock_timeout = '1s'")

    def block(name, fn):
        try:
            out[name] = fn()
        except Exception as error:      # noqa: BLE001 - next block still runs
            errors.append(f'{name}: {type(error).__name__}: {error}'[:300])
            try:
                conn.rollback()
            except Exception:           # noqa: BLE001
                pass

    def sizes():
        cur.execute("""
            SELECT pg_database_size(current_database()),
                   pg_total_relation_size('atomicdb_position'),
                   pg_total_relation_size('atomicdb_edge'),
                   pg_total_relation_size('atomicdb_ingestjob'),
                   pg_total_relation_size('atomicdb_dbevent'),
                   (SELECT reltuples::bigint FROM pg_class
                     WHERE oid = 'atomicdb_position'::regclass),
                   (SELECT reltuples::bigint FROM pg_class
                     WHERE oid = 'atomicdb_edge'::regclass)""")
        row = cur.fetchone()
        cur.execute("""
            SELECT relname, n_tup_ins, n_tup_upd, n_tup_hot_upd, n_live_tup
              FROM pg_stat_user_tables
             WHERE relname IN ('atomicdb_position', 'atomicdb_edge',
                               'atomicdb_ingestjob', 'atomicdb_dbevent')""")
        tables = {r[0]: {'ins': r[1], 'upd': r[2], 'hot_upd': r[3],
                         'live': r[4]} for r in cur.fetchall()}
        positions = max(row[5] or 0, tables.get('atomicdb_position',
                                                 {}).get('live') or 0)
        return {'database': row[0], 'position': row[1], 'edge': row[2],
                'ingestjob': row[3], 'dbevent': row[4],
                'positions_est': positions,
                'edges_est': max(row[6] or 0, tables.get(
                    'atomicdb_edge', {}).get('live') or 0),
                'bytes_per_position': (round(row[0] / positions, 1)
                                       if positions else None),
                'tables': tables}

    def queues():
        cur.execute("""SELECT count(*), min(created),
                              count(*) FILTER (WHERE attempts > 0)
                         FROM atomicdb_propagationseed""")
        seeds, oldest, failing = cur.fetchone()
        cur.execute("""SELECT state, count(*) FROM atomicdb_ingestjob
                        WHERE state IN ('PENDING', 'PROCESSING')
                        GROUP BY state""")
        open_jobs = dict(cur.fetchall())
        cur.execute("SELECT max(id) FROM atomicdb_ingestjob")
        max_job = cur.fetchone()[0]
        age = None
        if oldest is not None:
            age = round(time.time() - oldest.timestamp(), 1)
        return {'propagation_seeds': seeds, 'propagation_failing': failing,
                'propagation_oldest_s': age,
                'ingest_pending': open_jobs.get('PENDING', 0),
                'ingest_processing': open_jobs.get('PROCESSING', 0),
                'ingest_max_id': max_job}

    def sweeps():
        since = state.get('sweep_ts')
        if since is None:
            cur.execute("""SELECT max(ts) FROM atomicdb_dbevent
                            WHERE kind = 'BACKED_SWEEP'
                              AND ts > now() - interval '1 day'""")
            state['sweep_ts'] = (cur.fetchone()[0] or datetime.datetime.now(
                datetime.timezone.utc)).isoformat()
            return []
        cur.execute("""SELECT ts, payload->'stale', payload->'changed',
                              payload->'checked', payload->>'valve',
                              payload->>'mode'
                         FROM atomicdb_dbevent
                        WHERE kind = 'BACKED_SWEEP' AND ts > %s
                        ORDER BY ts LIMIT 100""", [since])
        passes = []
        for ts, stale, changed, checked, valve, mode in cur.fetchall():
            passes.append({'ts': ts.isoformat(), 'stale': stale,
                           'changed': changed, 'checked': checked,
                           'valve': valve, 'mode': mode})
            state['sweep_ts'] = ts.isoformat()
        return passes

    def stat_db():
        cur.execute("""SELECT xact_commit, xact_rollback, blks_read, blks_hit,
                              tup_returned, tup_fetched, tup_inserted,
                              tup_updated, tup_deleted, deadlocks,
                              temp_bytes, blk_read_time, blk_write_time
                         FROM pg_stat_database
                        WHERE datname = current_database()""")
        names = ('xact_commit', 'xact_rollback', 'blks_read', 'blks_hit',
                 'tup_returned', 'tup_fetched', 'tup_inserted', 'tup_updated',
                 'tup_deleted', 'deadlocks', 'temp_bytes', 'blk_read_time',
                 'blk_write_time')
        row = cur.fetchone()
        cur.execute("""SELECT state, wait_event_type, count(*)
                         FROM pg_stat_activity
                        WHERE datname = current_database()
                        GROUP BY 1, 2""")
        activity = {f'{s or "none"}/{w or "-"}': n
                    for s, w, n in cur.fetchall()}
        return {'counters': dict(zip(names, (float(v) if v is not None
                                             else None for v in row))),
                'activity': activity}

    def jobs():
        if not job_ids:
            return {}
        cur.execute("""SELECT id, extract(epoch FROM created),
                              extract(epoch FROM updated), state
                         FROM atomicdb_ingestjob WHERE id = ANY(%s)""",
                    [sorted(job_ids)])
        return {r[0]: (float(r[1]) if r[1] is not None else None,
                       float(r[2]) if r[2] is not None else None, r[3])
                for r in cur.fetchall()}

    block('sizes', sizes)
    block('queues', queues)
    block('sweeps', sweeps)
    block('pg', stat_db)
    block('job_times', jobs)
    return out


def pg_rates(pg, prev, elapsed):
    if not pg or not prev or not elapsed:
        return
    now, before = pg.get('counters') or {}, prev.get('counters') or {}
    rates = {}
    for name, value in now.items():
        old = before.get(name)
        if value is not None and old is not None and value >= old:
            rates[name] = round((value - old) / elapsed, 2)
    pg['per_s'] = rates


# ---------------------------------------------------------------- main


def load_state(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + '.tmp'
    with open(tmp, 'w') as handle:
        json.dump(state, handle)
    os.replace(tmp, path)


def prune(out_dir, keep_days, now):
    """Event files older than ``keep_days``; samples are kept."""
    cutoff = time.strftime('%Y%m%d', time.gmtime(now - keep_days * 86400))
    for path in glob.glob(os.path.join(out_dir, 'events-*.jsonl')):
        day = os.path.basename(path)[len('events-'):-len('.jsonl')]
        if day < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass


def take_sample(out_dir, timing_log, state, now=None, use_db=True):
    now = now or time.time()
    errors = []
    prev_t = state.get('t')
    elapsed = (now - prev_t) if prev_t else None
    sample = {'t': round(now, 3),
              'ts': datetime.datetime.fromtimestamp(
                  now, datetime.timezone.utc).isoformat(timespec='seconds'),
              'host': socket.gethostname(), 'window_s':
              round(elapsed, 3) if elapsed else None, 'v': 1}

    lines, info = read_appended(timing_log, state, 'timing', MAX_LOG_BYTES)
    records = [r for r in (parse_timing_line(line) for line in lines) if r]
    sample['http'] = summarize_requests(records)
    if info:
        sample['http_log'] = info

    events, event_info = read_events(out_dir, state, now)
    if event_info:
        sample['events_log'] = event_info
    job_ids = set()
    for e in events:
        if e.get('kind') == 'job' and isinstance(e.get('job'), int):
            job_ids.add(e['job'])
        if e.get('kind') == 'drain':
            job_ids.update(j for j in e.get('jobs') or () if isinstance(j, int))

    db = {}
    if use_db:
        try:
            with connect() as conn:
                db = db_sample(conn, state, job_ids, errors)
        except Exception as error:      # noqa: BLE001
            errors.append(f'connect: {type(error).__name__}: {error}'[:300])
    job_times = db.pop('job_times', {}) or {}
    sample.update(summarize_events(events, job_times))
    if elapsed:
        sample['applied_per_s'] = round(sample['jobs']['applied'] / elapsed, 3)
        sample['climbed_per_s'] = round(sample['drain']['landings'] / elapsed,
                                        3)
    pg = db.pop('pg', None)
    pg_rates(pg, state.get('pg'), elapsed)
    if pg:
        state['pg'] = {'counters': pg.get('counters')}
        sample['pg'] = pg
    sample.update(db)

    units = read_cgroups()
    cpu_percent(units, state.get('units'), elapsed)
    state['units'] = {k: {'cpu_usec': v.get('cpu_usec')}
                      for k, v in units.items()}
    sample['units'] = units
    disks = read_diskstats()
    disk_rates(disks, state.get('disks'), elapsed)
    state['disks'] = {k: {f: v[f] for f in ('reads', 'rsect', 'writes',
                                             'wsect', 'io_ms')}
                      for k, v in disks.items()}
    sample['disks'] = disks
    sample['host_stats'] = read_host()
    sample['sample_ms'] = round((time.time() - now) * 1000, 1)
    if errors:
        sample['errors'] = errors
    state['t'] = now
    return sample


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--out', default=os.environ.get(
        'ATOMICDB_PERF_DIR', DEFAULT_OUT))
    parser.add_argument('--timing-log', default=DEFAULT_TIMING_LOG)
    parser.add_argument('--keep-event-days', type=int, default=14)
    parser.add_argument('--no-db', action='store_true')
    parser.add_argument('--print', action='store_true',
                        help='Also print the sample to stdout.')
    args = parser.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    state_path = os.path.join(args.out, 'sampler-state.json')
    state = load_state(state_path)
    now = time.time()
    sample = take_sample(args.out, args.timing_log, state, now=now,
                         use_db=not args.no_db)
    day = time.strftime('%Y%m%d', time.gmtime(now))
    line = json.dumps(sample, separators=(',', ':'), default=str)
    with open(os.path.join(args.out, f'samples-{day}.jsonl'), 'a') as handle:
        handle.write(line + '\n')
    save_state(state_path, state)
    prune(args.out, args.keep_event_days, now)
    if args.print:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
