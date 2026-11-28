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
//! a strong `ETag`; a `?v=` URL is immutable for a year, anything else
//! revalidates (`no-cache`).

use crate::auth::{Sessions, cookie};
use crate::pages::{
    self, Page,
    shell::{Assets, Identity, Rendered, User},
};
use crate::read::{PageKind, SiteRead};
use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, HeaderValue, Response, StatusCode, Uri, header};
use axum::response::IntoResponse;
use axum::routing::get;
use axum::{Json, Router};
use std::collections::HashMap;
use std::collections::HashSet;
use std::io::Write;
use std::path::{Path as FsPath, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

/// One cached response: a page (HTML, with the identity zone spliced per
/// visitor) or the JSON of an in-place navigation.
pub struct Cached {
    version: u64,
    built: Instant,
    rendered: Option<Rendered>,
    status: StatusCode,
    ctype: &'static str,
    etag: String,
    raw: Bytes,
    br: Bytes,
    gz: Bytes,
}

#[derive(Default)]
pub struct PageCache {
    map: RwLock<HashMap<String, Arc<Cached>>>,
    rebuilding: Mutex<HashSet<String>>,
}

/// Entries kept before the cache is emptied (crude but bounded; explorer
/// URLs carry routes, so the key space is open).
const CACHE_MAX: usize = 20_000;

/// How long an outdated copy of a slow page may be served while one thread
/// rebuilds it (v1 `page_cache.stale_while_revalidate`: 600 s).
const STALE_MAX: Duration = Duration::from_secs(600);

impl PageCache {
    fn get(&self, key: &str) -> Option<Arc<Cached>> {
        self.map.read().ok()?.get(key).cloned()
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
    /// Background rebuilds in flight.
    pub fn rebuilding(&self) -> usize {
        self.rebuilding.lock().map(|r| r.len()).unwrap_or(0)
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

/// A request as the renderers see it: owned, so a rebuild can run on another
/// thread after the request is answered.
#[derive(Clone, Debug)]
pub struct Req {
    /// Path and query of the page (for a navigation JSON: of the page it
    /// navigates to).
    pub path: String,
    pub query: HashMap<String, String>,
    /// Path parameters in order (`<key>`, `<username>`...).
    pub params: Vec<String>,
    /// The visitor, for the few pages that differ by account (their cache
    /// key carries the account id).
    pub user: Option<User>,
}

impl Req {
    pub fn param(&self, i: usize) -> &str {
        self.params.get(i).map(String::as_str).unwrap_or("")
    }
}

/// What a route renders.
pub type RenderFn = fn(&AppState, &Req) -> (Page, StatusCode);

/// The two representations of a page.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Output {
    Html,
    /// `{url, title, description, canonical, main}` for explore-nav.js.
    Nav,
}

/// How a route is cached.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Policy {
    /// Render inline on a version change (cheap pages).
    Fresh,
    /// Serve the outdated copy and rebuild in the background (slow pages).
    StaleWhileRevalidate,
    /// The page differs by account: one cache entry per account.
    PerAccount,
}

fn identity(state: &AppState, h: &HeaderMap, uri: &Uri) -> Identity {
    let path = uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let mut id = Identity::anonymous(path);
    if let Some(c) = h.get(header::COOKIE).and_then(|v| v.to_str().ok())
        && let Some(s) = cookie(c, "sessionid")
    {
        id.user = state.sessions.user(s);
        id.notices = id.user.as_ref().map(|u| state.site.notices(u.id));
    }
    id
}

#[derive(serde::Serialize)]
struct NavJson<'a> {
    url: &'a str,
    title: &'a str,
    description: Option<&'a str>,
    canonical: Option<&'a str>,
    main: &'a str,
}

/// Render `page` into a cache entry.  HTML: the anonymous variant is
/// assembled for `path` (the sign-in links carry it, and the path is the
/// cache key, so it is the same for every anonymous visitor of that URL).
fn build(state: &AppState, page: Page, version: u64, status: StatusCode, path: &str, out: Output) -> Cached {
    let (rendered, raw, ctype) = match out {
        Output::Html => {
            let rendered = pages::finish(page, &state.assets);
            let raw = rendered.with_identity(&Identity::anonymous(path));
            (Some(rendered), raw, "text/html; charset=utf-8")
        }
        Output::Nav => {
            let json = serde_json::to_string(&NavJson {
                url: path,
                title: &page.meta.title,
                description: page.meta.description.as_deref(),
                canonical: page.meta.canonical.as_deref(),
                main: &page.body,
            })
            .unwrap_or_default();
            (None, json, "application/json")
        }
    };
    let hash = xxhash_rust::xxh3::xxh3_64(raw.as_bytes());
    Cached {
        version,
        built: Instant::now(),
        status,
        ctype,
        etag: format!("\"{version:x}-{hash:016x}\""),
        br: Bytes::from(brotli(raw.as_bytes(), 5)),
        gz: Bytes::from(gzip(raw.as_bytes(), 6)),
        raw: Bytes::from(raw),
        rendered,
    }
}

fn respond(state: &AppState, h: &HeaderMap, uri: &Uri, c: &Cached, started: Instant) -> Response<Body> {
    let id = if c.rendered.is_some() { identity(state, h, uri) } else { Identity::default() };
    let enc = negotiate(h);
    let mut b = Response::builder()
        .status(c.status)
        .header(header::CONTENT_TYPE, c.ctype)
        .header(header::VARY, if c.rendered.is_some() { "Accept-Encoding, Cookie" } else { "Accept-Encoding" })
        .header("X-Content-Type-Options", "nosniff")
        .header("X-Frame-Options", "DENY");
    let (etag, body, used) = match (&id.user, &c.rendered) {
        (Some(u), Some(rendered)) => {
            let s = rendered.with_identity(&id);
            let etag = format!("{}-u{}\"", c.etag.trim_end_matches('"'), u.id);
            match enc {
                Enc::Identity => (etag, Bytes::from(s), Enc::Identity),
                _ => (etag, Bytes::from(gzip(s.as_bytes(), 1)), Enc::Gz),
            }
        }
        _ => {
            let (bytes, used) = match enc {
                Enc::Br => (c.br.clone(), Enc::Br),
                Enc::Gz => (c.gz.clone(), Enc::Gz),
                Enc::Identity => (c.raw.clone(), Enc::Identity),
            };
            (c.etag.clone(), bytes, used)
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

/// Everything a cached route needs.
pub struct Route {
    pub kind: PageKind,
    pub render: RenderFn,
    pub output: Output,
    pub policy: Policy,
}

/// Serve a route from the cache: a hit at the current page version is a
/// lookup; a slow page with an outdated copy is served stale while one
/// background thread rebuilds it; anything else renders inline.
fn serve(state: &Shared, h: &HeaderMap, uri: &Uri, route: Route, req: Req) -> Response<Body> {
    let started = Instant::now();
    let key = req.params.first().map(String::as_str);
    let version = state.site.page_version(route.kind, key);
    let mut ckey = match route.output {
        Output::Html => req.path.clone(),
        Output::Nav => format!("nav:{}", req.path),
    };
    if route.policy == Policy::PerAccount {
        ckey.push_str(&format!("#u{}", req.user.as_ref().map(|u| u.id).unwrap_or(0)));
    }
    let current = state.cache.get(&ckey);
    if let Some(c) = &current {
        if c.version == version {
            return respond(state, h, uri, c, started);
        }
        if route.policy == Policy::StaleWhileRevalidate && c.built.elapsed() < STALE_MAX {
            rebuild_in_background(state, ckey, route, req, version);
            return respond(state, h, uri, c, started);
        }
    }
    let (p, status) = (route.render)(state, &req);
    let c = Arc::new(build(state, p, version, status, &req.path, route.output));
    state.cache.put(ckey, c.clone());
    respond(state, h, uri, &c, started)
}

fn rebuild_in_background(state: &Shared, ckey: String, route: Route, req: Req, version: u64) {
    let Ok(mut r) = state.cache.rebuilding.lock() else { return };
    if !r.insert(ckey.clone()) {
        return;
    }
    drop(r);
    let st = state.clone();
    let job = move || {
        let (p, status) = (route.render)(&st, &req);
        let c = Arc::new(build(&st, p, version, status, &req.path, route.output));
        st.cache.put(ckey.clone(), c);
        if let Ok(mut r) = st.cache.rebuilding.lock() {
            r.remove(&ckey);
        }
    };
    match tokio::runtime::Handle::try_current() {
        Ok(h) => {
            h.spawn_blocking(job);
        }
        Err(_) => job(),
    }
}

fn req(uri: &Uri, query: HashMap<String, String>, params: Vec<String>) -> Req {
    Req { path: uri.path_and_query().map(|p| p.as_str().to_string()).unwrap_or_default(), query, params, user: None }
}

fn route_of(q: &HashMap<String, String>) -> Vec<String> {
    q.get("play").map(|p| p.split(',').filter(|s| !s.is_empty()).map(str::to_string).collect()).unwrap_or_default()
}

fn is_key(k: &str) -> bool {
    k.len() == 64 && k.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn render_explore(s: &AppState, r: &Req) -> (Page, StatusCode) {
    let k = r.param(0);
    let route = route_of(&r.query);
    match is_key(k).then(|| s.site.explorer(k, &route)).flatten() {
        Some(e) => (pages::explore::render(e, &route), StatusCode::OK),
        None => (pages::missing(None), StatusCode::NOT_FOUND),
    }
}

async fn explore(
    State(s): State<Shared>,
    Path(key): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    h: HeaderMap,
    uri: Uri,
) -> Response<Body> {
    let route = Route { kind: PageKind::Explorer, render: render_explore, output: Output::Html, policy: Policy::Fresh };
    serve(&s, &h, &uri, route, req(&uri, q, vec![key]))
}

/// `nav/explore/<key>/`: the explorer page as JSON, for explore-nav.js.
async fn nav_explore(
    State(s): State<Shared>,
    Path(key): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    h: HeaderMap,
    uri: Uri,
) -> Response<Body> {
    let mut r = req(&uri, q, vec![key]);
    r.path = r.path.replacen("/atomicdb/nav/", "/atomicdb/", 1);
    let route = Route { kind: PageKind::Explorer, render: render_explore, output: Output::Nav, policy: Policy::Fresh };
    serve(&s, &h, &uri, route, r)
}

async fn roots(State(s): State<Shared>, Query(q): Query<HashMap<String, String>>, h: HeaderMap, uri: Uri) -> Response<Body> {
    fn render(s: &AppState, r: &Req) -> (Page, StatusCode) {
        let sort = r.query.get("sort").cloned().unwrap_or_default();
        (pages::roots::render(s.site.roots(), &sort), StatusCode::OK)
    }
    let route = Route { kind: PageKind::Roots, render, output: Output::Html, policy: Policy::StaleWhileRevalidate };
    serve(&s, &h, &uri, route, req(&uri, q, vec![]))
}

async fn queue(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    fn render(s: &AppState, r: &Req) -> (Page, StatusCode) {
        (pages::queue::render(s.site.queue(), r.user.as_ref()), StatusCode::OK)
    }
    // The backlog box offers its adopt buttons to an account: one entry per account.
    let mut r = req(&uri, HashMap::new(), vec![]);
    r.user = identity(&s, &h, &uri).user;
    let route = Route { kind: PageKind::Queue, render, output: Output::Html, policy: Policy::PerAccount };
    serve(&s, &h, &uri, route, r)
}

/// `docs/` and `docs/<slug>/`: static per deploy (version 0).
async fn docs(State(s): State<Shared>, slug: Option<Path<String>>, h: HeaderMap, uri: Uri) -> Response<Body> {
    fn render(s: &AppState, r: &Req) -> (Page, StatusCode) {
        match pages::docs::render(r.param(0), &s.assets.v()) {
            Some(p) => (p, StatusCode::OK),
            None => (pages::missing(None), StatusCode::NOT_FOUND),
        }
    }
    let slug = slug.map(|Path(p)| p).unwrap_or_default();
    let route = Route { kind: PageKind::Static, render, output: Output::Html, policy: Policy::Fresh };
    serve(&s, &h, &uri, route, req(&uri, HashMap::new(), vec![slug]))
}

async fn contributors(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    fn render(s: &AppState, _: &Req) -> (Page, StatusCode) {
        (pages::contributors::render(s.site.leaderboard()), StatusCode::OK)
    }
    let route = Route { kind: PageKind::Contributors, render, output: Output::Html, policy: Policy::Fresh };
    serve(&s, &h, &uri, route, req(&uri, HashMap::new(), vec![]))
}

async fn map(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    fn render(s: &AppState, _: &Req) -> (Page, StatusCode) {
        let root = s.site.root_key();
        let fen = s.site.explorer(&root, &[]).map(|e| e.pos.fen).unwrap_or_default();
        (pages::map::render(&root, &fen), StatusCode::OK)
    }
    let route = Route { kind: PageKind::Map, render, output: Output::Html, policy: Policy::Fresh };
    serve(&s, &h, &uri, route, req(&uri, HashMap::new(), vec![]))
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
    let c = build(&s, p, 0, StatusCode::NOT_FOUND, &path, Output::Html);
    respond(&s, &h, &uri, &c, started)
}
fn render_home(s: &AppState, _: &Req) -> (Page, StatusCode) {
    let root = s.site.root_key();
    match s.site.explorer(&root, &[]) {
        Some(e) => (pages::home::render(e, s.site.home(), s.site.leaderboard()), StatusCode::OK),
        None => (pages::missing(None), StatusCode::NOT_FOUND),
    }
}

async fn home(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    let root = s.site.root_key();
    let route = Route { kind: PageKind::Home, render: render_home, output: Output::Html, policy: Policy::StaleWhileRevalidate };
    serve(&s, &h, &uri, route, req(&uri, HashMap::new(), vec![root]))
}

async fn milestones(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    fn render(s: &AppState, _: &Req) -> (Page, StatusCode) {
        (pages::feeds::milestones(s.site.milestones()), StatusCode::OK)
    }
    let route = Route { kind: PageKind::Milestones, render, output: Output::Html, policy: Policy::Fresh };
    serve(&s, &h, &uri, route, req(&uri, HashMap::new(), vec![]))
}

async fn campaigns(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    fn render(s: &AppState, _: &Req) -> (Page, StatusCode) {
        (pages::feeds::campaigns(s.site.campaigns()), StatusCode::OK)
    }
    let route = Route { kind: PageKind::Campaigns, render, output: Output::Html, policy: Policy::Fresh };
    serve(&s, &h, &uri, route, req(&uri, HashMap::new(), vec![]))
}

/// `notifications/`: one account's page, never cached; anonymous visitors
/// are sent to sign in, as v1 does.
async fn notifications(State(s): State<Shared>, h: HeaderMap, uri: Uri) -> Response<Body> {
    let started = Instant::now();
    let id = identity(&s, &h, &uri);
    let Some(user) = id.user else {
        return Response::builder()
            .status(StatusCode::FOUND)
            .header(header::LOCATION, "/login/?next=/atomicdb/notifications/")
            .body(Body::empty())
            .unwrap_or_default();
    };
    let page = pages::feeds::notifications(s.site.notices(user.id));
    let path = uri.path_and_query().map(|p| p.as_str().to_string()).unwrap_or_default();
    let c = build(&s, page, 0, StatusCode::OK, &path, Output::Html);
    respond(&s, &h, &uri, &c, started)
}

/// `sitemap.xml` (v1 `views.sitemap`): the overview pages, the docs and every
/// named opening present in the tree.
async fn sitemap(State(s): State<Shared>) -> Response<Body> {
    let origin = &s.assets.origin;
    let mut xml =
        String::from("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n");
    let mut add = |path: &str| xml.push_str(&format!("  <url><loc>{origin}{path}</loc></url>\n"));
    for p in ["/atomicdb/", "/atomicdb/roots/", "/atomicdb/docs/"] {
        add(p);
    }
    for (slug, _) in pages::docs::PAGES {
        add(&format!("/atomicdb/docs/{slug}/"));
    }
    for o in s.site.roots().openings.iter().filter(|o| o.present) {
        add(&format!("/atomicdb/explore/{}/", o.key));
    }
    xml.push_str("</urlset>\n");
    Response::builder()
        .header(header::CONTENT_TYPE, "application/xml")
        .header(header::CACHE_CONTROL, "public, max-age=3600")
        .body(Body::from(xml))
        .unwrap_or_default()
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
        // An unversioned URL (the islands bundle, a v1 page that forgot its ?v=)
        // revalidates every time: a 304 costs one round trip, a stale WASM
        // after a deploy costs a broken page.
        .header(header::CACHE_CONTROL, if versioned { "public, max-age=31536000, immutable" } else { "public, no-cache" });
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
        .route("/atomicdb/nav/explore/{key}/", get(nav_explore))
        .route("/atomicdb/roots/", get(roots))
        .route("/atomicdb/queue/", get(queue))
        .route("/atomicdb/contributors/", get(contributors))
        .route("/atomicdb/map/", get(map))
        .route("/atomicdb/milestones/", get(milestones))
        .route("/atomicdb/campaigns/", get(campaigns))
        .route("/atomicdb/notifications/", get(notifications))
        .route("/atomicdb/sitemap.xml", get(sitemap))
        .route("/atomicdb/docs/", get(docs))
        .route("/atomicdb/docs/{slug}/", get(docs))
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
            ("/atomicdb/", "Now analyzing"),
        ] {
            let r = get(&app, uri, &[]).await;
            assert_eq!(r.status(), StatusCode::OK, "{uri}");
            assert!(text(r).await.contains(needle), "{uri}: {needle}");
        }
        let r = get(&app, "/atomicdb/workers/", &[]).await;
        assert_eq!(r.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn nav_json_matches_the_page() {
        let (app, site) = app();
        let nf3 = site.explorer(ROOT, &[]).unwrap().rows[0].key.clone();
        let r = get(&app, &format!("/atomicdb/nav/explore/{nf3}/?play=g1f3"), &[]).await;
        assert_eq!(r.status(), StatusCode::OK);
        assert_eq!(r.headers()[header::CONTENT_TYPE], "application/json");
        let v: serde_json::Value = serde_json::from_str(&text(r).await).unwrap();
        assert_eq!(v["url"], format!("/atomicdb/explore/{nf3}/?play=g1f3"));
        let main = v["main"].as_str().unwrap();
        assert!(main.contains("<div class=\"linebox\">") && main.contains("data-component=\"Board_"));
        let page = text(get(&app, &format!("/atomicdb/explore/{nf3}/?play=g1f3"), &[]).await).await;
        assert!(page.contains(main), "the JSON carries exactly the page's <main>");
        assert!(page.contains("/static/atomicdb/v2/explore-nav.js"));
    }

    #[tokio::test]
    async fn slow_pages_are_served_stale_and_rebuilt() {
        let (app, site) = app();
        let first = get(&app, "/atomicdb/roots/", &[]).await;
        let etag1 = first.headers()[header::ETAG].to_str().unwrap().to_string();
        site.bump_epoch();
        // The outdated copy answers at once; a rebuild runs in the background.
        let stale = get(&app, "/atomicdb/roots/", &[]).await;
        assert_eq!(stale.headers()[header::ETAG].to_str().unwrap(), etag1);
        for _ in 0..200 {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            let r = get(&app, "/atomicdb/roots/", &[]).await;
            if r.headers()[header::ETAG].to_str().unwrap() != etag1 {
                return;
            }
        }
        panic!("the background rebuild never landed");
    }
    #[tokio::test]
    async fn more_pages_render() {
        let (app, _) = app();
        for (uri, needle) in [
            ("/atomicdb/milestones/", "Solved: WHITE_WIN via MINIMAX"),
            ("/atomicdb/campaigns/", "positions to prove a White win"),
            ("/atomicdb/docs/", "AtomicDB documentation"),
            ("/atomicdb/docs/queue/", "docs-pager"),
            ("/atomicdb/sitemap.xml", "/atomicdb/docs/api/"),
            ("/atomicdb/roots/", "root-chart-svg"),
            ("/atomicdb/roots/", "Main line by day"),
            ("/atomicdb/queue/", "Community backlog"),
            ("/atomicdb/", "Proposed campaigns"),
        ] {
            let r = get(&app, uri, &[]).await;
            assert_eq!(r.status(), StatusCode::OK, "{uri}");
            assert!(text(r).await.contains(needle), "{uri}: {needle}");
        }
        let r = get(&app, "/atomicdb/docs/nope/", &[]).await;
        assert_eq!(r.status(), StatusCode::NOT_FOUND);
        let r = get(&app, "/atomicdb/notifications/", &[]).await;
        assert_eq!(r.status(), StatusCode::FOUND, "anonymous visitors sign in first");
    }

    struct OneUser;
    impl Sessions for OneUser {
        fn user(&self, key: &str) -> Option<User> {
            (key == "s1").then(|| User { id: 7, username: "belzedar".into(), is_staff: false })
        }
    }

    #[tokio::test]
    async fn signed_in_visitor_gets_bell_and_account_parts() {
        let site = Arc::new(FixtureSite::load().unwrap());
        let app = router(state(site, Arc::new(OneUser), FsPath::new("does-not-exist"), "https://example.org"));
        let cookie = [("cookie", "sessionid=s1")];
        let home = text(get(&app, "/atomicdb/", &cookie).await).await;
        assert!(home.contains("identity-user") && home.contains("belzedar"));
        assert!(home.contains("class=\"bell-badge\"") && home.contains("Your analysis of <b>1. Nf3</b> is ready"));
        let anon = text(get(&app, "/atomicdb/", &[]).await).await;
        assert!(anon.contains("Sign in") && !anon.contains("bell-badge"), "the anonymous copy is untouched");
        let queue = text(get(&app, "/atomicdb/queue/", &cookie).await).await;
        assert!(queue.contains(">Adopt 10<"), "adopt buttons for an account");
        let queue_anon = text(get(&app, "/atomicdb/queue/", &[]).await).await;
        assert!(!queue_anon.contains(">Adopt 10<") && queue_anon.contains("to adopt a batch"));
        let r = get(&app, "/atomicdb/notifications/", &cookie).await;
        assert_eq!(r.status(), StatusCode::OK);
        assert!(text(r).await.contains("notif-row unseen"));
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
