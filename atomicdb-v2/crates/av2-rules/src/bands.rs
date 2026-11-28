//! Score bands (proofs file SB-1 to SB-7; v1 `atomicdb/score_bands.py`).
//!
//! Stored values are White's point of view, symmetric in sign.  By absolute
//! value: `0..8999` plain centipawns, `9000..9249` a large ordinary eval
//! compressed, `9250..9500` a tablebase win `d` plies down (`9500 - d`),
//! `9501..9999` an engine mate (`10000 - moves`), `10000` proven (status).

use serde_json::{Map, Value};

pub const ENGINE_TB_CP: i64 = 20_000;
pub const ENGINE_MAX_PLY: i64 = 246;
pub const ENGINE_CP_MAX: i64 = 15_200;
pub const PLAIN_MAX: i32 = 8_999;
pub const BIG_LOW: i32 = 9_000;
pub const BIG_HIGH: i32 = 9_249;
pub const TB_TOP: i32 = 9_500;
pub const TB_DISTANCE_MAX: i32 = TB_TOP - BIG_HIGH - 1; // 250
pub const TB_LOW: i32 = TB_TOP - TB_DISTANCE_MAX; // 9250
pub const MATE_BASE: i32 = 10_000;
pub const ENGINE_TB_MIN_CP: i64 = ENGINE_TB_CP - TB_DISTANCE_MAX as i64; // 19750
pub const LEGACY_CLAMP: i64 = TB_TOP as i64;

#[inline]
fn sign(v: i64) -> i64 {
    if v < 0 { -1 } else { 1 }
}

/// SB-2 `tb_distance`: plies to the tables of a raw engine cp score.
pub fn tb_distance(cp: i64) -> Option<i64> {
    let m = cp.abs();
    if m < ENGINE_TB_MIN_CP {
        return None;
    }
    Some((ENGINE_TB_CP - m).clamp(0, TB_DISTANCE_MAX as i64))
}

/// SB-3 `engine_cp_to_stored` (same point of view in and out).
pub fn engine_cp_to_stored(cp: i64) -> i64 {
    let (m, s) = (cp.abs(), sign(cp));
    if m <= PLAIN_MAX as i64 {
        return cp;
    }
    if let Some(d) = tb_distance(cp) {
        return s * (TB_TOP as i64 - d);
    }
    if m >= ENGINE_CP_MAX {
        return s * BIG_HIGH as i64;
    }
    // Python floor division of non-negative operands.
    s * (BIG_LOW as i64 + (m - BIG_LOW as i64) * (BIG_HIGH - BIG_LOW) as i64 / (ENGINE_CP_MAX - BIG_LOW as i64))
}

/// SB-4 `legacy_value`: what workers send as `eval_cp` for a `score cp`.
pub fn legacy_value(cp: i64) -> i64 {
    cp.clamp(-LEGACY_CLAMP, LEGACY_CLAMP)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ScoreKind {
    Cp,
    Mate,
}

fn is_word(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// SB-4 `raw_score`: the first match of `\bscore (cp|mate) (-?\d+)\b` in a
/// raw UCI `info` line.
pub fn raw_score(raw: &str) -> Option<(ScoreKind, i64)> {
    let b = raw.as_bytes();
    let mut from = 0;
    while let Some(off) = raw[from..].find("score ") {
        let at = from + off;
        from = at + 1;
        if at > 0 && is_word(b[at - 1]) {
            continue;
        }
        let rest = at + 6;
        let (kind, after) = if raw[rest..].starts_with("cp ") {
            (ScoreKind::Cp, rest + 3)
        } else if raw[rest..].starts_with("mate ") {
            (ScoreKind::Mate, rest + 5)
        } else {
            continue;
        };
        let mut end = after;
        if end < b.len() && b[end] == b'-' {
            end += 1;
        }
        let digits = end;
        while end < b.len() && b[end].is_ascii_digit() {
            end += 1;
        }
        if end == digits {
            continue;
        }
        // `\b` after the digits: the next byte is not a word byte.
        if end < b.len() && is_word(b[end]) {
            // The regex would backtrack to a shorter digit run, which is then
            // followed by a digit (a word byte): no match at this position.
            continue;
        }
        if let Ok(v) = raw[after..end].parse::<i64>() {
            return Some((kind, v));
        }
        // Too many digits for an i64: Python would still match; such a line
        // is not something a worker sends.
        continue;
    }
    None
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Band {
    Plain,
    Big,
    Tb,
    Mate,
    Proven,
}

/// SB-7 `band` (reporting only).
pub fn band(value: Option<i64>) -> Option<Band> {
    let m = value?.abs();
    Some(if m <= PLAIN_MAX as i64 {
        Band::Plain
    } else if m <= BIG_HIGH as i64 {
        Band::Big
    } else if m <= TB_TOP as i64 {
        Band::Tb
    } else if m < MATE_BASE as i64 {
        Band::Mate
    } else {
        Band::Proven
    })
}

/// SB-5 `stored_from_raw`: `(new, legacy)` White-POV values of a line's raw
/// cp score, or None (a mate line, no raw line, no `score cp`).
pub fn stored_from_raw(line: &Map<String, Value>, stm_white: bool) -> Option<(i64, i64)> {
    if !matches!(line.get("mate"), None | Some(Value::Null)) {
        return None;
    }
    let raw = line.get("raw")?.as_str()?;
    if raw.is_empty() {
        return None;
    }
    let (kind, n) = raw_score(raw)?;
    if kind != ScoreKind::Cp {
        return None;
    }
    let s = if stm_white { 1 } else { -1 };
    Some((s * engine_cp_to_stored(n), s * legacy_value(n)))
}

/// Python equality of a JSON value with an int (`1 == 1.0`, `True == 1`).
fn json_eq_int(v: &Value, n: i64) -> bool {
    match v {
        Value::Number(x) => x.as_i64().map(|i| i == n).unwrap_or_else(|| x.as_f64() == Some(n as f64)),
        Value::Bool(b) => (*b as i64) == n,
        _ => false,
    }
}

/// SB-6 `normalize_line`: the line with its `eval_cp` on the stored scale.
/// Returns `None` when the line is left as it came (v1 returns the same
/// object), else the rewritten copy.
pub fn normalize_line(line: &Map<String, Value>, stm_white: bool) -> Option<Map<String, Value>> {
    let (new, legacy) = stored_from_raw(line, stm_white)?;
    let current = line.get("eval_cp");
    let is_none = matches!(current, None | Some(Value::Null));
    if let Some(c) = current {
        if json_eq_int(c, new) {
            return None;
        }
    }
    if !is_none && !json_eq_int(current.unwrap(), legacy) {
        return None;
    }
    let mut out = line.clone();
    out.insert("eval_cp".into(), Value::from(new));
    Some(out)
}

/// SB-6 `normalize_lines` over a JSON value: a non-list is returned as is,
/// non-objects inside pass through.
pub fn normalize_lines(lines: &Value, stm_white: bool) -> Value {
    match lines {
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|l| match l {
                    Value::Object(m) => normalize_line(m, stm_white).map(Value::Object).unwrap_or_else(|| l.clone()),
                    other => other.clone(),
                })
                .collect(),
        ),
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    //! `test_tb_distance.py::MapTests`, `::NormalizeTests`.
    use super::*;
    use serde_json::json;

    fn raw(cp: i64, mv: &str) -> String {
        format!("info depth 30 seldepth 41 multipv 1 score cp {cp} nodes 128000000 nps 6000000 hashfull 400 tbhits 12 time 21000 pv {mv}")
    }

    fn line(mv: &str, cp: i64, eval_cp: Option<i64>) -> Map<String, Value> {
        let v = json!({"move": mv, "mate": null, "pv": [mv],
                       "eval_cp": eval_cp.unwrap_or(legacy_value(cp)), "raw": raw(cp, mv)});
        v.as_object().unwrap().clone()
    }

    #[test]
    fn test_the_examples() {
        let f = engine_cp_to_stored;
        assert_eq!(f(19_981), 9_481);
        assert_eq!(f(20_000), 9_500);
        assert_eq!(f(-19_981), -9_481);
        assert_eq!(f(20_000 - 246), 9_254);
        assert_eq!(f(8_999), 8_999);
        assert_eq!(f(9_000), 9_000);
        assert_eq!(f(15_146), 9_246);
        assert_eq!(f(15_200), 9_249);
        assert_eq!(f(0), 0);
        assert_eq!(f(-35), -35);
    }

    #[test]
    fn test_monotone_and_symmetric() {
        let mut previous = None;
        for cp in -20_100..=20_100 {
            let v = engine_cp_to_stored(cp);
            assert_eq!(engine_cp_to_stored(-cp), -v);
            if let Some(p) = previous {
                assert!(v >= p, "{cp}");
            }
            previous = Some(v);
        }
    }

    #[test]
    fn test_bands_are_strictly_ordered() {
        let f = engine_cp_to_stored;
        let plain: Vec<i64> = (0..9_000).map(f).collect();
        let big: Vec<i64> = (9_000..19_750).map(f).collect();
        let tb: Vec<i64> = (19_750..20_001).map(f).collect();
        let mate: Vec<i64> = (1..124).map(|m| 10_000 - m).collect();
        let max = |v: &Vec<i64>| *v.iter().max().unwrap();
        let min = |v: &Vec<i64>| *v.iter().min().unwrap();
        assert!(max(&plain) < min(&big));
        assert!(max(&big) < min(&tb));
        assert!(max(&tb) < min(&mate));
        assert!(max(&mate) < 10_000);
        assert_eq!(max(&plain), PLAIN_MAX as i64);
        assert_eq!((min(&big), max(&big)), (BIG_LOW as i64, BIG_HIGH as i64));
        assert_eq!((min(&tb), max(&tb)), (TB_LOW as i64, TB_TOP as i64));
        let mut sorted = tb.clone();
        sorted.sort();
        assert_eq!(sorted, tb);
        sorted.dedup();
        assert_eq!(sorted.len(), tb.len());
    }

    #[test]
    fn test_the_thresholds_the_code_reads_did_not_move() {
        assert_eq!(crate::conv::MATE_BAND, 9_000);
        assert_eq!(crate::conv::TB_CLAMP_CEILING, 9_500);
        for cp in [9_000, 9_499, 9_500, 12_000, 15_146, 19_754, 19_981, 20_000] {
            let v = engine_cp_to_stored(cp);
            assert!(v >= 9_000 && v <= 9_500);
            assert_eq!(v >= 9_000, legacy_value(cp) >= 9_000);
        }
        for cp in [0, 500, 8_999] {
            assert!(engine_cp_to_stored(cp) < 9_000);
        }
    }

    #[test]
    fn test_band_names() {
        let got: Vec<_> = [None, Some(120), Some(-9_100), Some(9_481), Some(-9_997), Some(10_000)].into_iter().map(band).collect();
        assert_eq!(got, vec![None, Some(Band::Plain), Some(Band::Big), Some(Band::Tb), Some(Band::Mate), Some(Band::Proven)]);
    }

    #[test]
    fn test_white_and_black_to_move() {
        let l = line("e2e4", 19_981, None);
        assert_eq!(normalize_line(&l, true).unwrap()["eval_cp"], json!(9_481));
        let mut black = l.clone();
        black.insert("eval_cp".into(), json!(-9_500));
        assert_eq!(normalize_line(&black, false).unwrap()["eval_cp"], json!(-9_481));
    }

    #[test]
    fn test_idempotent_and_copying() {
        let l = line("e2e4", 12_000, None);
        let once = normalize_line(&l, true).unwrap();
        assert_eq!(l["eval_cp"], json!(9_500));
        assert_eq!(once["eval_cp"], json!(9_120));
        assert!(normalize_line(&once, true).is_none());
    }

    #[test]
    fn test_lines_it_leaves_alone() {
        let no_raw = json!({"move": "e2e4", "eval_cp": 9_500, "mate": null});
        let mate = json!({"move": "e2e4", "eval_cp": 9_997, "mate": 3, "raw": "info depth 9 multipv 1 score mate 3 pv e2e4"});
        let foreign = Value::Object(line("e2e4", 19_981, Some(4_000)));
        for l in [no_raw, mate, foreign] {
            assert!(normalize_line(l.as_object().unwrap(), true).is_none());
        }
        let junk = json!(["junk"]);
        assert_eq!(normalize_lines(&junk, true), junk);
        assert_eq!(normalize_lines(&json!("junk"), true), json!("junk"));
    }

    #[test]
    fn test_bounds_and_wdl_in_the_raw_line() {
        let mut l = line("e2e4", 0, None);
        l.insert("raw".into(), json!("info depth 30 seldepth 41 multipv 1 score cp 19970 lowerbound wdl 1000 0 0 nodes 9 pv e2e4"));
        l.insert("eval_cp".into(), json!(9_500));
        assert_eq!(normalize_line(&l, true).unwrap()["eval_cp"], json!(9_470));
    }

    #[test]
    fn raw_score_word_boundaries() {
        assert_eq!(raw_score("xscore cp 5"), None);
        assert_eq!(raw_score("score cp 5x"), None);
        assert_eq!(raw_score("info score cp -35 pv"), Some((ScoreKind::Cp, -35)));
        assert_eq!(raw_score("score mate 3"), Some((ScoreKind::Mate, 3)));
        assert_eq!(raw_score("xscore cp 1 score cp 7"), Some((ScoreKind::Cp, 7)));
        assert_eq!(raw_score(""), None);
    }
}
