# Opening names go from 60 to 80 characters (models.OPENING_NAME_MAX_CHARS):
# real names such as "Villager Defense, Knight Attack By Sea: Bremerhaven
# Variation" (61) did not fit.  Every column that stores a name widens with
# it: the suggestion's proposed and previous names, the vote's name and the
# review flag's name.  On PostgreSQL widening a varchar is a catalogue-only
# change, with no table rewrite.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0078_analysistask_queue_head_indexes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='openingnamesuggestion',
            name='proposed_name',
            field=models.CharField(max_length=80),
        ),
        migrations.AlterField(
            model_name='openingnamesuggestion',
            name='previous_name',
            field=models.CharField(blank=True, default='', max_length=80),
        ),
        migrations.AlterField(
            model_name='openingnamevote',
            name='name',
            field=models.CharField(max_length=80),
        ),
        migrations.AlterField(
            model_name='openingnameflag',
            name='name',
            field=models.CharField(max_length=80),
        ),
    ]
