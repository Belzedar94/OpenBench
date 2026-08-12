import datetime
import hashlib
import json
import re
import time

from django.db import OperationalError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from OpenBench.models import (
    DatagenChunk, DatagenProducerArtifact, DatagenProducerBuild,
    DatagenProducerOwnerQuota, DatagenProducerQuota, Test,
)
from OpenBench.datagen_publication import (
    DATAGEN_PUBLICATION_LEASE_SCHEMA,
    DATAGEN_PUBLICATION_PROTOCOL,
    DATAGEN_PUBLICATION_PROTOCOL_V42,
    canonical_json_sha256,
    publication_lease_schema,
)


# Heartbeats arrive every 30 seconds. Five minutes tolerates transient network
# failures while still making abandoned chunks available again promptly.
DATAGEN_LEASE = datetime.timedelta(minutes=5)

# A chunk is a persistent scheduler row, so accepting an unbounded count can
# exhaust both application memory and the database before a workload starts.
# Atomic BIN V2 uses the same 100,000-entry manifest ceiling.
MAX_DATAGEN_CHUNKS = 100000
DATAGEN_CHUNK_CREATE_BATCH = 1000

# Producer executables are build evidence, not dataset payloads.  Bound them
# independently so a compromised worker cannot use the CAS endpoint as an
# unbounded second artifact channel.
MAX_DATAGEN_PRODUCER_BYTES = 2 * 1024 * 1024 * 1024
MAX_DATAGEN_PRODUCER_REQUEST_BYTES = (
    MAX_DATAGEN_PRODUCER_BYTES + 8 * 1024 * 1024
)

# A publication campaign may legitimately contain distinct binaries built for
# different worker architectures/toolchains. Bound that authenticated build set
# so a compromised worker cannot turn producer evidence into unbounded storage.
MAX_DATAGEN_PRODUCERS_PER_CAMPAIGN = 256
MAX_DATAGEN_PRODUCER_BYTES_PER_CAMPAIGN = 16 * 1024 * 1024 * 1024

# Physical CAS quota and logical per-owner reservations.  Campaign quotas are
# smaller and remain the first line of defence; these caps bound aggregate
# storage even when many campaigns are created by one or many accounts.
MAX_DATAGEN_PRODUCERS_GLOBAL = 4096
MAX_DATAGEN_PRODUCER_BYTES_GLOBAL = 256 * 1024 * 1024 * 1024
MAX_DATAGEN_PRODUCERS_PER_OWNER = 1024
MAX_DATAGEN_PRODUCER_BYTES_PER_OWNER = 64 * 1024 * 1024 * 1024

# ``Test.max_games`` remains a signed 32-bit IntegerField for historical
# gameplay workloads. Generic DATAGEN keeps its canonical 64-bit total in
# ``datagen_total_count`` and only mirrors a saturated summary into max_games.
MAX_LEGACY_DATAGEN_GAMES = (1 << 31) - 1

# SQLite does not implement SELECT ... FOR UPDATE and briefly returns
# ``database is locked`` when several workers claim at once.  Claims use a
# compare-and-swap UPDATE and retry only a bounded number of times.  The same
# conditional UPDATE is safe on PostgreSQL and prevents duplicate leases.
DATAGEN_CLAIM_RETRIES = 12
DATAGEN_CLAIM_BACKOFF = 0.01

DATAGEN_TABLEBASE_LEASE_SCHEMA = 'openbench-datagen-tablebase-lease-v40'
ATOMIC_DATAGEN_TABLEBASE_MIN = 3
ATOMIC_DATAGEN_TABLEBASE_MAX = 6


def valid_atomic_datagen_tablebase_max(value):
    """Return whether ``value`` is representable by the pinned v40 corpus."""

    return (
        type(value) is int
        and ATOMIC_DATAGEN_TABLEBASE_MIN
        <= value
        <= ATOMIC_DATAGEN_TABLEBASE_MAX
    )


def valid_atomic_datagen_tablebase_contract(test):
    """Validate the frozen Atomic-only tablebase contract for protocol v40."""

    maximum = getattr(test, 'datagen_tablebase_max', None)
    manifest = getattr(test, 'datagen_tablebase_manifest_sha256', '')
    contract = getattr(test, 'datagen_environment_contract_sha256', '')
    return (
        bool(getattr(test, 'datagen_tablebase_required', False))
        and getattr(test, 'datagen_tablebase_family', '') == 'atomic'
        and valid_atomic_datagen_tablebase_max(maximum)
        and getattr(test, 'syzygy_wdl', '') == '%d-MAN' % maximum
        and getattr(test, 'syzygy_adj', '') == 'DISABLED'
        and getattr(test, 'datagen_teacher_mode', '') in {'pure', 'true'}
        and isinstance(manifest, str)
        and re.fullmatch(r'[0-9a-f]{64}', manifest) is not None
        and isinstance(contract, str)
        and re.fullmatch(r'[0-9a-f]{64}', contract) is not None
        and test.datagen_environment_contract_is_current()
    )


def is_generic_datagen(test):
    return (
        getattr(test, 'test_mode', None) == 'DATAGEN'
        and bool(getattr(test, 'datagen_command', ''))
    )


def machine_datagen_publication_protocols(machine):
    """Return authenticated worker support; missing legacy clients mean v41."""

    info = getattr(machine, 'info', {})
    if not isinstance(info, dict):
        return frozenset()
    if 'datagen_publication_protocols' not in info:
        return frozenset({DATAGEN_PUBLICATION_PROTOCOL})
    advertised = info['datagen_publication_protocols']
    if not isinstance(advertised, list):
        return frozenset()
    if any(type(value) is not int for value in advertised):
        return frozenset()
    return frozenset(advertised)


def valid_publication_assignment(test, machine):
    return (
        not test.is_publication_datagen()
        or test.datagen_publication_protocol
        in machine_datagen_publication_protocols(machine)
    )


def initialize_chunks(test):
    """Create the immutable chunk map for a newly saved generic DATAGEN test."""

    assert test.pk and is_generic_datagen(test)

    # Keep peak memory bounded even at the accepted workload maximum.  The
    # outer transaction also prevents a partially initialized chunk map when a
    # later batch fails.
    with transaction.atomic():
        persisted = Test.objects.select_for_update().get(pk=test.pk)
        total_chunks = persisted.datagen_total_chunks()
        if not 0 < total_chunks <= MAX_DATAGEN_CHUNKS:
            raise ValueError(
                'Generic DATAGEN workloads must contain between 1 and %d chunks'
                % MAX_DATAGEN_CHUNKS
            )
        # Freeze the producer/environment contracts and authenticate the v41
        # publication contract in the same transaction as the immutable chunk
        # map. Editing command text later cannot silently weaken provenance.
        persisted.freeze_datagen_producer_contract()
        persisted.freeze_datagen_environment_contract(
            persisted.datagen_tablebase_family,
            persisted.datagen_tablebase_max,
            persisted.datagen_tablebase_manifest_sha256,
            persisted.datagen_teacher_mode,
        )
        if (
            persisted.datagen_tablebase_required
            and not valid_atomic_datagen_tablebase_contract(persisted)
        ):
            raise ValueError(
                'Authenticated Atomic DATAGEN requires a frozen 3-MAN through '
                '6-MAN tablebase contract'
            )
        if not persisted.datagen_publication_contract_is_current():
            raise ValueError(
                'DATAGEN publication contract is missing, malformed, or stale'
            )
        Test.objects.filter(pk=test.pk).update(
            datagen_producer_required=persisted.datagen_producer_required,
            datagen_producer_contract_sha256=(
                persisted.datagen_producer_contract_sha256
            ),
            datagen_tablebase_required=persisted.datagen_tablebase_required,
            datagen_tablebase_family=persisted.datagen_tablebase_family,
            datagen_tablebase_max=persisted.datagen_tablebase_max,
            datagen_tablebase_manifest_sha256=(
                persisted.datagen_tablebase_manifest_sha256
            ),
            datagen_teacher_mode=persisted.datagen_teacher_mode,
            datagen_environment_contract_sha256=(
                persisted.datagen_environment_contract_sha256
            ),
        )
        test.datagen_producer_required = persisted.datagen_producer_required
        test.datagen_producer_contract_sha256 = (
            persisted.datagen_producer_contract_sha256
        )
        test.datagen_tablebase_required = persisted.datagen_tablebase_required
        test.datagen_tablebase_family = persisted.datagen_tablebase_family
        test.datagen_tablebase_max = persisted.datagen_tablebase_max
        test.datagen_tablebase_manifest_sha256 = (
            persisted.datagen_tablebase_manifest_sha256
        )
        test.datagen_teacher_mode = persisted.datagen_teacher_mode
        test.datagen_environment_contract_sha256 = (
            persisted.datagen_environment_contract_sha256
        )
        chunks = []
        for idx in range(total_chunks):
            offset = idx * persisted.datagen_positions_per_chunk
            count = min(
                persisted.datagen_positions_per_chunk,
                persisted.datagen_total_count - offset,
            )
            chunks.append(DatagenChunk(
                test=persisted, idx=idx, position_count=count
            ))

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
    return (
        not is_generic_datagen(test)
        or (
            test.datagen_producer_contract_is_current()
            and test.datagen_environment_contract_is_current()
            and test.datagen_publication_contract_is_current()
            and assignable_chunks(test).exists()
        )
    )


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


def tablebase_lease(test, machine, chunk_idx, lease_attempt):
    """Freeze v40 tablebase or v41 publication evidence at claim time."""

    publication = test.is_publication_datagen()
    if not test.datagen_tablebase_required and not publication:
        return {}, ''
    if publication and not test.datagen_publication_contract_is_current():
        raise ValueError('DATAGEN publication contract is stale')
    if publication and not valid_publication_assignment(test, machine):
        raise ValueError(
            'Worker does not advertise the publication protocol'
        )

    family = test.datagen_tablebase_family
    worker_max = 0
    manifest = None
    if test.datagen_tablebase_required:
        capability = machine.info.get('tablebases', {}).get(family, {})
        if not isinstance(capability, dict):
            raise ValueError('Worker lacks authenticated tablebase inventory')
        worker_max = int(capability.get('max', 0))
        manifest = capability.get('manifest_sha256')
        manifest = manifest.lower() if isinstance(manifest, str) else None
        if (
            not valid_atomic_datagen_tablebase_contract(test)
            or worker_max < test.datagen_tablebase_max
            or manifest != test.datagen_tablebase_manifest_sha256.lower()
        ):
            raise ValueError('Worker tablebase capability does not match campaign')

    if publication:
        if (
            test.datagen_publication_protocol
            == DATAGEN_PUBLICATION_PROTOCOL_V42
        ):
            threads = machine.info.get('concurrency')
            if type(threads) is not int or threads <= 0:
                raise ValueError('Worker has invalid DATAGEN concurrency')
            lease = {
                'schema': publication_lease_schema(
                    DATAGEN_PUBLICATION_PROTOCOL_V42
                ),
                'protocol': DATAGEN_PUBLICATION_PROTOCOL_V42,
                'test_id': test.id,
                'chunk_idx': chunk_idx,
                'attempt': lease_attempt,
                'machine_id': machine.id,
                'publication_contract_sha256': (
                    test.datagen_publication_contract_sha256.lower()
                ),
                'environment_contract_sha256': (
                    test.datagen_environment_contract_sha256.lower()
                ),
                'threads': threads,
                'teacher_id': test.datagen_teacher_id,
                'network_kind': 'none',
            }
            return lease, canonical_json_sha256(lease)

        lease = {
            'schema': DATAGEN_PUBLICATION_LEASE_SCHEMA,
            'protocol': DATAGEN_PUBLICATION_PROTOCOL,
            'test_id': test.id,
            'chunk_idx': chunk_idx,
            'attempt': lease_attempt,
            'machine_id': machine.id,
            'publication_contract_sha256': (
                test.datagen_publication_contract_sha256.lower()
            ),
            'environment_contract_sha256': (
                test.datagen_environment_contract_sha256.lower()
            ),
            'tablebase': {
                'required': test.datagen_tablebase_required,
                'family': family or None,
                'required_max': test.datagen_tablebase_max,
                'worker_max': worker_max,
                'manifest_sha256': manifest,
            },
            'teacher_mode': test.datagen_teacher_mode or None,
        }
        return lease, canonical_json_sha256(lease)

    lease = {
        'schema': DATAGEN_TABLEBASE_LEASE_SCHEMA,
        'protocol': 40,
        'test_id': test.id,
        'chunk_idx': chunk_idx,
        'attempt': lease_attempt,
        'machine_id': machine.id,
        'environment_contract_sha256': (
            test.datagen_environment_contract_sha256.lower()
        ),
        'tablebase': {
            'family': family,
            'required_max': test.datagen_tablebase_max,
            'worker_max': worker_max,
            'manifest_sha256': manifest,
        },
        'teacher_mode': test.datagen_teacher_mode,
    }
    encoded = json.dumps(
        lease, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return lease, hashlib.sha256(encoded).hexdigest()


def claim_chunk(test, machine):
    """Atomically lease one chunk to a machine, reclaiming stale work if needed.

    The conditional UPDATE is the serialization point.  In particular, this
    does not rely on ``select_for_update()``, which is a no-op on SQLite.
    """

    if (
        not is_generic_datagen(test)
        or not test.datagen_producer_contract_is_current()
        or not test.datagen_environment_contract_is_current()
        or not test.datagen_publication_contract_is_current()
    ):
        return None

    for attempt in range(DATAGEN_CLAIM_RETRIES):
        try:
            # Keep related-table predicates out of the chunk UPDATE below.
            # Django implements cross-table UPDATE filters through a subquery;
            # on PostgreSQL that subquery can retain a stale PENDING snapshot
            # while waiting for another claimant's row lock, allowing both
            # statements to report success for the same chunk.
            if not Test.objects.filter(
                pk=test.pk, finished=False, deleted=False,
            ).exists():
                return None
            now = timezone.now()
            chunk = _next_claim_candidate(test, now)
            if chunk is None:
                return None
            try:
                environment_lease, environment_lease_sha256 = tablebase_lease(
                    test, machine, chunk.idx, chunk.attempts + 1
                )
            except (TypeError, ValueError):
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
                )
                .filter(expected_state)
                .update(
                    status=DatagenChunk.RUNNING,
                    machine=machine,
                    assigned=now,
                    completed=None,
                    sha256='',
                    bytes=0,
                    producer_sha256='',
                    producer_bytes=0,
                    producer_commit='',
                    producer_build=None,
                    environment_receipt={},
                    environment_receipt_sha256='',
                    environment_lease=environment_lease,
                    environment_lease_sha256=environment_lease_sha256,
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
                chunk.producer_sha256 = ''
                chunk.producer_bytes = 0
                chunk.producer_commit = ''
                chunk.producer_build = None
                chunk.producer_build_id = None
                chunk.environment_receipt = {}
                chunk.environment_receipt_sha256 = ''
                chunk.environment_lease = environment_lease
                chunk.environment_lease_sha256 = environment_lease_sha256
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


def renew_chunk(test_id, chunk_idx, machine, lease_attempt):
    """Renew an active lease. False tells a stale/duplicate client to stop."""

    # As in claim_chunk(), the ownership CAS must contain only columns from
    # DatagenChunk so PostgreSQL rechecks them after waiting on a row lock.
    if not Test.objects.filter(
        pk=test_id, finished=False, deleted=False,
    ).exists():
        return False

    # This UPDATE is the ownership check and renewal in one database statement.
    # A read followed by save() is unsafe on SQLite because select_for_update()
    # is a no-op there: a stale heartbeat could otherwise overwrite a lease
    # reclaimed by another machine between those two operations.
    return DatagenChunk.objects.filter(
        test_id=test_id,
        idx=chunk_idx,
        status=DatagenChunk.RUNNING,
        machine_id=machine.id,
        attempts=lease_attempt,
    ).update(assigned=timezone.now()) == 1


def requeue_chunk(test_id, chunk_idx, machine, lease_attempt, error=''):
    """Release only the lease owned by this machine; completed data is immutable."""

    # Keep the ownership predicate in the UPDATE itself.  This prevents a late
    # error report from an expired worker from clobbering a newer lease.
    return DatagenChunk.objects.filter(
        test_id=test_id,
        idx=chunk_idx,
        status=DatagenChunk.RUNNING,
        machine_id=machine.id,
        attempts=lease_attempt,
    ).update(
        status=DatagenChunk.PENDING,
        machine=None,
        assigned=None,
        completed=None,
        sha256='',
        bytes=0,
        producer_sha256='',
        producer_bytes=0,
        producer_commit='',
        producer_build=None,
        environment_receipt={},
        environment_receipt_sha256='',
        environment_lease={},
        environment_lease_sha256='',
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
        producer_sha256='',
        producer_bytes=0,
        producer_commit='',
        producer_build=None,
        environment_receipt={},
        environment_receipt_sha256='',
        environment_lease={},
        environment_lease_sha256='',
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


def rebuild_producer_quota_counters():
    """Recompute cached quota/refcount counters from authoritative relations.

    The admission path updates these in O(1).  This idempotent repair primitive
    is intentionally separate so a crash, manual database edit, or legacy
    migration can be reconciled without trusting cached totals.
    """

    with transaction.atomic():
        # Match the admission lock order: campaign -> global -> owner -> CAS.
        # Acquiring every campaign is acceptable for this offline reconciler
        # and avoids a PostgreSQL deadlock with a live reservation.
        list(
            Test.objects.select_for_update().order_by('pk')
            .values_list('pk', flat=True)
        )
        global_quota, _ = DatagenProducerQuota.objects.select_for_update().get_or_create(
            key='global'
        )
        artifacts = DatagenProducerArtifact.objects.all()
        totals = artifacts.aggregate(bytes=Sum('bytes'))
        global_quota.artifact_count = artifacts.count()
        global_quota.reserved_bytes = totals['bytes'] or 0
        global_quota.save(update_fields=[
            'artifact_count', 'reserved_bytes', 'updated',
        ])

        Test.objects.update(
            datagen_producer_build_count=0,
            datagen_producer_build_bytes=0,
        )
        DatagenProducerOwnerQuota.objects.all().delete()
        test_totals = {}
        owner_totals = {}
        for build in DatagenProducerBuild.objects.select_related('artifact'):
            count, byte_count = test_totals.get(build.test_id, (0, 0))
            test_totals[build.test_id] = (
                count + 1, byte_count + build.artifact.bytes,
            )
            count, byte_count = owner_totals.get(build.owner_id, (0, 0))
            owner_totals[build.owner_id] = (
                count + 1, byte_count + build.artifact.bytes,
            )
        for test_id, (count, byte_count) in test_totals.items():
            Test.objects.filter(pk=test_id).update(
                datagen_producer_build_count=count,
                datagen_producer_build_bytes=byte_count,
            )
        DatagenProducerOwnerQuota.objects.bulk_create([
            DatagenProducerOwnerQuota(
                owner_id=owner_id,
                build_count=count,
                reserved_bytes=byte_count,
            )
            for owner_id, (count, byte_count) in owner_totals.items()
        ])

        DatagenProducerArtifact.objects.update(reference_count=0)
        for row in (
            DatagenProducerBuild.objects.values('artifact_id')
            .annotate(total=Count('id'))
        ):
            DatagenProducerArtifact.objects.filter(pk=row['artifact_id']).update(
                reference_count=row['total']
            )
