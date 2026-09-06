"""Out-of-vocabulary (OOV) concept consolidation — Supplementary Table S8 (deterministic).

Measured on the pre-case-fold dictionary snapshot (after BioLORD + LLM similarity
consolidation, before the final surface case-fold step of Table S12). For each field, over
the raw values that Phase 2 left OOV (source == "oov"):

  OOV values           distinct raw label values assigned an OOV concept id
  Distinct concepts    distinct OOV concept ids they were consolidated into
  Variants folded      OOV values - Distinct concepts (how many surface variants merged)
  Multi-variant clusters   OOV concepts backed by >= 2 distinct raw values

100% coverage, no LLM, no gold. Reads the shipped pre-fold dictionary snapshot.
"""

import gzip, json, csv, collections, os, sys

DICT = "Directory to pre-fold dictionary_prefold.json.gz"
OUT = "Directory to output"

HERE = os.path.dirname(os.path.abspath(__file__))
if DICT.startswith("Directory to"):
    DICT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "dictionary_prefold.json.gz")
if OUT.startswith("Directory to"):
    OUT = sys.argv[2] if len(sys.argv) > 2 else HERE

FIELDS = ["Tissue", "Condition", "Treatment"]


def main():
    d = json.load(gzip.open(DICT, "rt"))
    rows = []
    tot = [0, 0, 0, 0]
    for F in FIELDS:
        by_id = collections.defaultdict(int)
        oov_values = 0
        for _raw, v in d[F].items():
            if v.get("source") == "oov":
                oov_values += 1
                by_id[str(v["id"])] += 1
        distinct = len(by_id)
        folded = oov_values - distinct
        multivar = sum(1 for c in by_id.values() if c >= 2)
        rows.append([F, oov_values, distinct, folded, multivar])
        for i, x in enumerate([oov_values, distinct, folded, multivar]):
            tot[i] += x
    rows.append(["Total"] + tot)
    with open(f"{OUT}/oov_consolidation.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "field",
                "oov_values",
                "distinct_concepts",
                "variants_folded",
                "multi_variant_clusters",
            ]
        )
        w.writerows(rows)
    for r in rows:
        print(
            f"{r[0]:10} values={r[1]:7,} distinct={r[2]:7,} folded={r[3]:6,} multivar={r[4]:6,}"
        )
    print("wrote oov_consolidation.csv to", OUT)


if __name__ == "__main__":
    main()
