//! The rows of the queue (v1 `atomicdb/models.py`): `AnalysisTask`,
//! `SolveTask`, `WorkerPing`, `ContributorPref`, `WorkerServing`.
//!
//! Field names follow v1 where they mean the same thing; differences:
//! - `pos` is the v2 position id (v1 `position_id` is a key string);
//! - times are `Ms` (milliseconds) instead of datetimes;
//! - `backers` is not stored: it is `also.len()` by construction (Q-1.6 says
//!   the v1 column must always equal the list length);
//! - `budget` is `budget_nodes`, `also` is `also_requested_by`,
//!   `heartbeat_at` is `lease_heartbeat_at`, `token`/`session` are
//!   `lease_token`/`lease_session`.

use crate::consts::Ms;
use serde::{Deserialize, Serialize};

/// Q-1.1.  Readers always filter positively (`state == Pending`, ...).
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TState {
    Pending,
    Leased,
    Completed,
    Cancelled,
}

impl TState {
    pub fn v1(self) -> &'static str {
        match self {
            TState::Pending => "PENDING",
            TState::Leased => "LEASED",
            TState::Completed => "COMPLETED",
            TState::Cancelled => "CANCELLED",
        }
    }
    pub fn from_v1(s: &str) -> Option<TState> {
        Some(match s {
            "PENDING" => TState::Pending,
            "LEASED" => TState::Leased,
            "COMPLETED" => TState::Completed,
            "CANCELLED" => TState::Cancelled,
            _ => return None,
        })
    }
    /// `live_request.CHARGED`.
    pub fn charged(self) -> bool {
        matches!(self, TState::Pending | TState::Leased)
    }
}

/// Q-1.2.  The lease serves by the explicit rank USER > FILL > AUTO.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Source {
    Auto,
    Fill,
    User,
}

impl Source {
    /// Service rank (`-source` in v1 sorts the strings descending).
    pub fn rank(self) -> u8 {
        match self {
            Source::Auto => 0,
            Source::Fill => 1,
            Source::User => 2,
        }
    }
    pub fn v1(self) -> &'static str {
        match self {
            Source::Auto => "AUTO",
            Source::Fill => "FILL",
            Source::User => "USER",
        }
    }
    pub fn from_v1(s: &str) -> Option<Source> {
        Some(match s {
            "AUTO" => Source::Auto,
            "FILL" => Source::Fill,
            "USER" => Source::User,
            _ => return None,
        })
    }
}

/// One purchase of one engine search on one position (Q-1.*).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Task {
    pub id: u64,
    pub pos: u64,
    pub generation: i32,
    pub state: TState,
    pub source: Source,
    #[serde(default)]
    pub arm: String,
    pub budget: u64,
    pub multipv: u32,
    #[serde(default)]
    pub route: String,
    #[serde(default)]
    pub then_verify: bool,
    #[serde(default)]
    pub requested_by: String,
    #[serde(default)]
    pub also: Vec<String>,
    #[serde(default)]
    pub lane_account: String,
    #[serde(default)]
    pub queue_seq: i64,
    #[serde(default)]
    pub background: bool,
    #[serde(default)]
    pub machine: String,
    #[serde(default)]
    pub leased_at: Option<Ms>,
    #[serde(default)]
    pub heartbeat_at: Option<Ms>,
    #[serde(default)]
    pub token: String,
    #[serde(default)]
    pub session: String,
    #[serde(default)]
    pub attempts: u32,
    #[serde(default)]
    pub nodes_searched: u64,
    #[serde(default)]
    pub elapsed: f64,
    #[serde(default)]
    pub delivered_by: String,
    #[serde(default)]
    pub engine_sha: String,
    #[serde(default)]
    pub net_sha: String,
    #[serde(default)]
    pub completed: Option<Ms>,
    pub created: Ms,
    #[serde(default)]
    pub requested_at: Option<Ms>,
}

impl Task {
    /// A fresh PENDING row with the v1 model defaults.
    pub fn new(id: u64, pos: u64, generation: i32, budget: u64, created: Ms) -> Task {
        Task {
            id,
            pos,
            generation,
            state: TState::Pending,
            source: Source::Auto,
            arm: String::new(),
            budget,
            multipv: 5,
            route: String::new(),
            then_verify: false,
            requested_by: String::new(),
            also: Vec::new(),
            lane_account: String::new(),
            queue_seq: 0,
            background: false,
            machine: String::new(),
            leased_at: None,
            heartbeat_at: None,
            token: String::new(),
            session: String::new(),
            attempts: 0,
            nodes_searched: 0,
            elapsed: 0.0,
            delivered_by: String::new(),
            engine_sha: String::new(),
            net_sha: String::new(),
            completed: None,
            created,
            requested_at: None,
        }
    }

    /// v1 `backers` (Q-1.6).
    pub fn backers(&self) -> usize {
        self.also.len()
    }

    /// Q-1.6 / Q-2.18: `lane_account` if set, else the author ('' = anonymous).
    pub fn effective_account(&self) -> &str {
        if self.lane_account.is_empty() { &self.requested_by } else { &self.lane_account }
    }

    /// Q-1.11 `is_absorbed`.
    pub fn is_absorbed(&self) -> bool {
        self.state == TState::Completed && self.nodes_searched == 0 && self.machine.is_empty()
    }

    /// Q-1.10 `request_time`.
    pub fn request_time(&self) -> Ms {
        self.requested_at.unwrap_or(self.created)
    }

    /// Q-2.7 / Q-2.51: the row shape of a requeued lease.
    pub(crate) fn requeue(&mut self) {
        debug_assert_eq!(self.state, TState::Leased);
        self.state = TState::Pending;
        self.machine.clear();
        self.heartbeat_at = None;
        self.token.clear();
        self.session.clear();
    }

    /// Q-1.11 / Q-3.14: absorb a PENDING row.
    pub(crate) fn absorb(&mut self, now: Ms) {
        debug_assert_eq!(self.state, TState::Pending);
        self.state = TState::Completed;
        self.completed = Some(now);
        self.nodes_searched = 0;
    }
}

/// `SolveTask.TState` (Q-1.17).
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SolveState {
    Pending,
    Leased,
    Completed,
    Failed,
}

/// A proof request (Q-1.17), leased by `/api/solve/acquire`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SolveTask {
    pub id: u64,
    pub pos: u64,
    /// `WHITE_WIN` / `BLACK_WIN`.
    pub goal: String,
    /// `F0`..`F4`.
    pub stage: String,
    pub budget: u64,
    #[serde(default)]
    pub arm: String,
    #[serde(default)]
    pub campaign: Option<u32>,
    pub state: SolveState,
    #[serde(default)]
    pub machine: String,
    #[serde(default)]
    pub leased_at: Option<Ms>,
    #[serde(default)]
    pub heartbeat_at: Option<Ms>,
    #[serde(default)]
    pub token: String,
    #[serde(default)]
    pub session: String,
    #[serde(default)]
    pub attempts: u32,
    /// `PROVED`, `DISPROVED`, `UNKNOWN`, `DISPROVED_WHITE_WIN`, '' while open.
    #[serde(default)]
    pub outcome: String,
    #[serde(default)]
    pub verified: bool,
    #[serde(default)]
    pub reject_reason: String,
    #[serde(default)]
    pub searched_nodes: u64,
    #[serde(default)]
    pub elapsed: f64,
    #[serde(default)]
    pub solver_build: String,
    #[serde(default)]
    pub completed: Option<Ms>,
    pub created: Ms,
}

impl SolveTask {
    pub fn new(id: u64, pos: u64, goal: &str, stage: &str, budget: u64, arm: &str, created: Ms) -> SolveTask {
        SolveTask {
            id,
            pos,
            goal: goal.to_string(),
            stage: stage.to_string(),
            budget,
            arm: arm.to_string(),
            campaign: None,
            state: SolveState::Pending,
            machine: String::new(),
            leased_at: None,
            heartbeat_at: None,
            token: String::new(),
            session: String::new(),
            attempts: 0,
            outcome: String::new(),
            verified: false,
            reject_reason: String::new(),
            searched_nodes: 0,
            elapsed: 0.0,
            solver_build: String::new(),
            completed: None,
            created,
        }
    }

    /// SOL-5.2: the row shape of a requeued proof lease.
    pub(crate) fn requeue(&mut self) {
        self.state = SolveState::Pending;
        self.machine.clear();
        self.heartbeat_at = None;
        self.token.clear();
        self.session.clear();
    }
}

/// `WorkerPing`: one row per (machine slot, account) (Q-1.20).
#[derive(Clone, Debug, PartialEq, Default, Serialize, Deserialize)]
pub struct Ping {
    pub threads: u32,
    pub hash_mb: u64,
    #[serde(default)]
    pub os: String,
    pub last_seen: Ms,
    #[serde(default)]
    pub worker_build: Option<i64>,
    #[serde(default)]
    pub current_task: Option<u64>,
    #[serde(default)]
    pub last_nps: u64,
    #[serde(default)]
    pub nps_updated: Option<Ms>,
    #[serde(default)]
    pub tasks_done: u64,
    #[serde(default)]
    pub last_result_at: Option<Ms>,
}

/// `ContributorPref.serve_order` (Q-1.18).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ServeOrder {
    #[default]
    Own,
    Queue,
    Others,
}

/// `ContributorPref` (Q-1.18).  An account without a row has every default.
#[derive(Clone, Debug, PartialEq, Default, Serialize, Deserialize)]
pub struct Pref {
    #[serde(default)]
    pub lifo_queue: bool,
    #[serde(default)]
    pub serve_order: ServeOrder,
    /// Already parsed by `parse_serve_for`.
    #[serde(default)]
    pub serve_for: Vec<String>,
    /// NULL or <= 0 means none.
    #[serde(default)]
    pub serve_campaign: Option<i64>,
}

/// `WorkerServing.mode` (Q-1.19).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkerMode {
    #[default]
    Account,
    Server,
    Personal,
}

/// `WorkerServing` (Q-1.19), keyed by (account, machine base).
#[derive(Clone, Debug, PartialEq, Default, Serialize, Deserialize)]
pub struct Serving {
    #[serde(default)]
    pub mode: WorkerMode,
    /// None inherits `Pref::serve_campaign`, 0 none, >0 a campaign id.
    #[serde(default)]
    pub campaign: Option<i64>,
}

/// `preferences.parse_serve_for` (Q-1.18): split on commas and whitespace,
/// cut each name to 64 chars, de-duplicate without case keeping the first
/// spelling, at most 20 names.
pub fn parse_serve_for(raw: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for part in raw.replace(',', " ").split_whitespace() {
        let name: String = part.trim().chars().take(64).collect();
        let low = name.to_lowercase();
        if !name.is_empty() && !seen.contains(&low) {
            seen.push(low);
            names.push(name);
        }
    }
    names.truncate(20);
    names
}

/// `views.machine_base` (Q-2.13): `bob#3` -> `bob`.
pub fn machine_base(machine: &str) -> &str {
    machine.split('#').next().unwrap_or("")
}

/// Python `str.lower()` equality, as `__iexact` (ASCII and Unicode simple case).
pub fn iequal(a: &str, b: &str) -> bool {
    a == b || a.to_lowercase() == b.to_lowercase()
}
