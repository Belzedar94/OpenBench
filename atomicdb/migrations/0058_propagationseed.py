# The durable queue of the climbs a landing still owes its ancestors
# (see atomicdb/propagation_queue.py).  A new table and nothing else: with
# ATOMICDB_DEFER_PROPAGATION off (the default) it stays empty.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0057_contributorpref_serve_for'),
    ]

    operations = [
        migrations.CreateModel(
            name='PropagationSeed',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(blank=True, default='', max_length=16)),
                ('payload', models.JSONField()),
                ('attempts', models.IntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField(null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('position', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='propagation_seeds', to='atomicdb.position')),
            ],
            options={
                'indexes': [models.Index(fields=['next_attempt_at', 'id'], name='atomic_propagation_ready')],
            },
        ),
    ]
