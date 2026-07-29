from django.conf import settings


ATOMICDB_APP_LABEL = 'atomicdb'
ATOMICDB_DATABASE_ALIAS = 'atomicdb'


def _selected_alias():
    return getattr(settings, 'ATOMICDB_DATABASE_ALIAS', 'default')


def _atomicdb_migration_aliases():
    """Keep the legacy default schema current as a rollback shadow.

    Runtime reads and writes still use only ``_selected_alias()``.  Once the
    split is active, migrations must run on both SQLite files so Django never
    records an AtomicDB migration as applied on ``default`` while silently
    skipping its schema operations.
    """
    selected = _selected_alias()
    if selected == 'default':
        return {'default'}
    return {'default', selected}


class AtomicDBRouter:

    def db_for_read(self, model, **hints):
        if model._meta.app_label == ATOMICDB_APP_LABEL:
            return _selected_alias()
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == ATOMICDB_APP_LABEL:
            return _selected_alias()
        return None

    def allow_relation(self, obj1, obj2, **hints):
        obj1_is_atomicdb = obj1._meta.app_label == ATOMICDB_APP_LABEL
        obj2_is_atomicdb = obj2._meta.app_label == ATOMICDB_APP_LABEL
        if obj1_is_atomicdb or obj2_is_atomicdb:
            if not (obj1_is_atomicdb and obj2_is_atomicdb):
                return False
            selected_alias = _selected_alias()
            return (
                obj1._state.db in (None, selected_alias)
                and obj2._state.db in (None, selected_alias)
            )
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == ATOMICDB_APP_LABEL:
            return db in _atomicdb_migration_aliases()
        # La base de AtomicDB no acepta tablas de otras apps.  "La base de
        # AtomicDB" es el alias SELECCIONADO en settings, no solo el nombre
        # convencional: comparar contra el literal dejaba sin proteccion a un
        # despliegue cuyo alias se llame de otra manera (auth y compania se
        # migrarian dentro).  El literal se conserva ademas para el alias
        # configurado pero aun no activado.
        if db != 'default' and db in {ATOMICDB_DATABASE_ALIAS,
                                      _selected_alias()}:
            return False
        return None
