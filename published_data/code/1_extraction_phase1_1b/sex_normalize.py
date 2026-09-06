"""Canonical surface form for Sex.

Only sex tokens matter. Everything else in the value — a control designation, a
disease, an age, a donor identifier — is ignored rather than treated as
evidence, so a value like "female, control, 47y" still resolves to Female.
"""

from __future__ import annotations

import re
from typing import Tuple

NS = "Not Specified"

_SEX_PREFIX_RE = re.compile(r"^(sex|gender)\s*[:=]\s*", re.IGNORECASE)
_SEX_DIGIT_RE = re.compile(r"^\d+$")

_SEX_VARIANTS = {
    "male": "Male",
    "m": "Male",
    "man": "Male",
    "♂": "Male",
    "female": "Female",
    "f": "Female",
    "woman": "Female",
    "♀": "Female",
    "mixed": "Mixed",
    "pooled": "Mixed",
    "pool": "Mixed",
    "both": "Mixed",
    "unknown": NS,
    "na": NS,
    "n/a": NS,
    "none": NS,
    "-": NS,
    "?": NS,
}


def _is_ns(v) -> bool:
    return v is None or str(v).strip().lower() in ("", NS.lower())


def normalize_sex(v: str) -> Tuple[str, str]:
    """Return (canonical_value, reason). Malformed input becomes NS.

    only male tokens   -> Male
    only female tokens -> Female
    both               -> Mixed
    neither            -> Not Specified
    """
    if _is_ns(v):
        return NS, "ns_input"
    s = _SEX_PREFIX_RE.sub("", str(v).strip()).strip().lower()
    if not s:
        return NS, "empty_after_prefix_strip"

    if _SEX_DIGIT_RE.match(s):
        return NS, "unresolved_coded_value"
    if s in _SEX_VARIANTS:
        norm = _SEX_VARIANTS[s]
        return (
            norm if norm else NS,
            "variant_normalised" if norm else "explicit_unknown",
        )
    has_m = re.search(r"\bmale\b|\bman\b|\bm\b|♂", s) is not None
    has_f = re.search(r"\bfemale\b|\bwoman\b|\bf\b|♀", s) is not None
    if has_m and has_f:
        return "Mixed", "both_sexes_present"
    if has_m:
        return "Male", "male_token_present"
    if has_f:
        return "Female", "female_token_present"
    return NS, "no_sex_token"
