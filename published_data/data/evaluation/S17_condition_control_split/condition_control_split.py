"""Split the Condition labels into explicit healthy/control and disease/non-control.

Usage:
    python3 condition_control_split.py [final_labels.csv.gz] [phase2_mesh.py] [out.csv]

Basis: the samples carrying a Condition label after Phase 1b, excluding the "unknown"
tokens that Table S3 counts as Not Specified.

A sample counts as explicit healthy/control when EVERY label in its final Condition cell
is one of the canonical control surfaces defined by _CONDITION_CONTROL_CANONICAL in
code/2_normalization_phase2/phase2_mesh.py. A sample carrying both a control label and a
disease label therefore counts as disease, not control.
"""

import csv
import gzip
import re
import sys

UNKNOWN = {"unknown"}
NOT_SPECIFIED = "not specified"


def control_surfaces(mesh_source_path):
    src = open(mesh_source_path).read()
    start = src.find("_CONDITION_CONTROL_CANONICAL")
    block = src[start:src.find("}", start) + 1]
    return {v.lower() for v in re.findall(r'"[^"]+":\s*"([^"]+)"', block)}


def main():
    corpus = sys.argv[1] if len(sys.argv) > 1 else "../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz"
    mesh_src = sys.argv[2] if len(sys.argv) > 2 else "../../../code/2_normalization_phase2/phase2_mesh.py"
    out = sys.argv[3] if len(sys.argv) > 3 else "condition_control_split.csv"

    surfaces = control_surfaces(mesh_src)
    total = basis = control = 0
    with gzip.open(corpus, "rt") as fh:
        for row in csv.DictReader(fh):
            total += 1
            phase1b = (row.get("phase1b_Condition") or "").strip()
            if not phase1b or phase1b.lower() == NOT_SPECIFIED or phase1b.lower() in UNKNOWN:
                continue
            basis += 1
            labels = [x.strip().lower() for x in (row.get("final_Condition") or "").split(";") if x.strip()]
            if labels and all(x in surfaces for x in labels):
                control += 1

    disease = basis - control
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "samples", "pct_of_corpus"])
        w.writerow(["condition_labels", basis, f"{100 * basis / total:.2f}"])
        w.writerow(["healthy_control", control, f"{100 * control / total:.2f}"])
        w.writerow(["disease_non_control", disease, f"{100 * disease / total:.2f}"])
        w.writerow(["corpus_total", total, "100.00"])

    print(f"{'control surfaces':<26}{len(surfaces):>10,}")
    print(f"{'condition labels (basis)':<26}{basis:>10,}{100 * basis / total:>9.2f}%")
    print(f"{'healthy/control':<26}{control:>10,}{100 * control / total:>9.2f}%")
    print(f"{'disease/non-control':<26}{disease:>10,}{100 * disease / total:>9.2f}%")
    print(f"{'corpus total':<26}{total:>10,}")


if __name__ == "__main__":
    main()
