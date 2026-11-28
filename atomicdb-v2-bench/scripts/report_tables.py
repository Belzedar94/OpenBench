"""Turn atomicdb-v2-bench JSON lines into the Markdown tables of the report.

    python report_tables.py results/S-real-ball.jsonl [--crash results/S-crash.jsonl]
"""
import collections
import json
import sys

LABEL = {
    'mem': 'in memory (no durability)', 'memwal': 'in memory + WAL + snapshot', 'mmap': 'mmap columns + WAL', 'mmapnf': 'mmap columns + WAL, key filter off', 'mmapcold': 'mmap columns + WAL, 8 GB available', 'lmdb': 'LMDB', 'sqlite': 'SQLite (WAL)',
    'rocksdb': 'RocksDB', 'redb': 'redb', 'fjall': 'fjall', 'pgint': 'PostgreSQL, integer ids', 'pgv1': 'PostgreSQL, v1 schema',
}


def load(path):
    rows = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip().startswith('{')]
    by = collections.defaultdict(dict)
    order = []
    for r in rows:
        k = r.get('kind') or r.get('store')
        if k not in order:
            order.append(k)
        by[k][r['stage']] = r
    return rows, by, order


def ms(us):
    v = us / 1000.0
    return ('%.2f' % v) if v < 10 else ('%.1f' % v) if v < 100 else ('%.0f' % v)


def num(v):
    return f'{v:,.0f}'


def table(head, lines):
    print('| ' + ' | '.join(head) + ' |')
    print('|' + '|'.join(['---'] + ['---:'] * (len(head) - 1)) + '|')
    for l in lines:
        print('| ' + ' | '.join(str(x) for x in l) + ' |')
    print()


def main():
    path = sys.argv[1]
    rows, by, order = load(path)
    crash = None
    if '--crash' in sys.argv:
        crash = [json.loads(l) for l in open(sys.argv[sys.argv.index('--crash') + 1], encoding='utf-8') if l.strip().startswith('{')]

    print('**Bulk load**\n')
    table(['Store', 'Seconds', 'Positions/s', 'Disk MB', 'Bytes/position'],
          [[LABEL[k], '%.1f' % r['seconds'], num(r['positions_per_s']), '%.0f' % (r['disk_bytes'] / 1e6), '%.0f' % r['bytes_per_position']]
           for k in order if (r := by[k].get('load'))])

    for stage, title in (('ingest_b1', 'Ingest, one search per durable commit'), ('ingest_b32', 'Ingest, 32 searches per durable commit'),
                         ('climb_b32', 'Re-search of interior positions, 32 per durable commit')):
        if not any(stage in by[k] for k in order):
            continue
        print(f'**{title}** (latency of one batch, ms)\n')
        table(['Store', 'Searches/s', 'p50', 'p95', 'p99', 'max', 'compute p50', 'commit p50', 'commit p99'],
              [[LABEL[k], num(r['landings_per_s']), ms(r['batch']['p50_us']), ms(r['batch']['p95_us']), ms(r['batch']['p99_us']), ms(r['batch']['max_us']),
                ms(r['compute']['p50_us']), ms(r['commit']['p50_us']), ms(r['commit']['p99_us'])] for k in order if (r := by[k].get(stage))])
        r = next((by[k][stage] for k in order if stage in by[k]), None)
        p = r['per_landing']
        print('Per search: %.0f position reads, %.1f edge-block reads, %.1f parent reads, %.1f key lookups, %.1f new positions, %.1f new edges, '
              '%.1f rewrites, %.2f closures, %.2f backed-value writes, %s logical bytes; %.1f levels per batch.\n' % (
                  p['node_reads'], p['block_reads'], p['parent_reads'], p['key_lookups'], p['new_nodes'], p['new_edges'], p['rewrites'],
                  p['closures'], p['backed_writes'], num(p['logical_bytes']), r['levels_per_batch']))

    print('**Selector: descents from the root to a leaf**\n')
    table(['Store', 'Descents/s', 'p50 ms', 'p95 ms', 'p99 ms'],
          [[LABEL[k], num(r['descents_per_s']), ms(r['descent']['p50_us']), ms(r['descent']['p95_us']), ms(r['descent']['p99_us'])] for k in order if (r := by[k].get('select'))])

    for stage, title in (('explore_1_reader', 'Explorer reads, 1 reader, nobody writing'), ('explore_n_readers', 'Explorer reads, 8 readers, nobody writing'),
                         ('explore_under_writes', 'Explorer reads, 8 readers, while the writer ingests 32 searches per commit')):
        if not any(stage in by[k] for k in order):
            continue
        print(f'**{title}** (latency of one read, microseconds)\n')
        head = ['Store', 'Reads/s', 'p50', 'p95', 'p99', 'max']
        under = stage == 'explore_under_writes'
        if under:
            head += ['Writer searches/s', 'vs alone']
        lines = []
        for k in order:
            r = by[k].get(stage)
            if not r:
                continue
            l = [LABEL[k], num(r['reads_per_s']), r['read']['p50_us'], r['read']['p95_us'], r['read']['p99_us'], num(r['read']['max_us'])]
            if under:
                alone = by[k].get('ingest_b32', {}).get('landings_per_s')
                l += [num(r['writer_landings_per_s']), ('%.0f%%' % (100 * r['writer_landings_per_s'] / alone)) if alone else '']
            lines.append(l)
        table(head, lines)

    print('**Footprint and writes**\n')
    lines = []
    for k in order:
        r = by[k].get('footprint')
        if not r:
            continue
        wa = by[k].get('ingest_b32', {})
        lines.append([LABEL[k], num(r['positions']), '%.0f' % (r['disk_bytes'] / 1e6), '%.0f' % r['bytes_per_position'],
                      '%.0f' % (r['peak_rss_bytes'] / 1e6), '%.0f' % (r['private_bytes'] / 1e6),
                      ('%.1f' % wa['write_amplification']) if wa else '', '%.1f' % by[k].get('open', {}).get('open_ms', 0)])
    table(['Store', 'Positions', 'Disk MB', 'Bytes/position', 'Peak RSS MB', 'Private MB', 'Write amplification (ingest 32)', 'Open ms'], lines)

    ds = {k: by[k]['digest']['digest'] for k in order if 'digest' in by[k]}
    print('State hash after the suite: ' + ('**identical for all %d stores** (`%s`).' % (len(ds), next(iter(ds.values()))) if len(set(ds.values())) == 1
                                         else 'DIFFERENT: ' + json.dumps(ds)))
    print()

    if crash:
        print('**Crash recovery** (kill -9 at a random moment)\n')
        trials = collections.defaultdict(list)
        for r in crash:
            if r['stage'] == 'crash_trial':
                trials[r['kind']].append(r)
        lines = []
        for k, ts in trials.items():
            rec = sorted(t['recovery_ms'] for t in ts)
            q = lambda p: rec[min(len(rec) - 1, int(p * len(rec)))]
            lines.append([LABEL[k], len(ts), sum(1 for t in ts if t['durable_ok'] and t['identical']), sum(1 for t in ts if not t['durable_ok']),
                          sum(1 for t in ts if not t['identical']), '%.1f' % q(0.5), '%.1f' % q(0.95), '%.1f' % rec[-1], num(ts[-1]['recovered_seq'])])
        table(['Store', 'Trials', 'Clean', 'Lost an acknowledged batch', 'State differs', 'Recovery ms p50', 'p95', 'max', 'Batches by the end'], lines)


if __name__ == '__main__':
    main()
