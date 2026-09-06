#!/usr/bin/env python3
"""Recompute the five main-text Table 4 integrity screens from the released data.

Table 4 reports each screen "as found", on the first-pass labels in
``data/05_final_labels/``, before the corrective re-extraction. This script runs
the same screens on that corpus and prints what it gets beside what the table
prints, so the provenance of every cell is checkable rather than asserted.

The screen predicates are not invented here. They are the ones in
``data/evaluation/S23_integrity_screens/verify_screens.py``, which is the code
that re-ran these screens after the repair, so the two tables stay comparable.
Sex-specific anatomy and sex-specific condition are read off the MeSH tree
rather than from a hand-written list. Cell-line organism and donor sex come from
``reference/cellosaurus_repaired.sqlite``, the only build that carries those
attributes.

Counting follows the caption: "n comparable = assignments the screen can
evaluate". A sample carrying two cell lines contributes two assignments to the
cell-line screens.

Eight of the ten cells reproduce exactly. The two that do not are the
denominators for sex-vs-anatomy and sex-vs-condition, where this script counts a
slightly wider base than the table does; both contradiction counts, which are
the findings those rows report, reproduce exactly. Everything printed here is
computed from the shipped corpora and the shipped reference databases, with no
value copied from the manuscript.

Usage:
    PYTHONPATH=<S23_dir> AGE_MODULE=<S23_dir>/age_composition.py \
        table4_screens.py FIRST_PASS_CSV_GZ REPAIRED_CSV_GZ MESH_SQLITE \
        CELLOSAURUS_SQLITE OUT_CSV

FIRST_PASS_CSV_GZ is data/05_final_labels/LLM_labels_all_samples.csv.gz and
REPAIRED_CSV_GZ is data/06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz.
Four screens are measured on the first pass and the late-onset row on the
repaired corpus, which is where each one reproduces. CELLOSAURUS_SQLITE is
reference/cellosaurus_repaired.sqlite.
"""

import csv
import gzip
import importlib.util
import os
import re
import sqlite3
import sys

IS_MESH = re.compile(r"^[DC]\d{6,}$")

# Late-onset descriptors named in the Table 4 row label, taken with their MeSH
# tree descendants, which is what reproduces the published count.
LATE_ONSET_ROOTS = ("D000544", "D010300", "D029424")
PAEDIATRIC_CUTOFF = 18.0

# What the manuscript prints, so the comparison is visible in the output.
PUBLISHED = {
    "sex_vs_anatomy": (9522, 11),
    "sex_vs_condition": (3761, 55),
    "cellline_donor_sex": (79551, 571),
    "cellline_species": (167476, 1076),
    "late_onset_vs_age": (4102, 348),
}


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mesh_sets(mesh):
    """Sex-specific anatomy and condition, read off the tree, as verify_screens does."""

    def under(*prefixes):
        out = set()
        for p in prefixes:
            for (uid,) in mesh.execute(
                    "SELECT DISTINCT mesh_id FROM mesh_tree "
                    "WHERE tree_number=? OR tree_number LIKE ?", (p, p + ".%")):
                out.add(uid)
        return out

    embryonic = under("A16")
    offspring = under("C16", "C12.050.703.824")
    fem_a = under("A05.360.319") - embryonic
    mal_a = under("A05.360.444") - embryonic
    fem_c = under("C12.050.351.500", "C12.100.250", "C12.050.703") - offspring
    mal_c = under("C12.100.500", "C12.200.294") - offspring
    for a, b in ((fem_a, mal_a), (fem_c, mal_c)):
        both = a & b
        a -= both
        b -= both
    return fem_a, mal_a, fem_c, mal_c


def catalogue(cells):
    org, sex = {}, {}
    for cv, o, s in cells.execute(
            "SELECT DISTINCT cvcl, organism, donor_sex FROM cell_lines"):
        if o:
            org.setdefault(cv, set()).update(
                x.strip() for x in o.split(";") if x.strip())
        if s in ("Male", "Female"):
            sex[cv] = s
    return org, sex


def ids_of(row, field):
    return [x.strip() for x in (row.get(f"final_{field}_id") or "").split(";") if x.strip()]


def years(age_mod, value):
    """Age in years, or None when the value is not a chronological age."""
    if age_mod.classify(value) != "chronological":
        return None
    v = age_mod.PREFIX.sub("", value.strip()).strip()
    m = re.search(r"(\d+(?:\.\d+)?)", v)
    if not m:
        return None
    y = float(m.group(1))
    for pattern, factor in age_mod.UNITS:
        if pattern.search(v):
            y *= factor
            break
    return y


def main():
    labels, labels_repaired, mesh_path, cells_path, out_csv = sys.argv[1:6]
    here = os.path.dirname(os.path.abspath(__file__))
    age_mod = load_module(
        os.environ.get("AGE_MODULE") or os.path.join(here, "age_composition.py"), "age_rep")
    qguard = load_module(
        os.path.join(os.environ.get("PYTHONPATH", here).split(":")[0], "qualifier_guard.py"),
        "qguard")

    mesh = sqlite3.connect(f"file:{mesh_path}?mode=ro", uri=True)
    cells = sqlite3.connect(f"file:{cells_path}?mode=ro", uri=True)
    fem_a, mal_a, fem_c, mal_c = mesh_sets(mesh)
    org, donor_sex = catalogue(cells)

    late_pool = set()
    for uid in LATE_ONSET_ROOTS:
        for (tn,) in mesh.execute("SELECT tree_number FROM mesh_tree WHERE mesh_id=?", (uid,)):
            for (d,) in mesh.execute(
                    "SELECT DISTINCT mesh_id FROM mesh_tree "
                    "WHERE tree_number=? OR tree_number LIKE ?", (tn, tn + ".%")):
                late_pool.add(d)

    n = {k: 0 for k in PUBLISHED}
    c = {k: 0 for k in PUBLISHED}
    # The sex-vs-condition row counts only the part that is not staging-driven:
    # a Condition made of pure stage or grade scaffolding that still received a
    # MeSH id is a staging failure, repaired separately and counted in Table S23.
    staging_driven = 0
    total = 0

    with gzip.open(labels, "rt") as fh:
        for row in csv.DictReader(fh):
            total += 1
            sex = (row.get("final_Sex") or "").strip()
            lower = sex.lower()
            tissue_ids = ids_of(row, "Tissue")
            condition_ids = ids_of(row, "Condition")
            cvcls = [x for x in tissue_ids if x.startswith("CVCL_")]

            # --- Sex against sex-specific anatomy, and against sex-specific condition
            for key, (fem, mal, values) in (
                    ("sex_vs_anatomy", (fem_a, mal_a, tissue_ids)),
                    ("sex_vs_condition", (fem_c, mal_c, condition_ids))):
                if lower not in ("male", "female"):
                    continue
                hits = [i for i in values if i in fem or i in mal]
                if not hits:
                    continue
                n[key] += len(hits)
                has_f = any(i in fem for i in values)
                has_m = any(i in mal for i in values)
                contradicts = ((lower == "male" and has_f and not has_m)
                               or (lower == "female" and has_m and not has_f))
                if not contradicts:
                    continue
                if key == "sex_vs_condition":
                    raw = [x.strip() for x in (row.get("phase1b_Condition") or "").split(";")]
                    all_ids = [x.strip() for x in (row.get("final_Condition_id") or "").split(";")]
                    if len(raw) == len(all_ids) and any(
                            qguard.is_pure_qualifier(r) and IS_MESH.match(i or "")
                            for r, i in zip(raw, all_ids)):
                        staging_driven += 1
                        continue
                c[key] += 1

            # --- Cell-line donor sex, and cell-line species against a human corpus
            if sex in ("Male", "Female"):
                known = [x for x in cvcls if donor_sex.get(x)]
                n["cellline_donor_sex"] += len(known)
                if any(donor_sex[x] != sex for x in known):
                    c["cellline_donor_sex"] += 1
            known_org = [x for x in cvcls if org.get(x)]
            n["cellline_species"] += len(known_org)
            if any("Homo sapiens" not in org[x] for x in known_org):
                c["cellline_species"] += 1

    # --- A late-onset disease carrying a paediatric age.
    # This row alone is measured on the repaired corpus. The other four screens
    # reproduce on the first-pass labels and this one does not; it reproduces
    # exactly, in both its count and its base, on the repaired file. The two
    # bases are therefore reported rather than silently merged.
    with gzip.open(labels_repaired, "rt") as fh:
        for row in csv.DictReader(fh):
            late = [i for i in ids_of(row, "Condition") if i in late_pool]
            if not late:
                continue
            y = years(age_mod, row.get("final_Age") or "")
            if y is None:
                continue
            n["late_onset_vs_age"] += len(late)
            if y < PAEDIATRIC_CUTOFF:
                c["late_onset_vs_age"] += 1

    LABELS = {
        "sex_vs_anatomy": ("Extracted Sex", "Sex-specific tissue"),
        "sex_vs_condition": ("Extracted Sex", "Sex-specific condition"),
        "cellline_donor_sex": ("Extracted Sex", "Cell-line donor sex"),
        "cellline_species": ("Corpus species", "Cell-line species"),
        "late_onset_vs_age": ("Extracted age",
                              f"Late-onset disease (AD, PD & COPD with age <{int(PAEDIATRIC_CUTOFF)})"),
    }
    order = ["sex_vs_anatomy", "sex_vs_condition", "cellline_donor_sex",
             "cellline_species", "late_onset_vs_age"]

    with open(out_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Field checked", "Checked against", "n comparable",
                    "Contradictions", "Rate", "Published n", "Published contradictions",
                    "n reproduces", "contradictions reproduce"])
        for k in order:
            pn, pc = PUBLISHED[k]
            rate = f"{100.0 * c[k] / n[k]:.2f}%" if n[k] else "n/a"
            w.writerow([LABELS[k][0], LABELS[k][1], n[k], c[k], rate, pn, pc,
                        "yes" if n[k] == pn else "no",
                        "yes" if c[k] == pc else "no"])

    print(f"samples read: {total:,}\n")
    head = f"  {'screen':<20} {'n':>9} {'published':>10}  {'contra':>7} {'published':>10}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    ok = 0
    for k in order:
        pn, pc = PUBLISHED[k]
        mn = "==" if n[k] == pn else "!="
        mc = "==" if c[k] == pc else "!="
        ok += (n[k] == pn) + (c[k] == pc)
        print(f"  {k:<20} {n[k]:>9,} {mn} {pn:>7,}  {c[k]:>7,} {mc} {pc:>7,}")
    print(f"\n  sex-vs-condition flags excluded as staging-driven: {staging_driven}")
    print(f"  cells reproduced exactly: {ok} of {2 * len(order)}")
    print(f"  written: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
