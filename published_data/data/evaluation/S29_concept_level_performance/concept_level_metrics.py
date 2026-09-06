                      

"""Macro-average over gold classes - the reviewer's 'rarer classes' request.

A sample-weighted average is carried by whatever concepts happen to be common in
the benchmark. Averaging over the distinct gold concepts instead gives a rare
tissue the same weight as a frequent one, which is the comparison asked for.

Only Tissue and Condition have a class to group by: their gold is a curated
concept. The Treatment gold is the sample's own metadata text, adjudicated
case by case, so it has no class structure and is left out rather than invented.
"""



import csv

import os
import os as _os
PKG = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", ".."))

from collections import Counter, defaultdict



GOLD = _os.path.join(PKG, "data", "evaluation", "S1_manual_benchmark",
                     "manual_gold_Tissue_Condition.csv")

OUT = _os.path.join(_os.path.dirname(__file__), "concept_level_metrics.csv")



rows = list(csv.DictReader(open(GOLD)))

out = []

for field in ("Tissue", "Condition"):

    by = defaultdict(list)

    for r in rows:

        by[r[f"{field}_gold"].strip().lower()].append(

            r[f"{field}_verdict"].strip().upper() in ("TP", "TN"))



    sizes = Counter(len(v) for v in by.values())

    n_cls = len(by)

    micro = sum(sum(v) for v in by.values()) / sum(len(v) for v in by.values())

    macro = sum(sum(v) / len(v) for v in by.values()) / n_cls



                                                        

    rare = {k: v for k, v in by.items() if len(v) <= 2}

    common = {k: v for k, v in by.items() if len(v) > 8}

    r_acc = sum(sum(v) for v in rare.values()) / max(sum(len(v) for v in rare.values()), 1)

    c_acc = sum(sum(v) for v in common.values()) / max(sum(len(v) for v in common.values()), 1)



    print(f"\n  {field}")

    print(f"    distinct gold concepts            {n_cls:,}  "

          f"({sizes[1]:,} appear once, {100*sizes[1]/n_cls:.0f}%)")

    print(f"    largest class                     {max(len(v) for v in by.values())} samples")

    print(f"    accuracy, sample-weighted (micro) {micro:.4f}")

    print(f"    accuracy, class-weighted  (macro) {macro:.4f}   change {macro-micro:+.4f}")

    print(f"    rare concepts (<=2 samples)       {r_acc:.4f}  over {len(rare):,} concepts, "

          f"{sum(len(v) for v in rare.values()):,} samples")

    print(f"    common concepts (>8 samples)      {c_acc:.4f}  over {len(common):,} concepts, "

          f"{sum(len(v) for v in common.values()):,} samples")

    out.append(dict(field=field, distinct_gold_concepts=n_cls, singleton_concepts=sizes[1],

                    largest_class=max(len(v) for v in by.values()),

                    accuracy_micro=round(micro, 4), accuracy_macro=round(macro, 4),

                    delta=round(macro - micro, 4),

                    rare_concepts=len(rare), rare_samples=sum(len(v) for v in rare.values()),

                    accuracy_rare=round(r_acc, 4),

                    common_concepts=len(common),

                    common_samples=sum(len(v) for v in common.values()),

                    accuracy_common=round(c_acc, 4)))



with open(OUT, "w", newline="") as fh:

    w = csv.DictWriter(fh, fieldnames=list(out[0]))

    w.writeheader()

    w.writerows(out)

print(f"\n  wrote {OUT}")

