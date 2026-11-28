//! The community backlog box of the queue page (v1 `{% backlog_box %}`,
//! `_backlog_box.html` + `_backlog_row.html`).
//!
//! Signed in, each row offers "Adopt N" for its batch sizes and a
//! subscription form, posting to v1's `queue/adopt/` and `queue/subscribe/`
//! (Django keeps serving those).  The receipts after a post and the
//! account's current subscriptions are not ported yet.

use crate::pages::shell::User;
use crate::read::BacklogRow;
use leptos::prelude::*;

/// v1 `backlog.ADOPT_WAITING_MAX`.
pub const ADOPT_WAITING_MAX: u32 = 500;

fn row(b: &BacklogRow, signed_in: bool) -> impl IntoView + use<> {
    let held = (b.held > 0).then(|| {
        view! { <div class="dim" title="Kept on the list for a later round: under +33 a 128M search rarely closes them">{format!("{} held", b.held)}</div> }
    });
    let adopters = (b.adopters > 0).then(|| {
        view! { " "<span title="Accounts that adopted from this backlog">{format!("({} account{})", b.adopters, if b.adopters == 1 { "" } else { "s" })}</span> }
    });
    let adopt = signed_in.then(|| {
        let buttons = if b.open > 0 {
            b.sizes
                .iter()
                .map(|size| {
                    view! {
                        <form method="post" action="/atomicdb/queue/adopt/">
                            <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                            <input type="hidden" name="backlog" value=b.name.clone()/>
                            <input type="hidden" name="size" value=size.to_string()/>
                            <button type="submit" title=format!("Queue {size} of these searches as your own requests")>{format!("Adopt {size}")}</button>
                        </form>
                    }
                })
                .collect_view()
                .into_any()
        } else if b.adopted > 0 {
            view! { <span class="dim">"all adopted"</span> }.into_any()
        } else {
            view! { <span class="dim">"nothing to adopt"</span> }.into_any()
        };
        let default = b.sizes.get(b.sizes.len() / 2).copied().unwrap_or(0);
        let sub = (b.open > 0).then(|| {
            view! {
                <form method="post" action="/atomicdb/queue/subscribe/">
                    <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                    <input type="hidden" name="backlog" value=b.name.clone()/>
                    <select name="size" aria-label="Batch size">
                        {b.sizes.iter().map(|s| view! { <option value=s.to_string() selected=*s == default>{s.to_string()}</option> }).collect_view()}
                    </select>
                    <button type="submit" title="Adopt up to two batches for me whenever fewer than one batch of my adopted positions from this backlog are waiting">"Subscribe"</button>
                </form>
            }
        });
        view! { {buttons}<div class="backlog-sub">{sub}</div> }
    });
    view! {
        <tr>
            <td><strong>{b.title.clone()}</strong><div class="dim backlog-about">{b.about.clone()}</div></td>
            <td>{b.open}{held}</td>
            <td class="dim">{b.adopted}{adopters}</td>
            <td class="backlog-adopt">{adopt}</td>
        </tr>
    }
}

fn table(rows: &[&BacklogRow], signed_in: bool) -> impl IntoView + use<> {
    let rows = rows.iter().map(|b| row(b, signed_in)).collect_view();
    view! {
        <div class="contrib-scroll">
            <table class="backlog-table"><tbody>
                <tr><th>"Backlog"</th><th>"Left"</th><th>"Adopted"</th><th>{signed_in.then_some("Adopt")}</th></tr>
                {rows}
            </tbody></table>
        </div>
    }
}

pub fn view(backlogs: &[BacklogRow], user: Option<&User>) -> impl IntoView + use<> {
    let signed_in = user.is_some();
    let open: Vec<&BacklogRow> = backlogs.iter().filter(|b| !b.fully_adopted).collect();
    let adopted: Vec<&BacklogRow> = backlogs.iter().filter(|b| b.fully_adopted).collect();
    let your_queue = match user {
        Some(u) => view! { <a href=format!("/atomicdb/queue/{}/", crate::format::query_value(&u.username))>"your queue"</a> }.into_any(),
        None => "your queue".into_any(),
    };
    let n = adopted.len();
    let more = (n > 0).then(|| {
        view! {
            <details class="backlog-adopted">
                <summary>{format!("Show {n} fully adopted backlog{}", if n == 1 { "" } else { "s" })}</summary>
                {table(&adopted, signed_in)}
            </details>
        }
    });
    let note = if signed_in {
        view! {
            <p class="dim backlog-note">
                {format!("Each account has {ADOPT_WAITING_MAX} places for adopted positions waiting. A place is one search of 128M: one at 32M takes a quarter, one at 8M a sixteenth. Subscribe to a backlog and up to two batches are adopted for you whenever fewer than one batch from it are waiting; it stops by itself when the list runs out.")}
            </p>
        }
        .into_any()
    } else {
        view! { <p class="dim backlog-note"><a href="/login/?next=/atomicdb/queue/%23backlog">"Sign in"</a>" to adopt a batch."</p> }
            .into_any()
    };
    view! {
        <section class="backlog-box" id="backlog">
            <h3 class="contrib-sub">"Community backlog"</h3>
            <p class="backlog-intro">"Searches the tree needs that the server only runs when nobody is requesting anything. Adopt a batch and it becomes your own request: your workers serve it first, and it shows in "{your_queue}"."</p>
            {table(&open, signed_in)}
            {more}
            {note}
        </section>
    }
}
