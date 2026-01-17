from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='test',
            name='variant',
            field=models.CharField(default='chess', max_length=64),
        ),
        migrations.AddField(
            model_name='test',
            name='variant_path',
            field=models.CharField(blank=True, default='', max_length=256),
        ),
        migrations.AddField(
            model_name='test',
            name='match_runner',
            field=models.CharField(choices=[('FASTCHESS', 'FASTCHESS'), ('VARIANTFISHTEST', 'VARIANTFISHTEST')], default='FASTCHESS', max_length=32),
        ),
    ]
