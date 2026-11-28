//! Proof campaigns (PN-0.1, PN-0.2): which proposition the numbers are about.
//!
//! In v1 `ProofCampaign` is a relational row (name, root, goal, active,
//! algorithm_version, repertoire_policy).  In v2 it belongs to the relational
//! side (`meta.sqlite`, phase 5); this crate receives the list.

use crate::numbers::Goal;
use av2_model::PosId;

pub const DEFAULT_CAMPAIGN_NAME: &str = "root-white-win";
pub const ALGORITHM_VERSION: i32 = 1;
/// `DEFAULT_REPERTOIRE_POLICY` (primary, backup, explore).
pub const DEFAULT_POLICY: (f64, f64, f64) = (0.8, 0.15, 0.05);

#[derive(Clone, Debug, PartialEq)]
pub struct Campaign {
    /// v1 row id: campaigns are processed and chosen in id order.
    pub id: u32,
    pub name: String,
    pub root: PosId,
    pub goal: Goal,
    pub active: bool,
    pub algorithm_version: i32,
    /// `repertoire_policy` as stored (raw JSON); read by the descent (PN-6.1),
    /// not by the numbers.
    pub policy: serde_json::Value,
}

impl Campaign {
    /// PN-0.2: the default campaign (`root-white-win`, start position, WHITE_WIN).
    pub fn default_for(id: u32, root: PosId) -> Campaign {
        Campaign {
            id,
            name: DEFAULT_CAMPAIGN_NAME.into(),
            root,
            goal: Goal::WhiteWin,
            active: true,
            algorithm_version: ALGORITHM_VERSION,
            policy: serde_json::json!({"primary": DEFAULT_POLICY.0, "backup": DEFAULT_POLICY.1, "explore": DEFAULT_POLICY.2}),
        }
    }
}

/// PN-6.1 `normalized_policy` (used by the descent; here because it is a
/// property of the campaign and is pinned by the campaign seed tests).
pub fn normalized_policy(c: &Campaign) -> (f64, f64, f64) {
    let raw = c.policy.as_object();
    let defaults = [DEFAULT_POLICY.0, DEFAULT_POLICY.1, DEFAULT_POLICY.2];
    let mut w = [0.0f64; 3];
    for (i, name) in ["primary", "backup", "explore"].iter().enumerate() {
        let v = raw.and_then(|o| o.get(*name));
        w[i] = match v {
            None => defaults[i].max(0.0),
            Some(x) => match py_float(x) {
                Some(f) => {
                    // Python: max(0.0, nan) is 0.0 (nan comparisons are false).
                    if f > 0.0 { f } else { 0.0 }
                }
                None => defaults[i],
            },
        };
    }
    let mut total = w[0] + w[1] + w[2];
    // `total <= 0` in Python is false for NaN; a NaN weight is already 0 here.
    if total.partial_cmp(&0.0) != Some(std::cmp::Ordering::Greater) {
        w = defaults;
        total = w[0] + w[1] + w[2];
    }
    (w[0] / total, w[1] / total, w[2] / total)
}

/// Python `float(x)` of a JSON value, `None` where Python raises.
fn py_float(v: &serde_json::Value) -> Option<f64> {
    match v {
        serde_json::Value::Number(n) => n.as_f64(),
        serde_json::Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        serde_json::Value::String(s) => {
            let t = s.trim().to_ascii_lowercase();
            match t.as_str() {
                "nan" | "+nan" | "-nan" => Some(f64::NAN),
                "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
                "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
                _ => t.replace('_', "").parse().ok().filter(|_| !t.contains("__")),
            }
        }
        _ => None,
    }
}

/// PN-0.2 `active_campaigns`: active ones, by id.
pub fn active_campaigns(all: &[Campaign]) -> Vec<&Campaign> {
    let mut v: Vec<&Campaign> = all.iter().filter(|c| c.active).collect();
    v.sort_by_key(|c| c.id);
    v
}

/// The "headline" campaign: the first active one by id.
pub fn headline_campaign(all: &[Campaign]) -> Option<&Campaign> {
    active_campaigns(all).into_iter().next()
}

#[cfg(test)]
mod tests {
    //! `test_proof_manager.py::CampaignSeedTests` (the parts that are not the
    //! Django migration itself).
    use super::*;

    #[test]
    fn test_migration_created_the_default_campaign() {
        let c = Campaign::default_for(1, PosId(0));
        assert_eq!(c.name, DEFAULT_CAMPAIGN_NAME);
        assert_eq!(c.goal, Goal::WhiteWin);
        assert!(c.active);
        assert_eq!(c.root, PosId(0));
    }
    #[test]
    fn test_policy_defaults_to_eighty_fifteen_five() {
        let (p, b, e) = normalized_policy(&Campaign::default_for(1, PosId(0)));
        assert!((p - 0.8).abs() < 1e-12 && (b - 0.15).abs() < 1e-12 && (e - 0.05).abs() < 1e-12);
    }
    #[test]
    fn test_malformed_policy_is_normalised_not_trusted() {
        let mut c = Campaign::default_for(1, PosId(0));
        c.policy = serde_json::json!({"primary": 6, "backup": 3, "explore": "nonsense"});
        let (p, b, e) = normalized_policy(&c);
        assert!((p + b + e - 1.0).abs() < 1e-12);
        assert!(p > b);
        // "nonsense" falls back to the default 0.05: 6 / 9.05.
        assert!((p - 6.0 / 9.05).abs() < 1e-12);
    }
    #[test]
    fn nan_and_negative_weights_read_as_zero() {
        let mut c = Campaign::default_for(1, PosId(0));
        c.policy = serde_json::json!({"primary": "nan", "backup": -1, "explore": 1});
        assert_eq!(normalized_policy(&c), (0.0, 0.0, 1.0));
        c.policy = serde_json::json!({"primary": 0, "backup": 0, "explore": 0});
        let (p, _, _) = normalized_policy(&c);
        assert!((p - 0.8).abs() < 1e-12);
    }
    #[test]
    fn test_the_default_campaign_is_the_headline() {
        let mut other = Campaign::default_for(7, PosId(3));
        other.name = "later".into();
        let all = vec![other, Campaign::default_for(2, PosId(0))];
        assert_eq!(headline_campaign(&all).unwrap().id, 2);
    }
    #[test]
    fn test_no_active_campaign_means_no_headline() {
        let mut c = Campaign::default_for(1, PosId(0));
        c.active = false;
        assert!(headline_campaign(&[c]).is_none());
    }
}
