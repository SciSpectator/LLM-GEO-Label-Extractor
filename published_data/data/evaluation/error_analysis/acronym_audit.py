#!/usr/bin/env python3
"""Why each unresolved acronym-shaped Condition label was not resolved.

No judgement is made anywhere in this script. The question "is there a
controlled term for this acronym" cannot be answered by matching the acronym
against MeSH, because MeSH indexes expansions and not abbreviations: BCP-ALL,
HNSqCC and CNS-PNET all have descriptors (D015452, D000077195, D018242) that no
test on the acronym string can reach. The expansion has to come from somewhere,
and the only non-arbitrary source is the study that used the abbreviation.

So the expansion is taken from the study's own text with the pipeline's own
`find_definition` from acronym_expand.py -- the same function Phase 2 uses --
and the result is then looked up in the shipped mesh.sqlite. Every occurrence
lands in exactly one class:

  defined in the study and MeSH has it in-branch
      the study spelled the abbreviation out, a Condition-branch descriptor
      denotes that expansion, and the label was still left unresolved. A
      recall miss, established without anyone's opinion.
  defined in the study but MeSH holds it outside the Condition branch
      the concept exists and is not reachable under this field's scope.
  defined in the study and MeSH has no term for it
      a vocabulary gap, not a pipeline fault.
  not defined in the study's own text
      the study never spells it out. Nothing further is claimed: attributing
      these would need an external source or an adjudicator, and this script
      does neither.

Usage:
    acronym_audit.py FINAL_CORPUS.csv.gz GSE_META.json.gz MESH.sqlite \\
                     PHASE2_CODE_DIR OUT_DIR
"""

import csv
import gzip
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

DEFINED_IN_BRANCH = "defined in the study and MeSH has it in-branch"
DEFINED_OUT_BRANCH = "defined in the study but MeSH holds it outside the branch"
DEFINED_NO_TERM = "defined in the study and MeSH has no term for it"
NOT_DEFINED = "not defined in the study's own text"


def canon(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def load_mesh(path):
    con = sqlite3.connect(path)
    tree = defaultdict(set)
    for tn, mid in con.execute("SELECT tree_number, mesh_id FROM mesh_tree"):
        tree[mid].add(tn)
    by_term = defaultdict(set)
    for mid, nm in con.execute("SELECT id, name FROM mesh_terms"):
        by_term[canon(nm)].add(mid)
    for mid, sy in con.execute("SELECT mesh_id, synonym FROM mesh_synonyms"):
        by_term[canon(sy)].add(mid)
    names = dict(con.execute("SELECT id, name FROM mesh_terms"))
    return by_term, tree, names


def main() -> int:
    corpus, gse_meta, mesh_db, phase2_code, out_dir = sys.argv[1:6]
    os.makedirs(out_dir, exist_ok=True)
    sys.path.insert(0, phase2_code)
    from acronym_expand import _is_acronym_shaped, find_definition

    by_term, tree, names = load_mesh(mesh_db)
    in_branch = lambda mid: any(t[0] == "C" or t.startswith("F03")
                                for t in tree.get(mid, ()))

    with gzip.open(gse_meta, "rt") as fh:
        meta = json.load(fh)

    def context(gse):
        m = meta.get(gse) or {}
        return " ".join(filter(None, (m.get("gse_title"), m.get("gse_summary"),
                                      m.get("gse_design"))))

    # Every unresolved occurrence, keyed by the study that produced it, because
    # the expansion is a property of the study and not of the corpus.
    pairs = Counter()
    resolved = Counter()
    n_corpus = 0
    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            n_corpus += 1
            raw = (row["phase1b_Condition"] or "").strip()
            if ";" in raw or not _is_acronym_shaped(raw):
                continue
            if row["final_Condition_source"] in ("mesh", "cellosaurus"):
                resolved[raw] += 1
            else:
                pairs[(raw, row["gse"])] += 1

    rows, totals = [], Counter()
    for (acr, gse), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        expansion = find_definition(acr, context(gse))
        if not expansion:
            verdict, mid = NOT_DEFINED, ""
        else:
            ids = by_term.get(canon(expansion), set())
            hit = {i for i in ids if in_branch(i)}
            if hit:
                verdict, mid = DEFINED_IN_BRANCH, sorted(hit)[0]
            elif ids:
                verdict, mid = DEFINED_OUT_BRANCH, sorted(ids)[0]
            else:
                verdict, mid = DEFINED_NO_TERM, ""
        totals[verdict] += n
        rows.append(dict(acronym=acr, gse=gse, occurrences=n,
                         expansion_found_in_study=expansion or "",
                         verdict=verdict, mesh_id=mid,
                         mesh_name=names.get(mid, "")))

    with open(os.path.join(out_dir, "acronym_audit.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    total = sum(totals.values())
    with open(os.path.join(out_dir, "acronym_audit_summary.csv"), "w",
              newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["verdict", "acronym_study_pairs", "occurrences",
                    "pct_of_unresolved", "pct_of_corpus",
                    "of_which_surface_resolves_in_another_study"])
        for v in (DEFINED_IN_BRANCH, DEFINED_OUT_BRANCH, DEFINED_NO_TERM,
                  NOT_DEFINED):
            elsewhere = sum(r["occurrences"] for r in rows
                            if r["verdict"] == v and resolved[r["acronym"]])
            w.writerow([v, sum(1 for r in rows if r["verdict"] == v),
                        totals[v], "%.2f" % (100 * totals[v] / total),
                        "%.3f" % (100 * totals[v] / n_corpus), elsewhere])
        w.writerow(["total", len(rows), total, "100.00",
                    "%.3f" % (100 * total / n_corpus),
                    sum(r["occurrences"] for r in rows if resolved[r["acronym"]])])

    acr_total = total + sum(resolved.values())
    print("corpus %d samples" % n_corpus)
    print("acronym-shaped Condition occurrences %d (%.2f%% of corpus), "
          "%d resolved (%.2f%%)"
          % (acr_total, 100 * acr_total / n_corpus, sum(resolved.values()),
             100 * sum(resolved.values()) / acr_total))
    print("unresolved %d (%.2f%% of corpus) over %d acronym-study pairs"
          % (total, 100 * total / n_corpus, len(rows)))
    for v in (DEFINED_IN_BRANCH, DEFINED_OUT_BRANCH, DEFINED_NO_TERM,
              NOT_DEFINED):
        print("  %-56s %6d  %5.2f%% of unresolved  %5.3f%% of corpus"
              % (v, totals[v], 100 * totals[v] / total,
                 100 * totals[v] / n_corpus))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
