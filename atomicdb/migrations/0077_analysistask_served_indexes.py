# The two indexes behind ``live_request._served_stamps``: an account's last
# lease, read as one backward probe of (account, leased_at) per account
# instead of grouping every task leased in the last hour (14k rows, 68k heap
# blocks, 1.3-2.7 s per pass of the lease query on 25-Nov-2026).
#
#   atomic_task_user_served  (requested_by, leased_at) WHERE source = 'USER'
#   atomic_task_lane_served  (lane_account, leased_at)
#                            WHERE source = 'USER' AND lane_account <> ''
#
# atomicdb_analysistask holds 5.6M rows (2.8 GB), so on PostgreSQL both are
# built CONCURRENTLY, outside a transaction (``atomic = False``), and the lease
# and the ingest keep writing while they build.  ``IF NOT EXISTS`` lets the
# migration do nothing where production already received them by hand.  Django
# state gets the plain ``AddIndex`` (``Meta.indexes`` and ``makemigrations``
# agree).  SQLite, where the tests live, understands partial indexes too: it
# builds the same ones, without CONCURRENTLY (same pattern as 0073).

from django.db import migrations, models

INDICES = (
    ('atomic_task_user_served', '(requested_by, leased_at)',
     "source = 'USER'"),
    ('atomic_task_lane_served', '(lane_account, leased_at)',
     "source = 'USER' AND lane_account <> ''"),
)


def _concurrente(schema_editor):
    return ('CONCURRENTLY '
            if schema_editor.connection.vendor == 'postgresql' else '')


def crear(apps, schema_editor):
    for nombre, columnas, condicion in INDICES:
        schema_editor.execute(
            f'CREATE INDEX {_concurrente(schema_editor)}IF NOT EXISTS '
            f'{nombre} ON atomicdb_analysistask {columnas} WHERE {condicion}')


def borrar(apps, schema_editor):
    for nombre, _columnas, _condicion in INDICES:
        schema_editor.execute(
            f'DROP INDEX {_concurrente(schema_editor)}IF EXISTS {nombre}')


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('atomicdb', '0076_analysistask_background'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name='analysistask',
                    index=models.Index(
                        condition=models.Q(('source', 'USER')),
                        fields=['requested_by', 'leased_at'],
                        name='atomic_task_user_served'),
                ),
                migrations.AddIndex(
                    model_name='analysistask',
                    index=models.Index(
                        condition=models.Q(
                            ('source', 'USER'),
                            models.Q(('lane_account', ''), _negated=True)),
                        fields=['lane_account', 'leased_at'],
                        name='atomic_task_lane_served'),
                ),
            ],
            database_operations=[
                migrations.RunPython(crear, borrar),
            ],
        ),
    ]
