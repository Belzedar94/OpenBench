//! The reference-engine gate (Q-9).
//!
//! The verdict depends on files (the policy file and the engines directory),
//! so it is computed by the HTTP layer when a submit arrives and travels in
//! the input (`SubmitReq::gate_rejects`, OQ-10); the writer only applies it
//! (Q-2.40).  The loaders cache by `(size, mtime)` like v1.

use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub const DEFAULT_POLICY_FILE: &str = "/etc/openbench/atomicdb-engine-policy.json";
pub const DEFAULT_ENGINES_DIR: &str = "/var/www/atomicdb-engines";
pub const REFERENCE_PREFIX: &str = "Atomic-Stockfish-";
/// `engine_policy.REJECTION_DETAIL`.
pub const REJECTION_DETAIL: &str = "AtomicDB only takes analysis from the reference engine. Start the worker \
without --engine and it downloads the reference build; to get your own \
build accepted, ask on the Belzedar Labs Discord.";

#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Policy {
    pub enforce: bool,
    /// sha256 (lower-case hex) -> note.
    pub extra_engines: BTreeMap<String, String>,
}

fn is_sha256(s: &str) -> bool {
    s.len() == 64 && s.chars().all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
}

/// Q-9.1: parse a policy file's text (fail-open: anything odd reads `off`).
pub fn parse_policy(text: &str) -> Policy {
    let Ok(serde_json::Value::Object(o)) = serde_json::from_str::<serde_json::Value>(text) else {
        return Policy::default();
    };
    let enforce = o.get("mode").and_then(|m| m.as_str()) == Some("enforce");
    let mut extra = BTreeMap::new();
    if let Some(serde_json::Value::Object(e)) = o.get("extra_engines") {
        for (k, v) in e {
            let k = k.to_lowercase();
            if is_sha256(&k) {
                let note = match v {
                    serde_json::Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                extra.insert(k, note);
            }
        }
    }
    Policy { enforce, extra_engines: extra }
}

/// Q-9.3 `accepted_engines`: reference binaries first, extras where absent.
pub fn accepted_engines(policy: &Policy, references: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    let mut out: BTreeMap<String, String> = references.iter().map(|(k, v)| (k.clone(), format!("reference: {v}"))).collect();
    for (k, v) in &policy.extra_engines {
        out.entry(k.clone()).or_insert_with(|| format!("extra: {v}"));
    }
    out
}

/// Q-9.4 `rejects(engine_sha, tablebase)`.  `engine_sha` is already
/// cleaned (Q-2.37).
pub fn rejects(policy: &Policy, references: &BTreeMap<String, String>, engine_sha: &str, tablebase: bool) -> bool {
    if tablebase || !policy.enforce || references.is_empty() {
        return false;
    }
    !accepted_engines(policy, references).contains_key(engine_sha)
}

/// The two inputs of the gate read from disk, cached by file stamp.
pub struct GateFiles {
    pub policy_file: PathBuf,
    pub engines_dir: PathBuf,
    policy_cache: Mutex<Option<((u64, i128), Policy)>>,
    digest_cache: Mutex<BTreeMap<PathBuf, ((u64, i128), String)>>,
}

fn stamp(p: &Path) -> Option<(u64, i128)> {
    let m = std::fs::metadata(p).ok()?;
    let mtime = m.modified().ok()?.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as i128).unwrap_or(0);
    Some((m.len(), mtime))
}

impl GateFiles {
    pub fn new(policy_file: impl Into<PathBuf>, engines_dir: impl Into<PathBuf>) -> GateFiles {
        GateFiles {
            policy_file: policy_file.into(),
            engines_dir: engines_dir.into(),
            policy_cache: Mutex::new(None),
            digest_cache: Mutex::new(BTreeMap::new()),
        }
    }

    /// Q-9.1 `load_policy`.
    pub fn policy(&self) -> Policy {
        let Some(st) = stamp(&self.policy_file) else { return Policy::default() };
        let mut c = self.policy_cache.lock().unwrap();
        if let Some((s, p)) = c.as_ref()
            && *s == st
        {
            return p.clone();
        }
        let p = std::fs::read_to_string(&self.policy_file).map(|t| parse_policy(&t)).unwrap_or_default();
        *c = Some((st, p.clone()));
        p
    }

    /// Q-9.2 `reference_engines`: sha256 -> file name.
    pub fn references(&self) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();
        let Ok(rd) = std::fs::read_dir(&self.engines_dir) else { return out };
        let mut cache = self.digest_cache.lock().unwrap();
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            let path = e.path();
            if !name.starts_with(REFERENCE_PREFIX) || !path.is_file() {
                continue;
            }
            let Some(st) = stamp(&path) else { continue };
            let digest = match cache.get(&path) {
                Some((s, d)) if *s == st => d.clone(),
                _ => {
                    let Ok(bytes) = std::fs::read(&path) else { continue };
                    let d = hex::encode(Sha256::digest(&bytes));
                    cache.insert(path.clone(), (st, d.clone()));
                    d
                }
            };
            out.insert(digest, name);
        }
        out
    }

    /// The verdict to put in `SubmitReq::gate_rejects`.  While the gate is
    /// off nothing is hashed.
    pub fn rejects(&self, engine_sha: &str, tablebase: bool) -> bool {
        if tablebase {
            return false;
        }
        let policy = self.policy();
        if !policy.enforce {
            return false;
        }
        rejects(&policy, &self.references(), engine_sha, tablebase)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("av2-queue-gate-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn engine_gate() {
        let d = tmp("gate");
        let engines = d.join("engines");
        std::fs::create_dir_all(&engines).unwrap();
        let policy = d.join("policy.json");
        let gate = GateFiles::new(&policy, &engines);
        // test_the_gate_is_off_without_a_policy_file
        assert!(!gate.rejects("", false));
        // test_a_broken_or_unknown_policy_reads_as_off
        std::fs::write(&policy, "{not json").unwrap();
        assert!(!gate.rejects("", false));
        std::fs::write(&policy, r#"{"mode": "strict"}"#).unwrap();
        assert!(!gate.rejects("", false));
        std::fs::write(&policy, r#"["enforce"]"#).unwrap();
        assert!(!gate.rejects("", false));
        // test_no_reference_binary_lets_everything_through
        std::fs::write(&policy, r#"{"mode": "enforce"}"#).unwrap();
        assert!(!gate.rejects("", false));
        // test_current_and_previous_reference_builds_are_accepted
        std::fs::write(engines.join("Atomic-Stockfish-1102"), b"engine one").unwrap();
        std::fs::write(engines.join("Atomic-Stockfish-1114"), b"engine two").unwrap();
        std::fs::write(engines.join("README"), b"not an engine").unwrap();
        let one = hex::encode(Sha256::digest(b"engine one"));
        let two = hex::encode(Sha256::digest(b"engine two"));
        let readme = hex::encode(Sha256::digest(b"not an engine"));
        assert!(!gate.rejects(&one, false));
        assert!(!gate.rejects(&two, false));
        // test_other_files_in_the_engines_directory_are_not_engines
        assert!(gate.rejects(&readme, false));
        // test_a_missing_sha_is_refused_while_enforcing / foreign engine
        assert!(gate.rejects("", false));
        assert!(gate.rejects(&"ab".repeat(32), false));
        // test_tablebase_results_are_never_gated
        assert!(!gate.rejects("", true));
        // test_an_extra_engine_listed_in_the_policy_is_accepted (upper case normalised)
        let extra = "CD".repeat(32);
        std::fs::write(&policy, format!(r#"{{"mode": "enforce", "extra_engines": {{"{extra}": "lab build", "short": "x"}}}}"#)).unwrap();
        assert!(!gate.rejects(&extra.to_lowercase(), false));
        let acc = accepted_engines(&gate.policy(), &gate.references());
        assert_eq!(acc.get(&one).unwrap(), "reference: Atomic-Stockfish-1102");
        assert_eq!(acc.get(&extra.to_lowercase()).unwrap(), "extra: lab build");
        assert_eq!(acc.len(), 3);
        // test_a_replaced_binary_is_hashed_again
        std::fs::write(engines.join("Atomic-Stockfish-1114"), b"engine three, longer").unwrap();
        let three = hex::encode(Sha256::digest(b"engine three, longer"));
        assert!(!gate.rejects(&three, false));
        assert!(gate.rejects(&two, false));
        let _ = std::fs::remove_dir_all(&d);
    }
}
