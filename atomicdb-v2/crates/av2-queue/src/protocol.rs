//! Heartbeat, submit and release (Q-2.30..Q-2.51), plus the parsers the HTTP
//! layer uses to turn a form into an input (Q-2.2, Q-2.3, Q-2.32, Q-2.35..
//! Q-2.37, Q-2.47).  The parsers are pure and give the v1 answers.

use crate::consts::*;
use crate::env::QueueEnv;
use crate::input::*;
use crate::model::*;
use crate::state::QueueState;

// ------------------------------------------------------------ parsers

/// Python `int()` on a form string: optional sign, digits, surrounding
/// whitespace (underscores between digits are accepted by Python too).
pub fn py_int(raw: &str) -> Option<i64> {
    let s = raw.trim();
    let (neg, digits) = match s.strip_prefix('-') {
        Some(d) => (true, d),
        None => (false, s.strip_prefix('+').unwrap_or(s)),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    if !digits.chars().all(|c| c.is_ascii_digit() || c == '_') {
        return None;
    }
    let v: i128 = digits.replace('_', "").parse().ok()?;
    let v = if neg { -v } else { v };
    i64::try_from(v).ok()
}

/// Q-2.2 `reported_max_budget`: absent or '' -> no cap; not a positive int
/// -> `Err` (HTTP 400 "max_budget must be a positive whole number of nodes").
// The caller maps the failure to its fixed HTTP error string: no error value to carry.
#[allow(clippy::result_unit_err)]
pub fn parse_max_budget(raw: Option<&str>) -> Result<Option<u64>, ()> {
    match raw {
        None => Ok(None),
        Some("") => Ok(None),
        Some(s) => match py_int(s) {
            Some(v) if v > 0 => Ok(Some(v as u64)),
            _ => Err(()),
        },
    }
}

/// Q-2.3 `reported_threads`: clamp to 0..=1024, 0 on error.
pub fn parse_threads(raw: Option<&str>) -> u32 {
    let v = raw.filter(|s| !s.is_empty()).and_then(py_int).unwrap_or(0);
    v.clamp(0, MAX_REPORTED_THREADS as i64) as u32
}

/// Q-2.3: `hash` -> max(0, int), 0 on error.
pub fn parse_hash(raw: Option<&str>) -> u64 {
    raw.filter(|s| !s.is_empty()).and_then(py_int).unwrap_or(0).max(0) as u64
}

/// Q-2.3 `reported_worker_build`: only 1..=2^31-1.
pub fn parse_worker_build(raw: Option<&str>) -> Option<i64> {
    raw.and_then(py_int).filter(|&b| (1..=MAX_WORKER_BUILD).contains(&b))
}

/// Q-2.4: `int(POST worker_build or 0)`, 0 on error (capability test).
pub fn parse_build_capability(raw: Option<&str>) -> i64 {
    raw.filter(|s| !s.is_empty()).and_then(py_int).unwrap_or(0)
}

/// Q-2.37 `_sha_field`.
pub fn sha_field(raw: &str) -> String {
    let s = raw.trim().to_lowercase();
    if s.len() == 64 && s.chars().all(|c| c.is_ascii_hexdigit()) { s } else { String::new() }
}

/// Python `float()` for Q-2.36 (`inf`, `nan`, exponents, whitespace).
fn py_float(raw: &str) -> Option<f64> {
    let s = raw.trim().to_lowercase();
    match s.as_str() {
        "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
        "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
        "nan" | "+nan" | "-nan" => Some(f64::NAN),
        _ => s.replace('_', "").parse::<f64>().ok(),
    }
}

/// Q-2.36 elapsed: non-finite or unparsable -> 0, clamped to [0, 86400].
pub fn parse_elapsed(raw: Option<&str>) -> f64 {
    match raw.filter(|s| !s.is_empty()).and_then(py_float) {
        Some(x) if x.is_finite() => x.clamp(0.0, MAX_ELAPSED_SECONDS),
        _ => 0.0,
    }
}

/// Q-2.36 nodes: max(0, int), unparsable (`"1.5"`) -> 0.
pub fn parse_nodes(raw: Option<&str>) -> u64 {
    raw.filter(|s| !s.is_empty()).and_then(py_int).unwrap_or(0).max(0) as u64
}

/// Q-2.36 tb_wdl: '' -> None, not an int -> `Err` ("malformed: invalid tb_wdl").
// The caller maps the failure to its fixed HTTP error string: no error value to carry.
#[allow(clippy::result_unit_err)]
pub fn parse_tb_wdl(raw: Option<&str>) -> Result<Option<i64>, ()> {
    match raw {
        None => Ok(None),
        Some("") => Ok(None),
        Some(s) => py_int(s).map(Some).ok_or(()),
    }
}

/// Q-2.36 tb_dtz: '' or unparsable -> None, |dtz| > 1024 -> None.
pub fn parse_tb_dtz(raw: Option<&str>) -> Option<i64> {
    raw.filter(|s| !s.is_empty()).and_then(py_int).filter(|d| d.abs() <= MAX_TB_DTZ)
}

/// Q-2.32 nps: clamp to 0..=1e12; not an int -> `Err` ("invalid nps").
// The caller maps the failure to its fixed HTTP error string: no error value to carry.
#[allow(clippy::result_unit_err)]
pub fn parse_nps(raw: Option<&str>) -> Result<u64, ()> {
    match raw.filter(|s| !s.is_empty()) {
        None => Ok(0),
        Some(s) => py_int(s).map(|v| v.clamp(0, MAX_REPORTED_NPS as i64) as u64).ok_or(()),
    }
}

/// Q-2.35: validate the `lines` field.  `Err(message)` is the text after
/// `malformed: ` (JSON error texts differ from Python's; compare on prefix).
pub fn validate_lines(raw: &str) -> Result<serde_json::Value, String> {
    if raw.len() > MAX_SUBMIT_LINES_BYTES {
        return Err("lines payload is too large".into());
    }
    let v: serde_json::Value = serde_json::from_str(raw).map_err(|e| e.to_string())?;
    let Some(list) = v.as_array().filter(|l| l.len() <= MAX_SUBMIT_LINES) else {
        return Err("lines must be a list with at most 32 entries".into());
    };
    for line in list {
        let Some(obj) = line.as_object() else { return Err("each line must be an object".into()) };
        match obj.get("move").and_then(|m| m.as_str()) {
            Some(m) if !m.is_empty() && m.chars().count() <= MAX_SUBMIT_MOVE_LEN => {}
            _ => return Err("invalid root move".into()),
        }
        for (field, msg) in [("eval_cp", "invalid eval_cp"), ("mate", "invalid mate")] {
            match obj.get(field) {
                None | Some(serde_json::Value::Null) => {}
                Some(serde_json::Value::Number(n)) if n.is_i64() || n.is_u64() => {}
                _ => return Err(msg.into()),
            }
        }
        match obj.get("pv") {
            None | Some(serde_json::Value::Null) => {}
            Some(serde_json::Value::Array(pv))
                if pv.len() <= MAX_SUBMIT_PV_PLIES
                    && pv.iter().all(|m| m.as_str().map(|s| s.chars().count() <= MAX_SUBMIT_MOVE_LEN).unwrap_or(false)) => {}
            _ => return Err("invalid or excessively long PV".into()),
        }
        match obj.get("raw") {
            None | Some(serde_json::Value::Null) => {}
            Some(serde_json::Value::String(s)) if s.chars().count() <= MAX_SUBMIT_RAW_LEN => {}
            _ => return Err("raw line is too large".into()),
        }
    }
    Ok(v)
}

/// Q-2.47: parse the `leases` field of a release.  `Err(message)` is the
/// text after `malformed: `; `caller` is the caller's machine (default for
/// `owner`).
pub fn parse_release(raw: Option<&str>, caller: &str) -> Result<Vec<ReleaseEntry>, String> {
    let raw = raw.unwrap_or("[]");
    let v: serde_json::Value = serde_json::from_str(raw).map_err(|_| "leases is not JSON".to_string())?;
    let Some(list) = v.as_array() else { return Err("leases must be a list".into()) };
    if list.len() > RELEASE_MAX_LEASES {
        return Err(format!("at most {RELEASE_MAX_LEASES} leases per call"));
    }
    let mut out = Vec::new();
    for e in list {
        let Some(o) = e.as_object() else { return Err("each lease must be an object".into()) };
        let kind = match o.get("kind").map(|k| k.as_str()) {
            None => LeaseKind::Analysis,
            Some(Some("analysis")) => LeaseKind::Analysis,
            Some(Some("solve")) => LeaseKind::Solve,
            _ => return Err("kind must be analysis or solve".into()),
        };
        let task = match o.get("task_id") {
            Some(serde_json::Value::Number(n)) => n.as_u64().filter(|&t| t > 0 && t < (1u64 << 63)),
            _ => None,
        };
        let Some(task) = task else { return Err("invalid task_id".into()) };
        let token = match o.get("lease_token") {
            None => String::new(),
            Some(serde_json::Value::String(s)) => s.clone(),
            _ => return Err("lease_token must be a string".into()),
        };
        let owner = match o.get("machine") {
            None => caller.to_string(),
            Some(serde_json::Value::String(s)) => s.chars().take(64).collect(),
            _ => return Err("machine must be a string".into()),
        };
        out.push(ReleaseEntry { kind, task, token, owner });
    }
    Ok(out)
}

// ------------------------------------------------------------ writer side

impl QueueState {
    /// Q-2.32..Q-2.34.
    pub(crate) fn heartbeat(&mut self, r: &HeartbeatReq) -> HeartbeatOut {
        self.touch_worker(r.now, &r.account, &r.machine, &r.telemetry);
        if let Some(id) = r.task {
            // D8: an exact, non-empty token is required (no tokenless allowance).
            let ok = self
                .tasks
                .get(&id)
                .map(|t| t.state == TState::Leased && t.machine == r.machine && !t.token.is_empty() && t.token == r.token)
                .unwrap_or(false);
            if !ok {
                return HeartbeatOut::StaleLease;
            }
            let now = r.now;
            self.update_task(id, |t| t.heartbeat_at = Some(now));
        }
        let p = self.ping_mut(&r.machine, &r.account);
        p.current_task = r.task;
        p.last_seen = r.now;
        p.last_nps = if r.task.is_some() { r.nps.min(MAX_REPORTED_NPS) } else { 0 };
        p.nps_updated = if r.task.is_some() { Some(r.now) } else { None };
        HeartbeatOut::Ok { machine: r.machine.clone(), current: r.task }
    }

    /// Q-2.38..Q-2.45.
    pub(crate) fn submit(&mut self, r: &SubmitReq, env: &dyn QueueEnv) -> SubmitOut {
        let Some(t) = self.tasks.get(&r.task) else { return SubmitOut::UnknownTask };
        if !matches!(t.state, TState::Leased | TState::Completed) {
            return SubmitOut::NotLeased;
        }
        if r.machine.is_empty() || r.machine != t.machine {
            return SubmitOut::NotYourLease;
        }
        // D8: a lease with no token is never a proof of possession.
        if t.token.is_empty() || t.token != r.token {
            return SubmitOut::StaleLease;
        }
        if t.state == TState::Completed {
            return SubmitOut::Dup;
        }
        // Q-2.40: only now the gate, so nobody pushes another's lease back.
        if r.gate_rejects {
            let id = r.task;
            self.update_task(id, |t| t.requeue());
            if let Some(p) = self.ping_mut_existing(&r.machine, &r.account) {
                p.current_task = None;
            }
            if env.status(self.tasks[&id].pos) != av2_model::Status::Unknown {
                self.absorb(&[id], r.now);
            }
            return SubmitOut::EngineRefused;
        }
        // Q-2.41 claim; the writer is the serialisation point, so the CAS
        // always wins here.  Q-2.42 clamp with the authoritative budget.
        let now = r.now;
        let nodes = r.nodes.min(t.budget.saturating_mul(2));
        self.update_task(r.task, |t| {
            t.state = TState::Completed;
            t.machine = r.machine.clone();
            t.completed = Some(now);
            t.nodes_searched = nodes;
            t.elapsed = r.elapsed;
            t.delivered_by = r.account.clone();
            t.engine_sha = r.engine_sha.clone();
            t.net_sha = r.net_sha.clone();
        });
        let t = &self.tasks[&r.task];
        let claim = Claim {
            task: t.id,
            pos: t.pos,
            budget: t.budget,
            multipv: t.multipv,
            source: t.source,
            arm: t.arm.clone(),
            requested_by: t.requested_by.clone(),
            also: t.also.clone(),
            then_verify: t.then_verify,
            lines: r.lines.clone(),
            nodes,
            elapsed: r.elapsed,
            machine: r.machine.clone(),
            username: r.account.clone(),
            tb_wdl: r.tb_wdl,
            tb_dtz: r.tb_dtz,
            restricted: r.restricted,
            engine_sha: r.engine_sha.clone(),
            net_sha: r.net_sha.clone(),
        };
        // Q-2.45: only an existing ping of (machine, account) is stamped.
        if let Some(p) = self.ping_mut_existing(&r.machine, &r.account) {
            p.tasks_done += 1;
            p.last_seen = now;
            p.last_result_at = Some(now);
            p.current_task = None;
            if nodes > 0 && r.elapsed > 0.0 {
                p.last_nps = py_round(nodes as f64 / r.elapsed).min(MAX_REPORTED_NPS as f64) as u64;
                p.nps_updated = Some(now);
            }
        }
        SubmitOut::Accepted { claim: Box::new(claim) }
    }

    /// Q-2.48..Q-2.50.
    pub(crate) fn release(&mut self, r: &ReleaseReq, env: &dyn QueueEnv) -> usize {
        let caller = machine_base(&r.machine);
        let mut kept: Vec<ReleaseEntry> =
            r.entries.iter().filter(|e| !e.token.is_empty() && !caller.is_empty() && machine_base(&e.owner) == caller).cloned().collect();
        kept.sort();
        let mut released_analysis = Vec::new();
        let mut released = 0;
        for e in &kept {
            match e.kind {
                LeaseKind::Analysis => {
                    let ok = self
                        .tasks
                        .get(&e.task)
                        .map(|t| t.state == TState::Leased && t.machine == e.owner && t.token == e.token)
                        .unwrap_or(false);
                    if ok {
                        self.update_task(e.task, |t| t.requeue());
                        released_analysis.push((e.task, e.owner.clone()));
                        released += 1;
                    }
                }
                LeaseKind::Solve => {
                    let ok = self
                        .solves
                        .get(&e.task)
                        .map(|s| s.state == SolveState::Leased && s.machine == e.owner && s.token == e.token)
                        .unwrap_or(false);
                    if ok {
                        self.update_solve(e.task, |s| s.requeue());
                        released += 1;
                    }
                }
            }
        }
        if !released_analysis.is_empty() {
            self.absorb_requeued(r.now, env);
            for (id, owner) in released_analysis {
                if let Some(p) = self.pings.get(&(owner.clone(), r.account.clone()))
                    && p.current_task == Some(id)
                {
                    let p = self.ping_mut(&owner, &r.account);
                    p.current_task = None;
                    p.last_nps = 0;
                    p.nps_updated = None;
                }
            }
        }
        released
    }
}

/// Python `round()` (half to even) for non-negative values.
pub fn py_round(x: f64) -> f64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && (r as i64) % 2 != 0 { r - x.signum() } else { r }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_and_round() {
        assert_eq!(py_int(" 128000000 "), Some(128_000_000));
        assert_eq!(py_int("+5"), Some(5));
        assert_eq!(py_int("1_000"), Some(1000));
        assert_eq!(py_int("1.5"), None);
        assert_eq!(py_int(""), None);
        assert_eq!(py_round(2.5), 2.0);
        assert_eq!(py_round(3.5), 4.0);
        assert_eq!(py_round(400.0), 400.0);
    }

    #[test]
    fn max_budget_parsing() {
        // test_max_budget.py::BadValueTests
        assert_eq!(parse_max_budget(None), Ok(None));
        assert_eq!(parse_max_budget(Some("")), Ok(None));
        assert_eq!(parse_max_budget(Some("abc")), Err(()));
        assert_eq!(parse_max_budget(Some("0")), Err(()));
        assert_eq!(parse_max_budget(Some("-5")), Err(()));
        assert_eq!(parse_max_budget(Some("128000000")), Ok(Some(128_000_000)));
    }

    #[test]
    fn telemetry_parsing() {
        // tests.py::MachineVisibilityTests::test_reported_threads_are_capped_at_what_the_engine_can_use
        assert_eq!(parse_threads(Some("4096")), 1024);
        assert_eq!(parse_threads(Some("x")), 0);
        assert_eq!(parse_threads(Some("-3")), 0);
        assert_eq!(parse_worker_build(Some("2026111704")), Some(2026111704));
        assert_eq!(parse_worker_build(Some("0")), None);
        assert_eq!(parse_worker_build(Some("99999999999")), None);
        assert_eq!(parse_build_capability(Some("junk")), 0);
    }

    #[test]
    fn submit_scalars() {
        // tests.py::MachineVisibilityTests::test_non_finite_elapsed_is_safely_ignored
        assert_eq!(parse_elapsed(Some("nan")), 0.0);
        assert_eq!(parse_elapsed(Some("inf")), 0.0);
        assert_eq!(parse_elapsed(Some("90000")), 86_400.0);
        assert_eq!(parse_elapsed(Some("-1")), 0.0);
        assert_eq!(parse_elapsed(Some("2.5")), 2.5);
        // test_fencing.py::SubmitFencingTests::test_zero_reported_nodes_remain_zero
        assert_eq!(parse_nodes(Some("0")), 0);
        assert_eq!(parse_nodes(Some("1.5")), 0);
        assert_eq!(parse_nodes(Some("-7")), 0);
        assert_eq!(parse_tb_wdl(Some("")), Ok(None));
        assert_eq!(parse_tb_wdl(Some("2")), Ok(Some(2)));
        assert_eq!(parse_tb_wdl(Some("x")), Err(()));
        assert_eq!(parse_tb_dtz(Some("2000")), None);
        assert_eq!(parse_tb_dtz(Some("x")), None);
        assert_eq!(parse_tb_dtz(Some("-12")), Some(-12));
    }

    #[test]
    fn sha_cleaning() {
        // test_proof_manager.py::SubmitProvenanceTests::test_garbage_provenance_is_dropped_not_stored
        let good = "AB".repeat(32);
        assert_eq!(sha_field(&format!("  {good} ")), good.to_lowercase());
        assert_eq!(sha_field("xyz"), "");
        assert_eq!(sha_field(&"g".repeat(64)), "");
    }

    #[test]
    fn lines_validation() {
        assert!(validate_lines(r#"[{"move":"e2e4","eval_cp":12,"pv":["e2e4"]}]"#).is_ok());
        // test_fencing.py::SubmitFencingTests::test_oversized_pv_is_rejected_before_claiming_the_task
        let pv: Vec<String> = (0..513).map(|_| "e2e4".to_string()).collect();
        let raw = serde_json::json!([{"move": "e2e4", "pv": pv}]).to_string();
        assert_eq!(validate_lines(&raw).unwrap_err(), "invalid or excessively long PV");
        assert_eq!(validate_lines(r#"[{"move":"e2e4","eval_cp":true}]"#).unwrap_err(), "invalid eval_cp");
        assert_eq!(validate_lines(r#"[{"move":""}]"#).unwrap_err(), "invalid root move");
        assert_eq!(validate_lines(r#"[1]"#).unwrap_err(), "each line must be an object");
        let many = serde_json::Value::Array((0..33).map(|_| serde_json::json!({"move": "a2a3"})).collect()).to_string();
        assert_eq!(validate_lines(&many).unwrap_err(), "lines must be a list with at most 32 entries");
        assert!(validate_lines("not json").is_err());
    }

    #[test]
    fn release_parsing() {
        // test_lease_release.py::ReleaseRefusalTests
        assert_eq!(parse_release(None, "m").unwrap(), vec![]);
        assert_eq!(parse_release(Some("{"), "m").unwrap_err(), "leases is not JSON");
        assert_eq!(parse_release(Some("{}"), "m").unwrap_err(), "leases must be a list");
        let many = serde_json::Value::Array((1..=34).map(|i| serde_json::json!({"task_id": i, "lease_token": "t"})).collect());
        assert!(parse_release(Some(&many.to_string()), "m").is_err());
        let at_limit = serde_json::Value::Array((1..=33).map(|i| serde_json::json!({"task_id": i, "lease_token": "t"})).collect());
        assert_eq!(parse_release(Some(&at_limit.to_string()), "m").unwrap().len(), 33);
        assert!(parse_release(Some(r#"[{"task_id": true}]"#), "m").is_err());
        assert!(parse_release(Some(r#"[{"task_id": 1, "kind": "x"}]"#), "m").is_err());
        let e = parse_release(Some(r#"[{"task_id": 7, "lease_token": "t", "kind": "solve"}]"#), "box#1").unwrap();
        assert_eq!(e[0], ReleaseEntry { kind: LeaseKind::Solve, task: 7, token: "t".into(), owner: "box#1".into() });
    }
}
