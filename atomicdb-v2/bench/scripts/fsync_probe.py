"""One fdatasync on this file system: append N bytes, fdatasync, repeat.

Open question 1 of docs/atomicdb/v2-plan.md: what one durable commit costs
on production's md RAID1.  Appends of 4 KB (one search's redo frame) and
64 KB (a batch of ~32), 400 of each, plus an fsync of the same file.
"""
import json
import os
import sys
import time


def run(path, size, count, sync):
    data = os.urandom(size)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
    lat = []
    try:
        for _ in range(count):
            os.write(fd, data)
            t = time.perf_counter()
            sync(fd)
            lat.append((time.perf_counter() - t) * 1000.0)
    finally:
        os.close(fd)
        os.remove(path)
    lat.sort()
    n = len(lat)
    pick = lambda p: round(lat[min(n - 1, max(0, -(-p * n // 100) - 1))], 3)
    return {'bytes': size, 'n': n, 'mean_ms': round(sum(lat) / n, 3),
            'p50_ms': pick(50), 'p90_ms': pick(90), 'p99_ms': pick(99),
            'max_ms': round(lat[-1], 3), 'min_ms': round(lat[0], 3)}


path = sys.argv[1]
out = {'t': time.time(), 'path': path}
for size in (4096, 65536):
    out[f'fdatasync_{size}'] = run(path, size, 400, os.fdatasync)
out['fsync_4096'] = run(path, 4096, 200, os.fsync)
print(json.dumps(out))
