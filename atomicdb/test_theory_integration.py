"""Integration coverage for the untrusted theory scheduling boundary.

These tests intentionally use the real Django models and selector/lease paths.
The only fields a theory hint may influence are scheduling fields; proof truth
and topology remain exclusively owned by the normal AtomicDB ingest pipeline.
"""

import hashlib
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock, skipUnless

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from atomicdb import ingest, logic, theory_import
from atomicdb.models import (
    AnalysisTask,
    CohortMembership,
    DBEvent,
    Edge,
    Position,
    SchedulingCohort,
    WorkerPing,
)


POLICY_VERSION = "atomic-theory-shadow-v1"
BUNDLE_SHA256 = (
    "a6261fbf26b2eb4a80fac2b4ae545e16297c17db074c698d563bff7ba4790464"
)
THEORY_SETTINGS = {
    "ATOMICDB_THEORY_POLICY_VERSION": POLICY_VERSION,
    "ATOMICDB_THEORY_BUNDLE_SHA256": BUNDLE_SHA256,
    "ATOMICDB_THEORY_MAX_BOOST": 12.0,
    "ATOMICDB_THEORY_ACTIVE_ACK": POLICY_VERSION,
}


class TheorySchedulerIntegrationTests(TestCase):
    password = "integration-only-password"

    def setUp(self):
        self._reset_refresh_cache()

    def tearDown(self):
        self._reset_refresh_cache()

    @staticmethod
    def _reset_refresh_cache():
        ingest._priority_refresh_cache["at"] = 0.0
        ingest._priority_refresh_cache["signature"] = None

    @staticmethod
    def _position(move_uci, *, eval_cp=0, expanded=True, visits=0):
        fen = logic.apply_move(logic.start_fen(), move_uci)
        position = ingest.get_or_create_position(fen)
        position.eval_cp = eval_cp
        position.expanded = expanded
        position.visits = visits
        position.save(update_fields=["eval_cp", "expanded", "visits"])
        return position

    @staticmethod
    def _cohort(position, slug, priority_level, *, with_membership=True):
        cohort = SchedulingCohort.objects.create(
            slug=slug,
            label=slug,
            root_fen=position.fen,
            root_key=position.key,
            priority_level=priority_level,
            evidence_level=SchedulingCohort.EvidenceLevel.E2,
            manifest_sha256=BUNDLE_SHA256,
            policy_version=POLICY_VERSION,
            decay_policy={
                "kind": "linear-stale-compute",
                "aggregation": "max_not_sum",
            },
            metadata={"test_fixture": True},
            active=True,
        )
        if with_membership:
            TheorySchedulerIntegrationTests._membership(
                cohort, position, source_id=f"source-{slug}")
        return cohort

    @staticmethod
    def _membership(cohort, position, *, source_id):
        path_uci = f"fixture {cohort.slug} {position.key}"
        return CohortMembership.objects.create(
            cohort=cohort,
            position_key=position.key,
            fen=position.fen,
            ply=1,
            role="SEED_ROOT",
            source_id=source_id,
            source_url="https://lichess.org/study/HVXNmBDj",
            artifact_sha256="",
            path_uci=path_uci,
            path_sha256=hashlib.sha256(
                path_uci.encode("utf-8")).hexdigest(),
            provenance_kind="LICHESS_STUDY",
            metadata={"test_fixture": True},
        )

    def _mode(self, mode, *, bundle_sha256=BUNDLE_SHA256):
        values = {
            **THEORY_SETTINGS,
            "ATOMICDB_THEORY_SCHEDULER_MODE": mode,
            "ATOMICDB_THEORY_BUNDLE_SHA256": bundle_sha256,
        }
        return override_settings(**values)

    @staticmethod
    def _validated_import_plan():
        # Reuse the importer's adversarially validated fixture builder rather
        # than bypassing validation by constructing ImportPlan directly.
        from atomicdb.test_theory_import import _minimal_plan_inputs
        return theory_import.build_import_plan(*_minimal_plan_inputs())

    def _lease_order(self, count, prefix):
        order = []
        for index in range(count):
            response = self.client.post("/atomicdb/api/lease", {
                "username": "integration-worker",
                "password": self.password,
                "machine": f"{prefix}-{index}",
                "threads": "8",
                "tb": "1",
                "worker_build": "2026072203",
                "lease_session": f"{prefix}-session-{index}",
            })
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(len(payload["tasks"]), 1)
            task = AnalysisTask.objects.get(pk=payload["tasks"][0]["id"])
            order.append(task.position_id)
        return order

    @staticmethod
    def _reset_leases():
        AnalysisTask.objects.update(
            state=AnalysisTask.TState.PENDING,
            machine="",
            leased_at=None,
            lease_heartbeat_at=None,
            lease_token="",
            lease_session="",
            threads_at_lease=0,
            attempts=0,
        )
        WorkerPing.objects.all().delete()

    def test_off_and_shadow_keep_priority_and_real_lease_order_identical(self):
        User.objects.create_user(
            "integration-worker", password=self.password)
        positions = [
            self._position("e2e4", eval_cp=900),
            self._position("d2d4", eval_cp=500),
            self._position("g1f3", eval_cp=100),
        ]
        self._cohort(positions[-1], "shadow-low-line", "P1")
        for position in positions:
            AnalysisTask.objects.create(
                position=position,
                generation=0,
                budget_nodes=128_000_000,
                source=AnalysisTask.Source.AUTO,
            )

        with self._mode("OFF"):
            self.assertTrue(ingest.refresh_priorities())
            off_priorities = dict(Position.objects.values_list(
                "key", "priority"))
            off_order = self._lease_order(len(positions), "off")

        self._reset_leases()
        with self._mode("SHADOW"):
            self.assertTrue(ingest.refresh_priorities())
            shadow_priorities = dict(Position.objects.values_list(
                "key", "priority"))
            shadow_order = self._lease_order(len(positions), "shadow")

        self.assertEqual(shadow_priorities, off_priorities)
        self.assertEqual(shadow_order, off_order)
        theory_position = Position.objects.get(pk=positions[-1].pk)
        self.assertEqual(theory_position.theory_boost, 8.0)
        self.assertEqual(
            theory_position.shadow_priority,
            theory_position.priority + theory_position.theory_boost,
        )
        self.assertFalse(AnalysisTask.objects.filter(
            source=AnalysisTask.Source.THEORY).exists())

    def test_shadow_creates_no_work_or_topology_and_changes_no_truth(self):
        candidate = self._position("c2c4", eval_cp=321, expanded=True)
        candidate.best_move = "e7e5"
        candidate.last_analysis = {"multipv": [{"move": "e7e5"}]}
        candidate.depth_invested = 17
        candidate.nodes_invested = 123456
        candidate.time_invested = 3.5
        candidate.save(update_fields=[
            "best_move", "last_analysis", "depth_invested",
            "nodes_invested", "time_invested",
        ])
        solved = self._position("b1c3", eval_cp=-90, expanded=True)
        solved.status = "DRAW"
        solved.closure = "MINIMAX"
        solved.proof = "ANDOR"
        solved.won_line = "fixture-proof"
        solved.mate_in = 0
        solved.save(update_fields=[
            "status", "closure", "proof", "won_line", "mate_in",
        ])
        self._cohort(candidate, "shadow-observation", "P0")

        truth_fields = (
            "fen", "eval_cp", "status", "closure", "proof", "best_move",
            "won_line", "mate_in", "last_analysis", "expanded",
            "depth_invested", "nodes_invested", "time_invested", "visits",
            "campaign_id",
        )
        truth_before = {
            position.key: tuple(getattr(position, field)
                                for field in truth_fields)
            for position in Position.objects.order_by("key")
        }
        edge_count = Edge.objects.count()
        task_count = AnalysisTask.objects.count()
        event_count = DBEvent.objects.count()

        with self._mode("SHADOW"):
            self.assertTrue(ingest.refresh_priorities())

        truth_after = {
            position.key: tuple(getattr(position, field)
                                for field in truth_fields)
            for position in Position.objects.order_by("key")
        }
        self.assertEqual(truth_after, truth_before)
        self.assertEqual(Edge.objects.count(), edge_count)
        self.assertEqual(AnalysisTask.objects.count(), task_count)
        self.assertEqual(DBEvent.objects.count(), event_count)

        candidate.refresh_from_db()
        self.assertEqual(candidate.theory_boost, 12.0)
        self.assertEqual(
            candidate.shadow_priority,
            candidate.priority + candidate.theory_boost,
        )

    def test_active_uses_max_boost_and_only_materializes_autonomous_work(self):
        auto = self._position("e2e3", eval_cp=100)
        user = self._position("d2d3", eval_cp=200)
        leased = self._position("g2g3", eval_cp=300)
        unmaterialized = self._position("b2b3", eval_cp=1_500)

        self._cohort(auto, "active-primary", "P1")
        self._cohort(auto, "active-secondary", "P2")
        self._cohort(user, "active-user-boundary", "P0")
        self._cohort(leased, "active-lease-boundary", "P0")
        self._cohort(unmaterialized, "active-new-work", "P2")

        auto_task = AnalysisTask.objects.create(
            position=auto, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.AUTO,
            state=AnalysisTask.TState.PENDING)
        user_task = AnalysisTask.objects.create(
            position=user, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER,
            state=AnalysisTask.TState.PENDING)
        leased_task = AnalysisTask.objects.create(
            position=leased, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.AUTO,
            state=AnalysisTask.TState.LEASED,
            machine="already-running")

        with self._mode("ACTIVE"):
            self.assertTrue(ingest.refresh_priorities())
            ingest.next_tasks(20)

        auto.refresh_from_db()
        auto_task.refresh_from_db()
        user_task.refresh_from_db()
        leased_task.refresh_from_db()
        self.assertEqual(auto.theory_boost, 8.0)
        self.assertEqual(
            auto_task.source, AnalysisTask.Source.THEORY)
        self.assertEqual(
            user_task.source, AnalysisTask.Source.USER)
        self.assertEqual(
            leased_task.source, AnalysisTask.Source.AUTO)
        self.assertEqual(
            leased_task.state, AnalysisTask.TState.LEASED)

        created = AnalysisTask.objects.get(position=unmaterialized)
        self.assertEqual(created.source, AnalysisTask.Source.THEORY)
        event = DBEvent.objects.get(
            kind="THEORY_TASK_CREATED",
            payload__task_id=created.id,
        )
        self.assertEqual(event.payload["theory_boost"], 4.0)
        self.assertEqual(
            event.payload["policy_version"], POLICY_VERSION)

    def test_zero_decay_demotes_pending_theory_work(self):
        position = self._position(
            "a2a3", eval_cp=400, expanded=True, visits=5)
        self._cohort(position, "stale-theory", "P0")
        for generation in range(5):
            AnalysisTask.objects.create(
                position=position,
                generation=generation,
                budget_nodes=128_000_000,
                source=AnalysisTask.Source.AUTO,
                state=AnalysisTask.TState.COMPLETED,
                elapsed_seconds=60.0,
                threads_at_lease=8,
            )
        pending = AnalysisTask.objects.create(
            position=position,
            generation=5,
            budget_nodes=128_000_000,
            source=AnalysisTask.Source.THEORY,
            state=AnalysisTask.TState.PENDING,
        )

        with self._mode("ACTIVE"):
            self.assertTrue(ingest.refresh_priorities())

        position.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(position.theory_boost, 0.0)
        self.assertEqual(pending.source, AnalysisTask.Source.AUTO)

    def test_tombstones_are_not_revived_by_active_theory(self):
        tombstone = self._position("h2h3", eval_cp=9_999)
        tombstone.priority = ingest.DEAD
        tombstone.save(update_fields=["priority"])
        self._cohort(tombstone, "dead-theory-line", "P0")

        with self._mode("ACTIVE"):
            self.assertTrue(ingest.refresh_priorities())

        tombstone.refresh_from_db()
        self.assertEqual(tombstone.priority, ingest.DEAD)
        self.assertEqual(tombstone.theory_boost, 0.0)
        self.assertFalse(
            AnalysisTask.objects.filter(position=tombstone).exists())

    def test_refresh_cache_invalidates_on_membership_and_cohort_change(self):
        position = self._position("h2h4", eval_cp=100)
        cohort = self._cohort(
            position, "cache-revision", "P2", with_membership=False)

        with self._mode("SHADOW"):
            self.assertTrue(ingest.refresh_priorities())
            position.refresh_from_db()
            base_priority = position.priority
            self.assertIsNone(position.shadow_priority)

            self._membership(
                cohort, position, source_id="cache-source")
            self.assertTrue(ingest.refresh_priorities())
            position.refresh_from_db()
            self.assertEqual(position.theory_boost, 4.0)
            self.assertEqual(
                position.shadow_priority, base_priority + 4.0)

            cohort.priority_level = "P1"
            cohort.save(update_fields=["priority_level", "updated"])
            self.assertTrue(ingest.refresh_priorities())
            position.refresh_from_db()
            self.assertEqual(position.theory_boost, 8.0)
            self.assertEqual(
                position.shadow_priority, base_priority + 8.0)

    def test_shadow_report_does_not_reconcile_or_materialize_tasks(self):
        position = self._position("f2f3", eval_cp=250)
        self._cohort(position, "report-read-only", "P1")
        pending = AnalysisTask.objects.create(
            position=position,
            generation=0,
            budget_nodes=128_000_000,
            source=AnalysisTask.Source.THEORY,
            state=AnalysisTask.TState.PENDING,
        )
        task_count = AnalysisTask.objects.count()
        edge_count = Edge.objects.count()
        event_count = DBEvent.objects.count()
        output = StringIO()

        with self._mode("SHADOW"):
            call_command(
                "report_theory_shadow",
                limit=10,
                stdout=output,
            )

        pending.refresh_from_db()
        receipt = json.loads(output.getvalue())
        self.assertEqual(
            pending.source, AnalysisTask.Source.THEORY)
        self.assertEqual(AnalysisTask.objects.count(), task_count)
        self.assertEqual(Edge.objects.count(), edge_count)
        self.assertEqual(DBEvent.objects.count(), event_count)
        self.assertEqual(receipt["mode"], "SHADOW")
        self.assertEqual(receipt["safety"]["task_writes"], 0)
        self.assertEqual(receipt["safety"]["edge_writes"], 0)
        self.assertEqual(receipt["safety"]["truth_writes"], 0)

    def test_shadow_report_ranks_the_complete_live_queue(self):
        live_leader = self._position(
            "f2f4", eval_cp=1_200, expanded=True)
        ordinary = self._position(
            "g2g4", eval_cp=500, expanded=True)
        theory = self._position(
            "b2b4", eval_cp=100, expanded=True)
        tombstone = self._position(
            "a2a4", eval_cp=9_999, expanded=True)
        tombstone.priority = ingest.DEAD
        tombstone.save(update_fields=["priority"])
        self._cohort(theory, "report-theory-leader", "P0")
        self._cohort(tombstone, "report-dead-line", "P0")
        output = StringIO()

        with self._mode("SHADOW"):
            call_command(
                "report_theory_shadow",
                limit=10,
                stdout=output,
            )

        receipt = json.loads(output.getvalue())
        expected = {live_leader.key, ordinary.key, theory.key}
        live_keys = [row["key"] for row in receipt["live_top"]]
        shadow_keys = [row["key"] for row in receipt["shadow_top"]]
        self.assertEqual(receipt["queue_positions"], 3)
        self.assertEqual(receipt["matched_positions"], 1)
        self.assertEqual(set(live_keys), expected)
        self.assertEqual(set(shadow_keys), expected)
        self.assertNotIn(tombstone.key, live_keys)
        self.assertNotIn(tombstone.key, shadow_keys)
        self.assertEqual(live_keys[0], live_leader.key)
        self.assertEqual(shadow_keys[0], theory.key)

    def test_real_model_import_is_idempotent_without_duplicates(self):
        plan = self._validated_import_plan()

        with override_settings(
                ATOMICDB_THEORY_BUNDLE_SHA256=plan.bundle_manifest_sha256):
            first = theory_import.apply_import_plan(plan)
            second = theory_import.apply_import_plan(plan)

        self.assertEqual(first, {
            "cohorts_created": 7,
            "cohorts_reused": 0,
            "memberships_created": 7,
            "memberships_reused": 0,
        })
        self.assertEqual(second, {
            "cohorts_created": 0,
            "cohorts_reused": 7,
            "memberships_created": 0,
            "memberships_reused": 7,
        })
        self.assertEqual(SchedulingCohort.objects.count(), 7)
        self.assertEqual(CohortMembership.objects.count(), 7)
        identities = set(CohortMembership.objects.values_list(
            "cohort_id", "position_key", "source_id", "path_sha256"))
        self.assertEqual(len(identities), 7)

    def test_real_model_import_conflict_rolls_back_the_whole_plan(self):
        plan = self._validated_import_plan()
        conflict = plan.cohorts[1]
        SchedulingCohort.objects.create(
            slug=conflict.slug,
            label=f"{conflict.label}-conflict",
            root_fen=conflict.root_fen,
            root_key=conflict.root_key,
            priority_level=conflict.priority_level,
            evidence_level=conflict.evidence_level,
            manifest_sha256=conflict.manifest_sha256,
            policy_version=conflict.policy_version,
            decay_policy=conflict.decay_policy,
            metadata=conflict.metadata,
            active=conflict.active,
        )
        cohorts_before = list(SchedulingCohort.objects.order_by(
            "slug").values())
        memberships_before = list(CohortMembership.objects.order_by(
            "id").values())

        with self.assertRaisesRegex(
                theory_import.TheoryImportError, "conflicts"):
            with override_settings(
                    ATOMICDB_THEORY_BUNDLE_SHA256=(
                        plan.bundle_manifest_sha256)):
                theory_import.apply_import_plan(plan)

        self.assertEqual(
            list(SchedulingCohort.objects.order_by("slug").values()),
            cohorts_before,
        )
        self.assertEqual(
            list(CohortMembership.objects.order_by("id").values()),
            memberships_before,
        )
        self.assertFalse(SchedulingCohort.objects.filter(
            slug=plan.cohorts[0].slug).exists())

    def test_real_model_import_cannot_touch_truth_topology_or_work(self):
        root = ingest.get_or_create_position(logic.start_fen())
        child = ingest.get_or_create_position(
            logic.apply_move(root.fen, "e2e4"))
        root.eval_cp = 75
        root.best_move = "e2e4"
        root.last_analysis = {"multipv": [{"move": "e2e4", "cp": 75}]}
        root.depth_invested = 21
        root.nodes_invested = 987654
        root.time_invested = 4.25
        root.save(update_fields=[
            "eval_cp", "best_move", "last_analysis", "depth_invested",
            "nodes_invested", "time_invested",
        ])
        Edge.objects.create(
            parent=root, move_uci="e2e4", child=child)
        AnalysisTask.objects.create(
            position=child,
            generation=0,
            budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER,
        )
        positions_before = list(Position.objects.order_by("key").values())
        edges_before = list(Edge.objects.order_by("id").values_list(
            "id", "parent_id", "move_uci", "child_id"))
        tasks_before = list(AnalysisTask.objects.order_by("id").values())

        plan = self._validated_import_plan()
        with override_settings(
                ATOMICDB_THEORY_BUNDLE_SHA256=plan.bundle_manifest_sha256):
            theory_import.apply_import_plan(plan)

        self.assertEqual(
            list(Position.objects.order_by("key").values()),
            positions_before,
        )
        self.assertEqual(
            list(Edge.objects.order_by("id").values_list(
                "id", "parent_id", "move_uci", "child_id")),
            edges_before,
        )
        self.assertEqual(
            list(AnalysisTask.objects.order_by("id").values()),
            tasks_before,
        )

    def test_validated_import_then_shadow_refresh_observes_aligned_policy(self):
        plan = self._validated_import_plan()
        with override_settings(
                ATOMICDB_THEORY_BUNDLE_SHA256=plan.bundle_manifest_sha256):
            result = theory_import.apply_import_plan(plan)
        self.assertEqual(result["cohorts_created"], 7)
        self.assertEqual(Position.objects.count(), 0)
        self.assertEqual(Edge.objects.count(), 0)
        self.assertEqual(AnalysisTask.objects.count(), 0)
        policy_versions = {
            cohort.policy_version for cohort in plan.cohorts}
        self.assertEqual(policy_versions, {POLICY_VERSION})

        independently_discovered = ingest.get_or_create_position(
            plan.cohorts[0].root_fen)
        with self._mode(
                "SHADOW", bundle_sha256=plan.bundle_manifest_sha256):
            self.assertTrue(ingest.refresh_priorities())

        independently_discovered.refresh_from_db()
        self.assertEqual(independently_discovered.theory_boost, 12.0)
        self.assertEqual(
            independently_discovered.shadow_priority,
            independently_discovered.priority + 12.0,
        )
        self.assertEqual(CohortMembership.objects.filter(
            position_key=independently_discovered.key,
            cohort__policy_version=POLICY_VERSION,
        ).count(), 7)
        self.assertEqual(Edge.objects.count(), 0)
        self.assertEqual(AnalysisTask.objects.count(), 0)

    @skipUnless(
        theory_import.DEFAULT_COHORT_MANIFEST.exists()
        and theory_import.DEFAULT_PRIORITY_MANIFEST.exists()
        and theory_import.DEFAULT_SCHEDULER_MANIFEST.exists()
        and theory_import.DEFAULT_STUDY_ROOT.exists(),
        "pinned research bundle is outside this checkout",
    )
    def test_real_pinned_bundle_load_is_read_only(self):
        database_before = {
            "positions": Position.objects.count(),
            "edges": Edge.objects.count(),
            "tasks": AnalysisTask.objects.count(),
            "cohorts": SchedulingCohort.objects.count(),
            "memberships": CohortMembership.objects.count(),
            "events": DBEvent.objects.count(),
        }

        plan = theory_import.load_import_plan()

        self.assertEqual(len(plan.cohorts), 7)
        self.assertEqual(plan.membership_count, 32)
        self.assertEqual(
            plan.scheduler_manifest_sha256,
            theory_import.DEFAULT_SCHEDULER_MANIFEST_SHA256,
        )
        self.assertEqual(plan.bundle_manifest_sha256, BUNDLE_SHA256)
        self.assertEqual(
            {cohort.policy_version for cohort in plan.cohorts},
            {POLICY_VERSION},
        )
        self.assertEqual({
            "positions": Position.objects.count(),
            "edges": Edge.objects.count(),
            "tasks": AnalysisTask.objects.count(),
            "cohorts": SchedulingCohort.objects.count(),
            "memberships": CohortMembership.objects.count(),
            "events": DBEvent.objects.count(),
        }, database_before)

    def test_apply_command_persists_final_receipt_without_overwrite(self):
        plan = self._validated_import_plan()
        with tempfile.TemporaryDirectory() as temp:
            requested = Path(temp) / "theory-import-receipt.json"
            output = StringIO()
            with override_settings(
                    ATOMICDB_THEORY_BUNDLE_SHA256=(
                        plan.bundle_manifest_sha256)):
                with mock.patch(
                        "atomicdb.theory_import.load_import_plan",
                        return_value=plan):
                    call_command(
                        "import_atomic_studies",
                        apply=True,
                        receipt=str(requested),
                        stdout=output,
                    )
                    with self.assertRaises(CommandError):
                        call_command(
                            "import_atomic_studies",
                            apply=True,
                            receipt=str(requested),
                            stdout=StringIO(),
                        )

            stdout_receipt = json.loads(output.getvalue())
            persisted_receipts = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(Path(temp).glob("*.json"))
            ]

        self.assertEqual(stdout_receipt["mode"], "applied")
        self.assertTrue(
            any(receipt.get("mode") == "applied"
                for receipt in persisted_receipts),
            "the final applied receipt must be persisted, not stdout-only",
        )

    def test_cohort_slug_is_unique_per_policy_not_globally(self):
        position = self._position("c2c3")
        v1 = self._cohort(
            position, "versioned-route", "P1", with_membership=False)
        common = {
            "slug": v1.slug,
            "label": v1.label,
            "root_fen": v1.root_fen,
            "root_key": v1.root_key,
            "priority_level": v1.priority_level,
            "evidence_level": v1.evidence_level,
            "manifest_sha256": v1.manifest_sha256,
            "decay_policy": v1.decay_policy,
            "metadata": v1.metadata,
            "active": True,
        }
        v2 = SchedulingCohort.objects.create(
            **common, policy_version="atomic-theory-shadow-v2")

        self.assertNotEqual(v1.pk, v2.pk)
        self.assertEqual(SchedulingCohort.objects.filter(
            slug="versioned-route").count(), 2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SchedulingCohort.objects.create(
                    **common, policy_version=POLICY_VERSION)

    def test_shadow_ignores_same_policy_cohort_from_another_bundle(self):
        position = self._position("g1h3", eval_cp=150)
        foreign = self._cohort(
            position, "foreign-bundle-route", "P0")
        foreign.manifest_sha256 = "f" * 64
        foreign.save(update_fields=["manifest_sha256", "updated"])

        with self._mode("SHADOW"):
            self.assertTrue(ingest.refresh_priorities())

        position.refresh_from_db()
        self.assertEqual(position.theory_boost, 0.0)
        self.assertIsNone(position.shadow_priority)
        self.assertFalse(
            AnalysisTask.objects.filter(position=position).exists())
        self.assertFalse(DBEvent.objects.exists())
