"""Deterministic post-Phase-2 within-sample de-duplication.

Phase 1 extracts one or more spans per field; Phase 2 normalises each span, so when two different
spans of the same sample denote the SAME concept they yield a repeated identifier in that sample's
multi-label cell (e.g. 'Carcinoma, Renal Cell; Carcinoma, Renal Cell'). This step collapses repeated
IDENTICAL ids within each cell to a single occurrence, preserving first-occurrence order. Different
ids (including Cellosaurus vs MeSH) and 'Not Specified' entries are left untouched; no concept is
lost. Applies to the final per-sample corpus; no Phase 2 re-run and no model needed.

Usage: python3 phase2_dedup.py IN.csv.gz OUT.csv.gz
"""

import csv, gzip, sys

FIELDS = ("Tissue", "Condition", "Treatment")


def dedup_cell(labels: str, ids: str):
    L = [x.strip() for x in labels.split(";")]
    I = [x.strip() for x in ids.split(";")]
    if len(L) != len(I) or len(I) <= 1:
        return labels, ids
    seen, outL, outI = set(), [], []
    for lab, idv in zip(L, I):
        if idv and idv in seen:
            continue
        if idv:
            seen.add(idv)
        outL.append(lab)
        outI.append(idv)
    return "; ".join(outL), "; ".join(outI)


def main(src, dst):
    with gzip.open(src, "rt", newline="") as fi, gzip.open(dst, "wt", newline="") as fo:
        r = csv.DictReader(fi)
        w = csv.DictWriter(fo, fieldnames=r.fieldnames)
        w.writeheader()
        for row in r:
            for F in FIELDS:
                lk, ik = f"final_{F}", f"final_{F}_id"
                if lk in row and ik in row and ";" in row[ik]:
                    row[lk], row[ik] = dedup_cell(row[lk], row[ik])
            w.writerow(row)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
