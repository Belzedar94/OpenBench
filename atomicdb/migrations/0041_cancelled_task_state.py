"""Una peticion se puede retirar antes de que la mire ningun motor.

Peticion de comunidad (asfault): "is there a way to cancel requests before they
have started - for when you accidentally request analysis for some already
deeply analysed line".

ESTA MIGRACION NO TOCA LA TABLA.  ``choices`` es un atributo de Django y no una
restriccion de la columna, asi que el ``AlterField`` de aqui abajo compila a
nada: ``sqlmigrate`` lo dice con todas las letras, "(no-op)".  Por eso no hace
falta ninguna de las precauciones que si necesita una columna de verdad sobre
``atomicdb_analysistask``, que es una tabla de millones de filas y donde un
``AlterField`` real costaria copiarla entera (§ 0038 y 0040, donde esta escrito
por que).

Y sobre lo ya escrito no afirma nada nuevo: ninguna fila existente esta en el
estado nuevo, y todo el codigo que lee estados filtra en POSITIVO — PENDING,
LEASED, COMPLETED — asi que hasta que alguien pulse el boton esta base se
comporta exactamente igual que antes.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0040_queue_bump_and_dedupe'),
    ]

    operations = [
        migrations.AlterField(
            model_name='analysistask',
            name='state',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('LEASED', 'Leased'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled')], db_index=True, default='PENDING', max_length=10),
        ),
    ]
