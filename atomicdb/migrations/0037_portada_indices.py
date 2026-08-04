"""Los dos indices que la portada necesitaba y no tenia.

``atomic_pos_queue`` (status, priority DESC).  La cima de la cola se pide dos
veces en el sitio y siempre igual: ``status='UNKNOWN'`` ordenado por
``-priority`` y cortado en unas pocas filas — el arriendo (``next_tasks``,
4·n) y el widget "Up next" de la portada (12).  Con los dos indices sueltos
que habia, el motor bajaba por el de ``priority`` descartando fila a fila lo
que no fuera UNKNOWN, y lo cerrado CONSERVA su prioridad porque el refresco
solo repuntua lo abierto: cada cierre en la zona alta deja una entrada mas que
saltar, y no se recupera nunca.  Con el compuesto, doce filas pintadas son
doce entradas leidas.

``atomic_event_kind_ts`` (kind, ts).  Es la unica forma en la que se agrega
``atomicdb_dbevent``: una clase de evento dentro de una ventana — los cierres
de 24h de la portada y los doce ``COUNT`` de atribucion.  El indice suelto de
``ts`` obligaba a recorrer el rango entero mirando el ``kind`` de cada fila, y
el cierre es una minoria de lo que se escribe ahi.  El de ``ts`` se queda: el
feed de hitos pide las doce ultimas de casi cualquier clase, y para eso el
orden global por fecha ES el indice correcto.

Son dos ``CREATE INDEX`` y nada mas: ni una columna nueva, ni un dato que
reescribir, ni orden de despliegue que respetar.  El precio corriente es el de
cualquier indice — una entrada mas que mantener por escritura — y en las dos
tablas la escritura es un append o un ``UPDATE`` de una fila, nunca un
barrido.

ESTE PROYECTO TIENE DOS BASES Y NINGUNA ORDEN DE ESQUEMA SE LANZA SIN DECIR
SOBRE CUAL.  Al desplegar hacen falta las dos, explicitas:

    python manage.py migrate atomicdb --database atomicdb
    python manage.py migrate atomicdb --database default

La segunda no sobra: el esquema viejo de ``default`` se mantiene al dia como
SOMBRA DE VUELTA ATRAS (§ OpenSite.db_routers), y el router deja migrar la app
en las dos.  Saltarsela deja a ``default`` con la migracion apuntada como
aplicada pero sin los indices creados, que es la unica forma de que una vuelta
atras aterrice sobre un esquema que Django cree correcto y no lo es.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0036_position_reachable'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='dbevent',
            index=models.Index(fields=['kind', 'ts'], name='atomic_event_kind_ts'),
        ),
        migrations.AddIndex(
            model_name='position',
            index=models.Index(fields=['status', '-priority'], name='atomic_pos_queue'),
        ),
    ]
