//! Every constant of the queue rules, with the v1 name and rule id.
//!
//! Times are milliseconds (`Ms`, Unix epoch).  Budgets are engine nodes.

/// Milliseconds since the Unix epoch.  Every input carries its own `now`;
/// the rules never read a clock (plan 2.2, OQ-10).
pub type Ms = i64;

pub const SECOND: Ms = 1_000;
pub const MINUTE: Ms = 60 * SECOND;
pub const HOUR: Ms = 60 * MINUTE;
pub const DAY: Ms = 24 * HOUR;

// ------------------------------------------------------------ lease (Q-2)

/// `views.LEASE_MINUTES` (Q-2.5): a token lease silent this long is recycled.
pub const LEASE_MINUTES: Ms = 4 * MINUTE;
/// `views.RESTART_RECYCLE_MINUTES` (Q-2.11, Q-2.55).
pub const RESTART_RECYCLE_MINUTES: Ms = 3 * MINUTE;
/// `views.LEASE_TOKEN_BUILD` (Q-2.4).  Owner decision D8: builds below it are
/// refused at the lease (no tokenless leases are handed out any more).
pub const LEASE_TOKEN_BUILD: i64 = 2026072203;
/// `views.LEASES_PER_MACHINE` (Q-2.13).
pub const LEASES_PER_MACHINE: usize = 32;
/// `views.RELEASE_MAX_LEASES` (Q-2.47).
pub const RELEASE_MAX_LEASES: usize = LEASES_PER_MACHINE + 1;
/// `views.TASK_REFILL_COUNT` (Q-2.28).
pub const TASK_REFILL_COUNT: usize = 8;
/// `views.DEEP_TASK_NODES` (Q-2.24).
pub const DEEP_TASK_NODES: u64 = 10_000_000_000;
/// `views.DEEP_LEASES_POOL_SHARE` (Q-2.24).
pub const DEEP_LEASES_POOL_SHARE: usize = 3;
/// `views.DEEP_LEASES_FLOOR` (Q-2.24).
pub const DEEP_LEASES_FLOOR: usize = 1;
/// `metrics.LIVE_SECONDS` (Q-1.20, Q-2.24).
pub const LIVE_WINDOW: Ms = 180 * SECOND;
/// `views.MAX_REPORTED_THREADS` (Q-2.3).
pub const MAX_REPORTED_THREADS: u32 = 1024;
/// `views.MAX_WORKER_BUILD` (Q-2.3).
pub const MAX_WORKER_BUILD: i64 = 2_147_483_647;
/// `views.MAX_REPORTED_NPS` (Q-2.32, Q-2.45).
pub const MAX_REPORTED_NPS: u64 = 1_000_000_000_000;
/// `idle_repair.REPAIR_MAX_THREADS` (Q-2.25).
pub const REPAIR_MAX_THREADS: u32 = 8;
/// `views.HOUSE_ACCOUNTS` (Q-2.15).
pub const HOUSE_ACCOUNTS: &[&str] = &["codex_local_worker"];
/// `views.LEGACY_MAX_BUDGET` (Q-2.23).  Unused under D8; kept for reference.
pub const LEGACY_MAX_BUDGET: u64 = 128_000_000;

// ------------------------------------------------------------ submit (Q-2.35..)

/// `views.MAX_SUBMIT_LINES_BYTES`.
pub const MAX_SUBMIT_LINES_BYTES: usize = 512 * 1024;
/// `views.MAX_SUBMIT_PV_PLIES`.
pub const MAX_SUBMIT_PV_PLIES: usize = 512;
pub const MAX_SUBMIT_LINES: usize = 32;
pub const MAX_SUBMIT_MOVE_LEN: usize = 16;
pub const MAX_SUBMIT_RAW_LEN: usize = 65_536;
/// Q-2.36: elapsed is clamped to one day.
pub const MAX_ELAPSED_SECONDS: f64 = 86_400.0;
/// Q-2.36: |tb_dtz| above this is dropped.
pub const MAX_TB_DTZ: i64 = 1024;

// ------------------------------------------------------------ solve (Q-2.52.., SOL-5)

/// `ingest.SOLVE_LEASE_MINUTES` (SOL-5.1).
pub const SOLVE_LEASE_MINUTES: Ms = 20 * MINUTE;

// ------------------------------------------------------------ service order (Q-5)

/// `live_request.STARVED_AFTER` (Q-2.17).
pub const STARVED_AFTER: Ms = 24 * HOUR;
/// `live_request.SERVED_WINDOW` (Q-2.19).
pub const SERVED_WINDOW: Ms = HOUR;
/// `lanes.CONTRIBUTOR_WINDOW_DAYS` (Q-5.1).
pub const CONTRIBUTOR_WINDOW: Ms = 7 * DAY;
/// `views.REQUEST_QUEUE_MAX` (Q-5.10).
pub const REQUEST_QUEUE_MAX: usize = 10_000;
/// `views.LANE_CAP_RECEIPT_WINDOW` (Q-5.11).
pub const LANE_CAP_RECEIPT_WINDOW: Ms = MINUTE;
/// `views.QUEUE_FULL_RETRY_AFTER` seconds (Q-5.11).
pub const QUEUE_FULL_RETRY_AFTER_SECONDS: u32 = 300;

// ------------------------------------------------------------ budgets (PRI-5)

/// `ingest.BUDGET_LADDER` (PRI-5.1).
pub const BUDGET_LADDER: [u64; 5] = [8_000_000, 32_000_000, 128_000_000, 512_000_000, 2_000_000_000];
/// `ingest.REQUEST_BUDGET_LADDER` (PRI-5.2).
pub const REQUEST_BUDGET_LADDER: [u64; 3] = [128_000_000, 512_000_000, 2_000_000_000];
/// `ingest.SUBTREE_REQUEST_NODES`: a wire value, never a task budget.
pub const SUBTREE_REQUEST_NODES: u64 = 10_000_000_000;
pub const SUBTREE_SEARCH_NODES: u64 = 128_000_000;
pub const SUBTREE_TASK_CAP: u64 = SUBTREE_REQUEST_NODES / SUBTREE_SEARCH_NODES;
pub const NOTIFY_MIN_BUDGET: u64 = REQUEST_BUDGET_LADDER[0];
/// `ingest.SERVER_PICKED_MIN_NODES` (PRI-5.6).
pub const SERVER_PICKED_MIN_NODES: u64 = REQUEST_BUDGET_LADDER[0];
/// `ingest.AUTO_INTERIOR_MAX_NODES` (PRI-5.7).
pub const AUTO_INTERIOR_MAX_NODES: u64 = 128_000_000;
/// `ingest.DEPTH_BUDGET_THRESHOLD` (PRI-5.5).
pub const DEPTH_BUDGET_THRESHOLD: u64 = 512_000_000;
/// `ingest.DEPTH_MULTIPV` (PRI-5.5).
pub const DEPTH_MULTIPV: u32 = 2;
/// `ingest.MATE_BAND` / `score_bands.BIG_LOW` (PRI-1.1).
pub const MATE_BAND: i32 = 9000;
/// `ingest.TB_CLAMP_CEILING` (PRI-5.4).
pub const TB_CLAMP_CEILING: i32 = 9500;
/// `ingest.MATE_CLAMP_PLIES` (PRI-5.4): M<=3 counting the defender's ply.
pub const MATE_CLAMP_PLIES: u32 = 6;
pub const MATE_CLAMP_MIN_NODES: u64 = 2_000_000;
pub const MATE_CLAMP_NODES_PER_PLY: u64 = 4_000_000;
pub const MATE_CLAMP_MAX_NODES: u64 = 32_000_000;
pub const MATE_CLAMP_MULTIPV: u32 = 1;
/// `ingest.RUNG_OVERSHOOT_TOLERANCE` (PRI-5.8).
pub const RUNG_OVERSHOOT_TOLERANCE: f64 = 0.01;

// ------------------------------------------------------------ selector (PRI-1..4)

/// `ingest.DEAD` (PRI-1.1): v1 tombstone value.  v2 keeps a boolean (OQ-26).
pub const DEAD: f64 = -1e9;
pub const REGRET_WEIGHT: f64 = 3.0;
pub const DISCONNECTED_REGRET: f64 = 5.0;
pub const CAMPAIGN_BONUS: f64 = 40.0;
pub const EVAL_PROXIMITY_CAP: i64 = 1500;
pub const MATE_BAND_BONUS: f64 = 50.0;
pub const UNEXPANDED_BONUS: f64 = 2.0;
pub const VISIT_WEIGHT: f64 = 1.5;
pub const REGRET_CAP: f64 = 3000.0;
pub const FAR_REGRET: f64 = REGRET_CAP / 100.0;
/// `ingest.ANALYSIS_POOL_TARGET` (PRI-4.3).  Production passes 64 (OQ-28).
pub const ANALYSIS_POOL_TARGET: usize = 512;
/// `ingest.VALUE_SPINE_NODES` .. (PRI-4.8).
pub const VALUE_SPINE_NODES: u64 = REQUEST_BUDGET_LADDER[0];
pub const VALUE_BACKUP_NODES: u64 = BUDGET_LADDER[1];
pub const VALUE_EXPLORE_NODES: u64 = BUDGET_LADDER[0];
/// `ingest.VALUE_CASCADE_*` (PRI-4.9).
pub const VALUE_CASCADE_NODES: u64 = 8_000_000;
pub const VALUE_CASCADE_CAP: usize = 16;
/// `proof.OR_CLAMP_*` (PRI-4.6).
pub const OR_CLAMP_NODES: u64 = 250_000;
pub const OR_CLAMP_MULTIPV: u32 = 1;

// ------------------------------------------------------------ arms (Q-8)

pub const ARM_NONE: &str = "";
pub const ARM_CAMPAIGN: &str = "campaign";
pub const ARM_DESCEND: &str = "descend";
pub const ARM_CASCADE: &str = "cascade";
pub const ARM_COVERAGE: &str = "coverage";
pub const ARM_DN: &str = "dn";
pub const ARM_QUALITY: &str = "quality";
pub const ARM_REPAIR: &str = "repair";
pub const ARM_SWEEP16: &str = "sweep16";
pub const ARM_DEBT: &str = "debt";
pub const ARM_FRAGILE: &str = "fragile";
pub const ARM_AUTOSOLVE95: &str = "autosolve95";

/// `ingest.ARM_RATE_WINDOW` (Q-8.3).
pub const ARM_RATE_WINDOW: Ms = HOUR;
/// `ingest.ARM_RATE_RECEIPT_WINDOW` (Q-8.4).
pub const ARM_RATE_RECEIPT_WINDOW: Ms = 10 * MINUTE;
/// `ingest.ARM_RATE_SETTING` defaults (Q-8.2).
pub const ARM_RATE_DEFAULTS: &[(&str, u32)] = &[
    (ARM_SWEEP16, 1200),
    (ARM_CASCADE, 240),
    (ARM_QUALITY, 120),
    (ARM_DESCEND, 120),
    (ARM_REPAIR, 60),
    (ARM_AUTOSOLVE95, 60),
];

pub const DEBT_STAGE_NODES: u64 = 2_000_000;
pub const DEBT_QUEUE_CAP: usize = 500;
pub const COVERAGE_QUEUE_CAP: usize = 100;
pub const COVERAGE_MISSING_MAX: usize = 3;
pub const COVERAGE_DECISIVE_CP: i32 = 800;
pub const COVERAGE_SCAN_ROWS: usize = 2000;
pub const COVERAGE_SEED_NODES: u64 = 8_000_000;
pub const DN_REPAIR_QUEUE_CAP: usize = 50;
pub const DN_REPAIR_FLOOR: u64 = 2;
pub const DN_REPAIR_REPLIES: usize = 3;
pub const DN_REPAIR_MAX_PER_CYCLE: usize = 24;
pub const DN_REPAIR_MAX_NODES: usize = 200;
pub const DN_REPAIR_SEED_NODES: u64 = 8_000_000;
pub const FRAGILE_QUEUE_CAP: usize = 50;
pub const FRAGILE_SCAN_ROWS: usize = 2000;
pub const FRAGILE_STAGE_NODES: u64 = 2_000_000;
pub const UNEXPLORED_CLICK_CAP: usize = 64;
pub const QUALITY_QUEUE_CAP: usize = 50;
