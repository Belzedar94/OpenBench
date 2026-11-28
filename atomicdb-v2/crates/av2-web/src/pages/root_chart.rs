//! The start position's best record over time, on the roots page (v1
//! `root_chart.py` + `_root_chart.html`): a step line of the records on a
//! value axis snapped to a readable step, and behind it, on its own scale,
//! the cumulative engine nodes downsampled to at most 180 points.  Same
//! geometry, same SVG, same labels as v1.

use super::roots::value_h;
use crate::format::human;
use crate::read::{RootChartData, Status};
use crate::time::jm;
use leptos::prelude::*;

const WIDTH: f64 = 600.0;
const HEIGHT: f64 = 160.0;
const INSET: f64 = 8.0;
const MAX_POINTS: f64 = 180.0;
const TICK_STEPS: [i64; 12] = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000];
const MAX_TICK_STEPS: i64 = 4;

/// v1 `_fmt`: one decimal, trailing zeros and point dropped.
pub fn fmt(n: f64) -> String {
    let s = format!("{n:.1}");
    let s = s.trim_end_matches('0').trim_end_matches('.');
    if s == "-0" { "0".into() } else { s.to_string() }
}

/// v1 `_axis`: `(lo, hi, step)`, the values' range snapped outward.
pub fn axis(values: &[i64]) -> (i64, i64, i64) {
    let (v_lo, v_hi) = (*values.iter().min().unwrap_or(&0), *values.iter().max().unwrap_or(&0));
    let (mut lo, mut hi, mut step) = (0, 0, 1);
    for s in TICK_STEPS {
        step = s;
        lo = v_lo.div_euclid(s) * s;
        hi = -((-v_hi).div_euclid(s)) * s;
        if (hi - lo) / s <= MAX_TICK_STEPS {
            break;
        }
    }
    if hi == lo {
        lo -= step;
        hi += step;
    }
    (lo, hi, step)
}

struct Tick {
    label: String,
    top: String,
}

struct Column {
    x: String,
    w: String,
    title: String,
}

/// The chart, measured (v1 `measure`); `None` with no record.
pub struct Chart {
    line: String,
    node_area: String,
    nodes_last_h: String,
    node_ticks: Vec<Tick>,
    columns: Vec<Column>,
    ticks: Vec<Tick>,
    gridlines: Vec<String>,
    first_at: i64,
    last_at: i64,
    record_h: String,
    record_at: i64,
    records: usize,
}

pub fn measure(d: &RootChartData) -> Option<Chart> {
    let records = &d.records;
    let first = records.first()?.0;
    let end = d.end.max(records.last()?.0);
    let span = (end - first) as f64;
    let scaled: Vec<i64> = records.iter().filter(|r| r.2 == Status::Unknown).map(|r| i64::from(r.1)).collect();
    let all: Vec<i64> = records.iter().map(|r| i64::from(r.1)).collect();
    let (lo, hi, step) = axis(if scaled.is_empty() { &all } else { &scaled });
    let usable = HEIGHT - 2.0 * INSET;
    let x_of = |at: i64| if span > 0.0 { (at - first) as f64 / span * WIDTH } else { 0.0 };
    let y_of = |v: i64| (INSET + (hi - v) as f64 / (hi - lo) as f64 * usable).clamp(INSET, HEIGHT - INSET);
    let xs: Vec<f64> = records.iter().map(|r| x_of(r.0)).collect();
    let mut line = String::new();
    for (x, r) in xs.iter().zip(records) {
        if line.is_empty() {
            line = format!("M{} {}", fmt(*x), fmt(y_of(i64::from(r.1))));
        } else {
            line.push_str(&format!(" H{} V{}", fmt(*x), fmt(y_of(i64::from(r.1)))));
        }
    }
    line.push_str(&format!(" H{WIDTH}"));
    let columns = records
        .iter()
        .enumerate()
        .map(|(i, r)| {
            let right = xs.get(i + 1).copied().unwrap_or(WIDTH);
            Column {
                x: fmt(xs[i]),
                w: fmt((right - xs[i]).max(0.1)),
                title: format!("{} UTC \u{b7} record {}", crate::time::ymd_hm(r.0), value_h(Some(r.1), Some(r.2))),
            }
        })
        .collect();
    // Engine nodes: the last row of each slot of max(1 h, span / 180).
    let slot = (3600.0f64).max((span + 1.0) / MAX_POINTS);
    let mut nodes: Vec<(i64, u64)> = Vec::new();
    let mut last_slot = None;
    for &(at, n) in d.nodes.iter().filter(|(at, _)| *at >= first && *at <= end) {
        let s = ((at - first) as f64 / slot).floor() as i64;
        if last_slot == Some(s) {
            if let Some(l) = nodes.last_mut() {
                *l = (at, n);
            }
        } else {
            nodes.push((at, n));
        }
        last_slot = Some(s);
    }
    let (mut node_area, mut nodes_last_h, mut node_ticks) = (String::new(), String::new(), Vec::new());
    if nodes.len() >= 2 {
        let n_lo = nodes[0].1 as f64;
        let n_hi = nodes.iter().map(|r| r.1).max().unwrap_or(0) as f64;
        let n_span = if n_hi - n_lo == 0.0 { 1.0 } else { n_hi - n_lo };
        let coords: Vec<(f64, f64)> =
            nodes.iter().map(|&(at, n)| (x_of(at), HEIGHT - INSET - (n as f64 - n_lo) / n_span * usable)).collect();
        node_area = format!(
            "M{} L{} {HEIGHT} L{} {HEIGHT} Z",
            coords.iter().map(|(x, y)| format!("{} {}", fmt(*x), fmt(*y))).collect::<Vec<_>>().join(" L"),
            fmt(coords[coords.len() - 1].0),
            fmt(coords[0].0)
        );
        nodes_last_h = human(nodes[nodes.len() - 1].1);
        for n in [n_hi, (n_hi + n_lo) / 2.0, n_lo] {
            let y = HEIGHT - INSET - (n - n_lo) / n_span * usable;
            node_ticks.push(Tick { label: human(n as u64), top: fmt(y / HEIGHT * 100.0) });
        }
    }
    let tick_values: Vec<i64> = (0..).map(|i| hi - i * step).take_while(|v| *v >= lo).collect();
    let last = records.last()?;
    Some(Chart {
        line,
        node_area,
        nodes_last_h,
        node_ticks,
        columns,
        ticks: tick_values.iter().map(|&v| Tick { label: value_h(Some(v as i32), None), top: fmt(y_of(v) / HEIGHT * 100.0) }).collect(),
        gridlines: tick_values.iter().map(|&v| fmt(y_of(v))).collect(),
        first_at: first,
        last_at: end,
        record_h: value_h(Some(last.1), Some(last.2)),
        record_at: last.0,
        records: records.len(),
    })
}

pub fn view(c: Chart) -> impl IntoView {
    let label = format!(
        "Start position best record from {} to {}: {} record{}, now {}",
        jm(c.first_at, true, false),
        jm(c.last_at, true, false),
        c.records,
        if c.records == 1 { "" } else { "s" },
        c.record_h
    );
    let has_nodes = !c.node_ticks.is_empty();
    let plot_class = if has_nodes { "root-chart-plot has-node-axis" } else { "root-chart-plot" };
    let ticks = c.ticks.into_iter().map(|t| view! { <span style=format!("top:{}%", t.top)>{t.label}</span> }).collect_view();
    let node_ticks = has_nodes.then(|| {
        let spans = c.node_ticks.into_iter().map(|t| view! { <span style=format!("top:{}%", t.top)>{t.label}</span> }).collect_view();
        view! { <div class="root-chart-ticks root-chart-ticks-nodes dim" aria-hidden="true">{spans}</div> }
    });
    let grid = c
        .gridlines
        .into_iter()
        .map(|y| view! { <line class="rc-grid" x1="0" x2="600" y1=y.clone() y2=y vector-effect="non-scaling-stroke"></line> })
        .collect_view();
    let area = (!c.node_area.is_empty()).then(|| view! { <path class="rc-nodes" d=c.node_area.clone()></path> });
    let cols = c
        .columns
        .into_iter()
        .map(|col| view! { <rect class="rc-hit" x=col.x y="0" width=col.w height="160"><title>{col.title}</title></rect> })
        .collect_view();
    let legend = (!c.node_area.is_empty()).then(|| {
        view! { <span class="root-chart-legend"><span class="rc-key"><i class="rc-swatch-nodes"></i>{format!("engine nodes, {} now", c.nodes_last_h)}</span></span>" " }
    });
    view! {
        <section class="root-chart" aria-labelledby="root-chart-title">
            <div class="root-chart-head">
                <h2 id="root-chart-title">"Start position best record "<span class="root-chart-now">{c.record_h.clone()}</span></h2>
                <div class="root-chart-sub dim">{format!("White's side, in pawns \u{b7} set {} UTC", jm(c.record_at, true, true))}</div>
            </div>
            <div class=plot_class>
                <div class="root-chart-ticks dim" aria-hidden="true">{ticks}</div>
                {node_ticks}
                <svg class="root-chart-svg" viewBox="0 0 600 160" preserveAspectRatio="none" role="img" aria-label=label>
                    {grid}
                    {area}
                    <path class="rc-best" d=c.line vector-effect="non-scaling-stroke"></path>
                    {cols}
                </svg>
            </div>
            <div class="root-chart-foot dim">
                <span>{jm(c.first_at, false, false)}</span>" "
                {legend}
                <span>{format!("{} UTC", jm(c.last_at, false, true))}</span>
            </div>
        </section>
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn axis_like_v1() {
        // +12.50 to +13.00 -> step 25, three ticks (the production chart).
        assert_eq!(axis(&[1250, 1300, 1257]), (1250, 1300, 25));
        assert_eq!(axis(&[100]), (99, 101, 1));
        assert_eq!(fmt(152.0), "152");
        assert_eq!(fmt(3.25), "3.2");
        assert_eq!(fmt(80.04), "80");
    }
}
