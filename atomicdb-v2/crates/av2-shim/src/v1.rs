//! A v1 `atomicdb_position` row (column name -> JSON value, as Django's
//! `values()` or the golden-case recorder give it) and its v2 records.
//!
//! The mapping is the loader's (`av2-tools/src/export.rs`), written for JSON
//! instead of CSV, and in both directions: the shim exports rows into a v2
//! graph, and writes back the columns a v2 function changed.  Anything a v1
//! row holds that the v2 records cannot hold exactly is an error naming the
//! column, never an approximation: the shim reports it instead of running.

use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED, F_REACHABLE, NO_CP, NO_MATE, NO_SLACK};
use av2_model::{Closure, Core, Ext, Move, Proof, Status};
use serde::Serialize;
use serde_json::{Map, Number, Value};

/// A row: column name -> value.
pub type Row = Map<String, Value>;

/// The primary key column (sha256 hex of the canonical FEN).
pub const KEY: &str = "key";

/// The columns of `atomicdb_position` that v2 holds, in v1's order.
pub const MODELED: &[&str] = &[
    "fen",
    "eval_cp",
    "status",
    "closure",
    "proof",
    "best_move",
    "backed_eval",
    "backed_move",
    "backed_plies",
    "backed_nodes",
    "won_line",
    "mate_in",
    "clock_slack",
    "last_analysis",
    "analysis_passes",
    "expanded",
    "reachable",
    "depth_invested",
    "nodes_invested",
    "time_invested",
    "visits",
    "campaign_id",
];

/// Columns v2 does not store, and why.  A difference in one of them is
/// reported as `not_modeled`, never as a failure.
pub const NOT_MODELED: &[(&str, &str)] =
    &[("priority", "computed by the selector in memory (owner decision D5)"), ("updated", "replaced by the record version (plan 2.2.4)")];

/// The JSON columns: v1 hands them over either parsed (Django) or as JSON
/// text (a raw cursor); both are normalised to the parsed value.
pub const JSON_COLUMNS: &[&str] = &["last_analysis", "analysis_passes"];

pub fn is_modeled(column: &str) -> bool {
    MODELED.contains(&column)
}

pub fn not_modeled_reason(column: &str) -> Option<&'static str> {
    NOT_MODELED.iter().find(|(c, _)| *c == column).map(|(_, r)| *r)
}

/// A value the v2 records cannot hold.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MapError {
    pub column: String,
    pub value: Value,
    pub why: String,
}

impl std::fmt::Display for MapError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} = {}: {}", self.column, self.value, self.why)
    }
}

/// The v2 form of one row: the two records, the FEN and the blob contents
/// (the graph turns them into blob references).
#[derive(Debug, Clone, PartialEq)]
pub struct Encoded {
    pub core: Core,
    pub ext: Ext,
    pub fen: Option<String>,
    /// `last_analysis` as compact JSON text; None = NULL.
    pub analysis: Option<Vec<u8>>,
    /// `analysis_passes` as compact JSON text; None = the empty list (v1's default).
    pub passes: Option<Vec<u8>>,
    /// `won_line` text; None = NULL.
    pub won_line: Option<Vec<u8>>,
}

impl Default for Encoded {
    /// A row with every column at its v1 default.
    fn default() -> Encoded {
        Encoded { core: Core::default(), ext: Ext::default(), fen: None, analysis: None, passes: None, won_line: None }
    }
}

impl Encoded {
    /// Does this position need an ext record?
    pub fn needs_ext(&self) -> bool {
        !self.ext.is_default() || self.analysis.is_some() || self.passes.is_some() || self.won_line.is_some()
    }
}

/// Parse a JSON column that may arrive as JSON text.
pub fn normalise_json(v: &Value) -> Value {
    match v {
        Value::String(s) => serde_json::from_str(s).unwrap_or_else(|_| v.clone()),
        other => other.clone(),
    }
}

/// The value of a column normalised for comparison (JSON columns parsed,
/// integral floats of integer columns made integers).
pub fn normalise(column: &str, v: &Value) -> Value {
    if JSON_COLUMNS.contains(&column) {
        let n = normalise_json(v);
        if column == "analysis_passes" && n.is_null() {
            return Value::Array(vec![]);
        }
        return n;
    }
    match (column, v) {
        ("time_invested", Value::Number(n)) => n.as_f64().and_then(Number::from_f64).map(Value::Number).unwrap_or_else(|| v.clone()),
        ("closure" | "proof" | "best_move" | "backed_move", Value::String(s)) if s.is_empty() => Value::Null,
        _ => v.clone(),
    }
}

fn err(column: &str, value: &Value, why: impl Into<String>) -> MapError {
    MapError { column: column.to_string(), value: value.clone(), why: why.into() }
}

fn as_i64(column: &str, v: &Value) -> Result<Option<i64>, MapError> {
    match v {
        Value::Null => Ok(None),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(Some(i))
            } else if let Some(u) = n.as_u64() {
                i64::try_from(u).map(Some).map_err(|_| err(column, v, "out of the 64-bit signed range"))
            } else {
                let f = n.as_f64().unwrap_or(f64::NAN);
                if f.fract() == 0.0 && f.abs() < 9.0e15 { Ok(Some(f as i64)) } else { Err(err(column, v, "not an integer")) }
            }
        }
        Value::String(s) => s.trim().parse::<i64>().map(Some).map_err(|_| err(column, v, "not an integer")),
        Value::Bool(b) => Ok(Some(*b as i64)),
        _ => Err(err(column, v, "not an integer")),
    }
}

fn as_bool(column: &str, v: &Value) -> Result<bool, MapError> {
    match v {
        Value::Bool(b) => Ok(*b),
        Value::Null => Ok(false),
        Value::Number(n) if n.as_i64() == Some(0) || n.as_i64() == Some(1) => Ok(n.as_i64() == Some(1)),
        Value::String(s) => match s.as_str() {
            "t" | "true" | "True" | "1" => Ok(true),
            "f" | "false" | "False" | "0" | "" => Ok(false),
            _ => Err(err(column, v, "not a boolean")),
        },
        _ => Err(err(column, v, "not a boolean")),
    }
}

fn as_text<'a>(column: &str, v: &'a Value) -> Result<Option<&'a str>, MapError> {
    match v {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s)),
        _ => Err(err(column, v, "not text")),
    }
}

fn cp(column: &str, v: &Value) -> Result<i16, MapError> {
    match as_i64(column, v)? {
        None => Ok(NO_CP),
        Some(x) if x > NO_CP as i64 && x <= i16::MAX as i64 => Ok(x as i16),
        Some(_) => Err(err(column, v, "does not fit the 16-bit centipawn column")),
    }
}

fn mv(column: &str, v: &Value) -> Result<Move, MapError> {
    match as_text(column, v)? {
        None => Ok(Move::NONE),
        Some("") => Err(err(column, v, "the empty string is not NULL and not a move")),
        Some(s) => Move::parse(s).map_err(|e| err(column, v, e.to_string())),
    }
}

fn ranged<T: TryFrom<i64>>(column: &str, v: &Value, default: T) -> Result<T, MapError> {
    match as_i64(column, v)? {
        None => Ok(default),
        Some(x) => T::try_from(x).map_err(|_| err(column, v, "out of range for the v2 field")),
    }
}

/// Set the columns present in `row` on `enc`; columns absent keep their
/// value.  Unknown columns are ignored here (the caller classifies them).
pub fn apply(enc: &mut Encoded, row: &Row) -> Vec<MapError> {
    let mut errors = Vec::new();
    for (column, v) in row {
        if let Err(e) = apply_one(enc, column, v) {
            errors.push(e);
        }
    }
    errors
}

/// Encode a whole (or partial) row over the v1 defaults.
pub fn encode(row: &Row) -> Result<Encoded, Vec<MapError>> {
    let mut enc = Encoded::default();
    let errors = apply(&mut enc, row);
    if errors.is_empty() { Ok(enc) } else { Err(errors) }
}

fn apply_one(enc: &mut Encoded, column: &str, v: &Value) -> Result<(), MapError> {
    let c = &mut enc.core;
    match column {
        "fen" => {
            let fen = as_text(column, v)?.map(str::to_string);
            let black = fen.as_deref().and_then(|f| f.split(' ').nth(1)) == Some("b");
            c.set_flag(F_BLACK_TO_MOVE, black);
            enc.fen = fen;
        }
        "status" => {
            let s = as_text(column, v)?.unwrap_or("");
            let st = Status::from_v1(s).ok_or_else(|| err(column, v, "unknown status"))?;
            c.set_status(st, c.closure(), c.proof());
        }
        "closure" => {
            let s = as_text(column, v)?.unwrap_or("");
            let cl = Closure::from_v1(s).ok_or_else(|| err(column, v, "unknown closure"))?;
            c.set_status(c.status(), cl, c.proof());
        }
        "proof" => {
            let s = as_text(column, v)?.unwrap_or("");
            let p = Proof::from_v1(s).ok_or_else(|| err(column, v, "unknown proof"))?;
            c.set_status(c.status(), c.closure(), p);
        }
        "eval_cp" => c.eval_cp = cp(column, v)?,
        "backed_eval" => c.backed_eval = cp(column, v)?,
        "best_move" => c.best_move = mv(column, v)?,
        "backed_move" => c.backed_move = mv(column, v)?,
        "backed_plies" => c.backed_plies = ranged(column, v, 0u16)?,
        "mate_in" => {
            c.mate_in = match as_i64(column, v)? {
                None => NO_MATE,
                Some(x) if (0..NO_MATE as i64).contains(&x) => x as u16,
                Some(_) => return Err(err(column, v, "outside 0..65534")),
            }
        }
        "clock_slack" => {
            c.clock_slack = match as_i64(column, v)? {
                None => NO_SLACK,
                Some(x) if (0..NO_SLACK as i64).contains(&x) => x as u8,
                Some(_) => return Err(err(column, v, "outside 0..254")),
            }
        }
        "expanded" => c.set_flag(F_EXPANDED, as_bool(column, v)?),
        "reachable" => c.set_flag(F_REACHABLE, as_bool(column, v)?),
        "backed_nodes" => enc.ext.backed_nodes = ranged(column, v, 0u64)?,
        "nodes_invested" => enc.ext.nodes_invested = ranged(column, v, 0u64)?,
        "depth_invested" => enc.ext.depth_invested = ranged(column, v, 0i32)?,
        "visits" => enc.ext.visits = ranged(column, v, 0i32)?,
        "campaign_id" => {
            enc.ext.campaign = match as_i64(column, v)? {
                None => 0,
                Some(x) if x > 0 && x <= u32::MAX as i64 => x as u32,
                Some(_) => return Err(err(column, v, "campaign id outside 1..2^32")),
            }
        }
        "time_invested" => {
            enc.ext.time_invested = match v {
                Value::Null => 0.0,
                Value::Number(n) => n.as_f64().ok_or_else(|| err(column, v, "not a number"))?,
                Value::String(s) => s.parse().map_err(|_| err(column, v, "not a number"))?,
                _ => return Err(err(column, v, "not a number")),
            }
        }
        "won_line" => enc.won_line = as_text(column, v)?.map(|s| s.as_bytes().to_vec()),
        "last_analysis" => {
            let n = normalise_json(v);
            enc.analysis = if n.is_null() { None } else { Some(serde_json::to_vec(&n).expect("JSON serialises")) };
        }
        "analysis_passes" => {
            let n = normalise_json(v);
            enc.passes = match &n {
                Value::Null => None,
                Value::Array(a) if a.is_empty() => None,
                _ => Some(serde_json::to_vec(&n).expect("JSON serialises")),
            };
        }
        _ => {}
    }
    Ok(())
}

fn opt_i64(x: Option<i64>) -> Value {
    x.map(Value::from).unwrap_or(Value::Null)
}

fn opt_move(m: Move) -> Value {
    if m.is_none() { Value::Null } else { Value::String(m.to_uci()) }
}

fn opt_str(s: &str) -> Value {
    if s.is_empty() { Value::Null } else { Value::String(s.to_string()) }
}

/// Every modeled column of a position, as v1 would store it.
pub fn decode(enc: &Encoded) -> Row {
    let c = &enc.core;
    let x = &enc.ext;
    let mut r = Row::new();
    r.insert("fen".into(), enc.fen.clone().map(Value::String).unwrap_or(Value::Null));
    r.insert("eval_cp".into(), opt_i64(c.eval().map(i64::from)));
    r.insert("status".into(), Value::String(c.status().v1().to_string()));
    r.insert("closure".into(), opt_str(c.closure().v1()));
    r.insert("proof".into(), opt_str(c.proof().v1()));
    r.insert("best_move".into(), opt_move(c.best_move));
    r.insert("backed_eval".into(), opt_i64(c.backed().map(i64::from)));
    r.insert("backed_move".into(), opt_move(c.backed_move));
    r.insert("backed_plies".into(), Value::from(c.backed_plies));
    r.insert("backed_nodes".into(), Value::from(x.backed_nodes));
    r.insert(
        "won_line".into(),
        enc.won_line.as_ref().map(|b| Value::String(String::from_utf8_lossy(b).into_owned())).unwrap_or(Value::Null),
    );
    r.insert("mate_in".into(), opt_i64(c.mate().map(i64::from)));
    r.insert("clock_slack".into(), opt_i64(c.slack().map(i64::from)));
    r.insert("last_analysis".into(), enc.analysis.as_ref().and_then(|b| serde_json::from_slice(b).ok()).unwrap_or(Value::Null));
    r.insert(
        "analysis_passes".into(),
        enc.passes.as_ref().and_then(|b| serde_json::from_slice(b).ok()).unwrap_or_else(|| Value::Array(vec![])),
    );
    r.insert("expanded".into(), Value::Bool(c.expanded()));
    r.insert("reachable".into(), Value::Bool(c.flag(F_REACHABLE)));
    r.insert("depth_invested".into(), Value::from(x.depth_invested));
    r.insert("nodes_invested".into(), Value::from(x.nodes_invested));
    r.insert("time_invested".into(), Number::from_f64(x.time_invested).map(Value::Number).unwrap_or(Value::Null));
    r.insert("visits".into(), Value::from(x.visits));
    r.insert("campaign_id".into(), if x.campaign == 0 { Value::Null } else { Value::from(x.campaign) });
    r
}

/// The columns of `after` that differ from `before` (both full decoded rows).
pub fn changed(before: &Row, after: &Row) -> Row {
    let mut out = Row::new();
    for (k, v) in after {
        if before.get(k) != Some(v) {
            out.insert(k.clone(), v.clone());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(v: Value) -> Row {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn full_row_roundtrip() {
        let r = row(json!({
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "eval_cp": -37, "status": "WHITE_WIN", "closure": "MATE_PV", "proof": "ENGINE",
            "best_move": "e7e8q", "backed_eval": 10000, "backed_move": "g7h7", "backed_plies": 9,
            "backed_nodes": 1152921504606846976u64, "won_line": "d6d7 f8e7", "mate_in": 9, "clock_slack": 60,
            "last_analysis": {"lines": [{"move": "e2e4", "eval_cp": 12}], "budget": 128000000},
            "analysis_passes": [{"b": 1, "w": 3}], "expanded": true, "reachable": true,
            "depth_invested": 52, "nodes_invested": 18896000000u64, "time_invested": 1234.5, "visits": 13, "campaign_id": 7
        }));
        let enc = encode(&r).unwrap();
        assert!(enc.core.black_to_move());
        let back = decode(&enc);
        for (k, v) in &r {
            assert_eq!(normalise(k, &back[k]), normalise(k, v), "{k}");
        }
        assert!(changed(&back, &decode(&encode(&back).unwrap())).is_empty());
    }

    #[test]
    fn defaults_and_text_json() {
        let enc = encode(&row(json!({"last_analysis": "{\"a\": 1}", "analysis_passes": "[]", "closure": null}))).unwrap();
        let d = decode(&enc);
        assert_eq!(d["last_analysis"], json!({"a": 1}));
        assert_eq!(d["analysis_passes"], json!([]));
        assert_eq!(d["status"], json!("UNKNOWN"));
        assert_eq!(d["eval_cp"], Value::Null);
        assert_eq!(d["campaign_id"], Value::Null);
        assert!(enc.needs_ext(), "a non-null last_analysis needs a blob");
        assert!(!encode(&Row::new()).unwrap().needs_ext());
    }

    #[test]
    fn refuses_what_does_not_fit() {
        for (c, v) in [
            ("eval_cp", json!(40000)),
            ("eval_cp", json!(-32768)),
            ("best_move", json!("")),
            ("best_move", json!("P@e4")),
            ("clock_slack", json!(300)),
            ("backed_nodes", json!(-1)),
            ("status", json!("LOST")),
        ] {
            let mut r = Row::new();
            r.insert(c.into(), v);
            let e = encode(&r).unwrap_err();
            assert_eq!(e[0].column, c);
        }
    }
}
