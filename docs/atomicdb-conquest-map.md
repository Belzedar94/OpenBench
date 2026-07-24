# AtomicDB Atomic Move Tree

Status: product, interaction and data contract for the public solver map.

## Product decision

The public visualization is a **horizontal, zoomable node-link tree**. Its
product name is **Atomic move tree**; the compact navigation label is **Move
tree**.

The page should answer four questions without requiring a legend:

1. Which moves have been explored from the current position?
2. Which branches are unresolved or solved?
3. What is being analysed right now?
4. What line and opening does a selected position belong to?

The tree is the primary object. Every other element supports reading or
navigating it. There is no second "accessible" visualization, no permanent
keyboard legend and no unexplained dashboard jargon.

## Why a tree is a projection of the stored graph

AtomicDB is a directed graph rather than a strict tree. Several move orders can
transpose into the same canonical position, and reversible sequences can lead
back to a previously seen key. The snapshot builder therefore produces a
deterministic display tree:

1. Compute minimum depth from Atomic startpos.
2. Give each reachable position, except startpos, one display parent at
   `depth - 1`.
3. Preserve an existing valid display parent between snapshots to avoid visual
   jumps.
4. Otherwise choose by lowest regret, UCI and position key, in that order.
5. Retain other incoming edges as transposition metadata; do not duplicate the
   position in the display tree.

This projection guarantees that each position is drawn once, cycles cannot
recurse and subtree totals remain additive. A selected node may expose its
alternate incoming paths in the inspector or link to the full position
explorer.

## Visual architecture

### Layout

The implementation uses D3's tidy-tree layout (`d3.tree`):

- the root is on the left;
- depth increases from left to right;
- siblings are arranged vertically;
- links are simple curves with a clear parent-to-child direction;
- nodes use a stable, readable card or pill size rather than quantitative
  rectangle area;
- a collapsed group is an explicit node such as `17 more replies`, never
  unexplained blank space.

Branch size, compute and evaluation never change the physical width of a node.
They belong in labelled detail, not geometry. Blank rows are not meaningful
data and must never be reserved by the layout.

The initial view is fitted to the available stage. Pan and zoom use
`d3.zoom`; visible buttons provide **Zoom in**, **Zoom out** and **Fit tree**.
The user's selection and camera survive refresh when their target still exists.

### Node language

A node shows only information needed for scanning:

- move number and SAN;
- practical state as a word or familiar icon;
- an exact-work halo or badge when that position itself is leased or queued;
- a disclosure control when replies are collapsed.

The visual language is deliberately small:

- neutral: unresolved;
- green: White win;
- red: Black win;
- blue: draw;
- animated or high-contrast halo: analysing this exact position;
- outlined work badge: this exact position is queued.

Colour always has a textual or icon equivalent. Evaluation is not represented
as an unlabelled bar. When useful, the selected position's evaluation appears
as labelled text in the inspector, including its point of view and unit.

Aggregate activity may be used to decide which ancestor paths remain visible,
but it must not make an ancestor look as if that exact position is running.

### Progressive disclosure

The API remains bounded, so the tree starts with the most useful branches and
explicitly reports omitted replies. Expanding or zooming a branch requests or
reveals more detail. A user must never infer truncation from a narrow or empty
rectangle.

The public modes are exactly:

- **All branches**: show the current bounded projection.
- **Unresolved**: show a compact round-robin sample across first-move
  branches, retaining every ancestor needed to understand the sampled lines
  and reporting the full match count.
- **Active now**: retain exact leased/queued positions and enough ancestors to
  understand where they sit.

These modes affect visibility, not status semantics. They replace the old
metric selectors and multi-filter control bank.

### Search

The search box accepts a move, SAN fragment, opening name or position key.
Results are keyboard navigable. Choosing a result selects and reveals the node,
fits it into view and synchronizes the inspector. URL state preserves the
selected position or branch for sharing and reload.

### Live work rail

The **Analyzing now** rail is sourced from the global `work_items` response,
not from whichever nodes happened to fit into the current mark budget. It can
therefore show exact active or queued positions outside the visible tree.

Each item includes:

- exact state (`active` or `queued`);
- full SAN and UCI lineage from startpos;
- opening context;
- direct navigation to reveal/select the position.

Leased work sorts ahead of queued work. The response reports the total and
whether the visible rail was truncated.

### Inspector

Selecting a node updates one inspector containing:

- board;
- move and complete line from move 1;
- opening name;
- exact practical status and closure evidence;
- labelled evaluation, visits, engine nodes and elapsed time;
- exact work and descendant-work summaries, clearly separated;
- transposition count;
- `Open in Explorer`.

Generic labels such as "root" or internal scheduler terminology are not
displayed as product copy. Startpos is simply **Start position**.

## Exact and aggregate work semantics

The v1 response preserves the original aggregate fields for compatibility and
adds unambiguous fields:

- `exact_state`: `active`, `queued` or `idle` for this position;
- `own_active` / `own_queued`: exact task counts for this position;
- `subtree_active` / `subtree_queued`: task counts at this position and below;
- `descendant_active` / `descendant_queued`: subtree counts excluding this
  exact position;
- `state`, `active`, `queued`: retained v1 aggregate fields.

The tree halo and node badge use `exact_state` and `own_*`. Descendant activity
may appear only in explicitly labelled inspector copy such as "3 running
below".

`work_items` contains globally indexed exact work, capped at 16 entries in one
response:

- `work_items_total` reports the full count;
- `work_items_truncated` states whether the rail is partial;
- a snapshot may provide authenticated `work_keys`;
- legacy authenticated snapshots without that index are scanned safely.

Snapshot validation checks that indexed work agrees with exact `wa` / `wq`
counters. It fails closed on a mismatch.

## Opening inheritance

Opening recognition follows the same user expectation as an opening explorer:
the most recent named prefix remains visible until a deeper named prefix
replaces it.

For every rendered node:

- an exact catalog hit returns `opening.exact = true`;
- a descendant without a new hit inherits the last name with
  `opening.exact = false`;
- `opening.matched_ply` remains the ply at which that name was established;
- a later exact hit replaces the inherited opening;
- direct deep links derive the inherited opening from the full lineage, not
  just the bounded subtree.

The UI displays the opening name naturally. It does not show an "exact
position" badge, aliases dropdown or provenance dropdown.

## Interaction contract

- Click or tap selects a node.
- Visible controls explicitly expand, collapse or focus a branch; no essential
  action depends on double-click.
- Enter or Space selects/activates a focused tree item and expands or collapses
  it when it has children.
- Arrow Up and Arrow Down move through the previous and next visible tree item.
- Arrow Left collapses an expanded item or moves to its parent.
- Arrow Right expands a collapsed item or moves to its first child.
- Escape only closes transient help, dialogs or overlays. It never changes the
  tree root or camera.
- Search reveals a matching node.
- The visible help button opens a compact keyboard-help popover; help is not a
  permanent strip consuming map space.
- Refresh, filtering and resize preserve selection whenever possible.
- Polling pauses while the document is hidden.
- Reduced-motion users receive immediate state changes rather than animated
  camera transitions.

## Accessibility

There is one semantic visualization:

- the SVG has `role="tree"` and a useful accessible name;
- each interactive node has `role="treeitem"`;
- nodes expose `aria-level`, `aria-posinset`, `aria-setsize`,
  `aria-expanded` and `aria-selected` where applicable;
- roving `tabindex` gives a single predictable keyboard entry point;
- the selected node's accessible label includes move, status, opening and work
  state without relying on colour;
- status changes use a polite live region;
- toolbar buttons have text or accessible names;
- focus remains visible in both themes and forced-colours mode;
- touch targets are at least 44 CSS pixels where practical.

The inspector is ordinary semantic HTML following the tree in reading order.
There is no duplicate hidden/permanent tree table. If D3 cannot load, the page
shows a concise error and an **Open Explorer** route rather than maintaining a
second rendering implementation.

## Responsive behaviour

The same information hierarchy is preserved at all sizes:

- **Desktop (>= 1200 px):** toolbar above; tree and inspector share the main
  area; work rail remains visible without obscuring the canvas.
- **Tablet (720-1099 px):** inspector moves below or into a bounded side sheet;
  toolbar groups wrap without shrinking labels into icons only.
- **Mobile (< 720 px):** tree keeps a useful minimum canvas, supports pan and
  pinch zoom, and opens details below it; mode controls scroll or wrap as whole
  labelled controls.
- **Narrow mobile (<= 360 px):** controls remain operable, long SAN/opening
  names ellipsize visually and remain complete in accessible labels/tooltips.

Resize is observed with `ResizeObserver`; fitting is not recalculated on every
animation frame. No breakpoint creates zero-height rows or overlapping labels.

## Themes

AtomicDB supports dark and light themes through the shared theme switch. The
tree consumes shared CSS custom properties for:

- page, panel and elevated backgrounds;
- primary and muted text;
- borders and focus rings;
- result states;
- exact active/queued states;
- links and selection.

SVG colours are derived from these tokens rather than hard-coded for one
background. Both themes must meet AA contrast for normal text and preserve
focus in forced-colours mode. Patterns, gradients and decorative textures are
not used to compensate for unclear status semantics.

Theme choice persists using the shared AtomicDB theme mechanism; the map does
not implement a second switch or storage key.

## Backend and endpoint contract

The snapshot implementation lives in `atomicdb/conquest_map.py`. It does not
add solver models, write solver rows or traverse the live database from a web
request.

Build and atomically publish the observational snapshot with:

```console
python manage.py build_conquest_map
```

The default destination is
`Media/atomicdb/conquest-map-v1.json.gz`; production may override it with
`ATOMICDB_MAP_SNAPSHOT_PATH`. Publication writes a same-directory temporary
file, flushes it and calls `os.replace()`. A missing, invalid or inconsistent
artifact produces HTTP 503; the endpoint never falls back to live graph
queries.

The endpoint remains:

```text
GET /atomicdb/api/map/v1
    ?root=<startpos-reachable-position-key>
    &weight=frontier|explored|compute
    &limit=1..600
    &depth=1..32
```

`weight` remains a backwards-compatible server-side priority for choosing
which nodes enter a bounded response. It is not a public geometry control and
does not alter node width.

Responses include:

- schema `atomicdb.map.v1`;
- semantic metadata and snapshot identity;
- hierarchical `root`;
- stable `lineage` and `first_moves` navigation metadata;
- full SAN/UCI line for every visible exact node;
- exact and aggregate work fields;
- inherited or exact opening metadata;
- `work_items`, total and truncation fields.

Responses have weak content ETags, support 304 and gzip, contain no more than
600 API nodes and are capped at 2 MiB uncompressed. Snapshot input is read
in bounded chunks up to its hard budget. Corrupt artifacts are
negative-cached by file identity until replaced.

The legacy aggregate metric definitions remain:

- `positions`: unique startpos-reachable attributed positions including the
  node;
- `closed`: positions whose exact practical status is not `UNKNOWN`;
- `unknown`: positions whose exact status remains `UNKNOWN`;
- `frontier`: unknown positions above the scheduler tombstone boundary;
- `historical`: positions at or below that boundary;
- `nodes` / `seconds`: cumulative engine investment;
- `active_tasks` / `queued_tasks`: subtree task totals;
- `transpositions`: alternate reachable incoming edges.

`eval_cp` is heuristic and White-POV. `status`, `closure` and `proof` are exact
evidence fields and are never inferred from evaluation.

## Performance contract

- Initial API node count: at most 600.
- Visible interactive DOM nodes: at most 300.
- Search and filter scenes use a smaller readability budget: 40 nodes on
  larger canvases and 24 on narrow canvases, with the total match count kept
  visible. Broad filters sample at most 10 endpoints on larger canvases and 6
  on narrow canvases, balanced across first-move branches; a direct search
  instead preserves its complete target lineage.
- Uncompressed response: at most 2 MiB.
- Snapshot generation: offline and atomic.
- Request path: snapshot read/cache only, never recursive SQL.
- DOM: one SVG tree plus semantic controls/inspector, not a parallel table.
- Rendering: keyed joins and bounded labels; no per-node board instances.
- Work lookup: authenticated index when present, safe fallback for legacy
  snapshots.
- Refresh: ETag/304 and one current `AbortController`.
- Background tabs: polling suspended.
- Reference target: no main-thread task above 50 ms after initial parse.

Large deployments must run the opt-in synthetic 100k and 1M snapshot gates.
The visual tree remains bounded even when the stored graph is much larger.

## Validation gates

### Backend

- reversible cycles terminate;
- every reachable position is attributed once;
- transpositions do not duplicate totals;
- display-parent choice is deterministic and stable;
- corrupt or oversized snapshots fail closed;
- exact and aggregate task counts validate independently;
- indexed global work agrees with position counters;
- inherited openings survive unnamed descendants and are replaced by deeper
  exact matches;
- deep roots derive their opening from full lineage;
- ETag/304, gzip, query validation and payload budgets pass;
- synthetic 100k and explicit 1M scale runs pass.

### Front-end

- the heading and accessible name say **Atomic move tree**, while navigation
  uses the compact **Move tree** label;
- D3 tidy-tree and zoom are used; partition geometry is absent;
- search and all three named modes work by mouse, touch and keyboard;
- Zoom in, Zoom out, Fit tree and Keyboard help are discoverable;
- exact work is visually distinct from descendant work;
- global work remains reachable outside the currently rendered 300 nodes;
- inherited opening names persist without an exact-position badge;
- long SAN/opening text does not overlap at 320, 768 or 1440 px;
- selection, camera and URL restoration survive compatible refreshes;
- reduced motion, forced colours, screen-reader names and roving focus pass;
- dark and light themes pass AA contrast and visual regression checks;
- there is no duplicate tree table, permanent legend or permanent help strip;
- there are no unlabelled evaluation bars or patterned status definitions.

Run the focused checks with:

```console
node --check atomicdb/static/atomicdb/conquest-map.js
python manage.py test atomicdb.test_map_frontend atomicdb.test_conquest_map
```

## Operations and rollout

The map is observational. Deploying it must not stop, restart, pause or
reprioritize solver or OpenBench workers.

Before exposing navigation in production:

```console
python manage.py migrate
python manage.py capture_atomicdb_progress --json
python manage.py build_conquest_map
```

Keep progress capture and map generation as separate scheduled jobs. A failed
build leaves the last valid artifact in place. After deployment verify:

```text
GET  /atomicdb/map/
GET  /atomicdb/api/map/v1?limit=600&depth=8
HEAD /atomicdb/api/map/v1?limit=600&depth=8
```

The GET must return schema `atomicdb.map.v1`, at most 600 marks, exact work
semantics and a snapshot timestamp. HEAD must return the same ETag and
content-encoding choice without a body. Repeating GET with `If-None-Match`
must return 304.

The product principle is simple: **show the move tree first, explain exact work
truthfully, and reveal detail only when it helps the user act.**
