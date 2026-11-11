"""Proof-directed planner for AtomicDB (docs/atomicdb-planner-design.md).

Phase 1 is SHADOW only: the planner keeps the searched subgraph in memory,
computes cost-unit proof numbers (expected engine nodes still missing to
prove WHITE_WIN), selects the batch of work it would serve and records it
next to what the fleet was actually given.  It serves nothing and writes
nothing but its own two tables.

    costmodel  the fitted cost model (ln C ~ eval^-k, side to move) and the
               restricted-mean proof number of a leaf; versioned JSON
    graph      the in-memory graph (numpy columns) and its read-only loader
    master     pn/dn algebra (OR = min, AND = sum), full and incremental
               recompute, virtual loss and flags
    select     batch selection with the "4x worse than the best" stop
    shadow     one shadow cycle: refresh, pick, record, metrics
    calibrate  the read-only calibration export and the censored fit

Switch (``ATOMICDB_PLANNER``): ``off`` | ``shadow``.  ``fill``, ``auto`` and
``idle`` are phase 2 and refused by ``run_planner``.
"""

MODES = ('off', 'shadow')
PHASE2_MODES = ('fill', 'auto', 'idle')
