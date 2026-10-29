"""A landing reads its neighbours again once it has committed.

29-Oct-2026, 11:33:06 UTC: the last two open replies of 9d0c0b45 (Black to
move, closed once every reply is proven a White win), f0e41a59 and fe8a79a1,
closed in the same second, each landing in its own ingest process.  Each
cascade read the other reply still open, so neither closed the parent, which
waited for a visitor's click at 11:34:11.  Every landing now registers, with
``transaction.on_commit`` on the AtomicDB alias, a second look at what its
ascents read: of two landings that race, the one that commits last reads
what both wrote.

The race is driven here one landing after the other.  The landing beside
runs its cascade with the first landing's closure out of sight, as READ
COMMITTED showed it, and the captured commit callbacks then run the way the
commit runs them.
"""

import contextlib
import hashlib
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone

from . import ingest, ingest_queue, logic
from .database import connection, current_alias
from .models import AnalysisTask, DBEvent, Edge, IngestJob, Position
from .test_closure_reaches_all_parents import (FORK_FEN, FORK_LINK_MOVE,
                                               FORK_OTHER_MOVE, MATE_LINES)
from .test_proofs import FORCED_MATE_FEN
from .testing import TestCase, TransactionTestCase

# A search of the fork's reply g5h4, the middle link of the won line below
# FORCED_MATE_FEN: White to move, and g6g4 mates at once (its child is
# terminal), so the landing closes the reply.
LINK_LINES = [{'move': 'g6g4', 'eval_cp': 9_999, 'mate': 1, 'pv': ['g6g4']}]
# Black to move with 21 replies.  h6h5 reaches FORCED_MATE_FEN, and h6g5
# reaches a position whose queen move b1g6 reaches the fork.
TOP_FEN = '4p3/8/7k/8/n7/Kp2n3/3p4/1Q6 b - - 0 1'
# Black to move with five king moves; g8h8 reaches a position with a table.
TB_PARENT_FEN = '6k1/8/8/8/8/8/8/KR6 b - - 0 1'
_CLOSURE = ('status', 'closure', 'proof', 'won_line', 'mate_in',
            'clock_slack', 'best_move')


def _expanded(fen):
    pos = ingest.get_or_create_position(fen)
    ingest.expand(pos)
    return pos


def _child(parent, uci):
    return Edge.objects.get(parent=parent, move_uci=uci).child


def _status(pos):
    return Position.objects.values_list('status', flat=True).get(key=pos.key)


def _close_by_hand(*positions):
    """White wins written straight into the rows: no cascade runs."""
    Position.objects.filter(key__in=[pos.key for pos in positions]).update(
        status='WHITE_WIN', closure='MATE_PV', proof='ANDOR', mate_in=1,
        clock_slack=90)


@contextlib.contextmanager
def _out_of_sight(pos):
    """The row as a transaction that has not seen its closure commit reads
    it, and the closure back on the way out (the commit)."""
    rows = Position.objects.filter(key=pos.key)
    saved = rows.values(*_CLOSURE).get()
    rows.update(status='UNKNOWN', closure=None, proof=None, won_line=None,
                mate_in=None, clock_slack=None)
    try:
        yield
    finally:
        rows.update(**saved)


def _landing_beside(reply, unseen):
    """The landing that closes ``reply`` beside the one under test.  Its
    cascade runs with ``unseen`` still open, and it commits first."""
    with _out_of_sight(unseen):
        _close_by_hand(reply)
        ingest.backup_cascade([reply.key])


def _fork():
    """The 9d0c0b45 shape: Black to move, both replies open."""
    fork = _expanded(FORK_FEN)
    return fork, _child(fork, FORK_LINK_MOVE), _child(fork, FORK_OTHER_MOVE)


def _land(pos, lines=LINK_LINES):
    return ingest.ingest_analysis(pos.key, lines, nodes_budget=1_000,
                                  mate_proofs={})


def _receipts():
    return list(DBEvent.objects.filter(kind=ingest.RECHECK_EVENT)
                .values_list('payload', flat=True))


class ClosureRaceTests(TestCase):

    def test_the_landing_that_commits_last_closes_the_parent(self):
        fork, link, other = _fork()

        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True) as callbacks:
            with ingest.closure_attribution('USER'):
                _land(link)
            # Its own cascade closed its reply and read the other one open.
            self.assertEqual(_status(link), 'WHITE_WIN')
            self.assertEqual(_status(fork), 'UNKNOWN')
            _landing_beside(other, unseen=link)
            # Both replies closed and the parent open: 11:33:06 on 29-Oct.
            self.assertEqual(_status(other), 'WHITE_WIN')
            self.assertEqual(_status(fork), 'UNKNOWN')
            self.assertIn(fork.key, ingest.closable_rows([fork.key]))

        self.assertEqual(len(callbacks), 1)
        fork.refresh_from_db()
        self.assertEqual((fork.status, fork.closure), ('WHITE_WIN', 'MINIMAX'))
        closed = DBEvent.objects.get(kind='NODE_CLOSED',
                                     payload__key=fork.key)
        # The label of the landing whose cascade would have closed it.
        self.assertEqual(closed.payload['source'], 'USER')
        [receipt] = _receipts()
        self.assertEqual(receipt['key'], link.key)
        self.assertEqual(receipt['closed'], [fork.key])

    def test_the_landing_that_commits_first_leaves_it_to_the_other(self):
        fork, link, other = _fork()

        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True):
            _land(link)
        # Its recheck read the other reply still open: nothing to do.
        self.assertEqual(_status(fork), 'UNKNOWN')
        self.assertEqual(_receipts(), [])

        # The landing beside commits after it.  Its cascade had read this
        # reply open; its own recheck reads it closed.
        _landing_beside(other, unseen=link)
        summary = ingest.recheck_after_commit(other.key,
                                              [other.key, fork.key])

        self.assertEqual(summary['closed'], [fork.key])
        self.assertEqual(_status(fork), 'WHITE_WIN')

    def test_a_transposed_parent_of_a_closed_link(self):
        """The fork hangs from the middle link of a won line that another
        position's mate materialises: an other parent of what it closed."""
        fork, link, other = _fork()
        analysed = ingest.get_or_create_position(FORCED_MATE_FEN)

        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True):
            ingest.ingest_analysis(analysed.key, MATE_LINES,
                                   nodes_budget=1_000)
            self.assertEqual(_status(link), 'WHITE_WIN')
            self.assertEqual(_status(fork), 'UNKNOWN')
            _landing_beside(other, unseen=link)

        fork.refresh_from_db()
        self.assertEqual((fork.status, fork.closure), ('WHITE_WIN', 'MINIMAX'))

    def test_a_parent_of_what_the_cascade_closed_on_the_way_up(self):
        """The landing closes its reply, the fork and the queen move above
        it; the race is one more level up, at the top."""
        top = _expanded(TOP_FEN)
        via, rival = _child(top, 'h6g5'), _child(top, 'h6h5')
        _close_by_hand(*[edge.child for edge in Edge.objects.filter(
            parent=top).exclude(move_uci__in=('h6g5', 'h6h5'))])
        fork = ingest.get_or_create_position(
            logic.apply_move(via.fen, 'b1g6'))
        self.assertEqual(fork.fen, logic.canonical_fen(FORK_FEN))
        Edge.objects.create(parent=via, move_uci='b1g6', child=fork)
        ingest.expand(fork)
        link, other = _child(fork, FORK_LINK_MOVE), _child(fork,
                                                           FORK_OTHER_MOVE)
        _close_by_hand(other)

        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True):
            _land(link)
            self.assertEqual([_status(pos) for pos in (link, fork, via)],
                             ['WHITE_WIN'] * 3)
            self.assertEqual(_status(top), 'UNKNOWN')
            _landing_beside(rival, unseen=via)
            self.assertEqual(_status(top), 'UNKNOWN')

        top.refresh_from_db()
        self.assertEqual((top.status, top.closure), ('WHITE_WIN', 'MINIMAX'))
        [receipt] = _receipts()
        self.assertEqual(receipt['closed'], [top.key])

    def test_a_tb_landing_rechecks_its_parent(self):
        parent = _expanded(TB_PARENT_FEN)
        table, rest = _child(parent, 'g8h8'), _child(parent, 'g8h7')
        _close_by_hand(*[edge.child for edge in Edge.objects.filter(
            parent=parent).exclude(move_uci__in=('g8h8', 'g8h7'))])
        prepared = {'key': table.key, 'fen': table.fen, 'wdl': 2, 'dtz': 1}

        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True) as callbacks:
            self.assertTrue(ingest._apply_prepared_tb(table.key, prepared))
            self.assertEqual(_status(parent), 'UNKNOWN')
            _landing_beside(rest, unseen=table)

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(_status(parent), 'WHITE_WIN')

    def test_it_waits_for_the_commit_of_the_atomicdb_alias(self):
        """Registered on any other alias it would find no transaction there
        to wait for, and run before the landing had committed."""
        fork, link, _other = _fork()
        alias = current_alias()

        with self.captureOnCommitCallbacks(using=alias) as on_alias, \
                self.captureOnCommitCallbacks(using='default') as on_default:
            _land(link)

        self.assertEqual(len(on_alias), 1)
        if alias != 'default':
            self.assertEqual(on_default, [])
        # Nothing ran before the commit: the callbacks were only captured.
        self.assertEqual(_receipts(), [])


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


class StaleBackedTests(TestCase):
    """The same race on the backed value (b3225133, "bad arrow", 29-Oct)."""

    def test_the_last_write_on_a_stale_sibling_is_repaired(self):
        # Black to move with two searched replies; its backed value stands
        # on b1b2 at 200 before either lands.
        parent = Position.objects.create(
            key=_key('STALE-P'), fen='4k3/8/8/8/8/8/8/4K3 b - - 0 1',
            expanded=True, eval_cp=150, nodes_invested=128_000_000)
        mover = ingest.get_or_create_position('4k3/8/8/8/8/8/8/3K4 w - - 0 1')
        Position.objects.filter(key=mover.key).update(
            eval_cp=100, nodes_invested=128_000_000)
        rival = Position.objects.create(
            key=_key('STALE-B'), fen='4k3/8/8/8/8/8/8/2K5 w - - 0 1',
            eval_cp=200, nodes_invested=128_000_000)
        Edge.objects.create(parent=parent, move_uci='a1a2', child=mover)
        Edge.objects.create(parent=parent, move_uci='b1b2', child=rival)
        lines = [{'move': 'd1d2', 'eval_cp': 300, 'mate': None,
                  'pv': ['d1d2']}]

        with self.captureOnCommitCallbacks(using=current_alias(),
                                           execute=True):
            ingest.ingest_analysis(mover.key, lines,
                                   nodes_budget=128_000_000, mate_proofs={})
            # The landing valued the parent with b1b2 still at 200.
            parent.refresh_from_db()
            self.assertEqual((parent.backed_eval, parent.backed_move),
                             (200, 'b1b2'))
            # The search of b1b2 committed beside it, at 400; the parent its
            # ascent wrote was overwritten by this landing's later commit.
            Position.objects.filter(key=rival.key).update(
                eval_cp=400, nodes_invested=256_000_000)
            _stored, decided = ingest.stale_backed_rows(
                [parent.key])[parent.key]
            self.assertEqual(decided[:2], (300, 'a1a2'))

        parent.refresh_from_db()
        self.assertEqual((parent.backed_eval, parent.backed_move,
                          parent.backed_plies, parent.backed_nodes),
                         decided)
        [receipt] = _receipts()
        self.assertEqual((receipt['stale'], receipt['closed']),
                         ([parent.key], []))
        self.assertGreaterEqual(receipt['changed'], 1)


class SafetyTests(TestCase):

    def test_a_recheck_that_fails_leaves_the_landing_applied(self):
        _fork_row, link, _other = _fork()
        task = AnalysisTask.objects.create(position=link, generation=0,
                                           budget_nodes=1_000)
        job = IngestJob.objects.create(
            task=task, position=link,
            payload={'lines': LINK_LINES, 'nodes': 1_000})

        with self.captureOnCommitCallbacks(using=current_alias()) as callbacks:
            summary, error = ingest_queue.process_job(job)

        self.assertIsNone(error)
        self.assertIn('backed_up', summary)
        with mock.patch.object(ingest, 'closable_rows',
                               side_effect=OperationalError('boom')), \
                self.assertLogs('atomicdb.ingest', 'ERROR'):
            for callback in callbacks:
                callback()          # it must not raise
        job.refresh_from_db()
        self.assertEqual(job.state, IngestJob.JState.DONE)
        self.assertEqual(_status(link), 'WHITE_WIN')
        self.assertEqual(_receipts(), [])

    def test_at_most_sixteen_positions_are_read(self):
        many = [_key(f'MANY-{index}') for index in range(20)]

        with mock.patch.object(ingest, 'closable_rows',
                               wraps=ingest.closable_rows) as closure, \
                mock.patch.object(ingest, 'stale_backed_rows',
                                  wraps=ingest.stale_backed_rows) as backed:
            summary = ingest.recheck_after_commit(many[0], many,
                                                  climbed=many)

        self.assertEqual(summary['checked'], ingest.RECHECK_MAX_KEYS)
        self.assertEqual(closure.call_args[0][0], many[:16])
        self.assertEqual(backed.call_args[0][0], many[:16])

    def test_a_second_recheck_finds_the_parent_closed(self):
        """Two landings that commit together both read the parent closable;
        the second finds it closed, and it is announced once."""
        fork, link, other = _fork()
        _close_by_hand(link, other)

        first = ingest.recheck_after_commit(link.key, [link.key, fork.key])
        second = ingest.recheck_after_commit(other.key, [other.key, fork.key])

        self.assertEqual((first['closed'], second['closed']), ([fork.key], []))
        self.assertEqual(DBEvent.objects.filter(
            kind='NODE_CLOSED', payload__key=fork.key).count(), 1)
        self.assertEqual(len(_receipts()), 1)

    def test_a_quiet_recheck_writes_nothing(self):
        fork, link, _other = _fork()
        before = dict(Position.objects.values_list('key', 'updated'))
        events = DBEvent.objects.count()

        summary = ingest.recheck_after_commit(link.key, [link.key, fork.key])

        self.assertEqual(summary, {'checked': 2, 'closed': [], 'stale': [],
                                   'changed': 0})
        self.assertEqual(dict(Position.objects.values_list('key', 'updated')),
                         before)
        self.assertEqual(DBEvent.objects.count(), events)

    def test_its_receipt_stays_off_the_public_feed(self):
        from .views import FEED_HIDDEN_KINDS

        self.assertIn(ingest.RECHECK_EVENT, FEED_HIDDEN_KINDS)


def _lock_timeout():
    cause = Exception('canceling statement due to lock timeout')
    cause.sqlstate = '55P03'
    error = OperationalError('canceling statement due to lock timeout')
    error.__cause__ = cause
    return error


class LockConflictTests(TransactionTestCase):
    """Outside a transaction, as after a real commit: a lock conflict rolls
    the closure back whole and it is tried again."""

    def _decided_fork(self):
        fork, link, other = _fork()
        _close_by_hand(link, other)
        return fork, link

    def test_a_lock_conflict_is_tried_again(self):
        fork, _link = self._decided_fork()
        original = ingest.backup_cascade
        calls = []

        def flaky(keys, report=None):
            calls.append(list(keys))
            if len(calls) == 1:
                raise _lock_timeout()
            return original(keys, report=report)

        with mock.patch.object(ingest, 'backup_cascade', side_effect=flaky), \
                mock.patch.object(ingest.time, 'sleep') as slept:
            closed = ingest.close_missed([fork.key])

        self.assertEqual(closed, [fork.key])
        self.assertEqual(calls, [[fork.key], [fork.key]])
        slept.assert_called_once_with(ingest.RECHECK_RETRY_SLEEPS[0])
        self.assertEqual(DBEvent.objects.filter(
            kind='NODE_CLOSED', payload__key=fork.key).count(), 1)

    def test_a_conflict_that_outlasts_the_retries_is_only_logged(self):
        fork, link = self._decided_fork()

        with mock.patch.object(ingest, 'backup_cascade',
                               side_effect=_lock_timeout()), \
                mock.patch.object(ingest.time, 'sleep') as slept, \
                self.assertLogs('atomicdb.ingest', 'ERROR'):
            summary = ingest.recheck_after_commit(link.key,
                                                  [link.key, fork.key])

        self.assertIsNone(summary)
        self.assertEqual(slept.call_count, len(ingest.RECHECK_RETRY_SLEEPS))
        # Rolled back whole: the parent is still open, for the sweep.
        self.assertEqual(_status(fork), 'UNKNOWN')

    def test_the_session_lock_timeout_is_put_back(self):
        if connection.vendor != 'postgresql':
            self.skipTest('lock_timeout is a PostgreSQL setting')
        fork, link = self._decided_fork()

        def lock_timeout():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('lock_timeout')")
                return cursor.fetchone()[0]

        before = lock_timeout()
        seen = []
        original = ingest.backup_cascade

        def watching(keys, report=None):
            seen.append(lock_timeout())
            return original(keys, report=report)

        with mock.patch.object(ingest, 'backup_cascade', side_effect=watching):
            summary = ingest.recheck_after_commit(link.key,
                                                  [link.key, fork.key])

        self.assertEqual(summary['closed'], [fork.key])
        self.assertEqual(seen, ['%dms' % ingest.RECHECK_LOCK_TIMEOUT_MS])
        self.assertEqual(lock_timeout(), before)


def _sweep(*args):
    out = StringIO()
    call_command('sweep_stale_backed', '--settle-seconds', '0', *args,
                 stdout=out, stderr=StringIO())
    return (DBEvent.objects.filter(kind='BACKED_SWEEP').order_by('-id')
            .first(), out.getvalue())


class SweepBackstopTests(TestCase):
    """What no recheck saw: the sweep asks the cascade's question too, where
    ingests met."""

    def _missed(self):
        """Both replies closed and no cascade run since: the 29-Oct state
        that a lost recheck would leave."""
        fork, link, other = _fork()
        _close_by_hand(link, other)
        return fork

    def test_the_sweep_closes_a_parent_both_rechecks_missed(self):
        fork = self._missed()

        event, _out = _sweep('--keys', fork.key)

        self.assertEqual(_status(fork), 'WHITE_WIN')
        self.assertEqual((event.payload['closable'],
                          event.payload['closable_confirmed'],
                          event.payload['closed']), (1, 1, 1))
        self.assertEqual(event.payload['closable_examples'],
                         [{'key': fork.key, 'would_be': 'WHITE_WIN'}])
        closed = DBEvent.objects.get(kind='NODE_CLOSED', payload__key=fork.key)
        self.assertEqual(closed.payload['source'], 'NONE')

    def test_a_merge_point_of_overlapping_landings_is_closed(self):
        fork = self._missed()
        start = timezone.now() - timedelta(minutes=5)
        for offset, uci in enumerate((FORK_LINK_MOVE, FORK_OTHER_MOVE)):
            reply = _child(fork, uci)
            task = AnalysisTask.objects.create(position=reply, generation=0,
                                               budget_nodes=128_000_000)
            job = IngestJob.objects.create(task=task, position=reply,
                                           payload={},
                                           state=IngestJob.JState.DONE)
            IngestJob.objects.filter(pk=job.pk).update(
                created=start + timedelta(seconds=offset),
                updated=start + timedelta(seconds=offset + 4))

        event, _out = _sweep('--no-guards', '--no-rotation')

        self.assertEqual(event.payload['recent'],
                         {'jobs': 2, 'merge_points': 1})
        self.assertEqual(event.payload['closed'], 1)
        self.assertEqual(_status(fork), 'WHITE_WIN')

    def test_the_rotation_is_left_to_the_backed_check(self):
        """The cost of the cascade's check on every rotation slice buys
        nothing the merge points and the rechecks do not."""
        fork = self._missed()

        event, _out = _sweep('--no-guards', '--no-recent', '--units',
                             '1000000')

        self.assertEqual(event.payload['closable'], 0)
        self.assertEqual(_status(fork), 'UNKNOWN')

    def test_a_dry_run_reports_it_and_closes_nothing(self):
        fork = self._missed()
        out = StringIO()

        call_command('sweep_stale_backed', '--dry-run', '--settle-seconds',
                     '0', '--keys', fork.key, stdout=out)

        self.assertEqual(_status(fork), 'UNKNOWN')
        self.assertIn('closable=1', out.getvalue())
        self.assertIn('closed=0', out.getvalue())
        self.assertFalse(DBEvent.objects.filter(kind='BACKED_SWEEP').exists())

    def test_a_parent_closed_in_between_is_left_alone(self):
        fork = self._missed()

        def landing(_seconds):
            ingest.backup_cascade([fork.key])

        with mock.patch(
                'atomicdb.management.commands.sweep_stale_backed.time.sleep',
                side_effect=landing):
            event, _out = _sweep('--keys', fork.key, '--settle-seconds', '1')

        self.assertEqual((event.payload['closable'],
                          event.payload['closable_confirmed'],
                          event.payload['closed']), (1, 0, 0))
        self.assertEqual(DBEvent.objects.filter(
            kind='NODE_CLOSED', payload__key=fork.key).count(), 1)
