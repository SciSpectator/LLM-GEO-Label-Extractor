"""Collect every distinct label value in the corpus with its instance count.

Instance counts drive all downstream prioritisation: model effort and review
order are allocated by how many samples a value affects, not by how many
distinct strings exist.
"""

import glob
import gzip
import json
import sys
from collections import Counter

sys.path.insert(0, "Directory to working data dir")
from phase2_normalize import LABEL_COLS

pattern = (
    sys.argv[1] if len(sys.argv) > 1 else "Directory to working data dir/p1_[0-7].json"
)
out = (
    sys.argv[2]
    if len(sys.argv) > 2
    else "Directory to working data dir/label_dictionary.json.gz"
)
phase = sys.argv[3] if len(sys.argv) > 3 else "phase1b"

freq = {c: Counter() for c in LABEL_COLS}
n = 0
for p in sorted(glob.glob(pattern)):
    op = gzip.open if p.endswith(".gz") else open
    with op(p, "rt") as fh:
        d = json.load(fh)
    rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
    for s in rows:
        n += 1
        src = s.get(phase) or {}
        for c in LABEL_COLS:
            v = str(src.get(c, "")).strip()
            if v:
                freq[c][v] += 1
    print(f"  {p}: {len(rows)} samples", flush=True)

with gzip.open(out, "wt") as fh:
    json.dump({c: dict(freq[c]) for c in LABEL_COLS}, fh)
print(f"{n} samples, " + ", ".join(f"{c}={len(freq[c])}" for c in LABEL_COLS))
print("wrote", out)
