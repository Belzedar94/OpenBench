//! Number and label formatting, byte for byte what v1 prints.
//!
//! Every function names the v1 helper it ports.  The pages never format a
//! number any other way, so a value reads the same on v1 and v2 pages.

/// `3322 -> "3,322"` (Python `f'{n:,}'`).
pub fn thousands(n: u64) -> String {
    let s = n.to_string();
    let mut out = String::with_capacity(s.len() + s.len() / 3);
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// Signed thousands: `-1234 -> "-1,234"`.
pub fn thousands_i(n: i64) -> String {
    if n < 0 { format!("-{}", thousands(n.unsigned_abs())) } else { thousands(n as u64) }
}

/// v1 `views._human`: `3322 -> '3,322'`, `108600000 -> '108.6M'`,
/// `2400000000 -> '2.40B'`, trillions as `T`.
pub fn human(n: u64) -> String {
    let f = n as f64;
    if n >= 1_000_000_000_000 {
        format!("{:.2}T", f / 1e12)
    } else if n >= 1_000_000_000 {
        format!("{:.2}B", f / 1e9)
    } else if n >= 1_000_000 {
        format!("{:.1}M", f / 1e6)
    } else {
        thousands(n)
    }
}

/// v1 `views._budget_label` / `depth.short_nodes`: an exact multiple of a
/// billion or a million reads as `2B` / `128M`, anything else as [`human`].
pub fn budget_label(n: u64) -> String {
    if n > 0 && n.is_multiple_of(1_000_000_000) {
        format!("{}B", n / 1_000_000_000)
    } else if n > 0 && n.is_multiple_of(1_000_000) {
        format!("{}M", n / 1_000_000)
    } else {
        human(n)
    }
}

/// v1 `proof.PROOF_INFINITY`.
pub const PROOF_INFINITY: u64 = 1 << 62;

/// v1 `proof.format_number`: `None -> "-"`, infinity `"∞"`, else thousands.
pub fn proof_number(n: Option<u64>) -> String {
    match n {
        None => "-".into(),
        Some(v) if v >= PROOF_INFINITY => "\u{221e}".into(),
        Some(v) => thousands(v),
    }
}

/// v1 explorer `time_str`: `f'{time_invested:,.1f}s'`.
pub fn seconds(t: f64) -> String {
    let tenths = (t * 10.0).round_ties_even() as i64;
    let whole = tenths / 10;
    let frac = (tenths % 10).abs();
    format!("{}.{}s", thousands_i(whole), frac)
}

/// v1 header eval: `f'{eval_stm:+d}cp'`.
pub fn cp_signed(cp: i32) -> String {
    format!("{cp:+}cp")
}

/// Mate distance of a move-table row (v1 `_child_moves`, views.py:6339):
/// `plies` to the end from the parent, `n = (plies + 1) / 2`; `M{n}` when the
/// side to move wins, `-M{n}` when it loses.
pub fn mate_str(plies: u32, side_to_move_wins: bool) -> String {
    let n = plies.div_ceil(2);
    if side_to_move_wins { format!("M{n}") } else { format!("-M{n}") }
}

/// v1 verdict line: `M{(mate_in + 1) // 2}`.
pub fn verdict_mate(mate_in: u32) -> String {
    format!("M{}", mate_in.div_ceil(2))
}

/// v1 `pluralize:"y,ies"` on "pl": `ply` / `plies`.
pub fn plies_word(n: u32) -> &'static str {
    if n == 1 { "ply" } else { "plies" }
}

/// Pawns from White's side (v1 roots page `value_h`): `+1.84`, `−0.40`
/// (U+2212), `0.00`.
pub fn pawns_white(cp: i32) -> String {
    let sign = if cp > 0 {
        "+"
    } else if cp < 0 {
        "\u{2212}"
    } else {
        ""
    };
    format!("{sign}{:.2}", (cp.unsigned_abs() as f64) / 100.0)
}

/// v1 `templatetags/atomicdb_links.lichess_analysis`.
pub fn lichess_analysis(fen: &str) -> String {
    let mut out = String::from("https://lichess.org/analysis/atomic/");
    for c in fen.chars() {
        match c {
            ' ' => out.push('_'),
            'a'..='z' | 'A'..='Z' | '0'..='9' | '/' | '_' | '-' | '.' | '~' => out.push(c),
            _ => {
                let mut b = [0u8; 4];
                for byte in c.encode_utf8(&mut b).bytes() {
                    out.push_str(&format!("%{byte:02X}"));
                }
            }
        }
    }
    out
}

/// Percent-encode a query value (Django `urlencode` / JS `encodeURIComponent`
/// for the characters a route holds: UCI moves and commas).
pub fn query_value(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

// ---------------------------------------------------------------- W/D/L

const WDL_MIDPOINT_CP: f64 = 400.0;
const WDL_SCALE_CP: f64 = 150.0;

fn win_per_mille(cp: f64) -> f64 {
    let e = (WDL_MIDPOINT_CP - cp) / WDL_SCALE_CP;
    if e > 50.0 {
        0.0
    } else if e < -50.0 {
        1000.0
    } else {
        1000.0 / (1.0 + e.exp())
    }
}

/// v1 `wdl.estimate`: whole percent, win and loss rounded half to even
/// (Python `round`), draw takes the rest.
pub fn wdl_estimate(cp: i32) -> (u32, u32, u32) {
    let w = (win_per_mille(cp as f64) / 10.0).round_ties_even() as u32;
    let l = (win_per_mille(-(cp as f64)) / 10.0).round_ties_even() as u32;
    (w, 100u32.saturating_sub(w + l), l)
}

/// One W/D/L bar (v1 `wdl._bar`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Wdl {
    pub w: u32,
    pub d: u32,
    pub l: u32,
    pub proven: bool,
    pub title: String,
}

impl Wdl {
    pub fn new(w: u32, d: u32, l: u32, proven: bool, label: &str) -> Wdl {
        Wdl { w, d, l, proven, title: format!("{label}: win {w}% \u{b7} draw {d}% \u{b7} loss {l}%, for the side to move") }
    }
    pub fn estimate(cp: i32) -> Wdl {
        let (w, d, l) = wdl_estimate(cp);
        Wdl::new(w, d, l, false, "estimate from score")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn human_matches_v1() {
        assert_eq!(human(3322), "3,322");
        assert_eq!(human(108_600_000), "108.6M");
        assert_eq!(human(2_400_000_000), "2.40B");
        assert_eq!(human(12_776_005_537), "12.78B");
        assert_eq!(human(1_500_000_000_000), "1.50T");
        assert_eq!(human(0), "0");
    }

    #[test]
    fn budget_labels() {
        assert_eq!(budget_label(10_000_000_000), "10B");
        assert_eq!(budget_label(128_000_000), "128M");
        assert_eq!(budget_label(12_000_123_456), "12.00B");
    }

    #[test]
    fn seconds_like_python() {
        assert_eq!(seconds(3504.93), "3,504.9s");
        assert_eq!(seconds(0.04), "0.0s");
        assert_eq!(seconds(12.25), "12.2s"); // binary 12.25 is exact: half to even
    }

    #[test]
    fn wdl_matches_v1_docstring_and_page() {
        // wdl.py docstring: at 0 the bar reads 6 / 88 / 6, at +100 12 / 85 / 3,
        // at +400 50 / 50 / 0, at +1000 98 / 2 / 0.
        assert_eq!(wdl_estimate(0), (6, 88, 6));
        assert_eq!(wdl_estimate(100), (12, 85, 3));
        assert_eq!(wdl_estimate(400), (50, 50, 0));
        assert_eq!(wdl_estimate(1000), (98, 2, 0));
        // Production explorer, 28-Nov-2026: Nf3 1184 -> 99/1/0, Nh3 632 -> 82/18/0.
        assert_eq!(wdl_estimate(1184), (99, 1, 0));
        assert_eq!(wdl_estimate(632), (82, 18, 0));
        assert_eq!(Wdl::estimate(632).title, "estimate from score: win 82% \u{b7} draw 18% \u{b7} loss 0%, for the side to move");
    }

    #[test]
    fn mates_and_signs() {
        assert_eq!(mate_str(1, true), "M1");
        assert_eq!(mate_str(4, false), "-M2");
        assert_eq!(verdict_mate(5), "M3");
        assert_eq!(cp_signed(1184), "+1184cp");
        assert_eq!(cp_signed(0), "+0cp");
        assert_eq!(cp_signed(-40), "-40cp");
        assert_eq!(pawns_white(184), "+1.84");
        assert_eq!(pawns_white(-40), "\u{2212}0.40");
        assert_eq!(proof_number(Some(1_234)), "1,234");
        assert_eq!(proof_number(Some(PROOF_INFINITY)), "\u{221e}");
        assert_eq!(proof_number(None), "-");
    }

    #[test]
    fn lichess_link() {
        assert_eq!(
            lichess_analysis("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
            "https://lichess.org/analysis/atomic/rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR_w_KQkq_-_0_1"
        );
    }
}
