from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0009_merge_profile_and_datagen_v41'),
    ]

    operations = [
        migrations.AddField(
            model_name='test',
            name='variant_contract',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
