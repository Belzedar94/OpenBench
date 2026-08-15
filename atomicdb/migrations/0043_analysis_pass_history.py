"""Que dijo cada pase, no solo el ultimo.

Peticion de comunidad (Wolfram): "preferably all passes that were done in the
node, as it is practically useful to know which evaluation engine gave at lower
depth".  ``last_analysis`` guarda LINEAS y solo de dos pases: el vigente y el
anterior cuando era mas ancho.  Esto guarda la SERIE, un resumen por pase y sin
una sola PV dentro (§ ``Position.analysis_passes``).

HACIA DELANTE Y SIN RELLENO, y no por comodidad: los pases que nunca se
guardaron no se pueden reconstruir, y una columna rellenada a ojo desde
``visits`` afirmaria profundidades y evaluaciones que no midio nadie.  Las
filas que ya existen empiezan con lista vacia y su serie arranca en el
siguiente pase que reciban; la API lo declara al responder
(``analysis.passes_complete``) en vez de dejar que el que lee lo suponga.

COSTE.  Una columna ADITIVA con default CONSTANTE, que es el caso barato: en
PostgreSQL — donde vive la base de produccion — anadir una columna asi es
metadato desde la 11 y no reescribe ``atomicdb_position``, que es la tabla
grande del proyecto (§ 0038 y 0040, donde esta escrito por que eso importa
aqui).  Ni backfill, ni indice, ni orden de despliegue: se aplica con el sitio
en marcha y el codigo anterior ignora la columna.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0042_api_request_receipts'),
    ]

    operations = [
        migrations.AddField(
            model_name='position',
            name='analysis_passes',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
