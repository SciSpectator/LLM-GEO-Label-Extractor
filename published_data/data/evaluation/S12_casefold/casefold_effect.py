"""Measure the effect of the final Phase 2 case-fold step.

Usage:
    python3 casefold_effect.py [dictionary_prefold.json.gz] [dictionary.json.gz] [out.csv]

The fold merges out-of-vocabulary concepts whose labels are identical up to case,
whitespace, hyphen, underscore or dot onto a single canonical identifier. Its effect is
measured by comparing the value dictionary before the fold with the released one:

    merged_concepts   OOV identifiers that disappear (their values move to another id)
    fold_groups       post-fold identifiers that absorbed more than one pre-fold identifier
    repointed_samples sample instances whose identifier changed
    grouped_samples   sample instances belonging to a fold group

Both dictionaries ship with the package, so the numbers recompute without the pipeline.
"""

import csv
import gzip
import json
import sys
from collections import defaultdict

FIELDS = ("Tissue", "Condition", "Treatment")


def is_oov(identifier):
    return str(identifier).startswith(("OOV", "ART"))


def main():
    pre_path = sys.argv[1] if len(sys.argv) > 1 else "../S8_oov_consolidation/dictionary_prefold.json.gz"
    post_path = sys.argv[2] if len(sys.argv) > 2 else "../../06_final_labels_repaired/phase2_out/dictionary.json.gz"
    out = sys.argv[3] if len(sys.argv) > 3 else "casefold_effect.csv"

    with gzip.open(pre_path, "rt") as fh:
        pre = json.load(fh)
    with gzip.open(post_path, "rt") as fh:
        post = json.load(fh)

    rows = []
    totals = [0, 0, 0, 0]
    for field in FIELDS:
        before, after = pre[field], post[field]
        merged, repointed = set(), 0
        absorbed, instances = defaultdict(set), defaultdict(int)
        for raw, entry in before.items():
            later = after.get(raw)
            if not later:
                continue
            old, new = str(entry.get("id", "")), str(later.get("id", ""))
            if not is_oov(old):
                continue
            if old != new:
                merged.add(old)
                repointed += int(later.get("count", 0))
            absorbed[new].add(old)
            instances[new] += int(later.get("count", 0))
        groups = [k for k, ids in absorbed.items() if len(ids) > 1]
        grouped = sum(instances[k] for k in groups)
        rows.append([field, len(merged), len(groups), repointed, grouped])
        for i, v in enumerate((len(merged), len(groups), repointed, grouped)):
            totals[i] += v

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "merged_concepts", "fold_groups", "repointed_samples", "grouped_samples"])
        for r in rows:
            w.writerow(r)
        w.writerow(["Total"] + totals)

    header = f"{'field':<12}{'merged':>9}{'groups':>9}{'repointed':>12}{'grouped':>10}"
    print(header)
    for r in rows:
        print(f"{r[0]:<12}{r[1]:>9,}{r[2]:>9,}{r[3]:>12,}{r[4]:>10,}")
    print(f"{'Total':<12}{totals[0]:>9,}{totals[1]:>9,}{totals[2]:>12,}{totals[3]:>10,}")


if __name__ == "__main__":
    main()
