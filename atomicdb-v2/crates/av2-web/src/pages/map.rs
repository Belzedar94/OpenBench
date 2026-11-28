//! v1 `map/` (map.html): the conquest map.  The page is a static shell; the
//! map itself is v1's `conquest-map.js` (d3 + Chessground) reading
//! `api/map/v1`, unchanged.

use super::Page;
use crate::pages::shell::{Meta, esc};

const BODY: &str = include_str!("map_body.html");

pub fn render(root_key: &str, root_fen: &str) -> Page {
    Page {
        meta: Meta {
            title: "Atomic move tree \u{b7} AtomicDB".into(),
            extra_head: "<link rel=\"stylesheet\" href=\"/static/atomicdb/conquest-map.css\">\n".into(),
            extra_scripts: "<script src=\"/static/atomicdb/vendor/d3/d3.v7.9.0.min.js\"></script>\n".into(),
            ..Default::default()
        },
        main_class: "move-tree-main",
        body: BODY.replace("__ROOT_KEY__", &esc(root_key)).replace("__ROOT_FEN__", &esc(root_fen)),
        scripts: vec!["/static/atomicdb/conquest-map.js"],
    }
}
