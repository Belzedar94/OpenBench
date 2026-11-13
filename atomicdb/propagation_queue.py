"""The climb through the ancestors, deferred out of the landing.

WHY (7-Nov-2026).  A landing used to store its search AND climb through its
ancestors in one transaction: the status cascade, the backed values seeded
with its parents, the other parents of what it closed and the audience of
what it re-seeded, the arms that read them, the proof numbers.  Five ingest
writers run side by side, so when many results land under the same
ancestors (a request flood) their UPDATEs on those ancestors queue behind
each other, deadlock on ``atomicdb_position``, and a result that is already
stored by the worker shows up on the site 10 to 20 minutes late.  BManBJ
proposed the split, and the owner approved it:

(a) the result is committed on its own position at once, in a short
    transaction (``ingest.ingest_analysis`` up to its ``pos.save``: the
    position's own numbers, its children's seeds, and the closures of its
    own children: mates of its PV, revocations, certified lines);
(b) the climb is a separate stage that batches and deduplicates seeds, so a
    slow climb only delays the parents' numbers, never the result.

HOW.  With ``ATOMICDB_DEFER_PROPAGATION`` on, stage (a) writes a
``PropagationSeed`` row in its own commit: the seed lists, exactly as the
inline climb would have used them (``ingest.Landing``).  A process dying
between (a) and (b) leaves the row, and the next drain climbs it.  ONE
drainer (``manage.py process_propagation``, its own service) takes up to
``BATCH_SIZE`` rows at a time under a row lock, merges their seeds, runs
``ingest.propagate_landings`` on them (the very calls that ended
``ingest_analysis``, moved and not rewritten) and deletes the rows in the
same transaction.  A failure rolls the whole batch back and the rows retry,
one at a time, with backoff.

Why a dedicated drainer and not the ingest writers between jobs: the convoy
IS several writers climbing the same ancestors at once.  One drainer climbs
them once per batch, with nobody to deadlock against but the short stage (a)
transactions, and a batch merges what a flood lands under one ancestor into
one climb.  The writers only drain when deferral is off and rows are left
over (a rollback), so nothing is ever stranded.

WHY THE VALUES ARE THE SAME.  Both ascents compute a fixed point of rules
that read only the children's current rows: ``backup_cascade`` closes a
position when its children decide it (closure only ever adds information)
and re-derives proof, slack and mate distance from the children at every
visit; ``backup_backed_evals`` writes, level by level, what
``_decide_level`` decides from the children.  Neither rule changes here.
For a single landing the climb is the inline one call for call, with the
same seed lists in the same order.  For a batch, every key any landing
seeded is seeded; a position reached from two landings is recomputed once,
from children that already hold both results, which is what the second of
two consecutive inline climbs would write.  Three things are the batch's and
not a sequence's, and none of them changes a final value once the climb has
come to rest:

* the noise cut (BACKED_EPSILON_CP).  Two moves of 8 centipawns in a row
  each stop below the cut inline; merged, they move 16 and wake the parents.
  The merged climb writes the value one level of the backup decides from
  the children, which is the value ``sweep_stale_backed`` repairs the
  sequential result to anyway (``stale_backed_rows``): the same rest point,
  reached sooner;
* the guards (ply, node budget, revisit cap) cut a climb by its size; the
  frontier they leave goes to the BACKED_GUARD record and is carried on
  after the commit, as for an inline climb (``carried_cuts``);
* the order of the seeds: a level reads its rows and their children from
  the database whatever order the keys came in, and the cascade already
  pops its frontier from a set, so neither ascent depends on it.  The batch
  passes them in key order all the same.

What is NOT the same as one sequence is the stage (a) of a later landing
that runs before the climb of an earlier one: the seed guard of a child
(``_seed_child_eval``) reads ``backed_nodes``, which only a climb writes.
That is a schedule the five writers already produce today, when one
search commits while another writer's climb has not: the deferred queue
makes it more frequent, it does not make it new.

The tests in test_deferred_propagation hold all of this to a line from the
start position, a transposition and a closure reaching another parent.

The arms (uncertainty, witness refuted, value cascade, idle repair,
autosolve) and the recheck after commit run per landing, after the merged
climb, as they ran after the inline one; the proof numbers of the landed
positions and the deferred PV verification after the commit, as in
``ingest_queue.apply_job``.
"""

import contextlib
import logging
import time

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Count, Min
from django.utils import timezone

from . import ingest
from .database import atomic, connection, current_alias
from .models import IngestJob, Position, PropagationSeed

logger = logging.getLogger(__name__)

# Landings per drain transaction.  A batch holds the row locks of the
# ancestors it rewrites until it commits, and a stage (a) touching one of
# them waits for that commit, so the batch is kept to a size whose climb is
# a few seconds even in a flood.
BATCH_SIZE = 32
BACKOFF_SECONDS = (5, 30, 120, 600)


def deferral_enabled():
    """Stage (b) waits in the queue instead of running in the landing.

    Off by default: the landing climbs inline, exactly as before this
    module.  The synchronous ingest (the web process applying a submit
    itself, § ingest_queue.synchronous_ingest_enabled) always climbs inline,
    because it is the emergency path that must not depend on another
    service.
    """
    from .ingest_queue import synchronous_ingest_enabled
    return (bool(getattr(settings, 'ATOMICDB_DEFER_PROPAGATION', False))
            and not synchronous_ingest_enabled())


def defer(landing, extra=None):
    """Leave ``landing``'s climb in the queue.  The caller owns the
    transaction: the row commits with the landing or not at all."""
    if extra:
        landing.extra.update(extra)
    return PropagationSeed.objects.create(
        position_id=landing.key, source=landing.source or '',
        payload=landing.to_payload(), next_attempt_at=timezone.now())


def ready_seeds(now=None):
    now = now or timezone.now()
    return (PropagationSeed.objects.filter(next_attempt_at__lte=now)
            .order_by('id'))


def _backoff_seconds(attempts):
    index = min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)
    return BACKOFF_SECONDS[index]


def _mark_failure(ids, error):
    """Outside the batch that just rolled back: each row is retried alone."""
    now = timezone.now()
    for row in PropagationSeed.objects.filter(pk__in=ids).only('attempts'):
        attempts = row.attempts + 1
        PropagationSeed.objects.filter(pk=row.pk).update(
            attempts=attempts, last_error=str(error)[:2000],
            next_attempt_at=now + timezone.timedelta(
                seconds=_backoff_seconds(attempts)))
    logger.warning('AtomicDB propagation of %d seeds failed: %s',
                   len(ids), error)


def _merge(rows):
    """One Landing per landed position, in arrival order.

    Two rows for the same position (two searches of it, or a job applied
    twice) are one climb: their seed lists are merged without duplicates,
    and the position's arms run once, on the row as it stands.
    """
    snapshots = Position.objects.in_bulk({row.position_id for row in rows})
    landings, jobs = {}, {}
    for row in rows:
        payload = row.payload or {}
        landing = landings.get(row.position_id)
        incoming = ingest.Landing.from_payload(
            row.position_id, payload, source=row.source,
            pos=snapshots.get(row.position_id))
        if landing is None:
            landings[row.position_id] = incoming
        else:
            for name in ('closed', 'others', 'parents', 'audience'):
                merged = getattr(landing, name)
                merged.extend(key for key in getattr(incoming, name)
                              if key not in merged)
        jobs.setdefault(row.position_id, []).append(payload)
    return [landing for landing in landings.values()
            if landing.pos is not None], jobs


def _record(landings, jobs, summaries, batch):
    """What the climb did goes into the summary of each job it climbed for,
    the one record of a job, in the transaction that climbed."""
    by_job = {}
    for landing in landings:
        for payload in jobs.get(landing.key, ()):
            if payload.get('job'):
                by_job[payload['job']] = {**summaries.get(landing.key, {}),
                                          'batch': batch}
    if not by_job:
        return
    rows = list(IngestJob.objects.filter(pk__in=list(by_job)).only('summary'))
    for row in rows:
        row.summary = {**(row.summary or {}), 'propagated': by_job[row.pk]}
    IngestJob.objects.bulk_update(rows, ['summary'])


def _verify_requests(landings, jobs):
    """The deferred ``then_verify`` of each job, for after the commit."""
    out = []
    for landing in landings:
        for payload in jobs.get(landing.key, ()):
            verify = payload.get('verify')
            if verify:
                out.append((payload.get('job'), landing.key, verify))
    return out


class _Job:
    """What ingest_queue._verify_after reads of a job."""

    __slots__ = ('pk', 'position_id')

    def __init__(self, pk, position_id):
        self.pk = pk
        self.position_id = position_id


def proof_after_commit_enabled():
    """A batch refreshes its proof numbers once it has committed.

    On by default (see ingest.proof_refresh_after_commit); the setting
    ``ATOMICDB_PROOF_AFTER_COMMIT = False`` puts back the refresh inside the
    batch transaction plus the one of the landed keys after it.  A batch
    drained inside somebody else's transaction (a test, a shell) has no
    commit of its own to wait for, and refreshes inside as before.
    """
    return (bool(getattr(settings, 'ATOMICDB_PROOF_AFTER_COMMIT', True))
            and not connection.in_atomic_block)


def _refresh_proof_after_commit(calls):
    """The batch's proof refreshes, the first thing after its commit."""
    try:
        ingest.refresh_collected_proof_numbers(calls)
    except DatabaseError:
        logger.exception('AtomicDB propagation: proof numbers of a batch '
                         '(%d cascades) failed', len(calls))


def _claim_size(limit):
    """A row that failed before climbs alone, so one bad row never holds a
    whole batch back again."""
    first = ready_seeds().values_list('attempts', flat=True).first()
    if first is None:
        return 0
    return 1 if first else limit


def drain_batch(limit=BATCH_SIZE):
    """Climb one batch of ready seeds.  Returns how many rows it settled
    (0 when there was nothing to do), or raises nothing: a failure is
    recorded on the rows and reported as ``-1``."""
    size = _claim_size(limit)
    if not size:
        return 0
    ids = []
    summaries, landings, jobs = {}, [], {}
    proof_later = proof_after_commit_enabled()
    try:
        with ingest.carried_cuts() as cut:
            with atomic():
                rows = list(ready_seeds().select_for_update(skip_locked=True)
                            [:size])
                if not rows:
                    return 0
                ids = [row.pk for row in rows]
                with contextlib.ExitStack() as stack:
                    if proof_later:
                        # Registered before any recheck, so it runs first
                        # after the commit, where the refresh used to end
                        # the transaction (§ ingest.proof_refresh_after_commit).
                        calls = stack.enter_context(
                            ingest.proof_refresh_after_commit())

                        def refresh_proof():
                            _refresh_proof_after_commit(calls)

                        transaction.on_commit(refresh_proof,
                                              using=current_alias(),
                                              robust=True)
                    # One closure source per climb: the cascade reads it for
                    # every closure it writes (§ ingest.closure_attribution).
                    groups = {}
                    for row in rows:
                        groups.setdefault(row.source or '', []).append(row)
                    for source, group in groups.items():
                        with ingest.closure_attribution(source or None):
                            merged, merged_jobs = _merge(group)
                            summaries.update(
                                ingest.propagate_landings(merged))
                            landings.extend(merged)
                            for key, payloads in merged_jobs.items():
                                jobs.setdefault(key, []).extend(payloads)
                _record(landings, jobs, summaries, len(rows))
                PropagationSeed.objects.filter(pk__in=ids).delete()
    except Exception as error:      # noqa: BLE001 - the rows keep the work
        if ids:
            try:
                _mark_failure(ids, error)
            except DatabaseError:
                logger.exception('AtomicDB propagation: recording a failure '
                                 'failed')
        else:
            logger.exception('AtomicDB propagation: claiming a batch failed')
        return -1
    # The batch has committed: what follows never puts its rows back.
    _after_commit(landings, jobs, cut, proof_later)
    return len(ids)


def _after_commit(landings, jobs, cut, proof_refreshed=False):
    """What ``ingest_queue.apply_job`` did after its commit, for these
    landings: the proof numbers of the landed positions, the ascents a
    guard cut short, and the deferred PV verifications.

    With ``proof_refreshed`` the batch's own refresh after the commit has
    already climbed from every landed key (each is a seed of its cascade),
    over rows nothing has changed since, so the landed keys are not climbed
    a second time."""
    if not proof_refreshed:
        keys = [landing.key for landing in landings]
        try:
            ingest.proof.refresh_proof_numbers(keys)
        except DatabaseError:
            logger.exception('AtomicDB propagation: proof numbers of %d '
                             'landed positions failed', len(keys))
    if cut:
        try:
            ingest.carry_cut_ascents(cut)
        except DatabaseError:
            logger.exception('AtomicDB propagation: carrying %d cut keys on '
                             'failed', len(set(cut)))
    from .ingest_queue import _verify_after
    for job_id, key, verify in _verify_requests(landings, jobs):
        try:
            verified = _verify_after(_Job(job_id, key), verify)
            if verified is None or not job_id:
                continue
            row = IngestJob.objects.filter(pk=job_id).only('summary').first()
            if row is not None:
                IngestJob.objects.filter(pk=job_id).update(
                    summary={**(row.summary or {}), 'then_verify': verified})
        except DatabaseError:
            logger.exception('AtomicDB propagation: verifying the line of %s '
                             'failed', key)


def drain(max_batches=None, limit=BATCH_SIZE):
    """Climb batches until the ready rows run out (or ``max_batches``)."""
    settled, failed, batches = 0, 0, 0
    while max_batches is None or batches < max_batches:
        done = drain_batch(limit=limit)
        if done == 0:
            break
        batches += 1
        if done < 0:
            failed += 1
            # The failed rows wait for their backoff; the others go on.
            continue
        settled += done
    return {'settled': settled, 'failed_batches': failed, 'batches': batches}


def backlog(now=None):
    """Landed searches whose climb is still waiting, and the oldest's age.

    One aggregate over the table, the same shape as the write backlog
    (§ lanes._write_backlog).
    """
    now = now or timezone.now()
    row = PropagationSeed.objects.aggregate(waiting=Count('id'),
                                            oldest=Min('created'))
    oldest = row['oldest']
    return {'waiting': row['waiting'] or 0, 'oldest': oldest,
            'oldest_seconds': (round((now - oldest).total_seconds(), 1)
                               if oldest else None)}


def pending_positions(keys):
    """Those of ``keys`` whose own landing is still waiting to climb."""
    return set(PropagationSeed.objects.filter(position_id__in=list(keys))
               .values_list('position_id', flat=True))


def wait_idle(seconds, stopping):
    """A short sleep that a stop request cuts."""
    deadline = time.monotonic() + max(0.1, seconds)
    while not stopping() and time.monotonic() < deadline:
        time.sleep(0.1)
