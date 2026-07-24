#!/bin/bash
set -euo pipefail

cd /opt/openbench

git fetch origin spell-runner
git reset --hard origin/spell-runner

PYTHON=./.venv/bin/python

./.venv/bin/pip -q install -r req-linux.txt 2>/dev/null || true

database_alias="$("$PYTHON" - <<'PY'
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OpenSite.settings")
from django.conf import settings

alias = getattr(settings, "ATOMICDB_DATABASE_ALIAS", "default")
if alias not in {"default", "atomicdb"}:
    raise SystemExit("Unsupported AtomicDB database alias; refusing deploy")
print(alias)
PY
)"

if [[ "$database_alias" == "atomicdb" ]]; then
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

if [[ "$database_alias" == "atomicdb" ]]; then
    # A deploy interrupted here is re-runnable: shadow may be ahead of active,
    # but both must still be known histories and shadow must already be current.
    "$PYTHON" manage.py verify_atomicdb_shadow \
        --allow-active-pending-migrations
    "$PYTHON" manage.py migrate atomicdb \
        --database atomicdb --no-input | tail -1
    "$PYTHON" manage.py verify_atomicdb_database
    "$PYTHON" manage.py verify_atomicdb_shadow \
        --compare-active-schema
fi

"$PYTHON" manage.py collectstatic --no-input | tail -1

# Restart only after both databases have passed their migration gates.
systemctl restart openbench
sleep 2
curl --fail --silent --show-error --output /dev/null \
    --write-out "deploy OK: %{http_code}\n" \
    http://127.0.0.1:8000/index/
