"""Assemble the final five-label corpus.

The Phase 2 per-platform outputs contain only the normalized Tissue, Condition and
Treatment columns. This script adds the two demographic fields — Sex (from the Phase 1/1b
output) and Age (from the separate Age extraction) — to every sample and writes the
merged corpus.

Sex and Age are finalized in Phase 1 and are never normalized to a controlled vocabulary,
which is why they are joined in here rather than emitted by Phase 2.

Before running, set the four locations below to your own directories/files.
Output: <OUT_DIR>/LLM_labels_all_samples.csv.gz (all five labels)
        + <OUT_DIR>/by_GPL/<GPL>/<GPL>.csv.gz
"""

from __future__ import annotations
import csv, glob, gzip, json, os, sys

try:
    from cellline_db import CellLineDB
except Exception:            # reference vocabulary optional; rule then no-ops
    CellLineDB = None

# The four locations. They keep their published placeholder defaults so the
# script still reads as documentation, but each can be supplied by environment
# variable, which is what an unattended batch job needs: editing source in place
# per run is how two runs end up silently pointing at different inputs.
PHASE2_DIR = os.environ.get("MERGE_PHASE2_DIR", "Directory to Phase 2 per-GPL output")
PHASE1_DIR = os.environ.get("MERGE_PHASE1_DIR", "Directory to Phase 1/1b output")
AGE_FILE = os.environ.get("MERGE_AGE_FILE", "Directory to Age run file")
OUT_DIR = os.environ.get("MERGE_OUT_DIR", "Directory to output")


NS = "Not Specified"

# Anatomy that only one sex has. Read from the MeSH tree, not enumerated here:
# A05.360.319 is Genitalia, Female and A05.360.444 is Genitalia, Male. Terms
# that are ALSO embryonic structures (A16) are excluded, because the
# maternal-fetal interface -- decidua above all -- legitimately belongs to a
# male sample: the tissue is the mother's, the sample is the fetus.
MESH_DB_PATH = os.environ.get("MESH_DB", "")


def _sex_specific_anatomy():
    if not MESH_DB_PATH or not os.path.exists(MESH_DB_PATH):
        return set(), set()
    import sqlite3

    con = sqlite3.connect(f"file:{MESH_DB_PATH}?mode=ro", uri=True)

    def under(prefix):
        out = set()
        for (i,) in con.execute(
                "SELECT DISTINCT mesh_id FROM mesh_tree "
                "WHERE tree_number = ? OR tree_number LIKE ?",
                (prefix, prefix + ".%")):
            out.add(i)
        return out

    embryonic = under("A16")
    fem = under("A05.360.319") - embryonic
    mal = under("A05.360.444") - embryonic
    both = fem & mal
    return fem - both, mal - both


_FEMALE_ANATOMY, _MALE_ANATOMY = _sex_specific_anatomy()


def reconcile_sex_with_anatomy(row: dict, inferred: str) -> str:
    """Let sex-specific anatomy outrank an inferred Sex, on the same grounds.

    This is the cell-line rule generalised from one reference vocabulary to
    another: an attribute the resolved entity *attests* beats one inferred from
    surrounding prose. A sample whose Tissue resolves to Prostate is male
    whatever the study text suggested about the subject, in the same way that a
    sample which IS a catalogued line carries that line's donor sex.

    Deliberately narrow. It fires only when the tissue is unambiguously
    sex-specific in the MeSH anatomy tree AND the extracted Sex is a definite
    contradiction; a missing or non-binary Sex is left alone, since silence is
    not disagreement. Structures of the maternal-fetal interface are excluded
    from the vocabulary above, so a male sample of decidua or placenta is not
    touched.
    """
    if inferred not in ("Male", "Female") or not (_FEMALE_ANATOMY or _MALE_ANATOMY):
        return inferred
    ids = [i.strip() for i in (row.get("final_Tissue_id") or "").split(";") if i.strip()]
    hits_f = any(i in _FEMALE_ANATOMY for i in ids)
    hits_m = any(i in _MALE_ANATOMY for i in ids)
    if hits_f and not hits_m and inferred == "Male":
        return "Female"
    if hits_m and not hits_f and inferred == "Female":
        return "Male"
    return inferred


def reconcile_sex_with_cellline(row: dict) -> str:
    """Let a catalogued identity's attested donor sex outrank an inferred Sex.

    This assembly step is the ONLY place both facts exist at once: Phase 1
    infers Sex from study text and never sees a Cellosaurus resolution, while
    Phase 2 resolves the cell line and never sees Sex ("Sex/Age never enter
    phase 2"). A contradiction between them was therefore not merely missed --
    it was undetectable anywhere in the pipeline, which is why 571 of them
    reached the release, 216 from one line alone.

    The rule is about provenance, not about any particular line: a sample that
    IS a catalogued cell line has the donor's sex as a property of its
    identity, so a value inferred from surrounding prose cannot overrule it.
    Where the catalogue does not constrain sex ('Sex unspecified', ambiguous,
    mixed) the inferred value stands unchanged.
    """
    if CellLineDB is None:
        return row.get("final_Sex", NS)
    inferred = (row.get("final_Sex") or NS).strip()
    # Only a CONTRADICTION is repaired here. When Phase 1 returned no sex, the
    # catalogue could supply one, but that is a coverage change affecting 85,395
    # samples -- a different decision, with its own effect on every reported
    # Sex statistic, and not this defect. Silence is not disagreement, so a
    # missing or non-binary value is left exactly as extracted.
    if inferred not in ("Male", "Female"):
        return inferred
    ids = [i.strip() for i in (row.get("final_Tissue_id") or "").split(";")]
    db = CellLineDB.get()
    for cvcl in ids:
        if not cvcl.startswith("CVCL_"):
            continue
        attested = db.donor_sex(cvcl)
        if attested and attested != inferred:
            return attested
    return inferred


def load_sex(phase1_dir: str) -> dict:
    """gsm -> Sex, taken from each Phase 1/1b sample (phase1b preferred, else phase1)."""
    values = {}
    files = sorted(glob.glob(os.path.join(phase1_dir, "p1_*.json.gz")))
    if not files:
        sys.exit(f"no p1_*.json.gz under {phase1_dir}")
    for path in files:
        with gzip.open(path, "rt") as fh:
            data = json.load(fh)
        for sample in data.get("samples", data):
            gsm = sample["gsm"]
            stages = sample.get("phase1b") or sample.get("phase1") or {}
            value = str(stages.get("Sex") or NS).strip() or NS
            if gsm in values:
                raise RuntimeError(f"duplicate Phase 1 GSM: {gsm}")
            values[gsm] = value
    return values


def load_age(age_path: str) -> dict:
    """gsm -> Age, taken from the separate Age extraction."""
    with gzip.open(age_path, "rt") as fh:
        data = json.load(fh)
    values = {}
    for sample in data:
        gsm = sample["gsm"]
        value = str(sample.get("Age") or NS).strip() or NS
        if gsm in values:
            raise RuntimeError(f"duplicate Age GSM: {gsm}")
        values[gsm] = value
    return values


def main() -> None:
    if os.path.exists(OUT_DIR):
        sys.exit(f"output already exists: {OUT_DIR}")
    sex = load_sex(PHASE1_DIR)
    age = load_age(AGE_FILE)
    source_files = sorted(glob.glob(os.path.join(PHASE2_DIR, "*", "*.csv.gz")))
    if not source_files:
        sys.exit(f"no source CSV files below {PHASE2_DIR}")

    os.makedirs(os.path.join(OUT_DIR, "by_GPL"))
    master_path = os.path.join(OUT_DIR, "LLM_labels_all_samples.csv.gz")
    master_handle = gzip.open(master_path, "wt", newline="")
    master_writer = None
    final_gsms, platform_counts = set(), {}
    sex_present = age_present = 0
    try:
        for source_path in source_files:
            gpl = os.path.basename(os.path.dirname(source_path))
            platform_dir = os.path.join(OUT_DIR, "by_GPL", gpl)
            os.makedirs(platform_dir)
            target_path = os.path.join(platform_dir, f"{gpl}.csv.gz")
            count = 0
            with gzip.open(source_path, "rt", newline="") as src, gzip.open(
                target_path, "wt", newline=""
            ) as tgt:
                reader = csv.DictReader(src)
                fields = list(reader.fieldnames or []) + ["final_Sex", "final_Age"]
                writer = csv.DictWriter(tgt, fieldnames=fields)
                writer.writeheader()
                if master_writer is None:
                    master_writer = csv.DictWriter(master_handle, fieldnames=fields)
                    master_writer.writeheader()
                for row in reader:
                    gsm = row["gsm"]
                    if gsm in final_gsms:
                        raise RuntimeError(f"duplicate final GSM: {gsm}")
                    if gsm not in sex or gsm not in age:
                        raise RuntimeError(f"missing demographic label for {gsm}")
                    final_gsms.add(gsm)
                    row["final_Sex"] = sex[gsm]
                    row["final_Age"] = age[gsm]
                    sex_v = reconcile_sex_with_cellline(row)
                    row["final_Sex"] = reconcile_sex_with_anatomy(row, sex_v)
                    sex_present += row["final_Sex"] != NS
                    age_present += row["final_Age"] != NS
                    writer.writerow(row)
                    master_writer.writerow(row)
                    count += 1
            platform_counts[gpl] = count
    finally:
        master_handle.close()

    if final_gsms != set(sex) or final_gsms != set(age):
        raise RuntimeError("GSM sets differ across Phase 2 / Sex / Age sources")

    json.dump(
        {
            "corpus": "LLM GEO five-label final corpus",
            "samples": len(final_gsms),
            "platforms": len(platform_counts),
            "sex_present": sex_present,
            "age_present": age_present,
        },
        open(os.path.join(OUT_DIR, "manifest.json"), "w"),
        indent=2,
    )
    print(
        f"wrote {master_path}: {len(final_gsms):,} samples, {len(platform_counts)} platforms; "
        f"Sex present {sex_present :,}, Age present {age_present :,}"
    )


if __name__ == "__main__":
    main()
