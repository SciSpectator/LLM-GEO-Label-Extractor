                      

"""Reviewer concerns 4 and 5, recomputed from the published manual verdicts.

Concern 5 says the four counts do not sum to N. Under the sentence the Methods
use they do not, because a wrong label is booked twice. They do sum once the
false positives are split by what the gold says about the sample:

    substitution   the gold has a value and the pipeline produced a different one
    false alarm    the gold has no value and the pipeline claimed one

A substitution is a precision error and a recall error at once, which is what
the Methods meant; a false alarm is only a precision error. Splitting them lets
the five categories partition N while every published precision and recall comes
out unchanged. Which kind a field's false positives are is not re-judged here -
it follows from the benchmark's own construction: every Tissue and Condition
sample carries a gold value, so those false positives are substitutions, while
Treatment is the field with gold-negative samples and its false positives land
there (S1 README).

Concern 4 says a sample-level average lets a few large, well-annotated studies
carry the score. That is a question about the unit of analysis, so the same
verdicts are re-aggregated by study: every GSE counts once, whatever its size.
A cluster bootstrap over studies replaces the Wilson interval, which treats
samples as independent when samples from one study are not.

Nothing here re-judges a sample. The verdict column is the authors' own.
"""



import csv

import os

import random

from collections import Counter, defaultdict



import os as _os
PKG = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", ".."))

GOLD = f"{PKG}/data/evaluation/S1_manual_benchmark"

MAP = _os.path.join(_os.path.dirname(__file__), "gsm_gse.csv")

OUT = _os.path.dirname(__file__)

BOOT = 10000

SEED = 42



gse = {r["gsm"]: r["gse"] for r in csv.DictReader(open(MAP))}



                                                                           

                                                                        

FIELDS = [

    ("Tissue", "manual_gold_Tissue_Condition.csv", "Tissue_verdict", "sub"),

    ("Condition", "manual_gold_Tissue_Condition.csv", "Condition_verdict", "sub"),

    ("Treatment", "manual_gold_Treatment.csv", "Treatment_verdict", "fa"),

    ("Sex", "manual_gold_Sex.csv", "verdict", "sub"),

    ("Age", "manual_gold_Age.csv", "verdict", "sub"),

]





def load(fname, col):

    with open(os.path.join(GOLD, fname)) as fh:

        return [(r["gsm"], gse.get(r["gsm"], ""), r[col].strip().upper())

                for r in csv.DictReader(fh)]





def f1(p, r):

    return 2 * p * r / (p + r) if (p + r) else 0.0





def wilson(k, n, z=1.96):

    if not n:

        return (0.0, 0.0)

    ph, d = k / n, 1 + z * z / n

    c = (ph + z * z / (2 * n)) / d

    h = z * ((ph * (1 - ph) / n + z * z / (4 * n * n)) ** 0.5) / d

    return (max(0.0, c - h), min(1.0, c + h))





def score(verdicts, fp_kind):

    """Precision and recall from a bag of verdicts under the split above."""

    c = Counter(verdicts)

    tp, fp, fn, tn = c["TP"], c["FP"], c["FN"], c["TN"]

    sub, fa = (fp, 0) if fp_kind == "sub" else (0, fp)

    gold_pos = tp + sub + fn

    claimed = tp + fp

    p = tp / claimed if claimed else float("nan")

    r = tp / gold_pos if gold_pos else float("nan")

    return dict(tp=tp, fp=fp, fn=fn, tn=tn, sub=sub, fa=fa, n=len(verdicts),

                gold_pos=gold_pos, claimed=claimed, p=p, r=r, f1=f1(p, r),

                acc=(tp + tn) / len(verdicts))





print("=" * 86)

print("CONCERN 5 - one outcome per sample; the categories partition N")

print("=" * 86)

print("""
  TP   correct label
  FPs  substitution: gold has a value, a different one was produced   (precision + recall error)
  FPa  false alarm:  gold has no value, one was produced              (precision error only)
  FN   gold has a value, none was produced
  TN   gold has no value, none was claimed

  Precision = TP / (TP + FPs + FPa)      Recall = TP / (TP + FPs + FN)
""")

print("  %-10s %6s %6s %5s %5s %5s %5s %6s %8s %8s %7s %7s %7s" %

      ("field", "N", "TP", "FPs", "FPa", "FN", "TN", "sum", "gold+", "claimed",

       "P", "R", "F1"))

per_field = {}

for name, fname, col, kind in FIELDS:

    rows = load(fname, col)

    d = score([v for _, _, v in rows], kind)

    d["rows"], d["kind"] = rows, kind

    per_field[name] = d

    tot = d["tp"] + d["sub"] + d["fa"] + d["fn"] + d["tn"]

    print("  %-10s %6d %6d %5d %5d %5d %5d %6d%s %8d %8d %7.4f %7.4f %7.4f" %

          (name, d["n"], d["tp"], d["sub"], d["fa"], d["fn"], d["tn"], tot,

           " " if tot == d["n"] else "!", d["gold_pos"], d["claimed"],

           d["p"], d["r"], d["f1"]))



three = ["Tissue", "Condition", "Treatment"]

print("\n  averages over Tissue, Condition, Treatment:")

mtp = sum(per_field[f]["tp"] for f in three)

mfp = sum(per_field[f]["fp"] for f in three)

mgp = sum(per_field[f]["gold_pos"] for f in three)

mcl = sum(per_field[f]["claimed"] for f in three)

mp, mr = mtp / mcl, mtp / mgp

print(f"    micro  P={mp:.4f}  R={mr:.4f}  F1={f1(mp, mr):.4f}"

      f"   (pooled TP={mtp:,}, claimed={mcl:,}, gold-positive={mgp:,})")

ap = sum(per_field[f]["p"] for f in three) / 3

ar = sum(per_field[f]["r"] for f in three) / 3

print(f"    macro  P={ap:.4f}  R={ar:.4f}  F1={sum(per_field[f]['f1'] for f in three)/3:.4f}"

      f"   (unweighted mean over the three fields)")



print()

print("=" * 86)

print("CONCERN 4 - does a handful of large studies carry the score?")

print("=" * 86)



rng = random.Random(SEED)

out = []

for name, _, _, kind in FIELDS:

    d = per_field[name]

    by_gse = defaultdict(list)

    for gsm, g, v in d["rows"]:

        by_gse[g or f"__nogse_{gsm}"].append(v)

    keys = list(by_gse)

    n_gse = len(keys)

    sizes = sorted((len(v) for v in by_gse.values()), reverse=True)



    ok = lambda v: v in ("TP", "TN")

    micro = sum(ok(v) for _, _, v in d["rows"]) / d["n"]

    per_acc = {g: sum(ok(v) for v in vs) / len(vs) for g, vs in by_gse.items()}

    macro_acc = sum(per_acc.values()) / n_gse



                                                                                 

    ps = [s["p"] for s in (score(vs, kind) for vs in by_gse.values()) if s["claimed"]]

    rs = [s["r"] for s in (score(vs, kind) for vs in by_gse.values()) if s["gold_pos"]]

    macro_p, macro_r = sum(ps) / len(ps), sum(rs) / len(rs)



    boots = []

    for _ in range(BOOT):

        flat = [v for _ in range(n_gse) for v in by_gse[keys[rng.randrange(n_gse)]]]

        boots.append(sum(ok(v) for v in flat) / len(flat))

    boots.sort()

    lo_c, hi_c = boots[int(0.025 * BOOT)], boots[int(0.975 * BOOT)]

    lo_w, hi_w = wilson(sum(ok(v) for _, _, v in d["rows"]), d["n"])

    perfect = sum(1 for a in per_acc.values() if a == 1.0)



    print(f"\n  {name}")

    print(f"    studies (GSE)                     {n_gse:>6,}   median {sizes[len(sizes)//2]} "

          f"samples/study, largest {sizes[0]}, top-10 hold {100*sum(sizes[:10])/d['n']:.1f}%")

    print(f"    accuracy  per sample (published)   {micro:.4f}   Wilson [{lo_w:.4f}, {hi_w:.4f}]")

    print(f"    accuracy  per study  (macro)       {macro_acc:.4f}   change {macro_acc-micro:+.4f}")

    print(f"    cluster bootstrap over studies              [{lo_c:.4f}, {hi_c:.4f}]"

          f"   {(hi_c-lo_c)/max(hi_w-lo_w,1e-9):.1f}x the Wilson width")

    print(f"    precision per study (macro)        {macro_p:.4f}   vs {d['p']:.4f} per sample")

    print(f"    recall    per study (macro)        {macro_r:.4f}   vs {d['r']:.4f} per sample")

    print(f"    studies with no error             {perfect:>6,}/{n_gse:,} ({100*perfect/n_gse:.1f}%)")



    out.append(dict(field=name, n_samples=d["n"], TP=d["tp"], FP_substitution=d["sub"],

                    FP_false_alarm=d["fa"], FN=d["fn"], TN=d["tn"],

                    categories_sum=d["tp"] + d["sub"] + d["fa"] + d["fn"] + d["tn"],

                    gold_positive=d["gold_pos"], labels_claimed=d["claimed"],

                    precision=round(d["p"], 4), recall=round(d["r"], 4),

                    f1=round(d["f1"], 4), accuracy_per_sample=round(micro, 4),

                    wilson_lo=round(lo_w, 4), wilson_hi=round(hi_w, 4),

                    n_gse=n_gse, median_gse_size=sizes[len(sizes) // 2],

                    largest_gse=sizes[0], top10_share=round(sum(sizes[:10]) / d["n"], 4),

                    accuracy_per_study=round(macro_acc, 4),

                    precision_per_study=round(macro_p, 4),

                    recall_per_study=round(macro_r, 4),

                    cluster_boot_lo=round(lo_c, 4), cluster_boot_hi=round(hi_c, 4),

                    gse_with_no_error=perfect))



with open(f"{OUT}/study_level_metrics.csv", "w", newline="") as fh:

    w = csv.DictWriter(fh, fieldnames=list(out[0]))

    w.writeheader()

    w.writerows(out)

print(f"\n  wrote {OUT}/study_level_metrics.csv")

