import csv, math, sys, collections


def wilson(k, n, z=1.96):
    if not n:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / 2 / n) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / 4 / n / n) / d
    return (max(0, c - h), min(1, c + h))


rows = list(csv.DictReader(open(sys.argv[1])))
dec = [r for r in rows if r["verdict"] in ("appropriate", "inappropriate")]
k = sum(r["verdict"] == "inappropriate" for r in dec)
n = len(dec)
ik = sum(int(r["instances"]) for r in dec if r["verdict"] == "inappropriate")
inn = sum(int(r["instances"]) for r in dec)
lo, hi = wilson(k, n)
ilo, ihi = wilson(ik, inn)
print(
    f"inappropriate-OOV (recall failure) — value: {k}/{n}={k / n :.3f} [{lo :.2f},{hi :.2f}]  instance: {ik}/{inn}={ik / inn :.3f} [{ilo :.2f},{ihi :.2f}]"
)
