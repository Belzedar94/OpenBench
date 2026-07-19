# OpenBench operations

The permanent service is a separate production environment. Development and
migration validation for this repository must use an isolated clone, database,
Media tree, and client endpoint (conventionally `:8001`). Never point a dev
worker at production and never place credentials in commands committed to Git,
logs, manifests, receipts, or status files.

## Atomic Syzygy DATAGEN v40 gate

Before any production rollout:

1. Back up the development DB and Media tree, then run `python manage.py
   migrate`, `python manage.py check`, and `python manage.py makemigrations
   --check --dry-run`.
2. Run the focused server and client suites documented in
   `docs/datagen-mode.md`, including the legacy DATAGEN cases.
3. Confirm a matching Atomic worker receives a v40 lease and a wrong family,
   insufficient cardinality, or mismatched manifest receives no work.
4. Confirm upload creates a receipt, identical retry is idempotent, requeue
   clears attempt evidence, and the final manifest contains no local path.
5. Keep `syzygy_adj=DISABLED` and run both depth-7 canaries locally only after
   the engine bridge and golden tests pass. Compare `pure` and `true`
   (legacy-playing) output with the format auditor before approval.

Production deployment is a separate, explicitly authorized operation. It
requires an idle-window DB/Media backup, migration `0007`, server restart,
gradual worker-v40 restart, and a one-chunk smoke test. Do not use `--fake` for
`0007`, do not deploy midway through an active campaign, and retain the prior
application and backup for rollback.
