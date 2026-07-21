"""Retroactively certify AtomicDB's existing MATE_PV closures."""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from atomicdb import ingest, logic
from atomicdb.models import DBEvent, Position


class Command(BaseCommand):
    help = ('Verify existing MATE_PV closures with a bounded exhaustive '
            'AND/OR search. Safe to stop and resume.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--budget-positions', type=int, default=200_000,
            help='Maximum positions searched per MATE_PV (default: 200000).')
        parser.add_argument(
            '--batch-size', type=int, default=100,
            help='Number of keys fetched between resumable checkpoints.')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Stop after this many newly classified positions.')

    def handle(self, *args, **options):
        budget = options['budget_positions']
        batch_size = options['batch_size']
        limit = options['limit']
        if budget < 0:
            raise ValueError('--budget-positions must be non-negative')
        if batch_size <= 0:
            raise ValueError('--batch-size must be positive')
        if limit is not None and limit < 0:
            raise ValueError('--limit must be non-negative')

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        counts = {'ANDOR': 0, 'ENGINE': 0, 'DISPUTED': 0,
                  'SKIPPED': 0, 'ERROR': 0}
        disputed = []
        processed = 0
        after_key = ''

        while limit is None or processed < limit:
            take = batch_size
            if limit is not None:
                take = min(take, limit - processed)
            if take <= 0:
                break
            rows = list(
                Position.objects.filter(
                    closure='MATE_PV', proof__isnull=True,
                    key__gt=after_key,
                ).order_by('key').values(
                    'key', 'fen', 'status', 'closure', 'won_line',
                )[:take]
            )
            if not rows:
                break

            for snapshot in rows:
                after_key = snapshot['key']
                hint = (snapshot['won_line'] or '').split()
                winner_is_white = snapshot['status'] == 'WHITE_WIN'
                if snapshot['status'] not in ('WHITE_WIN', 'BLACK_WIN'):
                    counts['ERROR'] += 1
                    self.stderr.write(
                        f"ERROR {snapshot['key']}: MATE_PV has status "
                        f"{snapshot['status']!r}")
                    continue
                try:
                    verdict = logic.prove_forced_mate(
                        snapshot['fen'], winner_is_white,
                        max_plies=len(hint) + 2,
                        budget_positions=budget,
                        hint_pv=hint,
                    )
                except Exception as exc:  # leave proof NULL for a later resume
                    counts['ERROR'] += 1
                    self.stderr.write(
                        f"ERROR {snapshot['key']}: {type(exc).__name__}: {exc}")
                    continue

                proof = {
                    'PROVEN': 'ANDOR',
                    'INCONCLUSIVE': 'ENGINE',
                    'NO_MATE': 'DISPUTED',
                }[verdict]

                # The expensive proof runs outside the transaction.  Lock only
                # long enough to compare the snapshot and store one position.
                with transaction.atomic():
                    current = Position.objects.select_for_update().get(
                        key=snapshot['key'])
                    unchanged = (
                        current.proof is None
                        and current.closure == snapshot['closure']
                        and current.status == snapshot['status']
                        and current.fen == snapshot['fen']
                        and current.won_line == snapshot['won_line']
                    )
                    if not unchanged:
                        counts['SKIPPED'] += 1
                        continue
                    current.proof = proof
                    current.save(update_fields=['proof', 'updated'])
                    if proof == 'DISPUTED':
                        DBEvent.objects.create(
                            kind='MATE_PROOF_DISPUTED',
                            payload={
                                'key': current.key,
                                'status': current.status,
                                'closure': current.closure,
                                'max_plies': len(hint) + 2,
                            },
                        )

                counts[proof] += 1
                processed += 1
                if proof == 'DISPUTED':
                    disputed.append((snapshot['key'], snapshot['fen']))

        # Re-run the idempotent confidence backup from every classified mate.
        # Doing this as a final phase also repairs ancestors after an earlier
        # command was interrupted between classification and propagation.
        proof_seeds = list(Position.objects.filter(
            closure='MATE_PV', proof__in=('ANDOR', 'ENGINE', 'DISPUTED'),
        ).values_list('key', flat=True))
        if proof_seeds:
            ingest.backup_cascade(proof_seeds)

        self.stdout.write(
            'verify_mates: '
            + ' '.join(f'{name}={value}' for name, value in counts.items()))
        if disputed:
            self.stdout.write('DISPUTED positions (not reverted):')
            for key, fen in disputed:
                self.stdout.write(f'  {key} {fen}')
