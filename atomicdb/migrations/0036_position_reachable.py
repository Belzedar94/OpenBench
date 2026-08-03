"""``reachable``: la pregunta del Dijkstra global, guardada como columna.

QUE COMPRA.  "¿Cuelga este nodo de la raiz?" era una respuesta que solo
existia mientras corria el Dijkstra completo, y por eso el selector tenia que
recorrer el grafo entero para poder distinguir un nodo lejano de un cajetin
FEN suelto.  Con la columna, el selector acotado lee la respuesta del mismo
lote que ya paga y deja de necesitar la foto global
(§ docs/selector-incremental.md).

POR QUE EL INDICE ES PARCIAL.  En una base sana casi todo cuelga de la raiz;
un indice completo sobre un booleano tan sesgado es una copia de la tabla para
responder "dame los pocos raros".  Con ``condition=reachable=False`` el indice
mide lo que mide la respuesta, y es justo el lado que se consulta: lo que le
queda por marcar al backfill y cuantos sueltos hay.

DEFAULT False, Y EL ORDEN QUE OBLIGA.  Las filas existentes nacen sin marcar
y las siembra ``backfill_reachable`` (BFS desde la raiz, por lotes).  Sin
marcar NO significa "barato": significa el precio de cajetin FEN suelto, que
es el GENEROSO (``DISCONNECTED_REGRET``, 5, frente a los 30 del nodo lejano
pero conectado).  Por eso el despliegue va en este orden y no en otro:
migracion, backfill, y solo entonces ``ATOMICDB_SELECTOR_V2``.  La columna sin
sembrar no rompe nada mientras el conmutador siga apagado — el camino v1 ni la
mira.

Es un ``ADD COLUMN`` con default y un ``CREATE INDEX`` sobre la parte pequena:
barata en las dos bases.  Se aplica a mano y con ``--database`` explicito en
cada una — este proyecto tiene dos y ninguna orden de esquema se lanza aqui
sin decir sobre cual.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0035_analysistask_requester_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='position',
            name='reachable',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='position',
            index=models.Index(condition=models.Q(('reachable', False)), fields=['reachable'], name='atomic_pos_unreached'),
        ),
    ]
