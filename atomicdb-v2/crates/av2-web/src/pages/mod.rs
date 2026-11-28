//! The pages: each renders its `<main>` with Leptos (escaping included) and
//! hands it to [`shell::document`].
//!
//! Rendering happens once per (URL, graph epoch) in the page cache
//! (`server.rs`); a page is a pure function of what `SiteRead` returns, so
//! two renders at the same epoch are byte-identical and share one `ETag`.

pub mod backlog;
pub mod contributors;
pub mod docs;
pub mod explore;
pub mod home;
pub mod map;
pub mod queue;
pub mod root_chart;
pub mod roots;
pub mod shell;

use leptos::prelude::*;
use shell::{Assets, Meta, Rendered};

/// A rendered page before the shell: its head data, `<main>` class and HTML,
/// and the v2 scripts it needs (served from `/static/atomicdb/v2/`).
pub struct Page {
    pub meta: Meta,
    pub main_class: &'static str,
    pub body: String,
    pub scripts: Vec<&'static str>,
}

/// Django `json_script`: JSON safe inside `<script>` (`<`, `>`, `&` escaped).
pub fn json_script(json: &str) -> String {
    json.replace('<', "\\u003C").replace('>', "\\u003E").replace('&', "\\u0026")
}

/// Wrap a page in the document.
pub fn finish(mut page: Page, assets: &Assets) -> Rendered {
    let v = assets.v();
    for s in &page.scripts {
        let path = if s.starts_with('/') { s.to_string() } else { format!("/static/atomicdb/v2/{s}") };
        page.meta.extra_scripts.push_str(&format!("<script src=\"{path}{v}\" defer></script>\n"));
    }
    shell::document(&page.meta, assets, page.main_class, &page.body)
}

/// Render a view to HTML inside a reactive owner (what Leptos's SSR expects).
pub fn html<V: IntoView>(f: impl FnOnce() -> V) -> String {
    let owner = Owner::new();
    owner.with(|| f().to_html())
}

/// v1 `missing.html`: the 404 page.
pub fn missing(note: Option<&str>) -> Page {
    let note = note.unwrap_or("Position not in the tree (yet).").to_string();
    Page {
        meta: Meta { title: "AtomicDB".into(), ..Default::default() },
        main_class: "",
        body: html(move || view! { <p class="dim">{note}" "<a href="/atomicdb/">"Back to the map"</a></p> }),
        scripts: vec![],
    }
}
