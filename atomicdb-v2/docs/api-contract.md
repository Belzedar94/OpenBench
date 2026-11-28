# AtomicDB v2: the outside contract (workers and public JSON API)

Plan section 6 says the outside stays the same: no worker updates for the
cut-over and no API client notices it. This document is that promise written
down endpoint by endpoint, from the v1 code (`atomicdb/views.py`,
`roots.py`, `conquest_map.py`, `contributors.py`, `live_request.py`,
`ingest.py`, `engine_policy.py`), the worker client
(`Client/atomicdb_worker.py`, build 2026112501, the copy served at
`/atomicdb/engines/atomicdb_worker.py`) and production's nginx
configuration, read on 28-Nov-2026.

What checks it:

| Check | Where | What it covers |
|---|---|---|
| Golden file of recorded v1 responses | `atomicdb-v2/tests/golden/v1-20261128/` | every public read endpoint, ~3,000 responses sampled from production |
| SQL fixture | same directory, `fixture-rows.jsonl.gz` | the rows behind a subset of those responses, so v2 can be loaded with exactly what v1 answered from |
| Shapes from the v1 test suite | `atomicdb-v2/tests/golden/v1-shapes/calls.jsonl.gz` | worker protocol and authenticated calls, as v1's own tests drive them |
| Replay and diff | `atomicdb-v2/crates/av2-compat` (`av2-compat replay`) | replays the golden requests against a base URL and diffs field by field |
| Worker protocol scenarios | `av2-compat worker` | lease, heartbeat, submit, release, stale lease, not-your-lease, not-leased, engine-not-accepted, against a base URL; their run against a local v1 is kept in `tests/golden/v1-shapes/worker-scenarios-gate-{off,on}.jsonl` |

A difference found by any of these is a failure unless it is listed in
section 10 as an owner decision.

## 0. Production and the repository are not the same today

Production is patched file by file, so the repository's `atomicdb-v2` branch
(the base of v2) is a little ahead of it. Found on 28-Nov-2026 by diffing the
deployed `atomicdb/*.py` against the branch, ignoring line endings:

| File | Difference that reaches the API |
|---|---|
| `views.py`, `api_my_queue` | Repo answers **405** `{"error": "GET or POST only"}` to a method other than GET/POST. Production has no such branch: a PUT/DELETE falls to the session check and answers **401** `{"error": "login required"}` (anonymous) or the queue (signed in). The golden file records production's 401. |
| `views.py`, `api_queue_bump_script` | Repo forgets the cached queue place after a bump (`live_request.invalidate_queue_ahead`). Body unchanged. |
| `views.py`, `api_pref_lifo` | Not part of this contract (a page form). |
| `live_request.py` | The cached queue-place key helper; no body change. |
| `ingest.py` | `ONLINE_MATE_PROOF_SECONDS = 1.0` hot-patch (repo 2.5). No body change. |

`urls.py`, `roots.py`, `conquest_map.py`, `contributors.py`,
`engine_policy.py` and `depth.py` are identical. **v2 follows the repository**
where the two differ (the repo is what will be deployed next); the golden
file's one 405/401 case is marked accordingly in `normalize.json`
(`repo_vs_prod`).

## 1. Conventions shared by every endpoint

### 1.1 Encoding

Every JSON body except `api/map/v1` is Django's `JsonResponse`:

- `Content-Type: application/json` (no charset parameter).
- Python `json.dumps` defaults: separators `", "` and `": "`, **`ensure_ascii=True`**
  (every non-ASCII character is written as `\uXXXX`, surrogate pairs above
  the BMP), keys **in insertion order** (the order this document lists them),
  no trailing newline.
- Integers as integers, Python floats with `repr` (`0.5`, `1e-05`,
  `12.0`): a float that happens to be whole keeps its `.0`. `NaN`/`Infinity`
  never occur.
- `null`, `true`, `false` as JSON.

`api/map/v1` is different on purpose (it is a content-addressed artifact):
`sort_keys=True`, separators `","` and `":"`, **`ensure_ascii=False`** (UTF-8),
and gzip when the client accepts it. See section 3.7.

Every response of the site also carries the headers of Django's security
middleware, and v2 must send them too: `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
`Cross-Origin-Opener-Policy: same-origin`. A view that touches the session
(`live-request`) adds `Vary: Cookie`.

To be byte-compatible v2 has to reproduce these exactly. The golden file
keeps the raw body text for that reason; `av2-compat replay --bytes` compares
bytes where no field is normalized away.

### 1.2 Authentication

Three kinds, never mixed within one endpoint door:

- **Worker credentials**: `username` and `password` form fields (POST,
  `application/x-www-form-urlencoded` or multipart), checked by `_auth`:
  Django's `authenticate` **and** `OpenBench.models.Profile.enabled = True`.
  Failure is always **403** `{"error": "bad credentials"}` (disabled account
  and wrong password are indistinguishable on purpose). A success is cached
  for **300 s** under an HMAC of the pair; every cache hit re-checks in one
  query that the account is active, the profile enabled and the stored
  password hash unchanged, so a password change or a disable takes effect on
  the next request. Failures are never cached.
- **Script credentials** (`my-queue`, `my-workers`, `queue-bump`,
  `queue-cancel`): the same pair and the same check, read from the form or,
  when the form has no `username` and the body is `application/json`, from a
  JSON object `{"username": ..., "password": ...}`.
- **Session**: the Django session cookie (`sessionid`), only for the GET doors
  of `my-queue`/`my-workers` and for `live-request` (which personalises one
  sentence).

The worker doors and the script doors are CSRF-exempt and **never read the
session cookie**. Views that are not CSRF-exempt (`api/query`,
`principal-variations`, `backed-source`, `frontier`, `live-request`, `roots`,
`map/v1`) answer a POST without a CSRF token with Django's **403 HTML** page
(`text/html; charset=utf-8`, title "403 Forbidden"); this is recorded in the
golden file and v2 must keep a 403 (the body may be v2's own HTML: listed in
`normalize.json` as `csrf_page`).

### 1.3 Rate limits (nginx, unchanged by v2)

From `/etc/nginx/sites-enabled/openbench` on production:

| Zone | Key | Rate | Burst | Applies to | Refusal |
|---|---|---|---|---|---|
| `atomicdb_api` | client IP | 10 r/s | 100, `nodelay` | `^/atomicdb/api/(query\|principal-variations\|backed-source\|request\|request-children\|live-request\|frontier\|my-queue\|my-workers\|queue-bump\|queue-cancel\|map\|roots)(/\|$)` | **429**, `application/json`, `Retry-After: 2`, body `{"status":"rate-limited","detail":"Too many requests from this address. The API allows 10 requests/second (bursts up to 100). Wait a moment and retry."}` followed by a newline |
| `crawlers` | User-Agent class (`meta`, `crawler`, else none) | 6 r/m | 12 | `^/atomicdb/(goto\|explore\|lite\|backed-source\|proven-line-end)/` (pages, not the API) | 429 HTML "AtomicDB is busy", `Retry-After: 10`, `Cache-Control: no-store` |
| `tree_anon` | one bucket for every request without `sessionid` | 4 r/s | 12 | same page locations | same |

The worker protocol (`api/lease`, `api/heartbeat`, `api/submit`,
`api/release`, `api/solve/*`) matches none of these and is **not rate
limited** ("a contributor with many threads behind one IP is legitimate").
`/atomicdb/engines/` is a static `alias /var/www/atomicdb-engines/`.
Everything else proxies to `127.0.0.1:8000` with `proxy_read_timeout 300`,
`client_max_body_size 2g`. gzip is on for `application/json` responses of
512 bytes and more (`gzip_vary on`, so `Vary: Accept-Encoding` is added by
nginx), level 5.

v2 keeps nginx in front with this configuration; only the upstream of
`/atomicdb/` changes at the cut-over (plan section 8.5).

### 1.4 Caching

| Endpoint | Server-side cache | Response headers |
|---|---|---|
| `api/roots`, `api/roots/` | `page_cache.stale_while_revalidate`, fresh 30 s; an expired copy is served at once and one thread rebuilds it | `Cache-Control: max-age=30`, `Expires` (+30 s from the build of the copy served) |
| `api/map/v1` | shared cache of rendered views of the published snapshot, 6 h, keyed by snapshot signature and query | `ETag: W/"<sha256 of the uncompressed body>"`, `Cache-Control: public, max-age=60, stale-while-revalidate=300`, `Vary: Accept-Encoding`; `If-None-Match` answered with **304** and no body; errors 503 carry `Cache-Control: no-store` |
| everything else in this document | **none** ("live state") | none set by the view |

### 1.5 Signs, perspectives and constants

- The tree stores **White-POV** values. The public API turns them to the
  **side to move** of the position being described (chessdb.cn convention),
  exactly one sign change per ply shown. Exceptions, which are White-POV:
  `api/roots` (`"perspective": "white"`), `api/map/v1`
  (`"semantics": {"pov": "white"}`) and `analysis.passes[*].eval_cp`/`mate`
  (stored pass summaries, passed through unflipped).
- A proven win is `10000`, a proven loss `-10000`, a proven draw `0`
  (`score`, from the side to move). `best_known_eval`: proven status, else
  `backed_eval`, else `eval_cp`.
- Engine mate scores reach the store as `eval_cp = (10000 - |mate|) * sign`,
  and `mate` in moves (UCI), White-POV, both from the worker
  (`Client/atomicdb_worker.py`, `Engine.analyse`). `cp` scores are clamped by
  the worker to `[-9500, 9500]`.
- `status`: `UNKNOWN`, `WHITE_WIN`, `BLACK_WIN`, `DRAW`.
- `closure`: `null`, `TERMINAL`, `TB`, `MATE_PV`, `MINIMAX`, `SOLVE`.
- `trust`: `VERIFIED` (closure TERMINAL, TB or SOLVE), else the `proof`
  column (`ANDOR`, `ENGINE`, `DISPUTED`, `TB_LINE`) when set, else
  `UNCLASSIFIED` (MATE_PV, MINIMAX), else `UNPROVEN`.
- `tier` is always `"PRACTICAL"`; `history_scope` always
  `"COUNTERS_AND_REPETITION_IGNORED"`.
- Keys are `sha256(canonical FEN)` in lower-case hex (64 characters). The
  canonical FEN has the counters set to `0 1` and keeps an en-passant square
  only when the capture is executable.
- Request budgets: the ladder `128000000`, `512000000`, `2000000000`; the
  subtree stop `10000000000` (up to `78` searches of `128000000` below the
  position).

### 1.6 Public FEN parsing (every endpoint that takes `fen`)

`_parse_public_fen`, in this order:

1. `_` is read as a space, runs of whitespace collapse.
2. Empty: refused (`empty`, "no FEN given"). Longer than 100 characters:
   refused (`malformed`, "FEN is too long").
3. Must match
   `^[pnbrqkPNBRQK1-8]+(/[pnbrqkPNBRQK1-8]+){7} [wb] (-|[KQkqA-Ha-h]+) (-|[a-h][36])( \d+)?( \d+)?$`
   (`malformed`, `expected "board side castling en-passant [halfmove] [fullmove]"`).
4. Exactly one `K` and one `k` (`kings`, "an atomic position needs exactly
   one king per side"); every rank 8 squares (`ranks`, "every rank must
   describe 8 squares").
5. Missing counters are filled with `0` and `1`; the FEN is canonicalised and
   pyffish must accept it (`illegal`, "the atomic rules reject this
   position").

The read endpoints collapse every failure into `{"error": "invalid fen"}`;
the write endpoints (`api/request`, `api/request-children`) return the
sentence: `{"status": "refused", "reason": "<the sentence above>"}`, or
`"that FEN could not be read"` for anything unexpected.

## 2. Worker protocol

All four analysis endpoints and the three SOLVE endpoints are `POST`, form
encoded, CSRF-exempt, with worker credentials, and are never cached or rate
limited. The worker treats **any 2xx with a well-formed JSON body** as final,
**any 4xx as definitive**, and retries 5xx, 408, 425, 429 and transport
errors (`_request_tasks`, `_submit_until_definitive`). v2 must keep that
split: a 5xx is "try again with the same nonce", a 4xx is "stop".

### 2.1 Fields every worker call carries

Sent by build 2026112501 on every call (`auth_base`):

| Field | Type | Meaning on the server |
|---|---|---|
| `username`, `password` | string | worker credentials (1.2) |
| `machine` | string, cut to 64 | the leaseholder identity: `<user>-<host>-atomicdb`, or `<first 60 chars>#<slot>` with `--jobs K`. The physical machine is the part before `#` (`machine_base`). Defaults to the username when absent. |
| `threads` | int | threads per engine of this slot, clamped to `[0, 1024]`, unparseable -> 0 |
| `hash` | int, MB | `max(0, int)`, unparseable -> 0 |
| `tb` | `"1"`/`"0"` | whether the worker probes Syzygy itself (lease only) |
| `os` | string, cut to 64 | shown on pages |
| `worker_build` | int | `0 < build <= 2147483647`, else "no build"; lease tokens need `>= 2026072203` |
| `max_budget` | positive int string, optional | largest task this worker takes (lease and solve/acquire only) |
| `engine_sha`, `net_sha` | 64 lower-case hex, optional | provenance (submit only; anything else is stored as `""`) |

Every authenticated worker call refreshes the `WorkerPing` row
`(machine, user)` with `threads`, `hash_mb`, `os`, `last_seen` and, when a
usable build is sent, `worker_build` (`_touch_worker`). No lease is changed by
that refresh.

### 2.2 `POST /atomicdb/api/lease`

Request: 2.1 plus `lease_session` (string, cut to 64; a nonce the worker
keeps for one logical request and resends unchanged on retries).

Errors, checked in this order:

| Status | Body | When |
|---|---|---|
| 403 | `{"error": "bad credentials"}` | 1.2 |
| 400 | `{"error": "max_budget must be a positive whole number of nodes"}` | `max_budget` present and not a positive integer (checked before anything is written) |

Note: `api/lease` does **not** check the method; a GET runs the same code
with an empty form and is answered 403.

Success, **200**:

```json
{"tasks": [{"id": 123, "fen": "<canonical fen>", "budget_nodes": 128000000,
            "multipv": 4, "searchmoves": ["e2e4", "d2d4"],
            "lease_token": "<43 chars, token_urlsafe(32)>",
            "source": "USER", "requested_by": "someone",
            "line": "1. e4 e5 2. Nf3"}]}
```

- `tasks` holds **zero or one** task. An empty list means "nothing for you
  now, ask again later": no work, the machine already holds
  `LEASES_PER_MACHINE = 32` live analysis leases across its slots, or a live
  lease of this exact `machine` is held by another session whose heartbeat
  is younger than `RESTART_RECYCLE_MINUTES = 3`.
- `searchmoves`: the moves of the position still `UNKNOWN`, only when some
  but not all children are closed; `[]` otherwise (no restriction).
- `lease_token`: `""` for a worker older than build 2026072203 (no fencing).
- `source`: `USER`, `AUTO`, `FILL` (and the sweep's own source);
  `requested_by`: account name or `""`; `line`: SAN line from the root as the
  workers page shows it, `""` when no label is cached yet (never blocks the
  lease). These three are additive and for the console only.
- **Replay**: if the same `machine` already holds a live lease and the
  request carries the **same `lease_session`** (and a token-capable build),
  the same task and token are returned again and `attempts` is not counted.
  This is what makes a lost response harmless.
- **Restart takeover**: same `machine`, different session, holder's
  heartbeat older than 3 minutes: the old lease is recycled and this request
  picks a task at once.
- Before choosing, every call recycles leases silent for
  `LEASE_MINUTES = 4` (heartbeat or lease time), tokenless legacy leases
  after 60 min of silence or 24 h without any heartbeat, and absorbs
  recycled tasks whose position closed meanwhile.
- Choosing (what to serve first) is the selector's semantics, not this
  contract; the shape above is.

### 2.3 `POST /atomicdb/api/heartbeat`

Request: 2.1 plus `nps` (int, clamped to `[0, 10^12]`), and while searching
`task_id` and `lease_token`. A heartbeat without `task_id` only says the
machine is alive and clears its current task.

| Status | Body | When |
|---|---|---|
| 405 | `{"error": "POST required"}` | not POST |
| 403 | `{"error": "bad credentials"}` | |
| 400 | `{"error": "invalid nps"}` | `nps` not an integer |
| 400 | `{"error": "invalid task_id"}` | `task_id` present and not an integer |
| 409 | `{"error": "stale-lease"}` | the task is not `LEASED` to this `machine` under this token (a tokenless lease matches any token) |
| 200 | `{"ok": true, "machine": "<machine>", "current_task_id": 123}` | renewed; `current_task_id` is `null` when no task was named |

A 409 whose `error` is one of the worker's lost-lease errors makes the worker
stop that search (`HEARTBEAT_LOST_LEASE_ERRORS`). The worker beats every
60 s with a 30 s read timeout.

### 2.4 `POST /atomicdb/api/submit`

Request: 2.1 plus

| Field | Type | Rules |
|---|---|---|
| `task_id` | int | required |
| `lines` | JSON text | required, at most 512 KiB UTF-8, a list of at most 32 objects; each object: `move` non-empty string <= 16 chars; `eval_cp`, `mate` int or null (booleans refused); `pv` list of <= 512 strings of <= 16 chars, or null; `raw` string <= 65,536 chars or null. Extra keys are kept. `eval_cp`/`mate` are **White-POV** (the worker flips them). A TB result sends `[]`. |
| `elapsed` | float string | clamped to `[0, 86400]`, unparseable or non-finite -> 0 |
| `nodes` | int | `max(0, int)`, unparseable -> 0; stored as `min(nodes, 2 * budget_nodes)` |
| `tb_wdl` | int, optional | Syzygy WDL; unparseable -> 400 |
| `tb_dtz` | int, optional | ignored when unparseable or `abs > 1024` |
| `searchmoves` | space separated UCI, optional | the restriction the search ran with; non-empty marks the pass `restricted` |
| `lease_token` | string | must equal the task's token (constant-time compare) when it has one |
| `machine` | string | must equal the task's `machine` (here **not** defaulted to the username) |

Errors and answers, in the order v1 checks them:

| Status | Body | When |
|---|---|---|
| 403 | `{"error": "bad credentials"}` | |
| 400 | `{"error": "malformed: <reason>"}` | missing `task_id`/`lines`, not an int, bad JSON, any rule above. Reasons: `lines payload is too large`, `lines must be a list with at most 32 entries`, `each line must be an object`, `invalid root move`, `invalid eval_cp`, `invalid mate`, `invalid or excessively long PV`, `raw line is too large`, Python's own `KeyError`/`ValueError`/`JSONDecodeError` text otherwise (e.g. `malformed: 'task_id'`) |
| 400 | `{"error": "malformed: invalid tb_wdl"}` | |
| 400 | `{"error": "malformed: unknown task"}` | no such task |
| 400 | `{"error": "not-leased"}` | task is neither `LEASED` nor `COMPLETED` (PENDING after a recycle or release, CANCELLED, ...) |
| 409 | `{"error": "not-your-lease"}` | `machine` empty or not the task's |
| 409 | `{"error": "stale-lease"}` | token mismatch |
| 200 | `{"ok": true, "dup": true}` | the task is already `COMPLETED` (a retried submit whose first answer was lost) |
| 403 | `{"error": "engine-not-accepted", "detail": "AtomicDB only takes analysis from the reference engine. Start the worker without --engine and it downloads the reference build; to get your own build accepted, ask on the Belzedar Labs Discord."}` | the engine gate is enforcing, the result is not a TB result, and `engine_sha` is not a reference binary (`Atomic-Stockfish-*` in the engines directory) or an `extra_engines` entry of `/etc/openbench/atomicdb-engine-policy.json`. The task goes back to `PENDING` at once. **The policy file does not exist on production (gate off), so production never answers this today.** |
| 200 | `{"ok": true, "summary": {"queued": true, "ingest_job": 456, "submit_timing_seconds": {"transaction": 0.012, "apply": 0.0, "total": 0.034}}}` | accepted; the payload is queued for the ingest and applied later |

Race outcomes inside the claim (another submit or a recycle between the read
and the update) map to the same bodies: `dup`, `not-leased`, `stale-lease`
(same machine, different attempt/lease time/token) or `not-your-lease`.

With `ATOMICDB_SYNCHRONOUS_INGEST` (off in production) the `summary` is the
ingest's own result dict, or `{"ingest_job": N, "ingest_error": "..."}`, plus
`submit_timing_seconds`. v2 has no such switch; the asynchronous body is the
contract. `submit_timing_seconds` values are volatile (normalized).

### 2.5 `POST /atomicdb/api/release`

Request: 2.1 plus `leases`, a JSON list of at most
`RELEASE_MAX_LEASES = 33` objects
`{"kind": "analysis"|"solve", "task_id": <int 1..2^63-1>, "lease_token": "<str>", "machine": "<str, default the caller's machine>"}`.

| Status | Body | When |
|---|---|---|
| 405 | `{"error": "POST required"}` | not POST |
| 403 | `{"error": "bad credentials"}` | |
| 400 | `{"error": "malformed: <reason>"}` | `leases is not JSON`, `leases must be a list`, `at most 33 leases per call`, `each lease must be an object`, `kind must be analysis or solve`, `invalid task_id`, `lease_token and machine must be strings`. Nothing is released by half. |
| 200 | `{"ok": true, "released": N}` | `N` = analysis plus solve leases put back |

A lease is put back only while it is still `LEASED` to that machine under
that token; entries with an empty token, or naming a machine whose base is
not the caller's base, are ignored. Stale tokens, other machines' leases,
completed or cancelled tasks and repeated calls are no-ops (counted 0). A
worker that gets a 404 here (an old server) exits as before.

### 2.6 SOLVE endpoints (additive protocol)

`POST /atomicdb/api/solve/acquire`: 2.1 plus `lease_session`.
403 bad credentials; 400 bad `max_budget` (same body as lease); 200
`{"tasks": []}` or
`{"tasks": [{"id": 9, "fen": "...", "goal": "...", "stage": 0, "budget_nodes": 1000000, "ruleset": "<logic.RULESET_ID>", "lease_token": "..."}]}`.
Same replay and restart-takeover rules as `api/lease`; the lease window is
`SOLVE_LEASE_MINUTES = 20`.

`POST /atomicdb/api/solve/heartbeat`: 405 `{"error": "POST required"}`; 403;
400 `{"error": "invalid task_id"}`; 409 `{"error": "stale-lease"}`; 200
`{"ok": true, "current_task_id": 9}`.

`POST /atomicdb/api/solve/submit` (multipart with a `certificate` file, at
most `solve.MAX_COMPRESSED_BYTES`): 403; 400
`{"error": "malformed: task_id"}`, `{"error": "malformed: outcome"}`
(`outcome` must be `PROVED`, `DISPROVED` or `UNKNOWN`),
`{"error": "malformed: unknown task"}`, `{"error": "not-leased"}`; 409
`{"error": "not-your-lease"}`, `{"error": "stale-lease"}`; 413
`{"error": "certificate too large"}`; 200 `{"ok": true, "dup": true}` or
`{"ok": true, "summary": {...}}` (the dict `ingest.apply_solve_result`
returns). Advisory fields: `pn`, `dn`, `nodes`, `elapsed`, `solver_build`,
`fortress_tt_hit|quiet_scc|reset_rate|stagnation|score`.

### 2.7 Engine manifest and auto-update (static files, nginx)

Served by nginx from `/var/www/atomicdb-engines/` (`location /atomicdb/engines/`),
not by Django: v2 does not serve them and they do not move.

| Path | What the worker does with it |
|---|---|
| `GET /atomicdb/engines/manifest.json` | `{"engine": str, "cpu": str, "net": {"file", "sha256", "size_mb"}, "binaries": {"windows-x86_64"\|"linux-x86_64"\|"darwin-arm64": {"file", "sha256", "size_mb", "needs_net"?: bool, "bench"?: {...}}}, "bench": {"nnue": int}}`. `file` must be a bare name; `sha256` 64 hex. Re-read every few minutes for the engine hot swap. |
| `GET /atomicdb/engines/<file>` | downloaded and sha256-verified before use |
| `GET /atomicdb/engines/atomicdb_worker.py?current_build=<build>&_=<epoch>` | the auto-update: HTTPS only, no redirects (3xx is an error), `Content-Encoding` must be identity, `Content-Length` checked, the source is parsed with `ast` and must declare `ATOMICDB_WORKER_UPDATE_PROTOCOL` and a larger `ATOMICDB_WORKER_BUILD`. The query string defeats caches and is ignored by nginx. |
| `GET /atomicdb/engines/syzygy-345/manifest.json` and the files it lists | `{"set": "atomic-syzygy-345", "files": [{"name", "bytes", "sha256"}]}` |

The API side of the engine is only `engine_sha`/`net_sha` on submit and the
gate of 2.4.

## 3. Public read API

All are `GET`, anonymous, CSRF-protected (so a POST is the 403 HTML of 1.2),
uncached unless stated, and inside the `atomicdb_api` rate limit.

### 3.1 `GET /atomicdb/api/query?fen=<fen>[&opening=1]`

| Status | Body |
|---|---|
| 400 | `{"error": "invalid fen"}` (any failure of 1.6, a missing `fen` included) |
| 404 | `{"error": "unknown position"}` (legal but not in the tree; nothing is created) |
| 200 | the query block below |

The query block (`_query_block`), keys in this order:

| Key | Type | Meaning |
|---|---|---|
| `fen` | string | canonical FEN |
| `key` | string | 64 hex |
| `status` | string | 1.5 |
| `closure` | string or null | 1.5 |
| `score` | int or null | `best_known_eval`, side to move |
| `point` | int or null | the stored point eval `eval_cp`, side to move |
| `backed_plies` | int | plies below the origin of the backed value (0 when none) |
| `best_move` | string or null | UCI; the move `score` comes from (the arrow of the explorer: `_arrow_move` over the same `moves` table); `null` for `TERMINAL` |
| `tier` | `"PRACTICAL"` | |
| `trust` | string | 1.5 |
| `history_scope` | `"COUNTERS_AND_REPETITION_IGNORED"` | |
| `visits` | int | |
| `nodes` | int | `nodes_invested` |
| `moves` | list | one row per edge of the tree, sorted by value (below) |
| `analysis` | object or null | the engine's own lines (below); `null` when the position has neither stored lines nor pass history |
| `opening`, `opening_named_by` | string or null | only with `opening=1` (exactly `"1"`): the last named opening on the canonical line to the position and, when it is a community name, who proposed it |

A row of `moves` (only edges that exist; legal moves without an edge are
absent), keys in this order:

| Key | Type | Meaning |
|---|---|---|
| `uci` | string | |
| `status`, `closure` | string, string or null | of the child |
| `score` | int or null | the child's best known value from **this** position's side to move; `10000`/`-10000`/`0` when proven; `0` on a repetition row; the line value on a `returns` row; `null` when nothing is known |
| `point` | int or null | the child's `eval_cp`, from this side to move |
| `mate` | int or null | moves to mate counting the row's move: positive when the side to move wins, negative when it loses; only for proven decisive children with a known distance |
| `backed_plies` | int | the backed chip: the child's `backed_plies` when its backed value differs from its own eval, else 0 (0 on repetition/returns rows) |
| `node_backed_plies` | int or null | the child's own `backed_plies`, `null` when it has no backed value |
| `visits` | int | |
| `searched_nodes` | int or null | budget of the deepest search that ran on the child (COMPLETED tasks with nodes) |
| `floor` | bool | a value this position's backup refuses (never the `best_move`) |
| `borrowed` | bool | a number nothing searched and no line of this position gives |
| `repetition` | bool | the move comes back to a position on the line (value 0) |
| `returns` | bool | the move's line comes back and somebody on it leaves the loop |
| `returns_line` | list of UCI or null | when `returns` |
| `returns_exit` | object or null | when `returns`: `{"index": int, "uci": str, "score": int or null}` (side to move of this position) |

Order: by `rank` descending (proven win first, then values clamped to
`[-9999, 9999]`, unsearched at `-9999.5`, proven loss last; mate distance
breaks ties inside wins and losses), then `floor` rows after the others, then
the arrow's move, then UCI. `rank` itself is not exposed.

`analysis` (`_analysis_block`):

| Key | Type | Meaning |
|---|---|---|
| `lines` | list | the current pass, MultiPV order |
| `previous` | list | the previous pass when it was wider and survived |
| `budget_nodes` | int or null | the budget of the pass that wrote `lines` (`_budget`) |
| `passes` | list of objects | stored pass summaries, newest first, at most 6: `{"budget": int, "lines": int, "eval_cp": int\|null, "move": str\|null, "mate": int\|null, "restricted": bool, "showcase": bool, "at": ISO-8601 with offset, "depth"?: int, "seldepth"?: int, "nodes"?: int}`, **White-POV, not flipped** |
| `passes_complete` | bool | `len(passes) >= visits` |
| `visits`, `nodes` | int | |
| `seconds` | float | `time_invested` |

A line of `lines`/`previous`: `{"move": str, "cp": int|null, "mate": int|null, "pv": [str], "pv_truncated": bool, "raw"?: str, "depth"?: int, "seldepth"?: int, "nodes"?: int, "nps"?: int, "time"?: int, "hashfull"?: int}`.
`cp` and `mate` are flipped to the side to move. `raw` is the engine's own
`info` line when the worker sent one; the integer fields are parsed from it
(`depth seldepth nodes nps time hashfull`) and absent when it is absent. PVs
are stored cut to 24 plies (except mate lines), and then
`pv_truncated` is true.

### 3.2 `GET /atomicdb/api/principal-variations?fen=<fen>[&line_count=N][&ply_count=N]`

Errors as 3.1. `line_count` and `ply_count` default to 5 and are clamped to
`[1, 5]`; an unparseable value is the default (so `0` -> 1, `99` -> 5,
`abc` -> 5, `-3` -> 1).

200:

```json
{"position": <the query block of 3.1 without opening>,
 "lines": {"<uci of moves[i]>": ["<uci>", ...]},
 "positions": {"<fen>": {"key": str, "status": str, "closure": str|null,
                         "score": int|null, "best_move": str|null}},
 "line_count": 5, "ply_count": 5}
```

Each of the first `line_count` rows of `position.moves` starts a line that
follows `best_move` (same rule as 3.1) until it has `ply_count` moves, the
tree has no edge for the move, or the position has no best move. `lines`
keeps the row order; `positions` holds every position reached, keyed by its
canonical FEN, insertion order. The repetition walks of all tables share one
wall-clock budget of 1.5 s per call (`_line_walk_budget`); tables past it
show stored values (see section 7, budgeted fields).

### 3.3 `GET /atomicdb/api/backed-source?fen=<fen>`

Errors as 3.1. 200:

```json
{"position": {"fen", "key", "status", "closure", "score", "point", "visits", "nodes"},
 "origin": {"fen", "key", "status", "closure", "score", "point", "visits", "nodes",
            "analysis": <3.1 analysis block or null>},
 "moves": ["<uci>", ...], "plies": 3, "end": "origin"|"repetition"|"incomplete"}
```

`origin` is where the value comes from, walked down `backed_move` at most
`ingest.BACKED_ECHO_PLIES` plies; `end` says whether the walk ended there
normally, closed on itself, or broke off (then `origin` is the last position
reached). Scores from each record's own side to move.

### 3.4 `GET /atomicdb/api/frontier/<key>/`

No key validation: anything that is not a stored key is
404 `{"error": "unknown position"}`. 200:
`{"key": str, "status": str, "children_total": int, "children_solved": int, "running": int, "queued": int, "done": int}`
(USER tasks on the direct children, by state LEASED/PENDING/COMPLETED).

### 3.5 `GET /atomicdb/api/live-request/<key>/`

404 `{"error": "unknown position"}`; 200 `{"live": false}` when nothing is in
flight (or the position is closed); else
`{"live": true, "text": "<sentence>", "chip": "cold"|"hot"}`. The sentence
depends on the clock, the caller's session ("N of them yours") and the
machine's speed, so `text` and `chip` are volatile.

### 3.6 `GET /atomicdb/api/roots` and `/atomicdb/api/roots/`

200, cached (1.4):

```json
{"perspective": "white", "since": "<ISO-8601>"|null,
 "positions": [<row>], "openings": [<row>]}
```

A row, keys in this order: `key`, `label`, `kind` (`start`, `move` for the
first moves, `reply` for the replies of the main line, `opening`), `line` (UCI list), `line_san` (string), `community` (bool),
`named_by` (string or null), `present` (bool), `value` (int or null, White
view, proven = +-10000/0), `status`, `best_value`, `best_status`, `best_at`
(ISO or null), `worst_value`, `worst_status`, `worst_at`, `changes` (int),
`route` (UCI list); rows of `openings` add `votes_up`, `votes_down` (int) and
`under_review` (bool). `openings` is sorted by name (the page's default
sort).

### 3.7 `GET|HEAD /atomicdb/api/map/v1[?root=<key>&weight=<w>&limit=<n>&depth=<d>]`

Never reads the live tree: it serves a published snapshot artifact. Body
encoding is the canonical JSON of 1.1. Parameters: `weight` in
`frontier` (default), `explored`, `compute`; `limit` 1..600 (default 500);
`depth` 1..32 (default 16); `root` 64 hex (lower-cased). Errors use their own
schema, `{"schema": "atomicdb.map.error.v1", "error": {"code": str, "message": str}}`:

| Status | code | message |
|---|---|---|
| 405 | `method_not_allowed` | `GET required` (reached only by methods the CSRF middleware lets through; a POST without token is the 403 HTML) |
| 400 | `invalid_query` | `unknown query parameter: <first unknown, sorted>`, `<name> must be an integer`, `<name> must be between <min> and <max>` |
| 400 | `invalid_weight` | `weight must be frontier, explored or compute` |
| 400 | `invalid_root` | `root must be a 64-hex key` |
| 404 | `root_not_found` | `root is not startpos-reachable` |
| 503 | `snapshot_unavailable` | `Atomic move-tree snapshot is temporarily unavailable` (`Cache-Control: no-store`) |

200 (`"schema": "atomicdb.map.v1"`): `snapshot` (`id`, `generated_at`,
`root_key`, `start_key`, `positions`, `edges`, `max_depth`,
`opening_catalog_sha256`), `semantics` (fixed strings), `request`, `marks`,
`zoomable`, `work_items`, `work_items_total`, `work_items_truncated`,
`lineage`, `first_moves`, `root`. Headers and 304 as 1.4. The snapshot id and
time change with every rebuild (volatile); the rest is a pure function of the
snapshot.

## 4. Public write API

These change state, so the golden file does not replay them against
production; their shapes come from the v1 test suite
(`tests/golden/v1-shapes`). All POST, CSRF-exempt, never read the session.

### 4.1 `POST /atomicdb/api/request`

Fields: `fen` (required), `budget` (a ladder rung or `10000000000`; only
accounts allowed to choose have it honoured, others silently get the
default), `username`/`password` (optional: none = anonymous; present but
wrong = refused), `only_if_missing` (`1`/`true`), `priority` (`low`, the
default, or `normal`), `moves` (UCI line from the start, a label).

Refusals, `{"status": "refused", "reason": str}`: 405 `POST only`; 403
`bad credentials`; 400 the FEN sentence of 1.6; 400
`budget must be one of the ladder rungs (128000000, 512000000, 2000000000) or 10000000000, which queues up to 78 searches of 128000000 below the position`;
400 `priority must be low (the default) or normal`; 400
`budget 10000000000 queues searches below the position, and only_if_missing asks about the position itself: send one or the other`;
503 `server busy, retry` with `Retry-After: 2` (a SQLite lock; not
reachable on Postgres, kept for the shape).

Answer (200, or **429** with `Retry-After: 300` for `queue-full-account`):

```json
{"status": "<status>", ...detail..., "key": str, "fen": str, "seeded": bool,
 "task": int|null, "budget_nodes": int|null,
 "task_state": "waiting"|"searching"|null, "priority": "low"|"normal"|null,
 "backed": bool, "moves"?: str, "moves_ignored"?: str, "reason"?: str}
```

`status`: `queued`, `already-queued` (detail `adopted: true` when an
adopted request), `already-requested` (+ `reason`), `already-solved`,
`already-analyzed` (+ `completed_budget`), `expanded`/`saturated` (+ the
descent or subtree counters below; `saturated` has a `reason`),
`queue-full-account` (+ `reason`). `ahead` (int, place in the queue) follows
`status` for `queued`/`already-queued` when known. Descent detail:
`descent_plies`, `descent_key`, `descent_stop` (`queued`, `budget-spent`,
`no-candidate`, `depth-guard`), `children_considered`, `children_queued`,
`children_solved`, `children_exhausted`. Subtree detail: `subtree: true`,
`subtree_plies`, `subtree_stop`, `positions_scanned`, `searches_queued`,
`searches_waiting`, `positions_searched`, `positions_others`, and `message`.
Reasons: `already-requested` "this address already has a live request on
this position; wait for it to land"; `queue-full-account` "you already have
the maximum number of requests waiting; let some land or clear your queue";
`saturated` "nothing left to buy here or below it". `moves_ignored`
sentences: `the line is longer than 64 plies`, `ply N (<uci>) is not a UCI move`,
`ply N (<uci>) is not a legal move in that position`,
`the line repeats a position at ply N`,
`the line does not end at the requested position`,
`the line goes through positions AtomicDB does not have yet`,
`no request is waiting on this position, so there is nothing to label`,
`this request already shows another line`.

A FEN the tree does not have is **seeded** (created, `seeded: true`). The
per-account cap is `REQUEST_QUEUE_MAX = 10000` PENDING USER requests on
unsolved positions; anonymous callers share one bucket.

### 4.2 `POST /atomicdb/api/request-children` (also with a trailing slash)

Fields as 4.1 (`moves` alias `route`), plus `select` (UCI moves, comma or
space separated, or repeated; at most 64 legal ones). `budget=10000000000`
is refused for everybody (400, reason "budget 10000000000 queues searches
below one position; this endpoint queues one search per move, at a rung of
the ladder"); other refusals as 4.1, plus `select names no move`,
`none of the moves in select is legal in this position` (with `skipped`),
`select names more than 64 legal moves; send them in several calls`.

Answer: `{"key", "fen", "seeded", "moves"?|"moves_ignored"?, "status", "queued", "skipped", "counts"}`;
`status` `done`, `partial` (the account's queue filled part way),
`nothing-to-do`, `already-solved`, or `queue-full-account` (429, with
`reason`). `queued`: `{"move", "key", "task", "budget_nodes", "task_state", "priority"}`
(+ `moves`/`moves_ignored`). `skipped`: `{"move", "key", "reason"}` with
`reason` in `illegal`, `blocked`, `closed`, `already-analyzed`,
`batch-cap`, `queue-full-account`, `already-queued`, `already-requested`,
`ladder-spent`, ... plus `completed_budget`, the task fields and `backed`
where they apply. `counts`: `{"queued": n, "<reason>": n, ...}`. At most
`UNEXPLORED_CLICK_CAP = 64` new requests per call.

### 4.3 `POST /atomicdb/api/queue-bump/<id>` and `/atomicdb/api/queue-cancel/<id>` (with or without slash)

Script credentials. 405 `{"error": "POST only"}`; 403 bad credentials.
Bump: `{"status": "moved", "moved": true}`, `{"status": "already-first", "moved": false}`,
`{"status": "not-yours", "moved": false}` (not yours, not PENDING, not USER,
or absent: one answer). Cancel: `{"status": "cancelled"}`,
`{"status": "handed-over"}` (other backers keep it), `{"status": "not-yours"}`;
with `undo=1` (form, or JSON `"undo": "1"|1|true`):
`{"status": "restored"}` or `{"status": "nothing-to-undo"}`. Always 200.

### 4.4 `GET|POST /atomicdb/api/my-queue` and `/atomicdb/api/my-workers` (with or without slash)

GET with a session, or POST with script credentials. 401
`{"error": "login required"}` (GET without session); 403 bad credentials
(POST); non-GET/POST: repo 405 `{"error": "GET or POST only"}`, production
`my-queue` 401 (section 0).

my-queue: `{"account": str, "tasks": [...]}`, at most 200 rows, LEASED first,
then normal before background, `queue_seq`, id; then the requests the
account backed. Row: `task_id` int, `position` key, `fen`, `budget_nodes`,
`state` (`PENDING`/`LEASED`), `queue_seq` int, `priority` (`low`/`normal`),
`backed` bool, `moves` (comma separated UCI, `""` when none), `bump` and
`cancel` (`/atomicdb/api/queue-bump/<id>/`, `/atomicdb/api/queue-cancel/<id>/`,
or `null` on backed rows).

my-workers: `{"account": str, "online": int, "workers": [...]}`. Row:
`machine`, `build` (int or null), `threads`, `hash_mb`, `os`, `online`
(bool), `last_seen` (ISO or null), `seen` (text), `nodes_24h`, `tasks_24h`,
`nodes_all`, `tasks_all`, `nps`, `searches_last_hour`, `searching`
(`null` or `{"task_id", "position", "line", "moves", "budget_nodes"}`).

## 5. Endpoints outside this contract

Pages (`explore/`, `lite/`, `goto/`, `backed-source/<key>/`,
`proven-line-end/`, `borrowed-source/`, `queue/...`, `user/...`, `docs/`,
...), the explorer's session-and-CSRF doors (`request/<key>/`,
`request-unexplored/<key>/`, `request-moves/<key>/`, `pv-verify/<key>/`,
`queue/bump/`, `queue/cancel/`, `queue/clear/`, `queue/adopt/`,
`queue/subscribe/`, `queue/lifo/`), the campaign and name-vote forms, and the
fun side quests are site features, not API. They keep their URLs (plan 6,
"URLs"); their HTML is not byte-compared.

## 6. The golden file

`atomicdb-v2/tests/golden/v1-20261128/`:

- `responses.jsonl.gz`: one object per recorded response:
  `id`, `endpoint`, `method`, `path`, `query` (list of `[name, value]`),
  `form`, `request_headers`, `note`, `recorded_at` (UTC), `elapsed_ms`,
  `status`, `headers` (the kept set: `content-type`, `cache-control`, `vary`,
  `etag`, `retry-after`, `content-encoding`, `expires`, `allow`, and a few
  security headers), `body` (raw text).
- `manifest.json`: start and end of the recording, counts, seed.
- `keys.txt`: the positions chosen for the SQL fixture.
- `fixture-rows.jsonl.gz`: for those positions, read on production by
  primary key and index only (`atomicdb-v2/tools/golden/dump_fixture.py`):
  the position row, its edges, its children's rows, the COMPLETED task
  budgets of the children and the proof-node numbers, with the time of the
  read; and the `api/query` response fetched again right after the read,
  with `consistent: true` when it equals the one in `responses.jsonl.gz`
  (nothing moved between the recording and the read).

Sampling: the start position, every line of `api/roots`, and random descents
(half the time the best move, otherwise a visited row) down to 70 plies, at
most 2 requests a second, outside 04:30-07:15 UTC. No table scan. The
recorder is `atomicdb-v2/tools/golden/record_v1.py`.

`atomicdb-v2/tests/golden/v1-shapes/calls.jsonl.gz`: every call the v1 test
suite makes through an API view, with the test name, the form (password
redacted, lease tokens replaced by `<token:N>`), status, headers and body
(`atomicdb-v2/tools/golden/record_v1_shapes.py`).

`atomicdb-v2/tests/golden/v1-shapes/worker-scenarios-gate-off.jsonl` and
`-gate-on.jsonl`: every call of `av2-compat worker` against a local v1
(`manage.py runserver`, SQLite, this branch's code), with the engine gate off
and with a policy file enforcing it; all scenarios pass on v1, which is what
makes them a specification for v2.

## 7. Normalization (what a comparison ignores, and why)

The rules live in `atomicdb-v2/tests/golden/normalize.json` and are read by
`av2-compat`. Paths are `$.a.b`, `[*]` for every array element and `*` for
every object key. Three modes:

- **snapshot** (v2 loaded from the same rows v1 answered from): exact JSON
  equality (types, integer vs float, key sets) after removing the
  `volatile` paths. Fields in `budgeted` are compared, and a difference is a
  warning, not a failure. With `--strict-order` key order is checked too, and
  with `--bytes` the raw body is compared where no rule removed anything.
- **live** (v2 and v1 each on its own, the tree moving under both): the
  `volatile` and `growth` paths are dropped, and what is left must match
  exactly; for the growth paths, only the JSON type must match (`null`
  matches any type).
- **shape**: types and key sets only, everywhere.

Headers: `content-type`, `cache-control`, `retry-after`, `allow` and the
four security headers of 1.1 are compared everywhere; `vary` and
`content-encoding` where the application sets them (`map/v1`, `vary` of
`live-request`) and, with `--through-nginx`, everywhere (nginx adds them when
it gzips); `etag` and `expires` are volatile; transport headers (`date`,
`server`, `content-length`, `connection`) were never recorded.

The volatile list (ignored in every mode):

| Endpoint | Path | Why |
|---|---|---|
| live-request | `$.text`, `$.chip` | wall clock, caller's session, machine speed |
| map | `$.snapshot.id`, `$.snapshot.generated_at`, header `etag` | change at every snapshot rebuild |
| roots | header `expires` | time the cached copy was built |
| submit | `$.summary.submit_timing_seconds`, `$.summary.ingest_job` | timings and a sequence |
| lease | `$.tasks[*].lease_token`, `$.tasks[*].id` | random token, sequence |

The budgeted list (v1 itself is not deterministic here: a wall-clock budget
decides how far the repetition walk goes, `_line_walk_budget`; owner
decision D1 replaces it with a step budget):

| Endpoint | Path |
|---|---|
| query, principal-variations | `moves[*].repetition`, `moves[*].returns`, `moves[*].returns_line`, `moves[*].returns_exit`, and `moves[*].score` on those rows; the order of `moves` when those rows move |

The growth list (live mode only) is every value that the tree changes as it
grows: scores, statuses, closures, visits, nodes, best moves, the move
tables, the analysis blocks, roots values and dates, frontier counts, map
contents. `normalize.json` spells them out.

## 8. Known v1 behaviours v2 must keep (they look like bugs)

- `api/lease` answers a GET (403 without credentials, a lease with them).
- `api/frontier/<anything>/` returns 404 "unknown position" for malformed
  keys instead of 400.
- Read endpoints answer every FEN error with the same `invalid fen`; write
  endpoints explain.
- `api/query` with `opening` other than exactly `1` behaves as without it.
- `principal-variations` clamps bad numbers instead of refusing them.
- A POST to a read endpoint is the CSRF 403 HTML page, not JSON.
- `my-queue` and `my-workers` exist with and without the trailing slash, and
  `request-children` too, so that a script's POST is never redirected into a
  GET.

## 9. How to run the checks

```
cd atomicdb-v2
# v2 loaded from the fixture rows, all read endpoints, everything compared:
cargo run -p av2-compat -- replay --base-url http://127.0.0.1:8000 --mode snapshot --strict-order --bytes --report diff.jsonl
# v2 following production on its own tree (or v1 against itself later):
cargo run -p av2-compat -- replay --base-url http://127.0.0.1:8000 --mode live
# the worker protocol, on a test instance only (production is refused):
AV2_PW=... cargo run -p av2-compat -- worker --base-url http://127.0.0.1:8000 --username <test account> --password-env AV2_PW
```

`replay` against a public host is held to 2 requests a second.

## 10. Listed semantic decisions (allowed differences)

None yet. A difference that an owner decision (plan section 11, D1-D11)
makes intentional is added to `normalize.json` under `decisions` with the
decision id, the endpoint and the path, and announced on Discord before the
cut-over (as agreed after #222).
