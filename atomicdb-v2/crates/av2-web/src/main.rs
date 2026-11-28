//! `av2-web`: serve the v2 pages, assemble the static site, measure.
//!
//! ```text
//! av2-web serve    [--addr 127.0.0.1:3000] [--site-root target/site] [--origin URL]
//!                  [--django-db PATH --secret-file PATH]
//! av2-web assemble --v1-static ../atomicdb/static/atomicdb [--assets crates/av2-web/assets]
//!                  [--logo ../OpenBench/static/logo.svg] [--site-root target/site]
//! av2-web bench    [--site-root target/site] [--n 20000]
//! ```
//!
//! `serve` renders from the fixture (`FixtureSite`) until the store
//! implements `SiteRead`.  With `--django-db` and `--secret-file` it
//! recognises Django sessions (read-only).

use av2_web::auth::{DjangoSqliteSessions, NoSessions, Sessions};
use av2_web::fixture::FixtureSite;
use av2_web::server;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

fn args() -> (String, HashMap<String, String>) {
    let mut it = std::env::args().skip(1);
    let cmd = it.next().unwrap_or_else(|| "serve".into());
    let mut opts = HashMap::new();
    while let Some(a) = it.next() {
        if let Some(k) = a.strip_prefix("--") {
            opts.insert(k.to_string(), it.next().unwrap_or_default());
        }
    }
    (cmd, opts)
}

fn sessions(opts: &HashMap<String, String>) -> Arc<dyn Sessions> {
    match (opts.get("django-db"), opts.get("secret-file")) {
        (Some(db), Some(secret)) => {
            let key = std::fs::read_to_string(secret).expect("secret file").trim().to_string();
            Arc::new(DjangoSqliteSessions::open(db, key).expect("open the Django database read-only"))
        }
        _ => Arc::new(NoSessions),
    }
}

fn main() {
    let (cmd, opts) = args();
    let site_root = PathBuf::from(opts.get("site-root").cloned().unwrap_or_else(|| "target/site".into()));
    match cmd.as_str() {
        "assemble" => {
            let v1 = PathBuf::from(opts.get("v1-static").cloned().unwrap_or_else(|| "../atomicdb/static/atomicdb".into()));
            let assets = PathBuf::from(opts.get("assets").cloned().unwrap_or_else(|| "crates/av2-web/assets".into()));
            let logo = PathBuf::from(opts.get("logo").cloned().unwrap_or_else(|| "../OpenBench/static/logo.svg".into()));
            let (n, raw, br) = server::assemble(&v1, &assets, &site_root, Some(&logo)).expect("assemble");
            println!("{n} files under {}; text {raw} bytes -> {br} bytes brotli", site_root.join("static").display());
        }
        "bench" => bench(&site_root, opts.get("n").and_then(|n| n.parse().ok()).unwrap_or(20_000)),
        _ => {
            let addr = opts.get("addr").cloned().unwrap_or_else(|| "127.0.0.1:3000".into());
            let origin = opts.get("origin").cloned().unwrap_or_else(|| format!("http://{addr}"));
            let site = Arc::new(FixtureSite::load().expect("fixture"));
            let state = server::state(site, sessions(&opts), &site_root, &origin);
            println!("av2-web on http://{addr}/atomicdb/ ({} static files)", state.statics.len());
            let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(4).enable_all().build().expect("runtime");
            rt.block_on(async move {
                let listener = tokio::net::TcpListener::bind(&addr).await.expect("bind");
                axum::serve(listener, server::router(state)).await.expect("serve");
            });
        }
    }
}

/// In-process timings: render per page, then cached responses through the
/// router (no network), median and p99.
fn bench(site_root: &std::path::Path, n: usize) {
    use axum::body::Body;
    use axum::http::Request;
    use std::time::Instant;
    use tower::ServiceExt;
    let site = Arc::new(FixtureSite::load().expect("fixture"));
    let root = av2_web::read::SiteRead::root_key(&*site);
    let nf3 = av2_web::read::SiteRead::explorer(&*site, &root, &[]).expect("root").rows[0].key.clone();
    let state = server::state(site, Arc::new(NoSessions), site_root, "http://localhost");
    let app = server::router(state);
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let pages = [
        format!("/atomicdb/explore/{root}/"),
        format!("/atomicdb/explore/{nf3}/?play=g1f3"),
        "/atomicdb/".to_string(),
        "/atomicdb/roots/".to_string(),
        "/atomicdb/queue/".to_string(),
        "/atomicdb/contributors/".to_string(),
        "/atomicdb/map/".to_string(),
    ];
    // A signed-in hit: the cached pieces with the account's identity zone
    // spliced in, gzip level 1 on the fly.
    {
        use av2_web::pages::shell::{Identity, User};
        let e = av2_web::read::SiteRead::explorer(&FixtureSite::load().unwrap(), &root, &[]).unwrap();
        let rendered = av2_web::pages::finish(av2_web::pages::explore::render(e, &[]), &av2_web::pages::shell::Assets::default());
        let id = Identity { user: Some(User { id: 7, username: "someone".into(), is_staff: false }), next: "/atomicdb/".into() };
        let mut times: Vec<u64> = (0..2000)
            .map(|_| {
                let t = Instant::now();
                let s = rendered.with_identity(&id);
                let z = server::gzip(s.as_bytes(), 1);
                std::hint::black_box(z.len());
                t.elapsed().as_nanos() as u64
            })
            .collect();
        times.sort_unstable();
        println!(
            "signed-in explorer hit (splice + gzip 1): p50 {:.1} us, p99 {:.1} us",
            times[1000] as f64 / 1e3,
            times[1980] as f64 / 1e3
        );
    }
    println!("{:<44} {:>10} {:>9} {:>9} {:>9} {:>9}", "page", "render ms", "html B", "br B", "hit p50us", "hit p99us");
    for uri in pages {
        rt.block_on(async {
            let req = |enc: &str| Request::builder().uri(&uri).header("accept-encoding", enc).body(Body::empty()).unwrap();
            let t = Instant::now();
            let first = app.clone().oneshot(req("identity")).await.unwrap();
            let render_ms = t.elapsed().as_secs_f64() * 1e3;
            let raw = axum::body::to_bytes(first.into_body(), 1 << 26).await.unwrap().len();
            let br = axum::body::to_bytes(app.clone().oneshot(req("br")).await.unwrap().into_body(), 1 << 26).await.unwrap().len();
            let mut times = Vec::with_capacity(n);
            for _ in 0..n {
                let t = Instant::now();
                let r = app.clone().oneshot(req("br")).await.unwrap();
                let _ = axum::body::to_bytes(r.into_body(), 1 << 26).await.unwrap();
                times.push(t.elapsed().as_nanos() as u64);
            }
            times.sort_unstable();
            let p = |q: f64| times[((times.len() as f64 - 1.0) * q) as usize] as f64 / 1000.0;
            let label = if uri.len() > 44 { format!("{}...", &uri[..41]) } else { uri.clone() };
            println!("{label:<44} {render_ms:>10.2} {raw:>9} {br:>9} {:>9.1} {:>9.1}", p(0.5), p(0.99));
        });
    }
}
