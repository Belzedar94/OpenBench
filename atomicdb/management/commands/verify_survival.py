"""Replay a SURVIVE50 certificate with the server's own rules. REPLAYS IN FULL.

The sibling of ``verify_certificate``.  That one checks a win: a finite
strategy tree ending in mate everywhere.  This one checks the other half of a
weak solve — a fortress, where the refutation is not a tree at all but a
strategy that outlasts the fifty-move counter.  See ``atomicdb/survive.py``
for what is actually being claimed and why the check is local.

The replay runs on ``pyffish`` while the certificate comes from
Atomic-Stockfish's own move generator, so the complete legal move set at every
White state is regenerated here and matched exactly.  Nothing the prover says
about legality, terminality or the counter is taken on trust.

Deliberately file-only for now.  Storing a survival result is phase 5 of doc
18 §7 and needs a typed result on ``SolveTask`` — ``DISPROVED_WHITE_WIN`` is
NOT ``PROVEN_BLACK_WIN`` and must not be written through a column that cannot
tell them apart.

``--budget`` is the number of positions the move generator may construct.  It
is the only budget that costs wall clock here: pyffish charges a flat ~15 ms
per position whatever the position is, so a certificate's real price is one
construction per edge plus a handful per state, and the command reports what
it spent so callers can size their deadlines from measurements.
"""

import pathlib

from django.core.management.base import BaseCommand

from atomicdb import survive


class Command(BaseCommand):
    help = 'Replay SURVIVE50 certificates in full against the server movegen.'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True,
                            help='Certificate file to verify (plain or .gz).')
        parser.add_argument('--root', default=None,
                            help='Expected root FEN, counters included.')
        parser.add_argument('--budget', type=int, default=survive.MAX_POSITIONS,
                            help='Move generator budget, in positions built.')

    def handle(self, *args, **options):
        path = pathlib.Path(options['file'])
        raw = path.read_bytes()
        text = (survive.decompress(raw) if path.suffix == '.gz'
                else raw.decode('utf-8', errors='replace'))
        try:
            report = survive.verify_certificate(
                text, root_fen=options['root'],
                max_positions=options['budget'])
        except survive.CertificateError as error:
            self.stdout.write(f'REJECTED {path}: {error}')
            return
        self.stdout.write(
            f'VERIFIED {path}: result={report["result"]} '
            f'tau={report["root_tau"]}<=clock={report["entry_clock"]} '
            f'states={report["states"]} edges={report["edges"]} '
            f'reachable={report["reachable"]} '
            f'resets={report["zeroing_edges"]} '
            f'exits={report["terminal_exits"]} '
            f'positions={report["positions"]}')
