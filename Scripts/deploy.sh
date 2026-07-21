#!/bin/bash
set -e
cd /opt/openbench
git fetch origin spell-runner
git reset --hard origin/spell-runner
./.venv/bin/pip -q install -r req-linux.txt 2>/dev/null || true
./.venv/bin/python manage.py migrate --no-input | tail -1
./.venv/bin/python manage.py collectstatic --no-input | tail -1
systemctl restart openbench
sleep 2 && curl -s -o /dev/null -w "deploy OK: %{http_code}\n" http://127.0.0.1:8000/index/
