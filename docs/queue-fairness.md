# AtomicDB queue fairness: contributor lanes

What the community voted for on 10 August (3 to 1), Option A of the owner's
proposal, quoted so the target never drifts:

> Fair share + contributor lanes (an extension of what we have now). Your queue
> position is weighted by the nodes you already have queued (a 10B counts ~78x a
> 128M, so big batches interleave instead of walling the queue). Accounts that
> ran a worker in the last 7 days get their own lane; everyone else (anonymous
> or not) shares one common lane. Creating alt accounts gains nothing: only
> plugging in CPU earns you a lane. Plus a cap on total queued nodes per lane as
> a hard backstop. Both keep: your own worker always serves your own requests
> first, and 10B leases stay capped at a third of the live pool per account.

**Shipped and live.** Three changes landed on 15 and 16 August, with migrations
`0044_worker_last_result_at` and `0045_task_lane_account` applied in production.
This document describes what is running, not what was planned. The one place
the vote and the code differ is the cap, which the owner redirected during
review: see section 4.

## 1. How the queue is ordered

Every analysis job is one `AnalysisTask` row. Ordering happens in
`choose_pending` (`atomicdb/views.py:591`), inside the lease transaction:

| Step | Expression | Where | Effect |
|---|---|---|---|
| 1 | `-source` | `views.py:687` | USER before FILL before AUTO |
| 2 | `-own_first` | `views.py:615` | this worker's account serves its own USER requests first |
| 3 | `-named_first` | `views.py:635` | named, or waiting over 24h, before the fresh anonymous tide |
| 4 | `lane_ahead` | `live_request.py:177` | the lane weighting, section 2 |
| 5 | `-backer_rank` | `live_request.py:287` | among rows served together, the one more people asked for |
| 6 | `-queue_rank` | `views.py:689` | position priority, zero across the whole USER band |
| 7 | `id` | `views.py:689` | arrival order, stable tiebreak |

Steps 3 to 5 are `live_request.SERVICE_KEYS` (`live_request.py:287`), shared
literally with the estimator that tells a waiting visitor how many requests are
ahead and with the profile queue. One definition, three readers: when they were
three similar queries, the day one changed the others started lying to somebody
who was waiting.

Charged rows are PENDING plus LEASED (`live_request.py:89`) over positions still
UNKNOWN (`:82`). CANCELLED is a state of its own (`models.py:336`), so a request
you take back stops counting everywhere the moment you take it back.

## 2. Lanes

A **lane** is the unit that receives an equal share of the fleet.

- Each contributor account gets its own lane. Contributor means an enabled
  profile plus a `WorkerPing` whose `last_result_at` falls inside
  `CONTRIBUTOR_WINDOW_DAYS` (7) (`lanes.py:49`, `lanes.py:98`).
- Everyone else shares one lane, the **commons** (`lanes.py:42`): named non
  contributors, anonymous traffic, and revoked accounts.
- Anonymous requesters are a single member of the commons, not one per IP. They
  cannot be told apart, so treating the tide as one account is the only rule
  that holds. An IP is not an identity.

Lanes are **derived at read time**, never stored. A lane with no charged rows
does not exist, so an absent contributor holds nothing and membership refreshes
itself on the next read. No cron, no backfill, no state that can go stale.

**A lane is earned by delivering.** `WorkerPing.last_seen` is `auto_now` and
moves on any authenticated call, so a process that polls `/api/lease` forever
and never returns an analysis looked exactly like a machine that was searching.
`last_result_at` (`models.py:973`) is stamped in `api_submit`, where a result is
known to have landed. The rung selector reads the same predicate
(`depth.may_choose`, `depth.py:39`), so the same fake worker cannot buy 10B
searches either. `is_staff` opens the rung selector and does **not** open a
lane: CPU is what buys one.

### 2.1 The weighting

The weight of a request is its `budget_nodes`. Against the first rung the
ladder ratios (`ingest.py:38`) are exactly 4 (512M), 15.625 (2B) and
`10B/128M = 78.125`. That last number is the "~78x" of the proposal, literal.

For a charged, serveable row `r` in lane `L` from member `m`:

```
member_ahead(r) = SUM(budget_nodes) over the same (L, m), ordered leased first,
                  then queue_seq, then id, excluding r itself
members(L)      = COUNT(DISTINCT member) with at least one charged row in L
lane_ahead(r)   = member_ahead(r) * members(L)
```

The multiplier is what turns an account share into a lane share
(`live_request.py:145`). A contributor lane has one member, so it multiplies by
one. The commons has as many members as there are people in it, so each
advances that many times faster and the commons as a whole advances at one
lane's rate. That is the whole arithmetic of "creating alt accounts gains
nothing": five alts raise the count and split one share five ways.

Two properties make the change auditable, and both are pinned by tests:

1. **Inside a lane the multiplier is constant**, so nothing reorders within a
   lane.
2. **With everybody in the commons** it is a single global constant, so the
   served order is identical to the pre-lane order.

Property 2 is why `FairShareTests`, `AnonymousBucketTests` and
`DeepLeaseCapTests` still pass without a line edited, including the deep-lease
straddle at position 16. If any of them needs editing, the constant-multiplier
property is broken and the change is wrong.

`members(L)` cannot be a window function (PostgreSQL has no `COUNT(DISTINCT)`
over a window), so the layout is a small aggregate published through the shared
snapshot with 30 second freshness (`lanes.py:227`). It is resolved **before**
the lease transaction opens and handed down (`views.py:546`): resolved inside,
its refresh met the transaction's own writes on `WorkerPing` and failed under
lock. If it cannot be resolved at all, the answer is the neutral layout
(`lanes.py:224`), which is the pre-lane order. This key is a fairness weight,
not a correctness invariant, and guessing a layout would reorder the queue on a
fact nobody measured.

### 2.2 Boundaries

| Case | Behaviour |
|---|---|
| Contributor's worker goes quiet mid week | The row persists, so the lane lives until 7 days after the last delivery. Then their rows join the commons and their prefix sum is recomputed there, which can move them back. Queued work is never cancelled and a live lease is never revoked. |
| Anonymous burst | One member of the commons. Contributor lanes untouched; the burst still gets a full lane's worth of service, interleaved. |
| Contributor with nothing queued | Costs nothing. No share is reserved for an idle account. |
| Never ran a worker, new account | Commons. This is the alt account property. |
| First delivery mid queue | Their rows leave the commons and open a lane. Promotion only helps them, and lowers `members` for the commons, which helps the people left in it. |
| Account revoked | Falls to the commons, the same gate that closes the worker protocol. |

## 3. A backed request rides the lightest of its requesters

Reported live on 15 August (Eclipsia, on a request authored by soothdest). A
request several people had asked for was charged to its author alone, so with a
saturated author it stayed buried under that author's own backlog, roughly 2690
rows deep, and backing it changed nothing anybody could see. That reads as the
feature being broken, and it is a fair reading: the point of backing is that
more people waiting means sooner, not later.

A request now travels in the lane of the **best positioned** of its requesters
(`lanes.effective_account`, `lanes.py:129`):

```
lane_account = argmin over a in ({requested_by} + also_requested_by, named only)
               of charged nodes queued by a
```

Ties break on the author, so an unbacked request is unchanged by construction,
and an anonymous author with a named backer is adopted by the backer. Empty
means "the author" (`models.py:397`), which is why the column needed no
backfill and why deploying it moved nothing.

It is resolved when the **requester set changes**, not on every lease: the
candidates live in a JSON list and their loads are aggregates over the table the
window is already scanning, so neither fits in the one statement the lease read
can afford. `add_requester` (`ingest.py:3811`) resolves it when a backer joins,
and `withdraw_requester` (`ingest.py:3848`) resolves it again on handover, next
to where it already resets `queue_seq` and for the same reason: the row changed
owner, so a loan describing the old set of requesters is stale, and the backer
who lent the lane may be the very account that just became the author.

The assignment is a snapshot. If the chosen backer later fills their own queue,
the request rides a lane that is no longer the lightest. That is bounded (it is
by then near the front of that lane) and visible on the queue page.

**Authorship does not move.** The notice when the result lands, the profile
queue, and the own-worker affinity all still read `requested_by`. Lending a lane
is not taking the request.

## 4. The cap: per person, counted in requests

The vote asked for a cap on queued nodes per lane. The owner rejected both a
global cap and a cap in nodes during review, and the shipped rule is **5000
queued requests per person**, with the anonymous commons carrying one allowance
for the whole bucket because that bucket is one member.

`REQUEST_QUEUE_MAX` (`views.py:122`) keeps its number and its name and changed
what it counts. It used to sum every pending request in the project, so one busy
account could lock out people who had queued nothing: the same hoarding the
lanes exist to undo, sitting on the front door. It also lines the ceiling up
with `/api/request`, whose hourly allowance was per account from the day it
shipped.

Counting requests rather than nodes is the part worth defending: a node budget
is not something a person picks, the position's own ladder picks it, so a cap in
nodes would punish somebody for clicking on deep positions the site itself
decided were expensive. A request is what a human does.

Four gates create USER rows and all four read one predicate
(`_account_queue_full`, `views.py:390`): the bulk button (`:1281`), PV verify
(`:1353`), and `_queue_request` (`:1635`), which serves both the explorer click
and the public API. A second implementation of the ceiling for the API is
exactly how a ceiling stops existing without anybody noticing.

Waiting work counts; running work does not (it has an engine on it and will
finish); requests on solved positions count for nobody, because nobody serves
them; and cancelled work counts for nobody, so taking a request back hands the
allowance back and `clear_own_queue` (`ingest.py:3901`) hands all of it back at
once. The cap charges the **author**, not the lane account of section 3: backing
somebody else's request must not spend your allowance.

A refusal returns `queue-full-account` with a 503 and writes a `LANE_CAP_HIT`
event (`views.py:410`). It takes 5000 waiting requests from one account to write
one, so every row is signal.

## 5. What did not change

The own-worker affinity, verbatim. The 10B lease cap at a third of the live pool
with its floor of one and its pass that drops the cap rather than idle a slot
(`views.py:87`, `:463`). The band order `USER > FILL > AUTO`. Position priority
inside AUTO and FILL. The 24 hour starvation escape (`live_request.py:77`). The
32 leases per physical machine (`views.py:64`).

`named_first` stays for now, to be revisited with data from the queue page
rather than with an argument.

## 6. Seeing it work

`/atomicdb/queue/` (`urls.py:106`) groups the waiting band by lane and shows,
per lane: the people in it, waiting and running rows, queued nodes, and the
share of the last hour's served nodes. Its columns read the same `CHARGED` and
`SERVEABLE` predicates the ordering charges (`lanes.measure_lanes`,
`lanes.py:308`), so a disagreement between that table and the queue is a bug in
one place rather than two opinions.

The served share needs no new writes: completed tasks from the last hour grouped
by requester and folded into lanes, behind the shared snapshot with a 60 second
entry. There is no per-lease event, because at about 1,100 tasks an hour that
would be noise rather than observability.

## 7. How it shipped, and the decisions behind it

Three changes, in this order, each green before the next:

1. **The vocabulary and the evidence.** The shared contributor predicate,
   `last_result_at`, and the queue page reading lanes without ordering by them.
   No behaviour change, so the community watched the queue as lanes before the
   ordering moved.
2. **The switch.** `SERVICE_KEYS` moved to `lane_ahead`, the partition gained
   the effective account and the multiplier, the predicate moved onto
   deliveries, and the backed-request rule landed.
3. **The ceiling.** Per person, counted in requests, across all four gates.

Decisions the owner closed on 15 August, recorded because the reasons outlive
the conversation:

1. **A lane is earned by deliveries**, not by saying hello.
2. **`is_staff` earns no lane.** The rung selector keeps its owner shortcut; the
   fleet share does not have one.
3. **`named_first` stays**, revisit with data.
4. **No global cap and no cap in nodes.** Per person, 5000 requests, one
   allowance for the anonymous commons.
5. **No further sub cap inside the commons.** The member-level prefix sum
   already stops one commons member monopolising service.
6. **Anonymous stay one commons member.**

One consequence to watch, flagged when the global cap came out: with N accounts
each able to queue 5000, the total pending band is no longer bounded by a single
number. The ordering sorts that band on every lease, and it was measured at
22.6 ms over 2,825 rows. The queue page is where that would first become
visible.
