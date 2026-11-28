"""Where the cost of one fdatasync goes on this box.

(a) append 4 KB + fdatasync: the file grows, so ext4 must commit its journal
    (metadata) as well as flush the data;
(b) overwrite 4 KB inside a preallocated, already written file + fdatasync:
    no metadata changes, so only the data and the device cache flush;
(c) the same as (b) with O_DIRECT | O_DSYNC (one write, no page cache).
Repeated ROUNDS times, interleaved, so the box's own load hits all three.
"""
import json
import mmap
import os
import sys
import time

N = 200
ROUNDS = 3
SIZE = 4096
PREALLOC = 64 * 1024 * 1024


def stats(lat):
    lat = sorted(lat)
    n = len(lat)
    pick = lambda p: round(lat[min(n - 1, max(0, -(-p * n // 100) - 1))], 3)
    return {'n': n, 'mean_ms': round(sum(lat) / n, 3), 'p50_ms': pick(50),
            'p90_ms': pick(90), 'p99_ms': pick(99), 'max_ms': round(lat[-1], 3)}


def append(path):
    data = os.urandom(SIZE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
    lat = []
    for _ in range(N):
        os.write(fd, data)
        t = time.perf_counter()
        os.fdatasync(fd)
        lat.append((time.perf_counter() - t) * 1000)
    os.close(fd)
    os.remove(path)
    return lat


def prepared(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    block = b'\0' * (1 << 20)
    for _ in range(PREALLOC >> 20):
        os.write(fd, block)
    os.fsync(fd)
    return fd


def overwrite(path):
    fd = prepared(path)
    data = os.urandom(SIZE)
    lat = []
    for i in range(N):
        os.pwrite(fd, data, i * SIZE)
        t = time.perf_counter()
        os.fdatasync(fd)
        lat.append((time.perf_counter() - t) * 1000)
    os.close(fd)
    os.remove(path)
    return lat


def direct(path):
    os.close(prepared(path))
    fd = os.open(path, os.O_RDWR | os.O_DIRECT | os.O_DSYNC)
    buf = mmap.mmap(-1, SIZE)
    buf.write(os.urandom(SIZE))
    lat = []
    for i in range(N):
        t = time.perf_counter()
        os.pwritev(fd, [buf], i * SIZE)
        lat.append((time.perf_counter() - t) * 1000)
    os.close(fd)
    os.remove(path)
    return lat


path = sys.argv[1]
out = {'t': time.time(), 'bytes': SIZE}
acc = {'append_fdatasync': [], 'overwrite_fdatasync': [], 'direct_dsync': []}
for _ in range(ROUNDS):
    acc['append_fdatasync'] += append(path)
    acc['overwrite_fdatasync'] += overwrite(path)
    acc['direct_dsync'] += direct(path)
for name, lat in acc.items():
    out[name] = stats(lat)
for dev in ('nvme0n1', 'nvme1n1'):
    info = {}
    for name, rel in (('model', 'device/model'), ('write_cache', 'queue/write_cache'),
                      ('fua', 'queue/fua')):
        try:
            info[name] = open(f'/sys/block/{dev}/{rel}').read().strip()
        except OSError:
            pass
    out[dev] = info
out['loadavg'] = open('/proc/loadavg').read().split()[:3]
print(json.dumps(out))
