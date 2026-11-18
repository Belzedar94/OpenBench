"""The worker credential cache in front of ``authenticate`` (18-nov-2026).

Every worker call carries username and password, and the full PBKDF2 hash on
each one ate the web workers' CPU.  A success is remembered for a few minutes,
but the database stays the authority: a hit is re-checked in one query, so a
password change or a disabled account bites on the very next request.
"""

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache

from OpenBench.models import Profile

from . import views
from .testing import TestCase, worker_account


class AuthCacheTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = worker_account('worker', 'secret')

    def tearDown(self):
        cache.clear()

    def _count_authenticate(self):
        return mock.patch.object(views, 'authenticate',
                                 wraps=views.authenticate)

    def test_a_second_call_does_not_hash_again(self):
        with self._count_authenticate() as spy:
            first = views._auth_pair('worker', 'secret')
            second = views._auth_pair('worker', 'secret')
            third = views._auth_pair('worker', 'secret')

        self.assertEqual(first, self.user)
        self.assertEqual(second, self.user)
        self.assertEqual(third, self.user)
        self.assertEqual(spy.call_count, 1)

    def test_the_cache_never_holds_the_password(self):
        views._auth_pair('worker', 'secret')
        key = views._auth_cache_key('worker', 'secret')

        self.assertNotIn('secret', key)
        entry = cache.get(key)
        self.assertIsNotNone(entry)
        self.assertNotIn('secret', repr(entry))
        self.assertNotIn(self.user.password, repr(entry))

    def test_a_password_change_invalidates_at_once(self):
        views._auth_pair('worker', 'secret')
        self.user.set_password('other')
        self.user.save()

        with self._count_authenticate() as spy:
            self.assertIsNone(views._auth_pair('worker', 'secret'))
        # The stale entry was dropped and the full path ran (and refused).
        self.assertEqual(spy.call_count, 1)
        self.assertIsNone(cache.get(views._auth_cache_key('worker', 'secret')))
        self.assertEqual(views._auth_pair('worker', 'other'), self.user)

    def test_a_disabled_profile_is_refused_on_a_hit(self):
        views._auth_pair('worker', 'secret')
        Profile.objects.filter(user=self.user).update(enabled=False)

        self.assertIsNone(views._auth_pair('worker', 'secret'))
        self.assertIsNone(cache.get(views._auth_cache_key('worker', 'secret')))

    def test_an_inactive_user_is_refused_on_a_hit(self):
        views._auth_pair('worker', 'secret')
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assertIsNone(views._auth_pair('worker', 'secret'))

    def test_a_wrong_password_never_hits_and_is_never_cached(self):
        views._auth_pair('worker', 'secret')

        with self._count_authenticate() as spy:
            self.assertIsNone(views._auth_pair('worker', 'wrong'))
            self.assertIsNone(views._auth_pair('worker', 'wrong'))
        self.assertEqual(spy.call_count, 2)
        self.assertIsNone(cache.get(views._auth_cache_key('worker', 'wrong')))

    def test_a_disabled_account_is_never_cached(self):
        worker_account('expelled', 'pw', enabled=False)

        self.assertIsNone(views._auth_pair('expelled', 'pw'))
        self.assertIsNone(cache.get(views._auth_cache_key('expelled', 'pw')))

    def test_empty_credentials_take_the_old_path(self):
        self.assertIsNone(views._auth_cache_key('', ''))
        self.assertIsNone(views._auth_pair('', ''))

    def test_a_lease_call_authenticates_once_across_requests(self):
        with self._count_authenticate() as spy:
            for _ in range(3):
                self.client.post('/atomicdb/api/heartbeat',
                                 {'username': 'worker', 'password': 'secret',
                                  'machine': 'box-1'})
        self.assertEqual(spy.call_count, 1)
