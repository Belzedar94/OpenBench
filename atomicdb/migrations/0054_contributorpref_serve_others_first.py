# "Serve others first" becomes a per-account setting.  Off by default, so
# every existing row, and every account without one, keeps its workers'
# own-first affinity.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0053_contributorpref_notify_ready'),
    ]

    operations = [
        migrations.AddField(
            model_name='contributorpref',
            name='serve_others_first',
            field=models.BooleanField(default=False),
        ),
    ]
