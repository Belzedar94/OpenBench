import datetime
import hashlib
import os
import re
import stat

from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

import OpenBench.datagen
from OpenBench.models import (
    DatagenProducerArtifact, DatagenProducerBuild, DatagenProducerQuota,
)


def regular_identity(path):
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OSError('not a regular file')
    digest = hashlib.sha256()
    count = 0
    with open(path, 'rb') as source:
        opened = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError('file changed while opening')
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            count += len(block)
        after = os.fstat(source.fileno())
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or count != after.st_size
    ):
        raise OSError('file changed while hashing')
    return digest.hexdigest(), count


def cached_regular(path, expected_bytes):
    try:
        value = os.lstat(path)
        return (
            stat.S_ISREG(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and value.st_size == expected_bytes
        )
    except OSError:
        return False


def durable_replace(source, destination):
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    os.replace(source, destination)
    with open(destination, 'rb+' if os.name == 'nt' else 'rb') as promoted:
        os.fsync(promoted.fileno())
    if os.name != 'nt':
        directory = os.open(os.path.dirname(destination), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class Command(BaseCommand):
    help = 'Scrub, repair and garbage-collect DATAGEN producer CAS state'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--scrub', action='store_true')
        parser.add_argument('--staging-max-age-hours', type=int, default=24)
        parser.add_argument('--retention-days', type=int, default=30)

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        scrub = options['scrub']
        now = timezone.now()
        staging_cutoff = now - datetime.timedelta(
            hours=max(1, options['staging_max_age_hours'])
        )
        retention_cutoff = now - datetime.timedelta(
            days=max(1, options['retention_days'])
        )
        storage = FileSystemStorage()
        canonical_root = storage.path('datagen-producers/sha256')
        staging_root = storage.path('datagen-producers/.staging')
        stats = {
            'available': 0, 'repaired': 0, 'corrupt': 0,
            'builds_retired': 0, 'artifacts_deleted': 0,
            'orphans_deleted': 0,
        }

        # Retention applies only after a campaign is complete/deleted. Active
        # reservations survive requeue forever and therefore cannot be used to
        # cycle through quota with one chunk.
        retired = DatagenProducerBuild.objects.filter(
            test__updated__lt=retention_cutoff,
        ).filter(test__finished=True) | DatagenProducerBuild.objects.filter(
            test__updated__lt=retention_cutoff,
            test__deleted=True,
        )
        retired_ids = list(retired.values_list('id', flat=True))
        stats['builds_retired'] = len(retired_ids)
        if retired_ids and not dry_run:
            with transaction.atomic():
                DatagenProducerBuild.objects.filter(id__in=retired_ids).delete()
            OpenBench.datagen.rebuild_producer_quota_counters()

        artifacts = list(DatagenProducerArtifact.objects.all())
        known_staging = {
            artifact.staging_name for artifact in artifacts
            if artifact.staging_name
        }
        for artifact in artifacts:
            canonical = storage.path(artifact.filename())
            staging = (
                storage.path(artifact.staging_name)
                if artifact.staging_name else None
            )

            # A producer request may be between its durable DB reservation and
            # atomic rename. Never compete with a fresh publisher; only recover
            # STAGING rows whose lease window has clearly expired.
            if (
                artifact.state == DatagenProducerArtifact.STAGING
                and artifact.updated >= staging_cutoff
            ):
                continue

            identity = None
            if cached_regular(canonical, artifact.bytes):
                if scrub or artifact.state != DatagenProducerArtifact.AVAILABLE:
                    try:
                        identity = regular_identity(canonical)
                    except OSError:
                        identity = None
                else:
                    identity = (artifact.sha256, artifact.bytes)
            if identity == (artifact.sha256, artifact.bytes):
                stats['available'] += 1
                if (
                    not dry_run
                    and (
                        artifact.state != DatagenProducerArtifact.AVAILABLE
                        or artifact.staging_name
                        or scrub
                    )
                ):
                    DatagenProducerArtifact.objects.filter(
                        pk=artifact.pk,
                        state=artifact.state,
                        updated=artifact.updated,
                    ).update(
                        state=DatagenProducerArtifact.AVAILABLE,
                        staging_name='',
                        last_verified=now,
                        updated=now,
                    )
                continue

            staged_identity = None
            if staging and cached_regular(staging, artifact.bytes):
                try:
                    staged_identity = regular_identity(staging)
                except OSError:
                    staged_identity = None
            if staged_identity == (artifact.sha256, artifact.bytes):
                stats['repaired'] += 1
                if not dry_run:
                    durable_replace(staging, canonical)
                    DatagenProducerArtifact.objects.filter(
                        pk=artifact.pk,
                        state=artifact.state,
                        updated=artifact.updated,
                    ).update(
                        state=DatagenProducerArtifact.AVAILABLE,
                        staging_name='',
                        last_verified=now,
                        updated=now,
                    )
                continue

            stats['corrupt'] += 1
            if not dry_run:
                DatagenProducerArtifact.objects.filter(
                    pk=artifact.pk,
                    state=artifact.state,
                    updated=artifact.updated,
                ).update(
                    state=DatagenProducerArtifact.CORRUPT,
                    updated=now,
                )

        if os.path.isdir(staging_root):
            for entry in os.scandir(staging_root):
                relative = 'datagen-producers/.staging/%s' % entry.name
                if relative in known_staging:
                    continue
                try:
                    modified = datetime.datetime.fromtimestamp(
                        entry.stat(follow_symlinks=False).st_mtime,
                        tz=datetime.timezone.utc,
                    )
                    stale = modified < staging_cutoff
                except OSError:
                    stale = True
                if stale:
                    stats['orphans_deleted'] += 1
                    if not dry_run:
                        try:
                            os.unlink(entry.path)
                        except OSError:
                            pass

        # Delete unreferenced corrupt/retired rows, then their exact CAS paths.
        deletable = list(
            DatagenProducerArtifact.objects.filter(
                # The FK is authoritative.  A stale cached reference_count is
                # one of the inconsistencies this command must repair; using
                # it here can otherwise raise ProtectedError and abort scrub.
                campaign_builds__isnull=True,
                updated__lt=retention_cutoff,
            ).values_list('id', 'sha256')
        )
        for artifact_id, sha256 in deletable:
            descriptor = DatagenProducerArtifact(sha256=sha256, bytes=1)
            path = storage.path(descriptor.filename())
            deleted = 1 if dry_run else 0
            if not dry_run:
                # Serialize with admission before rechecking the authoritative
                # FK and removing the canonical path.  Without the global lock,
                # a campaign could reserve the just-deleted hash and publish a
                # new canonical file between DB delete and unlink.
                with transaction.atomic():
                    DatagenProducerQuota.objects.get_or_create(key='global')
                    DatagenProducerQuota.objects.select_for_update().get(
                        key='global'
                    )
                    artifact = (
                        DatagenProducerArtifact.objects.select_for_update()
                        .filter(pk=artifact_id, updated__lt=retention_cutoff)
                        .first()
                    )
                    if (
                        artifact is not None
                        and not DatagenProducerBuild.objects.filter(
                            artifact_id=artifact.id
                        ).exists()
                    ):
                        deleted, _detail = artifact.delete()
                        if deleted:
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
            stats['artifacts_deleted'] += int(bool(deleted))

        # Canonical files with no DB row are never downloadable. Retain them
        # briefly for disaster recovery, then remove by exact basename only.
        known_sha = set(
            DatagenProducerArtifact.objects.values_list('sha256', flat=True)
        )
        if os.path.isdir(canonical_root):
            for first in os.scandir(canonical_root):
                if not first.is_dir(follow_symlinks=False):
                    continue
                for entry in os.scandir(first.path):
                    if (
                        not re.fullmatch(r'[0-9a-f]{64}', entry.name)
                        or entry.name in known_sha
                    ):
                        continue
                    try:
                        modified = datetime.datetime.fromtimestamp(
                            entry.stat(follow_symlinks=False).st_mtime,
                            tz=datetime.timezone.utc,
                        )
                    except OSError:
                        modified = now
                    if modified < retention_cutoff:
                        deleted = dry_run
                        if not dry_run:
                            with transaction.atomic():
                                DatagenProducerQuota.objects.get_or_create(
                                    key='global'
                                )
                                DatagenProducerQuota.objects.select_for_update().get(
                                    key='global'
                                )
                                if not DatagenProducerArtifact.objects.filter(
                                    sha256=entry.name
                                ).exists():
                                    try:
                                        os.unlink(entry.path)
                                        deleted = True
                                    except OSError as error:
                                        self.stderr.write(
                                            'Unable to delete orphan %s: %s'
                                            % (entry.path, error)
                                        )
                        stats['orphans_deleted'] += int(bool(deleted))

        if not dry_run:
            OpenBench.datagen.rebuild_producer_quota_counters()
        self.stdout.write(' '.join(
            '%s=%d' % item for item in sorted(stats.items())
        ))
