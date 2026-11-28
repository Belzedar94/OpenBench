//! v1 `docs/` and `docs/<slug>/`: the site guide.  The bodies are v1's
//! templates copied as HTML (`docs/extract.py`), with the four constants they
//! print filled in here; the layout (side nav, pager) is v1's `_layout.html`.

use super::Page;
use crate::pages::shell::{Meta, esc};

/// v1 `views.DOCS_PAGES`.
pub const PAGES: [(&str, &str); 8] = [
    ("method", "Method"),
    ("rows", "Position rows"),
    ("backing", "Backed values"),
    ("repetition", "Repetition"),
    ("queue", "The queue"),
    ("contributing", "Contributing"),
    ("api", "API"),
    ("reading", "The position page"),
];

/// Constants the docs print (v1 `ingest.SUBTREE_TASK_CAP`,
/// `views.MULTI_REQUEST_CAP`, `views.CHILDREN_REQUEST_CAP`,
/// `views.REQUEST_QUEUE_MAX`).
pub const SUBTREE_CAP: u32 = 78;
pub const MULTI_REQUEST_CAP: u32 = 64;
pub const CHILDREN_CAP: u32 = 64;
pub const QUEUE_CAP: &str = "10000";

fn body(slug: &str) -> Option<&'static str> {
    Some(match slug {
        "" => include_str!("docs/overview.html"),
        "method" => include_str!("docs/method.html"),
        "rows" => include_str!("docs/rows.html"),
        "backing" => include_str!("docs/backing.html"),
        "repetition" => include_str!("docs/repetition.html"),
        "queue" => include_str!("docs/queue.html"),
        "contributing" => include_str!("docs/contributing.html"),
        "api" => include_str!("docs/api.html"),
        "reading" => include_str!("docs/reading.html"),
        _ => return None,
    })
}

/// The docs page `slug` (`""` for the overview), or `None`.
pub fn render(slug: &str, static_v: &str) -> Option<Page> {
    let raw = body(slug)?;
    let (first, rest) = raw.split_once('\n').unwrap_or(("", raw));
    let title = first.trim().trim_start_matches("<!-- title:").trim_end_matches("-->").trim().to_string();
    let doc = rest
        .replace("__SUBTREE_CAP__", &SUBTREE_CAP.to_string())
        .replace("__MULTI_REQUEST_CAP__", &MULTI_REQUEST_CAP.to_string())
        .replace("__CHILDREN_CAP__", &CHILDREN_CAP.to_string())
        .replace("__QUEUE_CAP__", QUEUE_CAP)
        .replace("__V__", static_v);
    let current = PAGES.iter().position(|(s, _)| *s == slug);
    let mut nav = String::from("<div class=\"docs-layout\">\n  <nav class=\"docs-nav\" aria-label=\"Documentation\">\n");
    nav.push_str(&format!(
        "    <a href=\"/atomicdb/docs/\"{}>Overview</a>\n",
        if current.is_none() { " aria-current=\"page\"" } else { "" }
    ));
    for (i, (s, t)) in PAGES.iter().enumerate() {
        let cur = if current == Some(i) { " aria-current=\"page\"" } else { "" };
        nav.push_str(&format!("    <a href=\"/atomicdb/docs/{s}/\"{cur}>{}</a>\n", esc(t)));
    }
    nav.push_str("  </nav>\n  <article class=\"docs-body\">\n");
    nav.push_str(&doc);
    if let Some(i) = current {
        nav.push_str("\n    <p class=\"docs-pager\">\n      ");
        match i.checked_sub(1).map(|p| PAGES[p]) {
            Some((s, t)) => nav.push_str(&format!("<a href=\"/atomicdb/docs/{s}/\">&larr; {}</a>", esc(t))),
            None => nav.push_str("<a href=\"/atomicdb/docs/\">&larr; Overview</a>"),
        }
        if let Some((s, t)) = PAGES.get(i + 1) {
            nav.push_str(&format!("\n      <a href=\"/atomicdb/docs/{s}/\">{} &rarr;</a>", esc(t)));
        }
        nav.push_str("\n    </p>");
    }
    nav.push_str("\n  </article>\n</div>");
    let mut meta = Meta { title, ..Default::default() };
    if slug.is_empty() {
        // v1: an old link such as /atomicdb/docs/#queue goes on to the page
        // that section became.
        let slugs = PAGES.iter().map(|(s, _)| format!("\"{s}\"")).collect::<Vec<_>>().join(", ");
        meta.extra_head = format!(
            "<script>\n(function () {{\n  var pages = [{slugs}];\n  var hash = window.location.hash.slice(1);\n  if (hash && pages.indexOf(hash) !== -1) {{\n    window.location.replace(\"/atomicdb/docs/\" + hash + \"/\");\n  }}\n}}());\n</script>\n"
        );
    }
    Some(Page { meta, main_class: "", body: nav, scripts: vec![] })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_page_renders_without_placeholders() {
        for slug in std::iter::once("").chain(PAGES.iter().map(|(s, _)| *s)) {
            let p = render(slug, "?v=x").unwrap();
            assert!(!p.body.contains("__"), "{slug}: placeholder left");
            assert!(!p.meta.title.is_empty());
        }
        assert!(render("nope", "").is_none());
        assert!(render("queue", "?v=x").unwrap().body.contains("queue-page.webp?v=x"));
    }
}
