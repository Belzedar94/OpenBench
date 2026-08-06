# Value semantics — the tight system

One page. Every number the explorer shows obeys these rules; anything that
does not is a bug against this document, not a matter of taste. Written
2026-08-04 after three display iterations in 48h proved that patching
without a spec creates churn. Community review welcome — especially vetoes.

## States of a displayed value

Every position value on the site is in exactly one of four states:

| State | Meaning | Storage signature |
|---|---|---|
| **SEARCHED** | an engine analysis was anchored *at this position* | `nodes_invested > 0`, `eval_cp` is its result |
| **BACKED** | minimax over children that carry real data beneath | `backed_nodes > 0`, spine of `backed_plies` |
| **LINE-CLAIM** | first move of a line in an ancestor's *current* analysis; the ancestor's search went through here, nothing searched *from* here | `eval_cp` set, `nodes_invested = 0`, `backed_nodes = 0`, move heads a current line |
| **STALE-CLAIM** | seeded by a pass that is no longer the ancestor's showcase | same signature, move heads no current line |

## Invariants (enforced at write time)

1. A claim never overwrites own data: seeding writes only where
   `eval_cp IS NULL OR (nodes_invested = 0 AND backed_nodes = 0)` —
   atomically, in the UPDATE's WHERE.
2. Claims follow the showcase: when a new pass wins a node's arbitration,
   its lines refresh that node's children claims. A pass that does *not*
   win the showcase refreshes nothing — the row always shows what the
   page stands behind.
3. Own data at a child always beats an ancestor's claim about it, even a
   fresher one. (Open question 1 below challenges this; until resolved,
   this is the rule.)
4. Backed values are recomputed by the cycle-aware cascade; repetition
   cycles contribute a draw bound, never a free win. Freshness is not left
   to luck: a periodic global sweep (`recascade_backed`, nightly) drives
   every backed value to the fixed point of the rules in force, so a node
   nobody's family has touched in weeks still obeys today's guards.
5. **A node's `backed_eval` is the minimax of its children's *current*
   best-known values, and no propagation cut may leave a node standing on
   a value no child holds any more.** A cut may silence a change upward
   only for nodes that are *not* standing on what moved; a node whose
   `backed_move` points at the edge that drifted is recomputed however
   small the drift, because its value *is* that child's and a drift can
   flip which sibling wins. Cheapness never buys a number nobody backs.

   What this does **not** promise, stated plainly so nobody reads more into
   it than is enforced: a sibling the node is *not* standing on may drift
   under `BACKED_EPSILON_CP` and quietly become the better edge. That
   leaves the argmax stale by strictly less than the epsilon — a real but
   bounded error, and the nightly sweep of invariant 4 is what closes it.
   A cut that could exceed that bound is a bug against this document.
6. **Position identity erases the counters, so repetition lives in path
   space, never on a node.** Deduplication canonicalises every FEN to
   `0 1`; the graph therefore *cannot* say "this position occurred twice"
   — only a walk can. Every layer that walks values owes the same
   adjudication at the point of crossing, because a value that justifies
   itself by passing through its own consumer is a repetition and a
   repetition is worth a draw:
   - *backed*: a child whose spine returns to the parent being evaluated
     contributes a draw, never the number the cycle invents (the
     2026-08-03 rule);
   - *proof numbers*: an edge whose primary spine returns to the node
     being computed contributes `(pn=∞, dn=0)` — a draw refutes a win
     proposition, PNS is binary — for exactly as long as the spine keeps
     looping. The moment real progress reroutes the spine, the edge is
     scored from its child's numbers again. Without this rule the sums
     feed back through the cycle and ratchet to saturation (the Eclipsia
     shuttle, 2026-08-06: `dn = 2^62` on open nodes, an impossible
     `(pn=1, dn=∞)` state);
   - *explorer*: a displayed line is cut at its first self-crossing, and a
     route can never re-enter a position it already went through — the
     crossing move is disabled, an incoming route that contains a crossing
     is truncated there. Repetitions are switched off, not merely
     labelled (owner decision, 2026-08-06).

   Open nodes whose pn/dn sit at saturation are *not* frontier (there is
   no effort left to estimate in a saturated number) but they never leave
   the books silently: the health panel counts them apart.

## Ordering (the rule that was wrong until 2026-08-04)

Open moves order by **best known value in the mover's perspective, and
nothing else**. Provenance is never a sort key: a LINE-CLAIM at −0.6
outranks a SEARCHED move at −15. Terminal moves (decided children) and
unexplored moves keep their established placement; provenance does not
partition anything.

## Rendering

- **What a node headlines is its best-known value** — proven status >
  backed > point eval, the one precedence of `best_known_eval`, on every
  surface that quotes the number: the explore header, the node's row in
  its parent's table, the query API `score`, the map inspector. The raw
  point eval never headlines while something better is known; it survives
  as own-search context (tooltips, the API `point` field). One node, one
  number. (Owner decision 2026-08-06, after a header quoted a 640M-node
  own eval over the 2.13G-backed value its own table stood on.)
- SEARCHED / BACKED values: plain text, as always.
- LINE-CLAIM / STALE-CLAIM values: same cell, muted and italic, with the
  full story in the tooltip (which line, which pass era, and that nothing
  searched the resulting position yet). **No per-move text captions.**
- Header keeps one summary line (`X searched · Y from lines only · Z
  queued`) and the rare header chip for a node whose *own* displayed eval
  is itself a claim.
- Repetitions never render as navigable play (invariant 6): a stored PV is
  shown only up to its first self-crossing, with the established
  `repetition` chip explaining the cut, and a move that would re-enter the
  current line renders as a disabled row wearing the same chip.

## Open questions (seeking the community's preference)

1. **Fresh claim vs old own-search.** A move has its own old shallow
   search (say −15, 2M nodes, weeks ago) and today's deep ancestor line
   claims −0.6 through it. Rule 3 shows −15. Should the *newer* claim win
   the cell instead, with the old own-search in the tooltip?
2. **Minimal marking.** Is muted-italic acceptable for claims, or should
   claims be visually identical to searched values with tooltip-only
   provenance?

Change process: edits to semantics land here first, then in code, in that
order.
