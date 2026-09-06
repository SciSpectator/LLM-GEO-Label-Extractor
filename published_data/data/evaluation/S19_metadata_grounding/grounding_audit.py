"""Audit whether Phase 1 Sex and Age decisions are supported by the sample's own metadata.

Usage:
    python3 grounding_audit.py [geo_metadata.sqlite] [final_labels.csv.gz] [out.csv]

Sex and Age are verbatim fields: a value must be supported by text in the sample's own
record, and "Not Specified" is the correct answer when the record does not state the
attribute. This script separates the two cases over the whole corpus, without any gold
standard:

    grounded value      a value was returned and the record contains a matching token
    ungrounded value    a value was returned and the record contains no such token
    justified NS        "Not Specified" was returned and the record contains no token
    missed              "Not Specified" was returned although the record contains a token

The Sex token pattern is the one the pipeline itself applies (sex_ground.py). Age has no
equally precise pattern: the age pattern below is deliberately permissive, so its
"Not Specified" split is reported for the Sex field only, and for Age only the value side
(grounded versus ungrounded) is reported.
"""

import csv
import gzip
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "code", "1_extraction_phase1_1b"))
from sex_ground import _SEX_TOKEN

AGE_TOKEN = re.compile(
    r"age|\byrs?\b|\byears?\b|\bmonths?\b|\bweeks?\b|\bdays?\b|\by\.?o\.?\b"
    r"|\bdpc\b|gestation|postnatal|\bpnd\b|\bnewborn\b|\bneonat|\bfetal\b|\bembryo",
    re.I)

MISSING = {
    "", "not specified", "na", "n/a", "n.a.", "none", "null", "unknown",
    "unspecified", "not applicable", "missing", "nd", "n.d.", "--", "-", "---",
    "?", "??", "not available", "not collected", "not reported",
    "not determined", "not known", "not given", "unkown", "no", "-.-", ".",
}

PREFIX = re.compile(r"^\s*age\s*:\s*", re.I)

FIELDS = ("title", "source_name", "characteristics", "treatment_protocol", "description")


def is_missing(value):
    return PREFIX.sub("", (value or "").strip()).lower() in MISSING


def main():
    meta_path = sys.argv[1] if len(sys.argv) > 1 else "../../01_input_metadata/geo_metadata.sqlite"
    corpus_path = sys.argv[2] if len(sys.argv) > 2 else "../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz"
    out = sys.argv[3] if len(sys.argv) > 3 else "grounding_audit.csv"

    con = sqlite3.connect(meta_path)
    blob = {}
    for row in con.execute(f"select gsm,{','.join(FIELDS)} from sample"):
        blob[row[0]] = " ".join(x or "" for x in row[1:])

    counts = {f: dict(grounded=0, ungrounded=0, justified_ns=0, missed=0)
              for f in ("Sex", "Age")}
    with gzip.open(corpus_path, "rt") as fh:
        for row in csv.DictReader(fh):
            text = blob.get(row["gsm"], "")
            for field, pattern in (("Sex", _SEX_TOKEN), ("Age", AGE_TOKEN)):
                present = bool(pattern.search(text))
                c = counts[field]
                if is_missing(row.get(f"final_{field}")):
                    c["missed" if present else "justified_ns"] += 1
                else:
                    c["grounded" if present else "ungrounded"] += 1

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "values_returned", "grounded", "ungrounded",
                    "not_specified", "justified_ns", "missed"])
        for field, c in counts.items():
            values = c["grounded"] + c["ungrounded"]
            ns = c["justified_ns"] + c["missed"]
            w.writerow([field, values, c["grounded"], c["ungrounded"],
                        ns, c["justified_ns"], c["missed"]])

    for field, c in counts.items():
        values = c["grounded"] + c["ungrounded"]
        ns = c["justified_ns"] + c["missed"]
        print(f"{field}: values {values:,} "
              f"(grounded {c['grounded']:,} = {100*c['grounded']/values:.2f}%, "
              f"ungrounded {c['ungrounded']:,} = {100*c['ungrounded']/values:.2f}%) | "
              f"Not Specified {ns:,} "
              f"(justified {c['justified_ns']:,} = {100*c['justified_ns']/ns:.2f}%, "
              f"token present {c['missed']:,} = {100*c['missed']/ns:.2f}%)")


if __name__ == "__main__":
    main()
