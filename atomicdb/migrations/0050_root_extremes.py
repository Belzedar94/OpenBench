# The start position and its twenty first moves, over time (atomicdb.roots).
#
# Two tables, and on PostgreSQL one row trigger that keeps the first of them.
#
# WHY A TRIGGER.  The member who asked wants every change caught, not an hourly
# sample, and most writes that move these numbers are the cascade's
# ``bulk_update``, which sends no Django signal.  The database is the one place
# that sees all of them.
#
# WHY IT IS CHEAP.  ``atomicdb_position`` holds ~100M rows and five ingest
# writers update it all day.  Three filters stand between an update and the
# function, cheapest first:
#   * ``UPDATE OF status, backed_eval, eval_cp``: for a statement whose SET
#     list names none of the three (the selector's
#     ``bulk_update(['priority'])``, ``expanded``, ``reachable``...) the
#     trigger is skipped on a column-set test;
#   * ``WHEN``: evaluated for each row as it is updated, before anything is
#     queued.  For any key but the 21 below it is one membership test against
#     21 constants, false, and nothing else happens: no event queued, no
#     function called.  The table already carries the row triggers of the
#     foreign keys that point at it, so PostgreSQL already reads the old row
#     of every update; this trigger adds no read of its own;
#   * and among the 21, only a write that really changes one of the three
#     columns calls the function, which upserts one row of a 21-row table.
#
# THE WATCHED KEYS ARE SPELLED OUT, not read from the edges at migration time,
# so a fresh database (CI, a new install) watches the same 21 positions as
# production before anything has been expanded.  They are the canonical keys
# of the start position and of the position after each legal first move;
# ``atomicdb/test_root_extremes.py`` holds this list equal to the one
# ``roots.watched_positions`` computes with ``logic``.
#
# PostgreSQL only, the same way ``0047`` is: SQLite never gets the trigger, and
# ``roots.fold_extremes`` folds the current values in at the hourly capture
# there instead.  The seeding below runs on both.

from django.db import migrations, models
from django.utils import timezone

START_KEY = 'b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879'
FIRST_MOVE_KEYS = (
    '1757408553ca3a86694b1cc014a84b7e2fdb5157eb9f8c5d6f74dc3c1e03c9e1',  # a3
    '362f79062a22667130a082a7a9579cbbf67ae1b712fbd6eeeb648a1e38d54955',  # a4
    'db78dd638384fd72ee9e53e4caf3559831bf9ec687fa9009f40e6ac2bff0072d',  # Na3
    'dce916e317986e4f7126e3faf6dc495c14c7a0437987540abaa479bc214a8222',  # Nc3
    '5ec1112a532d302767c29e0d13ec528b5494f982e1fd4261948a868e3e9b3431',  # b3
    '49c61ccecca2d33babe787646793a428f36472f025fea63567e37758921c17de',  # b4
    '43e1f23fa182d98c85cbac44200461b511165533802e335963e8826718229734',  # c3
    '1be838ff1e13ba41e9871b72ebcc2f2f6f6e75d755e9cf28f9e4a12c3c6a8ee6',  # c4
    '0916302fd3520a129ec0fd421fcabc901c3940f2013deea0760956a3edf31fd7',  # d3
    '4263f01cff2e56045fd4d53804b0ba85b915e632be58552b6bc4a461c36405a1',  # d4
    '568641c6004364db5c3321af46db9daccf7110e48ae2b18895996871a9b713f5',  # e3
    '7d93ad7e3a1c986e6f76bcf307af48d8174738064c5e3acfdf17b8cd05b4f7d6',  # e4
    '2b7fb8dd459ef9cfb78c0004afc84991fc16b995ce048512969b7bcd40b629ca',  # f3
    '824f55c3617bcf3569062c4356fd8ad10b0585755a997b993865263937acfc27',  # f4
    '05d0959198faa0410b36326cccb84d3b1f4ccbaf9824f9d5088bda723e97cf61',  # Nf3
    '269150855fc38863f2c3e0e4ba057f2fa36c62d8d2650d5f7cd7365431d17d1c',  # Nh3
    '64148148c66bf36ec33a14f168690de0beabe2ecc89d7e5cff700c5614a2ffdd',  # g3
    '5f7e377436a2196b4672f151fd47851ffa217ab5779a5dbfedff399005f4254d',  # g4
    'f47bb0ca3e359ae6bbe04ac05663b6436464b01f750385c6ed5f2380343e2f15',  # h3
    'ca026db7ab7f1598b8ce4f3bd9947a95f101ee00780efe9b61b49c62ae87ddb3',  # h4
)
WATCHED_KEYS = (START_KEY,) + FIRST_MOVE_KEYS

TRIGGER = 'atomicdb_root_extremes'
# How long the CREATE TRIGGER may queue for its lock before giving up.  It
# needs SHARE ROW EXCLUSIVE on atomicdb_position: readers go on, but every
# writer that arrives meanwhile waits behind it.  Failing after a few seconds
# and running the migration again beats stalling the ingest behind one long
# transaction.  (Not DROP TRIGGER IF EXISTS before it: a DROP takes ACCESS
# EXCLUSIVE even when there is nothing to drop, and that one stops readers.)
LOCK_TIMEOUT = '10s'

# The value the explorer headlines (ingest.known_eval_of), White's side: a
# proven status first, then the backed value, then the point eval.  The first
# value of a row starts it and is not a change.  A rewrite of the same value
# and status changes nothing, and best and worst only move on a strict
# improvement, so they keep the moment the value was FIRST reached.
#
# SET search_path FROM CURRENT pins the unqualified table name to the schema
# the migration created it in, whatever the session that fires it has set.
CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION atomicdb_root_extremes() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $body$
DECLARE
    v integer;
    t timestamptz := clock_timestamp();
BEGIN
    v := CASE NEW.status
             WHEN 'WHITE_WIN' THEN 10000
             WHEN 'BLACK_WIN' THEN -10000
             WHEN 'DRAW' THEN 0
             ELSE COALESCE(NEW.backed_eval, NEW.eval_cp)
         END;
    IF v IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO atomicdb_rootextreme AS r
        (key, best_value, best_status, best_at,
         worst_value, worst_status, worst_at,
         last_value, last_status, changed_at, changes, since)
    VALUES (NEW.key, v, NEW.status, t,
            v, NEW.status, t,
            v, NEW.status, t, 0, t)
    ON CONFLICT (key) DO UPDATE SET
        best_value = CASE WHEN v > r.best_value
                          THEN v ELSE r.best_value END,
        best_status = CASE WHEN v > r.best_value
                           THEN NEW.status ELSE r.best_status END,
        best_at = CASE WHEN v > r.best_value
                       THEN t ELSE r.best_at END,
        worst_value = CASE WHEN v < r.worst_value
                           THEN v ELSE r.worst_value END,
        worst_status = CASE WHEN v < r.worst_value
                            THEN NEW.status ELSE r.worst_status END,
        worst_at = CASE WHEN v < r.worst_value
                        THEN t ELSE r.worst_at END,
        last_value = v,
        last_status = NEW.status,
        changed_at = t,
        changes = r.changes + 1
    WHERE r.last_value IS DISTINCT FROM v
       OR r.last_status IS DISTINCT FROM NEW.status;
    RETURN NULL;
END
$body$
"""

CREATE_TRIGGER = """
CREATE TRIGGER atomicdb_root_extremes
AFTER UPDATE OF status, backed_eval, eval_cp ON atomicdb_position
FOR EACH ROW
WHEN (NEW.key IN ({keys})
      AND (OLD.status IS DISTINCT FROM NEW.status
           OR OLD.backed_eval IS DISTINCT FROM NEW.backed_eval
           OR OLD.eval_cp IS DISTINCT FROM NEW.eval_cp))
EXECUTE FUNCTION atomicdb_root_extremes()
""".format(keys=', '.join(f"'{key}'" for key in WATCHED_KEYS))

DROP_TRIGGER = 'DROP TRIGGER IF EXISTS atomicdb_root_extremes ON atomicdb_position'
DROP_FUNCTION = 'DROP FUNCTION IF EXISTS atomicdb_root_extremes()'


def _value(status, backed_eval, eval_cp):
    # Frozen copy of the precedence, as the function above spells it: a
    # migration does not import ``ingest`` (and with it pyffish).
    exact = {'WHITE_WIN': 10000, 'BLACK_WIN': -10000, 'DRAW': 0}.get(status)
    if exact is not None:
        return exact
    return backed_eval if backed_eval is not None else eval_cp


def seed(apps, schema_editor):
    """Start each watched row at today's value, on the database migrated."""
    alias = schema_editor.connection.alias
    Position = apps.get_model('atomicdb', 'Position')
    RootExtreme = apps.get_model('atomicdb', 'RootExtreme')
    now = timezone.now()
    rows = []
    for key, status, backed_eval, eval_cp in (
            Position.objects.using(alias).filter(key__in=WATCHED_KEYS)
            .values_list('key', 'status', 'backed_eval', 'eval_cp')):
        value = _value(status, backed_eval, eval_cp)
        if value is None:
            continue
        rows.append(RootExtreme(
            key=key, best_value=value, best_status=status, best_at=now,
            worst_value=value, worst_status=status, worst_at=now,
            last_value=value, last_status=status, changed_at=now,
            changes=0, since=now))
    RootExtreme.objects.using(alias).bulk_create(rows, ignore_conflicts=True)


def _trigger_exists(schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname = %s"
            " AND tgrelid = 'atomicdb_position'::regclass", [TRIGGER])
        return cursor.fetchone() is not None


def install(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        # Trigger FIRST, seed second, in the same transaction: once the
        # trigger holds its lock no writer can slip a change in between the
        # values read below and the moment the trigger starts watching.
        schema_editor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        schema_editor.execute(CREATE_FUNCTION)
        if not _trigger_exists(schema_editor):
            schema_editor.execute(CREATE_TRIGGER)
    seed(apps, schema_editor)


def uninstall(apps, schema_editor):
    # The DROP takes ACCESS EXCLUSIVE on atomicdb_position for the rest of
    # this transaction, so it waits for (and then holds off) readers too.
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    schema_editor.execute(DROP_TRIGGER)
    schema_editor.execute(DROP_FUNCTION)


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0049_contributorpref'),
    ]

    operations = [
        migrations.CreateModel(
            name='MainLineSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bucket_start', models.DateTimeField(unique=True)),
                ('captured_at', models.DateTimeField(auto_now_add=True)),
                ('value', models.IntegerField(null=True)),
                ('status', models.CharField(default='UNKNOWN', max_length=10)),
                ('plies', models.IntegerField(default=0)),
                ('moves', models.TextField(blank=True, default='')),
                ('san', models.TextField(blank=True, default='')),
                ('end_key', models.CharField(blank=True, default='', max_length=64)),
                ('end_value', models.IntegerField(null=True)),
                ('end_status', models.CharField(default='UNKNOWN', max_length=10)),
                ('end_reason', models.CharField(choices=[('PROVEN', 'Proven'), ('LIMIT', 'Limit'), ('NO_MOVE', 'No Move'), ('NO_EDGE', 'No Edge'), ('REPEAT', 'Repeat')], default='NO_MOVE', max_length=8)),
            ],
            options={
                'ordering': ['bucket_start'],
            },
        ),
        migrations.CreateModel(
            name='RootExtreme',
            fields=[
                ('key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('best_value', models.IntegerField()),
                ('best_status', models.CharField(max_length=10)),
                ('best_at', models.DateTimeField()),
                ('worst_value', models.IntegerField()),
                ('worst_status', models.CharField(max_length=10)),
                ('worst_at', models.DateTimeField()),
                ('last_value', models.IntegerField()),
                ('last_status', models.CharField(max_length=10)),
                ('changed_at', models.DateTimeField()),
                ('changes', models.IntegerField(default=0)),
                ('since', models.DateTimeField()),
            ],
        ),
        migrations.RunPython(install, uninstall),
    ]
