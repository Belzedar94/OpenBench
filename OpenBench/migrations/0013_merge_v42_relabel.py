# Production applied 0012_relabel_mode directly on top of
# 0010_test_variant_contract, before 0011 (protocol v42) was deployed there.
# Both branches start at 0010; this empty merge joins them so the repository
# and production keep one consistent migration history.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0011_datagen_publication_contract_v42'),
        ('OpenBench', '0012_relabel_mode'),
    ]

    operations = []
