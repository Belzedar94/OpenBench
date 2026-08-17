# The recertify resumability check asks, once per mate key, which
# MATE_RECERTIFIED events carry that key in their payload. With millions of
# events the (kind, ts) index left it reading the whole recertification band
# per key, and the nightly pass held Postgres at that cost for hours. A
# partial expression index turns each lookup into a probe (measured: 0.064 ms
# and 5 buffers against a 3.5M row table). Created IF NOT EXISTS because
# production got it live first, with CONCURRENTLY.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0046_analysistask_delivered_by'),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS atomic_event_recert_key "
                "ON atomicdb_dbevent USING btree ((payload -> 'key')) "
                "WHERE kind = 'MATE_RECERTIFIED'"
            ),
            reverse_sql="DROP INDEX IF EXISTS atomic_event_recert_key",
        ),
    ]
