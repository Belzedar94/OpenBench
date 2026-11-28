"""Run av2-proof's descent scenarios through v1's own proof code.

    python atomicdb-v2/crates/av2-proof/v1diff/run_v1_descent.py SCENARIOS.json OUT.json

Run from the repository root (the Django project).  Same throwaway SQLite
test database as ``run_v1.py``; every step applies its graph changes, the
backed-value columns and the live tasks, calls
``proof.refresh_proof_numbers`` and then answers the step's queries with
``proof.descend`` and ``proof.descend_value`` (v1 code untouched).  The Rust
test ``v1diff::descent::descents_match_v1`` replays the same scenarios.
"""

import hashlib
import json
import os
import sys


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
    from atomicdb.models import (AnalysisTask, DBEvent, Edge, Position,
                                 ProofCampaign, ProofNode)

    campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
    results = []
    original_nodes = proof.PROOF_MAX_NODES
    for sc in scenarios:
        AnalysisTask.objects.all().delete()
        ProofNode.objects.all().delete()
        Edge.objects.all().delete()
        Position.objects.exclude(key=campaign.root_id).delete()
        DBEvent.objects.all().delete()
        proof.PROOF_MAX_NODES = sc['max_nodes']
        proof.reset_solve_gate_log()
        gate = override_settings(ATOMICDB_SOLVE_GATE=bool(sc.get('gate')),
                                 ATOMICDB_SHARED_ALT=False)
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
                    elif kind == 'val':
                        _, key, backed_eval, backed_move, plies, nodes, bn = op
                        Position.objects.filter(key=key).update(
                            backed_eval=backed_eval, backed_move=backed_move,
                            backed_plies=plies, nodes_invested=nodes,
                            backed_nodes=bn)
                    elif kind == 'task':
                        _, key, state = op
                        AnalysisTask.objects.filter(position_id=key).delete()
                        AnalysisTask.objects.create(
                            position_id=key, budget_nodes=8_000_000,
                            state=state)
                    elif kind == 'untask':
                        AnalysisTask.objects.filter(position_id=op[1]).delete()
                    else:
                        raise ValueError(kind)
                proof.refresh_proof_numbers(step['refresh'],
                                            max_plies=sc['max_plies'])
                rows = [[row.position_id, row.pn, row.dn,
                         row.expanded_in_proof, row.selected_child]
                        for row in ProofNode.objects.filter(
                            campaign=campaign).order_by('position_id')]
                canonical = json.dumps(rows, separators=(',', ':'))
                digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
                answers = []
                for kind, counter, avoid, start, max_plies in step['q']:
                    node = Position.objects.get(key=start)
                    if kind == 'pn':
                        pos, plies = proof.descend(
                            campaign, counter=counter, max_plies=max_plies,
                            avoid=set(avoid), start=node)
                        answers.append([pos.key if pos else None, plies])
                    else:
                        target = proof.descend_value(
                            campaign, counter=counter, max_plies=max_plies,
                            avoid=set(avoid), start=node)
                        answers.append(None if target is None else [
                            target.position.key, target.plies, target.arm,
                            target.floor_nodes, target.line_plies])
                steps.append({'sha': digest, 'a': answers})
        finally:
            proof.PROOF_MAX_NODES = original_nodes
            gate.disable()
        results.append({'name': sc['name'], 'steps': steps})
    return results


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    main(args[0], args[1])
