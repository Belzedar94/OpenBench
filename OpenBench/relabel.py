"""RELABEL: a DATAGEN sub-mode that rescores an existing set of positions.

The operator installs an input set under ``Media/relabel/inputs/<set>/``: one
bzip2 file of fixed-size records per chunk plus ``manifest.json``. Creating a
RELABEL workload freezes that manifest into the chunk map; workers lease one
input chunk at a time, search every record, and upload the same records with
only the score replaced. Before a chunk is accepted the server decompresses
both sides and checks record count, the input's identity, and that every byte
outside the format's mutable ranges is unchanged. Accepted output is stored as
``Media/relabel/<test>/out/chunk_<idx>.bz2``.

Keep ``RELABEL_FORMATS`` in sync with ``Client/relabel.py``.
"""

import bz2
import hashlib
import json
import os
import re
import struct
import zlib


RELABEL_PROTOCOL = 1
RELABEL_MANIFEST_SCHEMA = 'openbench-relabel-input-v1'
RELABEL_INPUT_ROOT = 'relabel/inputs'
RELABEL_SET_PATTERN = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
VALUE_MATE = 32000

# Searches stop at a depth that is still useful for labels; nodes is the
# alternative limit. Both bounds only reject typos.
MAX_RELABEL_DEPTH = 64
MAX_RELABEL_NODES = 10 ** 10

RELABEL_FORMATS = {
    'alice-v2-record-v1': {
        'record_bytes': 84,
        'variant': 'alice',
        'score_offset': 68,
        'crc_offset': 80,
        'mutable': ((68, 70), (80, 84)),
        'move_replaced': False,
    },
}

# Read compressed input in bounded blocks and never inflate past the size the
# manifest promised: a hostile upload cannot become a decompression bomb.
BLOCK_RECORDS = 16384


class RelabelError(ValueError):
    pass


def is_relabel(test):
    return (
        getattr(test, 'test_mode', None) == 'DATAGEN'
        and bool(getattr(test, 'datagen_command', ''))
        and bool(getattr(test, 'relabel_format', ''))
    )


def relabel_command(set_name, format_name, depth, nodes):
    """Human-readable, placeholder-free command recorded on the workload."""

    return 'RELABEL set=%s format=%s depth=%d nodes=%d' % (
        set_name, format_name, depth, nodes,
    )


def input_directory(media_root, set_name):
    if not isinstance(set_name, str) or not RELABEL_SET_PATTERN.fullmatch(set_name):
        raise RelabelError('RELABEL input set must be a lowercase slug')
    return os.path.join(media_root, RELABEL_INPUT_ROOT, set_name)


def output_filename(test_id, idx):
    return 'relabel/%d/out/chunk_%d.bz2' % (test_id, idx)


def input_filename(set_name, chunk_input):
    return '%s/%s/%s' % (RELABEL_INPUT_ROOT, set_name, chunk_input['file'])


def _sha256_hex(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def manifest_sha256(manifest):
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def validate_manifest(manifest, set_name):
    """Return the manifest if it is a complete, self-consistent input set."""

    if not isinstance(manifest, dict):
        raise RelabelError('RELABEL manifest must be a JSON object')
    if manifest.get('schema') != RELABEL_MANIFEST_SCHEMA:
        raise RelabelError('RELABEL manifest schema must be %s' % RELABEL_MANIFEST_SCHEMA)
    if manifest.get('set') != set_name:
        raise RelabelError('RELABEL manifest names a different set')
    fmt = RELABEL_FORMATS.get(manifest.get('format'))
    if fmt is None:
        raise RelabelError('RELABEL manifest format is not supported')
    if manifest.get('variant') != fmt['variant']:
        raise RelabelError('RELABEL manifest variant does not match its format')
    if manifest.get('record_bytes') != fmt['record_bytes']:
        raise RelabelError('RELABEL manifest record size does not match its format')

    per_chunk = manifest.get('records_per_chunk')
    total = manifest.get('total_records')
    chunks = manifest.get('chunks')
    if type(per_chunk) is not int or per_chunk <= 0:
        raise RelabelError('RELABEL records_per_chunk must be a positive integer')
    if type(total) is not int or total <= 0:
        raise RelabelError('RELABEL total_records must be a positive integer')
    if not isinstance(chunks, list) or not chunks:
        raise RelabelError('RELABEL manifest has no chunks')
    if len(chunks) != (total + per_chunk - 1) // per_chunk:
        raise RelabelError('RELABEL chunk count does not match the record totals')

    seen = 0
    for idx, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or chunk.get('idx') != idx:
            raise RelabelError('RELABEL chunks must be numbered 0..n-1 in order')
        expected = min(per_chunk, total - idx * per_chunk)
        if chunk.get('records') != expected:
            raise RelabelError('RELABEL chunk %d must hold %d records' % (idx, expected))
        if chunk.get('file') != 'chunk_%d.bz2' % idx:
            raise RelabelError('RELABEL chunk %d must be stored as chunk_%d.bz2' % (idx, idx))
        for key in ('sha256', 'raw_sha256', 'keys_sha256'):
            if not _sha256_hex(chunk.get(key)):
                raise RelabelError('RELABEL chunk %d has a malformed %s' % (idx, key))
        if type(chunk.get('bytes')) is not int or chunk['bytes'] <= 0:
            raise RelabelError('RELABEL chunk %d has a malformed byte count' % idx)
        seen += expected
    if seen != total:
        raise RelabelError('RELABEL chunk records do not sum to total_records')
    return manifest


def load_manifest(media_root, set_name):
    directory = input_directory(media_root, set_name)
    path = os.path.join(directory, 'manifest.json')
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            manifest = json.load(handle)
    except FileNotFoundError:
        raise RelabelError('RELABEL input set %s has no manifest.json' % set_name) from None
    except (OSError, ValueError) as error:
        raise RelabelError('RELABEL manifest is unreadable: %s' % error) from None
    validate_manifest(manifest, set_name)
    for chunk in manifest['chunks']:
        chunk_path = os.path.join(directory, chunk['file'])
        try:
            size = os.path.getsize(chunk_path)
        except OSError:
            raise RelabelError('RELABEL input file %s is missing' % chunk['file']) from None
        if size != chunk['bytes']:
            raise RelabelError('RELABEL input file %s has the wrong size' % chunk['file'])
    return manifest


def chunk_input_record(manifest, chunk):
    """The frozen per-chunk input identity stored on DatagenChunk.relabel_input."""

    return {
        'file': chunk['file'],
        'records': chunk['records'],
        'sha256': chunk['sha256'],
        'bytes': chunk['bytes'],
        'raw_sha256': chunk['raw_sha256'],
        'keys_sha256': chunk['keys_sha256'],
    }


def _keep_ranges(fmt):
    keep = []
    start = 0
    for lo, hi in sorted(fmt['mutable']):
        if lo > start:
            keep.append((start, lo))
        start = hi
    if start < fmt['record_bytes']:
        keep.append((start, fmt['record_bytes']))
    return keep


def key_projection(data, fmt, keep=None):
    keep = keep or _keep_ranges(fmt)
    size = fmt['record_bytes']
    view = memoryview(data)
    return b''.join(
        view[base + lo:base + hi]
        for base in range(0, len(data), size)
        for lo, hi in keep
    )


class _BoundedReader:
    """Inflate a bzip2 file in record-aligned blocks with a hard size limit."""

    def __init__(self, path, limit, label):
        self.handle = open(path, 'rb')
        self.decompressor = bz2.BZ2Decompressor()
        self.limit = limit
        self.label = label
        self.produced = 0
        self.compressed = hashlib.sha256()
        self.compressed_bytes = 0
        self.buffer = b''

    def close(self):
        self.handle.close()

    def read(self, size):
        while len(self.buffer) < size:
            if self.decompressor.eof:
                if self.decompressor.unused_data or self.handle.read(1):
                    raise RelabelError('%s has trailing data after the bzip2 stream' % self.label)
                break
            if self.decompressor.needs_input:
                raw = self.handle.read(1 << 20)
                if not raw:
                    raise RelabelError('%s is a truncated bzip2 stream' % self.label)
                self.compressed.update(raw)
                self.compressed_bytes += len(raw)
            else:
                raw = b''
            piece = self.decompressor.decompress(raw, max_length=1 << 22)
            self.produced += len(piece)
            if self.produced > self.limit:
                raise RelabelError('%s inflates past the expected size' % self.label)
            self.buffer += piece
        block, self.buffer = self.buffer[:size], self.buffer[size:]
        return block


def verify_relabel_chunk(input_path, output_path, chunk_input, format_name):
    """Check an uploaded RELABEL chunk against its frozen input.

    Returns a small summary dict. Raises RelabelError with a reason on any
    mismatch: wrong input bytes, wrong record count, any changed key byte,
    scores outside the mate range, or a bad record checksum.
    """

    fmt = RELABEL_FORMATS.get(format_name)
    if fmt is None:
        raise RelabelError('RELABEL format is not supported')
    size = fmt['record_bytes']
    records = chunk_input['records']
    expected = records * size
    keep = _keep_ranges(fmt)
    score_offset = fmt['score_offset']
    crc_offset = fmt.get('crc_offset')

    source = _BoundedReader(input_path, expected, 'RELABEL input')
    output = _BoundedReader(output_path, expected, 'RELABEL output')
    raw_hash = hashlib.sha256()
    in_keys = hashlib.sha256()
    out_keys = hashlib.sha256()
    mates = 0
    total_score = 0
    try:
        remaining = records
        index = 0
        while remaining:
            count = min(BLOCK_RECORDS, remaining)
            a = source.read(count * size)
            b = output.read(count * size)
            if len(a) != count * size:
                raise RelabelError('RELABEL input holds fewer records than its manifest')
            if len(b) != count * size:
                raise RelabelError(
                    'RELABEL output holds fewer records than the input chunk (%d)' % records
                )
            raw_hash.update(a)
            projection_a = key_projection(a, fmt, keep)
            projection_b = key_projection(b, fmt, keep)
            in_keys.update(projection_a)
            out_keys.update(projection_b)
            if projection_a != projection_b:
                for k in range(count):
                    lo = k * size
                    if key_projection(a[lo:lo + size], fmt, keep) != key_projection(b[lo:lo + size], fmt, keep):
                        raise RelabelError(
                            'RELABEL output record %d does not match the input position'
                            % (index + k)
                        )
            for k in range(count):
                lo = k * size
                score = struct.unpack_from('<h', b, lo + score_offset)[0]
                if not -VALUE_MATE <= score <= VALUE_MATE:
                    raise RelabelError('RELABEL output record %d has an out-of-range score' % (index + k))
                if abs(score) >= 10000:
                    mates += 1
                total_score += score
                if crc_offset is not None:
                    crc = struct.unpack_from('<I', b, lo + crc_offset)[0]
                    if zlib.crc32(b[lo:lo + crc_offset]) & 0xFFFFFFFF != crc:
                        raise RelabelError('RELABEL output record %d has a bad checksum' % (index + k))
            remaining -= count
            index += count
        # Reading past the last record drains each stream to its end, so the
        # compressed hash below covers the whole file and trailing data fails.
        if source.read(1):
            raise RelabelError('RELABEL input holds more records than its manifest')
        if output.read(1):
            raise RelabelError('RELABEL output holds more records than the input chunk (%d)' % records)
    finally:
        source.close()
        output.close()

    if (source.compressed.hexdigest(), source.compressed_bytes) != (
        chunk_input['sha256'], chunk_input['bytes'],
    ):
        raise RelabelError('RELABEL input file no longer matches its frozen identity')
    if raw_hash.hexdigest() != chunk_input['raw_sha256']:
        raise RelabelError('RELABEL input records no longer match their frozen identity')
    if in_keys.hexdigest() != chunk_input['keys_sha256']:
        raise RelabelError('RELABEL input keys no longer match their frozen identity')
    if out_keys.hexdigest() != chunk_input['keys_sha256']:
        raise RelabelError('RELABEL output keys do not match the input chunk')
    return {
        'records': records,
        'keys_sha256': out_keys.hexdigest(),
        'mate_scores': mates,
        'mean_score': total_score / records if records else 0.0,
    }


def machine_supports(machine, format_name):
    info = getattr(machine, 'info', {})
    if not isinstance(info, dict):
        return False
    protocols = info.get('relabel_protocols')
    formats = info.get('relabel_formats')
    return (
        isinstance(protocols, list) and RELABEL_PROTOCOL in protocols
        and isinstance(formats, list) and format_name in formats
    )
