#!/bin/bash
#
# Usage: Scripts/deploy.sh [--restart-selector]
#
set -euo pipefail

cd /opt/openbench

RESTART_SELECTOR=0
[ "${1:-}" = "--restart-selector" ] && RESTART_SELECTOR=1

# The reset below is unconditional and silent, so anything edited straight on
# the host dies here without a trace. It has already been aimed at a live
# 162-line edit of Engines/Spell-Stockfish.json. Refuse instead, and show what
# would have been destroyed.
if ! git diff --quiet HEAD -- ; then
    echo "ABORTA: hay ediciones locales en ficheros versionados." >&2
    echo "El reset --hard de abajo las destruiria sin avisar. Canonizalas en" >&2
    echo "la rama (o descartalas a mano) y vuelve a lanzar:" >&2
    echo >&2
    git status --porcelain -uno >&2
    exit 1
fi

git fetch origin spell-runner
git reset --hard origin/spell-runner

PYTHON=./.venv/bin/python

./.venv/bin/pip -q install -r req-linux.txt 2>/dev/null || true

# systemd hands this env to the services, but an ssh session has none of it,
# and the alias check below is what turns that into a refusal rather than a
# silent deploy against the wrong database.
if [ -r /etc/openbench/atomicdb-pg.env ]; then
    set -a && . /etc/openbench/atomicdb-pg.env && set +a
fi

# The alias NAME says where AtomicDB lives; the ENGINE says which protocol
# applies.  The SQLite cutover verifiers below only make sense (and only
# run) for a split SQLite file: branching on the name alone used to send a
# PostgreSQL-backed alias into verifiers that hard-require SQLite and refuse
# the deploy.
database_identity="$("$PYTHON" - <<'PY'
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OpenSite.settings")
from django.conf import settings

alias = getattr(settings, "ATOMICDB_DATABASE_ALIAS", "default")
if alias not in settings.DATABASES:
    raise SystemExit("AtomicDB database alias is not configured; refusing deploy")
engine = settings.DATABASES[alias]["ENGINE"].rsplit(".", 1)[-1]
print(alias, engine)
PY
)"
database_alias="${database_identity%% *}"
database_engine="${database_identity##* }"

sqlite_split="no"
if [[ "$database_alias" != "default" && "$database_engine" == "sqlite3" ]]; then
    sqlite_split="yes"
fi

if [[ "$sqlite_split" == "yes" ]]; then
    # Authenticate the existing destination before mutating either database.
    # The verifier opens the copied destination fail-closed and never creates
    # a missing file.
    "$PYTHON" manage.py verify_atomicdb_database \
        --allow-pending-migrations
    # The legacy tables in default are a schema/history-current rollback
    # shadow.  Their data is intentionally stale after cutover, but Django
    # must never record an AtomicDB migration there without executing it.
    "$PYTHON" manage.py verify_atomicdb_shadow \
        --allow-pending-migrations
fi

# Compatibility mode migrates the original database.  In split mode the
# router deliberately applies AtomicDB migrations here too, preserving the
# rollback shadow while runtime reads/writes remain routed only to atomicdb.
"$PYTHON" manage.py migrate --database default --no-input | tail -1

if [[ "$database_alias" != "default" ]]; then
    if [[ "$sqlite_split" == "yes" ]]; then
        # A deploy interrupted here is re-runnable: shadow may be ahead of
        # active, but both must still be known histories and shadow must
        # already be current.
        "$PYTHON" manage.py verify_atomicdb_shadow \
            --allow-active-pending-migrations
    fi
    "$PYTHON" manage.py migrate atomicdb \
        --database "$database_alias" --no-input | tail -1
    if [[ "$sqlite_split" == "yes" ]]; then
        "$PYTHON" manage.py verify_atomicdb_database
        "$PYTHON" manage.py verify_atomicdb_shadow \
            --compare-active-schema
    fi
fi

"$PYTHON" manage.py collectstatic --no-input | tail -1

# Published to the web root for the AtomicDB solvers to fetch.
cp /opt/openbench/Client/atomicdb_worker.py /var/www/atomicdb-engines/

# Restart only after both databases have passed their migration gates.
systemctl restart openbench
# The ingest units run this same tree; leaving them up means half the host
# serves the previous commit.
systemctl restart atomicdb-ingest atomicdb-ingest2 atomicdb-ingest3 \
    atomicdb-ingest4 atomicdb-ingest5

if [[ "$RESTART_SELECTOR" == "1" ]]; then
    systemctl restart atomicdb-selector
else
    echo "AVISO: atomicdb-selector sigue con el codigo anterior."
    echo "       Un pase suyo dura mas de una hora y el restart lo tira;"
    echo "       relanza con --restart-selector cuando pueda perderse."
fi

sleep 2
curl --fail --silent --show-error --output /dev/null \
    --write-out "deploy OK: %{http_code}\n" \
    http://127.0.0.1:8000/index/
