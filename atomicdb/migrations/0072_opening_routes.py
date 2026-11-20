# Declared move orders of named positions (§ atomicdb.opening_routes): a new
# table, one row per position, and the line a suggestion may carry.  Both start
# empty; nothing existing is touched.  The suggestion table is a few hundred
# rows, so adding a column with a default is instant.

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0071_opening_name_hide'),
    ]

    operations = [
        migrations.CreateModel(
            name='OpeningRoute',
            fields=[
                ('position_key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('line', models.CharField(max_length=384)),
                ('line_san', models.CharField(blank=True, default='', max_length=1024)),
                ('set_by', models.CharField(blank=True, default='', max_length=64)),
                ('set_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
        migrations.AddField(
            model_name='openingnamesuggestion',
            name='line',
            field=models.CharField(blank=True, default='', max_length=384),
        ),
    ]
