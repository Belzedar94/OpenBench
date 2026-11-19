# Hidden opening names: a HIDE kind for a suggestion and a HIDE resolution for
# a name under review (§ atomicdb.community_names).  Only the choices change:
# no column, no default and no data touched.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0070_backlogitem_held'),
    ]

    operations = [
        migrations.AlterField(
            model_name='openingnamesuggestion',
            name='kind',
            field=models.CharField(choices=[('NEW', 'New'), ('EDIT', 'Edit'),
                                            ('DEL', 'Remove'),
                                            ('HIDE', 'Hide')],
                                   default='NEW', max_length=4),
        ),
        migrations.AlterField(
            model_name='openingnameflag',
            name='resolution',
            field=models.CharField(blank=True,
                                   choices=[('KEEP', 'Keep'),
                                            ('RESTORE', 'Restore'),
                                            ('REMOVE', 'Remove'),
                                            ('RENAME', 'Rename'),
                                            ('HIDE', 'Hide'),
                                            ('STALE', 'Stale')],
                                   default='', max_length=8),
        ),
    ]
