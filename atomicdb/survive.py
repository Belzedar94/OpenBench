"""SURVIVE50 certificates: the fortress half of the proof system.

WHY A SECOND CERTIFICATE FORMAT
-------------------------------
``solve.py`` verifies a WIN: a finite strategy tree, every branch ending in
mate.  It cannot express the other half of a weak solve.  When Black holds a
fortress there is no tree — the refutation is a strategy that outlasts the
fifty-move counter, and the counter is reset by every White capture and every
White pawn move.  ``solve.py`` would have to unroll a cyclic shuffle graph
into a tree, and it correctly refuses: it rejects any branch that repeats a
position, which is precisely what a fortress does on purpose.

So this is a sibling, not a replacement.  Same spirit — re-derive everything,
believe nothing, independent move generator — different proof object.

WHAT IS BEING CLAIMED
---------------------
For a base position ``s`` (no counters in the key), ``tau(s)`` is the smallest
halfmove clock from which Black has a strategy that avoids a White win.  The
certificate carries tau for every state it names, ONE selected reply at every
Black state, and EVERY legal move at every White state.  It is accepted when

    tau(root) <= the root's actual halfmove clock.

That refutes the boolean objective WHITE_WIN.  It is **not** a proof of
BLACK_WIN and must never be stored as one; see doc 18 §6.1.

WHY THE CHECK IS LOCAL
----------------------
Two inequalities per edge, and no cycle is ever unrolled:

    quiet edge     tau(child) <= tau(parent) + 1
    zeroing edge   tau(child) == 0

with terminals handled by re-deriving them: a White-win terminal is fatal
wherever it appears, anything else ends the game in Black's favour and needs
nothing further.

The soundness argument is an induction whose measure is atomic-specific.  With

    R(s) = pawn steps remaining to promotion + floor((N - 2) / 2)

every pawn move drops the first term and every capture drops the second (an
atomic capture removes at least the capturer and the captured), so every
zeroing move strictly decreases R, while every quiet move leaves R alone and
advances the clock.  M = (R, 100 - h) therefore decreases lexicographically on
every nonterminal move: play cannot reset forever.  The two inequalities keep
the invariant "h >= tau(current state)" true along any line the strategy
allows, so every line ends at a terminal that is not a White win, or at the
fifty-move claim.  A verifier that checks them has checked the whole strategy
without ever walking it.

NO REPETITION SHORTCUTS
-----------------------
``repetition_mode = NO_REPETITION_SHORTCUTS``.  Nothing here depends on move
history, so the same certificate is valid under any history and the
graph-history-interaction problem does not arise.  Note the consequence for
readers used to ``solve.py``: a repeated position is not an error here, it is
the point, and rejecting one would reject every fortress there is.

THE FORMAT
----------
Plain text, one record per line, states before edges so identifiers resolve in
a single pass::

    # atomic-survival-threshold-v1
    ruleset atomic-fide-claim-v1
    canonical 2
    repetition NO_REPETITION_SHORTCUTS
    terminal_precedence terminal-before-clock/1
    root <fen, with its real counters>
    entry_clock <c>
    states <n>
    edges <m>
    ---
    S <id> <tau> <canonical fen>     a state of the strategy
    W <id> <move> <target>           White to move: one line per LEGAL move
    B <id> <move> <target>           Black to move: THE selected reply
                                     target is #<id> or T

``T`` declares that the move ends the game in Black's favour.  The verifier
re-derives it and rejects a disagreement rather than taking the prover's word,
because a disagreement about terminality between two move generators is
exactly the bug this whole arrangement exists to catch.

WHAT IS CHECKED
---------------
Every state is canonical, distinct and genuinely interior.  Every White state
maps EXACTLY the legal set this verifier generates — not a subset, not a
superset, no duplicates.  Every Black state has exactly one reply and it is
legal.  Every edge is re-applied and lands on the state it claims.  Whether an
edge zeroes the counter is measured from the child, never declared.  Both
inequalities hold.  The root is present and tau(root) <= its clock.  And the
whole thing is bounded — bytes, states, edges, fan-out, and move generator
calls — because a public certificate endpoint is an adversarial input boundary
before it is anything else.

COST, MEASURED RATHER THAN ASSUMED
----------------------------------
pyffish charges a flat ~15 ms per position it constructs, independent of the
position: ``legal_moves`` on bare kings costs the same as on the opening, and
applying eight moves costs the same as applying one.  The floor for this
verifier is therefore one ``get_fen`` per edge plus a handful per state, and no
amount of care in this file moves it.

That is worth stating plainly, because doc 18 §3 budgets verification at
"40 states / ~500 universal edges in under 100 ms" and "10k states in
seconds", and neither is reachable here.  Measured on the reference box:

  * 500 edges  ->  about 8 seconds, not 100 ms;
  * the full bare-kings closure, 8,064 states and 52,080 edges, took 1,190
    seconds just to ENUMERATE, and verifying it costs the same again.

So the practical ceiling is thousands of edges per minute, not hundreds of
thousands per second, and the chunking-by-certified-SCC that the oracle
reserved for certificates above a million edges belongs two or three orders of
magnitude lower.  ``MAX_POSITIONS`` is set from the measurement rather than
from the doc, and ``positions`` is reported on every run so the fleet budgets
in the currency that actually costs something.
"""

import pyffish as pf

from . import logic
from .solve import CertificateError, compress, decompress

CERTIFICATE_FORMAT = 'atomic-survival-threshold-v1'
REPETITION_MODE = 'NO_REPETITION_SHORTCUTS'
# Identity of the claim automaton.  Terminal beats the counter: a mate
# delivered on the hundredth reversible ply is a mate.  Bump this string if
# that ever changes, because every tau label in the database depends on it.
TERMINAL_PRECEDENCE_ID = 'terminal-before-clock/1'

TAU_MIN = 0
TAU_MAX = logic.FIFTY_MOVE_PLIES  # 100

# Anti-bomb ceilings.  Structural ones first; they are about parsing safely.
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024 * 1024
MAX_HEADER_LINES = 32
MAX_STATES = 20_000
MAX_EDGES = 200_000
MAX_FANOUT = 256

# The one that costs wall clock.  A "position" is one pyffish position
# construction, measured at ~15 ms on the reference box, so 200k of them is
# roughly fifty minutes: a worker task, never an online request.  Callers with
# a deadline pass their own budget and get told what they spent.
MAX_POSITIONS = 200_000


class SurvivalReport(dict):
    """The verification report.  A dict, so it serialises without ceremony."""


def _fifty_move_counter(fen):
    parts = fen.split()
    try:
        return int(parts[4])
    except (IndexError, ValueError):
        return 0


class _Movegen:
    """pyffish behind a counter.

    Every call constructs a position and costs about 15 ms, so the count is
    the honest budget for this verifier and it is enforced rather than
    reported after the fact.  ``canonical_fen`` can spend up to two more calls
    of its own on positions that declare en passant; those are charged here
    too, by routing through this object.
    """

    def __init__(self, budget):
        self.budget = budget
        self.spent = 0

    def _charge(self, count=1):
        self.spent += count
        if self.spent > self.budget:
            raise CertificateError(
                f'certificate exceeds the move generator budget '
                f'({self.budget} positions)')

    def legal_moves(self, fen):
        self._charge()
        return logic.legal_moves(fen)

    def advance(self, fen, uci):
        self._charge()
        return pf.get_fen(logic.VARIANT, fen, [uci])

    def canonical(self, fen):
        # Two extra constructions, but only for positions that declare an en
        # passant square; charge for what the worst case actually costs.
        parts = fen.split()
        if len(parts) >= 4 and parts[3] != '-':
            self._charge(2)
        return logic.canonical_fen(fen)

    def terminal_status(self, fen):
        self._charge(3)
        return logic.terminal_status(fen)


def parse_header(text):
    """Header fields and the index at which the record stream starts."""
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


def _check_header(header, root_fen):
    if header.get('ruleset') != logic.RULESET_ID:
        raise CertificateError(
            f'certificate ruleset {header.get("ruleset")!r} is not '
            f'{logic.RULESET_ID!r}')
    if header.get('repetition') != REPETITION_MODE:
        raise CertificateError(
            f'certificate repetition mode {header.get("repetition")!r} is not '
            f'{REPETITION_MODE!r}')
    if header.get('terminal_precedence') != TERMINAL_PRECEDENCE_ID:
        raise CertificateError(
            f'certificate terminal precedence '
            f'{header.get("terminal_precedence")!r} is not '
            f'{TERMINAL_PRECEDENCE_ID!r}')
    # The canonicaliser is not the ruleset, but it decides which positions are
    # the SAME position, and a certificate keyed by a different identity would
    # pass every local check while describing an incoherent strategy.
    if header.get('canonical') != str(logic.CANONICAL_VERSION):
        raise CertificateError(
            f'certificate canonical version {header.get("canonical")!r} is not '
            f'{logic.CANONICAL_VERSION}')

    cert_root = header.get('root')
    if not cert_root:
        raise CertificateError('certificate has no root position')
    if root_fen is not None and \
            logic.canonical_fen(cert_root) != logic.canonical_fen(root_fen):
        raise CertificateError('certificate refutes a different position')

    declared = header.get('entry_clock')
    if declared is None:
        raise CertificateError('certificate has no entry clock')
    try:
        entry_clock = int(declared)
    except ValueError:
        raise CertificateError('certificate entry clock is malformed')
    if not 0 <= entry_clock <= TAU_MAX:
        raise CertificateError('certificate entry clock is out of range')
    # The clock is a property of the position, not of the prover's opinion.
    actual = _fifty_move_counter(root_fen if root_fen is not None else cert_root)
    if entry_clock != min(actual, TAU_MAX):
        raise CertificateError(
            f'certificate entry clock {entry_clock} does not match the '
            f"root position's halfmove counter {actual}")
    return cert_root, entry_clock


def _parse_body(lines, start, max_states, max_edges, max_fanout):
    """States, then edges.  One pass, no back-references to resolve later."""
    states = []          # id -> [tau, canonical fen]
    by_fen = {}          # canonical fen -> id
    white = {}           # id -> list of (move, target)
    black = {}           # id -> (move, target)
    edges = 0
    seen_edge = False

    for raw in lines[start:]:
        line = raw.strip()
        if not line:
            continue
        kind, _, rest = line.partition(' ')

        if kind == 'S':
            if seen_edge:
                raise CertificateError('a state is declared after an edge')
            parts = rest.split(' ', 2)
            if len(parts) != 3:
                raise CertificateError(f'malformed state record: {line!r}')
            try:
                state_id = int(parts[0])
                tau = int(parts[1])
            except ValueError:
                raise CertificateError(f'malformed state record: {line!r}')
            if state_id != len(states):
                raise CertificateError(
                    'state identifiers must run from 0 without gaps')
            if len(states) >= max_states:
                raise CertificateError('certificate exceeds the state limit')
            if not TAU_MIN <= tau <= TAU_MAX:
                raise CertificateError(
                    f'state {state_id} has tau {tau} outside [0, {TAU_MAX}]')
            fen = parts[2].strip()
            if fen in by_fen:
                raise CertificateError(
                    f'state {state_id} repeats the position of state '
                    f'{by_fen[fen]}')
            by_fen[fen] = state_id
            states.append([tau, fen])
            continue

        if kind in ('W', 'B'):
            seen_edge = True
            parts = rest.split()
            if len(parts) != 3:
                raise CertificateError(f'malformed edge record: {line!r}')
            try:
                state_id = int(parts[0])
            except ValueError:
                raise CertificateError(f'malformed edge record: {line!r}')
            if not 0 <= state_id < len(states):
                raise CertificateError(
                    f'edge cites unknown state {state_id}')
            edges += 1
            if edges > max_edges:
                raise CertificateError('certificate exceeds the edge limit')
            entry = (parts[1], parts[2])
            if kind == 'W':
                bucket = white.setdefault(state_id, [])
                if len(bucket) >= max_fanout:
                    raise CertificateError(
                        'a White state exceeds the fan-out limit')
                bucket.append(entry)
            else:
                if state_id in black:
                    raise CertificateError(
                        f'state {state_id} has more than one Black reply')
                black[state_id] = entry
            continue

        raise CertificateError(f'unknown record kind {kind!r}')

    return states, by_fen, white, black, edges


def _resolve(target, states):
    """``#id`` -> integer, ``T`` -> None.  Anything else is malformed."""
    if target == 'T':
        return None
    if not target.startswith('#'):
        raise CertificateError(f'malformed edge target {target!r}')
    try:
        index = int(target[1:])
    except ValueError:
        raise CertificateError(f'malformed edge target {target!r}')
    if not 0 <= index < len(states):
        raise CertificateError(f'edge target #{index} is not a state')
    return index


def verify_certificate(text, root_fen=None, max_states=MAX_STATES,
                       max_edges=MAX_EDGES, max_fanout=MAX_FANOUT,
                       max_positions=MAX_POSITIONS):
    """Replay a survival certificate in full.  Returns its report or raises.

    ``root_fen`` carries the counters; the certificate's states do not, because
    tau is a property of the base position and the clock is what tau is about.
    """
    header, start = parse_header(text)
    cert_root, entry_clock = _check_header(header, root_fen)
    states, by_fen, white, black, edges = _parse_body(
        text.split('\n'), start, max_states, max_edges, max_fanout)

    if not states:
        raise CertificateError('certificate has no states')
    for name, value in (('states', len(states)), ('edges', edges)):
        declared = header.get(name)
        if declared is None:
            continue
        try:
            expected = int(declared)
        except ValueError:
            raise CertificateError(f'certificate {name} count is malformed')
        if expected != value:
            raise CertificateError(
                f'certificate declares {expected} {name} but carries {value}')

    movegen = _Movegen(max_positions)
    zeroing_edges = 0
    terminal_exits = 0

    # PASS ONE: the states, in isolation.  Identity, terminality and the shape
    # of the quantifier at each one.  Doing this before any edge is walked is
    # not only tidier, it attributes a fault to the record that carries it: a
    # certificate with a non-canonical state should be rejected for that, not
    # for whatever the first edge pointing at it happens to trip over.
    plan = []
    for state_id, (tau, fen) in enumerate(states):
        # A state whose FEN is not in AtomicDB's canonical form is a state
        # whose identity is somebody else's.
        if movegen.canonical(fen) != fen:
            raise CertificateError(
                f'state {state_id} is not in canonical form: {fen}')
        status = movegen.terminal_status(fen)
        if status is not None:
            raise CertificateError(
                f'state {state_id} is already terminal ({status[0]}): {fen}')

        white_to_move = fen.split()[1] == 'w'
        legal = list(movegen.legal_moves(fen))

        if white_to_move:
            if state_id in black:
                raise CertificateError(
                    f'state {state_id} has a Black reply but White is to move')
            listed = white.get(state_id, [])
            moves = [move for move, _ in listed]
            if len(set(moves)) != len(moves):
                raise CertificateError(f'state {state_id} repeats a White move')
            if set(moves) != set(legal):
                missing = sorted(set(legal) - set(moves))
                extra = sorted(set(moves) - set(legal))
                raise CertificateError(
                    f'state {state_id} does not cover exactly the legal White '
                    f'moves (missing={missing}, extra={extra})')
            plan.append(listed)
        else:
            if state_id in white:
                raise CertificateError(
                    f'state {state_id} has White moves but Black is to move')
            if state_id not in black:
                raise CertificateError(
                    f'state {state_id} has no Black reply')
            move, target = black[state_id]
            if move not in legal:
                raise CertificateError(
                    f'state {state_id} selects illegal reply {move!r}')
            plan.append([(move, target)])

    # PASS TWO: the edges.  One position construction each, and the two local
    # inequalities that carry the whole induction.
    for state_id, outgoing in enumerate(plan):
        tau, fen = states[state_id]
        for move, target in outgoing:
            child = movegen.advance(fen, move)
            # Whether the counter reset is MEASURED on the child, never taken
            # from the certificate: the parent is canonical, so its counter is
            # zero and only a capture or a pawn move can leave the child at
            # zero as well.
            zeroing = _fifty_move_counter(child) == 0
            index = _resolve(target, states)

            if index is None:
                status = movegen.terminal_status(child)
                if status is None:
                    raise CertificateError(
                        f'state {state_id}: move {move} is declared terminal '
                        f'but the game continues')
                if status[0] == 'WHITE_WIN':
                    raise CertificateError(
                        f'state {state_id}: move {move} reaches a White win')
                terminal_exits += 1
                zeroing_edges += zeroing
                continue

            child_canonical = movegen.canonical(child)
            if states[index][1] != child_canonical:
                raise CertificateError(
                    f'state {state_id}: move {move} lands on '
                    f'{child_canonical} but claims state {index}')
            child_tau = states[index][0]
            if zeroing:
                # A reset restarts the horizon, so the destination has to hold
                # from a clock of zero.  Nothing weaker is admissible; this is
                # the exact point at which the "it is self-destructive anyway"
                # shortcut becomes unsound.
                if child_tau != 0:
                    raise CertificateError(
                        f'state {state_id}: zeroing move {move} enters state '
                        f'{index} with tau {child_tau}, not 0')
                zeroing_edges += 1
            elif child_tau > tau + 1:
                raise CertificateError(
                    f'state {state_id} (tau {tau}): quiet move {move} enters '
                    f'state {index} with tau {child_tau} > {tau + 1}')

    root_canonical = logic.canonical_fen(cert_root)
    root_id = by_fen.get(root_canonical)
    if root_id is None:
        raise CertificateError('the root position is not among the states')
    root_tau = states[root_id][0]
    if root_tau > entry_clock:
        raise CertificateError(
            f'certificate proves survival only from clock {root_tau}, but the '
            f'root enters at {entry_clock}')

    # Reachability costs nothing here — the graph is already parsed — and it
    # is the difference between a strategy and a strategy with luggage.
    reached = {root_id}
    stack = [root_id]
    while stack:
        current = stack.pop()
        edges_out = white.get(current) or (
            [black[current]] if current in black else [])
        for _, target in edges_out:
            index = _resolve(target, states)
            if index is not None and index not in reached:
                reached.add(index)
                stack.append(index)

    return SurvivalReport({
        'result': 'DISPROVED_WHITE_WIN',
        'root': cert_root,
        'entry_clock': entry_clock,
        'root_tau': root_tau,
        'states': len(states),
        'edges': edges,
        'reachable': len(reached),
        'zeroing_edges': zeroing_edges,
        'terminal_exits': terminal_exits,
        'max_tau': max(tau for tau, _ in states),
        'positions': movegen.spent,
    })


__all__ = [
    'CERTIFICATE_FORMAT', 'REPETITION_MODE', 'TERMINAL_PRECEDENCE_ID',
    'CertificateError', 'MAX_POSITIONS', 'SurvivalReport',
    'compress', 'decompress', 'parse_header', 'verify_certificate',
]
