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

PHASE2_DIR = "Directory to Phase 2 per-GPL output"
PHASE1_DIR = "Directory to Phase 1/1b output"
AGE_FILE = "Directory to Age run file"
OUT_DIR = "Directory to output"


NS = "Not Specified"


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
