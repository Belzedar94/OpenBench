"""Run av2-proof's differential scenarios through v1's own proof code.

    python atomicdb-v2/crates/av2-proof/v1diff/run_v1.py SCENARIOS.json OUT.json [--full]

Run from the repository root (the Django project).  It creates a throwaway
SQLite test database exactly like ``manage.py test`` does, replays every
scenario (create positions and edges, change statuses and evals, call
``proof.refresh_proof_numbers``) with v1's ``atomicdb/proof.py`` untouched,
and writes the ``ProofNode`` rows and ``PROOF_GUARD`` events after every
step.  The Rust test ``v1diff::matches_v1_golden`` replays the same
scenarios on ``av2-proof`` and compares.

Row order of a refresh level: SQLite answers ``key IN (...)`` from the
primary-key index, i.e. in key order; the Rust side uses the same order
(``KeyOrdered``).
"""

import hashlib
import json
import os
import sys

# --full also writes every row (for debugging a mismatch); the committed
# golden file keeps a sha256 prefix of the canonical rows per step.
FULL = '--full' in sys.argv


def main(src, dst):
    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')
    os.environ.setdefault('OPENBENCH_SECRET_KEY', 'local-differential-test')
    import django
    django.setup()
    from django.db import connections
    from django.test.utils import setup_test_environment

    setup_test_environment()
    names = []
    for alias in connections:
        conn = connections[alias]
        names.append((conn, conn.creation.create_test_db(verbosity=0)))
    try:
        out = run_all(json.load(open(src)))
    finally:
        for conn, name in names:
            conn.creation.destroy_test_db(name, verbosity=0)
    with open(dst, 'w') as handle:
        json.dump(out, handle, separators=(',', ':'))


def fen_for(stm):
    return f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1'


def run_all(scenarios):
    from django.test import override_settings

    from atomicdb import proof
    from atomicdb.models import DBEvent, Edge, Position, ProofCampaign, ProofNode

    campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
    results = []
    original_nodes = proof.PROOF_MAX_NODES
    for sc in scenarios:
        ProofNode.objects.all().delete()
        Edge.objects.all().delete()
        Position.objects.exclude(key=campaign.root_id).delete()
        DBEvent.objects.all().delete()
        proof.PROOF_MAX_NODES = sc['max_nodes']
        proof.reset_solve_gate_log()
        gate = override_settings(ATOMICDB_SOLVE_GATE=bool(sc.get('gate')))
        gate.enable()
        steps = []
        try:
            for step in sc['steps']:
                for op in step['ops']:
                    kind = op[0]
                    if kind == 'pos':
                        key, stm, status, eval_cp = op[1:5]
                        fen = op[5] if len(op) > 5 else fen_for(stm)
                        Position.objects.create(key=key, fen=fen,
                                                status=status, eval_cp=eval_cp)
                    elif kind == 'ana':
                        _, key, lines, mate_in = op
                        Position.objects.filter(key=key).update(
                            last_analysis=lines, mate_in=mate_in)
                    elif kind == 'edge':
                        _, parent, uci, child = op
                        Edge.objects.create(parent_id=parent, move_uci=uci,
                                            child_id=child)
                    elif kind == 'set':
                        _, key, status, eval_cp = op
                        Position.objects.filter(key=key).update(
                            status=status, eval_cp=eval_cp)
                    else:
                        raise ValueError(kind)
                before = set(DBEvent.objects.values_list('id', flat=True))
                written = proof.refresh_proof_numbers(
                    step['refresh'], max_plies=sc['max_plies'])
                guards = [event.payload for event in DBEvent.objects.filter(
                    kind='PROOF_GUARD').exclude(id__in=before).order_by('id')]
                disagree = [event.payload for event in DBEvent.objects.filter(
                    kind='SOLVE_GATE_DISAGREE').exclude(id__in=before)
                    .order_by('id')]
                rows = [[row.position_id, row.pn, row.dn,
                         row.expanded_in_proof, row.selected_child]
                        for row in ProofNode.objects.filter(
                            campaign=campaign).order_by('position_id')]
                canonical = json.dumps(rows, separators=(',', ':'))
                digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
                entry = {'written': written, 'guards': guards,
                         'disagree': disagree, 'n': len(rows), 'sha': digest}
                if FULL:
                    entry['rows'] = rows
                steps.append(entry)
        finally:
            proof.PROOF_MAX_NODES = original_nodes
            gate.disable()
        results.append({'name': sc['name'], 'steps': steps})
    return results


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    main(args[0], args[1])
