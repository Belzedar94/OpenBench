from django.db import migrations, models
import hashlib
import json
import string


TABLEBASE_PLACEHOLDERS = {
    'SYZYGY', 'SYZYGY_MANIFEST_SHA256', 'SYZYGY_MAX', 'TEACHER_MODE',
}


def backfill_environment_contracts(apps, schema_editor):
    Test = apps.get_model('OpenBench', 'Test')
    for test in Test.objects.filter(test_mode='DATAGEN').exclude(
        datagen_command=''
    ):
        try:
            fields = {
                name for _literal, name, _format_spec, _conversion
                in string.Formatter().parse(test.datagen_command)
                if name is not None
            }
        except ValueError:
            fields = set()
        required = bool(fields & TABLEBASE_PLACEHOLDERS)
        contract = json.dumps({
            'protocol': 40,
            'schema': 'openbench-datagen-environment-v40',
            'tablebase': {
                'required': required,
                'family': '',
                'max': 0,
                'manifest_sha256': '',
            },
            'teacher_mode': '',
        }, sort_keys=True, separators=(',', ':')).encode('utf-8')
        test.datagen_tablebase_required = required
        test.datagen_environment_contract_sha256 = hashlib.sha256(
            contract
        ).hexdigest()
        test.save(update_fields=[
            'datagen_tablebase_required',
            'datagen_environment_contract_sha256',
        ])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0006_producer_reservations_v39'),
    ]

    operations = [
        migrations.AddField(
            model_name='test',
            name='datagen_tablebase_required',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_tablebase_family',
            field=models.CharField(blank=True, default='', max_length=16),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_tablebase_max',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_tablebase_manifest_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_teacher_mode',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='test',
            name='datagen_environment_contract_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='datagenchunk',
            name='environment_receipt',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='datagenchunk',
            name='environment_receipt_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='datagenchunk',
            name='environment_lease',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='datagenchunk',
            name='environment_lease_sha256',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.RunPython(backfill_environment_contracts, noop_reverse),
    ]
