//! The backed ascent (av2-rules) buys through the queue's quality arm.
//!
//! The shape of `test_backed_eval.py::BackedGuardTests::
//! test_shallow_child_displaces_at_once_and_buys_convergence`: a parent with
//! 10B nodes of its own and partial coverage, one child searched at 128M
//! claiming a better value: the value displaces at once, and the missing depth
//! is bought on the child (BKD-5.3, BKD-11.1).

use av2_model::record::F_EXPANDED;
use av2_queue::config::QueueConfig;
use av2_queue::consts::ARM_QUALITY;
use av2_queue::{QueueState, Source, TState};
use av2_rules::ascent::{AscentEnv, AscentOpts, backup_backed_evals};
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Event, Recorder};
use av2_rules::loops::V1Loops;
use av2_rules::testkit::Fixture;

#[test]
fn the_ascent_buys_the_missing_depth_through_the_queue() {
    let mut f = Fixture::new();
    let p = f.named("QP", false);
    f.set(p, |c| {
        c.set_flag(F_EXPANDED, true);
        c.eval_cp = 100;
    });
    f.set_ext(p, |e| e.nodes_invested = 10_000_000_000);
    let shallow = f.named("QSHALLOW", true);
    f.set(shallow, |c| c.eval_cp = 900);
    f.set_ext(shallow, |e| {
        e.nodes_invested = 128_000_000;
        e.visits = 3;
    });
    let blind = f.named("QBLIND", true);
    f.edge(p, "a1a2", shallow);
    f.edge(p, "a1a3", blind);

    let mut q = QueueState::new(QueueConfig::default());
    let mut rec = Recorder::default();
    let cfg = RulesConfig::default();
    let mut loops = V1Loops::default();
    let (report, deltas) = {
        let mut buyer = q.quality_buyer(5_000);
        let r = {
            let mut env = AscentEnv { cfg: &cfg, effects: &mut rec, tasks: &mut buyer, loops: &mut loops };
            backup_backed_evals(&mut f.g, &[p], AscentOpts::default(), &mut env).unwrap()
        };
        let (deltas, _events) = buyer.finish();
        (r, deltas)
    };
    assert_eq!(f.stored(p).value, Some(900));
    assert_eq!(report.bought, 1);
    assert!(rec.events.iter().any(|e| matches!(e, Event::QualityConvergence { queued: 1, .. })));
    assert!(!deltas.is_empty(), "the purchase travels as queue deltas");
    // v1: generation = the child's visits; budget = the rung matching the
    // parent's 10B (2B), held at 128M only for an interior child.
    let t = q.slot(shallow.0, 3).expect("a task on the child's slot");
    assert_eq!((t.state, t.source, t.arm.as_str(), t.budget), (TState::Pending, Source::Fill, ARM_QUALITY, 2_000_000_000));
}
