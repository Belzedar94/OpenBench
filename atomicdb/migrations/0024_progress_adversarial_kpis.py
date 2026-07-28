"""Snapshot columns for the adversarial explorer package.

Three groups, and only the first one is irreplaceable: ``ProofNode`` rows are
overwritten in place by every cascade, so the dn distribution of an hour stops
existing when the hour does.  The other two are counted from durable events and
request logs and could be recomputed later; they live here so one row is one
complete observation instead of a join across three tables.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0023_survive50_disproof'),
    ]

    operations = [
        migrations.AddField(
            model_name='progresssnapshot',
            name='frontier_and_nodes',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='frontier_dn_median',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='frontier_dn_thin',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='closures_user',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='closures_fill',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='closures_auto',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='closures_solve',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='closures_none',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='human_close_median_seconds',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='progresssnapshot',
            name='human_close_samples',
            field=models.BigIntegerField(default=0),
        ),
    ]
