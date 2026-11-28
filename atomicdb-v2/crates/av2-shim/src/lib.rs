//! AtomicDB v2 shim: the semantic test bridges' common part (plan sections
//! 7.1 and 7.2).
//!
//! - [`v1`]: a v1 `atomicdb_position` row <-> the v2 records, exact or an
//!   error naming the column;
//! - [`graph`]: `ShimGraph`, v1 rows loaded into the in-memory v2 graph,
//!   and the rows to write back after a call;
//! - [`registry`]: the v2 entry points by v1 name (one line per ported
//!   function), called by the Python shim (`av2-py`) and by the golden-case
//!   replay (`av2-golden`);
//! - [`call_json`]: one call as a JSON request and reply, the whole
//!   protocol between Python and Rust.

pub mod graph;
pub mod registry;
pub mod v1;

pub use graph::{Changes, EdgeRow, LoadIssue, ShimGraph};
pub use registry::{Args, Ctx, ENTRIES, Entry, EntryError, EntryFn, Outcome};

use serde_json::{Map, Value, json};

/// Load issues that make a call meaningless (the graph would not be what
/// v1 holds).  A fingerprint collision only affects `GraphRead::lookup`.
pub fn is_fatal(issue: &LoadIssue) -> bool {
    issue.kind != "fingerprint_collision"
}

/// The JSON form of a call's outcome (the reply `call_json` returns).
pub fn outcome_json(o: Outcome, issues: &[LoadIssue]) -> Value {
    let updated: Map<String, Value> = o.changes.updated.into_iter().map(|(k, r)| (k, Value::Object(r))).collect();
    let facts: Vec<Value> = o.facts.iter().map(|f| serde_json::from_str(&f.to_json_line()).unwrap_or(Value::Null)).collect();
    json!({
        "ok": true,
        "result": o.result,
        "updated": updated,
        "created": o.changes.created,
        "edges": o.changes.edges,
        "facts": facts,
        "writes": o.writes,
        "issues": issues,
    })
}

pub fn error_json(e: &EntryError, issues: &[LoadIssue]) -> Value {
    json!({"ok": false, "error": e.code(), "message": e.to_string(), "issues": issues})
}

/// One call: `{"positions": [row...], "edges": [edge...], "tables": {...},
/// "args": {"args": [...], "kwargs": {...}}}` -> the reply.
pub fn call_json(name: &str, request: &Value) -> Value {
    let rows: Vec<v1::Row> = request
        .get("positions")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|r| r.as_object().cloned()).collect())
        .unwrap_or_default();
    let edges: Vec<EdgeRow> = request.get("edges").and_then(|e| serde_json::from_value(e.clone()).ok()).unwrap_or_default();
    let empty = Map::new();
    let tables = request.get("tables").and_then(Value::as_object).unwrap_or(&empty);
    let args = request.get("args").cloned().unwrap_or(Value::Null);
    let mut g = ShimGraph::load(rows, edges);
    let issues = std::mem::take(&mut g.issues);
    let fatal: Vec<v1::MapError> = issues.iter().filter(|i| is_fatal(i)).filter_map(|i| i.error.clone()).collect();
    if issues.iter().any(is_fatal) {
        let e = if fatal.is_empty() {
            EntryError::BadArgs(format!(
                "the rows do not load: {}",
                issues.iter().map(|i| format!("{} {}", i.kind, i.key)).collect::<Vec<_>>().join(", ")
            ))
        } else {
            EntryError::Unrepresentable(fatal)
        };
        return error_json(&e, &issues);
    }
    match registry::call(name, &mut g, &args, tables) {
        Ok(o) => outcome_json(o, &issues),
        Err(e) => error_json(&e, &issues),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_call_roundtrip() {
        let a = "a81b39f292eca4c6959bf6d1a81b39f292eca4c6959bf6d1a81b39f292eca4c6";
        let req = json!({
            "positions": [{"key": a, "eval_cp": 3, "status": "UNKNOWN"}],
            "edges": [],
            "args": {"args": [{a: {"status": "DRAW", "closure": "MINIMAX"}}]},
        });
        let r = call_json("shim.put_rows", &req);
        assert_eq!(r["ok"], json!(true), "{r}");
        assert_eq!(r["updated"][a], json!({"status": "DRAW", "closure": "MINIMAX"}));
        let r = call_json("ingest.not_a_function", &req);
        assert_eq!(r["error"], json!("not_ported"));
        let bad = json!({"positions": [{"key": a, "eval_cp": 1_000_000}]});
        assert_eq!(call_json("shim.echo", &bad)["error"], json!("unrepresentable"));
        let r = call_json("shim.fact", &json!({"args": {"args": ["NODE_CLOSED", {"key": a}]}}));
        assert_eq!(r["facts"][0]["kind"], json!("opaque"), "{r}");
    }
}
