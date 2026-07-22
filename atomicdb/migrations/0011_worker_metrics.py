from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('atomicdb', '0010_position_proof')]

    operations = [
        migrations.AddField(
            model_name='analysistask',
            name='elapsed_seconds',
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='lease_heartbeat_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='lease_token',
            field=models.CharField(default='', max_length=64),
        ),
        migrations.AddField(
            model_name='workerping',
            name='current_task_id',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='workerping',
            name='last_nps',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='workerping',
            name='nps_updated',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddIndex(
            model_name='analysistask',
            index=models.Index(fields=['state', 'completed'],
                               name='atomic_task_state_done'),
        ),
    ]
