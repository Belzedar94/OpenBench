from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0007_datagen_tablebase_receipts_v40'),
    ]

    operations = [
        migrations.AddField(
            model_name='test',
            name='datagen_publication_protocol',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_campaign_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_external_workload_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_role',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_cohort',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_publication_contract',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_publication_contract_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_network_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_network_bytes',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_book_kind',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_book_source',
            field=models.CharField(blank=True, default='', max_length=2048),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_book_text_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_book_raw_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddConstraint(
            model_name='test',
            constraint=models.CheckConstraint(
                check=models.Q(datagen_publication_protocol__in=[0, 41]),
                name='datagen_publication_protocol_valid',
            ),
        ),
        migrations.AddConstraint(
            model_name='test',
            constraint=models.UniqueConstraint(
                condition=models.Q(datagen_publication_protocol=41),
                fields=('datagen_campaign_id', 'datagen_external_workload_id'),
                name='unique_datagen_v41_campaign_workload',
            ),
        ),
        migrations.AddConstraint(
            model_name='test',
            constraint=models.UniqueConstraint(
                condition=models.Q(datagen_publication_protocol=41),
                fields=(
                    'datagen_campaign_id', 'datagen_role', 'datagen_cohort',
                ),
                name='unique_datagen_v41_campaign_role_cohort',
            ),
        ),
    ]
