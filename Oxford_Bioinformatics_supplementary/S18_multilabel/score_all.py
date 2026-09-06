import csv, math, glob, os, collections

B = os.path.dirname(os.path.abspath(__file__))


def wilson(k, n, z=1.96):
    if not n:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / 2 / n) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / 4 / n / n) / d
    return (max(0, c - h), min(1, c + h))


r = []
for f in [os.path.join(B, "multilabel_judge.csv")] + sorted(
    glob.glob(os.path.join(B, "multilabel_shard*_judge.csv"))
):
    try:
        r += [{**x, "_f": f} for x in csv.DictReader(open(f))]
    except:
        pass
dec = [x for x in r if x["verdict"] in ("correct", "incorrect")]
k = sum(x["verdict"] == "correct" for x in dec)
n = len(dec)
lo, hi = wilson(k, n)
by = collections.defaultdict(list)
for x in r:
    by[(x["_f"], x["item"])].append(x["verdict"])
allc = sum(
    all(v == "correct" for v in vs if v in ("correct", "incorrect"))
    for vs in by.values()
)
print(
    f"labels={len(r)} decided={n} | PRECISION {k}/{n}={k / n :.3f} [{lo :.3f},{hi :.3f}] | per-sample all-correct {allc}/{len(by)}={allc / len(by):.3f}"
)
