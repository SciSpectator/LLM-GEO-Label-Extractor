                      

"""Check the corrected Sex and Age rows of Table S1 against the data.

The corrected false-negative counts were obtained by subtracting the false
positives back out of the published FN column, which is arithmetic on the
paper's own numbers. That is worth confirming from the corpus itself, because
the whole point of the correction is that a count should mean one thing: a
false negative here has to be a sample the ALE curation labelled and the
pipeline returned as Not Specified, nothing else.
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

    head = next(rd)

    for row in rd:

        if not row or not row[0]:

            continue

        ale[row[0]] = {"age": (row[2] if len(row) > 2 else "").strip(),

                       "gender": (row[3] if len(row) > 3 else "").strip()}

print(f"  ALE records: {len(ale):,}")



with gzip.open(FINAL, "rt") as fh:

    rd = csv.DictReader(fh)

    cols = rd.fieldnames

    sex_col = next((c for c in cols if c.lower() in

                    ("final_sex", "sex", "final_sex_value")), None)

    age_col = next((c for c in cols if c.lower() in

                    ("final_age", "age", "final_age_value")), None)

    gsm_col = next((c for c in cols if c.lower() in ("gsm", "sample", "gsm_id")), None)

    print(f"  columns used: {gsm_col!r}, {sex_col!r}, {age_col!r}")

    pred = {}

    for r in rd:

        pred[r[gsm_col]] = (str(r.get(sex_col) or "").strip(),

                            str(r.get(age_col) or "").strip())

print(f"  corpus samples: {len(pred):,}\n")



for field, key, idx in (("Sex", "gender", 0), ("Age", "age", 1)):

    overlap = [g for g, v in ale.items() if v[key] and g in pred]

    blank = sum(1 for g in overlap if pred[g][idx].lower() in NS)

    print(f"  {field}: ALE-curated records present in the corpus  {len(overlap):,}")

    print(f"        of those, pipeline returned Not Specified    {blank:,}"

          f"   ({100*blank/max(len(overlap),1):.2f}%)")

    print(f"        pipeline returned a value                    {len(overlap)-blank:,}\n")

