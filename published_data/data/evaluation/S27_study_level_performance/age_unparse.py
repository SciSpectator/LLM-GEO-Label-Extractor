                      

"""What the Age false negatives are made of.

Table S1's caption counts a missing OR unparseable output as a false negative,
so the Age column mixes two things. Separating them matters for the corrected
table: a sample the pipeline said nothing about and a sample it described in
words are different failures, and only the first is an absence.
"""



import csv

import gzip



import os as _os
PKG = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", ".."))

ALE = f"{PKG}/data/evaluation/S1_manual_benchmark/ale_curated_labels.tsv"

FINAL = f"{PKG}/data/06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz"

NS = {"", "not specified", "na", "none", "n/a", "unknown"}



ale = {}

with open(ALE) as fh:

    rd = csv.reader(fh, delimiter="\t")

    next(rd)

    for row in rd:

        if row and row[0] and len(row) > 2 and row[2].strip():

            ale[row[0]] = row[2].strip()



with gzip.open(FINAL, "rt") as fh:

    pred = {r["gsm"]: str(r.get("final_Age") or "").strip()

            for r in csv.DictReader(fh)}





def numeric(v):

    """Does the value carry a number an age can be read from?"""

    return any(ch.isdigit() for ch in v)





overlap = [g for g in ale if g in pred]

blank = [g for g in overlap if pred[g].lower() in NS]

worded = [g for g in overlap if pred[g].lower() not in NS and not numeric(pred[g])]

print(f"  ALE Age records in corpus      {len(overlap):,}")

print(f"    no output (Not Specified)    {len(blank):,}")

print(f"    output with no number in it  {len(worded):,}")

print(f"    numeric output               {len(overlap)-len(blank)-len(worded):,}")

print(f"    -> unavailable-for-scoring   {len(blank)+len(worded):,}"

      f"   (Table S1 Age FN after removing the double-booked FP: 123 - 64 = 59)")

from collections import Counter

print("\n  the worded outputs:")

for v, c in Counter(pred[g] for g in worded).most_common(12):

    print(f"    {c:>4}  {v!r}")

