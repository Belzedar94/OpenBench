"""Shared test vectors for the two copies of the Alice Atomic rules.

``python -m atomicdb.funquests.vectors`` rewrites
``alice_atomic_vectors.json`` from the Python rules.  The Python tests check
that the file still matches these rules, and the JavaScript test checks the
browser's rules against the same file, so a change to either copy that the
other does not share fails a test.
"""

import hashlib
import json
import os
import random

from . import alice_atomic as rules

VECTORS_PATH = os.path.join(os.path.dirname(__file__),
                            'alice_atomic_vectors.json')

# Hand-made positions for the rules that random games rarely reach.
HANDMADE = (
    # En passant: Ae2e4 lands on B e4 next to the black pawn on B d4.
    '4k3/8/8/8/8/8/4P3/4K3|8/8/8/8/3p4/8/8/8 w - - 0 1',
    '4k3/8/8/8/8/8/8/4K3|8/8/8/8/3pP3/8/8/8 b - Be3 0 1',
    # Same, but the square passed over is taken on B: no en passant.
    '4k3/8/8/8/8/8/4P3/4K3|8/8/8/8/3p4/4n3/8/8 w - - 0 1',
    # Castling: B g1 taken, so only the long castle.
    'r3k2r/8/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/6n1 w KQkq - 0 1',
    'r3k2r/8/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/8 b KQkq - 0 1',
    # Castling through an attacked square on A.
    'r3k2r/5r2/8/8/8/8/8/R3K2R|8/8/8/8/8/8/8/8 w KQkq - 0 1',
    # Explosion wins: Qh5xf7 blows up the king on e8.
    '4k3/5p2/8/7Q/8/8/8/4K3|8/8/8/8/8/8/8/8 w - - 0 1',
    # Own king next to the capture square: that capture is illegal.
    'k7/8/8/8/8/8/2pK4/2R5|8/8/8/8/8/8/8/8 w - - 0 1',
    # Kings side by side on A: the rook on A gives no check.
    '8/8/8/8/3kK3/8/8/4r3|8/8/8/8/8/8/8/8 w - - 0 1',
    # A rook on B does not check a king on A.
    '4k3/8/8/8/8/8/8/4K3|4r3/8/8/8/8/8/8/8 w - - 0 1',
    # Check on B, answered by moving or by passing through.
    '4k3/8/8/8/8/8/8/8|8/8/8/8/8/8/8/r3K3 w - - 0 1',
    # Promotions: quiet ones pass through, B d8 empty; captures explode.
    '2r1k3/3P4/8/8/8/8/8/4K3|8/8/8/8/8/8/8/8 w - - 0 1',
    '2r1k3/3P4/8/8/8/8/8/4K3|3n4/8/8/8/8/8/8/8 w - - 0 1',
    # A king next to the queen on A but free to pass through to B.
    'k7/1Q6/2K5/8/8/8/8/8|8/8/8/8/8/8/8/8 b - - 0 1',
    # Stalemate and checkmate: the knights on B close every way through.
    'k7/2Q5/1K6/8/8/8/8/8|1N6/NN6/8/8/8/8/8/8 b - - 0 1',
    'k7/1Q6/2K5/8/8/8/8/8|1N6/N7/8/8/8/8/8/8 b - - 0 1',
    # The game is already over: White's king was blown up.
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQ1BNR|8/8/8/8/8/8/8/8 w kq - 0 1',
)


def afters_digest(state):
    """One digest of every legal move with the position it leads to, so the
    vectors check what each move does without listing every position."""
    lines = sorted(f'{rules.move_text(m)} {rules.to_afen(after)}'
                   for m, after in rules.legal_moves_detailed(state))
    return hashlib.sha1('\n'.join(lines).encode()).hexdigest()


def _vector(state):
    found = rules.outcome(state)
    return {
        'afters': afters_digest(state),
        'afen': rules.to_afen(state),
        'legal': sorted(rules.legal_moves(state)),
        'in_check': rules.in_check(state, state.turn),
        'outcome': list(found) if found else None,
    }


def _random_positions(seed, games, max_plies, every):
    rng = random.Random(seed)
    out = []
    for _game in range(games):
        state = rules.start_state()
        for ply in range(max_plies):
            legal = rules.legal_moves_detailed(state)
            if not legal or rules.outcome(state):
                break
            # Prefer captures half the time, so blasts show up early.
            captures = [x for x in legal
                        if x[0][4] in (rules.CAPTURE, rules.EN_PASSANT)]
            pool = captures if captures and rng.random() < 0.5 else legal
            state = rng.choice(pool)[1]
            if ply % every == every - 1 or state.ep:
                out.append(state)
    return out


def build():
    states = [rules.parse(afen) for afen in HANDMADE]
    states.append(rules.start_state())
    states.extend(_random_positions(seed=20261114, games=24, max_plies=80,
                                    every=7))
    seen, positions = set(), []
    for state in states:
        afen = rules.to_afen(state)
        if afen in seen:
            continue
        seen.add(afen)
        positions.append(_vector(state))
    start = rules.start_state()
    return {
        'format': 1,
        'positions': positions,
        'perft': [{'afen': rules.START_AFEN, 'depth': d,
                   'nodes': rules.perft(start, d)} for d in (1, 2, 3)],
    }


def write(path=VECTORS_PATH):
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(build(), handle, indent=1, sort_keys=True)
        handle.write('\n')


if __name__ == '__main__':
    write()
    print(VECTORS_PATH)
