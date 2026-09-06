#!/usr/bin/env python3
"""Table S3 — how much of what Phase 1 left unlabelled Phase 1b recovered.

The two columns come from different files because the corpus does not keep a
Phase 1 column: what a sample looked like before GSE-context inference survives
only in the Phase 1/1b output, while the label it ended up with is in the
released corpus. Both are shipped, and taking each column from the file that
holds it keeps the Phase 1b coverage identical to Table S15 rather than to an
earlier extraction pass.

Usage:
    phase1b_recovery.py PHASE1_DIR FINAL_CORPUS.csv.gz OUT.csv
"""

import csv
import glob
import gzip
import json
import os
import sys
from collections import Counter

FIELDS = ("Tissue", "Condition", "Treatment")
MISSING = {"not specified", "", "none", "unknown", None}


def has_value(v):
    return (v or "").strip().lower() not in MISSING


def main() -> int:
    phase1_dir, corpus, out = sys.argv[1:4]

    labelled_at_phase1 = Counter()
    seen = set()
    for path in sorted(glob.glob(os.path.join(phase1_dir, "p1_*.json.gz"))):
        with gzip.open(path, "rt") as fh:
            for s in json.load(fh)["samples"]:
                if s["gsm"] in seen:
                    continue
                seen.add(s["gsm"])
                for f in FIELDS:
                    if has_value((s.get("phase1") or {}).get(f)):
                        labelled_at_phase1[f] += 1

    labelled_after = Counter()
    total = 0
    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            total += 1
            for f in FIELDS:
                if has_value(row["phase1b_" + f]):
                    labelled_after[f] += 1

    rows = []
    for f in FIELDS:
        unlabelled = total - labelled_at_phase1[f]
        recovered = labelled_after[f] - labelled_at_phase1[f]
        rows.append(dict(
            field=f,
            not_specified_at_phase1=unlabelled,
            recovered_by_phase1b=recovered,
            recovery_rate="%.2f" % (100 * recovered / unlabelled),
            coverage_phase1="%.2f" % (100 * labelled_at_phase1[f] / total),
            coverage_phase1b="%.2f" % (100 * labelled_after[f] / total)))

    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print("corpus %d samples" % total)
    for r in rows:
        print("  %-10s NS@P1 %7d  recovered %7d  (%s%%)  coverage %s%% -> %s%%"
              % (r["field"], r["not_specified_at_phase1"],
                 r["recovered_by_phase1b"], r["recovery_rate"],
                 r["coverage_phase1"], r["coverage_phase1b"]))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
