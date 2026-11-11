"""The planner's cost model and its calibration.

  * the restricted mean (math only) matches the SciPy formula of the
    prototype, and the leaf pn is bracketed by the horizon;
  * a missing or broken model file falls back to the prototype fit, and a
    calibrated model round-trips through the state dir;
  * the censored fit recovers the slope of synthetic data;
  * the calibration export reads the database and writes nothing, and the
    command writes only its JSON.
"""

import json
import math
import os
import random
import re
import tempfile
import unittest
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connections
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from . import logic
from .database import current_alias
from .models import Edge, Position
from .planner import calibrate
from .planner.costmodel import (PROTOTYPE_MODEL, CostModel, load_model,
                                restricted_mean, save_model)
from .testing import TestCase


def scipy_restricted_mean(mu, s, lo, hi):
    from scipy import stats
    llo = math.log(lo) if lo > 0 else -1e9
    lhi = math.log(hi)
    surv_lo = stats.norm.sf((llo - mu) / s)
    part = math.exp(mu + s * s / 2) * (
        stats.norm.sf((llo - mu - s * s) / s)
        - stats.norm.sf((lhi - mu - s * s) / s))
    tail = hi * stats.norm.sf((lhi - mu) / s)
    return max(0.0, (part + tail) / surv_lo - lo)


class RestrictedMeanTest(unittest.TestCase):
    def test_matches_scipy(self):
        for mu in (18.0, 22.0, 27.0, 30.0, 34.0):
            for s in (1.0, 2.3, 3.3):
                for lo in (0.0, 1.28e8, 5e9, 1e11):
                    ref = scipy_restricted_mean(mu, s, lo, 1e13)
                    got = restricted_mean(mu, s, lo, 1e13)
                    self.assertAlmostEqual(got / max(ref, 1.0), 1.0, places=6)

    def test_bracketed_by_horizon(self):
        cm = CostModel()
        for a in (300, 800, 1500, 3000, 8000):
            for dog in (0.0, 1.0):
                for inv in (0, 1e8, 1e12):
                    v = cm.pn(a, dog, invested=inv)
                    self.assertGreaterEqual(v, 0.0)
                    self.assertLessEqual(v, cm.H)
        self.assertEqual(cm.fresh_pn(1500, 0.0), cm.pn(1500, 0.0))
        self.assertAlmostEqual(cm.pn(0, 0.0) / cm.H, 10 ** 2)
        self.assertLessEqual(cm.pn(-30000, 0.0), 1e22)

    def test_survivor_bracket_is_cheaper_at_low_evals(self):
        cm = CostModel()
        sv = cm.survivor()
        self.assertIn('survivor', sv.version)
        self.assertLess(sv.pn(1200, 0.0), cm.pn(1200, 0.0))


class ModelFileTest(unittest.TestCase):
    def test_fallback_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'm.json')
            self.assertEqual(load_model(path).version,
                             PROTOTYPE_MODEL['version'])
            with open(path, 'w') as fh:
                fh.write('{broken')
            self.assertEqual(load_model(path).version,
                             PROTOTYPE_MODEL['version'])
            data = dict(PROTOTYPE_MODEL, version='cal-test', sigma=2.0)
            save_model(data, path)
            m = load_model(path)
            self.assertEqual(m.version, 'cal-test')
            self.assertEqual(m.sigma, 2.0)
            with open(path, 'w') as fh:
                json.dump(dict(data, schema='other'), fh)
            self.assertEqual(load_model(path).version,
                             PROTOTYPE_MODEL['version'])


def synthetic(n=900, seed=3, beta=(40.0, -5.0, 1.2, 0.0), sigma=1.5):
    rnd = random.Random(seed)
    recs = []
    for _ in range(n):
        a = rnd.choice([400, 700, 1100, 1450, 2000, 3500, 6000])
        role = rnd.choice(['fav', 'dog'])
        dog = 1.0 if role == 'dog' else 0.0
        la = math.log(a / 100.0)
        mu = beta[0] + beta[1] * la + beta[2] * dog + beta[3] * dog * la
        c = math.exp(rnd.gauss(mu, sigma))
        exposure = math.exp(rnd.uniform(17, 27))
        proven = c <= exposure
        recs.append({'key': 'k', 'stm': 'w', 'role': role, 'a': a,
                     'band': calibrate.band(a),
                     'res': 'proven' if proven else 'open',
                     'own_nodes': 1, 'depth': 20, 'mat_fav': 0,
                     'pieces': 24, 'n_children': 28,
                     'desc_nodes': c if proven else exposure,
                     'desc_trunc': False})
    sizes = {}
    for r in recs:
        k = calibrate.stratum(r)
        sizes[k] = sizes.get(k, 0) + 1
    return {'strata_sizes': sizes, 'searched_rows': n}, recs


class FitTest(unittest.TestCase):
    def test_censored_fit_recovers_slope(self):
        summary, recs = synthetic()
        model, report = calibrate.fit(
            summary, recs, variants={'band_role': [0, 1, 2, 3]})
        self.assertEqual(model['variant'], 'band_role')
        self.assertAlmostEqual(model['beta'][1], -5.0, delta=0.6)
        self.assertAlmostEqual(model['sigma'], 1.5, delta=0.3)
        self.assertTrue(model['version'].startswith('cal-'))
        cm = CostModel(model)            # the fitted dict is a valid model
        self.assertGreater(cm.pn(1000, 0.0), cm.pn(3000, 0.0))
        self.assertIn('km', report)


_READ = re.compile(r'^\s*(SELECT|SAVEPOINT|RELEASE|SET|RESET)\b', re.I)


class CalibrationDatabaseTest(TestCase):
    def setUp(self):
        start = logic.start_fen()
        moves = logic.legal_moves(start)
        Position.objects.exclude(key=logic.key_of(start)).update(
            nodes_invested=0)
        root, _ = Position.objects.update_or_create(
            key=logic.key_of(start), defaults=dict(
                fen=start, eval_cp=900, nodes_invested=10 ** 9,
                expanded=True, last_analysis=[
                    {'eval_cp': 900, 'raw': 'info depth 30 score'}]))
        for i, mv in enumerate(moves[:6]):
            fen = logic.canonical_fen(logic.apply_move(start, mv))
            st = 'WHITE_WIN' if i % 2 else 'UNKNOWN'
            child = Position.objects.create(
                key=logic.key_of(fen), fen=fen, eval_cp=-1500 - 100 * i,
                status=st, nodes_invested=10 ** 8,
                last_analysis=[{'eval_cp': 1500 + 100 * i,
                                'raw': 'info depth 25'}])
            Edge.objects.create(parent=root, move_uci=mv, child=child)

    def test_export_reads_only(self):
        conn = connections[current_alias()]
        with CaptureQueriesContext(conn) as ctx:
            summary, recs = calibrate.export(pct=100, time_limit=60)
        for q in ctx.captured_queries:
            self.assertTrue(_READ.match(q['sql']), q['sql'][:120])
        self.assertEqual(summary['searched_rows'], 7)
        self.assertTrue(recs)
        root = [r for r in recs if r['n_children'] == 6]
        self.assertEqual(len(root), 1)
        self.assertEqual(root[0]['desc_nodes'], 6 * 10 ** 8)
        self.assertEqual(root[0]['depth'], 30)

    def test_command_writes_only_its_json(self):
        summary, recs = synthetic(n=400)
        with tempfile.TemporaryDirectory() as tmp, \
                override_settings(ATOMICDB_STATE_DIR=tmp), \
                mock.patch.object(calibrate, 'export',
                                  return_value=(summary, recs)):
            out = StringIO()
            call_command('calibrate_planner', stdout=out, stderr=StringIO())
            line = json.loads(out.getvalue())
            path = os.path.join(tmp, 'planner_costmodel.json')
            self.assertEqual(line['written'], path)
            self.assertTrue(os.path.exists(path.replace('.json',
                                                        '_report.json')))
            self.assertEqual(load_model().version, line['version'])
            out = StringIO()
            call_command('calibrate_planner', '--dry-run', stdout=out,
                         stderr=StringIO())
            self.assertNotIn('written', json.loads(out.getvalue()))
