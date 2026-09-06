#!/usr/bin/env python3
"""Final assembly: connect the per-platform outputs into one shipped corpus.

The manuscript ran extraction, normalization and assembly as three separate
jobs, one per GPU allocation, and merged their outputs by hand. This module is
the connected form of that last step: it reads the per-platform tables written
by Phase 2 export and produces the single five-label corpus the pipeline ships.

Two things happen here:

  merge          Every ``<GPL>/<GPL>.csv.gz`` table is concatenated, in a fixed
                 order, into one ``LLM_labels_all_samples.csv.gz`` carrying all
                 five final labels (Sex and Age from extraction, Tissue,
                 Condition and Treatment from normalization). A per-platform
                 copy is also written under ``by_GPL/`` for platform-scoped
                 analyses.

  relocate       A normalized cell may list several identifiers when its
                 phase-1 value fragmented across studies. Where the same
                 identifier is repeated in one cell, the duplicate label and its
                 identifier are dropped so a value cannot appear twice under one
                 concept. This is the id-level relocation of mislabeled entries
                 that the paper performed before reporting corpus counts.

The stage costs one linear pass over the exported tables and no model calls, so
it is safe to re-run and cheap to keep as the pipeline's final connected step.
"""
from __future__ import annotations

import csv
import glob
import gzip
import json
import os
import re
import sys

NS = "Not Specified"
NORMALIZED_FIELDS = ("Tissue", "Condition", "Treatment")
AGE_PREFIX = re.compile(r"^\s*age\s*:\s*", re.I)
MISSING = {
    "", "not specified", "na", "n/a", "n.a.", "none", "null", "unknown",
    "unspecified", "not applicable", "missing", "nd", "n.d.", "--", "-", "---",
    "?", "??", "not available", "not collected", "not reported",
    "not determined", "not known", "not given", "unkown", "no", "-.-", ".",
}


def _log(msg: str) -> None:
    print(f"[assemble] {msg}", flush=True)


def is_present(value: str, age: bool = False) -> bool:
    text = (value or "").strip()
    if age:
        text = AGE_PREFIX.sub("", text)
    return text.lower() not in MISSING


def deduplicate_ids(labels: str, identifiers: str) -> tuple[str, str]:
    """Drop repeated (label, id) pairs within one normalized cell.

    A cell holding ``A; B; A`` under ids ``D1; D2; D1`` becomes ``A; B`` under
    ``D1; D2``. Cells whose label and id counts disagree, or that hold a single
    value, are returned untouched: there is nothing to relocate.
    """
    label_parts = [value.strip() for value in labels.split(";")]
    id_parts = [value.strip() for value in identifiers.split(";")]
    if len(label_parts) != len(id_parts) or len(id_parts) <= 1:
        return labels, identifiers
    seen: set[str] = set()
    kept_labels: list[str] = []
    kept_ids: list[str] = []
    for label, identifier in zip(label_parts, id_parts):
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        kept_labels.append(label)
        kept_ids.append(identifier)
    return "; ".join(kept_labels), "; ".join(kept_ids)


def _platform_tables(final_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(final_dir, "*", "*.csv.gz")))


def assemble(final_dir: str, out_dir: str, force: bool = False) -> dict:
    """Merge the per-platform Phase 2 tables into one shipped corpus.

    ``final_dir`` is the Phase 2 export directory (``<GPL>/<GPL>.csv.gz`` per
    platform). ``out_dir`` receives ``LLM_labels_all_samples.csv.gz``, a
    ``by_GPL/`` mirror, and ``manifest.json``. Returns the manifest.
    """
    sources = _platform_tables(final_dir)
    if not sources:
        raise SystemExit(f"no per-platform tables below {final_dir}")

    if os.path.exists(out_dir) and not force:
        marker = os.path.join(out_dir, "LLM_labels_all_samples.csv.gz")
        if os.path.exists(marker):
            raise SystemExit(
                f"final corpus already exists: {marker} (use force to rebuild)")

    by_gpl_dir = os.path.join(out_dir, "by_GPL")
    os.makedirs(by_gpl_dir, exist_ok=True)
    master_path = os.path.join(out_dir, "LLM_labels_all_samples.csv.gz")
    master_handle = gzip.open(master_path, "wt", newline="")
    master_writer = None
    shared_fields: list[str] | None = None

    final_gsms: set[str] = set()
    platform_counts: dict[str, int] = {}
    sex_present = age_present = 0
    relocated = 0

    try:
        for source in sources:
            gpl = os.path.basename(os.path.dirname(source))
            platform_dir = os.path.join(by_gpl_dir, gpl)
            os.makedirs(platform_dir, exist_ok=True)
            target = os.path.join(platform_dir, f"{gpl}.csv.gz")
            count = 0
            with gzip.open(source, "rt", newline="") as src, \
                    gzip.open(target, "wt", newline="") as tgt:
                reader = csv.DictReader(src)
                fields = list(reader.fieldnames or [])
                if shared_fields is None:
                    shared_fields = fields
                elif fields != shared_fields:
                    raise SystemExit(
                        f"platform {gpl} header differs from the corpus header; "
                        f"all Phase 2 tables must share one schema")
                writer = csv.DictWriter(tgt, fieldnames=fields)
                writer.writeheader()
                if master_writer is None:
                    master_writer = csv.DictWriter(master_handle,
                                                   fieldnames=fields)
                    master_writer.writeheader()
                for row in reader:
                    gsm = row.get("gsm", "")
                    if gsm in final_gsms:
                        raise RuntimeError(f"duplicate final GSM: {gsm}")
                    final_gsms.add(gsm)
                    for field_name in NORMALIZED_FIELDS:
                        label_key = f"final_{field_name}"
                        id_key = f"final_{field_name}_id"
                        if ";" in (row.get(id_key) or ""):
                            before = row.get(id_key)
                            row[label_key], row[id_key] = deduplicate_ids(
                                row.get(label_key) or "", row.get(id_key) or "")
                            if row[id_key] != before:
                                relocated += 1
                    sex_present += is_present(row.get("final_Sex", ""))
                    age_present += is_present(row.get("final_Age", ""), age=True)
                    writer.writerow(row)
                    master_writer.writerow(row)
                    count += 1
            platform_counts[gpl] = count
    finally:
        master_handle.close()

    manifest = {
        "corpus": "LLM GEO five-label final corpus",
        "samples": len(final_gsms),
        "platforms": len(platform_counts),
        "sex_present": sex_present,
        "age_present": age_present,
        "relocated_cells": relocated,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    _log(f"wrote {master_path}: {len(final_gsms):,} samples, "
         f"{len(platform_counts):,} platforms; Sex present {sex_present:,}, "
         f"Age present {age_present:,}; {relocated:,} cells relocated")
    return manifest


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Assemble the final five-label GEO corpus from the "
                    "per-platform Phase 2 tables.")
    ap.add_argument("--final-dir", required=True,
                    help="Phase 2 export directory (<GPL>/<GPL>.csv.gz)")
    ap.add_argument("--out-dir", required=True,
                    help="directory for the merged corpus and manifest")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if a final corpus already exists")
    args = ap.parse_args()
    assemble(args.final_dir, args.out_dir, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
