#!/usr/bin/env python3
"""Merge the two re-extractions into the released corpus.

The released corpus is the deliverable; the re-runs correct part of it. So the
final corpus is the released file with the re-extracted rows substituted in --
same 804,427 samples, same columns, same order, different labels where a repair
changed one. Nothing is added and nothing is dropped, which is what makes the
corpus-wide evaluations recomputable rather than merely re-derivable.

Two substitution rules, and they are not the same:

* **Job B** re-extracted its samples end to end -- Phase 1, Phase 2 and the
  assembly step that reconciles Sex against the cell-line catalogue and against
  sex-specific anatomy. Every field is therefore newer than the released one and
  the whole row is replaced.

* **Job A** re-normalised acronyms only. It ran the repaired Phase 2 over the
  UNCHANGED Phase 1 output, and its input had Tissue and Treatment blanked so
  the run would not spend GPU on values it was not measuring. Its rows carry
  'Not Specified' in those fields as an artefact of that input, not as a
  finding. Copying the whole row would erase Tissue and Treatment for 55,858
  samples. Only the three Condition columns are taken.

Where a sample appears in both, Job B wins: it is the complete re-extraction,
and it ran on the final code.

Usage:
    build_final_corpus.py RELEASED_CSV_GZ JOBA_CSV_GZ JOBB_CSV_GZ OUT_CSV_GZ
"""

import csv
import gzip
import sys
from collections import Counter

COND = ("final_Condition", "final_Condition_id", "final_Condition_source")


def main() -> int:
    released, a_path, b_path, out_path = sys.argv[1:5]

    with gzip.open(b_path, "rt", newline="") as fh:
        job_b = {r["gsm"]: r for r in csv.DictReader(fh)}
    with gzip.open(a_path, "rt", newline="") as fh:
        job_a = {r["gsm"]: r for r in csv.DictReader(fh)
                 if r["gsm"] not in job_b}

    print(f"full-row substitution   : {len(job_b):,}")
    print(f"Condition-only          : {len(job_a):,}  "
          f"(after removing the overlap)")

    stats = Counter()
    seen = set()
    with gzip.open(released, "rt", newline="") as src, \
            gzip.open(out_path, "wt", newline="") as dst:
        reader = csv.DictReader(src)
        fields = list(reader.fieldnames)
        writer = csv.DictWriter(dst, fieldnames=fields)
        writer.writeheader()
        for row in reader:
            gsm = row["gsm"]
            if gsm in seen:
                raise RuntimeError(f"duplicate GSM in released corpus: {gsm}")
            seen.add(gsm)
            stats["total"] += 1

            b = job_b.get(gsm)
            if b is not None:
                changed = any((b.get(k) or "") != (row.get(k) or "")
                              for k in fields)
                for k in fields:
                    if k in b:
                        row[k] = b[k]
                stats["from_B"] += 1
                stats["changed_B"] += changed
            else:
                a = job_a.get(gsm)
                if a is not None:
                    changed = any((a.get(k) or "") != (row.get(k) or "")
                                  for k in COND)
                    for k in COND:
                        row[k] = a.get(k, row.get(k, ""))
                    stats["from_A"] += 1
                    stats["changed_A"] += changed
            writer.writerow(row)

    missing_b = set(job_b) - seen
    missing_a = set(job_a) - seen
    print(f"\nrows written            : {stats['total']:,}")
    print(f"  replaced in full      : {stats['from_B']:,} "
          f"(actually changed: {stats['changed_B']:,})")
    print(f"  Condition replaced    : {stats['from_A']:,} "
          f"(actually changed: {stats['changed_A']:,})")
    if missing_b or missing_a:
        print(f"  WARNING: absent from the corpus -- "
              f"B={len(missing_b)}, A={len(missing_a)}")
    print(f"\nwrote -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
