"""The page behind the "active workers" number of the home page.

What is pinned here: its rows are exactly the workers that number counts; a
row says what its worker is searching and who asked for it (an account, an
anonymous visitor or the server itself) with the rules of "Now analyzing";
nothing private reaches the page; and what it costs does not grow with the
fleet.
"""

import html as html_module
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db import connections
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import active_workers, ingest, logic, metrics, views
from .models import AnalysisTask, Edge, WorkerPing
from .testing import TestCase

USER = AnalysisTask.Source.USER
AUTO = AnalysisTask.Source.AUTO
FILL = AnalysisTask.Source.FILL
PAWNS = ('a2a3', 'b2b3', 'c2c3', 'd2d3', 'e2e3', 'f2f3', 'g2g3', 'h2h3',
         'a2a4', 'b2b4', 'c2c4', 'd2d4')


def _reading(body):
    """What a visitor READS: no tags, whitespace collapsed."""
    text = html_module.unescape(re.sub(r'<[^>]+>', ' ', body))
    return ' '.join(text.split())


def _play(parent, uci):
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


def _child(uci):
    """A child of the start position, with its edge materialised."""
    return _play(ingest.get_or_create_position(logic.start_fen()), uci)


def _ping(machine, user, seen=None, **fields):
    fields.setdefault('threads', 8)
    fields.setdefault('hash_mb', 1024)
    fields.setdefault('os', 'Linux')
    ping = WorkerPing.objects.create(machine=machine, user=user, **fields)
    # ``last_seen`` is auto_now: the only way to place it is an update.
    WorkerPing.objects.filter(pk=ping.pk).update(
        last_seen=seen or timezone.now())
    ping.refresh_from_db()
    return ping


def _lease(position, machine, minutes_ago=3, token='tok', beat_minutes_ago=0,
           **fields):
    """A lease as the protocol leaves it: modern (token) or legacy (none)."""
    now = timezone.now()
    generation = 0
    while AnalysisTask.objects.filter(position=position,
                                      generation=generation).exists():
        generation += 1
    fields.setdefault('budget_nodes', 128_000_000)
    return AnalysisTask.objects.create(
        position=position, generation=generation,
        state=AnalysisTask.TState.LEASED, machine=machine, lease_token=token,
        leased_at=now - timedelta(minutes=minutes_ago),
        lease_heartbeat_at=(now - timedelta(minutes=beat_minutes_ago)
                            if token else None),
        **fields)


def _busy(machine, user, uci, **fields):
    """A modern worker that publishes the task it is searching."""
    task = _lease(_child(uci), machine, **fields)
    _ping(machine, user, current_task_id=task.id)
    return task


def _page():
    return Client().get('/atomicdb/workers/').content.decode()


def _rows():
    return active_workers.present()['workers']


class ActiveCountTests(TestCase):
    """The rows ARE the number on the home page, boundary included."""

    def tearDown(self):
        metrics.reset_metrics_cache()

    def test_the_rows_are_the_workers_the_home_page_counts(self):
        now = timezone.now()
        window = timedelta(seconds=metrics.LIVE_SECONDS)
        _ping('a-box', 'alice', seen=now)
        _ping('b-box', 'bob', seen=now - window + timedelta(seconds=1))
        _ping('edge-box', 'bob', seen=now - window)
        _ping('gone-box', 'carol', seen=now - window - timedelta(seconds=1))

        page = active_workers.present(now)

        self.assertEqual(page['count'],
                         metrics.worker_metrics(now=now)['workers'])
        self.assertEqual([row['machine'] for row in page['workers']],
                         ['a-box', 'b-box', 'edge-box'])

    def test_the_threads_add_up_to_the_active_cores(self):
        now = timezone.now()
        _ping('small', 'alice', seen=now, threads=8)
        _ping('big', 'alice', seen=now, threads=24)
        # Written before the server capped what a worker may claim.
        _ping('boast', 'bob', seen=now, threads=10 ** 9)

        page = active_workers.present(now)

        self.assertEqual(
            page['threads_h'],
            views._human(metrics.worker_metrics(now=now)['cores']))
        self.assertEqual(page['threads_h'], '1,056')

    def test_the_summary_line_counts_workers_accounts_and_searches(self):
        _busy('alice-box', 'alice', 'a2a3')
        _ping('alice-idle', 'alice')
        _ping('bob-box', 'bob')

        body = _reading(_page())

        self.assertIn('3 workers from 2 accounts · 24 threads · 1 searching '
                      'right now', body)

    def test_one_of_everything_reads_in_the_singular(self):
        _ping('tiny-box', 'alice', threads=1)

        self.assertIn('1 worker from 1 account · 1 thread · 0 searching '
                      'right now', _reading(_page()))

    def test_no_worker_at_all_says_so(self):
        _ping('switched-off-box', 'alice',
              seen=timezone.now() - timedelta(hours=1))

        body = _reading(_page())

        self.assertIn('No worker has sent a heartbeat in the last 3 minutes',
                      body)
        self.assertNotIn('switched-off-box', body)


class RowTests(TestCase):

    def test_a_worker_shows_its_owner_and_its_capacity(self):
        _ping('alice-box-atomicdb', 'alice', threads=16, hash_mb=2048,
              os='Linux 6.8')

        raw = _page()
        body = _reading(raw)

        self.assertIn('alice-box-atomicdb', body)
        self.assertIn('16 threads · 2048 MB hash · Linux 6.8', body)
        self.assertIn('href="/atomicdb/user/alice/"', raw)

    def test_a_searching_worker_shows_line_budget_source_and_start(self):
        _busy('alice-box', 'alice', 'a2a3', minutes_ago=3)

        raw = _page()
        body = _reading(raw)

        self.assertIn('1. a3', body)
        self.assertIn('128.0M nodes · Auto · started 3 minutes ago', body)
        self.assertIn(
            f'href="/atomicdb/explore/{_child("a2a3").key}/"', raw)

    def test_a_declared_route_is_the_line_shown_and_linked(self):
        root = ingest.get_or_create_position(logic.start_fen())
        target = _play(_play(root, 'a2a3'), 'a7a6')
        task = _lease(target, 'box', source=USER, requested_by='bob',
                      route='a2a3,a7a6')
        _ping('box', 'alice', current_task_id=task.id)

        raw = _page()

        self.assertIn('1. a3 a6', _reading(raw))
        self.assertIn(f'/atomicdb/explore/{target.key}/?play=a2a3%2Ca7a6',
                      raw)

    def test_a_request_names_its_account(self):
        _busy('box', 'alice', 'a2a3', source=USER, requested_by='bob')

        task = _rows()[0]['task']
        raw = _page()

        self.assertEqual((task['requester'], task['requested_by'],
                          task['more']), ('account', 'bob', 0))
        self.assertIn('href="/atomicdb/user/bob/"', raw)

    def test_several_requesters_read_as_the_first_and_a_count(self):
        _busy('box', 'alice', 'a2a3', source=USER, requested_by='bob',
              also_requested_by=['carol', 'dave'], backers=2)

        raw = _page()

        self.assertIn('bob and 2 more', _reading(raw))
        # Who joined a request is a count everywhere on the site, never a
        # name: the contributor page says "2 more people asked for this".
        self.assertNotIn('carol', raw)
        self.assertNotIn('dave', raw)

    def test_an_anonymous_request_says_so(self):
        _busy('box', 'alice', 'a2a3', source=USER, requested_by='')

        self.assertEqual(_rows()[0]['task']['requester'], 'anonymous')
        self.assertIn('Anonymous', _reading(_page()))

    def test_what_the_selector_chose_belongs_to_the_server(self):
        _busy('auto-box', 'alice', 'a2a3', source=AUTO)
        _busy('fill-box', 'alice', 'b2b3', source=FILL)

        rows = _rows()

        self.assertEqual([row['task']['requester'] for row in rows],
                         ['server', 'server'])
        self.assertEqual([row['task']['source'] for row in rows],
                         ['AUTO', 'FILL'])
        body = _reading(_page())
        self.assertIn('Fill', body)
        self.assertIn('server', body)

    def test_a_worker_with_nothing_leased_is_between_tasks(self):
        _ping('idle-box', 'alice')

        self.assertIsNone(_rows()[0]['task'])
        self.assertIn('idle-box 8 threads · 1024 MB hash · Linux alice '
                      'between tasks', _reading(_page()))

    def test_rows_group_by_owner_and_list_slots_in_order(self):
        _ping('box#10', 'bob')
        _ping('box#2', 'bob')
        _ping('zed', 'alice')

        self.assertEqual([row['machine'] for row in _rows()],
                         ['zed', 'box#2', 'box#10'])


class LeaseRuleTests(TestCase):
    """Which task a worker is on: the rules of "Now analyzing"."""

    def test_a_modern_worker_whose_engine_stalled_is_between_tasks(self):
        # The worker clears its task when engine nodes stop advancing; the
        # home page then drops it from "Now analyzing", and so does this.
        _lease(_child('a2a3'), 'box')
        _ping('box', 'alice', current_task_id=None)

        self.assertIsNone(_rows()[0]['task'])

    def test_a_lease_gone_quiet_is_not_a_search(self):
        task = _lease(_child('a2a3'), 'box',
                      beat_minutes_ago=views.LEASE_MINUTES + 1)
        _ping('box', 'alice', current_task_id=task.id)

        self.assertIsNone(_rows()[0]['task'])

    def test_a_build_without_task_heartbeats_shows_its_lease(self):
        task = _lease(_child('a2a3'), 'legacy-box', token='', minutes_ago=25)
        _ping('legacy-box', 'alice', current_task_id=None)

        self.assertEqual(_rows()[0]['task']['key'], task.position_id)

    def test_one_search_is_never_two_rows(self):
        # Two accounts announcing the same machine name: one lease, one row.
        _lease(_child('a2a3'), 'shared-box', token='')
        _ping('shared-box', 'alice')
        _ping('shared-box', 'bob')

        tasks = [row['task'] for row in _rows()]

        self.assertEqual(sum(1 for task in tasks if task), 1)

    def test_the_worker_that_names_its_search_keeps_it(self):
        task = _lease(_child('a2a3'), 'shared-box', token='')
        _ping('shared-box', 'alice')
        _ping('shared-box', 'bob', current_task_id=task.id)

        owners = [row['owner'] for row in _rows() if row['task']]

        self.assertEqual(owners, ['bob'])


class HomeAgreementTests(TestCase):

    def setUp(self):
        metrics.reset_metrics_cache()

    def tearDown(self):
        metrics.reset_metrics_cache()

    def test_now_analyzing_and_this_page_name_the_same_searches(self):
        _busy('a-box', 'alice', 'a2a3')
        _busy('b-box', 'bob', 'b2b3', source=USER, requested_by='carol')
        _busy('c-box', 'bob', 'c2c3', source=FILL)
        _lease(_child('d2d3'), 'stalled-box')
        _ping('stalled-box', 'dave', current_task_id=None)
        _lease(_child('e2e3'), 'legacy-box', token='', minutes_ago=25)
        _ping('legacy-box', 'erin')
        _ping('idle-box', 'frank')

        home = Client().get('/atomicdb/')
        page = active_workers.present()

        shown_there = {row['key'] for row in home.context['analyzing']}
        shown_here = {row['task']['key'] for row in page['workers']
                      if row['task']}
        self.assertEqual(len(shown_here), 4)
        self.assertEqual(shown_there, shown_here)
        self.assertEqual(page['count'], 6)
        self.assertEqual(home.context['workers_h'], '6')

    def test_the_home_page_links_the_number_and_the_column_here(self):
        _ping('box', 'alice')

        raw = Client().get('/atomicdb/').content.decode()

        # By pattern and not by the literal markup: the response goes through
        # the HTML minifier, which owns the whitespace between tags.
        self.assertRegex(
            raw, r'title="Workers with a heartbeat in the last 3 minutes">\s*'
                 r'<div class="v">\s*<a href="/atomicdb/workers/">\s*1\s*</a>')
        self.assertRegex(raw, r'Now analyzing\s*<a href="/atomicdb/workers/"')

    def test_the_page_answers_at_its_address(self):
        response = Client().get('/atomicdb/workers/')

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'atomicdb/workers.html')
        self.assertIn('Active workers', _reading(response.content.decode()))

    def test_the_line_wraps_instead_of_being_cut(self):
        # The table borrows the contributor page's, whose .qline caps would
        # cut every line of one opening to the same visible prefix.
        _busy('box', 'alice', 'a2a3')
        style = (Path(settings.BASE_DIR) / 'atomicdb' / 'static' / 'atomicdb'
                 / 'atomicdb.css').read_text(encoding='utf-8')

        self.assertIn('<table class="contrib-workers fleet-workers"',
                      _page())
        rule = style[style.index('table.fleet-workers .qline {'):]
        rule = rule[:rule.index('}')]
        self.assertIn('display: inline', rule)
        self.assertIn('white-space: normal', rule)


class PrivacyTests(TestCase):

    def test_no_lease_secret_reaches_the_page(self):
        task = _busy('box', 'alice', 'a2a3', token='fencing-token-secret',
                     lease_session='session-secret')

        raw = _page()

        self.assertIn(task.position_id, raw)
        self.assertNotIn('fencing-token-secret', raw)
        self.assertNotIn('session-secret', raw)

    def test_a_row_carries_only_the_fields_it_shows(self):
        _busy('box', 'alice', 'a2a3', source=USER, requested_by='bob',
              engine_sha='e' * 64, net_sha='f' * 64)

        row = _rows()[0]

        self.assertEqual(set(row), {'machine', 'owner', 'threads', 'hash_mb',
                                    'os', 'task'})
        self.assertEqual(set(row['task']),
                         {'key', 'san', 'full', 'play', 'budget', 'source',
                          'since', 'requester', 'requested_by', 'more'})


class CostTests(TestCase):
    """What the page costs does not depend on how many workers there are."""

    def _cost(self, workers, warm):
        for index, uci in enumerate(PAWNS[:workers]):
            _busy(f'box{index}', f'owner{index % 3}', uci)
        _ping('idle', 'owner0')
        request = RequestFactory().get('/atomicdb/workers/')
        request.user = AnonymousUser()
        if warm:
            active_workers.workers_page(request)
        aliases = {settings.ATOMICDB_DATABASE_ALIAS, 'default'}
        captured = [CaptureQueriesContext(connections[alias])
                    for alias in sorted(aliases)]
        for context in captured:
            context.__enter__()
        try:
            response = active_workers.workers_page(request)
        finally:
            for context in reversed(captured):
                context.__exit__(None, None, None)
        self.assertEqual(response.status_code, 200)
        return sum(len(context.captured_queries) for context in captured)

    def test_a_warm_page_reads_the_pings_and_the_leases_and_nothing_else(self):
        self.assertEqual(self._cost(3, warm=True), 2)

    def test_three_workers_and_twelve_cost_the_same_warm(self):
        three = self._cost(3, warm=True)
        AnalysisTask.objects.all().delete()
        WorkerPing.objects.all().delete()

        twelve = self._cost(12, warm=True)

        self.assertEqual(three, twelve)

    def test_three_workers_and_twelve_cost_the_same_cold(self):
        three = self._cost(3, warm=False)
        AnalysisTask.objects.all().delete()
        WorkerPing.objects.all().delete()
        from django.core.cache import cache
        cache.clear()

        twelve = self._cost(12, warm=False)

        self.assertEqual(three, twelve)
