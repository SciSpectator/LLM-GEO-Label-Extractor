                      

"""Reviewer concern 3 - what the benchmark actually asks, and how it is scored.

The reviewer reads 0.997 F1 on Tissue as implausible against named-entity
recognition state of the art. The two numbers describe different tasks, and the
difference is measurable rather than rhetorical: a GEO record is semi-structured,
and much of the time the answer is already sitting in a key-value field the
depositor labelled. Where that is true the work is normalization; where it is
not, the model has to read prose, and the score falls.

Each benchmark sample is placed in one of three tiers by what the depositor
wrote, never by whether the pipeline was right:

    keyed          a characteristics key names the field  (tissue:, disease:, ...)
    source_name    no such key, but the record has a dedicated source-name field
    prose only     neither; the answer, if present, is in running text

The tiers are properties of the input. Accuracy is then read off within each,
and the gap is the answer to the reviewer.

The second half reports how 'Not Specified' is scored, including the one rule
that is not self-evident and that the Methods should state outright: for a
CREEDS control sample, an empty Condition was accepted. Its effect is measured
rather than argued.

Nothing here re-judges a sample. The verdict column is the authors' own.
"""



import csv

import json

import os

from collections import Counter



import os as _os
PKG = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", ".."))

GOLD = f"{PKG}/data/evaluation/S1_manual_benchmark"

SRC = f"{PKG}/data/01_input_metadata/samples_804k.json"

OUT = _os.path.dirname(__file__)



KEYS = {

    "Tissue": ("tissue", "cell line", "cell type", "cell-line", "celltype", "cell",

               "organ", "anatomical site", "biopsy site", "source", "tissue type"),

    "Condition": ("disease", "diagnosis", "disease state", "condition", "status",

                  "phenotype", "subject status", "health state", "disease status"),

    "Treatment": ("treatment", "treated with", "drug", "compound", "agent",

                  "stimulation", "infection", "transfection", "sirna", "shrna",

                  "dose", "exposure", "perturbation", "treatment protocol"),

}

NS = {"", "not specified", "na", "none", "n/a"}

FIELDS = [("Tissue", "manual_gold_Tissue_Condition.csv", "Tissue"),

          ("Condition", "manual_gold_Tissue_Condition.csv", "Condition"),

          ("Treatment", "manual_gold_Treatment.csv", "Treatment")]



bench = {}

for field, fname, prefix in FIELDS:

    with open(os.path.join(GOLD, fname)) as fh:

        for r in csv.DictReader(fh):

            bench.setdefault(r["gsm"], {})[field] = (

                r[f"{prefix}_extracted"].strip(), r[f"{prefix}_verdict"].strip().upper())



dec = json.JSONDecoder()

rec = {}

with open(SRC) as fh:

    buf = fh.read(1 << 20)

    i = buf.index("[") + 1

    while True:

        while i < len(buf) and buf[i] in " \t\r\n,":

            i += 1

        if i >= len(buf) or buf[i] == "]":

            chunk = fh.read(1 << 20)

            if not chunk:

                break

            buf, i = buf[i:] + chunk, 0

            continue

        try:

            obj, end = dec.raw_decode(buf, i)

        except ValueError:

            chunk = fh.read(1 << 20)

            if not chunk:

                break

            buf, i = buf[i:] + chunk, 0

            continue

        if obj.get("gsm") in bench:

            rec[obj["gsm"]] = obj

        i = end

print(f"  benchmark records resolved: {len(rec):,} / {len(bench):,}")





def char_keys(r):

    out = set()

    for part in str(r.get("characteristics") or "").split(";"):

        for piece in part.split("\t"):

            if ":" in piece:

                out.add(piece.split(":", 1)[0].strip().lower())

    return out





def blob(r):

    return " ".join(str(r.get(k) or "") for k in

                    ("title", "source_name", "characteristics",

                     "treatment_protocol", "description")).lower()





print()

print("=" * 90)

print("CONCERN 3a - how much of the answer the depositor already wrote down")

print("=" * 90)

print("\n  %-11s %-14s %7s %8s %10s %10s" %

      ("field", "tier", "n", "share", "accuracy", "verbatim"))

rows = []

for field, _, _ in FIELDS:

    tiers = {"keyed": [], "source_name": [], "prose only": []}

    verb = Counter()

    for gsm, per in bench.items():

        if field not in per or gsm not in rec:

            continue

        value, verdict = per[field]

        r = rec[gsm]

        if any(k in char_keys(r) for k in KEYS[field]):

            t = "keyed"

        elif str(r.get("source_name") or "").strip():

            t = "source_name"

        else:

            t = "prose only"

        tiers[t].append(verdict)

        if str(value).lower() not in NS and value.lower() in blob(r):

            verb[t] += 1

    tot = sum(len(v) for v in tiers.values())

    print()

    for t in ("keyed", "source_name", "prose only"):

        vs = tiers[t]

        if not vs:

            continue

        a = sum(v in ("TP", "TN") for v in vs) / len(vs)

        print("  %-11s %-14s %7d %7.1f%% %10.4f %9.1f%%" %

              (field if t == "keyed" else "", t, len(vs), 100 * len(vs) / tot,

               a, 100 * verb[t] / len(vs)))

        rows.append(dict(field=field, tier=t, n=len(vs),

                         share=round(100 * len(vs) / tot, 2), accuracy=round(a, 4),

                         verbatim_pct=round(100 * verb[t] / len(vs), 2)))



print("""
  Read: where a key names the field, the task is to map a stated string onto a
  concept. Where it does not, the model reads prose, and that is where the
  errors concentrate. GEO records are keyed for tissue far more often than for
  treatment, which is what separates 0.997 from 0.847 - not a claim to beat
  entity recognition on running text.""")



print()

print("=" * 90)

print("CONCERN 3b - how 'Not Specified' is scored")

print("=" * 90)

for field, fname, prefix in FIELDS:

    with open(os.path.join(GOLD, fname)) as fh:

        rs = list(csv.DictReader(fh))

    ns_rows = [r for r in rs if str(r[f"{prefix}_extracted"]).strip().lower() in NS]

    print(f"\n  {field}: {len(ns_rows):,} of {len(rs):,} benchmark samples returned Not Specified")

    for v, c in Counter(r[f"{prefix}_verdict"].strip().upper() for r in ns_rows).most_common():

        say = {"TN": "correct - the gold has no value either",

               "FN": "an error - the gold has a value",

               "TP": "accepted as correct - see the rule below"}.get(v, v)

        print(f"      {v}  {c:>6,}   {say}")



print("""
  The one rule a reader cannot infer: 25 Condition samples whose CREEDS gold is
  'control' were accepted as correct with an empty Condition. Every one of the
  25 has gold 'control' and no other value. Sensitivity to that choice:""")

with open(os.path.join(GOLD, "manual_gold_Tissue_Condition.csv")) as fh:

    rs = list(csv.DictReader(fh))

c = Counter(r["Condition_verdict"].strip().upper() for r in rs)

lenient_tp = c["TP"]

strict_tp = lenient_tp - 25

for tag, tp, fn in (("as published (accepted)", lenient_tp, c["FN"]),

                    ("if scored as misses", strict_tp, c["FN"] + 25)):

    p = tp / (tp + c["FP"])

    r_ = tp / (tp + c["FP"] + fn)

    print(f"      Condition {tag:<24} P={p:.4f}  R={r_:.4f}  F1={2*p*r_/(p+r_):.4f}")



with open(f"{OUT}/benchmark_grounding.csv", "w", newline="") as fh:

    w = csv.DictWriter(fh, fieldnames=list(rows[0]))

    w.writeheader()

    w.writerows(rows)

print(f"\n  wrote {OUT}/benchmark_grounding.csv")

