# The planner's count of distinct parents in atomicdb_edge (12-Nov-2026).
#
# THE CASE.  The propagation drainer fell an hour behind: one batch of 128
# landings took one to two minutes, the queue only broke even with the
# inflow, and proven closures waited for their parents.  Sampling the
# drainer's session showed it waiting on IPC (parallel workers) half the
# time, inside the climb's ``Edge.objects.filter(parent_id__in=...)``
# reads.  EXPLAIN ANALYZE of that read for 500 parents: 3,132 rows found,
# 784,580 rows estimated, a parallel plan, and JIT inlining plus
# optimisation at 0.5 to 0.8 s per statement, against 76 ms with JIT off.
# Every level of every ascent pays it, up to 64 levels per batch and again
# in the carry after the commit.
#
# WHY THE ESTIMATE IS WRONG.  ANALYZE puts n_distinct(parent_id) at 83,551,
# about 1,500 edges per parent.  A parent's edges are written together, so
# they share heap pages, and ANALYZE's page sample sees the same few parents
# over and over.  A sample of 12,481 positions on 12-Nov found 22.5% of them
# with edges and 4.8 edges each: about 28M distinct parents for 129M edges,
# n_distinct = -0.22.
#
# THE FIX.  Pin the ratio, as PostgreSQL allows for exactly this case: a
# negative n_distinct is a fraction of the rows, so it stays right as the
# table grows, and every later ANALYZE keeps it.  -0.2 is five edges per
# parent.  Statistics only: no row, index or query changes, so no value or
# closure can.  PostgreSQL only; SQLite has no such statistic.

from django.db import migrations

N_DISTINCT = -0.2
LOCK_TIMEOUT = '5s'


def pin(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    # Both statements take SHARE UPDATE EXCLUSIVE on atomicdb_edge, which
    # reads and writes pass: only a vacuum or another ANALYZE waits.
    schema_editor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    schema_editor.execute(
        'ALTER TABLE atomicdb_edge ALTER COLUMN parent_id '
        f'SET (n_distinct = {N_DISTINCT})')
    schema_editor.execute('ANALYZE atomicdb_edge (parent_id)')


def unpin(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    schema_editor.execute(
        'ALTER TABLE atomicdb_edge ALTER COLUMN parent_id '
        'RESET (n_distinct)')
    schema_editor.execute('ANALYZE atomicdb_edge (parent_id)')


class Migration(migrations.Migration):

    dependencies = [
        ('atomicdb', '0064_opening_name_votes'),
    ]

    operations = [
        migrations.RunPython(pin, unpin),
    ]
