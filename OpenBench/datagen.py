import datetime
import time

from django.db import OperationalError, transaction
from django.db.models import F, Q
from django.utils import timezone

from OpenBench.models import DatagenChunk


# Heartbeats arrive every 30 seconds. Five minutes tolerates transient network
# failures while still making abandoned chunks available again promptly.
DATAGEN_LEASE = datetime.timedelta(minutes=5)

# A chunk is a persistent scheduler row, so accepting an unbounded count can
# exhaust both application memory and the database before a workload starts.
# Atomic BIN V2 uses the same 100,000-entry manifest ceiling.
MAX_DATAGEN_CHUNKS = 100000
DATAGEN_CHUNK_CREATE_BATCH = 1000

# SQLite does not implement SELECT ... FOR UPDATE and briefly returns
# ``database is locked`` when several workers claim at once.  Claims use a
# compare-and-swap UPDATE and retry only a bounded number of times.  The same
# conditional UPDATE is safe on PostgreSQL and prevents duplicate leases.
DATAGEN_CLAIM_RETRIES = 12
DATAGEN_CLAIM_BACKOFF = 0.01


def is_generic_datagen(test):
    return (
        getattr(test, 'test_mode', None) == 'DATAGEN'
        and bool(getattr(test, 'datagen_command', ''))
    )


def initialize_chunks(test):
    """Create the immutable chunk map for a newly saved generic DATAGEN test."""

    assert test.pk and is_generic_datagen(test)

    total_chunks = test.datagen_total_chunks()
    if not 0 < total_chunks <= MAX_DATAGEN_CHUNKS:
        raise ValueError(
            'Generic DATAGEN workloads must contain between 1 and %d chunks'
            % MAX_DATAGEN_CHUNKS
        )

    # Keep peak memory bounded even at the accepted workload maximum.  The
    # outer transaction also prevents a partially initialized chunk map when a
    # later batch fails.
    with transaction.atomic():
        chunks = []
        for idx in range(total_chunks):
            offset = idx * test.datagen_positions_per_chunk
            count = min(
                test.datagen_positions_per_chunk,
                test.datagen_total_count - offset,
            )
            chunks.append(DatagenChunk(test=test, idx=idx, position_count=count))

            if len(chunks) == DATAGEN_CHUNK_CREATE_BATCH:
                DatagenChunk.objects.bulk_create(
                    chunks, batch_size=DATAGEN_CHUNK_CREATE_BATCH
                )
                chunks = []

        if chunks:
            DatagenChunk.objects.bulk_create(
                chunks, batch_size=DATAGEN_CHUNK_CREATE_BATCH
            )


def assignable_chunks(test, now=None):
    """Return pending chunks plus running chunks whose heartbeat lease expired."""

    now = now or timezone.now()
    stale_before = now - DATAGEN_LEASE
    return DatagenChunk.objects.filter(test=test).filter(
        Q(status=DatagenChunk.PENDING)
        | Q(status=DatagenChunk.RUNNING, assigned__lt=stale_before)
        | Q(status=DatagenChunk.RUNNING, assigned__isnull=True)
    )


def has_assignable_chunk(test):
    return not is_generic_datagen(test) or assignable_chunks(test).exists()


def _next_claim_candidate(test, now):
    """Return only the fields needed by the claim compare-and-swap."""

    return (
        assignable_chunks(test, now)
        .only(
            'id', 'test_id', 'idx', 'position_count', 'status', 'assigned',
            'attempts',
        )
        .order_by('idx')
        .first()
    )


def _is_sqlite_lock_contention(error):
    message = str(error).lower()
    return 'database is locked' in message or 'database table is locked' in message


def claim_chunk(test, machine):
    """Atomically lease one chunk to a machine, reclaiming stale work if needed.

    The conditional UPDATE is the serialization point.  In particular, this
    does not rely on ``select_for_update()``, which is a no-op on SQLite.
    """

    if not is_generic_datagen(test):
        return None

    for attempt in range(DATAGEN_CLAIM_RETRIES):
        try:
            now = timezone.now()
            chunk = _next_claim_candidate(test, now)
            if chunk is None:
                return None

            expected_state = Q(status=DatagenChunk.PENDING)
            if chunk.status == DatagenChunk.RUNNING:
                expected_state = Q(
                    status=DatagenChunk.RUNNING,
                    assigned=chunk.assigned,
                )

            claimed = (
                DatagenChunk.objects.filter(
                    pk=chunk.pk,
                    test_id=test.pk,
                    test__finished=False,
                    test__deleted=False,
                )
                .filter(expected_state)
                .update(
                    status=DatagenChunk.RUNNING,
                    machine=machine,
                    assigned=now,
                    completed=None,
                    sha256='',
                    bytes=0,
                    attempts=F('attempts') + 1,
                    last_error='',
                )
            )
            if claimed:
                # Avoid a second database operation after the successful CAS:
                # retrying a failed refresh could otherwise lease an additional
                # chunk to the same request.  These are all fields consumed by
                # workload serialization.
                chunk.status = DatagenChunk.RUNNING
                chunk.machine = machine
                chunk.machine_id = machine.id
                chunk.assigned = now
                chunk.completed = None
                chunk.sha256 = ''
                chunk.bytes = 0
                chunk.attempts += 1
                chunk.last_error = ''
                return chunk

            # Another worker won the compare-and-swap.  Re-read the queue and
            # try the next available chunk without holding any transaction.
            continue

        except OperationalError as error:
            # Do not retry arbitrary connection/commit errors: on PostgreSQL an
            # unknown commit outcome must never lead this request to claim a
            # second chunk.  SQLite's explicit BUSY/locked errors mean the
            # statement did not commit and are safe to retry.
            if not _is_sqlite_lock_contention(error):
                raise
            if attempt + 1 == DATAGEN_CLAIM_RETRIES:
                return None
            time.sleep(min(DATAGEN_CLAIM_BACKOFF * (attempt + 1), 0.05))

    return None


def renew_chunk(test_id, chunk_idx, machine):
    """Renew an active lease. False tells a stale/duplicate client to stop."""

    # This UPDATE is the ownership check and renewal in one database statement.
    # A read followed by save() is unsafe on SQLite because select_for_update()
    # is a no-op there: a stale heartbeat could otherwise overwrite a lease
    # reclaimed by another machine between those two operations.
    return DatagenChunk.objects.filter(
        test_id=test_id,
        idx=chunk_idx,
        status=DatagenChunk.RUNNING,
        machine_id=machine.id,
        test__finished=False,
        test__deleted=False,
    ).update(assigned=timezone.now()) == 1


def requeue_chunk(test_id, chunk_idx, machine, error=''):
    """Release only the lease owned by this machine; completed data is immutable."""

    # Keep the ownership predicate in the UPDATE itself.  This prevents a late
    # error report from an expired worker from clobbering a newer lease.
    return DatagenChunk.objects.filter(
        test_id=test_id,
        idx=chunk_idx,
        status=DatagenChunk.RUNNING,
        machine_id=machine.id,
    ).update(
        status=DatagenChunk.PENDING,
        machine=None,
        assigned=None,
        completed=None,
        sha256='',
        bytes=0,
        last_error=error[:4096],
    ) == 1


def requeue_running_chunks(test):
    """Make a manually stopped DATAGEN test immediately restartable."""

    if not is_generic_datagen(test):
        return

    DatagenChunk.objects.filter(
        test=test, status=DatagenChunk.RUNNING
    ).update(
        status=DatagenChunk.PENDING,
        machine=None,
        assigned=None,
        last_error='Requeued by workload restart',
    )


def completed_progress(test):
    # Submission maintains both counters atomically after the chunk CAS.  Do
    # not rescan every completed row after every upload: that becomes O(n^2)
    # over large campaigns and holds the Test write lock unnecessarily.
    return (
        test.datagen_completed_chunks,
        test.datagen_total_chunks(),
        test.games,
    )
