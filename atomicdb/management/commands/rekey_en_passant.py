"""Re-key the positions whose identity carried a phantom en-passant right.

WHAT CHANGED UNDER IT.  ``logic.CANONICAL_VERSION`` went from 1 to 2: the
en-passant square stays in the key only when the capture is actually legal.
The move generator emits it for pseudo-legal-but-illegal captures too — the
capture would explode the capturing side's own king, or the capturing pawn is
pinned — and those positions are the SAME position as their twin without the
right.  New rows already get the new key; this is the pass that moves the old
ones.

WHY IT IS WORTH MOVING PRIMARY KEYS FOR.  Two rows for one position lose
transpositions, which is merely wasteful.  The real cost is that the branch
repetition guard in ``prove_forced_mate`` compares these keys: with two keys
for one board, a genuine repetition goes unseen and a line that is really a
draw can be certified as won.  That is a soundness failure, and it is the
reason this is not filed under tidying.

TWO OUTCOMES PER ROW.  If the new key is free, the row MOVES: a new row is
written, every foreign key is repointed, the old row is deleted.  If the new
key is taken — the twin that never had the phantom — the two rows MERGE, and
the surviving row keeps the better-informed value of each field while the
counters are summed.

REFUSALS.  A merge whose two rows carry DIFFERENT proved statuses is not
merged.  Two rows for one position cannot both be right, so the honest move is
to leave them alone, record the conflict and let a human look; silently
picking one would be inventing an answer.

Resumable and idempotent: each row is its own transaction, and a row that has
already moved no longer carries a phantom, so a second run skips it.
"""

import json

from django.core.management.base import BaseCommand
from django.db.models import Max

from atomicdb import ingest, logic, proof
from atomicdb.database import atomic, connection
from atomicdb.models import (AnalysisTask, Campaign, DBEvent, Edge, IngestJob,
                             OpeningNameSuggestion, Position, ProofCampaign,
                             ProofNode, RequestLog, SolveTask)

# Every model that points at a Position, with the plain-repoint ones first.
SIMPLE_REFERENCES = (
    (IngestJob, 'position_id'),
    (RequestLog, 'position_id'),
    (OpeningNameSuggestion, 'position_id'),
    (SolveTask, 'position_id'),
    (Campaign, 'root_id'),
    (ProofCampaign, 'root_id'),
)
# Fields whose truth travels together: they are all justified by the status.
EXACT_FIELDS = ('status', 'closure', 'proof', 'won_line', 'mate_in',
                'clock_slack')


class Command(BaseCommand):
    help = ('Move positions whose key carried a phantom en-passant right to '
            'their canonical v2 key, merging with an existing twin.')

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=500,
                            help='Rows read per resumable checkpoint.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Stop after this many re-keyed rows.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would move without writing.')
        parser.add_argument('--json', action='store_true',
                            help='Machine-readable summary.')

    def handle(self, *args, **options):
        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        counts = {'scanned': 0, 'with_ep': 0, 'moved': 0, 'merged': 0,
                  'conflicts': 0, 'unchanged': 0}
        touched, after_key = [], ''
        while True:
            rows = list(Position.objects.filter(key__gt=after_key)
                        .order_by('key')
                        .values_list('key', 'fen')[:options['batch_size']])
            if not rows:
                break
            for key, fen in rows:
                after_key = key
                counts['scanned'] += 1
                parts = fen.split()
                if len(parts) < 4 or parts[3] == '-':
                    continue
                counts['with_ep'] += 1
                new_fen = logic.canonical_fen(fen)
                if new_fen == fen:
                    counts['unchanged'] += 1
                    continue
                new_key = logic.key_of(new_fen)
                if options['dry_run']:
                    exists = Position.objects.filter(key=new_key).exists()
                    counts['merged' if exists else 'moved'] += 1
                    self.stdout.write(
                        f'would {"merge" if exists else "move"} '
                        f'{key[:16]} -> {new_key[:16]}  ep={parts[3]}')
                    continue
                outcome = self._rekey(key, new_key, new_fen)
                counts[outcome] += 1
                if outcome in ('moved', 'merged'):
                    touched.append(new_key)
                if (options['limit'] is not None
                        and counts['moved'] + counts['merged']
                        >= options['limit']):
                    rows = []
                    break
            if options['limit'] is not None and \
                    counts['moved'] + counts['merged'] >= options['limit']:
                break

        if touched and not options['dry_run']:
            # A merged row inherited two histories; the derived values are
            # recomputed rather than guessed at.
            ingest.backup_cascade(touched)
            ingest.backup_backed_evals(touched)
            proof.refresh_proof_numbers(touched)

        if options['json']:
            self.stdout.write(json.dumps(
                {'canonical_version': logic.CANONICAL_VERSION, **counts},
                sort_keys=True))
            return
        self.stdout.write(
            f'rekey_en_passant (canonical v{logic.CANONICAL_VERSION}): '
            + ' '.join(f'{name}={value}' for name, value in counts.items()))
        if counts['conflicts']:
            self.stdout.write(
                'CONFLICTS were left untouched; see the REKEY_CONFLICT events.')

    # ---------------- one row ----------------

    def _rekey(self, old_key, new_key, new_fen):
        with atomic():
            old = Position.objects.select_for_update().get(key=old_key)
            target = Position.objects.select_for_update().filter(
                key=new_key).first()
            if target is None:
                self._move(old, new_key, new_fen)
                DBEvent.objects.create(kind='REKEYED', payload={
                    'old': old_key, 'new': new_key, 'merged': False,
                    'canonical_version': logic.CANONICAL_VERSION})
                return 'moved'

            if (old.status != 'UNKNOWN' and target.status != 'UNKNOWN'
                    and old.status != target.status):
                DBEvent.objects.create(kind='REKEY_CONFLICT', payload={
                    'old': old_key, 'new': new_key,
                    'old_status': old.status, 'new_status': target.status,
                    'old_closure': old.closure, 'new_closure': target.closure})
                return 'conflicts'

            self._merge_fields(target, old)
            target.save()
            self._repoint(old, target, merging=True)
            old.delete()
            DBEvent.objects.create(kind='REKEYED', payload={
                'old': old_key, 'new': new_key, 'merged': True,
                'canonical_version': logic.CANONICAL_VERSION})
            return 'merged'

    def _move(self, old, new_key, new_fen):
        """No twin: write the row under its new key and repoint everything."""
        fields = {field.attname: getattr(old, field.attname)
                  for field in Position._meta.fields
                  if field.attname not in ('key', 'updated')}
        fields['key'] = new_key
        fields['fen'] = new_fen
        target = Position.objects.create(**fields)
        self._repoint(old, target, merging=False)
        old.delete()
        return target

    # ---------------- field merge ----------------

    def _merge_fields(self, target, old):
        """Keep the better-informed value of each field on the survivor."""
        if target.status == 'UNKNOWN' and old.status != 'UNKNOWN':
            # The exact fields travel with the status that justifies them.
            for name in EXACT_FIELDS:
                setattr(target, name, getattr(old, name))
            if old.best_move:
                target.best_move = old.best_move

        # The eval belongs to the better funded search.
        if old.eval_cp is not None and (
                target.eval_cp is None
                or (old.nodes_invested or 0) > (target.nodes_invested or 0)):
            target.eval_cp = old.eval_cp
        if old.last_analysis is not None and (
                target.last_analysis is None
                or (old.visits or 0) > (target.visits or 0)):
            target.last_analysis = old.last_analysis
        if not target.best_move and old.best_move:
            target.best_move = old.best_move

        # Effort is additive: both rows really were searched.
        target.visits = (target.visits or 0) + (old.visits or 0)
        target.nodes_invested = ((target.nodes_invested or 0)
                                 + (old.nodes_invested or 0))
        target.time_invested = ((target.time_invested or 0.0)
                                + (old.time_invested or 0.0))
        target.depth_invested = max(target.depth_invested or 0,
                                    old.depth_invested or 0)
        # Same position, so either complete legal list covers the union.
        target.expanded = bool(target.expanded or old.expanded)
        target.priority = max(target.priority, old.priority)
        target.campaign_id = target.campaign_id or old.campaign_id
        # backed_* are derived; the caller recomputes them from the survivor.

    # ---------------- foreign keys ----------------

    def _repoint(self, old, target, merging):
        self._repoint_edges(old, target, merging)
        self._repoint_tasks(old, target, merging)
        self._repoint_proof_nodes(old, target, merging)
        for model, attname in SIMPLE_REFERENCES:
            model.objects.filter(**{attname: old.key}).update(
                **{attname: target.key})

    def _repoint_edges(self, old, target, merging):
        # Outgoing: (parent, move_uci) is unique, so a move the survivor
        # already has is the SAME edge of the SAME position — drop the copy.
        if merging:
            taken = set(Edge.objects.filter(parent=target)
                        .values_list('move_uci', flat=True))
            Edge.objects.filter(parent=old, move_uci__in=taken).delete()
        Edge.objects.filter(parent=old).update(parent_id=target.key)
        # Incoming: changing the child cannot collide, the unique key is on
        # (parent, move_uci).  It must happen before the delete because
        # Edge.child is PROTECT.
        Edge.objects.filter(child=old).update(child_id=target.key)

    def _repoint_tasks(self, old, target, merging):
        tasks = list(AnalysisTask.objects.filter(position=old)
                     .order_by('generation', 'id'))
        if not tasks:
            return
        if not merging:
            AnalysisTask.objects.filter(position=old).update(
                position_id=target.key)
            return
        used = set(AnalysisTask.objects.filter(position=target)
                   .values_list('generation', flat=True))
        nxt = (AnalysisTask.objects.filter(position=target).aggregate(
            top=Max('generation'))['top'] or 0) + 1
        for task in tasks:
            generation = task.generation
            if generation in used:
                # Same position, two histories: the visit numbering is
                # local bookkeeping, so renumber rather than lose the task.
                while nxt in used:
                    nxt += 1
                generation = nxt
            used.add(generation)
            AnalysisTask.objects.filter(pk=task.pk).update(
                position_id=target.key, generation=generation)
        IngestJob.objects.filter(position=old).update(position_id=target.key)

    def _repoint_proof_nodes(self, old, target, merging):
        if merging:
            # (campaign, position) is unique and pn/dn are recomputed from the
            # survivor anyway, so a duplicate node is dropped, not merged.
            covered = set(ProofNode.objects.filter(position=target)
                          .values_list('campaign_id', flat=True))
            ProofNode.objects.filter(position=old,
                                     campaign_id__in=covered).delete()
        ProofNode.objects.filter(position=old).update(position_id=target.key)
