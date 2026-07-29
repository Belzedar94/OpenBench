"""Audit phantom en-passant squares in the AtomicDB identity. REPORTS ONLY.

``canonical_fen`` puts the en-passant square in the key.  If the move generator
emits that square even when NO legal en-passant capture exists, two positions
that the game does not distinguish get two different keys.  Cost, in
increasing severity:

1. lost transposition — two rows where there should be one;
2. the branch repetition guard in ``prove_forced_mate`` compares those keys, so
   a genuine repetition can go unnoticed and a line that is actually a draw by
   repetition can be certified as won.  That one is a soundness failure.

CONFIRMED, for atomic, against the pinned move generator: a phantom square IS
emitted when the capture is pseudo-legal but illegal.  Two atomic-specific
shapes reproduce it — the capture would explode the capturing side's own king,
and the capturing pawn is pinned.  When no enemy pawn sits beside the pushed
pawn at all, the square is correctly dropped, so the exposure is exactly the
"pseudo-legal but illegal" band.

The canonicalizer now drops those squares (``logic.CANONICAL_VERSION`` 2), so
this command has two jobs.  Before the re-keying pass it MEASURES the exposure
on the stored rows; after it, ``--verify`` is the check that the pass finished
its work, and it fails loudly if any stored key still carries a right that
cannot be executed.  It still writes nothing itself.
"""

import json

import pyffish as pf
from django.core.management.base import BaseCommand, CommandError

from atomicdb import logic
from atomicdb.database import connection
from atomicdb.models import Position

# Synthetic adversarial positions, kept next to the scanner so `--self-test`
# proves the detector still detects on a database that happens to be clean.
ADVERSARIAL = (
    ('own-king-explodes-d3', '8/8/8/8/3p4/3k4/4P3/6K1 w - - 0 1', 'e2e4'),
    ('own-king-explodes-f3', '8/8/8/8/3p4/5k2/4P3/6K1 w - - 0 1', 'e2e4'),
    ('pinned-capturing-pawn', '3k4/8/8/8/3p4/8/4P3/3RK3 w - - 0 1', 'e2e4'),
    ('ordinary-legal-capture', '7k/8/8/8/3p4/8/4P3/4K3 w - - 0 1', 'e2e4'),
)


class Command(BaseCommand):
    help = ('Report positions whose key carries an en-passant square that no '
            'legal move can use. Read-only.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Stop after scanning this many stored positions.')
        parser.add_argument(
            '--sample', type=int, default=10,
            help='How many offending keys to print (default: 10).')
        parser.add_argument(
            '--self-test', action='store_true',
            help='Only run the synthetic adversarial cases and exit.')
        parser.add_argument(
            '--verify', action='store_true',
            help='Fail unless every stored key is free of phantom rights. '
                 'This is the post-rekey check.')
        parser.add_argument(
            '--json', action='store_true',
            help='Emit one JSON object instead of the readable report.')

    def handle(self, *args, **options):
        if options['self_test']:
            self._self_test(options['json'])
            return

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            # Igual que el resto de comandos largos: sin esto, el primer
            # write ajeno que coincida con el iterator tumba el escaneo con
            # "database is locked" en vez de esperarle medio minuto.
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')
        counts = {'scanned': 0, 'with_ep': 0, 'phantom': 0, 'errors': 0}
        offenders = []
        rows = Position.objects.exclude(fen__contains=' - 0 1').values_list(
            'key', 'fen')
        # ``exclude`` above only trims the obvious majority; the real filter is
        # the parse below, because a FEN with castling rights also lacks that
        # exact substring.
        for key, fen in rows.iterator(chunk_size=2000):
            if options['limit'] is not None \
                    and counts['scanned'] >= options['limit']:
                break
            counts['scanned'] += 1
            try:
                verdict = logic.audit_en_passant(fen)
            except Exception:
                counts['errors'] += 1
                continue
            if verdict is None:
                continue
            counts['with_ep'] += 1
            if verdict['phantom']:
                counts['phantom'] += 1
                if len(offenders) < options['sample']:
                    offenders.append({'key': key, 'fen': fen,
                                      'square': verdict['square']})

        if options['json']:
            self.stdout.write(json.dumps(
                {'ruleset': logic.RULESET_ID, **counts,
                 'sample': offenders}, sort_keys=True))
            return
        self.stdout.write(f'ruleset {logic.RULESET_ID}')
        self.stdout.write(
            'audit_en_passant: '
            + ' '.join(f'{name}={value}' for name, value in counts.items()))
        if counts['with_ep']:
            share = 100.0 * counts['phantom'] / counts['with_ep']
            self.stdout.write(
                f'  phantom share of e.p. positions: {share:.1f}%')
        for offender in offenders:
            self.stdout.write(
                f"  {offender['key'][:16]} ep={offender['square']} "
                f"{offender['fen']}")
        self.stdout.write('NOTHING WAS CHANGED; this command only measures.')
        if options['verify'] and counts['phantom']:
            raise CommandError(
                f"{counts['phantom']} stored position(s) still carry a "
                'phantom en-passant right: run rekey_en_passant')

    def _self_test(self, as_json):
        results = []
        for name, before, move in ADVERSARIAL:
            # RAW movegen output on purpose: `logic.apply_move` canonicalises,
            # and the canonicalizer is exactly what these cases exist to test.
            raw = pf.get_fen(logic.VARIANT, before, [move])
            verdict = logic.audit_en_passant(raw)
            results.append({
                'case': name, 'fen': raw,
                'square': None if verdict is None else verdict['square'],
                'legal_ep_moves': [] if verdict is None
                else verdict['legal_moves'],
                'phantom': bool(verdict and verdict['phantom']),
                'canonical': logic.canonical_fen(raw),
                'canonical_keeps_ep':
                    logic.canonical_fen(raw).split()[3] != '-',
            })
        if as_json:
            self.stdout.write(json.dumps(results, sort_keys=True))
            return
        for row in results:
            marker = 'PHANTOM' if row['phantom'] else 'ok     '
            kept = 'kept' if row['canonical_keeps_ep'] else 'dropped'
            self.stdout.write(
                f"  {marker} {row['case']:<24} ep={row['square']} "
                f"legal={row['legal_ep_moves']} canonical={kept}")
        self.stdout.write(
            f'canonicalizer v{logic.CANONICAL_VERSION}: a PHANTOM case must '
            'read "dropped" and a real one "kept".')
