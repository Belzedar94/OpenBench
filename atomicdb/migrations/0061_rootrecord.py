# The record history of the start position's best value (``RootRecord``).
# A new, empty table: nothing existing is touched.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0060_analysistask_requested_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='RootRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=64)),
                ('value', models.IntegerField()),
                ('status', models.CharField(default='UNKNOWN',
                                            max_length=10)),
                ('at', models.DateTimeField()),
                ('captured_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['key', 'at'],
                'indexes': [models.Index(fields=['key', 'at'],
                                         name='atomicdb_rootrec_key_at')],
            },
        ),
    ]
