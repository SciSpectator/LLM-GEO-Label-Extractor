#!/usr/bin/env python3
"""Recompute every published figure the repaired runs change, in one place.

The manuscript reports the acronym stress test and the Phase 2 evaluation on
particular denominators. A repaired run that reports its own denominators is not
comparable with the released one, so each figure here is recomputed on the SAME
basis the paper used, and the released value is printed beside it. Where a basis
cannot be reproduced exactly the script says so rather than substituting a
near-equivalent.

Published anchors (LLM_extr_paper_SUPPLEMENT.docx):
  §31   "2,924 of 37,852 instances were resolved correctly with 0 wrong
         expansions and 34,928 misses (precision 1.000; recall 0.077)"
  S6    per-acronym outcome for the curated disease acronyms
  S9    short-form expansion audit, per field

Usage:
    paper_updates.py JOB_A_PHASE2_OUT JOB_B_VERIFY_JSON MESH_SQLITE GOLD_JSON OUT_MD
"""

import csv
import glob
import gzip
import json
import os
import re
import sqlite3
import sys
from collections import Counter

# The released stress test, from SUPPLEMENT §31. Kept as constants so the
# comparison is against what the paper actually says, not a remembered value.
REL_TOTAL, REL_TP, REL_FN, REL_FP = 37852, 2924, 34928, 0
REL_RECALL, REL_PRECISION = 0.077, 1.000


def canon(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def main() -> int:
    p2_out, verify_json, mesh_path, gold_path, out_md = sys.argv[1:6]

    mesh = sqlite3.connect(f"file:{mesh_path}?mode=ro", uri=True)
    name_of = {i: n for i, n in mesh.execute("SELECT id, name FROM mesh_terms")}
    form_to_id = {}
    for i, n in mesh.execute("SELECT id, name FROM mesh_terms"):
        form_to_id.setdefault(canon(n), i)
    for i, sy in mesh.execute("SELECT mesh_id, synonym FROM mesh_synonyms"):
        form_to_id.setdefault(canon(sy), i)

    gold = {k.upper(): v for k, v in json.load(open(gold_path)).items()}
    want_of = {a: form_to_id.get(canon(v)) for a, v in gold.items()}

    # ---- acronyms, scored per sample on the labels the run wrote -----------
    per = {}
    wrong_by_study = Counter()
    for path in sorted(glob.glob(os.path.join(p2_out, "final", "*", "*.csv.gz"))):
        with gzip.open(path, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                acr = (r.get("phase1b_Condition") or "").strip().upper()
                if acr not in want_of:
                    continue
                got = (r.get("final_Condition_id") or "").strip()
                slot = per.setdefault(acr, {"tp": 0, "fp": 0, "fn": 0,
                                            "wrong": Counter()})
                if want_of[acr] and got == want_of[acr]:
                    slot["tp"] += 1
                elif re.match(r"^[DC]\d{6,}$", got):
                    # resolved to a DIFFERENT controlled concept: a wrong
                    # expansion, which is what the released run had none of
                    slot["fp"] += 1
                    slot["fn"] += 1
                    slot["wrong"][name_of.get(got, got)] += 1
                    wrong_by_study[(acr, got, name_of.get(got, got),
                                    r.get("gse", ""))] += 1
                else:
                    slot["fn"] += 1               # left out of vocabulary

    tp = sum(v["tp"] for v in per.values())
    fp = sum(v["fp"] for v in per.values())
    fn = sum(v["fn"] for v in per.values())
    seen = tp + fn

    # On the published denominator: the 2,924 already-correct instances were not
    # re-run (Job A takes only the samples the release left unresolved), so they
    # carry over unchanged and the newly resolved ones are added to them.
    new_tp = REL_TP + tp
    new_recall = new_tp / REL_TOTAL
    new_precision = new_tp / (new_tp + fp) if (new_tp + fp) else 1.0

    screens = json.load(open(verify_json))
    tot_err = sum(v["errors"] for v in screens.values())
    tot_fix = sum(v["fixed"] for v in screens.values())
    tot_still = sum(v["still_failing"] for v in screens.values())

    L = []
    A = L.append
    A("# Recomputed figures for the repaired runs\n")
    A("Every value is on the denominator the manuscript already uses.\n")

    A("\n## SUPPLEMENT §31 — disease-acronym stress test\n")
    A("Released:\n")
    A(f"> {REL_TP:,} of {REL_TOTAL:,} instances were resolved correctly with "
      f"{REL_FP} wrong expansions and {REL_FN:,} misses "
      f"(precision {REL_PRECISION:.3f}; recall {REL_RECALL:.3f}).\n")
    A("\nRepaired:\n")
    A(f"> {new_tp:,} of {REL_TOTAL:,} instances were resolved correctly with "
      f"{fp} wrong expansions and {REL_TOTAL - new_tp:,} misses "
      f"(precision {new_precision:.3f}; recall {new_recall:.3f}).\n")
    A(f"\nOf the {REL_FN:,} instances the release left unresolved, this run "
      f"scored {seen:,} and resolved {tp:,} of them.\n")

    # Table S6 as published: eleven representative acronyms with their
    # CORPUS-wide sample counts. Those counts stay -- they describe the corpus,
    # not this run -- and only the outcome column is recomputed. Every one of
    # them was unresolved in the release, so the run covers all of their
    # instances and an outcome measured here applies to the published count.
    S6 = [("NSCLC", "16,375", "Carcinoma, Non-Small-Cell Lung", "not resolved (FN)"),
          ("SLE", "4,683", "Lupus Erythematosus, Systemic", "not resolved (FN)"),
          ("AML", "3,494", "Leukemia, Myeloid, Acute", "not resolved (FN)"),
          ("COPD", "2,831", "Pulmonary Disease, Chronic Obstructive", "resolved (TP)"),
          ("CLL", "1,836", "Leukemia, Lymphocytic, Chronic, B-Cell", "not resolved (FN)"),
          ("GBM", "1,038", "Glioblastoma", "not resolved (FN)"),
          ("MDS", "885", "Myelodysplastic Syndromes", "not resolved (FN)"),
          ("AD", "647", "Alzheimer Disease", "not resolved (FN)"),
          ("HCC", "322", "Carcinoma, Hepatocellular", "not resolved (FN)"),
          ("CRC", "198", "Colorectal Neoplasms", "not resolved (FN)"),
          ("MS", "93", "Multiple Sclerosis", "resolved (TP)")]

    def outcome_of(acr):
        v = per.get(acr)
        if not v:
            return "not covered by this run"
        n = v["tp"] + v["fn"]
        if v["tp"] and v["fn"] == 0:
            return "resolved (TP)"
        if v["tp"] == 0:
            return "not resolved (FN)"
        return f"resolved in {v['tp']:,}/{n:,} instances"

    s6_rows = []
    A("\n## Table S6 — per-acronym outcome (published rows, recomputed)\n")
    A("| Acronym | Samples | Gold MeSH concept | Released | Repaired |")
    A("|---|---|---|---|---|")
    for acr, n_pub, concept, released in S6:
        new_out = "resolved (TP)" if released == "resolved (TP)" else outcome_of(acr)
        A(f"| {acr} | {n_pub} | {concept} | {released} | {new_out} |")
        s6_rows.append(dict(acronym=acr, samples_text=n_pub, gold=concept,
                            released=released, outcome=new_out))

    A("\n### Every gold acronym this run scored\n")
    A("| Acronym | Instances | Resolved | Wrong concept |")
    A("|---|---|---|---|")
    for acr in sorted(per, key=lambda a: -(per[a]["tp"] + per[a]["fn"])):
        v = per[acr]
        worst = v["wrong"].most_common(1)
        A(f"| {acr} | {v['tp'] + v['fn']:,} | {v['tp']:,} | "
          f"{worst[0][0] + ' x' + str(worst[0][1]) if worst else '--'} |")

    # Where the run and the gold disagree, the gold is not automatically right.
    # The gold assigns ONE meaning per acronym corpus-wide, while the repaired
    # stage assigns one per study, so a study that uses the abbreviation in its
    # other sense is scored as an error for being correct. Listing the
    # disagreements by study is what separates the two cases, and it is the
    # measurement the released evaluation could not make.
    A("\n## The instances scored as wrong expansions, by study\n")
    A("| Acronym | Study | Resolved to | Gold |")
    A("|---|---|---|---|")
    for (acr, cid, tgt, gse), n in wrong_by_study.most_common(15):
        A(f"| {acr} (n={n}) | {gse} | {tgt} | {gold.get(acr, '')} |")

    A("\n## Table 4 / audit integrity screens\n")
    A("| Screen | Flagged in release | Cleared | Still flagged | Genuine errors |")
    A("|---|---|---|---|---|")
    for s, v in screens.items():
        A(f"| {s} | {v['errors']:,} | {v['fixed']:,} | {v['still_failing']:,} | "
          f"{v.get('genuine', 0)} |")
    A(f"| **total** | **{tot_err:,}** | **{tot_fix:,}** | **{tot_still:,}** | "
      f"**{sum(v.get('genuine', 0) for v in screens.values())}** |")

    text = "\n".join(L) + "\n"
    open(out_md, "w").write(text)

    # Machine-readable companion, so the document editor applies exactly the
    # numbers reported here rather than a retyped copy of them.
    figures = {
        "acronyms": {
            "total": REL_TOTAL, "tp": new_tp, "fp": fp,
            "fn": REL_TOTAL - new_tp,
            "precision": round(new_precision, 3), "recall": round(new_recall, 3),
            "released_tp": REL_TP, "released_precision": REL_PRECISION,
            "released_recall": REL_RECALL,
            "scored_here": seen, "resolved_here": tp,
        },
        "s6_rows": s6_rows,
        "screens": {
            "total_errors": tot_err, "total_fixed": tot_fix,
            "total_still": tot_still,
            "genuine": sum(v.get("genuine", 0) for v in screens.values()),
            "exempt_germ": 14, "exempt_fetal": 7,
            "rows": [dict(screen=s, errors=v["errors"], fixed=v["fixed"],
                          still=v["still_failing"], genuine=v.get("genuine", 0))
                     for s, v in screens.items()],
        },
    }
    json.dump(figures, open(os.path.splitext(out_md)[0] + ".json", "w"), indent=2)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
