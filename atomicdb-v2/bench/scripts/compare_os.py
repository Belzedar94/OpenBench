"""Side-by-side table: the same bench stages on Windows (tower) and Linux (production box)."""
import json
import sys

STORES = ['mem', 'memwal', 'mmap', 'redb', 'fjall']
NAMES = {'mem': 'in memory', 'memwal': 'memory + WAL', 'mmap': 'mmap + WAL',
         'redb': 'redb', 'fjall': 'fjall'}


def load(path):
    out = {}
    for line in open(path, encoding='utf-8'):
        r = json.loads(line)
        if r.get('stage') in ('crash_trial', 'crash_summary'):
            continue
        out.setdefault(r['kind'], {})[r['stage']] = r
    return out


def g(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def f(v, scale=1.0, digits=0):
    if v is None:
        return '-'
    v = v * scale
    return f'{v:,.{digits}f}'


METRICS = [
    ('Searches/s, 1 per commit', ('ingest_b1', 'landings_per_s'), 1, 0),
    ('  commit p50, ms', ('ingest_b1', 'commit', 'p50_us'), 1e-3, 2),
    ('  commit p99, ms', ('ingest_b1', 'commit', 'p99_us'), 1e-3, 2),
    ('  compute p50, ms', ('ingest_b1', 'compute', 'p50_us'), 1e-3, 2),
    ('Searches/s, 32 per commit', ('ingest_b32', 'landings_per_s'), 1, 0),
    ('  batch p50, ms', ('ingest_b32', 'batch', 'p50_us'), 1e-3, 2),
    ('  batch p99, ms', ('ingest_b32', 'batch', 'p99_us'), 1e-3, 2),
    ('  commit p50, ms', ('ingest_b32', 'commit', 'p50_us'), 1e-3, 2),
    ('  compute p50, ms', ('ingest_b32', 'compute', 'p50_us'), 1e-3, 2),
    ('Re-searches/s, 32 per commit', ('climb_b32', 'landings_per_s'), 1, 0),
    ('Selector descents/s', ('select', 'descents_per_s'), 1, 0),
    ('Explorer reads/s, 1 reader', ('explore_1_reader', 'reads_per_s'), 1, 0),
    ('  p99, us', ('explore_1_reader', 'read', 'p99_us'), 1, 0),
    ('Explorer reads/s, 8 readers', ('explore_n_readers', 'reads_per_s'), 1, 0),
    ('Explorer reads/s, 8 readers + writer', ('explore_under_writes', 'reads_per_s'), 1, 0),
    ('  p99, us', ('explore_under_writes', 'read', 'p99_us'), 1, 0),
    ('  writer searches/s meanwhile', ('explore_under_writes', 'writer_landings_per_s'), 1, 0),
    ('Bulk load, s', ('load', 'seconds'), 1, 1),
    ('Bytes per position', ('footprint', 'bytes_per_position'), 1, 0),
    ('Open, ms', ('open', 'open_ms'), 1, 1),
]


def table(win, lin, stores):
    head = ['Stage'] + [f'{NAMES[s]} W / L' for s in stores]
    lines = ['| ' + ' | '.join(head) + ' |', '|' + '---|' + '---:|' * len(stores)]
    for label, path, scale, digits in METRICS:
        cells = [label]
        for s in stores:
            a = g(win.get(s, {}), *path)
            b = g(lin.get(s, {}), *path)
            cells.append(f'{f(a, scale, digits)} / {f(b, scale, digits)}')
        lines.append('| ' + ' | '.join(cells) + ' |')
    digests = {s: (g(win.get(s, {}), 'digest', 'digest'), g(lin.get(s, {}), 'digest', 'digest'))
               for s in stores}
    return '\n'.join(lines), digests


def crash(path):
    out = {}
    for line in open(path, encoding='utf-8'):
        r = json.loads(line)
        if r.get('stage') != 'crash_trial':
            continue
        slot = out.setdefault(r['kind'], {'n': 0, 'clean': 0, 'rec': []})
        slot['n'] += 1
        slot['clean'] += bool(r.get('durable_ok') and r.get('identical'))
        slot['rec'].append(r['recovery_ms'])
    for k, v in out.items():
        rec = sorted(v['rec'])
        v['p50'] = rec[len(rec) // 2]
        v['max'] = rec[-1]
    return out


if __name__ == '__main__':
    win, lin = load(sys.argv[1]), load(sys.argv[2])
    stores = [s for s in STORES if s in lin]
    text, digests = table(win, lin, stores)
    print(text)
    print()
    print('digests:', json.dumps(digests))
    if len(sys.argv) > 4:
        cw, cl = crash(sys.argv[3]), crash(sys.argv[4])
        print()
        print('| Store | Windows trials / clean / recovery p50 / max ms | Linux trials / clean / recovery p50 / max ms |')
        print('|---|---:|---:|')
        for s in ['mmap', 'memwal', 'redb', 'fjall']:
            a, b = cw.get(s), cl.get(s)
            fa = f"{a['n']} / {a['clean']} / {a['p50']:.1f} / {a['max']:.1f}" if a else '-'
            fb = f"{b['n']} / {b['clean']} / {b['p50']:.1f} / {b['max']:.1f}" if b else '-'
            print(f'| {NAMES[s]} | {fa} | {fb} |')
