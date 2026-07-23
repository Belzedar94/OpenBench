import copy
import hashlib
import json
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest import mock, skipUnless

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from atomicdb import logic, theory_import
from atomicdb.models import CohortMembership, SchedulingCohort


def _minimal_manifests():
    studies = []
    for index in range(21):
        study_id = f'A{index:07d}'
        studies.append({
            'study_id': study_id,
            'url': f'https://lichess.org/study/{study_id}',
            'pgn_sha256': hashlib.sha256(study_id.encode()).hexdigest(),
            'chapters': 1,
            'move_nodes': 1,
            'comments': 0,
            'rav_side_branches': 0,
            'branch_points': 0,
            'parse_errors': 0,
            'bytes': 1,
        })
    totals = {
        'studies': 21,
        'chapters': 161,
        'move_nodes': 21,
        'comments': 0,
        'rav_side_branches': 0,
        'branch_points': 0,
        'parse_errors': 0,
        'bytes': 21,
        'empty_chapters': 2,
    }
    # The validator requires per-study chapter sums to reconcile.  Put the
    # additional 140 empty editorial chapters in the first study.
    studies[0]['chapters'] = 141
    cohort_row = {
        'cohort': '2N=Nf3-f6-Nc3',
        'chapters': 161,
        'move_nodes': 21,
        'comments': 0,
        'rav_side_branches': 0,
        'branch_points': 0,
        'parse_errors': 0,
        'bytes': 21,
        'empty_chapters': 2,
    }
    two_n_fen = (
        'rnbqkbnr/ppppp1pp/5p2/8/8/2N2N2/'
        'PPPPPPPP/R1BQKB1R b KQkq - 1 2')
    cohorts = {
        'schema': 'atomic-the-house-study-cohort-audit-v1',
        'scope': {'study_count': 21, 'chapter_count': 161},
        'canonical_2n': {
            'san': '1.Nf3 f6 2.Nc3',
            'fen': two_n_fen,
            'explicitly_not': '1.Nf3 f6 2.Nd4',
        },
        'corpus_totals': totals,
        'cohort_totals': [cohort_row],
        'studies': studies,
        'valid_but_empty_chapters': [
            {'study_id': studies[0]['study_id'], 'chapter_index': 140},
            {'study_id': studies[0]['study_id'], 'chapter_index': 141},
        ],
        'unavailable_studies': [],
    }
    candidate = {
        'rank': 1,
        'candidate_id': '2n-full-trunk',
        'line': '1.Nf3 f6 2.Nc3',
        'portfolio_role': 'P1 primary trunk',
        'highest_evidence_level': 'E2',
        'evidence': [{
            'source_id': 'HVXNmBDj',
            'url': 'https://lichess.org/study/HVXNmBDj',
            'level': 'E2',
            'artifact_sha256': '',
        }],
    }
    candidates = []
    for rank in range(1, 8):
        item = copy.deepcopy(candidate)
        item['rank'] = rank
        item['candidate_id'] = f'candidate-{rank}'
        item['portfolio_role'] = (
            'P0 canary' if rank <= 3 else
            'P1 critical' if rank <= 5 else 'P2 reserve')
        candidates.append(item)
    priority = {
        'schema_version': 1,
        'evidence_levels': {
            'E0': 'hint', 'E1': 'claim', 'E2': 'study',
            'E3': 'local', 'E4': 'certificate',
        },
        'ranked_candidates': candidates,
        'notation': {
            '2N': {
                'canonical_line': '1.Nf3 f6 2.Nc3',
                'canonical_fen': two_n_fen,
                'must_not_be_confused_with': '1.Nf3 f6 2.Nd4',
            },
        },
    }
    return cohorts, priority


def _hash_data(data):
    payload = json.dumps(data, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _minimal_plan_inputs():
    cohorts, priority = _minimal_manifests()
    cohort_sha = theory_import.DEFAULT_COHORT_MANIFEST_SHA256
    priority_sha = theory_import.DEFAULT_PRIORITY_MANIFEST_SHA256
    study_bundle_sha = theory_import.DEFAULT_STUDY_BUNDLE_SHA256
    entries = []
    for candidate in priority['ranked_candidates']:
        positions = theory_import.resolve_san_line('1.Nf3 f6 2.Nc3')
        root = positions[-1]
        entries.append({
            'candidate_id': candidate['candidate_id'],
            'label': '1.Nf3 f6 2.Nc3',
            'priority_level': (
                'P0' if candidate['rank'] <= 3 else
                'P1' if candidate['rank'] <= 5 else 'P2'),
            'evidence_level': 'E2',
            'start_fen': logic.start_fen(),
            'san': '1.Nf3 f6 2.Nc3',
            'uci': root.path_uci.split(),
            'root_fen': root.fen,
            'root_key': root.position_key,
        })
    scheduler = {
        'schema': 'atomic-theory-scheduler-seeds-v1',
        'policy_version': theory_import.POLICY_VERSION,
        'source_manifests': {
            'study_cohorts_sha256': cohort_sha,
            'priority_evidence_sha256': priority_sha,
            'study_artifacts_sha256': study_bundle_sha,
            'study_count': 21,
        },
        'entries': entries,
    }
    scheduler_sha = theory_import.DEFAULT_SCHEDULER_MANIFEST_SHA256
    return (
        cohorts, priority, scheduler, cohort_sha, priority_sha,
        scheduler_sha, study_bundle_sha,
    )


def _minimal_plan():
    return theory_import.build_import_plan(*_minimal_plan_inputs())


class _Field:
    def __init__(self, name):
        self.name = name


class _Meta:
    def __init__(self, fields):
        self._fields = [_Field(name) for name in fields]

    def get_fields(self):
        return self._fields


class _Stored:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Manager:
    def __init__(self):
        self.rows = []

    def get_or_create(self, defaults=None, **identity):
        for row in self.rows:
            if all(getattr(row, key) == value
                   for key, value in identity.items()):
                return row, False
        row = _Stored(**identity, **(defaults or {}))
        self.rows.append(row)
        return row, True


def _fake_model(fields):
    return type('FakeModel', (), {
        '_meta': _Meta(fields),
        'objects': _Manager(),
    })


class TheoryImportDatabaseTests(TestCase):
    def test_real_shadow_models_are_idempotent(self):
        plan = _minimal_plan()

        first = theory_import.apply_import_plan(plan)
        second = theory_import.apply_import_plan(plan)

        self.assertEqual(first['cohorts_created'], 7)
        self.assertEqual(first['memberships_created'], 7)
        self.assertEqual(second['cohorts_reused'], 7)
        self.assertEqual(second['memberships_reused'], 7)
        self.assertEqual(SchedulingCohort.objects.count(), 7)
        self.assertEqual(CohortMembership.objects.count(), 7)

    def test_late_membership_conflict_rolls_back_whole_import(self):
        plan = _minimal_plan()
        cohort_spec = plan.cohorts[-1]
        member_spec = cohort_spec.memberships[-1]
        cohort = SchedulingCohort.objects.create(
            slug=cohort_spec.slug,
            label=cohort_spec.label,
            root_fen=cohort_spec.root_fen,
            root_key=cohort_spec.root_key,
            priority_level=cohort_spec.priority_level,
            evidence_level=cohort_spec.evidence_level,
            manifest_sha256=cohort_spec.manifest_sha256,
            policy_version=cohort_spec.policy_version,
            decay_policy=cohort_spec.decay_policy,
            metadata=cohort_spec.metadata,
            active=cohort_spec.active,
        )
        CohortMembership.objects.create(
            cohort=cohort,
            position_key=member_spec.position_key,
            source_id=member_spec.source_id,
            path_sha256=member_spec.path_sha256,
            fen=logic.start_fen(),
        )

        with self.assertRaisesRegex(
                theory_import.TheoryImportError, 'conflicts'):
            theory_import.apply_import_plan(plan)

        self.assertEqual(
            list(SchedulingCohort.objects.values_list('slug', flat=True)),
            [cohort_spec.slug])
        self.assertEqual(CohortMembership.objects.count(), 1)

    def test_unexpected_active_policy_cohort_aborts_without_import(self):
        plan = _minimal_plan()
        SchedulingCohort.objects.create(
            slug='unexpected-active',
            label='unexpected',
            root_fen=logic.start_fen(),
            root_key=logic.key_of(logic.start_fen()),
            priority_level='P3',
            evidence_level='E0',
            manifest_sha256=plan.bundle_manifest_sha256,
            policy_version=theory_import.POLICY_VERSION,
            decay_policy={},
            metadata={},
            active=True,
        )

        with self.assertRaisesRegex(
                theory_import.TheoryImportError,
                'unexpected active cohorts'):
            theory_import.apply_import_plan(plan)

        self.assertEqual(SchedulingCohort.objects.count(), 1)
        self.assertEqual(CohortMembership.objects.count(), 0)

    def test_unexpected_membership_aborts_without_mutating_sealed_set(self):
        plan = _minimal_plan()
        theory_import.apply_import_plan(plan)
        cohort = SchedulingCohort.objects.get(
            policy_version=theory_import.POLICY_VERSION,
            slug=plan.cohorts[0].slug)
        CohortMembership.objects.create(
            cohort=cohort,
            position_key=logic.key_of(logic.start_fen()),
            fen=logic.start_fen(),
            source_id='unexpected-source',
            path_uci='',
            path_sha256=hashlib.sha256(b'').hexdigest(),
        )
        before = (
            SchedulingCohort.objects.count(),
            CohortMembership.objects.count(),
        )

        with self.assertRaisesRegex(
                theory_import.TheoryImportError,
                'unexpected membership'):
            theory_import.apply_import_plan(plan)

        self.assertEqual((
            SchedulingCohort.objects.count(),
            CohortMembership.objects.count(),
        ), before)


class TheoryImportTests(SimpleTestCase):
    def test_san_is_regenerated_through_legal_atomic_moves(self):
        positions = theory_import.resolve_san_line('1.Nf3 f6 2.Nc3')

        self.assertEqual([item.path_uci for item in positions], [
            'g1f3',
            'g1f3 f7f6',
            'g1f3 f7f6 b1c3',
        ])
        self.assertEqual(positions[-1].fen, logic.canonical_fen(
            'rnbqkbnr/ppppp1pp/5p2/8/8/2N2N2/'
            'PPPPPPPP/R1BQKB1R b KQkq - 1 2'))
        self.assertEqual(positions[-1].role, 'SEED_ROOT')

    def test_illegal_or_narrative_san_is_rejected(self):
        with self.assertRaises(theory_import.TheoryImportError):
            theory_import.resolve_san_line('1.Nf3 f6 2.Nc3 Qz9')
        with self.assertRaises(theory_import.TheoryImportError):
            theory_import.resolve_san_line(
                '1.Nf3 f6 2.Nc3 Nh6, 6.h4 branch through 10...Bxc3')

    def test_pinned_json_rejects_tampering_before_use(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'manifest.json'
            path.write_text('{"schema": 1}', encoding='utf-8')
            wrong = hashlib.sha256(b'different').hexdigest()
            with self.assertRaisesRegex(
                    theory_import.TheoryImportError, 'hash mismatch'):
                theory_import.load_pinned_json(path, wrong)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'manifest.json'
            payload = b'{"schema":1,"schema":2}'
            path.write_bytes(payload)
            with self.assertRaisesRegex(
                    theory_import.TheoryImportError, 'duplicate JSON key'):
                theory_import.load_pinned_json(
                    path, hashlib.sha256(payload).hexdigest())

    def test_exact_21_file_bundle_is_authenticated_and_extras_fail(self):
        cohorts, _ = _minimal_manifests()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for study in cohorts['studies']:
                (root / f"{study['study_id']}.pgn").write_bytes(
                    study['study_id'].encode('ascii'))

            actual = theory_import.verify_study_bundle(root, cohorts)
            expected = hashlib.sha256()
            for study in sorted(
                    cohorts['studies'], key=lambda item: item['study_id']):
                expected.update(study['study_id'].encode('ascii'))
                expected.update(b'\0')
                expected.update(study['pgn_sha256'].encode('ascii'))
                expected.update(b'\n')
            self.assertEqual(actual, expected.hexdigest())

            (root / 'extra.pgn').write_bytes(b'extra')
            with self.assertRaisesRegex(
                    theory_import.TheoryImportError, 'exactly the 21'):
                theory_import.verify_study_bundle(root, cohorts)

    def test_bundle_rejects_missing_or_hash_drift(self):
        cohorts, _ = _minimal_manifests()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for study in cohorts['studies']:
                (root / f"{study['study_id']}.pgn").write_bytes(
                    study['study_id'].encode('ascii'))
            missing = root / f"{cohorts['studies'][0]['study_id']}.pgn"
            missing.unlink()
            with self.assertRaisesRegex(
                    theory_import.TheoryImportError, 'missing='):
                theory_import.verify_study_bundle(root, cohorts)
            missing.write_bytes(b'tampered')
            with self.assertRaisesRegex(
                    theory_import.TheoryImportError, 'hash mismatch'):
                theory_import.verify_study_bundle(root, cohorts)

    def test_scheduler_is_only_executable_path_source(self):
        values = list(_minimal_plan_inputs())
        values[1]['ranked_candidates'][0]['line'] = (
            'narrative, intentionally not executable')
        values[4] = _hash_data(values[1])
        values[2]['source_manifests']['priority_evidence_sha256'] = values[4]
        values[5] = _hash_data(values[2])

        plan = theory_import.build_import_plan(*values)

        self.assertEqual(len(plan.cohorts), 7)
        self.assertEqual(plan.cohorts[0].root_key, logic.key_of(
            plan.cohorts[0].root_fen))

    def test_scheduler_uci_fen_and_key_are_fail_closed(self):
        values = list(_minimal_plan_inputs())
        values[2]['entries'][0]['uci'][-1] = 'b1d2'

        with self.assertRaisesRegex(
                theory_import.TheoryImportError, 'UCI path'):
            theory_import.build_import_plan(*values)

    def test_scheduler_reconciles_ranks_priorities_and_2n_notation(self):
        values = list(_minimal_plan_inputs())
        values[1]['ranked_candidates'][0]['rank'] = 2
        with self.assertRaisesRegex(
                theory_import.TheoryImportError, 'ranks'):
            theory_import.build_import_plan(*values)

        values = list(_minimal_plan_inputs())
        values[2]['entries'][0]['priority_level'] = 'P3'
        with self.assertRaisesRegex(
                theory_import.TheoryImportError, 'priority'):
            theory_import.build_import_plan(*values)

        values = list(_minimal_plan_inputs())
        values[1]['notation']['2N']['canonical_line'] = '1.Nf3 f6 2.Nd4'
        with self.assertRaisesRegex(
                theory_import.TheoryImportError, '2N'):
            theory_import.build_import_plan(*values)

    def test_malformed_priority_notation_never_leaks_key_error(self):
        values = list(_minimal_plan_inputs())
        del values[1]['notation']

        with self.assertRaises(theory_import.TheoryImportError) as caught:
            theory_import.build_import_plan(*values)

        self.assertNotIsInstance(caught.exception, KeyError)
        self.assertIn('notation', str(caught.exception))

    def test_plan_is_shadow_only_and_deduplicated_by_provenance(self):
        plan = _minimal_plan()

        self.assertEqual(len(plan.cohorts), 7)
        self.assertEqual(plan.membership_count, 7)
        for cohort in plan.cohorts:
            self.assertEqual(cohort.root_key, logic.key_of(cohort.root_fen))
            self.assertEqual(cohort.decay_policy, {
                'kind': 'linear-lifetime-compute',
                'attempts': {'decay_starts': 3, 'zero_at': 5},
                'core_hours': {'decay_starts': 25, 'zero_at': 50},
                'aggregation': 'max_not_sum',
            })
            identities = {
                (item.position_key, item.source_id, item.path_sha256)
                for item in cohort.memberships
            }
            self.assertEqual(len(identities), len(cohort.memberships))
            for item in cohort.memberships:
                self.assertEqual(
                    item.path_sha256,
                    hashlib.sha256(item.path_uci.encode('utf-8')).hexdigest())
        receipt = plan.receipt('dry-run')
        self.assertEqual(receipt['safety']['position_writes'], 0)
        self.assertEqual(receipt['safety']['closure_effect'], 'none')

    def test_apply_is_idempotent_against_exact_shadow_interface(self):
        plan = _minimal_plan()
        cohort_model = _fake_model(theory_import._REQUIRED_COHORT_FIELDS)
        membership_model = _fake_model(
            theory_import._REQUIRED_MEMBERSHIP_FIELDS)

        first = theory_import.apply_import_plan(
            plan, cohort_model, membership_model,
            atomic_context=nullcontext)
        second = theory_import.apply_import_plan(
            plan, cohort_model, membership_model,
            atomic_context=nullcontext)

        self.assertEqual(first, {
            'cohorts_created': 7,
            'cohorts_reused': 0,
            'memberships_created': 7,
            'memberships_reused': 0,
        })
        self.assertEqual(second, {
            'cohorts_created': 0,
            'cohorts_reused': 7,
            'memberships_created': 0,
            'memberships_reused': 7,
        })

    def test_apply_fails_closed_on_existing_conflict(self):
        plan = _minimal_plan()
        cohort_model = _fake_model(theory_import._REQUIRED_COHORT_FIELDS)
        membership_model = _fake_model(
            theory_import._REQUIRED_MEMBERSHIP_FIELDS)
        theory_import.apply_import_plan(
            plan, cohort_model, membership_model,
            atomic_context=nullcontext)
        cohort_model.objects.rows[0].root_fen = logic.start_fen()

        with self.assertRaisesRegex(
                theory_import.TheoryImportError, 'conflicts'):
            theory_import.apply_import_plan(
                plan, cohort_model, membership_model,
                atomic_context=nullcontext)

    def test_dry_run_command_never_resolves_or_writes_models(self):
        plan = _minimal_plan()
        with mock.patch(
                'atomicdb.theory_import.load_import_plan',
                return_value=plan), mock.patch(
                'atomicdb.theory_import.apply_import_plan') as apply:
            output = tempfile.SpooledTemporaryFile(mode='w+')
            call_command('import_atomic_studies', stdout=output)
            output.seek(0)
            receipt = json.loads(output.read())

        apply.assert_not_called()
        self.assertEqual(receipt['mode'], 'dry-run')
        self.assertEqual(receipt['counts']['cohorts'], 7)

    def test_apply_requires_and_seals_preflight_receipt_before_write(self):
        plan = _minimal_plan()
        with tempfile.TemporaryDirectory() as temp:
            receipt_path = Path(temp) / 'apply.json'
            preflight_path = theory_import.preflight_receipt_path(
                receipt_path)
            events = []

            def apply(_plan):
                self.assertTrue(preflight_path.exists())
                self.assertFalse(receipt_path.exists())
                events.append('apply')
                return {
                    'cohorts_created': 7,
                    'cohorts_reused': 0,
                    'memberships_created': 7,
                    'memberships_reused': 0,
                }

            with mock.patch(
                    'atomicdb.theory_import.load_import_plan',
                    return_value=plan), mock.patch(
                    'atomicdb.theory_import.apply_import_plan',
                    side_effect=apply):
                output = tempfile.SpooledTemporaryFile(mode='w+')
                call_command(
                    'import_atomic_studies', '--apply', '--receipt',
                    str(receipt_path), stdout=output)
                output.seek(0)
                applied = json.loads(output.read())

            self.assertEqual(events, ['apply'])
            self.assertEqual(
                json.loads(preflight_path.read_text(encoding='utf-8'))['mode'],
                'preflight')
            self.assertEqual(
                json.loads(receipt_path.read_text(encoding='utf-8'))['mode'],
                'applied')
            self.assertEqual(applied['mode'], 'applied')
            self.assertEqual(
                applied['preflight_receipt']['sha256'],
                hashlib.sha256(preflight_path.read_bytes()).hexdigest())
            self.assertEqual(
                applied['final_receipt']['sha256'],
                hashlib.sha256(receipt_path.read_bytes()).hexdigest())

    def test_apply_without_preflight_receipt_is_rejected(self):
        with mock.patch(
                'atomicdb.theory_import.load_import_plan',
                return_value=_minimal_plan()), self.assertRaises(CommandError):
            call_command('import_atomic_studies', '--apply')

    def test_receipt_is_no_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'receipt.json'
            theory_import.write_receipt(path, {'ok': True})
            with self.assertRaisesRegex(
                    theory_import.TheoryImportError, 'already exists'):
                theory_import.write_receipt(path, {'ok': False})

    @skipUnless(
        theory_import.DEFAULT_COHORT_MANIFEST.exists()
        and theory_import.DEFAULT_PRIORITY_MANIFEST.exists()
        and theory_import.DEFAULT_SCHEDULER_MANIFEST.exists()
        and theory_import.DEFAULT_STUDY_ROOT.exists(),
        'real research manifests are outside this checkout')
    def test_real_manifests_build_seven_root_only_cohorts(self):
        plan = theory_import.load_import_plan()

        self.assertEqual(len(plan.cohorts), 7)
        self.assertEqual(plan.membership_count, 32)
        by_slug = {cohort.slug: cohort for cohort in plan.cohorts}
        self.assertEqual(
            by_slug['2n-full-trunk'].root_fen,
            logic.canonical_fen(
                'rnbqkbnr/ppppp1pp/5p2/8/8/2N2N2/'
                'PPPPPPPP/R1BQKB1R b KQkq - 1 2'))
        self.assertEqual(
            len(by_slug['2n-nh6-6h4-bxc3'].memberships), 2)
        self.assertEqual(
            by_slug['2n-nh6-6h4-bxc3'].metadata['start_fen'],
            'rnb1k2r/pp1p2pp/2p1p3/5q2/1b1P1P1P/'
            '2NB4/PPP5/R1BQK2R b KQkq - 0 1')
        self.assertTrue(all(
            membership.metadata['root_only']
            for cohort in plan.cohorts
            for membership in cohort.memberships))
        self.assertEqual(
            by_slug['2n-full-trunk'].evidence_level, 'E1')
