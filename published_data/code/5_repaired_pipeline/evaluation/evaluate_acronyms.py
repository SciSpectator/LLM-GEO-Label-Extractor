#!/usr/bin/env python3
"""Score acronym expansion after the repaired Phase 2, on two populations.

The published Table S6 measures 39 curated disease acronyms with a known MeSH
answer. Those are the only ones with a gold target, so they stay the headline
metric and are reported in exactly the released format -- but they are a
measurement sample, not the problem: the corpus carries 3,871 distinct
acronym-shaped labels over 233,970 samples, of which 191,052 were released
unresolved. Both populations are reported, so the gold figure stays comparable
while the real coverage change is not hidden behind it.

Usage:
    evaluate_acronyms.py PHASE2_OUT_DIR MESH_SQLITE ACRONYM_GOLD_JSON OUT_DIR
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


def canon(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def main() -> int:
    p2_out, mesh_path, gold_path, out_dir = sys.argv[1:5]
    os.makedirs(out_dir, exist_ok=True)

    gold = {k.upper(): v for k, v in json.load(open(gold_path)).items()}

    mesh = sqlite3.connect(f"file:{mesh_path}?mode=ro", uri=True)
    name_of = {i: n for i, n in mesh.execute("SELECT id, name FROM mesh_terms")}
    form_to_id = {}
    for i, n in mesh.execute("SELECT id, name FROM mesh_terms"):
        form_to_id.setdefault(canon(n), i)
    for i, sy in mesh.execute("SELECT mesh_id, synonym FROM mesh_synonyms"):
        form_to_id.setdefault(canon(sy), i)

    with gzip.open(os.path.join(p2_out, "dictionary.json.gz"), "rt") as fh:
        dictionary = json.load(fh)

    cond = dictionary.get("Condition", {})

    # ---- headline: the 39 gold acronyms, released format ------------------
    #
    # Scored PER SAMPLE, off the labels the run actually wrote, for the reason
    # the repair exists: expansion is decided per study, so one acronym has as
    # many answers as it has studies. The dictionary carries a single top-level
    # id per acronym and cannot represent that -- where studies disagree it
    # collapses to one of them, and scoring the collapsed value misreports the
    # corpus in both directions. HD resolves to Huntington disease in four
    # studies and Parkinson disease in one; the dictionary shows Parkinson, so
    # a dictionary-level score fails all 244 samples although 202 are right.
    # OA resolves correctly in eight studies but collapses to an OOV cluster,
    # failing 268 where 187 are right. Only the per-sample labels are what
    # ships, so only they are scored here.
    want_of = {acr: form_to_id.get(canon(want)) for acr, want in gold.items()}
    per_acr = {}
    tp = fn = 0
    for path in sorted(glob.glob(os.path.join(p2_out, "final", "*", "*.csv.gz"))):
        with gzip.open(path, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                acr = (r.get("phase1b_Condition") or "").strip().upper()
                want_id = want_of.get(acr)
                if acr not in want_of:
                    continue
                got_id = (r.get("final_Condition_id") or "").strip()
                hit = bool(want_id) and got_id == want_id
                slot = per_acr.setdefault(acr, {"tp": 0, "fn": 0, "targets": Counter()})
                slot["tp" if hit else "fn"] += 1
                if not hit:
                    slot["targets"][
                        name_of.get(got_id, r.get("final_Condition") or "")] += 1
                tp += hit
                fn += not hit

    rows = []
    for acr, slot in sorted(per_acr.items()):
        n = slot["tp"] + slot["fn"]
        worst = slot["targets"].most_common(1)
        rows.append(dict(acronym=acr, samples=n,
                         outcome="resolved" if slot["fn"] == 0 else
                         ("partly" if slot["tp"] else "not_resolved"),
                         resolved_samples=slot["tp"],
                         target=name_of.get(want_of.get(acr) or "", gold[acr]),
                         top_error=worst[0][0] if worst else ""))

    with open(os.path.join(out_dir, "acronym_expansion.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["acronym", "samples", "outcome",
                                           "resolved_samples", "target",
                                           "top_error"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["acronym"]))

    pooled = tp + fn
    recall = tp / pooled if pooled else 0.0

    # ---- the full acronym population --------------------------------------
    sys.path.insert(0, os.environ.get("PYTHONPATH", "").split(":")[0] or ".")
    from phase2_mesh import _is_shortform

    tot = res = 0
    per = []
    for col, entries in dictionary.items():
        for raw, rec in entries.items():
            if not _is_shortform(raw):
                continue
            n = int(rec.get("count") or 0)
            rid = rec.get("id") or ""
            tot += n
            hit = bool(re.match(r"^[DC]\d{6,}$", rid))
            if hit:
                res += n
            per.append(dict(column=col, acronym=raw, samples=n,
                            resolved="yes" if hit else "no",
                            target=rec.get("target") or "", id=rid))

    with open(os.path.join(out_dir, "acronym_all.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["column", "acronym", "samples",
                                           "resolved", "target", "id"])
        w.writeheader()
        w.writerows(sorted(per, key=lambda r: -r["samples"]))

    summary = {
        "gold_39": {"pooled_samples": pooled, "TP": tp, "FN": fn,
                    "recall": round(recall, 4), "released_recall": 0.077},
        "all_acronyms": {"samples": tot, "resolved": res,
                         "resolved_pct": round(100 * res / tot, 2) if tot else 0,
                         "released_resolved": 42918, "released_samples": 233970},
    }
    json.dump(summary, open(os.path.join(out_dir, "acronym_summary.json"), "w"),
              indent=2)

    print(json.dumps(summary, indent=2))
    print(f"\nwrote acronym_expansion.csv, acronym_all.csv, acronym_summary.json -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
