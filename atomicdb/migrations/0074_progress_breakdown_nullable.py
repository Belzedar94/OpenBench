# The full position breakdown of a progress snapshot (expanded, closures by
# type, trust classes) needs a scan of the whole position table, 16 to 40
# minutes at 160M rows.  The hourly capture now measures it once every 24
# hours and leaves it NULL in the other rows: "not measured this hour"
# (§ capture_atomicdb_progress.BREAKDOWN_FIELDS).  Existing rows keep their
# numbers.  On Postgres this is DROP NOT NULL, a catalog-only change.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0073_ingestjob_open_by_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='progresssnapshot',
            name='closure_mate_pv',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='closure_minimax',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='closure_tb',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='closure_terminal',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='closure_unclassified',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='positions_expanded',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='trust_andor',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='trust_disputed',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='trust_engine',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='trust_unclassified',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='progresssnapshot',
            name='trust_verified',
            field=models.BigIntegerField(null=True),
        ),
    ]
