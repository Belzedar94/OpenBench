"""A click is a click: the USER band is served strictly first-come.

Ordering humans by POSITION priority made a deep, losing line (strongly
negative priority — a mate the selector rightly despises) wait behind every
fresh click from anybody else.  Wolfram queued a line's replies and watched
an empty-looking queue serve everyone but him for an hour.  Position
priority still rules AUTO/FILL/SEED, where the selector computes it for
exactly that purpose.
"""

from django.contrib.auth.models import User
from django.test import Client

from . import ingest, logic
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


class UserBandFifoTests(TestCase):

    def setUp(self):
        User.objects.create_user(username='w', password='p')
        self.client = Client()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        edges = list(Edge.objects.filter(parent=root).order_by('move_uci')[:2])
        self.despised = edges[0].child      # deep losing line
        self.beloved = edges[1].child       # fresh attractive line
        Position.objects.filter(key=self.despised.key).update(priority=-73.0)
        Position.objects.filter(key=self.beloved.key).update(priority=8.0)

    def _lease(self, machine):
        return self.client.post('/atomicdb/api/lease', {
            'username': 'w', 'password': 'p', 'machine': machine,
            'worker_build': '2026072203', 'lease_session': machine,
        }).json()

    def test_an_older_click_is_served_before_a_better_positioned_one(self):
        older = AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)
        AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)

        leased = self._lease('m1')['tasks'][0]

        self.assertEqual(leased['id'], older.id)

    def test_the_selector_bands_still_follow_position_priority(self):
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)
        wanted = AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)

        leased = self._lease('m2')['tasks'][0]

        self.assertEqual(leased['id'], wanted.id)

    def test_a_user_click_still_outranks_every_selector_task(self):
        AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)
        clicked = AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)

        leased = self._lease('m3')['tasks'][0]

        self.assertEqual(leased['id'], clicked.id)
