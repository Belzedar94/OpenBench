//! One recorded case: what the batch read (rebuilt as the first image of
//! every row it could attribute), and what it wrote.
//!
//! `pre_image` is a port of `golden_cases.pre_image` (same attribution rule,
//! including the join of a position read through an edge), plus what v2
//! needs that the Python helper leaves implicit: the edges.  The drainer
//! reads children as `edge JOIN position` without selecting the edge's id,
//! so an edge is taken from any row that carries its parent, move and child;
//! a row that only says "this position has that parent" (the ascent's
//! `SELECT parent_id ... WHERE child_id = %s`) gives a parent link without a
//! move, which v2 cannot hold as an edge and which is counted instead.

use av2_shim::EdgeRow;
use av2_shim::v1::Row;
use regex::Regex;
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::LazyLock;

pub const POSITION: &str = "atomicdb_position";
pub const EDGE: &str = "atomicdb_edge";

static JOIN_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)"atomicdb_edge"\."(child_id|parent_id)"\s*=\s*(?:T\d+|"atomicdb_position")\."key""#).unwrap());
static JOIN_POS_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?i)JOIN\s+"atomicdb_position""#).unwrap());
static CHILD_EQ_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?i)WHERE\s+"atomicdb_edge"\."child_id"\s*=\s*%s\s*$"#).unwrap());

/// `golden_cases._join_pk_index`.
fn join_pk_index(sql: &str, cols: &[String], tables: &[Option<String>], table: &str) -> Option<usize> {
    if table != POSITION || sql.is_empty() {
        return None;
    }
    let joins: BTreeSet<String> = JOIN_RE.captures_iter(sql).map(|c| c[1].to_lowercase()).collect();
    if joins.len() != 1 || JOIN_POS_RE.find_iter(sql).count() != 1 {
        return None;
    }
    let fk = joins.into_iter().next()?;
    cols.iter().zip(tables).position(|(c, t)| t.as_deref() == Some(EDGE) && *c == fk)
}

fn pk_of(table: &str) -> &'static str {
    match table {
        POSITION => "key",
        _ => "id",
    }
}

/// Map a JSON value used as a primary key to a string key.
fn pk_string(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// What a case read.
#[derive(Default, Debug)]
pub struct PreImage {
    /// table -> pk -> first value read of each column.
    pub tables: BTreeMap<String, BTreeMap<String, Row>>,
    /// Edges with parent, move and child known, in reading order (deduplicated).
    pub edges: Vec<EdgeRow>,
    /// (parent, child) known only as a link.
    pub parent_links: BTreeSet<(String, String)>,
}

impl PreImage {
    pub fn positions(&self) -> Vec<Row> {
        self.tables
            .get(POSITION)
            .map(|t| {
                t.iter()
                    .map(|(k, r)| {
                        let mut row = r.clone();
                        row.insert("key".into(), Value::String(k.clone()));
                        row
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// The tables other than positions and edges, as `{table: [rows]}`.
    pub fn other_tables(&self) -> Map<String, Value> {
        self.tables
            .iter()
            .filter(|(t, _)| t.as_str() != POSITION && t.as_str() != EDGE)
            .map(|(t, rows)| (t.clone(), Value::Array(rows.values().map(|r| Value::Object(r.clone())).collect())))
            .collect()
    }
}

pub fn pre_image(case: &Value) -> PreImage {
    let mut out = PreImage::default();
    let mut seen_edges: HashMap<(String, String), usize> = HashMap::new();
    let empty = vec![];
    for entry in case.get("queries").and_then(Value::as_array).unwrap_or(&empty) {
        let (Some(rows), Some(tables), Some(cols)) = (
            entry.get("rows").and_then(Value::as_array),
            entry.get("tables").and_then(Value::as_array),
            entry.get("cols").and_then(Value::as_array),
        ) else {
            continue;
        };
        if rows.is_empty() {
            continue;
        }
        let sql = entry.get("sql").and_then(Value::as_str).unwrap_or("");
        let cols: Vec<String> = cols.iter().map(|c| c.as_str().unwrap_or("").to_string()).collect();
        let tables: Vec<Option<String>> = tables.iter().map(|t| t.as_str().map(str::to_string)).collect();
        let mut by_table: BTreeMap<&str, Vec<(usize, &str)>> = BTreeMap::new();
        for (i, (c, t)) in cols.iter().zip(&tables).enumerate() {
            if let Some(t) = t {
                by_table.entry(t.as_str()).or_default().push((i, c.as_str()));
            }
        }
        for (table, fields) in &by_table {
            let pk = pk_of(table);
            let pk_index = fields.iter().find(|(_, c)| *c == pk).map(|(i, _)| *i).or_else(|| join_pk_index(sql, &cols, &tables, table));
            let Some(pk_index) = pk_index else { continue };
            let target = out.tables.entry(table.to_string()).or_default();
            for row in rows {
                let Some(row) = row.as_array() else { continue };
                let Some(pk) = row.get(pk_index).and_then(pk_string) else { continue };
                let image = target.entry(pk).or_default();
                for (i, c) in fields {
                    if let Some(v) = row.get(*i) {
                        image.entry(c.to_string()).or_insert_with(|| v.clone());
                    }
                }
            }
        }
        // Edges.
        let edge_col = |name: &str| cols.iter().zip(&tables).position(|(c, t)| t.as_deref() == Some(EDGE) && c == name);
        let (p, m, c, id) = (edge_col("parent_id"), edge_col("move_uci"), edge_col("child_id"), edge_col("id"));
        let single_child = CHILD_EQ_RE
            .is_match(sql)
            .then(|| entry.get("params").and_then(Value::as_array).and_then(|a| a.first()).and_then(Value::as_str))
            .flatten();
        for row in rows {
            let Some(row) = row.as_array() else { continue };
            let get = |i: Option<usize>| i.and_then(|i| row.get(i)).and_then(Value::as_str).map(str::to_string);
            let parent = get(p);
            let child = get(c).or_else(|| single_child.map(str::to_string));
            match (parent, get(m), child) {
                (Some(parent), Some(mv), Some(child)) => {
                    let eid = id.and_then(|i| row.get(i)).and_then(Value::as_i64);
                    let k = (parent.clone(), mv.clone());
                    match seen_edges.get(&k) {
                        Some(&n) => {
                            if out.edges[n].id.is_none() {
                                out.edges[n].id = eid;
                            }
                        }
                        None => {
                            seen_edges.insert(k, out.edges.len());
                            out.edges.push(EdgeRow { id: eid, parent, mv, child });
                        }
                    }
                }
                (Some(parent), None, Some(child)) => {
                    out.parent_links.insert((parent, child));
                }
                _ => {}
            }
        }
    }
    // A link already covered by an edge is not missing anything.
    let covered: BTreeSet<(String, String)> = out.edges.iter().map(|e| (e.parent.clone(), e.child.clone())).collect();
    out.parent_links.retain(|l| !covered.contains(l));
    out
}

/// What the case wrote, merged over its commits (a ROLLBACK discards
/// nothing here: the recorder only lists rows at a COMMIT).
#[derive(Default, Debug)]
pub struct Written {
    /// key -> columns written (changed from the first image, or set and
    /// never read).
    pub updated: BTreeMap<String, Row>,
    /// key -> whole row of a position the batch inserted.
    pub inserted: BTreeMap<String, Row>,
    /// Edges inserted: (parent, move) -> child.
    pub edges: BTreeMap<(String, String), String>,
    pub rollbacks: usize,
    pub commits: usize,
    /// Keys the post image did not find (deleted or not visible).
    pub unseen: BTreeSet<String>,
    pub capture_errors: usize,
}

pub fn written(case: &Value) -> Written {
    let mut out = Written::default();
    let empty = vec![];
    for c in case.get("commits").and_then(Value::as_array).unwrap_or(&empty) {
        match c.get("at").and_then(Value::as_str) {
            Some("rollback") => {
                out.rollbacks += 1;
                continue;
            }
            _ => out.commits += 1,
        }
        if c.get("capture_error").is_some() {
            out.capture_errors += 1;
        }
        if let Some(pos) = c.get("positions").and_then(Value::as_object) {
            for (key, cols) in pos {
                let Some(cols) = cols.as_object() else { continue };
                if let Some(row) = cols.get("$row").and_then(Value::as_object) {
                    out.inserted.insert(key.clone(), row.clone());
                    out.updated.remove(key);
                } else if let Some(ins) = out.inserted.get_mut(key) {
                    ins.extend(cols.clone());
                } else {
                    out.updated.entry(key.clone()).or_default().extend(cols.clone());
                }
            }
        }
        for e in c.get("edges_inserted").and_then(Value::as_array).unwrap_or(&empty) {
            let get = |n: &str| e.get(n).and_then(Value::as_str).map(str::to_string);
            if let (Some(p), Some(m), Some(ch)) = (get("parent_id"), get("move_uci"), get("child_id")) {
                out.edges.insert((p, m), ch);
            }
        }
        for k in c.get("deleted_or_unseen").and_then(Value::as_array).unwrap_or(&empty) {
            if let Some(k) = k.as_str() {
                out.unseen.insert(k.to_string());
            }
        }
    }
    out
}

/// Reasons a case cannot be replayed faithfully, from its own counters.
pub fn incompleteness(case: &Value) -> Vec<String> {
    let mut why = Vec::new();
    if case.get("oversize").and_then(Value::as_bool) == Some(true) {
        why.push("oversize: the rows of its SELECTs were dropped".to_string());
    }
    if let Some(s) = case.get("stats").and_then(Value::as_object) {
        for k in ["uncaptured_selects", "unkeyed_position_writes", "capture_errors"] {
            if let Some(n) = s.get(k).and_then(Value::as_u64).filter(|&n| n > 0) {
                why.push(format!("{k}={n}"));
            }
        }
    }
    why
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rebuilds_rows_edges_and_writes() {
        let case = json!({
            "queries": [
                {"sql": "SELECT \"atomicdb_position\".\"key\", \"atomicdb_position\".\"status\" FROM \"atomicdb_position\" WHERE ...",
                 "cols": ["key", "status"], "tables": ["atomicdb_position", "atomicdb_position"], "rows": [["p", "UNKNOWN"]]},
                {"sql": "SELECT \"atomicdb_edge\".\"parent_id\", \"atomicdb_edge\".\"move_uci\", T3.\"status\", \"atomicdb_edge\".\"child_id\" FROM \"atomicdb_edge\" INNER JOIN \"atomicdb_position\" T3 ON (\"atomicdb_edge\".\"child_id\" = T3.\"key\") WHERE \"atomicdb_edge\".\"parent_id\" IN (%s)",
                 "cols": ["parent_id", "move_uci", "status", "child_id"],
                 "tables": ["atomicdb_edge", "atomicdb_edge", "atomicdb_position", "atomicdb_edge"],
                 "rows": [["p", "e2e4", "WHITE_WIN", "c"], ["p", "d2d4", "UNKNOWN", "d"]]},
                {"sql": "SELECT \"atomicdb_edge\".\"parent_id\" FROM \"atomicdb_edge\" WHERE \"atomicdb_edge\".\"child_id\" = %s",
                 "params": ["p"], "cols": ["parent_id"], "tables": ["atomicdb_edge"], "rows": [["gp"]]}
            ],
            "commits": [
                {"at": "commit", "positions": {"p": {"status": "WHITE_WIN", "updated": "x"}}},
                {"at": "rollback"},
                {"at": "commit", "positions": {"n": {"$row": {"key": "n", "status": "UNKNOWN"}}},
                 "edges_inserted": [{"parent_id": "p", "move_uci": "a2a3", "child_id": "n"}]}
            ]
        });
        let pre = pre_image(&case);
        let pos = &pre.tables[POSITION];
        assert_eq!(pos["p"]["status"], json!("UNKNOWN"));
        assert_eq!(pos["c"]["status"], json!("WHITE_WIN"));
        assert_eq!(pre.edges.len(), 2);
        assert_eq!(pre.parent_links.iter().next().unwrap(), &("gp".to_string(), "p".to_string()));
        let w = written(&case);
        assert_eq!(w.updated["p"]["status"], json!("WHITE_WIN"));
        assert!(w.inserted.contains_key("n"));
        assert_eq!(w.edges[&("p".into(), "a2a3".into())], "n");
        assert_eq!((w.commits, w.rollbacks), (2, 1));
    }
}
