"""Write the test suite's certificate fixtures out as files.

The fixtures live in ``atomicdb/test_survive.py`` because that is where they
are exercised; this dumps them so the native verifier can be run against the
same bytes the Python reference is tested on. It builds nothing of its own --
if the two ever diverge, they diverge at the source.
"""
import argparse
import os
import pathlib
import sys

import django

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')
django.setup()

from atomicdb import test_survive as fixtures  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='.', help='directory to write into')
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for name, root in (('king_walk', fixtures.KING_WALK_ROOT),
                       ('pawn_reset', fixtures.PAWN_RESET_ROOT)):
        fens, white, black = fixtures._expand(root, fixtures._shuffle_policy)
        tau = fixtures._thresholds(fens, white, black)
        text = fixtures._emit(root, 0, fens, white, black, tau)
        path = out / f'{name}.cert'
        path.write_text(text, encoding='utf-8')
        print(f'{path}  states={len(fens)} '
              f'edges={sum(len(e) for e in white.values()) + len(black)} '
              f'root={root}')


main()
