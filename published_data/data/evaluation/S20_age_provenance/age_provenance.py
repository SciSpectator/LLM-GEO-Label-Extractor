"""Separate donor chronological age from developmental and unsourced age values.

Usage:
    python3 age_provenance.py [geo_metadata.sqlite] [final_labels.csv.gz] [out.csv]

The Age prompt requires the value to come from a characteristics field whose name is or
contains "age", including gestational age, postnatal day and developmental stage, and only
then permits a fallback to the title, source or description for that subject's own age. It
admits years, months, weeks and days as units, so a sub-year value is not anomalous in
itself.

This script therefore classifies every returned Age value by what the sample's own record
offers as a source:

    donor age field       a characteristics key names an age ("age", "age at diagnosis",
                          "donor age", "patient age (yrs)")
    developmental field   no such key, but a key names a gestational, postnatal, embryonic
                          or developmental quantity
    no age field          the record names neither, so the value was taken from free text

Keys are matched on "age" as a whole token, which excludes the many keys that merely
contain the letters (stage, agent, storage, passage, dosage, lineage, percentage, average).
The unit of each value is reported alongside, because day- and week-denominated values
concentrate in the third class, where they are experimental timepoints rather than ages.
"""

import csv
import gzip
import re
import sqlite3
import sys
from collections import Counter

AGE_KEY = re.compile(r"(?<![a-z])age(?![a-z])|^age", re.I)
DEV_KEY = re.compile(r"gestation|postnatal|embryo|\bpnd\b|\bdpc\b|\bdpf\b|develop\w*\s*stage"
                     r"|dev\s*stage|fetus|larval|post[- ]?conception", re.I)

MISSING = {"", "not specified", "na", "n/a", "n.a.", "none", "null", "unknown",
           "unspecified", "not applicable", "missing", "nd", "n.d.", "--", "-",
           "---", "?", "??", "not available", "not collected", "not reported",
           "not determined", "not known", "not given", "unkown", "no", "-.-", "."}

PREFIX = re.compile(r"^\s*age\s*:\s*", re.I)
UNIT = re.compile(r"^(years?|months?|weeks?|days?)\s*:", re.I)

CLASSES = ("donor age field", "developmental field", "no age field")


def characteristic_keys(path):
    con = sqlite3.connect(path)
    keys = {}
    for gsm, chars in con.execute("select gsm,characteristics from sample"):
        found = []
        for segment in re.split(r"[;\t\n]", chars or ""):
            if ":" in segment:
                key = segment.split(":", 1)[0].strip()
                if key and len(key) < 60:
                    found.append(key)
        keys[gsm] = found
    return keys


def main():
    meta_path = sys.argv[1] if len(sys.argv) > 1 else "../../01_input_metadata/geo_metadata.sqlite"
    corpus_path = sys.argv[2] if len(sys.argv) > 2 else "../../06_final_labels_repaired/LLM_labels_all_samples_FINAL.csv.gz"
    out = sys.argv[3] if len(sys.argv) > 3 else "age_provenance.csv"

    keys = characteristic_keys(meta_path)
    counts = Counter()
    units = Counter()
    with gzip.open(corpus_path, "rt") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("final_Age") or "").strip()
            if PREFIX.sub("", raw).strip().lower() in MISSING:
                continue
            sample_keys = keys.get(row["gsm"], [])
            if any(AGE_KEY.search(k) for k in sample_keys):
                cls = "donor age field"
            elif any(DEV_KEY.search(k) for k in sample_keys):
                cls = "developmental field"
            else:
                cls = "no age field"
            counts[cls] += 1
            match = UNIT.match(raw)
            unit = match.group(1).rstrip("s").lower() if match else (
                "verbatim" if raw.lower().startswith("age:") else "other")
            units[(cls, unit)] += 1

    total = sum(counts.values())
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["class", "values", "pct_of_age_values", "day_or_week_values", "pct_day_or_week"])
        for cls in CLASSES:
            dw = units[(cls, "day")] + units[(cls, "week")]
            w.writerow([cls, counts[cls], f"{100 * counts[cls] / total:.2f}",
                        dw, f"{100 * dw / counts[cls]:.2f}" if counts[cls] else ""])
        w.writerow(["total", total, "100.00", "", ""])

    print(f"{'class':<22}{'values':>10}{'%':>8}{'day/week':>11}{'% d/w':>8}")
    for cls in CLASSES:
        dw = units[(cls, "day")] + units[(cls, "week")]
        print(f"{cls:<22}{counts[cls]:>10,}{100*counts[cls]/total:>8.2f}{dw:>11,}"
              f"{(100*dw/counts[cls] if counts[cls] else 0):>8.2f}")
    print(f"{'total':<22}{total:>10,}{100.0:>8.2f}")


if __name__ == "__main__":
    main()
