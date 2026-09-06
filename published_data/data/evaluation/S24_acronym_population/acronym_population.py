#!/usr/bin/env python3
"""Table S24 — the acronym-shaped Condition population and what it resolved to.

Table S6 scores a curated list of acronyms against a gold concept. This table
instead takes every acronym-shaped Condition label the corpus contains, so the
reader can see the population the pipeline actually faced rather than a chosen
subset, and how much of it received a controlled-vocabulary concept.

"Acronym-shaped" is not defined here: it is `_is_acronym_shaped` from
`code/5_repaired_pipeline/phase2/acronym_expand.py`, the same predicate the
pipeline uses to decide what to route through study-scoped expansion.

The measurement is taken on samples whose Condition cell holds a single label.
Multi-label cells carry several ids under one `final_Condition_source` and their
raw components are not positionally aligned with those ids, so an acronym inside
one cannot be scored unambiguously; restricting to single-label cells makes each
sample's outcome exact rather than apportioned.

Usage:
    acronym_population.py FINAL_CORPUS.csv.gz PHASE2_CODE_DIR OUT.csv
"""

import csv
import gzip
import sys
from collections import Counter

MIN_OCCURRENCES = 100


def main() -> int:
    corpus, code_dir, out = sys.argv[1:4]
    sys.path.insert(0, code_dir)
    from acronym_expand import _is_acronym_shaped

    occurrences = Counter()
    resolved = Counter()
    concepts = {}

    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row["phase1b_Condition"] or "").strip()
            if ";" in raw or not _is_acronym_shaped(raw):
                continue
            occurrences[raw] += 1
            if row["final_Condition_source"] in ("mesh", "cellosaurus"):
                resolved[raw] += 1
                concepts.setdefault(raw, Counter())[row["final_Condition"]] += 1

    listed = sorted(
        (a for a, n in occurrences.items() if n >= MIN_OCCURRENCES),
        key=lambda a: (-occurrences[a], a),
    )

    rows = []
    for a in listed:
        top = concepts.get(a)
        rows.append(dict(
            acronym=a,
            occurrences=occurrences[a],
            concept=top.most_common(1)[0][0] if top else "",
            resolved=resolved[a],
            instances_resolved=("all" if resolved[a] == occurrences[a]
                                else "%d/%d" % (resolved[a], occurrences[a])),
        ))

    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    total = sum(occurrences.values())
    print("acronym-shaped Condition labels : %d" % len(occurrences))
    print("their occurrences               : %d" % total)
    print("resolved to a controlled concept: %d (%.1f%%)"
          % (sum(resolved.values()), 100 * sum(resolved.values()) / total))
    print("listed at >=%d occurrences      : %d, covering %d"
          % (MIN_OCCURRENCES, len(listed), sum(occurrences[a] for a in listed)))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
