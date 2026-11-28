import csv, sys, collections, statistics

pos_path, depth_path = sys.argv[1], sys.argv[2]
n = 0
by_depth = collections.Counter()
exp_by_depth = collections.Counter()
closed_by_depth = collections.Counter()
status = collections.Counter()
closure = collections.Counter()
has_eval = has_backed = searched = backed_open = 0
with open(pos_path, newline='') as f:
    for r in csv.DictReader(f):
        n += 1
        d = int(r['depth'])
        by_depth[d] += 1
        status[r['status']] += 1
        closure[r['closure']] += 1
        if r['expanded'] == 't':
            exp_by_depth[d] += 1
        if r['status'] != 'UNKNOWN':
            closed_by_depth[d] += 1
        has_eval += r['eval_cp'] != ''
        has_backed += r['backed_eval'] != ''
        backed_open += (r['backed_eval'] != '' and r['status'] == 'UNKNOWN')
        searched += int(r['nodes_invested']) > 0
print("ball positions:", n)
print("by depth:", dict(sorted(by_depth.items())))
print("expanded by depth:", dict(sorted(exp_by_depth.items())))
print("closed by depth:", dict(sorted(closed_by_depth.items())))
print("status:", {k: round(v / n, 4) for k, v in status.items()})
print("closure:", {k: round(v / n, 4) for k, v in closure.items()})
print("has eval %.4f  has backed %.4f  backed&open %.4f  searched %.4f  expanded %.4f" % (has_eval / n, has_backed / n, backed_open / n, searched / n, sum(exp_by_depth.values()) / n))

rows = list(csv.DictReader(open(depth_path, newline='')))
dr = sorted(int(r['depth_to_root']) for r in rows if r['depth_to_root'] != '')
print("\nrecent landings: n=%d reached root via first-parent chain: %d" % (len(rows), len(dr)))
if dr:
    q = lambda p: dr[min(len(dr) - 1, int(p * len(dr)))]
    print("creation-tree depth of landings: min=%d p10=%d p25=%d p50=%d p75=%d p90=%d p99=%d max=%d mean=%.1f" % (dr[0], q(.10), q(.25), q(.5), q(.75), q(.9), q(.99), dr[-1], statistics.mean(dr)))
