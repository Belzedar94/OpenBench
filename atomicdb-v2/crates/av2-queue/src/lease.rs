//! The lease (Q-2.1..Q-2.29): recycling, replay, restart relief, the machine
//! cap, the service order and the pass sequence.
//!
//! v1 computes the order with a window function in SQL, locks candidates in
//! chunks with `SKIP LOCKED` and reads the head of each account's queue as
//! an optimisation (Q-2.20, Q-2.21).  In the single writer the order is
//! computed in memory over the live rows and the lease is "the first eligible
//! row in the total order" (OQ-16): no row is locked and skipped, and a slot
//! cannot hold two leases from two concurrent calls.

use crate::consts::*;
use crate::env::QueueEnv;
use crate::input::*;
use crate::model::*;
use crate::selector::Refill;
use crate::state::QueueState;
use av2_model::Status;
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};

/// What one worker sees of the preferences (Q-2.14).
#[derive(Clone, Debug)]
pub(crate) struct ServeCtx {
    pub user: String,
    pub serve_order: ServeOrder,
    pub serve_first: Vec<String>,
    pub personal: bool,
    pub personal_for: Vec<String>,
    pub campaign: Option<u32>,
    pub house: bool,
}

impl ServeCtx {
    pub(crate) fn new(q: &QueueState, user: &str, machine: &str) -> ServeCtx {
        let pref = q.pref(user);
        let mut serve_order = if user.is_empty() { ServeOrder::Own } else { pref.serve_order };
        let mut serve_first = if user.is_empty() || serve_order == ServeOrder::Queue {
            Vec::new()
        } else {
            pref.serve_for.clone()
        };
        let base: String = machine_base(machine).chars().take(64).collect();
        let (mode, pick) = if user.is_empty() {
            (WorkerMode::Account, None)
        } else {
            match q.serving.get(&(user.to_string(), base)) {
                Some(s) => (s.mode, s.campaign),
                None => (WorkerMode::Account, None),
            }
        };
        let pick = if user.is_empty() { None } else { pick.or(pref.serve_campaign) };
        let campaign = pick.filter(|&c| c > 0).map(|c| c as u32);
        let mut personal_for = Vec::new();
        match mode {
            WorkerMode::Server => {
                serve_order = ServeOrder::Queue;
                serve_first.clear();
            }
            WorkerMode::Personal => personal_for = if user.is_empty() { Vec::new() } else { pref.serve_for.clone() },
            WorkerMode::Account => {}
        }
        ServeCtx {
            user: user.to_string(),
            serve_order,
            serve_first,
            personal: mode == WorkerMode::Personal,
            personal_for,
            campaign,
            house: q.config.house_accounts.iter().any(|h| h == user),
        }
    }

    /// `own_q`: authored (exact) or backed by this account.
    pub(crate) fn own(&self, t: &Task) -> bool {
        t.requested_by == self.user || (!self.user.is_empty() && t.also.iter().any(|b| *b == self.user))
    }

    /// `requester_q(names)`.
    pub(crate) fn requester(t: &Task, names: &[String]) -> bool {
        for name in names {
            if name.to_lowercase() == "anonymous" {
                if t.requested_by.is_empty() {
                    return true;
                }
            } else if iequal(&t.requested_by, name) {
                return true;
            }
        }
        names
            .iter()
            .filter(|n| !n.is_empty() && n.to_lowercase() != "anonymous")
            .any(|n| t.also.iter().any(|b| !b.is_empty() && iequal(b, n)))
    }

    fn listed_band(&self) -> Option<i8> {
        match self.serve_order {
            ServeOrder::Others => Some(2),
            ServeOrder::Own => Some(1),
            ServeOrder::Queue => None,
        }
    }

    /// Q-2.16.1.
    pub(crate) fn own_first(&self, t: &Task) -> i8 {
        if t.source != Source::User {
            return 0;
        }
        if !self.serve_first.is_empty() {
            if let Some(b) = self.listed_band() {
                if Self::requester(t, &self.serve_first) {
                    return b;
                }
            }
        }
        if self.own(t) {
            return match self.serve_order {
                ServeOrder::Own => 1,
                ServeOrder::Queue => 0,
                ServeOrder::Others => -1,
            };
        }
        0
    }

    /// `first_band` of the personal filter / campaign band.
    fn first_band(&self, t: &Task) -> bool {
        t.source == Source::User && (!self.personal || self.own(t) || Self::requester(t, &self.personal_for))
    }
}

/// Sortable image of an f64 (`-0.0` and `0.0` equal, as SQL compares them).
fn f64_key(x: f64) -> i64 {
    let x = x + 0.0;
    let b = x.to_bits() as i64;
    b ^ ((((b >> 63) as u64) >> 1) as i64)
}

/// The lease order keys of Q-2.16, ascending.
type OrderKey = (i8, i8, i8, i8, u64, i64, u32, i64, u64);

impl QueueState {
    /// Q-2.3 / Q-2.31: capacity touch.
    pub(crate) fn touch_worker(&mut self, now: Ms, account: &str, machine: &str, tel: &Telemetry) {
        let p = self.ping_mut(machine, account);
        p.threads = tel.threads.min(MAX_REPORTED_THREADS);
        p.hash_mb = tel.hash_mb;
        p.os = tel.os.chars().take(64).collect();
        p.last_seen = now;
        if let Some(b) = tel.worker_build.filter(|&b| (1..=MAX_WORKER_BUILD).contains(&b)) {
            p.worker_build = Some(b);
        }
    }

    /// Q-2.5 (and Q-2.6 under D8): requeue every lease silent for more than
    /// `LEASE_MINUTES` (strict `<`).  Returns the requeued ids.
    pub(crate) fn recycle_silent_leases(&mut self, now: Ms) -> Vec<u64> {
        let stale = now - LEASE_MINUTES;
        let ids: Vec<u64> = self
            .idx
            .leased
            .iter()
            .copied()
            .filter(|id| {
                let t = &self.tasks[id];
                t.leased_at.map(|l| l < stale).unwrap_or(true) && t.heartbeat_at.map(|h| h < stale).unwrap_or(true)
            })
            .collect();
        for &id in &ids {
            self.update_task(id, |t| t.requeue());
        }
        ids
    }

    /// Q-2.8 `_absorb_requeued`: every PENDING row that once had a lease and
    /// sits on a closed position is absorbed (global, OQ-18 kept as in v1).
    pub(crate) fn absorb_requeued(&mut self, now: Ms, env: &dyn QueueEnv) -> usize {
        let ids: Vec<u64> =
            self.idx.requeued.iter().copied().filter(|id| env.status(self.tasks[id].pos) != Status::Unknown).collect();
        self.absorb(&ids, now)
    }

    /// Q-2.9: the oldest live lease of exactly this slot.
    fn active_same_machine(&self, machine: &str, now: Ms) -> Option<u64> {
        let stale = now - LEASE_MINUTES;
        self.idx
            .leased
            .iter()
            .map(|id| &self.tasks[id])
            .filter(|t| t.machine == machine)
            .filter(|t| match t.heartbeat_at {
                Some(h) => h >= stale,
                None => t.leased_at.map(|l| l >= stale).unwrap_or(false),
            })
            .min_by_key(|t| (t.leased_at, t.id))
            .map(|t| t.id)
    }

    /// Q-2.13 `_machine_at_lease_cap`.
    pub(crate) fn machine_at_lease_cap(&self, machine: &str) -> bool {
        let base = machine_base(machine);
        if base.is_empty() {
            return false;
        }
        let prefix = format!("{base}#");
        let n = self
            .idx
            .leased
            .iter()
            .filter(|id| {
                let m = &self.tasks[id].machine;
                m == base || m.starts_with(&prefix)
            })
            .count();
        n >= LEASES_PER_MACHINE
    }

    /// Q-2.24 `deep_lease_cap` and `_deep_capped_requesters`.
    pub fn deep_capped_requesters(&self, now: Ms) -> BTreeSet<String> {
        let live = self.pings.values().filter(|p| p.last_seen >= now - LIVE_WINDOW).count();
        let cap = DEEP_LEASES_FLOOR.max(live / DEEP_LEASES_POOL_SHARE);
        let mut held: BTreeMap<&str, usize> = BTreeMap::new();
        for id in &self.idx.leased {
            let t = &self.tasks[id];
            if t.source == Source::User && t.budget >= DEEP_TASK_NODES {
                *held.entry(t.requested_by.as_str()).or_default() += 1;
            }
        }
        held.into_iter().filter(|(_, n)| *n >= cap).map(|(a, _)| a.to_string()).collect()
    }

    /// Q-2.19 `served_rank`: account -> rank (0 = no stamp in the window).
    pub(crate) fn served_ranks(&self, now: Ms) -> BTreeMap<String, u32> {
        let cutoff = now - SERVED_WINDOW;
        let mut last: BTreeMap<&str, Ms> = BTreeMap::new();
        for &(at, id) in self.idx.served.range((cutoff, 0)..) {
            let t = &self.tasks[&id];
            if t.source != Source::User {
                continue;
            }
            let ok = t.state == TState::Leased || (t.state == TState::Completed && t.completed.map(|c| c >= cutoff).unwrap_or(false));
            if !ok {
                continue;
            }
            let acct = t.effective_account();
            if !self.idx.user_by_eff.contains_key(acct) {
                continue;
            }
            let e = last.entry(acct).or_insert(at);
            if at > *e {
                *e = at;
            }
        }
        let stamps: BTreeSet<Ms> = last.values().copied().collect();
        let rank: BTreeMap<Ms, u32> = stamps.iter().enumerate().map(|(i, s)| (*s, i as u32 + 1)).collect();
        last.into_iter().map(|(a, s)| (a.to_string(), rank[&s])).collect()
    }

    /// Whether a row passes this worker's row filter (house, personal,
    /// SERVEABLE), Q-2.15.
    fn passes(&self, ctx: &ServeCtx, t: &Task, env: &dyn QueueEnv) -> bool {
        if ctx.house && t.source == Source::User && t.requested_by != ctx.user {
            return false;
        }
        if ctx.personal {
            let in_campaign = ctx.campaign.map(|c| env.campaign(t.pos) == c).unwrap_or(false);
            if !(ctx.first_band(t) || in_campaign) {
                return false;
            }
        }
        env.status(t.pos) == Status::Unknown
    }

    /// The ordered PENDING candidates of Q-2.15/Q-2.16 for this worker.
    pub(crate) fn lease_order(&self, ctx: &ServeCtx, now: Ms, env: &dyn QueueEnv) -> Vec<(OrderKey, u64)> {
        let starved = now - STARVED_AFTER;
        let ranks = self.served_ranks(now);
        let mut out: Vec<(OrderKey, u64)> = Vec::with_capacity(self.idx.charged.len());
        let band = |t: &Task| -> i8 {
            match ctx.campaign {
                None => 0,
                Some(c) => {
                    if ctx.first_band(t) {
                        2
                    } else if env.campaign(t.pos) == c {
                        1
                    } else {
                        0
                    }
                }
            }
        };
        for (acct, rows) in &self.idx.user_by_eff {
            let served = ranks.get(acct.as_str()).copied().unwrap_or(0);
            let mut cum: u64 = 0;
            for &(_, _, _, id) in rows {
                let t = &self.tasks[&id];
                if !self.passes(ctx, t, env) {
                    continue;
                }
                let ahead = cum;
                cum = cum.saturating_add(t.budget);
                if t.state != TState::Pending {
                    continue;
                }
                let named = !t.requested_by.is_empty() || t.created < starved;
                let key: OrderKey = (
                    -band(t),
                    -(Source::User.rank() as i8),
                    -ctx.own_first(t),
                    -(named as i8),
                    ahead,
                    -(t.backers() as i64),
                    served,
                    f64_key(-0.0),
                    t.id,
                );
                out.push((key, id));
            }
        }
        for &id in &self.idx.other_charged {
            let t = &self.tasks[&id];
            if t.state != TState::Pending || !self.passes(ctx, t, env) {
                continue;
            }
            let key: OrderKey =
                (-band(t), -(t.source.rank() as i8), 0, 0, 0, 0, 0, f64_key(-env.queue_rank(t.pos)), t.id);
            out.push((key, id));
        }
        out
    }

    /// The same order as `lease_order`, produced lazily (the lease usually
    /// needs only its first few rows).
    ///
    /// A *plain* partition (every row authored by its effective account,
    /// nobody backing any, not anonymous) has one value of every key that
    /// comes before `lane_ahead` (band, source, own first, named), the same
    /// backers (0) and the same served rank; `lane_ahead` strictly grows
    /// along the window order (budgets are positive).  So inside it the
    /// lease order IS the window order, and only its head needs to be in the
    /// heap.  Other partitions, AUTO/FILL rows, and workers with a personal
    /// filter or a campaign band go through the full computation.  The
    /// property test `lazy_order_equals_full_order` checks the equivalence.
    pub(crate) fn merge<'a>(&'a self, ctx: &'a ServeCtx, now: Ms, env: &'a dyn QueueEnv) -> Merge<'a> {
        let mut m = Merge { q: self, ctx, env, heap: BinaryHeap::new(), cursors: Vec::new() };
        if ctx.personal || ctx.campaign.is_some() {
            m.heap.extend(self.lease_order(ctx, now, env).into_iter().map(|(k, id)| Reverse((k, id, usize::MAX))));
            return m;
        }
        let starved = now - STARVED_AFTER;
        let ranks = self.served_ranks(now);
        for (acct, rows) in &self.idx.user_by_eff {
            let served = ranks.get(acct.as_str()).copied().unwrap_or(0);
            let plain = !acct.is_empty() && !self.idx.user_irregular.contains_key(acct);
            if plain {
                let Some(&(_, _, _, first)) = rows.iter().next() else { continue };
                let own = ctx.own_first(&self.tasks[&first]);
                let mut c = Cursor { iter: rows.iter(), cum: 0, served, own };
                if let Some(head) = c.next(self, ctx, env) {
                    let idx = m.cursors.len();
                    m.cursors.push(c);
                    m.heap.push(Reverse((head.0, head.1, idx)));
                }
                continue;
            }
            let mut cum: u64 = 0;
            for &(_, _, _, id) in rows {
                let t = &self.tasks[&id];
                if !self.passes(ctx, t, env) {
                    continue;
                }
                let ahead = cum;
                cum = cum.saturating_add(t.budget);
                if t.state != TState::Pending {
                    continue;
                }
                let named = !t.requested_by.is_empty() || t.created < starved;
                let key: OrderKey =
                    (0, -(Source::User.rank() as i8), -ctx.own_first(t), -(named as i8), ahead, -(t.backers() as i64), served, f64_key(-0.0), t.id);
                m.heap.push(Reverse((key, id, usize::MAX)));
            }
        }
        for &id in &self.idx.other_charged {
            let t = &self.tasks[&id];
            if t.state != TState::Pending || !self.passes(ctx, t, env) {
                continue;
            }
            let key: OrderKey = (0, -(t.source.rank() as i8), 0, 0, 0, 0, 0, f64_key(-env.queue_rank(t.pos)), t.id);
            m.heap.push(Reverse((key, id, usize::MAX)));
        }
        m
    }

    /// Both orders, for the equivalence test.
    #[doc(hidden)]
    pub fn orders_for_test(&self, account: &str, machine: &str, now: Ms, env: &dyn QueueEnv) -> (Vec<u64>, Vec<u64>) {
        let ctx = ServeCtx::new(self, account, machine);
        let mut full = self.lease_order(&ctx, now, env);
        full.sort_unstable();
        let mut lazy = Vec::new();
        let mut m = self.merge(&ctx, now, env);
        while let Some(id) = m.pop() {
            lazy.push(id);
        }
        (full.into_iter().map(|x| x.1).collect(), lazy)
    }

    /// `choose_pending` (Q-2.20..Q-2.27): the first eligible row.
    pub(crate) fn choose_pending(
        &self,
        r: &LeaseReq,
        ctx: &ServeCtx,
        env: &dyn QueueEnv,
        honour_deep_cap: bool,
        sweep_only: bool,
    ) -> Option<u64> {
        let capped = if honour_deep_cap { self.deep_capped_requesters(r.now) } else { BTreeSet::new() };
        let mut first = None;
        let mut gate = |id: u64| -> Option<Option<u64>> {
            let t = &self.tasks[&id];
            if r.max_budget.map(|m| t.budget > m).unwrap_or(false) {
                return None;
            }
            if env.status(t.pos) != Status::Unknown || t.generation > env.visits(t.pos) {
                return None;
            }
            if t.budget >= DEEP_TASK_NODES && t.source == Source::User && capped.contains(&t.requested_by) {
                return None;
            }
            if !fits_slot(t, r.telemetry.threads) {
                return None;
            }
            if env.server_tables_cover(t.pos) {
                return None;
            }
            if first.is_none() {
                first = Some(id);
            }
            if r.tb || !env.tb_applicable(t.pos) {
                return Some(Some(id));
            }
            None
        };
        if sweep_only {
            let ids: Vec<u64> = self.sweep_waiting_tasks(env);
            for id in ids {
                if let Some(Some(id)) = gate(id) {
                    return Some(id);
                }
            }
            return first;
        }
        let mut order = self.merge(ctx, r.now, env);
        while let Some(id) = order.pop() {
            if let Some(Some(id)) = gate(id) {
                return Some(id);
            }
        }
        first
    }

    /// `sweep.waiting_tasks()`: PENDING sweep16 FILL rows on open positions, oldest first.
    pub(crate) fn sweep_waiting_tasks(&self, env: &dyn QueueEnv) -> Vec<u64> {
        self.idx
            .pending_arm
            .get(ARM_SWEEP16)
            .into_iter()
            .flatten()
            .copied()
            .filter(|id| {
                let t = &self.tasks[id];
                t.source == Source::Fill && env.status(t.pos) == Status::Unknown
            })
            .collect()
    }

    /// `sweep.take_share()` (Q-2.28.1), with the waiting flag and the lease
    /// counter as writer state (the 60 s flag is kept as in v1, OQ-12).
    fn sweep_take_share(&mut self, now: Ms, env: &dyn QueueEnv) -> bool {
        let share = self.config.sweep_share();
        if share <= 0.0 || !self.sweep_has_waiting(now, env) {
            return false;
        }
        if share >= 1.0 {
            return true;
        }
        self.sweep_leases += 1;
        self.tx.counters = true;
        let n = self.sweep_leases as f64;
        (n * share).floor() != ((n - 1.0) * share).floor()
    }

    fn sweep_has_waiting(&mut self, now: Ms, env: &dyn QueueEnv) -> bool {
        if let Some((w, until)) = self.sweep_waiting {
            if now < until {
                return w;
            }
        }
        let w = !self.sweep_waiting_tasks(env).is_empty();
        self.sweep_note_waiting(w, now);
        w
    }

    pub(crate) fn sweep_note_waiting(&mut self, waiting: bool, now: Ms) {
        self.sweep_waiting = Some((waiting, now + MINUTE));
        self.tx.sweep = true;
    }

    /// The lease (Q-2.1..Q-2.29).
    pub(crate) fn lease(&mut self, r: &LeaseReq, env: &dyn QueueEnv, refill: &mut dyn Refill) -> LeaseOut {
        // D8: a build that cannot hold a token gets nothing (and leaves no ping).
        if r.build < LEASE_TOKEN_BUILD {
            return LeaseOut::UpgradeRequired;
        }
        let now = r.now;
        self.touch_worker(now, &r.account, &r.machine, &r.telemetry);
        self.recycle_silent_leases(now);
        self.absorb_requeued(now, env);

        let mut held = None;
        let mut chosen = None;
        let mut replayed = None;
        if let Some(active) = self.active_same_machine(&r.machine, now) {
            let t = &self.tasks[&active];
            if !r.session.is_empty() && t.session == r.session {
                replayed = Some(active);
            } else {
                let beat = t.heartbeat_at.or(t.leased_at);
                if beat.map(|b| b < now - RESTART_RECYCLE_MINUTES).unwrap_or(false) {
                    self.update_task(active, |t| t.requeue());
                } else {
                    held = Some(active);
                }
            }
        }
        if replayed.is_none() && held.is_none() && !self.machine_at_lease_cap(&r.machine) {
            let ctx = ServeCtx::new(self, &r.account, &r.machine);
            if !ctx.personal && self.sweep_take_share(now, env) {
                chosen = self.choose_pending(r, &ctx, env, true, true);
                if chosen.is_none() {
                    self.sweep_note_waiting(false, now);
                }
            }
            if chosen.is_none() {
                chosen = self.choose_pending(r, &ctx, env, true, false);
            }
            if chosen.is_none() && !ctx.personal && r.max_budget.map(|m| m >= SERVER_PICKED_MIN_NODES).unwrap_or(true) {
                let plan = refill.next_tasks(self, env, TASK_REFILL_COUNT, now);
                self.apply_mint(&plan, now, env);
                chosen = self.choose_pending(r, &ctx, env, true, false);
            }
            if chosen.is_none() {
                chosen = self.choose_pending(r, &ctx, env, false, false);
            }
        }
        if let Some(id) = replayed {
            let t = &self.tasks[&id];
            let grant = grant_of(t, true);
            let p = self.ping_mut(&r.machine, &r.account);
            p.current_task = Some(id);
            p.last_nps = 0;
            p.nps_updated = None;
            p.last_seen = now;
            return LeaseOut::Task { grant };
        }
        if let Some(id) = chosen {
            self.update_task(id, |t| {
                t.state = TState::Leased;
                t.machine = r.machine.clone();
                t.leased_at = Some(now);
                t.heartbeat_at = Some(now);
                t.token = r.token.clone();
                t.session = r.session.clone();
                t.attempts += 1;
            });
            let grant = grant_of(&self.tasks[&id], false);
            let p = self.ping_mut(&r.machine, &r.account);
            p.current_task = Some(id);
            p.last_nps = 0;
            p.nps_updated = None;
            p.last_seen = now;
            return LeaseOut::Task { grant };
        }
        let p = self.ping_mut(&r.machine, &r.account);
        p.last_seen = now;
        if let Some(h) = held {
            p.current_task = Some(h);
            return LeaseOut::Busy { held: h };
        }
        p.current_task = None;
        p.last_nps = 0;
        p.nps_updated = None;
        LeaseOut::Empty
    }

    /// What the next lease of this worker would get, without changing
    /// anything (tests and the estimator): the order and the gates only.
    pub fn peek_lease(&self, r: &LeaseReq, env: &dyn QueueEnv) -> Option<u64> {
        let ctx = ServeCtx::new(self, &r.account, &r.machine);
        self.choose_pending(r, &ctx, env, true, false).or_else(|| self.choose_pending(r, &ctx, env, false, false))
    }
}

/// A plain partition walked in window order (see `QueueState::merge`).
pub(crate) struct Cursor<'a> {
    iter: std::collections::btree_set::Iter<'a, (u8, bool, i64, u64)>,
    cum: u64,
    served: u32,
    own: i8,
}

impl Cursor<'_> {
    fn next(&mut self, q: &QueueState, ctx: &ServeCtx, env: &dyn QueueEnv) -> Option<(OrderKey, u64)> {
        for &(_, _, _, id) in self.iter.by_ref() {
            let t = &q.tasks[&id];
            if !q.passes(ctx, t, env) {
                continue;
            }
            let ahead = self.cum;
            self.cum = self.cum.saturating_add(t.budget);
            if t.state != TState::Pending {
                continue;
            }
            let key: OrderKey = (0, -(Source::User.rank() as i8), -self.own, -1, ahead, 0, self.served, f64_key(-0.0), t.id);
            return Some((key, id));
        }
        None
    }
}

/// The lease order, popped one row at a time.
pub(crate) struct Merge<'a> {
    q: &'a QueueState,
    ctx: &'a ServeCtx,
    env: &'a dyn QueueEnv,
    heap: BinaryHeap<Reverse<(OrderKey, u64, usize)>>,
    cursors: Vec<Cursor<'a>>,
}

impl Merge<'_> {
    pub(crate) fn pop(&mut self) -> Option<u64> {
        let Reverse((_, id, c)) = self.heap.pop()?;
        if c != usize::MAX {
            if let Some(next) = self.cursors[c].next(self.q, self.ctx, self.env) {
                self.heap.push(Reverse((next.0, next.1, c)));
            }
        }
        Some(id)
    }
}

/// `idle_repair.fits_slot` (Q-2.25).
pub fn fits_slot(t: &Task, threads: u32) -> bool {
    if t.arm != ARM_REPAIR || t.source != Source::Auto {
        return true;
    }
    threads > 0 && threads <= REPAIR_MAX_THREADS
}

fn grant_of(t: &Task, replayed: bool) -> LeaseGrant {
    LeaseGrant {
        id: t.id,
        pos: t.pos,
        budget: t.budget,
        multipv: t.multipv,
        token: t.token.clone(),
        source: t.source,
        requested_by: t.requested_by.clone(),
        route: t.route.clone(),
        replayed,
    }
}

/// `_live_moves` (Q-2.29.1): the moves whose child is still open, only when
/// some but not all children are closed.  Edge order (OQ-14 / D3).
pub fn searchmoves(env: &dyn QueueEnv, pos: u64) -> Vec<String> {
    let p = env.pos(pos);
    if !p.expanded {
        return Vec::new();
    }
    let edges = env.children(pos);
    let live: Vec<String> = edges.iter().filter(|(_, c)| env.status(*c) == Status::Unknown).map(|(m, _)| m.clone()).collect();
    if !live.is_empty() && live.len() < edges.len() { live } else { Vec::new() }
}
