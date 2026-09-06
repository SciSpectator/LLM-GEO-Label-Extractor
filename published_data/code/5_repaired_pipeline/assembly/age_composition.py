"""Classify every final Age value and reproduce the Table 1, S3 and S11 age figures.

Usage:
    python3 age_composition.py [final_labels.csv.gz] [out.csv]

Five mutually exclusive classes:
    chronological   a parseable age within a plausible human range
    non_age         the string carries a non-age unit or keyword (passage number,
                    assay time point, hours post-treatment, dose, barcode)
    implausible     parses above 120 years, or carries digits but no readable number
    descriptor      an age term with no number at all ("adult", "newborn")
    not_specified   a missing-value token in any of its surface forms

"Age labels extracted" as reported in Table 1 and Table S3 is every class except
not_specified. Age summaries and the Figure S1 histogram use the chronological class only;
non_age and implausible together are the "spurious" rows of Table S11.
"""

import csv
import gzip
import re
import sys
from collections import Counter, defaultdict

MISSING = {
    "", "not specified", "na", "n/a", "n.a.", "none", "null", "unknown",
    "unspecified", "not applicable", "missing", "nd", "n.d.", "--", "-", "---",
    "?", "??", "not available", "not collected", "not reported",
    "not determined", "not known", "not given", "unkown", "no", "-.-", ".",
}

PREFIX = re.compile(r"^\s*age\s*:\s*", re.I)


def _unit_pattern(*roots):
    """Boundary-robust matcher for a unit concept given its root spellings.

    REPAIR-4 root cause (not a per-spelling patch): `\\b` requires a
    transition between a word char and a non-word char, but a digit and an
    adjacent letter are BOTH \\w, so a plain `\\bhour\\b`-style pattern never
    matches a digit-attached form ("24hours", "2.8mos") at all -- the value
    falls through unscaled. `(?<![A-Za-z])` fixes that once, structurally,
    for every root passed in: it only rejects a PRECEDING LETTER (so
    "Monday" still doesn't false-match "day"), not a preceding digit.
    `s?` covers the plural and `\\.?` covers the abbreviation-with-period
    style ("mo.", "yr."), so adding a future spelling variant is a one-word
    addition to the root list here, not a new hand-tuned regex alternative.

    The same digit/\\w collision exists on the TRAILING side when the unit
    comes before an attached digit ("day1", "D1", "M6"): plain `\\b` again
    sees two \\w chars (a letter then a digit) and refuses to match, so the
    value is silently treated as unitless. `(?![A-Za-z])` mirrors the fix
    on this side -- block a following LETTER (so "day" still doesn't
    false-match inside "daylight"), allow a following digit or end-of-string.
    """
    alt = "|".join(re.escape(r) for r in sorted(set(roots), key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z])(?:{alt})\.?s?(?![A-Za-z])", re.I)


NON_AGE = re.compile(
    r"(passage|\bp\d+\b|time\s*point|timepoint|\bcycle\b|batch|barcode|"
    r"\bdpi\b|\bhpi\b|post[-\s]?(infection|treatment|transplant|surgery)|"
    r"\bstage\b|\bgrade\b|\bscore\b|%|\bdose\b|\bgy\b|\bnm\b|\bum\b|\bmg\b|"
    r"\bml\b|" + _unit_pattern("hour", "hr").pattern + r"|" +
    _unit_pattern("min", "minute").pattern + r")",
    re.I,
)

UNITS = [
    (_unit_pattern("year", "yr", "y"), 1.0),
    # "mos" (mo+s) was missing under the old \bmo\b pattern: \b requires a
    # boundary right after "mo", but "s" is a word char too, so the plural
    # abbreviation never matched -- confirmed live: "age (mos): 2.8" was
    # released as 2.8 years, not 2.8 months. Fixed structurally above, not
    # by special-casing "mos": any future root sharing this shape (a short
    # abbreviation pluralized, or attached directly to a digit) is already
    # covered by _unit_pattern, not just this one spelling.
    (_unit_pattern("month", "mo"), 1 / 12),
    (_unit_pattern("week", "wk"), 1 / 52),
    (_unit_pattern("day"), 1 / 365),  # no "d" root: too short, not evidenced -- would risk new false-positives
    # NOT enabled: hour/hr as a scaling unit -- see REPAIR_PROPOSAL_v5.md
    # "Decyzje": whether an hour is ever a valid donor age is the user's
    # call, not assumed here. NON_AGE above still correctly reclassifies
    # every hour-denominated value as non_age instead of chronological.
]

MAX_YEARS = 120


def classify(value):
    v = PREFIX.sub("", (value or "").strip()).strip()
    if v.lower() in MISSING:
        return "not_specified"
    if not re.search(r"\d", v):
        return "descriptor"
    if NON_AGE.search(v):
        return "non_age"
    match = re.search(r"(\d+(?:\.\d+)?)", v)
    if not match:
        return "implausible"
    years = float(match.group(1))
    for pattern, factor in UNITS:
        if pattern.search(v):
            years *= factor
            break
    return "implausible" if years > MAX_YEARS else "chronological"


def main():
    corpus = sys.argv[1] if len(sys.argv) > 1 else "../../05_final_labels/LLM_labels_all_samples.csv.gz"
    out = sys.argv[2] if len(sys.argv) > 2 else "age_composition.csv"

    counts = Counter()
    examples = defaultdict(Counter)
    with gzip.open(corpus, "rt") as fh:
        for row in csv.DictReader(fh):
            value = row.get("final_Age")
            cls = classify(value)
            counts[cls] += 1
            if cls in ("non_age", "implausible", "descriptor"):
                examples[cls][PREFIX.sub("", (value or "").strip()).strip().lower()] += 1

    total = sum(counts.values())
    extracted = total - counts["not_specified"]
    spurious = counts["non_age"] + counts["implausible"]

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["class", "samples", "pct_of_corpus", "pct_of_age_labels"])
        for cls in ("chronological", "descriptor", "non_age", "implausible", "not_specified"):
            pct_age = f"{100 * counts[cls] / extracted:.2f}" if cls != "not_specified" else ""
            w.writerow([cls, counts[cls], f"{100 * counts[cls] / total:.2f}", pct_age])
        w.writerow(["total_spurious", spurious, f"{100 * spurious / total:.2f}",
                    f"{100 * spurious / extracted:.2f}"])
        w.writerow(["age_labels_extracted", extracted, f"{100 * extracted / total:.2f}", "100.00"])
        w.writerow(["corpus_total", total, "100.00", ""])

    for cls in ("chronological", "descriptor", "non_age", "implausible", "not_specified"):
        print(f"{cls:<22}{counts[cls]:>9,}{100 * counts[cls] / total:>9.2f}%")
    print(f"{'total spurious':<22}{spurious:>9,}{100 * spurious / extracted:>9.2f}% of age labels")
    print(f"{'age labels extracted':<22}{extracted:>9,}{100 * extracted / total:>9.2f}%")
    print(f"{'corpus total':<22}{total:>9,}")
    for cls in ("non_age", "implausible"):
        print(f"\ntop {cls}: " + ", ".join(f"{k} ({n})" for k, n in examples[cls].most_common(5)))


if __name__ == "__main__":
    main()
