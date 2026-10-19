"""The wall-clock cap on the returning-child valuation (19-Oct-2026 incident).

A family full of loops turned the valuation of returning children into
ten-minute ingests: the node budget does not count the spine walks each re-read
position runs.  The cap ends the walk at a deadline, with the same outcome as an
exhausted node budget: the walk's verdict stands.
"""
import time

from django.test import SimpleTestCase

from atomicdb import ingest


class CycleDeadlineTests(SimpleTestCase):
    # SimpleTestCase refuses database queries, so these tests also prove that a
    # walk past its deadline returns before reading anything.

    def tearDown(self):
        ingest._cycle_deadline[0] = 0.0

    def test_a_walk_past_its_deadline_stops_before_any_read(self):
        ingest._cycle_deadline[0] = time.monotonic() - 1.0
        budget = [ingest.BACKED_CYCLE_MAX_NODES]
        self.assertIsNone(ingest._value_on_line('0' * 64, frozenset(), None, budget))
        self.assertEqual(budget[0], ingest.BACKED_CYCLE_MAX_NODES)

    def test_a_top_level_call_opens_a_fresh_deadline(self):
        ingest._cycle_deadline[0] = 0.0
        before = time.monotonic()
        ingest._resolve_returning_children({}, None)
        self.assertGreaterEqual(ingest._cycle_deadline[0],
                                before + ingest.BACKED_CYCLE_MAX_SECONDS)

    def test_a_nested_call_keeps_the_running_deadline(self):
        ingest._cycle_deadline[0] = 123.0
        ingest._resolve_returning_children({}, None, budget=[1])
        self.assertEqual(ingest._cycle_deadline[0], 123.0)
