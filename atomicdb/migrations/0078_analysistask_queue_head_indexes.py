# The three indexes behind the head of the queue (§ views._lease_head_pages):
# the lease reads each account's first waiting rows, in the order the
# fair-share window walks them, instead of summing the window over every live
# row (about 17k rows and as many position probes per lease, 2-5 s under load
# on 25-Nov-2026).
#
#   atomic_task_queue_author     (requested_by, background, queue_seq, id)
#       WHERE source = 'USER' AND state = 'PENDING' AND lane_account = ''
#   atomic_task_queue_lane       (lane_account, background, queue_seq, id)
#       WHERE source = 'USER' AND state = 'PENDING' AND lane_account <> ''
#   atomic_task_queue_irregular  (id)
#       WHERE source = 'USER' AND state IN ('PENDING', 'LEASED')
#         AND (backers > 0 OR lane_account <> '' OR requested_by = '')
#
# The predicates are the text of live_request.QUEUE_*_WHERE, which the queries
# use verbatim so PostgreSQL can match them.  atomicdb_analysistask holds
# millions of rows, so on PostgreSQL the indexes are built CONCURRENTLY,
# outside a transaction (``atomic = False``), while the lease and the ingest
# keep writing.  ``IF NOT EXISTS`` lets the migration do nothing where
# production already received them by hand.  Django state gets the plain
# ``AddIndex``.  SQLite builds the same partial indexes, without CONCURRENTLY
# (same pattern as 0073 and 0077).

from django.db import migrations, models

INDICES = (
    ('atomic_task_queue_author', '(requested_by, background, queue_seq, id)',
     "source = 'USER' AND state = 'PENDING' AND lane_account = ''"),
    ('atomic_task_queue_lane', '(lane_account, background, queue_seq, id)',
     "source = 'USER' AND state = 'PENDING' AND lane_account <> ''"),
    ('atomic_task_queue_irregular', '(id)',
     "source = 'USER' AND state IN ('PENDING', 'LEASED') "
     "AND (backers > 0 OR lane_account <> '' OR requested_by = '')"),
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
        ('atomicdb', '0077_analysistask_served_indexes'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name='analysistask',
                    index=models.Index(
                        condition=models.Q(
                            ('lane_account', ''), ('source', 'USER'),
                            ('state', 'PENDING')),
                        fields=['requested_by', 'background', 'queue_seq',
                                'id'],
                        name='atomic_task_queue_author'),
                ),
                migrations.AddIndex(
                    model_name='analysistask',
                    index=models.Index(
                        condition=models.Q(
                            ('source', 'USER'), ('state', 'PENDING'),
                            models.Q(('lane_account', ''), _negated=True)),
                        fields=['lane_account', 'background', 'queue_seq',
                                'id'],
                        name='atomic_task_queue_lane'),
                ),
                migrations.AddIndex(
                    model_name='analysistask',
                    index=models.Index(
                        condition=models.Q(
                            ('source', 'USER'),
                            ('state__in', ['PENDING', 'LEASED']),
                            models.Q(('backers__gt', 0),
                                     models.Q(('lane_account', ''),
                                              _negated=True),
                                     ('requested_by', ''),
                                     _connector='OR')),
                        fields=['id'],
                        name='atomic_task_queue_irregular'),
                ),
            ],
            database_operations=[
                migrations.RunPython(crear, borrar),
            ],
        ),
    ]
