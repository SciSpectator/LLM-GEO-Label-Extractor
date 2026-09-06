"""Phase 2 — out-of-vocabulary surface folding (final normalization step).

The cascade mints out-of-vocabulary (OOV) concepts per surface form and routes
acronym-shaped tokens (any upper-case letter / digit) through a per-study
short-form path that is excluded from the BioLORD+LLM consolidation. As a
result the same concept could fragment across ids that differ only in case,
whitespace, or hyphenation (e.g. ``PBMC``/``pbmc``, ``wild type``/``wildtype``,
``Mock``/``mock``). This step folds every OOV/ART concept whose label is
identical up to case, whitespace, hyphen, underscore, or dot onto a single
canonical id and label (the most frequent surface form in the corpus). It is
deterministic, closed under the fold, and safe: only OOV/ART assignments are
touched — MeSH descriptors and Cellosaurus identifiers are never changed.

Usage:  python3 phase2_casefold.py <results_dir>
        (results_dir holds dictionary.json.gz and final/<GPL>/<GPL>.csv.gz)
"""

import gzip, json, re, glob, csv, collections, os, sys

FIELDS = ["Tissue", "Condition", "Treatment"]
NS = {
    "",
    "not specified",
    "na",
    "n/a",
    "none",
    "unknown",
    "not applicable",
    "unspecified",
}


def isns(v):
    return str(v).strip().lower() in NS


def is_oov(idv):
    return (idv or "").split(";")[0].strip().startswith(("OOV", "ART"))


def fold_key(s):
    return re.sub(r"[\s\-_\.]+", "", str(s).strip().lower())


def run(results):
    files = sorted(
        glob.glob(os.path.join(results, "final", "**", "*.csv.gz"), recursive=True)
    )

    key_id = collections.defaultdict(collections.Counter)
    key_lab = collections.defaultdict(collections.Counter)
    for fp in files:
        with gzip.open(fp, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                for F in FIELDS:
                    lab = r.get(f"final_{F}", "")
                    idv = r.get(f"final_{F}_id", "")
                    if isns(lab) or not is_oov(idv):
                        continue
                    k = (F, fold_key(lab))
                    key_id[k][idv] += 1
                    key_lab[k][lab] += 1
    canon = {
        k: (key_id[k].most_common(1)[0][0], key_lab[k].most_common(1)[0][0])
        for k in key_id
    }
    ids_before = len({(k[0], i) for k in key_id for i in key_id[k]})
    ids_after = len({(k[0], v[0]) for k, v in canon.items()})
    print(
        f"OOV concepts before fold: {ids_before :,}  ->  after: {ids_after :,}  "
        f"(merged {ids_before - ids_after :,})"
    )

    remapped = 0
    for fp in files:
        with gzip.open(fp, "rt", newline="") as fh:
            rd = csv.DictReader(fh)
            fieldnames = rd.fieldnames
            rows = list(rd)
        for r in rows:
            for F in FIELDS:
                lab = r.get(f"final_{F}", "")
                idv = r.get(f"final_{F}_id", "")
                if isns(lab) or not is_oov(idv):
                    continue
                cid, clab = canon[(F, fold_key(lab))]
                if idv != cid or lab != clab:
                    r[f"final_{F}_id"] = cid
                    r[f"final_{F}"] = clab
                    remapped += 1
        with gzip.open(fp, "wt", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    print(
        f"remapped {remapped :,} sample-field assignments across {len(files):,} platform files"
    )

    # Two normalizers write this file under two names: phase2.py calls it
    # dictionary.json.gz, run_phase2.py -- the one the pipeline drives --
    # calls it phase2_dictionary.json.gz. Accept whichever is present rather
    # than failing the whole case-fold on the spelling.
    for candidate in ("dictionary.json.gz", "phase2_dictionary.json.gz"):
        dpath = os.path.join(results, candidate)
        if os.path.exists(dpath):
            break
    else:
        raise SystemExit(
            f"no dictionary in {results}: expected dictionary.json.gz or "
            f"phase2_dictionary.json.gz"
        )
    d = json.load(gzip.open(dpath, "rt"))
    # The two writers also differ in shape, not just in name: phase2.py puts the
    # fields at the top level, run_phase2.py wraps them under "resolutions"
    # alongside n_samples and shorts. Edit whichever mapping actually holds the
    # fields; `d` is still what gets written back, so the wrapper survives.
    fields = d["resolutions"] if isinstance(d.get("resolutions"), dict) else d
    for F in FIELDS:
        if F not in fields:
            continue
        for raw, v in fields[F].items():
            if is_oov(v.get("id")) and not isns(v.get("target", raw)):
                k = (F, fold_key(v.get("target") or raw))
                if k in canon:
                    v["id"], v["target"] = canon[k]
            for gse, rv in (v.get("by_gse") or {}).items():
                if is_oov(rv.get("id")) and not isns(rv.get("target", "")):
                    k = (F, fold_key(rv.get("target") or ""))
                    if k in canon:
                        rv["id"], rv["target"] = canon[k]
    json.dump(d, gzip.open(dpath, "wt"))
    print(f"{os.path.basename(dpath)} updated")
    return ids_before, ids_after


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ".")
