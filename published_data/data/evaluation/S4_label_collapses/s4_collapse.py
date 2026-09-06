#!/usr/bin/env python3
"""Table S4 — the representative raw-label collapses, counted in the corpus.

The table lists raw strings that Phase 2 routed onto one concept, with the number
of samples carrying each. Those counts are measurements and this recomputes them,
so the table stops being the only one in the paper with nothing behind it.

For each raw string the count is the number of samples whose phase1b value for
that field is exactly that string, and the concept is the identifier those samples
carry after Phase 2. A string may resolve differently in a minority of studies,
because Phase 2 decides per study where study context is decisive, so the
dominant identifier and its share are both reported rather than only the first.

Usage:
    s4_collapse.py FINAL_CORPUS.csv.gz OUT.csv
"""

import csv
import gzip
import sys
from collections import Counter, defaultdict

# The rows Table S4 prints, in its order.
ROWS = [
    ("Tissue", "Blood Cells (MeSH D001773)",
     ["whole blood", "Whole blood", "Whole Blood"]),
    ("Tissue", "A-549 (Cellosaurus CVCL_0023)",
     ["A549", "A549 cells", "human cell line A549"]),
    ("Condition", "Adenocarcinoma (MeSH D000230)",
     ["adenocarcinoma", "Adenocarcinoma", "gastric adenocarcinoma"]),
    ("Condition", "Pulmonary Disease, Chronic Obstructive (MeSH D029424)", ["COPD"]),
    ("Treatment", "Dimethyl Sulfoxide (MeSH D004121)",
     ["DMSO", "dmso", "0.1% DMSO"]),
    ("Treatment", "vehicle (OOV concept; case-folded)",
     ["vehicle", "Vehicle", "VEHICLE"]),
]


def main() -> int:
    corpus, out = sys.argv[1:3]
    wanted = {(f, v) for f, _c, vs in ROWS for v in vs}
    counts = Counter()
    ids = defaultdict(Counter)
    total = 0

    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            total += 1
            for field in ("Tissue", "Condition", "Treatment"):
                raw = (row.get(f"phase1b_{field}") or "").strip()
                if (field, raw) not in wanted:
                    continue
                counts[(field, raw)] += 1
                assigned = (row.get(f"final_{field}_id") or "").strip()
                ids[(field, raw)][assigned] += 1

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "canonical_concept", "raw_value", "samples",
                    "dominant_id", "samples_with_dominant_id", "share_of_samples"])
        for field, concept, values in ROWS:
            for v in values:
                n = counts[(field, v)]
                top = ids[(field, v)].most_common(1)
                did, dn = (top[0] if top else ("", 0))
                w.writerow([field, concept, v, n, did, dn,
                            f"{100.0 * dn / n:.2f}%" if n else "n/a"])

    print(f"corpus {total:,} samples")
    print(f"  {'field':<11} {'raw value':<24} {'samples':>9}  dominant id (share)")
    for field, _concept, values in ROWS:
        for v in values:
            n = counts[(field, v)]
            top = ids[(field, v)].most_common(1)
            did, dn = (top[0] if top else ("", 0))
            share = f"{100.0 * dn / n:.2f}%" if n else "n/a"
            print(f"  {field:<11} {v:<24} {n:>9,}  {did} ({share})")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
