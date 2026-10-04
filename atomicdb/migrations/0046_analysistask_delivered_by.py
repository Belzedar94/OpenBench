# The authenticated account that delivered a completed task. Machine names
# are worker-chosen and two accounts can announce the same one (it happened,
# 16 August), so machine ownership is a guess and this column is a fact.
# No backfill on purpose: an authorship nobody recorded would be invented.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0045_task_lane_account'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistask',
            name='delivered_by',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
