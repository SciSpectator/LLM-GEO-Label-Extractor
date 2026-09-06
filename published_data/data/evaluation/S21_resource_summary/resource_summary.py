"""Count the distinct biomedical concepts the released corpus actually contains.

Usage:
    python3 resource_summary.py [final_labels.csv.gz] [out.csv]

Table S2 counts distinct raw values, that is, surface forms entering normalization. This
script counts distinct concepts leaving it: how many separate MeSH descriptors, Cellosaurus
cell lines and out-of-vocabulary concepts the corpus assigns, and how many samples each
group covers. The two are different quantities. Tissue routes 6,501 distinct raw values to
Cellosaurus, but those resolve to far fewer distinct cell lines, because many surface forms
name the same line.

A sample is counted once per group even when it carries several labels of that group.
"""

import csv
import gzip
import re
import sys
from collections import defaultdict

FIELDS = ("Tissue", "Condition", "Treatment")
GROUPS = ("MeSH", "Cellosaurus", "OOV")
MESH = re.compile(r"^D\d+$")


def group_of(identifier):
    if MESH.match(identifier):
        return "MeSH"
    if identifier.startswith("CVCL"):
        return "Cellosaurus"
    if identifier.startswith(("ART", "OOV")):
        return "OOV"
    return None


def main():
    corpus = sys.argv[1] if len(sys.argv) > 1 else "../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz"
    out = sys.argv[2] if len(sys.argv) > 2 else "resource_summary.csv"

    concepts = {f: defaultdict(set) for f in FIELDS}
    samples = {f: defaultdict(int) for f in FIELDS}
    total_samples = 0
    with gzip.open(corpus, "rt") as fh:
        for row in csv.DictReader(fh):
            total_samples += 1
            for field in FIELDS:
                present = set()
                for identifier in (row.get(f"final_{field}_id") or "").split(";"):
                    identifier = identifier.strip()
                    group = group_of(identifier)
                    if group:
                        concepts[field][group].add(identifier)
                        present.add(group)
                for group in present:
                    samples[field][group] += 1

    rows = []
    for field in FIELDS:
        for group in GROUPS:
            if concepts[field][group]:
                rows.append([field, group, len(concepts[field][group]),
                             samples[field][group],
                             f"{100 * samples[field][group] / total_samples:.2f}"])
    grand = sum(len(concepts[f][g]) for f in FIELDS for g in GROUPS)
    mesh_union = set().union(*[concepts[f]["MeSH"] for f in FIELDS])

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "vocabulary", "distinct_concepts", "samples", "pct_of_corpus"])
        w.writerows(rows)
        w.writerow(["all", "all", grand, "", ""])
        w.writerow(["all", "MeSH (union over fields)", len(mesh_union), "", ""])

    print(f"{'field':<11}{'vocabulary':<14}{'concepts':>10}{'samples':>11}{'% corpus':>10}")
    for r in rows:
        print(f"{r[0]:<11}{r[1]:<14}{r[2]:>10,}{r[3]:>11,}{r[4]:>10}")
    print(f"\n{'distinct concepts in the corpus':<40}{grand:>10,}")
    print(f"{'distinct MeSH descriptors (union)':<40}{len(mesh_union):>10,}")
    print(f"{'distinct Cellosaurus cell lines':<40}{len(concepts['Tissue']['Cellosaurus']):>10,}")


if __name__ == "__main__":
    main()
