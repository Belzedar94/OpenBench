"""The line back to the root is computed once, not once per click.

The night a nightly batch and a traffic record landed on the same VPS, load
hit 34 and the explorer stopped being usable: every explore/goto render was
paying a reverse BFS to the root — up to 96 plies of window-function queries,
up to 1024 nodes inside a transposed component, ~300ms on a deep position —
to rebuild a breadcrumb that had not changed since the visitor's previous
click and almost never does.

What follows pins the mechanism, not a millisecond count: the walk runs once
per position per TTL, the "deep line" marker survives the round trip, and the
live state of the position on screen (evals, move rows) is NOT part of the
deal — that must still be fresh on every render, warm cache or not.
"""

from unittest import mock

from django.core.cache import cache
from django.test import Client, override_settings

from . import ingest, logic, views
from .models import Edge, Position
from .testing import TestCase


CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-lineage-cache-tests',
    },
}


def _play(parent, uci):
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


def _chain(ucis):
    """Materialise a line from startpos and hand back its last position."""
    pos = ingest.get_or_create_position(logic.start_fen())
    for uci in ucis:
        pos = _play(pos, uci)
    return pos


def _counted_walk():
    """Patch the expensive half so a test can count how often it ran."""
    return mock.patch.object(views, '_walk_lines_to_root',
                             wraps=views._walk_lines_to_root)


@override_settings(CACHES=CACHE_FOR_TESTS)
class LineageCacheHitTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.target = _chain(['g1f3', 'g8f6', 'b1c3', 'b8c6'])

    def test_a_second_explore_render_does_not_walk_again(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        with _counted_walk() as walk:
            first = self.client.get(url)
            second = self.client.get(url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(walk.call_count, 1)

    def test_the_cached_breadcrumb_is_the_same_breadcrumb(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        first = self.client.get(url)
        second = self.client.get(url)

        self.assertTrue(first.context['line_from_root'])
        self.assertTrue(second.context['line_from_root'])
        self.assertEqual(
            [(token['num'], token['san']) for token in first.context['line']],
            [(token['num'], token['san']) for token in second.context['line']])
        self.assertEqual(second.context['line'][0]['san'], 'Nf3')

    def test_a_hit_and_a_miss_hand_back_the_same_shape(self):
        """Otherwise the difference only shows up under warm traffic."""
        miss_top, miss_line = views._line_to_root(self.target)
        hit_top, hit_line = views._line_to_root(self.target)

        self.assertEqual(type(miss_top), type(hit_top))
        self.assertEqual((miss_top.key, miss_top.fen),
                         (hit_top.key, hit_top.fen))
        self.assertEqual(miss_line, hit_line)

    def test_a_batch_walks_only_the_keys_the_cache_could_not_answer(self):
        """The home page asks for ~30 keys at a time and most repeat."""
        other = _chain(['e2e4', 'e7e6', 'd2d4'])
        views._line_labels_many([self.target.key])

        with _counted_walk() as walk:
            labels = views._line_labels_many([self.target.key, other.key])

        walk.assert_called_once()
        self.assertEqual(list(walk.call_args.args[0]), [other.key])
        self.assertEqual(set(labels), {self.target.key, other.key})

    def test_the_explorer_and_the_home_page_do_not_share_an_entry(self):
        """They walk different ply budgets; the same key can differ.

        The explorer resolves 64 plies and the home page 96.  One shared
        entry would let whichever page rendered first decide what the other
        one says about a line between those two depths.
        """
        views._lines_to_root([self.target.key], max_plies=64)

        with _counted_walk() as walk:
            views._lines_to_root([self.target.key], max_plies=96)

        self.assertEqual(walk.call_count, 1)

    def test_a_position_atomicdb_does_not_have_is_not_cached_as_absent(self):
        """`goto` materialises nodes; a two-minute "no such node" is a bug."""
        stranger = logic.key_of(logic.apply_move(logic.start_fen(), 'h2h4'))
        self.assertEqual(views._lines_to_root([stranger]), {})

        ingest.get_or_create_position(logic.apply_move(logic.start_fen(),
                                                       'h2h4'))

        self.assertEqual(set(views._lines_to_root([stranger])), {stranger})


@override_settings(CACHES=CACHE_FOR_TESTS)
class CappedMarkerSurvivesTheCacheTests(TestCase):
    """A capped walk must still read as DEEP after a cache round trip.

    ``lineage_capped`` reaches the renderer as an attribute glued onto the
    top position by the walk itself.  An ORM instance does not survive a
    cache, and neither would that marker if the payload were not explicit —
    the label would silently fall back to the orphan branch and the home page
    would show a headless "… Bb5 Kg2 Bc4 Bd7" again, but only once the cache
    was warm.
    """

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.deep = _chain(['a2a3', 'a7a6', 'b2b3', 'b7b6'])
        Position.objects.filter(key=self.deep.key).update(priority=9_000.0)

    def test_the_deep_line_label_is_identical_on_the_hit(self):
        with mock.patch.object(views, 'LINEAGE_SEARCH_MAX_NODES', 2), \
                _counted_walk() as walk:
            miss = views._line_labels(self.deep.key)
            hit = views._line_labels(self.deep.key)

        self.assertEqual(walk.call_count, 1)
        self.assertTrue(miss[0].startswith('deep line, ply ≥'), miss)
        self.assertEqual(miss, hit)

    def test_the_home_page_still_says_deep_line_when_the_cache_is_warm(self):
        # Two visitors, because the home page carries its own 15s
        # ``cache_page`` and a repeat visit would never reach the lineage
        # code at all.  Under gunicorn this is the ordinary case: a second
        # reader rendering the same rows the first one just resolved.
        other = Client()
        other.cookies['csrftoken'] = 'b' * 64

        with mock.patch.object(views, 'LINEAGE_SEARCH_MAX_NODES', 2), \
                _counted_walk() as walk:
            self.client.get('/atomicdb/')
            warm = other.get('/atomicdb/')

        self.assertEqual(walk.call_count, 1)
        row = next(entry for entry in warm.context['upnext']
                   if entry['key'] == self.deep.key)
        self.assertTrue(row['san'].startswith('deep line, ply ≥'), row['san'])
        self.assertFalse(row['san'].startswith('…'), row['san'])


@override_settings(CACHES=CACHE_FOR_TESTS)
class LiveStateIsNotCachedTests(TestCase):
    """Only the lineage. The position itself is read fresh, every render.

    The explorer exists to show current analysis.  Caching the breadcrumb is
    safe because a new edge can only make it shorter; caching what the engine
    currently thinks would be the explorer lying about its own subject.
    """

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.target = _chain(['g1f3', 'g8f6'])

    def test_a_new_eval_shows_on_the_next_render_with_the_line_still_cached(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        with _counted_walk() as walk:
            first = self.client.get(url)
            Position.objects.filter(key=self.target.key).update(eval_cp=777)
            second = self.client.get(url)

        # One walk across both renders: the breadcrumb really was served
        # from the cache on the second one...
        self.assertEqual(walk.call_count, 1)
        # ...and the number the visitor came for is still the current one.
        self.assertNotIn('+777cp', first.content.decode())
        self.assertIn('+777cp', second.content.decode())

    def test_a_new_child_move_row_shows_on_the_next_render(self):
        url = f'/atomicdb/explore/{self.target.key}/'

        with _counted_walk() as walk:
            first = self.client.get(url)
            _play(self.target, 'b1c3')
            second = self.client.get(url)

        self.assertEqual(walk.call_count, 1)
        self.assertEqual(len(first.context['moves']), 0)
        self.assertEqual([move['uci'] for move in second.context['moves']],
                         ['b1c3'])

    def test_the_explore_response_is_still_never_cached_as_a_page(self):
        response = self.client.get(f'/atomicdb/explore/{self.target.key}/')

        self.assertNotIn('max-age', response.headers.get('Cache-Control', ''))


@override_settings(CACHES=CACHE_FOR_TESTS)
class LineageCacheTtlTests(TestCase):
    """The TTL is the only invalidation, so it has to be honoured and movable."""

    def setUp(self):
        cache.clear()
        self.target = _chain(['g1f3', 'g8f6'])

    def test_the_named_constant_is_the_timeout_that_reaches_the_backend(self):
        with mock.patch.object(views, 'LINEAGE_CACHE_SECONDS', 7), \
                mock.patch.object(views.cache, 'set_many',
                                  wraps=views.cache.set_many) as store:
            views._line_labels(self.target.key)

        store.assert_called_once()
        self.assertEqual(store.call_args.args[1], 7)

    def test_zero_seconds_turns_the_cache_off_entirely(self):
        """The kill switch: no reads, no writes, straight to the walk."""
        with mock.patch.object(views, 'LINEAGE_CACHE_SECONDS', 0), \
                mock.patch.object(views.cache, 'set_many') as store, \
                _counted_walk() as walk:
            views._line_labels(self.target.key)
            views._line_labels(self.target.key)

        self.assertEqual(walk.call_count, 2)
        store.assert_not_called()

    def test_an_expired_entry_is_walked_again(self):
        with mock.patch.object(views, 'LINEAGE_CACHE_SECONDS', 300):
            views._line_labels(self.target.key)

        with mock.patch.object(views.cache, 'get_many', return_value={}), \
                _counted_walk() as walk:
            label = views._line_labels(self.target.key)

        self.assertEqual(walk.call_count, 1)
        self.assertEqual(label[0], '1. Nf3 Nf6')

    def test_the_default_ttl_is_short_enough_to_be_the_only_invalidation(self):
        self.assertGreater(views.LINEAGE_CACHE_SECONDS, 0)
        self.assertLessEqual(views.LINEAGE_CACHE_SECONDS, 300)


class LineageCacheBackendTests(TestCase):
    """Django's implicit default holds 300 entries, and that is not a cache.

    One home render resolves a couple of dozen breadcrumbs and shares the
    same store with the ``cache_page`` bodies.  At 300 slots a quiet evening
    of clicking evicts the lineage before the TTL ever expires it, which is
    memory spent to still pay the walk.  The backend must also stay local:
    per-process copies are the design, not a compromise.
    """

    def test_the_project_configures_a_real_local_cache(self):
        from django.conf import settings

        default = settings.CACHES['default']
        self.assertIn('locmem', default['BACKEND'])
        self.assertGreater(
            default.get('OPTIONS', {}).get('MAX_ENTRIES', 300), 1000)
