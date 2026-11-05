# "Others first" can name the accounts to serve first.  Empty by default,
# so every existing row keeps serving everybody else alike.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0056_analysistask_then_verify'),
    ]

    operations = [
        migrations.AddField(
            model_name='contributorpref',
            name='serve_for',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
