#!/usr/bin/env python3
"""The v1 baseline report: markdown tables over a window of the samples.

Reads what Scripts/atomicdb_perf_sample.py and atomicdb/perf_events.py leave
in /var/lib/atomicdb-perf, plus the nginx timing log, and prints the tables
of docs/atomicdb/v2-baseline.md for any window:

    atomicdb_perf_report.py --since 2026-11-28T07:30 --until 2026-11-29T07:30

Percentiles of latencies are exact: they are computed from the raw events
(climbs, jobs) and the raw nginx lines, and submit-to-visible looks up the
``created``/``updated`` of every job of the window by primary key (chunks of
5,000, read-only).  CPU, memory, disk, queues and sweeps come from the
minute samples.  Windows to leave out (a benchmark running on the box, say)
are given with ``--exclude START/END``.
"""

import argparse
import datetime
import glob
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atomicdb_perf_sample as sampler  # noqa: E402

PERF_DIR = '/var/lib/atomicdb-perf'
TIMING_GLOB = '/var/log/nginx/atomicdb-timing.log*'
HTTP_ORDER = ('lease', 'heartbeat', 'submit', 'release', 'solve', 'api_query',
              'api_public', 'api_other', 'explore', 'home', 'pages')


def epoch(text):
    when = datetime.datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.timestamp()


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc) \
        .strftime('%Y-%m-%d %H:%M UTC')


def in_window(t, lo, hi, excluded):
    return lo <= t < hi and not any(a <= t < b for a, b in excluded)


def read_jsonl(paths):
    for path in paths:
        opener = gzip.open if path.endswith('.gz') else open
        try:
            with opener(path, 'rt') as handle:
                for line in handle:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        pass
        except OSError:
            pass


def fmt(value, digits=0):
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:,.{digits}f}'
    return f'{value:,}'


def row(cells):
    return '| ' + ' | '.join(str(c) for c in cells) + ' |'


def pct_cells(stats, digits=0):
    return [fmt(stats.get('n')), fmt(stats.get('p50'), digits),
            fmt(stats.get('p90'), digits), fmt(stats.get('p95'), digits),
            fmt(stats.get('p99'), digits), fmt(stats.get('max'), digits)]


def job_times(job_ids):
    out = {}
    ids = sorted(job_ids)
    with sampler.connect() as conn:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '30s'")
        for start in range(0, len(ids), 5000):
            cur.execute("""SELECT id, extract(epoch FROM created),
                                  extract(epoch FROM updated)
                             FROM atomicdb_ingestjob WHERE id = ANY(%s)""",
                        [ids[start:start + 5000]])
            for job, created, updated in cur.fetchall():
                out[job] = (float(created), float(updated))
    return out


def http_records(lo, hi, excluded, pattern):
    paths = sorted(glob.glob(pattern), key=lambda p: (len(p), p),
                   reverse=True)
    for path in paths:
        opener = gzip.open if path.endswith('.gz') else open
        try:
            with opener(path, 'rt', errors='replace') as handle:
                for line in handle:
                    record = sampler.parse_timing_line(line)
                    if record and in_window(record[0], lo, hi, excluded):
                        yield record
        except OSError:
            pass


def report(args):
    lo, hi = epoch(args.since), epoch(args.until)
    excluded = []
    for item in args.exclude or ():
        a, b = item.split('/')
        excluded.append((epoch(a), epoch(b)))
    span = hi - lo - sum(max(0, min(b, hi) - max(a, lo)) for a, b in excluded)
    out = []

    samples = [s for s in read_jsonl(sorted(glob.glob(
        os.path.join(args.perf_dir, 'samples-*.jsonl'))))
        if in_window(s.get('t', 0), lo, hi, excluded)]
    events = [e for e in read_jsonl(sorted(glob.glob(
        os.path.join(args.perf_dir, 'events-*.jsonl'))))
        if in_window(e.get('t', 0), lo, hi, excluded)]
    jobs = [e for e in events if e.get('kind') == 'job']
    drains = [e for e in events if e.get('kind') == 'drain']
    ok_jobs = [e for e in jobs if not e.get('error')]

    out.append(f'Window: {iso(lo)} to {iso(hi)}'
               + (f', excluding {len(excluded)} interval(s)' if excluded
                  else '')
               + f'; {span / 3600:.1f} h measured, {len(samples)} minute '
                 f'samples, {len(events):,} events.')
    out.append('')

    # Throughput
    per_minute = {}
    for e in ok_jobs:
        per_minute[int(e['t'] // 60)] = per_minute.get(int(e['t'] // 60),
                                                       0) + 1
    minute_rates = [n / 60 for n in per_minute.values()]
    climbed = sum(e.get('settled') or 0 for e in drains)
    out.append('### Throughput')
    out.append('')
    out.append(row(['Metric', 'Value']))
    out.append(row(['---', '---:']))
    out.append(row(['Searches applied (stage a), total', fmt(len(ok_jobs))]))
    out.append(row(['Searches applied per second, mean',
                    fmt(len(ok_jobs) / span, 2) if span else '']))
    rates = sampler.percentiles(minute_rates)
    out.append(row(['Per second by minute, p50 / p95 / max',
                    f"{fmt(rates.get('p50'), 2)} / {fmt(rates.get('p95'), 2)}"
                    f" / {fmt(rates.get('max'), 2)}"]))
    out.append(row(['Landings climbed per second, mean',
                    fmt(climbed / span, 2) if span else '']))
    out.append(row(['Failed job attempts', fmt(len(jobs) - len(ok_jobs))]))
    out.append(row(['Climb batches', fmt(len(drains))]))
    out.append('')

    # Latency of the pipeline
    times = {}
    if not args.no_db:
        wanted = {e['job'] for e in ok_jobs if isinstance(e.get('job'), int)}
        for e in drains:
            wanted.update(j for j in e.get('jobs') or () if isinstance(j, int))
        times = job_times(wanted)
    own = [(times[e['job']][1] - times[e['job']][0]) * 1000 for e in ok_jobs
           if e.get('job') in times]
    climb_lat = [(e['commit_t'] - times[j][0]) * 1000 for e in drains
                 for j in e.get('jobs') or () if j in times
                 and e.get('commit_t')]
    out.append('### Latency of a search (ms)')
    out.append('')
    out.append(row(['Metric', 'n', 'p50', 'p90', 'p95', 'p99', 'max']))
    out.append(row(['---'] + ['---:'] * 6))
    out.append(row(['Submit to own result visible (job DONE)']
                   + pct_cells(sampler.percentiles(own))))
    out.append(row(['Submit to climbed (drainer commit)']
                   + pct_cells(sampler.percentiles(climb_lat))))
    out.append(row(['Stage (a) apply, one job']
                   + pct_cells(sampler.percentiles(
                       [e.get('ms') for e in ok_jobs]))))
    out.append(row(['Climb transaction, one batch']
                   + pct_cells(sampler.percentiles(
                       [e.get('climb_ms') for e in drains]))))
    out.append(row(['Climb batch incl. after-commit work']
                   + pct_cells(sampler.percentiles(
                       [e.get('ms') for e in drains]))))
    out.append(row(['Landings per climb batch']
                   + pct_cells(sampler.percentiles(
                       [e.get('settled') for e in drains]))))
    golden = [e.get('climb_ms') for e in drains if e.get('golden')]
    if golden:
        out.append(row(['Climb transaction, golden-case batches only']
                       + pct_cells(sampler.percentiles(golden))))
        out.append(row(['Climb transaction, other batches']
                       + pct_cells(sampler.percentiles(
                           [e.get('climb_ms') for e in drains
                            if not e.get('golden')]))))
    out.append('')

    # HTTP
    if not args.no_http:
        records = list(http_records(lo, hi, excluded, args.timing_glob))
        by_class = {}
        for ts, status, rt, upstream, method, uri in records:
            cls = sampler.classify(uri)
            if cls is None:
                continue
            slot = by_class.setdefault(cls, {'rt': [], 'up': [], '5xx': 0,
                                             '429': 0})
            slot['rt'].append(rt * 1000)
            if upstream is not None:
                slot['up'].append(upstream * 1000)
            if status >= 500:
                slot['5xx'] += 1
            elif status == 429:
                slot['429'] += 1
        out.append('### HTTP latency by route class (request time as nginx '
                   'saw it, ms)')
        out.append('')
        out.append(row(['Class', 'Requests', 'p50', 'p90', 'p95', 'p99',
                        'max', 'upstream p50', 'upstream p99', '5xx',
                        '429']))
        out.append(row(['---'] + ['---:'] * 10))
        for cls in sorted(by_class, key=lambda c: (
                HTTP_ORDER.index(c) if c in HTTP_ORDER else 99, c)):
            slot = by_class[cls]
            stats = sampler.percentiles(slot['rt'])
            up = sampler.percentiles(slot['up'])
            out.append(row([cls] + pct_cells(stats)
                           + [fmt(up.get('p50')), fmt(up.get('p99')),
                              fmt(slot['5xx']), fmt(slot['429'])]))
        out.append('')

    # Units
    units = {}
    for s in samples:
        for name, entry in (s.get('units') or {}).items():
            slot = units.setdefault(name, {'cpu': [], 'mem': [], 'anon': []})
            if entry.get('cpu_pct') is not None:
                slot['cpu'].append(entry['cpu_pct'])
            if entry.get('mem') is not None:
                slot['mem'].append(entry['mem'] / 2**20)
            if entry.get('mem_anon') is not None:
                slot['anon'].append(entry['mem_anon'] / 2**20)
    out.append('### CPU and memory by unit')
    out.append('')
    out.append('CPU in % of one thread (the box has 12), per minute; memory '
               'is the cgroup\'s `memory.current` (includes page cache) and '
               'its anonymous part, MiB.')
    out.append('')
    out.append(row(['Unit', 'CPU mean', 'CPU p95', 'Mem mean', 'Mem max',
                    'Anon mean', 'Anon max']))
    out.append(row(['---'] + ['---:'] * 6))
    for name in sorted(units, key=lambda n: -sum(units[n]['cpu'] or [0])):
        slot = units[name]
        cpu = sampler.percentiles(slot['cpu'])
        mem = sampler.percentiles(slot['mem'])
        anon = sampler.percentiles(slot['anon'])
        out.append(row([name.replace('.service', ''), fmt(cpu.get('mean'), 1),
                        fmt(cpu.get('p95'), 1), fmt(mem.get('mean')),
                        fmt(mem.get('max')), fmt(anon.get('mean')),
                        fmt(anon.get('max'))]))
    out.append('')

    # Disk and growth
    sized = [s for s in samples if s.get('sizes')]
    out.append('### Disk and growth')
    out.append('')
    out.append(row(['Metric', 'Value']))
    out.append(row(['---', '---:']))
    if len(sized) >= 2:
        a, b = sized[0], sized[-1]
        hours = (b['t'] - a['t']) / 3600
        day = 24 / hours if hours else 0
        sa, sb = a['sizes'], b['sizes']
        out.append(row(['Database size at the end, GB',
                        fmt(sb['database'] / 1e9, 1)]))
        out.append(row(['Positions (estimate) at the end',
                        fmt(sb['positions_est'])]))
        out.append(row(['Bytes per position, whole database',
                        fmt(sb.get('bytes_per_position'), 0)]))
        out.append(row(['Bytes per position, position + edge tables',
                        fmt((sb['position'] + sb['edge'])
                            / max(1, sb['positions_est']), 0)]))
        ins_a = sa['tables'].get('atomicdb_position', {}).get('ins')
        ins_b = sb['tables'].get('atomicdb_position', {}).get('ins')
        if ins_a is not None and ins_b is not None and ins_b >= ins_a:
            out.append(row(['New positions per day (inserts)',
                            fmt(int((ins_b - ins_a) * day))]))
        out.append(row(['Database growth per day, GB',
                        fmt((sb['database'] - sa['database']) / 1e9 * day,
                            2)]))
        ha, hb = a.get('host_stats', {}), b.get('host_stats', {})
        if ha.get('disk_used') and hb.get('disk_used'):
            out.append(row(['Root file system growth per day, GB',
                            fmt((hb['disk_used'] - ha['disk_used']) / 1e9
                                * day, 2)]))
            out.append(row(['Free at the end, GB',
                            fmt(hb.get('disk_free', 0) / 1e9, 0)]))
    md = [s['disks']['md2']['util_pct'] for s in samples
          if s.get('disks', {}).get('md2', {}).get('util_pct') is not None]
    if md:
        stats = sampler.percentiles(md)
        out.append(row(['md2 utilisation %, mean / p95',
                        f"{fmt(stats['mean'], 0)} / {fmt(stats['p95'], 0)}"]))
    loads = [s['host_stats']['load'][0] for s in samples
             if s.get('host_stats', {}).get('load')]
    if loads:
        stats = sampler.percentiles(loads)
        out.append(row(['Load average (1 min), mean / p95',
                        f"{fmt(stats['mean'], 1)} / {fmt(stats['p95'], 1)}"]))
    out.append('')

    # Queues and sweeps
    seeds = [s['queues']['propagation_seeds'] for s in samples
             if s.get('queues')]
    oldest = [s['queues']['propagation_oldest_s'] or 0 for s in samples
              if s.get('queues')]
    pending = [s['queues']['ingest_pending'] for s in samples
               if s.get('queues')]
    passes = [p for s in samples for p in s.get('sweeps') or ()]
    out.append('### Queues and the stale-value sweep')
    out.append('')
    out.append(row(['Metric', 'mean', 'p50', 'p95', 'max']))
    out.append(row(['---'] + ['---:'] * 4))
    for label, values in (('Propagation queue depth (seeds waiting)', seeds),
                          ('Oldest seed waiting, s', oldest),
                          ('Ingest jobs pending', pending),
                          ('Stale rows found per sweep pass',
                           [p.get('stale') for p in passes]),
                          ('Rows changed per sweep pass',
                           [p.get('changed') for p in passes]),
                          ('Rows checked per sweep pass',
                           [p.get('checked') for p in passes])):
        stats = sampler.percentiles([v for v in values
                                     if isinstance(v, (int, float))])
        out.append(row([label, fmt(stats.get('mean'), 1),
                        fmt(stats.get('p50')), fmt(stats.get('p95')),
                        fmt(stats.get('max'))]))
    out.append(row(['Sweep passes in the window', fmt(len(passes)), '', '',
                    '']))
    out.append('')
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True)
    parser.add_argument('--exclude', action='append',
                        help='START/END in ISO format, repeatable')
    parser.add_argument('--perf-dir', default=PERF_DIR)
    parser.add_argument('--timing-glob', default=TIMING_GLOB)
    parser.add_argument('--no-db', action='store_true')
    parser.add_argument('--no-http', action='store_true')
    args = parser.parse_args(argv)
    print(report(args))
    return 0


if __name__ == '__main__':
    sys.exit(main())
