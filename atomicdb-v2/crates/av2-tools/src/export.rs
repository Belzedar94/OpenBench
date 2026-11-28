//! The v1 export (Postgres `COPY ... CSV HEADER`, see `sql/export.sql`) and
//! its mapping onto v2 records.
//!
//! Columns are found by name, so the same code reads the full export and
//! the benchmark's production slice (which has 24-character key prefixes,
//! no FEN and only the sizes of the blobs).

use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED, F_REACHABLE, NO_CP, NO_MATE, NO_SLACK};
use av2_model::{Closure, Core, Ext, Fingerprint, Move, Proof, Status};
use csv::StringRecord;
use std::collections::HashMap;

pub struct Cols {
    idx: HashMap<String, usize>,
}

impl Cols {
    pub fn new(h: &StringRecord) -> Cols {
        Cols { idx: h.iter().enumerate().map(|(i, n)| (n.trim().to_string(), i)).collect() }
    }
    pub fn has(&self, name: &str) -> bool {
        self.idx.contains_key(name)
    }
    #[inline]
    pub fn get<'a>(&self, r: &'a StringRecord, name: &str) -> Option<&'a str> {
        self.idx.get(name).and_then(|&i| r.get(i))
    }
    pub fn req<'a>(&self, r: &'a StringRecord, name: &str) -> Result<&'a str, String> {
        self.get(r, name).ok_or_else(|| format!("missing column {name}"))
    }
}

pub fn reader(path: &std::path::Path) -> Result<csv::Reader<std::io::BufReader<std::fs::File>>, String> {
    let f = std::fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(csv::ReaderBuilder::new().has_headers(true).buffer_capacity(1 << 20).from_reader(std::io::BufReader::with_capacity(4 << 20, f)))
}

pub fn parse_bool(s: &str) -> Result<bool, String> {
    match s {
        "t" | "true" | "True" | "1" => Ok(true),
        "f" | "false" | "False" | "0" | "" => Ok(false),
        _ => Err(format!("bad boolean {s:?}")),
    }
}

fn cp(s: Option<&str>, name: &str) -> Result<i16, String> {
    match s {
        None | Some("") => Ok(NO_CP),
        Some(v) => {
            let x: i64 = v.parse().map_err(|_| format!("{name}: {v:?}"))?;
            if x <= NO_CP as i64 || x > i16::MAX as i64 {
                return Err(format!("{name} {x} does not fit the 16-bit column"));
            }
            Ok(x as i16)
        }
    }
}

fn int<T: std::str::FromStr>(s: Option<&str>, name: &str, default: T) -> Result<T, String> {
    match s {
        None | Some("") => Ok(default),
        Some(v) => v.parse().map_err(|_| format!("{name}: {v:?}")),
    }
}

/// One position row of the export, mapped to v2.
pub struct PosRow {
    pub fp: Fingerprint,
    pub core: Core,
    pub ext: Ext,
    pub fen: Option<String>,
    pub analysis: Option<String>,
    pub passes: Option<String>,
    pub won_line: Option<String>,
}

impl PosRow {
    /// Does this position need an ext record?  (Blobs count: the loader
    /// fills their references.)
    pub fn needs_ext(&self) -> bool {
        !self.ext.is_default() || self.analysis.is_some() || self.passes.is_some() || self.won_line.is_some()
    }
}

fn opt_text(s: Option<&str>) -> Option<String> {
    s.filter(|v| !v.is_empty()).map(str::to_string)
}

pub fn parse_position(c: &Cols, r: &StringRecord) -> Result<PosRow, String> {
    let key = c.req(r, "key")?;
    let fp = Fingerprint::from_hex(key).map_err(|e| e.to_string())?;
    let mut core = Core::default();
    let status = Status::from_v1(c.get(r, "status").unwrap_or("")).ok_or_else(|| format!("status {:?}", c.get(r, "status")))?;
    let closure = Closure::from_v1(c.get(r, "closure").unwrap_or("")).ok_or_else(|| format!("closure {:?}", c.get(r, "closure")))?;
    let proof = Proof::from_v1(c.get(r, "proof").unwrap_or("")).ok_or_else(|| format!("proof {:?}", c.get(r, "proof")))?;
    core.set_status(status, closure, proof);
    core.eval_cp = cp(c.get(r, "eval_cp"), "eval_cp")?;
    core.backed_eval = cp(c.get(r, "backed_eval"), "backed_eval")?;
    core.backed_move = Move::parse_opt(c.get(r, "backed_move").unwrap_or("")).map_err(|e| format!("backed_move: {e}"))?;
    core.best_move = Move::parse_opt(c.get(r, "best_move").unwrap_or("")).map_err(|e| format!("best_move: {e}"))?;
    core.backed_plies = int(c.get(r, "backed_plies"), "backed_plies", 0u16)?;
    core.mate_in = match c.get(r, "mate_in") {
        None | Some("") => NO_MATE,
        Some(v) => {
            let x: u16 = v.parse().map_err(|_| format!("mate_in: {v:?}"))?;
            if x == NO_MATE {
                return Err("mate_in 65535 collides with the NULL sentinel".into());
            }
            x
        }
    };
    core.clock_slack = match c.get(r, "clock_slack") {
        None | Some("") => NO_SLACK,
        Some(v) => {
            let x: i32 = v.parse().map_err(|_| format!("clock_slack: {v:?}"))?;
            if !(0..NO_SLACK as i32).contains(&x) {
                return Err(format!("clock_slack {x} outside 0..254"));
            }
            x as u8
        }
    };
    core.set_flag(F_EXPANDED, parse_bool(c.get(r, "expanded").unwrap_or(""))?);
    core.set_flag(F_REACHABLE, parse_bool(c.get(r, "reachable").unwrap_or(""))?);
    let fen = opt_text(c.get(r, "fen"));
    let stm = match &fen {
        Some(f) => f.split(' ').nth(1).unwrap_or("w").to_string(),
        None => c.get(r, "stm").unwrap_or("w").to_string(),
    };
    core.set_flag(F_BLACK_TO_MOVE, stm == "b");
    let ext = Ext {
        nodes_invested: int(c.get(r, "nodes_invested"), "nodes_invested", 0u64)?,
        backed_nodes: int(c.get(r, "backed_nodes"), "backed_nodes", 0u64)?,
        time_invested: int(c.get(r, "time_invested"), "time_invested", 0f64)?,
        depth_invested: int(c.get(r, "depth_invested"), "depth_invested", 0i32)?,
        visits: int(c.get(r, "visits"), "visits", 0i32)?,
        campaign: int(c.get(r, "campaign_id"), "campaign_id", 0u32)?,
        // The queue's ladder of the rows it does not import (export.sql).
        ladder_bought: Ext::ladder_units(int(c.get(r, "ladder_bought"), "ladder_bought", 0u64)?),
        ladder_searched: Ext::ladder_units(int(c.get(r, "ladder_searched"), "ladder_searched", 0u64)?),
        ..Ext::default()
    };
    Ok(PosRow {
        fp,
        core,
        ext,
        fen,
        analysis: opt_text(c.get(r, "last_analysis")),
        passes: opt_text(c.get(r, "analysis_passes")).filter(|p| p != "[]"),
        won_line: opt_text(c.get(r, "won_line")),
    })
}

/// The v1 stored `priority` of a position row (0.0 when the column is
/// absent or NULL).  Not part of the graph (D5): the loader writes it to
/// the read-only `v1prio.col` the queue orders AUTO/FILL leases by.
pub fn v1_priority(c: &Cols, r: &StringRecord) -> Result<f64, String> {
    match c.get(r, "priority") {
        None | Some("") => Ok(0.0),
        Some(v) => v.parse().map_err(|_| format!("priority: {v:?}")),
    }
}

/// One edge row: `(edge id, parent fingerprint, move, child fingerprint)`.
pub fn parse_edge(c: &Cols, r: &StringRecord) -> Result<(u64, Fingerprint, Move, Fingerprint), String> {
    let id: u64 = c.req(r, "id")?.parse().map_err(|_| "edge id".to_string())?;
    let parent = c.get(r, "parent").or_else(|| c.get(r, "parent_id")).ok_or("missing column parent")?;
    let child = c.get(r, "child").or_else(|| c.get(r, "child_id")).ok_or("missing column child")?;
    let mv = Move::parse(c.req(r, "move_uci")?).map_err(|e| e.to_string())?;
    Ok((id, Fingerprint::from_hex(parent).map_err(|e| e.to_string())?, mv, Fingerprint::from_hex(child).map_err(|e| e.to_string())?))
}

/// Field-by-field comparison of what the store holds with what the export
/// row says.  Returns the names of the fields that differ.
pub fn diff(
    row: &PosRow,
    core: &Core,
    ext: Option<&Ext>,
    fen: Option<&str>,
    blob: impl Fn(av2_model::BlobRef) -> Option<Vec<u8>>,
) -> Vec<&'static str> {
    let mut d = Vec::new();
    let e = &row.core;
    if core.status() != e.status() {
        d.push("status");
    }
    if core.closure() != e.closure() {
        d.push("closure");
    }
    if core.proof() != e.proof() {
        d.push("proof");
    }
    if core.eval_cp != e.eval_cp {
        d.push("eval_cp");
    }
    if core.backed_eval != e.backed_eval {
        d.push("backed_eval");
    }
    if core.backed_move != e.backed_move {
        d.push("backed_move");
    }
    if core.best_move != e.best_move {
        d.push("best_move");
    }
    if core.backed_plies != e.backed_plies {
        d.push("backed_plies");
    }
    if core.mate_in != e.mate_in {
        d.push("mate_in");
    }
    if core.clock_slack != e.clock_slack {
        d.push("clock_slack");
    }
    if core.expanded() != e.expanded() {
        d.push("expanded");
    }
    if core.flag(F_REACHABLE) != e.flag(F_REACHABLE) {
        d.push("reachable");
    }
    if core.black_to_move() != e.black_to_move() {
        d.push("side_to_move");
    }
    let x = ext.copied().unwrap_or_default();
    if x.nodes_invested != row.ext.nodes_invested {
        d.push("nodes_invested");
    }
    if x.backed_nodes != row.ext.backed_nodes {
        d.push("backed_nodes");
    }
    if x.time_invested.to_bits() != row.ext.time_invested.to_bits() {
        d.push("time_invested");
    }
    if x.depth_invested != row.ext.depth_invested {
        d.push("depth_invested");
    }
    if x.visits != row.ext.visits {
        d.push("visits");
    }
    if x.campaign != row.ext.campaign {
        d.push("campaign_id");
    }
    let text = |r: av2_model::BlobRef| blob(r).map(|b| String::from_utf8_lossy(&b).to_string());
    if text(x.analysis) != row.analysis {
        d.push("last_analysis");
    }
    if text(x.passes) != row.passes {
        d.push("analysis_passes");
    }
    if text(x.won_line) != row.won_line {
        d.push("won_line");
    }
    if row.fen.is_some() && fen != row.fen.as_deref() {
        d.push("fen");
    }
    d
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_slice_row() {
        let h = StringRecord::from(vec![
            "key",
            "depth",
            "status",
            "closure",
            "proof",
            "eval_cp",
            "backed_eval",
            "backed_move",
            "backed_plies",
            "backed_nodes",
            "best_move",
            "mate_in",
            "clock_slack",
            "expanded",
            "reachable",
            "nodes_invested",
            "visits",
            "priority",
            "campaign_id",
            "fen_len",
            "stm",
        ]);
        let c = Cols::new(&h);
        let r = StringRecord::from(vec![
            "a81b39f292eca4c6959bf6d1",
            "5",
            "WHITE_WIN",
            "MATE_PV",
            "ENGINE",
            "-120",
            "",
            "e2e4",
            "3",
            "1152921504606846976",
            "",
            "9",
            "100",
            "t",
            "f",
            "128000000",
            "2",
            "-44.7",
            "7",
            "63",
            "b",
        ]);
        let p = parse_position(&c, &r).unwrap();
        assert_eq!(p.core.status(), Status::WhiteWin);
        assert_eq!(p.core.closure(), Closure::MatePv);
        assert_eq!(p.core.proof(), Proof::Engine);
        assert_eq!(p.core.eval_cp, -120);
        assert_eq!(p.core.backed_eval, NO_CP);
        assert_eq!(p.core.backed_move.to_uci(), "e2e4");
        assert_eq!(p.core.mate_in, 9);
        assert_eq!(p.core.clock_slack, 100);
        assert!(p.core.expanded() && !p.core.flag(F_REACHABLE) && p.core.black_to_move());
        assert_eq!(p.ext.backed_nodes, 1 << 60);
        assert_eq!(p.ext.campaign, 7);
        assert!(p.needs_ext());
        assert!(diff(&p, &p.core, Some(&p.ext), None, |_| None).is_empty());
    }

    #[test]
    fn refuses_values_that_do_not_fit() {
        let c = Cols::new(&StringRecord::from(vec!["key", "eval_cp"]));
        assert!(parse_position(&c, &StringRecord::from(vec!["a81b39f292eca4c6959bf6d1", "40000"])).is_err());
        assert!(parse_position(&c, &StringRecord::from(vec!["a81b39f292eca4c6959bf6d1", "-32768"])).is_err());
        assert!(parse_position(&c, &StringRecord::from(vec!["nothex", "1"])).is_err());
    }
}
