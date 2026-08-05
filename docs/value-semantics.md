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

## Ordering (the rule that was wrong until 2026-08-04)

Open moves order by **best known value in the mover's perspective, and
nothing else**. Provenance is never a sort key: a LINE-CLAIM at −0.6
outranks a SEARCHED move at −15. Terminal moves (decided children) and
unexplored moves keep their established placement; provenance does not
partition anything.

## Rendering

- SEARCHED / BACKED values: plain text, as always.
- LINE-CLAIM / STALE-CLAIM values: same cell, muted and italic, with the
  full story in the tooltip (which line, which pass era, and that nothing
  searched the resulting position yet). **No per-move text captions.**
- Header keeps one summary line (`X searched · Y from lines only · Z
  queued`) and the rare header chip for a node whose *own* displayed eval
  is itself a claim.

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
