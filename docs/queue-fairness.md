# AtomicDB queue fairness

**As built, 16 August.** Contributor lanes existed from 10 to 16 August and
were removed. This document describes what is running now, and keeps the
history because the removal is only legible against it.

## The short version

- One shared queue. Every account advances by the nodes it already has
  queued, so the first request of a newcomer is served before the thousandth
  of a heavy user, and a big batch interleaves instead of walling the queue.
- Running a worker buys exactly one scheduling advantage: while it runs, it
  serves its owner's requests first. That advantage scales with the CPU you
  are plugging in right now.
- Backing somebody's request charges it to the best placed of its
  requesters, so joining a buried request actually moves it.
- The cap is per person: 5,000 queued requests, pending rows only.
- All of it is public at `/atomicdb/queue/`, one row per account, read from
  the same predicates the ordering uses.

## 1. How the queue is ordered

Every analysis job is one `AnalysisTask` row. Ordering happens in
`choose_pending` (`atomicdb/views.py`), inside the lease transaction:

| Step | Expression | Effect |
|---|---|---|
| 1 | `-source` | USER before FILL before AUTO |
| 2 | `-own_first` | this worker's account serves its own USER requests first |
| 3 | `-named_first` | named, or waiting over 24h, before the fresh anonymous tide |
| 4 | `lane_ahead` | per-account deficit round robin, section 2 |
| 5 | `-backer_rank` | among rows served together, the one more people asked for |
| 6 | `-queue_rank` | position priority, zero across the whole USER band |
| 7 | `id` | arrival order, stable tiebreak |

Steps 3 to 5 are `live_request.SERVICE_KEYS`, shared literally with the
estimator that tells a waiting visitor how many requests are ahead and with
the profile queue. One definition, three readers: when they were three
similar queries, the day one changed the others started lying to somebody
who was waiting.

Step 2 is the whole contributor privilege. It is affinity, not priority: it
only exists while your worker is polling, and it only affects your own
machine.

## 2. The per-account weight (`live_request.fair_share`)

`lane_ahead` is, for each USER row, the sum of `budget_nodes` this account
already has charged (pending plus leased) ahead of that row. The first
request of every account carries zero, so the first request of anybody is
served before the second of anybody else; and because the unit is nodes and
not rows, one 10B request yields to seventy-eight 128M requests, which is
what they cost. Nobody has a quota and nobody loses access: a burst of 1,500
requests still gets served, interleaved.

The column name survives the lanes it was born with, because every consumer
of the ordering reads it and a rename would touch each of them to change
nothing.

A backed request is charged to the best placed of its requesters
(`lanes.effective_account`, stored in `lane_account`, migration 0045): the
15 August case, where a request several people backed stayed buried under
its author's own backlog, is the reason the column exists. Authorship never
moves; only the charge does.

## 3. What happened to the lanes

The 10 August vote (3 to 1) created private lanes: an account that delivered
worker results inside a 7 day window got its own share of the fleet, and
everybody else shared one commons. Delivery was measured honestly
(`WorkerPing.last_result_at`, migration 0044, stamped only when an analysis
lands, never by polling), and alt accounts bought nothing because the
commons was one lane however many people stood in it.

Wolfram's 16 August critique killed the design, with the production data
agreeing with him:

- The window was binary. One delivered analysis bought the same lane as a
  thousand, so the rational move for anybody stuck in the commons was to run
  a worker for one minute as a toll. He was doing exactly that and said so.
- The lane holders were not the queue pressure. The heavy queues belonged to
  accounts that had never run a worker, and the flooding that the fairness
  work actually needed to stop was already stopped by the per-account weight,
  which never depended on lanes.
- Any repair (proportional shares, decay windows) buys complexity to defend
  an idea nobody was attached to.

So the lanes went, and what remains is the arrangement above: per-account
weight, affinity as the only contributor advantage, per-person cap. The
delivery-window predicate survives in `lanes.py` because the rung selector
(`depth.may_choose`) still reads it: choosing deep budgets is trusted to
people who put real work into the fleet recently.

The honest cost of the removal: alt accounts are accounts again. Each one
advances at the commons rate, so identities multiply share. The guards are
the per-person cap, the public queue page where the split is visible, and
the fact that queue position without a worker is still bounded by what the
fleet owner tolerates seeing there.

## 4. The cap

Per person, counted in requests, pending rows only: 5,000. The global cap it
replaced locked everybody out when one account was busy, which is the same
hoarding the weighting exists to undo, moved to the front door. Cancelled
rows do not count, running rows do not count, and clearing your queue gives
the allowance back immediately. Refusals leave a `LANE_CAP_HIT` receipt.

## 5. Attribution (front page, 16 August fix)

The front page leaderboards attribute completed work to accounts. Machine
names are worker-chosen, and two accounts can announce the same one; the old
machine-to-owner dict kept an arbitrary row, which is how an account that
never delivered anything appeared as a 24h contributor (reported by Wolfram,
reproduced with soothdest and NitroColoraze both claiming
`NitroColoraze-zen4`).

Since migration 0046, every completed task is stamped with the
authenticated account that delivered it (`AnalysisTask.delivered_by`), and
the boards group by that stamp. Rows older than the stamp fall back to the
machine map, unambiguous machines only: a disputed machine attributes to
nobody, exactly like a machine with no `WorkerPing` at all. Its nodes still
count for the fleet, because they were really searched, and the page says
out loud what belongs to nobody. The 24h board heals itself within a day of
the deploy; the all-time board keeps an unattributed remainder for old rows
on disputed machines, which is the honest number.
