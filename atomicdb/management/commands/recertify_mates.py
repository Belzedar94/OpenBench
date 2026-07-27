"""Re-certify the AtomicDB mate closures that were never proven exhaustively.

``verify_mates`` only looks at ``proof IS NULL`` rows, so the closures that it
already downgraded to ``ENGINE`` (its budget ran out) are never revisited: an
uncertified witness stays live in the exact cascade forever.  This command is
the second pass for exactly those, with a much larger, witness-length-tiered
budget, and — the part that was missing from the whole system — it REVOKES what
it refutes instead of leaving a contradicted closure in place.

WHAT IT PROCESSES

* ``closure='MATE_PV'`` with ``proof='ENGINE'`` — a legal terminal PV whose
  exhaustive AND/OR certificate was never produced.
* with ``--include-missing``, ``closure='MATE_PV'`` with ``proof IS NULL`` and
  no ``won_line``: the historical rows that predate the witness column.  Their
  PV is re-derived from the position's own ``last_analysis`` when it carries a
  mate line that still verifies move by move.  A row with no recoverable
  witness cannot be certified by any budget; it is reported, and revoked when
  ``--revoke-unwitnessed`` says so.

BUDGETS

Tiered by witness length, because the cost of an exhaustive proof explodes with
depth and a 25-ply witness deserves ten times what a 5-ply one needs:
``RECERTIFY_SHORT_BUDGET`` positions up to ``RECERTIFY_SHORT_PLIES`` plies of
witness, ``RECERTIFY_LONG_BUDGET`` beyond.  ``--budget-positions`` overrides
both with a single flat value.

RESUMABILITY

Every attempt writes a ``MATE_RECERTIFIED`` DBEvent carrying the budget it
spent.  A later run skips a key that already got at least the budget it is
about to spend, so an interrupted overnight pass resumes where it stopped
instead of re-proving the cheap half of the table.  ``--force`` ignores that
history.  The persistence itself is the same compare-and-swap UPDATE
``verify_mates`` uses: the expensive proof runs outside any transaction, and a
row someone else edited meanwhile is skipped rather than overwritten.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.db.models.functions import Length
from django.utils import timezone

from atomicdb import ingest, logic
from atomicdb.database import atomic, connection
from atomicdb.models import DBEvent, Position

RECERTIFY_SHORT_PLIES = 20
RECERTIFY_SHORT_BUDGET = 2_000_000
RECERTIFY_LONG_BUDGET = 20_000_000


def budget_for_witness(witness_plies, override=None):
    """Escalon de presupuesto por longitud del testigo."""
    if override is not None:
        return int(override)
    return (RECERTIFY_SHORT_BUDGET if witness_plies <= RECERTIFY_SHORT_PLIES
            else RECERTIFY_LONG_BUDGET)


def recover_witness(fen, status, last_analysis):
    """Testigo recuperable del propio analisis guardado, o ``None``.

    ``last_analysis`` es el MultiPV crudo de ESTA posicion, asi que una linea
    suya con score de mate es una PV desde aqui.  Solo cuenta si vuelve a
    verificarse jugada a jugada con nuestro movegen y acaba ganando el bando
    que el cierre declara: un JSON viejo no es evidencia por si mismo.
    """
    if not isinstance(last_analysis, list):
        return None
    winner_is_white = status == 'WHITE_WIN'
    candidates = []
    for line in last_analysis:
        if not isinstance(line, dict):
            continue
        mate, pv = line.get('mate'), line.get('pv')
        if mate is None or not isinstance(pv, list) or not pv:
            continue
        if (mate > 0) != winner_is_white:
            continue
        if any(not isinstance(uci, str) or not uci for uci in pv):
            continue
        candidates.append(pv)
    for pv in sorted(candidates, key=len):
        try:
            if logic.verify_mate_pv(fen, pv, winner_is_white):
                return list(pv)
        except Exception:
            continue
    return None


class Command(BaseCommand):
    help = ('Re-certify ENGINE (and optionally unwitnessed) MATE_PV closures '
            'with a larger tiered budget, revoking what it refutes.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--budget-positions', type=int, default=None,
            help='Flat budget for every witness, overriding the length tiers.')
        parser.add_argument(
            '--batch-size', type=int, default=100,
            help='Number of keys fetched between resumable checkpoints.')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Stop after this many processed positions.')
        parser.add_argument(
            '--include-missing', action='store_true',
            help='Also process MATE_PV closures that carry no witness.')
        parser.add_argument(
            '--revoke-unwitnessed', action='store_true',
            help='Revoke unwitnessed MATE_PV closures instead of reporting '
                 'them (implies --include-missing).')
        parser.add_argument(
            '--force', action='store_true',
            help='Ignore previous MATE_RECERTIFIED events and redo everything.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would happen without writing anything.')

    def handle(self, *args, **options):
        override = options['budget_positions']
        batch_size = options['batch_size']
        limit = options['limit']
        force = options['force']
        dry_run = options['dry_run']
        revoke_unwitnessed = options['revoke_unwitnessed']
        include_missing = options['include_missing'] or revoke_unwitnessed
        if override is not None and override < 0:
            raise ValueError('--budget-positions must be non-negative')
        if batch_size <= 0:
            raise ValueError('--batch-size must be positive')
        if limit is not None and limit < 0:
            raise ValueError('--limit must be non-negative')

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        counts = {'ANDOR': 0, 'ENGINE': 0, 'REVOKED': 0, 'RECOVERED': 0,
                  'UNWITNESSED': 0, 'SKIPPED': 0, 'ERROR': 0}
        revoked_nodes = 0
        processed = 0
        after_witness_len, after_key = None, ''
        deferred = set()

        while limit is None or processed < limit:
            take = batch_size
            if limit is not None:
                take = min(take, limit - processed)
            if take <= 0:
                break
            rows = self._next_batch(after_witness_len, after_key, take)
            if not rows:
                break
            for snapshot in rows:
                after_witness_len = snapshot['witness_len']
                after_key = snapshot['key']
                if snapshot['key'] in deferred:
                    continue
                outcome, nodes = self._process(
                    snapshot, override, force, dry_run, revoke_unwitnessed,
                    deferred, counts)
                revoked_nodes += nodes
                if outcome is not None:
                    processed += 1

        if include_missing and (limit is None or processed < limit):
            for snapshot in self._unwitnessed():
                if limit is not None and processed >= limit:
                    break
                nodes = self._process_unwitnessed(
                    snapshot, override, force, dry_run, revoke_unwitnessed,
                    counts)
                revoked_nodes += nodes
                processed += 1

        self.stdout.write(
            'recertify_mates run: '
            + ' '.join(f'{name}={value}' for name, value in counts.items())
            + f' revoked_nodes={revoked_nodes}')

        mate_positions = Position.objects.filter(closure='MATE_PV')
        totals = {proof: mate_positions.filter(proof=proof).count()
                  for proof in ('ANDOR', 'ENGINE', 'DISPUTED')}
        totals['UNCLASSIFIED'] = mate_positions.filter(
            proof__isnull=True).count()
        self.stdout.write(
            'recertify_mates totals: '
            + ' '.join(f'{name}={value}' for name, value in totals.items()))
        self.stdout.write(
            f'recertify_mates cursor: witness_len={after_witness_len} '
            f'key={after_key or "-"}')

    # ---------------- selection ----------------

    def _next_batch(self, after_witness_len, after_key, take):
        pending = Position.objects.filter(
            closure='MATE_PV', proof='ENGINE',
            won_line__isnull=False,
        ).exclude(won_line='').annotate(witness_len=Length('won_line'))
        if after_witness_len is not None:
            pending = pending.filter(
                Q(witness_len__gt=after_witness_len)
                | Q(witness_len=after_witness_len, key__gt=after_key))
        return list(pending.order_by('witness_len', 'key').values(
            'key', 'fen', 'status', 'closure', 'proof', 'won_line',
            'witness_len')[:take])

    def _unwitnessed(self):
        return list(Position.objects.filter(
            closure='MATE_PV', proof__isnull=True,
        ).filter(Q(won_line__isnull=True) | Q(won_line=''))
            .order_by('key')
            .values('key', 'fen', 'status', 'closure', 'last_analysis'))

    def _already_certified_budget(self, key):
        spent = 0
        for payload in DBEvent.objects.filter(
                kind='MATE_RECERTIFIED', payload__key=key,
        ).values_list('payload', flat=True):
            try:
                spent = max(spent, int(payload.get('budget') or 0))
            except (AttributeError, TypeError, ValueError):
                continue
        return spent

    # ---------------- one position ----------------

    def _process(self, snapshot, override, force, dry_run, revoke_unwitnessed,
                 deferred, counts):
        del revoke_unwitnessed
        hint = (snapshot['won_line'] or '').split()
        if snapshot['status'] not in ('WHITE_WIN', 'BLACK_WIN'):
            deferred.add(snapshot['key'])
            counts['ERROR'] += 1
            self.stderr.write(
                f"ERROR {snapshot['key']}: MATE_PV has status "
                f"{snapshot['status']!r}")
            return None, 0
        budget = budget_for_witness(len(hint), override)
        if not force and self._already_certified_budget(snapshot['key']) \
                >= budget:
            deferred.add(snapshot['key'])
            counts['SKIPPED'] += 1
            return None, 0
        return self._prove_and_persist(
            snapshot, hint, budget, dry_run, deferred, counts,
            recovered=False)

    def _process_unwitnessed(self, snapshot, override, force, dry_run,
                             revoke_unwitnessed, counts):
        recovered = recover_witness(
            snapshot['fen'], snapshot['status'], snapshot['last_analysis'])
        if recovered is None:
            counts['UNWITNESSED'] += 1
            self.stderr.write(
                f"UNWITNESSED {snapshot['key']}: no recoverable mate PV")
            if not revoke_unwitnessed or dry_run:
                return 0
            outcome = ingest.revoke_closure(
                snapshot['key'], reason='mate-witness-unrecoverable')
            counts['REVOKED'] += 1
            return len(outcome['revoked'])
        counts['RECOVERED'] += 1
        budget = budget_for_witness(len(recovered), override)
        if not force and self._already_certified_budget(snapshot['key']) \
                >= budget:
            counts['SKIPPED'] += 1
            return 0
        snapshot = dict(snapshot, proof=None, won_line=None)
        _, nodes = self._prove_and_persist(
            snapshot, recovered, budget, dry_run, set(), counts,
            recovered=True)
        return nodes

    def _prove_and_persist(self, snapshot, hint, budget, dry_run, deferred,
                           counts, recovered):
        winner_is_white = snapshot['status'] == 'WHITE_WIN'
        try:
            verdict = logic.prove_forced_mate(
                snapshot['fen'], winner_is_white,
                max_plies=len(hint) + 2, budget_positions=budget,
                hint_pv=hint)
        except Exception as exc:
            deferred.add(snapshot['key'])
            counts['ERROR'] += 1
            self.stderr.write(
                f"ERROR {snapshot['key']}: {type(exc).__name__}: {exc}")
            return None, 0

        if dry_run:
            counts[{'PROVEN': 'ANDOR', 'INCONCLUSIVE': 'ENGINE',
                    'NO_MATE': 'REVOKED'}[verdict]] += 1
            return verdict, 0

        if verdict == 'NO_MATE':
            with atomic():
                # Same compare-and-swap discipline: only refute the exact row
                # the proof was run against.
                matched = Position.objects.filter(
                    key=snapshot['key'], closure=snapshot['closure'],
                    status=snapshot['status'], fen=snapshot['fen'],
                    proof=snapshot['proof']).exists()
                if not matched:
                    deferred.add(snapshot['key'])
                    counts['SKIPPED'] += 1
                    return None, 0
                DBEvent.objects.create(kind='MATE_PROOF_DISPUTED', payload={
                    'key': snapshot['key'], 'status': snapshot['status'],
                    'closure': snapshot['closure'],
                    'max_plies': len(hint) + 2, 'budget': budget,
                    'source': 'recertify_mates'})
                outcome = ingest.revoke_closure(
                    snapshot['key'], reason='mate-witness-refuted',
                    mark_disputed=True)
                DBEvent.objects.create(kind='MATE_RECERTIFIED', payload={
                    'key': snapshot['key'], 'budget': budget,
                    'verdict': verdict})
            counts['REVOKED'] += 1
            self.stdout.write(
                f"REVOKED {snapshot['key']}: "
                f"{len(outcome['revoked'])} node(s) reopened")
            return verdict, len(outcome['revoked'])

        proof = 'ANDOR' if verdict == 'PROVEN' else 'ENGINE'
        with atomic():
            updates = {'proof': proof, 'updated': timezone.now()}
            if recovered:
                updates['won_line'] = ' '.join(hint)
                updates['mate_in'] = len(hint)
            updated = Position.objects.filter(
                key=snapshot['key'], closure=snapshot['closure'],
                status=snapshot['status'], fen=snapshot['fen'],
                proof=snapshot['proof'],
            ).update(**updates)
            if updated != 1:
                deferred.add(snapshot['key'])
                counts['SKIPPED'] += 1
                return None, 0
            DBEvent.objects.create(kind='MATE_RECERTIFIED', payload={
                'key': snapshot['key'], 'budget': budget, 'verdict': verdict})
        if proof == 'ANDOR':
            # A newly verified witness upgrades the assurance its ancestors
            # inherited; the idempotent cascade is what publishes that.
            ingest.backup_cascade([snapshot['key']])
        counts[proof] += 1
        return verdict, 0
