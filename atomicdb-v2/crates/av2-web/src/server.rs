//! HTTP: the routes of the ported pages (same URLs as v1), the page cache,
//! conditional requests and the static files.
//!
//! # Page cache
//!
//! A page is rendered once per (path + query, page version) and kept as two
//! shared pieces around the identity zone (`shell::Rendered`) plus the
//! anonymous variant fully assembled and precompressed (brotli and gzip).
//! The page version comes from `SiteRead::page_version`: the commit epoch of
//! what the page shows (for the store: the largest record version among the
//! position and its children; for the global pages a snapshot epoch).  An
//! anonymous hit is a hash-map lookup and a memcpy-free `Bytes` clone.
//!
//! A signed-in visitor gets the same cached pieces with their identity zone
//! spliced in, compressed on the fly (gzip level 1: about 0.2 ms for the
//! explorer).
//!
//! # Conditional requests
//!
//! `ETag` is `"<version>-<xxh3 of the anonymous body>"` (plus the account id
//! for a signed-in variant); `If-None-Match` answers 304 without a body.
//!
//! # Static files
//!
//! Everything under the site root's `static/` is loaded into memory at start
//! with its `.br`/`.gz` siblings (written by `av2-web assemble`), served with
//! a strong `ETag`; a `?v=` URL is immutable for a year.

use crate::auth::{Sessions, cookie};
use crate::pages::{
    self, Page,
    shell::{Assets, Identity, Rendered},
};
use crate::read::{PageKind, SiteRead};
use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, HeaderValue, Response, StatusCode, Uri, header};
use axum::response::IntoResponse;
use axum::routing::get;
use axum::{Json, Router};
use std::collections::HashMap;
use std::io::Write;
use std::path::{Path as FsPath, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::Instant;

/// One cached page.
pub struct Cached {
    version: u64,
    rendered: Rendered,
    status: StatusCode,
    etag: String,
    raw: Bytes,
    br: Bytes,
    gz: Bytes,
}

#[derive(Default)]
pub struct PageCache {
    map: RwLock<HashMap<String, Arc<Cached>>>,
}

/// Entries kept before the cache is emptied (crude but bounded; explorer
/// URLs carry routes, so the key space is open).
const CACHE_MAX: usize = 20_000;

impl PageCache {
    fn get(&self, key: &str, version: u64) -> Option<Arc<Cached>> {
        let m = self.map.read().ok()?;
        m.get(key).filter(|c| c.version == version).cloned()
    }
    fn put(&self, key: String, c: Arc<Cached>) {
        if let Ok(mut m) = self.map.write() {
            if m.len() >= CACHE_MAX {
                m.clear();
            }
            m.insert(key, c);
        }
    }
    pub fn len(&self) -> usize {
        self.map.read().map(|m| m.len()).unwrap_or(0)
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// A static file in memory.
pub struct StaticFile {
    ctype: &'static str,
    etag: String,
    raw: Bytes,
    br: Option<Bytes>,
    gz: Option<Bytes>,
}

pub struct AppState {
    pub site: Arc<dyn SiteRead>,
    pub sessions: Arc<dyn Sessions>,
    pub assets: Assets,
    pub cache: PageCache,
    pub statics: HashMap<String, StaticFile>,
}

pub type Shared = Arc<AppState>;

// ---------------------------------------------------------------- compression

pub fn brotli(data: &[u8], quality: u32) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len() / 4);
    {
        let mut w = brotli::CompressorWriter::new(&mut out, 64 * 1024, quality, 22);
        let _ = w.write_all(data);
    }
    out
}

pub fn gzip(data: &[u8], level: u32) -> Vec<u8> {
    let mut e = flate2::write::GzEncoder::new(Vec::with_capacity(data.len() / 4), flate2::Compression::new(level));
    let _ = e.write_all(data);
    e.finish().unwrap_or_default()
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Enc {
    Br,
    Gz,
    Identity,
}

fn negotiate(h: &HeaderMap) -> Enc {
    let ae = h.get(header::ACCEPT_ENCODING).and_then(|v| v.to_str().ok()).unwrap_or("");
    let has = |name: &str| ae.split(',').any(|p| p.trim().split(';').next().unwrap_or("").eq_ignore_ascii_case(name));
    if has("br") {
        Enc::Br
    } else if has("gzip") {
        Enc::Gz
    } else {
        Enc::Identity
    }
}

fn if_none_match(h: &HeaderMap, etag: &str) -> bool {
    h.get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.split(',').any(|t| t.trim().trim_start_matches("W/") == etag || t.trim() == "*"))
}

// ---------------------------------------------------------------- pages

fn identity(state: &AppState, h: &HeaderMap, uri: &Uri) -> Identity {
    let path = uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let mut id = Identity::anonymous(path);
    if let Some(c) = h.get(header::COOKIE).and_then(|v| v.to_str().ok())
        && let Some(s) = cookie(c, "sessionid")
    {
        id.user = state.sessions.user(s);
    }
    id
}

/// Render `page` and assemble its anonymous variant for `path` (the sign-in
/// links carry the path, which is the cache key, so the anonymous body is
/// the same for every anonymous visitor of that URL).
fn build(state: &AppState, page: Page, version: u64, status: StatusCode, path: &str) -> Cached {
    let rendered = pages::finish(page, &state.assets);
    let raw = rendered.with_identity(&Identity::anonymous(path));
    let hash = xxhash_rust::xxh3::xxh3_64(raw.as_bytes());
    Cached {
        version,
        status,
        etag: format!("\"{version:x}-{hash:016x}\""),
        br: Bytes::from(brotli(raw.as_bytes(), 5)),
        gz: Bytes::from(gzip(raw.as_bytes(), 6)),
        raw: Bytes::from(raw),
        rendered,
    }
}

fn respond(state: &AppState, h: &HeaderMap, uri: &Uri, c: &Cached, started: Instant) -> Response<Body> {
    let id = identity(state, h, uri);
    let enc = negotiate(h);
    let mut b = Response::builder()
        .status(c.status)
        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
        .header(header::VARY, "Accept-Encoding, Cookie")
        .header("X-Content-Type-Options", "nosniff")
        .header("X-Frame-Options", "DENY");
    let (etag, body, used) = match &id.user {
        None => {
            let (bytes, used) = match enc {
                Enc::Br => (c.br.clone(), Enc::Br),
                Enc::Gz => (c.gz.clone(), Enc::Gz),
                Enc::Identity => (c.raw.clone(), Enc::Identity),
            };
            (c.etag.clone(), bytes, used)
        }
        Some(u) => {
            let s = c.rendered.with_identity(&id);
            let etag = format!("{}-u{}\"", c.etag.trim_end_matches('"'), u.id);
            match enc {
                Enc::Identity => (etag, Bytes::from(s), Enc::Identity),
                _ => (etag, Bytes::from(gzip(s.as_bytes(), 1)), Enc::Gz),
            }
        }
    };
    b = b
        .header(header::ETAG, &etag)
        .header(header::CACHE_CONTROL, if id.user.is_some() { "private, no-cache" } else { "public, no-cache" });
    if if_none_match(h, &etag) {
        return b.status(StatusCode::NOT_MODIFIED).body(Body::empty()).unwrap_or_default();
    }
    if used != Enc::Identity {
        b = b.header(header::CONTENT_ENCODING, if used == Enc::Br { "br" } else { "gzip" });
    }
    let us = started.elapsed().as_micros();
    b.header("Server-Timing", format!("app;dur={:.3}", us as f64 / 1000.0)).body(Body::from(body)).unwrap_or_default()
}

/// Serve a page from the cache, rendering it on a miss.
fn page(
    state: &AppState,
    h: &HeaderMap,
    uri: &Uri,
    kind: PageKind,
    key: Option<&str>,
    render: impl FnOnce() -> (Page, StatusCode),
) -> Response<Body> {
    let started = Instant::now();
    let version = state.site.page_version(kind, key);
    let ckey = uri.path_and_query().map(|p| p.as_str().to_string()).unwrap_or_default();
    let cached = match state.cache.get(&ckey, version) {
        Some(c) => c,
        None => {
            let (p, status) = render();
            let c = Arc::new(build(state, p, version, status, &ckey));
            state.cache.put(ckey, c.clone());
            c
        }
    };
    respond(state, h, uri, &cached, started)
}

fn route_of(q: &HashMap<String, String>) -> Vec<String> {
    q.get("play").map(|p| p.split(',').filter(|s| !s.is_empty()).map(str::to_string).collect()).unwrap_or_default()
}

fn is_key(k: &str) -> bool {
    k.len() == 64 && k.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

async fn explore(
    State(s): State<Shared>,
    Path(key): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    h: HeaderMap,
    uri: Uri,
) -> Response<Body> {
    let route = route_of(&q);
    let k = key.clone();
    page(&s, &h, &uri, PageKind::Explorer, Some(&key), || match is_key(&k).then(|| s.site.explorer(&k, &route)).flatten() {
        Some(e) => (pages::explore::render(e, &route), StatusCode::OK),
        None => (pages::missing(None), StatusCode::NOT_FOUND),
    })
}

async fn home(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    let root = s.site.root_key();
    page(&s, &h, &uri, PageKind::Home, Some(&root), || match s.site.explorer(&root, &[]) {
        Some(e) => {
            let mut p = pages::explore::render(e, &[]);
            p.meta.title = "AtomicDB".into();
            p.meta.description = None;
            p.meta.canonical = Some("/atomicdb/".into());
            (p, StatusCode::OK)
        }
        None => (pages::missing(None), StatusCode::NOT_FOUND),
    })
}

async fn roots(State(s): State<Shared>, Query(q): Query<HashMap<String, String>>, h: HeaderMap, uri: Uri) -> Response<Body> {
    let sort = q.get("sort").cloned().unwrap_or_default();
    page(&s, &h, &uri, PageKind::Roots, None, || (pages::roots::render(s.site.roots(), &sort), StatusCode::OK))
}

async fn queue(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    page(&s, &h, &uri, PageKind::Queue, None, || (pages::queue::render(s.site.queue()), StatusCode::OK))
}

async fn contributors(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    page(&s, &h, &uri, PageKind::Contributors, None, || (pages::contributors::render(s.site.leaderboard()), StatusCode::OK))
}

async fn map(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    page(&s, &h, &uri, PageKind::Map, None, || {
        let root = s.site.root_key();
        let fen = s.site.explorer(&root, &[]).map(|e| e.pos.fen).unwrap_or_default();
        (pages::map::render(&root, &fen), StatusCode::OK)
    })
}

/// v1 `api/live-request/<key>/`: `{"live": false}` when nothing is in
/// flight, else the sentence and chip.
async fn live_request(State(s): State<Shared>, Path(key): Path<String>) -> impl IntoResponse {
    let mut r = match s.site.live_request(&key) {
        Some(l) => Json(serde_json::json!({"live": true, "text": l.text, "chip": l.chip})).into_response(),
        None => Json(serde_json::json!({"live": false})).into_response(),
    };
    r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    r
}

async fn not_ported(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    let started = Instant::now();
    let p = pages::missing(Some("This page is not part of the v2 site yet."));
    let path = uri.path_and_query().map(|p| p.as_str().to_string()).unwrap_or_default();
    let c = build(&s, p, 0, StatusCode::NOT_FOUND, &path);
    respond(&s, &h, &uri, &c, started)
}

async fn static_file(State(s): State<Shared>, Path(path): Path<String>, h: HeaderMap, uri: Uri) -> Response<Body> {
    let Some(f) = s.statics.get(&path) else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    let versioned = uri.query().is_some_and(|q| q.starts_with("v="));
    let mut b = Response::builder()
        .header(header::CONTENT_TYPE, f.ctype)
        .header(header::ETAG, &f.etag)
        .header(header::VARY, "Accept-Encoding")
        .header(header::CACHE_CONTROL, if versioned { "public, max-age=31536000, immutable" } else { "public, max-age=3600" });
    if if_none_match(&h, &f.etag) {
        return b.status(StatusCode::NOT_MODIFIED).body(Body::empty()).unwrap_or_default();
    }
    let (bytes, enc) = match (negotiate(&h), &f.br, &f.gz) {
        (Enc::Br, Some(br), _) => (br.clone(), Some("br")),
        (Enc::Br | Enc::Gz, _, Some(gz)) => (gz.clone(), Some("gzip")),
        _ => (f.raw.clone(), None),
    };
    if let Some(e) = enc {
        b = b.header(header::CONTENT_ENCODING, e);
    }
    b.body(Body::from(bytes)).unwrap_or_default()
}

pub fn router(state: Shared) -> Router {
    Router::new()
        .route("/atomicdb/", get(home))
        .route("/atomicdb/explore/{key}/", get(explore))
        .route("/atomicdb/roots/", get(roots))
        .route("/atomicdb/queue/", get(queue))
        .route("/atomicdb/contributors/", get(contributors))
        .route("/atomicdb/map/", get(map))
        .route("/atomicdb/api/live-request/{key}/", get(live_request))
        .route("/static/{*path}", get(static_file))
        .fallback(not_ported)
        .with_state(state)
}

// ---------------------------------------------------------------- statics

pub fn content_type(path: &str) -> &'static str {
    match path.rsplit('.').next().unwrap_or("") {
        "css" => "text/css; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "wasm" => "application/wasm",
        "webp" => "image/webp",
        "png" => "image/png",
        "html" => "text/html; charset=utf-8",
        "md" | "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn compressible(path: &str) -> bool {
    matches!(path.rsplit('.').next().unwrap_or(""), "css" | "js" | "mjs" | "json" | "svg" | "wasm" | "html" | "txt" | "md")
}

fn walk(dir: &FsPath, out: &mut Vec<PathBuf>) {
    if let Ok(rd) = std::fs::read_dir(dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                walk(&p, out);
            } else {
                out.push(p);
            }
        }
    }
}

/// Load `<site_root>/static/**` into memory (with precompressed siblings).
/// Returns the files and the static version (first 10 hex characters of a
/// digest of every file, v1's `static_version`).
pub fn load_statics(site_root: &FsPath) -> (HashMap<String, StaticFile>, String) {
    let base = site_root.join("static");
    let mut files = Vec::new();
    walk(&base, &mut files);
    files.sort();
    let mut map = HashMap::new();
    let mut digest = xxhash_rust::xxh3::Xxh3::new();
    for p in files {
        let rel = p.strip_prefix(&base).unwrap_or(&p).to_string_lossy().replace('\\', "/");
        if rel.ends_with(".br") || rel.ends_with(".gz") {
            continue;
        }
        let Ok(raw) = std::fs::read(&p) else { continue };
        let read = |ext: &str| std::fs::read(format!("{}.{ext}", p.display())).ok().map(Bytes::from);
        let h = xxhash_rust::xxh3::xxh3_64(&raw);
        digest.update(rel.as_bytes());
        digest.update(&h.to_le_bytes());
        map.insert(
            rel.clone(),
            StaticFile { ctype: content_type(&rel), etag: format!("\"{h:016x}\""), br: read("br"), gz: read("gz"), raw: Bytes::from(raw) },
        );
    }
    (map, format!("{:016x}", digest.digest())[..10].to_string())
}

/// Copy the v1 static tree, the v2 assets and OpenBench's logo under `<site_root>/static/` and
/// write `.br` (quality 11) and `.gz` (level 9) siblings for text files.
pub fn assemble(v1_static: &FsPath, v2_assets: &FsPath, site_root: &FsPath, logo: Option<&FsPath>) -> std::io::Result<(usize, u64, u64)> {
    let dest = site_root.join("static");
    let mut copied = Vec::new();
    // The nav's OpenBench logo is OpenBench's /static/logo.svg.
    if let Some(logo) = logo.filter(|l| l.exists()) {
        std::fs::create_dir_all(&dest)?;
        std::fs::copy(logo, dest.join("logo.svg"))?;
        copied.push(dest.join("logo.svg"));
    }
    for (src, sub) in [(v1_static.to_path_buf(), PathBuf::from("atomicdb")), (v2_assets.to_path_buf(), PathBuf::from("atomicdb/v2"))] {
        let mut files = Vec::new();
        walk(&src, &mut files);
        for f in files {
            let rel = f.strip_prefix(&src).unwrap_or(&f);
            let to = dest.join(&sub).join(rel);
            if let Some(parent) = to.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::copy(&f, &to)?;
            copied.push(to);
        }
    }
    // The islands' bundle, if cargo leptos built it.
    let mut pkg = Vec::new();
    walk(&dest.join("atomicdb/pkg"), &mut pkg);
    copied.extend(pkg.into_iter().filter(|p| !p.to_string_lossy().ends_with(".br") && !p.to_string_lossy().ends_with(".gz")));
    let (mut raw_total, mut br_total) = (0u64, 0u64);
    for f in &copied {
        let name = f.to_string_lossy().to_string();
        if !compressible(&name) {
            continue;
        }
        let data = std::fs::read(f)?;
        let br = brotli(&data, 11);
        raw_total += data.len() as u64;
        br_total += br.len() as u64;
        std::fs::write(format!("{name}.br"), br)?;
        std::fs::write(format!("{name}.gz"), gzip(&data, 9))?;
    }
    Ok((copied.len(), raw_total, br_total))
}

/// Build the app state from a site and the built site root.
pub fn state(site: Arc<dyn SiteRead>, sessions: Arc<dyn Sessions>, site_root: &FsPath, origin: &str) -> Shared {
    let (statics, version) = load_statics(site_root);
    let hydration = hydration_scripts();
    Arc::new(AppState {
        site,
        sessions,
        assets: Assets { version, hydration, origin: origin.to_string() },
        cache: PageCache::default(),
        statics,
    })
}

/// Leptos's `<HydrationScripts islands=true>` for our package path.
pub fn hydration_scripts() -> String {
    use leptos::prelude::*;
    let options = leptos::config::LeptosOptions::builder().output_name("av2_web").site_pkg_dir("static/atomicdb/pkg").build();
    let html = pages::html(move || view! { <leptos::hydration::HydrationScripts options=options islands=true/> });
    format!("<style>leptos-island{{display:contents}}</style>\n{html}\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::NoSessions;
    use crate::fixture::FixtureSite;
    use axum::http::Request;
    use tower::ServiceExt;

    fn app() -> (Router, Arc<FixtureSite>) {
        let site = Arc::new(FixtureSite::load().unwrap());
        let st = state(site.clone(), Arc::new(NoSessions), FsPath::new("does-not-exist"), "https://example.org");
        (router(st), site)
    }

    async fn get(app: &Router, uri: &str, headers: &[(&str, &str)]) -> Response<Body> {
        let mut r = Request::builder().uri(uri);
        for (k, v) in headers {
            r = r.header(*k, *v);
        }
        app.clone().oneshot(r.body(Body::empty()).unwrap()).await.unwrap()
    }

    async fn text(r: Response<Body>) -> String {
        let b = axum::body::to_bytes(r.into_body(), 1 << 26).await.unwrap();
        String::from_utf8(b.to_vec()).unwrap()
    }

    const ROOT: &str = "b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879";

    #[tokio::test]
    async fn explorer_renders_like_v1() {
        let (app, _) = app();
        let r = get(&app, &format!("/atomicdb/explore/{ROOT}/"), &[]).await;
        assert_eq!(r.status(), StatusCode::OK);
        let html = text(r).await;
        for needle in [
            "<h2 id=\"opening-name\"",
            "Starting position",
            "class=\"crumb pos-eval\"",
            "data-orig=\"+1184cp\"",
            "id=\"board\"",
            "class=\"atomic-board cg-wrap brown cburnett\"",
            "data-best-move=\"g1f3\"",
            "id=\"reqbtn\"",
            "<strong data-uci=\"g1f3\" title=\"g1f3\">Nf3</strong>",
            "<span class=\"opening-enters\">King's Knight Opening</span>",
            "class=\"dim proofnums\">170 / 967</td>",
            "<td title=\"searched to 10B nodes \u{b7} visits 2\" class=\"dim\">10B</td>",
            "class=\"wdl-w\" style=\"width:99%;\"",
            "visits 6 \u{b7} nodes 12.78B \u{b7} 3,504.9s",
            "https://lichess.org/analysis/atomic/rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR_w_KQkq_-_0_1",
            "<leptos-island",
            "/static/atomicdb/v2/explore.js",
        ] {
            assert!(html.contains(needle), "missing {needle:?}");
        }
    }

    #[tokio::test]
    async fn route_builds_line_and_links() {
        let (app, site) = app();
        let nf3 = site.explorer(ROOT, &[]).unwrap().rows[0].key.clone();
        let html = text(get(&app, &format!("/atomicdb/explore/{nf3}/?play=g1f3"), &[]).await).await;
        assert!(html.contains("<div class=\"linebox\">"));
        assert!(html.contains("<span class=\"dim\">1.</span>"));
        assert!(html.contains("?play=g1f3%2C"), "child links extend the route");
    }

    #[tokio::test]
    async fn unknown_position_is_404() {
        let (app, _) = app();
        let r = get(&app, &format!("/atomicdb/explore/{}/", "0".repeat(64)), &[]).await;
        assert_eq!(r.status(), StatusCode::NOT_FOUND);
        let r = get(&app, "/atomicdb/explore/not-a-key/", &[]).await;
        assert_eq!(r.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn etag_and_compression() {
        let (app, site) = app();
        let r = get(&app, "/atomicdb/queue/", &[("accept-encoding", "gzip, deflate, br")]).await;
        assert_eq!(r.headers()[header::CONTENT_ENCODING], "br");
        let etag = r.headers()[header::ETAG].to_str().unwrap().to_string();
        let r = get(&app, "/atomicdb/queue/", &[("if-none-match", &etag)]).await;
        assert_eq!(r.status(), StatusCode::NOT_MODIFIED);
        site.bump_epoch();
        let r = get(&app, "/atomicdb/queue/", &[("if-none-match", &etag)]).await;
        assert_eq!(r.status(), StatusCode::OK, "a new epoch is a new ETag");
        let r = get(&app, "/atomicdb/queue/", &[("accept-encoding", "gzip")]).await;
        assert_eq!(r.headers()[header::CONTENT_ENCODING], "gzip");
    }

    #[tokio::test]
    async fn other_pages_render() {
        let (app, _) = app();
        for (uri, needle) in [
            ("/atomicdb/roots/", "Start position and first moves"),
            ("/atomicdb/roots/?sort=-now", "Named openings"),
            ("/atomicdb/queue/", "nodes searched in the last hour"),
            ("/atomicdb/contributors/", "lb-medal first"),
            ("/atomicdb/map/", "id=\"conquest-map\""),
            ("/atomicdb/", "Starting position"),
        ] {
            let r = get(&app, uri, &[]).await;
            assert_eq!(r.status(), StatusCode::OK, "{uri}");
            assert!(text(r).await.contains(needle), "{uri}: {needle}");
        }
        let r = get(&app, "/atomicdb/workers/", &[]).await;
        assert_eq!(r.status(), StatusCode::NOT_FOUND);
    }

    #[test]
    fn negotiation() {
        let mut h = HeaderMap::new();
        h.insert(header::ACCEPT_ENCODING, HeaderValue::from_static("gzip;q=1.0, br;q=0.9"));
        assert_eq!(negotiate(&h), Enc::Br);
        h.insert(header::ACCEPT_ENCODING, HeaderValue::from_static("identity"));
        assert_eq!(negotiate(&h), Enc::Identity);
    }
}
