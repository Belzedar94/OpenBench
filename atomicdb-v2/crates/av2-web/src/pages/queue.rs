//! v1 `queue/` (queue.html): the queue by account.
//!
//! Not yet: the community backlog box (`{% backlog_box %}`), the anonymous
//! and per-account queue pages.

use super::{Page, html};
use crate::format::human;
use crate::pages::shell::Meta;
use crate::read::*;
use leptos::prelude::*;

/// Django `timesince(..., depth=1)` as v1 `_ago_text` uses it: one unit,
/// non-breaking space, `just now` under a minute.
pub fn ago_text(seconds: u64) -> String {
    const UNITS: [(u64, &str, &str); 6] = [
        (365 * 86_400, "year", "years"),
        (30 * 86_400, "month", "months"),
        (7 * 86_400, "week", "weeks"),
        (86_400, "day", "days"),
        (3_600, "hour", "hours"),
        (60, "minute", "minutes"),
    ];
    for (size, one, many) in UNITS {
        let n = seconds / size;
        if n > 0 {
            return format!("{n}\u{a0}{}", if n == 1 { one } else { many });
        }
    }
    "just now".into()
}

/// v1 `served_minutes` (`lanes.SERVED_WINDOW`).
const SERVED_MINUTES: u32 = 60;

pub fn render(q: QueueView) -> Page {
    let has_served = q.rows.iter().any(|r| r.served > 0);
    let body = html(move || {
        let served_title = format!(
            "Nodes the fleet actually spent in the last {SERVED_MINUTES} minutes, not what was asked for. An account with nothing waiting still appears while it has recent deliveries."
        );
        let rows = q
            .rows
            .iter()
            .map(|r| {
                let name = if r.user.is_empty() {
                    view! { <a class="qline" href="/atomicdb/queue/anonymous/"><strong>"Anonymous"</strong></a> }.into_any()
                } else {
                    view! { <a class="qline" href=format!("/atomicdb/queue/{}/", crate::format::query_value(&r.user))>{r.user.clone()}</a> }
                        .into_any()
                };
                let served = if has_served {
                    view! { {human(r.served)}" "<span class="dim">{format!("({:.1}%)", r.share)}</span> }.into_any()
                } else {
                    view! { <span class="dim">"nothing yet"</span> }.into_any()
                };
                view! {
                    <tr>
                        <td>{name}</td>
                        <td>{r.waiting}</td>
                        <td class="dim">{r.running}</td>
                        <td class="dim">{human(r.nodes)}</td>
                        <td>{served}</td>
                        <td class="dim">{r.oldest_seconds.map(ago_text).unwrap_or_default()}</td>
                    </tr>
                }
            })
            .collect_view();
        let table = if q.rows.is_empty() {
            view! { <p class="dim">"Nothing is queued right now."</p> }.into_any()
        } else {
            view! {
                <div class="contrib-scroll">
                    <table><tbody>
                        <tr>
                            <th>"Account"</th><th>"Waiting"</th><th>"Running"</th><th>"Queued nodes"</th>
                            <th title=served_title>{format!("Served ({SERVED_MINUTES} min)")}</th><th>"Oldest wait"</th>
                        </tr>
                        {rows}
                    </tbody></table>
                </div>
            }
            .into_any()
        };
        let writes = if q.ingest_pending > 0 {
            let s = if q.ingest_pending == 1 { "" } else { "es" };
            let oldest = q.ingest_oldest_seconds.map(|o| format!(", the oldest for {}", ago_text(o))).unwrap_or_default();
            view! { <p class="dim queue-writes"><strong>{q.ingest_pending}</strong>{format!(" finished search{s} waiting to be stored{oldest}: results appear on the site after this delay.")}</p> }.into_any()
        } else {
            view! { <p class="dim queue-writes">"Finished searches are stored as they arrive."</p> }.into_any()
        };
        view! {
            <h2 style="margin-top:0">"Queue "
                <a href="/atomicdb/docs/queue/" title="How the queue works" aria-label="How the queue works" style="font-size:.8em;text-decoration:none">"\u{24d8}"</a>" "
                <a href="/atomicdb/workers/" style="font-size:.8rem;font-weight:400" title="Every active worker, what it is searching and who asked for it">"workers"</a>
            </h2>
            <p class="queue-counts"><span title="Engine nodes in every search finished in the last hour, requested or automatic"><strong>{human(q.fleet_nodes_last_hour)}</strong>" nodes searched in the last hour"</span></p>
            {writes}
            {table}
        }
    });
    Page { meta: Meta { title: "AtomicDB queue".into(), ..Default::default() }, main_class: "", body, scripts: vec![] }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ago_like_django() {
        assert_eq!(ago_text(30), "just now");
        assert_eq!(ago_text(46 * 60), "46\u{a0}minutes");
        assert_eq!(ago_text(3 * 3600 + 59), "3\u{a0}hours");
        assert_eq!(ago_text(86_400 + 5), "1\u{a0}day");
    }
}
