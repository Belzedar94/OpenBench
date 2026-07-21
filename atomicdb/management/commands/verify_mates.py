"""Retroactively certify AtomicDB's existing MATE_PV closures."""

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Q
from django.db.models.functions import Length
from django.utils import timezone

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
                  'MISSING': 0, 'SKIPPED': 0, 'ERROR': 0}
        processed = 0
        after_witness_len = None
        after_key = ''
        deferred = set()

        missing = Position.objects.filter(
            closure='MATE_PV', proof__isnull=True,
        ).filter(Q(won_line__isnull=True) | Q(won_line='')).order_by('key')
        for key in missing.values_list('key', flat=True):
            counts['MISSING'] += 1
            self.stderr.write(f'MISSING {key}: no won_line; left NULL')

        while limit is None or processed < limit:
            take = batch_size
            if limit is not None:
                take = min(take, limit - processed)
            if take <= 0:
                break
            pending = Position.objects.filter(
                closure='MATE_PV', proof__isnull=True,
                won_line__isnull=False,
            ).exclude(won_line='').annotate(
                witness_len=Length('won_line'),
            )
            if after_witness_len is not None:
                pending = pending.filter(
                    Q(witness_len__gt=after_witness_len)
                    | Q(witness_len=after_witness_len, key__gt=after_key)
                )
            rows = list(pending.order_by('witness_len', 'key').values(
                'key', 'fen', 'status', 'closure', 'won_line',
                'witness_len',
            )[:take])
            if not rows:
                break

            for snapshot in rows:
                after_witness_len = snapshot['witness_len']
                after_key = snapshot['key']
                # A concurrent edit can move a skipped row later in the
                # length ordering.  Advance past it without proving it twice
                # in this run; a fresh invocation retries every proof=NULL.
                if snapshot['key'] in deferred:
                    continue
                hint = (snapshot['won_line'] or '').split()
                winner_is_white = snapshot['status'] == 'WHITE_WIN'
                if snapshot['status'] not in ('WHITE_WIN', 'BLACK_WIN'):
                    deferred.add(snapshot['key'])
                    counts['ERROR'] += 1
                    self.stderr.write(
                        f"ERROR {snapshot['key']}: MATE_PV has status "
                        f"{snapshot['status']!r}")
                    continue
                if not hint:
                    # Defensive fallback; the query excludes known blanks.
                    deferred.add(snapshot['key'])
                    counts['MISSING'] += 1
                    continue
                try:
                    verdict = logic.prove_forced_mate(
                        snapshot['fen'], winner_is_white,
                        max_plies=len(hint) + 2,
                        budget_positions=budget,
                        hint_pv=hint,
                    )
                except Exception as exc:  # leave proof NULL for a later resume
                    deferred.add(snapshot['key'])
                    counts['ERROR'] += 1
                    self.stderr.write(
                        f"ERROR {snapshot['key']}: {type(exc).__name__}: {exc}")
                    continue

                proof = {
                    'PROVEN': 'ANDOR',
                    'INCONCLUSIVE': 'ENGINE',
                    'NO_MATE': 'DISPUTED',
                }[verdict]

                # The expensive proof runs outside the transaction.  Persist
                # it with one compare-and-swap UPDATE: unlike a SELECT followed
                # by UPDATE, this does not require SQLite to upgrade a stale
                # read snapshot while the live web process is also writing.
                with transaction.atomic():
                    updated = Position.objects.filter(
                        key=snapshot['key'],
                        proof__isnull=True,
                        closure=snapshot['closure'],
                        status=snapshot['status'],
                        fen=snapshot['fen'],
                        won_line=snapshot['won_line'],
                    ).update(proof=proof, updated=timezone.now())
                    if updated != 1:
                        deferred.add(snapshot['key'])
                        counts['SKIPPED'] += 1
                        continue
                    if proof == 'DISPUTED':
                        DBEvent.objects.create(
                            kind='MATE_PROOF_DISPUTED',
                            payload={
                                'key': snapshot['key'],
                                'status': snapshot['status'],
                                'closure': snapshot['closure'],
                                'max_plies': len(hint) + 2,
                            },
                        )

                counts[proof] += 1
                processed += 1

        # Re-run the idempotent confidence backup from every classified mate.
        # Doing this as a final phase also repairs ancestors after an earlier
        # command was interrupted between classification and propagation.
        proof_seeds = list(Position.objects.filter(
            closure='MATE_PV', proof__in=('ANDOR', 'ENGINE', 'DISPUTED'),
        ).values_list('key', flat=True))
        if proof_seeds:
            ingest.backup_cascade(proof_seeds)

        self.stdout.write(
            'verify_mates run: '
            + ' '.join(f'{name}={value}' for name, value in counts.items()))

        mate_positions = Position.objects.filter(closure='MATE_PV')
        totals = {
            proof: mate_positions.filter(proof=proof).count()
            for proof in ('ANDOR', 'ENGINE', 'DISPUTED')
        }
        totals['UNCLASSIFIED'] = mate_positions.filter(proof__isnull=True).count()
        self.stdout.write(
            'verify_mates totals: '
            + ' '.join(f'{name}={value}' for name, value in totals.items()))

        disputed = mate_positions.filter(proof='DISPUTED').order_by('key') \
            .values_list('key', 'fen')
        if disputed.exists():
            self.stdout.write('DISPUTED positions (not reverted):')
            for key, fen in disputed:
                self.stdout.write(f'  {key} {fen}')
