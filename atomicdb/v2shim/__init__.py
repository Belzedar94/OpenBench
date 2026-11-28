"""Run v1's tests against AtomicDB v2 (plan docs/atomicdb/v2-plan.md, 7.1).

The switch is one environment variable::

    ATOMICDB_ENGINE=v2 python manage.py test atomicdb \\
        --testrunner atomicdb.v2shim.runner.ShimRunner --portability portable

With ``ATOMICDB_ENGINE`` unset (or ``v1``) nothing is patched and the same
command runs the tests on v1.  With ``v2``, every v1 function that v2 has
ported (the names ``av2.entries()`` lists, registered in Rust in
``atomicdb-v2/crates/av2-shim/src/registry.rs``) is routed to v2 when a test
calls it: the test's rows are exported into an in-memory v2 graph, the v2
function runs, and the rows it changed are written back to the Django models
the test asserts on (``bridge``).  Functions not ported yet keep running on
v1, and so does everything a v1 function calls inside itself, so a test runs
"setup on v1, act on v2" as soon as its act is ported (``intercept``).

Each test's report says which of its calls went to v2 and which still ran on
v1 (``runner``); ``atomicdb-v2/scripts/shim_report.py`` turns that into the
count of portable tests passing on v2 and the ratchet CI keeps.

Nothing here is imported by v1's own code paths.
"""

import os

ENGINE_ENV = 'ATOMICDB_ENGINE'


def engine():
    return os.environ.get(ENGINE_ENV, 'v1').strip().lower() or 'v1'


def is_v2():
    return engine() == 'v2'
