"""Deterministic evaluation of Phase 2 controlled-vocabulary normalization.

Reproduces every gold-free metric reported for the 804,427-sample corpus:
database-resolution precision/recall/F1, bare-acronym expansion recall,
per-vocabulary match precision, MeSH tree-branch validity, identical-input
consistency, fragmentation collapse, and corpus composition. All metrics are
deterministic and computed at 100% coverage from the released output; no LLM
and no manual labels are used.

Inputs
    results/dictionary.json.gz          per-value assignments {target,id,source,count}
    results/final/<GPL>/<GPL>.csv.gz    per-sample assignments
    mesh.sqlite                         mesh_terms, mesh_synonyms, mesh_tree
    cellosaurus.sqlite                  cell_lines(name, cvcl, primary_name)

Usage
    python3 phase2_evaluation.py RESULTS_DIR MESH_SQLITE CELLOSAURUS_SQLITE OUT_DIR

Outputs (OUT_DIR)
    db_resolution.csv, acronym_expansion.csv, vocabulary_precision.csv,
    branch_validity.csv, consistency.csv, composition.csv
"""

import csv, glob, gzip, json, os, re, sqlite3, sys
from collections import Counter, defaultdict

FIELDS = ("Tissue", "Condition", "Treatment")
BRANCH = {"Tissue": ("A",), "Condition": ("C", "F03"), "Treatment": ("D", "E02")}
NS = {
    "",
    "not specified",
    "na",
    "n/a",
    "none",
    "unknown",
    "not applicable",
    "unspecified",
}

ACRONYM_GOLD = {
    "NSCLC": "non-small-cell lung carcinoma",
    "AML": "leukemia, myeloid, acute",
    "SLE": "lupus erythematosus, systemic",
    "CLL": "leukemia, lymphocytic, chronic, b-cell",
    "GBM": "glioblastoma",
    "AD": "alzheimer disease",
    "CRC": "colorectal neoplasms",
    "MS": "multiple sclerosis",
    "COPD": "pulmonary disease, chronic obstructive",
    "RA": "arthritis, rheumatoid",
    "HCC": "carcinoma, hepatocellular",
    "RCC": "carcinoma, renal cell",
    "ALS": "amyotrophic lateral sclerosis",
    "IBD": "inflammatory bowel diseases",
    "UC": "colitis, ulcerative",
    "PD": "parkinson disease",
    "HD": "huntington disease",
    "MDS": "myelodysplastic syndromes",
    "TNBC": "triple negative breast neoplasms",
    "IPF": "idiopathic pulmonary fibrosis",
    "PCOS": "polycystic ovary syndrome",
    "OA": "osteoarthritis",
    "T1D": "diabetes mellitus, type 1",
    "T2D": "diabetes mellitus, type 2",
    "CKD": "renal insufficiency, chronic",
    "ASD": "autism spectrum disorder",
    "MDD": "depressive disorder, major",
    "TB": "tuberculosis",
    "CF": "cystic fibrosis",
    "ALL": "precursor cell lymphoblastic leukemia-lymphoma",
    "CML": "leukemia, myelogenous, chronic, bcr-abl positive",
    "PDAC": "carcinoma, pancreatic ductal",
    "NASH": "non-alcoholic fatty liver disease",
    "CD": "crohn disease",
    "MI": "myocardial infarction",
    "CAD": "coronary artery disease",
    "HF": "heart failure",
    "PAH": "pulmonary arterial hypertension",
    "AF": "atrial fibrillation",
    "BD": "bipolar disorder",
}


def canon(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def is_ns(v):
    return str(v).strip().lower() in NS


def id_parts(idstr):
    return [p.strip() for p in re.split("[;,]", idstr or "") if p.strip()]


def target_type(idstr):
    parts = id_parts(idstr)
    if not parts:
        return "uncovered"
    if any(re.match(r"[DC]\d", p) for p in parts):
        return "mesh"
    if any(p.startswith("CVCL") for p in parts):
        return "cellosaurus"
    if any(p.startswith(("OOV", "ART")) for p in parts):
        return "oov"
    return "uncovered"


def load_mesh(path):
    con = sqlite3.connect(path)
    by_term = defaultdict(set)
    for mid, name in con.execute("SELECT id, name FROM mesh_terms"):
        by_term[canon(name)].add(mid)
    for mid, syn in con.execute("SELECT mesh_id, synonym FROM mesh_synonyms"):
        by_term[canon(syn)].add(mid)
    tree = defaultdict(set)
    for tn, mid in con.execute("SELECT tree_number, mesh_id FROM mesh_tree"):
        tree[mid].add(tn)
    return by_term, tree


def load_cellosaurus(path):
    con = sqlite3.connect(path)
    by_name = defaultdict(set)
    for name, cvcl, _p in con.execute(
        "SELECT name, cvcl, primary_name FROM cell_lines"
    ):
        if len(canon(name)) >= 3:
            by_name[canon(name)].add(cvcl)
    return by_name


def in_branch(mid, field, tree):
    return any(tn.startswith(p) for tn in tree.get(mid, ()) for p in BRANCH[field])


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if (p == p and r == r and p + r) else float("nan")
    a = tp / max(tp + fp + fn, 1)
    return round(p, 4), round(r, 4), round(f, 4), round(a, 4)


def gse_counts(results):
    """Per (field, raw label) how many samples each study contributes.

    Phase 2 resolves a short form inside the study that used it, so a value can
    be left out-of-vocabulary corpus-wide while most samples carrying it were
    resolved in their own GSE. Scoring an instance-weighted metric therefore
    needs the study each sample belongs to, not just the value's total count.
    """
    per = defaultdict(Counter)
    for fp in glob.glob(os.path.join(results, "final", "**", "*.csv.gz"), recursive=True):
        with gzip.open(fp, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                for field in FIELDS:
                    for comp in (r.get(f"phase1b_{field}") or "").split(";"):
                        comp = comp.strip()
                        if comp:
                            per[(field, comp)][r["gse"]] += 1
    return per


def db_resolution(dictionary, mesh_by_term, tree, cello_by_name, per_gse=None):
    """Track A: on values whose canonical form has an exact in-branch MeSH or
    Cellosaurus answer, score the assignment (TP=correct id, FP=different id,
    FN=out-of-vocabulary).

    The value weight scores the dictionary's corpus-wide entry, one vote per
    distinct value. The instance weight scores each sample under the decision
    that applied in its own study, read from the entry's `by_gse` map; without
    that, a value withheld corpus-wide but resolved per study is charged its
    whole count as a miss (Tissue FN 10,893 rather than 802).
    """
    per_gse = per_gse or {}
    rows = []
    for field in FIELDS:
        cv, ci = Counter(), Counter()
        for raw, v in dictionary[field].items():
            gold = {
                m
                for m in mesh_by_term.get(canon(raw), set())
                if in_branch(m, field, tree)
            }
            if field == "Tissue":
                gold |= cello_by_name.get(canon(raw), set())
            if not gold:
                continue

            def verdict(entry):
                pids = id_parts(entry.get("id"))
                db_pids = [
                    p for p in pids if re.match(r"[DC]\d", p) or p.startswith("CVCL")
                ]
                return (
                    "TP" if any(p in gold for p in pids)
                    else ("FP" if db_pids else "FN")
                )

            cv[verdict(v)] += 1

            by_gse = v.get("by_gse") or {}
            studies = per_gse.get((field, raw))
            total = sum(studies.values()) if studies else 0
            if not (by_gse and studies and total):
                ci[verdict(v)] += v["count"]
                continue
            # Split the value's count over its studies, each scored under its
            # own decision. Distributed proportionally so the instance total
            # stays the count Phase 2 recorded; the largest share absorbs the
            # rounding so TP+FP+FN still equals it exactly.
            share, assigned = {}, 0
            for gse, n in studies.items():
                k = v["count"] * n // total
                share[gse] = k
                assigned += k
            if share:
                share[max(share, key=lambda g: studies[g])] += v["count"] - assigned
            for gse, n in share.items():
                ci[verdict(by_gse.get(gse, v))] += n
        for weight, c in (("value", cv), ("instance", ci)):
            p, r, f, a = prf(c["TP"], c["FP"], c["FN"])
            rows.append(
                dict(
                    field=field,
                    weight=weight,
                    answerable=c["TP"] + c["FP"] + c["FN"],
                    TP=c["TP"],
                    FP=c["FP"],
                    FN=c["FN"],
                    precision=p,
                    recall=r,
                    F1=f,
                    accuracy=a,
                )
            )
    return rows


def acronym_expansion(dictionary, mesh_by_term, tree):
    """Track B: bare disease acronyms expanded to their MeSH descriptor via
    study context (TP), a wrong descriptor (FP), or left out-of-vocabulary (FN)."""
    gold = {
        a: {
            m
            for m in mesh_by_term.get(canon(full), set())
            if in_branch(m, "Condition", tree)
        }
        for a, full in ACRONYM_GOLD.items()
    }
    gold = {a: ids for a, ids in gold.items() if ids}
    tp = fp = fn = 0
    rows = []
    for a, ids in sorted(gold.items()):
        v = dictionary["Condition"].get(a) or dictionary["Condition"].get(a.upper())
        if not v:
            continue
        pids = id_parts(v.get("id"))
        outcome = (
            "resolved"
            if any(p in ids for p in pids)
            else (
                "wrong"
                if any(re.match(r"[DC]\d", p) or p.startswith("CVCL") for p in pids)
                else "not_resolved"
            )
        )
        n = v["count"]
        tp += n * (outcome == "resolved")
        fp += n * (outcome == "wrong")
        fn += n * (outcome == "not_resolved")
        rows.append(
            dict(acronym=a, samples=n, outcome=outcome, target=v.get("target", ""))
        )
    p, r, f, _ = prf(tp, fp, fn)
    return {"TP": tp, "FP": fp, "FN": fn, "recall": r, "precision": p}, rows


def vocabulary_precision(dictionary, mesh_by_term, tree, cello_by_name):
    rows = []
    for field in FIELDS:
        c = Counter()
        for raw, v in dictionary[field].items():
            ids = id_parts(v.get("id"))
            cr = canon(raw)
            ct = canon(v.get("target", ""))
            cvcl = [p for p in ids if p.startswith("CVCL")]
            mesh = [p for p in ids if re.match(r"[DC]\d", p)]
            oov = [p for p in ids if p.startswith(("OOV", "ART"))]
            if cvcl:
                c["cvcl"] += 1
                if cvcl[0] in cello_by_name.get(cr, set()) or cvcl[
                    0
                ] in cello_by_name.get(ct, set()):
                    c["cvcl_ok"] += 1
            elif mesh:
                c["mesh"] += 1
                c["mesh_ok"] += any(
                    m in (mesh_by_term.get(cr, set()) | mesh_by_term.get(ct, set()))
                    for m in mesh
                )
            elif oov:
                c["oov"] += 1
                mhit = {
                    m for m in mesh_by_term.get(cr, set()) if in_branch(m, field, tree)
                }
                chit = cello_by_name.get(cr) if field == "Tissue" else None
                c["oov_inappropriate"] += 1 if (mhit or chit) else 0
        rows.append(
            dict(
                field=field,
                mesh=c["mesh"],
                mesh_verified=c["mesh_ok"],
                cellosaurus=c["cvcl"],
                cellosaurus_verified=c["cvcl_ok"],
                oov=c["oov"],
                oov_inappropriate=c["oov_inappropriate"],
            )
        )
    return rows


def branch_and_consistency(dictionary, tree):
    rows_b, rows_c = [], []
    for field in FIELDS:
        chk_i = bad_i = chk_v = bad_v = 0
        groups = defaultdict(list)
        for raw, v in dictionary[field].items():
            if target_type(v.get("id")) == "mesh":
                ids = [p for p in id_parts(v["id"]) if re.match(r"[DC]\d", p)]
                pref = set().union(*[tree.get(i, set()) for i in ids]) if ids else set()
                if pref:
                    chk_v += 1
                    chk_i += v["count"]
                    ok = any(tn.startswith(pr) for tn in pref for pr in BRANCH[field])
                    if not ok:
                        bad_v += 1
                        bad_i += v["count"]
            groups[canon(raw)].append(v.get("id"))
        multi = sum(1 for g in groups.values() if len(g) > 1)
        disagree = sum(1 for g in groups.values() if len(set(g)) > 1)
        rows_b.append(
            dict(
                field=field,
                mesh_checkable=chk_v,
                violations=bad_v,
                violation_pct_instances=round(100 * bad_i / max(chk_i, 1), 3),
            )
        )
        rows_c.append(
            dict(
                field=field,
                identical_groups=multi,
                disagreeing=disagree,
                disagreement_pct=round(100 * disagree / max(multi, 1), 3),
            )
        )
    return rows_b, rows_c


def composition(results):
    comp = {f: Counter() for f in FIELDS}
    tot = 0
    for fp in glob.glob(
        os.path.join(results, "final", "**", "*.csv.gz"), recursive=True
    ):
        with gzip.open(fp, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                tot += 1
                for field in FIELDS:
                    comp[field][
                        (
                            target_type(r.get(f"final_{field}_id", ""))
                            if not is_ns(r.get(f"final_{field}", ""))
                            else "uncovered"
                        )
                    ] += 1
    rows = [
        dict(
            field=f,
            samples=tot,
            mesh=comp[f]["mesh"],
            cellosaurus=comp[f]["cellosaurus"],
            oov=comp[f]["oov"],
            not_specified=comp[f]["uncovered"],
        )
        for f in FIELDS
    ]
    return rows


def collapse(dictionary):
    rows = []
    for field in FIELDS:
        raw = len(dictionary[field])
        canonical = len(
            {v.get("id") or ("~" + k) for k, v in dictionary[field].items()}
        )
        rows.append(
            dict(
                field=field,
                raw_values=raw,
                canonical=canonical,
                collapse_ratio=round(raw / max(canonical, 1), 3),
            )
        )
    return rows


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main(results, mesh_db, cello_db, out):
    os.makedirs(out, exist_ok=True)
    dictionary = json.load(gzip.open(os.path.join(results, "dictionary.json.gz"), "rt"))
    mesh_by_term, tree = load_mesh(mesh_db)
    cello_by_name = load_cellosaurus(cello_db)

    write_csv(
        os.path.join(out, "db_resolution.csv"),
        db_resolution(
            dictionary, mesh_by_term, tree, cello_by_name, gse_counts(results)
        ),
    )
    summary, acro_rows = acronym_expansion(dictionary, mesh_by_term, tree)
    write_csv(os.path.join(out, "acronym_expansion.csv"), acro_rows)
    write_csv(
        os.path.join(out, "vocabulary_precision.csv"),
        vocabulary_precision(dictionary, mesh_by_term, tree, cello_by_name),
    )
    rows_b, rows_c = branch_and_consistency(dictionary, tree)
    write_csv(os.path.join(out, "branch_validity.csv"), rows_b)
    write_csv(os.path.join(out, "consistency.csv"), rows_c)
    write_csv(os.path.join(out, "composition.csv"), composition(results))
    write_csv(os.path.join(out, "collapse.csv"), collapse(dictionary))
    print("acronym expansion:", summary)
    print("wrote metric CSVs to", out)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:5])
