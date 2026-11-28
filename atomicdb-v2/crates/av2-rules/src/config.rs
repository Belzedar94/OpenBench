//! The constants and switches of the value rules (spec section 1.4).
//!
//! Defaults are v1's, including the guards and the noise cut that owner
//! decision D2 drops only after the exact port passes: they stay switchable
//! here so that the port can be compared with v1 first and simplified later.

/// `ingest.CASCADE_GUARD_LIMIT`.
pub const CASCADE_GUARD_LIMIT: u64 = 100_000;
/// `ingest.CASCADE_REWRITE_LIMIT`.
pub const CASCADE_REWRITE_LIMIT: u32 = 32;
/// `ingest.BACKED_MAX_PLIES`.
pub const BACKED_MAX_PLIES: u64 = 64;
/// `ingest.BACKED_MAX_REVISITS` (= the level bound).
pub const BACKED_MAX_REVISITS: u32 = BACKED_MAX_PLIES as u32;
/// `ingest.BACKED_MAX_NODES`.
pub const BACKED_MAX_NODES: u64 = 50_000;
/// `ingest.BACKED_EPSILON_CP`.
pub const BACKED_EPSILON_CP: i32 = 10;
/// `ingest.BACKED_ECHO_PLIES` (= 4 x 64).
pub const BACKED_ECHO_PLIES: u16 = 4 * BACKED_MAX_PLIES as u16;
/// `ingest.BACKED_GUARD_KEYS`.
pub const BACKED_GUARD_KEYS: usize = 2_000;
/// `ingest.CARRY_MAX_KEYS` (BKD-9.2, read by whoever carries a cut on).
pub const CARRY_MAX_KEYS: usize = 64;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RulesConfig {
    /// `ATOMICDB_FRESH_CONTEXT` (CAS-1.2), default on.
    pub fresh_context: bool,
    /// BKD-8.1 the noise cut (D2: true = v1 behaviour).
    pub noise_cut: bool,
    /// `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS` (queue file; read by BKD-11.1),
    /// default off.
    pub auto_interior_deep_rungs: bool,
    /// CAS-5.1.
    pub cascade_guard_limit: u64,
    /// CAS-5.2.
    pub cascade_rewrite_limit: u32,
    /// DTM-3.1 `MATE_SUPPORT_HOPS`.
    pub mate_support_hops: usize,
    /// BKD-7.1 `max_plies`.
    pub backed_max_plies: u64,
    /// BKD-7.4.
    pub backed_max_revisits: u32,
    /// BKD-7.5.
    pub backed_max_nodes: u64,
}

impl Default for RulesConfig {
    fn default() -> Self {
        RulesConfig {
            fresh_context: true,
            noise_cut: true,
            auto_interior_deep_rungs: false,
            cascade_guard_limit: CASCADE_GUARD_LIMIT,
            cascade_rewrite_limit: CASCADE_REWRITE_LIMIT,
            mate_support_hops: crate::distance::MATE_SUPPORT_HOPS,
            backed_max_plies: BACKED_MAX_PLIES,
            backed_max_revisits: BACKED_MAX_REVISITS,
            backed_max_nodes: BACKED_MAX_NODES,
        }
    }
}
