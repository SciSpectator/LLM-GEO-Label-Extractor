"""Transparent selection of GEO accessions, fields, and pipeline scope."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

ALL_LABELS = ("Sex", "Age", "Tissue", "Condition", "Treatment")
_STOP = {"and", "all", "for", "from", "the", "with", "extract", "extraction",
         "sample", "samples", "phase", "only", "metadata", "labels", "label",
         "normalize", "normalization", "full", "without", "verbatim"}


@dataclass
class Selection:
    gsms: list[str] = field(default_factory=list)
    gpls: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    stop_after: str = "phase2"
    explanation: str = ""


def _ids(spec: str, prefix: str) -> list[str]:
    numbers = re.findall(rf"\b{prefix}\s*[-:]?\s*(\d+)\b", spec, re.I)
    return sorted({prefix + number for number in numbers})


def _assistant_plan(spec: str, model: str) -> dict:
    from geo_label_extractor.llm_backend import chat
    prompt = """Convert the user's GEO dataset request into a strict JSON search plan.
Return JSON only with keys: gsm (array), gpl (array), search_terms (array of concise
biomedical/platform phrases), labels (subset of Sex, Age, Tissue, Condition,
Treatment), and stop_after (phase1, phase1b, or phase2). Do not invent accessions.
phase1 means verbatim extraction, phase1b adds GSE context, phase2 adds normalization.

USER REQUEST:
""" + spec
    raw = chat([{"role": "user", "content": prompt}], model=model,
               temperature=0, seed=42, num_predict=600, think=False, timeout=120)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise RuntimeError("The AI Assistant did not return a valid GEO search plan.")
    return json.loads(match.group(0))


def resolve_spec(database: str, spec: str, max_platforms: int = 100,
                 model: str = "google/gemma-4-12b-it") -> Selection:
    """Use the configured LLM to interpret a request and search GEOmetadb."""
    plan = _assistant_plan(spec, model)
    gsms = sorted({str(v).upper() for v in plan.get("gsm", []) if re.fullmatch(r"GSM\d+", str(v), re.I)})
    gpls = sorted({str(v).upper() for v in plan.get("gpl", []) if re.fullmatch(r"GPL\d+", str(v), re.I)})
    labels = [label for label in ALL_LABELS if label in plan.get("labels", [])] or list(ALL_LABELS)
    stop_after = plan.get("stop_after", "phase2")
    if stop_after not in {"phase1", "phase1b", "phase2"}:
        stop_after = "phase2"

    if not gsms and not gpls and database:
        terms = [str(value).strip().casefold() for value in plan.get("search_terms", []) if str(value).strip()]
        tokens = [word for term in terms[:8] for word in re.findall(r"[a-z0-9-]{3,}", term)
                  if word not in _STOP]
        connection = sqlite3.connect(database)
        try:
            clauses, params = [], []
            for token in tokens[:8]:
                clauses.append("LOWER(COALESCE(title,'') || ' ' || "
                               "COALESCE(organism,'') || ' ' || "
                               "COALESCE(technology,'')) LIKE ?")
                params.append(f"%{token}%")
            where = " AND ".join(clauses) if clauses else "1=0"
            rows = connection.execute(
                f"SELECT gpl FROM gpl WHERE {where} ORDER BY gpl LIMIT ?",
                (*params, max_platforms)).fetchall()
            gpls = [str(row[0]) for row in rows]
        finally:
            connection.close()
    explanation = (f"{len(gsms)} GSM, {len(gpls)} GPL; "
                   f"labels={','.join(labels)}; stop_after={stop_after}")
    return Selection(gsms, gpls, labels, stop_after, explanation)
