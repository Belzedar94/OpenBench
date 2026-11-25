"""Within one account, requests from the website go before requests a script
queued through the API (jean_heibig, torom and Eclipsia, 25-Nov-2026).

What is defended here:

* the script doors (``api/request``, ``api/request-children``) queue
  background rows by default; the page doors queue normal ones;
  ``priority=normal`` lifts a script call to the page level, and anything
  but ``low``/``normal`` is refused before anything is touched;
* inside the account the normal rows are served first, whatever their age;
* the account's share against the others is unchanged, and the others'
  rows do not move;
* a normal request on a background row lifts it, also when the same address
  is answered ``already-requested``; a background request never lowers a
  normal row;
* existing rows (the column default) are normal.
"""

from django.test import Client

from . import contributors, ingest, live_request, logic
from .models import AnalysisTask, Edge, Position
from .testing import TestCase, worker_account

API = '/atomicdb/api/request'
CHILDREN = '/atomicdb/api/request-children'
RUNG = 128_000_000


class _Harness(TestCase):

    def setUp(self):
        worker_account('w', 'p')
        worker_account('jean', 'p')
        worker_account('other', 'p')
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.children = [edge.child for edge in
                         Edge.objects.filter(parent=self.root)
                         .order_by('move_uci')]

    def _api(self, pos, ip='10.0.0.1', **fields):
        return self.client.post(API, {'fen': pos.fen, 'username': 'jean',
                                      'password': 'p', **fields},
                                REMOTE_ADDR=ip)

    def _click(self, pos, ip='10.0.0.9'):
        browser = Client()
        browser.login(username='jean', password='p')
        return browser.post(f'/atomicdb/request/{pos.key}/', REMOTE_ADDR=ip)

    def _row(self, pos):
        return AnalysisTask.objects.get(position=pos,
                                        source=AnalysisTask.Source.USER)

    def _positions(self, count, offset):
        rows = [Position(key=f'{index + offset:064d}', fen=logic.start_fen(),
                         status='UNKNOWN', expanded=False)
                for index in range(count)]
        Position.objects.bulk_create(rows)
        return rows

    def _queue(self, owner, flags, offset):
        """One 128M request of ``owner`` per flag (True = background)."""
        return AnalysisTask.objects.bulk_create([
            AnalysisTask(position=position, generation=0, budget_nodes=RUNG,
                         source=AnalysisTask.Source.USER, requested_by=owner,
                         state='PENDING', background=flag)
            for position, flag in zip(self._positions(len(flags), offset),
                                      flags)])

    def _serve(self, count):
        served = []
        for index in range(count):
            tasks = self.client.post('/atomicdb/api/lease', {
                'username': 'w', 'password': 'p', 'machine': f'm{index}',
                'worker_build': '2026072203', 'lease_session': f'm{index}',
            }).json()['tasks']
            if not tasks:
                break
            served.append(tasks[0]['id'])
        return served


class DoorTests(_Harness):

    def test_an_api_request_is_background_by_default(self):
        body = self._api(self.children[0]).json()

        self.assertEqual(body['status'], 'queued')
        self.assertEqual(body['priority'], 'low')
        self.assertTrue(self._row(self.children[0]).background)

    def test_a_page_click_is_normal(self):
        self.assertEqual(self._click(self.children[0]).json()['status'],
                         'queued')

        self.assertFalse(self._row(self.children[0]).background)

    def test_priority_normal_puts_an_api_request_at_the_click_level(self):
        body = self._api(self.children[0], priority='normal').json()

        self.assertEqual(body['priority'], 'normal')
        self.assertFalse(self._row(self.children[0]).background)

    def test_an_unknown_priority_is_refused_before_anything_happens(self):
        response = self._api(self.children[0], priority='urgent')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'refused')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_request_children_is_background_by_default(self):
        body = self.client.post(CHILDREN, {
            'fen': self.root.fen, 'username': 'jean', 'password': 'p',
            'select': 'g1f3,b1c3'}).json()

        self.assertEqual({entry['priority'] for entry in body['queued']},
                         {'low'})
        self.assertEqual(set(AnalysisTask.objects.values_list(
            'background', flat=True)), {True})

    def test_request_children_takes_priority_normal(self):
        self.client.post(CHILDREN, {
            'fen': self.root.fen, 'username': 'jean', 'password': 'p',
            'select': 'g1f3', 'priority': 'normal'})

        self.assertEqual(set(AnalysisTask.objects.values_list(
            'background', flat=True)), {False})

    def test_an_existing_row_is_normal(self):
        task = AnalysisTask.objects.create(
            position=self.children[0], generation=0, budget_nodes=RUNG,
            source=AnalysisTask.Source.USER, requested_by='jean')

        self.assertFalse(task.background)


class OrderTests(_Harness):

    def test_the_accounts_clicks_go_before_its_older_api_requests(self):
        low = self._queue('jean', [True, True], offset=0)
        normal = self._queue('jean', [False, False], offset=10)

        self.assertEqual(self._serve(4),
                         [normal[0].id, normal[1].id, low[0].id, low[1].id])

    def test_end_to_end_a_click_after_a_crawl_is_served_first(self):
        self._api(self.children[0])
        self._api(self.children[1])
        self._click(self.children[2])

        served = self._serve(1)

        self.assertEqual(served, [self._row(self.children[2]).id])

    def test_other_accounts_keep_their_turns(self):
        jean = self._queue('jean', [True, True, False], offset=0)
        other = self._queue('other', [False, False], offset=10)
        flagged = {task.id: live_request.queue_ahead(task) for task in other}
        AnalysisTask.objects.update(background=False)
        unflagged = {task.id: live_request.queue_ahead(task) for task in other}
        AnalysisTask.objects.filter(
            id__in=[jean[0].id, jean[1].id]).update(background=True)

        served = self._serve(5)

        # Nobody else's place moves: the account's share is the same.
        self.assertEqual(flagged, unflagged)
        owners = dict(AnalysisTask.objects.values_list('id', 'requested_by'))
        self.assertEqual([owners[task_id] for task_id in served],
                         ['jean', 'other', 'jean', 'other', 'jean'])
        # Only which of jean's requests takes each of its turns changes.
        self.assertEqual([task_id for task_id in served
                          if owners[task_id] == 'jean'],
                         [jean[2].id, jean[0].id, jean[1].id])
        self.assertEqual([task_id for task_id in served
                          if owners[task_id] == 'other'],
                         [other[0].id, other[1].id])

    def test_the_estimator_agrees_with_the_order_served(self):
        self._queue('jean', [True, False, True, False], offset=0)
        self._queue('other', [False, True, False], offset=10)
        pending = list(AnalysisTask.objects.filter(state='PENDING'))
        estimated = {task.id: live_request.queue_ahead(task)
                     for task in pending}

        served = self._serve(len(pending))

        self.assertEqual([estimated[task_id] for task_id in served],
                         list(range(len(served))))

    def test_moving_to_the_front_stays_inside_the_level(self):
        low = self._queue('jean', [True, True], offset=0)
        normal, = self._queue('jean', [False], offset=10)

        # The normal one is already first of its own level.
        self.assertIsNone(ingest.front_of_own_queue(normal))
        self.assertIsNotNone(ingest.front_of_own_queue(low[1]))

    def test_the_queue_page_lists_them_in_the_order_served(self):
        low = self._queue('jean', [True], offset=0)
        normal = self._queue('jean', [False], offset=10)

        pending, _leased, _done = contributors._queue_rows('jean')

        self.assertEqual([task.id for task in pending],
                         [normal[0].id, low[0].id])
        page = self.client.get('/atomicdb/queue/jean/')
        self.assertContains(page, '>background</span>', count=1)


class UpgradeTests(_Harness):

    def test_a_click_from_another_address_lifts_the_api_row(self):
        self._api(self.children[0], ip='10.0.0.1')

        body = self._click(self.children[0], ip='10.0.0.2').json()

        self.assertEqual(body['status'], 'already-queued')
        self.assertFalse(self._row(self.children[0]).background)

    def test_a_click_from_the_same_address_lifts_it_too(self):
        """The dedup answers ``already-requested`` before the purchase."""
        self._api(self.children[0], ip='10.0.0.1')

        body = self._click(self.children[0], ip='10.0.0.1').json()

        self.assertEqual(body['status'], 'already-requested')
        self.assertFalse(self._row(self.children[0]).background)

    def test_priority_normal_on_the_api_lifts_it(self):
        self._api(self.children[0], ip='10.0.0.1')

        body = self._api(self.children[0], ip='10.0.0.3',
                         priority='normal').json()

        self.assertEqual(body['priority'], 'normal')
        self.assertFalse(self._row(self.children[0]).background)

    def test_a_background_request_never_lowers_a_normal_row(self):
        self._click(self.children[0], ip='10.0.0.2')

        body = self._api(self.children[0], ip='10.0.0.1').json()

        self.assertEqual(body['status'], 'already-queued')
        self.assertEqual(body['priority'], 'normal')
        self.assertFalse(self._row(self.children[0]).background)

    def test_a_withdrawn_api_row_comes_back_at_the_level_asked_now(self):
        self._api(self.children[0], ip='10.0.0.1')
        AnalysisTask.objects.update(state=AnalysisTask.TState.CANCELLED)

        self._click(self.children[0], ip='10.0.0.2')

        task = self._row(self.children[0])
        self.assertEqual(task.state, AnalysisTask.TState.PENDING)
        self.assertFalse(task.background)

    def test_a_server_row_promoted_by_the_api_becomes_background(self):
        AnalysisTask.objects.create(
            position=self.children[0], generation=0, budget_nodes=RUNG,
            source=AnalysisTask.Source.AUTO)

        self._api(self.children[0])

        task = AnalysisTask.objects.get(position=self.children[0])
        self.assertEqual(task.source, AnalysisTask.Source.USER)
        self.assertTrue(task.background)
