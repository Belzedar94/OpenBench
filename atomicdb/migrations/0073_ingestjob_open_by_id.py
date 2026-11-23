# El reclamo de la cola de ingesta (``ingest_queue.claim_next`` y
# ``ready_jobs``) filtra por estado con un OR y ordena por id.  Con millones de
# trabajos DONE el planificador escogia recorrer la pkey entera (4,9M filas,
# mas de 30 s por reclamo) o un bitmap sobre ``atomic_ingest_ready`` con
# cientos de miles de entradas viejas (4,6 s).  Un indice parcial sobre id con
# solo las filas abiertas (PENDING y PROCESSING) deja el reclamo en 0,4 ms: el
# indice mide lo que mide la cola viva, no la historia.
#
# Produccion lo recibio a mano y CONCURRENTLY el 23-nov-2026, asi que la
# migracion tiene que poder no hacer nada: el estado de Django recibe el
# AddIndex normal (``Meta.indexes`` y ``makemigrations`` quedan de acuerdo) y
# la base recibe ``IF NOT EXISTS``.  En Postgres va CONCURRENTLY para no
# bloquear la cola si alguna base todavia no lo tiene, y eso exige correr
# fuera de transaccion (``atomic = False``).  SQLite, donde viven los tests,
# tambien entiende indices parciales: se crea igual, sin CONCURRENTLY.

from django.db import migrations, models

INDICE = 'atomic_ingest_open_by_id'
CONDICION = "state IN ('PENDING', 'PROCESSING')"


def crear_indice(apps, schema_editor):
    concurrente = ('CONCURRENTLY '
                   if schema_editor.connection.vendor == 'postgresql' else '')
    schema_editor.execute(
        f'CREATE INDEX {concurrente}IF NOT EXISTS {INDICE} '
        f'ON atomicdb_ingestjob (id) WHERE {CONDICION}'
    )


def borrar_indice(apps, schema_editor):
    concurrente = ('CONCURRENTLY '
                   if schema_editor.connection.vendor == 'postgresql' else '')
    schema_editor.execute(f'DROP INDEX {concurrente}IF EXISTS {INDICE}')


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('atomicdb', '0072_opening_routes'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name='ingestjob',
                    index=models.Index(
                        condition=models.Q(
                            ('state__in', ['PENDING', 'PROCESSING'])),
                        fields=['id'], name=INDICE),
                ),
            ],
            database_operations=[
                migrations.RunPython(crear_indice, borrar_indice),
            ],
        ),
    ]
