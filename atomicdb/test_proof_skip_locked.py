"""The proof refresh skips proof rows another transaction holds.

7-Nov-2026: two ingests climbing the same campaign spine in a different order
deadlocked on ``atomicdb_proofnode`` rows (52 in an hour, write queue 518).
The refresh now takes its rows with ``SKIP LOCKED``: a row being written by
someone else is left to them instead of waited for.  Only PostgreSQL has row
locks to skip, so the concurrency test runs there.
"""
import threading
from unittest import skipUnless

from django.conf import settings
from django.db import connections

from . import ingest, logic, proof
from .database import atomic, connection
from .models import Edge, ProofCampaign, ProofNode
from .testing import TestCase, TransactionTestCase

ALIAS = settings.ATOMICDB_DATABASE_ALIAS
POSTGRES = 'postgresql' in settings.DATABASES[ALIAS]['ENGINE']


class OutsideTransactionTests(TestCase):
    """Without row locks to skip the refresh writes exactly as before."""

    def test_numbers_are_still_written(self):
        campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).first().child
        proof.refresh_proof_numbers([child.key])
        self.assertTrue(ProofNode.objects.filter(
            campaign=campaign, position=root).exists())


@skipUnless(POSTGRES, 'row locks to skip exist only on PostgreSQL')
class SkipLockedTests(TransactionTestCase):

    def test_a_row_held_elsewhere_is_skipped_not_waited_for(self):
        campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).first().child
        proof.refresh_proof_numbers([child.key])
        node = ProofNode.objects.get(campaign=campaign, position=root)
        before = (node.pn, node.dn)
        # Force a change at the root on the next refresh.
        ProofNode.objects.filter(id=node.id).update(pn=before[0] + 999)

        locked, release = threading.Event(), threading.Event()

        def hold():
            try:
                with atomic():
                    list(ProofNode.objects.select_for_update()
                         .filter(id=node.id))
                    locked.set()
                    release.wait(30)
            finally:
                connections[ALIAS].close()

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(locked.wait(10))
            done = threading.Event()

            def refresh():
                try:
                    with atomic():
                        self.assertTrue(connection.in_atomic_block)
                        # Seeded at the root itself, so the first ply has to
                        # rewrite the held row.
                        proof.refresh_proof_numbers([root.key])
                    done.set()
                finally:
                    connections[ALIAS].close()

            worker = threading.Thread(target=refresh)
            worker.start()
            # Before this change the refresh would block on the held row
            # until the holder let go.
            self.assertTrue(done.wait(10))
            worker.join(10)
        finally:
            release.set()
            holder.join(10)
        node.refresh_from_db()
        self.assertEqual(node.pn, before[0] + 999)   # left to its holder
