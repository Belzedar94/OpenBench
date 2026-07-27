"""SOLVE certificates: parsing, verification and closure.

WHY A VERIFIER AT ALL
---------------------
A volunteer's ``PROVED`` is not a proof.  What makes a distributed solver
sound is that the exact fact enters the database only after the SERVER has
replayed the whole strategy tree — not a sample of it.  Sampling gives
confidence; a closure needs a proof.

SEPARATE LINEAGE, ON PURPOSE
----------------------------
The solver walks Atomic-Stockfish's own move generator.  This verifier walks
``pyffish``.  Two implementations, two builds, two authors: a movegen bug that
would let the solver invent a mate has to exist identically in both to get
past here.  The losing-chess weak solve named shared move generation between
solver and verifier as its remaining common-mode weakness; this is the cheap
half of avoiding that, and it costs nothing because AtomicDB already runs
pyffish for every exact decision it makes.

THE FORMAT
----------
Plain text, one node per line, DFS preorder.  The structure is implicit: no
indices, no back-references, and it streams.

    # atomicdb-proof/1
    ruleset atomic-fide-claim-v1
    goal WHITE_WIN
    root <fen>
    nodes <count>
    ---
    T <reason>              terminal node that achieves the goal
    O <move>                attacker to move plays <move>; its subtree follows
    A <n> <m1> ... <mn>     defender to move has exactly these n legal moves;
                            their n subtrees follow, in this order

WHAT IS CHECKED
---------------
Every cited move is legal.  Every OR node has its one witness.  Every AND node
covers EXACTLY the legal set this verifier generates — not a subset, not a
superset, and duplicates are rejected.  Every leaf is genuinely terminal and
genuinely achieves the goal.  No position repeats on its branch, because a
repetition the defender can hold is a draw.  No branch reaches the fifty-move
horizon before the goal.  And the whole thing is bounded: bytes, nodes, depth
and fan-out, because a public certificate endpoint is an adversarial input
boundary before it is anything else.
"""

import gzip

import pyffish as pf

from . import logic

CERTIFICATE_FORMAT = 'atomicdb-proof/1'
SOLVER_GOALS = ('WHITE_WIN', 'BLACK_WIN')

# Anti-bomb ceilings.  They are deliberately hard: a certificate that does not
# fit is rejected, never truncated, because a truncated proof tree is a
# different (and false) statement.
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024 * 1024
MAX_NODES = 2_000_000
MAX_DEPTH = 512
MAX_FANOUT = 256
MAX_HEADER_LINES = 32


class CertificateError(ValueError):
    """A certificate that is malformed, over the limits, or simply false."""


def compress(text):
    """gzip bytes for storage.  Certificates are text and compress hard."""
    if isinstance(text, str):
        text = text.encode('utf-8')
    return gzip.compress(text, compresslevel=6)


def decompress(blob, max_bytes=MAX_TEXT_BYTES):
    """Bounded gunzip.  Never trust a declared size; read with a ceiling."""
    if blob is None:
        raise CertificateError('certificate is empty')
    blob = bytes(blob)
    if len(blob) > MAX_COMPRESSED_BYTES:
        raise CertificateError('certificate exceeds the compressed size limit')
    try:
        with gzip.GzipFile(fileobj=_BytesReader(blob)) as stream:
            data = stream.read(max_bytes + 1)
    except (OSError, EOFError) as error:
        raise CertificateError(f'certificate is not valid gzip: {error}')
    if len(data) > max_bytes:
        raise CertificateError('certificate exceeds the uncompressed limit')
    try:
        return data.decode('utf-8')
    except UnicodeError as error:
        raise CertificateError(f'certificate is not valid UTF-8: {error}')


class _BytesReader:
    """Minimal read-only file object; avoids importing io for one use."""

    def __init__(self, data):
        self._data = data
        self._at = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._data[self._at:]
            self._at = len(self._data)
            return chunk
        chunk = self._data[self._at:self._at + size]
        self._at += len(chunk)
        return chunk

    def seek(self, offset, whence=0):
        if whence == 0:
            self._at = offset
        elif whence == 1:
            self._at += offset
        else:
            self._at = len(self._data) + offset
        return self._at

    def tell(self):
        return self._at


def parse_header(text):
    """Header fields and the index at which the node stream starts."""
    lines = text.split('\n')
    if not lines or lines[0].strip() != '# ' + CERTIFICATE_FORMAT:
        raise CertificateError('unknown certificate format')
    header = {}
    for index in range(1, min(len(lines), MAX_HEADER_LINES)):
        line = lines[index].strip()
        if line == '---':
            return header, index + 1
        if not line:
            continue
        name, _, value = line.partition(' ')
        if not name or not value:
            raise CertificateError(f'malformed header line: {line!r}')
        header[name] = value.strip()
    raise CertificateError('certificate header is not terminated')


def _fifty_move_counter(fen):
    parts = fen.split()
    try:
        return int(parts[4])
    except (IndexError, ValueError):
        return 0


def _terminal_status(fen):
    return logic.terminal_status(fen)


def verify_certificate(text, root_fen=None, goal=None, max_nodes=MAX_NODES,
                       max_depth=MAX_DEPTH, max_fanout=MAX_FANOUT):
    """Replay a certificate in full. Returns its report or raises.

    ``root_fen`` and ``goal``, when given, must match the header: a
    certificate that proves something true about a DIFFERENT position is still
    not a proof about this one, and that is the shape a malicious submission
    takes.
    """
    header, start = parse_header(text)
    if header.get('ruleset') != logic.RULESET_ID:
        raise CertificateError(
            f'certificate ruleset {header.get("ruleset")!r} is not '
            f'{logic.RULESET_ID!r}')
    cert_goal = header.get('goal')
    if cert_goal not in SOLVER_GOALS:
        raise CertificateError(f'unknown goal {cert_goal!r}')
    if goal is not None and cert_goal != goal:
        raise CertificateError('certificate proves a different goal')
    cert_root = header.get('root')
    if not cert_root:
        raise CertificateError('certificate has no root position')
    if root_fen is not None and \
            logic.canonical_fen(cert_root) != logic.canonical_fen(root_fen):
        raise CertificateError('certificate proves a different position')

    winner_status = cert_goal
    attacker_white = cert_goal == 'WHITE_WIN'
    lines = text.split('\n')[start:]
    cursor = {'at': 0}

    def next_line():
        while cursor['at'] < len(lines):
            line = lines[cursor['at']].strip()
            cursor['at'] += 1
            if line:
                return line
        raise CertificateError('certificate ended before its tree did')

    # Explicit stack: a 512-deep recursion would need the interpreter's limit
    # raised, and raising it for untrusted input is exactly backwards.
    stack = [(cert_root, frozenset({logic.canonical_fen(cert_root)}), 0, 0)]
    nodes = 0
    deepest = 0
    worst_run = 0

    while stack:
        fen, path, run, depth = stack.pop()
        deepest = max(deepest, depth)
        if depth > max_depth:
            raise CertificateError('certificate exceeds the depth limit')
        nodes += 1
        if nodes > max_nodes:
            raise CertificateError('certificate exceeds the node limit')

        line = next_line()
        kind, _, rest = line.partition(' ')

        if kind == 'T':
            terminal = _terminal_status(fen)
            if terminal is None:
                raise CertificateError(
                    f'claimed terminal node is not terminal: {fen}')
            if terminal[0] != winner_status:
                raise CertificateError(
                    f'terminal node does not achieve {winner_status}: {fen}')
            worst_run = max(worst_run, run)
            continue

        # An interior node must actually be interior, and must not already be
        # adjudicated away under the frozen ruleset.
        if _terminal_status(fen) is not None:
            raise CertificateError(
                f'interior node is already terminal: {fen}')
        if _fifty_move_counter(fen) >= logic.FIFTY_MOVE_PLIES:
            raise CertificateError(
                'branch reaches the fifty-move horizon before the goal')

        stm_white = fen.split()[1] == 'w'
        attacker_to_move = stm_white == attacker_white
        legal = list(logic.legal_moves(fen))

        if kind == 'O':
            if not attacker_to_move:
                raise CertificateError(
                    'OR node claimed where the defender is to move')
            move = rest.strip()
            if move not in legal:
                raise CertificateError(f'illegal witness move {move!r}')
            child_fen, child_run = _advance(fen, move, run)
            if logic.canonical_fen(child_fen) in path:
                raise CertificateError(
                    'witness repeats a position on its own branch')
            stack.append((child_fen,
                          path | {logic.canonical_fen(child_fen)},
                          child_run, depth + 1))
            continue

        if kind == 'A':
            if attacker_to_move:
                raise CertificateError(
                    'AND node claimed where the attacker is to move')
            parts = rest.split()
            if not parts:
                raise CertificateError('AND node has no move count')
            try:
                declared = int(parts[0])
            except ValueError:
                raise CertificateError('AND node has a malformed move count')
            moves = parts[1:]
            if declared != len(moves):
                raise CertificateError(
                    'AND node move count does not match its list')
            if declared > max_fanout:
                raise CertificateError('AND node exceeds the fan-out limit')
            if len(set(moves)) != len(moves):
                raise CertificateError('AND node repeats a move')
            if set(moves) != set(legal):
                missing = sorted(set(legal) - set(moves))
                extra = sorted(set(moves) - set(legal))
                raise CertificateError(
                    'AND node does not cover exactly the legal replies '
                    f'(missing={missing}, extra={extra})')
            children = []
            for move in moves:
                child_fen, child_run = _advance(fen, move, run)
                if logic.canonical_fen(child_fen) in path:
                    raise CertificateError(
                        'defender can repeat a position on this branch')
                children.append((child_fen,
                                 path | {logic.canonical_fen(child_fen)},
                                 child_run, depth + 1))
            # LIFO: push reversed so they pop back in emission order.
            stack.extend(reversed(children))
            continue

        raise CertificateError(f'unknown node kind {kind!r}')

    if cursor['at'] < len(lines) and any(
            line.strip() for line in lines[cursor['at']:]):
        raise CertificateError('certificate has trailing content')

    declared_nodes = header.get('nodes')
    if declared_nodes is not None:
        try:
            declared = int(declared_nodes)
        except ValueError:
            raise CertificateError('certificate node count is malformed')
        if declared != nodes:
            raise CertificateError(
                'certificate node count does not match its tree')

    return {
        'goal': cert_goal,
        'root': cert_root,
        'nodes': nodes,
        'depth': deepest,
        'worst_reversible_run': worst_run,
        'clock_slack': logic.slack_from_run(worst_run),
    }


def _advance(fen, uci, run):
    """Child FEN WITH counters, and the reversible run that reaches it.

    The counters matter here and only here: everywhere else AtomicDB works
    with canonical FENs whose counters are zero, but a certificate is a claim
    about a branch, and a branch has a clock.
    """
    child = pf.get_fen(logic.VARIANT, fen, [uci])
    zeroed = _fifty_move_counter(child) == 0
    return child, 0 if zeroed else run + 1
