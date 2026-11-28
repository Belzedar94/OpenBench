# Real-parent pointers (§ atomicdb.real_parent): a new, empty table, one row
# per position the regret Dijkstra settles.  Nothing existing is touched, so
# this is a plain CREATE TABLE on both engines.

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0075_drop_unused_indexes'),
    ]

    operations = [
        migrations.CreateModel(
            name='RealParent',
            fields=[
                ('position_key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('parent_key', models.CharField(max_length=64)),
                ('move_uci', models.CharField(max_length=8)),
                ('updated', models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
    ]
