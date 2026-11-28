//! Group commit: the writer thread.
//!
//! Inputs are sent to one thread that owns the `Store`.  It takes every input
//! that is waiting (up to `max_batch`), runs the rules once over all of them
//! in one overlay, commits one frame (one fsync) and answers each sender with
//! the batch's sequence number.  While that fsync is in flight new inputs
//! queue up and become the next batch, so latency stays near one fsync at any
//! load (plan section 3.1).
//!
//! If the rules reject a batch of several inputs, each input is retried in a
//! batch of its own, so one bad input cannot sink the others.

use crate::Store;
use av2_rules::Rules;
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::thread::JoinHandle;
use std::time::Instant;

pub type Reply<O> = Result<(u64, O), String>;

struct Job<I, O> {
    input: I,
    reply: SyncSender<Reply<O>>,
}

/// Counters of the writer loop.
#[derive(Clone, Debug, Default)]
pub struct WriterStats {
    pub batches: u64,
    pub inputs: u64,
    pub retried_alone: u64,
    pub rejected: u64,
    pub max_batch_seen: u64,
    pub compute_micros: u64,
    pub commit_micros: u64,
}

pub struct Writer<I, O> {
    tx: Option<Sender<Job<I, O>>>,
    handle: Option<JoinHandle<(Store, WriterStats)>>,
}

impl<I: Send + 'static, O: Send + 'static> Writer<I, O> {
    pub fn spawn<R>(store: Store, mut rules: R, max_batch: usize) -> Writer<I, O>
    where
        R: Rules<Input = I, Output = O> + Send + 'static,
    {
        let (tx, rx) = mpsc::channel::<Job<I, O>>();
        let handle = std::thread::Builder::new()
            .name("av2-writer".into())
            .spawn(move || {
                let mut store = store;
                let mut st = WriterStats::default();
                while let Ok(first) = rx.recv() {
                    let mut inputs = vec![first.input];
                    let mut replies = vec![first.reply];
                    while inputs.len() < max_batch.max(1) {
                        match rx.try_recv() {
                            Ok(j) => {
                                inputs.push(j.input);
                                replies.push(j.reply);
                            }
                            Err(_) => break,
                        }
                    }
                    st.batches += 1;
                    st.inputs += inputs.len() as u64;
                    st.max_batch_seen = st.max_batch_seen.max(inputs.len() as u64);
                    match run(&mut store, &mut rules, &inputs, &mut st) {
                        Ok((seq, outs)) => {
                            for (r, o) in replies.into_iter().zip(outs) {
                                let _ = r.send(Ok((seq, o)));
                            }
                        }
                        Err(e) if inputs.len() == 1 => {
                            st.rejected += 1;
                            let _ = replies[0].send(Err(e));
                        }
                        Err(_) => {
                            for (i, r) in replies.into_iter().enumerate() {
                                st.retried_alone += 1;
                                let res =
                                    run(&mut store, &mut rules, &inputs[i..i + 1], &mut st).map(|(seq, mut o)| (seq, o.pop().unwrap()));
                                if res.is_err() {
                                    st.rejected += 1;
                                }
                                let _ = r.send(res);
                            }
                        }
                    }
                }
                (store, st)
            })
            .expect("spawn writer thread");
        Writer { tx: Some(tx), handle: Some(handle) }
    }

    /// Queue `input`; the receiver yields once its batch is durable.
    pub fn submit(&self, input: I) -> Receiver<Reply<O>> {
        let (reply, rx) = mpsc::sync_channel(1);
        match &self.tx {
            Some(tx) => {
                if let Err(mpsc::SendError(j)) = tx.send(Job { input, reply }) {
                    let _ = j.reply.send(Err("writer stopped".into()));
                }
            }
            None => {
                let _ = reply.send(Err("writer stopped".into()));
            }
        }
        rx
    }

    pub fn submit_wait(&self, input: I) -> Reply<O> {
        self.submit(input).recv().unwrap_or_else(|_| Err("writer stopped".into()))
    }

    /// Stop accepting inputs, finish the queue, and hand the store back.
    pub fn shutdown(mut self) -> (Store, WriterStats) {
        self.tx = None;
        self.handle.take().unwrap().join().expect("writer thread panicked")
    }
}

impl<I, O> Drop for Writer<I, O> {
    fn drop(&mut self) {
        self.tx = None;
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

fn run<R: Rules>(store: &mut Store, rules: &mut R, inputs: &[R::Input], st: &mut WriterStats) -> Result<(u64, Vec<R::Output>), String> {
    let t0 = Instant::now();
    let mut batch = store.batch();
    let mut facts = Vec::new();
    let outs = rules.apply(&mut batch, inputs, &mut facts).map_err(|e| e.to_string())?;
    if outs.len() != inputs.len() {
        return Err(format!("rules returned {} outputs for {} inputs", outs.len(), inputs.len()));
    }
    let bytes = av2_facts::encode_all(&facts);
    let t1 = Instant::now();
    let seq = store.commit(batch, &bytes).map_err(|e| e.to_string())?;
    st.compute_micros += (t1 - t0).as_micros() as u64;
    st.commit_micros += t1.elapsed().as_micros() as u64;
    Ok((seq, outs))
}
