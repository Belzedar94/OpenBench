//! av2-load + av2-check + av2-replay on a tiny export in the full format.

use std::process::Command;

const START: &str = "b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879";

fn key(i: u32) -> String {
    let h = (i as u128 + 1).wrapping_mul(0x9E37_79B9_7F4A_7C15_F39C_C060_5CED_C835u128);
    format!("{h:032x}{:032x}", h.rotate_left(64))
}

#[test]
fn load_then_check_a_small_export() {
    let d = tempfile::tempdir().unwrap();
    let pos = d.path().join("position.csv");
    let edges = d.path().join("edge.csv");
    let mut p = String::from(
        "key,fen,status,closure,proof,eval_cp,backed_eval,backed_move,backed_plies,backed_nodes,best_move,mate_in,clock_slack,expanded,reachable,depth_invested,nodes_invested,time_invested,visits,campaign_id,last_analysis,analysis_passes,won_line\n",
    );
    p.push_str(&format!(
        "{START},rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1,UNKNOWN,,,12,15,e2e4,3,128000000,e2e4,,,t,t,30,256000000,12.5,2,,\"{{\"\"lines\"\": [1, 2]}}\",\"[{{\"\"b\"\":1}}]\",\n"
    ));
    for i in 0..3u32 {
        // Children: one won with a witness line, one with an odd FEN (kept as a blob), one plain.
        let (status, closure, proof, mate, slack, won, fen) = match i {
            0 => ("WHITE_WIN", "MATE_PV", "ANDOR", "7", "100", "e7e5 d1h5", "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"),
            1 => ("UNKNOWN", "", "", "", "", "", "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq d3 7 9"),
            _ => ("UNKNOWN", "", "", "", "", "", ""),
        };
        p.push_str(&format!("{},{fen},{status},{closure},{proof},-30,,,0,0,,{mate},{slack},f,t,0,0,0,0,7,,[],{won}\n", key(i)));
    }
    std::fs::write(&pos, p).unwrap();
    let mut e = String::from("id,parent,move_uci,child\n");
    e.push_str(&format!("10,{START},e2e4,{}\n", key(0)));
    e.push_str(&format!("11,{START},d2d4,{}\n", key(1)));
    e.push_str(&format!("12,{START},g1f3,{}\n", key(2)));
    e.push_str(&format!("13,{},e7e5,{}\n", key(0), key(2))); // a transposition: key(2) gets a second parent
    std::fs::write(&edges, e).unwrap();
    let db = d.path().join("db");
    let st = Command::new(env!("CARGO_BIN_EXE_av2-load"))
        .args(["--pos", pos.to_str().unwrap(), "--edges", edges.to_str().unwrap(), "--out", db.to_str().unwrap(), "--shard-bits", "2"])
        .output()
        .unwrap();
    assert!(st.status.success(), "{}", String::from_utf8_lossy(&st.stderr));
    let out = String::from_utf8_lossy(&st.stdout);
    assert!(out.contains("\"positions\":4") && out.contains("\"edges\":4") && out.contains("\"blob_fens\":1"), "{out}");
    let st = Command::new(env!("CARGO_BIN_EXE_av2-check"))
        .args(["--db", db.to_str().unwrap(), "--pos", pos.to_str().unwrap(), "--edges", edges.to_str().unwrap(), "--structure", "--scrub"])
        .output()
        .unwrap();
    let out = String::from_utf8_lossy(&st.stdout);
    assert!(st.status.success(), "{out}\n{}", String::from_utf8_lossy(&st.stderr));
    assert!(out.contains("\"differ\":0") && out.contains("\"bad\":0") && out.contains("\"problems\":[]"), "{out}");
    // The genesis fact is in the log.
    let st = Command::new(env!("CARGO_BIN_EXE_av2-replay")).args(["dump", "--store", db.to_str().unwrap()]).output().unwrap();
    let out = String::from_utf8_lossy(&st.stdout);
    assert!(out.contains("\"kind\":\"genesis\"") && out.contains("pg-export"), "{out}");
    // A changed value is caught.
    let changed = std::fs::read_to_string(&pos).unwrap().replace(",-30,", ",-31,");
    std::fs::write(&pos, changed).unwrap();
    let st = Command::new(env!("CARGO_BIN_EXE_av2-check"))
        .args(["--db", db.to_str().unwrap(), "--pos", pos.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(!st.status.success());
    assert!(String::from_utf8_lossy(&st.stdout).contains("\"eval_cp\":3"));
}
