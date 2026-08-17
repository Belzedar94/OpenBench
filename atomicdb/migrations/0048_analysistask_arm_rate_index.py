# El techo de gasto por hora de los brazos del walker pregunta, antes de cada
# compra, cuantas tareas creo ESE brazo en los ultimos sesenta minutos
# (§ ingest, presupuesto HORARIO de los brazos).  Con el indice suelto de
# ``arm`` eso es bajar por la particion ENTERA del brazo — todo lo que compro
# desde que existe — para descartar casi todo por fecha, y la pregunta corre
# en el camino de la ingesta.  El compuesto la convierte en una lectura de
# rango: misma leccion que ``atomic_event_recert_key``, probe y no scan.
#
# Va como SQL en vez de ``Meta.indexes`` por lo mismo que aquel: produccion lo
# puede crear antes con CONCURRENTLY, y entonces la migracion tiene que ser
# capaz de no hacer nada.  Aqui si vale para los dos motores — no hay ninguna
# expresion dentro, solo dos columnas — asi que la base de los tests tambien
# lo lleva y lo que se prueba es lo que corre.

from django.db import migrations

INDICE = 'atomic_task_arm_created'


def crear_indice(apps, schema_editor):
    schema_editor.execute(
        f'CREATE INDEX IF NOT EXISTS {INDICE} '
        'ON atomicdb_analysistask (arm, created)'
    )


def borrar_indice(apps, schema_editor):
    schema_editor.execute(f'DROP INDEX IF EXISTS {INDICE}')


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0047_dbevent_recert_key_index'),
    ]

    operations = [
        migrations.RunPython(crear_indice, borrar_indice),
    ]
