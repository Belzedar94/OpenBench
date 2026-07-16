from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0004_test_games_bigint'),
    ]

    operations = [
        migrations.CreateModel(
            name='DatagenProducerArtifact',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sha256', models.CharField(max_length=64, unique=True)),
                ('bytes', models.BigIntegerField()),
                ('created', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='datagenchunk',
            name='producer_bytes',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='datagenchunk',
            name='producer_commit',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
        migrations.AddField(
            model_name='datagenchunk',
            name='producer_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
