"""Una sugerencia de nombre pasa a poder ser una CORRECCION de otro nombre.

Dos columnas nuevas y nada mas: ``kind`` dice si la fila propone un nombre
donde no habia ninguno (``NEW``, el comportamiento historico y por eso el
default) o corrige uno existente (``EDIT``), y ``previous_name`` congela el
nombre que se mostraba en el momento de proponer.  Ambas con default y sin
NOT NULL nuevo sobre datos: las filas ya escritas quedan validas tal cual,
que es exactamente lo que eran — propuestas de nombre nuevo.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0024_progress_adversarial_kpis'),
    ]

    operations = [
        migrations.AddField(
            model_name='openingnamesuggestion',
            name='kind',
            field=models.CharField(choices=[('NEW', 'New'), ('EDIT', 'Edit')], default='NEW', max_length=4),
        ),
        migrations.AddField(
            model_name='openingnamesuggestion',
            name='previous_name',
            field=models.CharField(blank=True, default='', max_length=60),
        ),
    ]
