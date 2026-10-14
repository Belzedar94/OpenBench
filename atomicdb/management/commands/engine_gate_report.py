"""Which recent deliveries the engine gate would refuse.

    python manage.py engine_gate_report [--days 7]

Read-only.  Prints the gate mode, the builds it accepts and, for every
account and engine sha that delivered searched analysis in the window, the
tasks and nodes and whether the gate takes them.  Run it before switching the
gate on: each REFUSED row is a contributor who has to change build first.
Tablebase deliveries carry no nodes and are never gated, so they are left out.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from atomicdb import engine_policy
from atomicdb.models import AnalysisTask


class Command(BaseCommand):
    help = 'Read-only report of the recent deliveries the engine gate would refuse.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=float, default=7.0)

    def handle(self, *args, **options):
        policy = engine_policy.load_policy()
        accepted = engine_policy.accepted_engines(policy)
        self.stdout.write(f"gate mode: {policy['mode']}")
        if not engine_policy.reference_engines():
            self.stdout.write('no reference binary found: an enforcing gate '
                              'would let everything through')
        for sha, note in sorted(accepted.items(), key=lambda item: item[1]):
            self.stdout.write(f'  accepts {sha[:12]}  {note}')

        since = timezone.now() - timedelta(days=options['days'])
        rows = (AnalysisTask.objects
                .filter(state='COMPLETED', completed__gte=since,
                        nodes_searched__gt=0)
                .values('delivered_by', 'engine_sha')
                .annotate(tasks=Count('id'), nodes=Sum('nodes_searched'))
                .order_by('-tasks'))
        total_tasks = refused_tasks = 0
        total_nodes = refused_nodes = 0
        self.stdout.write(f"\nlast {options['days']:g} days:")
        for row in rows:
            sha = row['engine_sha'] or ''
            verdict = 'ok' if sha in accepted else 'REFUSED'
            nodes = row['nodes'] or 0
            total_tasks += row['tasks']
            total_nodes += nodes
            if verdict != 'ok':
                refused_tasks += row['tasks']
                refused_nodes += nodes
            self.stdout.write(
                f"  {verdict:8} {(row['delivered_by'] or '?')[:24]:24} "
                f"{sha[:12] or '(none)':12} {row['tasks']:8} tasks "
                f"{nodes / 1e9:9.1f}G nodes")
        if total_tasks:
            self.stdout.write(
                f'\nwould refuse {refused_tasks} of {total_tasks} tasks '
                f'({100.0 * refused_tasks / total_tasks:.1f} %) and '
                f'{100.0 * refused_nodes / max(1, total_nodes):.1f} % of '
                f'the nodes')
