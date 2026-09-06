#!/usr/bin/env python3
"""Table S6 — the curated disease-acronym stress test, scored on the corpus.

This has to be scored per sample rather than per dictionary entry. Phase 2
resolves an abbreviation inside the study that used it, so the dictionary's
top-level entry for AML is the corpus-wide default the collision guard withheld
on, and reading the outcome from there reports the stage as having failed when
the corpus shows it resolved: 0.298 recall against 0.968. The released corpus is
the only place the per-sample outcome is recorded, so it is the input here.

The gold list is the one in phase2_evaluation.py, imported rather than restated,
and each acronym's expansion is looked up in the shipped mesh.sqlite instead of
being asserted.

Usage:
    acronym_gold_eval.py FINAL_CORPUS.csv.gz MESH.sqlite EVAL_SCRIPT OUT.csv
"""

import csv
import gzip
import re
import sqlite3
import sys
from collections import Counter, defaultdict


def canon(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def gold_from(eval_script):
    src = open(eval_script).read()
    i = src.index("ACRONYM_GOLD = {")
    j = src.index("}", i)
    return eval(src[i + len("ACRONYM_GOLD = "):j + 1])


def main() -> int:
    corpus, mesh_db, eval_script, out = sys.argv[1:5]
    gold = gold_from(eval_script)

    con = sqlite3.connect(mesh_db)
    by_term = defaultdict(set)
    for mid, nm in con.execute("SELECT id, name FROM mesh_terms"):
        by_term[canon(nm)].add(mid)
    for mid, sy in con.execute("SELECT mesh_id, synonym FROM mesh_synonyms"):
        by_term[canon(sy)].add(mid)

    target = {a: by_term.get(canon(full), set()) for a, full in gold.items()}
    target = {a: ids for a, ids in target.items() if ids}

    verdicts = defaultdict(Counter)
    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row["phase1b_Condition"] or "").strip()
            if raw not in target:
                continue
            ids = [x.strip() for x in (row["final_Condition_id"] or "").split(";")
                   if x.strip()]
            if any(i in target[raw] for i in ids):
                verdicts[raw]["TP"] += 1
            elif any(re.match(r"[DC]\d", i) for i in ids):
                verdicts[raw]["FP"] += 1
            else:
                verdicts[raw]["FN"] += 1

    rows = []
    for a in sorted(verdicts, key=lambda x: -sum(verdicts[x].values())):
        c = verdicts[a]
        n = sum(c.values())
        rows.append(dict(acronym=a, samples=n, resolved=c["TP"],
                         wrong=c["FP"], missed=c["FN"],
                         outcome=("resolved" if c["FN"] == 0 and c["FP"] == 0
                                  else "partly resolved" if c["TP"]
                                  else "not resolved"),
                         target=gold[a].replace(",", " -")))
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    tp = sum(r["resolved"] for r in rows)
    fp = sum(r["wrong"] for r in rows)
    fn = sum(r["missed"] for r in rows)
    full = sum(1 for r in rows if r["outcome"] == "resolved")
    part = sum(1 for r in rows if r["outcome"] == "partly resolved")
    print("curated acronyms occurring as labels : %d" % len(rows))
    print("  resolved in every instance         : %d" % full)
    print("  resolved in some                   : %d" % part)
    print("  instances TP %d  FP %d  FN %d  (of %d)" % (tp, fp, fn, tp + fp + fn))
    print("  recall %d/%d = %.4f   precision %d/%d = %.4f"
          % (tp, tp + fn, tp / (tp + fn), tp, tp + fp, tp / (tp + fp)))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
