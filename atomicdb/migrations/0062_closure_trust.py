# The closure trust gate: trusted accounts, pending claims, and who closed
# each position.  Three new, empty tables; the change to ``Position.proof``
# only adds a choice (``TB_LINE``) and emits no SQL.  Nothing existing is
# touched.

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0061_rootrecord'),
    ]

    operations = [
        migrations.CreateModel(
            name='TrustedAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(max_length=64, unique=True)),
                ('note', models.CharField(blank=True, default='', max_length=200)),
                ('added', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['username'],
            },
        ),
        migrations.AlterField(
            model_name='position',
            name='proof',
            field=models.CharField(choices=[('ANDOR', 'Andor'), ('ENGINE', 'Engine'), ('DISPUTED', 'Disputed'), ('TB_LINE', 'Tb Line')], max_length=8, null=True),
        ),
        migrations.CreateModel(
            name='PendingClaim',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('position_key', models.CharField(max_length=64)),
                ('parent_key', models.CharField(max_length=64)),
                ('task_id', models.BigIntegerField(null=True)),
                ('account', models.CharField(max_length=64)),
                ('engine_sha', models.CharField(blank=True, default='', max_length=64)),
                ('kind', models.CharField(choices=[('MATE', 'Mate'), ('TB', 'Tb')], max_length=4)),
                ('winner', models.CharField(max_length=5)),
                ('pv', models.TextField()),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('resolved', models.DateTimeField(null=True)),
                ('resolution', models.CharField(blank=True, choices=[('', 'Open'), ('CONFIRMED', 'Confirmed'), ('CONTRADICTED', 'Contradicted'), ('REFUTED', 'Refuted')], default='', max_length=12)),
            ],
            options={
                'indexes': [models.Index(fields=['position_key', 'resolved'], name='atomic_claim_pos_open')],
            },
        ),
        migrations.CreateModel(
            name='ClosureProvenance',
            fields=[
                ('key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('closed_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('status', models.CharField(max_length=10)),
                ('closure', models.CharField(blank=True, default='', max_length=8)),
                ('proof', models.CharField(blank=True, default='', max_length=8)),
                ('source', models.CharField(blank=True, default='', max_length=8)),
                ('task_id', models.BigIntegerField(null=True)),
                ('account', models.CharField(blank=True, default='', max_length=64)),
                ('engine_sha', models.CharField(blank=True, default='', max_length=64)),
            ],
            options={
                'indexes': [models.Index(fields=['account', 'closed_at'], name='atomic_prov_account'), models.Index(fields=['engine_sha', 'closed_at'], name='atomic_prov_engine')],
            },
        ),
    ]
