# A worker can have its own serving mode and campaign, and an account a
# campaign for all its workers.  A new empty table and a nullable column:
# every existing account and worker keeps today's behaviour.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0058_propagationseed'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkerServing',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account', models.CharField(max_length=64)),
                ('machine', models.CharField(max_length=64)),
                ('mode', models.CharField(blank=True, default='', max_length=8)),
                ('campaign', models.IntegerField(blank=True, null=True)),
                ('updated', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddField(
            model_name='contributorpref',
            name='serve_campaign',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='workerserving',
            constraint=models.UniqueConstraint(fields=('account', 'machine'), name='uniq_worker_serving'),
        ),
    ]
