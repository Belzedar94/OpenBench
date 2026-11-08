# When a task last became a person's request.  ``created`` is set once, but a
# withdrawn, absorbed or server-queued row can become a request made now on
# the same row.  A nullable column with no default and no index: existing rows
# stay NULL and are aged from ``created`` as before, and the ALTER is a
# catalogue change on PostgreSQL, not a table rewrite.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0059_worker_serving'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistask',
            name='requested_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
