from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0009_analysistask_nodes_searched'),
    ]

    operations = [
        migrations.AddField(
            model_name='position',
            name='proof',
            field=models.CharField(
                choices=[
                    ('ANDOR', 'Andor'),
                    ('ENGINE', 'Engine'),
                    ('DISPUTED', 'Disputed'),
                ],
                max_length=8,
                null=True,
            ),
        ),
    ]
