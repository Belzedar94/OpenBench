import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from OpenBench.models import DatagenChunk, Test


# Heartbeats arrive every 30 seconds. Five minutes tolerates transient network
# failures while still making abandoned chunks available again promptly.
DATAGEN_LEASE = datetime.timedelta(minutes=5)


def is_generic_datagen(test):
    return (
        getattr(test, 'test_mode', None) == 'DATAGEN'
        and bool(getattr(test, 'datagen_command', ''))
    )


def initialize_chunks(test):
    """Create the immutable chunk map for a newly saved generic DATAGEN test."""

    assert test.pk and is_generic_datagen(test)

    chunks = []
    for idx in range(test.datagen_total_chunks()):
        offset = idx * test.datagen_positions_per_chunk
        count = min(
            test.datagen_positions_per_chunk,
            test.datagen_total_count - offset,
        )
        chunks.append(DatagenChunk(test=test, idx=idx, position_count=count))

    DatagenChunk.objects.bulk_create(chunks)


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


def claim_chunk(test, machine):
    """Atomically lease one chunk to a machine, reclaiming stale work if needed."""

    if not is_generic_datagen(test):
        return None

    with transaction.atomic():
        locked_test = Test.objects.select_for_update().get(pk=test.pk)
        if locked_test.finished or locked_test.deleted:
            return None

        chunk = (
            assignable_chunks(locked_test)
            .select_for_update()
            .order_by('idx')
            .first()
        )
        if chunk is None:
            return None

        chunk.status = DatagenChunk.RUNNING
        chunk.machine = machine
        chunk.assigned = timezone.now()
        chunk.completed = None
        chunk.sha256 = ''
        chunk.bytes = 0
        chunk.attempts += 1
        chunk.last_error = ''
        chunk.save()
        return chunk


def renew_chunk(test_id, chunk_idx, machine):
    """Renew an active lease. False tells a stale/duplicate client to stop."""

    with transaction.atomic():
        chunk = (
            DatagenChunk.objects.select_for_update()
            .filter(test_id=test_id, idx=chunk_idx)
            .first()
        )
        if (
            chunk is None
            or chunk.status != DatagenChunk.RUNNING
            or chunk.machine_id != machine.id
        ):
            return False

        chunk.assigned = timezone.now()
        chunk.save(update_fields=['assigned'])
        return True


def requeue_chunk(test_id, chunk_idx, machine, error=''):
    """Release only the lease owned by this machine; completed data is immutable."""

    with transaction.atomic():
        chunk = (
            DatagenChunk.objects.select_for_update()
            .filter(test_id=test_id, idx=chunk_idx)
            .first()
        )
        if (
            chunk is None
            or chunk.status != DatagenChunk.RUNNING
            or chunk.machine_id != machine.id
        ):
            return False

        chunk.status = DatagenChunk.PENDING
        chunk.machine = None
        chunk.assigned = None
        chunk.last_error = error[:4096]
        chunk.save()
        return True


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
    completed = DatagenChunk.objects.filter(
        test=test, status=DatagenChunk.COMPLETED
    )
    return completed.count(), test.datagen_total_chunks(), sum(
        completed.values_list('position_count', flat=True)
    )
