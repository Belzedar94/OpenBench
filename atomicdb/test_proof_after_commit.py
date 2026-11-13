"""A drained batch refreshes its proof numbers once it has committed.

The drainer used to refresh pn/dn twice per batch: at the end of the
cascade, inside the batch transaction, and again for the landed keys after
the commit.  Now the cascade's calls wait for the commit and run first
after it, and the second climb is gone (§ ingest.proof_refresh_after_commit).
The rechecks of a batch share the reads that are the same for all of them
(§ ingest._RecheckMemo).  What is defended here:

1. the same landings drained the old way and the new way end with the same
   positions (values, closures) and the same proof nodes (pn, dn,
   selected_child, expanded_in_proof), on a line from the start position, a
   transposition, a closure reaching another parent and a repetition cycle
   (carrier walks through a loop back to the start position);
2. the new way refreshes nothing inside the transaction, and refreshes
   after the commit;
3. the memo reads a shared answer once while the rechecks only read, and
   reads afresh after one of them writes.
"""

from unittest import mock

from . import ingest, logic, propagation_queue
from .database import atomic, current_alias
from .models import DBEvent, Edge, Position, ProofNode, PropagationSeed
from .test_deferred_propagation import (_FIELDS, _Rollback, _child, _closure,
                                        _expanded, _land, _lines, _start_line,
                                        _transposition)
from .testing import TestCase


def _cycle():
    """1.Nf3 Nf6 2.Ng1 and then Ng8 is the start position again: the
    position after 2.Ng1 has an edge back to the root, and the proof
    numbers walk their carriers round it."""
    root = _expanded(logic.start_fen())
    knight = _child(root, 'g1f3')
    ingest.expand(knight)
    reply = _child(knight, 'g8f6')
    ingest.expand(reply)
    back = _child(reply, 'f3g1')
    ingest.expand(back)
    assert _child(back, 'f6g8').key == root.key
    other = _child(root, 'e2e3')
    ingest.expand(other)
    return [
        (back.key, _lines(back, [-30, -20, -10, 0, 10]), 4_000_000, {}),
        (reply.key, _lines(reply, [60, 40, 20, 0, -20]), 2_000_000, {}),
        (other.key, _lines(other, [-50, -25, 0, 25, 50]), 2_000_000, {}),
        (knight.key, _lines(knight, [-45, -35, -25, -15, -5]),
         1_000_000, {}),
        (root.key, _lines(root, [50, 40, 30, 20, 10]), 1_000_000, {}),
    ]


def _state():
    positions = {row[0]: row[1:] for row in Position.objects.order_by('key')
                 .values_list('key', *_FIELDS)}
    nodes = {(row[0], row[1]): row[2:] for row in ProofNode.objects
             .values_list('campaign_id', 'position_id', 'pn', 'dn',
                          'selected_child', 'expanded_in_proof')}
    closed = sorted(DBEvent.objects.filter(kind='NODE_CLOSED')
                    .values_list('payload__key', flat=True))
    return positions, nodes, closed


class SameProofNumbersTests(TestCase):
    """1. Old and new drains end in the same place."""

    def _run(self, build, after_commit):
        try:
            with atomic():
                for key, lines, budget, proofs in build():
                    _land(key, lines, budget, proofs, True)
                self.assertTrue(PropagationSeed.objects.exists())
                with mock.patch.object(
                        propagation_queue, 'proof_after_commit_enabled',
                        return_value=after_commit):
                    with self.captureOnCommitCallbacks(
                            using=current_alias(), execute=True):
                        result = propagation_queue.drain()
                self.assertEqual(result['failed_batches'], 0)
                self.assertFalse(PropagationSeed.objects.exists())
                state = _state()
                raise _Rollback()
        except _Rollback:
            return state

    def _assert_same(self, build):
        old = self._run(build, after_commit=False)
        new = self._run(build, after_commit=True)
        self.assertEqual(old[0].keys(), new[0].keys())
        for key in old[0]:
            self.assertEqual(old[0][key], new[0][key], f'position {key[:12]}')
        self.assertTrue(old[1], 'the fixture must write proof nodes')
        self.assertEqual(old[1].keys(), new[1].keys())
        for key in old[1]:
            self.assertEqual(old[1][key], new[1][key], f'proof node {key}')
        self.assertEqual(old[2], new[2])
        return old

    def test_a_line_from_the_start_position(self):
        self._assert_same(_start_line)

    def test_a_transposition(self):
        self._assert_same(_transposition)

    def test_a_closure_that_reaches_another_parent(self):
        _positions, _nodes, closed = self._assert_same(_closure)
        self.assertTrue(closed)

    def test_a_repetition_cycle(self):
        self._assert_same(_cycle)


class WhenTheRefreshRunsTests(TestCase):
    """2. Nothing inside the transaction, everything after it."""

    def test_the_refresh_waits_for_the_commit(self):
        try:
            with atomic():
                for key, lines, budget, proofs in _transposition():
                    _land(key, lines, budget, proofs, True)
                calls = []
                real = ingest.proof.refresh_proof_numbers

                def spy(seeds, *args, **kwargs):
                    calls.append(list(seeds))
                    return real(seeds, *args, **kwargs)

                with mock.patch.object(
                        propagation_queue, 'proof_after_commit_enabled',
                        return_value=True), \
                        mock.patch.object(ingest.proof,
                                          'refresh_proof_numbers', spy):
                    with self.captureOnCommitCallbacks(
                            using=current_alias(), execute=True) as hooks:
                        propagation_queue.drain()
                        # Committed nothing yet: no refresh has run.
                        self.assertEqual(calls, [])
                # The batch's hook is the first one, before the rechecks.
                self.assertEqual(hooks[0].__qualname__,
                                 'drain_batch.<locals>.refresh_proof')
                self.assertTrue(calls)
                landed = {key for key, _l, _b, _p in _transposition()}
                seeded = {key for seeds in calls for key in seeds}
                self.assertTrue(landed <= seeded)
                raise _Rollback()
        except _Rollback:
            pass

    def test_inside_another_transaction_it_refreshes_as_before(self):
        with atomic():
            self.assertFalse(propagation_queue.proof_after_commit_enabled())

    def test_the_setting_turns_it_off(self):
        with self.settings(ATOMICDB_PROOF_AFTER_COMMIT=False), \
                mock.patch.object(propagation_queue, 'connection') as conn:
            conn.in_atomic_block = False
            self.assertFalse(propagation_queue.proof_after_commit_enabled())
        with mock.patch.object(propagation_queue, 'connection') as conn:
            conn.in_atomic_block = False
            self.assertTrue(propagation_queue.proof_after_commit_enabled())


class RecheckMemoTests(TestCase):
    """3. Shared reads once, and afresh after a write."""

    def setUp(self):
        self.root = _expanded(logic.start_fen())
        self.child = _child(self.root, 'g1f3')
        ingest.expand(self.child)
        self.grand = _child(self.child, 'g8f6')

    def test_shared_reads_are_read_once_while_nothing_is_written(self):
        memo = ingest._RecheckMemo()
        real = ingest.closable_rows
        asked = []

        def spy(keys):
            asked.append(sorted(keys))
            return real(keys)

        with mock.patch.object(ingest, 'closable_rows', spy):
            for _ in range(3):
                ingest._recheck(self.grand.key, [self.grand.key],
                                [self.grand.key], memo)
        # The first recheck read its keys; the others found every answer.
        self.assertEqual(len(asked), 1)
        self.assertIn(self.child.key, asked[0])
        self.assertEqual(list(memo.parents.values()), [[self.child.key]])

    def test_a_recheck_that_writes_clears_what_the_batch_read(self):
        memo = ingest._RecheckMemo()
        memo.closable[self.child.key] = False
        memo.parents[('x',)] = ['y']
        with mock.patch.object(ingest, 'stale_backed_rows',
                               return_value={self.child.key: ((), ())}), \
                mock.patch.object(ingest, 'backup_backed_evals_retrying',
                                  return_value=0) as climb:
            summary = ingest._recheck(self.child.key, [self.child.key], [],
                                      memo)
        self.assertEqual(summary['stale'], [self.child.key])
        climb.assert_called_once()
        self.assertEqual(memo.closable, {})
        self.assertEqual(memo.parents, {})

    def test_with_and_without_the_memo_the_answers_agree(self):
        keys = [self.root.key, self.child.key, self.grand.key]
        plain = ingest._recheck(self.grand.key, keys, [self.grand.key])
        memo = ingest._RecheckMemo()
        first = ingest._recheck(self.grand.key, keys, [self.grand.key], memo)
        second = ingest._recheck(self.grand.key, keys, [self.grand.key], memo)
        self.assertEqual(plain, first)
        self.assertEqual(plain, second)

    def test_a_batch_shares_one_memo_and_a_single_landing_none(self):
        seen = []

        def spy(landed, keys, climbed, memo=None):
            seen.append(memo)

        landings = [ingest.Landing(self.child.key, (), (), [self.root.key],
                                   (), pos=self.child),
                    ingest.Landing(self.grand.key, (), (), [self.child.key],
                                   (), pos=self.grand)]
        with mock.patch.object(ingest, '_recheck_once_committed', spy):
            ingest.propagate_landings(landings)
            ingest.propagate_landings(landings[:1])
        self.assertEqual(len(seen), 3)
        self.assertIsNotNone(seen[0])
        self.assertIs(seen[0], seen[1])
        self.assertIsNone(seen[2])
