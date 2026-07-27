"""Fill ``clock_slack`` on the decisive closures that predate P1b.

Every decisive closure in the live database was written from a canonical FEN
whose fifty-move counter is zero, with no record of for which OTHER entry
clocks the proof survives.  Until that column is filled, the fresh-context
rule reads NULL as zero and refuses to let a quiet edge carry a closure
upwards — which is safe (it closes less) but stops progress.  This is the one
pass that fixes it.

WHAT EACH KIND GETS

* ``TERMINAL`` — the maximum.  A terminal position is terminal at any clock;
  mate and explosion take precedence over the fifty-move adjudication.
* ``MATE_PV`` with ``proof='ANDOR'`` — a real exhaustive re-proof measuring
  the worst reversible run of the proof tree.  Expensive, and only for these:
  they are the ones whose slack can be known exactly.
* ``MATE_PV`` otherwise — the crude ``100 - len(witness)`` bound.  An
  uncertified witness says nothing about where its captures are, so assuming
  every one of its plies was reversible is the only defensible reading.
  ``recertify_mates`` replaces it with the real figure when it certifies.
* ``TB`` — zero unless the closure event recorded a DTZ.  A bare WDL assumes
  the position was reached right after a reset, so it only speaks for clock
  zero.
* ``MINIMAX`` — recomputed from the children, bottom up, by the ordinary
  cascade.  This command seeds the leaves and then lets ``backup_cascade`` do
  what it already knows how to do.
* Draws get nothing: raising the clock only degrades wins towards draws, never
  the other way round, so a draw needs no slack.

Resumable in batches by key, and idempotent: a second pass writes nothing.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from atomicdb import ingest, logic
from atomicdb.database import atomic, connection
from atomicdb.models import DBEvent, Position

DECISIVE = ('WHITE_WIN', 'BLACK_WIN')


class Command(BaseCommand):
    help = 'Backfill clock_slack on decisive closures (resumable, batched).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=500,
            help='Rows fetched per resumable checkpoint (default: 500).')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Stop after this many rows.')
        parser.add_argument(
            '--reprove', action='store_true',
            help='Re-run the exhaustive proof on ANDOR mates to measure the '
                 'real reversible run instead of using the witness bound.')
        parser.add_argument(
            '--budget-positions', type=int, default=200_000,
            help='Proof budget per ANDOR mate when --reprove is given.')
        parser.add_argument(
            '--cascade', action='store_true',
            help='Run the ordinary backup cascade afterwards so MINIMAX '
                 'closures inherit the slack the leaves just gained.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing.')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        limit = options['limit']
        if batch_size <= 0:
            raise ValueError('--batch-size must be positive')

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        counts = {'TERMINAL': 0, 'MATE_PV': 0, 'REPROVEN': 0, 'TB': 0,
                  'MINIMAX': 0, 'UNCHANGED': 0}
        dtz_by_key = self._recorded_dtz()
        after_key, processed, seeds = '', 0, []

        while limit is None or processed < limit:
            take = batch_size
            if limit is not None:
                take = min(take, limit - processed)
            if take <= 0:
                break
            rows = list(Position.objects.filter(
                status__in=DECISIVE, key__gt=after_key,
            ).exclude(closure__isnull=True).order_by('key').values(
                'key', 'fen', 'status', 'closure', 'proof', 'won_line',
                'clock_slack')[:take])
            if not rows:
                break
            for row in rows:
                after_key = row['key']
                processed += 1
                slack, label = self._slack_for(row, dtz_by_key, options)
                if slack is None or slack == row['clock_slack']:
                    counts['UNCHANGED'] += 1
                    continue
                counts[label] += 1
                if options['dry_run']:
                    continue
                with atomic():
                    Position.objects.filter(
                        key=row['key'], status=row['status'],
                        closure=row['closure'],
                    ).update(clock_slack=slack, updated=timezone.now())
                seeds.append(row['key'])

        if options['cascade'] and seeds and not options['dry_run']:
            # The cascade is what turns leaf slack into MINIMAX slack; it is
            # idempotent, so running it on the whole seed set is safe.
            for start in range(0, len(seeds), 500):
                ingest.backup_cascade(seeds[start:start + 500])

        if not options['dry_run']:
            DBEvent.objects.create(kind='CLOCK_SLACK_BACKFILL', payload={
                'ruleset': logic.RULESET_ID, 'processed': processed,
                **counts})
        self.stdout.write(
            'backfill_clock_slack: '
            + ' '.join(f'{name}={value}' for name, value in counts.items())
            + f' processed={processed} cursor={after_key or "-"}')

    def _recorded_dtz(self):
        """DTZ claims the TB closure events recorded, keyed by position."""
        found = {}
        for payload in DBEvent.objects.filter(
                kind='NODE_CLOSED', payload__closure='TB',
        ).values_list('payload', flat=True):
            try:
                key, dtz = payload.get('key'), payload.get('dtz')
            except AttributeError:
                continue
            if key and dtz is not None:
                found[key] = dtz
        return found

    def _slack_for(self, row, dtz_by_key, options):
        closure = row['closure']
        if closure == 'TERMINAL':
            return logic.CLOCK_SLACK_MAX, 'TERMINAL'
        if closure == 'TB':
            return logic.slack_from_dtz(dtz_by_key.get(row['key'])), 'TB'
        if closure == 'MATE_PV':
            witness = (row['won_line'] or '').split()
            if options['reprove'] and row['proof'] == 'ANDOR' and witness:
                verdict, run = logic.prove_forced_mate(
                    row['fen'], row['status'] == 'WHITE_WIN',
                    max_plies=len(witness) + 2,
                    budget_positions=options['budget_positions'],
                    hint_pv=witness, return_run=True)
                if verdict == 'PROVEN' and run is not None:
                    return logic.slack_from_run(run), 'REPROVEN'
            return logic.slack_from_witness_length(len(witness)), 'MATE_PV'
        # MINIMAX and anything unclassified: the cascade owns it.
        return None, 'MINIMAX'
