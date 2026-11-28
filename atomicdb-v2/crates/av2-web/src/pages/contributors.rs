//! v1 `contributors/` (contributors.html + _contrib_rows.html): three
//! leaderboards (today UTC, last 24 hours, all time).

use super::{Page, html};
use crate::format::{human, thousands};
use crate::pages::shell::Meta;
use crate::read::*;
use leptos::prelude::*;

/// v1 shows ten rows and folds the rest behind "Show N more".
const SHOWN: usize = 10;

fn rows_table(rows: &[ContributorRow], offset: usize, fleet: u64) -> impl IntoView + use<> {
    let rows: Vec<_> = rows
        .iter()
        .enumerate()
        .map(|(i, r)| {
            let rank = offset + i + 1;
            let medal = match rank {
                1 => "lb-medal first",
                2 => "lb-medal second",
                3 => "lb-medal third",
                _ => "lb-medal",
            };
            let share = if fleet > 0 { (1000.0 * r.nodes as f64 / fleet as f64).round() / 10.0 } else { 0.0 };
            view! {
                <tr>
                    <td class="lb-place"><span class=medal>{rank}</span></td>
                    <td>
                        <a class="lb-name" href=format!("/atomicdb/user/{}/", crate::format::query_value(&r.user))>{r.user.clone()}</a>
                        <div class="lb-bar" aria-hidden="true"><span style=format!("width:{share:.1}%")></span></div>
                        <div class="dim contrib-spec">{format!("{share:.1}% of the fleet")}</div>
                    </td>
                    <td class="n">{human(r.nodes)}</td>
                    <td class="n">{thousands(r.tasks)}</td>
                </tr>
            }
        })
        .collect();
    view! {
        <div class="contrib-scroll">
            <table class="lb-table"><tbody>
                <tr>
                    <th class="lb-place"><span class="visually-hidden">"Place"</span><span aria-hidden="true">"#"</span></th>
                    <th>"Contributor"</th><th class="n">"Nodes"</th><th class="n">"Analyses"</th>
                </tr>
                {rows}
            </tbody></table>
        </div>
    }
}

pub fn render(lb: Leaderboard) -> Page {
    let boards = [("Today (UTC)", lb.today), ("Last 24 hours", lb.last_24h), ("All time", lb.all_time)];
    let any = boards.iter().any(|(_, r)| !r.is_empty());
    let body = html(move || {
        let sections = boards
            .into_iter()
            .map(|(window, rows)| {
                let fleet: u64 = rows.iter().map(|r| r.nodes).sum();
                let n = rows.len();
                let content = if rows.is_empty() {
                    view! { <p class="dim">"Nothing in this window yet."</p> }.into_any()
                } else {
                    let first = rows_table(&rows[..n.min(SHOWN)], 0, fleet);
                    let more = (n > SHOWN).then(|| {
                        let rest = rows_table(&rows[SHOWN..], SHOWN, fleet);
                        view! { <details class="lb-more"><summary>{format!("Show {} more", n - SHOWN)}</summary>{rest}</details> }
                    });
                    let foot = format!("{n} account{} \u{b7} {} nodes.", if n == 1 { "" } else { "s" }, human(fleet));
                    view! { {first}{more}<p class="dim lb-foot">{foot}</p> }.into_any()
                };
                view! { <section><h3 class="contrib-sub">{window}</h3>{content}</section> }
            })
            .collect_view();
        view! {
            <h2 id="contributors" style="margin-top:0">"Contributors "
                <a href="/atomicdb/workers/" style="font-size:.8rem;font-weight:400" title="Every active worker, what it is searching and who asked for it">"active workers"</a>
            </h2>
            {if any {
                view! { <div class="lb-grid">{sections}</div> }.into_any()
            } else {
                view! { <p class="dim">"No worker has searched anything yet. Run the AtomicDB client and the machines under your account show up here, with what they searched."</p> }.into_any()
            }}
        }
    });
    Page { meta: Meta { title: "Contributors \u{b7} AtomicDB".into(), ..Default::default() }, main_class: "", body, scripts: vec![] }
}
