"""Seed the root proof campaign (startpos, WHITE_WIN) if it is missing.

The proof manager needs at least one campaign to have anything to descend
from.  Creating it here, rather than lazily at the first lease, keeps the
production database and a fresh checkout in the same state and makes
``proof_status`` meaningful from the first minute.

Deliberately tolerant: if the start position is not in the table yet (a brand
new database that has never ingested anything), the row is created here with
the same canonical FEN the ingestor would compute.  ``pyffish`` is not
imported — a migration must not depend on an engine binding being installed —
so the start FEN is written literally and asserted against ``logic`` by
``atomicdb/test_proof_manager.py``.
"""

import hashlib

from django.db import migrations

# ``logic.canonical_fen(pf.start_fen('atomic'))``: same board as standard
# chess, counters stripped to '0 1' by AtomicDB's canonical identity.
START_FEN = ('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1')
CAMPAIGN_NAME = 'root-white-win'
DEFAULT_POLICY = {'primary': 0.8, 'backup': 0.15, 'explore': 0.05}


def create_default_campaign(apps, schema_editor):
    # Sobre la base QUE SE ESTA MIGRANDO: sin el ``using``, el router mandaba
    # las dos pasadas (default y alias) a la misma base y la sombra default
    # se quedaba SIN la campana raiz — en silencio, porque el verificador de
    # sombra compara esquemas y migraciones, nunca filas.
    alias = schema_editor.connection.alias
    Position = apps.get_model('atomicdb', 'Position')
    ProofCampaign = apps.get_model('atomicdb', 'ProofCampaign')
    if ProofCampaign.objects.using(alias).filter(name=CAMPAIGN_NAME).exists():
        return
    key = hashlib.sha256(START_FEN.encode()).hexdigest()
    Position.objects.using(alias).get_or_create(
        key=key, defaults={'fen': START_FEN})
    # FK por id crudo: asignar el OBJETO dispara allow_relation, y durante la
    # pasada sombra el router la veta (el objeto vive en la base migrada, no
    # en el alias seleccionado).  El id no pregunta a nadie.
    ProofCampaign.objects.using(alias).create(
        name=CAMPAIGN_NAME, root_id=key, goal='WHITE_WIN', active=True,
        algorithm_version=1, repertoire_policy=dict(DEFAULT_POLICY))


def drop_default_campaign(apps, schema_editor):
    alias = schema_editor.connection.alias
    ProofCampaign = apps.get_model('atomicdb', 'ProofCampaign')
    ProofCampaign.objects.using(alias).filter(name=CAMPAIGN_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [('atomicdb', '0017_proof_manager')]

    operations = [
        migrations.RunPython(create_default_campaign, drop_default_campaign),
    ]
