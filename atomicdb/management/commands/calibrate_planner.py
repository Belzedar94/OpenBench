"""Fit the planner's cost model from the database (read-only).

Samples searched positions, walks the explored subgraph below a stratified
subset and fits the censored lognormal of § docs/atomicdb-planner-design.md
section 2 (a port of ``tools/planner-proto/calib_export.py`` and
``fit_costs.py``).  Every query runs in a read-only session with a statement
timeout.  The ONLY output is a versioned JSON in ``ATOMICDB_STATE_DIR``
(``planner_costmodel.json``, plus ``planner_costmodel_report.json``), which
``run_planner`` reads at start; without it the planner runs on the
prototype's fit of 11-Nov-2026.

On production the defaults reproduce the prototype run: 4 % sample, 200
open and up to 1,500 proven per stratum, 2,500 expanded nodes per walk,
45 minutes.  Run it niced; it is a long read.
"""

import json
import os

from django.core.management.base import BaseCommand, CommandError

from atomicdb.planner import calibrate
from atomicdb.planner.costmodel import model_path, save_model


class Command(BaseCommand):
    help = 'Calibrate the planner cost model (read-only; writes a JSON file).'

    def add_arguments(self, parser):
        parser.add_argument('--pct', type=float, default=4.0,
                            help='TABLESAMPLE percentage (default: 4).')
        parser.add_argument('--per-stratum', type=int, default=200,
                            help='Open positions walked per stratum.')
        parser.add_argument('--event-cap', type=int, default=1500,
                            help='Proven positions walked per stratum.')
        parser.add_argument('--node-cap', type=int, default=2500,
                            help='Expanded nodes per walk before truncating.')
        parser.add_argument('--time-limit', type=float, default=2700.0,
                            help='Seconds of walking (default: 2700).')
        parser.add_argument('--seed', type=int, default=20261111)
        parser.add_argument('--horizon', type=float, default=1e13,
                            help='Horizon H of the restricted mean.')
        parser.add_argument('--out', default=None,
                            help='Output path (default: the state dir).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Fit and print; write nothing.')

    def handle(self, *args, **opts):
        out = opts['out'] or model_path()
        if not out and not opts['dry_run']:
            raise CommandError('ATOMICDB_STATE_DIR is not set; pass --out '
                               'or --dry-run')
        summary, recs = calibrate.export(
            pct=opts['pct'], per_stratum=opts['per_stratum'],
            event_cap=opts['event_cap'], node_cap=opts['node_cap'],
            time_limit=opts['time_limit'], seed=opts['seed'],
            log=lambda m: self.stderr.write(m))
        try:
            model, report = calibrate.fit(summary, recs,
                                          horizon=opts['horizon'])
        except ValueError as error:
            raise CommandError(str(error))
        line = {'version': model['version'], 'variant': model['variant'],
                'walked': model['walked'], 'events': model['events'],
                'beta': model['beta'], 'sigma': model['sigma'],
                'aic': model['aic'], 'stopped': summary.get('stopped')}
        if not opts['dry_run']:
            save_model(model, out)
            rep = os.path.splitext(out)[0] + '_report.json'
            with open(rep, 'w', encoding='utf-8') as fh:
                json.dump(report, fh, indent=1, sort_keys=True)
            line['written'] = out
        self.stdout.write(json.dumps(line, sort_keys=True))
