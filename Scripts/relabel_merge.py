#!/usr/bin/env python3
"""Verify RELABEL output chunks against their input set and merge them.

Example (Alice T349 at depth 12, workload #NNN):

    python Scripts/relabel_merge.py \\
        --inputs D:/alice-train/relabel/t349-train-v1 \\
        --outputs D:/alice-train/relabel/t349-train-v1-d12-out \\
        --fetch https://belzedar.duckdns.org --test NNN \\
        --records-out D:/alice-train/relabel/t349-train-v1-d12.records \\
        --alice-legacy-psv D:/alice-train/relabel/psv-d12 \\
        --alice-tools D:/alice-train/tools

Every output chunk must hold exactly the records of its input chunk, byte for
byte except the score and the record checksum, and its position-key SHA-256
must equal the one in the input manifest; any difference stops the merge.
``--fetch`` downloads missing chunk_<i>.bz2 files from the OpenBench API.

``--records-out`` writes all relabelled records, in input order, as one bare
record file. ``--alice-legacy-psv`` additionally converts them to the 72-byte
PackedSfenValue files the legacy Alice trainer reads, one file per source
chunk, using ``encode_block`` from D:/alice-train/tools/t349_to_legacy.py and
the same rule as that converter: records with |score| >= 10000 (mates) are
dropped.
"""

import argparse
import bz2
import hashlib
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Client'))

import relabel  # noqa: E402

SCORE_LIMIT = 10000


def fetch_chunk(server, test_id, idx, path):
    import requests

    url = '%s/api/datagen/%d/%d/' % (server.rstrip('/'), test_id, idx)
    with requests.get(url, stream=True, timeout=(60, 600)) as response:
        if response.status_code != 200 or 'json' in response.headers.get('Content-Type', ''):
            raise SystemExit('chunk %d: download failed: %s' % (idx, response.text[:200]))
        tmp = path + '.tmp'
        with open(tmp, 'wb') as handle:
            for block in response.iter_content(1 << 20):
                handle.write(block)
    os.replace(tmp, path)


def load_chunk(path, records, size):
    with open(path, 'rb') as handle:
        data = bz2.decompress(handle.read())
    if len(data) != records * size:
        raise SystemExit('%s: holds %d bytes, expected %d records' % (path, len(data), records))
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--inputs', required=True, help='directory written by relabel_split.py')
    parser.add_argument('--outputs', required=True, help='directory of relabelled chunk_<i>.bz2')
    parser.add_argument('--fetch', help='OpenBench server URL to download missing outputs from')
    parser.add_argument('--test', type=int, help='RELABEL workload id, with --fetch')
    parser.add_argument('--records-out', help='write all relabelled records to this file')
    parser.add_argument('--alice-legacy-psv', help='write legacy Alice PackedSfenValue files here')
    parser.add_argument('--alice-tools', default='D:/alice-train/tools')
    parser.add_argument('--psv-prefix', default='relabel')
    args = parser.parse_args(argv)
    if args.fetch and not args.test:
        parser.error('--fetch needs --test')

    with open(os.path.join(args.inputs, 'manifest.json'), encoding='utf-8') as handle:
        manifest = json.load(handle)
    fmt = relabel.record_format(manifest['format'])
    size = fmt['record_bytes']
    os.makedirs(args.outputs, exist_ok=True)

    encode_block = None
    if args.alice_legacy_psv:
        if manifest['format'] != 'alice-v2-record-v1':
            parser.error('--alice-legacy-psv needs alice-v2-record-v1 records')
        import numpy as np
        sys.path.insert(0, args.alice_tools)
        from t349_to_legacy import DT, encode_block  # noqa: F401
        os.makedirs(args.alice_legacy_psv, exist_ok=True)

    records_out = open(args.records_out + '.tmp', 'wb') if args.records_out else None
    by_source = {}
    summary = {'chunks': 0, 'records': 0, 'changed': 0, 'abs_delta_sum': 0,
               'mates_in': 0, 'mates_out': 0}

    for chunk in manifest['chunks']:
        idx = chunk['idx']
        out_path = os.path.join(args.outputs, 'chunk_%d.bz2' % idx)
        if not os.path.exists(out_path):
            if not args.fetch:
                raise SystemExit('chunk %d: %s is missing' % (idx, out_path))
            fetch_chunk(args.fetch, args.test, idx, out_path)

        source = load_chunk(os.path.join(args.inputs, chunk['file']), chunk['records'], size)
        if hashlib.sha256(source).hexdigest() != chunk['raw_sha256']:
            raise SystemExit('chunk %d: input records do not match the manifest' % idx)
        output = load_chunk(out_path, chunk['records'], size)
        try:
            relabel.verify_relabel_output(source, output, fmt)
        except relabel.RelabelFormatError as error:
            raise SystemExit('chunk %d: %s' % (idx, error))
        if hashlib.sha256(relabel.key_projection(output, fmt)).hexdigest() != chunk['keys_sha256']:
            raise SystemExit('chunk %d: output keys do not match the manifest' % idx)

        for base in range(0, len(output), size):
            old = struct.unpack_from('<h', source, base + fmt['score_offset'])[0]
            new = struct.unpack_from('<h', output, base + fmt['score_offset'])[0]
            summary['changed'] += old != new
            summary['abs_delta_sum'] += abs(old - new)
            summary['mates_in'] += abs(old) >= SCORE_LIMIT
            summary['mates_out'] += abs(new) >= SCORE_LIMIT
        summary['chunks'] += 1
        summary['records'] += chunk['records']
        if records_out:
            records_out.write(output)
        if encode_block is not None:
            offset = 0
            for part in chunk.get('sources') or [{'source': 0, 'first': 0, 'count': chunk['records']}]:
                piece = output[offset * size:(offset + part['count']) * size]
                by_source.setdefault(part['source'], []).append(piece)
                offset += part['count']
        print('chunk %d verified (%d records)' % (idx, chunk['records']), flush=True)

    if records_out:
        records_out.close()
        os.replace(args.records_out + '.tmp', args.records_out)

    if encode_block is not None:
        import numpy as np
        sources = manifest.get('sources') or [{}]
        for source_index, pieces in sorted(by_source.items()):
            a = np.frombuffer(b''.join(pieces), dtype=DT)
            keep = np.abs(a['score'].astype(np.int32)) < SCORE_LIMIT
            a = a[keep]
            enc = np.concatenate([encode_block(a[i:i + 100000]) for i in range(0, len(a), 100000)])
            label = sources[source_index].get('chunk_index', source_index)
            path = os.path.join(args.alice_legacy_psv, '%s_chunk_%s.bin' % (args.psv_prefix, label))
            enc.tofile(path + '.tmp')
            os.replace(path + '.tmp', path)
            info = {'source': sources[source_index].get('file'), 'kept': int(keep.sum()),
                    'dropped_mate': int((~keep).sum()),
                    'sha256': hashlib.sha256(enc.tobytes()).hexdigest()}
            with open(path[:-4] + '.json', 'w') as handle:
                json.dump(info, handle)
            print('psv %s: %s' % (path, json.dumps(info)), flush=True)

    summary['mean_abs_delta'] = summary['abs_delta_sum'] / max(1, summary['records'])
    print(json.dumps(summary))
    if summary['chunks'] != len(manifest['chunks']) or summary['records'] != manifest['total_records']:
        raise SystemExit('merge did not cover the whole input set')


if __name__ == '__main__':
    main()
