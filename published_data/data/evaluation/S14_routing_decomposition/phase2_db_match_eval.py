"""Phase-2 PER-LABEL, PER-DATABASE normalization evaluation (deterministic).

For every assigned label in the dictionary, cross-check the assignment against
the source vocabularies it claims to (or declines to) match:

  MeSH assignment      -> is canon(raw) an exact NAME/SYNONYM of the assigned
                          descriptor?  (verified-correct)  else retrieval-picked.
  Cellosaurus (CVCL)   -> is canon(raw) a NAME/SYNONYM of the assigned cell line?
                          (verified-correct)  else fuzzy.
  OOV mint             -> does an IN-BRANCH MeSH descriptor, or (Tissue) a
                          Cellosaurus name, actually denote this label?
                            yes -> INAPPROPRIATE OOV  (recall failure: "should be
                                   somewhere else")
                            no  -> appropriate OOV     (genuinely novel).

Per field (Tissue/Condition/Treatment), value- and instance-weighted.
100% coverage, no LLM, no gold needed. The verified rate is a precision LOWER
bound (exact/synonym only); the retrieval bucket is what a human/judge would
sample. The inappropriate-OOV rate is a recall-failure UPPER bound (exact string
match may occasionally be a homonym the picker rightly rejected — examples dumped
for inspection).

Outputs -> results/eval/
"""

import csv, gzip, json, os, re, sqlite3, collections

RUNS = {"final": "Directory to Phase 2 dictionary.json.gz"}
MESH = "Directory to reference mesh.sqlite"
CELLO = "Directory to reference cellosaurus.sqlite"
OUT = "Directory to output"

import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
_a = sys.argv[1:]
if RUNS["final"].startswith("Directory to"):
    RUNS = {"final": _a[0] if len(_a) > 0 else os.path.join(
        PKG, "data", "06_final_labels_repaired", "phase2_out", "dictionary.json.gz")}
if MESH.startswith("Directory to"):
    MESH = _a[1] if len(_a) > 1 else os.path.join(PKG, "reference", "mesh.sqlite")
if CELLO.startswith("Directory to"):
    CELLO = _a[2] if len(_a) > 2 else os.path.join(PKG, "reference", "cellosaurus.sqlite")
if OUT.startswith("Directory to"):
    OUT = _a[3] if len(_a) > 3 else HERE
FIELDS = ["Tissue", "Condition", "Treatment"]
BRANCH_OK = {"Tissue": ("A",), "Condition": ("C", "F03"), "Treatment": ("D", "E02")}
os.makedirs(OUT, exist_ok=True)


def canon(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


con = sqlite3.connect(MESH)
mesh_by_term = collections.defaultdict(set)
mesh_name = {}
for mid, name, cat in con.execute("SELECT id,name,category FROM mesh_terms"):
    mesh_by_term[canon(name)].add(mid)
    mesh_name[mid] = name
for mid, syn in con.execute("SELECT mesh_id,synonym FROM mesh_synonyms"):
    mesh_by_term[canon(syn)].add(mid)
tree = collections.defaultdict(set)
for tn, mid in con.execute("SELECT tree_number,mesh_id FROM mesh_tree"):
    tree[mid].add(tn)


def in_branch(mid, F):
    return any(tn.startswith(p) for tn in tree.get(mid, ()) for p in BRANCH_OK[F])


def mesh_hits_in_branch(text, F):
    return {m for m in mesh_by_term.get(canon(text), set()) if in_branch(m, F)}


cc = sqlite3.connect(CELLO)
cello_names_of = collections.defaultdict(set)
cello_by_name = collections.defaultdict(set)
for name, cvcl, primary in cc.execute("SELECT name,cvcl,primary_name FROM cell_lines"):
    cn = canon(name)
    if len(cn) >= 2:
        cello_names_of[cvcl].add(cn)
        cello_by_name[cn].add(cvcl)


def parts(idv):
    return [p.strip() for p in re.split("[;,]", idv or "") if p.strip()]


def classify(raw, target, idv, F):
    ids = parts(idv)
    cr = canon(raw)
    ct = canon(target)
    cvcl = [p for p in ids if p.startswith("CVCL")]
    mesh = [p for p in ids if re.match(r"[DC]\d", p)]
    oov = [p for p in ids if p.startswith(("OOV", "ART"))]
    if cvcl:
        v = cvcl[0]
        if cr in cello_names_of.get(v, ()) or ct in cello_names_of.get(v, ()):
            return "cellosaurus_exact"

        names = cello_names_of.get(v, ())
        if any(set(n.split()) <= set(cr.split()) for n in names if n):
            return "cellosaurus_contained"
        return "cellosaurus_fuzzy"
    if mesh:
        hitset = mesh_by_term.get(cr, set()) | mesh_by_term.get(ct, set())
        if any(m in hitset for m in mesh):
            return "mesh_exact"
        return "mesh_retrieval"
    if oov:
        m_hits = mesh_hits_in_branch(raw, F) or mesh_hits_in_branch(target, F)
        c_hits = (F == "Tissue") and (cello_by_name.get(cr) or cello_by_name.get(ct))
        if m_hits:
            return "oov_should_be_mesh"
        if c_hits:
            return "oov_should_be_cellosaurus"
        return "oov_appropriate"
    return "uncovered"


def evaluate(dpath):
    d = json.load(gzip.open(dpath, "rt"))
    res = {}
    misses = {F: [] for F in FIELDS}
    for F in FIELDS:
        gv = collections.Counter()
        gi = collections.Counter()
        for raw, v in d[F].items():
            g = classify(raw, v.get("target", ""), v.get("id", ""), F)
            gv[g] += 1
            gi[g] += v["count"]
            if (
                g in ("oov_should_be_mesh", "oov_should_be_cellosaurus")
                and len(misses[F]) < 400
            ):
                if g == "oov_should_be_mesh":
                    hits = mesh_hits_in_branch(raw, F) or mesh_hits_in_branch(
                        v.get("target", ""), F
                    )
                    tgt = (
                        ";".join(sorted(hits))[:40]
                        + " "
                        + "|".join(mesh_name[m] for m in list(hits)[:1])
                    )
                else:
                    tgt = ";".join(
                        sorted(list(cello_by_name.get(canon(raw), set()))[:2])
                    )
                misses[F].append((raw, v.get("id", ""), v["count"], g, tgt))
        res[F] = (gv, gi)
    return res, misses


RES = {}
MISS = {}
for run, path in RUNS.items():
    RES[run], MISS[run] = evaluate(path)


def block(F):
    print(f"\n{'='*84}\n{F}\n{'='*84}")
    for run in RUNS:
        gv, gi = RES[run][F]
        tv = sum(gv.values())
        ti = sum(gi.values())
        mesh_v = gv["mesh_exact"] + gv["mesh_retrieval"]
        cvcl_v = (
            gv["cellosaurus_exact"]
            + gv["cellosaurus_contained"]
            + gv["cellosaurus_fuzzy"]
        )
        oov_v = (
            gv["oov_appropriate"]
            + gv["oov_should_be_mesh"]
            + gv["oov_should_be_cellosaurus"]
        )
        mesh_ok = gv["mesh_exact"]
        cvcl_ok = gv["cellosaurus_exact"] + gv["cellosaurus_contained"]
        oov_bad = gv["oov_should_be_mesh"] + gv["oov_should_be_cellosaurus"]

        def pct(a, b):
            return f"{100 * a / b :.1f}%" if b else "-"

        print(f"\n [{run}] values={tv :,}  instances={ti :,}")
        print(
            f"   MeSH assigned:        {mesh_v :6,} val  | exact/synonym-verified {mesh_ok :6,} ({pct(mesh_ok ,mesh_v)})  retrieval-picked {gv['mesh_retrieval']:6,} ({pct(gv['mesh_retrieval'],mesh_v)})"
        )
        print(
            f"   Cellosaurus assigned: {cvcl_v :6,} val  | name-verified {cvcl_ok :6,} ({pct(cvcl_ok ,cvcl_v)})  fuzzy {gv['cellosaurus_fuzzy']:6,} ({pct(gv['cellosaurus_fuzzy'],cvcl_v)})"
        )
        print(
            f"   OOV minted:           {oov_v :6,} val  | appropriate {gv['oov_appropriate']:6,} ({pct(gv['oov_appropriate'],oov_v)})  INAPPROPRIATE {oov_bad :6,} ({pct(oov_bad ,oov_v)})"
        )
        print(
            f"        inappropriate split: should-be-MeSH {gv['oov_should_be_mesh']:,}  should-be-Cellosaurus {gv['oov_should_be_cellosaurus']:,}"
        )
        print(f"   uncovered (no id):    {gv['uncovered']:6,} val")


for F in FIELDS:
    block(F)


GRADES = [
    "mesh_exact",
    "mesh_retrieval",
    "cellosaurus_exact",
    "cellosaurus_contained",
    "cellosaurus_fuzzy",
    "oov_appropriate",
    "oov_should_be_mesh",
    "oov_should_be_cellosaurus",
    "uncovered",
]
with open(f"{OUT}/C_db_match_grades.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["run", "field", "weight"] + GRADES + ["total"])
    for run in RUNS:
        for F in FIELDS:
            gv, gi = RES[run][F]
            w.writerow(
                [run, F, "values"] + [gv[g] for g in GRADES] + [sum(gv.values())]
            )
            w.writerow(
                [run, F, "instances"] + [gi[g] for g in GRADES] + [sum(gi.values())]
            )


with open(f"{OUT}/C_inappropriate_oov_examples.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(
        [
            "field",
            "raw_value",
            "minted_id",
            "instances",
            "grade",
            "db_target_that_exists",
        ]
    )
    for F in FIELDS:
        for row in sorted(MISS["final"][F], key=lambda r: -r[2])[:60]:
            w.writerow([F] + list(row))
print("\nwrote C_db_match_grades.csv + C_inappropriate_oov_examples.csv to", OUT)
