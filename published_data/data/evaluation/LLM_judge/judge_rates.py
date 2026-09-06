#!/usr/bin/env python3
"""Recompute the inappropriate-OOV rates the S7 caption reports.

The value-level rate is unambiguous: it counts audited dictionary values, the
same per-value basis S7 tabulates.

The occurrence-weighted rate is not, and this script reports it as bounds rather
than a point estimate. Two facts make a single weight impossible to reconstruct
from the released corpus:

  * Phase 2 resolves short forms per study, so a value that is OOV at dictionary
    level may be resolved in most samples carrying it. The dictionary entry's
    top-level fields are the corpus-wide default the collision guard withholds
    on; the per-study decisions live in its `by_gse` map.
  * A multi-label cell carries several ids but one `final_<field>_source`, and
    its raw components are not positionally aligned with its ids -- components
    that normalize to the same concept collapse. Of the 4,084 cells carrying an
    `AML` Condition component, 3,579 carry D015470, and 109 carry both D015470
    and an OOV id, so that component is simultaneously resolved and not.

Upper bound weights each audited value by every sample carrying it (this is what
the dictionary's `count` field gives, and it charges verdicts with occurrences
the corpus did resolve). Lower bound counts only samples whose whole field
assignment stayed OOV, which misses OOV components inside multi-label cells.
The true figure lies between them.

Usage:
    judge_rates.py FINAL_CORPUS.csv.gz JUDGE_DIR
"""

import collections
import csv
import glob
import gzip
import math
import os
import sys

FIELDS = ("Tissue", "Condition", "Treatment")


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (centre - half), 100 * (centre + half)


def wholly_oov(corpus):
    """Samples whose entire field assignment stayed OOV, keyed by raw label."""
    n = collections.Counter()
    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            for f in FIELDS:
                raw = (row["phase1b_" + f] or "").strip()
                if raw and row["final_%s_source" % f] == "oov":
                    n[(f, raw)] += 1
    return n


def main() -> int:
    corpus, judge_dir = sys.argv[1:3]
    strict = wholly_oov(corpus)

    rows = []
    for path in sorted(glob.glob(os.path.join(judge_dir, "t5_shard[0-3]_judge.csv"))):
        with open(path, newline="") as fh:
            rows += list(csv.DictReader(fh))

    decided = [r for r in rows if r["verdict"] in ("appropriate", "inappropriate")]
    wrong = [r for r in decided if r["verdict"] == "inappropriate"]

    lo, hi = wilson(len(wrong), len(decided))
    print("audited values        %d (%d decided, %d indeterminate)"
          % (len(rows), len(decided), len(rows) - len(decided)))
    print("inappropriate-OOV     %d/%d = %.2f%% [95%% CI %.2f-%.2f]"
          % (len(wrong), len(decided), 100 * len(wrong) / len(decided), lo, hi))

    for label, weight in (
            ("upper (every occurrence)", lambda r: int(r["instances"])),
            ("lower (wholly-OOV cells)",
             lambda r: strict.get((r["field"], r["raw_value"]), 0))):
        num = sum(weight(r) for r in wrong)
        den = sum(weight(r) for r in decided)
        print("occurrence-weighted, %-24s %d/%d = %.2f%%"
              % (label, num, den, 100 * num / den))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
