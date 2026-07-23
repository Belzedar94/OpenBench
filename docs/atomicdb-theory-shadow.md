# AtomicDB theory scheduler: trust boundary and shadow runbook

This feature imports public Atomic community research as an **untrusted
scheduling prior**. It is deliberately separate from the proof DAG.

## Non-negotiable boundary

Theory rows may affect only:

- an explainable proposed priority;
- the `THEORY` queue source, if and only if ACTIVE is explicitly unlocked;
- cohort labels and provenance shown in the explorer.

They never create or modify:

- `Position`, `Edge`, legal move coverage or `expanded`;
- engine evaluations or analysis results;
- `status`, `closure`, `proof`, witnesses or tablebase facts;
- USER requests or an already leased task.

`USER > THEORY > AUTO` is an explicit database ordering, not string order.
Multiple studies are provenance, not independent votes: a position receives
the strongest cohort boost once, capped at 12.

The canonical formal roadmap remains
`Atomic-Stockfish-solving-docs/docs/atomic/solving/`. Community evidence does
not satisfy that roadmap's proof boundary.

## Modes

`ATOMICDB_THEORY_SCHEDULER_MODE` has three values:

- `OFF`: compute and store only the original production priority.
- `SHADOW` (default): keep live priority and lease order byte-for-byte
  equivalent to OFF, while storing `shadow_priority` for imported matches.
- `ACTIVE`: apply the bounded prior and allow idle AUTO tasks to become
  THEORY.

ACTIVE is fail-closed. It additionally requires:

```text
ATOMICDB_THEORY_ACTIVE_ACK=atomic-theory-shadow-v1
```

Do not set that variable during R0/R1. ACTIVE belongs to a later, finite pilot
after the shadow scorecard passes its gates.

Runtime matching is pinned to both the policy and the authenticated portfolio:

```text
ATOMICDB_THEORY_POLICY_VERSION=atomic-theory-shadow-v1
ATOMICDB_THEORY_BUNDLE_SHA256=a6261fbf26b2eb4a80fac2b4ae545e16297c17db074c698d563bff7ba4790464
```

A row with the right policy but a different bundle hash is ignored. Cohort
identity is `(policy_version, slug)`, so a future policy can coexist with v1
for rollback without rewriting historical provenance.

## Pinned inputs

The local evidence bundle is outside the deployable OpenBench checkout:

```text
research/the-house-atomic-study-cohorts.json
research/the-house-atomic-priority-evidence.json
research/the-house-atomic-scheduler-seeds.json
research/the-house-atomic-study-pgn/<study_id>.pgn
```

Pinned digests:

| Input | SHA-256 |
|---|---|
| Study-cohort manifest | `8ff04562fd5d5486b4543141a9d5083d2e660750e81f62acbc2e944c5a1ac2a4` |
| Priority-evidence manifest | `c6deabe6abbb903bef48b124d3ea22ea24e3e07fb23707347e34fc1a03d04a2a` |
| Canonical executable scheduler | `fd8ef7637deb52ee8fa0a22626b0db789ee9ae97429163f8712cbcaeb0f116ef` |
| Exact 21-PGN aggregate | `83132f3d01995e4e94b98eff9340fffe7c6588e629b4c8dba4ff2d432501d776` |
| Combined portfolio bundle | `a6261fbf26b2eb4a80fac2b4ae545e16297c17db074c698d563bff7ba4790464` |

The scheduler manifest is intentionally separate from narrative audit prose.
Every entry provides an exact starting FEN, SAN, UCI sequence, resulting FEN
and canonical key. Narrative phrases such as “branch through” are rejected,
never truncated.

The PGN bundle contains exactly the 21 audited study IDs. Every file is checked
against its pinned SHA-256. Its aggregate digest is SHA-256 over sorted UTF-8
records:

```text
study_id + NUL + pgn_sha256 + LF
```

With the production checkout at `/opt/openbench`, the command defaults to the
operator-installed, read-only bundle at `/opt/research`. Deploying code does
not install or import this data automatically.

The import command defaults to validation only:

```powershell
python manage.py import_atomic_studies `
  --study-root ..\research\the-house-atomic-study-pgn
```

Only after a successful dry run:

```powershell
python manage.py import_atomic_studies `
  --study-root ..\research\the-house-atomic-study-pgn `
  --apply `
  --receipt C:\sealed\atomic-theory-shadow-import-v1.json
```

The command creates two no-overwrite receipts. Given
`atomic-theory-shadow-import-v1.json`, it seals
`atomic-theory-shadow-import-v1.preflight.json` before opening the database
transaction, then persists the requested final receipt after a successful
commit. Repeating the same import with a fresh receipt pair must report zero
new rows. A changed cohort, changed provenance identity, unexpected active
cohort, or unexpected membership aborts the entire transaction.

## Shadow report

With SHADOW configured:

```powershell
python manage.py report_theory_shadow `
  --limit 25 `
  --output C:\sealed\atomic-theory-shadow-report-v1.json
```

The report compares the live and proposed rankings for matched positions,
shows cohort provenance and seals its canonical JSON body. It creates no task
and no public event.

The current decay denominator is intentionally conservative and lifetime
based: the larger of completed task count and `Position.visits`, plus recorded
core-hours (`elapsed_seconds * threads_at_lease`). It reaches zero after five
completed attempts or 50 core-hours. It is not described as “since last
progress”; a future resettable epoch requires an independently specified
progress event.

## Promotion gates

R1 can ship only in SHADOW and must demonstrate:

1. OFF and SHADOW produce the same live priorities and lease order.
2. Import and refresh leave all proof-bearing fields and all edges unchanged.
3. Import is 21/21 content-pinned, idempotent and transactionally fail-closed.
4. Only imported exact roots receive a shadow proposal.
5. Existing USER and leased work is invariant.
6. Migrations preserve existing AtomicDB rows on SQLite and the PostgreSQL CI
   path.

R2 ACTIVE remains blocked until a bounded portfolio, quotas and stop rules are
implemented and reviewed. Deployment of this document or SHADOW code is not
authorization to run more compute.
