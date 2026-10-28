"""A lease locks the TASK row, never its position.  PostgreSQL only.

On 28-Oct-2026 a USER request of 128M, first in the queue, waited 1,912 s
while the only machine serving the shared queue leased 60 tasks that ranked
below it.  The order was right; the lock was not.  ``_locked_in_order`` asked
for ``FOR UPDATE SKIP LOCKED`` together with ``select_related('position')``,
and without ``of`` PostgreSQL locks every table of the join: whenever the
ingest held the position -- it locks it to write each result that climbs
through it -- the task was skipped as if another worker had it.

Each test holds a row with ``SELECT ... FOR UPDATE`` from ANOTHER connection
(a thread, which in Django has its own) while a worker asks for work through
the usual protocol: the position, as the ingest does, or in the last test the
task itself, as another worker's lease does.  On SQLite ``select_for_update``
does nothing and there is nothing to test.
"""

import threading
from unittest import skipUnless

from django.utils import timezone

from . import ingest, logic, views
from .database import atomic, connection
from .models import AnalysisTask, Position, SolveTask
from .testing import TransactionTestCase, worker_account

POSTGRES = connection.vendor == 'postgresql'
# How long a statement of the test's own connection may wait for a lock while
# the other one holds its row.  A lease must not wait for the position at all;
# without this bound the path that waits (no ``skip_locked``) would just sit
# until the holder lets go, and pass late instead of failing.
LOCK_TIMEOUT = '3s'


class _HeldElsewhere:
    """``SELECT ... FOR UPDATE`` on ``rows`` from another transaction.

    The holder is a thread, so it is a different connection and a different
    transaction for real: its lock blocks the test's connection exactly as
    the ingest's lock blocks a lease.  While it is held, the test's
    connection gives up on any lock wait after ``LOCK_TIMEOUT``.
    """

    def __init__(self, rows):
        self.rows = rows
        self.locked = False
        self.ready = threading.Event()
        self.release = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        try:
            with atomic():
                if not list(self.rows.select_for_update()):
                    raise AssertionError('nothing to lock')
                self.locked = True
                self.ready.set()
                self.release.wait(timeout=60)
        except Exception as error:
            self.error = error
        finally:
            self.ready.set()
            connection.close()

    def _let_go(self):
        self.release.set()
        self.thread.join(timeout=60)

    def __enter__(self):
        self.thread.start()
        self.ready.wait(timeout=60)
        if not self.locked:
            self._let_go()
            raise self.error or AssertionError('the holder never got its lock')
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        except Exception:
            self._let_go()
            raise
        return self

    def __exit__(self, *exc_info):
        try:
            with connection.cursor() as cursor:
                cursor.execute('RESET lock_timeout')
        finally:
            self._let_go()
        return False


@skipUnless(POSTGRES, 'select_for_update only locks rows on PostgreSQL')
class LeaseWithThePositionLockedTests(TransactionTestCase):

    def setUp(self):
        worker_account('worker', 'secret')
        self.position = ingest.get_or_create_position(logic.start_fen())

    def _position_locked(self):
        return _HeldElsewhere(Position.objects.filter(key=self.position.key))

    def _lease(self, machine, session=None):
        return self.client.post('/atomicdb/api/lease', {
            'username': 'worker', 'password': 'secret', 'machine': machine,
            'worker_build': views.LEASE_TOKEN_BUILD,
            'lease_session': session or machine,
        }).json()['tasks']

    def _request(self, position=None, **fields):
        return AnalysisTask.objects.create(
            position=position or self.position, generation=0,
            budget_nodes=128_000_000, source=AnalysisTask.Source.USER,
            requested_by='asker', **fields)

    def test_a_request_is_leased_while_the_ingest_holds_its_position(self):
        request = self._request()

        with self._position_locked():
            tasks = self._lease('box-1')

        self.assertEqual([task['id'] for task in tasks], [request.id])
        request.refresh_from_db()
        self.assertEqual((request.state, request.machine),
                         ('LEASED', 'box-1'))

    def test_a_replayed_lease_does_not_wait_for_its_position(self):
        # The machine's live assignment is read under FOR UPDATE without
        # ``skip_locked``: locking the position as well made the request wait
        # behind the ingest (here: give up after LOCK_TIMEOUT).
        now = timezone.now()
        lease = self._request(state='LEASED', machine='box-1', attempts=1,
                              leased_at=now, lease_heartbeat_at=now,
                              lease_token='token-1',
                              lease_session='session-1')

        with self._position_locked():
            tasks = self._lease('box-1', session='session-1')

        self.assertEqual([(task['id'], task['lease_token']) for task in tasks],
                         [(lease.id, 'token-1')])

    def test_a_proof_task_is_leased_while_the_ingest_holds_its_position(self):
        proof = SolveTask.objects.create(position=self.position,
                                         budget_nodes=2_000_000)

        with self._position_locked():
            tasks = self.client.post('/atomicdb/api/solve/acquire', {
                'username': 'worker', 'password': 'secret',
                'machine': 'box-1', 'lease_session': 'solve-1',
            }).json()['tasks']

        self.assertEqual([task['id'] for task in tasks], [proof.id])

    def test_a_task_another_transaction_holds_is_still_skipped(self):
        # The other half of the contract: what stopped being locked is the
        # position, not the task.  Two workers still cannot take one task.
        held = self._request()
        other = self._request(position=ingest.get_or_create_position(
            logic.apply_move(self.position.fen, 'g1f3')))

        with _HeldElsewhere(AnalysisTask.objects.filter(pk=held.pk)):
            tasks = self._lease('box-2')

        self.assertEqual([task['id'] for task in tasks], [other.id])
        held.refresh_from_db()
        self.assertEqual(held.state, 'PENDING')
