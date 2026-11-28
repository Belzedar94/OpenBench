//! `av2-compat`: replay v1's golden answers against a base URL, compare two
//! recordings, or run the worker protocol scenarios.  `av2-compat help`.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use av2_compat::defaults;
use av2_compat::diff::{Mode, Severity};
use av2_compat::golden::{self, Record};
use av2_compat::http::{self, HttpTransport, Response, Transport};
use av2_compat::replay::{self, Judge, Outcome, Tally};
use av2_compat::rules::Rules;
use av2_compat::scenarios::{self, Settings};

const HELP: &str = "\
av2-compat: is v2's outside the same as v1's? (atomicdb-v2/docs/api-contract.md)

USAGE
  av2-compat replay --base-url URL [options]
      Send every request of a golden recording to URL and diff the answers.
        --golden PATH        recording (default: tests/golden/v1-20261128/responses.jsonl.gz)
        --rules PATH         normalization rules (default: tests/golden/normalize.json)
        --mode M             snapshot (default) | live | shape
        --rate N             requests per second, 0 = no cap (default 0; 2 against a public host)
        --endpoint E         only this endpoint (repeatable)
        --limit N            only the first N records
        --strict-order       key order must match too
        --bytes              raw bodies must match where no field was normalized
        --through-nginx      compare the headers nginx adds (vary, content-encoding)
        --report PATH        every difference as JSON lines
        --show N             differences printed per endpoint (default 5)
  av2-compat compare --expected PATH --actual PATH [--rules --mode --strict-order --bytes --report --show]
      Diff two recordings of the same requests (matched by method, path, query, form).
  av2-compat worker --base-url URL --username U (--password-env VAR | --password P) [options]
      Run the worker protocol scenarios. Refuses production.
        --machine-prefix P   machine names are P-<scenario> (default av2-compat)
        --worker-build N     (default 2026112501)
        --gate-enforced      the server enforces the engine gate: run engine-not-accepted
        --engine-sha HEX     engine_sha of every normal submit (a reference binary when the gate is on)
        --only NAME          only this scenario (repeatable)
        --transcript PATH    every call as JSON lines (password and tokens redacted)
        --rate N             requests per second, 0 = no cap (default 0)
  av2-compat scenarios       list the worker scenarios
";

struct Args {
    values: BTreeMap<String, Vec<String>>,
    flags: Vec<String>,
}

impl Args {
    fn parse(raw: &[String]) -> Result<Args, String> {
        const FLAGS: &[&str] = &["--strict-order", "--bytes", "--through-nginx", "--gate-enforced"];
        let mut values: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut flags = Vec::new();
        let mut i = 0;
        while i < raw.len() {
            let name = &raw[i];
            if !name.starts_with("--") {
                return Err(format!("unexpected argument {name:?}"));
            }
            if FLAGS.contains(&name.as_str()) {
                flags.push(name.clone());
                i += 1;
                continue;
            }
            let value = raw.get(i + 1).ok_or_else(|| format!("{name} needs a value"))?;
            values.entry(name.clone()).or_default().push(value.clone());
            i += 2;
        }
        Ok(Args { values, flags })
    }

    fn one(&self, name: &str) -> Option<&str> {
        self.values.get(name).and_then(|v| v.last()).map(String::as_str)
    }

    fn all(&self, name: &str) -> Vec<String> {
        self.values.get(name).cloned().unwrap_or_default()
    }

    fn flag(&self, name: &str) -> bool {
        self.flags.iter().any(|f| f == name)
    }

    fn number<T: std::str::FromStr>(&self, name: &str, default: T) -> Result<T, String> {
        match self.one(name) {
            None => Ok(default),
            Some(text) => text.parse().map_err(|_| format!("{name}: not a number: {text}")),
        }
    }
}

fn mode(args: &Args) -> Result<Mode, String> {
    let text = args.one("--mode").unwrap_or("snapshot");
    Mode::parse(text).ok_or_else(|| format!("--mode must be snapshot, live or shape, not {text}"))
}

fn rules(args: &Args) -> Result<Rules, String> {
    Rules::load(&PathBuf::from(args.one("--rules").unwrap_or(defaults::RULES)))
}

fn filter(records: Vec<Record>, args: &Args) -> Result<Vec<Record>, String> {
    let endpoints = args.all("--endpoint");
    let limit: usize = args.number("--limit", usize::MAX)?;
    Ok(records.into_iter().filter(|r| endpoints.is_empty() || endpoints.contains(&r.endpoint)).take(limit).collect())
}

/// Prints the verdicts as they come and keeps the report.
struct Reporter {
    show: usize,
    shown: BTreeMap<String, usize>,
    report: Option<std::fs::File>,
}

impl Reporter {
    fn new(args: &Args) -> Result<Reporter, String> {
        let report = match args.one("--report") {
            Some(path) => Some(std::fs::File::create(path).map_err(|e| format!("{path}: {e}"))?),
            None => None,
        };
        Ok(Reporter { show: args.number("--show", 5)?, shown: BTreeMap::new(), report })
    }

    fn each(&mut self, record: &Record, verdict: Result<&Outcome, &str>) {
        match verdict {
            Ok(outcome) => {
                if let Some(file) = self.report.as_mut() {
                    for d in &outcome.differences {
                        let line = serde_json::json!({"id": outcome.id, "endpoint": outcome.endpoint, "request": outcome.target,
                            "note": record.note, "severity": d.severity.name(), "path": d.path, "what": d.what,
                            "expected": d.expected, "actual": d.actual});
                        let _ = writeln!(file, "{line}");
                    }
                    if let Some(why) = &outcome.alternative {
                        let line = serde_json::json!({"id": outcome.id, "endpoint": outcome.endpoint, "request": outcome.target,
                            "severity": "info", "what": "accepted alternative", "why": why});
                        let _ = writeln!(file, "{line}");
                    }
                }
                let worst = outcome.worst();
                if matches!(worst, Some(Severity::Failure) | Some(Severity::Warning)) {
                    let count = self.shown.entry(outcome.endpoint.clone()).or_default();
                    if *count < self.show {
                        *count += 1;
                        println!("#{} {} ({})", outcome.id, outcome.target, record.note);
                        for d in outcome.differences.iter().take(6) {
                            println!("    {d}");
                        }
                        if outcome.differences.len() > 6 {
                            println!("    ... {} more", outcome.differences.len() - 6);
                        }
                    }
                }
            }
            Err(error) => {
                println!("#{} {} {}: transport error: {error}", record.id, record.method, record.path);
                if let Some(file) = self.report.as_mut() {
                    let _ = writeln!(
                        file,
                        "{}",
                        serde_json::json!({"id": record.id, "endpoint": record.endpoint, "severity": "error", "what": error})
                    );
                }
            }
        }
    }
}

fn print_tallies(tallies: &BTreeMap<String, Tally>, seconds: f64) -> bool {
    println!();
    println!("{:<22} {:>7} {:>7} {:>8} {:>7} {:>6} {:>6}", "endpoint", "total", "passed", "warnings", "failed", "alt", "errors");
    let mut sum = Tally::default();
    for (endpoint, t) in tallies {
        println!("{endpoint:<22} {:>7} {:>7} {:>8} {:>7} {:>6} {:>6}", t.total, t.passed, t.warnings, t.failed, t.alternatives, t.errors);
        sum.total += t.total;
        sum.passed += t.passed;
        sum.warnings += t.warnings;
        sum.failed += t.failed;
        sum.alternatives += t.alternatives;
        sum.errors += t.errors;
    }
    println!(
        "{:<22} {:>7} {:>7} {:>8} {:>7} {:>6} {:>6}",
        "all", sum.total, sum.passed, sum.warnings, sum.failed, sum.alternatives, sum.errors
    );
    println!("{:.1} s, {:.1} records/s", seconds, sum.total as f64 / seconds.max(1e-9));
    sum.failed == 0 && sum.errors == 0
}

fn judge_options(args: &Args) -> Result<Judge, String> {
    Ok(Judge {
        mode: mode(args)?,
        strict_order: args.flag("--strict-order"),
        bytes: args.flag("--bytes"),
        through_nginx: args.flag("--through-nginx"),
    })
}

fn cmd_replay(args: &Args) -> Result<bool, String> {
    let base = args.one("--base-url").ok_or("--base-url is required")?;
    let rules = rules(args)?;
    let records = filter(golden::read_records(&PathBuf::from(args.one("--golden").unwrap_or(defaults::GOLDEN)))?, args)?;
    let default_rate = if base.contains("127.0.0.1") || base.contains("localhost") { 0.0 } else { 2.0 };
    let rate: f64 = args.number("--rate", default_rate)?;
    if http::is_production(base) && (rate == 0.0 || rate > 2.0) {
        return Err("production is replayed at most 2 requests a second: pass --rate 2 or less".into());
    }
    let mut transport = HttpTransport::new(base, rate, Duration::from_secs(120));
    let mut reporter = Reporter::new(args)?;
    let started = Instant::now();
    println!("replaying {} records against {base} (mode {:?}, rules v{})", records.len(), mode(args)?, rules.version);
    let tallies = replay::replay(&rules, judge_options(args)?, &records, &mut transport, |r, v| reporter.each(r, v));
    Ok(print_tallies(&tallies, started.elapsed().as_secs_f64()))
}

/// A transport that answers from another recording.
struct Recorded {
    answers: BTreeMap<String, Record>,
}

impl Transport for Recorded {
    fn send(&mut self, request: &http::Request) -> Result<Response, String> {
        let key = format!(
            "{} {}?{}|{}",
            request.method,
            request.path,
            request.query.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join("&"),
            request.form.iter().flatten().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join("&")
        );
        let record = self.answers.get(&key).ok_or_else(|| format!("not in the other recording: {key}"))?;
        Ok(Response { status: record.status, headers: record.headers.clone(), body: record.body.clone().into_bytes() })
    }
}

fn cmd_compare(args: &Args) -> Result<bool, String> {
    let rules = rules(args)?;
    let expected = filter(golden::read_records(&PathBuf::from(args.one("--expected").ok_or("--expected is required")?))?, args)?;
    let actual = golden::read_records(&PathBuf::from(args.one("--actual").ok_or("--actual is required")?))?;
    let mut answers = BTreeMap::new();
    for record in actual {
        let key = record.request_key();
        let key = key.rsplit_once('|').map(|(head, _)| head.to_string()).unwrap_or(key);
        answers.entry(key).or_insert(record);
    }
    let mut transport = Recorded { answers };
    let mut reporter = Reporter::new(args)?;
    let started = Instant::now();
    let tallies = replay::replay(&rules, judge_options(args)?, &expected, &mut transport, |r, v| reporter.each(r, v));
    Ok(print_tallies(&tallies, started.elapsed().as_secs_f64()))
}

fn cmd_worker(args: &Args) -> Result<bool, String> {
    let base = args.one("--base-url").ok_or("--base-url is required")?;
    if http::is_production(base) {
        return Err(format!("{base} is production: the worker scenarios lease and submit, run them against a test instance"));
    }
    let password = match (args.one("--password-env"), args.one("--password")) {
        (Some(var), _) => std::env::var(var).map_err(|_| format!("environment variable {var} is not set"))?,
        (None, Some(password)) => password.to_string(),
        (None, None) => return Err("--password-env VAR or --password P is required".into()),
    };
    let settings = Settings {
        username: args.one("--username").ok_or("--username is required")?.to_string(),
        password,
        machine_prefix: args.one("--machine-prefix").unwrap_or("av2-compat").to_string(),
        worker_build: args.number("--worker-build", 2026112501)?,
        gate_enforced: args.flag("--gate-enforced"),
        engine_sha: args.one("--engine-sha").unwrap_or("").to_ascii_lowercase(),
    };
    let mut transport = HttpTransport::new(base, args.number("--rate", 0.0)?, Duration::from_secs(120));
    let mut transcript = Vec::new();
    let started = Instant::now();
    let verdicts = scenarios::run_all(&mut transport, &settings, &args.all("--only"), &mut transcript);
    if let Some(path) = args.one("--transcript") {
        let mut file = std::fs::File::create(path).map_err(|e| format!("{path}: {e}"))?;
        for step in &transcript {
            writeln!(file, "{}", serde_json::to_string(step).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
        }
    }
    let mut ok = true;
    for v in &verdicts {
        let word = if v.skipped {
            "SKIP"
        } else if v.passed {
            "PASS"
        } else {
            "FAIL"
        };
        println!("{word}  {:<24} {:>3} calls  {}", v.scenario, v.steps, v.message);
        ok &= v.passed;
    }
    println!("{} calls in {:.1} s", transcript.len(), started.elapsed().as_secs_f64());
    Ok(ok)
}

fn main() -> ExitCode {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let Some(command) = raw.first() else {
        print!("{HELP}");
        return ExitCode::from(2);
    };
    let result = Args::parse(&raw[1..]).and_then(|args| match command.as_str() {
        "replay" => cmd_replay(&args),
        "compare" => cmd_compare(&args),
        "worker" => cmd_worker(&args),
        "scenarios" => {
            for (name, what) in scenarios::list() {
                println!("{name:<24} {what}");
            }
            Ok(true)
        }
        "help" | "--help" | "-h" => {
            print!("{HELP}");
            Ok(true)
        }
        other => Err(format!("unknown command {other:?} (try help)")),
    });
    match result {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(message) => {
            eprintln!("av2-compat: {message}");
            ExitCode::from(2)
        }
    }
}
