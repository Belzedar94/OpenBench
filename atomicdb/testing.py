"""Database-aware Django test bases for the AtomicDB app."""

from django.conf import settings
from django.test import TestCase as DjangoTestCase
from django.test import TransactionTestCase as DjangoTransactionTestCase


def _atomicdb_test_databases():
    return {'default', settings.ATOMICDB_DATABASE_ALIAS}


class TestCase(DjangoTestCase):
    """Allow auth/default plus the configured AtomicDB database."""

    databases = _atomicdb_test_databases()


class TransactionTestCase(DjangoTransactionTestCase):
    """Transaction variant with the same explicit database contract."""

    databases = _atomicdb_test_databases()
