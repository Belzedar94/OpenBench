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
fi

# Compatibility mode always migrates the original database.  When the staged
# split is enabled, the router prevents AtomicDB schema changes here.
"$PYTHON" manage.py migrate --database default --no-input | tail -1

if [[ "$database_alias" == "atomicdb" ]]; then
    "$PYTHON" manage.py migrate atomicdb \
        --database atomicdb --no-input | tail -1
    "$PYTHON" manage.py verify_atomicdb_database
fi

"$PYTHON" manage.py collectstatic --no-input | tail -1

# Restart only after both databases have passed their migration gates.
systemctl restart openbench
sleep 2
curl -s -o /dev/null -w "deploy OK: %{http_code}\n" \
    http://127.0.0.1:8000/index/
