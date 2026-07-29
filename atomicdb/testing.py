"""Database-aware Django test bases for the AtomicDB app."""

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase as DjangoTestCase
from django.test import TransactionTestCase as DjangoTransactionTestCase


def _atomicdb_test_databases():
    return {'default', settings.ATOMICDB_DATABASE_ALIAS}


class _IsolatedViewCache:
    """Start every test with an empty view cache.

    The hot read views carry a short ``cache_page``.  The default locmem
    backend outlives a single test, so without this a page rendered by one
    test would be served, verbatim and contextless, to the next one.
    ``_pre_setup`` runs before ``setUp`` and no subclass has to remember it.
    """

    def _pre_setup(self):
        cache.clear()
        super()._pre_setup()


class TestCase(_IsolatedViewCache, DjangoTestCase):
    """Allow auth/default plus the configured AtomicDB database."""

    databases = _atomicdb_test_databases()


class TransactionTestCase(_IsolatedViewCache, DjangoTransactionTestCase):
    """Transaction variant with the same explicit database contract."""

    databases = _atomicdb_test_databases()


def worker_account(username, password='pw', enabled=True):
    """Una cuenta como las que existen de verdad: usuario Y perfil.

    En produccion nadie llega a tener credenciales sin ``Profile`` — el
    registro lo crea en el mismo POST — y el protocolo de workers exige que
    ese perfil este habilitado, porque expulsar a alguien tiene que cortarle
    tambien el arriendo de tareas.  Un test que solo hiciera ``create_user``
    estaria probando contra una cuenta que el sitio no sabe producir.
    """
    from django.contrib.auth.models import User

    from OpenBench.models import Profile

    user = User.objects.create_user(username, password=password)
    Profile.objects.create(user=user, enabled=enabled)
    return user
