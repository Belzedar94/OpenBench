"""What the planner's shadow cycles say (read-only).

    manage.py planner_status [--hours 24] [--json]

Prints the last shadow cycle (root expected cost, in nodes and in
fleet-days, its bracket, open obligations per ply, timings, memory) and,
over the last ``--hours``, how much of what the planner picked the fleet
was actually given: picked positions that got a task completed or leased
after the pick, and the share of all served tasks that landed on a pick, by
source.  Reads ``PlannerShadowCycle``, ``PlannerShadowPick`` and
``AnalysisTask``; writes nothing.
"""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from atomicdb.models import (AnalysisTask, PlannerShadowCycle,
                             PlannerShadowPick)


def _fmt(x):
    if x is None:
        return 'inf'
    for unit, scale in (('T', 1e12), ('G', 1e9), ('M', 1e6)):
        if abs(x) >= scale:
            return '%.1f%s' % (x / scale, unit)
    return '%.0f' % x


def overlap(hours):
    since = timezone.now() - timedelta(hours=hours)
    first_pick = {}
    for key, created in PlannerShadowPick.objects.filter(
            cycle__started__gte=since).values_list('position_key',
                                                   'cycle__started'):
        if key not in first_pick or created < first_pick[key]:
            first_pick[key] = created
    served = list(AnalysisTask.objects.filter(
        state='COMPLETED', completed__gte=since).values_list(
            'position_id', 'source', 'completed'))
    served += [(k, s, t) for k, s, t in AnalysisTask.objects.filter(
        state='LEASED', leased_at__gte=since).values_list(
            'position_id', 'source', 'leased_at')]
    by_source, hit_by_source = {}, {}
    picked_served = set()
    for key, src, when in served:
        by_source[src] = by_source.get(src, 0) + 1
        t = first_pick.get(key)
        if t is not None and when is not None and when >= t:
            hit_by_source[src] = hit_by_source.get(src, 0) + 1
            picked_served.add(key)
    return {'hours': hours, 'distinct_picks': len(first_pick),
            'picks_served_after_pick': len(picked_served),
            'served_tasks': len(served), 'served_by_source': by_source,
            'served_on_picks_by_source': hit_by_source,
            'cycles': PlannerShadowCycle.objects.filter(
                started__gte=since).count()}


class Command(BaseCommand):
    help = 'Show the planner shadow metrics (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=float, default=24.0)
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **opts):
        last = PlannerShadowCycle.objects.order_by('-started').first()
        ov = overlap(opts['hours'])
        if opts['json']:
            self.stdout.write(json.dumps({
                'last_cycle': None if last is None else {
                    'id': last.pk, 'started': last.started.isoformat(),
                    'model': last.model_version, 'root_pn': last.root_pn,
                    'root_dn': last.root_dn,
                    'fleet_nodes_per_day': last.fleet_nodes_per_day,
                    'picks': last.picks, 'metrics': last.metrics},
                'overlap': ov}, sort_keys=True, default=str))
            return
        if last is None:
            self.stdout.write('planner: no shadow cycle recorded yet')
        else:
            m = last.metrics or {}
            st = m.get('strategy') or {}
            days = m.get('root_expected_cost_fleet_days')
            self.stdout.write(
                'last cycle #%d at %s, model %s' % (
                    last.pk, last.started.isoformat(timespec='seconds'),
                    last.model_version))
            self.stdout.write(
                '  root expected cost %s nodes = %s fleet-days '
                '(fleet %s nodes/day)' % (
                    _fmt(last.root_pn),
                    'n/a' if days is None else '%.1f' % days,
                    _fmt(last.fleet_nodes_per_day)))
            self.stdout.write(
                '  strategy: %s obligations, cost %s (survivor bracket %s)%s'
                % (st.get('obligations'), _fmt(st.get('cost_main')),
                   _fmt(st.get('cost_survivor_bracket')),
                   ' [truncated]' if st.get('truncated') else ''))
            by_ply = st.get('obligations_by_ply') or {}
            self.stdout.write('  open obligations per ply: ' + ', '.join(
                '%s:%s' % (k, v) for k, v in by_ply.items()))
            self.stdout.write('  picks %d %s; seconds %s; rss %s MB' % (
                last.picks, m.get('picks_by_kind'), m.get('seconds'),
                None if m.get('rss_mb') is None else round(m['rss_mb'])))
        self.stdout.write(
            'last %.0fh: %d cycles, %d distinct picks, %d served after the '
            'pick; %d tasks served %s, on picks %s' % (
                ov['hours'], ov['cycles'], ov['distinct_picks'],
                ov['picks_served_after_pick'], ov['served_tasks'],
                ov['served_by_source'], ov['served_on_picks_by_source']))
