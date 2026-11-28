//! Settings that select branches of the queue rules (v1 Django settings,
//! environment variables and `refresh_selector` flags, PRI-6.6).
//!
//! The configuration is part of the queue state and changes only through a
//! logged input (`Input::SetConfig`), so a replay reads the same switches.
//! Defaults are the v1 **code** defaults.  The production values that are
//! not in the repo (OQ-28) must be captured and set at cut-over; the known
//! ones are in [`QueueConfig::production_known`].

use crate::consts::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SelectorMode {
    /// `ATOMICDB_SELECTOR=regret` (code default): regret priorities.
    #[default]
    Regret,
    /// `ATOMICDB_SELECTOR=pn` (production): proof/value descent.
    Pn,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct QueueConfig {
    /// `views.HOUSE_ACCOUNTS` (Q-2.15).
    pub house_accounts: Vec<String>,
    /// `ATOMICDB_SWEEP16_SHARE` (Q-2.28.1), clamped to [0, 1].
    pub sweep16_share: f64,
    /// `--pool-target` (PRI-4.3): 512 in the code, 64 in production.
    pub pool_target: usize,
    /// `ATOMICDB_ARM_RATE_*` (Q-8.2): tasks per rolling hour, 0 = arm off.
    pub arm_rates: BTreeMap<String, u32>,
    /// `ATOMICDB_SELECTOR` (PRI-4.1).
    pub selector: SelectorMode,
    /// `ATOMICDB_DESCENT == 'value'` (PRI-4.5).
    pub descent_value: bool,
    /// `ATOMICDB_CAMPAIGN_DESCENT` (PRI-4.4).
    pub campaign_descent: bool,
    /// `ATOMICDB_OR_CLAMP` (PRI-4.6).
    pub or_clamp: bool,
    /// `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS` (PRI-5.7).
    pub interior_deep_rungs: bool,
    /// `ATOMICDB_ADVERSARIAL` / `--adversarial` (Q-8.10).
    pub adversarial: bool,
    /// `ATOMICDB_BREADTH_SWAP` (Q-6.22).
    pub breadth_swap: bool,
    /// `ATOMICDB_TB_SERVER_MAX_MEN` (Q-2.26), default 5.
    pub tb_server_max_men: u32,
    /// `--debt-cap` (Q-8.7), `--coverage-cap` (Q-8.9), `--dn-repair-cap`,
    /// `--fragile-cap` (Q-8.11, Q-8.14).
    pub debt_cap: usize,
    pub coverage_cap: usize,
    pub dn_repair_cap: usize,
    pub fragile_cap: usize,
}

impl Default for QueueConfig {
    fn default() -> Self {
        QueueConfig {
            house_accounts: HOUSE_ACCOUNTS.iter().map(|s| s.to_string()).collect(),
            sweep16_share: 0.14,
            pool_target: ANALYSIS_POOL_TARGET,
            arm_rates: ARM_RATE_DEFAULTS.iter().map(|(a, r)| (a.to_string(), *r)).collect(),
            selector: SelectorMode::Regret,
            descent_value: false,
            campaign_descent: true,
            or_clamp: true,
            interior_deep_rungs: false,
            adversarial: false,
            breadth_swap: false,
            tb_server_max_men: 5,
            debt_cap: DEBT_QUEUE_CAP,
            coverage_cap: COVERAGE_QUEUE_CAP,
            dn_repair_cap: DN_REPAIR_QUEUE_CAP,
            fragile_cap: FRAGILE_QUEUE_CAP,
        }
    }
}

impl QueueConfig {
    /// What the repo says production runs (PRI-6.2): `ATOMICDB_SELECTOR=pn`,
    /// pool target 64.  The rest of OQ-28 is unknown and keeps the defaults.
    pub fn production_known() -> Self {
        QueueConfig { selector: SelectorMode::Pn, pool_target: 64, ..Default::default() }
    }

    /// `ingest.arm_rate(arm)` (Q-8.2).  An arm without a ceiling is `None`
    /// (v1 raises `KeyError`; its callers never ask).
    pub fn arm_rate(&self, arm: &str) -> Option<u32> {
        self.arm_rates.get(arm).copied()
    }

    /// The share as `sweep.share()` reads it: clamped, NaN -> default.
    pub fn sweep_share(&self) -> f64 {
        let s = self.sweep16_share;
        if s.is_nan() { 0.14 } else { s.clamp(0.0, 1.0) }
    }
}

/// Q-8.2: the value of an `ATOMICDB_ARM_RATE_*` string as v1 parses it
/// (`str.isdigit()` -> int, anything else -> default).
pub fn parse_arm_rate(raw: &str, default: u32) -> u32 {
    let s = raw.trim();
    if !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()) { s.parse().unwrap_or(default) } else { default }
}
