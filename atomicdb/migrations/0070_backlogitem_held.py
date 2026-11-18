# A fourth state for a community backlog row: HELD, kept on the list but out
# of the box (clear wins under +33, § atomicdb.backlog.CLEAR_WINS_TIERS).
# Only the choices change: no column, no default and no data touched.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0069_workerping_worker_build'),
    ]

    operations = [
        migrations.AlterField(
            model_name='backlogitem',
            name='state',
            field=models.CharField(choices=[('open', 'Open'),
                                            ('adopted', 'Adopted'),
                                            ('dropped', 'Dropped'),
                                            ('held', 'Held')],
                                   default='open', max_length=8),
        ),
    ]
