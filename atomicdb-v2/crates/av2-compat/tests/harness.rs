//! The harness against itself: the committed golden files must judge
//! themselves clean, and a replay through real HTTP must catch what changed.

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use av2_compat::diff::{Mode, Severity};
use av2_compat::golden::{self, Record};
use av2_compat::http::{HttpTransport, Request, Response, Transport};
use av2_compat::replay::{self, Judge};
use av2_compat::rules::Rules;
use av2_compat::scenarios::{self, Settings, Step};
use av2_compat::{defaults, json};

fn bundled_rules() -> Rules {
    Rules::load(Path::new(defaults::RULES)).expect("tests/golden/normalize.json loads")
}

const STRICT: Judge = Judge { mode: Mode::Snapshot, strict_order: true, bytes: true, through_nginx: true };

#[test]
fn every_rule_path_compiles_for_every_endpoint() {
    let rules = bundled_rules();
    for endpoint in
        ["query", "principal-variations", "backed-source", "frontier", "live-request", "roots", "map", "submit", "lease", "my-queue"]
    {
        rules.for_endpoint(endpoint).unwrap();
    }
}

#[test]
fn the_golden_recording_judges_itself_clean() {
    let path = Path::new(defaults::GOLDEN);
    if !path.exists() {
        eprintln!("no golden recording at {}; skipped", path.display());
        return;
    }
    let rules = bundled_rules();
    let records = golden::read_records(path).unwrap();
    assert!(records.len() >= 1000, "only {} records", records.len());
    let mut parsed = 0;
    for record in &records {
        if record.headers.get("content-type").is_some_and(|v| v.starts_with("application/json")) && !record.body.is_empty() {
            json::parse(&record.body).unwrap_or_else(|e| panic!("record {}: {e}", record.id));
            parsed += 1;
        }
        let echo = Response { status: record.status, headers: record.headers.clone(), body: record.body.clone().into_bytes() };
        let outcome = replay::judge(&rules, STRICT, record, &echo).unwrap();
        assert!(outcome.differences.is_empty(), "record {} differs from itself: {:?}", record.id, outcome.differences);
    }
    assert!(parsed > 900, "{parsed} JSON bodies");
}

#[test]
fn the_v1_shapes_parse() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/v1-shapes/calls.jsonl.gz");
    if !path.exists() {
        return;
    }
    let file = std::fs::File::open(&path).unwrap();
    let mut count = 0;
    for line in BufReader::new(flate2::read::GzDecoder::new(file)).lines() {
        let line = line.unwrap();
        let entry: serde_json::Value = serde_json::from_str(&line).unwrap();
        let body = entry["body"].as_str().unwrap();
        let content_type = entry["headers"]["Content-Type"].as_str().unwrap_or("");
        if content_type.starts_with("application/json") && !body.is_empty() {
            json::parse(body).unwrap_or_else(|e| panic!("{}: {e}: {body}", entry["test"]));
        }
        count += 1;
    }
    assert!(count > 1000, "{count} calls");
}

/// A one-request-per-connection HTTP server answering from a table.
fn serve(answers: BTreeMap<String, (u16, String)>) -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&seen);
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut first = String::new();
            if reader.read_line(&mut first).is_err() {
                continue;
            }
            let mut length = 0;
            loop {
                let mut header = String::new();
                reader.read_line(&mut header).unwrap();
                if header.trim().is_empty() {
                    break;
                }
                let lower = header.to_ascii_lowercase();
                if let Some(value) = lower.strip_prefix("content-length:") {
                    length = value.trim().parse().unwrap();
                }
            }
            let mut body = vec![0; length];
            reader.read_exact(&mut body).unwrap();
            let target = first.split_whitespace().take(2).collect::<Vec<_>>().join(" ");
            log.lock().unwrap().push(format!("{target} {}", String::from_utf8_lossy(&body)));
            let (status, text) = answers.get(&target).cloned().unwrap_or((404, "{\"error\": \"unknown position\"}".into()));
            let reply = format!(
                "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
                text.len()
            );
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    (base, seen)
}

#[allow(clippy::too_many_arguments)]
fn record(
    id: u64,
    endpoint: &str,
    method: &str,
    path: &str,
    query: &[(&str, &str)],
    form: Option<&[(&str, &str)]>,
    status: u16,
    body: &str,
) -> Record {
    let json = serde_json::json!({
        "id": id, "endpoint": endpoint, "method": method, "path": path,
        "query": query.iter().map(|(k, v)| [k, v]).collect::<Vec<_>>(),
        "form": form.map(|f| f.iter().map(|(k, v)| ((*k).to_string(), serde_json::Value::String((*v).to_string()))).collect::<serde_json::Map<_, _>>()),
        "status": status, "headers": {"content-type": "application/json"}, "body": body,
    });
    serde_json::from_value(json).unwrap()
}

#[test]
fn replay_over_http_catches_changes() {
    let query_body = r#"{"fen": "f", "key": "k", "score": 12, "moves": [{"uci": "e2e4", "score": 3}, {"uci": "d2d4", "score": 1}]}"#;
    let records = vec![
        record(0, "query", "GET", "/atomicdb/api/query", &[("fen", "8/8 w - - 0 1")], None, 200, query_body),
        record(1, "query", "GET", "/atomicdb/api/query", &[("fen", "bad")], None, 400, r#"{"error": "invalid fen"}"#),
        record(
            2,
            "live-request",
            "GET",
            "/atomicdb/api/live-request/k/",
            &[],
            None,
            200,
            r#"{"live": true, "text": "Waiting 3 min", "chip": "cold"}"#,
        ),
        record(3, "my-queue", "POST", "/atomicdb/api/my-queue", &[], Some(&[("username", "u")]), 403, r#"{"error": "bad credentials"}"#),
        record(4, "query", "GET", "/atomicdb/api/query", &[("fen", "x y")], None, 200, r#"{"a": 1, "b": 2}"#),
    ];
    let mut answers = BTreeMap::new();
    // 0: the score moved and the rows swapped; 1: same; 2: only volatile text changed;
    // 3: same; 4: same JSON, keys in another order.
    answers.insert(
        "GET /atomicdb/api/query?fen=8%2F8+w+-+-+0+1".into(),
        (200, r#"{"fen": "f", "key": "k", "score": 13, "moves": [{"uci": "d2d4", "score": 1}, {"uci": "e2e4", "score": 3}]}"#.into()),
    );
    answers.insert("GET /atomicdb/api/query?fen=bad".into(), (400, r#"{"error": "invalid fen"}"#.into()));
    answers.insert("GET /atomicdb/api/live-request/k/".into(), (200, r#"{"live": true, "text": "Waiting 9 min", "chip": "hot"}"#.into()));
    answers.insert("POST /atomicdb/api/my-queue".into(), (403, r#"{"error": "bad credentials"}"#.into()));
    answers.insert("GET /atomicdb/api/query?fen=x+y".into(), (200, r#"{"b": 2, "a": 1}"#.into()));
    let (base, seen) = serve(answers);
    let rules = bundled_rules();
    let mut transport = HttpTransport::new(&base, 0.0, Duration::from_secs(10));
    let mut verdicts = BTreeMap::new();
    let judge = Judge { mode: Mode::Snapshot, strict_order: true, bytes: false, through_nginx: false };
    let tallies = replay::replay(&rules, judge, &records, &mut transport, |r, v| {
        verdicts.insert(r.id, v.map(|o| o.differences.clone()).unwrap());
    });
    assert_eq!(seen.lock().unwrap().len(), 5);
    assert!(seen.lock().unwrap().iter().any(|s| s.starts_with("POST /atomicdb/api/my-queue username=u")));
    let whats = |id: u64| verdicts[&id].iter().map(|d| (d.path.clone(), d.what.clone(), d.severity)).collect::<Vec<_>>();
    assert_eq!(
        whats(0),
        vec![("$.score".into(), "value".into(), Severity::Failure), ("$.moves".into(), "row order".into(), Severity::Failure)]
    );
    assert!(whats(1).is_empty() && whats(2).is_empty() && whats(3).is_empty());
    assert_eq!(whats(4), vec![("$".into(), "key order".into(), Severity::Failure)]);
    assert_eq!(tallies["query"].failed, 2);
    assert_eq!(tallies["live-request"].passed, 1);

    // In live mode the moving values only need their types.
    let mut transport = HttpTransport::new(&base, 0.0, Duration::from_secs(10));
    let judge = Judge { mode: Mode::Live, strict_order: false, bytes: false, through_nginx: false };
    let outcome = {
        let mut out = None;
        replay::replay(&rules, judge, &records[..1], &mut transport, |_, v| out = Some(v.unwrap().differences.clone()));
        out.unwrap()
    };
    assert!(outcome.is_empty(), "{outcome:?}");
}

#[test]
fn an_accepted_alternative_is_not_a_failure() {
    let rules = bundled_rules();
    let recorded = record(9, "my-queue", "PUT", "/atomicdb/api/my-queue/", &[], None, 401, r#"{"error": "login required"}"#);
    let repo = Response {
        status: 405,
        headers: [("content-type".to_string(), "application/json".to_string())].into(),
        body: br#"{"error": "GET or POST only"}"#.to_vec(),
    };
    let judge = Judge { mode: Mode::Snapshot, strict_order: true, bytes: true, through_nginx: false };
    let outcome = replay::judge(&rules, judge, &recorded, &repo).unwrap();
    assert!(outcome.differences.is_empty() && outcome.alternative.is_some());
    let wrong = Response { status: 500, ..repo };
    assert_eq!(replay::judge(&rules, judge, &recorded, &wrong).unwrap().worst(), Some(Severity::Failure));
}

/// A transport that plays v1's part from a script: (path, expected form
/// fields) -> answer.  Answers v1's bodies for the not-leased scenario.
struct Scripted {
    replies: Vec<(&'static str, u16, String)>,
    calls: Vec<Request>,
}

impl Transport for Scripted {
    fn send(&mut self, request: &Request) -> Result<Response, String> {
        self.calls.push(request.clone());
        let index = self.calls.len() - 1;
        let (path, status, body) = self.replies.get(index).cloned().ok_or_else(|| format!("unexpected call {index}: {}", request.path))?;
        assert_eq!(path, request.path, "call {index}");
        Ok(Response { status, headers: [("content-type".to_string(), "application/json".to_string())].into(), body: body.into_bytes() })
    }
}

fn settings() -> Settings {
    Settings {
        username: "w".into(),
        password: "secret".into(),
        machine_prefix: "t".into(),
        worker_build: 2026112501,
        gate_enforced: false,
        decision_d8: false,
        engine_sha: String::new(),
    }
}

const TASK: &str = r#"{"tasks": [{"id": 5, "fen": "rnbqkbnr/pppppppp/8/8/8/2N5/PPPPPPPP/R1BQKBNR b KQkq - 0 1", "budget_nodes": 128000000, "multipv": 4, "searchmoves": [], "lease_token": "TOKEN-abcdefghijklmnopqrstuvwxyz", "source": "USER", "requested_by": "w", "line": "1. Nc3"}]}"#;

#[test]
fn the_not_leased_scenario_follows_v1() {
    let mut transport = Scripted {
        replies: vec![
            ("/atomicdb/api/request", 200, r#"{"status": "queued"}"#.into()),
            ("/atomicdb/api/lease", 200, TASK.into()),
            ("/atomicdb/api/release", 200, r#"{"ok": true, "released": 1}"#.into()),
            ("/atomicdb/api/query", 404, r#"{"error": "unknown position"}"#.into()),
            ("/atomicdb/api/submit", 400, r#"{"error": "not-leased"}"#.into()),
            ("/atomicdb/api/heartbeat", 409, r#"{"error": "stale-lease"}"#.into()),
            ("/atomicdb/api/release", 200, r#"{"ok": true, "released": 0}"#.into()),
        ],
        calls: Vec::new(),
    };
    let mut transcript: Vec<Step> = Vec::new();
    let verdicts = scenarios::run_all(&mut transport, &settings(), &["not-leased".into()], &mut transcript);
    assert_eq!(verdicts.len(), 1);
    assert!(verdicts[0].passed, "{}", verdicts[0].message);
    // The submit names the leased task with its token, from its own machine,
    // and the transcript never shows the password or the token.
    let submit = &transport.calls[4];
    let form: BTreeMap<_, _> = submit.form.clone().unwrap().into_iter().collect();
    assert_eq!(form["task_id"], "5");
    assert_eq!(form["lease_token"], "TOKEN-abcdefghijklmnopqrstuvwxyz");
    assert_eq!(form["machine"], "t-not-leased");
    assert!(form["lines"].contains("e7e6"), "a seed position gets a legal move: {}", form["lines"]);
    let text = serde_json::to_string(&transcript).unwrap();
    assert!(!text.contains("secret") && !text.contains("TOKEN-abc"), "{text}");
    assert!(text.contains("<token:1>"));
}

#[test]
fn a_scenario_fails_on_a_wrong_error_word() {
    let mut transport = Scripted {
        replies: vec![
            ("/atomicdb/api/request", 200, r#"{"status": "queued"}"#.into()),
            ("/atomicdb/api/lease", 200, TASK.into()),
            ("/atomicdb/api/release", 200, r#"{"ok": true, "released": 1}"#.into()),
            ("/atomicdb/api/query", 404, r#"{"error": "unknown position"}"#.into()),
            ("/atomicdb/api/submit", 409, r#"{"error": "not-your-lease"}"#.into()),
            // cleanup: nothing is held any more, so no further call
        ],
        calls: Vec::new(),
    };
    let mut transcript = Vec::new();
    let verdicts = scenarios::run_all(&mut transport, &settings(), &["not-leased".into()], &mut transcript);
    assert!(!verdicts[0].passed);
    assert!(verdicts[0].message.contains("submit after release"), "{}", verdicts[0].message);
}

#[test]
fn production_is_refused_by_the_worker_command() {
    let exe = env!("CARGO_BIN_EXE_av2-compat");
    let output = std::process::Command::new(exe)
        .args(["worker", "--base-url", "https://belzedar.duckdns.org", "--username", "x", "--password", "y"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("is production"));
}
