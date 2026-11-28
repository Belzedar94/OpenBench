# AtomicDB v2: the website

Owner decision of 28-Nov-2026 (closes plan question D6): the whole AtomicDB
website moves to Rust with v2. Pages are rendered by Leptos on the server,
inside the same binary as the graph. WASM is used only for the interactive
pieces ("islands"). Django keeps OpenBench (SPRT) and the logins. v2 reads
Django's session, so signed-in visitors stay signed in. Every URL stays the
same.

This document has three parts:

1. The inventory of what v1 serves today, with measured latency.
2. The target design, and what crate `av2-web` already implements.
3. How to build, run, test and measure it.

The numbers and the open decisions are at the end.

## 1. Inventory of v1 (28-Nov-2026)

The sources are `atomicdb/urls.py`, `atomicdb/views.py` and its sibling
modules, the templates in `atomicdb/templates/atomicdb/` and the assets in
`atomicdb/static/atomicdb/`. No AtomicDB template uses the project-level
`Templates/` directory.

All URLs below are under `/atomicdb/`. Line numbers are in `views.py` unless
another file is named.

Abbreviations in the cache column:

- **SWR**: `page_cache.stale_while_revalidate`. A copy is fresh for 15 s; after
  that an expired copy is served for up to 600 s while one thread rebuilds it.
- **VC**: `vary_on_cookie(csrf_protect(...))`.
- **LFM**: `_live_for_members`. Anonymous visitors get `cache_page(15)` with VC;
  signed-in visitors are served uncached.

### 1.1 HTML pages

| URL | View | Template | Auth | v1 cache | Data the page needs | v2 status |
|---|---|---|---|---|---|---|
| `` (home) | `home` l.6496 | home.html, _board, _contrib_rows | anon | SWR + VC | `metrics.tree_totals` and `activity_totals`; the start position with `_child_moves`; live `WorkerPing` and leased `AnalysisTask` (up to 5); up-next positions by priority; line labels and openings; `worker_metrics`; root pn/dn; campaign cards; leaderboard | **partial**: explorer of the start position (section 2.6) |
| `explore/<key>/` | `explore` l.9586 | explore.html, _board, missing | anon; signed in also marks notifications seen | none | Position (all fields); up to 8 parents; `?play=` route and lineage; `_child_moves` with search depth and pending marks; off-tree legal moves and SAN; opening (catalogue or community); won line / witness; raw analysis lines; provenance; request ladder; PV-verify lines; depth rungs (signed in); live request; queue bump and withdraw (own task); name suggestion; campaign credit | **ported** for anonymous visitors (section 2.6) |
| `lite/` | `lite.lite` lite.py:161 | lite.html | anon | cache_page(15) | position by key or FEN; `_child_moves`; arrow move; won line SAN; first engine line | todo (easy: no JS) |
| `map/` | `conquest_map` l.10128 | map.html | anon | cache_page(30) + VC | root key and FEN; `conquest-map.js` fetches `api/map/v1` | **ported** (static shell, v1 JS) |
| `roots/` | `roots.roots_page` roots.py:932 | roots.html, _root_chart | anon | SWR + VC (30 s) | `WatchedKey`, `RootExtreme`, first-move table, named openings, `MainLineSnapshot` and daily history, `root_chart`, vote minimum, `?sort` | **ported** except the chart and the "main line by day" table |
| `queue/` | `queue_page` l.7908 | queue.html, backlog box | anon | LFM | `lanes.queue_table` (per account: waiting, running, nodes, served, oldest, share); ingest backlog; propagation lag; nodes in the last hour; backlog box | **ported** except the backlog box |
| `queue/anonymous/` | `queue_anonymous` l.7963 | queue_anonymous.html | anon | LFM | `contributors.present_anonymous` | todo |
| `queue/<user>/` | `queue_user` l.7972 | queue_user.html | anon (owner controls) | LFM | `contributors.present_queue`, own subscriptions | todo |
| `user/<user>/` | `contributor` l.8345 | contributor.html | anon (owner controls) | none | worker pings, queue rows, fleet snapshot, suggestions, badges, sparkline | todo |
| `user/<user>/workers/` (fragment) | `contributor_workers` l.8386 | _contributor_workers.html | anon | cache_page(30) | every `WorkerPing` of the account and the fleet snapshot | todo |
| `workers/` | `active_workers.workers_page` active_workers.py:378 | workers.html, _workers_rows | anon | cache_page(15) + VC | live `WorkerPing`, current leases, speeds, line labels | todo (232 KB page, 0.4 to 1.9 s in v1) |
| `contributors/` | `contributors_page` l.6694 | contributors.html | anon | cache_page(30) + VC | `contributors.leaderboard` (today, 24 h, all time) | **ported** |
| `clubs/`, `clubs/<slug>/` | `clubs_page` l.6714 | clubs.html | anon | cache_page(30) + VC | `contributors.present_clubs` | todo |
| `milestones/` | `milestones_page` l.6731 | milestones.html | anon | cache_page(15) + VC | `DBEvent` feed (12 rows) | todo |
| `campaigns/` | `campaigns` l.8959 | campaigns.html | anon (staff buttons) | none | waiting `Campaign`s, reach, root pn/dn | todo |
| `docs/`, `docs/<slug>/` | `docs`, `docs_page` l.10094 | docs.html, docs/*.html | anon | cache_page(30) + VC | constants only | todo (static: one render per deploy) |
| `notifications/` | `notifications_page` l.8077 | notifications.html | login | none | `RequestNotification` (50) | todo |
| `notifications/settings/` | preferences.py:256 | notification_settings.html | login | none | `ContributorPref`, `WorkerServing`, pings, campaigns | todo |
| `suggestions/` | `suggestions` l.8004 | suggestions.html | approver | none | pending and resolved suggestions, flags, hidden names, routes | todo |
| `fun/`, `fun/alice-atomic/`, `fun/tictactoedb/`, `fun/alice-atomic/play/<token>/` | funquests/views.py | funquests/*.html | anon / token | cache_page(30) | static, or `AliceGame` | todo (can stay on Django: decision W5) |
| `method/`, `me/`, `user/<u>/queue/` | redirects | | | | | todo (trivial) |
| `sitemap.xml` | `sitemap` l.10150 | XML | anon | cache_page(3600) | catalogue and community keys present in the tree | todo |

### 1.2 Write endpoints

These are POST, with session and CSRF unless stated. Django keeps serving
them until v2 ports them, and the v2 pages post to the same URLs.

| URL | Auth | What it writes |
|---|---|---|
| `request/<key>/` | anon or session | `_queue_request` (AnalysisTask, RequestLog); 503 `busy` with `Retry-After` |
| `request-unexplored/<key>/` | anon or session | `enqueue_unexplored_children` |
| `request-moves/<key>/` | login | up to 64 moves |
| `pv-verify/<key>/` | anon or session | `enqueue_pv_verification` |
| `goto/<key>/<uci>/` (GET) | anon | materialises the child and redirects |
| `fen/` | anon | opening search or FEN/PGN jump; may create a Position |
| `backed-source/`, `proven-line-end/`, `borrowed-source/` (GET) | anon | walk a chain and redirect (read-only except `proven-line-end`) |
| `suggest/<key>/`, `campaign/propose/`, `campaign/<id>/vote/` | anon (voter cookie) | suggestions, campaigns, votes |
| `campaign/<id>/state/` | staff | activate, pause or finish a campaign |
| `names/vote/<key>/` | anon (voter cookie) | `OpeningNameVote` |
| `suggestions/`, `suggestions/flags/`, `suggestions/hide/`, `suggestions/line/` | approver | name review |
| `notifications/` (POST), `notifications/settings/` | login | marks notifications seen; preferences |
| `queue/bump/<id>/`, `queue/cancel/<id>/`, `queue/clear/`, `queue/lifo/`, `queue/adopt/`, `queue/subscribe/` | login | own queue |
| `fun/alice-atomic/api/...` | token | game state |

### 1.3 Public JSON API

None of these are cached, unless stated.

- `api/query`
- `api/principal-variations`
- `api/backed-source`
- `api/frontier/<key>/`
- `api/live-request/<key>/` (ported to v2: section 2.6)
- `api/roots` (SWR 30 s)
- `api/map/v1` (snapshot file with ETag)
- `names/votes/`
- `api/request`, `api/request-children`, `api/my-queue`, `api/my-workers`,
  `api/queue-bump/<id>`, `api/queue-cancel/<id>`: csrf-exempt, credentials in
  the body.

The read API (`api/query` and its siblings) belongs to the API agent's
golden file (plan phase 5). The pages call it by URL, so it can move
independently.

### 1.4 Worker protocol

`api/lease`, `api/heartbeat`, `api/submit`, `api/release`,
`api/solve/acquire`, `api/solve/heartbeat` and `api/solve/submit`. They are
out of scope here.

### 1.5 Shared template, assets and settings

**base.html.** Every page except lite and the partials extends it. It
provides:

- a pre-paint script (theme, notation, hidden columns, board colours) that
  runs before first paint;
- the header and nav (three groups);
- the identity zone: sign-in links, or the account's name, the notification
  bell (10 rows plus the unseen count) and a link to account settings;
- the settings panel;
- the footer;
- three inline scripts at the end: notation swap, eval mode and column
  toggles.

**Assets.** CSS and JS loaded on every page:

- `atomicdb.css` (88 KB)
- the Chessground base, brown and cburnett CSS
- `theme.js`, `notifications.js` and `settings.js` (deferred)

Assets loaded by one page only:

- **explorer:** `board.js` plus Chessground 10.1.1 (vendored ESM, 32 KB) and
  about 60 KB of inline scripts;
- **map:** `conquest-map.css`, `conquest-map.js`, d3 7.9.0 (280 KB);
- **roots:** `name-votes.js`;
- **fun pages:** their own JS.

**Legal moves.** The explorer computes them on the server with pyffish and
embeds them as JSON. A move on the board navigates to `goto/<key>/<uci>/`,
which applies it on the server and redirects. No client-side move generation
exists today: no ffish.js, no WASM.

**Settings.** These were read on production on 28-Nov-2026, read-only:

- Django 4.2.30, gunicorn with 5 gthread workers × 8 threads.
- `SESSION_ENGINE` is the default `django.contrib.sessions.backends.db`.
  Sessions live in the default database, which on production is the SQLite
  file `/opt/openbench/db.sqlite3` (about 1,329 rows). AtomicDB's data lives
  in Postgres.
- The cookie is `sessionid` (two weeks, HttpOnly, SameSite=Lax), with the
  JSON serializer and no `SECRET_KEY_FALLBACKS`.
- `AUTHENTICATION_BACKENDS` is `ModelBackend`; `AUTH_USER_MODEL` is
  `auth.User`.
- The CSRF cookie is `csrftoken`, with `CSRF_USE_SESSIONS = False`.
- Context processor `atomicdb.notifications.identity`.
- HTML is minified by `django-htmlmin`; static files are served by
  whitenoise.

### 1.6 Measured latency of v1

All runs were on 28-Nov-2026, around 08:00 UTC, while the box was busy (load
average about 9). Every request was anonymous, one at a time, at most one per
second. Two measurements:

- **Server.** curl on the box against gunicorn at `127.0.0.1:8000`, so there
  is no network. This is the time to first byte (TTFB) in ms. The first
  request is the cold-cache visitor (no cookie, so `cache_page` never
  applies); requests 2 to 4 come with the cookie.
- **Wire.** From this tower (Spain) to `https://belzedar.duckdns.org`.
  Sizes are brotli, as sent; "raw" is the uncompressed HTML.

| Page | Server TTFB: first, then 2-4 (ms) | HTML on the wire / raw |
|---|---|---|
| home | 1,103, then 205 / 68 / 82 | 11.9 KB / 44.5 KB |
| explorer (start position) | 148, 226, 297, 164 (never cached) | 21.6 KB / 85.0 KB |
| roots | 1,221, then 7 / 4 / 3 (8.4 s cold seen from outside) | 75.2 KB / 835 KB |
| queue | 829, then 30 / 28 / 28 | 6.5 KB / 20.6 KB |
| contributors | 76 / 102 / 60 / 56 | 6.9 KB / 34.4 KB |
| map | 26 / 32 / 30 / 46 | 7.1 KB / 22.8 KB |
| workers | 1,168, then 463 / 472 / 395 | 27.7 KB / 232 KB |

## 2. Target design

### 2.1 Process and routing

`av2-web` is a library. The v2 server binary (the one that owns the graph)
mounts its axum router, and the crate's own `av2-web` binary serves the same
router over a fixture for development.

In production, nginx sends the ported page URLs (section 1.1) and
`/static/atomicdb/` to v2, and everything else to Django, as today. A page
moves by adding one `location` line, and moves back by removing it. Once all
AtomicDB pages are ported, `/atomicdb/` goes to v2 as a whole.

### 2.2 Reading the graph: `SiteRead`

The pages never see the store. They call `av2_web::read::SiteRead`
(`src/read.rs`). It is object-safe and `Send + Sync`, and it returns
page-shaped records whose semantics the rules have already decided: a row's
score, whether it is backed, borrowed or a repetition, its mate distance.
The web layer only formats them.

One naming note: the core crate already has `av2_model::GraphRead`, the
low-level trait the rules use (records, edges, keys). `SiteRead` sits on top
of it, so it gets its own name.

`SiteRead` methods:

- `explorer(key, route) -> ExplorerView`. This covers the position, its rows
  in display order, the legal moves with SAN, the line along the route, the
  opening, the parents, the request state and the live request.
- `roots()`, `queue()`, `leaderboard()`, `workers()`, `totals()`,
  `live_request(key)`, `root_key()`.
- `epoch()`, and `page_version(kind, key)` for caching (section 2.4).

**How the store implements it.**

- **Records and edges.** These come from the mapped columns through
  `GraphRead`: one contiguous read per position and its children.
- **Row semantics** (`views._child_moves`: rank, tier, backed, borrowed,
  repetition, returning lines). The rules agent writes these as pure
  functions over `GraphRead`, used both here and by `api/query`, so the
  explorer and the API can never disagree.
- **Legal moves and SAN.** These come from the move generator that the
  preparers already need (FFI to Fairy-Stockfish or a Rust port).
- **Queue, contributors and snapshots.** These come from the in-memory queue
  and the relational projection (plan D7).

`fixture::FixtureSite` implements `SiteRead` from real production data, so
the pages render today. It was taken from the public API through
`fixtures/build_fixture.py` and `build_site_fixture.py`:

- 41 positions (the start position, its 20 children and 1. Nf3's 20 children);
- the full `api/roots` (499 openings);
- the queue and the three leaderboards.

### 2.3 Rendering

Each page renders its `<main>` with Leptos `view!`, which escapes everything
it prints. The document around it is v1's `base.html`, ported byte for byte
in `pages/shell.rs`: the same head, header, nav, settings panel, footer and
inline scripts. The DOM, class names and texts are v1's, so `atomicdb.css`,
`theme.js`, `settings.js`, `notifications.js`, `name-votes.js` and
`conquest-map.js` work unchanged.

The visual check compared element boxes at 1280 × 900:

- **Explorer:** header, opening line, tools row, board, request row and all
  21 table rows are at identical positions and sizes in v1 and v2. The only
  difference is the request row's height, and it is a data difference: v1's
  page had a third PV line, which makes the select wrap.
- **Contributors:** every measured box is identical, down to the document
  height (1,808 px).

The explorer's small v1 scripts that need no server data were moved verbatim
into one deferred file, `assets/explore.js`. They cover the proposal tabs, the
stale-reload helper, play-a-line, keyboard shortcuts, FEN copy and folding of
unanalysed rows. One line changed: the tabs script now finds its row by
selector instead of through `document.currentScript`.

### 2.4 Caching, ETag and compression

**The page cache.** Rendered pages are cached by URL (path and query), keyed
on `SiteRead::page_version(kind, key)`.

`page_version` must not be the global commit epoch. The graph commits every
few milliseconds, so a page keyed on the global epoch would never hit the
cache. Instead:

- an explorer page's version is the largest record version
  (`av2_model::Core.version`, the batch sequence number of a record's last
  change) among the position, its children and its live request;
- the global pages (roots, queue, contributors) use the epoch of the
  snapshot they are built from, rebuilt every 15 to 60 s as in v1.

The fixture answers `epoch()` for everything.

**What a cache entry holds.** The two shared pieces of the page, before and
after the identity zone, plus the anonymous variant fully assembled and
precompressed: brotli quality 5 and gzip level 6. The anonymous variant
depends only on the URL, because the sign-in links carry the path. An
anonymous hit is a hash-map lookup and a refcounted `Bytes` clone.

**Signed-in visitors.** The account's identity zone is spliced between the two
cached pieces and gzip level 1 is applied on the fly.

**Headers.**

- `ETag` is `"<version>-<xxh3 of the anonymous body>"`, with `-u<id>` added
  for a signed-in variant. A matching `If-None-Match` gets a 304 without a
  body.
- `Cache-Control: public, no-cache` (`private` when signed in), so browsers
  always revalidate and get a 304 whenever the page has not changed.
- `Vary: Accept-Encoding, Cookie`.
- `Server-Timing: app;dur=…`.

**Static files.** `av2-web assemble` copies v1's static tree, the v2 assets,
OpenBench's `logo.svg` and the WASM bundle under `target/site/static/`. It
writes `.br` (quality 11) and `.gz` (level 9) siblings for text files. The
server loads all of it into memory at start and serves it with strong ETags:

- a `?v=<digest>` URL is `immutable` for a year;
- anything else gets `max-age=3600`.

The `?v=` digest is v1's `static_version` idea: the first 10 hex characters
of a digest of the whole tree.

**Missing piece.** v1 rebuilds expensive pages off the request path
(stale-while-revalidate). v2 renders inline on a miss for now. Roots takes
20 ms to render and compress; the explorer takes 3 ms. A background refresh
is a small follow-up if the store's versions move faster than the pages are
read.

### 2.5 CSRF without per-visitor HTML

v1 puts a per-visitor `csrfmiddlewaretoken` in every form, which is why its
pages vary by cookie. v2 keeps the HTML identical for every anonymous visitor
instead:

- the forms carry an empty token;
- a few lines in the base script fill it in from the `csrftoken` cookie.
  Django 4.1 and later accept the raw 32-character cookie secret as the form
  token;
- a visitor without the cookie gets one minted the way Django mints it.

The request island sends the same value in `X-CSRFToken`, as v1 does. The
cost is that a visitor without JavaScript cannot post the name-suggestion and
campaign forms (decision W2). When v2 serves its own write endpoints, it can
check `Origin` and `Sec-Fetch-Site` together with the SameSite cookies, which
works without JavaScript.

### 2.6 Islands, board and legal moves

There are two islands. Both are hydrated on idle by Leptos's island script,
so they are off the critical path: first paint never waits for WASM.

**`Board`** (`islands/board.rs`) replaces `board.js`.

- On the server, it renders v1's board `div` with its data attributes.
- In the browser, it mounts Chessground through a 60-line JS glue
  (`board_glue.js`) on v1's vendored build. Chessground is a JS library;
  there is no Rust equivalent worth the weight.
- The island owns the decisions: `from → [to]` destinations, promotion (queen
  by default, as in v1), the `goto` URL with the route, orientation in
  sessionStorage, the arrow colour from settings and its live update, and
  the reset when the page comes back from the back/forward cache.

**`RequestRow`** (`islands/request.rs`) replaces v1's 450-line request
script. It ports:

- the three POSTs (`request/`, `request-unexplored/`, `pv-verify/`) with
  every v1 answer text;
- the retry on a busy server (`Retry-After`, up to 9 attempts);
- the descent notes;
- the `api/query` watch that reloads when the analysis lands;
- the live line polled from `api/live-request/` (every 20 s, at most 200
  times, paused while the tab is hidden);
- the cascade line from `api/frontier/`;
- the "then verify its line" box.

**Legal moves.** The server keeps sending them, exactly as v1 does. The
owner's brief named ffish.js, but nothing in v1's user experience needs
client-side move generation: every move is a page load through `goto`, and
the v2 server needs a move generator anyway (SAN for the table and the
preparers' child keys).

The atomic-only ffish build from Atomic-Stockfish (`tests/js/dist`, 266 KB
WASM) would be loaded lazily, only by a feature that plays several moves
without a page load (an analysis board, for instance). None exists yet.
Decision W1.

### 2.7 Session bridge (`auth.rs`)

`DjangoSqliteSessions` opens Django's SQLite file read-only and does what
`AuthenticationMiddleware` does:

1. Read the `sessionid` cookie and find its `django_session` row, which must
   not be expired.
2. Verify `signing.loads` with salt `django.contrib.sessions.SessionStore`:
   HMAC-SHA256 over `SECRET_KEY`, base64, zlib when the payload starts with
   a dot, then parse the JSON.
3. Check that `_auth_user_backend` is `ModelBackend`, and that
   `_auth_user_id` points to an active `auth_user`.
4. Check that `_auth_user_hash` equals `salted_hmac` of the stored password.
   A password change therefore signs every session out, as in Django.

Answers are cached for 10 s per session key, and the bridge never writes.
The tests use vectors that Django 4.2.30 itself produced with a test key.

Not ported yet: the notification bell's list and count, which still link to
the v1 page; the approver flag, which needs `OpenBench_profile.approver`; and
`SECRET_KEY_FALLBACKS`, which production does not use.

## 3. Build, run, test

```
cd atomicdb-v2
rustup target add wasm32-unknown-unknown
cargo install cargo-leptos --version 0.3.12 --locked

# server (ssr) + islands (wasm32, opt-level z, wasm-opt) -> target/site, target/release/av2-web
(cd crates/av2-web && cargo leptos build --release)

# v1 static tree + v2 assets + OpenBench logo + pkg -> target/site/static, with .br/.gz
./target/release/av2-web assemble --v1-static ../atomicdb/static/atomicdb \
    --assets crates/av2-web/assets --logo ../OpenBench/static/logo.svg --site-root target/site

./target/release/av2-web serve --addr 127.0.0.1:3000 --site-root target/site \
    [--django-db /opt/openbench/db.sqlite3 --secret-file /opt/openbench/Config/secret.key]

./target/release/av2-web bench --site-root target/site      # in-process timings
cargo test -p av2-web
cargo clippy -p av2-web --lib --target wasm32-unknown-unknown --no-default-features --features hydrate
```

The server can also be built without cargo-leptos (`cargo build -p av2-web`),
but its hydration script then expects `av2_web_bg.wasm`. Use the binary that
cargo-leptos builds.

On its first run, cargo-leptos downloads the `wasm-bindgen` CLI that matches
`Cargo.lock` (0.2.130) and `wasm-opt` (binaryen version_123).

CI is the job `web` in `.github/workflows/atomicdb-v2.yml`: clippy for the
hydrate build, `cargo leptos build --release`, assemble, and a report of the
bundle size. The main job's clippy and tests already cover the ssr build,
because `av2-web` is a default member.

## 4. Numbers (28-Nov-2026, this tower, fixture data)

### In-process

These are the router's own timings: no network, brotli requested.

| Page | Render + compress on a miss | HTML raw | HTML br | Cached hit p50 / p99 |
|---|---|---|---|---|
| explorer, start position | 2.9 ms | 56.6 KB | 11.6 KB | 2.9 / 3.2 µs |
| explorer, after 1. Nf3 | 2.5 ms | 49.1 KB | 10.7 KB | 3.7 / 4.1 µs |
| home | 2.9 ms | 56.4 KB | 11.7 KB | 2.4 / 4.4 µs |
| roots | 20.0 ms | 760 KB | 54.7 KB | 2.5 / 2.7 µs |
| queue | 1.2 ms | 17.9 KB | 5.3 KB | 2.4 / 2.9 µs |
| contributors | 1.7 ms | 35.0 KB | 6.5 KB | 2.4 / 3.1 µs |
| map | 1.9 ms | 26.0 KB | 7.1 KB | ~3 µs |

### Over HTTP

curl on the loopback, 200 requests, including the TCP connect on Windows:

| URL | p50 | p90 |
|---|---|---|
| explorer | 0.94 ms | 1.27 ms |
| roots | 0.96 ms | 1.17 ms |
| queue | 0.96 ms | 1.21 ms |
| static css | 0.80 ms | 1.05 ms |

The v1 server times, measured on the box, are in section 1.6: 28 ms to
1.2 s, and 150 to 300 ms for the explorer, which v1 never caches.

### First contentful paint

Headless Chrome 1280 × 900, cold cache, three runs (`fcp.ps1` over CDP).
v1 is measured over the internet and v2 on localhost, so compare FCP minus
TTFB (the client's own work) and the bytes.

| Page | v1 TTFB / FCP (ms) | v2 TTFB / FCP (ms) | Transferred, all resources: v1 / v2 |
|---|---|---|---|
| explorer | 245 to 470 / 352 to 600 | 2 / 88 to 188 | 76 KB / 179 KB, of which 106 KB is the WASM bundle (loaded on idle, after FCP) |
| roots | 94 to 316 / 336 to 568 | 2 / 184 to 224 | 116 KB / 91 KB |
| queue | 74 to 611 / 232 to 768 | 2 / 68 to 72 | 43 KB / 35 KB |
| contributors | 100 to 140 / 248 to 296 | 2 / 56 to 228 | 44 KB / 37 KB |

**The WASM bundle.** It is 283 KB raw, 105 KB brotli and 126 KB gzip
(Leptos 0.8.23, wasm-bindgen 0.2.130, `opt-level = "z"`, LTO, wasm-opt).
`av2_web.js` adds 5.2 KB brotli. v1's equivalent (`board.js`, Chessground
and the inline request script) is about 15 KB brotli. See decision W3.

## 5. Decisions needed (owner)

- **W1. Client-side move generation.** Keep the server-sent legal moves (v1
  behaviour, zero bytes), and load the atomic-only ffish WASM (266 KB raw)
  lazily only for a future multi-move feature.
  *Recommendation: yes.* The brief asked for ffish; nothing in today's user
  experience needs it.
- **W2. CSRF tokens filled by JavaScript.** This makes every anonymous page
  identical, so it can be cached and precompressed. The price is that the
  name-suggestion and campaign forms need JavaScript.
  *Recommendation: yes*, then move to Origin checks when v2 owns those POSTs.
  The alternative is a per-request splice with on-the-fly compression on
  pages with forms, which costs about 0.3 ms on the explorer and 15 ms on
  roots.
- **W3. Islands in WASM, or the tiny v1 scripts.** The two islands cost
  105 KB brotli, loaded on idle and cached immutable after the first visit.
  The same behaviour in v1's vanilla JS is about 15 KB. Options:
  - (a) keep the WASM islands, as briefed;
  - (b) only the board in WASM;
  - (c) vanilla JS everywhere, and Leptos for server-side rendering only.

  *Recommendation:* (a) only if more client-side features are coming
  (multi-select, live queue pages, an analysis board), because the bundle is
  paid once and shared. Otherwise (c).
- **W4. The home page.** v2 renders the explorer of the start position at
  `/atomicdb/`. v1's home also has the headline numbers, active workers,
  campaign cards and leaderboard, which need the store's metrics. Should
  those be ported as they are, or should the home page simply become the
  explorer?
- **W5. Pages that could stay on Django longer.** The fun pages, the
  suggestions admin and the notification settings get little traffic and are
  write-heavy. Should they be ported last, or stay on Django?
- **W6. Small markup differences.** None of these is visible:
  - Leptos adds `<!>` hydration markers where an optional child is absent
    (96 on the explorer);
  - inline styles get a trailing `;`;
  - attribute order differs;
  - v1's per-visitor CSRF values are gone (W2).
