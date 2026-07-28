"""SURVIVE50: the survival certificate verifier and the ways one can lie.

The fixtures are real closed atomic regions, not sketches.  A survival
certificate is only meaningful if EVERY legal White move is accounted for, so
a fixture that is not genuinely closed would be testing nothing; these are
built by expanding the region until it closes, with all of White's moves and
one chosen Black reply, exactly as the miner will.

Two regions carry the suite:

* ``KING_WALK`` — White has a bare king boxed onto the h-file by a Black rook
  on g8, Black shuffles its king.  Fourteen states, no resets, and it contains
  repeated positions on purpose: that is what makes it a fortress, and it is
  the case ``solve.py`` is right to reject and this verifier must not.
* ``PAWN_RESET`` — the same box plus a White pawn with exactly one push in it.
  Twenty-eight states and seven zeroing edges, which is what the reset rule
  needs to be tested on at all.

Everything else is one of those two, corrupted.
"""

import pyffish as pf
from django.core.management import call_command
from django.test import SimpleTestCase

from . import logic, survive

KING_WALK_ROOT = '6r1/k7/8/8/8/8/8/7K w - - 0 1'
PAWN_RESET_ROOT = '6r1/k7/1p6/8/1P6/8/8/7K w - - 0 1'
# `solve` proves this one: Qa1xg7 explodes g7 and the king on h8 with it.
MATE_IN_ONE = '7k/6p1/8/8/8/8/8/Q3K3 w - - 0 1'
# Rb7 covers a7 and b8, and an atomic king may not capture.
STALEMATE = 'k7/1R6/8/8/8/6K1/8/8 b - - 0 1'
# The black king has already been exploded: White has won, at any clock.
KING_GONE = '8/8/8/8/8/8/5K2/8 b - - 0 1'


def _shuffle_policy(fen, moves):
    """Black holds by walking its king between a7 and b7."""
    for candidate in ('a7b7', 'b7a7'):
        if candidate in moves:
            return candidate
    raise AssertionError(f'no policy move available at {fen}')


def _with_clock(fen, clock):
    parts = fen.split()
    parts[4] = str(clock)
    return ' '.join(parts)


def _expand(root, policy):
    """Close a region: every legal White move, one selected Black reply."""
    fens = [logic.canonical_fen(root)]
    ids = {fens[0]: 0}
    white, black = {}, {}
    queue = [0]
    while queue:
        state_id = queue.pop()
        fen = fens[state_id]
        white_to_move = fen.split()[1] == 'w'
        moves = list(logic.legal_moves(fen))
        chosen = moves if white_to_move else [policy(fen, moves)]
        outgoing = []
        for move in chosen:
            child = pf.get_fen(logic.VARIANT, fen, [move])
            zeroing = child.split()[4] == '0'
            status = logic.terminal_status(child)
            if status is not None:
                assert status[0] != 'WHITE_WIN', f'{fen} loses to {move}'
                outgoing.append((move, 'T', zeroing))
                continue
            key = logic.canonical_fen(child)
            if key not in ids:
                ids[key] = len(fens)
                fens.append(key)
                queue.append(ids[key])
            outgoing.append((move, f'#{ids[key]}', zeroing))
        if white_to_move:
            white[state_id] = outgoing
        else:
            black[state_id] = outgoing[0]
    return fens, white, black


def _thresholds(fens, white, black):
    """The tau fixed point, written a fourth time and in another language.

    The engine has three implementations of this recurrence and they agree;
    this one exists so that the certificates the suite feeds the verifier were
    not labelled by the same code that will be judged on them.
    """
    tau = [survive.TAU_MAX] * len(fens)
    changed = True
    while changed:
        changed = False
        for state_id in range(len(fens)):
            outgoing = white.get(state_id)
            is_white = outgoing is not None
            if not is_white:
                outgoing = [black[state_id]]
            needs = []
            for _, target, zeroing in outgoing:
                if target == 'T':
                    needs.append(0)
                    continue
                child = tau[int(target[1:])]
                if zeroing:
                    needs.append(0 if child == 0 else survive.TAU_MAX)
                else:
                    needs.append(min(survive.TAU_MAX, max(0, child - 1)))
            value = min(max(needs) if is_white else min(needs), survive.TAU_MAX)
            if value < tau[state_id]:
                tau[state_id] = value
                changed = True
    return tau


def _emit(root_fen, entry_clock, fens, white, black, tau):
    total = sum(len(edges) for edges in white.values()) + len(black)
    lines = [
        '# ' + survive.CERTIFICATE_FORMAT,
        'ruleset ' + logic.RULESET_ID,
        f'canonical {logic.CANONICAL_VERSION}',
        'repetition ' + survive.REPETITION_MODE,
        'terminal_precedence ' + survive.TERMINAL_PRECEDENCE_ID,
        'root ' + root_fen,
        f'entry_clock {entry_clock}',
        f'states {len(fens)}',
        f'edges {total}',
        '---',
    ]
    for state_id, fen in enumerate(fens):
        lines.append(f'S {state_id} {tau[state_id]} {fen}')
    for state_id in sorted(white):
        for move, target, _ in white[state_id]:
            lines.append(f'W {state_id} {move} {target}')
    for state_id in sorted(black):
        move, target, _ = black[state_id]
        lines.append(f'B {state_id} {move} {target}')
    return '\n'.join(lines) + '\n'


def _rewrite(text, old, new):
    assert old in text, f'fixture does not contain {old!r}'
    return text.replace(old, new, 1)


def _renumber(text):
    """Keep the declared counts honest after a structural mutation.

    The verifier checks the declared ``states``/``edges`` against what the
    body carries, and it checks that cheaply and early.  A test that adds or
    drops a record without fixing the header would only ever prove that the
    count check works, over and over, so every structural mutation goes
    through here and the intended check is the one that fires.
    """
    lines = text.split('\n')
    states = sum(1 for line in lines if line.startswith('S '))
    edges = sum(1 for line in lines
                if line.startswith('W ') or line.startswith('B '))
    fixed = []
    for line in lines:
        if line.startswith('states '):
            line = f'states {states}'
        elif line.startswith('edges '):
            line = f'edges {edges}'
        fixed.append(line)
    return '\n'.join(fixed)


def _drop_line(text, prefix):
    lines = text.split('\n')
    kept = [line for line in lines if not line.startswith(prefix)]
    assert len(kept) < len(lines), f'no line starts with {prefix!r}'
    return _renumber('\n'.join(kept))


def _append(text, line):
    return _renumber(text + line + '\n')


class SurvivalCertificateTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.walk_fens, cls.walk_white, cls.walk_black = _expand(
            KING_WALK_ROOT, _shuffle_policy)
        cls.walk_tau = _thresholds(cls.walk_fens, cls.walk_white,
                                   cls.walk_black)
        cls.walk = _emit(KING_WALK_ROOT, 0, cls.walk_fens, cls.walk_white,
                         cls.walk_black, cls.walk_tau)

        cls.reset_fens, cls.reset_white, cls.reset_black = _expand(
            PAWN_RESET_ROOT, _shuffle_policy)
        cls.reset_tau = _thresholds(cls.reset_fens, cls.reset_white,
                                    cls.reset_black)
        cls.reset = _emit(PAWN_RESET_ROOT, 0, cls.reset_fens, cls.reset_white,
                          cls.reset_black, cls.reset_tau)

    def _reject(self, text, fragment, **kwargs):
        with self.assertRaises(survive.CertificateError) as caught:
            survive.verify_certificate(text, **kwargs)
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    # ---- the fixtures are what they claim to be -------------------------

    def test_the_fixtures_are_closed_fortresses(self):
        """A structural fortress: tau 0 everywhere, so any clock holds."""
        self.assertEqual(len(self.walk_fens), 14)
        self.assertEqual(set(self.walk_tau), {0})
        self.assertEqual(len(self.reset_fens), 28)
        self.assertEqual(set(self.reset_tau), {0})
        resets = sum(1 for edges in self.reset_white.values()
                     for _, _, zeroing in edges if zeroing)
        self.assertEqual(resets, 7)
        self.assertEqual(
            sum(1 for edges in self.walk_white.values()
                for _, _, zeroing in edges if zeroing), 0)

    def test_accepts_a_real_closed_fortress(self):
        report = survive.verify_certificate(self.walk, root_fen=KING_WALK_ROOT)
        self.assertEqual(report['result'], 'DISPROVED_WHITE_WIN')
        self.assertEqual(report['root_tau'], 0)
        self.assertEqual(report['states'], 14)
        self.assertEqual(report['reachable'], 14)
        self.assertEqual(report['zeroing_edges'], 0)
        # One position built per edge plus a few per state, and no more: the
        # number is the verifier's real price and it is asserted, not hoped.
        self.assertLess(report['positions'], 6 * report['states']
                        + 2 * report['edges'])

    def test_accepts_a_region_whose_White_moves_reset_the_clock(self):
        report = survive.verify_certificate(self.reset,
                                            root_fen=PAWN_RESET_ROOT)
        self.assertEqual(report['states'], 28)
        self.assertEqual(report['zeroing_edges'], 7)
        self.assertEqual(report['result'], 'DISPROVED_WHITE_WIN')

    def test_a_repeated_position_is_the_point_not_an_error(self):
        """Invariance under history: nothing here depends on how we arrived.

        ``solve.py`` rejects a branch that repeats a position, correctly, since
        a repetition the defender can hold refutes a win.  The same shape is
        what a fortress IS, so this verifier must accept it — and because it
        never tracks a path, the same certificate is valid under every history.
        """
        # The region genuinely cycles: the White king walks h1-h2-h1.
        successors = {state: {int(target[1:])
                              for _, target, _ in edges if target != 'T'}
                      for state, edges in self.walk_white.items()}
        for state, edges in self.walk_black.items():
            _, target, _ = edges
            successors[state] = ({int(target[1:])} if target != 'T' else set())
        reachable, stack = set(), [0]
        while stack:
            for nxt in successors[stack.pop()]:
                if nxt not in reachable:
                    reachable.add(nxt)
                    stack.append(nxt)
        self.assertIn(0, reachable, 'the fixture must contain a cycle')
        survive.verify_certificate(self.walk, root_fen=KING_WALK_ROOT)

    # ---- the threshold ---------------------------------------------------

    def test_accepts_at_the_threshold_and_rejects_one_ply_below(self):
        """tau is a certified upper bound, and the root's clock must meet it."""
        raised = [50] * len(self.walk_fens)
        at = _emit(_with_clock(KING_WALK_ROOT, 50), 50, self.walk_fens,
                   self.walk_white, self.walk_black, raised)
        report = survive.verify_certificate(at)
        self.assertEqual(report['root_tau'], 50)
        self.assertEqual(report['entry_clock'], 50)

        below = _emit(_with_clock(KING_WALK_ROOT, 49), 49, self.walk_fens,
                      self.walk_white, self.walk_black, raised)
        self._reject(below, 'survival only from clock 50')

    def test_rejects_an_entry_clock_that_is_not_the_root_counter(self):
        lying = _rewrite(self.walk, 'entry_clock 0', 'entry_clock 40')
        self._reject(lying, 'does not match')

    def test_rejects_a_tau_outside_the_range(self):
        self._reject(_rewrite(self.walk, 'S 0 0 ', 'S 0 101 '), 'outside')

    # ---- White coverage --------------------------------------------------

    def test_rejects_an_omitted_White_move(self):
        state, edges = next(iter(sorted(self.walk_white.items())))
        move, target, _ = edges[0]
        self._reject(_drop_line(self.walk, f'W {state} {move} {target}'),
                     'does not cover exactly the legal White moves')

    def test_rejects_an_invented_White_move(self):
        state = min(self.walk_white)
        extra = _append(self.walk, f'W {state} a1a2 T')
        self._reject(extra, 'does not cover exactly the legal White moves')

    def test_rejects_a_repeated_White_move(self):
        state, edges = next(iter(sorted(self.walk_white.items())))
        move, target, _ = edges[0]
        self._reject(_append(self.walk, f'W {state} {move} {target}'),
                     'repeats a White move')

    def test_rejects_a_Black_reply_at_a_White_state(self):
        state = min(self.walk_white)
        self._reject(_append(self.walk, f'B {state} a1a2 T'),
                     'has a Black reply but White is to move')

    def test_rejects_a_missing_or_doubled_Black_reply(self):
        state, (move, target, _) = next(iter(sorted(self.walk_black.items())))
        self._reject(_drop_line(self.walk, f'B {state} {move} {target}'),
                     'has no Black reply')
        self._reject(_append(self.walk, f'B {state} {move} {target}'),
                     'more than one Black reply')

    def test_rejects_an_illegal_Black_reply(self):
        state, (move, target, _) = next(iter(sorted(self.walk_black.items())))
        self._reject(_rewrite(self.walk, f'B {state} {move} {target}',
                              f'B {state} h7h6 {target}'),
                     'selects illegal reply')

    # ---- the two local inequalities --------------------------------------

    def test_rejects_a_reset_whose_target_is_not_certified_at_tau_zero(self):
        """The rule that the whole subsystem turns on.

        A reset restarts the fifty-move horizon, so the destination has to hold
        from a clock of zero.  "It is self-destructive anyway" is not a proof,
        and tau(child) <= 1 is not the rule either.
        """
        push = next((state, move, target)
                    for state, edges in self.reset_white.items()
                    for move, target, zeroing in edges if zeroing)
        state, move, target = push
        victim = int(target[1:])
        text = _rewrite(self.reset, f'S {victim} 0 ', f'S {victim} 1 ')
        message = self._reject(text, 'zeroing move')
        self.assertIn('not 0', message)

    def test_rejects_a_quiet_edge_that_breaks_the_inequality(self):
        text = _rewrite(self.walk, 'S 1 0 ', 'S 1 2 ')
        self._reject(text, 'quiet move')

    def test_a_quiet_edge_may_climb_by_exactly_one(self):
        """tau(child) == tau(parent) + 1 is legal; the next ply pays for it."""
        raised = [50] * len(self.walk_fens)
        raised[1] = 51
        text = _emit(_with_clock(KING_WALK_ROOT, 51), 51, self.walk_fens,
                     self.walk_white, self.walk_black, raised)
        # State 1 climbs to 51 and every parent sits at 50: 51 <= 50 + 1.
        survive.verify_certificate(text)

    def test_rejects_an_edge_that_lands_on_a_different_position(self):
        state, edges = next(iter(sorted(self.walk_white.items())))
        move, target, _ = edges[0]
        other = '#2' if target != '#2' else '#3'
        self._reject(_rewrite(self.walk, f'W {state} {move} {target}',
                              f'W {state} {move} {other}'),
                     'but claims state')

    # ---- terminals and the claim automaton -------------------------------

    def _lost_position_certificate(self, moves):
        fen = logic.canonical_fen(MATE_IN_ONE)
        lines = ['# ' + survive.CERTIFICATE_FORMAT,
                 'ruleset ' + logic.RULESET_ID,
                 f'canonical {logic.CANONICAL_VERSION}',
                 'repetition ' + survive.REPETITION_MODE,
                 'terminal_precedence ' + survive.TERMINAL_PRECEDENCE_ID,
                 'root ' + MATE_IN_ONE, 'entry_clock 0', '---',
                 f'S 0 0 {fen}']
        lines += [f'W 0 {move} T' for move in moves]
        return '\n'.join(lines) + '\n'

    def test_rejects_a_move_that_reaches_a_White_win(self):
        """Mate outranks the counter, wherever in the certificate it appears."""
        moves = list(logic.legal_moves(logic.canonical_fen(MATE_IN_ONE)))
        self.assertIn('a1g7', moves)
        ordered = ['a1g7'] + [move for move in moves if move != 'a1g7']
        self._reject(self._lost_position_certificate(ordered),
                     'reaches a White win')

    def test_a_lost_position_cannot_be_certified_either_way(self):
        """The trap that makes the whole arrangement worth anything.

        White mates in one here, so no survival certificate exists and none may
        be manufactured.  There are exactly two ways to try: name the mating
        move, or leave it out.  Naming it is rejected because it reaches a White
        win; leaving it out is rejected because the universal closure is
        regenerated here and does not match.  Neither door opens, and it is the
        pair that matters — either check alone leaves the other door ajar.
        """
        moves = list(logic.legal_moves(logic.canonical_fen(MATE_IN_ONE)))
        self._reject(self._lost_position_certificate(['a1g7'] + [
            move for move in moves if move != 'a1g7']), 'reaches a White win')
        self._reject(self._lost_position_certificate(
            [move for move in moves if move != 'a1g7']),
            'does not cover exactly the legal White moves')

    def test_rejects_a_terminal_state(self):
        fen = logic.canonical_fen(STALEMATE)
        text = ('# ' + survive.CERTIFICATE_FORMAT + '\n'
                + 'ruleset ' + logic.RULESET_ID + '\n'
                + f'canonical {logic.CANONICAL_VERSION}\n'
                + 'repetition ' + survive.REPETITION_MODE + '\n'
                + 'terminal_precedence ' + survive.TERMINAL_PRECEDENCE_ID + '\n'
                + 'root ' + STALEMATE + '\nentry_clock 0\n---\n'
                + f'S 0 0 {fen}\n')
        self._reject(text, 'already terminal')

    def test_rejects_a_terminal_claim_where_the_game_continues(self):
        state, edges = next(iter(sorted(self.walk_white.items())))
        move, target, _ = edges[0]
        self._reject(_rewrite(self.walk, f'W {state} {move} {target}',
                              f'W {state} {move} T'),
                     'declared terminal but the game continues')

    def test_the_claim_automaton_is_blind_to_the_clock(self):
        """Plies 99 and 100 must not change what a terminal position is.

        This is the assumption that lets the certificate carry no clock at all:
        terminality is decided by the board, the counter is decided by tau, and
        the two never have to be reconciled inside the verifier.
        """
        for clock in (0, 98, 99, 100, 120):
            self.assertEqual(
                logic.terminal_status(_with_clock(KING_GONE, clock)),
                ('WHITE_WIN', 'terminal'),
                f'a won position stopped being won at clock {clock}')
            self.assertEqual(
                logic.terminal_status(_with_clock(STALEMATE, clock)),
                ('DRAW', 'terminal'),
                f'a stalemate stopped being a stalemate at clock {clock}')

    # ---- identity and the frozen statement -------------------------------

    def test_rejects_a_foreign_statement(self):
        for field, value, fragment in (
                ('ruleset ' + logic.RULESET_ID, 'ruleset atomic-other-v1',
                 'is not'),
                ('repetition ' + survive.REPETITION_MODE,
                 'repetition ALLOW_REPETITION', 'repetition mode'),
                ('terminal_precedence ' + survive.TERMINAL_PRECEDENCE_ID,
                 'terminal_precedence clock-before-terminal/1',
                 'terminal precedence'),
                (f'canonical {logic.CANONICAL_VERSION}', 'canonical 1',
                 'canonical version')):
            self._reject(_rewrite(self.walk, field, value), fragment)

    def test_rejects_an_unknown_format(self):
        self._reject(_rewrite(self.walk, '# ' + survive.CERTIFICATE_FORMAT,
                              '# atomicdb-proof/1'), 'unknown certificate format')

    def test_rejects_a_certificate_about_another_position(self):
        self._reject(self.walk, 'refutes a different position',
                     root_fen=PAWN_RESET_ROOT)

    def test_rejects_a_non_canonical_state(self):
        fen = self.walk_fens[1]
        self._reject(_rewrite(self.walk, f'S 1 0 {fen}',
                              f'S 1 0 {_with_clock(fen, 7)}'),
                     'not in canonical form')

    def test_rejects_a_repeated_state(self):
        self._reject(_rewrite(self.walk, f'S 1 0 {self.walk_fens[1]}',
                              f'S 1 0 {self.walk_fens[0]}'),
                     'repeats the position of state')

    def test_rejects_sparse_state_identifiers(self):
        self._reject(_rewrite(self.walk, 'S 1 0 ', 'S 4 0 '),
                     'without gaps')

    def test_rejects_a_root_outside_the_states(self):
        text = _rewrite(self.walk, 'root ' + KING_WALK_ROOT,
                        'root ' + STALEMATE)
        self._reject(text, 'not among the states')

    def test_rejects_declared_counts_that_do_not_match(self):
        self._reject(_rewrite(self.walk, 'states 14', 'states 13'),
                     'declares 13 states')
        self._reject(_rewrite(self.walk, 'edges 19', 'edges 40'),
                     'declares 40 edges')

    # ---- adversarial input boundary --------------------------------------

    def test_enforces_the_move_generator_budget(self):
        """The only budget that costs wall clock, so the only one that bites.

        pyffish charges a flat ~15 ms per position it builds, whatever the
        position, so a certificate's price is one construction per edge plus a
        few per state.  The ceiling is enforced during the walk, not audited
        after it.
        """
        self._reject(self.reset, 'move generator budget', max_positions=12)

    def test_enforces_the_structural_limits(self):
        self._reject(self.walk, 'state limit', max_states=4)
        self._reject(self.walk, 'edge limit', max_edges=4)
        self._reject(self.walk, 'fan-out limit', max_fanout=1)

    def test_rejects_malformed_records(self):
        self._reject(_append(self.walk, 'X 0 nonsense'), 'unknown record kind')
        self._reject(_rewrite(self.walk, 'S 1 0 ', 'S 1 x '),
                     'malformed state record')
        self._reject(_append(self.walk, 'W 99 a1a2 T'), 'unknown state 99')
        self._reject(_rewrite(self.walk, '\n---\n', '\n'),
                     'header is not terminated')
        state, edges = next(iter(sorted(self.walk_white.items())))
        move, target, _ = edges[0]
        self._reject(_rewrite(self.walk, f'W {state} {move} {target}',
                              f'W {state} {move} @9'),
                     'malformed edge target')

    def test_rejects_a_state_declared_after_an_edge(self):
        self._reject(_append(self.walk, f'S 14 0 {STALEMATE}'),
                     'declared after an edge')

    def test_round_trips_through_gzip(self):
        blob = survive.compress(self.walk)
        self.assertEqual(survive.decompress(blob), self.walk)


class SurvivalCommandTests(SimpleTestCase):
    """The command is the operator-facing half; it must not swallow a lie."""

    def setUp(self):
        fens, white, black = _expand(KING_WALK_ROOT, _shuffle_policy)
        self.text = _emit(KING_WALK_ROOT, 0, fens, white, black,
                          _thresholds(fens, white, black))

    def _run(self, text, **kwargs):
        import io
        import tempfile
        import pathlib
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'certificate.txt'
            path.write_text(text, encoding='utf-8')
            out = io.StringIO()
            call_command('verify_survival', file=str(path), stdout=out,
                         **kwargs)
            return out.getvalue()

    def test_reports_a_verified_certificate(self):
        output = self._run(self.text, root=KING_WALK_ROOT)
        self.assertIn('VERIFIED', output)
        self.assertIn('result=DISPROVED_WHITE_WIN', output)
        self.assertIn('states=14', output)

    def test_reports_a_rejected_certificate(self):
        broken = _rewrite(self.text, 'entry_clock 0', 'entry_clock 40')
        output = self._run(broken)
        self.assertIn('REJECTED', output)
        self.assertNotIn('VERIFIED', output)
