# The proof-directed planner in shadow mode: one row per cycle and the batch
# it would have served.  Two new, empty tables written only by
# ``run_planner --mode shadow``; nothing existing is touched.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0062_closure_trust'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlannerShadowCycle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started', models.DateTimeField(db_index=True)),
                ('finished', models.DateTimeField()),
                ('mode', models.CharField(default='shadow', max_length=8)),
                ('model_version', models.CharField(blank=True, default='', max_length=64)),
                ('root_key', models.CharField(blank=True, default='', max_length=64)),
                ('root_pn', models.FloatField(null=True)),
                ('root_dn', models.FloatField(null=True)),
                ('fleet_nodes_per_day', models.FloatField(null=True)),
                ('picks', models.IntegerField(default=0)),
                ('metrics', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'ordering': ['-started'],
            },
        ),
        migrations.CreateModel(
            name='PlannerShadowPick',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rank', models.IntegerField()),
                ('kind', models.CharField(max_length=4)),
                ('position_key', models.CharField(max_length=64)),
                ('parent_key', models.CharField(blank=True, default='', max_length=64)),
                ('move_uci', models.CharField(blank=True, default='', max_length=8)),
                ('ply', models.IntegerField(default=0)),
                ('pn', models.FloatField(null=True)),
                ('dn', models.FloatField(null=True)),
                ('eval_cp', models.IntegerField(null=True)),
                ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pick_rows', to='atomicdb.plannershadowcycle')),
            ],
            options={
                'indexes': [models.Index(fields=['position_key'], name='atomic_planner_pick_pos')],
            },
        ),
        migrations.AddConstraint(
            model_name='plannershadowpick',
            constraint=models.UniqueConstraint(fields=('cycle', 'rank'), name='uniq_planner_pick_rank'),
        ),
    ]
