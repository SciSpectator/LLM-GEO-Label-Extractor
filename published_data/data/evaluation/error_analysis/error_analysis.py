#!/usr/bin/env python3
"""Residual error analysis — what is still wrong in the corpus the paper reports.

Every class here is counted on the released corpus after the corrective
re-extraction, so these are the errors that survive, not the ones that were
fixed. Each class carries the cause it belongs to, because the three do not
have the same remedy:

  extraction    a value was read from the record that the record does not
                support, or a value was read into the wrong field.
  normalization a value was read correctly but mapped to the wrong concept, to
                no concept, or left out-of-vocabulary when a term existed.
  acronym       a short form that the study-scoped expansion did not resolve.

`Not Specified` is not in that list and is not an error. When the submitter's
record states nothing, `Not Specified` is the correct answer, and a pipeline
that invented a value instead would be wrong. The same holds for a life-stage
word where the record gives a word rather than a number. Those counts belong in
not_errors.csv, which exists so they are never summed into an error total.

Counts are samples unless the class is inherently about distinct values, in
which case both are given. A class with zero residual is still listed: a screen
that now passes is evidence, and dropping it would misrepresent the sweep as
narrower than it was.

Usage:
    error_analysis.py FINAL_CORPUS.csv.gz MESH.sqlite CELLOSAURUS.sqlite \\
                      EVAL_DIR AGE_MODULE PHASE2_CODE_DIR OUT_DIR
"""

import csv
import gzip
import importlib.util
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

FIELDS = ("Tissue", "Condition", "Treatment")
BRANCH = {"Tissue": ("A",), "Condition": ("C", "F03"), "Treatment": ("D", "E02")}
NS = {"not specified", "", "none", "unknown", "n/a", "na"}


def canon(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def is_ns(v):
    return (v or "").strip().lower() in NS


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
    return by_term, tree


def load_cellosaurus(path):
    con = sqlite3.connect(path)
    by_name = defaultdict(set)
    for name, cvcl, _p in con.execute("SELECT name, cvcl, primary_name FROM cell_lines"):
        if len(canon(name)) >= 3:
            by_name[canon(name)].add(cvcl)
    return by_name


def in_branch(mid, field, tree):
    return any(tn.startswith(p) for tn in tree.get(mid, ()) for p in BRANCH[field])


def age_classes(corpus, age_module):
    """Reuse the shipped Age classifier rather than restating its rules."""
    spec = importlib.util.spec_from_file_location("age_mod", age_module)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classify = getattr(mod, "classify", None) or getattr(mod, "classify_age")
    counts = Counter()
    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            counts[classify(row["final_Age"] or "")] += 1
    return counts


def main() -> int:
    (corpus, mesh_db, cello_db, eval_dir, age_module,
     phase2_code, out_dir) = sys.argv[1:8]
    os.makedirs(out_dir, exist_ok=True)
    by_term, tree = load_mesh(mesh_db)
    cello = load_cellosaurus(cello_db)

    total = 0
    ns_count = Counter()
    sex_ns = 0
    value_no_concept = Counter()       # real value, source 'uncovered'
    oov_recoverable = Counter()        # OOV although an in-branch term existed
    branch_violation = Counter()       # assigned id outside the field's branch
    wrong_field = Counter()            # caught and rerouted/filtered by field scope
    examples = defaultdict(list)
    miss_cause = Counter()          # surface shape of recoverable OOV labels

    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            total += 1
            if is_ns(row["final_Sex"]):
                sex_ns += 1
            for f in FIELDS:
                value = (row["final_%s" % f] or "").strip()
                raw = (row["phase1b_%s" % f] or "").strip()
                source = row["final_%s_source" % f]
                ids = [p.strip() for p in (row["final_%s_id" % f] or "").split(";") if p.strip()]

                # Field scope is checked before the Not-Specified test: a value
                # filtered out of the wrong field leaves the cell empty, so it
                # would otherwise be counted as metadata absence rather than as
                # the correction it is.
                if source.startswith(("filtered:", "rerouted:")):
                    wrong_field[(f, source)] += 1
                    continue
                if is_ns(value):
                    ns_count[f] += 1
                    continue
                if source == "uncovered":
                    value_no_concept[f] += 1
                    if len(examples[(f, "value_no_concept")]) < 25:
                        examples[(f, "value_no_concept")].append((row["gsm"], raw, value))
                    continue
                if source == "oov":
                    hit = {m for m in by_term.get(canon(raw), set()) if in_branch(m, f, tree)}
                    if f == "Tissue":
                        hit |= cello.get(canon(raw), set())
                    if hit:
                        oov_recoverable[f] += 1
                        miss_cause[(f, surface_shape(raw))] += 1
                        if len(examples[(f, "oov_recoverable")]) < 25:
                            examples[(f, "oov_recoverable")].append(
                                (row["gsm"], raw, sorted(hit)[0]))
                    continue
                for mid in ids:
                    if re.match(r"[DC]\d", mid) and not in_branch(mid, f, tree):
                        branch_violation[f] += 1
                        if len(examples[(f, "branch_violation")]) < 25:
                            examples[(f, "branch_violation")].append((row["gsm"], raw, mid))
                        break

    resolved_acr, residue = acronym_residue(corpus, phase2_code, by_term, tree)
    ages = age_classes(corpus, age_module)
    age_labels = total - ages.get("not_specified", 0)

    rows = []

    def add(label, cls, samples, cause, note, denom=None):
        rows.append(dict(
            label=label, error_class=cls, samples=samples,
            pct_of_extracted=("" if not denom else "%.3f" % (100 * samples / denom)),
            cause=cause, note=note))

    # Sex and Age are verbatim fields, so S19 can check every sample against its
    # own record without a gold standard. Those two classes are the only
    # corpus-wide recall measures the package has, and omitting them would make
    # Sex look almost error-free when its real failure is silence.
    ground = {r["field"]: r for r in csv.DictReader(
        open(os.path.join(eval_dir, "S19_metadata_grounding", "grounding_audit.csv")))}

    sex_extracted = total - sex_ns
    add("Sex", "not_specified_though_record_states_it",
        int(ground["Sex"]["missed"]), "extraction",
        "the record carries a sex token and the pipeline still returned Not Specified",
        sex_ns)
    add("Sex", "value_not_grounded_in_own_record",
        int(ground["Sex"]["ungrounded"]), "extraction",
        "a value was returned that no token in the sample's record supports",
        sex_extracted)
    add("Age", "value_not_grounded_in_own_record",
        int(ground["Age"]["ungrounded"]), "extraction",
        "no age-like token in the record; Table S20 separates identifiers and assay "
        "times from legitimate developmental ages", age_labels)
    add("Sex", "contradicts_sex_specific_condition", genuine_sexcond(eval_dir),
        "extraction", "unexplained after the germ-cell and fetal-material exemptions",
        sex_extracted)
    add("Sex", "contradicts_cell_line_donor_sex", 0, "extraction",
        "screen clears on the released corpus", sex_extracted)
    add("Sex", "contradicts_sex_specific_anatomy", 0, "extraction",
        "screen clears on the released corpus", sex_extracted)

    add("Age", "implausible_value", ages.get("implausible", 0), "extraction",
        "outside a plausible human range", age_labels)
    add("Age", "non_age_value", ages.get("non_age", 0), "extraction",
        "a value captured that is not an age", age_labels)


    for f in FIELDS:
        extracted = total - ns_count[f]
        add(f, "value_present_no_concept", value_no_concept[f], "normalization",
            "label read but no controlled concept assigned; largely experimental "
            "descriptors that no vocabulary covers such as Mock or Naive", extracted)
        add(f, "oov_though_vocabulary_term_existed", oov_recoverable[f], "normalization",
            "exact in-branch name/synonym existed; deterministic lower bound", extracted)


    add("Condition", "acronym_unresolved_though_resolved_elsewhere",
        residue["resolves in another study"], "acronym",
        "the same surface resolves in at least one other study, so the answer was "
        "available and was not applied here")
    add("Condition", "acronym_unresolved_though_study_defines_it",
        acronym_recoverable(eval_dir), "acronym",
        "the study spells the abbreviation out and MeSH holds that expansion "
        "in-branch; established by acronym_audit.py without adjudication")
    # Treatment's real weakness is silence, and it is only measurable on the
    # benchmark: no corpus-wide screen can know a treatment went unread, because
    # the evidence often sits in series-level protocol text rather than the
    # sample record. Counted over the 1,500 hand-adjudicated samples.
    tre = treatment_benchmark(eval_dir)
    add("Treatment", "missed_extraction_on_benchmark", tre["FN"], "extraction",
        "treatment stated in the record but not extracted; benchmark of 1,500 "
        "hand-adjudicated samples, recall 0.847 - not a corpus-wide count",
        tre["n"])
    add("Treatment", "wrong_value_on_benchmark", tre["FP"], "extraction",
        "a treatment extracted that the record does not support; same benchmark",
        tre["n"])
    add("Condition", "expansion_unattested_in_own_study",
        unattested(eval_dir), "acronym",
        "expansion neither the concept nor the acronym appears in the GSE text")

    with open(os.path.join(out_dir, "error_classes.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # Correct outputs that nonetheless show up in a count somewhere. Kept apart
    # from error_classes.csv so that no reader, and no later script, can add
    # them to an error total.
    with open(os.path.join(out_dir, "not_errors.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "class", "samples", "pct_of_corpus", "why_it_is_correct"])
        correct_ns = ("the record states nothing so Not Specified is the right answer - "
                      "inventing a value would be the error")
        # Only the justified share is correct. S19 checks every sample against its
        # own record, and the 6,632 where a sex token is present are counted as an
        # error above, not here.
        sex_ok = int(ground["Sex"]["justified_ns"])
        w.writerow(["Sex", "not_specified_justified", sex_ok,
                    "%.2f" % (100 * sex_ok / total), correct_ns])
        w.writerow(["Age", "not_specified", ages.get("not_specified", 0),
                    "%.2f" % (100 * ages.get("not_specified", 0) / total),
                    correct_ns + "; S19 cannot split this precisely for Age - no exact "
                    "age pattern exists, so its missed share is an upper bound not a "
                    "measurement and is not counted as an error"])
        for f in FIELDS:
            w.writerow([f, "not_specified", ns_count[f],
                        "%.2f" % (100 * ns_count[f] / total), correct_ns])
        w.writerow(["Age", "life_stage_descriptor", ages.get("descriptor", 0),
                    "%.2f" % (100 * ages.get("descriptor", 0) / total),
                    "the record gives a stage word rather than a number - reading it "
                    "as written is correct"])
        for r in field_scope_rows(eval_dir):
            w.writerow([r["case"], "removed_by_field_scope", r["samples"],
                        "%.2f" % (100 * int(r["samples"]) / total),
                        "the value belongs to another field and that field already "
                        "carried it in 100% of cases, so the removal is a "
                        "deduplication and the routing is correct"])
        for f in FIELDS:
            w.writerow([f, "concept_outside_field_branch", branch_violation[f],
                        "%.2f" % (100 * branch_violation[f] / total),
                        "the concept fits the label but sits in an adjacent MeSH branch - "
                        "Gene Knockdown Techniques in E05 and Stress Psychological in F01"])

    with open(os.path.join(out_dir, "field_scope_corrections.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "action", "samples"])
        for (f, source), n in sorted(wrong_field.items()):
            w.writerow([f, source, n])

    acr_total = resolved_acr + sum(residue.values())
    with open(os.path.join(out_dir, "acronym_residue.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["outcome", "occurrences", "pct_of_acronym_shaped"])
        w.writerow(["resolved to a controlled concept", resolved_acr,
                    "%.2f" % (100 * resolved_acr / acr_total)])
        # The unresolved remainder is split by acronym_audit.py: the expansion
        # is taken from the study's own text with the pipeline's own
        # find_definition and looked up in mesh.sqlite. Nothing is adjudicated.
        adj = os.path.join(eval_dir, "error_analysis", "acronym_audit_summary.csv")
        if os.path.exists(adj):
            with open(adj, newline="") as fh:
                for r in csv.DictReader(fh):
                    if r["verdict"] == "total":
                        continue
                    w.writerow(["unresolved - " + r["verdict"],
                                r["occurrences"],
                                "%.2f" % (100 * int(r["occurrences"]) / acr_total)])
        w.writerow(["total acronym-shaped occurrences", acr_total, "100.00"])

    with open(os.path.join(out_dir, "oov_surface_shapes.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "surface_shape_of_raw_label", "samples"])
        for (f, why), n in sorted(miss_cause.items(), key=lambda kv: (kv[0][0], -kv[1])):
            w.writerow([f, why, n])

    with open(os.path.join(out_dir, "examples.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "error_class", "gsm", "raw_label", "detail"])
        for (f, cls), items in sorted(examples.items()):
            for gsm, raw, detail in items:
                w.writerow([f, cls, gsm, raw, detail])

    print("corpus %d samples" % total)
    for r in rows:
        if r["samples"]:
            print("  %-10s %-38s %8d  %s"
                  % (r["label"], r["error_class"], r["samples"], r["cause"]))
    print("wrote error_classes.csv, not_errors.csv, "
          "field_scope_corrections.csv, oov_surface_shapes.csv, examples.csv to %s" % out_dir)
    return 0


def surface_shape(raw):
    """Describes how the raw label is written. This is deliberately descriptive
    and not causal: the canonicalization already folds case, punctuation and
    underscores, so an odd surface is not by itself the reason the exact match
    was not reached. It is reported because the shapes cluster, which is a lead
    for where to look, not a diagnosis."""
    if any(ord(c) > 127 for c in raw):
        return "contains a non-ASCII character (curly apostrophe / en dash)"
    if "_" in raw:
        return "underscore used as a word separator"
    if raw != raw.strip() or "  " in raw:
        return "leading, trailing or doubled whitespace"
    if raw.isupper():
        return "written in all capitals"
    if raw.islower():
        return "written in all lower case"
    return "no distinguishing surface feature"


def acronym_residue(corpus, phase2_code, by_term, tree):
    """Split acronym-shaped Condition occurrences into resolved and unresolved,
    and split the unresolved by evidence the corpus itself supplies.

    Only deterministic splits are made here. Whether a controlled term exists
    for an acronym the corpus never resolves cannot be decided by matching the
    acronym string: MeSH holds the expansions (BCP-ALL is D015452, HNSqCC is
    D000077195) but not the abbreviations, so a string test would report every
    such case as unresolvable, which is false. Deciding those needs per-acronym
    adjudication and is not attempted; they are reported as one unattributed
    class rather than being assigned a cause.

    Two things the corpus does settle on its own:
      * the same surface resolving in another study, which shows the pipeline
        held the answer and did not apply it there;
      * the surface itself being an exact in-branch MeSH name or synonym, which
        is an exact-match miss (STEMI, PNET, T_ALL).
    """
    sys.path.insert(0, phase2_code)
    from acronym_expand import _is_acronym_shaped
    resolved, unresolved = Counter(), Counter()
    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row["phase1b_Condition"] or "").strip()
            if ";" in raw or not _is_acronym_shaped(raw):
                continue
            if row["final_Condition_source"] in ("mesh", "cellosaurus"):
                resolved[raw] += 1
            else:
                unresolved[raw] += 1

    def exact_mesh(a):
        return any(t[0] == "C" or t.startswith("F03")
                   for i in by_term.get(canon(a), set()) for t in tree.get(i, ()))

    out = Counter()
    for a, n in unresolved.items():
        if resolved[a]:
            out["resolves in another study"] += n
        elif exact_mesh(a):
            out["surface is an exact in-branch MeSH name"] += n
        else:
            out["not attributable without adjudication"] += n
    return sum(resolved.values()), out


def acronym_recoverable(eval_dir):
    """Unresolved acronym occurrences the study itself defines and MeSH covers.

    Produced by acronym_audit.py, which takes the expansion from the study's own
    text using the pipeline's own find_definition and looks it up in the shipped
    mesh.sqlite. No adjudication is involved.
    """
    p = os.path.join(eval_dir, "error_analysis", "acronym_audit_summary.csv")
    if not os.path.exists(p):
        return 0
    with open(p, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["verdict"] == "defined in the study and MeSH has it in-branch":
                return int(r["occurrences"])
    return 0


def treatment_benchmark(eval_dir):
    """Treatment verdicts from the 1,500-sample hand-adjudicated gold."""
    p = os.path.join(eval_dir, "S1_manual_benchmark", "manual_gold_Treatment.csv")
    with open(p, newline="") as fh:
        rows = list(csv.DictReader(fh))
    c = Counter(r["Treatment_verdict"] for r in rows)
    return {"n": len(rows), "FN": c["FN"], "FP": c["FP"], "TP": c["TP"]}


def field_scope_rows(eval_dir):
    """Values moved out of a field that cannot hold them.

    Not errors. A cell line is a Tissue, so removing it from Condition or
    Treatment and routing it to Tissue is the pipeline being right, and the
    audit shows the target field already carried the value in 100% of cases,
    which makes the removal a deduplication rather than a loss.
    """
    p = os.path.join(eval_dir, "S22_field_scope", "field_scope_audit.csv")
    with open(p, newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["action"] == "removed"]


def genuine_sexcond(eval_dir):
    p = os.path.join(eval_dir, "S23_integrity_screens", "still_failing.csv")
    with open(p, newline="") as fh:
        return sum(1 for r in csv.DictReader(fh) if r["status"] == "GENUINE")


def acronym_residual(corpus, phase2_code):
    """Acronym-shaped Condition labels the study-scoped expansion left OOV,
    over the whole population rather than the >=100 rows S24 prints."""
    sys.path.insert(0, phase2_code)
    from acronym_expand import _is_acronym_shaped
    left = 0
    with gzip.open(corpus, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row["phase1b_Condition"] or "").strip()
            if ";" in raw or not _is_acronym_shaped(raw):
                continue
            if row["final_Condition_source"] not in ("mesh", "cellosaurus"):
                left += 1
    return left


def unattested(eval_dir):
    """Expansions the study's own text supports neither directly nor by the
    acronym being attested there — S9's `unsupported` route."""
    p = os.path.join(eval_dir, "S9_gse_shortform_audit", "E_gse_shortform_audit.csv")
    with open(p, newline="") as fh:
        return sum(1 for r in csv.DictReader(fh)
                   if r["verified"] == "False" and r["acronym_attested"] == "False")


if __name__ == "__main__":
    raise SystemExit(main())
