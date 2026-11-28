//! Field-by-field comparison of a recorded v1 body with an answer.

use std::collections::BTreeMap;
use std::fmt;

use crate::json::{Json, Kind};
use crate::rules::{EndpointRules, Path};

/// How strictly to compare (contract, section 7).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Both sides answer from the same rows: everything but the volatile
    /// paths must be equal.
    Snapshot,
    /// Each side on its own moving tree: growth paths compare by type only.
    Live,
    /// Types and key sets only, everywhere.
    Shape,
}

impl Mode {
    pub fn parse(text: &str) -> Option<Mode> {
        match text {
            "snapshot" => Some(Mode::Snapshot),
            "live" => Some(Mode::Live),
            "shape" => Some(Mode::Shape),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    /// Breaks the contract.
    Failure,
    /// A difference where v1 itself is not deterministic (budgeted fields).
    Warning,
    /// Allowed: an owner decision or an accepted alternative.
    Info,
}

impl Severity {
    pub fn name(self) -> &'static str {
        match self {
            Severity::Failure => "failure",
            Severity::Warning => "warning",
            Severity::Info => "info",
        }
    }
}

/// One difference.
#[derive(Clone, Debug, PartialEq)]
pub struct Difference {
    pub path: String,
    pub what: String,
    pub expected: String,
    pub actual: String,
    pub severity: Severity,
}

impl fmt::Display for Difference {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {} {}: expected {} got {}", self.severity.name(), self.path, self.what, self.expected, self.actual)
    }
}

/// Options that are not rules: key order and nginx headers.
#[derive(Clone, Copy, Debug)]
pub struct Options {
    pub mode: Mode,
    pub strict_order: bool,
}

struct Walk<'a> {
    rules: &'a EndpointRules,
    options: Options,
    out: Vec<Difference>,
}

#[derive(Clone, Copy)]
struct Context {
    shape: bool,
    severity: Severity,
}

const PREVIEW: usize = 120;

impl Walk<'_> {
    fn push(&mut self, path: &Path, ctx: Context, what: &str, expected: String, actual: String) {
        self.out.push(Difference { path: path.to_string(), what: what.to_string(), expected, actual, severity: ctx.severity });
    }

    fn node(&mut self, path: &Path, expected: &Json, actual: &Json, mut ctx: Context) {
        if self.rules.is_volatile(path) {
            return;
        }
        if let Some(id) = self.rules.decision(path) {
            if expected != actual {
                let id = format!("decision {id}");
                self.push(path, Context { severity: Severity::Info, ..ctx }, &id, expected.preview(PREVIEW), actual.preview(PREVIEW));
            }
            return;
        }
        if self.options.mode == Mode::Shape || (self.options.mode == Mode::Live && self.rules.is_growth(path)) {
            ctx.shape = true;
        }
        if self.options.mode == Mode::Snapshot && self.rules.is_budgeted(path) && ctx.severity == Severity::Failure {
            ctx.severity = Severity::Warning;
        }
        if ctx.shape {
            self.shape(path, expected, actual, ctx);
        } else {
            self.exact(path, expected, actual, ctx);
        }
    }

    fn exact(&mut self, path: &Path, expected: &Json, actual: &Json, ctx: Context) {
        let (ek, ak) = (expected.kind(), actual.kind());
        if ek != ak {
            self.push(
                path,
                ctx,
                "type",
                format!("{} {}", ek.name(), expected.preview(PREVIEW)),
                format!("{} {}", ak.name(), actual.preview(PREVIEW)),
            );
            return;
        }
        match (expected, actual) {
            (Json::Arr(e), Json::Arr(a)) => {
                if let Some(field) = self.rules.key_field(path) {
                    self.keyed(path, e, a, field, ctx);
                    return;
                }
                if e.len() != a.len() {
                    self.push(path, ctx, "length", e.len().to_string(), a.len().to_string());
                }
                for (index, (ev, av)) in e.iter().zip(a).enumerate() {
                    self.node(&path.child_index(index), ev, av, ctx);
                }
            }
            (Json::Obj(e), Json::Obj(a)) => self.object(path, e, a, ctx),
            _ => {
                if expected != actual {
                    self.push(path, ctx, "value", expected.preview(PREVIEW), actual.preview(PREVIEW));
                }
            }
        }
    }

    fn object(&mut self, path: &Path, e: &[(String, Json)], a: &[(String, Json)], ctx: Context) {
        let actual: BTreeMap<&str, &Json> = a.iter().map(|(k, v)| (k.as_str(), v)).collect();
        let expected: BTreeMap<&str, &Json> = e.iter().map(|(k, v)| (k.as_str(), v)).collect();
        for (key, ev) in e {
            let child = path.child_key(key);
            match actual.get(key.as_str()) {
                Some(av) => self.node(&child, ev, av, ctx),
                None => {
                    if self.skippable(&child, ctx) {
                        continue;
                    }
                    self.push(&child, ctx, "missing key", ev.preview(PREVIEW), "absent".into());
                }
            }
        }
        for (key, av) in a {
            if expected.contains_key(key.as_str()) {
                continue;
            }
            let child = path.child_key(key);
            if self.skippable(&child, ctx) {
                continue;
            }
            self.push(&child, ctx, "extra key", "absent".into(), av.preview(PREVIEW));
        }
        if self.options.strict_order && !ctx.shape {
            let keep = |fields: &[(String, Json)], other: &BTreeMap<&str, &Json>| -> Vec<String> {
                fields
                    .iter()
                    .map(|(k, _)| k.clone())
                    .filter(|k| other.contains_key(k.as_str()) && !self.rules.is_volatile(&path.child_key(k)))
                    .collect()
            };
            let (eo, ao) = (keep(e, &actual), keep(a, &expected));
            if eo != ao {
                self.push(path, ctx, "key order", eo.join(","), ao.join(","));
            }
        }
    }

    /// A key present on one side only that the rules allow to be absent.
    fn skippable(&self, path: &Path, ctx: Context) -> bool {
        self.rules.is_volatile(path) || self.rules.decision(path).is_some() || (ctx.shape && self.rules.is_optional(path))
    }

    fn keyed(&mut self, path: &Path, e: &[Json], a: &[Json], field: &str, ctx: Context) {
        let key_of = |item: &Json| item.get(field).map(|v| v.to_compact());
        let mut actual: BTreeMap<String, (usize, &Json)> = BTreeMap::new();
        for (index, item) in a.iter().enumerate() {
            if let Some(key) = key_of(item) {
                actual.insert(key, (index, item));
            }
        }
        let before = self.out.len();
        let mut expected_keys = Vec::new();
        for (index, item) in e.iter().enumerate() {
            let Some(key) = key_of(item) else {
                self.push(&path.child_index(index), ctx, "row without key field", item.preview(PREVIEW), String::new());
                continue;
            };
            expected_keys.push(key.clone());
            match actual.get(&key) {
                Some((_, av)) => self.node(&path.child_index(index), item, av, ctx),
                None => {
                    self.push(&path.child_index(index), ctx, &format!("missing row {field}={key}"), item.preview(PREVIEW), "absent".into())
                }
            }
        }
        let expected_set: std::collections::BTreeSet<&String> = expected_keys.iter().collect();
        for (key, (index, item)) in &actual {
            if !expected_set.contains(key) {
                self.push(&path.child_index(*index), ctx, &format!("extra row {field}={key}"), "absent".into(), item.preview(PREVIEW));
            }
        }
        let actual_order: Vec<String> = a.iter().filter_map(key_of).collect();
        if !ctx.shape && actual_order != expected_keys {
            // A reorder caused by budgeted rows is v1's own noise: a warning.
            let budgeted_noise = self.out[before..].iter().any(|d| d.severity == Severity::Warning);
            let severity = if budgeted_noise && ctx.severity == Severity::Failure { Severity::Warning } else { ctx.severity };
            self.push(path, Context { severity, ..ctx }, "row order", expected_keys.join(","), actual_order.join(","));
        }
    }

    fn shape(&mut self, path: &Path, expected: &Json, actual: &Json, ctx: Context) {
        let (ek, ak) = (expected.kind(), actual.kind());
        if ek == Kind::Null || ak == Kind::Null {
            return;
        }
        if ek != ak {
            let numbers = matches!((ek, ak), (Kind::Int, Kind::Float) | (Kind::Float, Kind::Int));
            let severity = if numbers && ctx.severity == Severity::Failure { Severity::Warning } else { ctx.severity };
            self.push(path, Context { severity, ..ctx }, "type", ek.name().into(), ak.name().into());
            return;
        }
        match (expected, actual) {
            (Json::Arr(e), Json::Arr(a)) => {
                for (index, (ev, av)) in e.iter().zip(a).enumerate() {
                    self.node(&path.child_index(index), ev, av, ctx);
                }
            }
            (Json::Obj(e), Json::Obj(a)) => self.object(path, e, a, ctx),
            _ => {}
        }
    }
}

/// Compare two JSON bodies under `rules`.
pub fn compare(rules: &EndpointRules, options: Options, expected: &Json, actual: &Json) -> Vec<Difference> {
    let mut walk = Walk { rules, options, out: Vec::new() };
    walk.node(&Path::default(), expected, actual, Context { shape: false, severity: Severity::Failure });
    walk.out
}

/// Compare the headers named in `names` (lower-case).
pub fn compare_headers(names: &[String], expected: &BTreeMap<String, String>, actual: &BTreeMap<String, String>) -> Vec<Difference> {
    let mut out = Vec::new();
    for name in names {
        let (e, a) = (expected.get(name), actual.get(name));
        if e != a {
            out.push(Difference {
                path: format!("header {name}"),
                what: "value".into(),
                expected: e.cloned().unwrap_or_else(|| "absent".into()),
                actual: a.cloned().unwrap_or_else(|| "absent".into()),
                severity: Severity::Failure,
            });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::parse;
    use crate::rules::Rules;

    fn rules() -> EndpointRules {
        Rules::from_json(
            r#"{"version": 1, "endpoints": {"q": {
                "volatile": ["$.t"],
                "budgeted": ["$.moves[*].repetition", "$.moves[*].score"],
                "growth": ["$.score", "$.moves"],
                "optional": ["$.moves[*].raw"],
                "keyed": {"$.moves": "uci"}}}}"#,
        )
        .unwrap()
        .for_endpoint("q")
        .unwrap()
    }

    fn run(mode: Mode, e: &str, a: &str) -> Vec<Difference> {
        compare(&rules(), Options { mode, strict_order: true }, &parse(e).unwrap(), &parse(a).unwrap())
    }

    #[test]
    fn equal_bodies_and_volatile_fields() {
        let body = r#"{"score": 1, "t": 5, "moves": [{"uci": "e2e4", "score": 3}]}"#;
        assert!(run(Mode::Snapshot, body, body).is_empty());
        let other_t = r#"{"score": 1, "t": 6, "moves": [{"uci": "e2e4", "score": 3}]}"#;
        assert!(run(Mode::Snapshot, body, other_t).is_empty());
        let no_t = r#"{"score": 1, "moves": [{"uci": "e2e4", "score": 3}]}"#;
        assert!(run(Mode::Snapshot, body, no_t).is_empty());
    }

    #[test]
    fn int_and_float_differ_in_snapshot_mode() {
        let d = run(Mode::Snapshot, r#"{"score": 1}"#, r#"{"score": 1.0}"#);
        assert_eq!(d.len(), 1);
        assert_eq!(d[0].what, "type");
        assert_eq!(d[0].severity, Severity::Failure);
    }

    #[test]
    fn growth_compares_types_in_live_mode() {
        let e = r#"{"score": 10, "moves": [{"uci": "e2e4", "score": 3}]}"#;
        let a = r#"{"score": -4, "moves": [{"uci": "d2d4", "score": null}, {"uci": "e2e4", "score": 9}]}"#;
        assert!(run(Mode::Live, e, a).is_empty());
        assert!(!run(Mode::Snapshot, e, a).is_empty());
        let wrong = r#"{"score": "x", "moves": []}"#;
        let d = run(Mode::Live, e, wrong);
        assert_eq!(d.len(), 1, "{d:?}");
        assert_eq!(d[0].path, "$.score");
    }

    #[test]
    fn keys_missing_or_extra() {
        let d = run(Mode::Snapshot, r#"{"a": 1, "b": 2}"#, r#"{"a": 1, "c": 2}"#);
        let whats: Vec<&str> = d.iter().map(|x| x.what.as_str()).collect();
        // the order is judged on the keys both sides have
        assert_eq!(whats, ["missing key", "extra key"]);
        // optional keys may be absent in shape comparisons only
        let e = r#"{"moves": [{"uci": "a", "raw": "x"}]}"#;
        let a = r#"{"moves": [{"uci": "a"}]}"#;
        assert!(run(Mode::Live, e, a).is_empty());
        assert_eq!(run(Mode::Snapshot, e, a).len(), 1);
    }

    #[test]
    fn key_order_is_checked_when_asked() {
        let d = run(Mode::Snapshot, r#"{"a": 1, "b": 2}"#, r#"{"b": 2, "a": 1}"#);
        assert_eq!(d.len(), 1);
        assert_eq!(d[0].what, "key order");
        let loose = compare(
            &rules(),
            Options { mode: Mode::Snapshot, strict_order: false },
            &parse(r#"{"a":1,"b":2}"#).unwrap(),
            &parse(r#"{"b":2,"a":1}"#).unwrap(),
        );
        assert!(loose.is_empty());
    }

    #[test]
    fn keyed_rows_and_budgeted_reorder() {
        let e = r#"{"moves": [{"uci": "a", "score": 5, "repetition": false}, {"uci": "b", "score": 0, "repetition": true}]}"#;
        // b lost its repetition (a budget ran out), so the rows swapped: warnings only.
        let a = r#"{"moves": [{"uci": "b", "score": 9, "repetition": false}, {"uci": "a", "score": 5, "repetition": false}]}"#;
        let d = run(Mode::Snapshot, e, a);
        assert!(!d.is_empty());
        assert!(d.iter().all(|x| x.severity == Severity::Warning), "{d:?}");
        // a plain reorder with equal rows is a failure
        let swapped = r#"{"moves": [{"uci": "b", "score": 0, "repetition": true}, {"uci": "a", "score": 5, "repetition": false}]}"#;
        let d = run(Mode::Snapshot, e, swapped);
        assert_eq!(d.len(), 1);
        assert_eq!((d[0].what.as_str(), d[0].severity), ("row order", Severity::Failure));
        // a missing row is a failure
        let short = r#"{"moves": [{"uci": "a", "score": 5, "repetition": false}]}"#;
        assert!(run(Mode::Snapshot, e, short).iter().any(|x| x.what.starts_with("missing row") && x.severity == Severity::Failure));
    }

    #[test]
    fn headers() {
        let mut e = BTreeMap::new();
        e.insert("content-type".to_string(), "application/json".to_string());
        let mut a = e.clone();
        assert!(compare_headers(&["content-type".into()], &e, &a).is_empty());
        a.insert("content-type".into(), "text/html".into());
        assert_eq!(compare_headers(&["content-type".into(), "retry-after".into()], &e, &a).len(), 1);
    }
}
