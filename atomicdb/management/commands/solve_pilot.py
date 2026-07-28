"""The measured pilot that decides whether SOLVE is worth the compute mix.

The question is not "does df-pn work" — the engine's own self-test answers
that.  The question is whether a second of SOLVE buys more VERIFIED CLOSURE
than a second of deeper evaluation, and nobody knows that for atomic until it
is measured.  So this is a precommitted, paired comparison, and the gates are
written down before the numbers exist:

    * zero falsely accepted certificates;
    * verifier CPU below 20% of solver CPU;
    * at least 2x verified closures per core-hour.

STRATA
------
Frontier positions are not interchangeable, so the sample is stratified and
the arms are PAIRED within each stratum:

  ``mate_band``   |eval| >= MATE_BAND: the engine already sees a mate and only
                  the proof is missing.  SOLVE should dominate here.
  ``high_eval``   |eval| >= 800 with no mate score.  The interesting middle.
  ``fortress``    decisive-ish eval with a LOW density of zeroing moves in the
                  stored analysis — captures and pawn moves are what reset the
                  fifty-move counter, so few of them is the proxy for a
                  drag-out line.  This is the stratum where evaluation is
                  believed to be weakest and where a solver can burn its whole
                  budget for nothing.
  ``moderate``    everything else that is still open.  The control.

ARMS
----
``analyze`` re-buys the evaluation ladder (512M then 2B) through the ordinary
AnalysisTask queue.  ``solve`` queues a SolveTask at 10^7 then 10^8 nodes.
Both are tagged so ``--report`` can tell them apart afterwards.
"""

import json

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum

from atomicdb import ingest, proof
from atomicdb.database import atomic
from atomicdb.models import (AnalysisTask, DBEvent, Position, ProofCampaign,
                             SolveTask)

ANALYZE_BUDGETS = (512_000_000, 2_000_000_000)
SOLVE_BUDGETS = (10_000_000, 100_000_000)
# Round 2, after round 1: the fortress arm spent twelve minutes per position
# to answer UNKNOWN.  Sending it back to 10^8 would buy the same answer at the
# same price, so that stratum now gets the CLASSIFIER budget instead — one F0
# pass whose product is the fortress telemetry, not a verdict.  What to do
# with a candidate the classifier flags belongs to SURVIVE50.
FORTRESS_BUDGETS = (ingest.DEBT_STAGE_NODES,)
STRATA = ('mate_band', 'high_eval', 'fortress', 'moderate')
PILOT_ARMS = ('analyze', 'solve')
# Gates, written down before the numbers exist.
GATE_MAX_FALSE_CERTIFICATES = 0
GATE_MAX_VERIFIER_SHARE = 0.20
GATE_MIN_CLOSURE_RATIO = 2.0


def goal_for(position, campaign=None):
    """Which side the SOLVE task should try to prove.

    Round 1 asked every arm to prove the CAMPAIGN's goal, which on a position
    evaluated at -1500 is asking the solver to prove the thing the engine is
    certain is false.  It burned the budget and answered UNKNOWN, as it
    should have.  The eval is a terrible oracle and a perfectly good SIGNPOST:
    decisively negative means point the prover at Black.
    """
    known = position.backed_eval if position.backed_eval is not None \
        else position.eval_cp
    if known is not None and known <= -GOAL_SIGN_THRESHOLD:
        return ProofCampaign.Goal.BLACK_WIN
    if known is not None and known >= GOAL_SIGN_THRESHOLD:
        return ProofCampaign.Goal.WHITE_WIN
    return (campaign.goal if campaign is not None
            else ProofCampaign.Goal.WHITE_WIN)


# Past this, in the attacker-agnostic sense, the engine is not hesitating.
GOAL_SIGN_THRESHOLD = 900


def zeroing_density(last_analysis):
    """Fraction of the stored PV plies that reset the fifty-move counter.

    A cheap proxy computed from what is already in the row: a UCI move whose
    destination is occupied is a capture, and a pawn move is recognised by its
    file/rank shape only in the crude sense of "moved two ranks or changed
    file".  It does not need to be exact — it only has to ORDER positions by
    how likely they are to be long reversible grinds.
    """
    if not isinstance(last_analysis, list) or not last_analysis:
        return None
    plies, resets = 0, 0
    for line in last_analysis:
        if not isinstance(line, dict):
            continue
        for uci in (line.get('pv') or [])[:24]:
            if not isinstance(uci, str) or len(uci) < 4:
                continue
            plies += 1
            # A promotion or a two-rank push is certainly a pawn move; a file
            # change with no promotion is very likely a capture.
            if len(uci) > 4 or uci[0] != uci[2] \
                    or abs(int(uci[3]) - int(uci[1])) == 2:
                resets += 1
    if not plies:
        return None
    return resets / plies


class Command(BaseCommand):
    help = 'Queue or report the paired ANALYZE vs SOLVE pilot.'

    def add_arguments(self, parser):
        parser.add_argument('--size', type=int, default=200,
                            help='Total positions across all strata.')
        parser.add_argument('--report', action='store_true',
                            help='Compute the metrics instead of queueing.')
        parser.add_argument('--json', action='store_true',
                            help='Machine-readable report.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show the sample without queueing anything.')

    def handle(self, *args, **options):
        if options['report']:
            self._report(options['json'])
            return
        self._queue(options)

    # ---------------- sampling ----------------

    def _sample(self, size):
        per_stratum = max(2, size // len(STRATA))
        open_positions = Position.objects.filter(status='UNKNOWN',
                                                 expanded=True)
        chosen, taken = {}, set()

        mate = list(open_positions.filter(
            Q(eval_cp__gte=ingest.MATE_BAND) | Q(eval_cp__lte=-ingest.MATE_BAND)
        ).exclude(key__in=taken).order_by('-visits', 'key')[:per_stratum])
        chosen['mate_band'] = mate
        taken.update(row.key for row in mate)

        high = list(open_positions.filter(
            Q(eval_cp__gte=800, eval_cp__lt=ingest.MATE_BAND)
            | Q(eval_cp__lte=-800, eval_cp__gt=-ingest.MATE_BAND)
        ).exclude(key__in=taken).order_by('-visits', 'key')[:per_stratum * 3])
        # Split the decisive band by how reversible its lines look.
        scored = []
        for row in high:
            density = zeroing_density(row.last_analysis)
            scored.append((density if density is not None else 1.0, row))
        scored.sort(key=lambda item: item[0])
        fortress = [row for _, row in scored[:per_stratum]]
        chosen['fortress'] = fortress
        taken.update(row.key for row in fortress)
        rest = [row for _, row in scored if row.key not in taken]
        chosen['high_eval'] = rest[:per_stratum]
        taken.update(row.key for row in chosen['high_eval'])

        moderate = list(open_positions.exclude(key__in=taken)
                        .order_by('-visits', 'key')[:per_stratum])
        chosen['moderate'] = moderate
        return chosen

    def _queue(self, options):
        campaign = ProofCampaign.objects.filter(active=True).first()
        sample = self._sample(options['size'])
        counts = {stratum: len(rows) for stratum, rows in sample.items()}
        queued = {'analyze': 0, 'solve': 0}

        if options['dry_run']:
            self.stdout.write(json.dumps(
                {'sample': counts, 'queued': queued, 'dry_run': True},
                sort_keys=True))
            return

        for stratum, rows in sample.items():
            # PAIRED assignment: alternate inside each stratum so the two arms
            # see the same distribution of whatever the stratum did not
            # control for.
            for index, row in enumerate(rows):
                arm = PILOT_ARMS[index % 2]
                with atomic():
                    if arm == 'analyze':
                        queued['analyze'] += self._queue_analyze(row, stratum)
                    else:
                        queued['solve'] += self._queue_solve(
                            row, stratum, campaign)
        DBEvent.objects.create(kind='SOLVE_PILOT_QUEUED', payload={
            'sample': counts, 'queued': queued,
            'analyze_budgets': list(ANALYZE_BUDGETS),
            'solve_budgets': list(SOLVE_BUDGETS)})
        self.stdout.write(json.dumps({'sample': counts, 'queued': queued},
                                     sort_keys=True))

    def _queue_analyze(self, position, stratum):
        made = 0
        generation = max(position.visits, 0)
        used = set(AnalysisTask.objects.filter(position=position)
                   .values_list('generation', flat=True))
        for budget in ANALYZE_BUDGETS:
            while generation in used:
                generation += 1
            AnalysisTask.objects.create(
                position=position, generation=generation,
                budget_nodes=budget, source='AUTO',
                multipv=ingest.multipv_for(generation))
            used.add(generation)
            made += 1
        DBEvent.objects.create(kind='SOLVE_PILOT_ARM', payload={
            'key': position.key, 'arm': 'analyze', 'stratum': stratum})
        return made

    def _queue_solve(self, position, stratum, campaign):
        made = 0
        goal = goal_for(position, campaign)
        fortress = stratum == 'fortress'
        budgets = FORTRESS_BUDGETS if fortress else SOLVE_BUDGETS
        for budget in budgets:
            SolveTask.objects.create(
                position=position, campaign=campaign, goal=goal,
                budget_stage='F0' if fortress else 'F4',
                budget_nodes=budget, arm=stratum)
            made += 1
        DBEvent.objects.create(kind='SOLVE_PILOT_ARM', payload={
            'key': position.key, 'arm': 'solve', 'stratum': stratum})
        return made

    # ---------------- report ----------------

    def _report(self, as_json):
        arms = {event.payload.get('key'): event.payload
                for event in DBEvent.objects.filter(kind='SOLVE_PILOT_ARM')}
        analyze_keys = {key for key, payload in arms.items()
                        if payload.get('arm') == 'analyze'}
        solve_keys = {key for key, payload in arms.items()
                      if payload.get('arm') == 'solve'}

        analyze = AnalysisTask.objects.filter(
            position_id__in=analyze_keys, state='COMPLETED')
        analyze_seconds = analyze.aggregate(
            total=Sum('elapsed_seconds'))['total'] or 0.0
        analyze_closed = Position.objects.filter(
            key__in=analyze_keys).exclude(status='UNKNOWN').count()

        solved = SolveTask.objects.filter(position_id__in=solve_keys)
        completed = solved.filter(state='COMPLETED')
        solve_seconds = completed.aggregate(
            total=Sum('elapsed_seconds'))['total'] or 0.0
        verified = completed.filter(verified=True)
        verified_count = verified.count()
        false_certificates = solved.filter(
            state='FAILED').exclude(reject_reason='').count()
        certificate_bytes = verified.aggregate(
            total=Sum('certificate_bytes'))['total'] or 0
        solve_closed = Position.objects.filter(
            key__in=solve_keys, closure='SOLVE').count()

        verifier_seconds = self._verifier_seconds()
        duplicates = solved.values('position_id').annotate(
            n=Count('id')).filter(n__gt=2).count()

        analyze_rate = (analyze_closed / (analyze_seconds / 3600.0)
                        if analyze_seconds else 0.0)
        solve_rate = (solve_closed / (solve_seconds / 3600.0)
                      if solve_seconds else 0.0)
        verifier_share = (verifier_seconds / solve_seconds
                          if solve_seconds else 0.0)
        ratio = (solve_rate / analyze_rate) if analyze_rate else None

        report = {
            'positions': {'analyze': len(analyze_keys),
                          'solve': len(solve_keys)},
            'analyze': {'tasks_completed': analyze.count(),
                        'engine_seconds': round(analyze_seconds, 1),
                        'closures': analyze_closed,
                        'closures_per_core_hour': round(analyze_rate, 3)},
            'solve': {'tasks_completed': completed.count(),
                      'solver_seconds': round(solve_seconds, 1),
                      'verified_certificates': verified_count,
                      'false_certificates': false_certificates,
                      'closures': solve_closed,
                      'closures_per_core_hour': round(solve_rate, 3),
                      'certificate_bytes_total': certificate_bytes,
                      'certificate_bytes_per_closure':
                          round(certificate_bytes / solve_closed, 1)
                          if solve_closed else None},
            'verifier': {'seconds': round(verifier_seconds, 1),
                         'share_of_solver': round(verifier_share, 4)},
            'duplicate_positions': duplicates,
            'gates': {
                'no_false_certificates':
                    false_certificates <= GATE_MAX_FALSE_CERTIFICATES,
                'verifier_under_20_percent':
                    verifier_share <= GATE_MAX_VERIFIER_SHARE,
                'closure_yield_at_least_2x':
                    ratio is not None and ratio >= GATE_MIN_CLOSURE_RATIO,
                'closure_ratio': round(ratio, 3) if ratio is not None else None,
            },
        }
        if as_json:
            self.stdout.write(json.dumps(report, sort_keys=True, indent=2))
            return
        self.stdout.write(json.dumps(report, sort_keys=True, indent=2))

    def _verifier_seconds(self):
        total = 0.0
        for payload in DBEvent.objects.filter(
                kind='SOLVE_VERIFIED').values_list('payload', flat=True):
            try:
                total += float(payload.get('seconds') or 0.0)
            except (AttributeError, TypeError, ValueError):
                continue
        return total
