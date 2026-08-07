#!/bin/bash
#
# Deploy the current spell-runner tip to this host.
#
# Usage: ./deploy.sh [--restart-selector]
#
# The selector holds a single pass for well over an hour, so restarting it
# throws away real work. It is left running by default and reported as stale
# instead; pass --restart-selector when the deploy actually touches it.
#
set -euo pipefail

cd /opt/openbench

RESTART_SELECTOR=0
[ "${1:-}" = "--restart-selector" ] && RESTART_SELECTOR=1

# The reset below is unconditional and silent. Anything edited straight on the
# host dies here with no trace, so refuse to run and let a human decide.
if ! git diff --quiet HEAD -- ; then
    echo "ABORTA: hay ediciones locales en ficheros versionados." >&2
    echo "El reset --hard de abajo las destruiria sin avisar. Canonizalas en" >&2
    echo "la rama (o descartalas a mano) y vuelve a lanzar:" >&2
    echo >&2
    git status --porcelain -uno >&2
    exit 1
fi

BEFORE=$(git rev-parse --short HEAD)
git fetch origin spell-runner
git reset --hard origin/spell-runner
AFTER=$(git rev-parse --short HEAD)
echo "deploy: ${BEFORE} -> ${AFTER}"

./.venv/bin/pip -q install -r req-linux.txt 2>/dev/null || true

# manage.py talks to the SQLite default unless this env is sourced; without it
# every atomicdb command silently addresses the wrong database.
set -a && . /etc/openbench/atomicdb-pg.env && set +a

# Two databases, two migrate calls. `migrate` alone only ever reaches the
# default, so an atomicdb migration would land in SQLite, get recorded as
# applied, and leave Postgres missing the column -- the 33-minute outage of
# 2026-07-29. Do not collapse these back into one.
./.venv/bin/python manage.py migrate --no-input
./.venv/bin/python manage.py migrate atomicdb --database atomicdb --no-input

./.venv/bin/python manage.py collectstatic --no-input | tail -1
cp /opt/openbench/Client/atomicdb_worker.py /var/www/atomicdb-engines/

# The ingest units run this same tree, so leaving them up means half the host
# serves the old code.
systemctl restart openbench
systemctl restart atomicdb-ingest atomicdb-ingest2 atomicdb-ingest3 atomicdb-ingest4 atomicdb-ingest5

if [ "$RESTART_SELECTOR" = "1" ]; then
    systemctl restart atomicdb-selector
else
    echo "AVISO: atomicdb-selector sigue con el codigo anterior (pase largo en curso)."
    echo "       Relanza con --restart-selector cuando el pase pueda perderse."
fi

sleep 2 && curl -s -o /dev/null -w "deploy OK: %{http_code}\n" http://127.0.0.1:8000/index/
