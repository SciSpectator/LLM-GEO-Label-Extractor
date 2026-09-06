"""Audit the cross-field scope step: what it removed, what it moved, and what it cost.

Usage:
    python3 field_scope_audit.py [final_labels.csv.gz] [out.csv]

A field must hold only its own kind of concept. After resolution, phase2.py::field_reroutes
checks each value against the controlled vocabularies and acts on two factual mismatches:

    disease in Tissue          the value resolves to exactly one MeSH disease (C or F branch)
                               and to no anatomy (A branch)
    cell line in Condition     the value is a catalogued Cellosaurus line
    or in Treatment

A mismatched value is removed from the field it does not belong to, and where the target is
unambiguous the concept is moved there instead. The question a reader will ask is whether
removing a label destroyed information, so this script reports, for every removal, whether
the field that should hold that kind of concept already carries a label, and for the
cell-line cases whether that label is a Cellosaurus identifier.
"""

import csv
import gzip
import sys
from collections import Counter

REMOVED = {
    "filtered:disease-in-tissue": ("Tissue", "Condition", "disease in Tissue"),
    "filtered:cellline-in-condition": ("Condition", "Tissue", "cell line in Condition"),
    "filtered:cellline-in-treatment": ("Treatment", "Tissue", "cell line in Treatment"),
}
FIELDS = ("Tissue", "Condition", "Treatment")


def identifiers(value):
    return [x.strip() for x in (value or "").split(";") if x.strip()]


def main():
    corpus = sys.argv[1] if len(sys.argv) > 1 else "../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz"
    out = sys.argv[2] if len(sys.argv) > 2 else "field_scope_audit.csv"

    removed = Counter()
    target_labelled = Counter()
    target_cvcl = Counter()
    moved = Counter()
    with gzip.open(corpus, "rt") as fh:
        for row in csv.DictReader(fh):
            for field in FIELDS:
                source = (row.get(f"final_{field}_source") or "").strip()
                if source in REMOVED:
                    _, target, label = REMOVED[source]
                    removed[label] += 1
                    ids = identifiers(row.get(f"final_{target}_id"))
                    if ids:
                        target_labelled[label] += 1
                    if any(i.startswith("CVCL") for i in ids):
                        target_cvcl[label] += 1
                elif source.startswith("rerouted:"):
                    moved[f"moved to {field}"] += 1

    rows = []
    for label in ("disease in Tissue", "cell line in Condition", "cell line in Treatment"):
        n = removed[label]
        if not n:
            continue
        rows.append([label, "removed", n, target_labelled[label],
                     f"{100 * target_labelled[label] / n:.2f}",
                     target_cvcl[label] if "cell line" in label else ""])
    for label, n in sorted(moved.items()):
        rows.append([label, "moved", n, "", "", ""])

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case", "action", "samples", "target_field_already_labelled",
                    "pct_target_labelled", "target_field_carries_cellosaurus"])
        w.writerows(rows)

    print(f"{'case':<24}{'action':<9}{'samples':>9}{'target labelled':>17}{'%':>8}{'target CVCL':>13}")
    for r in rows:
        print(f"{r[0]:<24}{r[1]:<9}{r[2]:>9,}{str(r[3]):>17}{str(r[4]):>8}{str(r[5]):>13}")
    total_removed = sum(removed.values())
    total_kept = sum(target_labelled.values())
    print(f"\n{'removed in total':<24}{total_removed:>9,}")
    print(f"{'of which the target field already had a label':<46}{total_kept:>9,} "
          f"({100 * total_kept / total_removed:.2f}%)")
    print(f"{'moved in total':<24}{sum(moved.values()):>9,}")


if __name__ == "__main__":
    main()
