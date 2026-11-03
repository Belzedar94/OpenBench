# The "analysis ready" notice becomes a per-account setting.  On by default,
# so every existing row, and every account without one, keeps the notices.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0052_watched_keys'),
    ]

    operations = [
        migrations.AddField(
            model_name='contributorpref',
            name='notify_ready',
            field=models.BooleanField(default=True),
        ),
    ]
