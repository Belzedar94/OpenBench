"""El backfill sobrevive a deadlocks; a todo lo demas, no.

Nacido del 4-ago: la limpieza global de ``reachable`` era un solo UPDATE de
millones de filas y PG la mato por deadlock contra la ingesta viva. El
reintento repite el LOTE (idempotente), no la pasada.
"""
from unittest import mock

from django.db.utils import OperationalError
from django.test import SimpleTestCase

from atomicdb.management.commands.backfill_reachable import _retry_deadlock


def _deadlock():
    return OperationalError(
        'deadlock detected\nDETAIL:  Process 1 waits for ShareLock')


class RetryDeadlockTests(SimpleTestCase):

    def test_reintenta_el_deadlock_y_devuelve_el_resultado(self):
        fn = mock.Mock(side_effect=[_deadlock(), _deadlock(), 42])
        with mock.patch('time.sleep'):
            self.assertEqual(_retry_deadlock(fn), 42)
        self.assertEqual(fn.call_count, 3)

    def test_otro_operationalerror_sube_sin_reintentar(self):
        fn = mock.Mock(side_effect=OperationalError('connection is closed'))
        with self.assertRaises(OperationalError):
            _retry_deadlock(fn)
        self.assertEqual(fn.call_count, 1)

    def test_el_ultimo_intento_sube_el_deadlock(self):
        fn = mock.Mock(side_effect=[_deadlock()] * 6)
        with mock.patch('time.sleep'):
            with self.assertRaises(OperationalError):
                _retry_deadlock(fn, attempts=6)
        self.assertEqual(fn.call_count, 6)
