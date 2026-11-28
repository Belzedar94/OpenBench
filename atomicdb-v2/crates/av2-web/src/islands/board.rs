//! The explorer board (v1 `_board.html` + `board.js`).
//!
//! Server side it renders the board's empty `div` with v1's data attributes;
//! in the browser it mounts Chessground on it (through `board_glue.js`) with
//! the legal moves the server sent, and a move navigates to
//! `/atomicdb/goto/<key>/<uci>/?play=<route>` exactly like v1: the server
//! applies it and redirects, so the board needs no Atomic rules of its own.
//! Promotion is queen by default, underpromotions from the move table (v1).

use leptos::prelude::*;

/// `from -> [to]`, in the order the moves come (v1 board.js `dests`).
pub fn dests(legal: &[String]) -> Vec<(String, Vec<String>)> {
    let mut out: Vec<(String, Vec<String>)> = Vec::new();
    for uci in legal {
        if uci.len() < 4 {
            continue;
        }
        let (from, to) = (&uci[0..2], &uci[2..4]);
        match out.iter_mut().find(|(f, _)| f == from) {
            Some((_, tos)) => {
                if !tos.iter().any(|t| t == to) {
                    tos.push(to.to_string());
                }
            }
            None => out.push((from.to_string(), vec![to.to_string()])),
        }
    }
    out
}

/// v1 board.js `resolvedUci`: the plain move if legal, else the first legal
/// promotion in `q, r, b, n` order.
pub fn resolve(legal: &[String], from: &str, to: &str) -> Option<String> {
    let plain = format!("{from}{to}");
    if legal.contains(&plain) {
        return Some(plain);
    }
    ["q", "r", "b", "n"].iter().map(|s| format!("{plain}{s}")).find(|c| legal.iter().any(|m| m == c))
}

/// Where a move goes (v1 board.js `after`).
pub fn goto_url(key: &str, uci: &str, play: &str) -> String {
    if play.is_empty() {
        format!("/atomicdb/goto/{key}/{uci}/")
    } else {
        format!("/atomicdb/goto/{key}/{uci}/?play={}", crate::format::query_value(play))
    }
}

#[island]
pub fn Board(key: String, fen: String, turn: String, best_move: String, play: String, legal: Vec<String>) -> impl IntoView {
    let board_ref = NodeRef::<leptos::html::Div>::new();
    #[cfg(not(feature = "hydrate"))]
    let _ = &legal;
    #[cfg(feature = "hydrate")]
    {
        let (key, fen, turn, best_move, play, legal) =
            (key.clone(), fen.clone(), turn.clone(), best_move.clone(), play.clone(), legal.clone());
        Effect::new(move |_| {
            if let Some(el) = board_ref.get() {
                client::mount(el.into(), key.clone(), fen.clone(), turn.clone(), best_move.clone(), play.clone(), legal.clone());
            }
        });
    }
    view! {
        <div class="boardwrap">
            <div
                id="board"
                class="atomic-board cg-wrap brown cburnett"
                aria-label="Atomic chess board"
                data-key=key
                data-fen=fen
                data-turn=turn
                data-best-move=best_move
                data-play=play
                node_ref=board_ref
            ></div>
        </div>
    }
}

#[cfg(feature = "hydrate")]
mod client {
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;
    use wasm_bindgen::JsCast;
    use wasm_bindgen::prelude::*;

    #[wasm_bindgen(module = "/src/islands/board_glue.js")]
    extern "C" {
        #[wasm_bindgen(js_name = mountBoard)]
        fn mount_board(
            el: &web_sys::Element,
            fen: &str,
            orientation: &str,
            turn: &str,
            dests: &str,
            colour: &str,
            best_move: &str,
            on_move: &Closure<dyn FnMut(String, String)>,
        ) -> JsValue;
        #[wasm_bindgen(js_name = resetBoard)]
        fn reset_board(ground: &JsValue, fen: &str, turn: &str, dests: &str);
        #[wasm_bindgen(js_name = restoreFen)]
        fn restore_fen(ground: &JsValue, fen: &str);
        #[wasm_bindgen(js_name = toggleOrientation)]
        fn toggle_orientation(ground: &JsValue) -> String;
        #[wasm_bindgen(js_name = setArrow)]
        fn set_arrow(ground: &JsValue, colour: &str, best_move: &str);
    }

    const ARROW_DEFAULT: &str = "#15781b";
    const ORIENTATION_KEY: &str = "atomicdb-orientation";

    fn is_colour(s: &str) -> bool {
        s.len() == 7 && s.starts_with('#') && s[1..].bytes().all(|b| b.is_ascii_hexdigit())
    }

    fn arrow_colour(w: &web_sys::Window) -> String {
        w.local_storage()
            .ok()
            .flatten()
            .and_then(|s| s.get_item("atomicdb-arrow").ok().flatten())
            .filter(|c| is_colour(c))
            .unwrap_or_else(|| ARROW_DEFAULT.to_string())
    }

    fn orientation(w: &web_sys::Window) -> &'static str {
        match w.session_storage().ok().flatten().and_then(|s| s.get_item(ORIENTATION_KEY).ok().flatten()) {
            Some(o) if o == "black" => "black",
            _ => "white",
        }
    }

    fn dests_json(legal: &[String]) -> String {
        let d = super::dests(legal);
        serde_json::to_string(&d).unwrap_or_else(|_| "[]".into())
    }

    pub fn mount(el: web_sys::Element, key: String, fen: String, turn: String, best_move: String, play: String, legal: Vec<String>) {
        let Some(w) = web_sys::window() else { return };
        let dests = dests_json(&legal);
        let ground: Rc<RefCell<JsValue>> = Rc::new(RefCell::new(JsValue::NULL));
        let navigating = Rc::new(Cell::new(false));
        let on_move = {
            let (ground, navigating, fen, w) = (ground.clone(), navigating.clone(), fen.clone(), w.clone());
            Closure::<dyn FnMut(String, String)>::new(move |from: String, to: String| {
                let Some(uci) = super::resolve(&legal, &from, &to) else { return };
                if navigating.get() {
                    return;
                }
                navigating.set(true);
                // Chessground knows no Atomic rules: put the server's position
                // back before leaving so a capture never shows as orthodox.
                restore_fen(&ground.borrow(), &fen);
                let _ = w.location().assign(&super::goto_url(&key, &uci, &play));
            })
        };
        let g = mount_board(&el, &fen, orientation(&w), &turn, &dests, &arrow_colour(&w), &best_move, &on_move);
        *ground.borrow_mut() = g;
        on_move.forget();

        let flip = {
            let (ground, w) = (ground.clone(), w.clone());
            Closure::<dyn FnMut()>::new(move || {
                let o = toggle_orientation(&ground.borrow());
                if let Ok(Some(s)) = w.session_storage() {
                    let _ = s.set_item(ORIENTATION_KEY, &o);
                }
            })
        };
        let _ = w.add_event_listener_with_callback("atomicdb-flip", flip.as_ref().unchecked_ref());
        flip.forget();

        let arrow = {
            let (ground, best_move) = (ground.clone(), best_move.clone());
            Closure::<dyn FnMut(web_sys::CustomEvent)>::new(move |e: web_sys::CustomEvent| {
                let c = e.detail().as_string().filter(|c| is_colour(c)).unwrap_or_else(|| ARROW_DEFAULT.to_string());
                set_arrow(&ground.borrow(), &c, &best_move);
            })
        };
        let _ = w.add_event_listener_with_callback("atomicdb-arrow", arrow.as_ref().unchecked_ref());
        arrow.forget();

        // Back from the position a move led to, the browser may restore this
        // page from its back/forward cache mid-navigation: put the board back
        // as the server drew it (v1, seaside_cliff 3-Nov-2026).
        let pageshow = {
            let ground = ground.clone();
            Closure::<dyn FnMut(web_sys::PageTransitionEvent)>::new(move |e: web_sys::PageTransitionEvent| {
                if !e.persisted() {
                    return;
                }
                navigating.set(false);
                reset_board(&ground.borrow(), &fen, &turn, &dests);
            })
        };
        let _ = w.add_event_listener_with_callback("pageshow", pageshow.as_ref().unchecked_ref());
        pageshow.forget();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn dests_group_by_origin() {
        let d = dests(&v(&["e2e3", "e2e4", "g1f3", "a7a8q", "a7a8n"]));
        assert_eq!(d[0], ("e2".to_string(), v(&["e3", "e4"])));
        assert_eq!(d[2], ("a7".to_string(), v(&["a8"])));
    }

    #[test]
    fn promotion_defaults_to_queen() {
        let legal = v(&["a7a8n", "a7a8q", "e2e4"]);
        assert_eq!(resolve(&legal, "a7", "a8").as_deref(), Some("a7a8q"));
        assert_eq!(resolve(&legal, "e2", "e4").as_deref(), Some("e2e4"));
        assert_eq!(resolve(&legal, "e2", "e5"), None);
    }

    #[test]
    fn goto_keeps_route() {
        assert_eq!(goto_url("k", "g1f3", ""), "/atomicdb/goto/k/g1f3/");
        assert_eq!(goto_url("k", "f7f6", "g1f3"), "/atomicdb/goto/k/f7f6/?play=g1f3");
        assert_eq!(goto_url("k", "f7f6", "g1f3,e2e3"), "/atomicdb/goto/k/f7f6/?play=g1f3%2Ce2e3");
    }
}
