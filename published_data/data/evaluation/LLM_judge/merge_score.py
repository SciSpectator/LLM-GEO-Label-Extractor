import csv, math, glob, os

BASE = os.path.dirname(os.path.abspath(__file__))


def wilson(k, n, z=1.96):
    if not n:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / 2 / n) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / 4 / n / n) / d
    return (max(0, c - h), min(1, c + h))


rows = []
for f in sorted(glob.glob(os.path.join(BASE, "t5_shard*_judge.csv"))):
    rows += list(csv.DictReader(open(f)))
dec = [r for r in rows if r.get("verdict") in ("appropriate", "inappropriate")]
k = sum(r["verdict"] == "inappropriate" for r in dec)
n = len(dec)
ik = sum(int(r["instances"]) for r in dec if r["verdict"] == "inappropriate")
inn = sum(int(r["instances"]) for r in dec)
lo, hi = wilson(k, n)
ilo, ihi = wilson(ik, inn)
print(f"n_judged={len(rows)} decided={n}")
print(
    f"inappropriate-OOV value: {k}/{n}={k / n :.3f} [{lo :.3f},{hi :.3f}]  instance: {ik}/{inn}={ik / inn :.3f} [{ilo :.3f},{ihi :.3f}]"
)
