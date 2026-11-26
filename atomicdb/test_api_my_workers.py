"""``api/my-workers``: the "Your workers" table of the profile, as JSON.

Requested by Torom (Discord, 26-Nov-2026).  What is pinned here: the two
doors of ``my-queue`` (session GET, credentials POST as form or JSON), the
refusals, that only the caller's own machines ever come back, and that every
row says what the profile table prints for the same machine.
"""

import json
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from . import contributors
from .models import AnalysisTask, WorkerPing
from .testing import TestCase, worker_account
from .test_profile import _child, _completed, _ping, _reading, _task


class MyWorkersApiTests(TestCase):

    def setUp(self):
        contributors.reset_cache()
        worker_account('alice', 'p')
        worker_account('bob', 'p')
        self.alice = {'username': 'alice', 'password': 'p'}
        # No session, CSRF enforced: the way a script arrives.
        self.script = Client(enforce_csrf_checks=True)
        _ping('alice-box-atomicdb', 'alice', threads=16, worker_build=42)
        old = _ping('alice-old-atomicdb', 'alice', threads=4)
        WorkerPing.objects.filter(pk=old.pk).update(
            last_seen=timezone.now() - timedelta(days=3))
        _ping('bob-box-atomicdb', 'bob')
        _completed(_child('b2b3'), 'alice-box-atomicdb', 6_000_000, 10,
                   ago_hours=0.5, delivered_by='alice')
        _completed(_child('c2c3'), 'alice-old-atomicdb', 2_000_000, 5,
                   ago_hours=48)
        _completed(_child('d2d3'), 'bob-box-atomicdb', 9_000_000, 10)
        self.running = _task(
            _child('a2a3'), state=AnalysisTask.TState.LEASED,
            machine='alice-box-atomicdb', leased_at=timezone.now(),
            lease_heartbeat_at=timezone.now())

    def _post(self, path='/atomicdb/api/my-workers/', **body):
        return self.script.post(path, body or self.alice)

    def test_a_get_without_a_session_needs_a_login(self):
        response = Client().get('/atomicdb/api/my-workers/')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'login required'})

    def test_wrong_or_missing_credentials_are_refused(self):
        worker_account('expelled', 'p', enabled=False)
        _ping('expelled-box-atomicdb', 'expelled')

        for body in ({'username': 'alice', 'password': 'wrong'},
                     {'username': 'expelled', 'password': 'p'},
                     {'username': 'nobody', 'password': 'p'},
                     {}):
            response = self.script.post('/atomicdb/api/my-workers/', body)
            self.assertEqual(response.status_code, 403, body)
            self.assertEqual(response.json(), {'error': 'bad credentials'})

    def test_other_methods_are_refused(self):
        self.assertEqual(
            self.script.put('/atomicdb/api/my-workers/').status_code, 405)

    def test_only_your_own_workers_come_back(self):
        payload = self._post().json()

        self.assertEqual(payload['account'], 'alice')
        self.assertEqual([w['machine'] for w in payload['workers']],
                         ['alice-box-atomicdb', 'alice-old-atomicdb'])
        self.assertEqual(payload['online'], 1)
        self.assertNotIn('bob-box-atomicdb', json.dumps(payload))

    def test_the_credentials_decide_the_account_not_the_session(self):
        browser = Client()
        browser.login(username='bob', password='p')

        payload = browser.post('/atomicdb/api/my-workers/', self.alice).json()

        self.assertEqual(payload['account'], 'alice')

    def test_a_signed_in_browser_gets_its_own(self):
        browser = Client()
        browser.login(username='bob', password='p')

        payload = browser.get('/atomicdb/api/my-workers/').json()

        self.assertEqual(payload['account'], 'bob')
        self.assertEqual([w['machine'] for w in payload['workers']],
                         ['bob-box-atomicdb'])

    def test_json_credentials_and_the_unslashed_route_answer(self):
        as_json = self.script.post('/atomicdb/api/my-workers/',
                                   data=json.dumps(self.alice),
                                   content_type='application/json')
        unslashed = self._post('/atomicdb/api/my-workers')

        self.assertEqual(as_json.status_code, 200)
        self.assertEqual(unslashed.status_code, 200)
        self.assertEqual(as_json.json()['account'], 'alice')
        self.assertEqual(unslashed.json()['account'], 'alice')

    def test_a_worker_without_machines_gets_an_empty_list(self):
        worker_account('carol', 'p')

        payload = self._post(username='carol', password='p').json()

        self.assertEqual(payload, {'account': 'carol', 'online': 0,
                                   'workers': []})

    def test_each_row_says_what_the_profile_table_prints(self):
        workers = {w['machine']: w for w in self._post().json()['workers']}
        page = {w['machine']: w for w in contributors.present('alice')['workers']}
        body = _reading(
            Client().get('/atomicdb/user/alice/').content.decode())

        box = workers['alice-box-atomicdb']
        self.assertEqual(set(workers), set(page))
        for machine, row in workers.items():
            shown = page[machine]
            for field in ('build', 'threads', 'hash_mb', 'os', 'online',
                          'seen', 'nodes_24h', 'tasks_24h', 'nodes_all',
                          'tasks_all', 'nps'):
                self.assertEqual(row[field], shown[field], (machine, field))
            self.assertEqual(row['searches_last_hour'], shown['searches'])
            self.assertEqual(contributors.human(row['nodes_all']),
                             shown['nodes_all_h'])

        self.assertEqual(box['build'], 42)
        self.assertEqual(box['threads'], 16)
        self.assertTrue(box['online'])
        self.assertEqual(box['nodes_24h'], 6_000_000)
        self.assertEqual(box['tasks_all'], 1)
        self.assertEqual(box['nps'], 600_000)
        self.assertIsNotNone(box['last_seen'])
        self.assertEqual(box['searching'], {
            'task_id': self.running.id, 'position': self.running.position_id,
            'line': page['alice-box-atomicdb']['searching']['full'],
            'moves': '', 'budget_nodes': 128_000_000})
        self.assertIn('searching 1. a3', body)
        self.assertIn('build 42', body)

        old = workers['alice-old-atomicdb']
        self.assertFalse(old['online'])
        self.assertEqual(old['nodes_24h'], 0)
        self.assertEqual(old['nodes_all'], 2_000_000)
        self.assertEqual(old['nps'], 0)
        self.assertIsNone(old['searching'])
        self.assertIsNone(old['build'])
        self.assertIn('offline', body)
        self.assertIn('seen 3 days ago', body)

    def test_a_shared_machine_name_never_shows_another_accounts_search(self):
        """Two accounts announce the same machine name; the leased task
        records only the name.  Each worker shows the search its own
        heartbeat names, and a disputed name without one shows none."""
        _ping('shared-atomicdb', 'alice')
        bob_ping = _ping('shared-atomicdb', 'bob')
        bobs = _task(_child('e2e3'), state=AnalysisTask.TState.LEASED,
                     machine='shared-atomicdb', leased_at=timezone.now(),
                     lease_heartbeat_at=timezone.now())

        def searching(account):
            rows = self._post(username=account, password='p').json()
            return {w['machine']: w['searching'] for w in rows['workers']}

        # No heartbeat on either side: the name is disputed, nobody gets it.
        self.assertIsNone(searching('alice')['shared-atomicdb'])
        self.assertIsNone(searching('bob')['shared-atomicdb'])

        WorkerPing.objects.filter(pk=bob_ping.pk).update(
            current_task_id=bobs.id)
        self.assertIsNone(searching('alice')['shared-atomicdb'])
        self.assertEqual(searching('bob')['shared-atomicdb']['task_id'],
                         bobs.id)
        # The page says the same as the API.
        page = {w['machine']: w for w in contributors.present('alice')['workers']}
        self.assertNotIn('searching', page['shared-atomicdb'])
        alice_body = _reading(
            Client().get('/atomicdb/user/alice/').content.decode())
        self.assertNotIn('searching 1. e3', alice_body)
        bob_body = _reading(
            Client().get('/atomicdb/user/bob/').content.decode())
        self.assertIn('searching 1. e3', bob_body)
        # An unshared name keeps working without a heartbeat (old builds).
        self.assertEqual(
            searching('alice')['alice-box-atomicdb']['task_id'],
            self.running.id)

    def test_the_json_carries_raw_text_not_html(self):
        from unittest import mock

        line = '1. a3 <b>&amp; "x" \'y\''
        real = contributors._labels

        def labels(keys, opening=True):
            found = real(keys, opening=opening)
            return {key: ('<&>', line, '') for key in found or keys}

        with mock.patch.object(contributors, '_labels', labels):
            response = self._post()

        raw = response.content.decode()
        box = {w['machine']: w for w in response.json()['workers']}[
            'alice-box-atomicdb']
        self.assertEqual(box['searching']['line'], line)
        self.assertEqual(box['searching']['position'],
                         self.running.position_id)
        self.assertNotIn('&lt;', raw)
        self.assertNotIn('&#x27;', raw)
        self.assertNotIn('&quot;', raw)

    def test_nginx_rate_limits_both_spellings(self):
        import re
        from pathlib import Path

        conf = (Path(__file__).resolve().parent.parent / 'docs' / 'server'
                / 'nginx-openbench.conf').read_text(encoding='utf-8')
        match = re.search(r'location ~ (\^/atomicdb/api/\S+) \{\s*\n'
                          r'\s*limit_req zone=atomicdb_api', conf)
        limited = re.compile(match.group(1))

        for path in ('/atomicdb/api/my-workers', '/atomicdb/api/my-workers/'):
            self.assertTrue(limited.search(path), path)

    def test_the_reads_do_not_grow_with_the_machines(self):
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        self._post()  # warm the shared fleet snapshot
        db = AnalysisTask.objects.all().db
        with CaptureQueriesContext(connections[db]) as few:
            self._post()
        for n in range(8):
            _ping(f'alice-extra{n}-atomicdb', 'alice')
        with CaptureQueriesContext(connections[db]) as many:
            payload = self._post().json()

        self.assertEqual(len(payload['workers']), 10)
        self.assertEqual(len(many.captured_queries),
                         len(few.captured_queries))
