from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0010_test_variant_contract'),
    ]

    operations = [
        migrations.AddField(
            model_name='test',
            name='datagen_teacher_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.RemoveConstraint(
            model_name='test',
            name='datagen_publication_protocol_valid',
        ),
        migrations.RemoveConstraint(
            model_name='test',
            name='unique_datagen_v41_campaign_workload',
        ),
        migrations.RemoveConstraint(
            model_name='test',
            name='unique_datagen_v41_campaign_role_cohort',
        ),
        migrations.AddConstraint(
            model_name='test',
            constraint=models.CheckConstraint(
                check=models.Q(datagen_publication_protocol__in=[0, 41, 42]),
                name='datagen_publication_protocol_valid',
            ),
        ),
        migrations.AddConstraint(
            model_name='test',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    datagen_publication_protocol__in=[41, 42]
                ),
                fields=(
                    'datagen_campaign_id', 'datagen_external_workload_id',
                ),
                name='unique_datagen_publication_campaign_workload',
            ),
        ),
        migrations.AddConstraint(
            model_name='test',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    datagen_publication_protocol__in=[41, 42]
                ),
                fields=(
                    'datagen_campaign_id', 'datagen_role', 'datagen_cohort',
                ),
                name='unique_datagen_publication_campaign_role_cohort',
            ),
        ),
    ]
