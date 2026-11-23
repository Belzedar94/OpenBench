# Indices que nadie lee, fuera (23-nov-2026).  El disco es el limite del
# servidor y cada uno de estos se paga en espacio y en cada escritura.
#
# Criterio: cero lecturas (o ninguna desde el 12-nov) en ``pg_stat_user_indexes``
# desde que el cluster existe (21-oct; las estadisticas sobrevivieron al
# reinicio limpio del 13-nov) Y ningun camino de codigo, comando o timer que
# los necesite.  Medido en produccion el 23-nov:
#
#   atomic_pos_queue        6,5 GiB  4.212 lecturas, la ultima el 12-nov.  Las
#                                    dos consultas para las que se creo (el
#                                    arriendo de reserva y "Up next") las
#                                    resuelve el planificador por el indice de
#                                    ``priority``; con o sin este, el plan es
#                                    el mismo.
#   atomic_pos_unreached    1,3 GiB  0.  Solo lo usaria el recuento final de
#                                    ``backfill_reachable``, un comando manual
#                                    de dias que ya recorre la tabla entera.
#   atomic_prov_engine      1,6 GiB  0.  ``ClosureProvenance`` solo se escribe
#   atomic_prov_account     0,4 GiB  0.  (upsert por la clave primaria).
#   ..._key_..._like        1,1 GiB  El gemelo ``varchar_pattern_ops`` de la
#                                    clave primaria: solo sirve a ``LIKE
#                                    'prefijo%'`` y nadie lo hace.  Django no
#                                    lo modela en el estado, asi que solo se
#                                    borra en la base (y solo en Postgres).
#   atomic_ingest_ready     0,4 GiB  21, el ultimo el 18-nov: lo sustituyo el
#                                    parcial ``atomic_ingest_open_by_id``
#                                    (0073).
#   atomic_apireq_account   0,2 GiB  0.  Eran del tope horario de la API, que
#   atomic_apireq_ip        0,1 GiB  0.  el propietario retiro el 17-ago.
#   proofnode campaign_id   0,3 GiB  Prefijo de ``uniq_proof_node``
#                                    (campaign, position).
#   requestnotification     0,1 GiB  0.  Lo cubre ``uniq_notification_per_task``
#     task_id                        (task, username) WHERE task IS NOT NULL.
#
# Se quedan, aunque se lean poco: ``atomic_pos_updated`` (modo delta del
# selector, completado de cobertura, digest diario de Discord) y
# ``atomic_proof_dn`` (el frente de prueba lo usa desde esta misma version).
#
# Postgres: ``DROP INDEX CONCURRENTLY IF EXISTS``, fuera de transaccion
# (``atomic = False``), para no bloquear ingesta ni arriendo.  La vuelta atras
# los recrea ``CONCURRENTLY IF NOT EXISTS`` con su definicion exacta.  SQLite
# (tests) hace lo mismo sin CONCURRENTLY.

from django.db import migrations, models
import django.db.models.deletion


# (nombre, tabla, definicion de columnas, condicion o None, solo_postgres)
INDICES = (
    ('atomic_pos_queue', 'atomicdb_position', '(status, priority DESC)',
     None, False),
    ('atomic_pos_unreached', 'atomicdb_position', '(reachable)',
     'NOT reachable', False),
    ('atomic_prov_engine', 'atomicdb_closureprovenance',
     '(engine_sha, closed_at)', None, False),
    ('atomic_prov_account', 'atomicdb_closureprovenance',
     '(account, closed_at)', None, False),
    ('atomicdb_closureprovenance_key_084ef0f7_like',
     'atomicdb_closureprovenance', '(key varchar_pattern_ops)', None, True),
    ('atomic_ingest_ready', 'atomicdb_ingestjob',
     '(state, next_attempt_at, id)', None, False),
    ('atomic_apireq_account', 'atomicdb_apirequestlog', '(account, created)',
     None, False),
    ('atomic_apireq_ip', 'atomicdb_apirequestlog', '(ip, created)', None,
     False),
    ('atomicdb_proofnode_campaign_id_9bcb7316', 'atomicdb_proofnode',
     '(campaign_id)', None, False),
    ('atomicdb_requestnotification_task_id_28f3c007',
     'atomicdb_requestnotification', '(task_id)', None, False),
)


def _concurrente(schema_editor):
    return ('CONCURRENTLY '
            if schema_editor.connection.vendor == 'postgresql' else '')


def borrar(apps, schema_editor):
    postgres = schema_editor.connection.vendor == 'postgresql'
    for nombre, _tabla, _columnas, _condicion, solo_pg in INDICES:
        if solo_pg and not postgres:
            continue
        schema_editor.execute(
            f'DROP INDEX {_concurrente(schema_editor)}IF EXISTS {nombre}')


def recrear(apps, schema_editor):
    postgres = schema_editor.connection.vendor == 'postgresql'
    for nombre, tabla, columnas, condicion, solo_pg in INDICES:
        if solo_pg and not postgres:
            continue
        donde = f' WHERE {condicion}' if condicion else ''
        schema_editor.execute(
            f'CREATE INDEX {_concurrente(schema_editor)}IF NOT EXISTS '
            f'{nombre} ON {tabla} {columnas}{donde}')


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('atomicdb', '0074_progress_breakdown_nullable'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveIndex(model_name='position',
                                       name='atomic_pos_queue'),
                migrations.RemoveIndex(model_name='position',
                                       name='atomic_pos_unreached'),
                migrations.RemoveIndex(model_name='closureprovenance',
                                       name='atomic_prov_engine'),
                migrations.RemoveIndex(model_name='closureprovenance',
                                       name='atomic_prov_account'),
                migrations.RemoveIndex(model_name='ingestjob',
                                       name='atomic_ingest_ready'),
                migrations.RemoveIndex(model_name='apirequestlog',
                                       name='atomic_apireq_account'),
                migrations.RemoveIndex(model_name='apirequestlog',
                                       name='atomic_apireq_ip'),
                migrations.AlterField(
                    model_name='proofnode',
                    name='campaign',
                    field=models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='nodes', to='atomicdb.proofcampaign'),
                ),
                migrations.AlterField(
                    model_name='requestnotification',
                    name='task',
                    field=models.ForeignKey(
                        db_index=False, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='notifications',
                        to='atomicdb.analysistask'),
                ),
            ],
            database_operations=[
                migrations.RunPython(borrar, recrear),
            ],
        ),
    ]
