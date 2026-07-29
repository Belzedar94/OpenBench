"""Acceso PEREZOSO a la base de AtomicDB.

Antes este modulo leia ``settings.ATOMICDB_DATABASE_ALIAS`` y resolvia
``connections[alias]`` EN EL IMPORT, y todo importador de ``connection`` se
quedaba la conexion de aquel momento para siempre: un ``override_settings``
en tests, o el protocolo de activacion del split cambiando el alias despues
de arrancar, dejaban a media aplicacion hablando con la base equivocada sin
ningun error que lo delatara.  Ahora el alias se lee de settings en el
momento de USARLO y ``connection`` es un proxy que resuelve en cada acceso —
el mismo patron que ``django.db.connection``, solo que con el alias vigente
de AtomicDB en lugar de ``default``.

``DATABASE_ALIAS`` se conserva como atributo de modulo (via ``__getattr__``)
por compatibilidad: quien lo importe recibe el valor VIGENTE en ese momento.
Para codigo nuevo, ``current_alias()`` dice lo que hace.
"""

from django.conf import settings
from django.db import connections, transaction


def current_alias():
    return getattr(settings, 'ATOMICDB_DATABASE_ALIAS', 'default')


class _AtomicDBConnectionProxy:
    """Proxy de ``connections[current_alias()]`` resuelto en cada acceso."""

    def __getattr__(self, name):
        return getattr(connections[current_alias()], name)

    def __setattr__(self, name, value):
        setattr(connections[current_alias()], name, value)

    def __delattr__(self, name):
        delattr(connections[current_alias()], name)

    def __eq__(self, other):
        return connections[current_alias()] == other

    def __hash__(self):
        return hash(connections[current_alias()])

    def __repr__(self):
        return f'<AtomicDBConnectionProxy alias={current_alias()!r}>'


connection = _AtomicDBConnectionProxy()


def atomic():
    return transaction.atomic(using=current_alias())


def __getattr__(name):
    if name == 'DATABASE_ALIAS':
        return current_alias()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
