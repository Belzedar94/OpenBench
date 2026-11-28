//! Requests, lanes and the people's queue controls (Q-5, Q-6, Q-8.16..Q-8.20).

use crate::budget::*;
use crate::consts::*;
use crate::env::{PosInfo, QueueEnv};
use crate::input::*;
use crate::model::*;
use crate::state::QueueState;
use av2_model::Status;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

/// What `_request_rung` / `request_analysis` answer (Q-6.1).
#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    Queued,
    AlreadyQueued {
        adopted: bool,
    },
    AlreadySolved,
    AlreadyAnalyzed {
        completed_budget: Option<u64>,
    },
    LadderSpent,
    /// Internal sentinel `_LADDER_EXHAUSTED`: never returned to a caller.
    LadderExhausted,
    /// The ladder is spent and the request falls to `request_subtree`
    /// (Q-7.10): not ported in this crate yet; the door answers `saturated`.
    Subtree,
}

impl Outcome {
    pub fn status(&self) -> &'static str {
        match self {
            Outcome::Queued => "queued",
            Outcome::AlreadyQueued { .. } => "already-queued",
            Outcome::AlreadySolved => "already-solved",
            Outcome::AlreadyAnalyzed { .. } => "already-analyzed",
            Outcome::LadderSpent => "ladder-spent",
            Outcome::LadderExhausted => "ladder-exhausted",
            Outcome::Subtree => "saturated",
        }
    }
}

/// Q-6.23 `notification_deserved(source, budget_nodes)`.
pub fn notification_deserved(source: Source, budget: u64) -> bool {
    source == Source::User || budget >= NOTIFY_MIN_BUDGET
}

/// `lanes.effective_account(author, backers, loads)` (Q-5.3).
pub fn effective_account(author: &str, backers: &[String], loads: &BTreeMap<String, u64>) -> String {
    let mut cands: Vec<&str> = Vec::new();
    if !author.is_empty() {
        cands.push(author);
    }
    for b in backers {
        if !b.is_empty() {
            cands.push(b);
        }
    }
    if cands.len() < 2 {
        return String::new();
    }
    let best = cands.iter().enumerate().min_by_key(|(i, n)| (loads.get(**n).copied().unwrap_or(0), *i)).map(|(_, n)| *n).unwrap();
    if best == author { String::new() } else { best.to_string() }
}

/// `is_unexplored(child)` (Q-8.16).
pub fn is_unexplored(p: &PosInfo) -> bool {
    p.status == Status::Unknown && p.nodes_invested == 0 && p.backed_nodes == 0
}

/// `is_unjudged(child)` (Q-8.17).
pub fn is_unjudged(p: &PosInfo) -> bool {
    p.status == Status::Unknown && p.eval_cp.is_none() && p.backed_eval.is_none()
}

/// `unexplored_children(pos)` in edge order.
pub fn unexplored_children(env: &dyn QueueEnv, pos: u64) -> Vec<(String, u64)> {
    env.children(pos).into_iter().filter(|(_, c)| is_unexplored(&env.pos(*c))).collect()
}

/// `unjudged_children(pos)` in edge order.
pub fn unjudged_children(env: &dyn QueueEnv, pos: u64) -> Vec<(String, u64)> {
    env.children(pos).into_iter().filter(|(_, c)| is_unjudged(&env.pos(*c))).collect()
}

/// Q-6.13 `_merge_priority`: the new `background`, if it changes.
fn merged_background(current: bool, background: bool, fresh: bool) -> bool {
    if fresh { background } else { current && background }
}

impl QueueState {
    // ------------------------------------------------------------ lanes (Q-5)

    /// Q-5.1 `ran_a_worker(username)`: a delivery within 7 days.  (The
    /// `Profile.enabled` half of `may_choose` lives with the accounts.)
    pub fn ran_a_worker(&self, account: &str, now: Ms) -> bool {
        !account.is_empty()
            && self.pings.iter().any(|((_, u), p)| u == account && p.last_result_at.map(|t| t >= now - CONTRIBUTOR_WINDOW).unwrap_or(false))
    }

    /// Q-5.2 `chosen_rung_for(raw, user)`: `(chosen, invalid)`.  `may_choose`
    /// is the caller's verdict (staff, or enabled and `ran_a_worker`).
    pub fn chosen_rung(raw: &str, may_choose: bool) -> (Option<u64>, bool) {
        let s = raw.trim();
        if s.is_empty() {
            return (None, false);
        }
        let Some(v) = crate::protocol::py_int(s) else { return (None, true) };
        let v = v.max(0) as u64;
        if !REQUEST_BUDGET_LADDER.contains(&v) && v != SUBTREE_REQUEST_NODES {
            return (None, true);
        }
        if !may_choose {
            return (None, false);
        }
        (Some(v), false)
    }

    /// Q-5.4 `charged_loads`: by author, USER CHARGED rows on open positions.
    pub fn charged_loads(&self, names: &[String], env: &dyn QueueEnv) -> BTreeMap<String, u64> {
        let mut out = BTreeMap::new();
        for n in names {
            if out.contains_key(n) {
                continue;
            }
            if let Some(ids) = self.idx.user_by_author.get(n) {
                let s: u64 = ids.iter().map(|id| &self.tasks[id]).filter(|t| env.status(t.pos) == Status::Unknown).map(|t| t.budget).sum();
                out.insert(n.clone(), s);
            }
        }
        out
    }

    /// Q-5.5 `_resolve_lane_account`.
    fn resolve_lane_account(&mut self, id: u64, env: &dyn QueueEnv) {
        let t = &self.tasks[&id];
        let names: Vec<String> = std::iter::once(t.requested_by.clone()).chain(t.also.iter().cloned()).filter(|n| !n.is_empty()).collect();
        let loads = self.charged_loads(&names, env);
        let lane = effective_account(&t.requested_by, &t.also, &loads);
        if lane != t.lane_account {
            self.update_task(id, |t| t.lane_account = lane);
        }
    }

    /// Q-6.17 `front_of_own_queue`.
    pub fn front_of_own_queue(&self, id: u64) -> Option<i64> {
        let t = self.tasks.get(&id)?;
        let account = t.effective_account();
        if account.is_empty() {
            return None;
        }
        let rows = self.idx.user_by_eff.get(account)?;
        let front = rows
            .iter()
            .filter(|(rf, bg, _, rid)| *rf == 1 && *bg == t.background && *rid != id)
            .map(|(_, _, seq, rid)| (*seq, *rid))
            .next()?;
        if (t.queue_seq, t.id) < front {
            return None;
        }
        Some(front.0 - 1)
    }

    /// Q-6.15 `_record_requester`.
    fn record_requester(&mut self, id: u64, user: &str, now: Ms) -> bool {
        let t = &self.tasks[&id];
        if user.is_empty() || !t.state.charged() {
            return false;
        }
        if t.requested_by.is_empty() {
            self.update_task(id, |t| {
                t.requested_by = user.to_string();
                t.requested_at = Some(now);
            });
            return false;
        }
        if t.requested_by == user || t.also.iter().any(|a| a == user) {
            return false;
        }
        self.update_task(id, |t| t.also.push(user.to_string()));
        true
    }

    /// Q-6.16 `add_requester`.
    pub(crate) fn add_requester(&mut self, id: u64, user: &str, now: Ms, env: &dyn QueueEnv) -> bool {
        if !self.record_requester(id, user, now) {
            return false;
        }
        self.resolve_lane_account(id, env);
        if self.tasks[&id].state != TState::Pending {
            return true;
        }
        if let Some(seq) = self.front_of_own_queue(id) {
            self.update_task(id, |t| t.queue_seq = seq);
        }
        true
    }

    /// Q-6.18 `_apply_lifo_preference`.
    fn apply_lifo(&mut self, id: u64, user: &str) {
        if user.is_empty() || !self.prefs.get(user).map(|p| p.lifo_queue).unwrap_or(false) {
            return;
        }
        if let Some(seq) = self.front_of_own_queue(id) {
            self.update_task(id, |t| t.queue_seq = seq);
        }
    }

    /// Q-6.8 `_revive_withdrawn`.
    #[allow(clippy::too_many_arguments)]
    fn revive_withdrawn(&mut self, id: u64, budget: u64, multipv: u32, user: &str, route: &str, source: Source, background: bool, now: Ms) {
        self.update_task(id, |t| {
            t.state = TState::Pending;
            t.completed = None;
            t.budget = budget;
            t.source = source;
            t.requested_by = user.to_string();
            t.also.clear();
            t.queue_seq = 0;
            t.route = route.to_string();
            t.multipv = multipv;
            t.background = background;
            t.requested_at = Some(now);
        });
    }

    /// Q-6.19 `withdraw_requester` (caller checked USER, PENDING, author).
    fn withdraw_requester(&mut self, id: u64, now: Ms, env: &dyn QueueEnv) -> &'static str {
        if !self.tasks[&id].also.is_empty() {
            self.update_task(id, |t| {
                let backers = std::mem::take(&mut t.also);
                t.requested_by = backers[0].clone();
                t.also = backers[1..].to_vec();
                t.queue_seq = 0;
            });
            self.resolve_lane_account(id, env);
            return "handed-over";
        }
        self.update_task(id, |t| {
            t.state = TState::Cancelled;
            t.completed = Some(now);
        });
        "cancelled"
    }

    /// Q-6.38 withdraw door.
    pub(crate) fn withdraw(&mut self, account: &str, id: u64, now: Ms, env: &dyn QueueEnv) -> &'static str {
        let ok = self
            .tasks
            .get(&id)
            .map(|t| t.source == Source::User && t.requested_by == account && t.state == TState::Pending)
            .unwrap_or(false);
        if !ok || account.is_empty() {
            return "not-yours";
        }
        self.withdraw_requester(id, now, env)
    }

    /// Q-6.38 undo door + Q-6.21 `restore_cancelled`.
    pub(crate) fn undo(&mut self, account: &str, id: u64, env: &dyn QueueEnv) -> &'static str {
        let ok = self
            .tasks
            .get(&id)
            .map(|t| t.source == Source::User && t.requested_by == account && t.state == TState::Cancelled)
            .unwrap_or(false);
        if !ok || account.is_empty() {
            return "nothing-to-undo";
        }
        if env.status(self.tasks[&id].pos) != Status::Unknown {
            return "cannot-restore";
        }
        self.update_task(id, |t| {
            t.state = TState::Pending;
            t.completed = None;
        });
        "restored"
    }

    /// Q-6.20 `clear_own_queue`: `(withdrawn, handed over)`.
    pub(crate) fn clear_own_queue(&mut self, account: &str, now: Ms, env: &dyn QueueEnv) -> (usize, usize) {
        if account.is_empty() {
            return (0, 0);
        }
        let mine: Vec<u64> = self
            .idx
            .user_by_author
            .get(account)
            .into_iter()
            .flatten()
            .copied()
            .filter(|id| self.tasks[id].state == TState::Pending)
            .collect();
        let (mut handed, mut by_hand, mut cancelled) = (0, 0, 0);
        for &id in &mine {
            if !self.tasks[&id].also.is_empty() {
                if self.withdraw_requester(id, now, env) == "handed-over" {
                    handed += 1;
                } else {
                    by_hand += 1;
                }
            }
        }
        for &id in &mine {
            let t = &self.tasks[&id];
            if t.state == TState::Pending && t.requested_by == account && t.also.is_empty() && t.source == Source::User {
                self.update_task(id, |t| {
                    t.state = TState::Cancelled;
                    t.completed = Some(now);
                });
                cancelled += 1;
            }
        }
        (cancelled + by_hand, handed)
    }

    /// Q-6.37 bump door.
    pub(crate) fn bump(&mut self, account: &str, id: u64) -> &'static str {
        let ok = self
            .tasks
            .get(&id)
            .map(|t| t.source == Source::User && t.requested_by == account && t.state == TState::Pending)
            .unwrap_or(false);
        if !ok || account.is_empty() {
            return "not-yours";
        }
        match self.front_of_own_queue(id) {
            None => "already-first",
            Some(seq) => {
                self.update_task(id, |t| t.queue_seq = seq);
                "moved"
            }
        }
    }

    // ------------------------------------------------------------ purchase (Q-6.4..Q-6.12)

    fn new_task_on_slot(&mut self, pos: u64, generation: i32, budget: u64, now: Ms) -> u64 {
        let id = self.alloc_task_id();
        self.insert_task(Task::new(id, pos, generation, budget, now));
        id
    }

    /// `_request_rung`.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn request_rung(
        &mut self,
        pos: u64,
        user: &str,
        route: &str,
        budget_floor: Option<u64>,
        only_if_missing: bool,
        only_unexplored: bool,
        background: bool,
        now: Ms,
        env: &dyn QueueEnv,
    ) -> Outcome {
        let p = env.pos(pos);
        if p.status != Status::Unknown {
            return Outcome::AlreadySolved;
        }
        if only_unexplored && !is_unexplored(&p) {
            return Outcome::AlreadyAnalyzed { completed_budget: self.searched_budget(pos) };
        }
        let mut target = None;
        if only_if_missing {
            let tgt = budget_floor.filter(|&b| b > 0).unwrap_or(REQUEST_BUDGET_LADDER[0]);
            target = Some(tgt);
            if let Some(s) = self.searched_budget(pos)
                && s >= tgt
            {
                return Outcome::AlreadyAnalyzed { completed_budget: Some(s) };
            }
        }
        let completed_max = self.completed_max_budget(pos);
        if target.is_none() && completed_max.map(|c| c >= REQUEST_BUDGET_LADDER[2]).unwrap_or(false) {
            return Outcome::LadderExhausted;
        }
        let mut floor = next_rung(completed_max);
        let clamp = short_mate_clamp(&p);
        if let Some((cb, _)) = clamp
            && cb > completed_max.unwrap_or(0)
        {
            floor = cb;
        }
        floor = floor.max(budget_for(&p));
        if let Some(b) = budget_floor.filter(|&b| b > 0) {
            floor = floor.max(b);
        }
        if let Some(tgt) = target {
            floor = floor.min(tgt);
        }
        let generation = p.visits;
        let Some(existing) = self.slot(pos, generation).map(|t| t.id) else {
            let id = self.new_task_on_slot(pos, generation, floor, now);
            self.update_task(id, |t| {
                t.source = Source::User;
                t.requested_by = user.to_string();
                t.route = route.to_string();
                t.requested_at = Some(now);
                t.background = background;
                t.multipv = multipv_for(generation, Some(floor), false, clamp);
            });
            self.apply_lifo(id, user);
            return Outcome::Queued;
        };
        let t = self.tasks[&existing].clone();
        if t.state == TState::Cancelled || t.is_absorbed() {
            self.revive_withdrawn(
                existing,
                floor,
                multipv_for(generation, Some(floor), false, clamp),
                user,
                route,
                Source::User,
                background,
                now,
            );
            self.apply_lifo(existing, user);
            return Outcome::Queued;
        }
        match t.state {
            TState::Pending => {
                let adopted = !user.is_empty() && t.requested_by.is_empty() && t.source == Source::User;
                let promoted = t.source != Source::User;
                self.update_task(existing, |t| {
                    t.budget = t.budget.max(floor);
                    t.background = merged_background(t.background, background, promoted);
                    t.source = Source::User;
                    if !user.is_empty() && t.requested_by.is_empty() {
                        t.requested_by = user.to_string();
                    }
                    if !route.is_empty() && t.route.is_empty() {
                        t.route = route.to_string();
                    }
                    if promoted || adopted {
                        t.requested_at = Some(now);
                    }
                });
                self.add_requester(existing, user, now, env);
                if promoted {
                    return Outcome::Queued;
                }
                Outcome::AlreadyQueued { adopted }
            }
            TState::Leased => {
                if t.budget < floor {
                    let follow = self
                        .idx
                        .by_pos
                        .get(&pos)
                        .into_iter()
                        .flatten()
                        .map(|id| &self.tasks[id])
                        .filter(|f| f.state == TState::Pending && f.generation > t.generation)
                        .min_by_key(|f| (f.generation, f.id))
                        .map(|f| f.id);
                    match follow {
                        None => {
                            let mut g = (generation + 1).max(t.generation + 1);
                            while self.slot(pos, g).is_some() {
                                g += 1;
                            }
                            let id = self.new_task_on_slot(pos, g, floor, now);
                            self.update_task(id, |n| {
                                n.source = Source::User;
                                n.requested_by = user.to_string();
                                n.route = route.to_string();
                                n.requested_at = Some(now);
                                n.background = background;
                                n.multipv = multipv_for(g, Some(floor), false, clamp);
                            });
                        }
                        Some(fid) => {
                            self.update_task(fid, |f| {
                                f.budget = f.budget.max(floor);
                                let fresh = f.source != Source::User;
                                f.background = merged_background(f.background, background, fresh);
                                if fresh || (!user.is_empty() && f.requested_by.is_empty()) {
                                    f.requested_at = Some(now);
                                }
                                f.source = Source::User;
                                if !user.is_empty() && f.requested_by.is_empty() {
                                    f.requested_by = user.to_string();
                                }
                                if !route.is_empty() && f.route.is_empty() {
                                    f.route = route.to_string();
                                }
                            });
                        }
                    }
                    return Outcome::Queued;
                }
                let fresh = t.source != Source::User;
                let bg = merged_background(t.background, background, fresh);
                if bg != t.background || fresh {
                    self.update_task(existing, |t| {
                        t.background = bg;
                        if fresh {
                            t.source = Source::User;
                            t.requested_at = Some(now);
                        }
                    });
                }
                self.add_requester(existing, user, now, env);
                Outcome::AlreadyQueued { adopted: false }
            }
            // Q-6.12: delivered, not applied yet.
            _ => Outcome::AlreadyQueued { adopted: false },
        }
    }

    /// `request_analysis` (Q-6.22) without the breadth swap (off in
    /// production, `ATOMICDB_BREADTH_SWAP`) and with `request_subtree`
    /// reported as `Outcome::Subtree`.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn request_analysis(
        &mut self,
        pos: u64,
        user: &str,
        route: &str,
        budget_floor: Option<u64>,
        only_if_missing: bool,
        one_search: bool,
        only_unexplored: bool,
        background: bool,
        now: Ms,
        env: &dyn QueueEnv,
    ) -> Outcome {
        if budget_floor == Some(SUBTREE_REQUEST_NODES) {
            return Outcome::Subtree;
        }
        let o = self.request_rung(pos, user, route, budget_floor, only_if_missing, only_unexplored, background, now, env);
        if o != Outcome::LadderExhausted {
            return o;
        }
        if one_search { Outcome::LadderSpent } else { Outcome::Subtree }
    }

    // ------------------------------------------------------------ the door (Q-6.24..Q-6.27, Q-5.10)

    /// Q-5.10 `_account_queue_full`.
    pub fn account_queue_full(&self, account: &str, env: &dyn QueueEnv) -> bool {
        let n = self
            .idx
            .user_by_author
            .get(account)
            .into_iter()
            .flatten()
            .filter(|id| {
                let t = &self.tasks[id];
                t.state == TState::Pending && env.status(t.pos) == Status::Unknown
            })
            .count();
        n >= REQUEST_QUEUE_MAX
    }

    /// Q-5.11: the receipt, at most one per account and minute.
    fn lane_cap_receipt(&mut self, account: &str, now: Ms) {
        let key = format!("lane_cap:{account}");
        if self.receipts.get(&key).map(|&at| at >= now - LANE_CAP_RECEIPT_WINDOW).unwrap_or(false) {
            return;
        }
        self.receipts.insert(key.clone(), now);
        self.tx.receipts.insert(key);
        let shown = if account.is_empty() { "(anonymous)" } else { account };
        self.event("LANE_CAP_HIT", json!({"account": shown, "waiting": REQUEST_QUEUE_MAX}));
    }

    /// One hour of `RequestLog(ip, position)` (Q-6.25), kept in receipts.
    fn recent_request(&self, ip: &str, pos: u64, now: Ms) -> bool {
        self.receipts.get(&format!("req:{ip}:{pos}")).map(|&at| at >= now - HOUR).unwrap_or(false)
    }

    fn log_request(&mut self, ip: &str, pos: u64, now: Ms) {
        let key = format!("req:{ip}:{pos}");
        self.receipts.insert(key.clone(), now);
        self.tx.receipts.insert(key);
    }

    fn live_user_task_on(&self, pos: u64) -> bool {
        self.live_tasks_of(pos).iter().any(|t| t.source == Source::User)
    }

    /// `views._queue_request` + the `then_verify` stamp of Q-6.27.
    pub(crate) fn queue_request(&mut self, r: &RequestReq, env: &dyn QueueEnv) -> RequestOut {
        let now = r.now;
        let mut detail = Map::new();
        let p = env.pos(r.pos);
        // Q-6.24
        if r.only_if_missing && p.status == Status::Unknown {
            let want = r.budget_floor.unwrap_or(REQUEST_BUDGET_LADDER[0]);
            if let Some(s) = self.searched_budget(r.pos).filter(|&s| s >= want) {
                detail.insert("completed_budget".into(), json!(s));
                self.task_fields(r, &mut detail);
                return RequestOut { status: "already-analyzed".into(), detail };
            }
        }
        // Q-6.25
        let recent = self.recent_request(&r.ip, r.pos, now);
        if recent && r.budget_floor.is_none() {
            let mut active = self.live_user_task_on(r.pos);
            if !active && ladder_exhausted(p.status == Status::Unknown, self.completed_max_budget(r.pos)) {
                active = env.children(r.pos).iter().any(|(_, c)| self.live_user_task_on(*c));
            }
            if active {
                if !r.background {
                    let ids: Vec<u64> =
                        self.live_tasks_of(r.pos).iter().filter(|t| t.source == Source::User && t.background).map(|t| t.id).collect();
                    for id in ids {
                        self.update_task(id, |t| t.background = false);
                    }
                }
                return self.with_then_verify(r, "already-requested", detail);
            }
        }
        // Q-5.10 / Q-5.11
        if self.account_queue_full(&r.account, env) {
            self.lane_cap_receipt(&r.account, now);
            detail.insert("retry_after".into(), json!(QUEUE_FULL_RETRY_AFTER_SECONDS));
            self.task_fields(r, &mut detail);
            return RequestOut { status: "queue-full-account".into(), detail };
        }
        let o = self.request_analysis(
            r.pos,
            &r.account,
            &r.route,
            r.budget_floor,
            r.only_if_missing,
            r.one_search,
            r.only_unexplored,
            r.background,
            now,
            env,
        );
        match &o {
            Outcome::AlreadyQueued { adopted: true } => {
                detail.insert("adopted".into(), json!(true));
            }
            Outcome::AlreadyAnalyzed { completed_budget } => {
                detail.insert("completed_budget".into(), json!(completed_budget));
            }
            _ => {}
        }
        if matches!(o, Outcome::Queued | Outcome::AlreadyQueued { .. } | Outcome::Subtree) {
            self.log_request(&r.ip, r.pos, now);
        }
        // `ahead` (Q-5.8, `with_place`) is display only and v1 computes it
        // after the commit: the HTTP layer asks `user_queue_ahead` on a read
        // copy of the queue, so a deep queue never costs the writer.
        self.with_then_verify(r, o.status(), detail)
    }

    fn with_then_verify(&mut self, r: &RequestReq, status: &str, mut detail: Map<String, Value>) -> RequestOut {
        if r.then_verify && matches!(status, "queued" | "already-queued" | "already-requested") {
            let ids: Vec<u64> = self.live_tasks_of(r.pos).iter().filter(|t| t.source == Source::User).map(|t| t.id).collect();
            let mut any = false;
            for id in ids {
                self.update_task(id, |t| t.then_verify = true);
                any = true;
            }
            if any {
                detail.insert("then_verify".into(), json!(true));
            }
        }
        self.task_fields(r, &mut detail);
        RequestOut { status: status.to_string(), detail }
    }

    /// The task fields of the API answer (api-contract 4.1): the live task of
    /// the position (lowest id), its budget, `waiting`/`searching`,
    /// `low`/`normal` and whether the caller backs it; all null without one.
    fn task_fields(&self, r: &RequestReq, detail: &mut Map<String, Value>) {
        let live = self.live_tasks_of(r.pos).first().map(|t| (*t).clone());
        match live {
            Some(t) => {
                detail.insert("task".into(), json!(t.id));
                detail.insert("budget_nodes".into(), json!(t.budget));
                detail.insert("task_state".into(), json!(if t.state == TState::Leased { "searching" } else { "waiting" }));
                detail.insert("priority".into(), json!(if t.background { "low" } else { "normal" }));
                detail.insert("backed".into(), json!(!r.account.is_empty() && t.also.iter().any(|a| *a == r.account)));
            }
            None => {
                for k in ["task", "budget_nodes", "task_state", "priority"] {
                    detail.insert(k.into(), Value::Null);
                }
                detail.insert("backed".into(), json!(false));
            }
        }
    }

    // ------------------------------------------------------------ place in the queue (Q-5.8)

    /// `queue_ahead_pair(task)`: PENDING USER rows (on open positions) served
    /// before `id`, and how many of them are of the same account.  `None`
    /// when the task is not a PENDING USER row.
    pub fn queue_ahead(&self, id: u64, now: Ms, env: &dyn QueueEnv) -> Option<(usize, usize)> {
        let t = self.tasks.get(&id)?;
        if t.state != TState::Pending || t.source != Source::User || env.status(t.pos) != Status::Unknown {
            return None;
        }
        let ctx = crate::lease::ServeCtx {
            user: String::new(),
            serve_order: ServeOrder::Queue,
            serve_first: Vec::new(),
            personal: false,
            personal_for: Vec::new(),
            campaign: None,
            house: false,
        };
        // With this neutral worker every USER row comes before AUTO/FILL.
        let mut order = self.merge(&ctx, now, env);
        let mine = t.effective_account();
        let mut ahead = 0;
        let mut same = 0;
        while let Some(rid) = order.pop() {
            if rid == id {
                return Some((ahead, same));
            }
            let r = &self.tasks[&rid];
            if r.source != Source::User {
                return None;
            }
            ahead += 1;
            if r.effective_account() == mine {
                same += 1;
            }
        }
        None
    }

    /// `_user_queue_ahead(pos)`: the place of the lowest-id PENDING USER task.
    pub fn user_queue_ahead(&self, pos: u64, now: Ms, env: &dyn QueueEnv) -> Option<usize> {
        let id = self.live_tasks_of(pos).iter().filter(|t| t.state == TState::Pending && t.source == Source::User).map(|t| t.id).min()?;
        self.queue_ahead(id, now, env).map(|(a, _)| a)
    }

    // ------------------------------------------------------------ bulk children (Q-8.18..Q-8.20)

    /// `enqueue_unexplored_children(pos, cap, source, requested_by, route,
    /// children)`; `children` defaults to `unexplored_children(pos)`.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn enqueue_unexplored_children(
        &mut self,
        pos: u64,
        cap: usize,
        source: Source,
        user: &str,
        route: &str,
        children: Option<Vec<u64>>,
        now: Ms,
        env: &dyn QueueEnv,
    ) -> usize {
        let edges = env.children(pos);
        let kids: Vec<u64> = children.unwrap_or_else(|| unexplored_children(env, pos).into_iter().map(|(_, c)| c).collect());
        // With two edges to one child the last one read wins (v1 dict).
        let mut move_of: BTreeMap<u64, String> = BTreeMap::new();
        for (m, c) in &edges {
            move_of.insert(*c, m.clone());
        }
        let mut queued = 0;
        for child in kids {
            if queued >= cap {
                break;
            }
            let cp = env.pos(child);
            let clamp = short_mate_clamp(&cp);
            let mut budget = if clamp.is_none() { REQUEST_BUDGET_LADDER[0].max(budget_for(&cp)) } else { budget_for(&cp) };
            if source != Source::User {
                budget = server_picked_budget(budget, clamp, Some(&cp), self.config.interior_deep_rungs);
            }
            let multipv = multipv_for(cp.visits, Some(budget), false, clamp);
            let child_route = match move_of.get(&child) {
                Some(m) if !route.is_empty() => format!("{route},{m}"),
                _ => String::new(),
            };
            let live = self.live_tasks_of(child).first().map(|t| t.id);
            if let Some(vivo) = live {
                let backed = self.add_requester(vivo, user, now, env);
                let v = &self.tasks[&vivo];
                if v.state == TState::Pending && v.source != source {
                    let cr = child_route.clone();
                    self.update_task(vivo, |v| {
                        v.source = source;
                        if !cr.is_empty() && v.route.is_empty() {
                            v.route = cr;
                        }
                        if source == Source::User {
                            v.requested_at = Some(now);
                        }
                    });
                    queued += 1;
                } else if backed {
                    queued += 1;
                }
                continue;
            }
            match self.slot(child, cp.visits).map(|t| (t.id, t.state)) {
                None => {
                    let id = self.new_task_on_slot(child, cp.visits, budget, now);
                    self.update_task(id, |t| {
                        t.multipv = multipv;
                        t.source = source;
                        t.requested_by = user.to_string();
                        t.route = child_route.clone();
                        t.requested_at = if source == Source::User { Some(now) } else { None };
                    });
                    queued += 1;
                }
                Some((id, TState::Cancelled)) => {
                    self.revive_withdrawn(id, budget, multipv, user, &child_route, source, false, now);
                    queued += 1;
                }
                // An absorbed or delivered row on the slot: skipped (OQ-38, as in v1).
                Some(_) => {}
            }
        }
        queued
    }
}
