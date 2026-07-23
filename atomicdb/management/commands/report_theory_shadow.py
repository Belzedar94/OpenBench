"""Emit one bounded, read-mostly comparison of live and theory priorities."""

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.db.models.functions import Coalesce
from django.utils import timezone

from atomicdb import ingest
from atomicdb.models import CohortMembership, Position, SchedulingCohort


def _canonical_sha256(payload):
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


class Command(BaseCommand):
    help = (
        'Refresh and report bounded AtomicDB theory SHADOW rankings. '
        'It never creates tasks or proof data.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=25,
            help='Number of matched positions to include (1-100).')
        parser.add_argument(
            '--output',
            help='Optional no-overwrite UTF-8 JSON receipt path.')

    def handle(self, *args, **options):
        mode = getattr(
            settings, 'ATOMICDB_THEORY_SCHEDULER_MODE', 'OFF').upper()
        if mode != 'SHADOW':
            raise CommandError(
                'report_theory_shadow requires '
                'ATOMICDB_THEORY_SCHEDULER_MODE=SHADOW')
        limit = options['limit']
        if not 1 <= limit <= 100:
            raise CommandError('--limit must be between 1 and 100')

        # The command is an explicit observation request, so bypass only the
        # 30-second refresh memo.  refresh_priorities remains the sole scorer.
        ingest._priority_refresh_cache['at'] = 0.0
        ingest._priority_refresh_cache['signature'] = None
        ingest.refresh_priorities(reconcile_tasks=False)

        policy = getattr(settings, 'ATOMICDB_THEORY_POLICY_VERSION', '')
        bundle_sha256 = getattr(
            settings, 'ATOMICDB_THEORY_BUNDLE_SHA256', '')
        queue = Position.objects.filter(
            status='UNKNOWN', priority__gt=ingest.DEAD / 2)
        matched_positions = queue.filter(
            shadow_priority__isnull=False).count()
        live_order = list(queue.order_by('-priority', 'key').values(
            'key', 'priority', 'shadow_priority', 'theory_boost',
            'visits', 'nodes_invested')[:limit])
        shadow_order = list(queue.annotate(
            proposed=Coalesce('shadow_priority', 'priority')).order_by(
                '-proposed', 'key').values(
                    'key', 'priority', 'shadow_priority', 'theory_boost',
                    'visits', 'nodes_invested')[:limit])
        keys = list({
            row['key'] for row in live_order + shadow_order})
        cohorts_by_key = {}
        for key, slug in CohortMembership.objects.filter(
                position_key__in=keys,
                cohort__active=True,
                cohort__policy_version=policy,
                cohort__manifest_sha256=bundle_sha256,
        ).values_list('position_key', 'cohort__slug').distinct():
            cohorts_by_key.setdefault(key, []).append(slug)

        live_rank = {
            row['key']: rank for rank, row in enumerate(live_order, start=1)}
        shadow_rank = {
            row['key']: rank
            for rank, row in enumerate(shadow_order, start=1)}

        def compact(row):
            key = row['key']
            return {
                **row,
                'cohorts': sorted(cohorts_by_key.get(key, ())),
                'live_rank': live_rank.get(key),
                'shadow_rank': shadow_rank.get(key),
                'rank_delta': (
                    live_rank[key] - shadow_rank[key]
                    if key in live_rank and key in shadow_rank else None),
            }

        active_cohorts = list(SchedulingCohort.objects.filter(
            active=True, policy_version=policy,
            manifest_sha256=bundle_sha256).annotate(
                membership_count=Count('memberships')).values(
                    'slug', 'priority_level', 'evidence_level',
                    'manifest_sha256', 'membership_count'))
        body = {
            'schema': 'atomic-theory-shadow-report-v1',
            'generated_at_utc': timezone.now().isoformat(),
            'mode': mode,
            'policy_version': policy,
            'bundle_sha256': bundle_sha256,
            'queue_positions': queue.count(),
            'matched_positions': matched_positions,
            'live_shadow_top_overlap': len(
                set(row['key'] for row in live_order[:limit])
                & set(row['key'] for row in shadow_order[:limit])),
            'live_top': [compact(row) for row in live_order],
            'shadow_top': [compact(row) for row in shadow_order],
            'cohorts': sorted(active_cohorts, key=lambda row: row['slug']),
            'safety': {
                'task_writes': 0,
                'edge_writes': 0,
                'truth_writes': 0,
                'selection_mode': 'unchanged-base-priority',
            },
        }
        receipt = {**body, 'content_sha256': _canonical_sha256(body)}
        text = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        output = options.get('output')
        if output:
            path = Path(output)
            if path.exists():
                raise CommandError(f'output already exists: {path}')
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open('x', encoding='utf-8', newline='\n') as handle:
                    handle.write(text)
            except OSError as exc:
                raise CommandError(
                    f'cannot write output {path}: {exc}') from exc
        self.stdout.write(text.rstrip())
