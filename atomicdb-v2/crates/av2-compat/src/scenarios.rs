//! Worker protocol scenarios: lease, heartbeat, submit and release, run
//! against a base URL and checked against v1's answers (contract, section 2).
//!
//! They change state (they lease tasks, submit analysis and seed positions
//! through `api/request`), so they run only against a test instance: a base
//! URL that points at production is refused (`http::is_production`).
//!
//! Each scenario leases under its own machine name, `<prefix>-<scenario>`, so
//! that one live lease per machine never makes two scenarios collide, and
//! hands back whatever it still holds when it ends, passed or not.

use std::collections::BTreeMap;

use serde_json::json;

use crate::diff::{self, Mode, Options};
use crate::http::{Request, Response, Transport};
use crate::json::{self, Json};
use crate::rules::EndpointRules;

/// The engine gate's refusal (`engine_policy.REJECTION_DETAIL`).
pub const REJECTION_DETAIL: &str = "AtomicDB only takes analysis from the reference engine. Start the worker without --engine and it downloads the reference build; to get your own build accepted, ask on the Belzedar Labs Discord.";

/// Positions after one white move: legal, cheap, and `e7e6` is legal in all.
pub const SEED_FENS: &[&str] = &[
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1",
    "rnbqkbnr/pppppppp/8/8/8/2N5/PPPPPPPP/R1BQKBNR b KQkq - 1 1",
    "rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/8/4P3/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/8/3P4/PPP1PPPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/8/6P1/PPPPPP1P/RNBQKBNR b KQkq - 0 1",
];

/// The keys of a leased task, in v1's order.
pub const LEASE_TASK_KEYS: &[&str] =
    &["id", "fen", "budget_nodes", "multipv", "searchmoves", "lease_token", "source", "requested_by", "line"];

/// Who the scenarios run as, and how.
#[derive(Clone, Debug)]
pub struct Settings {
    pub username: String,
    pub password: String,
    pub machine_prefix: String,
    pub worker_build: u64,
    /// The test server enforces the engine gate (a policy file with
    /// `"mode": "enforce"`): run the `engine-not-accepted` scenario.
    pub gate_enforced: bool,
    /// `engine_sha` sent with every normal submit (a reference binary's
    /// sha256 when the gate is on; empty otherwise, as a worker with
    /// `--engine` and no provenance sends).
    pub engine_sha: String,
}

/// One call of a scenario, for the transcript.
#[derive(Clone, Debug, serde::Serialize)]
pub struct Step {
    pub scenario: String,
    pub step: String,
    pub method: String,
    pub path: String,
    pub form: BTreeMap<String, String>,
    pub status: u16,
    pub content_type: Option<String>,
    pub body: String,
}

/// What one scenario came to.
#[derive(Clone, Debug)]
pub struct Verdict {
    pub scenario: &'static str,
    pub passed: bool,
    pub skipped: bool,
    pub message: String,
    pub steps: usize,
}

struct Leased {
    id: i64,
    token: String,
    fen: String,
    searchmoves: Vec<String>,
}

struct Run<'a> {
    transport: &'a mut dyn Transport,
    settings: &'a Settings,
    scenario: &'static str,
    machine: String,
    transcript: &'a mut Vec<Step>,
    tokens: BTreeMap<String, String>,
    held: Vec<(i64, String)>,
    steps: usize,
}

type Outcome = Result<(), String>;

impl Run<'_> {
    fn auth(&self, machine: &str) -> Vec<(String, String)> {
        vec![
            ("username".into(), self.settings.username.clone()),
            ("password".into(), self.settings.password.clone()),
            ("machine".into(), machine.into()),
            ("threads".into(), "1".into()),
            ("hash".into(), "16".into()),
            ("tb".into(), "0".into()),
            ("os".into(), "av2-compat".into()),
            ("worker_build".into(), self.settings.worker_build.to_string()),
        ]
    }

    fn redact(&mut self, form: &[(String, String)]) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();
        for (name, value) in form {
            let mut shown = match name.as_str() {
                "password" => "<redacted>".to_string(),
                "lease_token" => self.token_name(value),
                _ => value.clone(),
            };
            // Tokens inside other fields too (the `leases` list of a release).
            for (token, token_name) in &self.tokens {
                shown = shown.replace(token.as_str(), token_name);
            }
            out.insert(name.clone(), shown);
        }
        out
    }

    fn token_name(&mut self, value: &str) -> String {
        if value.is_empty() {
            return String::new();
        }
        let next = format!("<token:{}>", self.tokens.len() + 1);
        self.tokens.entry(value.to_string()).or_insert(next).clone()
    }

    fn call(&mut self, step: &str, request: Request) -> Result<(Response, Option<Json>), String> {
        let response = self.transport.send(&request).map_err(|e| format!("{step}: {e}"))?;
        self.steps += 1;
        let text = response.text();
        let mut shown = text.clone();
        for (token, name) in &self.tokens {
            shown = shown.replace(token, name);
        }
        let form = request.form.clone().unwrap_or_default();
        let form = self.redact(&form);
        // Name tokens seen for the first time in an answer, then redact.
        let parsed = json::parse(&text).ok();
        if let Some(tasks) = parsed.as_ref().and_then(|j| j.get("tasks")).and_then(Json::as_array) {
            for task in tasks {
                if let Some(token) = task.get("lease_token").and_then(Json::as_str) {
                    let name = self.token_name(token);
                    shown = shown.replace(token, &name);
                }
            }
        }
        self.transcript.push(Step {
            scenario: self.scenario.to_string(),
            step: step.to_string(),
            method: request.method.clone(),
            path: request.path.clone(),
            form,
            status: response.status,
            content_type: response.headers.get("content-type").cloned(),
            body: shown,
        });
        Ok((response, parsed))
    }

    fn post(&mut self, step: &str, path: &str, form: Vec<(String, String)>) -> Result<(Response, Option<Json>), String> {
        self.call(step, Request::post(path, form))
    }

    fn with_auth(&self, extra: &[(&str, &str)]) -> Vec<(String, String)> {
        let mut form = self.auth(&self.machine);
        for (name, value) in extra {
            form.retain(|(k, _)| k != name);
            form.push(((*name).to_string(), (*value).to_string()));
        }
        form
    }

    /// The answer must be `status` and exactly `body` (key order included).
    fn expect_exact(&mut self, step: &str, response: &Response, status: u16, body: &str) -> Outcome {
        let expected = json::parse(body).map_err(|e| format!("{step}: bad expectation {body}: {e}"))?;
        let actual = json::parse(&response.text()).map_err(|e| format!("{step}: answer is not JSON ({e}): {}", response.text()))?;
        let mut problems: Vec<String> = Vec::new();
        if response.status != status {
            problems.push(format!("status {} (expected {status})", response.status));
        }
        let rules = EndpointRules::default();
        let options = Options { mode: Mode::Snapshot, strict_order: true };
        problems.extend(diff::compare(&rules, options, &expected, &actual).iter().map(|d| d.to_string()));
        if problems.is_empty() { Ok(()) } else { Err(format!("{step}: {} -> {}", problems.join("; "), response.text())) }
    }

    fn expect_error(&mut self, step: &str, response: &Response, status: u16, error: &str) -> Outcome {
        self.expect_exact(step, response, status, &json!({ "error": error }).to_string())
    }

    fn expect_error_prefix(&mut self, step: &str, response: &Response, status: u16, prefix: &str) -> Outcome {
        let parsed = json::parse(&response.text()).ok();
        let error = parsed.as_ref().and_then(|j| j.get("error")).and_then(Json::as_str).unwrap_or("");
        if response.status == status && error.starts_with(prefix) && keys(parsed.as_ref()) == ["error"] {
            Ok(())
        } else {
            Err(format!("{step}: expected {status} with an error starting {prefix:?}, got {} {}", response.status, response.text()))
        }
    }

    /// Queue something to lease: an `api/request` for one of the seeds.
    fn seed(&mut self, index: usize) -> Outcome {
        let fen = SEED_FENS[index % SEED_FENS.len()];
        let form = vec![
            ("fen".to_string(), fen.to_string()),
            ("username".to_string(), self.settings.username.clone()),
            ("password".to_string(), self.settings.password.clone()),
            ("priority".to_string(), "normal".to_string()),
        ];
        let (response, parsed) = self.post("seed request", "/atomicdb/api/request", form)?;
        if response.status != 200 {
            return Err(format!("seed request: {} {}", response.status, response.text()));
        }
        let status = parsed.as_ref().and_then(|j| j.get("status")).and_then(Json::as_str).unwrap_or("");
        if status.is_empty() {
            return Err(format!("seed request: no status in {}", response.text()));
        }
        Ok(())
    }

    /// Lease one task under this scenario's machine.
    fn lease(&mut self, step: &str, session: &str) -> Result<Leased, String> {
        for attempt in 0..3 {
            let form = self.with_auth(&[("lease_session", session)]);
            let (response, parsed) = self.post(step, "/atomicdb/api/lease", form)?;
            if response.status != 200 {
                return Err(format!("{step}: {} {}", response.status, response.text()));
            }
            let tasks = parsed
                .as_ref()
                .and_then(|j| j.get("tasks"))
                .and_then(Json::as_array)
                .ok_or_else(|| format!("{step}: no tasks list: {}", response.text()))?;
            if tasks.len() > 1 {
                return Err(format!("{step}: {} tasks in one lease (v1 hands out one)", tasks.len()));
            }
            let Some(task) = tasks.first() else {
                if attempt < 2 {
                    std::thread::sleep(std::time::Duration::from_secs(2));
                    continue;
                }
                return Err(format!("{step}: nothing to lease after seeding a request"));
            };
            let order = keys(Some(task));
            if order != LEASE_TASK_KEYS {
                return Err(format!("{step}: task keys {order:?}, v1 has {LEASE_TASK_KEYS:?}"));
            }
            let id = task.get("id").and_then(Json::as_i64).ok_or_else(|| format!("{step}: id is not an integer"))?;
            let token = task.get("lease_token").and_then(Json::as_str).unwrap_or("").to_string();
            if token.is_empty() {
                return Err(format!("{step}: empty lease_token for build {}", self.settings.worker_build));
            }
            let fen = task.get("fen").and_then(Json::as_str).unwrap_or("").to_string();
            for (name, kind) in [
                ("budget_nodes", json::Kind::Int),
                ("multipv", json::Kind::Int),
                ("searchmoves", json::Kind::Arr),
                ("source", json::Kind::Str),
                ("requested_by", json::Kind::Str),
                ("line", json::Kind::Str),
            ] {
                let found = task.get(name).map(Json::kind);
                if found != Some(kind) {
                    return Err(format!("{step}: {name} is {found:?}, expected {}", kind.name()));
                }
            }
            let searchmoves = task
                .get("searchmoves")
                .and_then(Json::as_array)
                .unwrap_or(&[])
                .iter()
                .filter_map(|m| m.as_str().map(String::from))
                .collect();
            self.held.push((id, token.clone()));
            return Ok(Leased { id, token, fen, searchmoves });
        }
        unreachable!()
    }

    /// A line the server can store for `task`: a legal move when one is known.
    fn line_for(&mut self, task: &Leased) -> String {
        let mut uci = task.searchmoves.first().cloned();
        if uci.is_none() {
            let request = Request {
                method: "GET".into(),
                path: "/atomicdb/api/query".into(),
                query: vec![("fen".into(), task.fen.clone())],
                ..Request::default()
            };
            if let Ok((_, Some(body))) = self.call("query the leased position", request) {
                uci = body
                    .get("moves")
                    .and_then(Json::as_array)
                    .and_then(|m| m.first())
                    .and_then(|m| m.get("uci"))
                    .and_then(Json::as_str)
                    .map(String::from);
            }
        }
        if uci.is_none() && SEED_FENS.iter().any(|f| f.split(' ').take(4).eq(task.fen.split(' ').take(4))) {
            uci = Some("e7e6".into());
        }
        let uci = uci.unwrap_or_else(|| "e2e4".into());
        json!([{"move": uci, "eval_cp": 0, "mate": null, "pv": [uci],
                "raw": format!("info depth 1 seldepth 1 multipv 1 score cp 0 nodes 1 nps 1 time 1 pv {uci}")}])
        .to_string()
    }

    fn submit_form(&self, task: &Leased, lines: &str, extra: &[(&str, &str)]) -> Vec<(String, String)> {
        let id = task.id.to_string();
        let mut base: Vec<(&str, &str)> = vec![
            ("task_id", &id),
            ("lines", lines),
            ("elapsed", "0.50"),
            ("nodes", "1"),
            ("lease_token", &task.token),
            ("searchmoves", ""),
            ("engine_sha", &self.settings.engine_sha),
        ];
        for (name, value) in extra {
            base.retain(|(k, _)| k != name);
            base.push((name, value));
        }
        self.with_auth(&base)
    }

    fn release(&mut self, step: &str, entries: serde_json::Value) -> Result<(Response, Option<Json>), String> {
        let leases = entries.to_string();
        let form = self.with_auth(&[("leases", &leases)]);
        self.post(step, "/atomicdb/api/release", form)
    }

    fn release_task(&mut self, step: &str, task: &Leased, expected: u64) -> Outcome {
        let (response, _) =
            self.release(step, json!([{"kind": "analysis", "task_id": task.id, "lease_token": task.token, "machine": self.machine}]))?;
        self.expect_exact(step, &response, 200, &ordered(&[("ok", json!(true)), ("released", json!(expected))]))?;
        self.held.retain(|(id, _)| *id != task.id);
        Ok(())
    }

    /// Hand back whatever is still held; never fails the scenario.
    fn cleanup(&mut self) {
        if self.held.is_empty() {
            return;
        }
        let entries: Vec<serde_json::Value> = self
            .held
            .iter()
            .map(|(id, token)| json!({"kind": "analysis", "task_id": id, "lease_token": token, "machine": self.machine}))
            .collect();
        let _ = self.release("cleanup", serde_json::Value::Array(entries));
        self.held.clear();
    }
}

/// A JSON object with its keys in the order given (`json!` sorts them, and
/// v1's key order is part of what is checked).
fn ordered(pairs: &[(&str, serde_json::Value)]) -> String {
    let fields: Vec<String> = pairs.iter().map(|(k, v)| format!("{}: {v}", serde_json::Value::String((*k).to_string()))).collect();
    format!("{{{}}}", fields.join(", "))
}

fn keys(value: Option<&Json>) -> Vec<&str> {
    match value {
        Some(Json::Obj(fields)) => fields.iter().map(|(k, _)| k.as_str()).collect(),
        _ => Vec::new(),
    }
}

fn lease_heartbeat_submit(run: &mut Run) -> Outcome {
    run.seed(0)?;
    let task = run.lease("lease", "session-a")?;
    let replay = run.lease("lease again with the same session (replay)", "session-a")?;
    run.held.pop();
    if (replay.id, &replay.token) != (task.id, &task.token) {
        return Err(format!("replay gave task {} instead of {}", replay.id, task.id));
    }
    let form = run.with_auth(&[("lease_session", "session-b")]);
    let (response, _) = run.post("lease with another session while the lease is alive", "/atomicdb/api/lease", form)?;
    run.expect_exact("lease with another session while the lease is alive", &response, 200, r#"{"tasks": []}"#)?;
    let id = task.id.to_string();
    let form = run.with_auth(&[("task_id", &id), ("lease_token", &task.token), ("nps", "1000")]);
    let (response, _) = run.post("heartbeat", "/atomicdb/api/heartbeat", form)?;
    let machine = run.machine.clone();
    run.expect_exact(
        "heartbeat",
        &response,
        200,
        &ordered(&[("ok", json!(true)), ("machine", json!(machine)), ("current_task_id", json!(task.id))]),
    )?;
    let form = run.with_auth(&[("nps", "0")]);
    let (response, _) = run.post("heartbeat without a task", "/atomicdb/api/heartbeat", form)?;
    run.expect_exact(
        "heartbeat without a task",
        &response,
        200,
        &ordered(&[("ok", json!(true)), ("machine", json!(machine)), ("current_task_id", json!(null))]),
    )?;
    let lines = run.line_for(&task);
    let form = run.submit_form(&task, &lines, &[]);
    let (response, parsed) = run.post("submit", "/atomicdb/api/submit", form.clone())?;
    let summary = parsed.as_ref().and_then(|j| j.get("summary"));
    let summary_keys = keys(summary);
    let ok = response.status == 200
        && keys(parsed.as_ref()) == ["ok", "summary"]
        && parsed.as_ref().and_then(|j| j.get("ok")) == Some(&Json::Bool(true))
        && summary_keys == ["queued", "ingest_job", "submit_timing_seconds"]
        && summary.and_then(|s| s.get("queued")) == Some(&Json::Bool(true))
        && summary.and_then(|s| s.get("ingest_job")).map(Json::kind) == Some(json::Kind::Int)
        && keys(summary.and_then(|s| s.get("submit_timing_seconds"))) == ["transaction", "apply", "total"];
    if !ok {
        return Err(format!("submit: {} {}", response.status, response.text()));
    }
    run.held.retain(|(i, _)| *i != task.id);
    let (response, _) = run.post("submit again (lost answer)", "/atomicdb/api/submit", form)?;
    run.expect_exact("submit again (lost answer)", &response, 200, r#"{"ok": true, "dup": true}"#)?;
    let form = run.with_auth(&[("task_id", &id), ("lease_token", &task.token), ("nps", "1000")]);
    let (response, _) = run.post("heartbeat after submit", "/atomicdb/api/heartbeat", form)?;
    run.expect_error("heartbeat after submit", &response, 409, "stale-lease")?;
    run.release_task("release after submit", &task, 0)
}

fn stale_lease(run: &mut Run) -> Outcome {
    run.seed(1)?;
    let task = run.lease("lease", "session-a")?;
    let id = task.id.to_string();
    let wrong = format!("{}x", task.token);
    let form = run.with_auth(&[("task_id", &id), ("lease_token", &wrong), ("nps", "10")]);
    let (response, _) = run.post("heartbeat with a wrong token", "/atomicdb/api/heartbeat", form)?;
    run.expect_error("heartbeat with a wrong token", &response, 409, "stale-lease")?;
    let lines = run.line_for(&task);
    let form = run.submit_form(&task, &lines, &[("lease_token", &wrong)]);
    let (response, _) = run.post("submit with a wrong token", "/atomicdb/api/submit", form)?;
    run.expect_error("submit with a wrong token", &response, 409, "stale-lease")?;
    let form = run.with_auth(&[("task_id", &id), ("lease_token", &task.token), ("nps", "10")]);
    let (response, _) = run.post("heartbeat with the right token", "/atomicdb/api/heartbeat", form)?;
    let machine = run.machine.clone();
    run.expect_exact(
        "heartbeat with the right token",
        &response,
        200,
        &ordered(&[("ok", json!(true)), ("machine", json!(machine)), ("current_task_id", json!(task.id))]),
    )?;
    let (response, _) = run.release("release with a wrong token", json!([{"task_id": task.id, "lease_token": wrong}]))?;
    run.expect_exact("release with a wrong token", &response, 200, r#"{"ok": true, "released": 0}"#)?;
    run.release_task("release", &task, 1)
}

fn not_your_lease(run: &mut Run) -> Outcome {
    run.seed(2)?;
    let task = run.lease("lease", "session-a")?;
    let lines = run.line_for(&task);
    let other = format!("{}-other", run.machine);
    let form = run.submit_form(&task, &lines, &[("machine", &other)]);
    let (response, _) = run.post("submit from another machine", "/atomicdb/api/submit", form)?;
    run.expect_error("submit from another machine", &response, 409, "not-your-lease")?;
    let form = run.submit_form(&task, &lines, &[("machine", "")]);
    let (response, _) = run.post("submit without a machine", "/atomicdb/api/submit", form)?;
    run.expect_error("submit without a machine", &response, 409, "not-your-lease")?;
    let id = task.id.to_string();
    let mut form = run.auth(&other);
    form.extend([("task_id".to_string(), id), ("lease_token".to_string(), task.token.clone()), ("nps".to_string(), "1".to_string())]);
    let (response, _) = run.post("heartbeat from another machine", "/atomicdb/api/heartbeat", form)?;
    run.expect_error("heartbeat from another machine", &response, 409, "stale-lease")?;
    let (response, _) = run.release(
        "release naming a foreign machine",
        json!([{"task_id": task.id, "lease_token": task.token, "machine": "someone-else-atomicdb"}]),
    )?;
    run.expect_exact("release naming a foreign machine", &response, 200, r#"{"ok": true, "released": 0}"#)?;
    run.release_task("release", &task, 1)
}

fn not_leased(run: &mut Run) -> Outcome {
    run.seed(3)?;
    let task = run.lease("lease", "session-a")?;
    run.release_task("release", &task, 1)?;
    let lines = run.line_for(&task);
    let form = run.submit_form(&task, &lines, &[]);
    let (response, _) = run.post("submit after release", "/atomicdb/api/submit", form)?;
    run.expect_error("submit after release", &response, 400, "not-leased")?;
    let id = task.id.to_string();
    let form = run.with_auth(&[("task_id", &id), ("lease_token", &task.token), ("nps", "1")]);
    let (response, _) = run.post("heartbeat after release", "/atomicdb/api/heartbeat", form)?;
    run.expect_error("heartbeat after release", &response, 409, "stale-lease")?;
    run.release_task("release again", &task, 0)
}

fn release_errors(run: &mut Run) -> Outcome {
    let (response, _) = run.call("release with GET", Request::get("/atomicdb/api/release"))?;
    run.expect_error("release with GET", &response, 405, "POST required")?;
    let mut bad = run.auth(&run.machine.clone());
    bad.retain(|(k, _)| k != "password");
    bad.push(("password".into(), "not-the-password".into()));
    bad.push(("leases".into(), "[]".into()));
    let (response, _) = run.post("release with bad credentials", "/atomicdb/api/release", bad)?;
    run.expect_error("release with bad credentials", &response, 403, "bad credentials")?;
    let many: Vec<serde_json::Value> = (1..=34).map(|i| json!({"task_id": i, "lease_token": "t"})).collect();
    let many = serde_json::Value::Array(many).to_string();
    for (step, leases, error) in [
        ("leases not JSON", "nope", "malformed: leases is not JSON"),
        ("leases not a list", "{}", "malformed: leases must be a list"),
        ("too many leases", many.as_str(), "malformed: at most 33 leases per call"),
        ("lease not an object", "[1]", "malformed: each lease must be an object"),
        ("unknown kind", r#"[{"kind": "x", "task_id": 1}]"#, "malformed: kind must be analysis or solve"),
        ("task id a boolean", r#"[{"task_id": true}]"#, "malformed: invalid task_id"),
        ("task id zero", r#"[{"task_id": 0}]"#, "malformed: invalid task_id"),
        ("token not a string", r#"[{"task_id": 1, "lease_token": 5}]"#, "malformed: lease_token and machine must be strings"),
    ] {
        let form = run.with_auth(&[("leases", leases)]);
        let (response, _) = run.post(step, "/atomicdb/api/release", form)?;
        run.expect_error(step, &response, 400, error)?;
    }
    let form = run.with_auth(&[("leases", r#"[{"task_id": 1, "lease_token": ""}]"#)]);
    let (response, _) = run.post("a tokenless entry is ignored", "/atomicdb/api/release", form)?;
    run.expect_exact("a tokenless entry is ignored", &response, 200, r#"{"ok": true, "released": 0}"#)?;
    let form = run.with_auth(&[]);
    let (response, _) = run.post("no leases field", "/atomicdb/api/release", form)?;
    run.expect_exact("no leases field", &response, 200, r#"{"ok": true, "released": 0}"#)
}

fn auth_and_validation(run: &mut Run) -> Outcome {
    let machine = run.machine.clone();
    let mut wrong = run.auth(&machine);
    wrong.retain(|(k, _)| k != "password");
    wrong.push(("password".into(), "not-the-password".into()));
    for (step, path) in [
        ("lease with bad credentials", "/atomicdb/api/lease"),
        ("heartbeat with bad credentials", "/atomicdb/api/heartbeat"),
        ("submit with bad credentials", "/atomicdb/api/submit"),
        ("solve acquire with bad credentials", "/atomicdb/api/solve/acquire"),
        ("solve heartbeat with bad credentials", "/atomicdb/api/solve/heartbeat"),
        ("solve submit with bad credentials", "/atomicdb/api/solve/submit"),
    ] {
        let (response, _) = run.post(step, path, wrong.clone())?;
        run.expect_error(step, &response, 403, "bad credentials")?;
    }
    for (step, path) in [("heartbeat with GET", "/atomicdb/api/heartbeat"), ("solve heartbeat with GET", "/atomicdb/api/solve/heartbeat")] {
        let (response, _) = run.call(step, Request::get(path))?;
        run.expect_error(step, &response, 405, "POST required")?;
    }
    let (response, _) = run.call("lease with GET (no credentials)", Request::get("/atomicdb/api/lease"))?;
    run.expect_error("lease with GET (no credentials)", &response, 403, "bad credentials")?;
    for value in ["abc", "0", "-5", "1.5"] {
        let step = format!("lease with max_budget={value}");
        let form = run.with_auth(&[("max_budget", value), ("lease_session", "never")]);
        let (response, _) = run.post(&step, "/atomicdb/api/lease", form)?;
        run.expect_error(&step, &response, 400, "max_budget must be a positive whole number of nodes")?;
    }
    let form = run.with_auth(&[("max_budget", "x")]);
    let (response, _) = run.post("solve acquire with a bad max_budget", "/atomicdb/api/solve/acquire", form)?;
    run.expect_error("solve acquire with a bad max_budget", &response, 400, "max_budget must be a positive whole number of nodes")?;
    let form = run.with_auth(&[("nps", "fast")]);
    let (response, _) = run.post("heartbeat with a bad nps", "/atomicdb/api/heartbeat", form)?;
    run.expect_error("heartbeat with a bad nps", &response, 400, "invalid nps")?;
    let form = run.with_auth(&[("task_id", "abc")]);
    let (response, _) = run.post("heartbeat with a bad task_id", "/atomicdb/api/heartbeat", form)?;
    run.expect_error("heartbeat with a bad task_id", &response, 400, "invalid task_id")?;
    let form = run.with_auth(&[("task_id", "2147483646"), ("lease_token", "x")]);
    let (response, _) = run.post("heartbeat for a task that does not exist", "/atomicdb/api/heartbeat", form)?;
    run.expect_error("heartbeat for a task that does not exist", &response, 409, "stale-lease")?;
    let form = run.with_auth(&[("task_id", "abc")]);
    let (response, _) = run.post("solve heartbeat with a bad task_id", "/atomicdb/api/solve/heartbeat", form)?;
    run.expect_error("solve heartbeat with a bad task_id", &response, 400, "invalid task_id")?;
    let form = run.with_auth(&[("task_id", "1"), ("outcome", "MAYBE")]);
    let (response, _) = run.post("solve submit with a bad outcome", "/atomicdb/api/solve/submit", form)?;
    run.expect_error("solve submit with a bad outcome", &response, 400, "malformed: outcome")?;
    let form = run.with_auth(&[("outcome", "PROVED")]);
    let (response, _) = run.post("solve submit without task_id", "/atomicdb/api/solve/submit", form)?;
    run.expect_error("solve submit without task_id", &response, 400, "malformed: task_id")?;
    let unknown = "2147483646";
    for (step, lines, error) in [
        ("lines not a list", "{}", "malformed: lines must be a list with at most 32 entries"),
        ("line not an object", "[1]", "malformed: each line must be an object"),
        ("empty root move", r#"[{"move": ""}]"#, "malformed: invalid root move"),
        ("eval_cp a boolean", r#"[{"move": "e2e4", "eval_cp": true}]"#, "malformed: invalid eval_cp"),
        ("mate a float", r#"[{"move": "e2e4", "mate": 1.5}]"#, "malformed: invalid mate"),
        ("pv not a list", r#"[{"move": "e2e4", "pv": "e2e4"}]"#, "malformed: invalid or excessively long PV"),
        ("raw not a string", r#"[{"move": "e2e4", "raw": 5}]"#, "malformed: raw line is too large"),
    ] {
        let form = run.with_auth(&[("task_id", unknown), ("lines", lines)]);
        let (response, _) = run.post(step, "/atomicdb/api/submit", form)?;
        run.expect_error(step, &response, 400, error)?;
    }
    let thirty_three = serde_json::Value::Array((0..33).map(|_| json!({"move": "e2e4"})).collect()).to_string();
    let form = run.with_auth(&[("task_id", unknown), ("lines", &thirty_three)]);
    let (response, _) = run.post("33 lines", "/atomicdb/api/submit", form)?;
    run.expect_error("33 lines", &response, 400, "malformed: lines must be a list with at most 32 entries")?;
    let form = run.with_auth(&[("task_id", unknown), ("lines", "not json")]);
    let (response, _) = run.post("lines not JSON", "/atomicdb/api/submit", form)?;
    run.expect_error_prefix("lines not JSON", &response, 400, "malformed: ")?;
    let form = run.with_auth(&[("lines", "[]")]);
    let (response, _) = run.post("submit without task_id", "/atomicdb/api/submit", form)?;
    run.expect_error_prefix("submit without task_id", &response, 400, "malformed: ")?;
    let form = run.with_auth(&[("task_id", unknown), ("lines", "[]"), ("tb_wdl", "two")]);
    let (response, _) = run.post("bad tb_wdl", "/atomicdb/api/submit", form)?;
    run.expect_error("bad tb_wdl", &response, 400, "malformed: invalid tb_wdl")?;
    let form = run.with_auth(&[("task_id", unknown), ("lines", "[]")]);
    let (response, _) = run.post("unknown task", "/atomicdb/api/submit", form)?;
    run.expect_error("unknown task", &response, 400, "malformed: unknown task")
}

fn engine_not_accepted(run: &mut Run) -> Outcome {
    run.seed(4)?;
    let task = run.lease("lease", "session-a")?;
    let lines = run.line_for(&task);
    let bogus = "0".repeat(64);
    let form = run.submit_form(&task, &lines, &[("engine_sha", &bogus)]);
    let (response, _) = run.post("submit from an engine the gate refuses", "/atomicdb/api/submit", form.clone())?;
    run.expect_exact(
        "submit from an engine the gate refuses",
        &response,
        403,
        &ordered(&[("error", json!("engine-not-accepted")), ("detail", json!(REJECTION_DETAIL))]),
    )?;
    run.held.retain(|(i, _)| *i != task.id);
    let (response, _) = run.post("the task went back to the queue", "/atomicdb/api/submit", form)?;
    run.expect_error("the task went back to the queue", &response, 400, "not-leased")?;
    let lines = run.line_for(&task);
    let form = run.submit_form(&task, &lines, &[("engine_sha", &bogus), ("tb_wdl", "1"), ("lines", "[]")]);
    let (response, _) = run.post("a tablebase result is never gated (but the lease is gone)", "/atomicdb/api/submit", form)?;
    run.expect_error("a tablebase result is never gated (but the lease is gone)", &response, 400, "not-leased")
}

fn solve_acquire(run: &mut Run) -> Outcome {
    let form = run.with_auth(&[("lease_session", "solve-a")]);
    let (response, parsed) = run.post("solve acquire", "/atomicdb/api/solve/acquire", form)?;
    let tasks = parsed.as_ref().and_then(|j| j.get("tasks")).and_then(Json::as_array);
    match tasks {
        Some([]) if response.status == 200 && keys(parsed.as_ref()) == ["tasks"] => Ok(()),
        Some([task]) if response.status == 200 => {
            let order = keys(Some(task));
            let want = ["id", "fen", "goal", "stage", "budget_nodes", "ruleset", "lease_token"];
            let (id, token) = (
                task.get("id").and_then(Json::as_i64).unwrap_or(0),
                task.get("lease_token").and_then(Json::as_str).unwrap_or("").to_string(),
            );
            let (response, _) = run.release("hand the proof back", json!([{"kind": "solve", "task_id": id, "lease_token": token}]))?;
            run.expect_exact("hand the proof back", &response, 200, r#"{"ok": true, "released": 1}"#)?;
            if order == want { Ok(()) } else { Err(format!("solve task keys {order:?}, v1 has {want:?}")) }
        }
        _ => Err(format!("solve acquire: {} {}", response.status, response.text())),
    }
}

type Scenario = fn(&mut Run) -> Outcome;

/// The scenarios and what each checks, in the order they run.
pub fn list() -> Vec<(&'static str, &'static str)> {
    SCENARIOS.iter().map(|(name, _, what)| (*name, *what)).collect()
}

const SCENARIOS: &[(&str, Scenario, &str)] = &[
    ("lease-heartbeat-submit", lease_heartbeat_submit, "lease, replay, heartbeat, submit, duplicate submit"),
    ("stale-lease", stale_lease, "wrong token on heartbeat, submit and release"),
    ("not-your-lease", not_your_lease, "another machine submits, beats or releases"),
    ("not-leased", not_leased, "submit and heartbeat after the lease was handed back"),
    ("release-errors", release_errors, "every refusal of api/release"),
    ("auth-and-validation", auth_and_validation, "credentials, methods and malformed fields"),
    ("engine-not-accepted", engine_not_accepted, "the engine gate (needs --gate-enforced)"),
    ("solve-acquire", solve_acquire, "the SOLVE lease shape"),
];

/// Run the scenarios named in `only` (all when empty).
pub fn run_all(transport: &mut dyn Transport, settings: &Settings, only: &[String], transcript: &mut Vec<Step>) -> Vec<Verdict> {
    let mut verdicts = Vec::new();
    for (name, scenario, _) in SCENARIOS {
        if !only.is_empty() && !only.iter().any(|o| o == name) {
            continue;
        }
        if *name == "engine-not-accepted" && !settings.gate_enforced {
            verdicts.push(Verdict {
                scenario: name,
                passed: true,
                skipped: true,
                message: "skipped: the server's engine gate is off (pass --gate-enforced when it is on)".into(),
                steps: 0,
            });
            continue;
        }
        let mut run = Run {
            transport: &mut *transport,
            settings,
            scenario: name,
            machine: format!("{}-{}", settings.machine_prefix, name),
            transcript: &mut *transcript,
            tokens: BTreeMap::new(),
            held: Vec::new(),
            steps: 0,
        };
        let result = scenario(&mut run);
        run.cleanup();
        let steps = run.steps;
        verdicts.push(match result {
            Ok(()) => Verdict { scenario: name, passed: true, skipped: false, message: "ok".into(), steps },
            Err(message) => Verdict { scenario: name, passed: false, skipped: false, message, steps },
        });
    }
    verdicts
}
