# Moving AtomicDB to Postgres

Runbook, written before it is needed.  Nothing here has been executed against
production; the settings support is in place and inert, and the schema work
below is a design with its DDL written out, not a Django migration.

## 1. When, not whether

Do not migrate because the database has half a million positions.  Migrate
before roughly **2–5 million**, or at the first of these triggers, whichever
comes first:

- p95 ingest transaction above 250–500 ms;
- recurring `SQLITE_BUSY`;
- WAL regularly above 0.5–1 GB;
- checkpoints unable to complete for minutes;
- more than one sustained write-producing background service (the ingest
  processor and the selector refresher are already two);
- backup duration above 10–15 minutes;
- the proof manager needing a query SQLite cannot serve efficiently.

There is also a standing reason to bring it forward: the deployed SQLite is
3.45.1, which is inside the line affected by the documented WAL-reset race
(fixed in 3.51.3, backported to 3.44.6 and 3.50.7).  Five gunicorn processes
writing and checkpointing is the configuration the race needs.  The daily
backup-API snapshots are the mitigation in place; Postgres is the cure.

Postgres will not rescue a full Python graph pass.  That is what
`refresh_selector` and the pn descent are for.

## 2. What moves, and separately

Two databases, two decisions:

- **openbench.db** — accounts, machines, tests, SPRT.  Small, and the thing
  whose downtime people notice.
- **atomicdb.sqlite3** — positions, edges, analyses, proof nodes, solve tasks.
  Large, and the thing that actually needs Postgres.

They can move independently, and should: `ATOMICDB_DATABASE_ALIAS` already
points the AtomicDB app at its own alias, and `OpenSite.db_routers` already
forbids relations across the two.  Move AtomicDB first and leave OpenBench on
SQLite until there is a reason.

## 3. Settings

Already implemented, inert without the environment:

```
OPENBENCH_ATOMICDB_POSTGRES_DB=atomicdb
OPENBENCH_ATOMICDB_POSTGRES_USER=atomicdb
OPENBENCH_ATOMICDB_POSTGRES_PASSWORD=...
OPENBENCH_ATOMICDB_POSTGRES_HOST=127.0.0.1
OPENBENCH_ATOMICDB_POSTGRES_PORT=5432
OPENBENCH_ATOMICDB_POSTGRES_CONN_MAX_AGE=60
```

With `OPENBENCH_ATOMICDB_POSTGRES_DB` set, the SQLite split-identity block is
skipped entirely — a split receipt is a statement about a SQLite file and has
no meaning against a Postgres database — and the `atomicdb` alias points at
Postgres.  Falls back to the `OPENBENCH_POSTGRES_*` values for user, password,
host and port when the AtomicDB-specific ones are absent, so a single-server
install sets four variables instead of eight.

For OpenBench itself the existing `OPENBENCH_POSTGRES_DB` block is unchanged.

## 4. The move

Expected downtime: **20–40 minutes** at current size, dominated by the copy.
The web front end can stay up read-only for most of it if the ingest processor
and the selector are stopped first; the worker protocol tolerates a stopped
server (leases expire, results retry).

```bash
# 0. Freeze the writers. Workers keep their results and retry.
sudo systemctl stop atomicdb-ingest atomicdb-selector
sudo systemctl stop openbench          # or leave it up read-only

# 1. Snapshot first, with the backup API (not cp: WAL).
python manage.py dbbackup_atomicdb /var/backups/atomicdb-precutover.sqlite3

# 2. Create the target.
sudo -u postgres createuser --pwprompt atomicdb
sudo -u postgres createdb --owner=atomicdb atomicdb

# 3. Schema only, from the same migrations the SQLite file already ran.
OPENBENCH_ATOMICDB_POSTGRES_DB=atomicdb python manage.py migrate atomicdb

# 4. Data. Django's dumpdata/loaddata is the portable path and the slow one;
#    at a few million rows prefer COPY through a per-table CSV export.
python manage.py dumpdata atomicdb --database=atomicdb --natural-foreign \
    --exclude atomicdb.progresssnapshot -o /tmp/atomicdb.json
OPENBENCH_ATOMICDB_POSTGRES_DB=atomicdb python manage.py loaddata \
    /tmp/atomicdb.json --database=atomicdb

# 5. Sequences: loaddata does not advance them.
OPENBENCH_ATOMICDB_POSTGRES_DB=atomicdb python manage.py sqlsequencereset \
    atomicdb | sudo -u postgres psql atomicdb
```

Load order matters if you export table by table instead of using `dumpdata`:
`position` before `edge` (FK, and `Edge.child` is PROTECT), `position` before
`proofcampaign` (PROTECT on root), `analysistask` before `ingestjob`
(OneToOne), everything before `dbevent`.

## 5. Verification, before letting a writer near it

```bash
OPENBENCH_ATOMICDB_POSTGRES_DB=atomicdb python manage.py verify_atomicdb_database
OPENBENCH_ATOMICDB_POSTGRES_DB=atomicdb python manage.py proof_status
OPENBENCH_ATOMICDB_POSTGRES_DB=atomicdb python manage.py verify_certificate --all
```

Row counts must match table by table.  `verify_certificate --all` is the one
that matters most: it re-derives every SOLVE closure from its stored
certificate, so it proves the blobs survived the round trip byte for byte.
Then compare closure counts by kind against the pre-cutover snapshot.

## 6. Rollback

Unset `OPENBENCH_ATOMICDB_POSTGRES_DB` and restart.  The SQLite file and its
split receipt are untouched by any of the above, so rollback is a restart, not
a restore — as long as nothing has been written to Postgres that matters yet.
Once workers have submitted against Postgres, rolling back loses those
submissions; that is the point of no return, and it is the moment to stop
worrying about rollback and start worrying about backups.

Take the Postgres backup schedule live BEFORE the first worker submission:

```bash
pg_dump --format=custom atomicdb > /var/backups/atomicdb-$(date +%F).dump
```

## 7. Designed, not yet migrated: surrogate keys

The primary key today is a 64-character sha256 hex string, stored twice per
edge (parent and child).  Raw payload for the identifiers alone:

| Scale | Edge rows | Two hex sha | Two BIGINT |
|---|---:|---:|---:|
| now | 0.45M | 58 MB | 7 MB |
| 10x | 4.5M | 581 MB | 73 MB |
| 100x | 45M | 5.8 GB | 726 MB |

Indexes and row overhead multiply the difference.  Do this WITH the Postgres
migration, at half a million rows, not later at fifty million.

Target shape:

```sql
ALTER TABLE atomicdb_position ADD COLUMN id BIGSERIAL;
ALTER TABLE atomicdb_position ADD COLUMN sha256 BYTEA;
UPDATE atomicdb_position SET sha256 = decode(key, 'hex');
ALTER TABLE atomicdb_position ALTER COLUMN sha256 SET NOT NULL;
CREATE UNIQUE INDEX position_sha256_uniq ON atomicdb_position (sha256);

ALTER TABLE atomicdb_edge ADD COLUMN parent_ref BIGINT;
ALTER TABLE atomicdb_edge ADD COLUMN child_ref  BIGINT;
UPDATE atomicdb_edge e
   SET parent_ref = p.id, child_ref = c.id
  FROM atomicdb_position p, atomicdb_position c
 WHERE e.parent_id = p.key AND e.child_id = c.key;
ALTER TABLE atomicdb_edge ALTER COLUMN parent_ref SET NOT NULL;
ALTER TABLE atomicdb_edge ALTER COLUMN child_ref  SET NOT NULL;
CREATE UNIQUE INDEX edge_parent_move_uniq
    ON atomicdb_edge (parent_ref, move_uci);
CREATE INDEX edge_child_idx ON atomicdb_edge (child_ref);
```

Keep the full sha256 and the canonical FEN: they are the external identity and
the collision check.  A 128-bit truncation has negligible collision
probability at this scale, but there is no reason to make a truncated hash the
sole identity when a surrogate key is right there.

Deliberately NOT written as a Django migration yet.  Swapping a primary key
means every FK in the app moves at once — `Edge`, `AnalysisTask`, `IngestJob`,
`ProofNode`, `SolveTask`, `RequestLog`, `OpeningNameSuggestion` — plus every
`values_list('parent_id', ...)` in `ingest.py` and every URL that carries a
key.  It is its own project, and it should land on a Postgres database that is
already serving traffic correctly, not as part of the move.

## 8. Also designed, not yet built

`last_analysis` capping is DONE (non-mate PVs truncated to 24 plies at ingest;
mate lines kept whole because they are evidence).  At 1 KB per row the JSON
would have been 45 GB at 45M positions — more than the position table itself.

`legal_move_inventory` is designed in `atomicdb/models.py`, above `DBEvent`:
packed legal moves plus a set hash, so "expanded" can mean "materialised
coverage equals an AUTHENTICATED legal set" instead of "the rows that exist
look like all of them".  That change touches the exact fixed point, so it
waits until the proof manager has settled and the move to Postgres is done.
