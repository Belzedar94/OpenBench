"""Un recibo por llamada atendida de la API de peticion de analisis.

Peticion de comunidad (Wolfram): "is there official API for requesting
analysis".

POR QUE UNA TABLA PROPIA Y NO ``RequestLog``.  El tope es de la PUERTA
programatica, no de la persona: contar las dos juntas dejaria que una tarde de
explorador le agotase el presupuesto al script de esa misma cuenta, que es
justo al reves de la decision que tomo el propietario el 28-jul al quitarle el
limite horario al click.  ``RequestLog`` sigue siendo lo que era, el dedup por
ip+posicion, y no cambia ni una columna.

COSTE.  Una tabla nueva y vacia con dos indices compuestos.  Nada que
reconstruir, nada que rellenar, ningun orden de despliegue: se puede aplicar
con el sitio en marcha, y el codigo anterior a este cambio ni sabe que existe.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0041_cancelled_task_state'),
    ]

    operations = [
        migrations.CreateModel(
            name='ApiRequestLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account', models.CharField(blank=True, default='', max_length=64)),
                ('ip', models.GenericIPAddressField()),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                'indexes': [models.Index(fields=['account', 'created'], name='atomic_apireq_account'), models.Index(fields=['ip', 'created'], name='atomic_apireq_ip')],
            },
        ),
    ]
