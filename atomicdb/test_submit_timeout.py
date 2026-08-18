from unittest import mock

from django.test import SimpleTestCase

from Client import atomicdb_worker


class WorkerSubmitRetryTests(SimpleTestCase):

    def setUp(self):
        self.session = mock.Mock()
        patcher = mock.patch('Client.atomicdb_worker._http_session',
                             return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch('Client.atomicdb_worker.time.sleep')
    def test_retries_exact_payload_after_read_timeout(self, sleep):
        post = self.session.post
        accepted = mock.Mock(status_code=200)
        accepted.json.return_value = {'ok': True, 'dup': True}
        post.side_effect = [atomicdb_worker.requests.ReadTimeout('late ACK'),
                            accepted]
        payload = {'task_id': 3016, 'lines': '[{"move":"e2e4"}]'}

        response = atomicdb_worker._submit_until_definitive(
            'https://example.invalid', payload, 3016)

        self.assertIs(response, accepted)
        self.assertEqual(post.call_count, 2)
        self.assertIs(post.call_args_list[0].kwargs['data'], payload)
        self.assertIs(post.call_args_list[1].kwargs['data'], payload)
        self.assertEqual(post.call_args_list[0].kwargs['timeout'], (3, 600))
        sleep.assert_called_once_with(15)

    @mock.patch('Client.atomicdb_worker.time.sleep')
    def test_does_not_retry_definitive_stale_lease(self, sleep):
        post = self.session.post
        stale = mock.Mock(status_code=409)
        stale.json.return_value = {'error': 'stale-lease'}
        post.return_value = stale

        response = atomicdb_worker._submit_until_definitive(
            'https://example.invalid', {'task_id': 7}, 7)

        self.assertIs(response, stale)
        post.assert_called_once()
        sleep.assert_not_called()
