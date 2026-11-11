#!/usr/bin/env python3
"""Cut a training set into RELABEL input chunks plus a manifest of keys.

Example (Alice T349, the 48 train chunks, 100k records per input chunk):

    python Scripts/relabel_split.py --set t349-train-v1 \\
        --format alice-v2-record-v1 --container alice-v2-chunk \\
        --records-per-chunk 100000 --out D:/alice-train/relabel/t349-train-v1 \\
        D:/alice-train/t349/chunks/chunk_{0..47}.bin

Sources are read in the order given and their records are concatenated
unchanged. ``alice-v2-chunk`` sources (ALICE_V2_CHUNK_V1: a 4096-byte
authenticated header, then 84-byte records) have their header hash, record
count and payload SHA-256 checked; ``raw`` sources are bare records. Every
record must decode as a position of the format's variant, so a wrong-variant
or corrupt record stops the split before anything is installed.

The output directory receives chunk_<i>.bz2 and manifest.json, in the layout
``Media/relabel/inputs/<set>/`` expects. Per chunk the manifest records the
compressed and raw SHA-256, the SHA-256 of the position keys (each record
without its mutable bytes), and which source records it holds.
"""

import argparse
import bz2
import hashlib
import json
import os
import sys
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Client'))

import relabel  # noqa: E402

MANIFEST_SCHEMA = 'openbench-relabel-input-v1'
ALICE_CHUNK_HEADER = 4096


def alice_chunk_payload(path):
    """Return (payload, provenance) of one authenticated ALICE_V2_CHUNK_V1 file."""

    with open(path, 'rb') as handle:
        header = handle.read(ALICE_CHUNK_HEADER)
        payload = handle.read()
    if len(header) != ALICE_CHUNK_HEADER or header[:8] != b'ALCHNK1\0':
        raise SystemExit('%s: not an ALICE_V2_CHUNK_V1 file' % path)
    if hashlib.sha256(header[:-32]).digest() != header[-32:]:
        raise SystemExit('%s: chunk header SHA-256 mismatch' % path)
    manifest_bytes = int.from_bytes(header[12:16], 'little')
    manifest = json.loads(header[16:16 + manifest_bytes].decode('utf-8'))
    if manifest['record_bytes'] != 84 or len(payload) != manifest['record_count'] * 84:
        raise SystemExit('%s: payload length disagrees with record_count' % path)
    if hashlib.sha256(payload).hexdigest().upper() != manifest['payload_sha256']:
        raise SystemExit('%s: payload SHA-256 mismatch' % path)
    provenance = {
        'file': os.path.basename(path),
        'header_sha256': header[-32:].hex(),
        'payload_sha256': manifest['payload_sha256'].lower(),
        'records': manifest['record_count'],
        'chunk_index': manifest['partition']['chunk_index'],
        'split': manifest['partition']['split'],
        'run_config_sha256': manifest['run_config_sha256'].lower(),
        'source_commit': manifest['source_commit'],
        'depth': manifest['generation']['depth'],
    }
    return payload, provenance


def raw_payload(path, record_bytes):
    with open(path, 'rb') as handle:
        payload = handle.read()
    if len(payload) % record_bytes:
        raise SystemExit('%s: size is not a whole number of records' % path)
    return payload, {
        'file': os.path.basename(path),
        'payload_sha256': hashlib.sha256(payload).hexdigest(),
        'records': len(payload) // record_bytes,
    }


def check_records(job):
    """Decode every record of one block; return the first failure or None."""

    format_name, data, first = job
    fmt = relabel.record_format(format_name)
    decode = relabel.DECODERS[format_name]
    size = fmt['record_bytes']
    for index in range(len(data) // size):
        try:
            decode(data[index * size:(index + 1) * size])
        except relabel.RelabelFormatError as error:
            return 'record %d: %s' % (first + index, error)
    return None


def write_chunk(job):
    format_name, out_dir, idx, data, sources = job
    fmt = relabel.record_format(format_name)
    compressed = bz2.compress(data, 9)
    name = 'chunk_%d.bz2' % idx
    tmp = os.path.join(out_dir, name + '.tmp')
    with open(tmp, 'wb') as handle:
        handle.write(compressed)
    os.replace(tmp, os.path.join(out_dir, name))
    return {
        'idx': idx,
        'file': name,
        'records': len(data) // fmt['record_bytes'],
        'sha256': hashlib.sha256(compressed).hexdigest(),
        'bytes': len(compressed),
        'raw_sha256': hashlib.sha256(data).hexdigest(),
        'keys_sha256': hashlib.sha256(relabel.key_projection(data, fmt)).hexdigest(),
        'sources': sources,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--set', required=True, help='input set name (lowercase slug)')
    parser.add_argument('--format', required=True, choices=sorted(relabel.FORMATS))
    parser.add_argument('--container', default='raw', choices=['raw', 'alice-v2-chunk'])
    parser.add_argument('--records-per-chunk', type=int, required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('sources', nargs='+')
    args = parser.parse_args(argv)

    fmt = relabel.record_format(args.format)
    size = fmt['record_bytes']
    if args.container == 'alice-v2-chunk' and args.format != 'alice-v2-record-v1':
        parser.error('alice-v2-chunk sources hold alice-v2-record-v1 records')
    if args.records_per_chunk <= 0:
        parser.error('--records-per-chunk must be positive')
    os.makedirs(args.out, exist_ok=True)
    if os.listdir(args.out):
        parser.error('--out must be an empty directory')

    chunks = []
    provenance = []
    pending = bytearray()
    pending_sources = []
    total = 0
    with Pool(max(1, args.jobs)) as pool:

        def flush(final):
            nonlocal pending, pending_sources
            per = args.records_per_chunk * size
            jobs = []
            while len(pending) >= per or (final and pending):
                take = min(per, len(pending))
                block = bytes(pending[:take])
                del pending[:take]
                records = take // size
                sources = []
                while records:
                    src = pending_sources[0]
                    used = min(records, src['count'])
                    sources.append({'source': src['source'], 'first': src['first'], 'count': used})
                    src['first'] += used
                    src['count'] -= used
                    records -= used
                    if not src['count']:
                        pending_sources.pop(0)
                jobs.append((args.format, args.out, len(chunks) + len(jobs), block, sources))
            for result in pool.imap(write_chunk, jobs):
                chunks.append(result)
                print('chunk %d: %d records, %d bytes' % (
                    result['idx'], result['records'], result['bytes']), flush=True)

        for source_index, path in enumerate(args.sources):
            if args.container == 'alice-v2-chunk':
                payload, info = alice_chunk_payload(path)
            else:
                payload, info = raw_payload(path, size)
            step = 50000 * size
            jobs = [(args.format, payload[o:o + step], total + o // size)
                    for o in range(0, len(payload), step)]
            failures = [f for f in pool.map(check_records, jobs) if f]
            if failures:
                raise SystemExit('%s: %s' % (path, failures[0]))
            provenance.append(info)
            records = len(payload) // size
            pending += payload
            pending_sources.append({'source': source_index, 'first': 0, 'count': records})
            total += records
            print('source %s: %d records decoded' % (info['file'], records), flush=True)
            flush(False)
        flush(True)

    manifest = {
        'schema': MANIFEST_SCHEMA,
        'set': args.set,
        'format': args.format,
        'variant': fmt['variant'],
        'record_bytes': size,
        'records_per_chunk': args.records_per_chunk,
        'total_records': total,
        'sources': provenance,
        'chunks': chunks,
    }
    with open(os.path.join(args.out, 'manifest.json'), 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
        handle.write('\n')
    print('wrote %d chunks, %d records, manifest %s' % (
        len(chunks), total, os.path.join(args.out, 'manifest.json')))


if __name__ == '__main__':
    main()
