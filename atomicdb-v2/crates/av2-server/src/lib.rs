//! AtomicDB v2 service skeleton (plan section 3.1): the single writer with
//! group commit, fed by a channel of prepared inputs, calling the
//! `av2_rules::Rules` trait, publishing each batch to lock-free readers
//! through the store's epoch, and a Prometheus endpoint with the per-stage
//! timings of plan section 3.6.
//!
//! What is not here yet: the preparers (stage (a), `av2-chess`/`av2-proof`),
//! the queue and lease protocol (`av2-queue`), the HTTP API and pages
//! (`av2-web`), the projector.  They plug in as producers of inputs and as
//! readers.

pub mod http;
pub mod metrics;
pub mod stub;
pub mod writer;

use av2_rules::Rules;
use av2_store::{Reader, Store};
use metrics::Family;
use std::net::SocketAddr;
use std::sync::atomic::Ordering;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Instant;
use writer::{Reply, StoreGauges, Submission, WriterMetrics};

pub use writer::Ack;

#[derive(Clone, Debug)]
pub struct ServiceConfig {
    /// Most inputs in one batch.
    pub max_batch: usize,
    /// Where to serve `/metrics` (e.g. `127.0.0.1:9187`), if anywhere.
    pub metrics_addr: Option<String>,
}

impl Default for ServiceConfig {
    fn default() -> Self {
        ServiceConfig { max_batch: 256, metrics_addr: None }
    }
}

pub struct Service<I, O> {
    tx: Option<Sender<Submission<I, O>>>,
    handle: Option<JoinHandle<Store>>,
    reader: Reader,
    metrics: Arc<WriterMetrics>,
    gauges: Arc<Mutex<StoreGauges>>,
    started: Instant,
    metrics_addr: Mutex<Option<SocketAddr>>,
}

impl<I: Send + 'static, O: Send + Clone + 'static> Service<I, O> {
    /// Start the writer thread on `store` with `rules`.
    pub fn start<R>(store: Store, rules: R, cfg: ServiceConfig) -> std::io::Result<Arc<Service<I, O>>>
    where
        R: Rules<Input = I, Output = O> + Send + 'static,
    {
        let reader = store.reader();
        let metrics = Arc::new(WriterMetrics::default());
        let gauges = Arc::new(Mutex::new(StoreGauges::default()));
        let (tx, rx) = mpsc::channel::<Submission<I, O>>();
        let (m2, g2) = (metrics.clone(), gauges.clone());
        let max_batch = cfg.max_batch;
        let handle =
            std::thread::Builder::new().name("av2-writer".into()).spawn(move || writer::run(store, rules, rx, max_batch, m2, g2))?;
        let svc = Arc::new(Service {
            tx: Some(tx),
            handle: Some(handle),
            reader,
            metrics,
            gauges,
            started: Instant::now(),
            metrics_addr: Mutex::new(None),
        });
        if let Some(addr) = &cfg.metrics_addr {
            let weak = Arc::downgrade(&svc);
            let (local, _h) = http::serve(
                addr,
                Arc::new(move |path: &str| match (path, weak.upgrade()) {
                    ("/metrics", Some(s)) => (200, "text/plain; version=0.0.4", s.metrics_text()),
                    ("/healthz", Some(_)) => (200, "text/plain", "ok\n".into()),
                    (_, None) => (503, "text/plain", "stopping\n".into()),
                    _ => (404, "text/plain", "not found\n".into()),
                }),
            )?;
            *svc.metrics_addr.lock().unwrap() = Some(local);
        }
        Ok(svc)
    }

    /// Queue an input; the receiver yields once its batch is durable and visible.
    pub fn submit(&self, input: I) -> Receiver<Reply<O>> {
        let (reply, rx) = mpsc::sync_channel(1);
        self.metrics.queue_depth.fetch_add(1, Ordering::Relaxed);
        match &self.tx {
            Some(tx) => {
                if let Err(mpsc::SendError(s)) = tx.send(Submission { input, enqueued: Instant::now(), reply }) {
                    self.metrics.queue_depth.fetch_sub(1, Ordering::Relaxed);
                    let _ = s.reply.send(Err("writer stopped".into()));
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

    /// Where `/metrics` is served, if it is.
    pub fn metrics_addr(&self) -> Option<SocketAddr> {
        *self.metrics_addr.lock().unwrap()
    }

    /// A lock-free reader of the committed graph.
    pub fn reader(&self) -> Reader {
        self.reader.clone()
    }

    pub fn writer_metrics(&self) -> &WriterMetrics {
        &self.metrics
    }

    /// The Prometheus text of every metric.
    pub fn metrics_text(&self) -> String {
        let m = &self.metrics;
        let g = self.gauges.lock().unwrap().clone();
        let st = &g.stats;
        let stages: Vec<(String, &metrics::Histogram)> =
            writer::STAGES.iter().zip(&m.stage).map(|(s, h)| (format!("stage=\"{s}\""), h)).collect();
        let one = |v: f64| vec![(String::new(), v)];
        metrics::render(&[
            Family::Histogram {
                name: "av2_writer_stage_seconds",
                help: "Time per batch stage (queue_wait is per input).",
                samples: stages,
            },
            Family::Histogram {
                name: "av2_submit_to_visible_seconds",
                help: "From submit to the acknowledgement: durable and visible to readers.",
                samples: vec![(String::new(), &m.submit_to_visible)],
            },
            Family::Histogram {
                name: "av2_batch_inputs",
                help: "Inputs per committed batch.",
                samples: vec![(String::new(), &m.batch_inputs)],
            },
            Family::Histogram { name: "av2_frame_kib", help: "Log frame size per batch.", samples: vec![(String::new(), &m.frame_kib)] },
            Family::Counter { name: "av2_inputs_total", help: "Inputs taken by the writer.", samples: one(m.inputs.get() as f64) },
            Family::Counter { name: "av2_batches_total", help: "Batches committed.", samples: one(m.batches.get() as f64) },
            Family::Counter { name: "av2_rejected_total", help: "Inputs the rules rejected.", samples: one(m.rejected.get() as f64) },
            Family::Counter {
                name: "av2_retried_alone_total",
                help: "Inputs retried in a batch of their own after a rejected batch.",
                samples: one(m.retried_alone.get() as f64),
            },
            Family::Gauge {
                name: "av2_queue_depth",
                help: "Inputs submitted and not yet taken by the writer.",
                samples: one(m.queue_depth.load(Ordering::Relaxed) as f64),
            },
            Family::Gauge { name: "av2_last_seq", help: "Sequence number of the last committed batch.", samples: one(m.last_seq.get()) },
            Family::Gauge { name: "av2_positions", help: "Positions in the graph.", samples: one(g.positions as f64) },
            Family::Gauge {
                name: "av2_graph_live_bytes",
                help: "Bytes in use per graph file.",
                samples: g.live_bytes.iter().map(|(f, b)| (format!("file=\"{f}\""), *b as f64)).collect(),
            },
            Family::Gauge { name: "av2_log_bytes", help: "Bytes of the log segments kept.", samples: one(g.log_bytes as f64) },
            Family::Counter { name: "av2_checkpoints_total", help: "Checkpoints written.", samples: one(st.checkpoints as f64) },
            Family::Gauge {
                name: "av2_checkpoint_seconds_max",
                help: "Longest checkpoint since the store opened.",
                samples: one(st.checkpoint_micros_max as f64 / 1e6),
            },
            Family::Counter {
                name: "av2_checkpoint_seconds_total",
                help: "Time spent in checkpoints.",
                samples: one(st.checkpoint_micros_total as f64 / 1e6),
            },
            Family::Counter {
                name: "av2_segments_released_total",
                help: "Log segments released.",
                samples: one(st.segments_released as f64),
            },
            Family::Counter { name: "av2_keymap_compactions_total", help: "Key-map compactions.", samples: one(st.compactions as f64) },
            Family::Counter { name: "av2_file_grows_total", help: "Graph file growths.", samples: one(st.grows as f64) },
            Family::Counter {
                name: "av2_archive_bytes_total",
                help: "Facts archived, before (raw) and after (zstd) compression.",
                samples: vec![("form=\"raw\"".into(), st.archive_raw_bytes as f64), ("form=\"zstd\"".into(), st.archive_bytes as f64)],
            },
            Family::Gauge {
                name: "av2_uptime_seconds",
                help: "Seconds since the service started.",
                samples: one(self.started.elapsed().as_secs_f64()),
            },
        ])
    }

    /// Stop taking inputs, drain the queue, return the store.
    pub fn shutdown(self: Arc<Self>) -> Option<Store> {
        let mut s = Arc::try_unwrap(self).ok()?;
        s.tx = None;
        s.handle.take().and_then(|h| h.join().ok())
    }
}

impl<I, O> Drop for Service<I, O> {
    fn drop(&mut self) {
        self.tx = None;
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}
