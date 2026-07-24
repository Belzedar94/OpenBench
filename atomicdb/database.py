from django.conf import settings
from django.db import connections, transaction


DATABASE_ALIAS = getattr(settings, 'ATOMICDB_DATABASE_ALIAS', 'default')
connection = connections[DATABASE_ALIAS]


def atomic():
    return transaction.atomic(using=DATABASE_ALIAS)
