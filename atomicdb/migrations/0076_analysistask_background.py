# ``priority=low`` on the request API: a background request, served after the
# same account's normal requests (§ AnalysisTask.background).  False on every
# existing row, so nothing changes until a script asks for it.
#
# On PostgreSQL 11+ ``ADD COLUMN ... DEFAULT false NOT NULL`` stores the
# default in the catalog and rewrites nothing; the ``DROP DEFAULT`` Django
# issues next is catalog-only too.  Instant on atomicdb_analysistask whatever
# its size, after the brief ACCESS EXCLUSIVE lock both statements take.  No
# index: the lease query never filters on it, it only orders by it inside the
# fair-share window.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0075_drop_unused_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistask',
            name='background',
            field=models.BooleanField(default=False),
        ),
    ]
