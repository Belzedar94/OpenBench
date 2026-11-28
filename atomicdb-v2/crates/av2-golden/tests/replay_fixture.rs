//! The replay on the two hand-written cases of `tests/fixtures/cases.jsonl`
//! (one climb, one landing, in the recorder's format).

use av2_golden::read;
use av2_golden::replay::{self, Mode};
use std::path::PathBuf;

fn fixture() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/cases.jsonl")
}

fn cases() -> Vec<serde_json::Value> {
    let mut c = read::open(&fixture()).unwrap();
    let v: Vec<_> = c.by_ref().map(Result::unwrap).collect();
    assert!(c.truncated.is_none());
    v
}

#[test]
fn echo_is_clean() {
    for c in cases() {
        let r = replay::replay(&c, "fixture", Mode::Echo);
        assert_eq!(r.status, "clean", "{}: {:?}", r.id, r.diffs);
        assert!(r.counts["match"] > 0);
    }
}

#[test]
fn noop_lists_everything_v1_wrote() {
    let c = cases();
    let drain = replay::replay(&c[0], "fixture", Mode::Noop);
    assert_eq!(drain.status, "diff");
    // status, closure, proof, mate_in, clock_slack; `best_move` was written
    // but never read (unread_input); `updated` is not modeled.
    assert_eq!(drain.counts["missing_write"], 5, "{:?}", drain.diffs);
    assert_eq!(drain.counts["unread_input"], 1);
    assert_eq!(drain.counts["not_modeled"], 1);
    assert_eq!(drain.edges_read, 2);
    assert_eq!(drain.parent_links, 1);
    let landing = replay::replay(&c[1], "fixture", Mode::Noop);
    assert_eq!(landing.counts["missing_row"], 1, "{:?}", landing.diffs);
    assert_eq!(landing.counts["missing_edge"], 1);
}

#[test]
fn v2_mode_says_what_is_not_ported() {
    for c in cases() {
        let r = replay::replay(&c, "fixture", Mode::V2);
        assert_eq!(r.status, "not_ported", "{}", r.id);
    }
}

#[test]
fn summary_counts() {
    let s = replay::run_files(&[fixture()], Mode::Echo, None, None, 3, |_| {});
    assert_eq!(s.cases, 2);
    assert_eq!(s.by_status["drain"]["clean"], 1);
    assert_eq!(s.by_status["stage_a"]["clean"], 1);
    assert!(s.text().contains("cases=2"));
}
