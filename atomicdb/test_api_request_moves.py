"""``moves`` on ``POST /atomicdb/api/request``: the caller's move order.

Community request (MoKy, 14-Nov-2026): a bot that sends only a FEN finds its
request on the queue pages under the canonical path of the tree, another move
order, and cannot recognise its own opening.  ``moves`` carries the line from
the start position and the queue rows and their explorer links show it.

What is defended here:

* it is a LABEL: the request buys the same task, budget and status with it,
  without it, and with a line that is left out;
* a line is used only when it is legal, ends at the requested position and
  only goes through positions the tree has; otherwise the request goes ahead
  and the reply says why (``moves_ignored``);
* a reply to a call without ``moves`` keeps exactly the fields it had;
* the first line given to a request stays.
"""

from django.core.cache import cache
from django.test import Client

from . import ingest, logic
from .models import AnalysisTask
from .testing import TestCase, worker_account

PLAYED = ['g1f3', 'f7f6', 'b1c3']
CANONICAL = ['b1c3', 'f7f6', 'g1f3']


def _fen_after(ucis):
    fen = logic.start_fen()
    for uci in ucis:
        fen = logic.apply_move(fen, uci)
    return fen


class ApiRequestMovesTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = Client()
        worker_account('moky', 'p')
        worker_account('torom', 'p')
        # The tree's own path to the target is 1.Nc3 f6 2.Nf3: those edges
        # exist, so that is the line every page shows by default.
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        node = self.root
        for uci in CANONICAL[:-1]:
            node = ingest.get_or_create_position(
                logic.apply_move(node.fen, uci))
            ingest.expand(node)
        self.target = ingest.get_or_create_position(_fen_after(CANONICAL))
        # The bot played 1.Nf3 f6 2.Nc3.  The position after 1.Nf3 f6 is in
        # the tree, though not on the canonical path.
        ingest.get_or_create_position(_fen_after(PLAYED[:2]))
        self.assertEqual(logic.key_of(_fen_after(PLAYED)), self.target.key)

    def _post(self, account='moky', ip='10.0.0.1', **fields):
        body = {'fen': self.target.fen, 'username': account, 'password': 'p'}
        body.update(fields)
        return self.client.post('/atomicdb/api/request', body,
                                REMOTE_ADDR=ip).json()

    def _task(self):
        return AnalysisTask.objects.get(position=self.target)

    def test_an_accepted_line_labels_the_request(self):
        payload = self._post(moves=','.join(PLAYED))

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['moves'], 'g1f3,f7f6,b1c3')
        self.assertNotIn('moves_ignored', payload)
        self.assertEqual(self._task().route, 'g1f3,f7f6,b1c3')

    def test_the_queue_page_and_its_explorer_link_show_that_line(self):
        self._post(moves=','.join(PLAYED))

        body = Client().get('/atomicdb/queue/moky/').content.decode()

        self.assertIn(f'/atomicdb/explore/{self.target.key}/'
                      '?play=g1f3%2Cf7f6%2Cb1c3', body)
        # SAN of the played order, not of the canonical one.
        self.assertIn('>1. Nf3 f6 2. Nc3<', body)

    def test_my_queue_says_which_line_each_request_shows(self):
        self._post(moves=','.join(PLAYED))

        rows = self.client.post('/atomicdb/api/my-queue/', {
            'username': 'moky', 'password': 'p'}).json()['tasks']

        self.assertEqual([row['moves'] for row in rows], ['g1f3,f7f6,b1c3'])

    def test_spaces_and_capitals_are_read_as_the_same_line(self):
        payload = self._post(moves='  G1F3 f7f6\tb1c3 ')

        self.assertEqual(payload['moves'], 'g1f3,f7f6,b1c3')
        self.assertEqual(self._task().route, 'g1f3,f7f6,b1c3')

    def test_an_illegal_line_is_ignored_with_its_reason(self):
        payload = self._post(moves='g1f3,f7f5,e1e3')

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['moves_ignored'],
                         'ply 3 (e1e3) is not a legal move in that position')
        self.assertNotIn('moves', payload)
        self.assertEqual(self._task().route, '')

    def test_a_token_that_is_not_uci_is_ignored_with_its_reason(self):
        payload = self._post(moves='Nf3,f6,Nc3')

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['moves_ignored'],
                         'ply 1 (nf3) is not a UCI move')

    def test_a_line_that_ends_elsewhere_is_ignored(self):
        payload = self._post(moves='g1f3,f7f6')

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['moves_ignored'],
                         'the line does not end at the requested position')
        self.assertEqual(self._task().route, '')

    def test_a_legal_line_to_the_target_through_unknown_positions(self):
        # 1.Nc3 Nh6 2.Nf3 Ng8 3.Nb1 f6 4.Nc3 reaches the target, but the
        # positions after 1.Nc3 Nh6 and later are not in the tree.
        payload = self._post(moves='b1c3,g8h6,g1f3,h6g8,c3b1,f7f6,b1c3')

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['moves_ignored'],
                         'the line goes through positions AtomicDB does not '
                         'have yet')
        self.assertEqual(self._task().route, '')

    def test_a_line_over_the_cap_is_ignored(self):
        payload = self._post(moves=','.join(['g1f3', 'g8f6', 'f3g1',
                                             'f6g8'] * 17))

        self.assertEqual(payload['moves_ignored'],
                         'the line is longer than 64 plies')

    def test_without_moves_the_reply_and_the_request_are_unchanged(self):
        payload = self._post()

        self.assertEqual(payload['status'], 'queued')
        self.assertNotIn('moves', payload)
        self.assertNotIn('moves_ignored', payload)
        self.assertLessEqual(set(payload), {
            'status', 'key', 'fen', 'seeded', 'task', 'budget_nodes',
            'task_state', 'backed', 'ahead'})
        self.assertEqual(self._task().route, '')
        # And the queue keeps showing the tree's own path.
        body = Client().get('/atomicdb/queue/moky/').content.decode()
        self.assertNotIn('?play=', body)
        self.assertIn('>1. Nc3 f6 2. Nf3<', body)

    def test_the_line_changes_nothing_but_the_label(self):
        with_line = self._post(moves=','.join(PLAYED))
        task = self._task()
        labelled = (task.budget_nodes, task.source, task.requested_by,
                    task.generation, task.queue_seq, task.multipv)
        task.delete()
        cache.clear()
        without = self._post(ip='10.0.0.2')
        task = self._task()

        self.assertEqual(with_line['status'], without['status'])
        self.assertEqual(with_line['budget_nodes'], without['budget_nodes'])
        self.assertEqual(labelled, (task.budget_nodes, task.source,
                                    task.requested_by, task.generation,
                                    task.queue_seq, task.multipv))

    def test_the_first_line_stays(self):
        self._post(moves=','.join(PLAYED))

        payload = self._post(account='torom', ip='10.0.0.2',
                             moves=','.join(CANONICAL))

        self.assertEqual(payload['status'], 'already-queued')
        self.assertTrue(payload['backed'])
        self.assertEqual(payload['moves_ignored'],
                         'this request already shows another line')
        self.assertEqual(self._task().route, 'g1f3,f7f6,b1c3')

    def test_a_request_without_a_line_takes_the_first_one_sent(self):
        self._post()

        payload = self._post(moves=','.join(PLAYED))

        self.assertEqual(payload['status'], 'already-requested')
        self.assertEqual(payload['moves'], 'g1f3,f7f6,b1c3')
        self.assertEqual(self._task().route, 'g1f3,f7f6,b1c3')

    def test_a_request_already_on_an_engine_takes_a_line_too(self):
        self._post()
        AnalysisTask.objects.filter(position=self.target).update(
            state=AnalysisTask.TState.LEASED)

        payload = self._post(account='torom', ip='10.0.0.2',
                             moves=','.join(PLAYED))

        self.assertEqual(payload['task_state'], 'searching')
        self.assertEqual(payload['moves'], 'g1f3,f7f6,b1c3')
        task = self._task()
        self.assertEqual(task.route, 'g1f3,f7f6,b1c3')
        self.assertEqual(task.state, AnalysisTask.TState.LEASED)
