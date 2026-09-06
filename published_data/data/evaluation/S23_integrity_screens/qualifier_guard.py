#!/usr/bin/env python3
"""Refuse controlled-vocabulary normalisation for values that name no concept.

    A value such as 'stage III' or 'grade 2' measures a finding instead of
    naming one. Resolving it to a disease descriptor asserts a diagnosis the
    record does not carry. The test is structural rather than a term list:
    every token must be scaffolding or a code, a code must be present, and a
    scaffolding word must be present, which keeps bare codes such as p53 or
    CD4 on the normal path.
    """

from __future__ import annotations

import re

# Scaffolding words: they qualify or quantify a finding without naming one.
# Grouped only for readability; membership is what matters.
_SCAFFOLD = {
    # the measurement itself
    "stage", "stages", "staging", "grade", "grades", "grading", "score",
    "scores", "scoring", "group", "groups", "class", "classification",
    "category", "categories", "cat", "code", "level", "levels", "index",
    "status", "value", "values", "count", "type",
    # what is being measured on
    "tumor", "tumour", "tumors", "tumours", "node", "nodal", "nodes", "lymph",
    "metastasis", "disease", "clinical", "pathological", "pathologic",
    "patholog", "path", "histological", "histologic", "histology", "nuclear",
    "overall", "final", "initial", "primary", "biopsy", "surgical", "risk",
    "differentiation", "differentiated", "invasion", "margin", "margins",
    "size", "depth", "extent",
    # named scales and staging systems
    "who", "ajcc", "uicc", "figo", "tnm", "inss", "inrg", "siopen", "gleason",
    "isup", "fuhrman", "banff", "braak", "sbr", "nottingham", "elston",
    "bloom", "richardson", "dukes", "astler", "coller", "masaoka", "binet",
    "rai", "ann", "arbor", "lugano", "karnofsky", "ecog", "child", "pugh",
    "damico", "amico", "bcr", "psa",
    # glue
    "of", "the", "and", "or", "in", "at", "by", "to", "with", "per", "total",
    "sum", "plus", "minus", "positive", "negative", "pos", "neg", "yes", "no",
    "not", "specified", "unknown", "na", "nos", "other", "mixed", "combined",
}

# Token shapes that are codes rather than words. A stage designation is a
# compact mix of numerals (arabic or roman) and letter sub-divisions, in no
# fixed order across systems -- IIIB, IB1, pT3a, 4s, G3, 7th. Rather than
# enumerate the systems, a token counts as a code when it is short and its
# letters never form a pronounceable run: at most three consecutive letters
# outside a roman numeral. That admits every notation above, and future ones,
# while an actual word ("carcinoma", "breast") is rejected on its letter run.
_CODE = re.compile(
    r"""^(?:
          [0-9]+(?:\.[0-9]+)?                    # 3, 2.5
        | [ivxIVX]+[a-zA-Z]?[0-9]*               # IV, IIIB, IB1
        | [a-zA-Z]{0,3}[0-9]+[a-zA-Z]{0,3}       # 4a, T2, pT3a, G3, 7th, 4s
        | [a-zA-Z]                               # a single letter: a, b, x, T
        )$""",
    re.X,
)
_MAX_CODE_LEN = 6                 # beyond this a token is prose, not a code

_WORD = re.compile(r"[A-Za-z0-9]+")


def _tokens(value: str):
    return _WORD.findall(value or "")


def is_pure_qualifier(value: str) -> bool:
    """True when the value measures a finding instead of naming one.

    All three conditions must hold, and the third is what makes the guard
    narrow: without it every bare alphanumeric token would be refused.
    """
    toks = _tokens(value)
    if not toks:
        return False
    saw_code = saw_scaffold = False
    for t in toks:
        tl = t.lower()
        if tl in _SCAFFOLD:
            saw_scaffold = True
            continue
        if len(t) <= _MAX_CODE_LEN and _CODE.match(t):
            saw_code = True
            continue
        return False                      # a content word -> normalise as usual
    return saw_code and saw_scaffold


def has_content_term(value: str) -> bool:
    """Convenience inverse used at call sites that read better positively."""
    return not is_pure_qualifier(value)
