//! AtomicDB v2 website.
//!
//! Server-side rendered pages (Leptos, `ssr` feature) inside the same binary
//! as the graph, with WASM islands (`hydrate` feature) only for the
//! interactive parts: the explorer board and the request row with its live
//! queue line.  Everything else is plain HTML, cached per graph epoch and
//! served precompressed.  See `docs/web.md` for the inventory of v1 pages and
//! the design.
//!
//! Modules:
//! - [`format`]: v1's number formatting, byte for byte (both builds).
//! - [`islands`]: the islands (both builds; the browser build has only these).
//! - `read`: the `SiteRead` trait the store implements, and the page records.
//! - `fixture`: `SiteRead` from sample data, so the pages render today.
//! - `pages`: the pages, rendered to strings.
//! - `server`: axum routes, page cache, ETags, static files.
//! - `auth`: the Django session bridge (signed-in visitors stay signed in).

#![recursion_limit = "256"]

pub mod format;
pub mod islands;

#[cfg(feature = "ssr")]
pub mod auth;
#[cfg(feature = "ssr")]
pub mod fixture;
#[cfg(feature = "ssr")]
pub mod pages;
#[cfg(feature = "ssr")]
pub mod read;
#[cfg(feature = "ssr")]
pub mod server;
#[cfg(feature = "ssr")]
pub mod time;

/// Entry point of the WASM bundle: hydrate the islands of the page.
#[cfg(feature = "hydrate")]
#[wasm_bindgen::prelude::wasm_bindgen]
pub fn hydrate() {
    console_error_panic_hook::set_once();
    leptos::mount::hydrate_islands();
}
