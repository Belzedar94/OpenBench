import threading
from unittest import mock

from django.test import SimpleTestCase

from Client import atomicdb_worker


class WorkerHttpSessionTests(SimpleTestCase):

    def setUp(self):
        self._discard_current_session()

    def tearDown(self):
        self._discard_current_session()

    @staticmethod
    def _discard_current_session():
        session = getattr(atomicdb_worker._HTTP_THREAD_LOCAL, 'session', None)
        if session is not None:
            session.close()
            del atomicdb_worker._HTTP_THREAD_LOCAL.session

    @mock.patch('Client.atomicdb_worker.requests.Session')
    def test_reuses_one_session_within_a_worker_thread(self, session_class):
        session = session_class.return_value

        first = atomicdb_worker._http_session()
        second = atomicdb_worker._http_session()

        self.assertIs(first, session)
        self.assertIs(second, session)
        session_class.assert_called_once_with()

    def test_does_not_share_a_session_between_threads(self):
        main_session = atomicdb_worker._http_session()
        child_sessions = []

        def create_session():
            child_sessions.append(atomicdb_worker._http_session())

        thread = threading.Thread(target=create_session)
        thread.start()
        thread.join()

        self.assertEqual(len(child_sessions), 1)
        self.assertIsNot(child_sessions[0], main_session)
        child_sessions[0].close()

    def test_adapter_retries_connect_only_for_all_http_methods(self):
        retry = atomicdb_worker._http_session().get_adapter(
            'https://').max_retries

        self.assertEqual(retry.total, atomicdb_worker.HTTP_CONNECT_RETRIES)
        self.assertEqual(retry.connect, atomicdb_worker.HTTP_CONNECT_RETRIES)
        self.assertEqual(retry.read, 0)
        self.assertEqual(retry.status, 0)
        self.assertEqual(retry.redirect, 0)
        self.assertEqual(retry.other, 0)
        self.assertIsNone(retry.allowed_methods)

