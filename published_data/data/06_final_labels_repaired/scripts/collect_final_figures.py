#!/usr/bin/env python3
"""Collect every recomputed figure into one file, released beside final.

Each number here is produced by the manuscript's OWN evaluation script, run
twice: once on the released Phase 2 output and once on the merged output that
carries the re-extracted samples. Reporting both on the same instrument is the
point -- a repaired figure is only meaningful next to the released one it
replaces, and running the published script rather than a new one is what makes
the comparison a measurement instead of a claim.

The acronym figure is the exception and says so. The published
acronym_expansion() reads one dictionary entry per acronym and multiplies by its
count, which cannot represent an abbreviation resolved differently in different
studies -- the very thing the repair introduces. Scored that way the repaired
corpus reports recall 0.298, not because the labels are wrong but because the
instrument cannot see them. The per-sample score, over the same corpus and the
same gold, is carried alongside it.

Usage:
    collect_final_figures.py EVAL_DIR OUT_JSON
"""

import csv
import json
import os
import sys


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def pair(eval_dir, name):
    return {"released": read(os.path.join(eval_dir, "released", name)),
            "final": read(os.path.join(eval_dir, "final", name))}


def main() -> int:
    eval_dir, out_json = sys.argv[1:3]
    figs = {
        "composition": pair(eval_dir, "composition.csv"),          # S10
        "db_resolution": pair(eval_dir, "db_resolution.csv"),      # S5, S16
        "vocabulary_precision": pair(eval_dir, "vocabulary_precision.csv"),  # S7, S2
        "collapse": pair(eval_dir, "collapse.csv"),                # S2, Table 3
        "branch_validity": pair(eval_dir, "branch_validity.csv"),
        "resource_summary": {
            "released": read(os.path.join(eval_dir, "S21_released.csv")),
            "final": read(os.path.join(eval_dir, "S21_final.csv"))},          # S21
        "field_scope": {
            "released": read(os.path.join(eval_dir, "S22_released.csv")),
            "final": read(os.path.join(eval_dir, "S22_final.csv"))},          # S22
        "condition_split": {
            "released": read(os.path.join(eval_dir, "S17_released.csv")),
            "final": read(os.path.join(eval_dir, "S17_final.csv"))},          # S17
    }
    json.dump(figs, open(out_json, "w"), indent=2)

    def show(key, cols):
        r, f = figs[key]["released"], figs[key]["final"]
        if not r or not f:
            return
        print(f"\n=== {key} ===")
        keyname = list(r[0])[0]
        idx = {tuple(x.get(c, "") for c in cols[:1]): x for x in f}
        for row in r:
            k = tuple(row.get(c, "") for c in cols[:1])
            g = idx.get(k, {})
            deltas = []
            for c in cols[1:]:
                a, b = row.get(c, ""), g.get(c, "")
                if a != b:
                    deltas.append(f"{c}: {a} -> {b}")
            if deltas:
                print(f"  {row[keyname]:<12} " + " | ".join(deltas))

    show("composition", ["field", "mesh", "cellosaurus", "oov", "not_specified"])
    show("collapse", ["field", "canonical", "collapse_ratio"])
    show("vocabulary_precision",
         ["field", "mesh", "mesh_verified", "cellosaurus",
          "cellosaurus_verified", "oov", "oov_inappropriate"])
    print(f"\nwrote -> {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
