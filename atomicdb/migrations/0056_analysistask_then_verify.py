# "Request + verify PV": a request can ask for its line to be verified once
# the search lands.  Off by default, so every existing row is unchanged.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0055_contributorpref_serve_order'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistask',
            name='then_verify',
            field=models.BooleanField(default=False),
        ),
    ]
