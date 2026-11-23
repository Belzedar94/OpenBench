"""Capture one append-only, cumulative AtomicDB progress observation.

The same hourly run records the main line from the start position and, where
no database trigger keeps them, folds in the root extremes (``roots``).
"""

import json
from datetime import timedelta, timezone as datetime_timezone

from django.core.management.base import BaseCommand
from django.db.models import BigIntegerField, Case, Count, F, Q, Sum, Value, When
from django.utils import timezone

from atomicdb import ingest, proof, roots
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


# EL DESGLOSE COMPLETO, UNA VEZ AL DIA (23-nov-2026).  El aggregate de abajo
# recorre ``atomicdb_position`` entera: a 160M filas (89 GB) la captura de las
# :05 tardaba entre 16 y 40 minutos, una CPU y el disco ocupados casi media
# hora de cada hora.  Lo que la portada lee de esta fila — total y cerradas
# (§ metrics._seed_tree_totals) — no necesita ese barrido: un GROUP BY status
# lo resuelve exacto leyendo el indice de ``status``.  El resto del desglose
# (expandidas, cierres por tipo, confianza) no lo pinta ninguna pagina; se
# mide en la primera captura de cada 24 horas y las demas lo dejan en NULL,
# que dice "esta hora no se midio" en vez de inventar o arrastrar un numero.
BREAKDOWN_FIELDS = (
    'positions_expanded',
    'closure_terminal', 'closure_tb', 'closure_mate_pv', 'closure_minimax',
    'closure_unclassified',
    'trust_verified', 'trust_andor', 'trust_engine', 'trust_disputed',
    'trust_unclassified',
)
FULL_BREAKDOWN_EVERY = timedelta(hours=24)


def needs_full_breakdown(bucket):
    """Si ninguna captura de las 24 horas anteriores a ``bucket`` lo midio."""
    return not ProgressSnapshot.objects.filter(
        bucket_start__gt=bucket - FULL_BREAKDOWN_EVERY,
        bucket_start__lt=bucket,
        positions_expanded__isnull=False).exists()


def _status_counts():
    """Total, abiertas y cerradas, exactas, sin leer la tabla.

    ``COUNT(*)`` y no ``COUNT(key)``: la clave no esta en el indice de
    ``status`` y contarla obligaria a visitar cada fila, que es justo el
    barrido que esto evita.
    """
    by_status = dict(Position.objects.order_by().values_list('status')
                     .annotate(n=Count('*')))
    total = sum(by_status.values())
    unknown = by_status.get('UNKNOWN', 0)
    return {'positions_total': total,
            'positions_unknown': unknown,
            'positions_closed': total - unknown,
            **{field: None for field in BREAKDOWN_FIELDS}}


def _snapshot_values(observed_at, *, full=True):
    positions = _full_position_counts() if full else _status_counts()
    return {**positions, **_other_values(observed_at)}


def _full_position_counts():
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
                    & Q(proof__in=('ENGINE', 'TB_LINE')))),
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
    return positions


def _other_values(observed_at):
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
        **tasks,
        'recorded_rejections_total': DBEvent.objects.filter(
            kind='TB_REJECTED').count(),
        'active_workers': live['workers'],
        'active_threads': live['cores'],
        # The reported heartbeat sum, which is what this column has always
        # held and what the glossary says it holds.  The home page's
        # "nodes / sec" is now the delivered rate (``live['nps']``); writing
        # that here would change the meaning of an append-only series halfway
        # through, and the delivered rate is already derivable from the
        # ``engine_nodes_total`` deltas between rows.
        'active_nps': live['nps_reported'],
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
def capture_progress(*, now=None, full=None):
    """Capture the current hour once; return ``(snapshot, created)``.

    An existing bucket is returned without recomputing or mutating it.  The
    optional ``now`` exists for deterministic tests; the management command
    always captures the real current instant.  ``full`` forces (``True``) or
    skips (``False``) the full position breakdown; ``None`` measures it when
    no capture of the previous 24 hours did (§ BREAKDOWN_FIELDS).
    """
    observed_at = now or timezone.now()
    bucket = utc_hour(observed_at)
    existing = ProgressSnapshot.objects.filter(bucket_start=bucket).first()
    if existing is not None:
        return existing, False
    if full is None:
        full = needs_full_breakdown(bucket)
    return ProgressSnapshot.objects.get_or_create(
        bucket_start=bucket,
        defaults=_snapshot_values(observed_at, full=full),
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


def _main_line_payload(line, created):
    if line is None:
        return None
    return {'created': created,
            'bucket_start': line.bucket_start.isoformat(),
            'value': line.value, 'status': line.status,
            'plies': line.plies, 'moves': line.moves,
            'end_reason': line.end_reason}


def _extremes_payload(folded, added):
    # ``folded`` is ``None`` where the trigger keeps them and the capture had
    # nothing to fold.  ``watch_added``: keys the capture put on the watch
    # list because nothing else had.
    if folded is None:
        return {'kept_by': 'trigger', 'watch_added': added}
    return {'kept_by': 'capture', 'new': folded[0], 'changed': folded[1],
            'watch_added': added}


class Command(BaseCommand):
    help = (
        'Capture the current AtomicDB counters and the main line from the '
        'start position in immutable UTC-hour buckets. Re-running within the '
        'hour is a no-op.'
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
        parser.add_argument(
            '--full', action='store_true',
            help='Measure the full position breakdown (one scan of the whole '
                 'position table) even if a capture of the last 24 hours '
                 'already did. With --preview, preview it too.',
        )

    def handle(self, *args, **options):
        if options['preview']:
            # Escribe NADA.  Existe porque una hora ya capturada no se
            # reescribe — es la garantia de la tabla — y justo despues de una
            # migracion que anade columnas hace falta poder LEER lo que esa
            # fila no llego a guardar, sin romper esa garantia.
            self.stdout.write(json.dumps(
                {'preview': True,
                 'snapshot': _snapshot_values(timezone.now(),
                                              full=options['full']),
                 'main_line': roots.walk_main_line()},
                sort_keys=True, separators=(',', ':'), default=str))
            return
        now = timezone.now()
        snapshot, created = capture_progress(
            now=now, full=True if options['full'] else None)
        # After the progress row has committed, and in its own transactions:
        # whatever happens to the main line cannot cost this hour's counters.
        line, line_created, folded, added = roots.capture(now=now)
        # After the extremes are current: the record is read from them.
        record = roots.capture_best_record()
        payload = _payload(snapshot, created)
        payload['main_line'] = _main_line_payload(line, line_created)
        payload['extremes'] = _extremes_payload(folded, added)
        payload['start_record'] = (
            None if record is None
            else {'value': record.value, 'status': record.status,
                  'at': record.at.isoformat()})
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
        if line is not None:
            action = 'created' if line_created else 'reused'
            self.stdout.write(
                f'main line: {action} UTC bucket '
                f'{line.bucket_start.isoformat()} plies={line.plies} '
                f'value={line.value} end={line.end_reason}')
        extremes = payload['extremes']
        if extremes['kept_by'] == 'capture':
            self.stdout.write(
                f"root extremes: folded new={extremes['new']} "
                f"changed={extremes['changed']}")
        if added:
            self.stdout.write(f'watch list: added {added}')
        if record is not None:
            self.stdout.write(
                f'start record: {record.value} {record.status} '
                f'at {record.at.isoformat()}')
