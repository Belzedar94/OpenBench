"""Columna de foto para los abiertos SATURADOS (invariante 6).

Un ``UNKNOWN`` con pn o dn en ``PROOF_INFINITY`` es el sintoma visible de un
ciclo que se realimento (caso Eclipsia, 6-ago): el frente lo excluye con razon
y hasta hoy lo excluia EN SILENCIO.  La columna existe por lo mismo que la
mediana de dn de al lado: los ``ProofNode`` se reescriben en el sitio, asi que
sin foto no hay "antes" contra el que medir si el barrido de re-baseline
(``recascade_proof``) converge o la poblacion vuelve a crecer.

Un ``AddField`` con default a cero: sin backfill, sin bloqueo que valga la
pena mencionar, y en las DOS bases como todas las ordenes de esquema de este
proyecto:

    python manage.py migrate atomicdb --database atomicdb
    python manage.py migrate atomicdb --database default
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0038_position_updated_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='progresssnapshot',
            name='frontier_saturated',
            field=models.BigIntegerField(default=0),
        ),
    ]
