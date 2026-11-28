//! Replaying golden requests and judging the answers.

use std::collections::BTreeMap;

use crate::diff::{self, Difference, Mode, Options, Severity};
use crate::golden::Record;
use crate::http::{Request, Response, Transport};
use crate::json;
use crate::rules::Rules;

/// How to judge one answer.
#[derive(Clone, Copy, Debug)]
pub struct Judge {
    pub mode: Mode,
    pub strict_order: bool,
    /// Compare the raw body bytes too where no rule removed a field.
    pub bytes: bool,
    /// The answers went through production's nginx (compare its headers).
    pub through_nginx: bool,
}

/// The request a record describes.
pub fn request_of(record: &Record) -> Request {
    let form = record.form.as_ref().map(|_| record.form_pairs());
    let mut headers: Vec<(String, String)> = record.request_headers.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
    if !headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("accept-encoding")) {
        // What the recorder sent by default.
        headers.push(("Accept-Encoding".into(), "gzip".into()));
    }
    Request { method: record.method.clone(), path: record.path.clone(), query: record.query.clone(), form, headers }
}

/// The verdict on one record.
#[derive(Clone, Debug)]
pub struct Outcome {
    pub id: u64,
    pub endpoint: String,
    pub target: String,
    pub differences: Vec<Difference>,
    /// An accepted alternative answer was given (contract, section 0).
    pub alternative: Option<String>,
}

impl Outcome {
    pub fn worst(&self) -> Option<Severity> {
        self.differences.iter().map(|d| d.severity).min()
    }
}

fn is_html(headers: &BTreeMap<String, String>) -> bool {
    headers.get("content-type").is_some_and(|v| v.starts_with("text/html"))
}

/// Judge `actual` against the recorded answer of `record`.
pub fn judge(rules: &Rules, judge: Judge, record: &Record, actual: &Response) -> Result<Outcome, String> {
    let endpoint_rules = rules.for_endpoint(&record.endpoint)?;
    let mut outcome = Outcome {
        id: record.id,
        endpoint: record.endpoint.clone(),
        target: format!("{} {}", record.method, request_of(record).target()),
        differences: Vec::new(),
        alternative: None,
    };
    let actual_text = actual.text();

    // An accepted alternative ends the judgement (production vs repository).
    for alternative in &rules.alternatives {
        if alternative.endpoint != record.endpoint
            || alternative.method.as_deref().is_some_and(|m| m != record.method)
            || alternative.path.as_deref().is_some_and(|p| p != record.path)
        {
            continue;
        }
        for accepted in &alternative.accept {
            let body_ok = match &accepted.body {
                None => true,
                Some(body) => json::parse(body).ok() == json::parse(&actual_text).ok(),
            };
            if accepted.status == actual.status && body_ok {
                outcome.alternative = Some(alternative.why.clone());
                return Ok(outcome);
            }
        }
    }

    if record.status != actual.status {
        outcome.differences.push(Difference {
            path: "status".into(),
            what: "value".into(),
            expected: record.status.to_string(),
            actual: actual.status.to_string(),
            severity: Severity::Failure,
        });
        return Ok(outcome);
    }

    let mut names = rules.compare_headers.clone();
    names.extend(endpoint_rules.headers.iter().cloned());
    if judge.through_nginx {
        names.extend(rules.nginx_headers.iter().cloned());
    }
    names.retain(|n| !rules.volatile_headers.contains(n) && !endpoint_rules.volatile_headers.contains(n));
    names.sort();
    names.dedup();

    // Django's CSRF page and the like: an HTML error is judged by its status
    // and its content type, never by its markup.
    if is_html(&record.headers) {
        if !is_html(&actual.headers) {
            outcome.differences.push(Difference {
                path: "header content-type".into(),
                what: "value".into(),
                expected: record.headers.get("content-type").cloned().unwrap_or_default(),
                actual: actual.headers.get("content-type").cloned().unwrap_or_else(|| "absent".into()),
                severity: Severity::Failure,
            });
        }
        return Ok(outcome);
    }
    outcome.differences.extend(diff::compare_headers(&names, &record.headers, &actual.headers));

    if record.body.is_empty() && actual.body.is_empty() {
        return Ok(outcome);
    }
    let expected = match json::parse(&record.body) {
        Ok(value) => value,
        Err(_) => {
            if record.body != actual_text {
                outcome.differences.push(Difference {
                    path: "body".into(),
                    what: "text".into(),
                    expected: preview(&record.body),
                    actual: preview(&actual_text),
                    severity: Severity::Failure,
                });
            }
            return Ok(outcome);
        }
    };
    let parsed = match json::parse(&actual_text) {
        Ok(value) => value,
        Err(error) => {
            outcome.differences.push(Difference {
                path: "body".into(),
                what: format!("not JSON ({error})"),
                expected: expected.preview(120),
                actual: preview(&actual_text),
                severity: Severity::Failure,
            });
            return Ok(outcome);
        }
    };
    let options = Options { mode: judge.mode, strict_order: judge.strict_order };
    let differences = diff::compare(&endpoint_rules, options, &expected, &parsed);
    let untouched = endpoint_rules.volatile.is_empty() && endpoint_rules.decisions.is_empty();
    if judge.bytes && judge.mode == Mode::Snapshot && differences.is_empty() && untouched && record.body != actual_text {
        outcome.differences.push(Difference {
            path: "body".into(),
            what: "bytes (same JSON, different text)".into(),
            expected: preview(&record.body),
            actual: preview(&actual_text),
            severity: Severity::Failure,
        });
    }
    outcome.differences.extend(differences);
    Ok(outcome)
}

fn preview(text: &str) -> String {
    let cut: String = text.chars().take(160).collect();
    if cut.len() < text.len() { format!("{cut}...") } else { cut }
}

/// Totals per endpoint.
#[derive(Clone, Debug, Default)]
pub struct Tally {
    pub total: usize,
    pub passed: usize,
    pub warnings: usize,
    pub failed: usize,
    pub alternatives: usize,
    pub errors: usize,
}

/// Replay every record through `transport`, calling `each` with every verdict.
pub fn replay(
    rules: &Rules,
    judge_with: Judge,
    records: &[Record],
    transport: &mut dyn Transport,
    mut each: impl FnMut(&Record, Result<&Outcome, &str>),
) -> BTreeMap<String, Tally> {
    let mut tallies: BTreeMap<String, Tally> = BTreeMap::new();
    for record in records {
        let tally = tallies.entry(record.endpoint.clone()).or_default();
        tally.total += 1;
        let verdict = transport.send(&request_of(record)).and_then(|response| judge(rules, judge_with, record, &response));
        match &verdict {
            Ok(outcome) => {
                if outcome.alternative.is_some() {
                    tally.alternatives += 1;
                }
                match outcome.worst() {
                    None | Some(Severity::Info) => tally.passed += 1,
                    Some(Severity::Warning) => tally.warnings += 1,
                    Some(Severity::Failure) => tally.failed += 1,
                }
                each(record, Ok(outcome));
            }
            Err(error) => {
                tally.errors += 1;
                each(record, Err(error));
            }
        }
    }
    tallies
}
