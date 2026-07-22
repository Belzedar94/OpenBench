# AtomicDB Conquest Map

Status: product and architecture decision for the public solver UI.

## Decision

AtomicDB's flagship visualization will be a **top-down, zoomable rectangular
icicle**. It will be paired with a bounded **local DAG lens** in the position
explorer and a separate **Progress** view.

The global view will not be a force-directed graph. At the current scale it
would be visually unstable, produce an unreadable hairball and hide the only
relationships visitors actually need: opening, ply, branch size, state and
progress.

The intended ten-second story is:

1. how large the explored space is;
2. which openings contain the frontier;
3. which branches are solved, queued or being analysed;
4. where compute is going now;
5. how quickly the frontier is opening and closing.

## Why the stored structure needs a visual projection

AtomicDB is not a strict tree. A position can have several parents through
transpositions. Because canonical position identity intentionally ignores some
history, reversible sequences can also return to an existing key and create a
cycle. The global visualization therefore uses a deterministic *display tree*:

1. Compute minimum depth from Atomic startpos.
2. For every reachable position except the root, choose one `display_parent`
   at exactly `depth - 1`.
3. Keep an existing valid display parent between snapshots, preventing layout
   jumps.
4. Otherwise prefer lowest regret, then UCI, then key as deterministic ties.
5. Keep every other parent as a transposition portal, never as a duplicated
   visual node.

Every position is attributed exactly once. Cycles cannot recurse, aggregate
widths remain additive, and the opening shown for a node is stable. A badge such
as `↗ 3` reports alternate incoming paths; selecting the node reveals them.

## Global Conquest Map

The root is at the top and each horizontal band is a ply. A rectangle is either
one attributed position or an aggregate of small siblings.

### Width

The visitor can switch the width measure:

- **Frontier** (default): still-relevant unknown positions below the node.
- **Explored**: unique attributed positions below the node.
- **Compute**: engine nodes invested below the node.

The first-move strip always gives each legal first move selectable space. An
opening must not disappear merely because it has little data so far.

### Visual encoding

- Green: practical `WHITE_WIN`.
- Red: practical `BLACK_WIN`.
- Blue: practical `DRAW`.
- Grey: `UNKNOWN`.
- Diverging inset: White-POV evaluation for unknown nodes.
- Gold border: actively analysed.
- Dashed border: queued.
- Shield icon: verified / AND-OR evidence.
- Gear icon: engine evidence.
- Warning icon: disputed evidence.

Colour is always accompanied by text, border or icon. The global map is
White-POV; any mover-POV panel must say so explicitly.

### Interaction

- Hover, focus or tap shows the complete SAN line from move 1, FEN, state,
  trust, evaluation, visits, nodes, time and active work.
- Click selects a node and synchronizes board plus details without navigating.
- Double-click or Enter zooms the selected branch to full width.
- Escape moves one visual level upward.
- Arrow keys move through parent, children and siblings.
- `Open in Explorer` links to the canonical position page.
- Filters cover state, closure, trust and work state.
- Tiny siblings collapse into a labelled `+N moves` rectangle and expand on
  zoom.
- Refresh preserves focus, selection and camera.
- URL parameters encode branch and selected position for shareable views.

The visual delight comes from meaningful zoom transitions, board
synchronization, recent closures and historical replay. Continuous particles,
3D and gratuitous motion are explicitly out of scope.

## Local DAG lens

The explorer can use a node-link diagram because its scope is bounded:

- at most two parent generations;
- the current position;
- at most two child generations;
- a hard limit of 150–250 nodes;
- deterministic columns by ply;
- curved secondary links for transpositions;
- the canonical line highlighted;
- selection synchronized with the board and move table.

This is where a transposition is explained. It is not the global project map.

## Progress view

The Progress page should show:

- discovered, relevant unknown, closed and historical positions;
- new frontier per hour versus closures per hour;
- active workers, threads, nodes/s and analyses/min;
- engine nodes and wall-clock compute;
- closure mix: terminal, tablebase, mate PV and minimax;
- trust mix: verified, AND-OR, engine and disputed;
- progress by first move;
- transposition reuse;
- compute per closure;
- expired leases, retries and rejected submissions.

`closed / total` is not sufficient: discovering a large legitimate subtree can
make it fall despite real progress. The main flow metric is the balance between
frontier created and frontier closed.

Once hourly instrumentation exists, a 24-hour / 7-day replay can animate new
branches and backward closure propagation. History before instrumentation must
not be fabricated.

## Data architecture

No recursive aggregation runs in a home-page request or a submit transaction.
A periodic snapshot job:

1. bulk-reads positions, edges, tasks and worker state;
2. separates startpos-reachable data from ad-hoc FEN requests;
3. computes minimum depth and stable display parents;
4. builds the attributed tree;
5. aggregates in post-order;
6. writes versioned compressed JSON;
7. publishes by atomic rename.

The initial response contains only 300–600 visible aggregate nodes. More detail
is lazy-loaded.

Per aggregate, the contract includes:

- positions, closed, unknown and relevant frontier;
- historical descendants;
- nodes and time invested;
- active and queued tasks;
- transposition count;
- status, closure and trust breakdowns.

### Endpoints

- `GET /atomicdb/api/map/v1?...`
- `GET /atomicdb/api/neighborhood/v1/<key>?parents=2&children=2`
- `GET /atomicdb/api/progress/v1?range=30d&bucket=1h`

All endpoints require versioned schemas, ETags/304, compression, hard depth and
node limits, and explicit semantic metadata (`tier`, `pov`, `weight`, snapshot
timestamp). Polling pauses when the tab is hidden.

## Front-end

The existing Django-template architecture remains:

- modular, pinned D3 for partition, zoom and transitions;
- SVG for the 300–600 visible marks;
- server-aggregated data;
- no React/Vue application shell;
- Canvas/WebGL only for an optional bounded experimental mode.

On mobile a first-move strip selects one full-width branch and details appear
below the chart. The SVG has an equivalent textual/table view. Tooltips also
work by focus and tap; keyboard navigation, AA contrast and
`prefers-reduced-motion` are mandatory.

## Delivery phases

### Phase 0 — truth and performance

- formalize depth, display parent and frontier definitions;
- cache/fix SAN provenance;
- build aggregate and hourly snapshots;
- publish a metric glossary;
- target a cached home response below 250 ms.

### Phase 1 — Conquest Map MVP

- first-move strip;
- zoomable icicle;
- state/eval/work/transposition encodings;
- synchronized details and explorer link;
- keyboard, mobile and ETag support.

### Phase 2 — DAG lens

- bounded neighborhood endpoint;
- transposition portals and links;
- board/PV synchronization;
- shareable selection.

### Phase 3 — Progress

- hourly history;
- frontier balance and throughput;
- workers, closure mix and opening comparison.

### Phase 4 — replay and polish

- 24-hour / 7-day replay;
- closure propagation animation;
- snapshot comparison and share cards;
- reserved hooks for real proof-number/disproof-number data from Floor 2.

Floor-2 fields are not shown before the backend actually produces them.

## Validation gates

Backend:

- reversible cycles terminate;
- every reachable position is attributed once;
- transpositions never duplicate totals;
- child aggregates sum to their parent;
- display-parent choice is deterministic and stable;
- snapshot generation does not block submits;
- corrupt snapshots fail closed;
- ETag/304, query and payload budgets are tested;
- synthetic 100k then 1M-position scale runs pass.

Front-end:

- zoom, selection, URL restoration and live refresh preserve state;
- complete opening line works with hover, focus and tap;
- secondary transpositions are discoverable;
- child aggregation expands predictably;
- 320, 768 and 1440 px layouts pass;
- keyboard, screen-reader summary, reduced-motion and colour-vision checks pass;
- initial marks stay at or below 600;
- compressed initial JSON stays near 150–200 KB;
- no main-thread task exceeds 50 ms on the reference device.

The product principle is simple: **a stable quantitative map for understanding
the whole solving effort, and a local graph for understanding transpositions**.
