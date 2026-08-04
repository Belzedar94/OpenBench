"""``Position.updated`` indexada: la pregunta del modo delta.

QUE COMPRA.  "¿Que se ha movido desde la ultima pasada?" es la consulta sobre
la que se apoya el selector incremental (§ ingest, ``_delta_keys``), y sin
indice la respuesta se paga recorriendo doce millones y pico de filas — o sea
exactamente el barrido que el modo delta viene a quitar.  Con el indice, la
respuesta mide lo que mide la respuesta.

Y DE PASO ARREGLA UNA QUE YA DOLIA.  ``enqueue_coverage_completion`` pide las
mas recientes con ``order_by('-updated')`` acotado a unas miles de filas, y
eso, sin indice, es ordenar la tabla ENTERA cada pasada del mismo servicio
para quedarse con el principio.

EL PRECIO, Y POR QUE ES DE LOS BARATOS.  Una entrada mas que mantener por
escritura de fila.  ``updated`` es ``auto_now``, o sea monotona: las altas
entran siempre por el extremo derecho del arbol, que es el caso amable de un
B-tree, y no hay reordenacion de paginas por el medio.  Nada que reescribir en
los datos, ningun backfill, ningun orden de despliegue que respetar.

ESTE PROYECTO TIENE DOS BASES Y NINGUNA ORDEN DE ESQUEMA SE LANZA SIN DECIR
SOBRE CUAL.  Al desplegar hacen falta las dos, explicitas:

    python manage.py migrate atomicdb --database atomicdb
    python manage.py migrate atomicdb --database default

La segunda no sobra: el esquema viejo de ``default`` se mantiene al dia como
sombra de vuelta atras (§ OpenSite.db_routers).  Saltarsela deja alli la
migracion apuntada como aplicada y el indice sin crear.

OJO EN POSTGRES.  Un ``CREATE INDEX`` normal toma un ``SHARE`` sobre la tabla
y bloquea las escrituras mientras dura, que sobre esta tabla no es un
instante.  Dos formas de hacerlo sin ventana mala, en orden de preferencia:

  * con el servicio del selector y el procesador de la cola parados, y
    entonces esto tal cual; o
  * a mano y ``CONCURRENTLY``, usando el NOMBRE EXACTO que Django espera —
    ``manage.py sqlmigrate atomicdb 0038`` lo imprime — y despues
    ``migrate atomicdb 0038 --fake --database ...`` en cada base.  Con otro
    nombre, la migracion volveria a crear el indice.

El modo delta no necesita este indice para ser CORRECTO: sin el la consulta da
las mismas filas y solo tarda mas.  No hay ninguna prisa aqui que justifique
una ventana de bloqueo.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0037_portada_indices'),
    ]

    operations = [
        migrations.AlterField(
            model_name='position',
            name='updated',
            field=models.DateTimeField(auto_now=True, db_index=True),
        ),
    ]
