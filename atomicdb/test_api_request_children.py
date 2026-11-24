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
            self.assertEqual(entry['task_state'], 'waiting')

    def test_an_already_queued_move_says_how_deep_and_whether_it_runs(self):
        """torom, 24-Nov: ``task_state`` on every entry with a task, and
        ``budget_nodes`` on ``already-queued`` too, as ``api/request``
        answers for one position."""
        self.client.post('/atomicdb/api/request',
                         {'fen': self._child('g1f3').fen,
                          **self._credentials('eclipsia')},
                         REMOTE_ADDR='10.0.0.2')
        task = AnalysisTask.objects.get(position=self._child('g1f3'),
                                        source=AnalysisTask.Source.USER)

        payload = self._post(**self._credentials()).json()
        entry = {e['move']: e for e in payload['skipped']}['g1f3']
        self.assertEqual(entry['reason'], 'already-queued')
        self.assertEqual(entry['task'], task.id)
        self.assertEqual(entry['budget_nodes'], task.budget_nodes)
        self.assertEqual(entry['task_state'], 'waiting')

        AnalysisTask.objects.filter(pk=task.pk).update(
            state=AnalysisTask.TState.LEASED)
        payload = self._post(**self._credentials()).json()
        entry = {e['move']: e for e in payload['skipped']}['g1f3']
        # Second call from the same account: already joined.
        self.assertEqual(entry['reason'], 'already-requested')
        self.assertEqual(entry['task'], task.id)
        self.assertEqual(entry['budget_nodes'], task.budget_nodes)
        self.assertEqual(entry['task_state'], 'searching')

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

    def test_known_transpositions_are_linked_before_they_are_skipped(self):
        target = self._e4()
        searched = ingest.get_or_create_position(
            logic.apply_move(target.fen, 'e7e5'))
        closed = ingest.get_or_create_position(
            logic.apply_move(target.fen, 'd7d5'))
        Position.objects.filter(key=searched.key).update(nodes_invested=1)
        Position.objects.filter(key=closed.key).update(
            status='WHITE_WIN', closure='MINIMAX')
        self.assertFalse(Edge.objects.filter(parent=target).exists())

        with mock.patch.object(ingest, 'backup_cascade',
                               wraps=ingest.backup_cascade) as cascade:
            payload = self._post(fen=target.fen,
                                 **self._credentials()).json()

        skipped = {e['move']: e['reason'] for e in payload['skipped']}
        self.assertEqual(skipped['e7e5'], 'already-analyzed')
        self.assertEqual(skipped['d7d5'], 'closed')
        linked = dict(Edge.objects.filter(parent=target)
                      .values_list('move_uci', 'child_id'))
        self.assertEqual(linked['e7e5'], searched.key)
        self.assertEqual(linked['d7d5'], closed.key)
        # The closure reaches the parent, as following the move does.
        cascade.assert_any_call([target.key])
        self.assertFalse(AnalysisTask.objects.filter(
            position__in=[searched, closed]).exists())

    @mock.patch('atomicdb.depth.may_choose', return_value=True)
    def test_a_deeper_follow_up_behind_a_running_search_is_reported(self, _):
        target = self._e4()
        child = ingest.get_or_create_position(
            logic.apply_move(target.fen, 'e7e5'))
        running = AnalysisTask.objects.create(
            position=child, generation=child.visits,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            source=AnalysisTask.Source.USER, requested_by='eclipsia',
            state=AnalysisTask.TState.LEASED)
        deeper = ingest.REQUEST_BUDGET_LADDER[1]

        payload = self._post(fen=target.fen, moves='e2e4',
                             budget=str(deeper),
                             **self._credentials()).json()

        entry = next(e for e in payload['queued'] if e['move'] == 'e7e5')
        follow_up = AnalysisTask.objects.get(position=child,
                                             state=AnalysisTask.TState.PENDING)
        self.assertNotEqual(entry['task'], running.id)
        self.assertEqual(entry['task'], follow_up.id)
        self.assertEqual(entry['budget_nodes'], deeper)
        self.assertEqual(entry['moves'], 'e2e4,e7e5')
        self.assertEqual(follow_up.route, 'e2e4,e7e5')
        running.refresh_from_db()
        self.assertEqual(running.route, '')

    @mock.patch('atomicdb.views.API_MOVES_MAX_PLIES', 1)
    def test_a_line_at_the_ply_limit_is_ignored_with_its_reason(self):
        # With the limit at one ply, ``e2e4`` is a line ``api/request``
        # accepts, and one more ply for each child would be past it.
        target = self._e4()

        payload = self._post(fen=target.fen, moves='e2e4',
                             **self._credentials()).json()

        self.assertNotIn('moves', payload)
        self.assertEqual(
            payload['moves_ignored'],
            'the line is longer than 0 plies, which leaves no room for the '
            'move of each request')
        self.assertTrue(payload['queued'])
        self.assertEqual(set(AnalysisTask.objects.values_list(
            'route', flat=True)), {''})

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


class RequestSelectedChildrenTests(TestCase):
    """``select``: the moves the caller names, as the move list's
    multi-select requests them (torom, Discord)."""

    def setUp(self):
        cache.clear()
        self.client = Client()
        worker_account('wolfram', 'p')
        worker_account('eclipsia', 'p')
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.e4 = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))

    def _post(self, fen=None, ip='10.0.0.1', **fields):
        return self.client.post(URL, {'fen': fen or self.root.fen,
                                      'username': 'wolfram', 'password': 'p',
                                      **fields}, REMOTE_ADDR=ip)

    def _child(self, uci, parent=None):
        parent = parent or self.root
        return Position.objects.get(
            key=logic.key_of(logic.apply_move(parent.fen, uci)))

    def _searched(self, child, budget):
        """``child`` with one completed search of ``budget``."""
        AnalysisTask.objects.create(
            position=child, generation=child.visits, budget_nodes=budget,
            source=AnalysisTask.Source.USER, requested_by='eclipsia',
            state=AnalysisTask.TState.COMPLETED, nodes_searched=budget)
        Position.objects.filter(key=child.key).update(
            visits=child.visits + 1, nodes_invested=budget)

    def test_exactly_the_named_moves_are_queued_in_the_order_given(self):
        payload = self._post(select='g1f3,e2e4').json()

        self.assertEqual(payload['status'], 'done')
        self.assertEqual([e['move'] for e in payload['queued']],
                         ['g1f3', 'e2e4'])
        self.assertEqual(payload['skipped'], [])
        self.assertEqual(
            set(AnalysisTask.objects.values_list('position_id', flat=True)),
            {self._child('g1f3').key, self._child('e2e4').key})

    def test_commas_spaces_and_repeated_fields_are_the_same_list(self):
        for ip, select in (('10.0.0.1', 'g1f3, E2E4'),
                           ('10.0.0.2', 'g1f3 e2e4'),
                           ('10.0.0.3', ['g1f3', 'e2e4', 'g1f3'])):
            AnalysisTask.objects.all().delete()
            payload = self._post(ip=ip, select=select).json()

            self.assertEqual([e['move'] for e in payload['queued']],
                             ['g1f3', 'e2e4'], select)

    def test_illegal_moves_are_skipped_and_the_rest_go_ahead(self):
        payload = self._post(select='e2e4,e2e5,nonsense').json()

        self.assertEqual([e['move'] for e in payload['queued']], ['e2e4'])
        self.assertEqual(payload['skipped'], [
            {'move': 'e2e5', 'key': None, 'reason': 'illegal'},
            {'move': 'nonsense', 'key': None, 'reason': 'illegal'}])
        self.assertEqual(payload['counts'], {'queued': 1, 'illegal': 2})

    def test_a_list_with_no_legal_move_is_refused(self):
        response = self._post(select='e2e5')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'refused')
        self.assertEqual(response.json()['skipped'][0]['reason'], 'illegal')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_an_empty_list_is_refused_not_read_as_absent(self):
        response = self._post(select=' , ')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AnalysisTask.objects.exists())

    @mock.patch('atomicdb.views.MULTI_REQUEST_CAP', 2)
    def test_more_legal_moves_than_the_cap_are_refused_whole(self):
        response = self._post(select='e2e4,d2d4,g1f3')

        self.assertEqual(response.status_code, 400)
        self.assertIn('more than 2 legal moves', response.json()['reason'])
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_named_move_already_searched_gets_its_next_rung(self):
        child = self._child('e2e4')
        self._searched(child, ingest.REQUEST_BUDGET_LADDER[0])

        named = self._post(select='e2e4').json()

        entry = named['queued'][0]
        self.assertEqual(entry['move'], 'e2e4')
        self.assertEqual(entry['budget_nodes'],
                         ingest.REQUEST_BUDGET_LADDER[1])
        # Without ``select`` the same move is the button's: skipped.
        AnalysisTask.objects.filter(state='PENDING').delete()
        cache.clear()
        unnamed = self._post(ip='10.0.0.2').json()
        reasons = {e['move']: e['reason'] for e in unnamed['skipped']}
        self.assertEqual(reasons['e2e4'], 'already-analyzed')

    def test_a_named_move_at_the_top_of_the_ladder_buys_nothing(self):
        self._searched(self._child('e2e4'), ingest.REQUEST_BUDGET_LADDER[-1])

        payload = self._post(select='e2e4').json()

        self.assertEqual(payload['skipped'][0]['reason'],
                         ingest.LADDER_SPENT)
        self.assertFalse(AnalysisTask.objects.filter(
            state='PENDING').exists())

    @mock.patch('atomicdb.depth.may_choose', return_value=True)
    def test_a_chosen_budget_is_a_target(self, _):
        self._searched(self._child('e2e4'), ingest.REQUEST_BUDGET_LADDER[1])

        payload = self._post(select='e2e4,d2d4',
                             budget=str(ingest.REQUEST_BUDGET_LADDER[1])
                             ).json()

        self.assertEqual(payload['skipped'], [
            {'move': 'e2e4', 'key': self._child('e2e4').key,
             'reason': 'already-analyzed',
             'completed_budget': ingest.REQUEST_BUDGET_LADDER[1]}])
        self.assertEqual(payload['queued'][0]['move'], 'd2d4')
        self.assertEqual(payload['queued'][0]['budget_nodes'],
                         ingest.REQUEST_BUDGET_LADDER[1])

    @mock.patch('atomicdb.depth.may_choose', return_value=True)
    def test_a_shallower_named_move_gets_the_chosen_budget(self, _):
        self._searched(self._child('e2e4'), ingest.REQUEST_BUDGET_LADDER[0])

        payload = self._post(select='e2e4',
                             budget=str(ingest.REQUEST_BUDGET_LADDER[-1])
                             ).json()

        # The chosen budget, not the next rung after 128M.
        self.assertEqual(payload['queued'][0]['budget_nodes'],
                         ingest.REQUEST_BUDGET_LADDER[-1])

    @mock.patch('atomicdb.depth.may_choose', return_value=True)
    def test_a_search_landing_during_the_batch_covers_the_target(self, _):
        from . import views
        real = views._queue_request
        raced = self._child('e2e4')

        def landing_first(pos, *args, **kwargs):
            if pos.key == raced.key:
                self._searched(raced, ingest.REQUEST_BUDGET_LADDER[0])
            return real(pos, *args, **kwargs)

        with mock.patch.object(views, '_queue_request',
                               side_effect=landing_first):
            payload = self._post(select='e2e4',
                                 budget=str(ingest.REQUEST_BUDGET_LADDER[0])
                                 ).json()

        self.assertEqual(payload['skipped'][0]['reason'], 'already-analyzed')
        self.assertFalse(AnalysisTask.objects.filter(
            position=raced, state='PENDING').exists())

    def test_a_closed_named_move_is_skipped(self):
        Position.objects.filter(key=self._child('e2e4').key).update(
            status='WHITE_WIN', closure='MINIMAX')

        payload = self._post(select='e2e4').json()

        self.assertEqual(payload['skipped'][0]['reason'], 'closed')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_named_transposition_is_linked_and_queued(self):
        known = ingest.get_or_create_position(
            logic.apply_move(self.e4.fen, 'e7e5'))
        Position.objects.filter(key=known.key).update(nodes_invested=1)
        self.assertFalse(Edge.objects.filter(parent=self.e4).exists())

        payload = self._post(fen=self.e4.fen, select='e7e5',
                             moves='e2e4').json()

        self.assertEqual(payload['queued'][0]['key'], known.key)
        self.assertTrue(Edge.objects.filter(
            parent=self.e4, move_uci='e7e5', child=known).exists())
        # Labelled with the caller's line plus the move.
        self.assertEqual(payload['queued'][0]['moves'], 'e2e4,e7e5')
        self.assertEqual(AnalysisTask.objects.get(position=known).route,
                         'e2e4,e7e5')

    def test_a_move_back_onto_the_line_is_blocked(self):
        line = ['g1f3', 'g8f6', 'f3g1']
        fen = self.root.fen
        for uci in line:
            fen = logic.apply_move(fen, uci)
            ingest.get_or_create_position(fen)

        payload = self._post(fen=fen, moves=','.join(line),
                             select='f6g8,e7e5').json()

        reasons = {e['move']: e['reason'] for e in payload['skipped']}
        self.assertEqual(reasons, {'f6g8': 'blocked'})
        self.assertEqual([e['move'] for e in payload['queued']], ['e7e5'])

    def test_a_line_sent_again_labels_the_requests_already_made(self):
        self._post(fen=self.e4.fen, select='e7e5')

        payload = self._post(fen=self.e4.fen, select='e7e5',
                             moves='e2e4').json()

        self.assertEqual(payload['skipped'][0]['reason'],
                         'already-requested')
        self.assertEqual(payload['skipped'][0]['moves'], 'e2e4,e7e5')
        self.assertEqual(AnalysisTask.objects.get().route, 'e2e4,e7e5')

    @mock.patch('atomicdb.views.REQUEST_QUEUE_MAX', 1)
    def test_the_account_cap_stops_the_named_batch_part_way(self):
        payload = self._post(select='e2e4,d2d4').json()

        self.assertEqual(payload['status'], 'partial')
        self.assertEqual([e['move'] for e in payload['queued']], ['e2e4'])
        self.assertEqual(payload['skipped'], [
            {'move': 'd2d4', 'key': self._child('d2d4').key,
             'reason': 'queue-full-account'}])

    def test_receipts_and_the_batch_event(self):
        from .models import DBEvent
        self._post(select='e2e4,d2d4')

        self.assertEqual(ApiRequestLog.objects.filter(
            account='wolfram').count(), 1)
        event = DBEvent.objects.filter(kind='BULK_REQUEST').get()
        self.assertEqual(event.payload['via'], 'api-children-select')
        self.assertEqual(event.payload['queued'], 2)
        self.assertEqual(event.payload['candidates'], 2)

    def test_only_if_missing_with_select_buys_no_deeper_search(self):
        # Any account: wolfram may not choose a budget here.
        self._searched(self._child('e2e4'), ingest.REQUEST_BUDGET_LADDER[0])

        payload = self._post(select='e2e4,d2d4', only_if_missing='1').json()

        self.assertEqual(payload['skipped'], [
            {'move': 'e2e4', 'key': self._child('e2e4').key,
             'reason': 'already-analyzed',
             'completed_budget': ingest.REQUEST_BUDGET_LADDER[0]}])
        self.assertEqual([(e['move'], e['budget_nodes'])
                          for e in payload['queued']],
                         [('d2d4', ingest.REQUEST_BUDGET_LADDER[0])])
        self.assertFalse(AnalysisTask.objects.filter(
            position=self._child('e2e4'), state='PENDING').exists())

    def test_only_if_missing_without_select_caps_at_the_first_rung(self):
        # A visit count that would ask the ladder for more than the first
        # rung on its own: with the flag the target holds.
        child = self._child('e2e4')
        Position.objects.filter(key=child.key).update(visits=500)
        with mock.patch.object(ingest, 'budget_for',
                               return_value=ingest.REQUEST_BUDGET_LADDER[1]):
            plain = self._post(ip='10.0.0.2').json()
            AnalysisTask.objects.all().delete()
            cache.clear()
            flagged = self._post(ip='10.0.0.3', only_if_missing='true').json()

        budget = {e['move']: e['budget_nodes'] for e in plain['queued']}
        self.assertEqual(budget['e2e4'], ingest.REQUEST_BUDGET_LADDER[1])
        budget = {e['move']: e['budget_nodes'] for e in flagged['queued']}
        self.assertEqual(set(budget.values()),
                         {ingest.REQUEST_BUDGET_LADDER[0]})

    def test_the_api_docs_describe_select(self):
        body = Client().get('/atomicdb/docs/api/').content.decode()

        self.assertIn('<code>select=e7e5,g8f6</code>', body)
        self.assertRegex(body, r'more than\s+64 legal moves')

    def test_without_credentials_it_is_anonymous_like_the_rest(self):
        payload = self.client.post(URL, {'fen': self.root.fen,
                                         'select': 'e2e4'}).json()

        self.assertEqual(payload['queued'][0]['move'], 'e2e4')
        self.assertEqual(AnalysisTask.objects.get().requested_by, '')
