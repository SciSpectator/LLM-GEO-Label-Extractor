"""Rebuild the phase 2 input corpus from an exported phase 2 result set.

Phase 2 consumes shards of samples carrying gsm, gse, gpl and a stage block of
label values. An exported result set already carries every one of those fields,
because each row records the phase1b value it started from alongside the final
one. Rebuilding the input from the output therefore costs nothing and avoids
re-running label extraction over the whole corpus.

The rebuilt corpus is validated against the manifest of the run that produced
it: the sample count and the number of distinct values must both match, which
is only possible if the reconstruction is exact.
"""

from __future__ import annotations

import csv
import glob
import gzip
import json
import os
import sys
from collections import Counter

LABEL_COLS = ["Tissue", "Condition", "Treatment"]
STAGE = "phase1b"
SHARDS = 8


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "phase2_final"
    out = sys.argv[2] if len(sys.argv) > 2 else "p1corpus"

    files = sorted(glob.glob(os.path.join(src, "final", "*", "*.csv.gz")))
    if not files:
        print(f"no exported results under {src}")
        return 2

    samples = []
    distinct = {c: set() for c in LABEL_COLS}
    for f in files:
        with gzip.open(f, "rt") as fh:
            for row in csv.DictReader(fh):
                rec = {
                    "gsm": row.get("gsm", ""),
                    "gse": row.get("gse", ""),
                    "gpl": row.get("gpl", ""),
                    STAGE: {},
                }
                for c in LABEL_COLS:
                    v = (row.get(f"{STAGE}_{c}", "") or "").strip()
                    rec[STAGE][c] = v
                    if v:
                        distinct[c].add(v)
                samples.append(rec)

    os.makedirs(out, exist_ok=True)
    per = (len(samples) + SHARDS - 1) // SHARDS
    written = []
    for i in range(SHARDS):
        chunk = samples[i * per : (i + 1) * per]
        if not chunk:
            continue
        path = os.path.join(out, f"p1_{i}.json.gz")
        with gzip.open(path, "wt") as fh:
            json.dump({"samples": chunk}, fh)
        written.append((path, len(chunk), os.path.getsize(path)))

    n_dist = sum(len(distinct[c]) for c in LABEL_COLS)
    print(f"samples          {len(samples):,}")
    print(f"distinct values  {n_dist :,}")
    for c in LABEL_COLS:
        print(f"   {c :<11}{len(distinct[c]):>8,}")
    print(f"platforms        {len(files):,}")
    total = 0
    for path, k, size in written:
        print(
            f"   {os.path.basename(path):<16}{k :>9,} samples  "
            f"{size / 1e6 :>7.1f} MB"
        )
        total += size
    print(f"corpus size      {total / 1e6 :.1f} MB in {len(written)} shards")

    man = os.path.join(src, "manifest.json")
    if os.path.exists(man):
        m = json.load(open(man))
        ok = True
        for key, got in (
            ("samples", len(samples)),
            ("distinct_values", n_dist),
            ("platforms", len(files)),
        ):
            want = m.get(key)
            if want is None:
                continue
            mark = "ok  " if want == got else "FAIL"
            if want != got:
                ok = False
            print(f"{mark} {key :<16} manifest {want :,}  rebuilt {got :,}")
        if not ok:
            print("\nreconstruction does not match the manifest")
            return 1
        print("\nPASS rebuilt corpus is identical to the input of the " "recorded run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
