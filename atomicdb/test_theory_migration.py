"""Migration preservation checks for the theory shadow schema."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class TheoryShadowMigrationTests(TransactionTestCase):
    migrate_from = [('atomicdb', '0012_analysistask_lease_session')]
    migrate_to = [('atomicdb', '0013_theory_shadow_scheduler')]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old = executor.loader.project_state(self.migrate_from).apps

        Position = old.get_model('atomicdb', 'Position')
        Edge = old.get_model('atomicdb', 'Edge')
        AnalysisTask = old.get_model('atomicdb', 'AnalysisTask')
        self.parent_key = 'a' * 64
        self.child_key = 'b' * 64
        parent = Position.objects.create(
            key=self.parent_key,
            fen='8/8/8/8/8/8/4K3/7k w - - 0 1',
            eval_cp=123,
            status='UNKNOWN',
            expanded=True,
            visits=4,
            priority=7.5,
        )
        child = Position.objects.create(
            key=self.child_key,
            fen='8/8/8/8/8/8/8/4K2k b - - 0 1',
            status='DRAW',
            closure='TERMINAL',
            expanded=False,
            priority=-2.0,
        )
        Edge.objects.create(parent=parent, move_uci='e2e1', child=child)
        task = AnalysisTask.objects.create(
            position=parent,
            budget_nodes=128_000_000,
            nodes_searched=64_000_000,
            elapsed_seconds=12.5,
            generation=4,
            source='USER',
            state='PENDING',
            lease_session='preserve-me',
            attempts=2,
        )
        self.task_id = task.id

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        # Always return the shared test connection to the leaf migration for
        # whichever test class follows this one.
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        executor.migrate(leaves)
        super().tearDown()

    def test_existing_truth_topology_and_task_are_preserved(self):
        Position = self.apps.get_model('atomicdb', 'Position')
        Edge = self.apps.get_model('atomicdb', 'Edge')
        AnalysisTask = self.apps.get_model('atomicdb', 'AnalysisTask')
        SchedulingCohort = self.apps.get_model(
            'atomicdb', 'SchedulingCohort')
        CohortMembership = self.apps.get_model(
            'atomicdb', 'CohortMembership')

        parent = Position.objects.get(pk=self.parent_key)
        child = Position.objects.get(pk=self.child_key)
        task = AnalysisTask.objects.get(pk=self.task_id)

        self.assertEqual(parent.eval_cp, 123)
        self.assertEqual(parent.status, 'UNKNOWN')
        self.assertTrue(parent.expanded)
        self.assertEqual(parent.visits, 4)
        self.assertEqual(parent.priority, 7.5)
        self.assertEqual(parent.theory_boost, 0.0)
        self.assertIsNone(parent.shadow_priority)
        self.assertEqual(child.status, 'DRAW')
        self.assertEqual(child.closure, 'TERMINAL')
        self.assertEqual(
            list(Edge.objects.values_list(
                'parent_id', 'move_uci', 'child_id')),
            [(self.parent_key, 'e2e1', self.child_key)],
        )
        self.assertEqual(task.source, 'USER')
        self.assertEqual(task.nodes_searched, 64_000_000)
        self.assertEqual(task.elapsed_seconds, 12.5)
        self.assertEqual(task.lease_session, 'preserve-me')
        self.assertEqual(task.threads_at_lease, 0)
        self.assertEqual(SchedulingCohort.objects.count(), 0)
        self.assertEqual(CohortMembership.objects.count(), 0)
