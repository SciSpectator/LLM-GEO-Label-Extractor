"""Export the finished corpus: one directory per platform, one table per file.

This is the last stage of the pipeline. It writes, for every sample, the source
metadata it was extracted from and the label at every stage, so a reader can see
what the model was given and what each stage concluded without re-running
anything.

Samples are grouped by GPL because a platform is the unit analyses are usually
restricted to, and because per-platform files stay small enough to open
directly. Each file is gzipped CSV.

Columns, in order:

  identity      gsm, gse, gpl
  input         title, source, characteristics, treatment_protocol, description
                -- the text the extractor actually saw
  extraction    phase1_<label>   verbatim values from the first pass
                phase1b_<label>  values after context refinement
  normalization phase2_<label>   controlled-vocabulary value
                phase2_<label>_id        MeSH / Cellosaurus / OOV identifier
                phase2_<label>_stage      which stage decided it
  final         final_<label>    the curated value that ships
                final_<label>_id
                final_<label>_stage

Sex and Age are carried through normalization unchanged; their final value is
their extracted value.
"""

from __future__ import annotations

import csv
import glob
import gzip
import json
import os
import re
from collections import Counter, defaultdict

LABELS = ("Sex", "Age", "Tissue", "Condition", "Treatment")
NORMALIZED = ("Tissue", "Condition", "Treatment")
META = ("title", "source", "characteristics", "treatment_protocol", "description")


def _safe(name: str) -> str:
    """A platform accession used as a directory name."""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "unknown")).strip("_")
    return s or "unknown"


def _clean(v) -> str:
    """CSV-safe single-line text."""
    if v is None:
        return ""
    return re.sub(r"[\r\n\t]+", " ", str(v)).strip()


_AGE_UNIT_MAP = {
    "years": "years",
    "year": "years",
    "yrs": "years",
    "yr": "years",
    "y": "years",
    "months": "months",
    "month": "months",
    "mo": "months",
    "mos": "months",
    "weeks": "weeks",
    "week": "weeks",
    "wk": "weeks",
    "wks": "weeks",
    "days": "days",
    "day": "days",
    "d": "days",
    "hours": "hours",
    "hour": "hours",
    "hr": "hours",
    "h": "hours",
    "pcw": "pcw",
    "wpc": "pcw",
    "gw": "pcw",
}
_AGE_GROUPS = (
    "embryonic",
    "fetal",
    "foetal",
    "neonatal",
    "newborn",
    "infant",
    "pediatric",
    "paediatric",
    "child",
    "juvenile",
    "adolescent",
    "young adult",
    "old adult",
    "middle aged",
    "middle-aged",
    "elderly",
    "aged",
    "adult",
)
_AGE_KEY = re.compile(r"^\s*([A-Za-z_ ()]{1,24}?)\s*[:=]\s*(.+?)\s*$")
_NUM = r"\d+(?:\.\d+)?"
_RANGE = re.compile(rf"({_NUM})\s*(?:-|–|to|<=\s*age\s*<)\s*({_NUM})", re.I)
_GE = re.compile(rf"(?:>=|>|at least|over)\s*({_NUM})", re.I)
_LE = re.compile(rf"(?:<=|<|under|less than)\s*({_NUM})", re.I)
_ONE = re.compile(rf"^\s*({_NUM})\s*$")


def _age_parts(value) -> dict:
    """Split an extracted age into display, number, unit, bounds and stage."""
    out = {"display": "", "value": "", "unit": "", "min": "", "max": "", "group": ""}
    v = _clean(value)
    if not v or v == "Not Specified":
        out["display"] = v
        return out

    unit = ""
    m = _AGE_KEY.match(v)
    if m:
        key, rest = m.group(1).strip().lower(), m.group(2).strip()
        if key in _AGE_UNIT_MAP:
            unit = _AGE_UNIT_MAP[key]
        body = rest or v
    else:
        body = v

    low = body.lower()
    for u, canon in _AGE_UNIT_MAP.items():
        if re.search(rf"\b{re.escape(u)}\b", low):
            unit = unit or canon
            break
    for g in _AGE_GROUPS:
        if g in low:
            out["group"] = (
                "middle aged"
                if g in ("middle-aged", "middle aged")
                else (
                    "fetal"
                    if g == "foetal"
                    else ("pediatric" if g == "paediatric" else g)
                )
            )
            break

    r = _RANGE.search(body)
    if r:
        out["min"], out["max"] = r.group(1), r.group(2)
    else:
        ge, le = _GE.search(body), _LE.search(body)
        if ge:
            out["min"] = ge.group(1)
        if le:
            out["max"] = le.group(1)
        if not ge and not le:
            one = _ONE.match(body)
            if one:
                out["value"] = one.group(1)
            else:
                nums = re.findall(_NUM, body)
                if len(nums) == 1:
                    out["value"] = nums[0]

    out["unit"] = unit
    if out["value"] and unit:
        out["display"] = f"{out['value']} {unit}"
    elif out["min"] and out["max"]:
        out["display"] = f"{out['min']}-{out['max']}" + (f" {unit}" if unit else "")
    elif out["group"]:
        out["display"] = out["group"]
    else:
        out["display"] = body
    return out


def header() -> list:
    cols = ["gsm", "gse", "gpl"]
    cols += list(META)
    for stage in ("phase1", "phase1b"):
        cols += [f"{stage}_{c}" for c in LABELS]
    for c in NORMALIZED:
        cols += [
            f"phase2_{c}",
            f"phase2_{c}_id",
            f"phase2_{c}_mesh_id",
            f"phase2_{c}_cell_id",
            f"phase2_{c}_stage",
        ]
    for c in LABELS:
        cols += [f"final_{c}"]
        if c == "Age":
            cols += [
                "final_Age_value",
                "final_Age_unit",
                "final_Age_min",
                "final_Age_max",
                "final_Age_group",
            ]
        if c in NORMALIZED:
            cols += [
                f"final_{c}_id",
                f"final_{c}_mesh_id",
                f"final_{c}_cell_id",
                f"final_{c}_stage",
                f"final_{c}_curated",
            ]
    return cols


def _row(s: dict, res: dict) -> list:
    p1 = s.get("phase1") or {}
    p1b = s.get("phase1b") or {}
    p2 = s.get("phase2") or {}
    p2id = s.get("phase2_id") or {}
    p2mesh = s.get("phase2_mesh_id") or {}
    p2cell = s.get("phase2_cell_id") or {}
    p2stage = s.get("phase2_stage") or {}
    p2cur = s.get("phase2_curated") or {}

    out = [_clean(s.get("gsm")), _clean(s.get("gse")), _clean(s.get("gpl"))]
    out += [_clean(s.get(k)) for k in META]
    out += [_clean(p1.get(c)) for c in LABELS]
    out += [_clean(p1b.get(c)) for c in LABELS]

    for c in NORMALIZED:
        out += [
            _clean(p2.get(c)),
            _clean(p2id.get(c)),
            _clean(p2mesh.get(c)),
            _clean(p2cell.get(c)),
            _clean(p2stage.get(c)),
        ]

    for c in LABELS:
        if c not in NORMALIZED:
            v = p1b.get(c) or p1.get(c)
            if c == "Age":
                a = _age_parts(v)
                out += [
                    a["display"],
                    a["value"],
                    a["unit"],
                    a["min"],
                    a["max"],
                    a["group"],
                ]
            else:
                out.append(_clean(v))
            continue
        out += [
            _clean(p2.get(c)),
            _clean(p2id.get(c)),
            _clean(p2mesh.get(c)),
            _clean(p2cell.get(c)),
            _clean(p2stage.get(c)),
            _clean(p2cur.get(c)) or "no",
        ]
    return out


def export(corpus, dictionary: str, out_dir: str, log=print) -> dict:
    """Write one gzipped CSV per platform. Returns per-platform sample counts.

    `corpus` is either an explicit list of applied files or a glob matching them.
    A list is preferred: the applied filenames follow the input names, so a
    pattern assumed here can silently match nothing and produce no output.
    """
    with gzip.open(dictionary, "rt") as fh:
        res = json.load(fh)["resolutions"]

    candidates = corpus if isinstance(corpus, (list, tuple)) else glob.glob(corpus)
    paths = sorted(
        p
        for p in candidates
        if not re.search(r"\.(partial|tmp|ckpt)\b", os.path.basename(p))
    )
    if not paths:
        raise SystemExit(f"no corpus files to export from {corpus!r}")
    os.makedirs(out_dir, exist_ok=True)

    cols = header()
    writers: dict = {}
    handles: dict = {}
    counts = Counter()
    try:
        for p in paths:
            op = gzip.open if p.endswith(".gz") else open
            with op(p, "rt") as fh:
                d = json.load(fh)
            rows = d["samples"] if isinstance(d, dict) and "samples" in d else d
            for s in rows:
                gpl = _safe(s.get("gpl"))
                if gpl not in writers:
                    dest = os.path.join(out_dir, gpl)
                    os.makedirs(dest, exist_ok=True)
                    fh_out = gzip.open(
                        os.path.join(dest, f"{gpl}.csv.gz"), "wt", newline=""
                    )
                    w = csv.writer(fh_out)
                    w.writerow(cols)
                    handles[gpl] = fh_out
                    writers[gpl] = w
                writers[gpl].writerow(_row(s, res))
                counts[gpl] += 1
            log(
                f"  exported {os.path.basename(p)} "
                f"({sum(counts.values()):,} samples so far)"
            )
    finally:
        for fh_out in handles.values():
            fh_out.close()

    manifest = {
        "platforms": len(counts),
        "samples": sum(counts.values()),
        "columns": cols,
        "per_platform": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    log(
        f"exported {sum(counts.values()):,} samples across {len(counts):,} platforms"
    )
    for gpl, n in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
        log(f"    {gpl :<14} {n :,}")
    return dict(counts)


if __name__ == "__main__":
    import sys

    export(
        sys.argv[1] if len(sys.argv) > 1 else "p2_[0-7].json",
        sys.argv[2] if len(sys.argv) > 2 else "phase2_dictionary.json.gz",
        sys.argv[3] if len(sys.argv) > 3 else "export",
    )
