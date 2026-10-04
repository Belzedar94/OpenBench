"""When a worker last DELIVERED, which is not when it was last SEEN.

The community voted contributor lanes in on 10 August, and a lane is earned by
plugging in CPU.  The only evidence the server has today is
``WorkerPing.last_seen``, which is ``auto_now`` and therefore moves on any
authenticated call: a process that polls ``/api/lease`` in a loop and never
returns an analysis is indistinguishable from a machine that is actually
searching.  That gap is survivable while the only thing it unlocks is the rung
selector (§ ``depth.may_choose``); it is not survivable when it hands out a
share of the fleet.  ``last_result_at`` is written where a result is known to
have landed, in ``views.api_submit``, and the lane predicate moves onto it once
the column has a window's worth of history behind it.

NULL EVERYWHERE, AND THAT IS THE POINT.  Every row that exists today means "has
not delivered since this column existed", which is true, rather than a
backfilled timestamp that would assert deliveries nobody recorded.  It is also
why nothing changes on deploy: the predicate still reads ``last_seen`` in this
release, so no lane opens or closes because of this migration.

COST.  One additive nullable column with an index, on a table of tens of rows.
No ``AlterField``, so nothing rebuilds ``atomicdb_workerping`` (§ 0038, where
the reason that matters is written down).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0043_analysis_pass_history'),
    ]

    operations = [
        migrations.AddField(
            model_name='workerping',
            name='last_result_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
