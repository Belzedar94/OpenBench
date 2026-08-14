"""Dos peticiones de la comunidad sobre la cola, y lo que hace falta guardar.

ADELANTAR LO PROPIO (Wolfram): "some checkmark to push smth to the front of my
own queue, not to the back, would be nice".  ``queue_seq`` es el sitio de una
tarea DENTRO de la cola de quien la pidio.  Cero en todas las filas, que es lo
que deja esta migracion, significa "manda el ``id``": el reparto justo ordena
exactamente como ordenaba (§ live_request.fair_share) y desplegar esto no mueve
ni una peticion de sitio.  Solo escribe el boton, y solo sobre filas de la
cuenta que lo pulsa.

MAS GENTE PIDIENDO LO MISMO (Eclipsia): "multiple people asking to analyse the
same position should give it more precedence".  ``also_requested_by`` guarda a
los que se sumaron y ``backers`` cuenta cuantos son, desnormalizado porque el
orden de servicio lo lee en SQL y contar un JSON no se escribe igual en los dos
motores.  Lista vacia y cero en todo lo que ya existe: una peticion que pidio
una sola persona sigue valiendo lo que valia.

Y EL AVISO, QUE ERA IMPOSIBLE DE DAR.  La unicidad de ``RequestNotification``
colgaba de ``task`` a secas — una tarea, un aviso — asi que el segundo
peticionario no podia recibir el suyo ni queriendo: la fila del primero ocupaba
el hueco.  Pasa a ser (tarea, nombre), que es lo que de verdad no debe
repetirse.  Sobre lo ya escrito no afirma menos: ningun aviso existente pierde
su dedup, porque hasta hoy no habia dos del mismo par.

COSTE.  Tres columnas ADITIVAS con default constante y un indice unico parcial
que se rehace.  En PostgreSQL — donde vive la base de produccion — anadir una
columna con default constante es metadato desde la 11, y el indice de avisos se
recrea sobre una tabla pequena.  Ninguna operacion reconstruye ``atomicdb_
analysistask``: no hay ``AlterField`` aqui, que es la que costaria copiar la
tabla entera (§ 0038, donde esta escrito por que).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0039_snapshot_saturated'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='requestnotification',
            name='uniq_notification_per_task',
        ),
        migrations.AddField(
            model_name='analysistask',
            name='also_requested_by',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='backers',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='queue_seq',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name='requestnotification',
            constraint=models.UniqueConstraint(condition=models.Q(('task__isnull', False)), fields=('task', 'username'), name='uniq_notification_per_task'),
        ),
    ]
