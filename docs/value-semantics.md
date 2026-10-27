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
   to luck: a periodic sweep (`sweep_stale_backed`, every 15 minutes) reads
   the whole table once a week and repairs every open position that one
   level of the backup would rewrite, so a node nobody's family has touched
   in weeks still obeys today's guards. It repairs only what is still stale
   when it looks again minutes later, with the ordinary ascent, and never
   writes a proven position. Two things it also covers, which the cascade
   alone cannot (2026-10-26):
   - an ascent stops after `BACKED_MAX_PLIES` levels, and the main line is
     deeper than that. The `BACKED_GUARD` event of a cut ascent carries the
     frontier it left, and the sweep carries the ascent on from there, a
     bounded number of times. Since 2026-10-27 an ingest carries its own cut
     on as soon as it has committed (`ingest.carry_cut_ascents`), so the top
     of the main line no longer waits for the sweep; the sweep keeps the
     frontiers too wide for that and the carries that failed;
   - two ingests in flight at once each compute a shared ancestor without
     the other's uncommitted change, and the one that commits last can leave
     it on a number no child holds. The sweep looks first where overlapping
     ingests meet.

   The nightly global pass this invariant used to name (`recascade_backed`)
   stopped fitting in a night and has been off since 2026-10-05; it remains
   the tool for a deliberate recascade after a change of rule.
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
   bounded error, and the sweep of invariant 4 is what closes it.
   A cut that could exceed that bound is a bug against this document.

   The same holds inside one level of the ascent: **a level never writes
   a number it read from a child that the same level rewrites.** Such a
   node keeps what it stored and is decided again one level later, from
   the child's new number (2026-10-21). Otherwise an old number travels
   round a cycle of positions one position per level, always one step
   ahead of its correction, for ever and one ply longer at every step:
   the recascades of cz_eclipsia's rook ending that never came to rest,
   1208 everywhere and the plies growing by 64 a pass (2026-10-20). This
   changes when a write happens, never what is written at rest.
6. **Position identity erases the counters, so repetition lives in path
   space, never on a node.** Deduplication canonicalises every FEN to
   `0 1`; the graph therefore *cannot* say "this position occurred twice"
   — only a walk can. Every layer that walks values owes the same
   adjudication at the point of crossing, because a value that justifies
   itself by passing through its own consumer is a repetition and a
   repetition is worth a draw:
   - *backed*: a child whose spine returns to the parent being evaluated
     contributes a draw, never the number the cycle invents (the
     2026-08-03 rule). The draw is scored **where the line returns**:
     the child is re-read with the line in hand, so a position on the way
     back that has a better move than returning plays it, and the child is
     worth that move; and the side that can play the repeating move itself
     is never worse off there than the draw (2026-10-04; scoring the whole
     child at 0 handed the losing side a draw the winning side could
     refuse, took the winning side's best move away, and had no resting
     state where two loops share positions). **Between moves of equal
     value, a move whose value comes back to the position never wins, and
     no recompute writes a loop of `backed_move` pointers at a value other
     than 0**: a loop it would close is opened at one of its positions,
     the first by key that has another move worth the same. A returning
     line is worth its value only because somebody leaves the loop later,
     and of two moves worth the same the one that leaves is the progress.
     Values do not change; the move a position stands on does, and with it
     the recommended move, the API `best_move` and the spine of the backed
     chip (2026-10-19, cz_eclipsia: at `909727fb` Rd6-f6 Rf8-d8 Rf6-d6
     Rd8-f8 published 1200 at all four positions, each standing on the
     next, because the returning line carried more search than the way out
     it borrowed its value from; following the best move went round for
     ever and the family never came to rest). The tie is graded by where
     each move's chain of pointers goes: **to a source** (an own search, a
     proven position, a leaf) **beats into a loop elsewhere, which beats
     back to this position**, and a loop is only opened with a move that
     reaches a source. Without the middle grade the positions around a
     loop could stand on it and make it protect itself: every same-value
     move of the loop's own positions then ran back into it through them
     (2026-10-19, the same loop after the fix went live, at 1208). **A
     loop of pointers is worth its best exit** (owner, 2026-10-21): when
     no position can open a loop for the same value, the moves that keep
     play inside it bring no number of their own. Every position chooses
     between its best way out and going on round, and a full lap is the
     repetition, worth 0; a way out whose chain of pointers comes back
     into the loop keeps play inside it too, since all it carries is the
     loop's own number; a way out into a loop elsewhere counts at what
     that loop holds; and the distance is counted from the way out, never
     through the loop. A loop nobody wants to leave is the repetition
     draw. Positions that drain into a loop take its new value through the
     normal backup, and a proven position is never on a loop and counts at
     its exact value (2026-10-20: three loops in cz_eclipsia's rook ending
     held 1208, above every way out, and gained plies on every
     recascade). That draw is an assertion
     about a *walk* over
     `backed_move` pointers, not a measurement: nobody has ever searched
     whether the repetition is forced. So it carries **no search weight**,
     and it may settle a node **only when that node's coverage is
     complete**, the one state in which "the mover has nothing better than
     repeating" is a verdict instead of a guess. A node with answers nobody
     has opened keeps its own measure, publishes nothing when it has none,
     and buys the analysis that answers the question either way (2026-08-12,
     same arithmetic as the anchorless loss of 2026-08-14). Whatever a node
     does stand on it must be able to show: a node published on a
     repetition draw renders that same draw on the row it comes from, with
     the repetition chip. A header quoting a draw no row displays is a bug
     against this document;
   - *proof numbers*: an edge whose value walks back into the node being
     computed contributes its child's **static leaf estimate** instead of
     the child's stored numbers, because a number that travels the loop is
     an echo of the node itself and says nothing about the cost of proving
     anything. Without this the sums feed back through the cycle and
     ratchet to saturation (the Eclipsia shuttle, 2026-08-06: `dn = 2^62`
     on open nodes, an impossible `(pn=1, dn=∞)` state).

     This layer deliberately claims **less** than the other two. "A
     repetition is a draw, and a draw refutes a win" is true of the *game*,
     and that is what certificates and backed values enforce — but they
     adjudicate a path they are standing on, while a proof number is
     persisted on a node and outlives the walk that produced it. Storing
     `(∞, 0)` there says "this node is refuted" when all that is known is
     "this node repeats *by this path*", which is invariant 6 read
     backwards. It is also unstable, and measurably so: `(∞, 0)` erases the
     values the cycle detector itself walks, so detection flips off, the
     numbers return, and it fires again — five corridor nodes oscillating
     between `(∞, 0)` and `(1, 392)` on every pass, with `recascade_proof`
     never reaching the fixpoint that is its own stop condition (measured
     live, 2026-08-06). The leaf estimate is bounded, carries nothing from
     the loop, and leaves the tree the detector reads intact;
   - *explorer*: a displayed line is cut at its first self-crossing, and a
     route can never re-enter a position it already went through — the
     crossing move is disabled, an incoming route that contains a crossing
     is truncated there. Repetitions are switched off, not merely
     labelled (owner decision, 2026-08-06).

   Open nodes whose pn/dn sit at saturation are *not* frontier (there is
   no effort left to estimate in a saturated number) but they never leave
   the books silently: the health panel counts them apart.
7. **A position nobody has searched holds a floor, and the multipv
   certificate holds against a floor.** A position with
   `nodes_invested = 0` whose value is borrowed from below (`backed_eval`
   set) knows only the replies somebody opened. Its number is the best of
   those replies: what its mover can get *at least*, a floor, and the
   replies nobody opened may be better for that mover. One ply up the same
   number is read from the other side of the board, where it is a ceiling
   on what that move can bring. Meanwhile a parent whose last analysis
   published two or more current lines holds the engine's word that no
   move outside those lines is better for the mover than the last of them,
   the same bound that lets a table with unopened moves be read as
   complete. So a floor on a move **outside the current lines** is never
   taken at a value that **beats that bound**: the move is read as one
   nobody has valued, which is what the certificate already calls it, the
   parent stands on its other moves, and the analysis that would turn the
   floor into a value is queued.

   What this leaves alone, on purpose. A child an engine searched backs up
   against any parent, whatever the two budgets (2026-08-08 and
   2026-08-15: 128M moves 10B). A move that heads one of the current lines
   refines its line however it is supported, which is the reported case of
   2026-07-28 (a header at 369 under a row at 416). A parent with a single
   line or none has no certificate to hold against anybody, so a walked
   line still shows what it leads to.

   The case that set the rule (Eclipsia, 2026-10-05): after 1. e3 d6
   2. Nc3 the position's own 648M search, two lines, said −743 with ...e5,
   and the header read −363 through ...Bh3, a move outside both lines that
   nobody had searched, whose only opened reply was valued from an 8M
   search two plies further down. One reply out of thirty said "White has
   at least +363"; a 128M search of ...Bh3 itself said +1491.
8. **Among moves of equal value, a position never stands on a proven loss
   while a move is still open, and the order is the same on every replay.**
   Ties go first by where each move's chain of pointers goes (invariant 6);
   then to a proven win, the soonest mate first; then to every move still
   open, and to a proven draw; then to a proven loss, the latest mate
   first; then to the heavier support, the shorter chain, the position's
   own search move (the one the explorer lists first among equal rows) and
   the move's name. Only the move a position stands on changes, with its
   distance and support, not its value (2026-10-26: at `1725116b`, Black to
   move and every known move lost, the backup stood on ...f4, a mate in
   one, while ...Nc6 was still open at the same number: proven moves
   carried the heaviest weight and no distance, and the first edge in
   storage order won).
9. **A claim the backup refuses is set aside, not obeyed.** A favourable
   number nobody searched never displaces a position's own search, but it
   does not freeze that search either: the position is decided over its
   other moves exactly as it would be without the claim, and the claim's
   analysis is queued. Under partial coverage no value changes (the own
   number stands unless a move beats it); where a multipv bound vouches for
   the moves nobody opened, the searched moves decide, below the own number
   too (2026-10-26: `d2501a13` held its own 1282 over ...Nc5 at 1285 and
   ...Bg7 at 1295, because ...Rb8, seeded at 1216 by another line, topped
   its table). Two refused numbers keep the own number as before, unless a
   searched move beats it: a claim on the own search's own move, because
   that search is the move's value until the move itself is searched (a
   claim on it only says another line thought more of it), and a
   repetition, which is no claim: invariant 6 has a position with replies
   nobody opened keep its own number with one.
10. **An echo is no value.** A backed distance counts the plies of the
    chain of pointers down to the analysis the number comes from, and that
    chain never repeats a position. A distance past `BACKED_ECHO_PLIES`
    (256) was counted round a cycle by a number that outlived its source;
    the backup reads such a move as if its position had no backed value
    (its own search, or nothing). On 2026-10-26 the only positions past 128
    plies in a 10 % page sample of production were 17 holding the 1208 of
    cz_eclipsia's rook ending at up to 3,365 plies: the number the loop of
    2026-10-20 held, still passed between 63 positions a week later, each
    one taking it from a neighbour one ply further at every recompute. It
    fooled the repetition walk too: the top of the family, `795ffd96`
    (own search 1234), read its searched moves as lines coming back to it
    and published a draw.

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
- A move whose value walks back into the position being displayed shows the
  draw the backup uses for it, not the number the resulting position claims
  on its own page, and wears the same `repetition` chip in place of the
  backed chip: the backed chip promises a spine that ends somewhere, and
  what is underneath this one is a loop. This is the row half of invariant
  6, and it is what keeps the header and the table quoting one number per
  edge (reported 2026-08-12: a header at 0 above a table whose best move
  said +9, with no way to tell where either number came from).
- A move whose number is a floor the position does not take (invariant 7)
  keeps its place in the table, because the table orders by value and
  nothing else, and shows that number muted and italic with the reason in
  the tooltip. It is never the recommended move: the board arrow and the
  API `best_move` go to the first move the position does stand on, so the
  recommendation and the headline value always describe the same move. The
  API marks the row with `floor: true`.

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
