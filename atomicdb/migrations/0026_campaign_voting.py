"""Las campanas de exploracion pasan a proponerse y votarse en publico.

Tres cosas entran aqui y conviene separarlas:

* ``state`` sustituye al booleano ``active`` como fuente de verdad.  El
  booleano NO se borra: hay codigo desplegado que lo lee, y quitarlo en la
  misma migracion que introduce el estado obligaria a una ventana atomica
  entre base y web que esta funcion no merece.  La ``RunPython`` traduce lo
  que habia — ``active=True`` era "abierta", ``active=False`` era "cerrada" —
  y de ahi en adelante ``Campaign.apply_state`` mantiene los dos de acuerdo.

* ``votes`` es una CACHE del recuento, no el recuento.  Nace a cero incluso en
  filas viejas porque no hay ningun voto que contar todavia.

* ``CampaignVote`` guarda un voto por (campana, cookie).  La unicidad la pone
  la base, no la vista: dos pestanas que voten a la vez tienen que chocar
  contra una restriccion, no contra un ``exists()`` que ya paso.
"""

from django.db import migrations, models
import django.db.models.deletion


def state_from_active(apps, schema_editor):
    """Traduce el booleano historico al estado nuevo.

    ``active=False`` en una campana antigua significaba exactamente una cosa:
    su raiz se cerro y ``_emit_closure_events`` la apago.  Eso es ``DONE``.
    Lo demas estaba abierto y lo unico honesto es llamarlo ``ACTIVE``: eran
    campanas que el selector ya estaba mirando, no propuestas esperando turno.
    """
    Campaign = apps.get_model('atomicdb', 'Campaign')
    Campaign.objects.filter(active=True).update(state='ACTIVE')
    Campaign.objects.filter(active=False).update(state='DONE')


def active_from_state(apps, schema_editor):
    """Vuelta atras: el booleano recupera el papel de fuente de verdad."""
    Campaign = apps.get_model('atomicdb', 'Campaign')
    Campaign.objects.filter(state='ACTIVE').update(active=True)
    Campaign.objects.exclude(state='ACTIVE').update(active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0025_opening_name_edits'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='state',
            field=models.CharField(choices=[('PROPOSED', 'Proposed'), ('ACTIVE', 'Active'), ('PAUSED', 'Paused'), ('DONE', 'Done')], db_index=True, default='PROPOSED', max_length=8),
        ),
        migrations.AddField(
            model_name='campaign',
            name='votes',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='campaign',
            name='proposed_by',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='campaign',
            name='proposed_token',
            field=models.CharField(blank=True, db_index=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='campaign',
            name='activated_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.CreateModel(
            name='CampaignVote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(max_length=64)),
                ('name', models.CharField(blank=True, default='', max_length=64)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('campaign', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='vote_rows', to='atomicdb.campaign')),
            ],
        ),
        migrations.AddConstraint(
            model_name='campaignvote',
            constraint=models.UniqueConstraint(fields=('campaign', 'token'), name='uniq_campaign_vote'),
        ),
        migrations.RunPython(state_from_active, active_from_state),
    ]
