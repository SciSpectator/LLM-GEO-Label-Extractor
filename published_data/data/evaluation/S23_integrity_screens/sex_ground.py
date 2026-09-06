"""General Sex-grounding rule (post-extraction correction).

Sex is a VERBATIM field: a definite Male/Female must be supported by an actual
sex token in the sample's OWN metadata. If none appears, the value was INFERRED
(from a cell line's known origin, from world knowledge, etc.) — which violates
the verbatim/no-inference policy — so it is forced to 'Not Specified'.

General rule, not a cell-line allow/deny list: it keys only on whether a sex
word is present in the sample text, so it catches every inference path.
"""

import re

_SEX_TOKEN = re.compile(
    r"\b(?:males?|females?|men|women|man|woman|boys?|girls?)\b"
    r"|(?:^|[;\t|,])\s*(?:[a-z]+[\s_-]+){0,3}(?:sex|gender)\s*[:=]"
    r"|(?:^|[;\t|,])\s*(?:patient|donor|subject|individual|pt)\s*[:=]\s*[MF]\b"
    r"|\b[MF]\s*/\s*[MF]\b",
    re.IGNORECASE,
)
_RAW_FIELDS = (
    "characteristics",
    "source",
    "title",
    "treatment_protocol",
    "description",
    "characteristics_ch1",
    "source_name_ch1",
    "treatment_protocol_ch1",
)


def sex_is_grounded(sample: dict) -> bool:
    raw = " ".join(str(sample.get(k) or "") for k in _RAW_FIELDS)
    return bool(_SEX_TOKEN.search(raw))


def apply_sex_grounding(sample: dict) -> bool:
    """Force Sex->'Not Specified' in phase1b AND phase2 when ungrounded.
    Returns True if it changed anything."""
    changed = False
    for phase in ("phase1b", "phase2"):
        d = sample.get(phase)
        if isinstance(d, dict) and d.get("Sex") in ("Male", "Female"):
            if not sex_is_grounded(sample):
                d["Sex"] = "Not Specified"
                changed = True
    return changed
