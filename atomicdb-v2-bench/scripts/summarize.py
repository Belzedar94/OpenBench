"""Summarise atomicdb-v2-bench JSON lines as plain-text tables (also used to build the report)."""
import json, sys, collections

path = sys.argv[1]
rows = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip().startswith('{')]
by = collections.defaultdict(dict)
order = []
for r in rows:
    k = r.get('kind') or r.get('store')
    if k not in order:
        order.append(k)
    by[k][r['stage']] = r

def ms(us):
    return "%.2f" % (us / 1000.0)

def g(d, *ks, default=None):
    for k in ks:
        if d is None or k not in d:
            return default
        d = d[k]
    return d

print("== load")
print("%-8s %10s %12s %10s %8s" % ("store", "seconds", "positions/s", "disk MB", "B/pos"))
for k in order:
    r = by[k].get('load')
    if r:
        print("%-8s %10.1f %12.0f %10.1f %8.1f" % (k, r['seconds'], r['positions_per_s'], r['disk_bytes'] / 1e6, r['bytes_per_position']))

for stage in ('ingest_b1', 'ingest_b32', 'climb_b32'):
    print("\n== %s  (batch latency ms; compute = reads+climb, commit = durable write)" % stage)
    print("%-8s %10s %8s %8s %8s %8s %9s %9s %9s %7s" % ("store", "landings/s", "p50", "p95", "p99", "max", "comp p50", "comm p50", "comm p99", "wamp"))
    for k in order:
        r = by[k].get(stage)
        if r:
            print("%-8s %10.0f %8s %8s %8s %8s %9s %9s %9s %7.1f" % (k, r['landings_per_s'], ms(r['batch']['p50_us']), ms(r['batch']['p95_us']), ms(r['batch']['p99_us']), ms(r['batch']['max_us']),
                                                                   ms(r['compute']['p50_us']), ms(r['commit']['p50_us']), ms(r['commit']['p99_us']), r['write_amplification']))
    r = next((by[k][stage] for k in order if stage in by[k]), None)
    if r:
        p = r['per_landing']
        print("   per landing: %.0f node reads, %.1f edge-block reads, %.1f parent reads, %.1f key lookups, %.1f new positions, %.1f new edges, %.1f rewrites, %.2f closures, %.2f backed writes, %.0f logical bytes; %.1f levels/batch" % (
            p['node_reads'], p['block_reads'], p['parent_reads'], p['key_lookups'], p['new_nodes'], p['new_edges'], p['rewrites'], p['closures'], p['backed_writes'], p['logical_bytes'], r['levels_per_batch']))

print("\n== select (descent from the root to a leaf)")
print("%-8s %12s %8s %8s %8s" % ("store", "descents/s", "p50 ms", "p95 ms", "p99 ms"))
for k in order:
    r = by[k].get('select')
    if r:
        print("%-8s %12.0f %8s %8s %8s" % (k, r['descents_per_s'], ms(r['descent']['p50_us']), ms(r['descent']['p95_us']), ms(r['descent']['p99_us'])))

for stage in ('explore_1_reader', 'explore_n_readers', 'explore_under_writes'):
    print("\n== %s (one read = position by key + record + all moves with their records + parents; latency in microseconds)" % stage)
    print("%-8s %8s %12s %8s %8s %8s %9s %14s" % ("store", "readers", "reads/s", "p50", "p95", "p99", "max", "writer land/s"))
    for k in order:
        r = by[k].get(stage)
        if r:
            print("%-8s %8d %12.0f %8d %8d %8d %9d %14.0f" % (k, r['readers'], r['reads_per_s'], r['read']['p50_us'], r['read']['p95_us'], r['read']['p99_us'], r['read']['max_us'], r['writer_landings_per_s']))

print("\n== footprint after the suite")
print("%-8s %12s %10s %8s %10s %12s %10s" % ("store", "positions", "disk MB", "B/pos", "RSS MB", "private MB", "open ms"))
for k in order:
    r = by[k].get('footprint')
    if r:
        print("%-8s %12d %10.1f %8.1f %10.0f %12.0f %10.1f" % (k, r['positions'], r['disk_bytes'] / 1e6, r['bytes_per_position'], r['peak_rss_bytes'] / 1e6, r['private_bytes'] / 1e6, g(by[k], 'open', 'open_ms', default=0)))

print("\n== digest")
ds = {k: by[k]['digest']['digest'] for k in order if 'digest' in by[k]}
for k, d in ds.items():
    print("%-8s %s" % (k, d))
print("all equal:", len(set(ds.values())) == 1)

cr = [r for r in rows if r['stage'] == 'crash_summary']
if cr:
    print("\n== crash")
    for r in cr:
        print("%-8s trials %d ok %d lost %d differs %d  recovery ms p50 %s p99 %s max %s" % (r['store'], r['trials'], r['ok'], r['lost_acknowledged'], r['state_differs'],
              ms(r['recovery']['p50_us']), ms(r['recovery']['p99_us']), ms(r['recovery']['max_us'])))
