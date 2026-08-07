"""Capture one append-only, cumulative AtomicDB progress observation."""

import json
from datetime import timezone as datetime_timezone

from django.core.management.base import BaseCommand
from django.db.models import BigIntegerField, Case, Count, F, Q, Sum, Value, When
from django.utils import timezone

from atomicdb import ingest, proof
from atomicdb.database import atomic
from atomicdb.metrics import (closure_attribution_totals,
                              human_close_latency, worker_metrics)
from atomicdb.models import AnalysisTask, DBEvent, Position, ProgressSnapshot


UTC = datetime_timezone.utc


def utc_hour(value):
    """Return ``value`` normalized to the beginning of its UTC hour."""
    if timezone.is_naive(value):
        raise ValueError('progress capture requires an aware datetime')
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _snapshot_values(observed_at):
    closed_filter = ~Q(status='UNKNOWN')
    positions = Position.objects.aggregate(
        positions_total=Count('key'),
        positions_unknown=Count('key', filter=Q(status='UNKNOWN')),
        positions_closed=Count('key', filter=closed_filter),
        positions_expanded=Count('key', filter=Q(expanded=True)),
        closure_terminal=Count(
            'key', filter=closed_filter & Q(closure='TERMINAL')),
        closure_tb=Count('key', filter=closed_filter & Q(closure='TB')),
        closure_mate_pv=Count(
            'key', filter=closed_filter & Q(closure='MATE_PV')),
        closure_minimax=Count(
            'key', filter=closed_filter & Q(closure='MINIMAX')),
        trust_verified=Count(
            'key', filter=closed_filter & Q(closure__in=('TERMINAL', 'TB'))),
        trust_andor=Count(
            'key',
            filter=(closed_filter
                    & ~Q(closure__in=('TERMINAL', 'TB'))
                    & Q(proof='ANDOR'))),
        trust_engine=Count(
            'key',
            filter=(closed_filter
                    & ~Q(closure__in=('TERMINAL', 'TB'))
                    & Q(proof='ENGINE'))),
        trust_disputed=Count(
            'key',
            filter=(closed_filter
                    & ~Q(closure__in=('TERMINAL', 'TB'))
                    & Q(proof='DISPUTED'))),
    )
    positions['closure_unclassified'] = max(
        0,
        positions['positions_closed']
        - positions['closure_terminal']
        - positions['closure_tb']
        - positions['closure_mate_pv']
        - positions['closure_minimax'],
    )
    positions['trust_unclassified'] = max(
        0,
        positions['positions_closed']
        - positions['trust_verified']
        - positions['trust_andor']
        - positions['trust_engine']
        - positions['trust_disputed'],
    )

    retry_excess = Case(
        When(attempts__gt=1, then=F('attempts') - Value(1)),
        default=Value(0),
        output_field=BigIntegerField(),
    )
    tasks = AnalysisTask.objects.aggregate(
        engine_nodes_total=Sum(
            'nodes_searched', filter=Q(state='COMPLETED')),
        engine_seconds_total=Sum(
            'elapsed_seconds', filter=Q(state='COMPLETED')),
        analyses_completed=Count('id', filter=Q(state='COMPLETED')),
        tasks_pending=Count('id', filter=Q(state='PENDING')),
        tasks_leased=Count('id', filter=Q(state='LEASED')),
        tasks_retried=Count('id', filter=Q(attempts__gt=1)),
        lease_retries_total=Sum(retry_excess),
    )
    for field in (
            'engine_nodes_total', 'engine_seconds_total',
            'lease_retries_total'):
        tasks[field] = tasks[field] or 0

    live = worker_metrics(now=observed_at)
    # Observed, like everything else here: the numbers as they stand at
    # capture time, never reconstructed.  A tree with no campaign records
    # zeroes rather than skipping the bucket.
    root_pn, root_dn = proof.headline_numbers()
    # The one measurement that cannot wait for a later reader: ProofNode rows
    # are overwritten in place by every cascade, so this hour's dn
    # distribution stops existing the moment the hour does.
    frontier = proof.frontier_dn_headline(ingest.DN_REPAIR_FLOOR) or {}
    attributed = closure_attribution_totals()
    latency = human_close_latency(now=observed_at)
    return {
        **positions,
        **tasks,
        'recorded_rejections_total': DBEvent.objects.filter(
            kind='TB_REJECTED').count(),
        'active_workers': live['workers'],
        'active_threads': live['cores'],
        'active_nps': live['nps'],
        'root_pn': 0 if root_pn is None else int(root_pn),
        'root_dn': 0 if root_dn is None else int(root_dn),
        'frontier_and_nodes': frontier.get('and_nodes', 0),
        'frontier_dn_median': frontier.get('dn_median', 0),
        'frontier_dn_thin': frontier.get('thin', 0),
        'frontier_saturated': frontier.get('saturated', 0),
        'closures_user': attributed['USER'],
        'closures_fill': attributed['FILL'],
        'closures_auto': attributed['AUTO'],
        'closures_solve': attributed['SOLVE'],
        'closures_none': attributed['NONE'],
        'human_close_median_seconds': latency['median_seconds'],
        'human_close_samples': latency['samples'],
    }


@atomic()
def capture_progress(*, now=None):
    """Capture the current hour once; return ``(snapshot, created)``.

    An existing bucket is returned without recomputing or mutating it.  The
    optional ``now`` exists for deterministic tests; the management command
    always captures the real current instant.
    """
    observed_at = now or timezone.now()
    bucket = utc_hour(observed_at)
    existing = ProgressSnapshot.objects.filter(bucket_start=bucket).first()
    if existing is not None:
        return existing, False
    return ProgressSnapshot.objects.get_or_create(
        bucket_start=bucket,
        defaults=_snapshot_values(observed_at),
    )


def _payload(snapshot, created):
    fields = {
        field.name: getattr(snapshot, field.name)
        for field in ProgressSnapshot._meta.concrete_fields
        if field.name != 'id'
    }
    fields['bucket_start'] = snapshot.bucket_start.isoformat()
    fields['captured_at'] = snapshot.captured_at.isoformat()
    return {'created': created, 'snapshot': fields}


class Command(BaseCommand):
    help = (
        'Capture the current AtomicDB counters in one immutable UTC-hour '
        'bucket. Re-running within the hour is a no-op.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--json', action='store_true',
            help='Emit a stable JSON receipt instead of a one-line summary.',
        )
        parser.add_argument(
            '--preview', action='store_true',
            help='Compute the counters and print them WITHOUT writing a row. '
                 'For reading the current numbers when this hour is already '
                 'captured; it is not a backfill and cannot become one.',
        )

    def handle(self, *args, **options):
        if options['preview']:
            # Escribe NADA.  Existe porque una hora ya capturada no se
            # reescribe — es la garantia de la tabla — y justo despues de una
            # migracion que anade columnas hace falta poder LEER lo que esa
            # fila no llego a guardar, sin romper esa garantia.
            self.stdout.write(json.dumps(
                {'preview': True,
                 'snapshot': _snapshot_values(timezone.now())},
                sort_keys=True, separators=(',', ':'), default=str))
            return
        snapshot, created = capture_progress()
        payload = _payload(snapshot, created)
        if options['json']:
            self.stdout.write(json.dumps(
                payload, sort_keys=True, separators=(',', ':')))
            return
        action = 'created' if created else 'reused'
        self.stdout.write(self.style.SUCCESS(
            f'{action} UTC bucket {snapshot.bucket_start.isoformat()} '
            f'positions={snapshot.positions_total} '
            f'closed={snapshot.positions_closed} '
            f'analyses={snapshot.analyses_completed}'))
