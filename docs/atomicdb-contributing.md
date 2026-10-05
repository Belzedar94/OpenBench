# Contributing to AtomicDB

AtomicDB is the Django app in `atomicdb/`. It stores a persistent graph of
Atomic chess positions that volunteer machines deepen with Atomic-Stockfish.
A position is closed (`WHITE_WIN`, `BLACK_WIN` or `DRAW`) only through an
exact gate, listed in `atomicdb/models.py` as `Closure`: a terminal position,
a mate line re-verified move by move, a three-valued minimax over the complete
legal move list, a proof certificate replayed in full on the server, or a
tablebase result. Engine evaluations order the exploration and never close a
position.

The public instance is <https://belzedar.duckdns.org/atomicdb/>. Its
`/atomicdb/method/` page explains what closes a position, and `/atomicdb/docs/`
explains every number the explorer shows.

The app shares the Django project (`OpenSite/`) and the accounts of this
OpenBench fork. The default branch is `spell-runner`.

## Code map

| Area | Where |
|---|---|
| Data model | `atomicdb/models.py`: `Position` and `Edge` (the graph), `AnalysisTask`, `SolveTask`, `IngestJob`, `Campaign`, `ProofCampaign` and `ProofNode`, `DBEvent`, `WorkerPing`. Migrations in `atomicdb/migrations/`. |
| Exact core | `atomicdb/logic.py`: canonical position identity, move generation through `pyffish`, mate-line verification, three-valued backup. `atomicdb/tb.py`: server-side Syzygy probing. |
| Ingest pipeline | `atomicdb/ingest.py` is the only writer of the graph: child upsert, local closures, the backup cascade, events. `atomicdb/ingest_queue.py` and `manage.py process_ingest_queue` apply worker submissions outside the HTTP request. |
| Selector | `ingest.refresh_priorities`, driven by `manage.py refresh_selector`. Proof numbers in `atomicdb/proof.py`, the solve-cost estimate in `atomicdb/solve_estimate.py`. Design notes: `docs/selector-incremental.md`, `docs/solver-allocation.md`. |
| Certificates | `atomicdb/solve.py` (forced wins) and `atomicdb/survive.py` (fifty-move fortresses). Both replay a certificate with `pyffish`, independently of the engine that produced it. |
| Request queue | `atomicdb/live_request.py` (service order, place in the queue), `atomicdb/lanes.py`, `atomicdb/depth.py`. Design: `docs/queue-fairness.md`. |
| Views and explorer | `atomicdb/views.py`, `atomicdb/urls.py`, `atomicdb/templates/atomicdb/`, `atomicdb/static/atomicdb/`. |
| Front page and caches | `atomicdb/metrics.py`, `atomicdb/page_cache.py`, `atomicdb/revalidate.py`. |
| People | `atomicdb/contributors.py` (contributor pages), `atomicdb/notifications.py`. |
| Opening names | `atomicdb/openings.py`, `atomicdb/data/atomic_openings_v1.json`, `atomicdb/community_names.py`. Policy: `docs/atomicdb-opening-authority.md`. |
| Move tree | `atomicdb/conquest_map.py` and `manage.py build_conquest_map`. Design: `docs/atomicdb-conquest-map.md`. |
| Worker client | `Client/atomicdb_worker.py`, a single file that does not depend on the OpenBench client. |
| Maintenance commands | `atomicdb/management/commands/`. |
| Tests | `atomicdb/tests.py` and `atomicdb/test_*.py`; base classes in `atomicdb/testing.py`. |

The values the explorer displays follow `docs/value-semantics.md`.

## HTTP interface

Routes are declared in `atomicdb/urls.py` and served under `/atomicdb/`.

Public API, described for users on `/atomicdb/docs/`:

- `GET api/query?fen=...` returns the stored row and analysis of a position.
- `POST api/request` asks for analysis of a FEN. `username` and `password`
  are optional and put the request on an account.
- `POST api/my-queue/` with `username` and `password` lists the pending and
  running requests of that account. A signed-in browser can `GET` it.
- `POST api/queue-bump/<id>/` and `POST api/queue-cancel/<id>/` move one of
  your own requests to the front of your own queue, or withdraw it
  (`undo=1` restores it). Credentials go in the body, as form fields or JSON.
- `GET api/map/v1` serves the published move-tree snapshot.

The explorer page polls `api/query`, `api/frontier/<key>/` and
`api/live-request/<key>/`.

These endpoints are rate limited by nginx, not by Django: 5 requests a second
per client address with bursts of 50, answered with `429` and `Retry-After`
past that. The worker protocol below is deliberately left out of the limit.
The site configuration that does it is kept in
`docs/server/nginx-openbench.conf`.

Worker protocol: `api/lease`, `api/heartbeat` and `api/submit` for analysis
tasks; `api/solve/acquire`, `api/solve/heartbeat` and `api/solve/submit` for
proof tasks. The worker authenticates with an OpenBench account whose profile
is enabled.

## Running the tests

`.github/workflows/datagen-postgres.yml` runs on every pull request, on
Python 3.12:

```console
pip install -r requirements.txt -r Client/requirements.txt
python manage.py test atomicdb --verbosity 2
python manage.py makemigrations --check --dry-run
python manage.py check
python -m unittest discover -s UnitTests -t UnitTests -v
```

The Django suite needs no configuration on a fresh clone. Without database
environment variables the settings select SQLite, the test runner creates its
own test database, and the cache is forced to the in-process backend. While
working on one area, run only its module or class:

```console
python manage.py test atomicdb.test_queue_control
python manage.py test atomicdb.test_queue_control.ScriptedQueueDoorTests
```

CI runs the AtomicDB suite on two layouts: PostgreSQL (the
`OPENBENCH_POSTGRES_*` variables), and SQLite with AtomicDB split into its own
database file. A change has to pass on both.

Test cases derive from `atomicdb.testing.TestCase` or
`atomicdb.testing.TransactionTestCase`. They allow both database aliases and
start every test with an empty cache. `atomicdb.testing.worker_account`
creates an account that the worker protocol accepts.

## Running a local instance

With no environment set, `OpenSite/settings.py` uses development defaults:
`DEBUG` on, a placeholder secret key, and a SQLite database at `db.sqlite3` in
the repository root. CI prepares a clone with:

```console
python manage.py migrate --noinput
```

`AGENTS.md` (section 2) describes starting a disposable local server with
`python manage.py runserver`. This project overrides `runserver`
(`OpenBench/management/commands/runserver.py`) so that it also starts the
OpenBench artifact and PGN watchers. `Config/config.json`, `Engines/*.json`
and `Books/*.json` are read at startup.

In production three more processes work on the AtomicDB data. The unit files
in `Documentation/` show the commands:

- `manage.py process_ingest_queue` applies worker submissions to the graph
  (`atomicdb-ingest.service`). `--once` drains what is ready and exits.
- `manage.py refresh_selector --loop` recomputes the exploration priorities
  (`atomicdb-selector.service`). Setting `ATOMICDB_INLINE_SELECTOR=1` makes
  the web process do it inline.
- `manage.py warm_public_snapshot` and `manage.py recascade_backed` run on
  timers.

The worker's own usage line is
`python atomicdb_worker.py -U user -P pass -S https://server -T 8`; `--engine`
points it at a local engine build.

A local instance is for disposable testing. `docs/operations.md` sets the
rule: never point a development worker at production, and never run workers
against a copy of the live queue.

## Proposing a change

1. Fork the repository and create a branch from `spell-runner`.
2. Keep one idea per pull request. A fix and an unrelated cleanup are two
   pull requests.
3. Include tests. A behaviour change comes with a test that fails without it,
   and a bug fix comes with a test that reproduces the report. The existing
   tests are named after the behaviour they defend; follow that style.
4. Check that `python manage.py test atomicdb` passes and that
   `python manage.py makemigrations --check --dry-run` reports nothing.
   Migrations in `atomicdb/migrations/` form one numbered chain; add yours
   at the end.
5. Open the pull request against `spell-runner` and describe what changes
   for a visitor, a worker or an operator.

Write code comments, documentation, commit messages and pull requests in
English. Part of the existing code and several design notes are in Spanish;
new text is not.

Do not commit credentials or instance data. `.gitignore` already excludes
`Config/secret.key`, credential files, the SQLite databases and `Media/`.

Some changes need agreement before code. Discuss them first, in a draft pull
request or on the Discord server linked under Support in the sidebar of the
OpenBench pages:

- **Value semantics.** `docs/value-semantics.md` states its own process:
  edits to the semantics land in that document first, then in code.
- **What counts as proved.** The ruleset is frozen as
  `ATOMICDB_RULESET_ID` in `OpenSite/settings.py`, and its meaning is written
  in the docstring of `atomicdb/logic.py`. Changing closure rules changes
  what every existing closure claims.
- **Anything under `Client/`.** OpenBench workers download and run the
  commit named by `client_repo_ref` in `Config/config.json`, and CI requires
  that commit to carry exactly the `Client/` being merged. The AtomicDB
  worker updates itself from the server and carries `ATOMICDB_WORKER_BUILD`,
  which must increase with every published change and is never reused.
- **Opening names.** The catalogue is validated against a digest
  (`manage.py validate_atomic_openings`) and follows
  `docs/atomicdb-opening-authority.md`. Names are proposed through the
  suggestion form of the explorer, not by editing the JSON.

## Good first areas

- **The open questions of `docs/value-semantics.md`.** The document lists two
  and asks for the community's preference: whether a newer line claim should
  replace an older own search in a move row, and how claims are marked.
- **The user documentation pages.** `templates/atomicdb/docs.html` and
  `method.html` describe the API and the queue by hand.
  `atomicdb/test_docs_page.py` pins the sections and requires quoted limits
  to come from the constants that enforce them.
- **The move tree front end.** `static/atomicdb/conquest-map.js` and
  `conquest-map.css` work from a published snapshot and never query the live
  database. `docs/atomicdb-conquest-map.md` lists the accessibility and
  layout gates and the focused checks to run.
- **Explorer templates and styles.** `templates/atomicdb/` and
  `static/atomicdb/atomicdb.css`, covered by page tests such as
  `test_explorer_header.py` and `test_profile.py`.
- **English versions of the design notes.** `docs/solver-allocation.md` and
  `docs/selector-incremental.md` exist only in Spanish.

`Documentation/postgres-migration.md` (sections 7 and 8) records two larger
pieces that are designed and not built: surrogate keys for positions and
edges, and an authenticated legal-move inventory. They are projects, not first
contributions.
