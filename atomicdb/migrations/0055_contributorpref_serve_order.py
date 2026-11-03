# "Serve others first" becomes a choice of three: own first (the default),
# queue order, or others first.  Accounts that had it on keep it as
# "others".

from django.db import migrations, models


def carry_over(apps, schema_editor):
    ContributorPref = apps.get_model('atomicdb', 'ContributorPref')
    ContributorPref.objects.using(schema_editor.connection.alias).filter(
        serve_others_first=True).update(serve_order='others')


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0054_contributorpref_serve_others_first'),
    ]

    operations = [
        migrations.AddField(
            model_name='contributorpref',
            name='serve_order',
            field=models.CharField(default='own', max_length=8),
        ),
        migrations.RunPython(carry_over, migrations.RunPython.noop),
    ]
