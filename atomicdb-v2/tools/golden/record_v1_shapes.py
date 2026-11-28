#!/usr/bin/env python3
"""Record the request/response shapes of v1's API as its own test suite drives it.

The worker protocol is stateful and production must never receive a fake
submission, so the shapes of ``api/lease``, ``api/heartbeat``, ``api/submit``,
``api/release`` and ``api/solve/*`` (and of the authenticated public calls,
``my-queue``, ``my-workers``, ``queue-bump``, ``queue-cancel``, ``request``,
``request-children``) are taken from the v1 test suite instead: every view of
the API is wrapped before the URL configuration is loaded, the chosen test
modules run as usual (SQLite, the repo's own settings), and each call through
a wrapped view is written out with the test that made it.

Run from the repository root (the directory with ``manage.py``)::

    python atomicdb-v2/tools/golden/record_v1_shapes.py \
        --out atomicdb-v2/tests/golden/v1-shapes/calls.jsonl.gz

Passwords are replaced with ``<redacted>`` and lease tokens with
``<token:N>`` (the same token keeps the same N within one test), so the
recording shows that a token was echoed without carrying a value.
"""

import argparse
import gzip
import json
import os
import re
import sys
import threading
import unittest
from pathlib import Path

WRAPPED_VIEWS = {
    'atomicdb.views': (
        'api_lease', 'api_heartbeat', 'api_submit', 'api_release',
        'api_solve_acquire', 'api_solve_heartbeat', 'api_solve_submit',
        'api_query', 'api_principal_variations', 'api_backed_source',
        'api_public_request', 'api_public_request_children',
        'api_my_queue', 'api_my_workers', 'api_queue_bump_script',
        'api_queue_cancel_script', 'api_frontier', 'api_live_request'),
    'atomicdb.roots': ('roots_api',),
    'atomicdb.conquest_map': ('map_api',),
}
DEFAULT_MODULES = (
    'atomicdb.tests', 'atomicdb.test_worker_http', 'atomicdb.test_worker_lease',
    'atomicdb.test_worker_release', 'atomicdb.test_lease_release',
    'atomicdb.test_lease_heads', 'atomicdb.test_lease_paging',
    'atomicdb.test_lease_position_lock', 'atomicdb.test_fencing',
    'atomicdb.test_revocation', 'atomicdb.test_submit_timeout',
    'atomicdb.test_engine_policy', 'atomicdb.test_max_budget',
    'atomicdb.test_worker_serving', 'atomicdb.test_serve_others_first',
    'atomicdb.test_auth_cache', 'atomicdb.test_solve',
    'atomicdb.test_public_api', 'atomicdb.test_public_fen',
    'atomicdb.test_api_backed_source', 'atomicdb.test_api_my_workers',
    'atomicdb.test_api_principal_variations',
    'atomicdb.test_api_request_children', 'atomicdb.test_api_request_moves',
    'atomicdb.test_queue_control', 'atomicdb.test_request_priority',
    'atomicdb.test_multi_request', 'atomicdb.test_subtree_request',
    'atomicdb.test_adopted_request', 'atomicdb.test_frontier',
    'atomicdb.test_live_request', 'atomicdb.test_conquest_map',
    'atomicdb.test_conquest_map_cache', 'atomicdb.test_root_extremes',
    'atomicdb.test_view_cache', 'atomicdb.test_depth_slider',
    'atomicdb.test_mate_sign', 'atomicdb.test_repetition_ui',
)
KEPT_HEADERS = ('Content-Type', 'Cache-Control', 'Vary', 'ETag',
                'Retry-After', 'Content-Encoding', 'Allow')
SECRET_FIELDS = ('password',)
TOKEN_FIELDS = ('lease_token',)

_state = threading.local()


def _current_test():
    return getattr(_state, 'test', '')


class _Tokens:
    """Lease tokens replaced by ``<token:N>``, stable within one test."""

    def __init__(self):
        self.test = None
        self.seen = {}

    def name(self, value):
        if self.test != _current_test():
            self.test, self.seen = _current_test(), {}
        if not value:
            return value
        if value not in self.seen:
            self.seen[value] = f'<token:{len(self.seen) + 1}>'
        return self.seen[value]


TOKENS = _Tokens()
TOKEN_RE = re.compile(r'"lease_token": "([A-Za-z0-9_\-]{20,})"')


def _scrub_form(form):
    out = {}
    for key, values in form.lists():
        cleaned = []
        for value in values:
            if key in SECRET_FIELDS:
                value = '<redacted>'
            elif key in TOKEN_FIELDS:
                value = TOKENS.name(value)
            elif key == 'leases':
                value = re.sub(r'"lease_token":\s*"([^"]+)"',
                               lambda m: '"lease_token": "%s"'
                               % TOKENS.name(m.group(1)), value)
            cleaned.append(value)
        out[key] = cleaned if len(cleaned) > 1 else cleaned[0]
    return out


def _scrub_body(text):
    return TOKEN_RE.sub(
        lambda m: '"lease_token": "%s"' % TOKENS.name(m.group(1)), text)


def _wrap(view, name, sink, lock):
    def recorded(request, *args, **kwargs):
        try:
            raw_json = None
            if request.content_type == 'application/json':
                try:
                    raw_json = json.loads(request.body.decode('utf-8'))
                    if isinstance(raw_json, dict) and 'password' in raw_json:
                        raw_json = dict(raw_json, password='<redacted>')
                except (ValueError, UnicodeDecodeError):
                    raw_json = '<unreadable>'
            form = _scrub_form(request.POST) if request.method == 'POST' else {}
        except Exception as exc:                     # noqa: BLE001
            form, raw_json = {'<unreadable>': str(exc)}, None
        response = view(request, *args, **kwargs)
        try:
            content = getattr(response, 'content', b'')
            if response.get('Content-Encoding') == 'gzip':
                content = gzip.decompress(content)
            body = content.decode('utf-8', errors='replace')
            entry = {
                'test': _current_test(),
                'view': name,
                'method': request.method,
                'path': request.path,
                'query': list(request.GET.lists()),
                'form': form,
                'json_body': raw_json,
                'authenticated_session': bool(
                    getattr(getattr(request, 'user', None),
                            'is_authenticated', False)),
                'status': response.status_code,
                'headers': {h: response[h] for h in KEPT_HEADERS
                            if response.has_header(h)},
                'body': _scrub_body(body),
            }
            with lock:
                sink.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as exc:                     # noqa: BLE001
            print(f'record failed for {name}: {exc}', file=sys.stderr)
        return response

    recorded.__name__ = getattr(view, '__name__', name)
    recorded.__wrapped__ = view
    for attr in ('csrf_exempt',):
        if hasattr(view, attr):
            setattr(recorded, attr, getattr(view, attr))
    return recorded


class _NamingResult(unittest.TextTestResult):
    def startTest(self, test):
        _state.test = test.id()
        super().startTest(test)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('modules', nargs='*', default=list(DEFAULT_MODULES))
    args = parser.parse_args()

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')
    os.environ.setdefault('OPENBENCH_SECRET_KEY', 'golden-shapes-only')
    import django
    django.setup()
    import importlib
    from django.conf import settings
    from django.test.utils import get_runner

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sink = gzip.open(out, 'wt', encoding='utf-8', compresslevel=9)
    lock = threading.Lock()
    # Before anything resolves a URL: ``atomicdb.urls`` reads these module
    # attributes when it is first imported, and the test client imports it
    # lazily on its first request.
    for module_name, names in WRAPPED_VIEWS.items():
        module = importlib.import_module(module_name)
        for name in names:
            setattr(module, name, _wrap(getattr(module, name),
                                        f'{module_name}.{name}', sink, lock))
    assert 'atomicdb.urls' not in sys.modules, 'urls imported too early'

    runner_class = get_runner(settings)

    class Runner(runner_class):
        def get_resultclass(self):
            return _NamingResult

    runner = Runner(verbosity=1, interactive=False, parallel=0)
    failures = runner.run_tests(args.modules)
    sink.close()
    print(f'recorded into {out}; test failures: {failures}')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
