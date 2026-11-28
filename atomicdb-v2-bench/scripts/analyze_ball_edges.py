import csv, sys, collections

path = sys.argv[1]
parents = collections.Counter()
children = collections.Counter()
pdepth = {}
n = 0
with open(path, newline='') as f:
    for r in csv.DictReader(f):
        n += 1
        parents[r['parent']] += 1
        children[r['child']] += 1
        pdepth[r['parent']] = int(r['pdepth'])
print("edges:", n)
print("distinct parents:", len(parents), " distinct children:", len(children))
allk = set(parents) | set(children)
print("distinct positions touched:", len(allk))
print("children that are also parents:", len(set(parents) & set(children)))
# children depth = min parent depth + 1 (approx)
by_depth = collections.Counter(pdepth.values())
print("parents with edges by depth:", dict(sorted(by_depth.items())))
edges_by_depth = collections.Counter()
for p, c in parents.items():
    edges_by_depth[pdepth[p]] += c
print("edges by parent depth:", dict(sorted(edges_by_depth.items())))
od = sorted(parents.values())
print("out-degree of parents: p5=%d p50=%d p95=%d max=%d ; ==1: %d" % (od[len(od)//20], od[len(od)//2], od[len(od)*95//100], od[-1], sum(1 for d in od if d == 1)))
ind = collections.Counter(children.values())
tot = len(children)
print("in-degree (within ball) hist:", {k: round(v / tot, 4) for k, v in sorted(ind.items())[:12]}, " max:", max(children.values()))
print("mean in-degree within ball: %.3f" % (n / tot))
