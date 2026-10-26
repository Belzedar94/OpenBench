# The watch list becomes a table, so it can grow while the site runs.
#
# WHY.  0051 wrote the 365 watched keys into the body of
# ``atomicdb_watched_key(key)``, which made every change to the list a
# migration.  Community names do not wait for migrations: a name approved on
# the site (Rhaxier Gambit and the like, 122 positions on production today,
# 2 of them watched) has to be followed from the moment it is approved.  Now
# the function looks the key up in ``atomicdb_watchedkey``, the approval adds
# the position in the same transaction (``roots.watch_approved``, connected
# in ``apps.py``), and the hourly capture adds whatever should be there and is
# not (``roots.sync_watched_keys``).
#
# THE TRIGGER DOES NOT CHANGE.  Its WHEN is still "status, backed_eval or
# eval_cp really changed AND atomicdb_watched_key(NEW.key)", so only the
# function is replaced, and replacing a function takes no lock on
# atomicdb_position.  The lookup stays in the WHEN and not in the trigger
# function: a row the WHEN turns away never becomes a queued trigger event.
#
# WHAT IT COSTS.  Measured on PostgreSQL 16.15 against the same 200k-row
# stand-in as 0051, with 485 watched keys:
#
#     watch-list form                       per changed row  statement  transaction
#     constant jsonb (0051)                      +0.9 us       +9 us      +24 us
#     table lookup, VOLATILE (this)              +5.8 us      +16 us      +31 us
#     table lookup, STABLE                       +5.7 us      +15 us      +35 us
#     table lookup with SET search_path          +7.3 us      +19 us      +42 us
#     table lookup as a SQL function             +4.7 us      +64 us      +84 us
#     lookup inside the trigger function         +6.2 us      +16 us      +42 us
#
# A row whose three values did not change costs nothing either way: the WHEN
# never calls the function for it.  At production's ~1,000 row updates a
# minute that is a few milliseconds of CPU a minute.
#
# VOLATILE ON PURPOSE.  A STABLE function reads with the snapshot of the
# UPDATE that calls it.  An UPDATE that waited for an approval's row lock
# took its snapshot before the approval committed, and would not see the key
# the approval just added; a VOLATILE function reads with a fresh one.  It
# costs 0.1 us a row.
#
# The table name is schema-qualified when the function is created rather
# than pinned with SET search_path, which would cost 1.5 us a row: a session
# with another search_path (a maintenance psql, say) must not make every
# UPDATE of a position fail on a missing table.
#
# Reversible to 0051: the constant function comes back before the table goes,
# and the rows of positions 0051 does not watch are removed.

import importlib

import django.utils.timezone
from django.db import migrations, models
from django.utils import timezone

FIRST = importlib.import_module('atomicdb.migrations.0050_root_extremes')
PREVIOUS = importlib.import_module('atomicdb.migrations.0051_watch_openings')

LOCK_TIMEOUT = FIRST.LOCK_TIMEOUT
# 0051 lists the first table first: the start position, its 20 first moves
# and the three replies to 1.Nf3.  The catalogue follows.
FIRST_TABLE_KEYS = 24

WATCH_FUNCTION = """
CREATE OR REPLACE FUNCTION atomicdb_watched_key(k text) RETURNS boolean
LANGUAGE plpgsql VOLATILE STRICT
AS $body$
BEGIN
    RETURN EXISTS (SELECT 1 FROM {table} WHERE key = k);
END
$body$
"""


def _standing_sources():
    """``{key: source}`` for the 365 keys 0051 watches."""
    root = set(FIRST.WATCHED_KEYS)
    sources = {}
    for index, key in enumerate(PREVIOUS.WATCHED_KEYS):
        if key in root:
            sources[key] = 'root'
        elif index < FIRST_TABLE_KEYS:
            sources[key] = 'reply'
        else:
            sources[key] = 'catalog'
    return sources


def _watched_table(schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('SELECT current_schema()')
        schema = cursor.fetchone()[0]
    quote = schema_editor.connection.ops.quote_name
    return f'{quote(schema)}.{quote("atomicdb_watchedkey")}'


def _seed(apps, schema_editor, keys, now):
    """Start the rows of newly watched keys at today's value.

    The position rows are locked first: a writer already in flight finishes
    before the value is read, and one that comes later waits for this commit
    and then finds the key watched.
    """
    alias = schema_editor.connection.alias
    Position = apps.get_model('atomicdb', 'Position')
    RootExtreme = apps.get_model('atomicdb', 'RootExtreme')
    rows = []
    for key, status, backed_eval, eval_cp in (
            Position.objects.using(alias).select_for_update()
            .filter(key__in=list(keys)).order_by('key')
            .values_list('key', 'status', 'backed_eval', 'eval_cp')):
        value = FIRST._value(status, backed_eval, eval_cp)
        if value is None:
            continue
        rows.append(RootExtreme(
            key=key, best_value=value, best_status=status, best_at=now,
            worst_value=value, worst_status=status, worst_at=now,
            last_value=value, last_status=status, changed_at=now,
            changes=0, since=now))
    RootExtreme.objects.using(alias).bulk_create(rows, ignore_conflicts=True)


def install(apps, schema_editor):
    alias = schema_editor.connection.alias
    WatchedKey = apps.get_model('atomicdb', 'WatchedKey')
    Suggestion = apps.get_model('atomicdb', 'OpeningNameSuggestion')
    now = timezone.now()
    sources = _standing_sources()
    community = [
        key for key in (Suggestion.objects.using(alias)
                        .filter(status='APPROVED')
                        .values_list('position_id', flat=True).distinct())
        if key not in sources]
    sources.update({key: 'community' for key in community})
    WatchedKey.objects.using(alias).bulk_create(
        [WatchedKey(key=key, source=source, added=now)
         for key, source in sources.items()],
        ignore_conflicts=True)
    if schema_editor.connection.vendor == 'postgresql':
        # Function and seed in one transaction: the new list and the rows of
        # the positions it adds become visible together, at commit.
        schema_editor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        schema_editor.execute(
            WATCH_FUNCTION.format(table=_watched_table(schema_editor)))
    _seed(apps, schema_editor, community, now)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        schema_editor.execute(PREVIOUS.CREATE_WATCH_FUNCTION)
    RootExtreme = apps.get_model('atomicdb', 'RootExtreme')
    (RootExtreme.objects.using(schema_editor.connection.alias)
     .exclude(key__in=PREVIOUS.WATCHED_KEYS).delete())


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0051_watch_openings'),
    ]

    operations = [
        migrations.CreateModel(
            name='WatchedKey',
            fields=[
                ('key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('source', models.CharField(choices=[('root', 'Root'), ('reply', 'Reply'), ('catalog', 'Catalog'), ('community', 'Community')], max_length=10)),
                ('added', models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
        migrations.RunPython(install, uninstall),
    ]
