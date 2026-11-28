import csv, sys, collections, statistics, json

sample_path, landings_path = sys.argv[1], sys.argv[2]

def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(p / 100.0 * len(sorted_vals)))
    return sorted_vals[i]

def hist(vals, edges):
    out = collections.OrderedDict()
    for lo, hi in zip(edges, edges[1:]):
        out[f"{lo}..{hi-1}"] = sum(1 for v in vals if lo <= v < hi)
    out[f">={edges[-1]}"] = sum(1 for v in vals if v >= edges[-1])
    return out

rows = list(csv.DictReader(open(sample_path, newline='')))
n = len(rows)
print("=== position sample: n =", n)
def frac(pred):
    return sum(1 for r in rows if pred(r)) / n

print("status:", {k: round(v / n, 4) for k, v in collections.Counter(r['status'] for r in rows).items()})
print("closure:", {k: round(v / n, 4) for k, v in collections.Counter(r['closure'] for r in rows).items()})
print("proof:", {k: round(v / n, 4) for k, v in collections.Counter(r['proof'] for r in rows).items()})
print("expanded:", round(frac(lambda r: r['expanded'] == 't'), 4), " reachable:", round(frac(lambda r: r['reachable'] == 't'), 4))
print("searched (nodes_invested>0):", round(frac(lambda r: int(r['nodes_invested']) > 0), 4))
print("has eval_cp:", round(frac(lambda r: r['eval_cp'] != ''), 4))
print("has backed_eval:", round(frac(lambda r: r['backed_eval'] != ''), 4))
print("  backed & open:", round(frac(lambda r: r['backed_eval'] != '' and r['status'] == 'UNKNOWN'), 4))
print("  backed & open & backed_nodes>0:", round(frac(lambda r: r['backed_eval'] != '' and r['status'] == 'UNKNOWN' and int(r['backed_nodes']) > 0), 4))
print("has best_move:", round(frac(lambda r: r['best_move'] != ''), 4))
print("has won_line:", round(frac(lambda r: r['wl_len'] != ''), 4))
print("has last_analysis:", round(frac(lambda r: r['la_bytes'] != ''), 4))
print("stm white:", round(frac(lambda r: r['stm'] == 'w'), 4))
print("campaign set:", round(frac(lambda r: r['campaign_id'] != ''), 4))
print("priority != 0:", round(frac(lambda r: float(r['priority']) != 0.0), 4))

outdeg = [int(r['outdeg']) for r in rows]
indeg = [int(r['indeg']) for r in rows]
print("\nout-degree: mean %.3f  sum %d" % (statistics.mean(outdeg), sum(outdeg)))
print("  frac outdeg==0: %.4f  ==1: %.4f  2..9: %.4f  >=10: %.4f" % (
    sum(1 for d in outdeg if d == 0) / n, sum(1 for d in outdeg if d == 1) / n,
    sum(1 for d in outdeg if 2 <= d <= 9) / n, sum(1 for d in outdeg if d >= 10) / n))
exp_out = sorted(int(r['outdeg']) for r in rows if r['expanded'] == 't')
print("  expanded nodes: n=%d mean=%.2f p5=%s p25=%s p50=%s p75=%s p95=%s p99=%s max=%s" % (
    len(exp_out), statistics.mean(exp_out) if exp_out else 0, pct(exp_out, 5), pct(exp_out, 25), pct(exp_out, 50), pct(exp_out, 75), pct(exp_out, 95), pct(exp_out, 99), exp_out[-1] if exp_out else None))
nonexp_out = [int(r['outdeg']) for r in rows if r['expanded'] != 't']
print("  non-expanded nodes with edges: frac %.4f ; their outdeg hist:" % (sum(1 for d in nonexp_out if d > 0) / n), hist([d for d in nonexp_out if d > 0], [1, 2, 3, 5, 10, 20]))
print("  outdeg hist (expanded):", hist(exp_out, [0, 1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 80]))
print("\nin-degree: mean %.3f" % statistics.mean(indeg))
print("  hist:", hist(indeg, [0, 1, 2, 3, 4, 5, 8, 16, 32]))
si = sorted(indeg)
print("  p50=%s p90=%s p99=%s p99.9=%s max=%s" % (pct(si, 50), pct(si, 90), pct(si, 99), pct(si, 99.9), si[-1]))
# in-degree by class
for name, pred in [("expanded", lambda r: r['expanded'] == 't'), ("closed", lambda r: r['status'] != 'UNKNOWN'), ("open leaf", lambda r: r['status'] == 'UNKNOWN' and r['expanded'] != 't')]:
    v = [int(r['indeg']) for r in rows if pred(r)]
    if v:
        print("  indeg mean for %-10s n=%6d mean=%.3f frac>1=%.4f" % (name, len(v), statistics.mean(v), sum(1 for d in v if d > 1) / len(v)))

# closed breakdown
closed = [r for r in rows if r['status'] != 'UNKNOWN']
print("\nclosed: n=%d (%.4f)" % (len(closed), len(closed) / n))
print("  closed & searched: %.4f of closed" % (sum(1 for r in closed if int(r['nodes_invested']) > 0) / max(1, len(closed))))
print("  closed & expanded: %.4f of closed" % (sum(1 for r in closed if r['expanded'] == 't') / max(1, len(closed))))
print("  closed outdeg hist:", hist([int(r['outdeg']) for r in closed], [0, 1, 2, 5, 10, 20, 40]))
mi = sorted(int(r['mate_in']) for r in closed if r['mate_in'] != '')
print("  mate_in p50=%s p90=%s p99=%s max=%s" % (pct(mi, 50), pct(mi, 90), pct(mi, 99), mi[-1] if mi else None))
wl = sorted(int(r['wl_len']) for r in rows if r['wl_len'] != '')
print("  won_line chars: mean=%.1f p50=%s p90=%s p99=%s max=%s" % (statistics.mean(wl), pct(wl, 50), pct(wl, 90), pct(wl, 99), wl[-1]))
la = sorted(int(r['la_bytes']) for r in rows if r['la_bytes'] != '')
print("last_analysis bytes: n=%d mean=%.0f p50=%s p90=%s p99=%s max=%s" % (len(la), statistics.mean(la), pct(la, 50), pct(la, 90), pct(la, 99), la[-1]))
ap = sorted(int(r['ap_bytes']) for r in rows if r['ap_bytes'] not in ('', '5', '8'))
print("analysis_passes bytes (non-empty): n=%d mean=%.0f p50=%s p99=%s" % (len(ap), statistics.mean(ap) if ap else 0, pct(ap, 50), pct(ap, 99)))
fl = sorted(int(r['fen_len']) for r in rows)
print("fen chars: mean=%.1f p50=%s p99=%s max=%s" % (statistics.mean(fl), pct(fl, 50), pct(fl, 99), fl[-1]))
ni = sorted(int(r['nodes_invested']) for r in rows if int(r['nodes_invested']) > 0)
print("nodes_invested (searched): n=%d p10=%s p50=%s p90=%s p99=%s max=%s" % (len(ni), pct(ni, 10), pct(ni, 50), pct(ni, 90), pct(ni, 99), ni[-1]))
vi = collections.Counter(int(r['visits']) for r in rows)
print("visits:", dict(sorted(vi.items())))
bp = sorted(int(r['backed_plies']) for r in rows if int(r['backed_plies']) > 0)
print("backed_plies>0: n=%d p50=%s p90=%s p99=%s max=%s" % (len(bp), pct(bp, 50), pct(bp, 90), pct(bp, 99), bp[-1] if bp else None))
ev = sorted(int(r['eval_cp']) for r in rows if r['eval_cp'] != '')
print("eval_cp: n=%d p1=%s p10=%s p50=%s p90=%s p99=%s  frac|e|>=9000: %.3f" % (len(ev), pct(ev, 1), pct(ev, 10), pct(ev, 50), pct(ev, 90), pct(ev, 99), sum(1 for e in ev if abs(e) >= 9000) / len(ev)))
# eval on unsearched open leaves (seeded claims)
seeded = sum(1 for r in rows if r['eval_cp'] != '' and int(r['nodes_invested']) == 0 and r['status'] == 'UNKNOWN')
print("seeded evals on unsearched open nodes: %.4f" % (seeded / n))
upd = sorted(int(r['updated_s']) for r in rows)
import time
now = max(upd)
print("updated age days: p10=%.1f p50=%.1f p90=%.1f" % ((now - pct(upd, 90)) / 86400, (now - pct(upd, 50)) / 86400, (now - pct(upd, 10)) / 86400))

print("\n=== landings")
L = list(csv.DictReader(open(landings_path, newline='')))
print("n =", len(L), " states:", dict(collections.Counter(r['state'] for r in L)))
t0, t1 = float(L[0]['created_s']), float(L[-1]['created_s'])
print("span hours: %.1f  mean rate/min: %.1f" % ((t1 - t0) / 3600, len(L) / ((t1 - t0) / 60)))
permin = collections.Counter(int(float(r['created_s']) // 60) for r in L)
pm = sorted(permin.values())
print("per-minute submits: p50=%s p90=%s p99=%s max=%s" % (pct(pm, 50), pct(pm, 90), pct(pm, 99), pm[-1]))
lat = sorted(float(r['updated_s']) - float(r['created_s']) for r in L if r['state'] == 'DONE')
print("job created->updated seconds (ingest+summary write): p50=%.2f p90=%.2f p99=%.2f max=%.0f" % (pct(lat, 50), pct(lat, 90), pct(lat, 99), lat[-1]))
pb = sorted(int(r['payload_bytes']) for r in L)
print("payload bytes: mean=%.0f p50=%s p90=%s p99=%s max=%s" % (statistics.mean(pb), pct(pb, 50), pct(pb, 90), pct(pb, 99), pb[-1]))
be = sorted(int(r['backed_evals']) for r in L if r['backed_evals'] != '')
bu = sorted(int(r['backed_up']) for r in L if r['backed_up'] != '')
bt = sorted(int(r['batch']) for r in L if r['batch'] != '')
print("with propagated summary: %d (%.3f)" % (len(be), len(be) / len(L)))
print("batch size (landings merged in the climb): p50=%s p90=%s p99=%s max=%s mean=%.1f" % (pct(bt, 50), pct(bt, 90), pct(bt, 99), bt[-1], statistics.mean(bt)))
print("backed_evals per batch-summary: p50=%s p90=%s p99=%s max=%s mean=%.1f" % (pct(be, 50), pct(be, 90), pct(be, 99), be[-1], statistics.mean(be)))
print("backed_up per batch-summary:    p50=%s p90=%s p99=%s max=%s mean=%.2f" % (pct(bu, 50), pct(bu, 90), pct(bu, 99), bu[-1], statistics.mean(bu)))
# per-landing normalised
per = sorted(int(r['backed_evals']) / max(1, int(r['batch'])) for r in L if r['backed_evals'] != '' and r['batch'] != '')
print("backed_evals / batch: p50=%.2f p90=%.2f p99=%.2f mean=%.2f" % (pct(per, 50), pct(per, 90), pct(per, 99), statistics.mean(per)))
cc = collections.Counter(int(r['closed_children']) for r in L if r['closed_children'] != '')
tot = sum(cc.values())
print("closed_children per landing: frac0=%.3f mean=%.3f max=%s" % (cc[0] / tot, sum(k * v for k, v in cc.items()) / tot, max(cc)))
keys = [r['key'] for r in L]
print("distinct positions among landings: %d (%.3f)" % (len(set(keys)), len(set(keys)) / len(keys)))
print("tb landings:", sum(1 for r in L if r['tb'] not in ('', None)))
