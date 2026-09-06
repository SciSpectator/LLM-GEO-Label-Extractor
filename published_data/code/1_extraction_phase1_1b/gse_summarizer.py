"""Heuristic GSE summariser — no LLM.

A long ``gse_summary`` (some 1000+ char) inflates the Phase 1b system
prompt and hurts the Ollama KV-cache hit-rate. This module produces a
deterministic compressed form by retaining the most informative
sentences and clipping to ``max_chars``.

Algorithm (intentionally simple, no third-party deps):

  1. Concatenate ``gse_title`` and ``gse_summary`` with a separator.
  2. Sentence-split on ``[.!?]\\s``.
  3. Score each sentence by frequency of biomedical keywords (organ,
     disease, treatment, control, age, gender) and prefer earlier
     positions (lead-bias).
  4. Keep the highest-scoring sentences in original order until
     ``max_chars`` is reached.
  5. Always include the title plus the first sentence of the summary.

Result is cached in
:pyclass:`gse_context_cache.GSEContextCache.gse_summary_compressed`,
keyed by a content hash so a changed source summary refreshes lazily.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, Optional

from gse_context_cache import GSEContextCache

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")

_KEYWORDS = {
    "tissue",
    "cell",
    "organ",
    "sample",
    "biopsy",
    "cohort",
    "group",
    "disease",
    "disorder",
    "syndrome",
    "control",
    "case",
    "patient",
    "subject",
    "treatment",
    "drug",
    "dose",
    "vehicle",
    "vs",
    "versus",
    "compared",
    "mutant",
    "wild-type",
    "wildtype",
    "gene",
    "expression",
    "methylation",
    "rna",
    "dna",
    "age",
    "gender",
    "sex",
    "male",
    "female",
    "year",
    "years",
    "weeks",
    "human",
    "mouse",
    "rat",
}


def _hash(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _score_sentence(s: str, position: int) -> float:
    toks = re.findall(r"\b[a-zA-Z][a-zA-Z\-]+\b", s.lower())
    if not toks:
        return 0.0
    kw_hits = sum(1 for t in toks if t in _KEYWORDS)

    pos_bonus = max(0.0, 2.0 - position * 0.05)
    length_penalty = 0.0 if 30 <= len(s) <= 300 else 0.5
    return kw_hits + pos_bonus - length_penalty


def compress_gse_meta(
    gse_title: str, gse_summary: str, gse_design: str = "", max_chars: int = 512
) -> str:
    """Produce a compressed summary string ≤ ``max_chars``.

    The output begins with ``Title:`` then the highest-scoring summary
    sentences in original order, optionally followed by the design
    sentence if room remains.
    """
    title = (gse_title or "").strip()
    summary = (gse_summary or "").strip()
    design = (gse_design or "").strip()

    head = f"Title: {title}" if title else ""

    sents = [s.strip() for s in _SENT_SPLIT.split(summary) if s.strip()]
    scored = [(i, s, _score_sentence(s, i)) for i, s in enumerate(sents)]

    keep = []
    if sents:
        keep.append((0, sents[0]))
    for idx, s, sc in sorted(scored[1:], key=lambda t: -t[2]):
        keep.append((idx, s))
    keep = sorted(keep, key=lambda t: t[0])

    body_parts = []
    used = len(head) + 2
    for _, s in keep:
        if used + len(s) + 1 > max_chars:
            break
        body_parts.append(s)
        used += len(s) + 1
    body = " ".join(body_parts)

    if design and used + 16 < max_chars:
        room = max_chars - used - 10
        if room > 0:
            body = body + f" Design: {design[:room]}"

    if head and body:
        out = (head + ". " + body).strip()
    else:
        out = (head or body).strip()
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


def get_or_build_compressed(
    cache: GSEContextCache,
    gse: str,
    gse_title: str,
    gse_summary: str,
    gse_design: str = "",
    max_chars: int = 512,
) -> str:
    """Cache-aware wrapper. Reuses the stored compressed summary unless
    the source content changed (compared by sha256 of the inputs)."""
    src = "\x1f".join([gse_title or "", gse_summary or "", gse_design or ""])
    full_hash = _hash(src)
    cached = cache.get_compressed_summary(gse)
    if cached and cached.get("full_hash") == full_hash:
        return cached["summary_compressed"]
    out = compress_gse_meta(gse_title, gse_summary, gse_design, max_chars=max_chars)
    cache.set_compressed_summary(gse, full_hash, out, n_chars_in=len(src))
    return out
