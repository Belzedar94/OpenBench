"""RELABEL: rescore a fixed set of training positions at a fixed search depth.

A RELABEL workload is a DATAGEN sub-mode. The server leases one input chunk of
fixed-size position records; the worker searches every record with the
engine over plain UCI and returns the same records with only the score (and
the derived checksum) replaced. The server re-checks that every other byte of
every record is unchanged before it accepts the chunk.

This module has no Django or OpenBench imports so the worker, the operator
tools and the unit tests can share one codec. ``OpenBench/relabel.py`` holds
the server's copy of the immutability rule and a test pins the two tables
together.
"""

import struct
import subprocess
import threading
import time
import zlib


RELABEL_PROTOCOL = 1

# Internal Stockfish values (types.h). Records store the search value in the
# engine's own internal units, from the side to move, exactly as the datagen
# training search writes them.
VALUE_MATE = 32000
MAX_PLY = 246
VALUE_MATE_IN_MAX_PLY = VALUE_MATE - MAX_PLY
VALUE_TB = VALUE_MATE_IN_MAX_PLY - 1
TB_CP = 20000


class RelabelFormatError(ValueError):
    """A record does not decode to a position the format allows."""


class RelabelEngineError(RuntimeError):
    """The engine died, refused a position, or never reported a score."""


class RelabelStopped(Exception):
    """The lease was revoked or the worker was asked to exit."""


# Per-format layout. ``mutable`` lists the only byte ranges a relabel may
# change inside a record; everything else is the position key and must come
# back byte-identical. Keep this in sync with OpenBench/relabel.py.
FORMATS = {
    'alice-v2-record-v1': {
        'record_bytes': 84,
        'variant': 'alice',
        'score_offset': 68,
        'crc_offset': 80,
        'mutable': ((68, 70), (80, 84)),
        'move_replaced': False,
    },
}

ALICE_STRUCT = struct.Struct('<64sIhHbBBBHHI')
ALICE_SYMBOLS = {
    1: 'P', 2: 'N', 3: 'B', 4: 'R', 5: 'Q', 6: 'K',
    9: 'p', 10: 'n', 11: 'b', 12: 'r', 13: 'q', 14: 'k',
}
ALICE_MATERIAL = {1: 1, 2: 3, 3: 3, 4: 5, 5: 9, 6: 0}


def record_format(name):
    try:
        return FORMATS[name]
    except KeyError:
        raise RelabelFormatError('Unsupported RELABEL format %r' % (name,)) from None


def split_records(data, fmt):
    size = fmt['record_bytes']
    if len(data) % size:
        raise RelabelFormatError(
            'Chunk length %d is not a multiple of the %d-byte record'
            % (len(data), size)
        )
    return len(data) // size


def alice_decode(record):
    """Return (fen, material) for one ALICEV2_RECORD_V1 record.

    Mirrors the trainer's ``alice_v2_record.unpack_record`` validation of the
    position fields; the stored move is kept verbatim and not re-derived.
    """

    if len(record) != 84:
        raise RelabelFormatError('Alice record must be 84 bytes')
    (cells, _move, _score, _ply, result, side, castling, flags,
     halfmove, fullmove, crc) = ALICE_STRUCT.unpack(record)
    if zlib.crc32(record[:80]) & 0xFFFFFFFF != crc:
        raise RelabelFormatError('Alice record CRC32 mismatch')
    if side not in (0, 1) or castling & ~0x0F or flags != 1:
        raise RelabelFormatError('Alice record has invalid side/castling/flags')
    if result not in (-1, 0, 1):
        raise RelabelFormatError('Alice record has an invalid game result')
    if fullmove < 1:
        raise RelabelFormatError('Alice record has an invalid fullmove number')

    kings = {'K': 0, 'k': 0}
    pieces = 0
    material = 0
    ranks = []
    for rank in range(7, -1, -1):
        text = ''
        empty = 0
        for file_index in range(8):
            square = rank * 8 + file_index
            cell = cells[square]
            if cell == 0:
                empty += 1
                continue
            if cell & 0xE0:
                raise RelabelFormatError('Reserved cell bits at square %d' % square)
            code = cell & 0x0F
            symbol = ALICE_SYMBOLS.get(code)
            if symbol is None:
                raise RelabelFormatError('Unknown piece code %d' % code)
            if symbol in 'Pp' and rank in (0, 7):
                raise RelabelFormatError('Pawn on a back rank')
            if symbol in kings:
                kings[symbol] += 1
            pieces += 1
            material += ALICE_MATERIAL[code & 7]
            if empty:
                text += str(empty)
                empty = 0
            text += ('|' if cell & 0x10 else '') + symbol
        if empty:
            text += str(empty)
        ranks.append(text)
    if kings != {'K': 1, 'k': 1} or not 2 <= pieces <= 32:
        raise RelabelFormatError('Alice record needs one king each and 2..32 pieces')

    rights = ''.join(
        token for bit, token in ((1, 'K'), (2, 'Q'), (4, 'k'), (8, 'q'))
        if castling & bit
    )
    fen = '%s %s %s - %d %d' % (
        '/'.join(ranks), 'b' if side else 'w', rights or '-', halfmove, fullmove,
    )
    return fen, material


DECODERS = {
    'alice-v2-record-v1': alice_decode,
}


def rewrite_score(record, fmt, value):
    """Return ``record`` with its score replaced and its checksum recomputed."""

    if not -VALUE_MATE <= value <= VALUE_MATE:
        raise RelabelFormatError('Score %d is outside the mate range' % value)
    offset = fmt['score_offset']
    out = bytearray(record)
    struct.pack_into('<h', out, offset, value)
    crc_offset = fmt.get('crc_offset')
    if crc_offset is not None:
        struct.pack_into('<I', out, crc_offset, zlib.crc32(out[:crc_offset]) & 0xFFFFFFFF)
    return bytes(out)


def record_score(record, fmt):
    return struct.unpack_from('<h', record, fmt['score_offset'])[0]


def key_projection(data, fmt):
    """Bytes of every record with the mutable ranges removed: the position key."""

    size = fmt['record_bytes']
    keep = []
    start = 0
    for lo, hi in sorted(fmt['mutable']):
        if lo > start:
            keep.append((start, lo))
        start = hi
    if start < size:
        keep.append((start, size))
    view = memoryview(data)
    parts = []
    for base in range(0, len(data), size):
        for lo, hi in keep:
            parts.append(view[base + lo:base + hi])
    return b''.join(parts)


def verify_relabel_output(source, output, fmt):
    """Raise RelabelFormatError unless ``output`` is a pure relabel of ``source``."""

    count = split_records(source, fmt)
    if len(output) != len(source):
        raise RelabelFormatError(
            'Record count mismatch: %d records in, %d bytes out'
            % (count, len(output))
        )
    if key_projection(source, fmt) != key_projection(output, fmt):
        raise RelabelFormatError('Position keys differ from the input chunk')
    crc_offset = fmt.get('crc_offset')
    size = fmt['record_bytes']
    for base in range(0, len(output), size):
        score = struct.unpack_from('<h', output, base + fmt['score_offset'])[0]
        if not -VALUE_MATE <= score <= VALUE_MATE:
            raise RelabelFormatError('Score outside the mate range at record %d' % (base // size))
        if crc_offset is not None:
            crc = struct.unpack_from('<I', output, base + crc_offset)[0]
            if zlib.crc32(output[base:base + crc_offset]) & 0xFFFFFFFF != crc:
                raise RelabelFormatError('Checksum mismatch at record %d' % (base // size))
    return count


# Stockfish's UCI cp output is round(100 * v / a(material)) for ordinary
# scores (uci.cpp: win_rate_params / to_cp). Invert it so relabelled scores
# stay in the internal units of the original records. The rounding error is at
# most a / 200, under two internal units.
WDL_AS = (-72.32565836, 185.93832038, -144.58862193, 416.44950446)


def win_rate_a(material):
    m = min(max(material, 17), 78) / 58.0
    return ((WDL_AS[0] * m + WDL_AS[1]) * m + WDL_AS[2]) * m + WDL_AS[3]


def uci_score_to_internal(kind, number, material):
    if kind == 'mate':
        if number > 0:
            value = VALUE_MATE - (2 * number - 1)
        else:
            value = -VALUE_MATE - 2 * number
    elif kind == 'cp':
        if abs(number) > TB_CP - MAX_PLY - 1 and abs(number) <= TB_CP:
            plies = TB_CP - abs(number)
            value = VALUE_TB - plies if number > 0 else -(VALUE_TB - plies)
        else:
            value = int(round(number * win_rate_a(material) / 100.0))
            value = max(-(VALUE_TB - MAX_PLY - 1), min(VALUE_TB - MAX_PLY - 1, value))
    else:
        raise RelabelEngineError('Unknown UCI score kind %r' % (kind,))
    return max(-VALUE_MATE, min(VALUE_MATE, value))


def parse_info_score(line):
    """Return (kind, number, bound, multipv) from a UCI info line, or None."""

    tokens = line.split()
    if not tokens or tokens[0] != 'info' or 'score' not in tokens:
        return None
    multipv = 1
    if 'multipv' in tokens:
        try:
            multipv = int(tokens[tokens.index('multipv') + 1])
        except (IndexError, ValueError):
            return None
    at = tokens.index('score')
    try:
        kind = tokens[at + 1]
        number = int(tokens[at + 2])
    except (IndexError, ValueError):
        return None
    bound = None
    if at + 3 < len(tokens) and tokens[at + 3] in ('lowerbound', 'upperbound'):
        bound = tokens[at + 3]
    return kind, number, bound, multipv


class UciSearcher:
    """One engine process searching positions one at a time over UCI."""

    def __init__(self, engine_path, hash_mb=16, evalfile=None, options=None,
                 popen=subprocess.Popen, creationflags=0):
        self.process = popen(
            [engine_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=creationflags,
        )
        try:
            self.send('uci')
            self.wait_for('uciok')
            settings = [('Threads', '1'), ('Hash', str(hash_mb))]
            if evalfile:
                settings.append(('EvalFile', evalfile))
            for name, value in (options or []):
                if name.lower() not in ('threads', 'hash', 'evalfile'):
                    settings.append((name, value))
            for name, value in settings:
                self.send('setoption name %s value %s' % (name, value))
            self.send('isready')
            self.wait_for('readyok')
        except BaseException:
            self.close()
            raise

    def send(self, text):
        try:
            self.process.stdin.write(text + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise RelabelEngineError('Engine input closed: %s' % error) from error

    def readline(self):
        line = self.process.stdout.readline()
        if not line:
            raise RelabelEngineError(
                'Engine exited (code %s)' % (self.process.poll(),)
            )
        return line.rstrip('\r\n')

    def wait_for(self, prefix):
        while True:
            line = self.readline()
            if line.startswith(prefix):
                return line

    def new_game(self):
        self.send('ucinewgame')
        self.send('isready')
        self.wait_for('readyok')

    def search(self, fen, depth=0, nodes=0):
        """Return (kind, number, bestmove) of the last exact score for ``fen``."""

        self.send('position fen ' + fen)
        limit = ('depth %d' % depth) if depth else ('nodes %d' % nodes)
        self.send('go ' + limit)
        exact = None
        last = None
        while True:
            line = self.readline()
            if line.startswith('bestmove'):
                parts = line.split()
                bestmove = parts[1] if len(parts) > 1 else '(none)'
                break
            parsed = parse_info_score(line)
            if parsed is None or parsed[3] != 1:
                continue
            last = parsed
            if parsed[2] is None:
                exact = parsed
        chosen = exact or last
        if chosen is None:
            raise RelabelEngineError('Engine reported no score for: ' + fen)
        return chosen[0], chosen[1], bestmove

    def close(self):
        try:
            if self.process.poll() is None:
                self.send('quit')
                self.process.wait(timeout=5)
        except Exception:
            pass
        if self.process.poll() is None:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (self.process.stdin, self.process.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


class RelabelProgress:

    def __init__(self, total, interval=60.0, printer=print, clock=time.monotonic):
        self.total = total
        self.done = 0
        self.interval = interval
        self.printer = printer
        self.clock = clock
        self.started = clock()
        self.last = self.started
        self.lock = threading.Lock()

    def add(self, count=1):
        with self.lock:
            self.done += count
            now = self.clock()
            if now - self.last >= self.interval or self.done == self.total:
                self.last = now
                self.printer(self.line(now))

    def rate(self, now=None):
        elapsed = (now or self.clock()) - self.started
        return self.done / elapsed if elapsed > 0 else 0.0

    def line(self, now=None):
        rate = self.rate(now)
        remaining = (self.total - self.done) / rate if rate > 0 else 0
        return 'RELABEL %d/%d positions (%.1f%%), %.1f pos/s, ~%d min left' % (
            self.done, self.total, 100.0 * self.done / max(1, self.total),
            rate, remaining // 60,
        )


def relabel_slice(searcher, data, fmt, start, stop, depth, nodes, out, progress,
                  should_stop):
    decode = DECODERS[fmt['name']]
    size = fmt['record_bytes']
    for index in range(start, stop):
        if should_stop():
            raise RelabelStopped()
        record = data[index * size:(index + 1) * size]
        fen, material = decode(record)
        kind, number, _bestmove = searcher.search(fen, depth=depth, nodes=nodes)
        value = uci_score_to_internal(kind, number, material)
        out[index * size:(index + 1) * size] = rewrite_score(record, fmt, value)
        progress.add()


def relabel_chunk(data, format_name, engine_path, depth=0, nodes=0, engines=1,
                  hash_mb=16, evalfile=None, options=None, should_stop=None,
                  progress=None, searcher_factory=None):
    """Relabel every record in ``data`` and return the new bytes.

    The chunk is cut into ``engines`` contiguous slices, one engine process
    each, so consecutive positions of a game share one transposition table.
    """

    if bool(depth) == bool(nodes):
        raise RelabelFormatError('Exactly one of depth or nodes must be set')
    fmt = dict(record_format(format_name), name=format_name)
    count = split_records(data, fmt)
    # Decode everything first: a malformed or wrong-variant record fails the
    # chunk before any engine time is spent on it.
    for index in range(count):
        DECODERS[format_name](data[index * fmt['record_bytes']:(index + 1) * fmt['record_bytes']])

    should_stop = should_stop or (lambda: False)
    progress = progress or RelabelProgress(count)
    engines = max(1, min(engines, count))
    factory = searcher_factory or (
        lambda: UciSearcher(engine_path, hash_mb=hash_mb, evalfile=evalfile, options=options)
    )
    out = bytearray(len(data))
    bounds = [(count * k // engines, count * (k + 1) // engines) for k in range(engines)]
    errors = []
    abort = threading.Event()

    def stop_now():
        return abort.is_set() or should_stop()

    def run(start, stop):
        searcher = None
        try:
            searcher = factory()
            relabel_slice(searcher, data, fmt, start, stop, depth, nodes, out,
                          progress, stop_now)
        except BaseException as error:  # noqa: BLE001 - reported to caller
            errors.append(error)
            abort.set()
        finally:
            if searcher is not None:
                searcher.close()

    threads = [threading.Thread(target=run, args=b, daemon=True) for b in bounds]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if errors:
        stopped = [e for e in errors if isinstance(e, RelabelStopped)]
        real = [e for e in errors if not isinstance(e, RelabelStopped)]
        raise (real or stopped)[0]
    verify_relabel_output(data, bytes(out), fmt)
    return bytes(out)
