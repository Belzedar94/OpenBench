"""Avisos en-pagina de "tu analisis ya esta servido".

Una tabla nueva y nada mas: no toca ninguna columna existente, asi que la
marcha atras es un ``DROP TABLE`` limpio y lo unico que se pierde son los
avisos sin leer.  El hecho que describen — el resultado en el arbol — no vive
aqui y no se va con ellos.

``username`` es texto y no una FK a ``auth.User`` a proposito: con el split
activo esta tabla vive en otro fichero SQLite, donde ``auth_user`` no existe,
y el router prohibe la relacion.  Ver el modelo para el precio exacto.

DEPENDE DE UNA MIGRACION QUE NO ESTA EN ESTA RAMA.  ``0028_analysistask_route``
entro en spell-runner mientras esto se escribia, y dos hojas con el mismo
numero dejan el grafo sin cabeza ("multiple leaf nodes").  Asi que esta se
numera 0029 y cuelga de aquella, escrito a mano: hasta que las dos ramas se
junten, esta sola NO carga — el grafo se queja de un padre que no existe.  El
orden entre ambas da igual (tocan tablas distintas); lo que no da igual es que
al final haya UNA hoja.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0028_analysistask_route'),
    ]

    operations = [
        migrations.CreateModel(
            name='RequestNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(max_length=64)),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('seen', models.BooleanField(default=False)),
                ('position', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to='atomicdb.position')),
                ('task', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='notifications', to='atomicdb.analysistask')),
            ],
            options={
                'indexes': [models.Index(fields=['username', 'seen'], name='atomic_notify_unseen')],
            },
        ),
        migrations.AddConstraint(
            model_name='requestnotification',
            constraint=models.UniqueConstraint(fields=('task',), name='uniq_notification_per_task'),
        ),
    ]
