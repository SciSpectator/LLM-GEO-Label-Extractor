#!/usr/bin/env python3
"""Build the merged Phase 2 output directory the released evaluations read.

The manuscript's Phase 2 tables are produced by phase2_evaluation.py, which
reads a Phase 2 output directory -- per-sample files under final/<GPL>/ and a
per-value dictionary -- not the assembled label file. To recompute those tables
on the corrected corpus, that directory has to exist in the corrected form.

So the released output is copied with the re-extracted assignments substituted
in, by the same two rules used for the label file:

* Job B replaced whole rows: it re-extracted its samples end to end.
* Job A replaced Condition only: it re-normalised acronyms over the UNCHANGED
  Phase 1 output, with Tissue and Treatment blanked in its input so the run
  would not spend GPU on values it was not measuring. Those fields carry
  'Not Specified' as an artefact of that input, not as a finding.

The dictionary is per VALUE, and a value is only rewritten when the re-runs
agree on what it now resolves to. Where they disagree -- the same surface
resolving to different concepts in different studies, which is what per-study
resolution is for -- the released entry is kept and the disagreement is left to
the per-sample files, which are the authoritative record. A dictionary cannot
represent one surface having several answers, so it is not made to pretend.

Usage:
    build_final_phase2_out.py RELEASED_DIR JOBA_DIR JOBB_CSV OUT_DIR
"""

import collections
import csv
import glob
import gzip
import json
import os
import shutil
import sys

COND = ("final_Condition", "final_Condition_id", "final_Condition_source")
COLS = ("Tissue", "Condition", "Treatment")


def main() -> int:
    rel_dir, a_dir, b_csv, out_dir = sys.argv[1:5]

    with gzip.open(b_csv, "rt", newline="") as fh:
        job_b = {r["gsm"]: r for r in csv.DictReader(fh)}

    job_a = {}
    for f in sorted(glob.glob(os.path.join(a_dir, "final", "*", "*.csv.gz"))):
        with gzip.open(f, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["gsm"] not in job_b:
                    job_a[r["gsm"]] = r
    print(f"full-row substitution: {len(job_b):,} samples")
    print(f"Condition-only substitution: {len(job_a):,} samples")

    os.makedirs(out_dir, exist_ok=True)
    n_rows = n_b = n_a = 0
    for src in sorted(glob.glob(os.path.join(rel_dir, "final", "*", "*.csv.gz"))):
        gpl = os.path.basename(os.path.dirname(src))
        dst_dir = os.path.join(out_dir, "final", gpl)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(src))
        with gzip.open(src, "rt", newline="") as fh_in, \
                gzip.open(dst, "wt", newline="") as fh_out:
            reader = csv.DictReader(fh_in)
            fields = list(reader.fieldnames)
            writer = csv.DictWriter(fh_out, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                gsm = row["gsm"]
                b = job_b.get(gsm)
                if b is not None:
                    for k in fields:
                        if k in b:
                            row[k] = b[k]
                    n_b += 1
                else:
                    a = job_a.get(gsm)
                    if a is not None:
                        for k in COND:
                            if k in a:
                                row[k] = a[k]
                        n_a += 1
                writer.writerow(row)
                n_rows += 1
    print(f"per sample: {n_rows:,} rows | full-row {n_b:,} | Condition-only {n_a:,}")

    # ---- dictionary -------------------------------------------------------
    with gzip.open(os.path.join(rel_dir, "dictionary.json.gz"), "rt") as fh:
        released = json.load(fh)

    # what each raw value now resolves to, per column, across the re-runs
    seen = {c: collections.defaultdict(collections.Counter) for c in COLS}
    for rows, cols in ((job_b.values(), COLS), (job_a.values(), ("Condition",))):
        for r in rows:
            for col in cols:
                raw = (r.get(f"phase1b_{col}") or "").strip()
                ident = (r.get(f"final_{col}_id") or "").strip()
                if raw and ident:
                    seen[col][raw][(ident, r.get(f"final_{col}") or "",
                                    r.get(f"final_{col}_source") or "")] += 1

    rewritten = ambiguous = 0
    for col in COLS:
        entries = released.get(col) or {}
        for raw, votes in seen[col].items():
            if raw not in entries:
                continue
            if len(votes) > 1:
                ambiguous += 1          # per-study disagreement: leave it alone
                continue
            (ident, target, source), _ = votes.most_common(1)[0]
            rec = entries[raw]
            if rec.get("id") != ident:
                rec["id"], rec["target"], rec["source"] = ident, target, source
                rewritten += 1
    print(f"dictionary: {rewritten:,} values rewritten | "
          f"{ambiguous:,} left as-is (the surface means different things in different studies)")

    with gzip.open(os.path.join(out_dir, "dictionary.json.gz"), "wt") as fh:
        json.dump(released, fh)
    print(f"\nwrote -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
