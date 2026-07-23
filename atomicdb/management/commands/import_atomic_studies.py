"""Import content-pinned Atomic theory as shadow scheduling provenance."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from atomicdb import theory_import


class Command(BaseCommand):
    help = (
        'Validate pinned Atomic study/evidence manifests and optionally import '
        'SchedulingCohort/CohortMembership shadow rows. Dry-run is the default.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--cohort-manifest',
            default=str(theory_import.DEFAULT_COHORT_MANIFEST),
            help='Path to the content-pinned 21-study cohort manifest.')
        parser.add_argument(
            '--cohort-sha256',
            default=theory_import.DEFAULT_COHORT_MANIFEST_SHA256,
            help='Required SHA-256 for --cohort-manifest.')
        parser.add_argument(
            '--priority-manifest',
            default=str(theory_import.DEFAULT_PRIORITY_MANIFEST),
            help='Path to the content-pinned priority-evidence manifest.')
        parser.add_argument(
            '--priority-sha256',
            default=theory_import.DEFAULT_PRIORITY_MANIFEST_SHA256,
            help='Required SHA-256 for --priority-manifest.')
        parser.add_argument(
            '--scheduler-manifest',
            default=str(theory_import.DEFAULT_SCHEDULER_MANIFEST),
            help='Path to the canonical self-hashed executable seed manifest.')
        parser.add_argument(
            '--scheduler-sha256',
            default=theory_import.DEFAULT_SCHEDULER_MANIFEST_SHA256,
            help='Required canonical SHA-256 for --scheduler-manifest.')
        parser.add_argument(
            '--study-root',
            default=str(theory_import.DEFAULT_STUDY_ROOT),
            help='Directory containing exactly the 21 pinned <study_id>.pgn.')
        parser.add_argument(
            '--apply', action='store_true',
            help='Write only SchedulingCohort/CohortMembership rows.')
        parser.add_argument(
            '--receipt', default=None,
            help=(
                'No-overwrite final UTF-8 JSON receipt path. Required with '
                '--apply; a .preflight companion is sealed before database '
                'writes and the requested final receipt after commit.'))

    def handle(self, *args, **options):
        try:
            plan = theory_import.load_import_plan(
                cohort_manifest=options['cohort_manifest'],
                priority_manifest=options['priority_manifest'],
                scheduler_manifest=options['scheduler_manifest'],
                study_root=options['study_root'],
                cohort_sha256=options['cohort_sha256'],
                priority_sha256=options['priority_sha256'],
                scheduler_sha256=options['scheduler_sha256'],
            )
            database = None
            mode = 'dry-run'
            if options['apply']:
                if not options['receipt']:
                    raise theory_import.TheoryImportError(
                        '--apply requires a no-overwrite --receipt path')
                final_path = Path(options['receipt'])
                if final_path.exists():
                    raise theory_import.TheoryImportError(
                        f'receipt already exists: {final_path}')
                preflight_path = theory_import.preflight_receipt_path(
                    final_path)
                preflight = plan.receipt(mode='preflight')
                preflight_sha256 = theory_import.write_receipt(
                    preflight_path, preflight)
                database = theory_import.apply_import_plan(plan)
                mode = 'applied'
            receipt = plan.receipt(mode=mode, database=database)
            if options['apply']:
                receipt['preflight_receipt'] = {
                    'path': str(preflight_path),
                    'sha256': preflight_sha256,
                }
                final_sha256 = theory_import.write_receipt(
                    final_path, receipt)
                output_receipt = dict(receipt)
                output_receipt['final_receipt'] = {
                    'path': str(final_path),
                    'sha256': final_sha256,
                }
            elif options['receipt']:
                theory_import.write_receipt(options['receipt'], receipt)
                output_receipt = receipt
            else:
                output_receipt = receipt
        except theory_import.TheoryImportError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(
            output_receipt, ensure_ascii=False, sort_keys=True))
