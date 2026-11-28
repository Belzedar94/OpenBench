"""Route the v1 functions v2 has ported to v2, at the call a test makes.

``install()`` wraps every v1 function the tests drive (the ``functions``
column of ``atomicdb-v2/docs/test-map.csv``) plus every ported name, in its
own module and wherever another ``atomicdb`` module imported it by name.

A wrapper decides per call:

* inside another wrapped v1 function (depth > 0): run v1, untouched;
* the function is ported (``av2.entries()`` lists its v1 name): call v2
  through ``bridge.call_v2``;
* otherwise run v1, one level deeper, so that whatever it calls inside
  stays on v1 too.  With ``ATOMICDB_V2_STRICT=1`` a function that is not a
  setup helper raises ``NotPorted`` instead.

So a test's setup (positions, expansions, landings) runs on v1 until those
are ported, and the act it asserts on runs on v2 as soon as it is ported.
Every call is counted in ``bridge.STATS`` for the per-test report.

``ATOMICDB_V2_ONLY`` / ``ATOMICDB_V2_EXCLUDE`` (comma-separated v1 names)
narrow the routed set, to bisect a failure.
"""

import csv
import functools
import importlib
import inspect
import os
import sys
import threading

from . import bridge

TEST_MAP_ENV = 'ATOMICDB_V2_TEST_MAP'
DEFAULT_TEST_MAP = os.path.join(bridge._REPO, 'atomicdb-v2', 'docs', 'test-map.csv')
STRICT_ENV = 'ATOMICDB_V2_STRICT'

# Helpers a test uses to build its rows; never "not ported" in strict mode.
SETUP_MODULES = ('logic', 'testing', 'models')
SETUP_FUNCTIONS = {'ingest.get_or_create_position', 'ingest.expand'}

_local = threading.local()
_installed = {}         # v1 name -> (original, wrapper)
_patched = []           # (module, attribute, original)


class NotPorted(RuntimeError):
    pass


def _depth():
    return getattr(_local, 'depth', 0)


def _enter():
    _local.depth = _depth() + 1


def _leave():
    _local.depth = _depth() - 1


def _env_set(name):
    return {s.strip() for s in os.environ.get(name, '').split(',') if s.strip()}


def routed_names():
    """The v1 names that go to v2 (ported, narrowed by the env filters);
    none when the extension is not built."""
    if not bridge.available():
        return set()
    names = bridge.ported()
    only = _env_set('ATOMICDB_V2_ONLY')
    if only:
        names &= only
    return names - _env_set('ATOMICDB_V2_EXCLUDE')


def test_map_functions(path=None):
    path = path or os.environ.get(TEST_MAP_ENV) or DEFAULT_TEST_MAP
    names = set()
    try:
        with open(path, newline='', encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                for name in (row.get('functions') or '').split(';'):
                    name = name.strip()
                    if name and ':' not in name and '.' in name:
                        names.add(name)
    except OSError:
        pass
    return names


def _resolve(name):
    """(module, attribute, function) of a v1 dotted name, or None."""
    module_name, _, attr = name.rpartition('.')
    try:
        module = importlib.import_module(f'atomicdb.{module_name}')
    except Exception:       # noqa: BLE001 - a stale name in the map
        return None
    fn = getattr(module, attr, None)
    fn = getattr(fn, '__av2_original__', fn)
    if not inspect.isfunction(fn) or not (fn.__module__ or '').startswith('atomicdb'):
        return None
    return module, attr, fn


def is_setup(name):
    return name.split('.', 1)[0] in SETUP_MODULES or name in SETUP_FUNCTIONS


def make_wrapper(name, original, routed):
    strict = os.environ.get(STRICT_ENV, '').strip() in ('1', 'true', 'yes')

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        if _depth() > 0:
            return original(*args, **kwargs)
        stats = bridge.STATS
        if name in routed:
            stats.v2_calls[name] = stats.v2_calls.get(name, 0) + 1
            # Still at depth 0: the follow-up calls an entry asks for
            # (``$calls``) are routed like the test's own calls.
            return bridge.call_v2(name, args, kwargs)
        stats.v1_calls[name] = stats.v1_calls.get(name, 0) + 1
        if strict and not is_setup(name):
            raise NotPorted(f'{name} is not ported to v2 (ATOMICDB_V2_STRICT)')
        _enter()
        try:
            return original(*args, **kwargs)
        finally:
            _leave()

    wrapper.__av2_original__ = original
    wrapper.__av2_name__ = name
    return wrapper


def install(names=None):
    """Wrap ``names`` (default: the test map's functions and every ported
    name).  Returns the routed set."""
    if _installed:
        uninstall()
    routed = routed_names()
    wanted = set(names) if names is not None else test_map_functions()
    wanted |= routed
    originals = {}
    for name in sorted(wanted):
        found = _resolve(name)
        if found is None:
            continue
        module, attr, fn = found
        wrapper = make_wrapper(name, fn, routed)
        _installed[name] = (fn, wrapper)
        originals[id(fn)] = wrapper
    # Replace every reference by name in the loaded atomicdb modules (a
    # module that did ``from .ingest import x`` holds its own reference).
    for mod_name, module in list(sys.modules.items()):
        if module is None or not (mod_name == 'atomicdb' or mod_name.startswith('atomicdb.')):
            continue
        if mod_name.startswith('atomicdb.v2shim'):
            continue
        for attr, value in list(vars(module).items()):
            wrapper = originals.get(id(value))
            if wrapper is not None and getattr(value, '__av2_original__', None) is None:
                setattr(module, attr, wrapper)
                _patched.append((module, attr, value))
    return routed


def uninstall():
    while _patched:
        module, attr, value = _patched.pop()
        setattr(module, attr, value)
    _installed.clear()


def installed():
    return dict(_installed)
