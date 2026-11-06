"""Measure the reach of every PROPOSED campaign for /atomicdb/campaigns/.

Read-only (``default_transaction_read_only`` plus a per-statement timeout,
the same session the Conquest Map build uses) and never tags anything: see
§ ``atomicdb.proposed_campaigns``.  One JSON line per campaign on stdout and
a summary line at the end.
"""

import json
import time

from django.core.management.base import BaseCommand

from atomicdb import proposed_campaigns
from atomicdb.conquest_map import _BoundedSession


class Command(BaseCommand):
    help = ('Walk the subtree of each proposed campaign (read-only, capped) '
            'and store its position counts for the campaigns page.')

    def add_arguments(self, parser):
        parser.add_argument('--max-nodes', type=int,
                            default=proposed_campaigns.MAX_NODES,
                            help='Walk cap per campaign (default %(default)s)')
        parser.add_argument('--statement-timeout-ms', type=int,
                            default=proposed_campaigns.STATEMENT_TIMEOUT_MS)

    def handle(self, *args, **options):
        started = time.monotonic()

        def log(campaign, row, seconds):
            self.stdout.write(json.dumps({
                'campaign': campaign.id, 'total': row['total'],
                'explored': row['explored'], 'resolved': row['resolved'],
                'capped': row['capped'], 'seconds': round(seconds, 1)}))

        with _BoundedSession(options['statement_timeout_ms']):
            rows = proposed_campaigns.measure_all(
                cap=max(1, options['max_nodes']), log=log)
        self.stdout.write(json.dumps({
            'measured': len(rows),
            'seconds': round(time.monotonic() - started, 1)}))
