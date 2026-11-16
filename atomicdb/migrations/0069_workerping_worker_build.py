# The worker build each AtomicDB machine announces: one nullable column on
# WorkerPing, no default and no backfill (NULL means "not announced yet").

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0068_backlog_subscription'),
    ]

    operations = [
        migrations.AddField(
            model_name='workerping',
            name='worker_build',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
