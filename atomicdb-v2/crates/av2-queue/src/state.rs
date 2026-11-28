//! The queue state of the single writer, its indexes, and its deltas.
//!
//! # Model
//!
//! `QueueState` holds every row the queue rules read: the analysis tasks that
//! can still matter (open, parked on the current visit, cancelled, or served
//! within the last hour), the proof tasks, worker pings, serving preferences,
//! tombstones, the arm hourly logs, receipt windows and the configuration.
//! Indexes are derived from the rows and rebuilt on load.
//!
//! # Deltas: how leases survive a restart
//!
//! Every input is applied by `QueueState::apply` (in `apply.rs`) to the
//! state, and every row the input changed is recorded.  When the input
//! finishes, `finish` turns those rows into [`Delta`]s: the full new value of
//! each changed row (or its removal).  The writer logs the input and its
//! deltas in the same frame as the rest of the batch (`fact::record`), so:
//!
//! - replaying the deltas of the log onto a snapshot gives back the exact
//!   state, without the graph and without the rules (`apply_deltas`);
//! - a LEASED row is in the log the moment its lease is acknowledged, with
//!   its token and heartbeat; after a restart the same worker heartbeats and
//!   submits against the same row, so a running 2B search is not lost.
//!
//! The deltas are idempotent (upserts of whole rows), so replaying a frame
//! twice is harmless, as for the store's redo records.

use crate::config::QueueConfig;
use crate::consts::*;
use crate::model::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

/// Bought and searched rungs of the rows folded out of memory (Q-6.2):
/// `bought` = max `budget_nodes` over COMPLETED rows (absorbed included),
/// `searched` = max over COMPLETED rows with `nodes_searched > 0`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ladder {
    #[serde(default)]
    pub bought: Option<u64>,
    #[serde(default)]
    pub searched: Option<u64>,
}

impl Ladder {
    fn fold(&mut self, t: &Task) {
        if t.state == TState::Completed {
            self.bought = Some(self.bought.map_or(t.budget, |b| b.max(t.budget)));
            if t.nodes_searched > 0 {
                self.searched = Some(self.searched.map_or(t.budget, |b| b.max(t.budget)));
            }
        }
    }
}

/// A change to the state, as logged.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "d", rename_all = "snake_case")]
pub enum Delta {
    Task {
        task: Box<Task>,
    },
    DropTask {
        id: u64,
    },
    Solve {
        task: Box<SolveTask>,
    },
    Ping {
        machine: String,
        user: String,
        ping: Ping,
    },
    Pref {
        account: String,
        pref: Pref,
    },
    Serving {
        account: String,
        machine: String,
        serving: Serving,
    },
    Frozen {
        pos: u64,
        ladder: Ladder,
    },
    Dead {
        pos: u64,
        dead: bool,
        #[serde(default)]
        revived: bool,
    },
    ArmLog {
        arm: String,
        created: Vec<Ms>,
    },
    Receipt {
        key: String,
        at: Ms,
    },
    Counters {
        next_task: u64,
        next_solve: u64,
        sweep_leases: u64,
    },
    SweepWaiting {
        waiting: Option<(bool, Ms)>,
    },
    Config {
        config: Box<QueueConfig>,
    },
}

/// A log entry for the projection (v1 `DBEvent`), produced by an input.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub kind: String,
    pub payload: serde_json::Value,
}

#[derive(Default, Debug, Clone)]
pub(crate) struct Indexes {
    /// PENDING or LEASED (`CHARGED`).
    pub charged: BTreeSet<u64>,
    pub pending: BTreeSet<u64>,
    pub leased: BTreeSet<u64>,
    /// PENDING rows that once had a lease (Q-2.8 `_absorb_requeued` scope).
    pub requeued: BTreeSet<u64>,
    /// (position, generation) -> task (Q-1.3).
    pub slots: HashMap<(u64, i32), u64>,
    /// Every retained task of a position.
    pub by_pos: HashMap<u64, BTreeSet<u64>>,
    /// (leased_at, id) of LEASED or COMPLETED rows with a lease (Q-2.19).
    pub served: BTreeSet<(Ms, u64)>,
    /// PENDING rows by arm.
    pub pending_arm: BTreeMap<String, BTreeSet<u64>>,
    /// USER CHARGED rows by author (Q-5.4, Q-5.10).
    pub user_by_author: BTreeMap<String, BTreeSet<u64>>,
    /// USER CHARGED rows by effective account, in fair-share window order
    /// (Q-5.6): (running_first, background, queue_seq, id).
    pub user_by_eff: BTreeMap<String, BTreeSet<(u8, bool, i64, u64)>>,
    /// Partitions with rows whose keys are not a function of the partition
    /// alone (backed rows, lane rows, anonymous): count of such rows.
    pub user_irr_rows: BTreeMap<String, BTreeSet<u64>>,
    /// PENDING USER rows per effective account.
    pub user_pending_by_eff: BTreeMap<String, usize>,
    /// Non-USER CHARGED rows.
    pub other_charged: BTreeSet<u64>,
    pub solve_pending: BTreeSet<u64>,
    pub solve_leased: BTreeSet<u64>,
    pub solve_by_pos: HashMap<u64, BTreeSet<u64>>,
}

#[derive(Default, Debug, Clone)]
pub(crate) struct TxLog {
    pub tasks: BTreeSet<u64>,
    pub solves: BTreeSet<u64>,
    pub pings: BTreeSet<(String, String)>,
    pub prefs: BTreeSet<String>,
    pub serving: BTreeSet<(String, String)>,
    pub frozen: BTreeSet<u64>,
    pub dead: BTreeSet<u64>,
    pub arms: BTreeSet<String>,
    pub receipts: BTreeSet<String>,
    pub counters: bool,
    pub sweep: bool,
    pub config: bool,
    pub events: Vec<Event>,
}

/// The queue: see the module documentation.
#[derive(Default, Debug, Clone)]
pub struct QueueState {
    pub(crate) config: QueueConfig,
    pub(crate) tasks: BTreeMap<u64, Task>,
    pub(crate) solves: BTreeMap<u64, SolveTask>,
    pub(crate) pings: BTreeMap<(String, String), Ping>,
    pub(crate) prefs: BTreeMap<String, Pref>,
    pub(crate) serving: BTreeMap<(String, String), Serving>,
    pub(crate) frozen: HashMap<u64, Ladder>,
    pub(crate) dead: BTreeSet<u64>,
    /// Tombstones revived since import: v1 wrote `priority = 0.0` (PRI-1.7).
    pub(crate) revived: BTreeSet<u64>,
    pub(crate) arm_log: BTreeMap<String, VecDeque<Ms>>,
    pub(crate) receipts: BTreeMap<String, Ms>,
    pub(crate) next_task: u64,
    pub(crate) next_solve: u64,
    pub(crate) sweep_leases: u64,
    pub(crate) sweep_waiting: Option<(bool, Ms)>,
    pub(crate) idx: Indexes,
    pub(crate) tx: TxLog,
}

/// Serialised form of the whole state (snapshot next to a checkpoint).
#[derive(Serialize, Deserialize)]
struct Snapshot {
    format: u32,
    config: QueueConfig,
    tasks: Vec<Task>,
    solves: Vec<SolveTask>,
    pings: Vec<(String, String, Ping)>,
    prefs: Vec<(String, Pref)>,
    serving: Vec<(String, String, Serving)>,
    frozen: Vec<(u64, Ladder)>,
    dead: Vec<u64>,
    #[serde(default)]
    revived: Vec<u64>,
    arm_log: Vec<(String, Vec<Ms>)>,
    receipts: Vec<(String, Ms)>,
    next_task: u64,
    next_solve: u64,
    sweep_leases: u64,
    sweep_waiting: Option<(bool, Ms)>,
}

const SNAPSHOT_FORMAT: u32 = 1;

impl QueueState {
    pub fn new(config: QueueConfig) -> QueueState {
        QueueState { config, next_task: 1, next_solve: 1, ..Default::default() }
    }

    pub fn config(&self) -> &QueueConfig {
        &self.config
    }
    pub fn task(&self, id: u64) -> Option<&Task> {
        self.tasks.get(&id)
    }
    pub fn solve(&self, id: u64) -> Option<&SolveTask> {
        self.solves.get(&id)
    }
    pub fn tasks(&self) -> impl Iterator<Item = &Task> {
        self.tasks.values()
    }
    pub fn solves(&self) -> impl Iterator<Item = &SolveTask> {
        self.solves.values()
    }
    pub fn ping(&self, machine: &str, user: &str) -> Option<&Ping> {
        self.pings.get(&(machine.to_string(), user.to_string()))
    }
    pub fn pref(&self, account: &str) -> Pref {
        self.prefs.get(account).cloned().unwrap_or_default()
    }
    pub fn is_dead(&self, pos: u64) -> bool {
        self.dead.contains(&pos)
    }
    /// Highest task id handed out so far (`_task_counter`, PRI-4.4; 0 if none).
    pub fn max_task_id(&self) -> u64 {
        self.next_task.saturating_sub(1)
    }
    pub fn pending_count(&self) -> usize {
        self.idx.pending.len()
    }
    pub fn leased_count(&self) -> usize {
        self.idx.leased.len()
    }
    pub fn retained_count(&self) -> usize {
        self.tasks.len()
    }
    /// Live (PENDING or LEASED) tasks of a position, lowest id first.
    pub fn live_tasks_of(&self, pos: u64) -> Vec<&Task> {
        self.idx.by_pos.get(&pos).into_iter().flatten().filter_map(|id| self.tasks.get(id)).filter(|t| t.state.charged()).collect()
    }
    /// The task on a slot, whatever its state (Q-1.3).
    pub fn slot(&self, pos: u64, generation: i32) -> Option<&Task> {
        self.idx.slots.get(&(pos, generation)).and_then(|id| self.tasks.get(id))
    }

    // ------------------------------------------------------------ ladder (Q-6.2)

    /// `_completed_max_budget(pos)`: the rung bought (absorbed rows included).
    pub fn completed_max_budget(&self, pos: u64) -> Option<u64> {
        let mut l = self.frozen.get(&pos).copied().unwrap_or_default();
        for id in self.idx.by_pos.get(&pos).into_iter().flatten() {
            l.fold(&self.tasks[id]);
        }
        l.bought
    }

    /// `searched_budget(pos)`: the deepest search that ran.
    pub fn searched_budget(&self, pos: u64) -> Option<u64> {
        let mut l = self.frozen.get(&pos).copied().unwrap_or_default();
        for id in self.idx.by_pos.get(&pos).into_iter().flatten() {
            l.fold(&self.tasks[id]);
        }
        l.searched
    }

    // ------------------------------------------------------------ mutation helpers

    fn unindex(&mut self, t: &Task) {
        let idx = &mut self.idx;
        idx.charged.remove(&t.id);
        idx.pending.remove(&t.id);
        idx.leased.remove(&t.id);
        idx.requeued.remove(&t.id);
        if idx.slots.get(&(t.pos, t.generation)) == Some(&t.id) {
            idx.slots.remove(&(t.pos, t.generation));
        }
        if let Some(s) = idx.by_pos.get_mut(&t.pos) {
            s.remove(&t.id);
            if s.is_empty() {
                idx.by_pos.remove(&t.pos);
            }
        }
        if let Some(at) = t.leased_at {
            idx.served.remove(&(at, t.id));
        }
        if let Some(s) = idx.pending_arm.get_mut(&t.arm) {
            s.remove(&t.id);
        }
        if let Some(s) = idx.user_by_author.get_mut(&t.requested_by) {
            s.remove(&t.id);
            if s.is_empty() {
                idx.user_by_author.remove(&t.requested_by);
            }
        }
        let eff = t.effective_account().to_string();
        if let Some(s) = idx.user_by_eff.get_mut(&eff) {
            if s.remove(&window_key(t)) {
                if irregular(t) {
                    let r = idx.user_irr_rows.get_mut(&eff).unwrap();
                    r.remove(&t.id);
                    if r.is_empty() {
                        idx.user_irr_rows.remove(&eff);
                    }
                }
                if t.state == TState::Pending {
                    let n = idx.user_pending_by_eff.get_mut(&eff).unwrap();
                    *n -= 1;
                    if *n == 0 {
                        idx.user_pending_by_eff.remove(&eff);
                    }
                }
            }
            if s.is_empty() {
                idx.user_by_eff.remove(&eff);
            }
        }
        idx.other_charged.remove(&t.id);
    }

    fn index(&mut self, t: &Task) {
        let idx = &mut self.idx;
        idx.slots.insert((t.pos, t.generation), t.id);
        idx.by_pos.entry(t.pos).or_default().insert(t.id);
        match t.state {
            TState::Pending => {
                idx.charged.insert(t.id);
                idx.pending.insert(t.id);
                if t.leased_at.is_some() && t.machine.is_empty() && t.heartbeat_at.is_none() {
                    idx.requeued.insert(t.id);
                }
                idx.pending_arm.entry(t.arm.clone()).or_default().insert(t.id);
            }
            TState::Leased => {
                idx.charged.insert(t.id);
                idx.leased.insert(t.id);
            }
            _ => {}
        }
        if matches!(t.state, TState::Leased | TState::Completed)
            && let Some(at) = t.leased_at
        {
            idx.served.insert((at, t.id));
        }
        if t.state.charged() {
            if t.source == Source::User {
                idx.user_by_author.entry(t.requested_by.clone()).or_default().insert(t.id);
                idx.user_by_eff.entry(t.effective_account().to_string()).or_default().insert(window_key(t));
                if irregular(t) {
                    idx.user_irr_rows.entry(t.effective_account().to_string()).or_default().insert(t.id);
                }
                if t.state == TState::Pending {
                    *idx.user_pending_by_eff.entry(t.effective_account().to_string()).or_default() += 1;
                }
            } else {
                idx.other_charged.insert(t.id);
            }
        }
    }

    /// Insert a new row (the caller checked the slot is free).
    pub(crate) fn insert_task(&mut self, t: Task) {
        debug_assert!(!self.tasks.contains_key(&t.id));
        debug_assert!(!self.idx.slots.contains_key(&(t.pos, t.generation)), "slot taken");
        self.index(&t);
        self.tx.tasks.insert(t.id);
        self.tasks.insert(t.id, t);
    }

    /// Allocate the next task id.
    pub(crate) fn alloc_task_id(&mut self) -> u64 {
        let id = self.next_task.max(1);
        self.next_task = id + 1;
        self.tx.counters = true;
        id
    }

    pub(crate) fn alloc_solve_id(&mut self) -> u64 {
        let id = self.next_solve.max(1);
        self.next_solve = id + 1;
        self.tx.counters = true;
        id
    }

    /// Change a row in place; indexes and the delta log follow.
    pub(crate) fn update_task<R>(&mut self, id: u64, f: impl FnOnce(&mut Task) -> R) -> Option<R> {
        let mut t = self.tasks.remove(&id)?;
        self.unindex(&t);
        let r = f(&mut t);
        self.index(&t);
        self.tasks.insert(id, t);
        self.tx.tasks.insert(id);
        Some(r)
    }

    pub(crate) fn drop_task(&mut self, id: u64) {
        if let Some(t) = self.tasks.remove(&id) {
            self.unindex(&t);
            self.tx.tasks.insert(id);
        }
    }

    fn unindex_solve(&mut self, s: &SolveTask) {
        self.idx.solve_pending.remove(&s.id);
        self.idx.solve_leased.remove(&s.id);
        if let Some(v) = self.idx.solve_by_pos.get_mut(&s.pos) {
            v.remove(&s.id);
        }
    }
    fn index_solve(&mut self, s: &SolveTask) {
        match s.state {
            SolveState::Pending => {
                self.idx.solve_pending.insert(s.id);
            }
            SolveState::Leased => {
                self.idx.solve_leased.insert(s.id);
            }
            _ => {}
        }
        self.idx.solve_by_pos.entry(s.pos).or_default().insert(s.id);
    }

    pub(crate) fn insert_solve(&mut self, s: SolveTask) {
        self.index_solve(&s);
        self.tx.solves.insert(s.id);
        self.solves.insert(s.id, s);
    }

    pub(crate) fn update_solve<R>(&mut self, id: u64, f: impl FnOnce(&mut SolveTask) -> R) -> Option<R> {
        let mut s = self.solves.remove(&id)?;
        self.unindex_solve(&s);
        let r = f(&mut s);
        self.index_solve(&s);
        self.solves.insert(id, s);
        self.tx.solves.insert(id);
        Some(r)
    }

    pub(crate) fn ping_mut(&mut self, machine: &str, user: &str) -> &mut Ping {
        let key = (machine.to_string(), user.to_string());
        self.tx.pings.insert(key.clone());
        self.pings.entry(key).or_default()
    }

    pub(crate) fn ping_mut_existing(&mut self, machine: &str, user: &str) -> Option<&mut Ping> {
        let key = (machine.to_string(), user.to_string());
        if self.pings.contains_key(&key) {
            self.tx.pings.insert(key.clone());
        }
        self.pings.get_mut(&key)
    }

    /// PRI-1.7 revival: the tombstone goes and v1's stored priority becomes 0.0.
    pub(crate) fn revive(&mut self, pos: u64) {
        let was = self.dead.remove(&pos);
        let new = self.revived.insert(pos);
        if was || new {
            self.tx.dead.insert(pos);
        }
    }

    /// `Position.priority` as the lease reads it for AUTO/FILL rows (Q-2.16
    /// key 8).  Exact port (default): what v1 has stored today, i.e. `DEAD`
    /// for a tombstone, 0.0 for a revived one, else the value the last pass
    /// left (production has run `--no-priorities` since 29-Aug), which the
    /// environment provides.  With `computed_priority` on (a later switch):
    /// the priority computed in memory (D5).
    pub fn queue_rank(&self, pos: u64, env: &dyn crate::env::QueueEnv) -> f64 {
        if self.dead.contains(&pos) {
            return DEAD;
        }
        if self.config.computed_priority {
            return env.computed_priority(pos).unwrap_or(0.0);
        }
        if self.revived.contains(&pos) {
            return 0.0;
        }
        env.queue_rank(pos)
    }

    pub fn is_revived(&self, pos: u64) -> bool {
        self.revived.contains(&pos)
    }

    pub(crate) fn set_dead(&mut self, pos: u64, dead: bool) {
        let changed = if dead { self.dead.insert(pos) } else { self.dead.remove(&pos) };
        if changed {
            self.tx.dead.insert(pos);
        }
    }

    pub(crate) fn note_arm_created(&mut self, arm: &str, at: Ms) {
        self.arm_log.entry(arm.to_string()).or_default().push_back(at);
        self.tx.arms.insert(arm.to_string());
    }

    pub(crate) fn event(&mut self, kind: &str, payload: serde_json::Value) {
        self.tx.events.push(Event { kind: kind.to_string(), payload });
    }

    /// Absorb (Q-1.11) the PENDING rows among `ids`; returns how many.
    pub(crate) fn absorb(&mut self, ids: &[u64], now: Ms) -> usize {
        let mut n = 0;
        for &id in ids {
            if self.tasks.get(&id).map(|t| t.state == TState::Pending).unwrap_or(false) {
                self.update_task(id, |t| t.absorb(now));
                n += 1;
            }
        }
        n
    }

    // ------------------------------------------------------------ deltas

    /// Close the current input: the deltas of every row it changed, in a
    /// fixed order, and the events it produced.
    pub(crate) fn finish(&mut self) -> (Vec<Delta>, Vec<Event>) {
        let tx = std::mem::take(&mut self.tx);
        let mut out = Vec::new();
        if tx.config {
            out.push(Delta::Config { config: Box::new(self.config.clone()) });
        }
        for id in tx.tasks {
            match self.tasks.get(&id) {
                Some(t) => out.push(Delta::Task { task: Box::new(t.clone()) }),
                None => out.push(Delta::DropTask { id }),
            }
        }
        for id in tx.solves {
            if let Some(s) = self.solves.get(&id) {
                out.push(Delta::Solve { task: Box::new(s.clone()) });
            }
        }
        for (m, u) in tx.pings {
            if let Some(p) = self.pings.get(&(m.clone(), u.clone())) {
                out.push(Delta::Ping { machine: m, user: u, ping: p.clone() });
            }
        }
        for a in tx.prefs {
            out.push(Delta::Pref { pref: self.prefs.get(&a).cloned().unwrap_or_default(), account: a });
        }
        for (a, m) in tx.serving {
            out.push(Delta::Serving {
                serving: self.serving.get(&(a.clone(), m.clone())).cloned().unwrap_or_default(),
                account: a,
                machine: m,
            });
        }
        for pos in tx.frozen {
            out.push(Delta::Frozen { pos, ladder: self.frozen.get(&pos).copied().unwrap_or_default() });
        }
        for pos in tx.dead {
            out.push(Delta::Dead { pos, dead: self.dead.contains(&pos), revived: self.revived.contains(&pos) });
        }
        for arm in tx.arms {
            let created = self.arm_log.get(&arm).map(|d| d.iter().copied().collect()).unwrap_or_default();
            out.push(Delta::ArmLog { arm, created });
        }
        for key in tx.receipts {
            if let Some(at) = self.receipts.get(&key) {
                out.push(Delta::Receipt { key, at: *at });
            }
        }
        if tx.sweep {
            out.push(Delta::SweepWaiting { waiting: self.sweep_waiting });
        }
        if tx.counters {
            out.push(Delta::Counters { next_task: self.next_task, next_solve: self.next_solve, sweep_leases: self.sweep_leases });
        }
        (out, tx.events)
    }

    /// Discard the changes recorded so far without producing deltas (used by
    /// the read-only queries that share code with the writer).
    pub(crate) fn forget_tx(&mut self) {
        self.tx = TxLog::default();
    }

    /// Replay deltas from the log (recovery, standby, tests).
    pub fn apply_deltas(&mut self, deltas: &[Delta]) {
        for d in deltas {
            match d {
                Delta::Task { task } => {
                    if let Some(old) = self.tasks.remove(&task.id) {
                        self.unindex(&old);
                    }
                    self.index(task);
                    self.tasks.insert(task.id, (**task).clone());
                }
                Delta::DropTask { id } => {
                    if let Some(old) = self.tasks.remove(id) {
                        self.unindex(&old);
                    }
                }
                Delta::Solve { task } => {
                    if let Some(old) = self.solves.remove(&task.id) {
                        self.unindex_solve(&old);
                    }
                    self.index_solve(task);
                    self.solves.insert(task.id, (**task).clone());
                }
                Delta::Ping { machine, user, ping } => {
                    self.pings.insert((machine.clone(), user.clone()), ping.clone());
                }
                Delta::Pref { account, pref } => {
                    self.prefs.insert(account.clone(), pref.clone());
                }
                Delta::Serving { account, machine, serving } => {
                    self.serving.insert((account.clone(), machine.clone()), serving.clone());
                }
                Delta::Frozen { pos, ladder } => {
                    if *ladder == Ladder::default() {
                        self.frozen.remove(pos);
                    } else {
                        self.frozen.insert(*pos, *ladder);
                    }
                }
                Delta::Dead { pos, dead, revived } => {
                    if *revived {
                        self.revived.insert(*pos);
                    } else {
                        self.revived.remove(pos);
                    }
                    if *dead {
                        self.dead.insert(*pos);
                    } else {
                        self.dead.remove(pos);
                    }
                }
                Delta::ArmLog { arm, created } => {
                    self.arm_log.insert(arm.clone(), created.iter().copied().collect());
                }
                Delta::Receipt { key, at } => {
                    self.receipts.insert(key.clone(), *at);
                }
                Delta::Counters { next_task, next_solve, sweep_leases } => {
                    self.next_task = *next_task;
                    self.next_solve = *next_solve;
                    self.sweep_leases = *sweep_leases;
                }
                Delta::SweepWaiting { waiting } => self.sweep_waiting = *waiting,
                Delta::Config { config } => self.config = (**config).clone(),
            }
        }
        self.tx = TxLog::default();
    }

    // ------------------------------------------------------------ snapshot

    pub fn snapshot(&self) -> Vec<u8> {
        let mut frozen: Vec<(u64, Ladder)> = self.frozen.iter().map(|(k, v)| (*k, *v)).collect();
        frozen.sort_by_key(|x| x.0);
        let snap = Snapshot {
            format: SNAPSHOT_FORMAT,
            config: self.config.clone(),
            tasks: self.tasks.values().cloned().collect(),
            solves: self.solves.values().cloned().collect(),
            pings: self.pings.iter().map(|((m, u), p)| (m.clone(), u.clone(), p.clone())).collect(),
            prefs: self.prefs.iter().map(|(a, p)| (a.clone(), p.clone())).collect(),
            serving: self.serving.iter().map(|((a, m), s)| (a.clone(), m.clone(), s.clone())).collect(),
            frozen,
            dead: self.dead.iter().copied().collect(),
            revived: self.revived.iter().copied().collect(),
            arm_log: self.arm_log.iter().map(|(a, d)| (a.clone(), d.iter().copied().collect())).collect(),
            receipts: self.receipts.iter().map(|(k, v)| (k.clone(), *v)).collect(),
            next_task: self.next_task,
            next_solve: self.next_solve,
            sweep_leases: self.sweep_leases,
            sweep_waiting: self.sweep_waiting,
        };
        serde_json::to_vec(&snap).expect("snapshot serialises")
    }

    pub fn restore(bytes: &[u8]) -> Result<QueueState, String> {
        let s: Snapshot = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        if s.format != SNAPSHOT_FORMAT {
            return Err(format!("queue snapshot format {} (expected {SNAPSHOT_FORMAT})", s.format));
        }
        let mut q = QueueState::new(s.config);
        for t in s.tasks {
            q.index(&t);
            q.tasks.insert(t.id, t);
        }
        for t in s.solves {
            q.index_solve(&t);
            q.solves.insert(t.id, t);
        }
        q.pings = s.pings.into_iter().map(|(m, u, p)| ((m, u), p)).collect();
        q.prefs = s.prefs.into_iter().collect();
        q.serving = s.serving.into_iter().map(|(a, m, v)| ((a, m), v)).collect();
        q.frozen = s.frozen.into_iter().collect();
        q.dead = s.dead.into_iter().collect();
        q.revived = s.revived.into_iter().collect();
        q.arm_log = s.arm_log.into_iter().map(|(a, d)| (a, d.into_iter().collect())).collect();
        q.receipts = s.receipts.into_iter().collect();
        q.next_task = s.next_task;
        q.next_solve = s.next_solve;
        q.sweep_leases = s.sweep_leases;
        q.sweep_waiting = s.sweep_waiting;
        Ok(q)
    }

    /// A canonical digest of the whole state (equal states, equal digests).
    pub fn digest(&self) -> String {
        use sha2::{Digest, Sha256};
        hex::encode(Sha256::digest(self.snapshot()))
    }

    // ------------------------------------------------------------ import

    /// Load a v1 row (migration).  Ids must be unique; the slot rule is not
    /// re-checked (v1 enforced it).
    pub fn import_task(&mut self, t: Task) {
        self.next_task = self.next_task.max(t.id + 1);
        if let Some(old) = self.tasks.remove(&t.id) {
            self.unindex(&old);
        }
        self.index(&t);
        self.tasks.insert(t.id, t);
    }
    pub fn import_solve(&mut self, s: SolveTask) {
        self.next_solve = self.next_solve.max(s.id + 1);
        self.index_solve(&s);
        self.solves.insert(s.id, s);
    }
    /// A position's ladder from rows that are not imported (folded).
    pub fn import_ladder(&mut self, pos: u64, ladder: Ladder) {
        self.frozen.insert(pos, ladder);
    }
    pub fn import_dead(&mut self, pos: u64) {
        self.dead.insert(pos);
    }
    pub fn import_pref(&mut self, account: &str, pref: Pref) {
        self.prefs.insert(account.to_string(), pref);
    }
    pub fn import_serving(&mut self, account: &str, machine_base: &str, s: Serving) {
        self.serving.insert((account.to_string(), machine_base.to_string()), s);
    }
    pub fn import_ping(&mut self, machine: &str, user: &str, p: Ping) {
        self.pings.insert((machine.to_string(), user.to_string()), p);
    }

    // ------------------------------------------------------------ retention

    /// Fold out of memory the rows no rule can read again: COMPLETED rows on
    /// a generation below the position's visits (never revived, Q-6.8: every
    /// revive path works on `generation = visits`), served more than an hour
    /// ago (Q-2.19).  CANCELLED rows are kept (undo works on any of them,
    /// Q-6.21).  `visits` returns the position's visit count.
    pub(crate) fn prune(&mut self, now: Ms, visits: &dyn Fn(u64) -> i32) -> usize {
        let cutoff = now - SERVED_WINDOW;
        let mut drop = Vec::new();
        for t in self.tasks.values() {
            if t.state != TState::Completed || t.generation >= visits(t.pos) {
                continue;
            }
            if t.completed.map(|c| c >= cutoff).unwrap_or(false) {
                continue;
            }
            drop.push(t.id);
        }
        for id in &drop {
            let t = self.tasks[id].clone();
            let l = self.frozen.entry(t.pos).or_default();
            l.fold(&t);
            self.tx.frozen.insert(t.pos);
            self.drop_task(*id);
        }
        // Arm logs only matter for an hour.
        let horizon = now - ARM_RATE_WINDOW;
        let arms: Vec<String> = self.arm_log.keys().cloned().collect();
        for arm in arms {
            let d = self.arm_log.get_mut(&arm).unwrap();
            let before = d.len();
            while d.front().map(|&c| c < horizon).unwrap_or(false) {
                d.pop_front();
            }
            if d.len() != before {
                self.tx.arms.insert(arm);
            }
        }
        drop.len()
    }

    // ------------------------------------------------------------ invariants

    /// Check every invariant the rules promise.  Used by the property tests;
    /// returns the first violation.
    pub fn check_invariants(&self) -> Result<(), String> {
        let mut slots: HashMap<(u64, i32), u64> = HashMap::new();
        for t in self.tasks.values() {
            if let Some(other) = slots.insert((t.pos, t.generation), t.id) {
                return Err(format!("slot ({}, {}) held by {} and {}", t.pos, t.generation, other, t.id));
            }
            if t.state == TState::Leased {
                if t.machine.is_empty() {
                    return Err(format!("task {} LEASED without machine", t.id));
                }
                if t.leased_at.is_none() || t.attempts == 0 {
                    return Err(format!("task {} LEASED without lease fields", t.id));
                }
            }
            if t.state == TState::Pending && !t.machine.is_empty() {
                return Err(format!("task {} PENDING with machine {}", t.id, t.machine));
            }
            if t.also.iter().any(|a| a.is_empty() || *a == t.requested_by) {
                return Err(format!("task {} backers {:?} vs author {:?}", t.id, t.also, t.requested_by));
            }
            let mut seen = BTreeSet::new();
            if !t.also.iter().all(|a| seen.insert(a)) {
                return Err(format!("task {} duplicate backer", t.id));
            }
            if t.id >= self.next_task {
                return Err(format!("task {} beyond counter {}", t.id, self.next_task));
            }
        }
        // Rebuilt indexes equal the maintained ones.
        let mut fresh = QueueState::new(self.config.clone());
        for t in self.tasks.values() {
            fresh.index(t);
        }
        for s in self.solves.values() {
            fresh.index_solve(s);
        }
        let a = &self.idx;
        let b = &fresh.idx;
        if a.charged != b.charged
            || a.pending != b.pending
            || a.leased != b.leased
            || a.requeued != b.requeued
            || a.served != b.served
            || a.user_by_eff != b.user_by_eff
            || a.user_irr_rows != b.user_irr_rows
            || a.user_pending_by_eff != b.user_pending_by_eff
            || a.user_by_author != b.user_by_author
            || a.other_charged != b.other_charged
            || a.solve_pending != b.solve_pending
            || a.solve_leased != b.solve_leased
            || a.slots != b.slots
        {
            return Err("indexes differ from a rebuild".into());
        }
        let pa: BTreeMap<_, _> = a.pending_arm.iter().filter(|(_, v)| !v.is_empty()).collect();
        let pb: BTreeMap<_, _> = b.pending_arm.iter().filter(|(_, v)| !v.is_empty()).collect();
        if pa != pb {
            return Err("pending-by-arm index differs from a rebuild".into());
        }
        // One live proof lease per machine identity is NOT a v1 invariant
        // (Q-2.55 only prevents a second one at acquire time), so not checked.
        Ok(())
    }
}

/// A USER row whose order keys depend on more than its partition.
fn irregular(t: &Task) -> bool {
    !t.also.is_empty() || t.requested_by.is_empty() || (!t.lane_account.is_empty() && t.lane_account != t.requested_by)
}

/// Fair-share window order inside a partition (Q-5.6).
pub(crate) fn window_key(t: &Task) -> (u8, bool, i64, u64) {
    (if t.state == TState::Leased { 0 } else { 1 }, t.background, t.queue_seq, t.id)
}
