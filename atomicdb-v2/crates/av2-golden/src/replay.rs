//! Replay one case on v2 and classify every difference with v1.
//!
//! Modes:
//! - `v2`: call the v2 entry of the batch (`propagation_queue.drain_batch`
//!   for a climb, `ingest_queue.apply_job` for a landing) on an isolated
//!   graph holding only what the case read;
//! - `echo`: write v1's own writes through the v2 graph (no rules): every
//!   difference left is a v1 value the v2 records cannot hold, or a bug of
//!   this tool.  It runs today, before any rule is ported;
//! - `noop`: write nothing: the size of what the port has to reproduce.
//!
//! Classes of a (position, column) pair:
//!
//! | class | meaning | failure |
//! |---|---|---|
//! | `match` | both wrote the same value, or v1 wrote the value v2 already had | no |
//! | `value_diff` | both wrote, different values | yes |
//! | `missing_write` | v1 wrote, v2 left another value | yes |
//! | `extra_write` | v2 wrote, v1 did not | yes |
//! | `unread_input` | a difference on a position or column the case never read (v2 started from a default, not from v1's value) | no, listed |
//! | `not_modeled` | a column v2 does not store (`priority`, `updated`) | no |
//!
//! Rows: `missing_row` / `extra_row` (a position inserted by one side only),
//! edges: `missing_edge`, `extra_edge`, `edge_child_diff`.

use crate::case::{self, PreImage, Written};
use av2_shim::registry;
use av2_shim::v1::{self, Row};
use av2_shim::{EdgeRow, ShimGraph};
use serde::Serialize;
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    V2,
    Echo,
    Noop,
}

impl std::str::FromStr for Mode {
    type Err = String;
    fn from_str(s: &str) -> Result<Mode, String> {
        match s {
            "v2" => Ok(Mode::V2),
            "echo" => Ok(Mode::Echo),
            "noop" => Ok(Mode::Noop),
            _ => Err(format!("unknown mode {s:?} (v2, echo, noop)")),
        }
    }
}

/// The v2 entry that replays a case of `kind`.
pub fn entry_for(kind: &str) -> Option<&'static str> {
    match kind {
        "drain" => Some("propagation_queue.drain_batch"),
        "stage_a" => Some("ingest_queue.apply_job"),
        _ => None,
    }
}

pub const FAILURES: &[&str] = &[
    "unrepresentable",
    "load_unrepresentable",
    "value_diff",
    "missing_write",
    "extra_write",
    "missing_row",
    "extra_row",
    "missing_edge",
    "extra_edge",
    "edge_child_diff",
];

#[derive(Debug, Clone, Serialize)]
pub struct Diff {
    pub class: &'static str,
    pub key: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub column: String,
    pub v1: Value,
    pub v2: Value,
}

#[derive(Debug, Serialize)]
pub struct CaseReport {
    pub id: String,
    pub kind: String,
    pub file: String,
    /// `clean`, `diff`, `not_ported`, `v2_error`, `load_error`, `v1_error`, `unknown_kind`.
    pub status: &'static str,
    pub incomplete: Vec<String>,
    pub elapsed_ms_v1: f64,
    pub load_us: u64,
    pub run_us: u64,
    pub positions_read: usize,
    pub edges_read: usize,
    pub parent_links: usize,
    pub stubs: usize,
    pub positions_written_v1: usize,
    pub counts: BTreeMap<&'static str, u64>,
    pub diffs: Vec<Diff>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub load_issues: usize,
}

fn norm(col: &str, v: &Value) -> Value {
    v1::normalise(col, v)
}

pub fn replay(case_v: &Value, file: &str, mode: Mode) -> CaseReport {
    let kind = case_v.get("kind").and_then(Value::as_str).unwrap_or("").to_string();
    let mut rep = CaseReport {
        id: case_v.get("id").and_then(Value::as_str).unwrap_or("").to_string(),
        kind: kind.clone(),
        file: file.to_string(),
        status: "clean",
        incomplete: case::incompleteness(case_v),
        elapsed_ms_v1: case_v.get("elapsed_ms").and_then(Value::as_f64).unwrap_or(0.0),
        load_us: 0,
        run_us: 0,
        positions_read: 0,
        edges_read: 0,
        parent_links: 0,
        stubs: 0,
        positions_written_v1: 0,
        counts: BTreeMap::new(),
        diffs: Vec::new(),
        error: None,
        load_issues: 0,
    };
    let t0 = Instant::now();
    let pre: PreImage = case::pre_image(case_v);
    let w: Written = case::written(case_v);
    rep.positions_read = pre.tables.get(case::POSITION).map_or(0, BTreeMap::len);
    rep.edges_read = pre.edges.len();
    rep.parent_links = pre.parent_links.len();
    rep.positions_written_v1 = w.updated.len() + w.inserted.len();
    if w.capture_errors > 0 {
        rep.incomplete.push(format!("commit capture errors={}", w.capture_errors));
    }
    // Positions that exist before the batch but whose columns the case never
    // read: both ends of a parent link, rows v1 updated without reading
    // them, ends of the edges v1 inserted that it did not create.  They load
    // with no column (every difference on them is `unread_input`).
    let mut rows = pre.positions();
    let mut known: BTreeSet<String> = rows.iter().filter_map(|r| r.get("key").and_then(Value::as_str).map(str::to_string)).collect();
    let existing = pre
        .parent_links
        .iter()
        .flat_map(|(p, c)| [p, c])
        .chain(w.updated.keys())
        .chain(w.edges.iter().flat_map(|((p, _), c)| [p, c]))
        .filter(|k| !w.inserted.contains_key(*k))
        .cloned()
        .collect::<Vec<String>>();
    for k in existing {
        if known.insert(k.clone()) {
            let mut r = Row::new();
            r.insert("key".into(), Value::String(k));
            rows.push(r);
        }
    }
    let mut g = ShimGraph::load(rows, pre.edges.clone());
    rep.load_us = t0.elapsed().as_micros() as u64;
    rep.stubs = (0..av2_model::GraphRead::len(&g)).filter(|&i| g.is_stub(av2_model::PosId(i))).count();
    let issues = std::mem::take(&mut g.issues);
    rep.load_issues = issues.len();
    for i in issues.iter().filter(|i| i.kind == "unrepresentable") {
        *rep.counts.entry("load_unrepresentable").or_default() += 1;
        if let Some(e) = &i.error {
            rep.diffs.push(Diff {
                class: "load_unrepresentable",
                key: i.key.clone(),
                column: e.column.clone(),
                v1: e.value.clone(),
                v2: Value::String(e.why.clone()),
            });
        }
    }
    let fatal: Vec<String> = issues
        .iter()
        .filter(|i| av2_shim::is_fatal(i))
        .map(|i| format!("{} {}{}", i.kind, i.key, i.error.as_ref().map(|e| format!(" {e}")).unwrap_or_default()))
        .collect();
    if case_v.get("error").is_some() {
        rep.incomplete.push(format!("v1 raised: {}", case_v["error"].as_str().unwrap_or("").chars().take(200).collect::<String>()));
    }
    let t1 = Instant::now();
    match mode {
        Mode::Noop => {}
        Mode::Echo => match echo(&mut g, &w) {
            Ok(lost) => {
                for e in lost {
                    *rep.counts.entry("unrepresentable").or_default() += 1;
                    if rep.diffs.len() < 200 {
                        rep.diffs.push(Diff {
                            class: "unrepresentable",
                            key: String::new(),
                            column: e.column.clone(),
                            v1: e.value.clone(),
                            v2: Value::String(e.why.clone()),
                        });
                    }
                }
            }
            Err(e) => {
                rep.status = "v2_error";
                rep.error = Some(e);
            }
        },
        Mode::V2 => {
            let Some(name) = entry_for(&kind) else {
                rep.status = "unknown_kind";
                return rep;
            };
            if !fatal.is_empty() {
                rep.status = "load_error";
                rep.error = Some(fatal.join("; "));
                return rep;
            }
            let args = json!({
                "args": [case_v.get("inputs").cloned().unwrap_or(Value::Null)],
                "kwargs": {
                    "seed_rows": case_v.get("seed_rows").cloned().unwrap_or(Value::Null),
                    "constants": case_v.get("constants").cloned().unwrap_or(Value::Null),
                    "settings": case_v.get("settings").cloned().unwrap_or(Value::Null),
                }
            });
            let tables = pre.other_tables();
            match registry::call(name, &mut g, &args, &tables) {
                Ok(_) => {}
                Err(e) => {
                    rep.status = if e.code() == "not_ported" { "not_ported" } else { "v2_error" };
                    rep.error = Some(e.to_string());
                    return rep;
                }
            }
        }
    }
    rep.run_us = t1.elapsed().as_micros() as u64;
    compare(&g, &w, &mut rep);
    if rep.status == "clean" && FAILURES.iter().any(|f| rep.counts.get(f).copied().unwrap_or(0) > 0) {
        rep.status = "diff";
    }
    if rep.status == "clean" && rep.incomplete.iter().any(|s| s.starts_with("v1 raised")) {
        rep.status = "v1_error";
    }
    rep
}

/// Write v1's writes through the v2 graph.
fn echo(g: &mut ShimGraph, w: &Written) -> Result<Vec<av2_shim::v1::MapError>, String> {
    let mut lost = Vec::new();
    for (key, cols) in &w.updated {
        let id = match g.id(key) {
            Some(id) => id,
            None => {
                let mut r = Row::new();
                r.insert("key".into(), Value::String(key.clone()));
                g.create_row(&r).map_err(|e| e.to_string())?.0
            }
        };
        let modeled: Row = cols.iter().filter(|(c, _)| v1::is_modeled(c)).map(|(c, v)| (c.clone(), v.clone())).collect();
        lost.extend(g.put_row(id, &modeled).map_err(|e| e.to_string())?);
    }
    for (key, row) in &w.inserted {
        let mut r: Row = row.iter().filter(|(c, _)| v1::is_modeled(c)).map(|(c, v)| (c.clone(), v.clone())).collect();
        r.insert("key".into(), Value::String(key.clone()));
        match g.id(key) {
            Some(id) => lost.extend(g.put_row(id, &r).map_err(|e| e.to_string())?),
            None => lost.extend(g.create_row(&r).map_err(|e| e.to_string())?.1),
        }
    }
    for ((p, m), c) in &w.edges {
        let (Some(pid), Some(cid)) = (g.id(p), g.id(c)) else { continue };
        let mv = av2_model::Move::parse(m).map_err(|e| e.to_string())?;
        g.add_edge(pid, mv, cid).map_err(|e| e.to_string())?;
    }
    Ok(lost)
}

fn compare(g: &ShimGraph, w: &Written, rep: &mut CaseReport) {
    let ch = g.changes();
    let v2_updated: BTreeMap<&str, &Row> = ch.updated.iter().map(|(k, r)| (k.as_str(), r)).collect();
    let v2_created: BTreeMap<&str, &Row> = ch.created.iter().filter_map(|r| r.get("key").and_then(Value::as_str).map(|k| (k, r))).collect();
    let push = |rep: &mut CaseReport, class: &'static str, key: &str, column: &str, a: Value, b: Value| {
        *rep.counts.entry(class).or_default() += 1;
        if class != "match" && class != "not_modeled" && rep.diffs.len() < 200 {
            rep.diffs.push(Diff { class, key: key.to_string(), column: column.to_string(), v1: a, v2: b });
        }
    };
    // Updates.
    let keys: BTreeSet<&str> = w.updated.keys().map(String::as_str).chain(v2_updated.keys().copied()).collect();
    for key in keys {
        if w.inserted.contains_key(key) || v2_created.contains_key(key) {
            continue;
        }
        let v1c = w.updated.get(key);
        let v2c = v2_updated.get(key).copied();
        let id = g.id(key);
        let now = id.map(|id| g.row(id));
        let read: BTreeSet<&str> = id.map(|id| g.given_columns(id).iter().map(String::as_str).collect()).unwrap_or_default();
        let stub = id.is_none_or(|id| g.is_stub(id));
        let cols: BTreeSet<&str> = v1c
            .into_iter()
            .flat_map(|r| r.keys().map(String::as_str))
            .chain(v2c.into_iter().flat_map(|r| r.keys().map(String::as_str)))
            .collect();
        for col in cols {
            let a = v1c.and_then(|r| r.get(col));
            let b = v2c.and_then(|r| r.get(col));
            if !v1::is_modeled(col) {
                if a.is_some() {
                    push(rep, "not_modeled", key, col, a.cloned().unwrap_or(Value::Null), Value::Null);
                }
                continue;
            }
            let current = now.as_ref().and_then(|r| r.get(col)).cloned().unwrap_or(Value::Null);
            let class = match (a, b) {
                (Some(a), Some(b)) if norm(col, a) == norm(col, b) => "match",
                (Some(_), Some(_)) => "value_diff",
                (Some(a), None) if norm(col, a) == norm(col, &current) => "match",
                (Some(_), None) => "missing_write",
                (None, Some(_)) => "extra_write",
                (None, None) => continue,
            };
            let class = if class != "match" && (stub || !read.contains(col)) { "unread_input" } else { class };
            push(rep, class, key, col, a.cloned().unwrap_or(Value::Null), b.cloned().unwrap_or(current));
        }
    }
    // Inserted rows.
    let ins: BTreeSet<&str> = w.inserted.keys().map(String::as_str).chain(v2_created.keys().copied()).collect();
    for key in ins {
        match (w.inserted.get(key), v2_created.get(key)) {
            (Some(a), Some(b)) => {
                for col in v1::MODELED {
                    let (x, y) = (a.get(*col).cloned().unwrap_or(Value::Null), b.get(*col).cloned().unwrap_or(Value::Null));
                    let class = if norm(col, &x) == norm(col, &y) { "match" } else { "value_diff" };
                    push(rep, class, key, col, x, y);
                }
            }
            (Some(a), None) => {
                // Echo creates through `put_row` when the row was read before: compare to the graph.
                match g.id(key) {
                    Some(id) => {
                        let now = g.row(id);
                        for col in v1::MODELED {
                            let x = a.get(*col).cloned().unwrap_or(Value::Null);
                            let y = now.get(*col).cloned().unwrap_or(Value::Null);
                            let class = if norm(col, &x) == norm(col, &y) { "match" } else { "missing_write" };
                            push(rep, class, key, col, x, y);
                        }
                    }
                    None => push(rep, "missing_row", key, "", Value::Object(a.clone()), Value::Null),
                }
            }
            (None, Some(b)) => push(rep, "extra_row", key, "", Value::Null, Value::Object((*b).clone())),
            (None, None) => {}
        }
    }
    // Edges.
    let v2_edges: BTreeMap<(String, String), String> =
        ch.edges.iter().map(|e: &EdgeRow| ((e.parent.clone(), e.mv.clone()), e.child.clone())).collect();
    let ekeys: BTreeSet<&(String, String)> = w.edges.keys().chain(v2_edges.keys()).collect();
    for k in ekeys {
        let label = format!("{} {}", k.0, k.1);
        match (w.edges.get(k), v2_edges.get(k)) {
            (Some(a), Some(b)) if a == b => push(rep, "match", &label, "edge", json!(a), json!(b)),
            (Some(a), Some(b)) => push(rep, "edge_child_diff", &label, "edge", json!(a), json!(b)),
            // v1 inserts edges with ON CONFLICT DO NOTHING: an edge that was
            // already there is "inserted" without changing anything.
            (Some(a), None) if g.edge_child(&k.0, &k.1).as_deref() == Some(a.as_str()) => {
                push(rep, "match", &label, "edge", json!(a), json!(a))
            }
            (Some(a), None) => push(rep, "missing_edge", &label, "edge", json!(a), json!(g.edge_child(&k.0, &k.1))),
            (None, Some(b)) => push(rep, "extra_edge", &label, "edge", Value::Null, json!(b)),
            (None, None) => {}
        }
    }
}

// ---------------------------------------------------------------- summary

#[derive(Default, Serialize)]
pub struct Summary {
    pub mode: Option<Mode>,
    pub files: Vec<String>,
    pub truncated_files: Vec<String>,
    pub bad_cases: u64,
    pub cases: u64,
    pub by_kind: BTreeMap<String, u64>,
    pub by_status: BTreeMap<String, BTreeMap<&'static str, u64>>,
    pub by_class: BTreeMap<&'static str, u64>,
    /// class -> column -> count (failures and `unread_input` only).
    pub by_column: BTreeMap<&'static str, BTreeMap<String, u64>>,
    pub incomplete_cases: u64,
    pub incomplete_reasons: BTreeMap<String, u64>,
    pub positions_read: u64,
    pub edges_read: u64,
    pub parent_links: u64,
    pub stubs: u64,
    pub positions_written_v1: u64,
    pub load_us: Vec<u64>,
    pub run_us: Vec<u64>,
    pub examples: Vec<Value>,
}

impl Summary {
    pub fn add(&mut self, r: &CaseReport, examples: usize) {
        self.cases += 1;
        *self.by_kind.entry(r.kind.clone()).or_default() += 1;
        *self.by_status.entry(r.kind.clone()).or_default().entry(r.status).or_default() += 1;
        for (c, n) in &r.counts {
            *self.by_class.entry(c).or_default() += n;
        }
        for d in &r.diffs {
            if d.class != "match" && d.class != "not_modeled" {
                *self.by_column.entry(d.class).or_default().entry(d.column.clone()).or_default() += 1;
            }
        }
        if !r.incomplete.is_empty() {
            self.incomplete_cases += 1;
            for why in &r.incomplete {
                let k = why.split(['=', ':']).next().unwrap_or(why).to_string();
                *self.incomplete_reasons.entry(k).or_default() += 1;
            }
        }
        self.positions_read += r.positions_read as u64;
        self.edges_read += r.edges_read as u64;
        self.parent_links += r.parent_links as u64;
        self.stubs += r.stubs as u64;
        self.positions_written_v1 += r.positions_written_v1 as u64;
        self.load_us.push(r.load_us);
        self.run_us.push(r.run_us);
        if self.examples.len() < examples && r.status != "clean" {
            let mut e = serde_json::to_value(r).unwrap_or(Value::Null);
            if let Some(d) = e.get_mut("diffs").and_then(Value::as_array_mut) {
                d.truncate(12);
            }
            self.examples.push(e);
        }
    }

    pub fn json(&self) -> Value {
        let mut v = serde_json::to_value(self).unwrap_or(Value::Null);
        if let Some(o) = v.as_object_mut() {
            o.remove("load_us");
            o.remove("run_us");
            o.insert("load_us".into(), percentiles(&self.load_us));
            o.insert("run_us".into(), percentiles(&self.run_us));
        }
        v
    }

    pub fn text(&self) -> String {
        let mut s = String::new();
        let mode = self.mode.map(|m| format!("{m:?}").to_lowercase()).unwrap_or_default();
        s += &format!("av2-golden  mode={mode}  cases={}  files={}  bad_cases={}\n", self.cases, self.files.len(), self.bad_cases);
        for f in &self.truncated_files {
            s += &format!("  truncated: {f}\n");
        }
        for (kind, st) in &self.by_status {
            let parts: Vec<String> = st.iter().map(|(k, n)| format!("{k}={n}")).collect();
            s += &format!("  {kind:8} {}\n", parts.join("  "));
        }
        s += &format!(
            "  read: {} positions, {} edges, {} parent links without a move, {} stubs; v1 wrote {} positions\n",
            self.positions_read, self.edges_read, self.parent_links, self.stubs, self.positions_written_v1
        );
        if self.incomplete_cases > 0 {
            let parts: Vec<String> = self.incomplete_reasons.iter().map(|(k, n)| format!("{k}={n}")).collect();
            s += &format!("  incomplete cases: {} ({})\n", self.incomplete_cases, parts.join(", "));
        }
        let parts: Vec<String> = self.by_class.iter().map(|(k, n)| format!("{k}={n}")).collect();
        s += &format!("  fields: {}\n", parts.join("  "));
        for (class, cols) in &self.by_column {
            let mut v: Vec<_> = cols.iter().collect();
            v.sort_by_key(|(_, n)| std::cmp::Reverse(**n));
            let parts: Vec<String> = v.iter().take(12).map(|(c, n)| format!("{c}={n}")).collect();
            s += &format!("    {class:15} {}\n", parts.join("  "));
        }
        let p = percentiles(&self.load_us);
        let r = percentiles(&self.run_us);
        s += &format!(
            "  load us p50={} p99={} max={}; run us p50={} p99={} max={}\n",
            p["p50"], p["p99"], p["max"], r["p50"], r["p99"], r["max"]
        );
        s
    }
}

fn percentiles(v: &[u64]) -> Value {
    if v.is_empty() {
        return json!({"n": 0, "p50": 0, "p99": 0, "max": 0});
    }
    let mut s = v.to_vec();
    s.sort_unstable();
    let at = |q: f64| s[((s.len() - 1) as f64 * q).round() as usize];
    json!({"n": s.len(), "p50": at(0.5), "p99": at(0.99), "max": s[s.len() - 1]})
}

/// Run a whole set of files.
pub fn run_files(
    files: &[std::path::PathBuf],
    mode: Mode,
    kind: Option<&str>,
    limit: Option<u64>,
    examples: usize,
    mut per_case: impl FnMut(&CaseReport),
) -> Summary {
    let mut sum = Summary { mode: Some(mode), ..Summary::default() };
    'files: for f in files {
        let name = f.display().to_string();
        sum.files.push(name.clone());
        let mut cases = match crate::read::open(f) {
            Ok(c) => c,
            Err(e) => {
                sum.truncated_files.push(format!("{name}: {e}"));
                continue;
            }
        };
        for c in cases.by_ref() {
            if limit.is_some_and(|l| sum.cases >= l) {
                break 'files;
            }
            let c = match c {
                Ok(c) => c,
                Err(_) => {
                    sum.bad_cases += 1;
                    continue;
                }
            };
            if kind.is_some_and(|k| c.get("kind").and_then(Value::as_str) != Some(k)) {
                continue;
            }
            let r = replay(&c, &name, mode);
            per_case(&r);
            sum.add(&r, examples);
        }
        if let Some(t) = cases.truncated {
            sum.truncated_files.push(format!("{name}: {t}"));
        }
    }
    sum
}

/// Helper for callers that want the raw tables of a case (debugging).
pub fn tables_of(case_v: &Value) -> Map<String, Value> {
    case::pre_image(case_v).other_tables()
}
