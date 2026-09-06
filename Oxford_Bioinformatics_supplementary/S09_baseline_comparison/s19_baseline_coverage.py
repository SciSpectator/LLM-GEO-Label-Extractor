"""Reproduce the deterministic coverage rows of Table S13.

Both the exact-MeSH baseline and the Phase 2 pipeline are scored on the same
population: the sample labels the pipeline actually extracted, i.e. every sample
whose Phase 1b value for that field is not "Not Specified". This is the S13
denominator and it differs from Table S10, which reports the same assignments as
a fraction of all 804,427 corpus samples.

    exact_mesh  the raw Phase 1b label matches a MeSH descriptor name or entry
                synonym exactly (case-folded, whitespace-trimmed), with no
                retrieval, no LLM, and no controlled-vocabulary cascade.
    ours        the final label carries a MeSH descriptor id or a Cellosaurus
                cell-line id.

Usage:
    python3 s19_baseline_coverage.py <LLM_labels_all_samples.csv.gz> <mesh.sqlite> <out.csv>
"""

import csv
import gzip
import re
import sqlite3
import sys

FIELDS = ["Tissue", "Condition", "Treatment"]
MESH_ID = re.compile(r"^D\d+$")


def mesh_surface_forms(mesh_sqlite):
    con = sqlite3.connect(mesh_sqlite)
    forms = {n.strip().lower() for (n,) in con.execute("SELECT name FROM mesh_terms") if n}
    forms |= {s.strip().lower() for (s,) in con.execute("SELECT synonym FROM mesh_synonyms") if s}
    con.close()
    return forms


NOT_A_LABEL = {"", "not specified", "unknown", "unspecified", "na", "n/a", "none"}


def is_db_resolved(id_cell):
    ids = [x.strip() for x in (id_cell or "").split(";") if x.strip()]
    return any(MESH_ID.match(i) or i.startswith("CVCL") for i in ids)


def main(labels_gz, mesh_sqlite, out_csv):
    forms = mesh_surface_forms(mesh_sqlite)
    extracted = {f: 0 for f in FIELDS}
    exact_mesh = {f: 0 for f in FIELDS}
    ours = {f: 0 for f in FIELDS}
    total = 0

    with gzip.open(labels_gz, "rt") as fh:
        for row in csv.DictReader(fh):
            total += 1
            for f in FIELDS:
                raw = (row["phase1b_%s" % f] or "").strip()
                if not raw or raw.lower() in NOT_A_LABEL:
                    continue
                extracted[f] += 1
                if raw.lower() in forms:
                    exact_mesh[f] += 1
                if is_db_resolved(row["final_%s_id" % f]):
                    ours[f] += 1

    with open(out_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "corpus_samples", "labels_extracted",
                    "exact_mesh_resolved", "exact_mesh_coverage",
                    "ours_resolved", "ours_coverage"])
        for f in FIELDS:
            w.writerow([f, total, extracted[f],
                        exact_mesh[f], round(exact_mesh[f] / extracted[f], 4),
                        ours[f], round(ours[f] / extracted[f], 4)])

    for f in FIELDS:
        print("%-10s labels_extracted=%7d  exact_mesh=%.3f  ours=%.3f"
              % (f, extracted[f], exact_mesh[f] / extracted[f], ours[f] / extracted[f]))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
