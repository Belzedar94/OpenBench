"""``POST /atomicdb/api/request-children``: the explorer's "analyse all
unanalysed" button for scripts.

What is defended here:

* it requests every legal move whose position nobody has searched yet, one
  ply and never below;
* moves already searched, closed or already queued are skipped and the reply
  says why;
* every move takes the path of ``api/request``: same identity rules, budget
  ladder, joining and per-account cap, which stops the batch part way;
* a bad FEN, a bad budget or a bad method is refused before anything is
  touched.
"""

from unittest import mock

from django.core.cache import cache
from django.test import Client

from . import ingest, logic
from .models import AnalysisTask, ApiRequestLog, Edge, Position
from .testing import TestCase, worker_account

URL = '/atomicdb/api/request-children'


class RequestChildrenApiTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = Client()
        worker_account('wolfram', 'p')
        worker_account('eclipsia', 'p')
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.legal = list(logic.legal_moves(self.root.fen))

    def _post(self, fen=None, ip='10.0.0.1', url=URL, **fields):
        return self.client.post(url, {'fen': fen or self.root.fen, **fields},
                                REMOTE_ADDR=ip)

    def _credentials(self, username='wolfram'):
        return {'username': username, 'password': 'p'}

    def _child(self, uci):
        return Position.objects.get(
            key=logic.key_of(logic.apply_move(self.root.fen, uci)))

    def test_every_unanalysed_move_is_queued_in_one_call(self):
        response = self._post(**self._credentials())
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload['status'], 'done')
        self.assertEqual(payload['key'], self.root.key)
        self.assertFalse(payload['seeded'])
        self.assertEqual(sorted(entry['move'] for entry in payload['queued']),
                         sorted(self.legal))
        self.assertEqual(payload['skipped'], [])
        self.assertEqual(payload['counts'], {'queued': len(self.legal)})
        tasks = AnalysisTask.objects.filter(source=AnalysisTask.Source.USER)
        self.assertEqual(tasks.count(), len(self.legal))
        for entry in payload['queued']:
            task = AnalysisTask.objects.get(id=entry['task'])
            self.assertEqual(task.position.key, entry['key'])
            self.assertEqual(task.requested_by, 'wolfram')
            # The rung a click on that move's own page buys.
            self.assertEqual(task.budget_nodes,
                             ingest.REQUEST_BUDGET_LADDER[0])
            self.assertEqual(entry['budget_nodes'], task.budget_nodes)

    def test_one_ply_only(self):
        self._post(**self._credentials())

        children = set(Edge.objects.filter(parent=self.root)
                       .values_list('child_id', flat=True))
        self.assertFalse(AnalysisTask.objects.exclude(
            position_id__in=children).exists())

    def test_the_trailing_slash_is_the_same_endpoint(self):
        payload = self._post(url=URL + '/', **self._credentials()).json()

        self.assertEqual(payload['status'], 'done')
        self.assertEqual(len(payload['queued']), len(self.legal))

    def test_searched_closed_and_queued_moves_are_skipped_with_a_reason(self):
        Position.objects.filter(key=self._child('e2e4').key).update(
            nodes_invested=128_000_000)
        Position.objects.filter(key=self._child('d2d4').key).update(
            status='WHITE_WIN', closure='MINIMAX')
        self.client.post('/atomicdb/api/request',
                         {'fen': self._child('g1f3').fen,
                          **self._credentials('eclipsia')},
                         REMOTE_ADDR='10.0.0.2')

        payload = self._post(**self._credentials()).json()

        skipped = {entry['move']: entry for entry in payload['skipped']}
        self.assertEqual(skipped['e2e4']['reason'], 'already-analyzed')
        self.assertEqual(skipped['d2d4']['reason'], 'closed')
        self.assertEqual(skipped['g1f3']['reason'], 'already-queued')
        # Joined, as a second click from another account joins.
        self.assertTrue(skipped['g1f3']['backed'])
        task = AnalysisTask.objects.get(position=self._child('g1f3'),
                                        source=AnalysisTask.Source.USER)
        self.assertEqual(task.requested_by, 'eclipsia')
        self.assertIn('wolfram', task.also_requested_by)
        self.assertEqual(len(payload['queued']), len(self.legal) - 3)
        self.assertNotIn('e2e4', [e['move'] for e in payload['queued']])
        self.assertFalse(AnalysisTask.objects.filter(
            position=self._child('e2e4')).exists())

    def test_asking_twice_queues_nothing_new(self):
        self._post(**self._credentials())

        payload = self._post(**self._credentials()).json()

        self.assertEqual(payload['queued'], [])
        self.assertEqual({entry['reason'] for entry in payload['skipped']},
                         {'already-requested'})
        self.assertEqual(
            AnalysisTask.objects.filter(source='USER').count(),
            len(self.legal))

    def test_nothing_left_says_so(self):
        Position.objects.filter(
            key__in=Edge.objects.filter(parent=self.root)
            .values_list('child_id', flat=True)).update(nodes_invested=1)

        payload = self._post(**self._credentials()).json()

        self.assertEqual(payload['status'], 'nothing-to-do')
        self.assertEqual(payload['queued'], [])
        self.assertEqual(payload['counts'],
                         {'queued': 0, 'already-analyzed': len(self.legal)})

    @mock.patch('atomicdb.views.REQUEST_QUEUE_MAX', 5)
    def test_the_account_cap_stops_the_batch_part_way(self):
        response = self._post(**self._credentials())
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload['status'], 'partial')
        self.assertEqual(len(payload['queued']), 5)
        self.assertEqual(payload['counts']['queue-full-account'],
                         len(self.legal) - 5)
        self.assertEqual(
            AnalysisTask.objects.filter(requested_by='wolfram').count(), 5)

    @mock.patch('atomicdb.views.CHILDREN_REQUEST_CAP', 5)
    def test_past_the_batch_cap_a_second_call_reaches_the_rest(self):
        first = self._post(**self._credentials()).json()

        self.assertEqual(len(first['queued']), 5)
        self.assertEqual(first['counts']['batch-cap'], len(self.legal) - 5)

        second = self._post(**self._credentials()).json()

        self.assertEqual(len(second['queued']), 5)
        self.assertFalse({e['move'] for e in first['queued']}
                         & {e['move'] for e in second['queued']})
        self.assertEqual(second['counts']['already-requested'], 5)

    def test_the_api_docs_describe_it(self):
        body = Client().get('/atomicdb/docs/api/').content.decode()

        self.assertIn('/atomicdb/api/request-children', body)
        self.assertIn('At most 64 moves are requested per call', body)

    @mock.patch('atomicdb.views.REQUEST_QUEUE_MAX', 0)
    def test_a_full_queue_refuses_like_api_request(self):
        response = self._post(**self._credentials())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'queue-full-account')
        self.assertIn('clear your queue', response.json()['reason'])
        self.assertFalse(AnalysisTask.objects.exists())

    def test_an_unreadable_fen_is_refused(self):
        response = self._post(fen='lol nope')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'refused')
        self.assertIn('reason', response.json())
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_budget_outside_the_ladder_is_refused(self):
        response = self._post(budget='999', **self._credentials())

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AnalysisTask.objects.exists())

    def test_the_searches_below_one_position_are_refused(self):
        response = self._post(budget=str(ingest.SUBTREE_REQUEST_NODES),
                              **self._credentials())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'refused')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_without_credentials_it_is_served_as_anonymous(self):
        payload = self._post().json()

        self.assertEqual(payload['status'], 'done')
        self.assertEqual(
            set(AnalysisTask.objects.values_list('requested_by', flat=True)),
            {''})

    def test_wrong_credentials_are_refused(self):
        response = self._post(username='wolfram', password='wrong')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'refused')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_browser_session_does_not_authenticate_this_endpoint(self):
        self.client.login(username='wolfram', password='p')
        payload = self._post().json()
        self.client.logout()

        self.assertEqual(payload['status'], 'done')
        self.assertEqual(
            set(AnalysisTask.objects.values_list('requested_by', flat=True)),
            {''})

    def test_a_get_queues_nothing(self):
        response = self.client.get(URL)

        self.assertEqual(response.status_code, 405)
        self.assertFalse(AnalysisTask.objects.exists())

    def test_each_call_leaves_one_receipt(self):
        self._post(**self._credentials())

        self.assertEqual(ApiRequestLog.objects.filter(
            account='wolfram').count(), 1)

    def test_a_decided_position_buys_nothing(self):
        Position.objects.filter(key=self.root.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        payload = self._post(**self._credentials()).json()

        self.assertEqual(payload['status'], 'already-solved')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_moves_that_are_not_nodes_yet_are_made_and_queued(self):
        target = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))
        self.assertFalse(Edge.objects.filter(parent=target).exists())
        legal = list(logic.legal_moves(target.fen))

        payload = self._post(fen=target.fen, moves='e2e4',
                             **self._credentials()).json()

        self.assertEqual(payload['status'], 'done')
        self.assertEqual(payload['moves'], 'e2e4')
        self.assertEqual(len(payload['queued']), len(legal))
        self.assertEqual(Edge.objects.filter(parent=target).count(),
                         len(legal))
        # Each request is shown under the caller's line plus its move.
        for entry in payload['queued']:
            self.assertEqual(AnalysisTask.objects.get(id=entry['task']).route,
                             'e2e4,' + entry['move'])

    def test_route_is_accepted_as_another_name_for_moves(self):
        target = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))

        payload = self._post(fen=target.fen, route='e2e4',
                             **self._credentials()).json()

        self.assertEqual(payload['moves'], 'e2e4')

    def _e4(self):
        return ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))

    def test_a_line_sent_on_a_repeated_call_labels_the_requests(self):
        target = self._e4()
        self._post(fen=target.fen, **self._credentials())
        self.assertEqual(set(AnalysisTask.objects.values_list(
            'route', flat=True)), {''})

        payload = self._post(fen=target.fen, moves='e2e4',
                             **self._credentials()).json()

        self.assertEqual({e['reason'] for e in payload['skipped']},
                         {'already-requested'})
        for entry in payload['skipped']:
            self.assertEqual(entry['moves'], 'e2e4,' + entry['move'])
        for task in AnalysisTask.objects.select_related('position'):
            uci = next(e['move'] for e in payload['skipped']
                       if e['key'] == task.position.key)
            self.assertEqual(task.route, 'e2e4,' + uci)

    def test_joining_a_request_already_on_an_engine_labels_it(self):
        target = self._e4()
        self._post(fen=target.fen, **self._credentials('eclipsia'))
        AnalysisTask.objects.update(state=AnalysisTask.TState.LEASED)

        payload = self._post(fen=target.fen, ip='10.0.0.2', moves='e2e4',
                             **self._credentials()).json()

        self.assertEqual({e['reason'] for e in payload['skipped']},
                         {'already-queued'})
        for task in AnalysisTask.objects.select_related('position'):
            self.assertEqual(task.state, AnalysisTask.TState.LEASED)
            self.assertTrue(task.route.startswith('e2e4,'))
            self.assertEqual(logic.key_of(logic.apply_move(
                target.fen, task.route.split(',')[1])), task.position.key)

    def test_a_line_already_on_a_request_stays(self):
        target = self._e4()
        child = ingest.get_or_create_position(
            logic.apply_move(target.fen, 'e7e5'))
        AnalysisTask.objects.create(
            position=child, generation=child.visits,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            source=AnalysisTask.Source.USER, requested_by='eclipsia',
            route='e2e4,e7e5,x', state=AnalysisTask.TState.LEASED)

        payload = self._post(fen=target.fen, ip='10.0.0.2', moves='e2e4',
                             **self._credentials()).json()

        entry = next(e for e in payload['skipped'] if e['move'] == 'e7e5')
        self.assertEqual(entry['moves_ignored'],
                         'this request already shows another line')
        self.assertEqual(AnalysisTask.objects.get(position=child).route,
                         'e2e4,e7e5,x')

    def test_a_search_landing_during_the_batch_is_not_deepened(self):
        """The first move's first search lands after the batch picked it.

        The purchase asks again under its row lock and skips the move, rather
        than buying the next rung of the ladder on top of what just landed.
        """
        from . import views
        real = views._queue_request
        raced = self._child(self.legal[0])
        finished = AnalysisTask.objects.create(
            position=raced, generation=raced.visits,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            source=AnalysisTask.Source.USER, requested_by='eclipsia',
            state=AnalysisTask.TState.PENDING)

        def landing_first(pos, *args, **kwargs):
            if pos.key == raced.key:
                AnalysisTask.objects.filter(pk=finished.pk).update(
                    state=AnalysisTask.TState.COMPLETED)
                Position.objects.filter(key=raced.key).update(
                    nodes_invested=ingest.REQUEST_BUDGET_LADDER[0],
                    visits=raced.visits + 1)
            return real(pos, *args, **kwargs)

        with mock.patch.object(views, '_queue_request',
                               side_effect=landing_first):
            payload = self._post(**self._credentials()).json()

        entries = {e['move']: e
                   for e in payload['queued'] + payload['skipped']}
        self.assertEqual(entries[self.legal[0]].get('reason'),
                         'already-analyzed')
        self.assertFalse(AnalysisTask.objects.filter(position=raced).exclude(
            pk=finished.pk).exists())
        self.assertEqual(len(payload['queued']), len(self.legal) - 1)

    def test_nginx_rate_limits_both_spellings(self):
        import re
        from pathlib import Path

        conf = (Path(__file__).resolve().parent.parent / 'docs' / 'server'
                / 'nginx-openbench.conf').read_text(encoding='utf-8')
        match = re.search(r'location ~ (\^/atomicdb/api/\S+) \{\s*\n'
                          r'\s*limit_req zone=atomicdb_api', conf)
        self.assertIsNotNone(match)
        limited = re.compile(match.group(1))

        for path in ('/atomicdb/api/request-children',
                     '/atomicdb/api/request-children/',
                     '/atomicdb/api/request'):
            self.assertTrue(limited.search(path), path)
        for path in ('/atomicdb/api/lease', '/atomicdb/api/submit'):
            self.assertFalse(limited.search(path), path)

    def test_a_fen_the_tree_does_not_have_is_seeded(self):
        fresh = logic.apply_move(
            logic.apply_move(self.root.fen, 'g1f3'), 'g8f6')

        payload = self._post(fen=fresh, **self._credentials()).json()

        self.assertTrue(payload['seeded'])
        self.assertEqual(payload['status'], 'done')
        self.assertEqual(len(payload['queued']),
                         len(logic.legal_moves(fresh)))
