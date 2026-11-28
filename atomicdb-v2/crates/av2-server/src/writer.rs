//! The single writer: group commit over a channel of prepared inputs.
//!
//! One thread owns the `Store`.  It takes every input already waiting (up to
//! `max_batch`): those are the inputs that arrived while the previous commit
//! was in flight.  The rules run once over all of them in one overlay, the
//! store logs one frame (one fsync) and applies it between two epoch bumps
//! (that is what publishes the batch to the lock-free readers), and every
//! sender gets the batch's sequence number.
//!
//! True group commit: while batch n's fsync is in flight the writer gathers
//! the inputs that arrive; when the fsync completes it applies n, answers its
//! senders, computes batch n+1 from everything gathered and starts its fsync.
//! With a slow disk (16 ms per fsync on production) batches grow with the
//! load and latency stays near two fsyncs.  If the batch fills up before the
//! fsync completes, n+1 is computed on an overlay that reads through n
//! (`Store::commit_start`, `batch_after`, `commit_finish`); either way n is
//! applied before n+1 is logged and readers never see a batch before it is
//! durable.
//! If the rules reject a batch of several inputs, each is retried alone so
//! that one bad input cannot sink the others.
//!
//! Every stage is timed into the metrics (plan section 3.6).

use crate::metrics::{COUNT_BUCKETS, Counter, Gauge, Histogram, LATENCY_BUCKETS};
use av2_rules::Rules;
use av2_store::Store;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::mpsc::{Receiver, Sender, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// What a sender gets back once its input's batch is durable and visible.
#[derive(Debug, Clone)]
pub struct Ack<O> {
    pub seq: u64,
    pub output: O,
    /// How many inputs shared the batch.
    pub batch_inputs: usize,
}

pub type Reply<O> = Result<Ack<O>, String>;

pub(crate) struct Submission<I, O> {
    pub input: I,
    pub enqueued: Instant,
    pub reply: SyncSender<Reply<O>>,
}

/// What the writer's channel carries: inputs, the sync thread's "your frame
/// is durable" (so the writer wakes the moment an fsync completes instead of
/// polling, which on Windows would cost a 15.6 ms timer tick), and stop.
pub(crate) enum Msg<I, O> {
    Input(Submission<I, O>),
    Durable,
    Stop,
}

/// The stages of a batch, as labelled in the metrics.
pub const STAGES: [&str; 7] = ["queue_wait", "compute", "finalize", "write", "fsync", "apply", "batch"];

pub struct WriterMetrics {
    /// Per stage (`STAGES`): seconds.
    pub stage: Vec<Histogram>,
    /// From `submit` to the acknowledgement (durable and visible to readers).
    pub submit_to_visible: Histogram,
    pub batch_inputs: Histogram,
    pub frame_kib: Histogram,
    pub inputs: Counter,
    pub batches: Counter,
    pub rejected: Counter,
    pub retried_alone: Counter,
    pub queue_depth: AtomicI64,
    pub last_seq: Gauge,
    /// How long the writer waited for an fsync after computing the next
    /// batch (the part of the fsync the pipeline did not hide).
    pub fsync_wait: Histogram,
    /// The rules' post-commit work per batch (`Rules::after_commit`: the
    /// proof numbers).
    pub post_commit: Histogram,
}

impl Default for WriterMetrics {
    fn default() -> Self {
        WriterMetrics {
            stage: STAGES.iter().map(|_| Histogram::new(LATENCY_BUCKETS)).collect(),
            submit_to_visible: Histogram::new(LATENCY_BUCKETS),
            batch_inputs: Histogram::new(COUNT_BUCKETS),
            frame_kib: Histogram::new(COUNT_BUCKETS),
            inputs: Counter::new(),
            batches: Counter::new(),
            rejected: Counter::new(),
            retried_alone: Counter::new(),
            queue_depth: AtomicI64::new(0),
            last_seq: Gauge::new(),
            fsync_wait: Histogram::new(LATENCY_BUCKETS),
            post_commit: Histogram::new(LATENCY_BUCKETS),
        }
    }
}

/// Store numbers the writer refreshes for the metrics endpoint (the store
/// itself lives on the writer thread).
#[derive(Clone, Debug, Default)]
pub struct StoreGauges {
    pub stats: av2_store::Stats,
    pub live_bytes: Vec<(String, u64)>,
    pub log_bytes: u64,
    pub positions: u64,
}

pub(crate) fn refresh_gauges(store: &Store, g: &Mutex<StoreGauges>) {
    let (per_file, _) = store.live_bytes();
    let live_bytes = av2_store::layout::NAMES.iter().zip(per_file).map(|(n, b)| (n.to_string(), b)).collect();
    let positions = store.reader().read(|v| av2_model::GraphRead::len(v));
    *g.lock().unwrap() = StoreGauges { stats: store.stats(), live_bytes, log_bytes: store.log_bytes(), positions };
}

pub(crate) fn run<R: Rules>(
    mut store: Store,
    mut rules: R,
    rx: Receiver<Msg<R::Input, R::Output>>,
    wake: Sender<Msg<R::Input, R::Output>>,
    max_batch: usize,
    m: Arc<WriterMetrics>,
    gauges: Arc<Mutex<StoreGauges>>,
) -> Store
where
    R::Input: Send + 'static,
    R::Output: Clone + Send + 'static,
{
    refresh_gauges(&store, &gauges);
    let mut last_refresh = Instant::now();
    // The batch whose fsync is in flight, with its senders.  The next batch
    // is computed meanwhile, on an overlay that reads through it.
    let mut flight: Option<Flight<R::Output>> = None;
    let mut closed = false;
    loop {
        // True group commit: the next batch is whatever arrives while the
        // previous fsync is in flight.  With nothing in flight, wait for one
        // input; with a commit in flight, gather inputs until its fsync
        // completes (or the batch is full).
        let mut subs = Vec::new();
        let take = |msg: Msg<R::Input, R::Output>, subs: &mut Vec<Submission<R::Input, R::Output>>, closed: &mut bool| match msg {
            Msg::Input(s) => subs.push(s),
            Msg::Durable => {}
            Msg::Stop => *closed = true,
        };
        match flight.as_mut() {
            None => {
                if closed {
                    break;
                }
                match rx.recv() {
                    Ok(msg) => take(msg, &mut subs, &mut closed),
                    Err(_) => closed = true,
                }
            }
            Some(f) => {
                while !closed && subs.len() < max_batch.max(1) && !f.inflight.is_durable() {
                    match rx.recv() {
                        Ok(msg) => take(msg, &mut subs, &mut closed),
                        Err(_) => closed = true,
                    }
                }
            }
        }
        while subs.len() < max_batch.max(1) {
            match rx.try_recv() {
                Ok(msg) => take(msg, &mut subs, &mut closed),
                Err(_) => break,
            }
        }
        // The batch in flight is durable (or nothing arrived): apply it and
        // answer its senders before computing the next one.
        if let Some(f) = flight.as_mut()
            && (subs.is_empty() || f.inflight.is_durable())
        {
            finish(&mut store, flight.take().unwrap(), &m, &mut rules);
        }
        if subs.is_empty() {
            continue;
        }
        let t0 = Instant::now();
        m.queue_depth.fetch_sub(subs.len() as i64, Ordering::Relaxed);
        let mut inputs = Vec::with_capacity(subs.len());
        let mut meta = Vec::with_capacity(subs.len());
        for s in subs {
            m.stage[0].observe((t0 - s.enqueued).as_secs_f64());
            inputs.push(s.input);
            meta.push((s.enqueued, s.reply));
        }
        m.inputs.inc(inputs.len() as u64);
        // Compute on top of the batch in flight, if any.
        let mut b = match &flight {
            Some(f) => store.batch_after(&f.inflight),
            None => store.batch(),
        };
        let mut facts = Vec::new();
        let computed = rules.apply(&mut b, &inputs, &mut facts).map_err(|e| e.to_string()).and_then(|outs| {
            if outs.len() == inputs.len() {
                Ok(outs)
            } else {
                Err(format!("rules returned {} outputs for {} inputs", outs.len(), inputs.len()))
            }
        });
        m.stage[1].observe(t0.elapsed().as_secs_f64());
        if let Some(f) = flight.take() {
            finish(&mut store, f, &m, &mut rules);
        }
        match computed {
            Ok(outs) => match store.commit_start_notify(b, &av2_facts::encode_all(&facts), Some(notify(&wake))) {
                Ok(inflight) => flight = Some(Flight { inflight, meta, outs, started: t0 }),
                Err(e) => {
                    rules.abandoned();
                    for (_, reply) in meta {
                        m.rejected.inc(1);
                        let _ = reply.send(Err(e.to_string()));
                    }
                }
            },
            Err(e) if inputs.len() == 1 => {
                m.rejected.inc(1);
                let _ = meta[0].1.send(Err(e));
            }
            Err(_) => {
                // One bad input must not sink the others: each alone, one commit at a time.
                drop(b);
                for (i, (enq, reply)) in meta.into_iter().enumerate() {
                    m.retried_alone.inc(1);
                    let r = batch(&mut store, &mut rules, &inputs[i..i + 1], &m).map(|(seq, mut o)| Ack {
                        seq,
                        output: o.pop().unwrap(),
                        batch_inputs: 1,
                    });
                    if r.is_err() {
                        m.rejected.inc(1);
                    } else {
                        m.submit_to_visible.observe(enq.elapsed().as_secs_f64());
                    }
                    let _ = reply.send(r);
                }
            }
        }
        if last_refresh.elapsed() > Duration::from_millis(500) {
            refresh_gauges(&store, &gauges);
            last_refresh = Instant::now();
        }
    }
    if let Some(f) = flight.take() {
        finish(&mut store, f, &m, &mut rules);
    }
    refresh_gauges(&store, &gauges);
    store
}

fn notify<I: Send + 'static, O: Send + 'static>(wake: &Sender<Msg<I, O>>) -> av2_store::Notify {
    let w = wake.clone();
    Box::new(move || {
        let _ = w.send(Msg::Durable);
    })
}

struct Flight<O> {
    inflight: av2_store::InFlight,
    meta: Vec<(Instant, SyncSender<Reply<O>>)>,
    outs: Vec<O>,
    started: Instant,
}

/// Wait for the commit in flight to be durable, apply it, acknowledge, and
/// hand the committed graph to the rules' post-commit work.
fn finish<R: Rules>(store: &mut Store, f: Flight<R::Output>, m: &WriterMetrics, rules: &mut R) {
    let n = f.meta.len();
    match store.commit_finish(f.inflight) {
        Ok(seq) => {
            let ct = store.last_commit();
            m.stage[2].observe(ct.finalize.as_secs_f64());
            m.stage[3].observe((ct.write + ct.grow).as_secs_f64());
            m.stage[4].observe(ct.fsync.as_secs_f64());
            m.stage[5].observe(ct.apply.as_secs_f64());
            m.stage[6].observe(f.started.elapsed().as_secs_f64());
            m.fsync_wait.observe(ct.fsync_wait.as_secs_f64());
            m.frame_kib.observe(ct.frame_bytes as f64 / 1024.0);
            m.batch_inputs.observe(n as f64);
            m.batches.inc(1);
            m.last_seq.set(seq as f64);
            for ((enq, reply), output) in f.meta.into_iter().zip(f.outs) {
                m.submit_to_visible.observe(enq.elapsed().as_secs_f64());
                let _ = reply.send(Ok(Ack { seq, output, batch_inputs: n }));
            }
            let t = Instant::now();
            store.reader().read(|v| rules.after_commit(v, seq));
            m.post_commit.observe(t.elapsed().as_secs_f64());
        }
        Err(e) => {
            rules.abandoned();
            for (_, reply) in f.meta {
                m.rejected.inc(1);
                let _ = reply.send(Err(e.to_string()));
            }
        }
    }
}

fn batch<R: Rules>(store: &mut Store, rules: &mut R, inputs: &[R::Input], m: &WriterMetrics) -> Result<(u64, Vec<R::Output>), String> {
    let t0 = Instant::now();
    let mut b = store.batch();
    let mut facts = Vec::new();
    let outs = rules.apply(&mut b, inputs, &mut facts).map_err(|e| e.to_string())?;
    if outs.len() != inputs.len() {
        return Err(format!("rules returned {} outputs for {} inputs", outs.len(), inputs.len()));
    }
    let bytes = av2_facts::encode_all(&facts);
    m.stage[1].observe(t0.elapsed().as_secs_f64());
    let seq = match store.commit(b, &bytes) {
        Ok(seq) => seq,
        Err(e) => {
            rules.abandoned();
            return Err(e.to_string());
        }
    };
    store.reader().read(|v| rules.after_commit(v, seq));
    let ct = store.last_commit();
    m.stage[2].observe(ct.finalize.as_secs_f64());
    m.stage[3].observe((ct.write + ct.grow).as_secs_f64());
    m.stage[4].observe(ct.fsync.as_secs_f64());
    m.stage[5].observe(ct.apply.as_secs_f64());
    m.frame_kib.observe(ct.frame_bytes as f64 / 1024.0);
    m.batch_inputs.observe(inputs.len() as f64);
    m.batches.inc(1);
    m.last_seq.set(seq as f64);
    Ok((seq, outs))
}
