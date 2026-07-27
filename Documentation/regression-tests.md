# Regression tests

OpenBench derives the regression tracker from existing `Test` and `Engine`
rows. It does not require a dedicated model or database migration.

## Naming convention

A periodic regression measurement is a finished `GAMES` test whose development
`Engine.name` starts with:

```text
regression-YYYYMMDD
```

Set `Test.dev_engine` to the engine being measured. The development side should
point at the current development revision. The base side is the frozen anchor
for the latest release; for example, its `Engine.name` can be `release-v1.0`.
Use the same time control, book, options, and network policy across measurements
that are intended to form one comparable progression series.

Only tests matching all of these conditions appear:

- `finished=True`
- `deleted=False`
- `test_mode="GAMES"`
- development name starts with `regression-`

The index is available at `/regression/`. Each engine has a page at
`/regression/<engine>/`, ordered from newest to oldest. The displayed Elo and
95% interval use OpenBench's existing result statistics; LOS uses the same
trinomial or pentanomial distribution selected for that test.
